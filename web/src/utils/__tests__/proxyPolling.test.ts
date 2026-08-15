/*
web/src/utils/__tests__/proxyPolling.test.ts
proxyPolling.ts 纯函数的单元测试（Vitest）
覆盖：快照构建、运行中判断、任务「恰好完成」转移判断的边界情况
*/
import { describe, expect, it } from 'vitest'
import {
  isTaskRunning,
  pollStateFromStats,
  shouldReloadAfterCompletion,
  type ProxyPollSnapshot
} from '../proxyPolling'

function snapshot(partial: Partial<ProxyPollSnapshot>): ProxyPollSnapshot {
  return {
    running: false,
    progressRunning: false,
    stage: '',
    hasProgress: false,
    ...partial
  }
}

describe('pollStateFromStats', () => {
  it('无 progress 时构建全 false 快照', () => {
    expect(pollStateFromStats({ running: false })).toEqual({
      running: false,
      progressRunning: false,
      stage: '',
      hasProgress: false
    })
  })

  it('读取 stats.running 与 progress 字段', () => {
    expect(
      pollStateFromStats({
        running: true,
        progress: { running: true, stage: 'speedtest' }
      })
    ).toEqual({
      running: true,
      progressRunning: true,
      stage: 'speedtest',
      hasProgress: true
    })
  })

  it('progress 为 null 时视为不存在', () => {
    const s = pollStateFromStats({ running: false, progress: null })
    expect(s.hasProgress).toBe(false)
    expect(s.progressRunning).toBe(false)
  })
})

describe('isTaskRunning', () => {
  it('任一 running 标记为 true 即为运行中', () => {
    expect(isTaskRunning(snapshot({ running: true }))).toBe(true)
    expect(isTaskRunning(snapshot({ progressRunning: true }))).toBe(true)
    expect(isTaskRunning(snapshot({ running: true, progressRunning: true }))).toBe(true)
  })

  it('全部 false 时为空闲', () => {
    expect(isTaskRunning(snapshot({}))).toBe(false)
  })
})

describe('shouldReloadAfterCompletion', () => {
  it('运行中 -> 空闲：任务恰好完成，需要刷新', () => {
    const prev = snapshot({ running: true, progressRunning: true, stage: 'testing' })
    const next = snapshot({ stage: 'done', hasProgress: true })
    expect(shouldReloadAfterCompletion(prev, next)).toBe(true)
  })

  it('空闲 -> 空闲：没有任务结束，不刷新', () => {
    const prev = snapshot({})
    const next = snapshot({})
    expect(shouldReloadAfterCompletion(prev, next)).toBe(false)
  })

  it('空闲 -> 运行中：任务刚开始，不刷新', () => {
    const prev = snapshot({})
    const next = snapshot({ running: true, progressRunning: true, stage: 'fetching' })
    expect(shouldReloadAfterCompletion(prev, next)).toBe(false)
  })

  it('运行中 -> 运行中：仍在跑，不刷新', () => {
    const prev = snapshot({ running: true, progressRunning: true })
    const next = snapshot({ running: true, progressRunning: true })
    expect(shouldReloadAfterCompletion(prev, next)).toBe(false)
  })

  it('无上一次快照（首次轮询）时不刷新，避免页面刚加载误触发', () => {
    expect(shouldReloadAfterCompletion(null, snapshot({}))).toBe(false)
    expect(shouldReloadAfterCompletion(null, snapshot({ running: false, progressRunning: false }))).toBe(false)
  })

  it('只有 progress.running 在跑、stats.running 缺失时也能识别完成转移', () => {
    const prev = snapshot({ progressRunning: true, stage: 'testing' })
    const next = snapshot({ stage: 'done' })
    expect(shouldReloadAfterCompletion(prev, next)).toBe(true)
  })

  it('stats 顶层 running 为 true 但 progress 缺省时按运行中处理', () => {
    const prev = snapshot({ running: true })
    const next = snapshot({ running: true })
    expect(shouldReloadAfterCompletion(prev, next)).toBe(false)
    // 结束后转空闲
    const done = snapshot({})
    expect(shouldReloadAfterCompletion(prev, done)).toBe(true)
  })
})
