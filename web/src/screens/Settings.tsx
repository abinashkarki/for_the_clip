import { useEffect, useState } from 'react'
import { api } from '../api/client'
import { ApiError } from '../api/errors'
import type { StorageInfo } from '../api/types'
import {
  KEY_PROVIDERS,
  configured,
  externallyManaged,
  originLabel,
  routeNeedsKey,
  routingChanged,
  systemApi,
  type Catalog,
  type Health,
  type KeyProvider,
  type Routing,
  type Settings,
} from '../api/system'
import { useAsync, useDocumentTitle } from '../state/hooks'
import { Icon } from '../ui/Icon'
import { Button, EmptyState, Spinner, cx, toast } from '../ui/kit'
import '../styles/system.css'
import { ModelSetupSection } from './ModelSetup'
import { ProviderSetup } from './ProviderSetup'
import { formatBytes } from './libraryLogic'

const message = (e: unknown) => (e instanceof ApiError || e instanceof Error ? e.message : 'Something went wrong')

export function SettingsScreen() {
  useDocumentTitle('Settings')
  const health = useAsync(() => systemApi.health(), [])
  const [settings, setSettings] = useState<Settings | null>(null)
  const [advanced, setAdvanced] = useState(false)
  const [routingEpoch, setRoutingEpoch] = useState(0)
  useEffect(() => {
    if (health.data) setSettings(health.data)
  }, [health.data])

  return (
    <main className="page sys-page">
      <header className="sys-head">
        <div>
          <h1 className="sys-title">Settings</h1>
          <p className="muted">API keys stay in macOS Keychain. Local model setup contacts the publishers; clip generation uses the providers you choose.</p>
        </div>
      </header>
      <section className="sys-section" aria-labelledby="notices-h">
        <h2 className="section-title" id="notices-h">Third-party notices</h2>
        <p className="muted small"><a className="sys-link" href="/studio/third-party-notices.txt" target="_blank" rel="noreferrer">Frontend software and font licenses</a></p>
      </section>
      <StorageSection />
      <ModelSetupSection />
      {health.error && !health.data && <EmptyState title="Settings are unavailable">{health.error}</EmptyState>}
      {!health.data && !health.error && <div className="sys-loading"><Spinner /> Loading</div>}
      {health.data && settings && (
        <>
          <ProviderSetup settings={settings} onChange={setSettings}
            onRoutingChanged={() => setRoutingEpoch(epoch => epoch + 1)}
            onOpenAdvanced={() => {
              setAdvanced(true)
              requestAnimationFrame(() => document.getElementById('advanced-provider-settings')?.scrollIntoView({ behavior: 'smooth' }))
            }} />
          <details id="advanced-provider-settings" open={advanced} onToggle={event => setAdvanced(event.currentTarget.open)}>
            <summary className="sys-link">Advanced provider keys and model routing</summary>
            <KeysSection settings={settings} onChange={setSettings} />
            <RoutingSection key={routingEpoch} settings={settings} />
          </details>
          <ToolsSection health={health.data} />
        </>
      )}
    </main>
  )
}

function KeysSection({ settings, onChange }: { settings: Settings; onChange: (s: Settings) => void }) {
  return (
    <section className="sys-section" aria-labelledby="keys-h">
      <div className="section-head">
        <h2 className="section-title" id="keys-h">API keys</h2>
        <span className="muted small">Stored in macOS Keychain. Keys are never sent back to the browser.</span>
      </div>
      {settings.credential_warning && (
        <p className="sys-note sys-note-warn" role="status">
          <Icon name="warn" size={16} /> {settings.credential_warning}
        </p>
      )}
      <ul className="sys-keys">
        {KEY_PROVIDERS.map((p) => (
          <KeyRow key={p.id} provider={p.id} label={p.label} use={p.use} link={p.link} settings={settings} onChange={onChange} />
        ))}
      </ul>
    </section>
  )
}

