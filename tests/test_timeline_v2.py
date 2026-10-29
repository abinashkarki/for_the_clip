"""Additive timeline versions share the same executable integrity gates."""
import copy

import pytest

from clippie import planner, render


SOURCES = {"a": {"duration": 300}, "b": {"duration": 60}}


def moment():
    return {"schema_version": 2, "clips": [
        {"id": "c1", "source_id": "a", "start": 10, "end": 25}]}


def test_v1_default_canonical_contract_and_legacy_roles():
    timeline = {"clips": [
        {"id": "c1", "source_id": "a", "start": 0, "end": 5, "role": "hook"},
        {"id": "c2", "source_id": "a", "start": 5, "end": 10, "role": "payoff"}]}
    result = planner.validate_timeline(timeline, SOURCES)
    assert result == {
        "schema_version": 1, "aspect_ratio": "16:9", "width": 1280, "height": 720, "fps": 30,
        "clips": [{**clip, "start": float(clip["start"]), "end": float(clip["end"]),
                   "note": "", "at": float(index * 5),
                   "framing": {"mode": "fit", "zoom": 1., "x": .5, "y": .5}}
                  for index, clip in enumerate(timeline["clips"])],
        "broll": [], "titles": [], "captions": True, "caption_style": "clean_focus",
        "audio": {"normalize": True}, "duration": 10.}
    for mutation in (lambda t: t["clips"].pop(),
                     lambda t: t["clips"][0].pop("role"),
                     lambda t: t["clips"][0].update(role="answer")):
        invalid = copy.deepcopy(timeline)
        mutation(invalid)
        with pytest.raises(ValueError):
            planner.validate_timeline(invalid, SOURCES)


def test_v2_moment_is_portrait_idempotent_and_renderer_accepted():
    timeline = moment()
    result = render._validate(timeline, SOURCES)
    assert (result["schema_version"], result["width"], result["height"], result["fps"]) == (2, 1080, 1920, 30)
    assert result["aspect_ratio"] == "9:16"
    assert result["duration"] == 15
    assert "role" not in result["clips"][0]
    assert planner.validate_timeline(result, SOURCES) == result
    assert timeline == moment()


def test_v2_story_preserves_variable_passages_and_descriptive_roles():
    timeline = moment()
    timeline["clips"] += [{"id": f"c{i}", "source_id": "b", "start": (i-2)*5,
                           "end": (i-1)*5, "role": "answer"} for i in range(2, 7)]
    timeline["broll"] = [{"id": "b1", "source_id": "a", "start": 0, "end": 4, "at": 14}]
    timeline["titles"] = [{"text": " Context ", "at": 0, "duration": 3}]
    result = planner.validate_timeline(timeline, SOURCES)
    assert [c["at"] for c in result["clips"]] == [0, 15, 20, 25, 30, 35]
    assert result["duration"] == 40
    assert result["clips"][-1]["role"] == "answer"
    assert result["titles"][0]["text"] == "Context"


@pytest.mark.parametrize("aspect,size", [("16:9", (1280, 720)), ("9:16", (1080, 1920))])
def test_v2_aspect_controls_default_geometry(aspect, size):
    result = planner.validate_timeline({**moment(), "aspect_ratio": aspect}, SOURCES)
    assert (result["width"], result["height"]) == size


@pytest.mark.parametrize("version", [True, False, 1., 2., "2", None, 0, 3, [], {}])
def test_unsupported_or_noninteger_versions_rejected(version):
    with pytest.raises(ValueError, match="schema_version"):
        planner.validate_timeline({**moment(), "schema_version": version}, SOURCES)


@pytest.mark.parametrize("role", [None, "", "  ", 3, True, "x" * 81])
def test_v2_supplied_roles_are_bounded_text(role):
    timeline = moment()
    timeline["clips"][0]["role"] = role
    with pytest.raises(ValueError, match="role"):
        planner.validate_timeline(timeline, SOURCES)


@pytest.mark.parametrize("change", [
    {"start": -1}, {"end": 301}, {"end": 10}, {"end": float("inf")},
    {"start": float("nan")}, {"start": True}, {"source_id": "outside"},
    {"framing": {"zoom": 1.6}}, {"framing": {"x": -1}},
])
def test_v2_source_bounds_and_framing_stay_strict(change):
    timeline = moment()
    timeline["clips"][0].update(change)
    with pytest.raises(ValueError):
        planner.validate_timeline(timeline, SOURCES)


@pytest.mark.parametrize("change", [
    {"clips": []}, {"width": 1920, "height": 1080}, {"fps": True},
    {"captions": "yes"}, {"caption_style": "unknown"}, {"audio": {"normalize": 1}},
    {"clips": [{"id": "c1", "source_id": "a", "start": 0, "end": 181}]},
    {"broll": [{"id": "b1", "source_id": "a", "start": 0, "end": 16, "at": 0}]},
    {"titles": [{"text": "late", "at": 14, "duration": 2}]},
])
def test_v2_program_and_output_checks_stay_strict(change):
    with pytest.raises(ValueError):
        planner.validate_timeline({**moment(), **change}, SOURCES)


def test_v2_overlapping_primary_and_overlays_are_rejected():
    for field, items in [
        ("clips", [{"id": "c2", "source_id": "a", "start": 24, "end": 30}]),
        ("broll", [{"id": f"b{i}", "source_id": "b", "start": 0, "end": 4, "at": i} for i in (1, 2)]),
        ("titles", [{"text": "title", "at": i, "duration": 4} for i in (1, 2)]),
    ]:
        timeline = moment()
        timeline[field] = timeline.get(field, []) + items
        with pytest.raises(ValueError, match="overlap"):
            planner.validate_timeline(timeline, SOURCES)
