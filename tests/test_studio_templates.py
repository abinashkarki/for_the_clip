from fastapi.testclient import TestClient

from clippie import app as module, caption_templates


def test_templates_return_full_precision_font_metrics():
    with TestClient(module.app) as client:
        body = client.get('/api/studio/templates').json()
    by_file = {item['font']['file']: item['font']['metrics'] for item in body['captions']}
    inter = by_file['Inter-Bold.ttf']
    # Inter's ascender is 1984/2048 em; a three-decimal value (0.969) would shift preview baselines.
    assert inter['ascent'] == 0.96875
    assert abs(inter['descent'] - 0.24121) < 1e-4
    for item in body['captions']:
        metrics = item['font']['metrics']
        assert metrics == caption_templates.font_metrics(item['font']['file'])
        assert round(metrics['ascent'], 3) != metrics['ascent'] or round(metrics['descent'], 3) != metrics['descent']
