/*
web/src/api/__tests__/config.test.ts
配置接口封装测试（Vitest）
覆盖：
- applyAccountOps 打到 /accounts/ops 且不带 revision（服务端在最新配置上重放，无需乐观锁）
- upsert 带 previous_name 时原样透传（改名靠它还原打码凭据）
- getConfigRevision 走轻量端点
- saveConfig 的 revision 可选语义
- listAccounts 的清单解析与「不含凭据」约束
- getAccountDetail 的路径转义与脱敏摘要解析
说明：mock 掉 ../http，只验证「调了哪个地址、带了什么体」，不发真实请求
*/
import { beforeEach, describe, expect, it, vi } from 'vitest'

const get = vi.fn()
const put = vi.fn()
const post = vi.fn()

vi.mock('../http', () => ({
  default: {
    get: (...args: unknown[]) => get(...args),
    put: (...args: unknown[]) => put(...args),
    post: (...args: unknown[]) => post(...args)
  }
}))

import { applyAccountOps, getAccountDetail, getConfigRevision, listAccounts, saveConfig } from '../config'
import type { Account, AppConfig } from '@/types'

const account = (name: string): Account => ({
  name,
  url: 'https://a.com',
  login_method: 'newapi_cookie',
  cookie: '***',
  github_user_session: '',
  github_client_id: '',
  github_account: '',
  user_id: null,
  proxy: null,
  checkin_path: null,
  browser_path: null,
  enabled: true
})

beforeEach(() => {
  get.mockReset()
  put.mockReset()
  post.mockReset()
})

describe('账号增量操作', () => {
  it('打到 /accounts/ops，且请求体里没有 revision', async () => {
    post.mockResolvedValue({ data: { ok: true, config: {}, updated_at: 't', revision: 7, skipped: null } })
    await applyAccountOps([{ type: 'delete', name: 'A' }])
    expect(post).toHaveBeenCalledWith('/accounts/ops', { ops: [{ type: 'delete', name: 'A' }] })
    const body = post.mock.calls[0][1] as Record<string, unknown>
    expect('revision' in body).toBe(false)
  })

  it('改名的 previous_name 原样透传', async () => {
    post.mockResolvedValue({ data: { ok: true, config: {}, updated_at: 't', revision: 8, skipped: null } })
    const payload = account('new-name')
    await applyAccountOps([{ type: 'upsert', account: payload, previous_name: 'old-name' }])
    expect(post).toHaveBeenCalledWith('/accounts/ops', {
      ops: [{ type: 'upsert', account: payload, previous_name: 'old-name' }]
    })
  })

  it('一次可以下发多条操作，顺序保持不变', async () => {
    post.mockResolvedValue({ data: { ok: true, config: {}, updated_at: 't', revision: 9, skipped: [] } })
    await applyAccountOps([
      { type: 'set_enabled', name: 'A', enabled: false },
      { type: 'set_enabled', name: 'B', enabled: false },
      { type: 'delete', name: 'C' }
    ])
    const body = post.mock.calls[0][1] as { ops: Array<{ name: string }> }
    expect(body.ops.map((o) => o.name)).toEqual(['A', 'B', 'C'])
  })

  it('解出服务端回传的最新配置与 skipped', async () => {
    post.mockResolvedValue({
      data: {
        ok: true,
        config: { accounts: [account('A')] } as unknown as AppConfig,
        updated_at: '2024-01-01T00:00:00Z',
        revision: 12,
        skipped: ['账号 "X" 已不存在，跳过删除']
      }
    })
    const res = await applyAccountOps([{ type: 'delete', name: 'X' }])
    expect(res.revision).toBe(12)
    expect(res.config.accounts).toHaveLength(1)
    expect(res.skipped).toEqual(['账号 "X" 已不存在，跳过删除'])
  })

  it('服务端 400 原样抛出，交给页面提示', async () => {
    post.mockRejectedValue({ response: { status: 400, data: { error: '账号名重复' } } })
    await expect(applyAccountOps([{ type: 'delete', name: 'A' }])).rejects.toMatchObject({
      response: { data: { error: '账号名重复' } }
    })
  })
})

describe('轻量版本号', () => {
  it('走 /config/revision 而不是整份 /config', async () => {
    get.mockResolvedValue({ data: { revision: 5 } })
    const res = await getConfigRevision()
    expect(get).toHaveBeenCalledWith('/config/revision')
    expect(res.revision).toBe(5)
  })
})

