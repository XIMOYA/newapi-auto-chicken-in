/*
web/src/api/__tests__/export.test.ts
导出与二次密码确认的接口封装测试（Vitest）
覆盖：exportConfig 必须把票据放进 X-Export-Ticket 头、verifyPassword 解出票据、403 透传
说明：mock 掉 ../http，只验证「调了哪个地址、带了什么头」，不发真实请求
*/
import { beforeEach, describe, expect, it, vi } from 'vitest'

const get = vi.fn()
const post = vi.fn()

vi.mock('../http', () => ({
  default: {
    get: (...args: unknown[]) => get(...args),
    post: (...args: unknown[]) => post(...args)
  }
}))

import { exportConfig, importConfig } from '../export'
import { verifyPassword } from '../auth'

beforeEach(() => {
  get.mockReset()
  post.mockReset()
})

describe('二次密码确认', () => {
  it('成功时解出一次性票据与有效期', async () => {
    post.mockResolvedValue({ data: { ok: true, ticket: 'tk-abc', expires_in: 120 } })
    const result = await verifyPassword('pw')
    expect(post).toHaveBeenCalledWith('/auth/verify-password', { password: 'pw' })
    expect(result.ticket).toBe('tk-abc')
    expect(result.expires_in).toBe(120)
  })

  it('密码错误时把 400 透传给调用方', async () => {
    post.mockRejectedValue({ response: { status: 400, data: { error: '密码错误' } } })
    await expect(verifyPassword('bad')).rejects.toMatchObject({
      response: { data: { error: '密码错误' } }
    })
  })
})

describe('导出完整配置', () => {
  it('票据必须放进 X-Export-Ticket 头', async () => {
    get.mockResolvedValue({ data: { json: '{"accounts":[]}' } })
    const result = await exportConfig('tk-abc')
    expect(get).toHaveBeenCalledWith('/export', { headers: { 'X-Export-Ticket': 'tk-abc' } })
    expect(result.json).toBe('{"accounts":[]}')
  })

  it('票据缺失/失效时服务端的 403 原样抛出', async () => {
    get.mockRejectedValue({
      response: { status: 403, data: { error: '导出需要先通过密码确认（票据缺失、已使用或已过期），请重新验证密码' } }
    })
    await expect(exportConfig('')).rejects.toMatchObject({ response: { status: 403 } })
  })

  it('每次导出都要带票据，不复用上一次的调用参数', async () => {
    get.mockResolvedValue({ data: { json: '{}' } })
    await exportConfig('tk-1')
    await exportConfig('tk-2')
    expect(get).toHaveBeenNthCalledWith(1, '/export', { headers: { 'X-Export-Ticket': 'tk-1' } })
    expect(get).toHaveBeenNthCalledWith(2, '/export', { headers: { 'X-Export-Ticket': 'tk-2' } })
  })
})

describe('导入配置', () => {
  it('按 mode 与 modules 提交，不需要票据', async () => {
    post.mockResolvedValue({ data: { ok: true, mode: 'merge', modules: ['accounts'] } })
    await importConfig({ config: { accounts: [] } as never, mode: 'merge', modules: ['accounts'] })
    expect(post).toHaveBeenCalledWith('/config/import', {
      config: { accounts: [] },
      mode: 'merge',
      modules: ['accounts']
    })
  })

  it('无法还原的占位符会收到 400', async () => {
    post.mockRejectedValue({ response: { status: 400, data: { error: 'accounts[0].cookie 无法还原' } } })
    await expect(
      importConfig({ config: {} as never, mode: 'overwrite', modules: [] })
    ).rejects.toMatchObject({ response: { status: 400 } })
  })
})
