import { useCallback, useState } from 'react'
import {
  fmtCost,
  fmtMs,
  fmtSeconds,
  fmtTokens,
  systemApi,
  title,
  totals,
  type ModelCall,
  type ModelCallDetail,
  type ModelGroup,
  type RunDetail,
  type RunRow,
} from '../api/system'
import { useAsync, useDocumentTitle } from '../state/hooks'
import { Dialog, EmptyState, Spinner, cx } from '../ui/kit'
import '../styles/system.css'

const when = (seconds: number) =>
  new Date(seconds * 1000).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit' })
const whenNs = (ns: number) => when(ns / 1e9)

export function ModelsScreen() {
  useDocumentTitle('Models')
  const summary = useAsync(() => systemApi.modelSummary(), [], () => 15000)
  return (
    <main className="page sys-page">
      <header className="sys-head">
        <div>
          <h1 className="sys-title">Models</h1>
          <p className="muted">Every model call Clippie makes, what it cost and how long it took. Unknown prices are shown as unknown, never as zero.</p>
        </div>
      </header>
      <Overview groups={summary.data?.groups ?? []} pricesAsOf={summary.data?.prices_as_of ?? null} loading={summary.loading && !summary.data} error={summary.error} />
      <CallsSection />
      <SystemSection />
    </main>
  )
}

function Stat({ label, value, note }: { label: string; value: string; note?: string }) {
  return (
    <div className="sys-stat">
      <span className="sys-stat-label">{label}</span>
      <strong className="sys-stat-value">{value}</strong>
      {note && <span className="sys-stat-note">{note}</span>}
    </div>
  )
}

