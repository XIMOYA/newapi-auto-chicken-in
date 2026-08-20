/*
web/src/types/index.ts
类型定义：与 docs/api-contract.md 严格对齐
职责：
- 配置对象各模块类型（accounts/ai/browser/http/defaults/proxy_pool/notify/config_sync/security）
- API 请求/响应类型
数据来源：docs/api-contract.md、config.example.json
*/

// ===== 配置对象类型（与 config.example.json 结构一致）=====

/**
 * 登录方式。
 * 旧值 `github_cookie` 已由后端 config v3 迁移统一改判为 `tabiai`（GitHub OAuth 不再是登录方式），
 * 前端不需要再识别旧值。
 */
export type LoginMethod = 'newapi_cookie' | 'tabiai'

export interface Account {
  name: string
  url: string
  login_method: LoginMethod
  /**
   * 站点凭据：newapi_cookie 存完整 Cookie 头；tabiai 存 new_api_refresh 的值。
   * tabiai 的值由后台签到持续轮转，因此不带 revision 的保存与导入都不会覆盖它 ——
   * 要显式改就得带 revision（网页始终带），或用签发 / 回写接口。
   */
  cookie: string
  /** 不再是登录凭据，仅作 POST /api/tabiai/issue-cookie 签发 new_api_refresh 的原料 */
  github_user_session: string
  github_client_id: string
  user_id: number | null
  /** 手动代理。有意不打码，会以明文回传给已登录管理员（详见 api-contract 打码规则） */
  proxy: string | null
  checkin_path: string | null
  browser_path: string | null
  enabled: boolean
}

export interface Site {
  name: string
  url: string
  checkin_path: string | null
  browser_path: string | null
}

export interface AIConfig {
  enabled: boolean
  base_url: string
  api_key: string
  model: string
  timeout: number
  max_retries: number
}

export interface BrowserConfig {
  driver: string
  headless: string
  humanize: boolean
  timeout: number
  keep_artifacts_on_fail: boolean
  locale: string
  window: [number, number]
  executable_path: string | null
}

export interface HttpConfig {
  impersonate: string
  timeout: number
  verify: boolean
}

export interface DefaultsConfig {
  retry: number
  interval_seconds: [number, number]
}

export interface ProxyPoolConfig {
  enabled: boolean
  test_url: string
  timeout: number
  max_workers: number
  max_proxies: number
  ip_swap_limit: number
  sources: string[]
  /** 服务器端代理池后台刷新间隔（分钟），0=关闭自动刷新 */
  refresh_minutes: number
  /** 服务器端最多保留多少条可用代理 */
  save_limit: number
  /** 后台刷新时是否测通 */
  auto_test: boolean
  /** Actions 预取地址（供展示） */
  remote_url: string
  /** Actions 预取鉴权 token（与 config_sync 同一个 API Key） */
  remote_token: string
  remote_token_header: string
  remote_token_prefix: string
  /** Actions 跑完是否回传各代理的成败计数，供服务端优选排序 */
  report_feedback: boolean
  /** 开跑前在客户端本机快测一遍预取列表，剔掉当场连不上的 */
  preflight_check: boolean
  /** 自筛只测前多少个（列表已按优选排序） */
  preflight_limit: number
  /** 自筛整体时间盒（秒），到点就用已有结论 */
  preflight_seconds: number
}

export interface NotifyEmailConfig {
  enabled: boolean
  smtp_host: string
  smtp_port: number
  use_ssl: boolean
  username: string
  password: string
  from_addr: string
  to_addrs: string[]
  subject_prefix: string
  timeout: number
}

export interface NotifyConfig {
  email: NotifyEmailConfig
}

export interface ConfigSyncConfig {
  enabled: boolean
  url: string
  method: string
  token: string
  token_header: string
  token_prefix: string
  headers: Record<string, string>
  body: unknown
  response_field: string
  timeout: number
  auto_before_checkin: boolean
}

export interface SecurityConfig {
  encryption_enabled: boolean
  config_key: string
  encrypted_file: string
}

export interface AppConfig {
  accounts: Account[]
  sites: Site[]
  ai: AIConfig
  browser: BrowserConfig
  http: HttpConfig
  defaults: DefaultsConfig
  proxy_pool: ProxyPoolConfig
  notify: NotifyConfig
  config_sync: ConfigSyncConfig
  security: SecurityConfig
}

// ===== 通用响应 =====

export interface ApiError {
  error: string
}

// ===== Cookie 可用性测试 =====

export type CookieTestMode = 'newapi_cookie' | 'tabiai'

/** pending / running 是后台任务的中间态，只在轮询期间出现 */
export type CookieTestState = 'valid' | 'invalid' | 'abnormal' | 'skipped' | 'pending' | 'running'

