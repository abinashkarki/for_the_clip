import { ApiError } from './errors'

export type SetupModelId = 'whisper' | 'embeddings' | 'vision'
export type SetupModelState = 'missing' | 'existing' | 'ready' | 'downloading' | 'verifying' | 'converting' | 'installing' | 'cancelling' | 'cancelled' | 'interrupted' | 'error'
export interface SetupModel {
  id: SetupModelId
  label: string
  description: string
  required: boolean
  dependencies_installed: boolean
  managed: boolean
  ready: boolean
  verified: boolean
  state: SetupModelState
  bytes_received: number
  bytes_total: number
  remaining_download_bytes?: number
  download_bytes: number
  required_disk_bytes: number
  message: string
  error: string | null
  source_url: string
  terms_url: string
  terms_identity?: string
}
export interface ModelSetup {
  desktop: boolean
  active: string | null
  models: SetupModel[]
  batch?: { state: string; selections: Record<string, string>; completed: string[]; current: string | null; error: string | null } | null
}

async function request(method: string, path: string, signal?: AbortSignal, body?: unknown): Promise<ModelSetup> {
  let response: Response
  try {
    response = await fetch(path, {
      method,
      signal,
      ...(method === 'POST' ? { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body ?? { accept_terms: true }) } : {}),
    })
  } catch (error) {
    if (signal?.aborted) throw error
    throw new ApiError(0, 'Cannot reach Clippie. Check that the app is running, then try again.')
  }
  if (!response.ok) {
    let detail = response.statusText || 'Model setup request failed'
    try {
      const body = await response.json()
      if (typeof body.detail === 'string') detail = body.detail
    } catch { /* retain the HTTP error if the response is not JSON */ }
    throw new ApiError(response.status, detail)
  }
  return response.json() as Promise<ModelSetup>
}

export const setupApi = {
  models: (signal?: AbortSignal) => request('GET', '/api/setup/models', signal),
  install: (id: SetupModelId, signal?: AbortSignal) => request('POST', `/api/setup/models/${encodeURIComponent(id)}/install`, signal),
  cancel: (id: SetupModelId, signal?: AbortSignal) => request('DELETE', `/api/setup/models/${encodeURIComponent(id)}/install`, signal),
  batch: (selections: Record<string, string>, signal?: AbortSignal) => request('POST', '/api/setup/batch', signal, { selections, accept_terms: true }),
  resumeBatch: (signal?: AbortSignal) => request('POST', '/api/setup/batch/resume', signal),
  cancelBatch: (signal?: AbortSignal) => request('DELETE', '/api/setup/batch', signal),
}

export function setupAction(model: SetupModel, active: string | null, accepted: boolean, pending: boolean) {
  const running = ['downloading', 'verifying', 'converting', 'installing', 'cancelling'].includes(model.state)
  const label = model.state === 'error' ? 'Retry download'
    : model.state === 'interrupted' || model.state === 'cancelled' ? 'Resume download' : 'Download model'
  return {
    label,
    showInstall: !model.ready && !running,
    canInstall: !model.ready && !running && model.managed && model.dependencies_installed && !active && accepted && !pending,
    showCancel: active === model.id && running,
    canCancel: active === model.id && running && model.state !== 'cancelling' && !pending,
  }
}

export function setupStatus(model: SetupModel): string {
  if (model.ready) return model.verified ? 'Ready · verified at installation' : 'Prepared outside setup'
  const labels: Record<SetupModelState, string> = {
    missing: 'Download needed', existing: 'Prepared outside setup', ready: 'Ready', downloading: 'Downloading', verifying: 'Verifying download',
    converting: 'Preparing model', installing: 'Installing', cancelling: 'Cancelling', cancelled: 'Cancelled', interrupted: 'Interrupted', error: 'Needs attention',
  }
  return labels[model.state]
}

export function formatModelBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return '0 B'
  if (bytes < 1_000_000) return `${Math.ceil(bytes / 1_000)} KB`
  if (bytes < 1_000_000_000) return `${(bytes / 1_000_000).toFixed(1)} MB`
  return `${(bytes / 1_000_000_000).toFixed(2)} GB`
}

/** Conversion/verification deliberately have no numeric percentage. */
export function downloadProgress(model: SetupModel): number | null {
  return model.state === 'downloading' && model.bytes_total > 0
    ? Math.min(1, Math.max(0, model.bytes_received / model.bytes_total)) : null
}
