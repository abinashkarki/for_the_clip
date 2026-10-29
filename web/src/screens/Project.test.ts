import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { ProjectScreen } from './Project'

const state = vi.hoisted(() => ({ data: {} as Record<string, unknown> }))

vi.mock('../state/hooks', async (original) => ({
  ...await original<typeof import('../state/hooks')>(),
  useDocumentTitle: () => {},
  useAsync: () => ({ data: state.data, reload: () => {} }),
}))

beforeEach(() => {
  state.data = {
    id: 'held-project', name: 'Review project', status: 'needs_attention',
    resumable: false, sources: [],
    error: 'Open a clip to inspect its review.',
    clips: [{ id: 'held-clip', title: 'Inspect this held clip', duration: 10,
      status: 'needs_attention', score: 75, score_reasons: [],
      held_review: { revision_id: 'held', title: 'Held title', hook_line: 'Claims life extension', status: 'needs_revision',
        issues: [{ severity: 'critical', category: 'faithfulness', detail: 'The hook omits in dish.' }] } }],
  }
})

describe('review-held project', () => {
  it('keeps the held clip accessible when there are no ready clips', () => {
    const html = renderToStaticMarkup(createElement(ProjectScreen, { id: 'held-project' }))
    expect(html).toContain('Review the clips below')
    expect(html).not.toContain('href="#/c/held-clip"')
    expect(html).toContain('Inspect this held clip')
    expect(html).toContain('Inspect editorial review')
    expect(html).toContain('The hook omits in dish.')
    expect(html).toContain('Claims life extension')
    expect(html).not.toContain('Something went wrong')
  })
})

describe('shared generation steps', () => {
  it('shows a stopped sibling reason and project Resume without an unavailable editor link', () => {
    state.data = { ...state.data, status: 'partially_ready', resumable: true, clip_count: 1,
      clips: [
        { id: 'ready', title: 'Ready clip', duration: 10, status: 'ready', revision_id: 'r', score: 75, score_reasons: [] },
        { id: 'paused', title: 'Waiting clip', duration: null, status: 'paused', score: 70, score_reasons: [],
          status_message: 'Waiting for fixing titles and hooks. This step has stopped.',
          status_action: { id: 'resume_project', label: 'Resume project' } },
      ] }
    const html = renderToStaticMarkup(createElement(ProjectScreen, { id: 'project' }))
    expect(html).toContain('href="#/c/ready"')
    expect(html).not.toContain('href="#/c/paused"')
    expect(html).toContain('This step has stopped.')
    expect(html).toContain('Resume project')
    expect(html).not.toContain('NaN')
  })

  it('labels completed recorded steps without claiming percentage completion and keeps ready clips visible', () => {
    state.data = { ...state.data, status: 'processing',
      progress: { stage: 'Fixing titles and hooks', fraction: 0.923, basis: 'recorded_tasks', label: '12 steps finished' },
      clips: [{ id: 'ready', title: 'Ready while others run', duration: 10, status: 'ready', revision_id: 'r', score: 75, score_reasons: [] }] }
    const html = renderToStaticMarkup(createElement(ProjectScreen, { id: 'project' }))
    expect(html).toContain('12 steps finished')
    expect(html).not.toContain('92%')
    expect(html).toContain('href="#/c/ready"')
    expect(html).not.toContain('Rendering previews')
  })
})
