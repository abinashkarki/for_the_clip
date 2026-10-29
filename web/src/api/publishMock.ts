// In-memory demo of /api/publish for ?mock=1. Retention is mapped onto the demo clip's real transcript words.
import { api } from './client'
import { ApiError } from './errors'
import { mapRetention, type RetentionPoint } from './publishLogic'
import type { Channel, CreatePostsInput, MetricsHistory, Performance, Post, PublishApi, PublishSettings, RetentionData } from './publish'

const iso = (ms: number) => new Date(ms).toISOString().replace(/\.\d{3}Z$/, 'Z')
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms))
let counter = 0
const uid = (p: string) => `${p}-${(++counter).toString(36)}`

const settings: PublishSettings = {
  youtube_client_id: '',
  youtube_client_secret_set: false,
  aggregator_key_set: false,
  aggregator_base_url: '',
  cloud_url: '',
  cloud_device_token_set: false,
  redirect_uri_hint: '/api/publish/oauth/youtube/callback',
}
const channels: Channel[] = []
const posts: Post[] = []
let seeded: Promise<void> | null = null

/** A curve with a steady fade and a cliff near 60%, so the demo shows a clear drop-off. */
export function demoCurve(): RetentionPoint[] {
  const pts: RetentionPoint[] = []
  for (let i = 0; i <= 100; i++) {
    const r = i / 100
    let watch = 1 - 0.28 * r
    if (r > 0.56) watch -= Math.min((r - 0.56) / 0.07, 1) * 0.3
    pts.push([r, Math.round(watch * 1000) / 1000, Math.round((0.62 - 0.35 * r) * 1000) / 1000])
  }
  return pts
}

function post(over: Partial<Post> & Pick<Post, 'short_id' | 'revision_id' | 'title'>): Post {
  return {
    id: uid('post'), platform: 'youtube', channel_id: 'ch-yt', channel_name: 'Clippie Demo', artifact_id: 'art-1', description: '',
    hashtags: [], status: 'draft', scheduled_at: null, published_at: null, created_at: iso(Date.now() - 86400000),
    external_id: null, url: null, error: null, warnings: [], duration: 31, rendering: false, metrics: null, metrics_at: null,
    project_id: 'proj-khan', project_name: 'Khanmigo interview', project_deleted: false, ...over,
  }
}

function seed(): Promise<void> {
  if (!seeded)
    seeded = (async () => {
      channels.push({ id: 'ch-yt', platform: 'youtube', provider: 'youtube', display_name: 'Clippie Demo', external_id: 'UCdemo', scopes: [], created_at: iso(Date.now() - 9 * 86400000) })
      settings.youtube_client_id = '1234567890-demo.apps.googleusercontent.com'
      settings.youtube_client_secret_set = true
      let revision = 'rev-current'
      try {
        revision = (await api.getClip('short-7d1c')).revision_id
      } catch {
        /* demo clip unavailable */
      }
      const day = 86400000
      posts.push(
        post({
          short_id: 'short-7d1c', revision_id: 'rev-9b3e', title: "Khanmigo asks how you'd approach it", hashtags: ['khanmigo', 'tutor'],
          status: 'published', created_at: iso(Date.now() - 6 * day), published_at: iso(Date.now() - 5 * day), external_id: 'demo1', url: 'https://youtube.com/shorts/demo1',
          metrics: { views: 4210, likes: 311, comments: 24, shares: 57, subscribers_gained: 12, avg_view_duration_s: 14, avg_view_percentage: 45, watch_minutes: 982 }, metrics_at: iso(Date.now() - 3600000),
        }),
        post({
          short_id: 'short-7d1c', revision_id: revision, title: 'This AI tutor refuses to give answers', hashtags: ['khanmigo', 'education'],
          status: 'published', created_at: iso(Date.now() - 2 * day), published_at: iso(Date.now() - 2 * day + 3600000), external_id: 'demo2', url: 'https://youtube.com/shorts/demo2',
          metrics: { views: 15860, likes: 1342, comments: 96, shares: 288, subscribers_gained: 74, avg_view_duration_s: 19, avg_view_percentage: 63, watch_minutes: 5021 }, metrics_at: iso(Date.now() - 3600000),
        }),
        post({
          short_id: 'short-7d1c', revision_id: revision, title: 'This AI tutor refuses to give answers', platform: 'instagram', channel_id: 'ch-ig', channel_name: 'clippie.demo',
          status: 'scheduled', scheduled_at: iso(Date.now() + day), external_id: 'agg-9', created_at: iso(Date.now() - 3600000),
        }),
      )
      const other = { project_id: 'proj-ai', project_name: 'AI in the classroom panel' }
      const published = (n: number, ago: number) => ({
        status: 'published' as const, created_at: iso(Date.now() - (ago + 1) * day), published_at: iso(Date.now() - ago * day), external_id: `demo-x${n}`,
        url: `https://youtube.com/shorts/demo-x${n}`, metrics: { views: 800 * n }, metrics_at: iso(Date.now() - 3600000),
      })
      for (let n = 1; n <= 7; n++)
        posts.push(post({ short_id: `short-panel-${n}`, revision_id: 'rev-p', title: `Panel highlight ${n}`, ...other, ...published(n, 3 + n) }))
      posts.push(
        post({ short_id: 'short-panel-1', revision_id: 'rev-p', title: 'Panel highlight 1', ...other, platform: 'tiktok', channel_id: 'ch-tt', channel_name: 'clippie.demo', status: 'failed', error: 'TikTok rejected the upload.', created_at: iso(Date.now() - 4 * 3600000) }),
        post({ short_id: 'short-old-1', revision_id: 'rev-o', title: 'Why the lecture is dead', project_id: 'proj-gone', project_name: 'Old keynote', project_deleted: true, ...published(3, 40) }),
        post({ short_id: 'short-old-2', revision_id: 'rev-o', title: 'Three questions to ask', project_id: 'proj-gone', project_name: 'Old keynote', project_deleted: true, ...published(2, 41) }),
      )
    })()
  return seeded
}

