"""Read-only local operational view and a strictly typed interaction endpoint."""
import json
import math
import time
from collections import Counter, defaultdict

from fastapi import APIRouter, HTTPException

from . import telemetry

router = APIRouter(prefix='/api/system')


def decoded(rows):
    for row in rows:
        try:
            row['attributes'] = telemetry.safe_attributes(json.loads(row['attributes']))
        except (ValueError,TypeError,AttributeError):
            row['attributes'] = {}
    return rows


def percentile(values, q):
    if not values:
        return None
    values = sorted(values)
    return round(values[math.ceil((len(values)-1)*q)], 3)


@router.get('/summary')
def summary(observation: str = 'user', days: int = 30):
    if observation not in {'user', 'qa', 'synthetic', 'all'} or not 1 <= days <= 30:
        raise HTTPException(422, 'Choose a valid observation group and a window from 1 to 30 days.')
    rec = telemetry.recorder()
    cutoff = time.time_ns()-days*86400*10**9
    where = 'created_ns>=?'+(' AND observation=?' if observation != 'all' else '')
    params = (cutoff, observation) if observation != 'all' else (cutoff,)
    runs = decoded(rec.read(f'SELECT * FROM runs WHERE {where} ORDER BY created_ns DESC LIMIT 5001', params))
    truncated = len(runs) > 5000
    runs = runs[:5000]
    events = decoded(rec.read('SELECT * FROM events WHERE at_ns>=?'+(' AND observation=?' if observation != 'all' else '')+' ORDER BY at_ns DESC LIMIT 20001', params))
    event_truncated = len(events) > 20000
    events = events[:20000]
    terminal = [r for r in runs if r['status'] in {'success', 'failed', 'interrupted'}]
    production = [r for r in terminal if r['workflow'] in {'production', 'replan'} and r['attributes'].get('auto_render')]
    ready = [r for r in production if r['status'] == 'success']
    groups = defaultdict(list)
    for run in terminal:
        groups[(run['workflow'], run['attributes'].get('cache_state', 'unknown'))].append(run)
    performance = []
    for (workflow, cache), values in sorted(groups.items()):
        durations = [(r['ended_ns']-r['created_ns'])/1e9 for r in values if r['status'] == 'success']
        performance.append({'workflow': workflow, 'cache_state': cache, 'attempts': len(values),
            'successes': len(durations), 'failed': sum(r['status'] == 'failed' for r in values),
            'interrupted': sum(r['status'] == 'interrupted' for r in values),
            'p50_seconds': percentile(durations, .5), 'p95_seconds': percentile(durations, .95)})
    # Reviews are decisions about a specific saved revision, not repeated clicks.
    decisions = {}
    edits = []
    for item in reversed(events):
        attrs = item['attributes']
        if item['name'] == 'review_saved':
            decisions[(attrs.get('production_id'), attrs.get('revision'))] = attrs.get('rating')
        if item['name'] == 'timeline_saved':
            edits.append(attrs.get('changed_clip_count', 0))
    counts = Counter(decisions.values())
    return {'retention': {'days':30,'terminal_runs':5000,'events':20000,'completed_spans':50000}, 'window_days': days, 'observation': observation, 'sample_limited': truncated or event_truncated,
        'attempts': len(runs), 'terminal_attempts': len(terminal),
        'failures': sum(r['status'] == 'failed' for r in terminal),
        'interruptions': sum(r['status'] == 'interrupted' for r in terminal),
        'ready_video_attempts': len(ready), 'video_attempts': len(production),
        'time_to_ready_p50_seconds': percentile([(r['ended_ns']-r['created_ns'])/1e9 for r in ready], .5),
        'reviews': {'reviewed_revisions': len(decisions), **{rating: counts[rating] for rating in ('keep', 'revise', 'reject')}},
        'interactions': dict(Counter(e['name'] for e in events)),
        'editing': {'saved_revisions': len(edits), 'changed_clips_p50': percentile(edits, .5)},
        'performance': performance, 'recent_runs': runs[:50],
        'diagnostics': {**rec.counters, 'pending': rec.queue.qsize(), 'last_error': rec.last_error,
            'session_id': rec.session, 'writer_alive': rec.thread.is_alive()},
        'scope': 'Recorded since telemetry was installed. Successful timing samples only; queue wait included. Warm/cold refers to workflow cache reuse/build, not whether a model was already loaded. Playback and download clicks are not editorial acceptance. MLX peak memory is model-reported, not total system RAM. Cloud cost is unknown.'}


@router.get('/runs/{run_id}')
def run_detail(run_id: str):
    if not telemetry._SAFE_ID.fullmatch(run_id):
        raise HTTPException(404, 'Run not found')
    rec = telemetry.recorder()
    runs = decoded(rec.read('SELECT * FROM runs WHERE id=?', (run_id,)))
    if not runs:
        raise HTTPException(404, 'Run not found')
    run = runs[0]
    return {**run, 'spans': decoded(rec.read('SELECT * FROM spans WHERE trace_id=? ORDER BY start_ns', (run['trace_id'],))),
            'events': decoded(rec.read('SELECT * FROM events WHERE run_id=? ORDER BY at_ns', (run_id,)))}