function KeyRow({
  provider, label, use, link, settings, onChange,
}: { provider: KeyProvider; label: string; use: string; link?: string; settings: Settings; onChange: (s: Settings) => void }) {
  const [value, setValue] = useState('')
  const [busy, setBusy] = useState<'save' | 'remove' | null>(null)
  const [error, setError] = useState<string | null>(null)
  const isSet = configured(settings, provider)
  const origin = settings.credential_storage[provider]
  const external = isSet && externallyManaged(origin)

  const run = async (kind: 'save' | 'remove') => {
    setBusy(kind)
    setError(null)
    try {
      const next = kind === 'save' ? await systemApi.saveKey(provider, value) : await systemApi.removeKey(provider)
      onChange(next as Settings)
      setValue('')
      toast(kind === 'save' ? `${label} key saved` : `${label} key removed`, 'ok')
    } catch (e) {
      setError(message(e))
    }
    setBusy(null)
  }

  return (
    <li className="sys-key">
      <div className="sys-key-info">
        <div className="sys-key-name">
          <strong>{label}</strong>
          <span className={cx('sys-status', isSet ? 'is-ok' : 'is-off', external && 'is-external')}>
            <i /> {originLabel(origin, isSet)}
          </span>
        </div>
        <p className="muted small">
          {use}
          {link && (
            <>
              {' '}
              <a className="sys-link" href={link} target="_blank" rel="noreferrer">Get a key</a>
            </>
          )}
        </p>
      </div>
      <form
        className="sys-key-form"
        onSubmit={(e) => {
          e.preventDefault()
          if (value.trim()) void run('save')
        }}
      >
        <input
          className="input"
          type="password"
          autoComplete="new-password"
          aria-label={`${label} API key`}
          placeholder={external ? 'Managed outside Settings' : isSet ? 'Paste a new key to replace it' : 'Paste key'}
          value={value}
          disabled={external}
          onChange={(e) => setValue(e.target.value)}
        />
        <Button type="submit" kind="primary" busy={busy === 'save'} disabled={external || !value.trim()}>Save</Button>
        <Button type="button" kind="ghost" busy={busy === 'remove'} disabled={!isSet || external} onClick={() => run('remove')}>Remove</Button>
      </form>
      {external && <p className="sys-key-help small muted">Change this key where it is set, then restart Clippie.</p>}
      {error && <p className="sys-key-help small bad" role="alert">{error}</p>}
    </li>
  )
}

