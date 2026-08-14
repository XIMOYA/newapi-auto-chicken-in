/*
web/src/api/auth.ts
鉴权接口封装（对应契约 §2 登录 / §8 修改密码 / §1 健康检查）
*/
import http from './http'
import type { LoginParams, LoginResult, ChangePasswordParams, HealthResult } from '@/types'

export function login(data: LoginParams): Promise<LoginResult> {
  return http.post<LoginResult>('/login', data).then((r) => r.data)
}

export function changePassword(data: ChangePasswordParams): Promise<{ ok: boolean }> {
  return http.put<{ ok: boolean }>('/password', data).then((r) => r.data)
}

/** 二次确认当前用户密码（查看明文 Cookie / API Key 前调用） */
export function verifyPassword(password: string): Promise<{ ok: boolean }> {
  return http.post<{ ok: boolean }>('/auth/verify-password', { password }).then((r) => r.data)
}

export function getHealth(): Promise<HealthResult> {
  return http.get<HealthResult>('/health').then((r) => r.data)
}
