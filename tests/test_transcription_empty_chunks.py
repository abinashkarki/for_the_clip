"""Silent chunks can complete; empty recordings and invalid alignment still fail."""
import json
from pathlib import Path
import sys
import types

import pytest

from clippie import media


def setup_chunking(monkeypatch, tmp_path, response):
    monkeypatch.setattr(media, '_binary', lambda *args: 'ffmpeg')
    monkeypatch.setattr(media, '_run', lambda args, **kwargs: Path(args[-1]).touch())
    calls = []
    def transcribe(path, **kwargs):
        start = int(Path(path).stem.split('_')[-1]) / 1000
        calls.append(start)
        return response(start)
    monkeypatch.setitem(sys.modules, 'mlx_whisper', types.SimpleNamespace(transcribe=transcribe))
    return calls


def spoken(text='speech'):
    return {'segments': [{'words': [{'word': text, 'start': 1, 'end': 1.5}]}]}


def test_real_duration_padding_tail_finishes_and_retains_all_cached_speech(monkeypatch, tmp_path):
    duration = 2992.030476
    cache = (tmp_path / 'cache', 'original')
    cache[0].mkdir()
    for index, start in enumerate([0, 598, 1196, 1794, 2392]):
        word = {'id': 0, 'text': f'cached-{index}', 'start': start + 3, 'end': start + 3.5}
        (cache[0] / f'original-{start * 1000}.json').write_text(json.dumps({'words': [word], 'approximate': False}))
    calls = setup_chunking(monkeypatch, tmp_path, lambda start: {'text': '', 'segments': []})
    progress = []
    hooks = media.TranscribeHooks(finished=lambda covered, total, words: progress.append((covered, total)))
    words, segments, approximate = media._ml_transcribe_chunked(
        tmp_path / 'audio.m4a', tmp_path, None, 'cached-model', duration, hooks=hooks, cache=cache)
    assert calls == [2990.0]  # Five completed ten-minute chunks were not transcribed again.
    assert [w['text'] for w in words] == [f'cached-{i}' for i in range(5)]
    assert [w['id'] for w in words] == list(range(5))
    assert progress[-1] == (duration, duration)
    assert approximate is False and segments
    silent = json.loads((cache[0] / 'original-2990000.json').read_text())
    assert silent == {'words': [], 'approximate': False}
    calls.clear()
    assert media._ml_transcribe_chunked(tmp_path / 'audio.m4a', tmp_path, None, 'cached-model', duration, cache=cache)[0] == words
    assert calls == []  # Explicitly empty completed chunks are reusable too.


def test_silent_middle_chunk_does_not_prevent_later_speech(monkeypatch, tmp_path):
    monkeypatch.setattr(media, 'MLX_CHUNK_SECONDS', 10)
    calls = setup_chunking(monkeypatch, tmp_path, lambda start:
        {'text': '', 'segments': []} if start == 8 else spoken('before' if start == 0 else 'after'))
    words, _, _ = media._ml_transcribe_chunked(tmp_path / 'audio', tmp_path, 'en', 'model', 25)
    assert calls == [0, 8, 16]
    assert [(w['text'], w['start']) for w in words] == [('before', 1), ('after', 17)]


def test_entire_silent_recording_is_still_rejected(monkeypatch, tmp_path):
    setup_chunking(monkeypatch, tmp_path, lambda start: {'text': '', 'segments': []})
    with pytest.raises(RuntimeError, match='no usable speech timings'):
        media._ml_transcribe_chunked(tmp_path / 'audio', tmp_path, None, 'model', 601)
    with pytest.raises(RuntimeError, match='no usable speech timings'):
        media._ml_transcribe(tmp_path / 'chunk_0.wav', None, 'model')


@pytest.mark.parametrize('result', [
    {'text': 'speech without alignment', 'segments': []},
    {'segments': [{'text': 'speech with missing timestamps'}]},
    {'text': ''},  # Missing alignment structure is not a certified empty chunk.
])
def test_malformed_or_nonempty_untimed_output_is_not_treated_as_silence(monkeypatch, tmp_path, result):
    setup_chunking(monkeypatch, tmp_path, lambda start: result)
    with pytest.raises(RuntimeError, match='no usable speech timings'):
        media._ml_transcribe_chunked(tmp_path / 'audio', tmp_path, None, 'model', 601)


def test_provider_errors_are_not_silently_skipped(monkeypatch, tmp_path):
    def fail(start):
        raise OSError('model unavailable')
    setup_chunking(monkeypatch, tmp_path, fail)
    with pytest.raises(OSError, match='model unavailable'):
        media._ml_transcribe_chunked(tmp_path / 'audio', tmp_path, None, 'model', 601)
