"""Upload streaming and caps, source import, source list, thumbnails, SPA route, fonts and traversal."""
import subprocess

import pytest

from test_studio_support import fake_engine, studio, video_bytes  # noqa: F401

from clippie import studio_api, studio_media
from clippie import app as module

API = '/api/studio'


def upload(studio, content=None, name='talk.mp4', content_type='video/mp4', **fields):
    content = studio.media.read_bytes() if content is None else content
    return studio.client.post(f'{API}/uploads', files={'file': (name, content, content_type)}, data=fields)


def upload_dirs(studio):
    root = studio.root / 'uploads'
    return sorted(root.iterdir()) if root.exists() else []


def test_upload_streams_to_data_uploads_and_starts_a_normal_source_job(studio):
    response = upload(studio, title='My talk')
    assert response.status_code == 202, response.text
    body = response.json()
    assert body['status'] == 'queued'
    job = module.STORE.get(body['source_id'])
    path = studio.root / 'uploads' / job['source'].split('/')[-2] / 'talk.mp4'
    assert job['source'] == str(path) and path.read_bytes() == studio.media.read_bytes()
    assert job['transcriber'] == 'pending' and job['selector'] == 'heuristic'
    assert job['options']['transcriber'] == 'auto'
    assert job['studio_title'] == 'My talk'
    listed = {item['id']: item for item in studio.client.get(f'{API}/sources').json()}
    assert listed[body['source_id']]['title'] == 'My talk'
    assert studio.client.get(f'{API}/jobs/{body["source_id"]}').status_code == 404  # sources are not studio jobs


def test_upload_title_defaults_to_the_file_name(studio):
    source_id = upload(studio, name='Board meeting.mov').json()['source_id']
    titles = {item['id']: item['title'] for item in studio.client.get(f'{API}/sources').json()}
    assert titles[source_id] == 'Board meeting'


def test_upload_size_cap_is_enforced_and_leaves_nothing_behind(studio, monkeypatch):
    monkeypatch.setenv('CLIPPIE_STUDIO_MAX_UPLOAD_BYTES', str(50_000))
    response = upload(studio)
    assert response.status_code == 413 and 'larger than' in response.json()['detail']
    assert upload_dirs(studio) == []
    assert [job['id'] for job in module.STORE.all()] == ['src']


def test_upload_streaming_parser_writes_in_bounded_pieces(studio, monkeypatch):
    seen = []
    original = studio_media._Collector.on_part_data

    def spy(self, data, start, end):
        seen.append(end - start)
        return original(self, data, start, end)
    monkeypatch.setattr(studio_media._Collector, 'on_part_data', spy)
    assert upload(studio).status_code == 202
    assert sum(seen) == studio.media.stat().st_size


def test_upload_rejects_non_video_unreadable_and_malformed_requests(studio):
    assert upload(studio, content_type='text/plain').status_code == 422
    assert upload(studio, content_type='image/png').status_code == 422
    garbage = upload(studio, content=b'not a video at all' * 100)
    assert garbage.status_code == 422 and 'readable video' in garbage.json()['detail']
    assert upload(studio, content=b'').status_code == 422
    assert studio.client.post(f'{API}/uploads', json={'file': 'x'}).status_code == 422
    assert studio.client.post(f'{API}/uploads', files={'other': ('a.mp4', b'x', 'video/mp4')}).status_code == 422
    assert studio.client.post(f'{API}/uploads', data={'title': 'no file'}, files={'x': ('', b'')}).status_code == 422
    two = studio.client.post(f'{API}/uploads', files=[('file', ('a.mp4', b'x', 'video/mp4')), ('file', ('b.mp4', b'x', 'video/mp4'))])
    assert two.status_code == 422
    assert studio.client.post(f'{API}/uploads', files={'file': ('a.mp4', b'x', 'video/mp4')},
                              data={'title': 't' * 5000}).status_code == 422
    assert upload_dirs(studio) == []
    assert [job['id'] for job in module.STORE.all()] == ['src']


def test_upload_filename_cannot_escape_the_upload_directory(studio):
    for name in ['../../evil.mp4', '/etc/passwd.mp4', '..\\..\\evil.mp4', 'a/b/../../../c.mp4', '.hidden', 'x\x00y.mp4']:
        response = upload(studio, name=name)
        assert response.status_code == 202, (name, response.text)
        job = module.STORE.get(response.json()['source_id'])
        target = studio_media.Path(job['source']).resolve()
        assert target.is_relative_to((studio.root / 'uploads').resolve()), name
    assert not (studio.root.parent / 'evil.mp4').exists() and not (studio.root / 'evil.mp4').exists()


def test_safe_filename_rules():
    assert studio_media.safe_filename('../../a b.MP4') == 'a b.MP4'
    assert studio_media.safe_filename('') == 'upload'
    assert studio_media.safe_filename('...') == 'upload'
    assert studio_media.safe_filename('x' * 300 + '.mp4').endswith('.mp4') and len(studio_media.safe_filename('x' * 300 + '.mp4')) <= 109
    assert studio_media.safe_filename('naïve ☃.mp4') == 'na_ve _.mp4'


