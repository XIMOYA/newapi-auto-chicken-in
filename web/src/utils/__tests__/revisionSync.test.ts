/*
web/src/utils/__tests__/revisionSync.test.ts
版本号轮询时机判断的测试（Vitest）
覆盖：三种跳过条件（标签页隐藏 / 上一发未落地 / store 正在拉配置）与正常放行
说明：纯函数，不涉及定时器与 DOM
*/
import { describe, expect, it } from 'vitest'
import { REVISION_POLL_INTERVAL, shouldPollRevision } from '../revisionSync'

const idle = { hidden: false, inFlight: false, loading: false }

describe('shouldPollRevision', () => {
  it('空闲且可见时放行', () => {
    expect(shouldPollRevision(idle)).toBe(true)
  })

  it('标签页不可见时跳过：没人在看，白花流量', () => {
    expect(shouldPollRevision({ ...idle, hidden: true })).toBe(false)
  })

  it('上一发还在飞时跳过：否则慢响应下请求会堆积', () => {
    expect(shouldPollRevision({ ...idle, inFlight: true })).toBe(false)
  })

  it('store 正在拉整份配置时跳过：重复劳动', () => {
    expect(shouldPollRevision({ ...idle, loading: true })).toBe(false)
  })

  it('多个条件同时命中仍是跳过', () => {
    expect(shouldPollRevision({ hidden: true, inFlight: true, loading: true })).toBe(false)
  })
})

describe('轮询间隔', () => {
  it('取值落在「够快又不扰民」的区间内', () => {
    expect(REVISION_POLL_INTERVAL).toBeGreaterThanOrEqual(2000)
    expect(REVISION_POLL_INTERVAL).toBeLessThanOrEqual(15000)
  })
})
