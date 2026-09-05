/*
web/src/api/githubAccounts.ts
GitHub 凭据池接口封装
职责：
- 池子增删改：POST /api/github-accounts/ops（提交操作而非整份快照）
- 单条 user_session 可用性探测：POST /api/github-accounts/check
- 账号自身状态探测：POST /api/github-accounts/status（可登录/停用/封禁）
- 按站点 URL 批量建签到账号：POST /api/sites/provision
- 站点 API Key：POST /api/sites/apikeys（备号取值）、GET /api/sites/apikeys（脱敏清单）
说明：
- 增删改刻意不走 PUT /api/config —— 整份提交会把后台签到刚轮转的凭据覆盖掉
- 两个探测都会实际请求 GitHub，单次要几十秒，必须单独放大超时（默认 30s 不够）
- API Key 的明文清单在 /api/sites/apikeys/raw，只认 API Key，前端不封装它 ——
  那些 key 能直接调用付费接口，不该经过浏览器
数据来源：
- POST /api/github-accounts/ops
- POST /api/github-accounts/check
- POST /api/github-accounts/status
- POST /api/sites/provision
- POST /api/sites/apikeys
- GET /api/sites/apikeys
*/
import http from './http'
import type {
  APIKeyEntry,
  APIKeyOutcome,
  GitHubAccountCheckResult,
  GitHubAccountOp,
  GitHubAccountOpsResult,
  GitHubStatusResult,
  ProvisionOutcome,
} from '@/types'

/**
 * 探测的超时。服务端串行执行且要走完「取 flow_token → 换授权 code」两步，
 * 实测几十秒，取 3 分钟留足余量；沿用 http 实例的 30s 会在服务端还在跑时就报超时，
 * 用户看到的是「失败」而实际探测仍在进行。
 */
export const GITHUB_CHECK_TIMEOUT_MS = 180_000

/** 服务端限制单次最多 500 条 op，超了直接 400 —— 这里同源一份常量供界面自检 */
export const MAX_GITHUB_ACCOUNT_OPS = 500

/**
 * 池子增删改。不带 revision：服务端持锁重读最新配置再重放，
 * 因此不会 409，也不会用我的快照覆盖别人刚改的另一条。
 *
 * 改名要在 upsert 里带 previous_name，user_session 可以只传 "***"。
 * 删除时若还有站点账号在引用，服务端返回 400 并说明有几个在用。
 */
export function applyGitHubAccountOps(ops: GitHubAccountOp[]): Promise<GitHubAccountOpsResult> {
  return http.post<GitHubAccountOpsResult>('/github-accounts/ops', { ops }).then((r) => r.data)
}

/**
 * 探测某个池子账号的 user_session 还能不能在它引用的站点上完成授权。
 *
 * 站点上下文来自「第一个引用它的账号」，所以没被任何账号引用时服务端返回 400。
 */
export function checkGitHubAccount(name: string): Promise<GitHubAccountCheckResult> {
  return http
    .post<GitHubAccountCheckResult>('/github-accounts/check', { name }, { timeout: GITHUB_CHECK_TIMEOUT_MS })
    .then((r) => r.data)
}

/**
 * 探测账号自身状态（与站点无关）：active / suspended / banned / expired / unknown。
 * 停用/封禁的账号不该留在池子里（每轮签发都会失败）。
 */
export function checkGitHubAccountStatus(name: string): Promise<{ result: GitHubStatusResult }> {
  return http
    .post<{ result: GitHubStatusResult }>('/github-accounts/status', { name }, { timeout: GITHUB_CHECK_TIMEOUT_MS })
    .then((r) => r.data)
}

/**
 * 为启用账号在各自站点上备一条 API Key 并取回全值。
 *
 * 服务端串行执行（站点给取值接口挂了限流），账号多时要几分钟，超时同样放宽到 10 分钟。
 * 已有那条不会被重建 —— 重建会换出新值，把已经配到别处的旧 key 作废。
 */
export function ensureSiteAPIKeys(
  only?: string[],
): Promise<{ got: number; total: number; results: APIKeyOutcome[] }> {
  return http
    .post<{ got: number; total: number; results: APIKeyOutcome[] }>(
      '/sites/apikeys',
      { only },
      { timeout: 600_000 },
    )
    .then((r) => r.data)
}

/**
 * 拉汇总清单。key 是脱敏的 —— 明文只有 /sites/apikeys/raw 给，那个端点只认
 * API Key，浏览器这边拿不到也不该拿到。要判断有没有 key 请看 has_key。
 */
export function listSiteAPIKeys(): Promise<{ total: number; items: APIKeyEntry[] }> {
  return http
    .get<{ total: number; items: APIKeyEntry[] }>('/sites/apikeys')
    .then((r) => r.data)
}

/**
 * 按站点 URL 为池子里的 GitHub 账号批量建签到账号。
 *
 * 会为每个账号签发一条站点凭据，整批可能几分钟 —— 超时放宽到 10 分钟。
 * 已存在的账号服务端会跳过，不会重签。
 */
export function provisionSite(
  url: string,
  only?: string[],
): Promise<{ created: number; total: number; results: ProvisionOutcome[] }> {
  return http
    .post<{ created: number; total: number; results: ProvisionOutcome[] }>(
      '/sites/provision',
      { url, only },
      { timeout: 600_000 },
    )
    .then((r) => r.data)
}
