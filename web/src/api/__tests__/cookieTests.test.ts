/*
web/src/api/__tests__/cookieTests.test.ts
cookieTests.ts 接口封装的单元测试（Vitest）
覆盖：两个启动接口走各自路径、status/stop 的取值、TaBiAI 凭据签发的请求体与错误透传、
      凭据失效名单的解析与「不含凭据」约束
说明：mock 掉 ../http，只验证「调了哪个地址、带了什么 body、怎么解包 data」，不发真实请求
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

import {
  getCookieTestStatus,
  issueTabiAICookie,
  listExpiredTabiAI,
  startNewAPICookieTest,
  startTabiAICookieTest,
  stopCookieTest
} from '../cookieTests'

beforeEach(() => {
  post.mockReset()
  get.mockReset()
})

describe('启动检测', () => {
  it('站点 Cookie 走 /cookie-tests/newapi', async () => {
    post.mockResolvedValue({ data: { ok: true, mode: 'newapi_cookie', started: true } })
    const result = await startNewAPICookieTest(['A', 'B'])
    expect(post).toHaveBeenCalledWith('/cookie-tests/newapi', { account_names: ['A', 'B'] })
    expect(result.mode).toBe('newapi_cookie')
  })

  it('TaBiAI 走 /cookie-tests/tabiai', async () => {
    post.mockResolvedValue({ data: { ok: true, mode: 'tabiai', started: true } })
    const result = await startTabiAICookieTest(['TaBiAI'])
    expect(post).toHaveBeenCalledWith('/cookie-tests/tabiai', { account_names: ['TaBiAI'] })
    expect(result.mode).toBe('tabiai')
  })

  it('不传账号名时提交空数组，表示检测该类型全部启用账号', async () => {
    post.mockResolvedValue({ data: { ok: true, mode: 'tabiai', started: true } })
    await startTabiAICookieTest()
    expect(post).toHaveBeenCalledWith('/cookie-tests/tabiai', { account_names: [] })
  })

  it('409 冲突原样抛出，交给调用方提示「已有任务在进行中」', async () => {
    post.mockRejectedValue({ response: { status: 409, data: { error: '已有 Cookie 检测任务在进行中' } } })
    await expect(startTabiAICookieTest()).rejects.toMatchObject({
      response: { status: 409 }
    })
  })
})

describe('状态与停止', () => {
  it('status 解包 data', async () => {
    get.mockResolvedValue({ data: { running: true, mode: 'tabiai', round: 2, results: [] } })
    const status = await getCookieTestStatus()
    expect(get).toHaveBeenCalledWith('/cookie-tests/status')
    expect(status.mode).toBe('tabiai')
  })

  it('stop 是幂等的，无任务时同样返回 ok', async () => {
    post.mockResolvedValue({ data: { ok: true } })
    await expect(stopCookieTest()).resolves.toEqual({ ok: true })
    expect(post).toHaveBeenCalledWith('/cookie-tests/stop')
  })
})

describe('签发 TaBiAI 凭据', () => {
  it('按 account_name 提交并回显账号名', async () => {
    post.mockResolvedValue({ data: { ok: true, account_name: 'TaBiAI' } })
    const result = await issueTabiAICookie('TaBiAI')
    expect(post).toHaveBeenCalledWith('/tabiai/issue-cookie', { account_name: 'TaBiAI' })
    expect(result).toEqual({ ok: true, account_name: 'TaBiAI' })
  })

  it('缺少 user_session 时把服务端的 400 说明透传给调用方', async () => {
    const message =
      '该账号未填写 GitHub user_session，无法自动签发；请填写后重试，或直接从浏览器复制 new_api_refresh'
    post.mockRejectedValue({ response: { status: 400, data: { error: message } } })
    await expect(issueTabiAICookie('TaBiAI')).rejects.toMatchObject({
      response: { data: { error: message } }
    })
  })

  it('账号不存在时抛出 404', async () => {
    post.mockRejectedValue({ response: { status: 404, data: { error: '账号不存在: X' } } })
    await expect(issueTabiAICookie('X')).rejects.toMatchObject({ response: { status: 404 } })
  })

  it('账号名含空格等字符时原样放进 body，不做拼接', async () => {
    post.mockResolvedValue({ data: { ok: true, account_name: '我的 站点/A' } })
    await issueTabiAICookie('我的 站点/A')
    expect(post).toHaveBeenCalledWith('/tabiai/issue-cookie', { account_name: '我的 站点/A' })
  })
})

describe('凭据失效名单', () => {
  it('走 GET /tabiai/expired，解出名单与判定时间', async () => {
    get.mockResolvedValue({
      data: {
        accounts: [
          { name: 'A', state: 'invalid', paused: false, message: '凭据已失效',
            last_run_at: '2026-08-28T07:00:00Z', has_user_session: true },
          { name: 'B', state: 'abnormal', paused: true, message: '已暂停',
            last_run_at: '2026-08-28T06:00:00Z', has_user_session: false }
        ],
        count: 2,
        checked_at: '2026-08-28T07:00:00Z'
      }
    })
    const result = await listExpiredTabiAI()
    expect(get).toHaveBeenCalledWith('/tabiai/expired')
    expect(result.count).toBe(2)
    expect(result.checked_at).toBe('2026-08-28T07:00:00Z')
    // has_user_session 决定能不能自动签发，是一键签发的筛选依据
    expect(result.accounts.filter((a) => a.has_user_session).map((a) => a.name)).toEqual(['A'])
  })

  it('名单里不含任何凭据字段', async () => {
    get.mockResolvedValue({
      data: {
        accounts: [{ name: 'A', state: 'invalid', paused: false, message: '',
                     last_run_at: '', has_user_session: true }],
        count: 1,
        checked_at: ''
      }
    })
    const result = await listExpiredTabiAI()
    const serialized = JSON.stringify(result)
    expect(serialized).not.toContain('new_api_refresh')
    expect(serialized).not.toContain('***')
    expect(result.accounts[0]).not.toHaveProperty('cookie')
    expect(result.accounts[0]).not.toHaveProperty('github_user_session')
  })

  it('没有失效账号时是空数组', async () => {
    get.mockResolvedValue({ data: { accounts: [], count: 0, checked_at: '' } })
    const result = await listExpiredTabiAI()
    expect(result.accounts).toEqual([])
    expect(result.count).toBe(0)
  })

  it('服务端错误原样抛出，交页面提示', async () => {
    get.mockRejectedValue({ response: { status: 500, data: { error: '服务器内部错误' } } })
    await expect(listExpiredTabiAI()).rejects.toMatchObject({
      response: { data: { error: '服务器内部错误' } }
    })
  })
})