def test_add_source_by_path_and_url(studio, tmp_path):
    local = tmp_path / 'other.mp4'
    local.write_bytes(b'x')
    created = studio.client.post(f'{API}/sources', json={'path': str(local), 'title': 'Local one'})
    assert created.status_code == 202 and created.json()['status'] == 'queued'
    assert module.STORE.get(created.json()['source_id'])['studio_title'] == 'Local one'
    remote = studio.client.post(f'{API}/sources', json={'url': 'https://example.com/watch?v=1'})
    assert remote.status_code == 202
    assert module.STORE.get(remote.json()['source_id'])['source'] == 'https://example.com/watch?v=1'
    for invalid in [{}, {'url': 'https://a.example/x', 'path': str(local)}, {'url': 'ftp://a.example/x'},
                    {'path': str(tmp_path / 'missing.mp4')}, {'path': str(local), 'extra': 1}, {'path': str(local), 'title': 'x' * 300}]:
        assert studio.client.post(f'{API}/sources', json=invalid).status_code == 422, invalid


def test_source_list_shape(studio):
    item = next(item for item in studio.client.get(f'{API}/sources').json() if item['id'] == 'src')
    assert item == {'id': 'src', 'title': 'Sal Khan on Transforming Education', 'status': 'ready', 'progress': 1.0,
                    'duration': 10.0, 'thumbnail_url': f'{API}/sources/src/thumbnail', 'media_url': '/api/jobs/src/media',
                    'error': None, 'message': 'Ready', 'kind': 'file', 'stages': [], 'queue': None, 'stalled': False,
                    'stall_reason': None, 'eta_seconds': None, 'quality': None, 'range': None, 'cancellable': False,
                    'retryable': False, 'freed': False, 'dismissable': False}
    module.STORE.save({'id': 'pending', 'title': 'Waiting', 'status': 'transcribing', 'progress': 'Transcribing…', 'duration': None})
    module.STORE.save({'id': 'broken', 'title': 'Broken', 'status': 'failed', 'error': 'No usable video stream.', 'progress': 'x'})
    by_id = {item['id']: item for item in studio.client.get(f'{API}/sources').json()}
    assert by_id['pending']['progress'] == 0.5 and by_id['pending']['thumbnail_url'] is None and by_id['pending']['media_url'] is None
    assert by_id['broken']['status'] == 'failed' and by_id['broken']['error'] == 'No usable video stream.'


def test_source_thumbnail_is_a_cached_jpeg(studio):
    first = studio.client.get(f'{API}/sources/src/thumbnail')
    assert first.status_code == 200 and first.headers['content-type'] == 'image/jpeg'
    assert first.content[:3] == b'\xff\xd8\xff'
    cache = studio.root / 'studio' / 'thumbnails'
    files = sorted(cache.iterdir())
    assert len(files) == 1
    assert studio.client.get(f'{API}/sources/src/thumbnail').content == first.content
    assert sorted(cache.iterdir()) == files
    assert studio.client.get(f'{API}/sources/unknown/thumbnail').status_code == 404
    assert studio.client.get(f'{API}/sources/..%2f..%2fx/thumbnail').status_code == 404
    module.STORE.save({'id': 'nodur', 'title': 'x', 'status': 'queued'})
    assert studio.client.get(f'{API}/sources/nodur/thumbnail').status_code == 404


def test_thumbnail_ffmpeg_has_a_timeout_and_failure_is_a_404(studio, monkeypatch):
    calls = []

    def hang(command, **kwargs):
        calls.append(kwargs)
        raise subprocess.TimeoutExpired(command, kwargs['timeout'])
    monkeypatch.setattr(studio_media.subprocess, 'run', hang)
    assert studio.client.get(f'{API}/sources/src/thumbnail').status_code == 404
    assert calls and calls[0]['timeout'] == studio_media.THUMBNAIL_TIMEOUT
    assert not list((studio.root / 'studio' / 'thumbnails').glob('*'))


def test_clip_thumbnail_uses_the_first_frame_of_the_clip(studio):
    _, short, _ = studio.clip()
    response = studio.client.get(f'{API}/clips/{short["id"]}/thumbnail')
    assert response.status_code == 200 and response.content[:3] == b'\xff\xd8\xff'
    assert studio.client.get(f'{API}/clips/missing/thumbnail').status_code == 404


# -- SPA, fonts, traversal ----------------------------------------------------

def test_studio_route_without_a_build_is_a_friendly_404(studio, tmp_path, monkeypatch):
    monkeypatch.setattr(studio_api, 'STATIC', tmp_path / 'nothing')
    for path in ['/studio', '/studio/', '/studio/projects/abc']:
        response = studio.client.get(path)
        assert response.status_code == 404 and 'not been built' in response.text, path
        assert response.headers['content-type'].startswith('text/plain')