const find = (pid: string) => {
  const p = posts.find((x) => x.id === pid)
  if (!p) throw new ApiError(404, 'Post not found.')
  return p
}

export const mockPublish: PublishApi = {
  async settings() {
    await seed()
    return { ...settings }
  },
  async saveSettings(patch) {
    await seed()
    if (patch.youtube_client_id !== undefined) settings.youtube_client_id = patch.youtube_client_id
    if (patch.youtube_client_secret !== undefined) settings.youtube_client_secret_set = !!patch.youtube_client_secret
    if (patch.aggregator_key !== undefined) settings.aggregator_key_set = !!patch.aggregator_key
    if (patch.cloud_url !== undefined) settings.cloud_url = patch.cloud_url
    if (patch.cloud_device_token !== undefined) settings.cloud_device_token_set = !!patch.cloud_device_token
    return { ...settings }
  },
  async channels() {
    await seed()
    return channels.map((c) => ({ ...c }))
  },
  async connectYoutube() {
    await seed()
    if (!settings.youtube_client_id || !settings.youtube_client_secret_set) throw new ApiError(422, 'Add your Google Cloud OAuth client ID and secret first.')
    if (!channels.some((c) => c.id === 'ch-yt'))
      channels.unshift({ id: 'ch-yt', platform: 'youtube', provider: 'youtube', display_name: 'Clippie Demo', external_id: 'UCdemo', scopes: [], created_at: iso(Date.now()) })
    return { authorization_url: '', state: 'mock', redirect_uri: '', mock: true }
  },
  async connectAggregator(key) {
    await seed()
    if (key) settings.aggregator_key_set = true
    if (!settings.aggregator_key_set) throw new ApiError(422, 'Add your posting-service API key first.')
    for (const [id, platform, name] of [['ch-tt', 'tiktok', 'clippie.demo'], ['ch-ig', 'instagram', 'clippie.demo']] as const)
      if (!channels.some((c) => c.id === id)) channels.push({ id, platform, provider: 'aggregator', display_name: name, external_id: id, scopes: [], created_at: iso(Date.now()) })
    return channels.filter((c) => c.provider === 'aggregator').map((c) => ({ ...c }))
  },
  async disconnect(id) {
    await seed()
    const i = channels.findIndex((c) => c.id === id)
    if (i < 0) throw new ApiError(404, 'Channel not found.')
    channels.splice(i, 1)
  },
  async createPosts(input: CreatePostsInput) {
    await seed()
    const made = input.platforms.map((name) => {
      const channel = channels.find((c) => c.id === name || c.platform === name)
      if (!channel) throw new ApiError(422, `No ${name} channel is connected. Connect one on the Channels page.`)
      return post({
        short_id: input.short_id, revision_id: input.revision_id, title: input.title, description: input.description, hashtags: input.hashtags,
        platform: channel.platform, channel_id: channel.id, channel_name: channel.display_name, created_at: iso(Date.now()),
        status: input.draft ? 'draft' : 'scheduled', scheduled_at: input.scheduled_at ?? null, rendering: true, artifact_id: null,
      })
    })
    posts.push(...made)
    setTimeout(() => made.forEach((p) => (p.rendering = false)), 1800)
    setTimeout(() => {
      for (const p of made) if (p.status === 'scheduled') Object.assign(p, { external_id: uid('demo'), ...(p.scheduled_at ? {} : { status: 'published', published_at: iso(Date.now()) }) })
    }, 3200)
    return made.map((p) => ({ ...p }))
  },
  async listPosts(shortId) {
    await seed()
    return posts.filter((p) => !shortId || p.short_id === shortId).map((p) => ({ ...p }))
  },
  async publishPost(id) {
    await seed()
    const p = find(id)
    p.status = 'scheduled'
    return { ...p }
  },
  async retryPost(id) {
    await seed()
    const p = find(id)
    Object.assign(p, { status: 'scheduled', error: null })
    return { ...p }
  },
  async resolveUncertain(id) {
    await seed()
    const p = find(id)
    p.error = 'Platform checked. You can retry the upload when ready.'
    return { ...p }
  },
  async deletePost(id) {
    await seed()
    posts.splice(posts.indexOf(find(id)), 1)
  },
  async syncPost(id) {
    await seed()
    await sleep(400)
    return { ...find(id) }
  },
  async metrics(id): Promise<MetricsHistory> {
    await seed()
    const p = find(id)
    const latest = p.metrics ? { fetched_at: p.metrics_at ?? iso(Date.now()), source: 'local', metrics: p.metrics } : null
    return { post: { ...p }, latest, history: latest ? [latest] : [] }
  },
  async retention(id): Promise<RetentionData> {
    await seed()
    const p = find(id)
    if (p.status !== 'published') return { post_id: id, revision_id: p.revision_id, fetched_at: null, points: [], duration: p.duration, words: [], drops: [] }
    const detail = await api.getClip(p.short_id)
    const words = detail.words.filter((w) => !w.removed && w.program_start != null).map((w) => ({ text: w.text, start: w.program_start as number, end: w.program_end as number }))
    const duration = detail.clip.duration
    const points = demoCurve()
    const mapped = mapRetention(points, words, duration)
    return { post_id: id, revision_id: p.revision_id, fetched_at: iso(Date.now() - 3600000), points, duration, words: mapped.words, drops: mapped.drops }
  },
  async performance(shortId): Promise<Performance> {
    await seed()
    const mine = posts.filter((p) => p.short_id === shortId)
    const totals: Record<string, number> = { views: 0, likes: 0, comments: 0, shares: 0, saves: 0, subscribers_gained: 0 }
    const groups = new Map<string, { revision_id: string; posts: number; views: number }>()
    for (const p of mine) {
      const g = groups.get(p.revision_id) ?? { revision_id: p.revision_id, posts: 0, views: 0 }
      g.posts++
      g.views += p.metrics?.views ?? 0
      groups.set(p.revision_id, g)
      for (const k of Object.keys(totals)) totals[k] += (p.metrics as Record<string, number> | null)?.[k] ?? 0
    }
    let current: string | null = null
    try {
      current = (await api.getClip(shortId)).revision_id
    } catch {
      /* ignore */
    }
    return { short_id: shortId, current_revision_id: current, posts: mine.map((p) => ({ ...p })), totals, revisions: [...groups.values()] }
  },
  async cloudSync() {
    await seed()
    await sleep(500)
    return { posts_registered: 2, snapshots_merged: 3 }
  },
}
