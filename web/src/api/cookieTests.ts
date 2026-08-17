/*
web/src/api/cookieTests.ts
Cookie 可用性测试接口封装：站点 Cookie 与 GitHub OAuth 使用独立启动接口，
检测在服务端后台执行，前端通过 status 轮询进度、通过 stop 手动结束。
*/
import http from './http'
import type { CookieTestStatus } from '@/types'

export interface CookieTestStartResult {
  ok: boolean
  mode: string
  started: boolean
}

export function startNewAPICookieTest(accountNames: string[] = []): Promise<CookieTestStartResult> {
  return http
    .post<CookieTestStartResult>('/cookie-tests/newapi', { account_names: accountNames })
    .then((r) => r.data)
}

export function startGithubCookieTest(accountNames: string[] = []): Promise<CookieTestStartResult> {
  return http
    .post<CookieTestStartResult>('/cookie-tests/github', { account_names: accountNames })
    .then((r) => r.data)
}

export function getCookieTestStatus(): Promise<CookieTestStatus> {
  return http.get<CookieTestStatus>('/cookie-tests/status').then((r) => r.data)
}

export function stopCookieTest(): Promise<{ ok: boolean }> {
  return http.post<{ ok: boolean }>('/cookie-tests/stop').then((r) => r.data)
}
