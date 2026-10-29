import { externallyManaged, systemApi, type Preset, type Routing, type Settings } from './system'

export type ProviderSetupAction = 'load' | 'save' | 'check' | 'apply'
export interface ProviderSetupState {
  busy: ProviderSetupAction | null
  presets: Preset[]
  routing: Routing | null
  modelCount: number | null
  message: string | null
  error: string | null
  notes: string[]
}

export const initialProviderSetupState = (): ProviderSetupState => ({
  busy: null, presets: [], routing: null, modelCount: null, message: null, error: null, notes: [],
})

export const geminiOnlyPresets = (presets: Preset[]) => presets.filter(p =>
  p.providers.length > 0 && p.providers.every(provider => provider === 'gemini'),
)

type SetupApi = Pick<typeof systemApi, 'saveKey' | 'available' | 'presets' | 'applyPreset' | 'routing'>
const errors: Record<ProviderSetupAction, string> = {
  load: 'Could not load provider presets and current routing. Try again.',
  save: 'Could not save the key. Check macOS Keychain access and try again.',
  check: 'Could not list models. Check the configured key, network, and provider account, then try again.',
  apply: 'Could not confirm the preset was applied. Reload current routing before trying again.',
}

/** Coordinates explicit actions without retaining credentials or exposing transport errors. */
export function createProviderSetupFlow(
  onState: (state: ProviderSetupState) => void,
  onSettings: (settings: Settings) => void,
  onRoutingChanged: () => void,
  api: SetupApi = systemApi,
) {
  let state = initialProviderSetupState()
  let disposed = false
  const update = (patch: Partial<ProviderSetupState>) => {
    if (disposed) return
    state = { ...state, ...patch }
    onState(state)
  }
  const run = async (action: ProviderSetupAction, work: () => Promise<void>) => {
    if (disposed || state.busy) return
    update({ busy: action, error: null, message: null })
    try {
      await work()
    } catch {
      // Never show raw response/transport errors: a server or proxy could echo a key.
      update({ error: errors[action] })
    } finally {
      update({ busy: null })
    }
  }
  return {
    load: () => run('load', async () => {
      const [presets, routing] = await Promise.all([api.presets(), api.routing()])
      update({ presets: geminiOnlyPresets(presets.presets), routing })
    }),
    save: (value: string, settings: Settings, clearSecret: () => void) => run('save', async () => {
      if (externallyManaged(settings.credential_storage.gemini) || !value.trim()) return
      const next = await api.saveKey('gemini', value.trim())
      if (disposed) return
      clearSecret()
      update({ modelCount: null, notes: [], message: 'Gemini key saved in macOS Keychain. Access has not been checked.' })
      onSettings(next)
    }),
    check: (settings: Settings) => run('check', async () => {
      update({ modelCount: null })
      if (!settings.gemini_configured) return
      const result = await api.available('gemini')
      update({ modelCount: result.models.length })
    }),
    apply: (id: string, settings: Settings) => run('apply', async () => {
      if (!settings.gemini_configured || !state.presets.some(p => p.id === id)) return
      update({ notes: [] })
      const result = await api.applyPreset(id)
      if (disposed) return
      update({ routing: result.routing, notes: result.notes, message: result.notes.length
        ? 'Preset applied with warnings. Model choices may be unresolved; review the notes before generating clips.'
        : 'Preset applied. Clip generation and editorial quality have not been tested.' })
      onRoutingChanged()
    }),
    dispose: () => { disposed = true },
  }
}