function RoutingSection({ settings }: { settings: Settings }) {
  const routing = useAsync(() => systemApi.routing(), [])
  const catalog = useAsync(() => systemApi.catalog(), [])
  const [draft, setDraft] = useState<Routing | null>(null)
  const [saving, setSaving] = useState(false)
  const presets = useAsync(() => systemApi.presets(), [])
  const [applying, setApplying] = useState<string | null>(null)
  const [live, setLive] = useState<Record<string, string[]>>({})
  const [fetching, setFetching] = useState(false)
  useEffect(() => {
    if (routing.data) setDraft(JSON.parse(JSON.stringify(routing.data)))
  }, [routing.data])

  const applyPreset = async (id: string) => {
    setApplying(id)
    try {
      const result = await systemApi.applyPreset(id)
      toast(result.notes.length ? `Preset applied. ${result.notes.join(' ')}` : 'Preset applied', result.notes.length ? 'info' : 'ok')
      routing.reload()
    } catch (e) {
      toast(message(e), 'bad')
    }
    setApplying(null)
  }

  const fetchModels = async () => {
    if (!draft) return
    setFetching(true)
    const providers = Array.from(new Set(Object.values(draft.defaults).map((r) => r.provider)))
    const next: Record<string, string[]> = { ...live }
    for (const p of providers) {
      try {
        next[p] = (await systemApi.available(p)).models
      } catch (e) {
        toast(`${p}: ${message(e)}`, 'bad')
      }
    }
    setLive(next)
    setFetching(false)
  }

  if (routing.error || catalog.error) {
    return (
      <section className="sys-section">
        <h2 className="section-title">Model routing</h2>
        <p className="bad small">{routing.error || catalog.error}</p>
      </section>
    )
  }
  if (!draft || !routing.data || !catalog.data) return <section className="sys-section"><div className="sys-loading"><Spinner /> Loading routing</div></section>

  const dirty = routingChanged(routing.data, draft)
  const set = (stage: string, patch: Partial<{ provider: string; model: string; options: Record<string, unknown> }>) =>
    setDraft({ ...draft, defaults: { ...draft.defaults, [stage]: { ...draft.defaults[stage], ...patch } } })

  const save = async () => {
    setSaving(true)
    try {
      await systemApi.saveRouting(draft)
      toast('Routing saved', 'ok')
      routing.reload()
    } catch (e) {
      toast(message(e), 'bad')
    }
    setSaving(false)
  }

  return (
    <section className="sys-section" aria-labelledby="route-h">
      <div className="section-head">
        <h2 className="section-title" id="route-h">Model routing</h2>
        <span className="muted small">Which model handles each stage. A stage never silently falls back to another provider.</span>
      </div>
      {Object.values(draft.defaults).some((r) => r.provider === 'vertex') && (
        <p className="muted small">Google Cloud uses your local Application Default Credentials. {settings.vertex_configured ? 'Local sign-in found.' : 'Sign in with gcloud auth application-default login.'} Requests use the project shown below; costs and credits appear in that project’s Cloud Billing.</p>
      )}
      {presets.data && (
        <div className="sys-presets" role="group" aria-label="Routing presets">
          {presets.data.presets.map((p) => {
            const missing = p.providers.map(routeNeedsKey).filter((k): k is NonNullable<typeof k> => k !== null && !configured(settings, k))
            return (
              <div key={p.id} className="sys-preset">
                <div className="sys-preset-head">
                  <strong>{p.name}</strong>
                  <Button kind="ghost" disabled={missing.length > 0} busy={applying === p.id} onClick={() => applyPreset(p.id)}>Use</Button>
                </div>
                <p className="muted small">{p.description}</p>
                {missing.length > 0 && <p className="bad small">Add a {missing.join(', ')} key first.</p>}
              </div>
            )
          })}
        </div>
      )}
      <div className="sys-table-wrap">
        <table className="sys-table sys-routing">
          <thead>
            <tr><th>Stage</th><th>Provider</th><th>Model</th><th>Key</th></tr>
          </thead>
          <tbody>
            {catalog.data.stages.map((stage) => (
              <RouteRow key={stage} stage={stage} route={draft.defaults[stage]} catalog={catalog.data!} live={live} settings={settings} onChange={(p) => set(stage, p)} />
            ))}
          </tbody>
        </table>
      </div>
      <div className="btn-row">
        <Button kind="primary" disabled={!dirty} busy={saving} onClick={save}>Save routing</Button>
        <Button kind="ghost" disabled={!dirty} onClick={() => setDraft(JSON.parse(JSON.stringify(routing.data)))}>Discard changes</Button>
        <Button kind="ghost" busy={fetching} onClick={fetchModels}>Fetch available models</Button>
        {dirty && <span className="muted small">Unsaved changes</span>}
      </div>
    </section>
  )
}

function RouteRow({
  stage, route, catalog, live, settings, onChange,
}: {
  stage: string
  route: { provider: string; model: string; options?: Record<string, unknown> } | undefined
  catalog: Catalog
  live: Record<string, string[]>
  settings: Settings
  onChange: (p: Partial<{ provider: string; model: string; options: Record<string, unknown> }>) => void
}) {
  const current = route ?? { provider: catalog.providers[0].id, model: '' }
  const needs = routeNeedsKey(current.provider)
  const missing = needs !== null && !configured(settings, needs)
  const listId = `models-${stage}`
  return (
    <tr>
      <th scope="row" className="sys-stage">{stage}</th>
      <td>
        <select
          aria-label={`${stage} provider`}
          value={current.provider}
          onChange={(e) => onChange({ provider: e.target.value, model: catalog.models[e.target.value]?.[0] ?? current.model })}
        >
          {catalog.providers.map((p) => <option key={p.id} value={p.id}>{p.label}</option>)}
        </select>
      </td>
      <td>
        <input
          className="input sys-mono"
          aria-label={`${stage} model`}
          list={listId}
          value={current.model}
          spellCheck={false}
          onChange={(e) => onChange({ model: e.target.value })}
        />
        <datalist id={listId}>{(live[current.provider] ?? catalog.models[current.provider] ?? []).map((m) => <option key={m} value={m} />)}</datalist>
        {current.provider === 'vertex' && <label className="small">Cloud project
          <input className="input sys-mono" aria-label={`${stage} Cloud project`} value={String(current.options?.project ?? '')}
            onChange={(e) => onChange({ options: { ...current.options, project: e.target.value, location: current.options?.location ?? 'global' } })} />
        </label>}
      </td>
      <td>
        {current.provider === 'vertex' ? <span className={`sys-status ${settings.vertex_configured ? 'is-ok' : 'is-bad'}`}><i /> {settings.vertex_configured ? 'ADC found' : 'Sign in to Cloud'}</span>
          : needs === null ? <span className="sys-status is-ok"><i /> Not needed</span>
          : missing ? <span className="sys-status is-bad"><i /> Add {needs} key</span>
          : <span className="sys-status is-ok"><i /> Ready</span>}
      </td>
    </tr>
  )
}

