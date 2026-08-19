/*
web/src/utils/__tests__/runLock.test.ts
签到锁展示与轮询判断的测试（Vitest）
覆盖：自动解锁倒计时的各种边界、锁文案、409 响应体解析、轮询跳过条件
说明：纯函数，时间通过参数注入，不依赖真实时钟
*/
import { describe, expect, it } from 'vitest'
import {
  RUN_LOCK_POLL_INTERVAL,
  idleRunState,
  runLockFromError,
  runLockOwner,
  runLockSummary,
  secondsUntilAutoUnlock,
  shouldPollRunLock
} from '../runLock'
import type { RunState } from '@/types'

const NOW = Date.parse('2026-08-18T12:00:00Z')

function locked(overrides: Partial<RunState> = {}): RunState {
  return {
    running: true,
    source: 'GitHub Actions（me/repo）',
    started_at: '2026-08-18T11:50:00Z',
    heartbeat_at: '2026-08-18T11:59:00Z',
    stale_after_seconds: 300,
    heartbeat_seconds: 60,
    ...overrides
  }
}

describe('idleRunState', () => {
  it('默认是未锁定：拿不到后端数据时不该凭空拦住用户', () => {
    expect(idleRunState().running).toBe(false)
  })
})

describe('shouldPollRunLock', () => {
  it('空闲可见时放行', () => {
    expect(shouldPollRunLock({ hidden: false, inFlight: false })).toBe(true)
  })

  it('标签页不可见时跳过', () => {
    expect(shouldPollRunLock({ hidden: true, inFlight: false })).toBe(false)
  })

  it('上一发未落地时跳过，避免慢响应下请求堆积', () => {
    expect(shouldPollRunLock({ hidden: false, inFlight: true })).toBe(false)
  })
})

describe('secondsUntilAutoUnlock', () => {
  it('按最后心跳 + 过期时长算出剩余秒数', () => {
    // 心跳在 11:59:00，过期 300s，则 12:04:00 自动解锁，距 12:00:00 还有 240s
    expect(secondsUntilAutoUnlock(locked(), NOW)).toBe(240)
  })

  it('未锁定时为 0', () => {
    expect(secondsUntilAutoUnlock(locked({ running: false }), NOW)).toBe(0)
  })

  it('已经超过过期时刻时返回 0 而不是负数', () => {
    expect(secondsUntilAutoUnlock(locked({ heartbeat_at: '2026-08-18T11:00:00Z' }), NOW)).toBe(0)
  })

  it('心跳时间解析不出来时返回 0，不让界面出现 NaN', () => {
    expect(secondsUntilAutoUnlock(locked({ heartbeat_at: '不是时间' }), NOW)).toBe(0)
  })

  it('后端没给过期时长时返回 0', () => {
    expect(secondsUntilAutoUnlock(locked({ stale_after_seconds: 0 }), NOW)).toBe(0)
  })
})

describe('runLockOwner', () => {
  it('来源为空时给个兜底名字，别在界面上留空白', () => {
    expect(runLockOwner(locked({ source: '   ' }))).toBe('签到客户端')
  })

  it('有来源就原样用', () => {
    expect(runLockOwner(locked({ source: 'my-vps' }))).toBe('my-vps')
  })
})

describe('runLockSummary', () => {
  it('未锁定时是空串，调用方据此不渲染提示', () => {
    expect(runLockSummary(locked({ running: false }), NOW)).toBe('')
  })

  it('锁定时要同时说清「谁在跑」和「多久自动解锁」', () => {
    const text = runLockSummary(locked(), NOW)
    expect(text).toContain('GitHub Actions（me/repo）')
    expect(text).toContain('4 分钟')
  })

  it('不足一分钟也报 1 分钟，避免出现「还剩 0 分钟」', () => {
    const text = runLockSummary(locked({ heartbeat_at: '2026-08-18T11:55:10Z' }), NOW)
    expect(text).toContain('1 分钟')
  })

  it('已过期但后端仍说 running 时退化成模糊文案，不显示倒计时', () => {
    const text = runLockSummary(locked({ heartbeat_at: '2026-08-18T10:00:00Z' }), NOW)
    expect(text).toContain('已锁定')
    expect(text).not.toContain('分钟后自动解锁')
  })
})

describe('runLockFromError', () => {
  it('从 409 响应体里取出锁状态，省一次往返', () => {
    const state = runLockFromError({
      response: { status: 409, data: { error: '锁定', run_state: locked() } }
    })
    expect(state?.running).toBe(true)
    expect(state?.source).toBe('GitHub Actions（me/repo）')
  })

  it('非 409 一律返回 null：别把别的错误当成锁', () => {
    expect(runLockFromError({ response: { status: 400, data: { run_state: locked() } } })).toBeNull()
  })

  it('409 但没带 run_state 时返回 null，调用方退回轮询', () => {
    expect(runLockFromError({ response: { status: 409, data: { error: '别的冲突' } } })).toBeNull()
  })

  it('run_state 结构不对时返回 null 而不是造出半个对象', () => {
    expect(runLockFromError({ response: { status: 409, data: { run_state: { source: 'x' } } } })).toBeNull()
  })

  it('缺字段时补默认值，保证调用方拿到完整结构', () => {
    const state = runLockFromError({
      response: { status: 409, data: { run_state: { running: true } } }
    })
    expect(state).toEqual({
      running: true,
      source: '',
      started_at: '',
      heartbeat_at: '',
      stale_after_seconds: 0,
      heartbeat_seconds: 0
    })
  })

  it('不是对象的输入不会抛异常', () => {
    expect(runLockFromError(null)).toBeNull()
    expect(runLockFromError('boom')).toBeNull()
  })
})

describe('轮询间隔', () => {
  it('比配置版本号轮询更慢：锁的变化频率低得多', () => {
    expect(RUN_LOCK_POLL_INTERVAL).toBeGreaterThanOrEqual(5000)
    expect(RUN_LOCK_POLL_INTERVAL).toBeLessThanOrEqual(30000)
  })
})
