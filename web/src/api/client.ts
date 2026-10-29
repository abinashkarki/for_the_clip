// Typed client for /api/studio. Falls back to the mock layer with ?mock=1, or (in dev) when the API is absent.
import { mockApi, mockAssets } from './mock'
import type { StudioApi, UploadHandle } from './types'

const BASE = '/api/studio'

import { ApiError } from './errors'

export { ApiError }

let mockMode = false
try {
  const q = new URLSearchParams(location.search)
  if (q.get('mock') === '1') {
    mockMode = true
    sessionStorage.setItem('clippie-mock', '1')
  } else if (sessionStorage.getItem('clippie-mock') === '1' && q.get('mock') !== '0') {
    mockMode = true
  } else if (q.get('mock') === '0') {
    sessionStorage.removeItem('clippie-mock')
  }
} catch {
  /* storage unavailable */
}

export const isMock = () => mockMode
const listeners = new Set<() => void>()
export function onModeChange(cb: () => void) {
  listeners.add(cb)
  return () => listeners.delete(cb)
}
function enterMock() {
  if (mockMode) return
  mockMode = true
  listeners.forEach((l) => l())
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  let res: Response
  try {
    res = await fetch(BASE + path, {
      method,
      headers: body !== undefined ? { 'Content-Type': 'application/json' } : undefined,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    })
  } catch (e) {
    if (import.meta.env.DEV) throw new MockFallback()
    throw new ApiError(0, 'Cannot reach the Clippie server. Is it running?')
  }
  if (!res.ok) {
    if (res.status === 404 && import.meta.env.DEV && path.split('?')[0].match(/^\/(projects|sources|templates|brand-kits|activity)$/)) throw new MockFallback()
    let detail = res.statusText
    try {
      const j = await res.json()
      detail = typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail)
    } catch {
      /* not json */
    }
    throw new ApiError(res.status, detail)
  }
  if (res.status === 204) return undefined as T
  return (await res.json()) as T
}

class MockFallback extends Error {}