function ToolsSection({ health }: { health: Health }) {
  const checks: [string, boolean, string][] = [
    ['FFmpeg', health.ffmpeg, 'Required for every render. Install it and make sure `ffmpeg` is on your PATH.'],
    ['FFprobe', health.ffprobe, 'Installed with FFmpeg.'],
    ['Caption overlays', health.caption_burn_available, 'Requires the caption fonts and FFmpeg overlay filters. Repair or reinstall Clippie if these are missing.'],
  ]
  return (
    <section className="sys-section" aria-labelledby="tools-h">
      <div className="section-head">
        <h2 className="section-title" id="tools-h">System tools</h2>
        <span className="muted small">Clippie {health.version}</span>
      </div>
      <ul className="sys-checks">
        {checks.map(([name, ok, help]) => (
          <li key={name}>
            <span className={cx('sys-status', ok ? 'is-ok' : 'is-bad')}><i /> {name}</span>
            {!ok && <span className="muted small">{help}</span>}
          </li>
        ))}
      </ul>
    </section>
  )
}

function StorageSection() {
  const storage = useAsync<StorageInfo>(() => api.getStorage(), [])
  const [info, setInfo] = useState<StorageInfo | null>(null)
  useEffect(() => {
    if (storage.data) setInfo(storage.data)
  }, [storage.data])

  const segs = info
    ? [
        { key: 'sources', label: 'Sources', bytes: info.sources_bytes },
        { key: 'working', label: 'Working files', bytes: info.working_bytes },
        { key: 'clips', label: 'Clips', bytes: info.exports_bytes },
        { key: 'models', label: 'Models', bytes: info.models_bytes },
        { key: 'other', label: 'Other', bytes: info.other_bytes },
      ]
    : []
  return (
    <section className="sys-section" aria-labelledby="storage-h">
      <div className="section-head">
        <h2 className="section-title" id="storage-h">Storage</h2>
        {info && <span className="muted small">{formatBytes(info.total_bytes)} used by Clippie</span>}
      </div>
      {storage.error && !info && <p className="bad small">{storage.error}</p>}
      {!info && !storage.error && <div className="sys-loading"><Spinner /> Loading</div>}
      {info && (
        <>
          <div>
            <div className="storage-bar" role="img" aria-label={segs.map((x) => `${x.label} ${formatBytes(x.bytes)}`).join(', ')}>
              {segs.filter((x) => x.bytes > 0).map((x) => (
                <span key={x.key} className={`storage-seg seg-${x.key}`} style={{ flexGrow: x.bytes, flexBasis: 0 }} title={`${x.label}: ${formatBytes(x.bytes)}`} />
              ))}
            </div>
            <ul className="storage-legend">
              {segs.map((x) => (
                <li key={x.key}>
                  <span className={`storage-swatch storage-seg seg-${x.key}`} aria-hidden="true" />
                  {x.label} <strong>{formatBytes(x.bytes)}</strong>
                </li>
              ))}
            </ul>
          </div>
          <p className="muted small">Automatic cleanup and project deletion are temporarily paused to protect active work. Recovery copies are stored separately and can retain media.</p>
        </>
      )}
    </section>
  )
}
