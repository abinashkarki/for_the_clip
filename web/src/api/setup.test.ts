import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from './errors'
import { downloadProgress, formatModelBytes, setupAction, setupApi, setupStatus, type ModelSetup, type SetupModel, type SetupModelState } from './setup'

const model = (overrides: Partial<SetupModel> = {}): SetupModel => ({
  id: 'whisper', label: 'Whisper', description: 'Local transcription', required: true,
  dependencies_installed: true, managed: true, ready: false, verified: false, state: 'missing',
  bytes_received: 0, bytes_total: 0, download_bytes: 1_000_000_000, required_disk_bytes: 3_000_000_000,
  message: '', error: null, source_url: 'https://publisher.example/model', terms_url: 'https://publisher.example/terms', ...overrides,
})
const snapshot: ModelSetup = { desktop: true, active: null, models: [model()] }
afterEach(() => vi.unstubAllGlobals())

describe('model setup actions', () => {
  it('requires consent, dependencies, app management and a free installer', () => {
    expect(setupAction(model(), null, true, false).canInstall).toBe(true)
    expect(setupAction(model(), null, false, false).canInstall).toBe(false)
    expect(setupAction(model({ dependencies_installed: false }), null, true, false).canInstall).toBe(false)
    expect(setupAction(model({ managed: false }), null, true, false).canInstall).toBe(false)
    expect(setupAction(model(), 'embeddings', true, false).canInstall).toBe(false)
    expect(setupAction(model(), null, true, true).canInstall).toBe(false)
    expect(setupAction(model({ ready: true }), null, true, false).showInstall).toBe(false)
  })
  it.each<SetupModelState>(['downloading', 'verifying', 'converting', 'installing'])('allows cancellation during %s without requiring consent', state => {
    const action = setupAction(model({ state }), 'whisper', false, false)
    expect(action.showInstall).toBe(false)
    expect(action.canCancel).toBe(true)
    expect(setupAction(model({ state }), 'embeddings', false, false).canCancel).toBe(false)
    expect(setupAction(model({ state }), 'whisper', false, true).canCancel).toBe(false)
  })
  it('prevents duplicate cancellation and distinguishes recovery actions', () => {
    expect(setupAction(model({ state: 'cancelling' }), 'whisper', false, false).canCancel).toBe(false)
    expect(setupAction(model({ state: 'error' }), null, true, false).label).toBe('Retry download')
    for (const state of ['interrupted', 'cancelled'] as const) {
      expect(setupAction(model({ state }), null, true, false)).toMatchObject({ label: 'Resume download', canInstall: true })
    }
  })
  it('does not claim an existing model was verified by setup', () => {
    expect(setupStatus(model({ ready: true, state: 'existing' }))).toBe('Prepared outside setup')
    expect(setupStatus(model({ ready: true, verified: true, state: 'ready' }))).toBe('Ready · verified at installation')
  })
  it('only assigns a percentage to downloads with a known total', () => {
    expect(downloadProgress(model({ state: 'downloading', bytes_received: 50, bytes_total: 100 }))).toBe(0.5)
    expect(downloadProgress(model({ state: 'downloading', bytes_received: 500, bytes_total: 100 }))).toBe(1)
    expect(downloadProgress(model({ state: 'downloading', bytes_total: 0 }))).toBeNull()
    for (const state of ['verifying', 'converting', 'installing'] as const) {
      expect(downloadProgress(model({ state, bytes_received: 100, bytes_total: 100 }))).toBeNull()
    }
    expect(formatModelBytes(3_000_000_000)).toBe('3.00 GB')
    expect(formatModelBytes(500_000_000)).toBe('500.0 MB')
  })
})

describe('setup API', () => {
  it('reads status and only sends accepted terms on explicit install', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => snapshot })
    vi.stubGlobal('fetch', fetchMock)
    const controller = new AbortController()
    expect(await setupApi.models(controller.signal)).toEqual(snapshot)
    expect(fetchMock).toHaveBeenLastCalledWith('/api/setup/models', { method: 'GET', signal: controller.signal })
    await setupApi.install('whisper')
    expect(fetchMock).toHaveBeenLastCalledWith('/api/setup/models/whisper/install', {
      method: 'POST', signal: undefined, headers: { 'Content-Type': 'application/json' }, body: '{"accept_terms":true}',
    })
    await setupApi.cancel('embeddings')
    expect(fetchMock).toHaveBeenLastCalledWith('/api/setup/models/embeddings/install', { method: 'DELETE', signal: undefined })
  })
  it.each([409, 422, 507])('preserves actionable server errors (%s)', async status => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status, statusText: 'Error', json: async () => ({ detail: 'Free more space before retrying.' }) }))
    await expect(setupApi.install('whisper')).rejects.toMatchObject({ status, message: 'Free more space before retrying.' })
  })
  it('gives a usable connection error and preserves intentional aborts', async () => {
    const aborted = new Error('aborted')
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(aborted))
    await expect(setupApi.models()).rejects.toBeInstanceOf(ApiError)
    await expect(setupApi.models()).rejects.toMatchObject({ status: 0 })
    const controller = new AbortController()
    controller.abort()
    await expect(setupApi.models(controller.signal)).rejects.toBe(aborted)
  })
})
