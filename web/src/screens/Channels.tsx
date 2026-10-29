import { useEffect, useState } from 'react'
import { isMock } from '../api/client'
import { platformName } from '../api/publishLogic'
import { publishApi, type Channel, type PublishSettings } from '../api/publish'
import { useAsync, useDocumentTitle } from '../state/hooks'
import { href } from '../state/route'
import { Icon } from '../ui/Icon'
import { Button, EmptyState, Spinner, cx, toast } from '../ui/kit'
import '../styles/publish.css'

const errText = (e: unknown, fallback: string) => (e instanceof Error ? e.message : fallback)

export function ChannelsScreen() {
  useDocumentTitle('Channels')
  const channels = useAsync<Channel[]>(() => publishApi.channels(), [])
  const settings = useAsync<PublishSettings>(() => publishApi.settings(), [])
  const reloadAll = () => {
    channels.reload()
    settings.reload()
  }
  return (
    <div className="page">
      <nav className="crumbs" aria-label="Breadcrumb">
        <a href={href.analytics()}>
          <Icon name="back" size={15} /> Publishing
        </a>
      </nav>
      <header className="project-head">
        <div>
          <h1 className="project-title">Channels</h1>
          <div className="muted">Manage the accounts you publish to and the services used to connect them.</div>
        </div>
      </header>

      <section aria-labelledby="accounts-h">
        <div className="section-head">
          <h2 className="section-title" id="accounts-h">Connected accounts</h2>
        </div>
        <Accounts channels={channels.data} loading={channels.loading} error={channels.error} onChange={reloadAll} />
      </section>

      {settings.data && (
        <div className="pub-setup-sections">
          <section aria-labelledby="connect-h">
            <div className="section-head pub-section-heading"><div><h2 className="section-title" id="connect-h">Connect accounts</h2><p className="muted small">Choose a platform below. Credentials are stored in this computer's Keychain.</p></div></div>
            <div className="pub-cards">
              <YoutubeCard settings={settings.data} connected={(channels.data ?? []).some((c) => c.platform === 'youtube')} onChange={reloadAll} />
              <AggregatorCard settings={settings.data} onChange={reloadAll} />
            </div>
          </section>
          <section aria-labelledby="tracking-h">
            <div className="section-head pub-section-heading"><div><h2 className="section-title" id="tracking-h">Optional tracking</h2><p className="muted small">Configure a separate service if you want metrics collected while Clippie is closed.</p></div></div>
            <CloudCard settings={settings.data} onChange={reloadAll} />
          </section>
        </div>
      )}
      {settings.loading && !settings.data && <div className="skeleton pub-skel" />}
      {settings.error && !settings.data && <EmptyState title="Could not load publishing settings" action={<Button onClick={settings.reload}>Try again</Button>}>{settings.error}</EmptyState>}
    </div>
  )
}

function Accounts({ channels, loading, error, onChange }: { channels: Channel[] | null; loading: boolean; error: string | null; onChange: () => void }) {
  const [busy, setBusy] = useState<string | null>(null)
  if (loading && !channels) return <div className="skeleton pub-skel" />
  if (error && !channels) return <EmptyState title="Could not load channels" action={<Button onClick={onChange}>Try again</Button>}>{error}</EmptyState>
  if (!channels?.length)
    return (
      <EmptyState title="No accounts connected yet">Connect YouTube below to publish your first Short. TikTok and Instagram connect through a posting service.</EmptyState>
    )
  const remove = async (c: Channel) => {
    if (!confirm(`Disconnect ${c.display_name}? Clippie forgets its sign-in. Posts already on the platform stay there.`)) return
    setBusy(c.id)
    try {
      await publishApi.disconnect(c.id)
      toast(`${c.display_name} disconnected`, 'ok')
      onChange()
    } catch (e) {
      toast(errText(e, 'Could not disconnect this account'), 'bad')
    }
    setBusy(null)
  }
  return (
    <ul className="pub-accounts" aria-label="Connected accounts">
      {channels.map((c) => (
        <li key={c.id} className="pub-account">
          <span className={cx('pub-badge', `pub-${c.platform}`)} aria-hidden="true">{c.platform === 'youtube' ? 'YT' : c.platform === 'tiktok' ? 'TT' : 'IG'}</span>
          <div className="pub-account-main">
            <div className="queue-title">{c.display_name}</div>
            <div className="muted small">{platformName(c.platform)}{c.provider === 'aggregator' ? ' · via posting service' : ' · direct'}</div>
          </div>
          <Button size="sm" kind="ghost" icon="trash" busy={busy === c.id} onClick={() => void remove(c)}>Disconnect</Button>
        </li>
      ))}
    </ul>
  )
}

