/*
web/src/utils/cookieTestPolling.ts
纯函数工具：Cookie 检测后台任务的轮询判断。

为什么存在：
- 检测改为后台任务后，CookieTestsView 需要每 2s 轮询 GET /api/cookie-tests/status，
  并在任务从「运行中」变为「已结束」的那一刻弹一次汇总提示、停止轮询。
- 另一类模式的任务正在跑时，本模式的按钮要禁用，避免两个 Tab 互相打断。
- 判断逻辑抽成纯函数，便于 Vitest 覆盖边界（首次轮询、切模式、停止后等）。
*/

import type { CookieTestStatus } from '@/types'

export interface CookieTestSnapshot {
  running: boolean
  stopped: boolean
  mode: string
  round: number
  /** 已定论账号数（valid + invalid + abnormal + skipped） */
  settled: number
  total: number
}

/** 由 status 响应构建一次轮询快照 */
export function snapshotFromStatus(status: CookieTestStatus): CookieTestSnapshot {
  const s = status.summary
  return {
    running: status.running === true,
    stopped: status.stopped === true,
    mode: status.mode ?? '',
    round: status.round ?? 0,
    settled: (s?.valid ?? 0) + (s?.invalid ?? 0) + (s?.abnormal ?? 0) + (s?.skipped ?? 0),
    total: s?.total ?? 0
  }
}

/**
 * 判断两次连续轮询之间任务是否「恰好结束」。
 *
 * 要求上一次快照必须处于运行中：否则页面刚打开、或任务从未启动时，
 * 每次空闲轮询都会误触发一次完成提示。
 */
export function justFinished(
  prev: CookieTestSnapshot | null,
  next: CookieTestSnapshot
): boolean {
  if (next.running) return false
  if (!prev) return false
  return prev.running
}

/** 该模式的检测按钮是否应禁用：另一类模式的任务正在跑 */
export function isBusyWithOtherMode(snapshot: CookieTestSnapshot | null, mode: string): boolean {
  if (!snapshot || !snapshot.running) return false
  return snapshot.mode !== '' && snapshot.mode !== mode
}

/** 快照是否属于当前模式（用于决定要不要把结果渲染到本 Tab） */
export function belongsToMode(snapshot: CookieTestSnapshot | null, mode: string): boolean {
  if (!snapshot) return false
  return snapshot.mode === mode
}
