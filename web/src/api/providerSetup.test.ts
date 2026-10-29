import { describe, expect, it, vi } from 'vitest'
import { createProviderSetupFlow, geminiOnlyPresets, initialProviderSetupState, type ProviderSetupState } from './providerSetup'
import type { Preset, Routing, Settings } from './system'

const settings: Settings = {
  gemini_configured: false, anthropic_configured: true, openai_configured: false,
  groq_configured: false, deepseek_configured: false, gemini_model: 'existing-model',
  credential_storage: { anthropic: 'keychain' }, credential_warning: null,
}
const connected = { ...settings, gemini_configured: true, credential_storage: { ...settings.credential_storage, gemini: 'keychain' as const } }
const routing: Routing = { defaults: { plan: { provider: 'anthropic', model: 'existing-route' } }, fallbacks: {} }
const applied: Routing = { defaults: { plan: { provider: 'gemini', model: 'backend-chosen-model' } }, fallbacks: {} }
const presets: Preset[] = [
  { id: 'server-gemini-choice', name: 'Backend preset title', description: 'Backend description', providers: ['gemini'] },
  { id: 'mixed', name: 'Mixed', description: '', providers: ['gemini', 'anthropic'] },
  { id: 'local', name: 'Local', description: '', providers: [] },
]
function fixture() {
  let state = initialProviderSetupState()
  const onState = vi.fn((next: ProviderSetupState) => { state = next })
  const onSettings = vi.fn()
  const onRoutingChanged = vi.fn()
  const api = {
    presets: vi.fn().mockResolvedValue({ presets }), routing: vi.fn().mockResolvedValue(routing),
    saveKey: vi.fn().mockResolvedValue(connected), available: vi.fn().mockResolvedValue({ provider: 'gemini', models: ['one', 'two'] }),
    applyPreset: vi.fn().mockResolvedValue({ routing: applied, notes: [] }),
  }
  const flow = createProviderSetupFlow(onState, onSettings, onRoutingChanged, api)
  return { flow, api, onState, onSettings, onRoutingChanged, state: () => state }
}