@pytest.fixture
def build(tmp_path, monkeypatch):
    root = tmp_path / 'static' / 'studio'
    (root / 'assets').mkdir(parents=True)
    (root / 'index.html').write_text('<!doctype html><title>Studio</title><div id=root></div>')
    (root / 'assets' / 'app-1a2b.js').write_text('console.log("studio")')
    (tmp_path / 'static' / 'secret.txt').write_text('TOP SECRET')
    (root / 'secret-in-build.txt').write_text('IN BUILD ROOT')
    monkeypatch.setattr(studio_api, 'STATIC', root)
    return root


def test_studio_serves_the_spa_with_history_fallback_and_hashed_assets(studio, build):
    for path in ['/studio', '/studio/', '/studio/projects/p1', '/studio/clips/c1/export']:
        response = studio.client.get(path)
        assert response.status_code == 200 and '<div id=root>' in response.text, path
        assert response.headers['cache-control'] == 'no-store'
        assert response.headers['x-content-type-options'] == 'nosniff'
    asset = studio.client.get('/studio/assets/app-1a2b.js')
    assert asset.status_code == 200 and asset.text == 'console.log("studio")'
    assert 'immutable' in asset.headers['cache-control']
    assert studio.client.get('/studio/assets/missing.js').status_code == 404
    assert studio.client.get('/studio/assets/').status_code == 404


def test_studio_static_routes_reject_traversal(studio, build):
    attempts = ['/studio/assets/%2e%2e/secret-in-build.txt', '/studio/assets/..%2fsecret-in-build.txt',
                '/studio/assets/%2e%2e%2f%2e%2e%2fsecret.txt', '/studio/assets//etc/passwd',
                '/studio/assets/%2fetc%2fpasswd', '/studio/assets/..%5csecret.txt']
    for path in attempts:
        response = studio.client.get(path)
        assert 'SECRET' not in response.text and 'IN BUILD ROOT' not in response.text and 'root:' not in response.text, path
        assert response.status_code == 404, path
    assert studio.client.get('/studio/assets/%2e%2e/secret-in-build.txt').status_code == 404
    assert studio.client.get('/studio/assets//etc/passwd').status_code == 404
    assert studio.client.get('/studio/secret-in-build.txt').text.startswith('<!doctype html>')  # only index and assets are served


def test_frontend_notices_are_served_as_files_and_revalidated(studio, build):
    (build / 'third-party-notices.txt').write_text('Copyright and font license fixture')
    (build / 'third-party-notices.provenance.json').write_text('{"schema_version":1}')
    notice = studio.client.get('/studio/third-party-notices.txt')
    assert notice.status_code == 200 and notice.text == 'Copyright and font license fixture'
    assert notice.headers['content-type'].startswith('text/plain')
    assert notice.headers['cache-control'] == 'no-cache'
    provenance = studio.client.get('/studio/third-party-notices.provenance.json')
    assert provenance.json() == {'schema_version': 1}
    (build / 'third-party-notices.txt').unlink()
    assert studio.client.get('/studio/third-party-notices.txt').status_code == 404


def test_fonts_are_served_by_strict_file_name(studio, tmp_path, monkeypatch):
    fonts = tmp_path / 'fonts'
    fonts.mkdir()
    (fonts / 'Montserrat-ExtraBold.ttf').write_bytes(b'\x00\x01\x00\x00font')
    (fonts / 'notes.txt').write_text('no')
    (tmp_path / 'secret.ttf').write_bytes(b'secret')
    monkeypatch.setattr(studio_api, 'FONTS_DIR', fonts)
    ok = studio.client.get('/static/fonts/Montserrat-ExtraBold.ttf')
    assert ok.status_code == 200 and ok.content == b'\x00\x01\x00\x00font' and 'immutable' in ok.headers['cache-control']
    for bad in ['notes.txt', '..%2fsecret.ttf', '%2e%2e%2fsecret.ttf', '.hidden.ttf', 'a%2fb.ttf', 'x.ttf%00.txt',
                'missing.ttf', 'Montserrat-ExtraBold.ttf/', '..%5csecret.ttf', 'a b.ttf']:
        assert studio.client.get(f'/static/fonts/{bad}').status_code == 404, bad
    assert studio.client.get('/static/app.css').status_code == 404  # legacy assets are gone


def test_existing_routes_are_unaffected(studio):
    assert studio.client.get('/api/health').status_code == 200
    assert studio.client.get('/').status_code == 200
    assert studio.client.get('/api/collections').status_code == 200


def test_studio_routes_stay_behind_the_local_only_and_origin_checks(studio):
    foreign_host = studio.client.get(f'{API}/sources', headers={'Host': 'example.com'})
    assert foreign_host.status_code == 403
    cross_origin = studio.client.post(f'{API}/brand-kits', headers={'Origin': 'https://evil.example'},
                                      json={'name': 'x', 'template_id': 'bold_pop'})
    assert cross_origin.status_code == 403 and studio.client.get(f'{API}/brand-kits').json() == []
    forged_upload = studio.client.post(f'{API}/uploads', headers={'Origin': 'https://evil.example'},
                                       files={'file': ('a.mp4', studio.media.read_bytes(), 'video/mp4')})
    assert forged_upload.status_code == 403 and upload_dirs(studio) == []
    assert studio.client.get('/studio', headers={'Host': 'example.com'}).status_code == 403
