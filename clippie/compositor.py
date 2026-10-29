"""Overlay layer for v3: captions, hook text, progress bar and logo drawn with Pillow as RGBA.

`Compositor(timeline, sources, width, height, assets=...)` lays out the whole program once. `scene_at(t)`
describes what is on screen (hashable via `scene_key`), `render_scene` draws it and `frame_plan` returns the
run-length frame schedule (only frames where the overlay changes or animates) that `render_v3` turns into a
concat-demuxer PNG sequence. The progress bar changes every frame, so the renderer draws it with an FFmpeg
overlay expression instead; `draw(t)` includes it for previews and tests.
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

from . import caption_templates as ct
from . import timeline_v3 as tl

BOX_PAD_X = 0.45  # page box padding, em
BOX_PAD_Y = 0.22
BOX_RADIUS = 0.34
ACTIVE_PAD_X = 0.2  # active word box padding, em
ACTIVE_PAD_Y = 0.07
ACTIVE_RADIUS = 0.26
ACTIVE_BOX_LIFT = 0.04  # active box sits slightly above the baseline-centred ink area, em
HOOK_MIN_SCALE = 0.55
HOOK_MAX_LINES = 4
LOGO_MARGIN = 0.04  # fraction of canvas width


def _rgba(color: str | None) -> tuple[int, int, int, int]:
    return ct.parse_color(color) if color else (0, 0, 0, 0)


def _shadow_layer(silhouette: Image.Image, shadow: dict, k: float) -> Image.Image | None:
    r, g, b, a = _rgba(shadow["color"])
    if a == 0:
        return None
    alpha = silhouette.point(lambda v: v * a // 255)
    layer = Image.new("RGBA", silhouette.size, (r, g, b, 0))
    layer.putalpha(alpha)
    if shadow["blur"] > 0:
        layer = layer.filter(ImageFilter.GaussianBlur(shadow["blur"] * k / 2))
    dx, dy = round(shadow["dx"] * k), round(shadow["dy"] * k)
    if dx or dy:
        shifted = Image.new("RGBA", layer.size, (0, 0, 0, 0))
        shifted.paste(layer, (dx, dy))
        layer = shifted
    return layer


def _scale_patch(patch: Image.Image, scale: float, alpha: float) -> Image.Image:
    if abs(scale - 1.0) > 1e-3:
        size = (max(1, round(patch.width * scale)), max(1, round(patch.height * scale)))
        patch = patch.resize(size, Image.Resampling.BICUBIC)
    if alpha < 0.999:
        patch = patch.copy()
        patch.putalpha(patch.getchannel("A").point(lambda v: round(v * alpha)))
    return patch


def _paste_centered(layer: Image.Image, patch: Image.Image, cx: float, cy: float) -> None:
    _paste_clipped(layer, patch, cx, cy)


def _paste_clipped(layer: Image.Image, patch: Image.Image, cx: float, cy: float) -> None:
    x, y = round(cx - patch.width / 2), round(cy - patch.height / 2)
    left, top = max(0, -x), max(0, -y)
    right, bottom = min(patch.width, layer.width - x), min(patch.height, layer.height - y)
    if right > left and bottom > top:
        layer.alpha_composite(patch.crop((left, top, right, bottom)), (x + left, y + top))


class Compositor:
    def __init__(self, timeline: dict, sources: dict, width: int, height: int, *, assets: dict | None = None):
        self.width, self.height = int(width), int(height)
        self.k = self.width / 1080
        self.timeline = timeline
        self.duration = tl.duration(timeline)
        self.warnings: list[str] = []
        captions = timeline["captions"]
        self.captions_enabled = bool(captions["enabled"])
        template_id = captions["template_id"] if self.captions_enabled else "phrase_clean"
        self.template = ct.resolve(template_id, captions["overrides"] if self.captions_enabled else {})
        self.words = tl.program_words(timeline, sources)
        self.boundaries = [r["at"] for r in tl.program_map(timeline)]
        self.measure = ct.pil_measure(self.template["font"]["file"])
        self.pages = ct.pages(self.template, self.words, self.width, self.height, self.measure, boundaries=self.boundaries)
        self.fps = timeline["canvas"]["fps"]
        self.hooks = [self._layout_hook(o) for o in timeline["overlays"] if o["kind"] == "hook"]
        self.bar = next((o for o in timeline["overlays"] if o["kind"] == "progress_bar"), None)
        self.logo = self._load_logo(next((o for o in timeline["overlays"] if o["kind"] == "logo"), None), assets or {})

    # -------------------------------------------------------------- layout

    def _layout_hook(self, overlay: dict) -> dict:
        style = ct.hook_style(overlay["style"])
        measure = ct.pil_measure(style["font"]["file"])
        text = ct.cased(overlay["text"], style["case"])
        max_w = style["max_width"] * self.width - (0 if style["band"] else 2 * style["padding"] * self.k)
        max_w = min(max_w, self.width - 2 * style["padding"] * self.k)
        size = style["size"] * self.k
        lines = ct.wrap_text_block(text, measure, size, max_w)
        while (len(lines) > HOOK_MAX_LINES or max(measure(l, size) for l in lines) > max_w + 1e-6) and \
                size > style["size"] * self.k * HOOK_MIN_SCALE:
            size *= 0.94
            lines = ct.wrap_text_block(text, measure, size, max_w)
        metrics = style["font"]["metrics"]
        line_h = size * style["line_height"]
        pad = style["padding"] * self.k
        text_h = line_h * len(lines)
        box_h = text_h + (2 * pad if style["colors"]["box"] or style["band"] else 0)
        top = min(max(style["position"]["y"] * self.height - box_h / 2, 0.0), max(0.0, self.height - box_h))
        widths = [measure(l, size) for l in lines]
        rows = []
        for i, line in enumerate(lines):
            line_top = top + (pad if style["colors"]["box"] or style["band"] else 0) + i * line_h
            baseline = line_top + (line_h - (metrics["ascent"] + metrics["descent"]) * size) / 2 + metrics["ascent"] * size
            rows.append({"text": line, "w": widths[i], "x": (self.width - widths[i]) / 2, "baseline": baseline})
        box_w = self.width if style["band"] else max(widths) + 2 * pad
        return {"overlay": overlay, "style": style, "size": size, "rows": rows, "top": top, "box_h": box_h, "box_w": box_w,
                "line_h": line_h}

    def _load_logo(self, overlay: dict | None, assets: dict):
        if not overlay:
            return None
        path = assets.get(overlay["asset_id"])
        try:
            if not path:
                raise OSError("no asset path was supplied")
            image = Image.open(Path(path)).convert("RGBA")
        except (OSError, ValueError) as exc:
            self.warnings.append(f"Logo asset {overlay['asset_id']!r} was skipped: {exc}")
            return None
        width = max(1, round(overlay["scale"] * self.width))
        image = image.resize((width, max(1, round(image.height * width / image.width))), Image.Resampling.LANCZOS)
        image.putalpha(image.getchannel("A").point(lambda v: round(v * overlay["opacity"])))
        margin = round(LOGO_MARGIN * self.width)
        anchor = overlay["anchor"]
        x = margin if anchor.endswith("left") else self.width - margin - image.width
        y = margin if anchor.startswith("top") else self.height - margin - image.height
        return {"image": image, "xy": (x, y)}

    # -------------------------------------------------------------- scenes

    def scene_at(self, t: float) -> dict:
        """What is on screen at program time `t` (without the progress bar)."""
        scene: dict = {"page": None, "hook": None}
        if self.captions_enabled and self.pages:
            lay = ct.layout_at(self.template, self.pages, t)
            if lay["page"]:
                page = lay["page"]
                scene["page"] = {"index": page["index"], "active": page["active"], "progress": round(page["progress"], 3)}
        for index, hook in enumerate(self.hooks):
            o = hook["overlay"]
            if o["start"] - 1e-9 <= t < o["end"] - 1e-9:
                anim = hook["style"]["animation"]
                span = anim["duration_ms"] / 1000
                progress = 1.0 if anim["in"] == "none" or span <= 0 else min(1.0, max(0.0, (t - o["start"]) / span))
                scene["hook"] = {"index": index, "progress": round(progress, 3)}
        return scene

    @staticmethod
    def scene_key(scene: dict) -> str:
        page, hook = scene["page"], scene["hook"]
        return (f"p{page['index']}:{page['active']}:{page['progress']}" if page else "p-") + \
               (f"|h{hook['index']}:{hook['progress']}" if hook else "|h-")

    def is_empty(self, scene: dict) -> bool:
        return scene["page"] is None and scene["hook"] is None and self.logo is None

    def frame_plan(self, fps: int | None = None, total_frames: int | None = None) -> list[tuple[int, int, str, dict]]:
        """Run-length schedule `[(first_frame, frame_count, key, scene)]` covering every output frame."""
        fps = fps or self.fps
        total = total_frames if total_frames is not None else math.ceil(self.duration * fps - 1e-6)
        points = {0, total}

        def frame(t: float) -> int:
            return min(total, max(0, round(t * fps)))

        anim = self.template["animation"]
        for page in self.pages:
            if not self.captions_enabled:
                break
            points.update((frame(page["start"]), frame(page["visible_end"])))
            if self.template["active_word"]["style"] != "none":
                points.update(frame(w["start"]) for w in page["words"])
            if anim["in"] != "none":
                points.update(range(frame(page["start"]), frame(page["start"] + anim["duration_ms"] / 1000) + 2))
        for hook in self.hooks:
            o, a = hook["overlay"], hook["style"]["animation"]
            points.update((frame(o["start"]), frame(o["end"])))
            if a["in"] != "none":
                points.update(range(frame(o["start"]), frame(o["start"] + a["duration_ms"] / 1000) + 2))
        ordered = sorted(p for p in points if 0 <= p <= total)
        plan: list[tuple[int, int, str, dict]] = []
        for first, last in zip(ordered, ordered[1:]):
            scene = self.scene_at((first + 0.5) / fps)
            key = self.scene_key(scene)
            if plan and plan[-1][2] == key:
                plan[-1] = (plan[-1][0], plan[-1][1] + last - first, key, plan[-1][3])
            else:
                plan.append((first, last - first, key, scene))
        return plan

    # -------------------------------------------------------------- drawing

    def render_scene(self, scene: dict) -> Image.Image:
        layer = Image.new("RGBA", (self.width, self.height), (0, 0, 0, 0))
        if scene["page"]:
            self._draw_page(layer, scene["page"])
        if scene["hook"]:
            self._draw_hook(layer, scene["hook"])
        if self.logo:
            layer.alpha_composite(self.logo["image"], self.logo["xy"])
        return layer

    def draw(self, t: float, *, progress_bar: bool = True) -> Image.Image:
        layer = self.render_scene(self.scene_at(t))
        if progress_bar and self.bar:
            self.draw_bar(layer, t)
        return layer

    def bar_rect(self, t: float) -> tuple[int, int, int, int]:
        thickness = max(1, round(self.bar["thickness"] * self.k))
        y = 0 if self.bar["position"] == "top" else self.height - thickness
        length = round(self.width * min(1.0, max(0.0, t / self.duration)))
        return 0, y, length, y + thickness

    def draw_bar(self, layer: Image.Image, t: float) -> None:
        left, top, right, bottom = self.bar_rect(t)
        if right > left:
            ImageDraw.Draw(layer).rectangle((left, top, right - 1, bottom - 1), fill=_rgba(self.bar["color"]))

    def _word_fill(self, word: dict, active: bool) -> tuple[int, int, int, int]:
        colors, style = self.template["colors"], self.template["active_word"]["style"]
        if active and style in ("color", "color_box"):
            return _rgba(colors["active"])
        return _rgba(colors["emphasis"] if word["emphasis"] else colors["text"])

    def _draw_page(self, layer: Image.Image, info: dict) -> None:
        tpl = self.template
        page = self.pages[info["index"]]
        active_index = info["active"]
        size, k = page["font_size"], self.k
        font = ct.pil_font(tpl["font"]["file"], size)
        stroke = round(tpl["stroke_width"] * k)
        block = page["block"]
        margin = math.ceil(size * 0.9 + stroke + (tpl["shadow"]["blur"] + abs(tpl["shadow"]["dy"]) + abs(tpl["shadow"]["dx"])) * k
                           + size * (BOX_PAD_X if tpl["colors"]["box"] else 0))
        w, h = math.ceil(block["w"]) + 2 * margin, math.ceil(block["h"]) + 2 * margin
        ox, oy = block["x"] - margin, block["y"] - margin
        boxes = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        silhouette = Image.new("L", (w, h), 0)
        sil_draw, box_draw = ImageDraw.Draw(silhouette), ImageDraw.Draw(boxes)
        if tpl["colors"]["box"]:
            px, py = size * BOX_PAD_X, size * BOX_PAD_Y
            box_draw.rounded_rectangle((block["x"] - ox - px, block["y"] - oy - py, block["x"] - ox + block["w"] + px,
                                        block["y"] - oy + block["h"] + py), radius=size * BOX_RADIUS,
                                       fill=_rgba(tpl["colors"]["box"]))
        metrics = tpl["font"]["metrics"]
        fills: list[tuple] = []
        for line in page["lines"]:
            line_cy = line["y"] + page["line_height_px"] / 2
            for word in line["words"]:
                active = word["index"] == active_index and tpl["active_word"]["style"] != "none"
                scale = tpl["active_word"]["scale"] if active else 1.0
                wf = font if scale == 1.0 else ct.pil_font(tpl["font"]["file"], size * scale)
                cx = word["x"] + word["w"] / 2
                x = cx - word["w"] * scale / 2 - ox
                baseline = line_cy + (line["baseline"] - line_cy) * scale - oy
                if active and tpl["active_word"]["style"] in ("box", "color_box"):
                    asc, desc = metrics["ascent"] * size * scale, metrics["descent"] * size * scale
                    ex, ey = size * ACTIVE_PAD_X, size * ACTIVE_PAD_Y
                    lift = size * ACTIVE_BOX_LIFT
                    box_draw.rounded_rectangle((x - ex, baseline - asc - ey + lift, x + word["w"] * scale + ex,
                                                baseline + desc + ey - lift), radius=size * ACTIVE_RADIUS,
                                               fill=_rgba(tpl["colors"]["active_box"] or tpl["colors"]["active"]))
                sil_draw.text((x, baseline), word["text"], font=wf, fill=255, anchor="ls", stroke_width=stroke,
                              stroke_fill=255)
                fills.append(((x, baseline), word["text"], wf, self._word_fill(word, active)))
        patch = boxes
        shadow = _shadow_layer(silhouette, tpl["shadow"], k)
        if shadow:
            patch.alpha_composite(shadow)
        if stroke:
            stroke_rgba = _rgba(tpl["colors"]["stroke"])
            solid = Image.new("RGBA", (w, h), stroke_rgba)
            solid.putalpha(silhouette.point(lambda v: v * stroke_rgba[3] // 255))
            patch.alpha_composite(solid)
        text = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        text_draw = ImageDraw.Draw(text)
        for xy, content, wf, fill in fills:
            text_draw.text(xy, content, font=wf, fill=fill, anchor="ls")
        patch.alpha_composite(text)
        anim = ct.animation_state(tpl["animation"]["in"], info["progress"], k)
        patch = _scale_patch(patch, anim["scale"], anim["alpha"])
        _paste_centered(layer, patch, block["x"] + block["w"] / 2, block["y"] + block["h"] / 2 + anim["dy"])

    def _draw_hook(self, layer: Image.Image, info: dict) -> None:
        hook = self.hooks[info["index"]]
        style, k, size = hook["style"], self.k, hook["size"]
        font = ct.pil_font(style["font"]["file"], size)
        stroke = round(style["stroke_width"] * k)
        shadow = style["shadow"]
        margin = math.ceil(size * 0.6 + stroke + (shadow["blur"] + abs(shadow["dy"]) + abs(shadow["dx"])) * k)
        w, h = math.ceil(hook["box_w"]) + 2 * margin, math.ceil(hook["box_h"]) + 2 * margin
        left = (self.width - hook["box_w"]) / 2
        ox, oy = left - margin, hook["top"] - margin
        silhouette = Image.new("L", (w, h), 0)
        patch = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        has_box = bool(style["colors"]["box"])
        if has_box:
            shape = (margin, margin, margin + hook["box_w"], margin + hook["box_h"])
            ImageDraw.Draw(silhouette).rounded_rectangle(shape, radius=style["radius"] * k, fill=255)
        else:
            for row in hook["rows"]:
                ImageDraw.Draw(silhouette).text((row["x"] - ox, row["baseline"] - oy), row["text"], font=font, fill=255,
                                                anchor="ls", stroke_width=stroke, stroke_fill=255)
        shade = _shadow_layer(silhouette, shadow, k)
        if shade:
            patch.alpha_composite(shade)
        if has_box:
            box = Image.new("RGBA", (w, h), (0, 0, 0, 0))
            ImageDraw.Draw(box).rounded_rectangle(shape, radius=style["radius"] * k, fill=_rgba(style["colors"]["box"]))
            patch.alpha_composite(box)
        text = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        text_draw = ImageDraw.Draw(text)
        for row in hook["rows"]:
            xy = (row["x"] - ox, row["baseline"] - oy)
            if stroke:
                text_draw.text(xy, row["text"], font=font, fill=_rgba(style["colors"]["stroke"]), anchor="ls",
                               stroke_width=stroke, stroke_fill=_rgba(style["colors"]["stroke"]))
            text_draw.text(xy, row["text"], font=font, fill=_rgba(style["colors"]["text"]), anchor="ls")
        patch.alpha_composite(text)
        anim = ct.animation_state(style["animation"]["in"], info["progress"], k)
        patch = _scale_patch(patch, anim["scale"], anim["alpha"])
        _paste_centered(layer, patch, left + hook["box_w"] / 2, hook["top"] + hook["box_h"] / 2 + anim["dy"])

    # -------------------------------------------------------------- text export

    def srt_cues(self) -> list[dict]:
        """One cue per caption page with the speaker's original casing."""
        text_of = {w["key"]: w["text"] for w in self.words}
        return [{"start": p["start"], "end": max(p["visible_end"], p["start"] + 0.04),
                 "text": " ".join(text_of[w["key"]] for w in p["words"])} for p in self.pages]
