/*
web/src/utils/runLock.ts
纯函数工具：签到锁的展示与轮询判断。

为什么单独抽出来：
- 「还剩多久自动解锁」这类计算要拿 heartbeat_at + stale_after_seconds 推，边界不少
  （时间解析失败、心跳已经过期、后端没给 stale_after_seconds），塞在组件里没法测。
- 轮询的跳过条件与 revisionSync 是同一套思路，保持一致以免两处行为漂移。
*/

import type { RunState } from '@/types'

/**
 * 锁状态轮询间隔。
 *
 * 比配置版本号那个（5s）慢一些：锁的变化频率低得多（一轮签到才一次），
 * 而且后端 stale_after 是分钟级，10 秒的滞后完全无感。
 */
export const RUN_LOCK_POLL_INTERVAL = 10000

/** 空闲状态：拿不到后端数据时用它，宁可显示「没锁」也不要凭空拦住用户 */
export function idleRunState(): RunState {
  return {
    running: false,
    source: '',
    started_at: '',
    heartbeat_at: '',
    stale_after_seconds: 0,
    heartbeat_seconds: 0,
    holders: 0
  }
}

/** 这一拍该不该发轮询请求（跳过条件与 revisionSync 保持一致） */
export function shouldPollRunLock(ctx: { hidden: boolean; inFlight: boolean }): boolean {
  if (ctx.hidden) return false
  if (ctx.inFlight) return false
  return true
}

/**
 * 距离自动解锁还剩多少秒。
 *
 * 返回 0 表示「已经该解锁了」或「算不出来」——调用方据此退化成模糊文案，
 * 不要显示负数或 NaN 分钟。
 */
export function secondsUntilAutoUnlock(state: RunState, now = Date.now()): number {
  if (!state.running || !state.heartbeat_at || state.stale_after_seconds <= 0) return 0
  const beat = Date.parse(state.heartbeat_at)
  if (Number.isNaN(beat)) return 0
  const left = Math.floor((beat + state.stale_after_seconds * 1000 - now) / 1000)
  return left > 0 ? left : 0
}

/** 「谁在跑」的展示名：后端可能没拿到来源，别在界面上留空白 */
export function runLockOwner(state: RunState): string {
  return state.source.trim() || '签到客户端'
}

/**
 * 锁状态的一句话说明。带上「最晚何时自动解锁」，用户才知道该等还是该强制解锁。
 */
export function runLockSummary(state: RunState, now = Date.now()): string {
  if (!state.running) return ''
  const left = secondsUntilAutoUnlock(state, now)
  const owner = runLockOwner(state)
  // 分片并行时会有多个持有者，说一句能省掉「为什么点了解锁还锁着」的困惑
  const shards = state.holders > 1 ? `（${state.holders} 个任务在跑）` : ''
  if (left <= 0) {
    return `${owner} 正在签到${shards}，TaBiAI 凭据检测与签发已锁定`
  }
  // 不足一分钟也报 1 分钟：显示「还剩 0 分钟」比模糊说法更让人困惑
  const minutes = Math.max(1, Math.ceil(left / 60))
  return `${owner} 正在签到${shards}，TaBiAI 凭据检测与签发已锁定；`
    + `若对方已停止上报心跳，约 ${minutes} 分钟后自动解锁`
}

/**
 * 从 409 错误里取出后端回传的锁状态。
 *
 * 前端禁用按钮只能挡住「已知在锁」的情况；两次轮询之间签到才开跑时请求照样会撞
 * 409，此时用响应里带的状态立刻纠正本地视图，省一次往返。
 */
export function runLockFromError(error: unknown): RunState | null {
  if (!error || typeof error !== 'object') return null
  const response = (error as { response?: { status?: number; data?: unknown } }).response
  if (response?.status !== 409) return null
  const data = response.data
  if (!data || typeof data !== 'object') return null
  const raw = (data as { run_state?: unknown }).run_state
  if (!raw || typeof raw !== 'object') return null
  const state = raw as Partial<RunState>
  if (typeof state.running !== 'boolean') return null
  return {
    running: state.running,
    source: typeof state.source === 'string' ? state.source : '',
    started_at: typeof state.started_at === 'string' ? state.started_at : '',
    heartbeat_at: typeof state.heartbeat_at === 'string' ? state.heartbeat_at : '',
    stale_after_seconds:
      typeof state.stale_after_seconds === 'number' ? state.stale_after_seconds : 0,
    heartbeat_seconds:
      typeof state.heartbeat_seconds === 'number' ? state.heartbeat_seconds : 0,
    holders: typeof state.holders === 'number' ? state.holders : 0
  }
}
