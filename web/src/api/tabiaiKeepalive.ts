/*
web/src/api/tabiaiKeepalive.ts
TaBiAI 凭据保活接口封装。

保活是服务端的后台行为：按设定间隔主动 refresh 一次，让 new_api_refresh 的代次
保持滚动并立刻落库。这里只负责读状态、改策略、手动触发一轮。

两条规则会体现在返回数据里：
- 签到运行期间整轮避让（skipped_by_checkin=true），因为两边抢同一条 sid 会把账号打死
- 凭据失效的账号会被暂停（paused=true），要等人工改过凭据才在下一轮自动恢复

数据来源：
- GET  /api/tabiai/keepalive
- PUT  /api/tabiai/keepalive
- POST /api/tabiai/keepalive/run
*/
import http from './http'

/** 保活策略：开关 + 间隔（服务端会把间隔夹到 15~720 分钟） */
export interface TabiAIKeepaliveSetting {
  enabled: boolean
  minutes: number
  updated_at: string
}

/** 单个账号的最后一次刷新结果 */
export interface TabiAIKeepaliveRow {
  account_name: string
  last_run_at: string
  /** 沿用 Cookie 检测的状态词：valid / invalid / abnormal / skipped；空串表示还没轮到它。
   * 代理不通也归 abnormal（服务端内部另有 retryable 标记），要看 message 才知道是链路问题 */
  state: string
  message: string
  /** 上一次站点是否真的换了代次。长期为 false 说明回写链路可能有问题 */
  rotated: boolean
  paused: boolean
  paused_at: string
  /** 空串表示那一次是直连 */
  proxy_addr: string
}

export interface TabiAIKeepaliveStatus {
  setting: TabiAIKeepaliveSetting
  accounts: TabiAIKeepaliveRow[]
  last_run_at: string
  running: boolean
  skipped_by_checkin: boolean
  next_run_at: string
}

export interface TabiAIKeepaliveRunResult {
  ok_count: number
  paused_count: number
  failed_count: number
  status: TabiAIKeepaliveStatus
}

export function getTabiAIKeepalive(): Promise<TabiAIKeepaliveStatus> {
  return http.get<TabiAIKeepaliveStatus>('/tabiai/keepalive').then((r) => r.data)
}

export function saveTabiAIKeepalive(
  setting: Pick<TabiAIKeepaliveSetting, 'enabled' | 'minutes'>
): Promise<TabiAIKeepaliveStatus> {
  return http.put<TabiAIKeepaliveStatus>('/tabiai/keepalive', setting).then((r) => r.data)
}

/**
 * 立刻手动刷一轮。服务端同步跑完才返回，所以调用方要给按钮加 loading。
 * 签到进行中会返回 409，由 http 拦截器统一抛出，页面提示「稍后再试」即可。
 */
export function runTabiAIKeepalive(): Promise<TabiAIKeepaliveRunResult> {
  return http.post<TabiAIKeepaliveRunResult>('/tabiai/keepalive/run').then((r) => r.data)
}

/** 状态词 -> 中文标签与徽章配色，页面与测试共用一份，避免两处写歪 */
export const KEEPALIVE_STATE_LABEL: Record<string, string> = {
  valid: '正常',
  invalid: '凭据失效',
  abnormal: '链路或响应异常',
  skipped: '已跳过'
}

export function keepaliveStateLabel(state: string): string {
  if (!state) return '尚未刷新'
  return KEEPALIVE_STATE_LABEL[state] ?? state
}

export function keepaliveStateType(state: string): 'success' | 'error' | 'warning' | 'default' {
  if (state === 'valid') return 'success'
  if (state === 'invalid') return 'error'
  if (state === 'abnormal') return 'warning'
  return 'default'
}
