"""Caption template registry, overrides, layout rules and golden fixtures."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path

import pytest

from clippie import caption_templates as ct

GOLDEN_DIR = Path(__file__).parent / "fixtures" / "caption_golden"
SENTENCE = ("So honestly the biggest mistake people make with AI tutors is trusting them blindly. Wait that is not what I "
            "expected at all but here is the surprising part international collaboration changes everything").split()
CANVASES = [(1080, 1920), (720, 1280)]
TEMPLATE_IDS = ["bold_pop", "boxed_word", "color_word", "phrase_clean", "karaoke", "minimal_lower", "impact_one", "podcast_box"]


def fake_measure(text: str, size: float) -> float:
    return len(text) * size * 0.5


def golden_words() -> tuple[list[dict], list[float]]:
    words, t = [], 0.4
    for i, text in enumerate(SENTENCE):
        gap = 0.62 if SENTENCE[i - 1] == "blindly." else 0.06
        t += gap if i else 0
        dur = 0.18 + 0.03 * (len(text) % 5)
        words.append({"key": f"src:{i}", "text": text, "start": round(t, 3), "end": round(t + dur, 3), "source_id": "src",
                      "word_id": i, "emphasis": i in (4, 22), "hidden": i == 11})
        t += dur
    boundary = words[20]["start"]
    return words, [0.0, boundary]


def make_words(spec: list[tuple[str, float, float]], **flags) -> list[dict]:
    return [{"key": f"s:{i}", "text": text, "start": a, "end": b, "emphasis": False, "hidden": False}
            for i, (text, a, b) in enumerate(spec)]


def template(**changes) -> dict:
    base = ct.resolve("bold_pop")
    for key, value in changes.items():
        if isinstance(value, dict):
            base[key].update(value)
        else:
            base[key] = value
    return base


# ------------------------------------------------------------------ registry

def test_registry_ships_all_templates_and_hooks():
    loaded = ct.load()
    assert sorted(loaded["captions"]) == sorted(TEMPLATE_IDS)
    assert sorted(loaded["hooks"]) == ["hook_banner", "hook_box", "hook_outline"]
    for tpl in loaded["captions"].values():
        assert (ct.FONT_DIR / tpl["font"]["file"]).is_file()
        assert tpl["font"]["metrics"]["ascent"] > 0


def test_fonts_ship_with_licences():
    files = {p.name for p in ct.FONT_DIR.iterdir()}
    for family in ("Montserrat", "Inter", "Poppins", "Anton"):
        assert f"OFL-{family}.txt" in files


def test_static_weights_differ():
    bold = ct.pil_measure("Inter-Bold.ttf")("Hello world", 100)
    medium = ct.pil_measure("Inter-Medium.ttf")("Hello world", 100)
    assert bold > medium


@pytest.mark.parametrize("mutate,path", [
    (lambda t: t["font"].update(file="nope.ttf"), "font.file"),
    (lambda t: t["font"].update(file="../x.ttf"), "font.file"),
    (lambda t: t["font"].update(size=10), "font.size"),
    (lambda t: t.update(case="title"), "case"),
    (lambda t: t["grouping"].update(max_lines=0), "grouping.max_lines"),
    (lambda t: t["colors"].update(text="red"), "colors.text"),
    (lambda t: t["colors"].update(box=5), "colors.box"),
    (lambda t: t["active_word"].update(style="glow"), "active_word.style"),
    (lambda t: t["active_word"].update(scale=1.6), "active_word.scale"),
    (lambda t: t["animation"].update({"in": "spin"}), "animation.in"),
    (lambda t: t["position"].update(y=1.4), "position.y"),
])
def test_template_validation_rejects(mutate, path):
    raw = json.loads((ct.TEMPLATE_DIR / "bold_pop.json").read_text())
    mutate(raw)
    with pytest.raises(ct.TemplateError) as err:
        ct.validate_template(raw, "bold_pop")
    assert err.value.path == path


def test_resolve_applies_overrides():
    out = ct.resolve("bold_pop", {"colors": {"active": "#ff0000"}, "case": "lower", "position": {"y": 0.5},
                                  "grouping": {"max_words": 2}, "stroke_width": 4, "active_word": {"style": "box"},
                                  "animation": {"in": "fade"}, "font": {"family": "Inter", "file": "Inter-Bold.ttf", "size": 60}})
    assert out["colors"]["active"] == "#FF0000" and out["case"] == "lower" and out["position"]["y"] == 0.5
    assert out["grouping"]["max_words"] == 2 and out["font"]["file"] == "Inter-Bold.ttf"
    assert out["font"]["metrics"] == ct.font_metrics("Inter-Bold.ttf")
    assert ct.load()["captions"]["bold_pop"]["case"] == "upper"  # registry untouched


@pytest.mark.parametrize("overrides,path", [
    ({"grouping": {"max_lines": 3}}, "overrides.grouping.max_lines"),
    ({"position": {"max_width": 0.5}}, "overrides.position.max_width"),
    ({"line_height": 1.5}, "overrides.line_height"),
    ({"font": {"family": "Inter"}}, "overrides.font"),
    ({"font": {"family": "X", "file": "missing.ttf"}}, "font.file"),
    ({"colors": {"bogus": "#000000"}}, "overrides.colors.bogus"),
    ({"colors": {"text": "nope"}}, "colors.text"),
    ({"case": "weird"}, "case"),
    ({"font": 5}, "overrides.font"),
])
def test_resolve_rejects_bad_overrides(overrides, path):
    with pytest.raises(ct.TemplateError) as err:
        ct.resolve("bold_pop", overrides)
    assert err.value.path == path


def test_resolve_unknown_template():
    with pytest.raises(ct.TemplateError):
        ct.resolve("missing")


# ------------------------------------------------------------------ grouping rules

def pages_of(words, **tpl):
    return ct.pages(template(**tpl), words, 1080, 1920, fake_measure)


def test_groups_by_max_words_and_chars():
    words = make_words([(w, i * 0.3, i * 0.3 + 0.2) for i, w in enumerate("aa bb cc dd ee".split())])
    result = pages_of(words, grouping={"max_words": 2, "max_chars": 18, "max_lines": 2, "break_on_pause": 0.35})
    assert [[w["key"] for w in p["words"]] for p in result] == [["s:0", "s:1"], ["s:2", "s:3"], ["s:4"]]
    result = pages_of(words, grouping={"max_words": 5, "max_chars": 5, "max_lines": 2, "break_on_pause": 0.35})
    assert [len(p["words"]) for p in result] == [2, 2, 1]  # max_chars excludes spaces: aa+bb=4, +cc=6 > 5


def test_page_breaks_on_pause_and_boundary():
    words = make_words([("one", 0, 0.2), ("two", 0.2, 0.4), ("three", 0.9, 1.1), ("four", 1.1, 1.3)])
    result = pages_of(words, grouping={"max_words": 9, "max_chars": 60, "max_lines": 2, "break_on_pause": 0.35})
    assert [len(p["words"]) for p in result] == [2, 2]
    result = ct.pages(template(grouping={"max_words": 9, "max_chars": 60, "max_lines": 2, "break_on_pause": 5}),
                      words, 1080, 1920, fake_measure, boundaries=[0.0, 0.2])
    assert [len(p["words"]) for p in result] == [1, 3]  # a kept range starts at 0.2, before "two"


def test_hidden_words_are_not_drawn_and_do_not_split_pages():
    words = make_words([("a", 0, 0.2), ("b", 0.2, 0.4), ("c", 0.4, 0.6)])
    words[1]["hidden"] = True
    result = pages_of(words)
    assert [w["key"] for w in result[0]["words"]] == ["s:0", "s:2"]


def test_line_wrap_and_max_lines_split():
    words = make_words([(w, i * 0.3, i * 0.3 + 0.2) for i, w in enumerate(["abcdef"] * 8)])
    tpl = dict(grouping={"max_words": 8, "max_chars": 80, "max_lines": 2, "break_on_pause": 5})
    result = pages_of(words, **tpl)
    # width 0.86*1080 = 928.8; size 84*0.5*6=252 px per word, 3 words = 252*3+3 gaps*42 > fits? 3*252+2*42=840 yes, 4 no
    assert [l["text"].count(" ") + 1 for l in result[0]["lines"]] == [3, 3]
    assert [len(p["words"]) for p in result] == [6, 2]
    assert result[1]["start"] == pytest.approx(1.8)
    assert result[0]["visible_end"] == pytest.approx(1.8)  # until the next page starts


def test_visible_window_capped_after_last_word():
    words = make_words([("one", 0, 0.5), ("two", 3.0, 3.4)])
    result = pages_of(words)
    assert result[0]["visible_end"] == pytest.approx(0.5 + ct.VISIBLE_TAIL)
    assert result[1]["visible_end"] == pytest.approx(3.4 + ct.VISIBLE_TAIL)


def test_oversized_word_shrinks_page_font():
    words = make_words([("x" * 40, 0, 1)])
    page = pages_of(words)[0]
    assert page["font_size"] < 84
    assert page["lines"][0]["w"] <= 0.86 * 1080 + 1e-6


def test_case_applied_before_measure_and_text():
    words = make_words([("hello", 0, 1)])
    lay = ct.layout(template(case="upper"), words, 1080, 1920, 0.1, fake_measure)
    assert lay["page"]["lines"][0]["words"][0]["text"] == "HELLO"


def test_outline_clearance_is_used_for_both_caption_positions_and_wrapping():
    tpl = ct.resolve('bold_pop')
    words = make_words([(word, i * .3, (i + 1) * .3) for i, word in enumerate(['THEY', 'NEVER', 'AGREE'])])
    measure = lambda text, size: size * (.05 if text == ' ' else len(text) * .5)
    page = ct.pages(tpl, words, 1080, 1920, measure)[0]
    left, right = page['lines'][0]['words'][:2]
    assert right['x'] - left['x'] - left['w'] == pytest.approx(22 + .12 * page['font_size'])
    tpl['position']['max_width'] = 210 / 1080
    wrapped = ct.pages(tpl, words[:2], 1080, 1920, lambda text, size: 1 if text == ' ' else 100)[0]
    assert len(wrapped['lines']) == 2
    assert all(line['w'] <= 210 for line in wrapped['lines'])


def test_geometry_centred_and_scaled():
    words = make_words([("hi", 0, 1)])
    a = ct.layout(template(), words, 1080, 1920, 0.5, fake_measure)["page"]
    b = ct.layout(template(), words, 540, 960, 0.5, fake_measure)["page"]
    assert b["font_size"] == pytest.approx(a["font_size"] / 2)
    line = a["lines"][0]
    assert line["x"] + line["w"] / 2 == pytest.approx(540)
    assert a["block"]["y"] + a["block"]["h"] / 2 == pytest.approx(0.70 * 1920)


def test_block_clamped_inside_canvas():
    words = make_words([("hi", 0, 1)])
    page = ct.layout(template(position={"y": 0.95, "max_width": 0.86}), words, 1080, 1920, 0.5, fake_measure)["page"]
    assert page["block"]["y"] + page["block"]["h"] <= 1920 + 1e-6


# ------------------------------------------------------------------ time-dependent state

def test_layout_page_window_and_active_word():
    words = make_words([("one", 1.0, 1.3), ("two", 1.4, 1.7), ("three", 1.8, 2.1)])
    tpl = template()
    assert ct.layout(tpl, words, 1080, 1920, 0.99, fake_measure)["page"] is None
    assert ct.layout(tpl, words, 1080, 1920, 1.0, fake_measure)["page"]["active"] == 0
    assert ct.layout(tpl, words, 1080, 1920, 1.35, fake_measure)["page"]["active"] == 0  # sticky through gaps
    assert ct.layout(tpl, words, 1080, 1920, 1.45, fake_measure)["page"]["active"] == 1
    assert ct.layout(tpl, words, 1080, 1920, 2.05, fake_measure)["page"]["active"] == 2
    assert ct.layout(tpl, words, 1080, 1920, 2.1 + ct.VISIBLE_TAIL + 0.01, fake_measure)["page"] is None


def test_active_none_style_reports_minus_one_and_scale_applies_to_active_only():
    words = make_words([("one", 0, 0.3), ("two", 0.4, 0.7)])
    lay = ct.layout(template(active_word={"style": "none", "scale": 1.0}), words, 1080, 1920, 0.5, fake_measure)
    assert lay["page"]["active"] == -1
    lay = ct.layout(template(active_word={"style": "scale", "scale": 1.3}), words, 1080, 1920, 0.5, fake_measure)
    scales = [w["scale"] for w in lay["page"]["lines"][0]["words"]]
    assert scales == [1.0, 1.3]


def test_animation_progress_and_transforms():
    words = make_words([("one", 1.0, 2.0)])
    tpl = template(animation={"in": "pop", "duration_ms": 200})
    assert ct.layout(tpl, words, 1080, 1920, 1.0, fake_measure)["page"]["progress"] == 0.0
    assert ct.layout(tpl, words, 1080, 1920, 1.1, fake_measure)["page"]["progress"] == pytest.approx(0.5)
    assert ct.layout(tpl, words, 1080, 1920, 1.5, fake_measure)["page"]["progress"] == 1.0
    assert ct.animation_state("pop", 0)["scale"] == pytest.approx(0.85)
    assert ct.animation_state("pop", 1)["scale"] == pytest.approx(1.0)
    assert ct.animation_state("fade", 0)["alpha"] == 0
    assert ct.animation_state("slide_up", 0, 2.0)["dy"] == pytest.approx(48)
    none = template(animation={"in": "none", "duration_ms": 0})
    assert ct.layout(none, words, 1080, 1920, 1.0, fake_measure)["page"]["progress"] == 1.0


def test_emphasis_flag_flows_to_words():
    words = make_words([("one", 0, 1)])
    words[0]["emphasis"] = True
    page = ct.layout(template(), words, 1080, 1920, 0.5, fake_measure)["page"]
    assert page["lines"][0]["words"][0]["emphasis"] is True


# ------------------------------------------------------------------ golden fixtures

def _round(value):
    if isinstance(value, float):
        return round(value, 2)
    if isinstance(value, dict):
        return {k: _round(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_round(v) for v in value]
    return value


def _sample_times(page_list: list[dict]) -> list[float]:
    times = [0.1]
    for page in page_list[:7]:
        times += [page["start"] + 0.01, page["start"] + 0.07, page["words"][-1]["start"] + 0.01, page["visible_end"] - 0.01]
    times.append(page_list[-1]["visible_end"] + 0.05)
    return sorted({round(t, 3) for t in times})


def build_golden(template_id: str) -> dict:
    tpl = ct.resolve(template_id)
    words, boundaries = golden_words()
    measure = ct.pil_measure(tpl["font"]["file"])
    runs = []
    space_widths = {}
    for width, height in CANVASES:
        page_list = ct.pages(tpl, words, width, height, measure, boundaries=boundaries)
        for page in page_list:
            space_widths[f'{page["font_size"]:.1f}'] = measure(' ', page['font_size'])
        runs.append({
            "canvas": {"width": width, "height": height},
            "pages": [{"index": p["index"], "start": p["start"], "visible_end": p["visible_end"], "font_size": p["font_size"],
                       "keys": [w["key"] for w in p["words"]], "lines": [l["text"] for l in p["lines"]]} for p in page_list],
            "cases": [{"t": t, "layout": ct.layout_at(tpl, page_list, t)} for t in _sample_times(page_list)],
        })
    return _round({
        "format": "clippie.caption_golden/1", "template": tpl, "boundaries": boundaries, "words": words,
        "measure": {"engine": "Pillow BASIC layout getlength(text) at float size px, per word; space = getlength(' ')",
                    "font_file": tpl["font"]["file"], "space_widths": space_widths},
        "runs": runs})


def _assert_close(actual, expected, path="root"):
    if isinstance(expected, dict):
        assert isinstance(actual, dict) and actual.keys() == expected.keys(), path
        for key in expected:
            _assert_close(actual[key], expected[key], f"{path}.{key}")
    elif isinstance(expected, list):
        assert isinstance(actual, list) and len(actual) == len(expected), path
        for i, (a, e) in enumerate(zip(actual, expected)):
            _assert_close(a, e, f"{path}[{i}]")
    elif isinstance(expected, float):
        assert actual == pytest.approx(expected, abs=0.06), path
    else:
        assert actual == expected, path


@pytest.mark.parametrize("template_id", TEMPLATE_IDS)
def test_golden_fixture_matches(template_id):
    path = GOLDEN_DIR / f"{template_id}.json"
    current = build_golden(template_id)
    if os.environ.get("CLIPPIE_WRITE_GOLDEN"):
        path.write_text(json.dumps(current, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    expected = json.loads(path.read_text(encoding="utf-8"))
    _assert_close(json.loads(json.dumps(current)), expected)


def test_golden_fixtures_exercise_the_rules():
    data = json.loads((GOLDEN_DIR / "bold_pop.json").read_text())
    run = data["runs"][0]
    assert len(run["pages"]) > 6
    assert any(len(p["lines"]) == 2 for p in run["pages"])
    assert any(c["layout"]["page"] is None for c in run["cases"])
    assert any(c["layout"]["page"] and c["layout"]["page"]["progress"] < 1 for c in run["cases"])
