/*
web/src/types/index.ts
类型定义：与 docs/api-contract.md 严格对齐
职责：
- 配置对象各模块类型（accounts/ai/browser/http/defaults/proxy_pool/notify/config_sync/security）
- API 请求/响应类型
数据来源：docs/api-contract.md、config.example.json
*/

// ===== 配置对象类型（与 config.example.json 结构一致）=====

export interface Account {
  name: string
  url: string
  cookie: string
  user_id: number | string | null
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
}

export interface SaveConfigResult {
  ok: boolean
  updated_at: string
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

// ===== 导出 =====

export interface ExportResult {
  json: string
}

// ===== 导入 =====

export interface ImportParams {
  /** 导入的完整配置对象 */
  config: Record<string, unknown>
  /** overwrite 覆盖全部 / merge 合并（账号/站点按 name 合并，其余模块保留） */
  mode: 'overwrite' | 'merge'
}

export interface ImportResult {
  ok: boolean
  mode: 'overwrite' | 'merge'
  updated_at: string
}
