"""Caption templates, hook styles and the deterministic page/line layout (docs/v3/CAPTION_TEMPLATES.md).

Templates are data (`clippie/templates/captions/*.json`). `layout` makes every layout decision (pages,
line wraps, active word, animation progress) as plain numbers so the Pillow compositor and the browser
preview can be checked against identical golden fixtures. Measurement assumptions:

* sizes are authored for 1080 px width and scaled by `canvas_w / 1080`;
* `measure(text, size_px) -> width_px` is the advance width of `text` set in the template font at `size_px`
  (Pillow BASIC layout, kerning included, no stroke, no letter spacing);
* caption gaps are max(space advance, two stroke radii + 0.12 em), shared by wrapping and placement;
  hook text retains natural spaces. Wrapping never depends on cross-space kerning;
* vertical placement uses the font's ascent/descent (em fractions stored as `font.metrics`).
"""

from __future__ import annotations

import bisect
import copy
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Iterable

from .paths import resource_root

ROOT = resource_root()
FONT_DIR = ROOT / "assets" / "fonts"
TEMPLATE_DIR = ROOT / "templates" / "captions"
HOOK_DIR = ROOT / "templates" / "hooks"

VISIBLE_TAIL = 0.6
COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}([0-9A-Fa-f]{2})?$")
COLOR_KEYS = ("text", "active", "emphasis", "stroke", "box", "active_box")
NULLABLE_COLORS = ("box", "active_box")
CASES = ("none", "upper", "lower")
ACTIVE_STYLES = ("none", "color", "box", "scale", "color_box")
ANIMATIONS = ("none", "fade", "pop", "slide_up")
SLIDE_PX = 24.0
POP_FROM = 0.85
# Override-able keys (CAPTION_TEMPLATES.md); `font` is special-cased (family+file together).
OVERRIDABLE: dict[str, Any] = {
    "font": {"family", "file", "size"}, "case": None, "position": {"y"}, "colors": set(COLOR_KEYS),
    "stroke_width": None, "active_word": {"style"}, "animation": {"in"}, "grouping": {"max_words"},
}


class TemplateError(ValueError):
    """Invalid template, hook style or override; `.path` locates the field."""

    def __init__(self, message: str, path: str = ""):
        super().__init__(f"{path}: {message}" if path else message)
        self.path = path


def _fail(path: str, message: str):
    raise TemplateError(message, path)


def _number(value: Any, path: str, low: float, high: float, *, integer: bool = False) -> float:
    ok = isinstance(value, (int, float)) and not isinstance(value, bool)
    if integer:
        ok = isinstance(value, int) and not isinstance(value, bool)
    if not ok or value != value or not low <= value <= high:
        _fail(path, f"must be {'an integer' if integer else 'a number'} from {low} to {high}")
    return value


def _color(value: Any, path: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not COLOR_RE.match(value):
        _fail(path, "must be #RRGGBB or #RRGGBBAA")
    return value.upper()


def parse_color(value: str) -> tuple[int, int, int, int]:
    value = value.lstrip("#")
    alpha = int(value[6:8], 16) if len(value) == 8 else 255
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16), alpha


def _object(value: Any, path: str) -> dict:
    if not isinstance(value, dict):
        _fail(path, "must be an object")
    return value


def _font(raw: Any, path: str, *, with_size: bool) -> dict:
    raw = _object(raw, path)
    family, file = raw.get("family"), raw.get("file")
    if not isinstance(family, str) or not 1 <= len(family) <= 60:
        _fail(f"{path}.family", "must be text of 1-60 characters")
    if not isinstance(file, str) or Path(file).name != file or not (FONT_DIR / file).is_file():
        _fail(f"{path}.file", "must name a font file in clippie/assets/fonts")
    font: dict[str, Any] = {"family": family, "file": file}
    if with_size:
        font["size"] = _number(raw.get("size"), f"{path}.size", 24, 160)
    return font


