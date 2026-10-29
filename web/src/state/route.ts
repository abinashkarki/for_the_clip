import { useEffect, useState } from 'react'

export type Route =
  | { name: 'home' }
  | { name: 'project'; id: string }
  | { name: 'clip'; id: string }
  | { name: 'brands' }
  | { name: 'settings' }
  | { name: 'models' }
  | { name: 'channels' }
  | { name: 'analytics'; id?: string }

export function parseHash(hash: string): Route {
  const path = hash.replace(/^#/, '').split('?')[0]
  const parts = path.split('/').filter(Boolean)
  if (parts[0] === 'p' && parts[1]) return { name: 'project', id: decodeURIComponent(parts[1]) }
  if (parts[0] === 'c' && parts[1]) return { name: 'clip', id: decodeURIComponent(parts[1]) }
  if (parts[0] === 'brands') return { name: 'brands' }
  if (parts[0] === 'settings') return { name: 'settings' }
  if (parts[0] === 'models') return { name: 'models' }
  if (parts[0] === 'channels') return { name: 'channels' }
  if (parts[0] === 'analytics') return { name: 'analytics', id: parts[1] ? decodeURIComponent(parts[1]) : undefined }
  return { name: 'home' }
}

export const href = {
  home: () => '#/',
  project: (id: string) => `#/p/${encodeURIComponent(id)}`,
  clip: (id: string) => `#/c/${encodeURIComponent(id)}`,
  brands: () => '#/brands',
  settings: () => '#/settings',
  models: () => '#/models',
  channels: () => '#/channels',
  analytics: (id?: string) => (id ? `#/analytics/${encodeURIComponent(id)}` : '#/analytics'),
}

export function useRoute(): Route {
  const [route, setRoute] = useState<Route>(() => parseHash(location.hash))
  useEffect(() => {
    const on = () => setRoute(parseHash(location.hash))
    window.addEventListener('hashchange', on)
    return () => window.removeEventListener('hashchange', on)
  }, [])
  return route
}

export function navigate(to: string) {
  location.hash = to
}

/** Remember where the editor was opened from so Back returns there. */
let lastProject: string | null = null
export const setLastProject = (id: string | null) => (lastProject = id)
export const getLastProject = () => lastProject
