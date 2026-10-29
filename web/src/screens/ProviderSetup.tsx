import { useEffect, useRef, useState } from 'react'
import { createProviderSetupFlow, initialProviderSetupState } from '../api/providerSetup'
import { KEY_PROVIDERS, externallyManaged, originLabel, type Settings } from '../api/system'
import { Button } from '../ui/kit'
import '../styles/provider-setup.css'

export interface ProviderSetupProps {
  settings: Settings
  onChange: (settings: Settings) => void
  onRoutingChanged?: () => void
  onOpenAdvanced?: () => void
}

export function ProviderSetup({ settings, onChange, onRoutingChanged, onOpenAdvanced }: ProviderSetupProps) {
  const [state, setState] = useState(initialProviderSetupState)
  const [secret, setSecret] = useState('')
  const [selected, setSelected] = useState('')
  const callbacks = useRef({ onChange, onRoutingChanged })
  callbacks.current = { onChange, onRoutingChanged }
  const flow = useRef<ReturnType<typeof createProviderSetupFlow> | null>(null)
  useEffect(() => {
    const current = createProviderSetupFlow(setState,
      next => callbacks.current.onChange(next),
      () => callbacks.current.onRoutingChanged?.())
    flow.current = current
    void current.load()
    return () => { current.dispose(); flow.current = null }
  }, [])

  const provider = KEY_PROVIDERS.find(p => p.id === 'gemini')!
  const external = externallyManaged(settings.credential_storage.gemini)
  const busy = state.busy !== null
  const preset = state.presets.find(p => p.id === selected)
  const providers = state.routing ? [...new Set(Object.values(state.routing.defaults).map(route => route.provider))] : []

  return (
    <section className="sys-section provider-setup" aria-labelledby="provider-setup-heading" aria-busy={busy}>
      <div className="section-head">
        <h2 className="section-title" id="provider-setup-heading">Connect an AI provider</h2>
        <a className="sys-link small" href="#/settings" onClick={event => { if (onOpenAdvanced) { event.preventDefault(); onOpenAdvanced() } }}>Use another provider / advanced settings</a>
      </div>
      <p className="muted small">Bring your own Gemini AI Studio key to get started. You can also continue with your existing provider setup in advanced settings.</p>
      {settings.credential_warning && <p className="sys-note sys-note-warn" role="status">{settings.credential_warning}</p>}
      <ol className="provider-setup-steps">
        <li>
          <h3>Get and save a Gemini key</h3>
          <p className="muted small">{provider.link && <a className="sys-link" href={provider.link} target="_blank" rel="noreferrer">Get an API key in Google AI Studio ↗</a>} Provider billing and quotas are separate from Clippie. Review the <a className="sys-link" href="https://ai.google.dev/gemini-api/docs/billing" target="_blank" rel="noreferrer">AI Studio billing guide</a> and your account’s current terms before generating clips.</p>
          <p className="small" role="status">{originLabel(settings.credential_storage.gemini, settings.gemini_configured)}</p>
          {external ? <p className="muted small">This key is managed outside Settings. Change it at its source and restart Clippie; this guide will not replace it.</p> : (
            <div className="provider-setup-key">
              <label className="small" htmlFor="provider-setup-key">Gemini API key</label>
              <div className="btn-row">
                <input id="provider-setup-key" className="input" type="password" autoComplete="new-password" spellCheck={false}
                  value={secret} disabled={busy} onChange={event => setSecret(event.target.value)}
                  placeholder={settings.gemini_configured ? 'Paste a replacement key' : 'Paste your key'} />
                <Button type="button" kind="primary" busy={state.busy === 'save'} disabled={busy || !secret.trim()}
                  onClick={() => void flow.current?.save(secret, settings, () => setSecret(''))}>Save key to macOS Keychain</Button>
              </div>
            </div>
          )}
        </li>
        <li>
          <h3>Check account access</h3>
          <p className="muted small">This contacts Google to list models using the configured key. It sends no media and performs no inference. Listing models does not test clip generation or editorial quality.</p>
          <Button type="button" kind="secondary" busy={state.busy === 'check'} disabled={busy || !settings.gemini_configured}
            onClick={() => void flow.current?.check(settings)}>Check access (list models)</Button>
          {state.modelCount !== null && <p className="small" role="status">Google returned {state.modelCount} available models. Access to generation has not been tested.</p>}
        </li>
        <li>
          <h3>Choose your stage routing</h3>
          {state.routing && <p className="muted small">Current stage providers: {providers.join(', ') || 'none'}.</p>}
          <p className="small">Applying a preset replaces stage routing and preserves your other provider keys. Later clip generation sends selected transcripts and sampled frames to Google. Billing and quotas remain with your provider account.</p>
          <label className="provider-setup-choice small" htmlFor="provider-setup-preset">Gemini preset</label>
          <select id="provider-setup-preset" className="input" value={selected} disabled={busy || !state.presets.length}
            onChange={event => setSelected(event.target.value)}>
            <option value="">Choose a preset…</option>
            {state.presets.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}
          </select>
          {preset && <p className="muted small">{preset.description}</p>}
          <p className="muted small">Preset names and descriptions come from Clippie’s current catalog; they are not a price or quota guarantee. Applying checks the provider’s model list. If it is unavailable, the preset may still be saved with unresolved choices and warnings.</p>
          <div className="btn-row">
            <Button type="button" kind="primary" busy={state.busy === 'apply'} disabled={busy || !settings.gemini_configured || !preset}
              onClick={() => void flow.current?.apply(selected, settings)}>Apply selected preset</Button>
            <Button type="button" kind="ghost" busy={state.busy === 'load'} disabled={busy}
              onClick={() => void flow.current?.load()}>Reload presets and routing</Button>
          </div>
          {!busy && !state.error && !state.presets.length && <p className="muted small">No Gemini-only presets are available. Continue in advanced settings.</p>}
        </li>
      </ol>
      <div aria-live="polite">
        {state.message && <p className={state.notes.length ? 'sys-note sys-note-warn' : 'small'}>{state.message}</p>}
        {state.notes.length > 0 && <ul className="sys-note sys-note-warn provider-setup-notes">{state.notes.map((note, i) => <li key={i}>{note}</li>)}</ul>}
      </div>
      {state.error && <p className="bad small" role="alert">{state.error}</p>}
    </section>
  )
}
