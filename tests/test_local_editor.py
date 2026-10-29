import subprocess
import json
from io import StringIO
import pytest
from clippie import local_editor


def test_local_editor_timeout_is_visible_and_offline(monkeypatch):
    monkeypatch.setattr(local_editor, 'status', lambda: {'installed': True, 'weights_ready': True})
    def run(*args, **kwargs):
        assert kwargs['env']['HF_HUB_OFFLINE'] == '1'
        assert kwargs['timeout'] == 240
        raise subprocess.TimeoutExpired(args[0], 240)
    monkeypatch.setattr(local_editor.subprocess, 'run', run)
    with pytest.raises(RuntimeError, match='No baseline fallback'):
        local_editor.generate([{'role': 'user', 'content': 'Select source excerpts.'}])


def test_worker_keeps_model_logs_out_of_the_response_protocol(monkeypatch, capsys):
    from clippie import local_editor_worker
    monkeypatch.setattr(local_editor_worker.sys, 'stdin', StringIO('{"messages":[]}'))
    def infer(request):
        print('A model loading warning')
        return {'text': '{"selections":[]}'}
    monkeypatch.setattr(local_editor_worker, 'infer', infer)
    local_editor_worker.main()
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {'text': '{"selections":[]}'}
    assert 'loading warning' in captured.err


def test_background_brief_writer_does_not_queue_behind_active_inference(monkeypatch):
    class BusyLock:
        def acquire(self, blocking):
            assert blocking is False
            return False
    monkeypatch.setattr(local_editor, '_INFERENCE_LOCK', BusyLock())
    monkeypatch.setattr(local_editor.subprocess, 'run', lambda *a, **kw: pytest.fail('Busy model must not launch another worker'))
    with pytest.raises(RuntimeError, match='busy'):
        local_editor.generate([], max_tokens=240, wait=False)
