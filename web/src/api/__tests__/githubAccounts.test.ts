/*
web/src/api/__tests__/githubAccounts.test.ts
GitHub 凭据池接口封装测试（Vitest）
覆盖：
- applyGitHubAccountOps 打到 /github-accounts/ops、不带 revision（服务端持锁重放）
- 改名 op 原样透传 previous_name，user_session 允许只传 "***"
- 删除被引用条目时 400 原样抛出（页面要把「有几个在用」展示给用户）
- skipped 与回传配置的解析
- checkGitHubAccount 单独放大超时（默认 30s 撑不住几十秒的 GitHub 探测）
- 三态响应解析，含可选的 authorized_client_id
说明：mock 掉 ../http，只验证「调了哪个地址、带了什么体/配置」，不发真实请求
*/
import { beforeEach, describe, expect, it, vi } from 'vitest'

const post = vi.fn()

vi.mock('../http', () => ({
  default: {
    post: (...args: unknown[]) => post(...args)
  }
}))

import {
  GITHUB_CHECK_TIMEOUT_MS, applyGitHubAccountOps, checkGitHubAccount
} from '../githubAccounts'
import type { AppConfig, GitHubAccount } from '@/types'

const pooled = (name: string, session = '***'): GitHubAccount => ({
  name,
  user_session: session,
  client_id: ''
})

beforeEach(() => {
  post.mockReset()
})

describe('池子增删改', () => {
  it('打到 /github-accounts/ops，请求体里没有 revision', async () => {
    post.mockResolvedValue({ data: { ok: true, config: {}, updated_at: 't', revision: 3, skipped: null } })
    await applyGitHubAccountOps([{ type: 'upsert', account: pooled('Steven', 'raw-session') }])
    expect(post).toHaveBeenCalledWith('/github-accounts/ops', {
      ops: [{ type: 'upsert', account: pooled('Steven', 'raw-session') }]
    })
    const body = post.mock.calls[0][1] as Record<string, unknown>
    expect('revision' in body).toBe(false)
  })

  it('改名带 previous_name，user_session 可以只传占位符', async () => {
    post.mockResolvedValue({ data: { ok: true, config: {}, updated_at: 't', revision: 4, skipped: null } })
    await applyGitHubAccountOps([
      { type: 'upsert', account: pooled('新名'), previous_name: '旧名' }
    ])
    expect(post).toHaveBeenCalledWith('/github-accounts/ops', {
      ops: [{ type: 'upsert', account: pooled('新名'), previous_name: '旧名' }]
    })
  })

  it('一次可以下发多条，顺序保持不变', async () => {
    post.mockResolvedValue({ data: { ok: true, config: {}, updated_at: 't', revision: 5, skipped: [] } })
    await applyGitHubAccountOps([
      { type: 'upsert', account: pooled('A', 's') },
      { type: 'delete', name: 'B' }
    ])
    const body = post.mock.calls[0][1] as { ops: Array<{ type: string }> }
    expect(body.ops.map((o) => o.type)).toEqual(['upsert', 'delete'])
  })

  it('解出回传的最新配置与 skipped', async () => {
    post.mockResolvedValue({
      data: {
        ok: true,
        config: { github_accounts: [pooled('Steven')] } as unknown as AppConfig,
        updated_at: '2024-05-01T10:00:00Z',
        revision: 9,
        skipped: ['delete "X"：已不存在']
      }
    })
    const res = await applyGitHubAccountOps([{ type: 'delete', name: 'X' }])
    expect(res.revision).toBe(9)
    expect(res.config.github_accounts).toHaveLength(1)
    expect(res.skipped).toEqual(['delete "X"：已不存在'])
  })

  it('还有账号在引用时 400 原样抛出，提示交给页面展示', async () => {
    post.mockRejectedValue({
      response: { status: 400, data: { error: '还有 2 个站点账号在用 GitHub 账号 "Steven"，请先改掉它们的引用再删除' } }
    })
    await expect(applyGitHubAccountOps([{ type: 'delete', name: 'Steven' }])).rejects.toMatchObject({
      response: { data: { error: expect.stringContaining('还有 2 个站点账号') } }
    })
  })
})

describe('user_session 可用性探测', () => {
  const okResp = {
    data: {
      ok: true,
      name: 'Steven',
      site: 'https://tabiai.cc',
      result: { status: 'ok', message: 'GitHub 返回授权 code，user_session 有效', authorized_client_id: 'Iv1.abc' }
    }
  }

  it('打到 /github-accounts/check，并单独放大超时', async () => {
    post.mockResolvedValue(okResp)
    await checkGitHubAccount('Steven')
    expect(post).toHaveBeenCalledWith(
      '/github-accounts/check',
      { name: 'Steven' },
      { timeout: GITHUB_CHECK_TIMEOUT_MS }
    )
    // 探测要几十秒，超时必须比 http 实例默认的 30s 大，否则前端先超时、服务端还在跑
    expect(GITHUB_CHECK_TIMEOUT_MS).toBeGreaterThan(30_000)
  })

  it('解出 ok 三态与回显的 client_id', async () => {
    post.mockResolvedValue(okResp)
    const res = await checkGitHubAccount('Steven')
    expect(res.result.status).toBe('ok')
    expect(res.site).toBe('https://tabiai.cc')
    expect(res.result.authorized_client_id).toBe('Iv1.abc')
  })

  it('expired / unknown 都是 200 响应，靠 status 区分而不是靠报错', async () => {
    for (const status of ['expired', 'unknown'] as const) {
      post.mockResolvedValue({
        data: { ok: true, name: 'Steven', site: 'https://tabiai.cc', result: { status, message: '说明' } }
      })
      const res = await checkGitHubAccount('Steven')
      expect(res.result.status).toBe(status)
      // 服务端只在有值时给这个字段，缺省是 undefined 而不是空串
      expect(res.result.authorized_client_id).toBeUndefined()
    }
  })

  it('没被任何账号引用时 400 原样抛出', async () => {
    post.mockRejectedValue({
      response: { status: 400, data: { error: 'GitHub 账号 Steven 还没有被任何站点账号引用，无法确定探测哪个站点' } }
    })
    await expect(checkGitHubAccount('Steven')).rejects.toMatchObject({
      response: { data: { error: expect.stringContaining('无法确定探测哪个站点') } }
    })
  })
})
