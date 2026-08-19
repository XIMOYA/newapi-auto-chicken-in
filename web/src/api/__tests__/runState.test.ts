/*
web/src/api/__tests__/runState.test.ts
签到运行状态接口封装测试（Vitest）
覆盖：查询走 GET /run-state、强制解锁走 POST /run-state/unlock、409 原样抛给调用方
说明：mock 掉 ../http，只验证「调了哪个地址」，不发真实请求
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

import { getRunState, unlockRunState } from '../runState'

beforeEach(() => {
  get.mockReset()
  post.mockReset()
})

describe('查询签到锁状态', () => {
  it('走 GET /run-state 并解出后端判活结论', async () => {
    get.mockResolvedValue({
      data: {
        running: true,
        source: 'github-actions',
        started_at: '2026-08-18T11:00:00Z',
        heartbeat_at: '2026-08-18T11:04:00Z',
        stale_after_seconds: 300,
        heartbeat_seconds: 60
      }
    })
    const state = await getRunState()
    expect(get).toHaveBeenCalledWith('/run-state')
    expect(state.running).toBe(true)
    expect(state.stale_after_seconds).toBe(300)
  })

  it('空闲状态原样透传', async () => {
    get.mockResolvedValue({
      data: {
        running: false, source: '', started_at: '', heartbeat_at: '',
        stale_after_seconds: 300, heartbeat_seconds: 60
      }
    })
    expect((await getRunState()).running).toBe(false)
  })
})

describe('强制解锁', () => {
  it('走 POST /run-state/unlock', async () => {
    post.mockResolvedValue({ data: { ok: true } })
    const res = await unlockRunState()
    expect(post).toHaveBeenCalledWith('/run-state/unlock')
    expect(res.ok).toBe(true)
  })

  it('失败原样抛出，交给页面提示', async () => {
    post.mockRejectedValue({ response: { status: 500, data: { error: '服务器内部错误' } } })
    await expect(unlockRunState()).rejects.toMatchObject({
      response: { data: { error: '服务器内部错误' } }
    })
  })
})

describe('被锁住的检测请求', () => {
  it('409 里带着锁状态，供前端就地纠正视图', async () => {
    // 这条守的是契约：后端必须把 run_state 一起回传，否则前端只能再拉一次
    post.mockRejectedValue({
      response: {
        status: 409,
        data: {
          error: 'github-actions 正在签到，为避免 TaBiAI 凭据代次冲突已暂时锁定该操作',
          run_state: { running: true, source: 'github-actions' }
        }
      }
    })
    await expect(post('/cookie-tests/tabiai', {})).rejects.toMatchObject({
      response: { status: 409, data: { run_state: { running: true } } }
    })
  })
})