describe('整份保存的乐观锁', () => {
  it('传了 revision 就带上', async () => {
    put.mockResolvedValue({ data: { ok: true, updated_at: 't', revision: 3 } })
    await saveConfig({} as AppConfig, 2)
    expect(put).toHaveBeenCalledWith('/config', { config: {}, revision: 2 })
  })

  it('没传 revision 时请求体里不出现该字段', async () => {
    put.mockResolvedValue({ data: { ok: true, updated_at: 't', revision: 3 } })
    await saveConfig({} as AppConfig)
    const body = put.mock.calls[0][1] as Record<string, unknown>
    expect('revision' in body).toBe(false)
  })
})

describe('账号清单', () => {
  const list = {
    data: {
      accounts: [
        { name: 'Steven', url: 'https://a.com', login_method: 'tabiai',
          enabled: true, has_cookie: true, proxy: null },
        { name: '组/B 号', url: 'https://b.com', login_method: 'newapi_cookie',
          enabled: false, has_cookie: false, proxy: 'http://1.2.3.4:8080' }
      ],
      count: 2,
      updated_at: '2024-05-01T10:00:00Z',
      revision: 42
    }
  }

  it('打到 /accounts，解出清单与 revision', async () => {
    get.mockResolvedValue(list)
    const res = await listAccounts()
    expect(get).toHaveBeenCalledWith('/accounts')
    expect(res.count).toBe(2)
    expect(res.revision).toBe(42)
    expect(res.accounts.map((a) => a.name)).toEqual(['Steven', '组/B 号'])
  })

  it('没配代理的账号是 null，不是空串', async () => {
    get.mockResolvedValue(list)
    const res = await listAccounts()
    expect(res.accounts[0].proxy).toBeNull()
    expect(res.accounts[1].proxy).toBe('http://1.2.3.4:8080')
  })

  it('清单里不带任何凭据字段，连打码占位符都没有', async () => {
    get.mockResolvedValue(list)
    const res = await listAccounts()
    const serialized = JSON.stringify(res)
    expect(serialized).not.toContain('***')
    expect(res.accounts[0]).not.toHaveProperty('cookie')
    expect(res.accounts[0]).not.toHaveProperty('github_user_session')
    // 只表示「有没有配」，不给值
    expect(res.accounts[0].has_cookie).toBe(true)
    expect(res.accounts[1].has_cookie).toBe(false)
  })

  it('空配置时是空数组而不是 null', async () => {
    get.mockResolvedValue({
      data: { accounts: [], count: 0, updated_at: '', revision: 1 }
    })
    const res = await listAccounts()
    expect(res.accounts).toEqual([])
    expect(res.count).toBe(0)
  })
})

describe('单账号查询（凭据落库核实）', () => {
  const detail = {
    data: {
      account: account('A'),
      cookie_digest: { fingerprint: 'a1b2c3d4e5f6', length: 128, has_refresh: true },
      updated_at: '2024-05-01T10:00:00Z'
    }
  }

  it('打到 /accounts/{name}，取回指纹与落库时间', async () => {
    get.mockResolvedValue(detail)
    const res = await getAccountDetail('A')
    expect(get).toHaveBeenCalledWith('/accounts/A')
    expect(res.cookie_digest.fingerprint).toBe('a1b2c3d4e5f6')
    expect(res.updated_at).toBe('2024-05-01T10:00:00Z')
  })

  it('账号名转义后再拼路径，斜杠不会串到别的端点', async () => {
    get.mockResolvedValue(detail)
    await getAccountDetail('组/A 号')
    expect(get).toHaveBeenCalledWith('/accounts/%E7%BB%84%2FA%20%E5%8F%B7')
  })

  it('取的是脱敏端点：cookie 只会是打码值', async () => {
    get.mockResolvedValue(detail)
    const res = await getAccountDetail('A')
    expect(res.account.cookie).toBe('***')
  })

  it('库里凭据为空时摘要是空值形态', async () => {
    get.mockResolvedValue({
      data: {
        account: { ...account('A'), cookie: '' },
        cookie_digest: { fingerprint: '', length: 0, has_refresh: false }
      }
    })
    const res = await getAccountDetail('A')
    expect(res.cookie_digest.length).toBe(0)
    expect(res.cookie_digest.has_refresh).toBe(false)
    expect(res.updated_at).toBeUndefined()
  })

  it('账号不存在时 404 原样抛出', async () => {
    get.mockRejectedValue({ response: { status: 404, data: { error: '账号不存在: X' } } })
    await expect(getAccountDetail('X')).rejects.toMatchObject({
      response: { status: 404 }
    })
  })
})
