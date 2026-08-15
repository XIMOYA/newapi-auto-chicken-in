/*
web/src/utils/proxyPolling.ts
纯函数工具：代理后台任务（刷新/测速）的轮询状态快照与「任务恰好完成」判断。

为什么存在：
- ProxiesView 每 2s 轮询 GET /api/proxies/stats，需要检测后台任务从「运行中」
  变为「已结束」，以便触发列表自动刷新。
- 该判断被抽成纯函数，便于用 Vitest 单测覆盖边界情况。

数据结构（与 types/index.ts 对齐，这里用最小结构类型避免循环依赖）：
- stats.running：服务端任务总开关
- stats.progress?.running：进度对象自身的 running 标记
- stats.progress?.stage：fetching / testing / speedtest / done
*/

export interface ProxyPollSnapshot {
  /** stats.running === true */
  running: boolean
  /** stats.progress?.running === true */
  progressRunning: boolean
  /** stats.progress?.stage ?? '' */
  stage: string
  /** stats.progress 是否存在 */
  hasProgress: boolean
}

export interface ProxyPollStatsLike {
  running: boolean
  progress?: { running?: boolean; stage?: string } | null
}

/** 由 stats 响应构建一次轮询快照 */
export function pollStateFromStats(stats: ProxyPollStatsLike): ProxyPollSnapshot {
  return {
    running: stats.running === true,
    progressRunning: stats.progress?.running === true,
    stage: stats.progress?.stage ?? '',
    hasProgress: stats.progress != null
  }
}

/** 快照是否表示「有后台任务在跑」 */
export function isTaskRunning(s: ProxyPollSnapshot): boolean {
  return s.running || s.progressRunning
}

/**
 * 判断两次连续轮询之间后台任务是否「恰好完成」。
 *
 * 规则：
 * - 本次快照仍在运行 -> 未完成，返回 false
 * - 没有上一次快照（页面首次轮询，无从比较）-> 返回 false
 * - 上一次在运行、本次已结束 -> 任务恰好完成，返回 true
 *
 * 之所以要求「上一次在运行」，是为了避免页面刚加载、或任务从未启动时，
 * 每次空闲轮询都误触发一次列表刷新。
 */
export function shouldReloadAfterCompletion(
  prev: ProxyPollSnapshot | null,
  next: ProxyPollSnapshot
): boolean {
  if (isTaskRunning(next)) return false
  if (!prev) return false
  return isTaskRunning(prev)
}
