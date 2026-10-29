import json
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

from clippie import media


FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
pytestmark = pytest.mark.skipif(not FFMPEG or not FFPROBE, reason="FFmpeg tools are required")


@pytest.fixture
def short_video(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("CLIPPIE_FFMPEG", FFMPEG or "ffmpeg")
    monkeypatch.setenv("CLIPPIE_FFPROBE", FFPROBE or "ffprobe")
    source = tmp_path / "source with spaces.mp4"
    subprocess.run(
        [FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=24:duration=4",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=4", "-c:v", "libx264", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-shortest", str(source)],
        check=True,
        capture_output=True,
    )
    return source


def test_probe_and_local_ingest_keep_source_path(short_video: Path, tmp_path: Path) -> None:
    details = media.probe(short_video)
    assert details["duration"] == pytest.approx(4, abs=0.1)
    assert (details["width"], details["height"], details["has_audio"]) == (320, 240, True)
    ingested = media.ingest(str(short_video), tmp_path / "job")
    assert ingested == short_video.resolve()
    assert short_video.exists()


def test_preview_is_browser_playable_mp4(short_video: Path, tmp_path: Path) -> None:
    preview = media.make_preview(short_video, tmp_path / "job")
    details = media.probe(preview)
    assert preview.suffix == ".mp4"
    assert preview == short_video.resolve()
    assert details["duration"] == pytest.approx(4, abs=0.15)
    assert details["width"] <= 1920 and details["height"] <= 1080 and details["has_audio"]


def test_local_preview_is_validated_and_cache_tracks_source(short_video: Path, tmp_path: Path) -> None:
    source = tmp_path / 'incompatible.mkv'
    subprocess.run([FFMPEG, '-v', 'error', '-i', str(short_video), '-c', 'copy', str(source)], check=True)
    directory = tmp_path / 'job'
    assert media.preview_plan(source, directory)[0] == 'encode'
    preview = media.make_preview(source, directory)
    assert media.preview_plan(source, directory) == ('cached', preview)
    assert media.probe(preview)['duration'] == pytest.approx(media.probe(source)['duration'], abs=.15)
    source.touch()
    assert media.preview_plan(source, directory)[0] == 'encode'


def test_imported_segment_only_transcript_marks_approximate(tmp_path: Path) -> None:
    path = tmp_path / "transcript.json"
    path.write_text(json.dumps({"segments": [{"start": 5, "end": 7, "text": "A real sentence."}]}))
    result = media.transcribe(tmp_path / "unused.mp4", tmp_path / "job", transcript_path=path)
    assert result["provider"] == "import"
    assert [word["text"] for word in result["words"]] == ["A", "real", "sentence."]
    assert result["words"][0]["start"] == 5
    assert any("approximate" in warning for warning in result["warnings"])


def test_srt_import_retains_actual_caption_text(tmp_path: Path) -> None:
    path = tmp_path / "source.srt"
    path.write_text("1\n00:00:01,250 --> 00:00:02,500\nActual words here.\n")
    result = media.transcribe(tmp_path / "unused.mp4", tmp_path / "job", transcript_path=path)
    assert "Actual words here." == result["segments"][0]["text"]
    assert result["segments"][0]["start"] == pytest.approx(1.25)


def test_word_field_and_nested_whisper_words_keep_exact_timings(tmp_path: Path) -> None:
    path = tmp_path / "nested.json"
    path.write_text(json.dumps({"segments": [{"start": 1, "end": 2, "text": "not used for timing", "words": [
        {"word": " exact", "start": 1.1, "end": 1.4},
        {"word": " timing.", "start": 1.5, "end": 1.9},
    ]}]}))
    result = media.transcribe(tmp_path / "unused.mp4", tmp_path / "job", transcript_path=path)
    assert [word["text"] for word in result["words"]] == ["exact", "timing."]
    assert [word["start"] for word in result["words"]] == [1.1, 1.5]
    assert not result["warnings"]


def test_mlx_uses_configured_model_and_whisper_word_field(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen = {}
    monkeypatch.delenv("HF_HUB_DISABLE_XET", raising=False)

    def transcribe(path: str, **kwargs: object) -> dict:
        seen.update(kwargs)
        return {"segments": [{"words": [{"word": " actual", "start": 0.3, "end": 0.8}]}]}

    monkeypatch.setitem(sys.modules, "mlx_whisper", types.SimpleNamespace(transcribe=transcribe))
    words, _segments, approximate = media._ml_transcribe(tmp_path / "source.mp4", "en", "test-model")
    assert words == [{"id": 0, "text": "actual", "start": 0.3, "end": 0.8}]
    assert seen["path_or_hf_repo"] == "test-model"
    assert approximate is False
    assert media.os.environ["HF_HUB_DISABLE_XET"] == "1"


def test_groq_chunk_seam_repairs_truncated_word_response_and_mixes_segments(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(media, "probe", lambda _path: {"duration": 541.0})
    monkeypatch.setattr(media, "_binary", lambda *_args: "ffmpeg")

    def fake_run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        Path(args[-1]).touch()
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(media, "_run", fake_run)
    responses = [
        {"words": [{"word": " before", "start": 537.5, "end": 537.8}]},
        {"segments": [{"text": "fallback word", "start": 1.1, "end": 2.5}]},
    ]

    class Response:
        def __init__(self, data: dict) -> None:
            self.data = data

        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict:
            return self.data

    class Client:
        def __init__(self, **_kwargs: object) -> None:
            self.index = 0

        def __enter__(self) -> "Client":
            return self

        def __exit__(self, *_args: object) -> None:
            pass

        def post(self, *_args: object, **_kwargs: object) -> Response:
            data = responses[self.index]
            self.index += 1
            return Response(data)

    import httpx

    monkeypatch.setattr(httpx, "Client", Client)
    words, segments, approximate = media._groq_transcribe(tmp_path / "source.mp4", tmp_path, None, "key")
    assert [word["text"] for word in words] == ["before", "fallback", "word"]
    assert [word["start"] for word in words] == pytest.approx([537.5, 539.1, 540.033333], abs=0.001)
    assert segments[-1]["text"] == "fallback word"
    assert approximate is True
