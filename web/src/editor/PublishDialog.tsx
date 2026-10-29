import { useEffect, useMemo, useRef, useState } from 'react'
import { formatTime } from '../render/timeline'
import {
  checkSchedule,
  defaultSchedule,
  formatHashtags,
  parseHashtags,
  platformName,
  publishDefaults,
  type SuggestedCopy,
  shortsChecks,
  statusLabel,
  toApiTime,
  zonedToUtc,
} from '../api/publishLogic'
import { publishApi, type Channel, type Post } from '../api/publish'
import { href } from '../state/route'
import { Icon } from '../ui/Icon'
import { Button, Dialog, Segmented, Select, Spinner, cx } from '../ui/kit'
import '../styles/publish.css'

export interface PublishItem {
  clipId: string
  title: string
  revisionId: string
  duration?: number | null
  hook?: string | null
  /** Post title, caption and hashtags suggested at generation. */
  post?: SuggestedCopy | null
  width?: number | null
  height?: number | null
}

type When = 'now' | 'later' | 'draft'

function localZone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'
  } catch {
    return 'UTC'
  }
}

export function zoneOptions(current: string): { value: string; label: string }[] {
  let zones: string[] = []
  try {
    zones = (Intl as unknown as { supportedValuesOf?: (k: string) => string[] }).supportedValuesOf?.('timeZone') ?? []
  } catch {
    /* older browsers */
  }
  const all = new Set(['UTC', current, ...zones])
  return [...all].map((z) => ({ value: z, label: z.replace(/_/g, ' ') }))
}

