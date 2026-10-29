import copy
import pytest
from clippie import source_framing as framing


def samples():
    return [{'faces': [[.43,.3,.14,.25]], 'layout_uncertain': False,
             'width': 320, 'height': 180} for _ in range(3)]


def test_safe_static_geometry():
    crop, reason = framing._decision(samples(), [])
    assert crop == {'mode':'fill', 'zoom':1, 'x':.5, 'y':.5}
    assert 'stable' in reason


@pytest.mark.parametrize('case', ['cut','split','unknown','text','movement','tiny','edge'])
def test_uncertainty_fits(case):
    frames = samples(); cuts = []
    if case == 'cut': cuts = [.8]
    if case == 'split': frames[1]['faces'].append([.1,.2,.2,.3])
    if case == 'unknown': frames[1].pop('layout_uncertain')
    if case == 'text': frames[0]['layout_uncertain'] = True
    if case == 'movement': frames[2]['faces'][0][0] = .7
    if case == 'tiny':
        for frame in frames: frame['faces'] = [[.4,.4,.03,.05]]
    if case == 'edge':
        for frame in frames: frame['faces'] = [[0,.2,.2,.3]]
    assert framing._decision(frames,cuts)[0]['mode'] == 'fit'


def setup_local(monkeypatch, tmp_path):
    media = tmp_path/'video.mp4'; media.write_bytes(b'video')
    source = {'id':'s','duration':400,'source_path':str(media)}
    monkeypatch.setattr(framing, '_native_backend', lambda work: lambda path: {
        'faces':[[.43,.3,.14,.25]], 'layout_uncertain':False})
    calls = []
    def prepare(source, root, times, owner):
        calls.append(times)
        return {'cache_artifact_key':'fake', 'artifact':{'samples':[
            {'path':'fake.jpg','at':t,'width':320,'height':180} for t in times]}}
    monkeypatch.setattr(framing.source_visuals, 'prepare_samples', prepare)
    monkeypatch.setattr(framing, '_scene_changes', lambda *args: [])
    return source, calls


def test_caps_cache_and_identity(monkeypatch,tmp_path):
    source,calls = setup_local(monkeypatch,tmp_path)
    units = [{'id':str(i),'start':i*10,'end':i*10+5} for i in range(10)]
    original = copy.deepcopy(source)
    run = lambda: framing.recommend_framing(source,units,tmp_path/'cache')
    first = run()
    assert len(calls) == 8
    assert all(r['framing']['mode']=='fill' for r in first['units'][:8])
    assert all(r['reason']=='workload_limit' for r in first['units'][8:])
    assert all(r['cache_status']=='hit' for r in run()['units'][:8])
    assert len(calls)==8
    (tmp_path/'video.mp4').write_bytes(b'changed media')
    run(); assert len(calls)==16
    assert source == original


def test_duration_cap(monkeypatch,tmp_path):
    source,calls = setup_local(monkeypatch,tmp_path)
    result = framing.recommend_framing(source,[{'id':'a','start':0,'end':180},
        {'id':'b','start':180,'end':181}],tmp_path/'cache')
    assert len(calls)==1
    assert result['units'][1]['reason']=='workload_limit'


def test_timeout_and_backend_fallback(monkeypatch,tmp_path):
    source,_ = setup_local(monkeypatch,tmp_path)
    def fail(*args): raise RuntimeError('timed out')
    monkeypatch.setattr(framing,'_scene_changes',fail)
    units = [{'id':'a','start':0,'end':1}]
    result = framing.recommend_framing(source,units,tmp_path/'cache')
    assert result['units'][0]['reason']=='analysis_failed'
    monkeypatch.setattr(framing,'_native_backend',fail)
    assert framing.recommend_framing(source,units,tmp_path/'cache')['units'][0]['reason']=='backend_unavailable'


def test_symlink_cache_fits(monkeypatch,tmp_path):
    source,_ = setup_local(monkeypatch,tmp_path)
    root = tmp_path/'cache'; root.symlink_to(tmp_path,target_is_directory=True)
    result = framing.recommend_framing(source,[{'id':'a','start':0,'end':1}],root)
    assert result['units'][0]['framing']['mode']=='fit'