describe('guided provider setup', () => {
  it('loads existing routing and dynamic Gemini-only presets without contacting providers or changing settings', async () => {
    const f = fixture()
    await f.flow.load()
    expect(geminiOnlyPresets(presets)).toEqual([presets[0]])
    expect(f.state()).toMatchObject({ routing, presets: [presets[0]], modelCount: null, busy: null })
    expect(f.api.available).not.toHaveBeenCalled()
    expect(f.api.saveKey).not.toHaveBeenCalled()
    expect(f.api.applyPreset).not.toHaveBeenCalled()
    expect(f.onSettings).not.toHaveBeenCalled()
  })

  it('saves only on an explicit action and clears the secret before notifying the parent', async () => {
    const f = fixture()
    const clear = vi.fn()
    f.onSettings.mockImplementation(() => expect(clear).toHaveBeenCalledOnce())
    await f.flow.save('  private-key  ', settings, clear)
    expect(f.api.saveKey).toHaveBeenCalledOnce()
    expect(f.api.saveKey).toHaveBeenCalledWith('gemini', 'private-key')
    expect(f.onSettings).toHaveBeenCalledWith(connected)
    expect(JSON.stringify(f.state())).not.toContain('private-key')
    expect(f.api.available).not.toHaveBeenCalled()
    expect(f.api.applyPreset).not.toHaveBeenCalled()
    expect(f.state().modelCount).toBeNull()
  })

  it.each(['environment', 'env_file'] as const)('preserves %s credentials and rejects blank keys', async origin => {
    const f = fixture()
    const clear = vi.fn()
    await f.flow.save('replacement', { ...connected, credential_storage: { gemini: origin } }, clear)
    await f.flow.save(' ', settings, clear)
    expect(f.api.saveKey).not.toHaveBeenCalled()
    expect(clear).not.toHaveBeenCalled()
  })

  it('never displays a credential echoed in an API error', async () => {
    const f = fixture()
    f.api.saveKey.mockRejectedValue(new Error('Proxy rejected private-key'))
    await f.flow.save('private-key', settings, vi.fn())
    expect(f.state().error).toContain('Could not save')
    expect(JSON.stringify(f.state())).not.toContain('private-key')
    expect(f.onSettings).not.toHaveBeenCalled()
  })

  it('checks only the model list, returns a count, and clears stale success after failure', async () => {
    const f = fixture()
    await f.flow.check(settings)
    expect(f.api.available).not.toHaveBeenCalled()
    await f.flow.check(connected)
    expect(f.api.available).toHaveBeenCalledOnce()
    expect(f.api.available).toHaveBeenCalledWith('gemini')
    expect(f.state().modelCount).toBe(2)
    expect(f.api.applyPreset).not.toHaveBeenCalled()
    f.api.available.mockRejectedValue(new Error('provider-error'))
    await f.flow.check(connected)
    expect(f.state().modelCount).toBeNull()
    expect(f.state().error).toContain('Could not list models')
  })

  it('requires a configured key and explicit selection from loaded Gemini-only presets', async () => {
    const f = fixture()
    await f.flow.load()
    await f.flow.apply(presets[0].id, settings)
    await f.flow.apply('mixed', connected)
    await f.flow.apply('', connected)
    expect(f.api.applyPreset).not.toHaveBeenCalled()
    await f.flow.apply(presets[0].id, connected)
    expect(f.api.applyPreset).toHaveBeenCalledOnce()
    expect(f.api.applyPreset).toHaveBeenCalledWith(presets[0].id)
    expect(f.state().routing).toEqual(applied)
    expect(f.onRoutingChanged).toHaveBeenCalledOnce()
    expect(f.api.saveKey).not.toHaveBeenCalled()
  })

  it('surfaces every preset warning without claiming validation, even if the list check failed', async () => {
    const f = fixture()
    await f.flow.load()
    f.api.available.mockRejectedValue(new Error('offline'))
    await f.flow.check(connected)
    const notes = ['Live list unavailable', 'Plan choice unresolved']
    f.api.applyPreset.mockResolvedValue({ routing: applied, notes })
    await f.flow.apply(presets[0].id, connected)
    expect(f.state()).toMatchObject({ notes, routing: applied, error: null, modelCount: null })
    expect(f.state().message).toContain('unresolved')
    expect(f.state().message).not.toContain('validated')
  })

  it('prevents overlapping actions and ignores late results and callbacks after unmount', async () => {
    const f = fixture()
    let resolve!: (value: Settings) => void
    f.api.saveKey.mockReturnValue(new Promise<Settings>(done => { resolve = done }))
    const clear = vi.fn()
    const saving = f.flow.save('private-key', settings, clear)
    await f.flow.check(connected)
    expect(f.api.available).not.toHaveBeenCalled()
    f.flow.dispose()
    const updates = f.onState.mock.calls.length
    resolve(connected)
    await saving
    expect(f.onState).toHaveBeenCalledTimes(updates)
    expect(f.onSettings).not.toHaveBeenCalled()
    await f.flow.load()
    expect(f.api.presets).not.toHaveBeenCalled()
  })

  it('reports loading and apply failures without pretending routing changed', async () => {
    const f = fixture()
    f.api.routing.mockRejectedValueOnce(new Error('offline'))
    await f.flow.load()
    expect(f.state().error).toContain('Could not load')
    await f.flow.load()
    f.api.applyPreset.mockRejectedValue(new Error('offline'))
    await f.flow.apply(presets[0].id, connected)
    expect(f.state().routing).toEqual(routing)
    expect(f.state().error).toContain('Could not confirm')
    expect(f.onRoutingChanged).not.toHaveBeenCalled()
  })
})
