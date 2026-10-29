import { describe, expect, it } from 'vitest'
import { externallyManaged, fmtCost, fmtMs, fmtTokens, originLabel, routeNeedsKey, routingChanged, totals, type ModelGroup } from './system'

const group = (over: Partial<ModelGroup>): ModelGroup => ({
  provider: 'gemini', model: 'm', stage: 'plan', calls: 1, failures: 0, p50_latency_ms: 100, p95_latency_ms: 200,
  input_tokens: 10, output_tokens: 5, thinking_tokens: 0, cached_tokens: 0, cost_usd: 0.5, cost_unknown_calls: 0, ...over,
})

describe('cost formatting', () => {
  it('never shows an unknown cost as zero', () => {
    expect(fmtCost(null)).toBe('unknown')
    expect(fmtCost(0)).toBe('$0.00')
    expect(fmtCost(0.0042)).toBe('$0.0042')
    expect(fmtCost(1.234, 3)).toBe('$1.23 + 3 unknown')
  })
  it('totals keep unknown cost separate', () => {
    const t = totals([group({}), group({ cost_usd: null, cost_unknown_calls: 2, calls: 2 })])
    expect(t.cost).toBe(0.5)
    expect(t.unknownCalls).toBe(2)
    expect(totals([group({ cost_usd: null, cost_unknown_calls: 1 })]).cost).toBeNull()
  })
})

describe('units', () => {
  it('formats latency and tokens', () => {
    expect(fmtMs(850)).toBe('850 ms')
    expect(fmtMs(2345)).toBe('2.35 s')
    expect(fmtMs(null)).toBe('—')
    expect(fmtTokens(950)).toBe('950')
    expect(fmtTokens(12_400)).toBe('12k')
    expect(fmtTokens(2_500_000)).toBe('2.50M')
  })
})

describe('keys and routing', () => {
  it('labels where a key comes from', () => {
    expect(originLabel('none', false)).toBe('Not set')
    expect(originLabel('keychain', true)).toBe('Saved in Keychain')
    expect(originLabel('environment', true)).toBe('Set by environment variable')
    expect(externallyManaged('env_file')).toBe(true)
    expect(externallyManaged('keychain')).toBe(false)
  })
  it('knows which routes need a key', () => {
    expect(routeNeedsKey('anthropic')).toBe('anthropic')
    expect(routeNeedsKey('local_qwen')).toBeNull()
  })
  it('detects routing edits and ignores fallbacks', () => {
    const a = { defaults: { plan: { provider: 'gemini', model: 'x' } }, fallbacks: {} }
    expect(routingChanged(a, { ...a, fallbacks: { plan: [] } })).toBe(false)
    expect(routingChanged(a, { defaults: { plan: { provider: 'openai', model: 'x' } }, fallbacks: {} })).toBe(true)
  })
})
