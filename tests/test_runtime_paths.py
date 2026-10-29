"""Relocated app resources and models must not depend on the source checkout."""
import hashlib
import importlib.util
from pathlib import Path

import pytest

from clippie import analysis, local_editor, local_models, paths
from clippie.analyzers import cache, faces


@pytest.fixture
def clean_path_environment(monkeypatch):
    for name in ('CLIPPIE_DATA_DIR', 'CLIPPIE_RESOURCE_DIR', 'CLIPPIE_WHISPER_MODEL',
                 'CLIPPIE_EMBEDDING_MODEL', 'CLIPPIE_EDITOR_MODEL', 'CLIPPIE_YUNET_MODEL'):
        monkeypatch.delenv(name, raising=False)


def test_source_defaults_do_not_create_directories(clean_path_environment):
    package = Path(paths.__file__).resolve().parent
    assert paths.resource_root() == package
    assert paths.data_root() == package.parent / 'data'


def test_relocated_data_root_is_shared_by_models_and_analyzer_cache(tmp_path, monkeypatch, clean_path_environment):
    root = tmp_path / 'Application Support' / 'Clippie 雪'
    monkeypatch.setenv('CLIPPIE_DATA_DIR', str(root))
    assert local_models.converted_model_path() == root / 'models/whisper-small-mlx'
    assert analysis.model_directory() == root / 'models/embeddinggemma-2'
    assert local_editor.model_directory() == root / 'models/qwen3-4b-editor-4bit'
    assert faces.model_path() == root / 'models' / faces.MODEL_NAME
    assert cache.data_dir() == root
    assert not root.exists(), 'Resolving a location must not write into it.'


def test_resource_override_does_not_redirect_writable_state(tmp_path, monkeypatch, clean_path_environment):
    bundle = tmp_path / 'Clippie.app/Contents/Resources/app/clippie'
    monkeypatch.setenv('CLIPPIE_RESOURCE_DIR', str(bundle))
    assert paths.resource_root() == bundle
    assert paths.data_root() == Path(paths.__file__).resolve().parent.parent / 'data'
    monkeypatch.setenv('CLIPPIE_DATA_DIR', str(tmp_path / 'writable'))
    assert paths.data_root() == tmp_path / 'writable'
    assert paths.resource_root() == bundle
    assert not bundle.exists()


def test_model_overrides_expand_home_and_win_over_data_root(tmp_path, monkeypatch, clean_path_environment):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('CLIPPIE_DATA_DIR', '~/data')
    monkeypatch.setenv('CLIPPIE_EMBEDDING_MODEL', '~/selected/embedding')
    monkeypatch.setenv('CLIPPIE_EDITOR_MODEL', '~/selected/editor')
    monkeypatch.setenv('CLIPPIE_YUNET_MODEL', '~/selected/custom-name.onnx')
    assert paths.data_root() == tmp_path / 'data'
    assert analysis.model_directory() == tmp_path / 'selected/embedding'
    assert local_editor.model_directory() == tmp_path / 'selected/editor'
    assert faces.model_path() == tmp_path / 'selected/custom-name.onnx'
    monkeypatch.setenv('CLIPPIE_EDITOR_MODEL', '')
    assert local_editor.model_directory() == tmp_path / 'data/models/qwen3-4b-editor-4bit'


def test_relative_overrides_resolve_from_current_working_directory(tmp_path, monkeypatch, clean_path_environment):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('CLIPPIE_DATA_DIR', 'relative data')
    root = paths.data_root()
    assert root == tmp_path / 'relative data'
    assert root.is_absolute()


def test_whisper_conversion_reuses_relocated_prepared_model_without_user_cache(tmp_path, monkeypatch, clean_path_environment):
    monkeypatch.setenv('HOME', str(tmp_path / 'empty home'))
    monkeypatch.setenv('CLIPPIE_DATA_DIR', str(tmp_path / 'app data'))
    monkeypatch.setenv('CLIPPIE_WHISPER_MODEL', 'organization/remote-model')
    target = local_models.converted_model_path()
    target.mkdir(parents=True)
    (target / 'weights.safetensors').write_bytes(b'already prepared')
    (target / 'config.json').write_text('{}')
    assert local_models.convert_cached_small() == target
    assert target == tmp_path / 'app data/models/whisper-small-mlx'
    assert not (tmp_path / 'empty home').exists()


def load_script(name):
    source = Path(__file__).resolve().parents[1] / 'scripts' / f'{name}.py'
    spec = importlib.util.spec_from_file_location(name, source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('script_name,model_module,override', [
    ('prepare_embeddings', analysis, 'CLIPPIE_EMBEDDING_MODEL'),
    ('prepare_editor', local_editor, 'CLIPPIE_EDITOR_MODEL'),
])
def test_preparation_and_readiness_use_same_override_without_network(
        script_name, model_module, override, tmp_path, monkeypatch, clean_path_environment):
    target = tmp_path / 'external models' / script_name
    target.mkdir(parents=True)
    payload = b'preverified local checkpoint fixture'
    (target / 'model.safetensors').write_bytes(payload)
    monkeypatch.setenv('CLIPPIE_DATA_DIR', str(tmp_path / 'other data'))
    monkeypatch.setenv(override, str(target))
    script = load_script(script_name)
    monkeypatch.setattr(script, 'FILES', ['model.safetensors'])
    monkeypatch.setattr(script, 'MODEL_SHA', hashlib.sha256(payload).hexdigest())

    class NoNetworkClient:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def stream(self, *args, **kwargs): pytest.fail('Prepared model should require no network.')

    monkeypatch.setattr(script.httpx, 'Client', NoNetworkClient)
    script.prepare()
    assert model_module.model_directory() == target
    state = model_module.semantic_status() if model_module is analysis else model_module.status()
    assert state['weights_ready'] is True
    assert (target / 'clippie-model.json').is_file()
    assert not (tmp_path / 'other data').exists()


def test_vision_preparation_honors_overridden_filename(tmp_path, monkeypatch, capsys, clean_path_environment):
    target = tmp_path / 'selected/custom-detector.onnx'
    target.parent.mkdir()
    target.write_bytes(b'preverified model')
    monkeypatch.setenv('CLIPPIE_YUNET_MODEL', str(target))
    monkeypatch.setattr(faces, 'MODEL_SHA256', hashlib.sha256(target.read_bytes()).hexdigest())
    script = load_script('prepare_vision')
    assert script.main([]) == 0
    assert capsys.readouterr().out.strip() == str(target)
    assert faces.model_path() == target
    assert not (target.parent / faces.MODEL_NAME).exists()
