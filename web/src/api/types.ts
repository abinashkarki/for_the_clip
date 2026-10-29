import type { SourceWord, TemplateRegistry, Timeline } from '../render/types'

export type { TemplateRegistry }

export type StageId = 'download' | 'merge' | 'preview' | 'transcribe' | 'index'
export type StageStatus = 'pending' | 'active' | 'done' | 'failed' | 'skipped'

export interface IntakeStage {
  id: StageId
  label: string
  status: StageStatus
  /** 0..1, or null when the step cannot say how far along it is */
  fraction: number | null
  /** e.g. "1.83 of 3.13 GB · 3.4 MB/s · ~6 min left" */
  detail: string | null
  started_at: string | null
  updated_at: string | null
  eta_seconds: number | null
  stalled: boolean
  stall_reason?: string | null
}

export interface QueueInfo {
  lane: 'network' | 'local'
  ahead: number
  position: number
  /** e.g. "Waiting · 1 video ahead (transcribing, ~8 min left)" */
  message: string
}

export interface ClipRange {
  start: number
  end: number
}

export interface SourceSummary {
  id: string
  title: string
  status: string
  progress: number
  duration: number | null
  thumbnail_url: string | null
  media_url: string | null
  error: string | null
  message?: string | null
  kind?: 'url' | 'file'
  stages?: IntakeStage[]
  queue?: QueueInfo | null
  stalled?: boolean
  stall_reason?: string | null
  eta_seconds?: number | null
  quality?: SourceQuality | null
  range?: ClipRange | null
  cancellable?: boolean
  retryable?: boolean
  freed?: boolean
}

export type SourceQuality = 'standard' | 'high' | 'small'

export interface InspectOption {
  id: SourceQuality | 'audio_first'
  label: string
  download_bytes: number | null
  height?: number | null
  strategy?: boolean
  detail?: string
}

export interface InspectResult {
  title: string
  duration: number | null
  thumbnail_url: string | null
  uploader: string | null
  options: InspectOption[]
  estimates: {
    download_bytes: Record<string, number | null>
    download_seconds: Record<string, number | null>
    transcribe_seconds: number | null
  }
}

export type ActivityState = 'running' | 'waiting' | 'attention' | 'done'

export interface ActivityAction {
  id: 'cancel' | 'retry' | 'resume'
  label: string
}

export interface ActivityItem {
  kind: 'source' | 'project' | 'export' | 'reframe' | 'publish'
  id: string
  title: string
  state: ActivityState
  status: string
  fraction: number | null
  message: string | null
  eta_seconds: number | null
  stalled: boolean
  stall_reason: string | null
  thumbnail_url: string | null
  stages: IntakeStage[]
  queue: QueueInfo | null
  actions: ActivityAction[]
  diagnostics?: { attempt: number; updated_at: number | null; events: { at: number; status: string; message: string; attempt: number }[] }
  open: { route: 'library' } | { route: 'project' | 'clip' | 'publishing'; id: string } | { url: string }
}

export interface ActivityResponse {
  items: ActivityItem[]
  running: number
  attention: number
}

export interface ProjectOptions {
  aspect: '9:16' | '16:9' | '1:1' | '4:5'
  count: 'auto' | number
  max_duration: number
  template_id?: string
  brand_kit_id?: string
  focus?: string
}

export interface ProjectSummary {
  id: string
  name: string
  source_ids: string[]
  status: string
  clip_count: number
  created_at: string
  thumbnail_url: string | null
  progress?: ProjectProgress
  lifecycle: ProjectLifecycle
  last_activity_at: string
  keep_source: boolean
  /** null when pinned, never, or blocked */
  source_expires_at: string | null
  size_bytes: ProjectSizes
  trashed_at: string | null
  purge_at: string | null
}

export type ProjectLifecycle = 'active' | 'source_freed' | 'trashed'
export interface ProjectSizes {
  source: number
  working: number
  exports: number
}

export interface StorageInfo {
  total_bytes: number
  sources_bytes: number
  working_bytes: number
  exports_bytes: number
  models_bytes: number
  other_bytes: number
  reclaimable_bytes: number
  /** 0 means never */
  retention_days: number
}

export interface ProjectProgress {
  stage: string
  fraction: number
  basis?: 'recorded_tasks'
  completed_tasks?: number
  recorded_tasks?: number
  label?: string
}

export interface ClipSummary {
  id: string
  title: string
  score: number | null
  score_reasons: string[]
  duration: number
  revision_id: string
  thumbnail_url: string | null
  preview_url: string | null
  status: string
  status_message?: string
  status_action?: { id: 'resume_project'; label: string }
  held_review?: {
    revision_id: string
    title: string
    hook_line: string
    status: string
    issues: { category: string; severity: string; detail: string; unit_ids?: string[] }[]
  }
  format: string | null
  /** Suggested public copy written at generation: post title, caption and hashtags. */
  post?: PostCopy
}