function YoutubeCard({ settings, connected, onChange }: { settings: PublishSettings; connected: boolean; onChange: () => void }) {
  const [clientId, setClientId] = useState(settings.youtube_client_id)
  const [secret, setSecret] = useState('')
  const [busy, setBusy] = useState<'save' | 'connect' | null>(null)
  useEffect(() => setClientId(settings.youtube_client_id), [settings.youtube_client_id])
  const callback = `${location.origin}${settings.redirect_uri_hint}`
  const ready = !!clientId.trim() && (settings.youtube_client_secret_set || !!secret.trim())
  const dirty = clientId.trim() !== settings.youtube_client_id || !!secret.trim()

  const save = async () => {
    const patch: Record<string, string> = { youtube_client_id: clientId.trim() }
    if (secret.trim()) patch.youtube_client_secret = secret.trim()
    await publishApi.saveSettings(patch)
    setSecret('')
  }
  const saveOnly = async () => {
    setBusy('save')
    try {
      await save()
      toast('Google credentials saved to the Keychain', 'ok')
      onChange()
    } catch (e) {
      toast(errText(e, 'Could not save'), 'bad')
    }
    setBusy(null)
  }
  const connect = async () => {
    setBusy('connect')
    try {
      if (dirty) await save()
      const start = await publishApi.connectYoutube()
      if (start.mock || isMock()) {
        toast('Connected a demo YouTube channel', 'ok')
        onChange()
      } else location.href = start.authorization_url
    } catch (e) {
      toast(errText(e, 'Could not start the Google sign-in'), 'bad')
    }
    setBusy(null)
  }

  return (
    <article className="pub-card" aria-labelledby="yt-h">
      <header className="pub-card-head">
        <span className="pub-badge pub-youtube" aria-hidden="true">YT</span>
        <div>
          <h2 id="yt-h">YouTube Shorts</h2>
          <p className="muted small">Direct upload and scheduling. YouTube publishes scheduled Shorts itself, even when this computer is off.</p>
        </div>
        {connected && <span className="chip"><span className="dot" /> Connected</span>}
      </header>

      <details className="pub-guide" open={!settings.youtube_client_secret_set}>
        <summary>Set up your Google Cloud OAuth client (one time, about 10 minutes)</summary>
        <ol>
          <li>Open <a href="https://console.cloud.google.com/projectcreate" target="_blank" rel="noreferrer">Google Cloud Console</a> and create a project named Clippie.</li>
          <li>Under APIs &amp; Services, Library, enable <strong>YouTube Data API v3</strong> and <strong>YouTube Analytics API</strong>.</li>
          <li>Under OAuth consent screen, choose External, fill in the app name and your email, and add yourself as a test user. Add the scopes <code>youtube.upload</code>, <code>youtube.readonly</code> and <code>yt-analytics.readonly</code>.</li>
          <li>Under Credentials, Create credentials, OAuth client ID, and choose <strong>Desktop app</strong>. Copy the client ID and secret into the fields below.</li>
          <li>If you chose a Web application client instead, add <code className="pub-copy">{callback}</code> as an authorised redirect URI.</li>
          <li>Press Connect YouTube and approve the sign-in. While the app is in Testing, Google signs you out every 7 days; publish the app to keep the connection.</li>
        </ol>
        <p className="muted small">Videos uploaded through a Google project that has not passed YouTube's API compliance audit are locked to private. Request the audit from the YouTube API Services page when you are ready to post publicly.</p>
      </details>

      <div className="pub-form">
        <label className="field">
          <span className="field-label">Client ID</span>
          <input className="input" value={clientId} onChange={(e) => setClientId(e.target.value)} placeholder="123456789-abc.apps.googleusercontent.com" autoComplete="off" spellCheck={false} />
        </label>
        <label className="field">
          <span className="field-label">Client secret</span>
          <input className="input" type="password" value={secret} onChange={(e) => setSecret(e.target.value)} autoComplete="off" placeholder={settings.youtube_client_secret_set ? 'Saved in Keychain. Type to replace.' : 'GOCSPX-…'} />
        </label>
      </div>
      <div className="btn-row">
        <Button kind="primary" icon="link" busy={busy === 'connect'} disabled={!ready} onClick={() => void connect()}>
          {connected ? 'Connect another channel' : 'Connect YouTube'}
        </Button>
        <Button busy={busy === 'save'} disabled={!dirty || !clientId.trim()} onClick={() => void saveOnly()}>Save credentials</Button>
      </div>
    </article>
  )
}

