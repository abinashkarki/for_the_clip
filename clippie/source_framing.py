"""Bounded local static crop recommendations; callers retain manual crop priority.

Sparse stills and scene metrics cannot establish continuous subject safety. This
module never identifies people, tracks subjects, or changes a source or plan.
"""
from __future__ import annotations

import json
import math
import os
import platform
import re
import shutil
import sys
import tempfile
from pathlib import Path

from . import render, source_visuals
from .collection_visuals import source_snapshot
from .source_evidence import _hash

VERSION = 1
LIMITS = {"units": 8, "stills": 24, "selected_seconds": 180}
CONFIG = {"scene_threshold": .18, "face_margin": .06, "stability": .04,
          "target_aspect": 9 / 16, "sample_version": source_visuals.IMPLEMENTATION_VERSION}
FIT = {"mode": "fit", "zoom": 1, "x": .5, "y": .5}
_SWIFT = r'''
import Foundation
import Vision
import ImageIO
let url = URL(fileURLWithPath: CommandLine.arguments[1])
let handler = VNImageRequestHandler(url: url, options: [:])
let faces = VNDetectFaceRectanglesRequest()
let text = VNDetectTextRectanglesRequest()
try handler.perform([faces, text])
let boxes = (faces.results ?? []).map { r -> [Double] in
 let b = r.boundingBox
 return [Double(b.minX), Double(1-b.maxY), Double(b.width), Double(b.height)]
}
let out: [String: Any] = ["faces": boxes, "layout_uncertain": !(text.results ?? []).isEmpty]
let data = try JSONSerialization.data(withJSONObject: out, options: [.sortedKeys])
print(String(data: data, encoding: .utf8)!)
'''


def _safe_root(path):
    path = Path(path).expanduser().absolute()
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("Unsafe cache symlink")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _native_backend(work):
    """Compile only embedded trusted code; never execute a cached executable."""
    if sys.platform != "darwin" or not shutil.which("swiftc"):
        return None
    src, exe = work / "faces.swift", work / "faces"
    src.write_text(_SWIFT)
    render._run([shutil.which("swiftc"), str(src), "-o", str(exe)],
                timeout=45, what="Local Vision helper compilation")
    def detect(path):
        result = render._run([str(exe), str(path)], timeout=10, what="Local Vision still analysis")
        return json.loads(result.stdout)
    return detect


def _scene_changes(media, start, end):
    # Evaluate all decoded frames in the bounded unit, including short cuts
    # between our three stills. No fps reduction ahead of scene measurement.
    result = render._run([render._binary("ffmpeg", "CLIPPIE_FFMPEG"),
        "-hide_banner", "-loglevel", "info", "-nostdin", "-ss", str(start),
        "-i", str(media), "-t", str(end-start), "-an", "-vf",
        f"scale=320:-2,select='gt(scene,{CONFIG['scene_threshold']})',showinfo",
        "-f", "null", "-"], timeout=35, what="Local shot change analysis")
    return [float(x) for x in re.findall(r"pts_time:([0-9.]+)", result.stderr)
            if 0 < float(x) < end-start]


def _decision(samples, cuts):
    if len(samples) != 3:
        return dict(FIT), "incomplete_samples"
    if cuts:
        return dict(FIT), "internal_shot_change"
    if any(s.get("layout_uncertain", True) for s in samples):
        return dict(FIT), "text_or_layout_uncertain"
    if any(len(s.get("faces", [])) != 1 for s in samples):
        return dict(FIT), "no_or_multiple_faces"
    boxes = [s["faces"][0] for s in samples]
    if any(len(b) != 4 or any(not isinstance(v, (int, float)) or not math.isfinite(v) for v in b)
           or min(b) < 0 or b[2] <= 0 or b[3] <= 0 or b[0]+b[2] > 1 or b[1]+b[3] > 1 for b in boxes):
        return dict(FIT), "invalid_face_geometry"
    if any(b[2] < .08 or b[3] < .12 for b in boxes):
        return dict(FIT), "tiny_face_layout_uncertain"
    if any(max(b[i] for b in boxes)-min(b[i] for b in boxes) > CONFIG['stability'] for i in range(4)):
        return dict(FIT), "unstable_faces"
    # Match render's max scale and (scaled_size-target)*x crop offset.
    aspect = samples[0]["width"] / samples[0]["height"]
    cw, ch = min(1., CONFIG['target_aspect']/aspect), min(1., aspect/CONFIG['target_aspect'])
    cx = sum(b[0]+b[2]/2 for b in boxes)/3
    cy = sum(b[1]+b[3]/2 for b in boxes)/3
    left = max(0., min(1-cw, cx-cw/2)); top = max(0., min(1-ch, cy-ch/2))
    margin = CONFIG['face_margin']
    if any(b[0]-margin < left or b[0]+b[2]+margin > left+cw
           or b[1]-margin < top or b[1]+b[3]+margin > top+ch for b in boxes):
        return dict(FIT), "face_margin_outside_crop"
    return {"mode": "fill", "zoom": 1, "x": left/(1-cw) if cw < 1 else .5,
            "y": top/(1-ch) if ch < 1 else .5}, "three_stable_single_face_samples_no_detected_cut"


