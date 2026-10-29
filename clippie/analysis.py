"""Reusable source indexing. Embeddings retrieve assets; they do not author a story."""
from __future__ import annotations

from . import telemetry
from .paths import model_directory as runtime_model_directory
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import subprocess
import threading
import time

import numpy as np

MODEL_ID = 'google/embeddinggemma-2'
MODEL_REVISION = '914f7f89142e33e77833254d9c9b90c3cef7303b'
_MODEL = None
_MODEL_LOCK = threading.RLock()


def model_directory():
    return runtime_model_directory('embeddinggemma-2', override='CLIPPIE_EMBEDDING_MODEL')


def semantic_status():
    path = model_directory()
    return {'model':MODEL_ID,'revision':MODEL_REVISION,
            'installed': importlib.util.find_spec('sentence_transformers') is not None,
            'weights_ready': (path/'model.safetensors').is_file(),
            'scope':'text passages and sampled images; no full-motion or audio-event analysis',
            'device':'cpu','precision':'float32'}


def _load_model():
    global _MODEL
    with _MODEL_LOCK:
        if _MODEL is None:
            if not semantic_status()['installed']:
                raise RuntimeError('Install semantic dependencies with uv sync --extra local --extra semantic --extra dev.')
            path = model_directory()
            if not (path/'model.safetensors').is_file():
                raise RuntimeError('EmbeddingGemma weights are not prepared. Run uv run --extra semantic python scripts/prepare_embeddings.py, then index the source.')
            os.environ.setdefault('HF_HUB_DISABLE_XET','1')
            os.environ.setdefault('TOKENIZERS_PARALLELISM','false')
            import torch
            from sentence_transformers import SentenceTransformer
            torch.set_num_threads(max(1,min(4,os.cpu_count() or 1)))
            # M1 does not support BF16 natively. Never use FP16 for this model.
            _MODEL = SentenceTransformer(str(path),device='cpu',
                model_kwargs={'torch_dtype':torch.float32},config_kwargs={'audio_config':None},local_files_only=True)
        return _MODEL


def encode(items, query=False):
    if not items:
        return np.empty((0,768),dtype=np.float32)
    with _MODEL_LOCK:
        model = _load_model()
        kwargs = {'batch_size':2,'normalize_embeddings':True,'convert_to_numpy':True,'show_progress_bar':False}
        if query:
            kwargs['prompt_name']='SearchQuery'
        vector = np.asarray(model.encode(items,**kwargs),dtype=np.float32)
    if vector.ndim==1:
        vector=vector.reshape(1,-1)
    if vector.shape[1] != 768 or not np.all(np.isfinite(vector)):
        raise RuntimeError('EmbeddingGemma produced invalid vectors; no index was saved.')
    norm=np.linalg.norm(vector,axis=1,keepdims=True)
    if np.any(norm<1e-10):
        raise RuntimeError('EmbeddingGemma produced an empty vector; no index was saved.')
    return vector/norm


def source_fingerprint(source):
    path=Path(source['source_path'])
    st=path.stat()
    payload={'path':str(path.resolve()),'size':st.st_size,'mtime_ns':st.st_mtime_ns,
             'words':source.get('words',[]),'model_revision':MODEL_REVISION,'index_version':1}
    return hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest()


def passages(source, max_seconds=24):
    """Sentence-sized passages, grouping very short lines without cutting speech."""
    from .selection import build_segments
    words=source.get('words') or []
    segments=build_segments(words) if words else []
    items=[]
    group=[]
    def flush():
        if group:
            items.append({'source_id':source['id'],'start':group[0]['start'],'end':group[-1]['end'],
                'text':' '.join(s['text'] for s in group),'modality':'text', 'id':f"{source['id']}:p{len(items)}"})
            group.clear()
    for segment in segments:
        if group and (segment['end']-group[0]['start']>max_seconds or segment['start']-group[-1]['end']>2):
            flush()
        group.append(segment)
        if segment['end']-group[0]['start']>=8:
            flush()
    flush()
    return items


