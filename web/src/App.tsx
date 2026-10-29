import { lazy, Suspense, useEffect, useState } from 'react'
import { isMock, onModeChange } from './api/client'
import { configureFonts } from './render/fonts'
const AnalyticsScreen = lazy(() => import('./screens/Analytics').then(module => ({ default: module.AnalyticsScreen })))
const BrandsScreen = lazy(() => import('./screens/Brands').then(module => ({ default: module.BrandsScreen })))
const ChannelsScreen = lazy(() => import('./screens/Channels').then(module => ({ default: module.ChannelsScreen })))
const EditorScreen = lazy(() => import('./screens/Editor').then(module => ({ default: module.EditorScreen })))
import { Library } from './screens/Library'
const ModelsScreen = lazy(() => import('./screens/Models').then(module => ({ default: module.ModelsScreen })))
const ProjectScreen = lazy(() => import('./screens/Project').then(module => ({ default: module.ProjectScreen })))
const SettingsScreen = lazy(() => import('./screens/Settings').then(module => ({ default: module.SettingsScreen })))
import { href, useRoute } from './state/route'
import { setupApi } from './api/setup'
import { ActivityButton } from './ui/Activity'
import { Logo } from './ui/Icon'
import { Toasts, cx } from './ui/kit'
import { RecoveryNotice } from './ui/RecoveryNotice'

export function App() {
  const route = useRoute()
  const [mock, setMock] = useState(isMock())
  const [setupNeeded, setSetupNeeded] = useState(false)
  useEffect(() => {
    if (mock || route.name !== 'home') return
    const controller = new AbortController()
    void setupApi.models(controller.signal).then(result => {
      if (!controller.signal.aborted) setSetupNeeded(result.desktop && result.models.some(model => model.required && !model.ready))
    }).catch(() => { /* Settings remains available if setup status cannot load. */ })
    return () => controller.abort()
  }, [mock, route.name])
  useEffect(() => {
    const off = onModeChange(() => setMock(isMock()))
    return () => {
      off()
    }
  }, [])
  useEffect(() => configureFonts({ mock }), [mock])
  configureFonts({ mock })

  const inEditor = route.name === 'clip'
  return (
    <>
      <a className="skip" href="#main">Skip to content</a>
      {!inEditor && (
        <header className="topbar">
          <a className="brand" href={href.home()}>
            <Logo size={24} />
            <span>Clippie</span>
          </a>
          <nav aria-label="Main">
            <a className={cx(route.name === 'home' || route.name === 'project' ? 'is-on' : '')} href={href.home()}>Library</a>
            <a className={cx(route.name === 'brands' && 'is-on')} href={href.brands()}>Brand kits</a>
            <a className={cx(route.name === 'models' && 'is-on')} href={href.models()}>Models</a>
            <a className={cx((route.name === 'analytics' || route.name === 'channels') && 'is-on')} href={href.analytics()}>Publishing</a>
          </nav>
          <div className="topbar-end">
            {mock && <span className="chip chip-demo" title="You are looking at generated sample data">Demo data</span>}
            <ActivityButton />
            <a className={cx('link-quiet', route.name === 'settings' && 'is-on')} href={href.settings()} title="API keys, model routing and local models">
              Settings
            </a>
          </div>
        </header>
      )}
      <div id="main" className={cx('view', inEditor && 'view-editor')}>
        {!mock && <RecoveryNotice />}
        {route.name === 'home' && setupNeeded && <p className="setup-library-notice">Local models need setup for features that use them. <a href={href.settings()}>Set up models</a></p>}
        <Suspense fallback={<p className="muted" role="status">Loading…</p>}>
        {route.name === 'home' && <Library />}
        {route.name === 'project' && <ProjectScreen id={route.id} key={route.id} />}
        {route.name === 'clip' && <EditorScreen id={route.id} key={route.id} />}
        {route.name === 'brands' && <BrandsScreen />}
        {route.name === 'models' && <ModelsScreen />}
        {route.name === 'settings' && <SettingsScreen />}
        {route.name === 'channels' && <ChannelsScreen />}
        {route.name === 'analytics' && <AnalyticsScreen id={route.id} key={route.id ?? 'all'} />}
        </Suspense>
      </div>
      <Toasts />
    </>
  )
}
