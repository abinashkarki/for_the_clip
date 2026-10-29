import { useEffect, useMemo, useState } from 'react'
import { api } from '../api/client'
import type { ClipRange, InspectResult, SourceQuality } from '../api/types'
import { estimateLines, inspectedResolution, formatClock, formatDuration, parseClock, validateRange } from '../state/intake'
import { Icon } from '../ui/Icon'
import { Button, Dialog, Segmented, Spinner, Switch } from '../ui/kit'

/** Shown after a link is pasted: what is there, how big, how long it should take, and how much of it to take. */
export function LinkPreflight({ url, onStart, onClose }: { url: string; onStart: (opts: { quality: SourceQuality; range?: ClipRange }) => void; onClose: () => void }) {
  const [info, setInfo] = useState<InspectResult | null>(null)
  const [problem, setProblem] = useState<string | null>(null)
  const [quality, setQuality] = useState<SourceQuality>('standard')
  const [partial, setPartial] = useState(false)
  const [from, setFrom] = useState('')
  const [to, setTo] = useState('')

  useEffect(() => {
    let alive = true
    api.inspectSource(url).then(
      (r) => alive && setInfo(r),
      (e: Error) => alive && setProblem(e.message),
    )
    return () => {
      alive = false
    }
  }, [url])

  const start = parseClock(from)
  const end = parseClock(to)
  const rangeProblem = partial ? validateRange(start, end, info?.duration ?? null) : null
  const range: ClipRange | null = partial && !rangeProblem ? { start: start!, end: info?.duration != null ? Math.min(end!, info.duration) : end! } : null
  const lines = useMemo(() => (info ? estimateLines(info, quality, range) : []), [info, quality, range])
  const options: { value: SourceQuality; label: string }[] = [
    { value: 'standard', label: 'Standard · up to 1080p' },
    { value: 'high', label: 'High · up to 2160p' },
    { value: 'small', label: 'Small · up to 480p' },
  ]
  const long = info?.duration != null && info.duration >= 3600
  const go = () => onStart({ quality, ...(range ? { range } : {}) })

  return (
    <Dialog
      open
      onClose={onClose}
      title="Add this video"
      width={580}
      footer={
        <>
          <Button kind="ghost" onClick={onClose}>
            Cancel
          </Button>
          {problem ? (
            <Button kind="secondary" onClick={() => onStart({ quality: 'standard' })}>
              Add anyway
            </Button>
          ) : (
            <Button kind="primary" icon="download" onClick={go} disabled={!info || Boolean(rangeProblem)}>
              {range ? 'Download this part' : 'Download video'}
            </Button>
          )}
        </>
      }
    >
      {!info && !problem && (
        <div className="pf-loading" role="status">
          <Spinner size={16} /> Reading the link…
        </div>
      )}
      {problem && (
        <div className="pf-problem" role="alert">
          <Icon name="warn" size={18} />
          <div>
            <strong>Clippie could not read that link.</strong>
            <p className="muted small">{problem}</p>
            <p className="muted small">You can still add it, but the download may fail.</p>
          </div>
        </div>
      )}
      {info && (
        <>
          <div className="pf-card">
            <div className="pf-thumb">{info.thumbnail_url ? <img src={info.thumbnail_url} alt="" /> : <Icon name="film" size={28} />}
              {info.duration != null && <span className="pf-length">{formatClock(info.duration)}</span>}
            </div>
            <div className="pf-meta">
              <div className="pf-title">{info.title}</div>
              <div className="muted small">
                {[info.uploader, info.duration != null ? formatDuration(info.duration) : null].filter(Boolean).join(' · ')}
              </div>
              {long && <div className="pf-long small">A long video. Downloading and transcribing it takes a while, and you can keep working meanwhile.</div>}
            </div>
          </div>

          <div className="field">
            <span className="field-label">Download size</span>
            <Segmented
              label="Download quality"
              value={quality}
              onChange={setQuality}
              options={options}
            />
            <span className="muted small">{inspectedResolution(info, quality)}</span>
            <span className="muted small">Clippie downloads one source for editing and final output. Actual resolution depends on what the link offers. Small may be faster, but sources below 1080p cannot pass final HD export or Publish checks.</span>
          </div>

          <div className="field pf-range">
            <Switch checked={partial} onChange={setPartial} label="Only use part of the video" />
            {partial && (
              <div className="pf-range-inputs">
                <div className="field">
                  <label htmlFor="pf-from">From</label>
                  <input id="pf-from" className="input" value={from} onChange={(e) => setFrom(e.target.value)} placeholder="0:00" inputMode="numeric" aria-invalid={Boolean(rangeProblem && from)} />
                </div>
                <div className="field">
                  <label htmlFor="pf-to">To</label>
                  <input id="pf-to" className="input" value={to} onChange={(e) => setTo(e.target.value)} placeholder={info.duration != null ? formatClock(info.duration) : '45:00'} inputMode="numeric" aria-invalid={Boolean(rangeProblem && to)} />
                </div>
                <p className={rangeProblem && (from || to) ? 'bad small pf-range-note' : 'muted small pf-range-note'}>
                  {rangeProblem && (from || to) ? rangeProblem : range ? `${formatDuration(range.end - range.start)} of video. Times in the clips count from the start of this part.` : 'Use minutes and seconds, like 12:30.'}
                </p>
              </div>
            )}
          </div>

          {lines.length > 0 && (
            <ul className="pf-estimates">
              {lines.map((l) => (
                <li key={l}>{l}</li>
              ))}
            </ul>
          )}
        </>
      )}
    </Dialog>
  )
}