@telemetry.instrument('index')
def index_source(source, directory:Path, embeddings=False, progress=None):
    directory=Path(directory)
    directory.mkdir(parents=True,exist_ok=True)
    fingerprint=source_fingerprint(source)
    state_path=directory/'analysis.json'
    if state_path.is_file():
        cached=json.loads(state_path.read_text())
        if cached.get('fingerprint')==fingerprint and (not embeddings or cached.get('semantic')):
            telemetry.attributes(source_id=source['id'], cache='hit', cache_kind='index', use_embeddings=embeddings, item_count=cached.get('item_count',0), model_id=cached.get('model'),model_revision=cached.get('model_revision'))
            return cached
    telemetry.attributes(source_id=source['id'],cache='miss',cache_kind='index',use_embeddings=embeddings,model_id=MODEL_ID if embeddings else None,model_revision=MODEL_REVISION if embeddings else None)
    started=time.monotonic()
    items=passages(source)
    source_duration=float(source['duration'])
    if not math.isfinite(source_duration) or source_duration<=0:
        raise ValueError('Source duration must be positive.')
    # Uniform sampling is explicitly a coverage aid, not a claim of scene understanding.
    max_frames=int(os.getenv('CLIPPIE_INDEX_MAX_FRAMES','72'))
    max_frames=max(1,min(240,max_frames))
    spacing=max(8,source_duration/max_frames)
    ffmpeg=os.getenv('CLIPPIE_FFMPEG','ffmpeg')
    frames_dir=directory/'frames'
    frames_dir.mkdir(exist_ok=True)
    frame_times=[min(source_duration-.04,(i+.5)*spacing) for i in range(max(1,int(math.ceil(source_duration/spacing))))]
    visual=[]
    for i,at in enumerate(frame_times):
        path=frames_dir/f'frame-{i:04d}.jpg'
        if not path.is_file():
            result=subprocess.run([ffmpeg,'-y','-v','error','-ss',str(at),'-i',source['source_path'],
                '-frames:v','1','-vf','scale=384:-2','-q:v','4',str(path)],capture_output=True,text=True,timeout=30)
            if result.returncode or not path.is_file():
                raise RuntimeError('Could not extract a source frame: '+result.stderr[-500:])
        visual.append({'id':f"{source['id']}:v{i}",'source_id':source['id'],'start':max(0,at-2),
                       'end':min(source_duration,at+2),'at':at,'text':'Sampled source image',
                       'modality':'image','frame_path':str(path.resolve()),'thumbnail':f"/api/analysis/{source['id']}/frames/{path.name}"})
        if progress:
            progress(f'Extracting visual coverage {i+1}/{len(frame_times)}…')
    items.extend(visual)
    warnings=['Images are sampled coverage, not exact shot boundaries. Visual matches need review.']
    if embeddings:
        if progress:
            progress(f'Embedding {len(items)} text passages and sampled images on CPU…')
        inputs=[f"title: {source.get('title','none')} | text: {item['text']}" if item['modality']=='text' else {'image':item['frame_path']} for item in items]
        # Keep model encode batches small and explicitly separate modality batches.
        vectors=[]
        for item in inputs:
            vectors.append(encode([item])[0])
            if progress:
                progress(f'Embedding source assets {len(vectors)}/{len(inputs)}…')
        array=np.stack(vectors) if vectors else np.empty((0,768),dtype=np.float32)
        temp=directory/'embeddings.pending.npy'
        np.save(temp,array)
        temp.replace(directory/'embeddings.npy')
    state={'source_id':source['id'],'status':'ready','fingerprint':fingerprint,'semantic':bool(embeddings),
           'model':MODEL_ID if embeddings else None,'model_revision':MODEL_REVISION if embeddings else None,
           'items':items,'item_count':len(items),'text_count':sum(x['modality']=='text' for x in items),
           'image_count':len(visual),'seconds':round(time.monotonic()-started,2),'warnings':warnings,
           'scope':'text and sampled images' if embeddings else 'text baseline and visual contact sheet'}
    temp=state_path.with_suffix('.pending.json')
    temp.write_text(json.dumps(state,ensure_ascii=False,indent=2))
    temp.replace(state_path)
    return state


