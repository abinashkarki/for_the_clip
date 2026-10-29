"""HD masters and file-level publishing checks; never replace editing evidence."""
import copy
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path

from . import download, media

POLICY = 1
MIN_SIDE = 1080
PRESETS = {(9, 16): (1080, 1920), (16, 9): (1920, 1080), (1, 1): (1080, 1080), (4, 5): (1080, 1350)}
MASTER_FORMAT = ('bestvideo[width>=1080][height>=1080][width<=3840][height<=3840]+bestaudio/'
                 'best[width>=1080][height>=1080][width<=3840][height<=3840]')


class FinalQualityError(RuntimeError):
    pass


def digest(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def probe(path):
    try:
        return media.probe(Path(path))
    except (OSError, RuntimeError, ValueError) as error:
        raise FinalQualityError('Cannot verify video quality: ' + str(error)) from error


def hd(info):
    return (isinstance(info, dict) and all(type(info.get(k)) is int and info[k] >= MIN_SIDE
                                         for k in ('width', 'height')))


def final_timeline(timeline):
    result = copy.deepcopy(timeline)
    canvas = result['canvas']
    for (a, b), (width, height) in PRESETS.items():
        if canvas['width'] * b == canvas['height'] * a:
            if min(canvas['width'], canvas['height']) < MIN_SIDE:
                canvas.update(width=width, height=height)
            return result
    raise FinalQualityError('Final output must use a supported aspect ratio.')


def _check_master(path, reference):
    info = probe(path)
    if not hd(info):
        raise FinalQualityError('A source is below 1080p. Final export requires a high-resolution original, not an upscaled proxy.')
    if abs(info['duration'] - reference['duration']) > .5:
        raise FinalQualityError('The source has changed: the HD original duration differs from the editing source. Your edits are preserved; restore the matching original before publishing.')
    aspect = reference.get('display_width', reference['width']) / reference.get('display_height', reference['height'])
    display_aspect = info.get('display_width', info['width']) / info.get('display_height', info['height'])
    if abs(display_aspect / aspect - 1) > .01:
        raise FinalQualityError('The source has changed: the HD original aspect differs from the editing source. Your edits are preserved; restore the matching original before publishing.')
    if reference.get('has_audio') and not info.get('has_audio'):
        raise FinalQualityError('The HD original is missing the source audio. Final export stopped.')
    return info


def _master(source_id, source, original, info, root):
    url = source.get('source', '')
    if not isinstance(url, str) or not url.startswith(('https://', 'http://')):
        raise FinalQualityError(f"{source.get('title', source_id)} is a low-resolution local file. Add a source with at least 1080 pixels on its short side for final export.")
    provider_id = (source.get('provenance') or {}).get('id')
    if not provider_id:
        raise FinalQualityError('Cannot verify which original matches this proxy. Re-import the high-resolution source before publishing.')
    stat = original.stat()
    identity = {'source_id': source_id, 'url': url, 'provider_id': str(provider_id),
                'range': (source.get('options') or {}).get('range'),
                'proxy': {'path': str(original), 'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns}}
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    cache = Path(root) / 'final-sources' / key
    cache.mkdir(parents=True, exist_ok=True)
    with (cache / 'lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        manifest = cache / 'master.json'
        try:
            saved = json.loads(manifest.read_text())
            path = cache / saved['file']
            if path.parent == cache and saved['identity'] == identity and digest(path) == saved['sha256']:
                return path, _check_master(path, info)
        except (OSError, ValueError, TypeError, KeyError, FinalQualityError):
            pass
        # Separate persistent work directory lets yt-dlp resume interrupted masters.
        try:
            path, remote = download.download_file(url, cache / 'download', stem='master', fmt=MASTER_FORMAT,
                                                  merge=True, range_=identity['range'])
        except Exception as error:
            if 'sign in to confirm' in str(error).lower() or 'not a bot' in str(error).lower():
                raise FinalQualityError('The video site requires sign-in verification before this original can be downloaded.') from error
            raise FinalQualityError('Could not fetch an HD original. Check the link and that a 1080p or higher version is available; then retry final export.') from error
        path = Path(path).resolve()
        if str(remote.get('id')) != str(provider_id):
            raise FinalQualityError('The source has changed: the downloaded original does not match the imported recording. Your edits are preserved; restore the matching original before publishing.')
        high = _check_master(path, info)
        current = original.stat()
        if current.st_size != stat.st_size or current.st_mtime_ns != stat.st_mtime_ns:
            raise FinalQualityError('The editing source changed while preparing its original. Retry from a stable source.')
        target = cache / ('master' + path.suffix)
        os.replace(path, target)
        data = {'identity': identity, 'file': target.name, 'sha256': digest(target)}
        temporary = cache / 'master.json.tmp'
        temporary.write_text(json.dumps(data, sort_keys=True))
        os.replace(temporary, manifest)
        return target, high


def _snapshot(path):
    """Capture bytes and metadata together, rejecting a file changing during the read."""
    try:
        before = path.stat()
        sha256 = digest(path)
        after = path.stat()
    except OSError as error:
        raise FinalQualityError('Cannot read the saved source. Restore it or re-import the recording.') from error
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
        raise FinalQualityError('A source changed while preparing the final render. Retry from stable originals.')
    return {'file': str(path), 'size': after.st_size, 'mtime_ns': after.st_mtime_ns,
            'sha256': sha256}


def prepare_sources(timeline, sources, root, progress=None):
    """Return render-only source copies plus provenance. No saved source is mutated."""
    result = dict(sources)
    proof = {}
    ids = dict.fromkeys(item['source_id'] for key in ('segments', 'broll') for item in timeline.get(key, []))
    for source_id in ids:
        source = sources[source_id]
        original = Path(source['source_path']).resolve()
        original_snapshot = _snapshot(original)
        provenance = source.get('provenance') or {}
        marked_master = any(key in provenance for key in ('master_version', 'master_sha256', 'master_size_bytes'))
        if marked_master and (provenance.get('master_version') != 1
                              or provenance.get('master_sha256') != original_snapshot['sha256']
                              or provenance.get('master_size_bytes') != original_snapshot['size']):
            raise FinalQualityError('The saved source has changed and no longer matches the imported master. Your edits are preserved; restore the matching source before final export.')
        info = probe(original)
        path = original
        if not hd(info):
            if marked_master:
                raise FinalQualityError('The saved source is below 1080p. Final export requires a higher-resolution source; re-import one to publish.')
            if progress:
                progress(f"Fetching HD original: {source.get('title', source_id)}")
            path, info = _master(source_id, source, original, info, root)
        if path != original:
            # Fetching an HD master takes a while: the editing source must not have changed meanwhile.
            # Without a fetch nothing happened in between, and a second full read would only cost time.
            verify_sources({'original': original_snapshot})
        snapshot = original_snapshot if path == original else _snapshot(path)
        result[source_id] = {**source, 'source_path': str(path), 'source_info': info}
        proof[source_id] = {'width': info['width'], 'height': info['height'], 'duration': info['duration'],
                            **snapshot}
        if path != original:
            proof[source_id]['editing_source'] = original_snapshot
    return result, proof


def verify_sources(proof):
    for source in proof.values():
        try:
            current = _snapshot(Path(source['file']))
        except (KeyError, TypeError, FinalQualityError) as error:
            raise FinalQualityError('A source changed during the final render. Render again from stable originals.') from error
        if any(current[key] != source.get(key) for key in ('size', 'mtime_ns', 'sha256')):
            raise FinalQualityError('A source changed during the final render. Render again from stable originals.')
        if 'editing_source' in source:
            verify_sources({'editing_source': source['editing_source']})


def verify_artifact(artifact, *, short_id=None, revision_id=None):
    """Reject before upload, including legacy/resumed artifacts; labels are not evidence."""
    proof = artifact.get('final_quality')
    if not isinstance(proof, dict):
        proof = {}
    source_proof = proof.get('sources')
    if (artifact.get('quality') != '1080p' or proof.get('policy') != POLICY
            or not isinstance(source_proof, dict) or not source_proof
            or not all(hd(info) for info in source_proof.values())):
        raise FinalQualityError('This export has no verified HD-original provenance. Render a new final export before publishing.')
    if ((short_id is not None and artifact.get('clip_id') != short_id)
            or (revision_id is not None and artifact.get('revision_id') != revision_id)):
        raise FinalQualityError('The export does not match the edit being published. Render that exact revision again.')
    path = Path(artifact.get('video_path') or '')
    info = probe(path)
    expected = proof.get('canvas')
    if not isinstance(expected, dict):
        expected = {}
    if (not hd(info) or (info['width'], info['height']) != (expected.get('width'), expected.get('height'))
            or (info['width'], info['height']) != (artifact.get('width'), artifact.get('height'))):
        raise FinalQualityError('The actual export is not the verified final resolution. Render a new final export; low-resolution files cannot be published.')
    if (type(proof.get('duration')) not in (int, float) or not math.isfinite(proof['duration']) or proof['duration'] <= 0
            or abs(info['duration'] - proof['duration']) > .5):
        raise FinalQualityError('The exported duration does not match the final edit. Render it again before publishing.')
    try:
        valid = bool(artifact.get('sha256')) and digest(path) == artifact['sha256']
    except OSError:
        valid = False
    if not valid:
        raise FinalQualityError('The exported file changed after rendering. Render it again before publishing.')
    return info
