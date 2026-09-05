/*
web/src/api/__tests__/siteApiKeys.test.ts
站点 API Key 接口封装测试（Vitest）
覆盖：
- ensureSiteAPIKeys 打到 /sites/apikeys，only 原样透传，超时放宽到 10 分钟
- listSiteAPIKeys 走 GET /sites/apikeys（脱敏视图）
- 前端不封装 /sites/apikeys/raw —— 那是明文，只认 API Key，不该经过浏览器
说明：mock 掉 ../http，只验证「调了哪个地址、带了什么体/配置」，不发真实请求
*/
import { beforeEach, describe, expect, it, vi } from 'vitest'

const post = vi.fn()
const get = vi.fn()

vi.mock('../http', () => ({
  default: {
    post: (...args: unknown[]) => post(...args),
    get: (...args: unknown[]) => get(...args)
  }
}))

import { ensureSiteAPIKeys, listSiteAPIKeys } from '../githubAccounts'

beforeEach(() => {
  post.mockReset()
  get.mockReset()
})

describe('备号取 key', () => {
  it('打到 /sites/apikeys 并放宽超时（服务端串行执行，账号多时要几分钟）', async () => {
    post.mockResolvedValue({ data: { ok: true, total: 2, got: 2, results: [] } })
    await ensureSiteAPIKeys()
    const [url, body, cfg] = post.mock.calls[0] as [string, unknown, { timeout: number }]
    expect(url).toBe('/sites/apikeys')
    expect(body).toEqual({ only: undefined })
    // 默认 30s 会在账号稍多时直接超时，而服务端那边其实还在跑
    expect(cfg.timeout).toBeGreaterThanOrEqual(600_000)
  })

  it('only 原样透传，服务端据此只处理指定账号', async () => {
    post.mockResolvedValue({ data: { ok: true, total: 1, got: 1, results: [] } })
    await ensureSiteAPIKeys(['A（a.com）'])
    const [, body] = post.mock.calls[0] as [string, { only: string[] }]
    expect(body.only).toEqual(['A（a.com）'])
  })

  it('返回体原样解出 got/total/results', async () => {
    post.mockResolvedValue({
      data: {
        ok: true,
        total: 2,
        got: 1,
        results: [
          { account: 'A', status: 'created' },
          { account: 'B', status: 'failed', message: '登录站点失败' }
        ]
      }
    })
    const r = await ensureSiteAPIKeys()
    expect(r.got).toBe(1)
    expect(r.total).toBe(2)
    expect(r.results[1].message).toContain('登录站点失败')
  })
})

describe('汇总清单', () => {
  it('走 GET /sites/apikeys（脱敏视图）', async () => {
    get.mockResolvedValue({ data: { total: 1, items: [] } })
    await listSiteAPIKeys()
    expect(get.mock.calls[0][0]).toBe('/sites/apikeys')
  })

  it('用 has_key 判断有没有 key，不去解析被脱敏的 api_key', async () => {
    get.mockResolvedValue({
      data: {
        total: 2,
        items: [
          { account: 'A', url: 'https://a.com', api_key: 'sk-abc**********wxyz', has_key: true },
          { account: 'B', url: 'https://b.com', api_key: '', has_key: false }
        ]
      }
    })
    const r = await listSiteAPIKeys()
    expect(r.items.filter((i) => i.has_key)).toHaveLength(1)
    // 脱敏值必须保持不可用状态：谁要是把它当 key 用，是配置错误而不是这里的锅
    expect(r.items[0].api_key).toContain('*')
  })
})

describe('明文端点不给浏览器', () => {
  it('封装里不存在 raw 版本', async () => {
    const mod = await import('../githubAccounts')
    const names = Object.keys(mod)
    // /sites/apikeys/raw 只认 API Key，前端封装它没有意义且扩大暴露面
    expect(names.some((n) => /raw/i.test(n))).toBe(false)
  })

  it('两个封装都没打到 /raw 路径', async () => {
    post.mockResolvedValue({ data: { ok: true, total: 0, got: 0, results: [] } })
    get.mockResolvedValue({ data: { total: 0, items: [] } })
    await ensureSiteAPIKeys()
    await listSiteAPIKeys()
    const urls = [...post.mock.calls, ...get.mock.calls].map((c) => String(c[0]))
    expect(urls.some((u) => u.includes('/raw'))).toBe(false)
  })
})