def _shadow(raw: Any, path: str) -> dict:
    raw = _object(raw if raw is not None else {}, path)
    return {"dx": _number(raw.get("dx", 0), f"{path}.dx", -60, 60), "dy": _number(raw.get("dy", 0), f"{path}.dy", -60, 60),
            "blur": _number(raw.get("blur", 0), f"{path}.blur", 0, 60),
            "color": _color(raw.get("color", "#00000000"), f"{path}.color")}


def validate_template(raw: Any, expected_id: str | None = None) -> dict:
    """Return the canonical template (defaults filled, derived `font.metrics` added)."""
    raw = _object(raw, "template")
    tid = raw.get("id")
    if not isinstance(tid, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", tid) or (expected_id and tid != expected_id):
        _fail("id", "must be a lowercase identifier matching its file name")
    name = raw.get("name", tid)
    if not isinstance(name, str) or not 1 <= len(name) <= 60:
        _fail("name", "must be text of 1-60 characters")
    font = _font(raw.get("font"), "font", with_size=True)
    if raw.get("case", "none") not in CASES:
        _fail("case", f"must be one of {', '.join(CASES)}")
    grouping_in = _object(raw.get("grouping"), "grouping")
    grouping = {"max_words": int(_number(grouping_in.get("max_words"), "grouping.max_words", 1, 12, integer=True)),
                "max_chars": int(_number(grouping_in.get("max_chars"), "grouping.max_chars", 1, 80, integer=True)),
                "max_lines": int(_number(grouping_in.get("max_lines"), "grouping.max_lines", 1, 4, integer=True)),
                "break_on_pause": float(_number(grouping_in.get("break_on_pause"), "grouping.break_on_pause", 0.05, 5))}
    pos_in = _object(raw.get("position"), "position")
    position = {"y": float(_number(pos_in.get("y"), "position.y", 0.05, 0.95)),
                "max_width": float(_number(pos_in.get("max_width"), "position.max_width", 0.3, 1.0))}
    colors_in = _object(raw.get("colors"), "colors")
    colors = {key: _color(colors_in.get(key), f"colors.{key}", nullable=key in NULLABLE_COLORS) for key in COLOR_KEYS}
    active_in = _object(raw.get("active_word", {"style": "none", "scale": 1.0}), "active_word")
    if active_in.get("style", "none") not in ACTIVE_STYLES:
        _fail("active_word.style", f"must be one of {', '.join(ACTIVE_STYLES)}")
    anim_in = _object(raw.get("animation", {"in": "none", "duration_ms": 0}), "animation")
    if anim_in.get("in", "none") not in ANIMATIONS:
        _fail("animation.in", f"must be one of {', '.join(ANIMATIONS)}")
    result = {
        "id": tid, "name": name, "font": font, "case": raw.get("case", "none"), "grouping": grouping,
        "position": position, "colors": colors,
        "stroke_width": float(_number(raw.get("stroke_width", 0), "stroke_width", 0, 30)),
        "shadow": _shadow(raw.get("shadow"), "shadow"),
        "active_word": {"style": active_in.get("style", "none"),
                        "scale": float(_number(active_in.get("scale", 1.0), "active_word.scale", 1.0, 1.4))},
        "animation": {"in": anim_in.get("in", "none"),
                      "duration_ms": int(_number(anim_in.get("duration_ms", 0), "animation.duration_ms", 0, 1000, integer=True))},
        "line_height": float(_number(raw.get("line_height", 1.15), "line_height", 0.8, 2.0)),
    }
    result["font"]["metrics"] = font_metrics(font["file"])
    return result


def validate_hook(raw: Any, expected_id: str | None = None) -> dict:
    raw = _object(raw, "hook")
    hid = raw.get("id")
    if not isinstance(hid, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", hid) or (expected_id and hid != expected_id):
        _fail("id", "must be a lowercase identifier matching its file name")
    colors_in = _object(raw.get("colors"), "colors")
    colors = {"text": _color(colors_in.get("text"), "colors.text"),
              "box": _color(colors_in.get("box"), "colors.box", nullable=True),
              "stroke": _color(colors_in.get("stroke", "#000000"), "colors.stroke")}
    anim_in = _object(raw.get("animation", {"in": "none", "duration_ms": 0}), "animation")
    if anim_in.get("in", "none") not in ANIMATIONS:
        _fail("animation.in", f"must be one of {', '.join(ANIMATIONS)}")
    if raw.get("case", "none") not in CASES:
        _fail("case", f"must be one of {', '.join(CASES)}")
    font = _font(raw.get("font"), "font", with_size=False)
    font["metrics"] = font_metrics(font["file"])
    return {
        "id": hid, "name": str(raw.get("name", hid))[:60], "font": font,
        "size": float(_number(raw.get("size"), "size", 24, 160)), "case": raw.get("case", "none"), "colors": colors,
        "stroke_width": float(_number(raw.get("stroke_width", 0), "stroke_width", 0, 30)),
        "shadow": _shadow(raw.get("shadow"), "shadow"),
        "position": {"y": float(_number(raw.get("position", {}).get("y"), "position.y", 0.03, 0.97))},
        "max_width": float(_number(raw.get("max_width"), "max_width", 0.3, 1.0)),
        "padding": float(_number(raw.get("padding", 0), "padding", 0, 120)),
        "radius": float(_number(raw.get("radius", 0), "radius", 0, 120)),
        "band": bool(raw.get("band", False)),
        "line_height": float(_number(raw.get("line_height", 1.15), "line_height", 0.8, 2.0)),
        "animation": {"in": anim_in.get("in", "none"),
                      "duration_ms": int(_number(anim_in.get("duration_ms", 0), "animation.duration_ms", 0, 1000, integer=True))},
    }


# ---------------------------------------------------------------- fonts and measurement

@lru_cache(maxsize=64)
def pil_font(file: str, size: float):
    from PIL import ImageFont

    return ImageFont.truetype(str(FONT_DIR / file), size=round(float(size), 2), layout_engine=ImageFont.Layout.BASIC)


METRICS_EM = 16384


@lru_cache(maxsize=16)
def font_metrics(file: str) -> dict:
    # FreeType reports whole pixels, so a large em keeps the ratios exact to about five digits.
    ascent, descent = pil_font(file, METRICS_EM).getmetrics()
    return {"ascent": ascent / METRICS_EM, "descent": descent / METRICS_EM}


def pil_measure(font_file: str) -> Callable[[str, float], float]:
    """Reference `measure(text, size_px)` implementation used for export and golden fixtures."""

    def measure(text: str, size: float) -> float:
        return float(pil_font(font_file, size).getlength(text))

    return measure


# ---------------------------------------------------------------- registry

@lru_cache(maxsize=1)
def _load_cached() -> tuple[dict, dict]:
    captions = {}
    for path in sorted(TEMPLATE_DIR.glob("*.json")):
        captions[path.stem] = validate_template(json.loads(path.read_text(encoding="utf-8")), path.stem)
    hooks = {}
    for path in sorted(HOOK_DIR.glob("*.json")):
        hooks[path.stem] = validate_hook(json.loads(path.read_text(encoding="utf-8")), path.stem)
    return captions, hooks


def load() -> dict:
    captions, hooks = _load_cached()
    return {"captions": copy.deepcopy(captions), "hooks": copy.deepcopy(hooks)}


def hook_style(style_id: str) -> dict:
    hooks = _load_cached()[1]
    if style_id not in hooks:
        _fail("style", f"unknown hook style {style_id!r}")
    return copy.deepcopy(hooks[style_id])


def _apply_overrides(template: dict, overrides: dict) -> dict:
    merged = copy.deepcopy(template)
    merged["font"].pop("metrics", None)
    for key, value in overrides.items():
        path = f"overrides.{key}"
        if key not in OVERRIDABLE:
            _fail(path, "is not an override-able key")
        allowed = OVERRIDABLE[key]
        if allowed is None:
            merged[key] = value
            continue
        value = _object(value, path)
        for sub, sub_value in value.items():
            if sub not in allowed:
                _fail(f"{path}.{sub}", "is not an override-able key")
            merged[key][sub] = sub_value
        if key == "font" and ("family" in value) != ("file" in value):
            _fail(path, "font.family and font.file must be overridden together")
    return merged


def resolve(template_id: str, overrides: dict | None = None) -> dict:
    """Merge override-able keys onto a registry template and validate the result."""
    captions = _load_cached()[0]
    if not isinstance(template_id, str) or template_id not in captions:
        _fail("template_id", f"unknown caption template {template_id!r}")
    if overrides is not None and not isinstance(overrides, dict):
        _fail("overrides", "must be an object")
    merged = _apply_overrides(captions[template_id], overrides or {})
    return validate_template(merged, template_id)


# ---------------------------------------------------------------- layout

def cased(text: str, case: str) -> str:
    return text.upper() if case == "upper" else text.lower() if case == "lower" else text


def ease_out(p: float) -> float:
    return 1.0 - (1.0 - p) ** 3


def animation_state(kind: str, progress: float, scale: float = 1.0) -> dict:
    """Transform at animation progress `progress` in [0, 1]: `scale`, `alpha`, `dy` (px at canvas scale)."""
    e = ease_out(progress)
    if kind == "pop":
        return {"scale": POP_FROM + (1 - POP_FROM) * e, "alpha": 1.0, "dy": 0.0}
    if kind == "fade":
        return {"scale": 1.0, "alpha": e, "dy": 0.0}
    if kind == "slide_up":
        return {"scale": 1.0, "alpha": 1.0, "dy": SLIDE_PX * scale * (1.0 - e)}
    return {"scale": 1.0, "alpha": 1.0, "dy": 0.0}


def _group(items: list[dict], grouping: dict, boundaries: list[float]) -> list[list[dict]]:
    groups: list[list[dict]] = []
    page: list[dict] = []
    chars = 0
    for item in items:
        if page:
            prev = page[-1]
            crosses = bisect.bisect_right(boundaries, item["start"] + 1e-9) > bisect.bisect_right(boundaries, prev["start"] + 1e-9)
            if (len(page) + 1 > grouping["max_words"] or chars + len(item["text"]) > grouping["max_chars"]
                    or item["start"] - prev["end"] >= grouping["break_on_pause"] - 1e-9 or crosses):
                groups.append(page)
                page, chars = [], 0
        page.append(item)
        chars += len(item["text"])
    if page:
        groups.append(page)
    return groups


def wrap_lines(texts: list[str], measure: Callable[[str, float], float], size: float, max_width: float,
               *, gap: float | None = None) -> list[list[int]]:
    """Greedy wrap: returns word indexes per line."""
    space = measure(" ", size) if gap is None else gap
    widths = [measure(text, size) for text in texts]
    lines: list[list[int]] = []
    current: list[int] = []
    current_w = 0.0
    for index, width in enumerate(widths):
        needed = width + (space if current else 0.0)
        if current and current_w + needed > max_width + 1e-6:
            lines.append(current)
            current, current_w, needed = [], 0.0, width
        current.append(index)
        current_w += needed
    if current:
        lines.append(current)
    return lines


def _page_geometry(group: list[dict], template: dict, canvas_w: int, canvas_h: int,
                   measure: Callable[[str, float], float]) -> tuple[dict, list[dict]]:
    scale = canvas_w / 1080
    base = template["font"]["size"] * scale
    max_w = template["position"]["max_width"] * canvas_w
    widest = max(measure(item["text"], base) for item in group)
    size = base * min(1.0, max_w / widest) if widest > 0 else base
    space = max(measure(" ", size), 2 * template["stroke_width"] * scale + size * 0.12)
    lines = wrap_lines([item["text"] for item in group], measure, size, max_w, gap=space)
    rest: list[dict] = []
    max_lines = template["grouping"]["max_lines"]
    if len(lines) > max_lines:
        keep = sum(len(line) for line in lines[:max_lines])
        rest = group[keep:]
        group = group[:keep]
        lines = lines[:max_lines]
    metrics = template["font"]["metrics"]
    line_h = size * template["line_height"]
    block_h = line_h * len(lines)
    top = min(max(template["position"]["y"] * canvas_h - block_h / 2, 0.0), max(0.0, canvas_h - block_h))
    out_lines = []
    for row, indexes in enumerate(lines):
        widths = [measure(group[i]["text"], size) for i in indexes]
        line_w = sum(widths) + space * (len(indexes) - 1)
        x = (canvas_w - line_w) / 2
        line_top = top + row * line_h
        baseline = line_top + (line_h - (metrics["ascent"] + metrics["descent"]) * size) / 2 + metrics["ascent"] * size
        words = []
        for i, width in zip(indexes, widths):
            item = group[i]
            words.append({"index": i, "key": item["key"], "text": item["text"], "x": x, "w": width,
                          "emphasis": item["emphasis"]})
            x += width + space
        out_lines.append({"text": " ".join(w["text"] for w in words), "x": (canvas_w - line_w) / 2, "y": line_top,
                          "w": line_w, "baseline": baseline, "words": words})
    page = {"font_size": size, "line_height_px": line_h, "lines": out_lines,
            "block": {"x": min(l["x"] for l in out_lines), "y": top, "w": max(l["w"] for l in out_lines), "h": block_h},
            "words": [{"key": w["key"], "start": w["start"], "end": w["end"]} for w in group]}
    return page, rest


def pages(template: dict, words: Iterable[dict], canvas_w: int, canvas_h: int,
          measure: Callable[[str, float], float], *, boundaries: Iterable[float] = ()) -> list[dict]:
    """All caption pages with static geometry. `boundaries` are program times where a kept range starts."""
    case = template["case"]
    items = [{"key": w["key"], "text": cased(str(w["text"]), case), "start": float(w["start"]), "end": float(w["end"]),
              "emphasis": bool(w.get("emphasis"))}
             for w in sorted(words, key=lambda w: (w["start"], w["end"])) if not w.get("hidden") and str(w["text"]).strip()]
    queue = _group(items, template["grouping"], sorted(float(b) for b in boundaries))
    result: list[dict] = []
    while queue:
        group = queue.pop(0)
        page, rest = _page_geometry(group, template, canvas_w, canvas_h, measure)
        if rest:
            queue.insert(0, rest)
        page["index"] = len(result)
        page["start"] = page["words"][0]["start"]
        page["last_end"] = max(w["end"] for w in page["words"])
        result.append(page)
    for page, following in zip(result, result[1:] + [None]):
        stop = page["last_end"] + VISIBLE_TAIL
        page["visible_end"] = min(stop, following["start"]) if following else stop
    return result


def layout_at(template: dict, page_list: list[dict], t: float) -> dict:
    """The layout at program time `t` from precomputed `pages` output."""
    starts = [p["start"] for p in page_list]
    i = bisect.bisect_right(starts, t + 1e-9) - 1
    if i < 0 or t >= page_list[i]["visible_end"] - 1e-9:
        return {"t": t, "template_id": template["id"], "page": None}
    page = copy.deepcopy(page_list[i])
    word_starts = [w["start"] for w in page["words"]]
    style = template["active_word"]["style"]
    active = max(0, bisect.bisect_right(word_starts, t + 1e-9) - 1) if style != "none" else -1
    duration = template["animation"]["duration_ms"] / 1000
    kind = template["animation"]["in"]
    progress = 1.0 if kind == "none" or duration <= 0 else min(1.0, max(0.0, (t - page["start"]) / duration))
    for line in page["lines"]:
        for word in line["words"]:
            word["active"] = word["index"] == active
            word["scale"] = template["active_word"]["scale"] if word["active"] else 1.0
    page.update({"active": active, "progress": progress, "animation": kind})
    return {"t": t, "template_id": template["id"], "page": page}


def layout(template: dict, words: Iterable[dict], canvas_w: int, canvas_h: int, t: float,
           measure: Callable[[str, float], float], *, boundaries: Iterable[float] = ()) -> dict:
    """Page/line layout at program time `t` (see module docstring for the measurement contract)."""
    return layout_at(template, pages(template, words, canvas_w, canvas_h, measure, boundaries=boundaries), t)


def wrap_text_block(text: str, measure: Callable[[str, float], float], size: float, max_width: float) -> list[str]:
    tokens = text.split()
    return [" ".join(tokens[i] for i in line) for line in wrap_lines(tokens, measure, size, max_width)] or [""]