export interface PostCopy {
  title: string
  description: string
  hashtags: string[]
  hook_line: string
}

export interface ProjectDetail {
  clip_count?: number
  counts?: { ready: number; held: number; running: number; stopped: number }
  clip_counts?: { ready: number; held: number; processing: number; waiting: number; stopped: number; cancelled: number }
  task_counts?: { completed: number; running: number; stopped: number; cancelled: number }
  id: string
  name: string
  status: string
  progress: ProjectProgress
  sources: SourceSummary[]
  clips: ClipSummary[]
  error?: string
  resumable?: boolean
}

export interface ApiWord extends SourceWord {
  program_start: number | null
  program_end: number | null
  removed: boolean
  emphasis: boolean
  hidden: boolean
}

export interface ClipSource {
  title: string
  duration: number
  media_url: string
  width: number
  height: number
}

export interface ClipRevisionInfo {
  id: string
  created_at: string
  origin: string
}

export interface ClipDetail {
  clip: ClipSummary
  project_id: string
  revision_id: string
  timeline: Timeline
  words: ApiWord[]
  sources: Record<string, ClipSource>
  revisions: ClipRevisionInfo[]
}

export interface SaveResult {
  revision_id: string
  timeline: Timeline
}

export interface JobInfo {
  message?: string
  id: string
  kind: string
  status: 'queued' | 'running' | 'done' | 'failed'
  progress: number
  error: string | null
  result: Record<string, unknown> | null
}

export interface ExportResult {
  video_url: string
  srt_url: string
  duration: number
}

export interface BrandKitInput {
  name: string
  template_id: string
  overrides: Record<string, unknown>
  hook_style: string
  progress_bar: { enabled: boolean; color: string }
  logo_asset_id: string | null
}
export interface BrandKit extends BrandKitInput {
  id: string
  logo_url?: string | null
}

export interface UploadHandle {
  promise: Promise<{ source_id: string; status: string }>
  abort: () => void
}

export type ActionResponse = SaveResult | { job_id: string }

export interface StudioApi {
  listSources(): Promise<SourceSummary[]>
  upload(file: File, title: string | undefined, onProgress: (f: number) => void): UploadHandle
  addSource(input: { url?: string; path?: string; title?: string; quality?: SourceQuality; range?: ClipRange }): Promise<{ source_id: string; status: string }>
  inspectSource(url: string): Promise<InspectResult>
  cancelSource(id: string): Promise<SourceSummary>
  retrySource(id: string): Promise<SourceSummary>
  getActivity(): Promise<ActivityResponse>
  listProjects(opts?: { includeTrashed?: boolean }): Promise<ProjectSummary[]>
  trashProject(id: string): Promise<ProjectSummary>
  untrashProject(id: string): Promise<ProjectSummary>
  /** permanent; only for trashed projects */
  deleteProject(id: string): Promise<void>
  freeSource(id: string): Promise<ProjectSummary>
  keepSource(id: string, keep: boolean): Promise<ProjectSummary>
  restoreSource(id: string): Promise<ProjectSummary>
  restoreUpload(id: string, sourceId: string, file: File): Promise<ProjectSummary>
  dismissSource(id: string): Promise<void>
  getStorage(): Promise<StorageInfo>
  setRetention(days: number): Promise<StorageInfo>
  freeStorageNow(): Promise<StorageInfo>
  createProject(input: { source_ids: string[]; name?: string; options: ProjectOptions }): Promise<ProjectSummary>
  getProject(id: string): Promise<ProjectDetail>
  resumeProject(id: string): Promise<ProjectDetail>
  getClip(id: string): Promise<ClipDetail>
  saveTimeline(id: string, body: { base_revision: string; timeline: Timeline; request_id: string }): Promise<SaveResult>
  removeFillers(id: string, body: { base_revision: string; request_id: string; silence_min: number }): Promise<SaveResult>
  autoReframe(id: string, body: { base_revision: string; request_id: string }): Promise<ActionResponse>
  applyBrandKit(id: string, body: { base_revision: string; request_id: string; brand_kit_id: string }): Promise<SaveResult>
  exportClip(id: string, body: { revision_id: string; quality: 'preview' | '1080p' }): Promise<{ job_id: string }>
  getJob(id: string): Promise<JobInfo>
  templates(): Promise<TemplateRegistry>
  listBrandKits(): Promise<BrandKit[]>
  saveBrandKit(kit: BrandKitInput, id?: string): Promise<BrandKit>
  deleteBrandKit(id: string): Promise<void>
  uploadBrandAsset(file: File): Promise<{ asset_id: string; url: string }>
}
