/*
web/src/utils/__tests__/cookieTestPolling.test.ts
覆盖 Cookie 检测轮询判断的边界：首次轮询、任务恰好结束、跨模式占用。
*/
import { describe, expect, it } from 'vitest'
import {
  belongsToMode,
  isBusyWithOtherMode,
  justFinished,
  snapshotFromStatus,
  type CookieTestSnapshot
} from '../cookieTestPolling'
import type { CookieTestStatus } from '@/types'

function status(partial: Partial<CookieTestStatus> = {}): CookieTestStatus {
  return {
    running: false,
    stopped: false,
    mode: 'github_cookie',
    round: 0,
    started_at: '',
    checked_at: '',
    duration_sec: 0,
    last_error: '',
    summary: { total: 0, valid: 0, invalid: 0, abnormal: 0, skipped: 0 },
    results: [],
    ...partial
  }
}

function snapshot(partial: Partial<CookieTestSnapshot> = {}): CookieTestSnapshot {
  return { running: false, stopped: false, mode: 'github_cookie', round: 0, settled: 0, total: 0, ...partial }
}

describe('snapshotFromStatus', () => {
  it('累加四类终态作为已定论数', () => {
    const s = snapshotFromStatus(
      status({
        running: true,
        round: 3,
        summary: { total: 6, valid: 2, invalid: 1, abnormal: 1, skipped: 1 }
      })
    )
    expect(s.running).toBe(true)
    expect(s.round).toBe(3)
    expect(s.settled).toBe(5)
    expect(s.total).toBe(6)
  })

  it('summary 缺字段时按 0 处理，不产生 NaN', () => {
    const raw = status()
    // 模拟后端未返回 summary 的极端情况
    ;(raw as unknown as { summary: undefined }).summary = undefined
    const s = snapshotFromStatus(raw)
    expect(s.settled).toBe(0)
    expect(s.total).toBe(0)
  })

  it('mode 为空串时保持空串而不是 undefined', () => {
    expect(snapshotFromStatus(status({ mode: '' })).mode).toBe('')
  })
})

describe('justFinished', () => {
  it('本次仍在运行 -> 未结束', () => {
    expect(justFinished(snapshot({ running: true }), snapshot({ running: true }))).toBe(false)
  })

  it('没有上一次快照 -> 不触发（避免页面刚打开就弹提示）', () => {
    expect(justFinished(null, snapshot({ running: false }))).toBe(false)
  })

  it('上一次运行、本次结束 -> 恰好完成', () => {
    expect(justFinished(snapshot({ running: true }), snapshot({ running: false }))).toBe(true)
  })

  it('两次都空闲 -> 不重复触发', () => {
    expect(justFinished(snapshot({ running: false }), snapshot({ running: false }))).toBe(false)
  })

  it('手动停止也算结束', () => {
    expect(
      justFinished(snapshot({ running: true }), snapshot({ running: false, stopped: true }))
    ).toBe(true)
  })
})

describe('isBusyWithOtherMode', () => {
  it('无快照或未运行 -> 不占用', () => {
    expect(isBusyWithOtherMode(null, 'github_cookie')).toBe(false)
    expect(isBusyWithOtherMode(snapshot({ running: false }), 'github_cookie')).toBe(false)
  })

  it('同模式在跑 -> 不算被别人占用', () => {
    expect(isBusyWithOtherMode(snapshot({ running: true, mode: 'github_cookie' }), 'github_cookie')).toBe(false)
  })

  it('另一模式在跑 -> 占用', () => {
    expect(isBusyWithOtherMode(snapshot({ running: true, mode: 'newapi_cookie' }), 'github_cookie')).toBe(true)
  })

  it('运行中但 mode 为空 -> 不误判', () => {
    expect(isBusyWithOtherMode(snapshot({ running: true, mode: '' }), 'github_cookie')).toBe(false)
  })
})

describe('belongsToMode', () => {
  it('模式一致才归属本 Tab', () => {
    expect(belongsToMode(snapshot({ mode: 'github_cookie' }), 'github_cookie')).toBe(true)
    expect(belongsToMode(snapshot({ mode: 'newapi_cookie' }), 'github_cookie')).toBe(false)
    expect(belongsToMode(null, 'github_cookie')).toBe(false)
  })
})
