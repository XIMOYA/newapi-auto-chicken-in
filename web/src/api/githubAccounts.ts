/*
web/src/api/githubAccounts.ts
GitHub 凭据池接口封装
职责：
- 池子增删改：POST /api/github-accounts/ops（提交操作而非整份快照）
- 单条 user_session 可用性探测：POST /api/github-accounts/check
说明：
- 增删改刻意不走 PUT /api/config —— 整份提交会把后台签到刚轮转的凭据覆盖掉
- 探测会实际请求 GitHub OAuth，单次要几十秒，必须单独放大超时（默认 30s 不够）
数据来源：
- POST /api/github-accounts/ops
- POST /api/github-accounts/check
*/
import http from './http'
import type { GitHubAccountCheckResult, GitHubAccountOp, GitHubAccountOpsResult } from '@/types'

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
