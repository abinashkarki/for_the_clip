"""Templates, brand kit CRUD and brand assets."""
import io

from PIL import Image

from clippie import studio_api

from test_studio_support import fake_engine, no_engine, png_bytes, studio, video_bytes  # noqa: F401

API = '/api/studio'
KIT = {'name': 'Acme', 'template_id': 'bold_pop', 'overrides': {'colors': {'active': '#FFE600'}}, 'hook_style': 'hook_box',
       'progress_bar': {'enabled': True, 'color': '#FFE600'}, 'logo_asset_id': None}


def jpeg_bytes():
    buffer = io.BytesIO()
    Image.new('RGB', (20, 10), (0, 128, 255)).save(buffer, 'JPEG')
    return buffer.getvalue()


def asset(studio, content, name='logo.png', content_type='image/png'):
    return studio.client.post(f'{API}/brand-assets', files={'file': (name, content, content_type)})


def test_templates_lists_captions_hooks_layouts_and_font_base(studio):
    body = studio.client.get(f'{API}/templates').json()
    assert [item['id'] for item in body['captions']] == ['bold_pop', 'boxed_word', 'color_word', 'phrase_clean']
    assert [item['id'] for item in body['hooks']] == ['hook_box', 'hook_outline']
    assert body['layouts'] == ['fill', 'fit', 'stack', 'screen_speaker']
    assert body['fonts_base_url'] == '/static/fonts/'


def test_brand_kit_crud(studio):
    assert studio.client.get(f'{API}/brand-kits').json() == []
    created = studio.client.post(f'{API}/brand-kits', json=KIT)
    assert created.status_code == 201, created.text
    kit = created.json()
    assert {key: kit[key] for key in KIT} == KIT and kit['id'] and kit['logo_url'] is None
    assert kit['created_at'].endswith('Z') and kit['updated_at'].endswith('Z')
    assert studio.client.get(f'{API}/brand-kits').json() == [kit]
    changed = studio.client.put(f'{API}/brand-kits/{kit["id"]}', json={**KIT, 'name': 'Acme 2', 'template_id': 'color_word', 'overrides': {}})
    assert changed.status_code == 200
    updated = changed.json()
    assert updated['id'] == kit['id'] and updated['name'] == 'Acme 2' and updated['template_id'] == 'color_word'
    assert updated['created_at'] == kit['created_at']
    assert studio.client.get(f'{API}/brand-kits').json() == [updated]
    assert studio.client.delete(f'{API}/brand-kits/{kit["id"]}').status_code == 204
    assert studio.client.get(f'{API}/brand-kits').json() == []
    assert studio.client.delete(f'{API}/brand-kits/{kit["id"]}').status_code == 404
    assert studio.client.put(f'{API}/brand-kits/{kit["id"]}', json=KIT).status_code == 404
    assert studio.client.put(f'{API}/brand-kits/bad!id', json=KIT).status_code == 404
    assert studio.client.delete(f'{API}/brand-kits/nope').status_code == 404


def test_brand_kit_validation(studio):
    bad = {
        'unknown template': {**KIT, 'template_id': 'nope'},
        'unknown hook': {**KIT, 'hook_style': 'nope'},
        'override key': {**KIT, 'overrides': {'font_weight': 900}},
        'bad color': {**KIT, 'progress_bar': {'enabled': True, 'color': 'yellow'}},
        'bar extra': {**KIT, 'progress_bar': {'enabled': True, 'color': '#FFFFFF', 'x': 1}},
        'empty name': {**KIT, 'name': '  '},
        'long name': {**KIT, 'name': 'n' * 81},
        'control name': {**KIT, 'name': 'a\nb'},
        'extra field': {**KIT, 'extra': 1},
        'missing name': {key: value for key, value in KIT.items() if key != 'name'},
        'unknown logo': {**KIT, 'logo_asset_id': 'nope'},
        'bad template chars': {**KIT, 'template_id': '../x'},
        'non bool enabled': {**KIT, 'progress_bar': {'enabled': 'yes', 'color': '#FFFFFF'}},
    }
    for name, body in bad.items():
        assert studio.client.post(f'{API}/brand-kits', json=body).status_code == 422, name
    assert studio.client.get(f'{API}/brand-kits').json() == []
    kit = studio.client.post(f'{API}/brand-kits', json=KIT).json()
    assert studio.client.put(f'{API}/brand-kits/{kit["id"]}', json=bad['unknown template']).status_code == 422
    assert studio.client.get(f'{API}/brand-kits').json()[0]['template_id'] == 'bold_pop'