function AggregatorCard({ settings, onChange }: { settings: PublishSettings; onChange: () => void }) {
  const [key, setKey] = useState('')
  const [busy, setBusy] = useState(false)
  const connect = async () => {
    setBusy(true)
    try {
      const found = await publishApi.connectAggregator(key.trim() || undefined)
      setKey('')
      toast(`Found ${found.length} account${found.length === 1 ? '' : 's'}`, 'ok')
      onChange()
    } catch (e) {
      toast(errText(e, 'Could not connect the posting service'), 'bad')
    }
    setBusy(false)
  }
  return (
    <article className="pub-card" aria-labelledby="agg-h">
      <header className="pub-card-head">
        <span className="pub-badge pub-tiktok" aria-hidden="true">TT</span>
        <div>
          <h2 id="agg-h">TikTok and Instagram Reels</h2>
          <p className="muted small">Posted through an Ayrshare-compatible posting service until direct integrations pass TikTok's and Meta's reviews.</p>
        </div>
        {settings.aggregator_key_set && <span className="chip"><span className="dot" /> Key saved</span>}
      </header>
      <ul className="pub-notes">
        <li>Create an account at the posting service, link your TikTok and Instagram accounts there, and copy its API key.</li>
        <li>TikTok: apps that have not passed TikTok's content-posting audit can only post privately. The posting service holds that audit.</li>
        <li>Instagram: an Instagram professional account is required. Reels metrics: views, reach, average watch time, skip rate, saves and shares.</li>
      </ul>
      <label className="field">
        <span className="field-label">API key</span>
        <input className="input" type="password" value={key} onChange={(e) => setKey(e.target.value)} autoComplete="off" placeholder={settings.aggregator_key_set ? 'Saved in Keychain. Type to replace.' : 'Paste the API key'} />
      </label>
      <div className="btn-row">
        <Button kind="primary" icon="link" busy={busy} disabled={!key.trim() && !settings.aggregator_key_set} onClick={() => void connect()}>
          {settings.aggregator_key_set && !key.trim() ? 'Refresh accounts' : 'Save key and find accounts'}
        </Button>
      </div>
    </article>
  )
}

function CloudCard({ settings, onChange }: { settings: PublishSettings; onChange: () => void }) {
  const [url, setUrl] = useState(settings.cloud_url)
  const [token, setToken] = useState('')
  const [busy, setBusy] = useState<'save' | 'sync' | null>(null)
  const [result, setResult] = useState<string | null>(null)
  const configured = !!settings.cloud_url && settings.cloud_device_token_set
  const dirty = url.trim() !== settings.cloud_url || !!token.trim()
  const save = async () => {
    setBusy('save')
    try {
      const patch: Record<string, string> = { cloud_url: url.trim() }
      if (token.trim()) patch.cloud_device_token = token.trim()
      await publishApi.saveSettings(patch)
      setToken('')
      toast('Cloud settings saved', 'ok')
      onChange()
    } catch (e) {
      toast(errText(e, 'Could not save'), 'bad')
    }
    setBusy(null)
  }
  const sync = async () => {
    setBusy('sync')
    try {
      const r = await publishApi.cloudSync()
      setResult(`Registered ${r.posts_registered} post${r.posts_registered === 1 ? '' : 's'}, pulled ${r.snapshots_merged} new metric snapshot${r.snapshots_merged === 1 ? '' : 's'}.`)
    } catch (e) {
      setResult(null)
      toast(errText(e, 'Cloud sync failed'), 'bad')
    }
    setBusy(null)
  }
  return (
    <article className="pub-card" aria-labelledby="cloud-h">
      <header className="pub-card-head">
        <span className="pub-badge pub-cloud" aria-hidden="true"><Icon name="upload" size={15} /></span>
        <div>
          <h2 id="cloud-h">Cloud tracking <span className="muted small">(optional)</span></h2>
          <p className="muted small">A small always-on service that keeps collecting YouTube metrics while this computer sleeps. It never receives your videos.</p>
        </div>
        {configured && <span className="chip"><span className="dot" /> Configured</span>}
      </header>
      <div className="pub-form">
        <label className="field">
          <span className="field-label">Service address</span>
          <input className="input" value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://clippie-cloud.example.com" autoComplete="off" spellCheck={false} />
        </label>
        <label className="field">
          <span className="field-label">Device token</span>
          <input className="input" type="password" value={token} onChange={(e) => setToken(e.target.value)} autoComplete="off" placeholder={settings.cloud_device_token_set ? 'Saved in Keychain. Type to replace.' : 'Token from the service'} />
        </label>
      </div>
      <p className="muted small">Syncing sends your YouTube refresh token to your own service, encrypted at rest there. Set it up with <code>cloud/README.md</code>.</p>
      <div className="btn-row">
        <Button busy={busy === 'save'} disabled={!dirty || !url.trim()} onClick={() => void save()}>Save</Button>
        <Button kind="primary" busy={busy === 'sync'} disabled={!configured || dirty} onClick={() => void sync()}>
          {busy === 'sync' ? <Spinner size={14} /> : null} Sync now
        </Button>
      </div>
      {result && <p className="small pub-ok" role="status">{result}</p>}
    </article>
  )
}
