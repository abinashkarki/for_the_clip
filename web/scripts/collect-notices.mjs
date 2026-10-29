#!/usr/bin/env node
/** Assemble local production npm notices; native/runtime compliance is separate. */
import { createHash } from 'node:crypto'
import { readFile, readdir, mkdir, writeFile } from 'node:fs/promises'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const output = path.resolve(root, '../clippie/static/studio')
const verifyAssets = process.argv.includes('--verify-assets')
if (process.argv.slice(2).some(arg => arg !== '--verify-assets')) throw new Error('Unknown notice collector argument')
const hash = bytes => createHash('sha256').update(bytes).digest('hex')
const read = file => readFile(path.join(root, file))
const lockBytes = await read('package-lock.json')
const lock = JSON.parse(lockBytes)
if (!lock.packages || lock.lockfileVersion < 2) throw new Error('Expected npm lockfile with package records')
const packages = []
const sections = [
  'Clippie frontend third-party notices',
  'Includes production npm dependency metadata and full local notice files. Some packages may be build-time/type-only or unused after tree shaking.',
  'This frontend inventory does not cover Python, FFmpeg, native libraries, model weights or every application resource. Redistribution review remains separate.',
]
for (const [location, entry] of Object.entries(lock.packages).sort(([a], [b]) => a.localeCompare(b))) {
  if (!location || entry.dev) continue
  if (entry.link || !location.startsWith('node_modules/') || location.includes('..')) {
    throw new Error(`Unsupported production package location: ${location}`)
  }
  const expectedName = location.slice(location.lastIndexOf('node_modules/') + 'node_modules/'.length)
  const metadataBytes = await read(`${location}/package.json`)
  const metadata = JSON.parse(metadataBytes)
  if (metadata.name !== expectedName || metadata.version !== entry.version) {
    throw new Error(`Installed package differs from lock: ${location} (${metadata.name}@${metadata.version}, expected ${expectedName}@${entry.version})`)
  }
  const noticeNames = (await readdir(path.join(root, location))).filter(name => /^(licen[sc]e|copying|notice|copyright)([._-]|$)/i.test(name)).sort()
  if (!noticeNames.length) throw new Error(`No local notice file for ${expectedName}@${entry.version}; review before building`)
  const notices = []
  for (const filename of noticeNames) {
    const bytes = await read(`${location}/${filename}`)
    if (!bytes.length) throw new Error(`Empty notice: ${location}/${filename}`)
    notices.push({ path: `${location}/${filename}`, sha256: hash(bytes), bytes: bytes.length })
    sections.push(`\n${'='.repeat(78)}\n${metadata.name}@${metadata.version} — ${filename}\nSource: ${location}/${filename}\nSHA256: ${hash(bytes)}\n${'='.repeat(78)}\n${bytes.toString('utf8')}`)
  }
  packages.push({ name: metadata.name, version: metadata.version, location,
    installed_package_json_sha256: hash(metadataBytes), license: metadata.license ?? entry.license ?? null,
    repository: metadata.repository ?? null, homepage: metadata.homepage ?? null,
    resolved: entry.resolved ?? null, integrity: entry.integrity ?? null, notices })
}

const fontPackage = packages.find(pkg => pkg.name === '@fontsource-variable/hanken-grotesk')
if (!fontPackage) throw new Error('Hanken Grotesk must be present in the production lock inventory')
const cssPath = `${fontPackage.location}/index.css`
const cssBytes = await read(cssPath)
const fontPaths = [...cssBytes.toString('utf8').matchAll(/url\(\.\/([^)]*\.woff2)\)/g)].map(match => match[1])
if (!fontPaths.length) throw new Error('No Hanken font sources found in the installed entry CSS')
const fontSources = await Promise.all([...new Set(fontPaths)].map(async source => {
  const sourcePath = `${fontPackage.location}/${source}`
  const bytes = await read(sourcePath)
  return { path: sourcePath, sha256: hash(bytes), bytes: bytes.length }
}))
const artifactNames = ['third-party-notices.txt', 'third-party-notices.provenance.json']
const text = `${sections.join('\n')}\n`
const provenance = {
  schema_version: 1,
  scope: 'Production npm lock entries and installed local root notice files; frontend only. Entries are conservative and may include types or tree-shaken dependencies. Native/runtime and other resource compliance is not established.',
  redistribution_review_complete: false,
  package_lock_sha256: hash(lockBytes),
  packages,
  notice_text_sha256: hash(Buffer.from(text)),
  hanken_grotesk: {
    package: fontPackage.name, version: fontPackage.version,
    entry_css: cssPath, entry_css_sha256: hash(cssBytes),
    license_files: fontPackage.notices, source_fonts: fontSources,
    emitted_assets_verified: false, emitted_assets: [],
  },
}
if (verifyAssets) {
  const assetsDir = path.join(output, 'assets')
  const assets = (await readdir(assetsDir)).sort()
  const emitted = []
  for (const filename of assets) {
    if (filename.startsWith('hanken-grotesk-') && filename.endsWith('.woff2')) {
      const bytes = await readFile(path.join(assetsDir, filename))
      const digest = hash(bytes)
      const source = fontSources.find(font => font.sha256 === digest)
      if (!source) throw new Error(`Emitted Hanken asset has no matching installed source: ${filename}`)
      emitted.push({ path: `assets/${filename}`, sha256: digest, source_path: source.path, delivery: 'file' })
    }
    if (filename.endsWith('.css')) {
      const css = await readFile(path.join(assetsDir, filename), 'utf8')
      for (const match of css.matchAll(/url\(data:font\/woff2;base64,([A-Za-z0-9+/=]+)\)/g)) {
        const digest = hash(Buffer.from(match[1], 'base64'))
        const source = fontSources.find(font => font.sha256 === digest)
        if (source) emitted.push({ path: `assets/${filename}`, sha256: digest, source_path: source.path, delivery: 'inline-base64-woff2' })
      }
    }
  }
  for (const source of fontSources) {
    if (!emitted.some(asset => asset.sha256 === source.sha256)) throw new Error(`Hanken source font missing from emitted assets: ${source.path}`)
  }
  const builtNotices = await readFile(path.join(output, artifactNames[0]))
  if (hash(builtNotices) !== provenance.notice_text_sha256) throw new Error('Built notices differ from collected local notice text')
  provenance.hanken_grotesk.emitted_assets_verified = true
  provenance.hanken_grotesk.emitted_assets = emitted
}
const json = `${JSON.stringify(provenance, null, 2)}\n`
await mkdir(path.join(root, 'public'), { recursive: true })
await writeFile(path.join(root, 'public', artifactNames[0]), text)
await writeFile(path.join(root, 'public', artifactNames[1]), json)
if (verifyAssets) await writeFile(path.join(output, artifactNames[1]), json)
console.log(JSON.stringify({ packages: packages.length, notice_files: packages.reduce((n, pkg) => n + pkg.notices.length, 0),
  hanken_version: fontPackage.version, hanken_source_fonts: fontSources.length,
  emitted_assets_verified: provenance.hanken_grotesk.emitted_assets_verified }))