export interface CookieTestResult {
  name: string
  url: string
  state: CookieTestState
  user_id: number | null
  duration_ms: number
  message: string
  /** 已尝试轮次 */
  attempts: number
  /** 最后一次使用的代理（host:port，空串=直连） */
  proxy: string
}

export interface CookieTestSummary {
  total: number
  valid: number
  invalid: number
  abnormal: number
  skipped: number
}

/** GET /api/cookie-tests/status 的响应：后台任务快照 */
export interface CookieTestStatus {
  running: boolean
  stopped: boolean
  mode: CookieTestMode | ''
  round: number
  started_at: string
  checked_at: string
  duration_sec: number
  last_error: string
  summary: CookieTestSummary
  results: CookieTestResult[]
}

// ===== 鉴权 =====

export interface LoginParams {
  username: string
  password: string
}

export interface LoginResult {
  token: string
  username: string
  expires_in: number
}

export interface ChangePasswordParams {
  old_password: string
  new_password: string
}

export interface HealthResult {
  ok: boolean
  version: string
  time: string
}

// ===== 配置 =====

export interface ConfigResponse {
  config: AppConfig
  updated_at: string
  /** 乐观锁版本号：保存时必须带回，服务端据此拒绝陈旧快照 */
  revision: number
}

export interface SaveConfigResult {
  ok: boolean
  updated_at: string
  revision: number
}

/** 轻量版本号：只用于轮询判断配置有没有被别人改过 */
export interface ConfigRevisionResult {
  revision: number
}

// ===== 账号级增量操作 =====

/**
 * 一条账号操作。走 POST /api/accounts/ops 时提交的是「意图」而不是整份快照，
 * 服务端在最新配置上按账号名重放，多人同时改不同账号互不覆盖。
 */
export type AccountOp =
  | {
      type: 'upsert'
      account: Account
      /** 改名时填原名：服务端据此找回打码字段的真值，用户不必重填凭据 */
      previous_name?: string
    }
  | { type: 'delete'; name: string }
  | { type: 'set_enabled'; name: string; enabled: boolean }

export interface AccountOpsResult {
  ok: boolean
  /** 服务端重放后的最新打码配置，前端直接换上即可 */
  config: AppConfig
  updated_at: string
  revision: number
  /** 被跳过的操作说明（如目标账号已被他人删除），非错误 */
  skipped: string[] | null
}

// ===== 签到运行状态（网页端凭据操作锁）=====

/**
 * 签到进程在平台上占的那把锁。
 *
 * TaBiAI 的 new_api_refresh 有代次轮转 + 重放检测：签到进程和网页端同时动同一条
 * sid，旧代次会被站点判为重放、整条会话被撤销。所以签到期间要锁住凭据检测与签发。
 * running 由后端算好（有记录且心跳未过期），前端直接用，不要自己拿时间去判。
 */
export interface RunState {
  running: boolean
  /** 客户端自报来源，如 "GitHub Actions（me/repo）"；可能为空 */
  source: string
  started_at: string
  /** 最后一次心跳；配合 stale_after_seconds 能算出最晚何时自动解锁 */
  heartbeat_at: string
  /** 多久没心跳就自动解锁——Actions 被强杀时没人来发「结束」，靠它兜底 */
  stale_after_seconds: number
  /** 平台建议的心跳间隔，客户端用 */
  heartbeat_seconds: number
}

// ===== API Key =====

export interface ApiKey {
  id: number
  name: string
  prefix: string
  created_at: string
  last_used_at: string | null
}

export interface CreateKeyResult {
  id: number
  name: string
  key: string
}

// ===== 代理池 =====

export interface ProxyEntry {
  id: number
  source: string
  addr: string
  latency_ms: number
  alive: boolean
  last_checked_at: string
  last_alive_at?: string
  /** 实测下载吞吐（字节/秒），0=未测速 */
  speed_bps: number
}

export interface ProxyStatsResult {
  total: number
  alive: number
  by_source: Record<string, number>
  last_run: string
  last_error: string
  running: boolean
  progress?: ProxyProgress
}

export interface ProxyProgress {
  running: boolean
  stage: string
  fetched: number
  candidates: number
  tested: number
  alive: number
  target: number
  started_at: string
  duration_sec: number
}

// ===== 导出 =====

export interface ExportResult {
  json: string
}

/** 二次密码确认的结果：ticket 是 GET /api/export 的一次性通行证 */
export interface VerifyPasswordResult {
  ok: boolean
  ticket: string
  expires_in: number
}

// ===== 导入 =====

export interface ImportParams {
  /** 导入的完整配置对象 */
  config: Record<string, unknown>
  /** overwrite 覆盖全部 / merge 合并（按 modules 勾选模块） */
  mode: 'overwrite' | 'merge'
  /** merge 模式下要导入的模块键；缺省/undefined = 默认全部 */
  modules?: string[]
}

export interface ImportResult {
  ok: boolean
  mode: 'overwrite' | 'merge'
  updated_at: string
}
