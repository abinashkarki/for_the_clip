import type { SVGProps } from 'react'

const P: Record<string, string> = {
  play: 'M7 4.5v15l12-7.5z',
  pause: 'M7 4.5h3.6v15H7zM13.4 4.5H17v15h-3.6z',
  undo: 'M9 7 4 12l5 5M4.5 12H15a5 5 0 0 1 0 10h-3',
  redo: 'm15 7 5 5-5 5M19.5 12H9a5 5 0 0 0 0 10h3',
  scissors: 'M6 9a3 3 0 1 0 0-6 3 3 0 0 0 0 6zm0 12a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM8.1 7.9 20 19M8.1 16.1 20 5',
  plus: 'M12 5v14M5 12h14',
  minus: 'M5 12h14',
  trash: 'M4 7h16M10 11v6M14 11v6M6 7l1 12a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-12M9 7V4h6v3',
  upload: 'M12 16V4m0 0L7 9m5-5 5 5M4 16v3a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-3',
  link: 'M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1M14 10a4 4 0 0 0-5.7 0l-3 3A4 4 0 0 0 11 18.7l1-1',
  check: 'm5 12.5 4.5 4.5L19 7.5',
  x: 'M6 6l12 12M18 6 6 18',
  back: 'M15 5l-7 7 7 7',
  chevron: 'm9 6 6 6-6 6',
  download: 'M12 4v12m0 0 5-5m-5 5-5-5M4 20h16',
  wand: 'M5 19 16 8m-2-3 1-2 1 2 2 1-2 1-1 2-1-2-2-1zm5 6 .7-1.3.7 1.3 1.3.7-1.3.7-.7 1.3-.7-1.3-1.3-.7z',
  text: 'M5 6V4h14v2M12 4v16M9 20h6',
  frame: 'M8 3h8a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2z',
  hook: 'M4 6h16v8H12l-4 4v-4H4z',
  brand: 'M12 3l2.5 5.5 6 .7-4.4 4.1 1.2 5.9L12 16.3 6.7 19.2l1.2-5.9L3.5 9.2l6-.7z',
  audio: 'M4 10v4M8 6v12M12 3v18M16 8v8M20 11v2',
  search: 'M11 18a7 7 0 1 0 0-14 7 7 0 0 0 0 14zm9 3-4.5-4.5',
  settings: 'M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM19 12a7 7 0 0 0-.1-1.2l2-1.5-2-3.4-2.3.9a7 7 0 0 0-2-1.2L14.2 3h-4l-.4 2.6a7 7 0 0 0-2 1.2l-2.3-.9-2 3.4 2 1.5A7 7 0 0 0 5 12c0 .4 0 .8.1 1.2l-2 1.5 2 3.4 2.3-.9a7 7 0 0 0 2 1.2l.4 2.6h4l.4-2.6a7 7 0 0 0 2-1.2l2.3.9 2-3.4-2-1.5c.1-.4.1-.8.1-1.2z',
  restore: 'M4 12a8 8 0 1 0 2.4-5.7L4 8.5M4 4v4.5h4.5',
  grid: 'M4 4h7v7H4zM13 4h7v7h-7zM4 13h7v7H4zM13 13h7v7h-7z',
  sort: 'M7 4v16m0 0-3-3m3 3 3-3M17 20V4m0 0-3 3m3-3 3 3',
  film: 'M4 4h16v16H4zM4 9h16M4 15h16M9 4v16M15 4v16',
  emphasis: 'M5 18 12 4l7 14M8 13h8',
  eye: 'M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12zm10 3a3 3 0 1 0 0-6 3 3 0 0 0 0 6z',
  eyeoff: 'M3 3l18 18M10.6 6.2A9 9 0 0 1 12 6c6 0 10 6 10 6a17 17 0 0 1-3.2 3.8M6.2 7.5A17 17 0 0 0 2 12s4 6 10 6a9 9 0 0 0 3.8-.8',
  split: 'M12 3v18M7 8 3 12l4 4M17 8l4 4-4 4',
  volume: 'M4 9v6h4l5 4V5L8 9zm12.5 0a4.5 4.5 0 0 1 0 6',
  mute: 'M4 9v6h4l5 4V5L8 9zm12 1 5 4m0-4-5 4',
  warn: 'M12 4 2.5 20h19zM12 10v4.5M12 17.5v.1',
  external: 'M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5',
  pin: 'M12 3v8m0 0-4 3v2h8v-2zm0 5v5',
  activity: 'M3 12h4l3-8 4 16 3-8h4',
}

export type IconName = keyof typeof P

export function Icon({ name, size = 18, ...rest }: { name: IconName; size?: number } & SVGProps<SVGSVGElement>) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill={name === 'play' || name === 'pause' ? 'currentColor' : 'none'}
      stroke="currentColor"
      strokeWidth={name === 'play' || name === 'pause' ? 0 : 1.8}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      {...rest}
    >
      <path d={P[name]} />
    </svg>
  )
}

/** Layout glyphs drawn as miniature 9:16 frames. */
export function LayoutGlyph({ kind, size = 44 }: { kind: string; size?: number }) {
  const w = size * 0.56
  const h = size
  const common = { rx: 3, fill: 'currentColor' }
  return (
    <svg width={w} height={h} viewBox="0 0 28 50" aria-hidden="true">
      <rect x="1" y="1" width="26" height="48" rx="4" fill="none" stroke="currentColor" strokeWidth="1.6" opacity=".55" />
      {kind === 'fill' && <rect x="4" y="4" width="20" height="42" {...common} opacity=".9" />}
      {kind === 'fit' && <rect x="4" y="17" width="20" height="16" {...common} opacity=".9" />}
      {kind === 'stack' && (
        <>
          <rect x="4" y="4" width="20" height="19" {...common} opacity=".9" />
          <rect x="4" y="27" width="20" height="19" {...common} opacity=".6" />
        </>
      )}
      {kind === 'screen_speaker' && (
        <>
          <rect x="4" y="4" width="20" height="25" {...common} opacity=".9" />
          <rect x="4" y="32" width="20" height="14" {...common} opacity=".6" />
        </>
      )}
    </svg>
  )
}

export function Logo({ size = 22 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 32 32" aria-hidden="true">
      <rect x="10.5" y="5.5" width="11" height="21" rx="3.5" fill="none" stroke="#ff5a4e" strokeWidth="2.6" />
      <circle cx="16" cy="16" r="2.2" fill="#ff5a4e" />
    </svg>
  )
}
