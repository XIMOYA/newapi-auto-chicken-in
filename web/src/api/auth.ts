/*
web/src/api/auth.ts
鉴权接口封装（对应契约 §2 登录 / §8 修改密码 / §1 健康检查）
*/
import http from './http'
import type {
  LoginParams,
  LoginResult,
  ChangePasswordParams,
  HealthResult,
  VerifyPasswordResult
} from '@/types'

export function login(data: LoginParams): Promise<LoginResult> {
  return http.post<LoginResult>('/login', data).then((r) => r.data)
}

export function changePassword(data: ChangePasswordParams): Promise<{ ok: boolean }> {
  return http.put<{ ok: boolean }>('/password', data).then((r) => r.data)
}

/**
 * 二次确认当前用户密码，成功后拿到一张**一次性导出票据**。
 * 服务端的 GET /api/export 会校验并销毁该票据，所以必须把它传给 exportConfig()，
 * 否则导出会被 403 拒绝 —— 这样密码确认才是服务端真正生效的保护，而非前端装饰。
 */
export function verifyPassword(password: string): Promise<VerifyPasswordResult> {
  return http.post<VerifyPasswordResult>('/auth/verify-password', { password }).then((r) => r.data)
}

export function getHealth(): Promise<HealthResult> {
  return http.get<HealthResult>('/health').then((r) => r.data)
}