const real: StudioApi = {
  listSources: () => request('GET', '/sources'),
  upload(file, title, onProgress): UploadHandle {
    const xhr = new XMLHttpRequest()
    const promise = new Promise<{ source_id: string; status: string }>((resolve, reject) => {
      xhr.open('POST', BASE + '/uploads')
      xhr.upload.onprogress = (e) => e.lengthComputable && onProgress(e.loaded / e.total)
      xhr.onload = () => {
        try {
          const j = JSON.parse(xhr.responseText)
          if (xhr.status >= 200 && xhr.status < 300) resolve(j)
          else reject(new ApiError(xhr.status, j.detail ?? 'Upload failed'))
        } catch {
          reject(new ApiError(xhr.status, 'Upload failed'))
        }
      }
      xhr.onerror = () => reject(new ApiError(0, 'Upload failed. Check the connection and try again.'))
      xhr.onabort = () => reject(new ApiError(0, 'Upload cancelled'))
      const form = new FormData()
      form.append('file', file)
      if (title) form.append('title', title)
      xhr.send(form)
    })
    return { promise, abort: () => xhr.abort() }
  },
  addSource: (input) => request('POST', '/sources', input),
  inspectSource: (url) => request('POST', '/sources/inspect', { url }),
  cancelSource: (id) => request('POST', `/sources/${encodeURIComponent(id)}/cancel`),
  retrySource: (id) => request('POST', `/sources/${encodeURIComponent(id)}/retry`),
  getActivity: () => request('GET', '/activity'),
  listProjects: (opts) => request('GET', opts?.includeTrashed ? '/projects?include=trashed' : '/projects'),
  trashProject: (id) => request('POST', `/projects/${encodeURIComponent(id)}/trash`),
  untrashProject: (id) => request('POST', `/projects/${encodeURIComponent(id)}/untrash`),
  deleteProject: (id) => request('DELETE', `/projects/${encodeURIComponent(id)}`),
  freeSource: (id) => request('POST', `/projects/${encodeURIComponent(id)}/free-source`),
  keepSource: (id, keep) => request('POST', `/projects/${encodeURIComponent(id)}/keep-source`, { keep }),
  restoreSource: (id) => request('POST', `/projects/${encodeURIComponent(id)}/restore-source`),
  async restoreUpload(id, sourceId, file) {
    const res = await fetch(`${BASE}/projects/${encodeURIComponent(id)}/sources/${encodeURIComponent(sourceId)}/restore-upload`, { method: 'POST', body: file })
    if (!res.ok) {
      const body = await res.json().catch(() => ({}))
      throw new ApiError(res.status, body.detail ?? 'Could not restore the original.')
    }
    return res.json()
  },
  dismissSource: (id) => request('POST', `/sources/${encodeURIComponent(id)}/dismiss`),
  getStorage: () => request('GET', '/storage'),
  setRetention: (days) => request('POST', '/storage/settings', { retention_days: days }),
  freeStorageNow: () => request('POST', '/storage/free'),
  createProject: (input) => request('POST', '/projects', input),
  getProject: (id) => request('GET', `/projects/${encodeURIComponent(id)}`),
  resumeProject: (id) => request('POST', `/projects/${encodeURIComponent(id)}/resume`),
  getClip: (id) => request('GET', `/clips/${encodeURIComponent(id)}`),
  saveTimeline: (id, body) => request('PUT', `/clips/${encodeURIComponent(id)}/timeline`, body),
  removeFillers: (id, body) => request('POST', `/clips/${encodeURIComponent(id)}/actions/remove-fillers`, body),
  autoReframe: (id, body) => request('POST', `/clips/${encodeURIComponent(id)}/actions/auto-reframe`, body),
  applyBrandKit: (id, body) => request('POST', `/clips/${encodeURIComponent(id)}/actions/apply-brand-kit`, body),
  exportClip: (id, body) => request('POST', `/clips/${encodeURIComponent(id)}/export`, body),
  getJob: (id) => request('GET', `/jobs/${encodeURIComponent(id)}`),
  templates: () => request('GET', '/templates'),
  listBrandKits: () => request('GET', '/brand-kits'),
  saveBrandKit: (kit, id) => (id ? request('PUT', `/brand-kits/${encodeURIComponent(id)}`, kit) : request('POST', '/brand-kits', kit)),
  deleteBrandKit: (id) => request('DELETE', `/brand-kits/${encodeURIComponent(id)}`),
  async uploadBrandAsset(file) {
    const form = new FormData()
    form.append('file', file)
    const res = await fetch(BASE + '/brand-assets', { method: 'POST', body: form })
    if (!res.ok) {
      let detail = 'Upload failed'
      try {
        detail = (await res.json()).detail ?? detail
      } catch {
        /* ignore */
      }
      throw new ApiError(res.status, detail)
    }
    return res.json()
  },
}

type Fn = (...a: never[]) => unknown
function wrap(): StudioApi {
  const out = {} as Record<string, Fn>
  for (const key of Object.keys(real) as (keyof StudioApi)[]) {
    out[key] = ((...args: never[]) => {
      if (mockMode) return (mockApi[key] as Fn)(...args)
      if (key === 'upload') return (real.upload as Fn)(...args)
      const attempt = (real[key] as Fn)(...args) as Promise<unknown>
      return attempt.catch((e) => {
        if (e instanceof MockFallback) {
          console.info('[studio] API unavailable in dev, switching to mock data')
          enterMock()
          return (mockApi[key] as Fn)(...args)
        }
        throw e
      })
    }) as Fn
  }
  return out as unknown as StudioApi
}

export const api: StudioApi = wrap()

export function newRequestId(): string {
  return (crypto.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(16).slice(2)}`).slice(0, 36)
}

/** URL helper: absolute media URLs pass through; mock assets resolve under the studio base. */
export function assetUrl(url: string | null | undefined): string {
  if (!url) return ''
  if (url.startsWith('mock/')) return import.meta.env.BASE_URL + url
  return url
}

/** Image URL of an uploaded brand asset (logo). */
export function brandAssetUrl(assetId: string): string {
  if (mockMode && mockAssets.has(assetId)) return mockAssets.get(assetId)!
  return `${BASE}/brand-assets/${encodeURIComponent(assetId)}`
}