export function PublishDialog({ items, onClose, beforeStart }: { items: PublishItem[]; onClose: () => void; beforeStart?: () => Promise<PublishItem[] | void> }) {
  const multi = items.length > 1
  const first = items[0]
  const defaults = useMemo(() => publishDefaults(first.title, first.hook, first.post), [first.title, first.hook, first.post])
  const zone = useMemo(localZone, [])
  const [channels, setChannels] = useState<Channel[] | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [picked, setPicked] = useState<Set<string>>(new Set())
  const [title, setTitle] = useState(defaults.title)
  const [description, setDescription] = useState(defaults.description)
  const [tags, setTags] = useState(formatHashtags(defaults.hashtags))
  const [when, setWhen] = useState<When>('now')
  const [local, setLocal] = useState(() => defaultSchedule(new Date(), zone))
  const [tz, setTz] = useState(zone)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [posts, setPosts] = useState<Post[] | null>(null)
  const alive = useRef(true)

  useEffect(() => {
    alive.current = true
    publishApi
      .channels()
      .then((list) => {
        if (!alive.current) return
        setChannels(list)
        setPicked(new Set(list.slice(0, 1).map((c) => c.id)))
      })
      .catch((e) => alive.current && setLoadError(e instanceof Error ? e.message : 'Could not load channels'))
    return () => {
      alive.current = false
    }
  }, [])

  const chosen = (channels ?? []).filter((c) => picked.has(c.id))
  const toYoutube = chosen.some((c) => c.platform === 'youtube')
  const checks = toYoutube ? shortsChecks({ duration: first.duration ?? null, width: first.width, height: first.height }) : []
  const schedule = when === 'later' ? checkSchedule(local, tz, new Date()) : null
  const utcHint = when === 'later' ? zonedToUtc(local, tz) : null
  const zones = useMemo(() => zoneOptions(zone), [zone])
  const tagList = parseHashtags(tags)
  const canSubmit = chosen.length > 0 && title.trim().length > 0 && (!schedule || schedule.ok) && !busy

  const toggle = (id: string) =>
    setPicked((s) => {
      const n = new Set(s)
      if (n.has(id)) n.delete(id)
      else n.add(id)
      return n
    })

  // After submitting, follow each post until it has been exported and uploaded.
  useEffect(() => {
    if (!posts) return
    const settled = (p: Post) => p.status === 'failed' || p.status === 'published' || (p.status === 'draft' && !p.rendering) || (p.status === 'scheduled' && !!p.external_id)
    if (posts.every(settled)) return
    const t = setTimeout(async () => {
      try {
        const ids = new Set(posts.map((p) => p.id))
        const fresh = (await Promise.all([...new Set(posts.map((p) => p.short_id))].map((id) => publishApi.listPosts(id)))).flat().filter((p) => ids.has(p.id))
        if (alive.current && fresh.length) setPosts(fresh)
      } catch {
        /* keep the last known state */
      }
    }, 1500)
    return () => clearTimeout(t)
  }, [posts])

  const submit = async () => {
    setBusy(true)
    setError(null)
    try {
      let list = items
      if (beforeStart) list = (await beforeStart()) ?? items
      const scheduled = schedule && schedule.ok ? toApiTime(schedule.at) : null
      const made: Post[] = []
      for (const item of list) {
        const d = multi ? publishDefaults(item.title, item.hook, item.post) : null
        made.push(
          ...(await publishApi.createPosts({
            short_id: item.clipId,
            revision_id: item.revisionId,
            platforms: chosen.map((c) => c.id),
            title: d ? d.title : title.trim(),
            description: d ? d.description : description.trim(),
            hashtags: d ? d.hashtags : tagList,
            scheduled_at: scheduled,
            draft: when === 'draft',
          })),
        )
      }
      if (alive.current) setPosts(made)
    } catch (e) {
      if (alive.current) setError(e instanceof Error ? e.message : 'Could not create the post')
    }
    if (alive.current) setBusy(false)
  }

  const verb = when === 'now' ? 'Publish now' : when === 'later' ? 'Schedule' : 'Save draft'
  const heading = multi ? `Publish ${items.length} clips` : 'Publish clip'

  if (posts)
    return (
      <Dialog open onClose={onClose} title={heading} width={560} footer={<Button kind="primary" onClick={onClose}>Done</Button>}>
        <ul className="export-list">
          {posts.map((p) => {
            const label = statusLabel(p)
            const working = label.tone === 'warn'
            return (
              <li key={p.id} className="export-row">
                <div className="export-main">
                  <div className="queue-title">{p.title}</div>
                  <div className="muted small">
                    {platformName(p.platform)}
                    {p.channel_name ? ` · ${p.channel_name}` : ''}
                    {p.scheduled_at ? ` · ${new Date(p.scheduled_at).toLocaleString()}` : ''}
                  </div>
                  <div className={cx('small', label.tone === 'bad' && 'bad', label.tone === 'ok' && 'pub-ok')}>{label.text}</div>
                  {p.error && <div className="bad small">{p.error}</div>}
                  {p.warnings.map((w) => <div key={w} className="small pub-warn">{w}</div>)}
                </div>
                <div className="export-actions">
                  {working && <Spinner />}
                  <a className="btn btn-secondary btn-sm" href={href.analytics(p.short_id)} onClick={onClose}>Analytics</a>
                </div>
              </li>
            )
          })}
        </ul>
        <p className="muted small pub-after">
          Exporting runs on your computer. Once uploaded, YouTube publishes scheduled Shorts by itself, so you can close Clippie.
        </p>
      </Dialog>
    )

  return (
    <Dialog
      open
      onClose={onClose}
      title={heading}
      width={560}
      footer={
        <>
          <Button kind="ghost" onClick={onClose}>Cancel</Button>
          <Button kind="primary" icon="upload" busy={busy} disabled={!canSubmit} onClick={() => void submit()}>{verb}</Button>
        </>
      }
    >
      <div className="pub-dialog">
        <div className="field">
          <span className="field-label">Publish to</span>
          {!channels && !loadError && <div className="muted small"><Spinner size={12} /> Loading channels…</div>}
          {loadError && <div className="bad small">{loadError}</div>}
          {channels && channels.length === 0 && (
            <div className="pub-nochannel">
              <p className="muted small">No channel is connected yet.</p>
              <a className="btn btn-secondary btn-sm" href={href.channels()} onClick={onClose}>Connect a channel</a>
            </div>
          )}
          {channels && channels.length > 0 && (
            <ul className="pub-channel-pick">
              {channels.map((c) => (
                <li key={c.id}>
                  <label className={cx('pub-pick', picked.has(c.id) && 'is-on')}>
                    <input type="checkbox" checked={picked.has(c.id)} onChange={() => toggle(c.id)} />
                    <span className={cx('pub-badge', `pub-${c.platform}`)} aria-hidden="true">{c.platform === 'youtube' ? 'YT' : c.platform === 'tiktok' ? 'TT' : 'IG'}</span>
                    <span>
                      <span className="pub-pick-name">{c.display_name}</span>
                      <span className="muted small"> {platformName(c.platform)}</span>
                    </span>
                  </label>
                </li>
              ))}
            </ul>
          )}
        </div>

        {multi ? (
          <p className="muted small">Each clip is posted with its own title and hook. Edit them per clip from the clip's Publish button.</p>
        ) : (
          <>
            <label className="field">
              <span className="field-label">Title <span className="muted">{title.length}/100</span></span>
              <input className="input" value={title} maxLength={100} onChange={(e) => setTitle(e.target.value)} data-autofocus />
            </label>
            <label className="field">
              <span className="field-label">Description</span>
              <textarea className="input" rows={3} value={description} maxLength={5000} onChange={(e) => setDescription(e.target.value)} placeholder="Optional. Starts from the hook line." />
            </label>
            <label className="field">
              <span className="field-label">Hashtags</span>
              <input className="input" value={tags} onChange={(e) => setTags(e.target.value)} placeholder="#ai #learning" />
              <span className="muted small">{toYoutube ? '#Shorts is added for YouTube.' : 'Separate with spaces.'}</span>
            </label>
          </>
        )}

        <div className="field">
          <span className="field-label">When</span>
          <Segmented
            label="When to publish"
            value={when}
            onChange={setWhen}
            options={[
              { value: 'now', label: 'Now' },
              { value: 'later', label: 'Schedule' },
              { value: 'draft', label: 'Save as draft' },
            ]}
          />
          {when === 'later' && (
            <div className="pub-when">
              <input className="input" type="datetime-local" value={local} onChange={(e) => setLocal(e.target.value)} aria-label="Date and time" />
              <Select label="Time zone" value={tz} onChange={setTz} options={zones} />
              <p className={cx('small', schedule && !schedule.ok ? 'bad' : 'muted')}>
                {schedule && !schedule.ok ? schedule.error : utcHint ? `Goes live ${utcHint.toISOString().slice(0, 16).replace('T', ' ')} UTC. YouTube publishes it even if this computer is off.` : ''}
              </p>
            </div>
          )}
          {when === 'draft' && <p className="muted small">Saved in Publishing. HD preparation and upload wait until you publish it.</p>}
        </div>

        {checks.length > 0 && (
          <ul className="pub-checks" aria-label="YouTube Shorts checks">
            {checks.map((c) => (
              <li key={c.id} className={cx(c.ok ? 'is-ok' : 'is-warn')}>
                <Icon name={c.ok ? 'check' : 'warn'} size={15} /> {c.text}
              </li>
            ))}
          </ul>
        )}
        <p className="muted small">Publish prepares a final HD render of this exact edit using the imported source. Draft previews are never uploaded. Sources and the final file must pass the 1080p quality checks.</p>
        {first.duration != null && !multi && <p className="muted small">Clip length {formatTime(first.duration)}.</p>}
        {error && <div className="bad small" role="alert">{error}</div>}
      </div>
    </Dialog>
  )
}