function Overview({ groups, pricesAsOf, loading, error }: { groups: ModelGroup[]; pricesAsOf: string | null; loading: boolean; error: string | null }) {
  if (error && !groups.length) return <EmptyState title="Model history is unavailable">{error}</EmptyState>
  if (loading) return <div className="sys-loading"><Spinner /> Loading</div>
  const t = totals(groups)
  return (
    <section className="sys-section" aria-labelledby="by-model-h">
      <div className="sys-stats">
        <Stat label="Calls" value={String(t.calls)} />
        <Stat label="Failures" value={String(t.failures)} note={t.calls ? `${((t.failures / t.calls) * 100).toFixed(1)}% of calls` : undefined} />
        <Stat label="Tokens" value={fmtTokens(t.tokens)} note="input, output and thinking" />
        <Stat label="Cost" value={fmtCost(t.cost, t.cost && t.unknownCalls ? t.unknownCalls : 0)} note={pricesAsOf ? `list prices as of ${pricesAsOf}` : undefined} />
      </div>
      <div className="section-head">
        <h2 className="section-title" id="by-model-h">By provider, model and stage</h2>
      </div>
      {groups.length === 0 ? (
        <EmptyState title="No model calls yet">Calls appear here as soon as a project is generated.</EmptyState>
      ) : (
        <div className="sys-table-wrap">
          <table className="sys-table sys-num">
            <thead>
              <tr>
                <th>Provider / model</th><th>Stage</th><th>Calls</th><th>Failed</th><th>p50</th><th>p95</th>
                <th>Input</th><th>Output</th><th>Thinking</th><th>Cost</th>
              </tr>
            </thead>
            <tbody>
              {groups.map((g) => (
                <tr key={`${g.provider}/${g.model}/${g.stage}`}>
                  <th scope="row"><div className="sys-model"><span>{g.provider}</span><code>{g.model}</code></div></th>
                  <td>{g.stage}</td>
                  <td>{g.calls}</td>
                  <td className={cx(g.failures > 0 && 'bad')}>{g.failures}</td>
                  <td>{fmtMs(g.p50_latency_ms)}</td>
                  <td>{fmtMs(g.p95_latency_ms)}</td>
                  <td>{fmtTokens(g.input_tokens)}</td>
                  <td>{fmtTokens(g.output_tokens)}</td>
                  <td>{fmtTokens(g.thinking_tokens)}</td>
                  <td className={cx(g.cost_usd === null && 'sys-unknown')}>{fmtCost(g.cost_usd, g.cost_usd !== null ? g.cost_unknown_calls : 0)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}

function CallsSection() {
  const [stage, setStage] = useState('')
  const [provider, setProvider] = useState('')
  const [status, setStatus] = useState('')
  const [open, setOpen] = useState<string | null>(null)
  const close = useCallback(() => setOpen(null), [])
  const catalog = useAsync(() => systemApi.catalog(), [])
  const calls = useAsync(() => systemApi.modelCalls({ stage, provider, status }), [stage, provider, status], () => 15000)

  return (
    <section className="sys-section" aria-labelledby="calls-h">
      <div className="section-head">
        <h2 className="section-title" id="calls-h">Recent calls</h2>
        <div className="sys-filters">
          <select aria-label="Filter by stage" value={stage} onChange={(e) => setStage(e.target.value)}>
            <option value="">All stages</option>
            {(catalog.data?.stages ?? []).map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
          <select aria-label="Filter by provider" value={provider} onChange={(e) => setProvider(e.target.value)}>
            <option value="">All providers</option>
            {(catalog.data?.providers ?? []).map((p) => <option key={p.id} value={p.id}>{p.label}</option>)}
          </select>
          <select aria-label="Filter by outcome" value={status} onChange={(e) => setStatus(e.target.value)}>
            <option value="">Any outcome</option>
            <option value="ok">Succeeded</option>
            <option value="error">Failed</option>
          </select>
        </div>
      </div>
      {calls.error && !calls.data && <p className="bad small">{calls.error}</p>}
      {calls.data && calls.data.length === 0 && <EmptyState title="No calls match">Change the filters or generate a project.</EmptyState>}
      {calls.data && calls.data.length > 0 && (
        <div className="sys-table-wrap sys-scroll">
          <table className="sys-table sys-num sys-calls">
            <thead>
              <tr><th>Time</th><th>Model</th><th>Stage</th><th>Outcome</th><th>Latency</th><th>In</th><th>Out</th><th>Cost</th></tr>
            </thead>
            <tbody>
              {calls.data.map((c) => <CallRow key={c.call_id} call={c} onOpen={() => setOpen(c.call_id)} />)}
            </tbody>
          </table>
        </div>
      )}
      {open && <CallDialog id={open} onClose={close} />}
    </section>
  )
}

function CallRow({ call: c, onOpen }: { call: ModelCall; onOpen: () => void }) {
  return (
    <tr className="sys-click" onClick={onOpen}>
      <td>
        <button className="sys-rowbtn" onClick={(e) => { e.stopPropagation(); onOpen() }} aria-label={`Inspect call from ${when(c.created_at)}`}>{when(c.created_at)}</button>
      </td>
      <th scope="row"><div className="sys-model"><span>{c.provider}</span><code>{c.model}</code></div></th>
      <td>{c.stage}</td>
      <td><span className={cx('sys-status', c.status === 'ok' ? 'is-ok' : 'is-bad')}><i /> {c.status === 'ok' ? 'OK' : title(c.error_category ?? 'failed')}</span></td>
      <td>{fmtMs(c.latency_ms)}</td>
      <td>{fmtTokens(c.input_tokens)}</td>
      <td>{fmtTokens(c.output_tokens)}</td>
      <td className={cx(c.cost_usd === null && 'sys-unknown')}>{fmtCost(c.cost_usd)}</td>
    </tr>
  )
}

function Blob({ label, value }: { label: string; value: Record<string, unknown> | null }) {
  if (!value) return <section><h3 className="sys-blob-h">{label}</h3><p className="muted small">Not archived for this call.</p></section>
  const text = typeof value.prompt === 'string' ? value.prompt : typeof value.text === 'string' ? value.text : null
  const rest = Object.fromEntries(Object.entries(value).filter(([k, v]) => k !== 'prompt' && k !== 'text' && v !== null))
  return (
    <section>
      <h3 className="sys-blob-h">{label}</h3>
      {text !== null && <pre className="sys-pre" tabIndex={0}>{text}</pre>}
      {Object.keys(rest).length > 0 && <pre className="sys-pre sys-pre-meta" tabIndex={0}>{JSON.stringify(rest, null, 2)}</pre>}
    </section>
  )
}

function CallDialog({ id, onClose }: { id: string; onClose: () => void }) {
  const detail = useAsync<ModelCallDetail>(() => systemApi.modelCall(id), [id])
  const d = detail.data
  return (
    <Dialog open onClose={onClose} title="Model call" width={860}>
      {detail.error && <p className="bad">{detail.error}</p>}
      {!d && !detail.error && <div className="sys-loading"><Spinner /> Loading</div>}
      {d && (
        <div className="sys-call">
          <dl className="sys-facts">
            <div><dt>Model</dt><dd>{d.provider} / {d.model}</dd></div>
            <div><dt>Stage</dt><dd>{d.stage}</dd></div>
            <div><dt>Outcome</dt><dd>{d.status === 'ok' ? 'OK' : title(d.error_category ?? 'failed')}</dd></div>
            <div><dt>Latency</dt><dd>{fmtMs(d.latency_ms)}</dd></div>
            <div><dt>Tokens</dt><dd>{fmtTokens(d.input_tokens)} in, {fmtTokens(d.output_tokens)} out{d.thinking_tokens ? `, ${fmtTokens(d.thinking_tokens)} thinking` : ''}</dd></div>
            <div><dt>Cost</dt><dd>{fmtCost(d.cost_usd)}</dd></div>
          </dl>
          <Blob label="Request" value={d.request} />
          <Blob label="Response" value={d.response} />
        </div>
      )}
    </Dialog>
  )
}

const OBSERVATIONS = [
  ['user', 'Your activity'],
  ['qa', 'QA activity'],
  ['synthetic', 'Synthetic activity'],
  ['all', 'All activity'],
] as const

function SystemSection() {
  const [observation, setObservation] = useState<string>('user')
  const [open, setOpen] = useState<string | null>(null)
  const close = useCallback(() => setOpen(null), [])
  const sys = useAsync(() => systemApi.systemSummary(observation), [observation], () => 20000)
  const s = sys.data
  return (
    <section className="sys-section" aria-labelledby="runs-h">
      <div className="section-head">
        <h2 className="section-title" id="runs-h">System runs</h2>
        <div className="sys-filters">
          <select aria-label="Activity group" value={observation} onChange={(e) => setObservation(e.target.value)}>
            {OBSERVATIONS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
          </select>
        </div>
      </div>
      {sys.error && !s && <p className="bad small">{sys.error}</p>}
      {s && (
        <>
          <div className="sys-stats">
            <Stat label="Attempts" value={String(s.attempts)} note={`last ${s.window_days} days`} />
            <Stat label="Failed or interrupted" value={`${s.failures + s.interruptions} / ${s.terminal_attempts}`} note="completed attempts" />
            <Stat label="Typical time to ready" value={fmtSeconds(s.time_to_ready_p50_seconds)} note="successful video attempts" />
          </div>
          <div className="sys-two">
            <div>
              <h3 className="sys-sub">Performance by workflow</h3>
              {s.performance.length === 0 ? <p className="muted small">Timing appears after the first completed run.</p> : (
                <div className="sys-table-wrap">
                  <table className="sys-table sys-num">
                    <thead><tr><th>Workflow</th><th>Cache</th><th>Done</th><th>Failed</th><th>p50</th><th>p95</th></tr></thead>
                    <tbody>
                      {s.performance.map((p) => (
                        <tr key={`${p.workflow}/${p.cache_state}`}>
                          <th scope="row">{title(p.workflow)}</th><td>{p.cache_state}</td><td>{p.successes}/{p.attempts}</td><td>{p.failed}</td>
                          <td>{fmtSeconds(p.p50_seconds)}</td><td>{fmtSeconds(p.p95_seconds)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
            <div>
              <h3 className="sys-sub">Recent runs</h3>
              {s.recent_runs.length === 0 ? <p className="muted small">No recorded runs in this group yet.</p> : (
                <ul className="sys-runs">
                  {s.recent_runs.slice(0, 12).map((r) => <RunItem key={r.id} run={r} onOpen={() => setOpen(r.id)} />)}
                </ul>
              )}
            </div>
          </div>
          <p className="sys-foot small muted">
            {s.diagnostics.last_error ? `History has gaps: ${s.diagnostics.last_error}. ` : ''}{s.scope}
          </p>
        </>
      )}
      {open && <RunDialog id={open} onClose={close} />}
    </section>
  )
}

function RunItem({ run, onOpen }: { run: RunRow; onOpen: () => void }) {
  const seconds = run.ended_ns ? (run.ended_ns - run.created_ns) / 1e9 : null
  return (
    <li>
      <button className="sys-run" onClick={onOpen}>
        <span className="sys-run-top"><strong>{title(run.workflow)}</strong><span className={cx('sys-status', run.status === 'success' ? 'is-ok' : run.status === 'failed' ? 'is-bad' : 'is-off')}><i /> {title(run.status)}</span></span>
        <span className="muted small">{whenNs(run.created_ns)} · {seconds === null ? 'in progress' : fmtSeconds(seconds)} · {run.id.slice(0, 8)}</span>
      </button>
    </li>
  )
}

function RunDialog({ id, onClose }: { id: string; onClose: () => void }) {
  const run = useAsync<RunDetail>(() => systemApi.systemRun(id), [id])
  const r = run.data
  const total = r ? Math.max(1, (r.ended_ns ?? Date.now() * 1e6) - r.created_ns) : 1
  return (
    <Dialog open onClose={onClose} title="Run" width={760}>
      {run.error && <p className="bad">{run.error}</p>}
      {!r && !run.error && <div className="sys-loading"><Spinner /> Loading</div>}
      {r && (
        <div className="sys-call">
          <p className="muted">{title(r.workflow)} · {title(r.status)} · {whenNs(r.created_ns)}</p>
          <h3 className="sys-blob-h">Stages</h3>
          <ul className="sys-spans">
            {r.spans.filter((x) => x.name !== 'workflow').map((x) => {
              const len = Math.max(0, (x.end_ns ?? r.ended_ns ?? x.start_ns) - x.start_ns)
              return (
                <li key={x.id}>
                  <span className="sys-span-name">{title(x.name)}</span>
                  <span className="sys-span-bar"><i style={{ marginLeft: `${Math.min(98, ((x.start_ns - r.created_ns) / total) * 100)}%`, width: `${Math.max(1, (len / total) * 100)}%` }} /></span>
                  <span className="sys-span-time">{fmtSeconds(len / 1e9)}</span>
                </li>
              )
            })}
            {r.spans.length === 0 && <li className="muted small">No stages were recorded.</li>}
          </ul>
          {Object.keys(r.attributes).length > 0 && (
            <>
              <h3 className="sys-blob-h">Attributes</h3>
              <dl className="sys-attrs">
                {Object.entries(r.attributes).map(([k, v]) => (
                  <div key={k}><dt>{title(k)}</dt><dd>{Array.isArray(v) ? v.join(', ') : String(v)}</dd></div>
                ))}
              </dl>
            </>
          )}
        </div>
      )}
    </Dialog>
  )
}
