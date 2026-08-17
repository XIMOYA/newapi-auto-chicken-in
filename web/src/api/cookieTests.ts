/*
web/src/api/cookieTests.ts
Cookie 可用性测试接口封装：站点 Cookie 与 TaBiAI 凭据使用独立启动接口，
检测在服务端后台执行，前端通过 status 轮询进度、通过 stop 手动结束。
另附 TaBiAI 凭据签发入口（用账号里的 GitHub user_session 换一条新的 new_api_refresh）。

数据来源：
- POST /api/cookie-tests/newapi
- POST /api/cookie-tests/tabiai
- GET  /api/cookie-tests/status
- POST /api/cookie-tests/stop
- POST /api/tabiai/issue-cookie
*/
import http from './http'
import type { CookieTestStatus } from '@/types'

export interface CookieTestStartResult {
  ok: boolean
  mode: string
  started: boolean
}

/** 签发结果里的 account_name 由服务端回显，用于确认改的是哪个账号 */
export interface IssueTabiAICookieResult {
  ok: boolean
  account_name: string
}

export function startNewAPICookieTest(accountNames: string[] = []): Promise<CookieTestStartResult> {
  return http
    .post<CookieTestStartResult>('/cookie-tests/newapi', { account_names: accountNames })
    .then((r) => r.data)
}

export function startTabiAICookieTest(accountNames: string[] = []): Promise<CookieTestStartResult> {
  return http
    .post<CookieTestStartResult>('/cookie-tests/tabiai', { account_names: accountNames })
    .then((r) => r.data)
}

export function getCookieTestStatus(): Promise<CookieTestStatus> {
  return http.get<CookieTestStatus>('/cookie-tests/status').then((r) => r.data)
}

export function stopCookieTest(): Promise<{ ok: boolean }> {
  return http.post<{ ok: boolean }>('/cookie-tests/stop').then((r) => r.data)
}

/**
 * 为指定账号签发一条全新的 new_api_refresh 并写回 accounts[].cookie。
 * 服务端会走 GitHub OAuth 三步，因此该账号必须已填 github_user_session，否则返回 400。
 */
export function issueTabiAICookie(accountName: string): Promise<IssueTabiAICookieResult> {
  return http
    .post<IssueTabiAICookieResult>('/tabiai/issue-cookie', { account_name: accountName })
    .then((r) => r.data)
}
