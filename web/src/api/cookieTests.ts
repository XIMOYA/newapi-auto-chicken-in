/*
web/src/api/cookieTests.ts
Cookie 可用性测试接口封装：NewAPI Cookie 与 GitHub Cookie 使用独立接口。
*/
import http from './http'
import type { CookieTestResponse } from '@/types'

export function testNewAPICookies(accountNames: string[] = []): Promise<CookieTestResponse> {
  return http
    .post<CookieTestResponse>('/cookie-tests/newapi', { account_names: accountNames })
    .then((r) => r.data)
}

export function testGithubCookies(accountNames: string[] = []): Promise<CookieTestResponse> {
  return http
    .post<CookieTestResponse>('/cookie-tests/github', { account_names: accountNames })
    .then((r) => r.data)
}
