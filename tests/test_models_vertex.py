import json

import httpx
import pytest

from clippie.models import ModelError, ModelRequest, gateway
from clippie.models.providers import vertex
from tests.support_models import gemini_stream, install


PROJECT = 'clippie-cloud-test'
SCHEMA = {'type': 'object', 'properties': {'ok': {'type': 'boolean'}}, 'required': ['ok']}


def request(**values):
    return ModelRequest(stage='other', provider='vertex', model='gemini-3.8-flash',
                        prompt='Return ok.', json_schema=SCHEMA,
                        options={'project': PROJECT, **values})


def test_vertex_uses_project_endpoint_adc_and_normal_gateway_log(monkeypatch):
    seen = []
    monkeypatch.setattr(vertex, 'headers', lambda project, timeout: {
        'Authorization': 'Bearer adc-private-token', 'x-goog-user-project': project})
    def handler(req):
        seen.append(req)
        return gemini_stream()
    install(monkeypatch, handler)
    result = gateway.complete(request(), {})
    assert result.json == {'ok': True}
    assert result.provider == 'vertex' and result.cost_usd > 0
    assert seen[0].url.host == 'aiplatform.googleapis.com'
    assert seen[0].url.path == f'/v1/projects/{PROJECT}/locations/global/publishers/google/models/gemini-3.8-flash:streamGenerateContent'
    assert seen[0].headers['Authorization'] == 'Bearer adc-private-token'
    assert seen[0].headers['x-goog-user-project'] == PROJECT
    assert 'x-goog-api-key' not in seen[0].headers
    record = gateway.get_call(result.call_id)
    assert 'adc-private-token' not in json.dumps(record)
    assert record['provider'] == 'vertex'


@pytest.mark.parametrize('options', [{'project': ''}, {'project': '../escape'},
                                    {'project': PROJECT, 'location': 'evil.com/path'}])
def test_bad_cloud_route_cannot_send_credentials(monkeypatch, options):
    monkeypatch.setattr(vertex, 'headers', lambda *args: pytest.fail('Must validate before accessing credentials'))
    req = request()
    req.options = options
    with pytest.raises(ModelError) as error:
        vertex.call(req, req.model, '')
    assert error.value.category == 'config'


def test_adc_refresh_is_reused_and_quota_project_explicit(monkeypatch):
    import google.auth
    seen = []
    class Credentials:
        valid = False
        def refresh(self, transport):
            seen.append('refresh')
            self.valid = True
        def apply(self, headers):
            headers.update(authorization='Bearer private-token', **{'x-goog-user-project': PROJECT})
    credentials = Credentials()
    def default(**kwargs):
        seen.append(kwargs)
        return credentials, None
    monkeypatch.setattr(vertex, '_CREDENTIALS', {})
    monkeypatch.setattr(vertex, 'configured', lambda: True)
    monkeypatch.setattr(google.auth, 'default', default)
    assert vertex.headers(PROJECT, 5)['x-goog-user-project'] == PROJECT
    vertex.headers(PROJECT, 5)
    assert seen == [{'scopes': ['https://www.googleapis.com/auth/cloud-platform'],
                     'quota_project_id': PROJECT}, 'refresh']


def test_missing_adc_blocks_generation_and_has_sign_in_instruction(monkeypatch):
    monkeypatch.setattr(vertex, 'configured', lambda: False)
    monkeypatch.setattr(gateway, 'routing', lambda: {'defaults': {'other': {'provider': 'vertex', 'model': 'gemini-3.8-flash'}}, 'fallbacks': {}})
    assert gateway.keys_message({}) is not None
    with pytest.raises(ModelError) as error:
        vertex.headers(PROJECT, 5)
    assert error.value.category == 'auth' and 'application-default login' in str(error.value)


def test_vertex_quota_errors_are_classified_for_gateway_fallbacks(monkeypatch):
    monkeypatch.setattr(vertex, 'headers', lambda *args: {})
    install(monkeypatch, lambda req: httpx.Response(429, json={'error': {'message': 'quota exceeded'}}))
    with pytest.raises(ModelError) as error:
        gateway.complete(request(), {})
    assert error.value.category == 'quota'
    assert error.value.detail == 'quota exceeded'


@pytest.mark.parametrize('failure', ['quota', 'timeout'])
def test_cloud_fallback_retains_images_after_primary_outage(monkeypatch, failure):
    seen, admitted = [], []
    monkeypatch.setattr(vertex, 'headers', lambda *args: {})
    primary = {'provider': 'vertex', 'model': 'gemini-3.8-flash', 'options': {'project': PROJECT}}
    fallback = {'provider': 'vertex', 'model': 'gemini-3.5-flash-lite', 'options': {'project': PROJECT}}
    gateway.save_routing({'defaults': {'map': primary}, 'fallbacks': {'map': [fallback]}})
    def handler(req):
        seen.append(req)
        if 'gemini-3.8-flash:' in req.url.path:
            if failure == 'timeout':
                raise httpx.ReadTimeout('simulated primary outage')
            return httpx.Response(429, json={'error': {'message': 'Resource exhausted. Please try again later.'}})
        return gemini_stream()
    install(monkeypatch, handler)
    req = ModelRequest(stage='map', prompt='Map this image.', json_schema=SCHEMA,
                       images=[{'mime': 'image/jpeg', 'data': b'test-image'}],
                       before_attempt=lambda identity: admitted.append(identity))
    result = gateway.complete(req, {})
    assert result.provider == 'vertex' and result.model == 'gemini-3.5-flash-lite'
    assert result.cost_usd > 0
    assert result.attempts == 2 and len(admitted) == 2
    assert all(json.loads(r.content)['contents'][0]['parts'][1]['inlineData']['data'] == 'dGVzdC1pbWFnZQ==' for r in seen)
    assert len(gateway.calls(10)) == 2
