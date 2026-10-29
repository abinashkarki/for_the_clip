"""Collection correlation remains typed metadata, not evidence capture."""
import json

import pytest

from clippie import telemetry


@pytest.fixture
def recorder(tmp_path, monkeypatch):
    rec = telemetry.Recorder(tmp_path / 'telemetry')
    monkeypatch.setattr(telemetry, 'recorder', lambda root=None: rec)
    yield rec
    rec.shutdown()


def rows(recorder, table):
    assert recorder.flush()
    result = recorder.read(f'SELECT * FROM {table}')
    return [dict(row, attributes=json.loads(row['attributes'])) for row in result]


def test_collection_correlations_persist_on_real_qa_operation_and_stage(recorder):
    identities = dict(collection_id='a' * 32, short_id='b' * 32,
                      revision_id='c' * 32, task_id='d' * 32, attempt_id='e' * 32)
    budget = dict(reserved_tokens=100, reserved_calls=2, charged_tokens=80, charged_calls=1)
    cohort = telemetry.OBSERVATION.set('qa')
    try:
        with telemetry.operation('collection_prepare', **identities, **budget):
            with telemetry.stage('source_evidence', **identities, **budget):
                telemetry.attributes(cache='hit', cache_kind='evidence')
    finally:
        telemetry.OBSERVATION.reset(cohort)
    run, = rows(recorder, 'runs')
    assert run['workflow'] == 'collection_prepare' and run['observation'] == 'qa'
    assert run['status'] == 'success' and run['attributes']['cache_state'] == 'warm'
    evidence, = [span for span in rows(recorder, 'spans') if span['name'] == 'source_evidence']
    assert evidence['trace_id'] == run['trace_id']
    for entry in (run, evidence):
        assert {key: entry['attributes'][key] for key in identities} == identities
        assert {key: entry['attributes'][key] for key in budget} == budget
    assert evidence['attributes']['cache_kind'] == 'evidence'


def test_collection_content_invalid_ids_and_nonfinite_usage_never_persist(recorder):
    secret = 'private transcript /Users/private/key=secret'
    private = dict(payload={'transcript': secret}, transcript=secret, api_key=secret,
                   exception=secret, collection_id=secret, short_id='https://private',
                   revision_id='../private', task_id=True, attempt_id='x' * 101,
                   reserved_tokens=float('nan'), reserved_calls=True,
                   charged_tokens=float('inf'), charged_calls='secret')
    with pytest.raises(ValueError):
        with telemetry.operation('collection_prepare', **private):
            with telemetry.stage('source_evidence', **private):
                telemetry.attributes(**private)
                raise ValueError(secret)
    stored = rows(recorder, 'runs') + rows(recorder, 'spans')
    assert secret not in json.dumps(stored)
    for entry in stored:
        assert not (private.keys() & entry['attributes'].keys())
    assert rows(recorder, 'runs')[0]['attributes']['error_type'] == 'ValueError'


def test_collection_work_survives_telemetry_initialization_failure(monkeypatch):
    def unavailable(root=None):
        raise OSError('private storage path')
    monkeypatch.setattr(telemetry, 'recorder', unavailable)
    with telemetry.operation('collection_prepare', collection_id='a' * 32) as run:
        assert run is None
        with telemetry.stage('source_evidence'):
            telemetry.attributes(cache='miss', cache_kind='evidence')
            result = 'work completed'
    assert result == 'work completed'
