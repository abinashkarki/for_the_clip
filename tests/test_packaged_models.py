"""Desktop inference must not silently fetch/substitute a speech model."""
from pathlib import Path

import pytest

from clippie import media


def test_desktop_without_prepared_weights_refuses_before_inference(tmp_path, monkeypatch):
    monkeypatch.setenv('CLIPPIE_DESKTOP', '1')
    monkeypatch.setenv('CLIPPIE_DATA_DIR', str(tmp_path / 'empty-data'))
    monkeypatch.delenv('CLIPPIE_WHISPER_MODEL', raising=False)
    monkeypatch.setattr(media, '_ml_transcribe', lambda *a, **kw: pytest.fail('Must not invoke implicit model download'))
    source = tmp_path / 'fixture.wav'
    source.touch()
    with pytest.raises(RuntimeError, match='Complete model setup'):
        media.transcribe(source, tmp_path / 'transcript', provider='mlx')


def test_desktop_accepts_explicit_complete_local_weights(tmp_path, monkeypatch):
    monkeypatch.setenv('CLIPPIE_DESKTOP', '1')
    model = tmp_path / 'Models with spaces'
    model.mkdir()
    (model / 'weights.safetensors').touch()
    (model / 'config.json').write_text('{}')
    monkeypatch.setenv('CLIPPIE_WHISPER_MODEL', str(model))
    observed = []
    def infer(source, language, reference):
        observed.append(reference)
        return [{'text': 'hello', 'start': 0, 'end': 1}], [], False
    monkeypatch.setattr(media, '_ml_transcribe', infer)
    monkeypatch.setattr(media, 'probe_duration', lambda _: 1)
    source = tmp_path / 'fixture.wav'
    source.touch()
    result = media.transcribe(source, tmp_path / 'transcript', provider='mlx')
    assert observed == [str(model.resolve())]
    assert result['words'][0]['text'] == 'hello'


def test_desktop_partial_model_is_not_ready(tmp_path, monkeypatch):
    monkeypatch.setenv('CLIPPIE_DESKTOP', '1')
    model = tmp_path / 'partial'
    model.mkdir()
    (model / 'weights.safetensors').touch()
    monkeypatch.setenv('CLIPPIE_WHISPER_MODEL', str(model))
    with pytest.raises(RuntimeError, match='Complete model setup'):
        media.transcribe(tmp_path / 'fixture.wav', tmp_path / 'transcript', provider='mlx')
