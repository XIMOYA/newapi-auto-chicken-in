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
- GET  /api/tabiai/expired
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

/**
 * 凭据失效的 tabiai 账号。
 *
 * has_user_session 决定能不能自动签发 —— 没填的只能人工粘贴新凭据，
 * 一键签发要把这类账号跳过并单独提示，别让用户以为点了就都处理了。
 */
export interface ExpiredTabiAIAccount {
  name: string
  state: string
  paused: boolean
  message: string
  last_run_at: string
  has_user_session: boolean
}

export interface ExpiredTabiAIListResult {
  accounts: ExpiredTabiAIAccount[]
  count: number
  /** 名单的判定时间（取最近一次保活刷新时间），空串表示还没有任何记录 */
  checked_at: string
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

/**
 * 凭据失效的 tabiai 账号名单。
 *
 * 读的是库里保活写下的判定，**不触发检测**，所以毫秒返回、可随时查 —— 不必逼用户
 * 每次先跑一遍几十秒的凭据检测。判定口径从严：只有 invalid / paused 算失效，
 * proxy_issue / abnormal 不算（那是代理或网络问题，凭据可能还好，签发它等于白白
 * 作废一条可用凭据）。
 */
export function listExpiredTabiAI(): Promise<ExpiredTabiAIListResult> {
  return http.get<ExpiredTabiAIListResult>('/tabiai/expired').then((r) => r.data)
}
