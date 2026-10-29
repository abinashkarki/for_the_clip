import copy
from concurrent.futures import ThreadPoolExecutor

import pytest

from clippie import source_evidence as evidence


def source(count=8):
    return {"id": "ready", "duration": count * 2., "transcript_version": "v1",
            "words": [{"id": i, "text": f"Sentence{i}.", "start": i*2., "end": i*2.+1}
                      for i in range(count)]}


def test_stable_ids_cache_and_changed_transcript_preserves_old(tmp_path):
    original = source()
    built = evidence.prepare_evidence(original, tmp_path)
    hit = evidence.prepare_evidence(copy.deepcopy(original), tmp_path)
    assert (built["cache_status"], hit["cache_status"]) == ("build", "hit")
    assert built["artifact"] == hit["artifact"]
    assert evidence.evidence_key(original) == built["cache_artifact_key"]
    original["words"][0]["text"] = "Changed."
    changed = evidence.prepare_evidence(original, tmp_path)
    assert changed["path"] != built["path"] and built["path"].is_file()
    assert changed["artifact"]["units"][0]["id"] != built["artifact"]["units"][0]["id"]


def test_context_retrieves_neighbors_absent_from_summary(tmp_path):
    result = evidence.prepare_evidence(source(), tmp_path)
    result["artifact"]["summary"] = "Sentence3."
    unit = result["artifact"]["units"][3]
    context = evidence.context_for(unit["id"], result, before=2, after=1)
    assert context["text"] == "Sentence1. Sentence2. Sentence3. Sentence4."
    assert (context["start"], context["end"]) == (2., 9.)
    assert context["units"][0]["word_ids"] == [1]
    with pytest.raises(ValueError, match="Unknown"):
        evidence.context_for("invented", result)


def test_complete_long_source_packet_coverage_and_overlap(tmp_path):
    result = evidence.prepare_evidence(source(1000), tmp_path, max_chunk_chars=64)
    packets = evidence.packets(result)
    assert len(packets) > 100
    assert all(len(packet["text"]) <= 64 for packet in packets)
    covered = {identity for packet in packets for identity in packet["unit_ids"]}
    assert covered == {unit["id"] for unit in result["artifact"]["units"]}
    assert packets[-1]["end"] == 1999.
    assert any(packet["overlap_with_previous"] for packet in packets)


def test_punctuation_complete_unit_and_oversized_packet_fragments(tmp_path):
    data = source(30)
    for row in data["words"]:
        row["text"] = "unbroken"
    data["words"][-1]["text"] = "ending."
    result = evidence.prepare_evidence(data, tmp_path, max_chunk_chars=64)
    assert len(result["artifact"]["units"]) == 1
    unit = result["artifact"]["units"][0]
    assert unit["word_ids"] == list(range(30))
    fragments = [f for packet in evidence.packets(result) for f in packet["fragments"]]
    assert "".join(f["text"] for f in fragments) == unit["text"]
    assert all(f["partial_unit"] for f in fragments)


def test_approximate_segments_and_unknown_speaker_labels(tmp_path):
    data = {"id": "segment-source", "duration": 8, "segments": [{"id": "seg", "text": "Exact retained text.", "start": 1, "end": 7}]}
    result = evidence.prepare_evidence(data, tmp_path)
    unit = result["artifact"]["units"][0]
    assert unit["alignment"] == "approximate" and unit["speaker_status"] == "unknown"
    assert len(unit["warnings"]) == 2 and unit["word_ids"] == []
    assert unit["segment_ids"] == ["seg"]
    assert result["artifact"]["manifest"]["semantic_topic_map"] == "not_generated"
    assert result["artifact"]["manifest"]["content_bearing_local_retention_version"] == 1


def test_media_identity_content_hash_and_implementation_invalidate(tmp_path, monkeypatch):
    data = source()
    media = tmp_path / "media"
    media.write_bytes(b"old")
    data["source_path"] = media
    original = evidence.evidence_key(data)
    media.write_bytes(b"replacement")
    assert evidence.evidence_key(data) != original
    original = evidence.evidence_key(data)
    data["content_hash"] = "provided-hash"
    assert evidence.evidence_key(data) != original
    original = evidence.evidence_key(data)
    monkeypatch.setattr(evidence, "IMPLEMENTATION_VERSION", 3)
    assert evidence.evidence_key(data) != original


def test_concurrent_producers_commit_once(tmp_path):
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: evidence.prepare_evidence(source(), tmp_path), range(4)))
    assert sorted(row["cache_status"] for row in results) == ["build", "hit", "hit", "hit"]
    assert len(list(tmp_path.glob("*.json"))) == 1


@pytest.mark.parametrize("mutate", [
    lambda s: s["words"][0].update(start=float("nan")),
    lambda s: s["words"][0].update(end=100),
    lambda s: s["words"][0].update(start=True),
])
def test_invalid_timings_rejected(tmp_path, mutate):
    data = source()
    mutate(data)
    with pytest.raises(ValueError):
        evidence.prepare_evidence(data, tmp_path)


def test_source_provenance_ids_differ_but_transport_size_does_not(tmp_path):
    first = source()
    second = {**first, "id": "another-source"}
    a = evidence.prepare_evidence(first, tmp_path, max_chunk_chars=64)["artifact"]
    b = evidence.prepare_evidence(second, tmp_path, max_chunk_chars=64)["artifact"]
    c = evidence.prepare_evidence(first, tmp_path, max_chunk_chars=128)["artifact"]
    assert a["units"][0]["id"] != b["units"][0]["id"]
    assert a["units"] == c["units"]
    assert all(unit["source_id"] == "ready" for unit in a["units"])


def test_known_speaker_turn_splits_without_punctuation(tmp_path):
    data = source(4)
    for row, speaker in zip(data["words"], ["A", "A", "B", "B"]):
        row.update(text="unpunctuated", speaker=speaker)
    units = evidence.prepare_evidence(data, tmp_path)["artifact"]["units"]
    assert [unit["word_ids"] for unit in units] == [[0, 1], [2, 3]]
    assert [unit["speakers"] for unit in units] == [["A"], ["B"]]
    assert all(unit["speaker_status"] == "provided_unverified" for unit in units)


def test_mismatched_cache_manifest_is_rejected_without_rewriting(tmp_path):
    import json
    built = evidence.prepare_evidence(source(), tmp_path)
    artifact = built["artifact"]
    artifact["manifest"]["fingerprint"] = "wrong"
    corrupted = json.dumps(artifact)
    built["path"].write_text(corrupted)
    with pytest.raises(ValueError, match="fingerprint mismatch"):
        evidence.prepare_evidence(source(), tmp_path)
    assert built["path"].read_text() == corrupted


@pytest.mark.parametrize("identity", [None, "", "  ", 5, True])
def test_nonempty_source_identity_required(tmp_path, identity):
    data = {**source(), "id": identity}
    with pytest.raises(ValueError, match="source_id"):
        evidence.prepare_evidence(data, tmp_path)