def recommend_framing(source, units, cache_root, process_owner=None):
    """Return ordered recommendations for {id,start,end} units of ONE source.

    Errors yield fit with evidence status. Cancellation checkpoints propagate.
    Cache is advisory local evidence, versioned by stat identity/config/backend.
    """
    rows = []
    result = {"version": VERSION, "limits": dict(LIMITS), "units": rows,
              "backend": "unavailable", "limitation": "Three stills plus scene metric; no tracking or continuous safety guarantee."}
    elapsed = 0.
    with render.process_ownership(process_owner):
        with tempfile.TemporaryDirectory(prefix="clippie-framing-") as tmp:
            try:
                root = _safe_root(cache_root)
                raw = Path(source['source_path']).expanduser().absolute()
                if any(p.is_symlink() for p in (raw, *raw.parents)):
                    raise ValueError("Unsafe source symlink")
                identity = source_snapshot(source)
                detector = _native_backend(Path(tmp))
                result['backend'] = 'macos_vision' if detector else 'unavailable'
            except Exception:
                detector = None
            for index, unit in enumerate(units):
                source_visuals._checkpoint(process_owner)
                row = {"unit_id": unit.get('id'), "framing": dict(FIT), "reason": "backend_unavailable",
                       "cache_status": "none", "evidence": {}}
                rows.append(row)
                try:
                    start, end = unit['start'], unit['end']
                    if (any(isinstance(v, bool) or not isinstance(v, (int,float)) or not math.isfinite(v) for v in (start,end))
                        or not 0 <= start < end <= source['duration']):
                        raise ValueError('Invalid unit bounds')
                    if index >= LIMITS['units'] or elapsed + end-start > LIMITS['selected_seconds']:
                        row['reason'] = 'workload_limit'; continue
                    elapsed += end-start
                    if detector is None:
                        continue
                    key = _hash([VERSION, identity, start, end, CONFIG, result['backend'], platform.mac_ver()[0], _hash(_SWIFT)])
                    path = root / f'framing-{key}.json'
                    if path.is_symlink():
                        raise ValueError('Unsafe evidence cache')
                    evidence = None
                    if path.is_file():
                        cached = json.loads(path.read_text())
                        if cached.get('key') == key and cached.get('digest') == _hash(cached.get('evidence')):
                            evidence = cached['evidence']; row['cache_status'] = 'hit'
                    if evidence is None:
                        times = [start, (start+end)/2, end-min(.05, (end-start)/4)]
                        prepared = source_visuals.prepare_samples(source, root, times, process_owner)
                        samples = []
                        for sample in prepared['artifact']['samples']:
                            source_visuals._checkpoint(process_owner)
                            detected = detector(sample['path'])
                            samples.append(dict(detected, at=sample['at'], width=sample['width'], height=sample['height']))
                        evidence = {'samples': samples, 'cuts': _scene_changes(raw, start, end),
                                    'source_identity': identity, 'sample_artifact_key': prepared['cache_artifact_key']}
                        if source_snapshot(source) != identity:
                            raise ValueError('Source changed during analysis')
                        fd, temporary = tempfile.mkstemp(prefix='.framing-', dir=root)
                        try:
                            with os.fdopen(fd, 'w') as handle:
                                json.dump({'key': key, 'evidence': evidence, 'digest': _hash(evidence)}, handle, allow_nan=False)
                                handle.flush(); os.fsync(handle.fileno())
                            os.replace(temporary, path)
                        finally:
                            if os.path.exists(temporary): os.unlink(temporary)
                        row['cache_status'] = 'build'
                    row['framing'], row['reason'] = _decision(evidence['samples'], evidence['cuts'])
                    row['evidence'] = evidence
                except Exception:
                    source_visuals._checkpoint(process_owner)
                    row.update(framing=dict(FIT), reason='analysis_failed', cache_status='failed')
    return result