@telemetry.instrument('retrieval')
def search(indexes, query, limit=12, use_embeddings=True, modality=None):
    if not query.strip():
        raise ValueError('Enter a search query.')
    results=[]
    q=encode([query],query=True)[0] if use_embeddings else None
    terms=set(re.findall(r'\w+',query.casefold()))
    for directory in indexes:
        directory=Path(directory)
        info=json.loads((directory/'analysis.json').read_text())
        if use_embeddings:
            if not info.get('semantic') or not (directory/'embeddings.npy').is_file():
                raise ValueError('Source has no semantic index. Build its EmbeddingGemma index first.')
            vectors=np.load(directory/'embeddings.npy',allow_pickle=False)
            if vectors.shape != (len(info['items']),768):
                raise ValueError('Index vectors do not match source assets; reindex the source.')
            scores=vectors@q
        else:
            scores=[len(terms&set(re.findall(r'\w+',x['text'].casefold())))/max(1,len(terms)) if x['modality']=='text' else 0 for x in info['items']]
        for item,score in zip(info['items'],scores):
            if modality and item['modality']!=modality:
                continue
            if not use_embeddings and item['modality']!='text':
                continue
            results.append({**item,'score':float(score),'provider':MODEL_ID if use_embeddings else 'keyword baseline'})
    results.sort(key=lambda x:x['score'],reverse=True)
    return results[:max(1,min(100,limit))]


@telemetry.instrument('retrieval')
def story_material(indexes, brief, beats, use_embeddings=True):
    """Keep distinct evidence for the brief and each editorial purpose.

    These are retrieval estimates, not judgments that a passage fulfills a beat.
    Text only: image scores cannot compete with primary narration candidates.
    """
    # Search for editorial function independently of subject. Repeating the
    # whole brief in every query swamps the distinction between an explanation
    # and its conclusion. The brief retains its own separate relevance signal.
    queries = [brief] + [f"{beat['query']}. {beat['purpose']}" for beat in beats]
    queries_vectors = encode(queries, query=True) if use_embeddings else None
    rows = []
    score_rows = []
    for directory in indexes:
        directory = Path(directory)
        info = json.loads((directory/'analysis.json').read_text())
        text_indices = [i for i, item in enumerate(info['items']) if item['modality'] == 'text']
        rows.extend(info['items'][i] for i in text_indices)
        if use_embeddings:
            if not info.get('semantic') or not (directory/'embeddings.npy').is_file():
                raise ValueError('Source has no semantic index. Build its EmbeddingGemma index first.')
            vectors = np.load(directory/'embeddings.npy', allow_pickle=False)
            if vectors.shape != (len(info['items']), 768) or not np.all(np.isfinite(vectors)):
                raise ValueError('Index vectors do not match source assets; reindex the source.')
            score_rows.extend(vectors[text_indices] @ queries_vectors.T)
        else:
            terms = [set(re.findall(r'\w+', query.casefold())) for query in queries]
            for i in text_indices:
                words = set(re.findall(r'\w+', info['items'][i]['text'].casefold()))
                score_rows.append([len(words & query)/max(1, len(query)) for query in terms])
    if not rows:
        return []
    scores = np.asarray(score_rows)
    # Relative relevance removes the high common cosine floor within a source.
    # Record raw cosine separately; never call either a confidence probability.
    floor = np.percentile(scores, 10, axis=0)
    ceiling = np.max(scores, axis=0)
    relative = np.clip((scores-floor)/np.maximum(ceiling-floor, 1e-6), 0, 1)
    pool = set(np.argsort(-scores[:, 0], kind='stable')[:36].tolist())
    for column in range(1, len(queries)):
        pool.update(np.argsort(-scores[:, column], kind='stable')[:16].tolist())
    output = []
    for i in sorted(pool, key=lambda i: (-scores[i, 0], rows[i]['source_id'], rows[i]['start'])):
        output.append({**rows[i], 'score': float(relative[i, 0]), 'raw_score': float(scores[i, 0]),
                       'beat_scores': [float(x) for x in relative[i, 1:]],
                       'provider': MODEL_ID if use_embeddings else 'keyword baseline'})
    return output