def test_brand_kits_work_without_the_caption_template_module(studio, no_engine):
    created = studio.client.post(f'{API}/brand-kits', json=KIT)
    assert created.status_code == 201
    assert studio.client.get(f'{API}/brand-kits').json()[0]['id'] == created.json()['id']


def test_brand_asset_upload_and_fetch(studio):
    png = asset(studio, png_bytes())
    assert png.status_code == 201, png.text
    body = png.json()
    assert set(body) == {'asset_id', 'url'} and body['url'] == f'{API}/brand-assets/{body["asset_id"]}'
    fetched = studio.client.get(body['url'])
    assert fetched.status_code == 200 and fetched.headers['content-type'] == 'image/png' and fetched.content == png_bytes()
    assert fetched.headers['x-content-type-options'] == 'nosniff'
    jpeg = asset(studio, jpeg_bytes(), 'logo.jpg', 'image/jpeg').json()
    assert studio.client.get(jpeg['url']).headers['content-type'] == 'image/jpeg'
    assert (studio.root / 'studio' / 'brand-assets' / body['asset_id'] / 'logo.png').is_file()
    kit = studio.client.post(f'{API}/brand-kits', json={**KIT, 'logo_asset_id': body['asset_id']}).json()
    assert kit['logo_url'] == body['url']


def test_brand_asset_rejections_leave_nothing_stored(studio, monkeypatch):
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
    gif = io.BytesIO()
    Image.new('P', (4, 4)).save(gif, 'GIF')
    cases = {
        'svg by type': asset(studio, svg, 'logo.svg', 'image/svg+xml'),
        'svg posing as png': asset(studio, svg),
        'gif posing as png': asset(studio, gif.getvalue()),
        'gif type': asset(studio, gif.getvalue(), 'a.gif', 'image/gif'),
        'webp type': asset(studio, png_bytes(), 'a.webp', 'image/webp'),
        'text': asset(studio, b'hello', 'a.txt', 'text/plain'),
        'garbage png': asset(studio, b'\x89PNG\r\n\x1a\ngarbage'),
        'empty': asset(studio, b''),
    }
    for name, response in cases.items():
        assert response.status_code == 422, (name, response.text)
    assert 'SVG' in cases['svg by type'].json()['detail']
    assert studio.client.post(f'{API}/brand-assets', json={}).status_code == 422
    assert studio.client.post(f'{API}/brand-assets', files={'file': ('a.png', png_bytes(), 'image/png')}, data={'title': 'x'}).status_code == 422
    monkeypatch.setattr(studio_api, 'MAX_ASSET_BYTES', 100)
    too_big = asset(studio, png_bytes() + b'0' * 200)
    assert too_big.status_code == 413 and 'larger than' in too_big.json()['detail']
    root = studio.root / 'studio' / 'brand-assets'
    assert not root.exists() or list(root.iterdir()) == []


def test_brand_asset_fetch_validation(studio):
    assert studio.client.get(f'{API}/brand-assets/nope').status_code == 404
    assert studio.client.get(f'{API}/brand-assets/..%2f..%2fstudio.sqlite3').status_code == 404
    created = asset(studio, png_bytes()).json()
    (studio.root / 'studio' / 'brand-assets' / created['asset_id'] / 'logo.png').unlink()
    assert studio.client.get(created['url']).status_code == 404
