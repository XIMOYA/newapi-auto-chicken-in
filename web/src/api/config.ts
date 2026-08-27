/*
web/src/api/config.ts
配置接口封装（对应契约 §3 获取配置 / §4 保存配置 / §5 拉取配置 / §4.1 账号增量操作 / §4.2 账号清单与单账号查询）
*/
import http from './http'
import type {
  AccountDetailResponse, AccountListResponse, AccountOp, AccountOpsResult, AppConfig,
  ConfigResponse, ConfigRevisionResult, SaveConfigResult
} from '@/types'

export function getConfig(): Promise<ConfigResponse> {
  return http.get<ConfigResponse>('/config').then((r) => r.data)
}

// 只取版本号：整份配置有几十 KB，而轮询只需要判断「变没变」
export function getConfigRevision(): Promise<ConfigRevisionResult> {
  return http.get<ConfigRevisionResult>('/config/revision').then((r) => r.data)
}

// revision 走乐观锁：服务端版本不一致时返回 409，避免用陈旧快照整份覆盖别人的改动
export function saveConfig(config: AppConfig, revision?: number): Promise<SaveConfigResult> {
  const body: { config: AppConfig; revision?: number } = { config }
  if (typeof revision === 'number') body.revision = revision
  return http.put<SaveConfigResult>('/config', body).then((r) => r.data)
}

/**
 * 账号增删改走这里，不带 revision：服务端在最新配置上重放操作，
 * 因此不会 409，也不会用我的快照覆盖别人刚加的账号。
 */
export function applyAccountOps(ops: AccountOp[]): Promise<AccountOpsResult> {
  return http.post<AccountOpsResult>('/accounts/ops', { ops }).then((r) => r.data)
}

export function getRawConfig(token: string): Promise<AppConfig> {
  return http
    .get<AppConfig>('/config/raw', { headers: { Authorization: `Bearer ${token}` } })
    .then((r) => r.data)
}

/**
 * 账号名清单 + 常用元数据，不含任何凭据。
 *
 * 用途是「先找名字、再按名字查改」：详情走 getAccountDetail，改内容走
 * applyAccountOps 的 upsert。比拉整份 getConfig 轻得多，也不必解析其他配置段。
 */
export function listAccounts(): Promise<AccountListResponse> {
  return http.get<AccountListResponse>('/accounts').then((r) => r.data)
}

/**
 * 单个账号的脱敏配置 + cookie 核实摘要。
 *
 * 用途是核实凭据回写有没有真的落库：客户端每轮转一次代次就回写一次，页面靠比对
 * cookie_digest.fingerprint 判断平台手里是不是最新那一代，明文不会下发浏览器。
 * 账号名要转义 —— 名字里允许出现斜杠和中文，直接拼进路径会串到别的端点上去。
 */
export function getAccountDetail(name: string): Promise<AccountDetailResponse> {
  return http
    .get<AccountDetailResponse>(`/accounts/${encodeURIComponent(name)}`)
    .then((r) => r.data)
}
