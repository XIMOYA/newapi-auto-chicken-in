/*
web/src/utils/configConflict.ts
纯函数工具：识别配置保存的 409 版本冲突并取出服务端回传的最新状态。

为什么存在：
- PUT /api/config 带 revision 走乐观锁，多人/多标签页各自用旧快照提交时服务端返回 409，
  响应体里带着当前 revision 与打码后的最新配置，前端要据此直接接管而不是让用户手动刷新。
- 判定与取值逻辑抽成纯函数，便于 Vitest 覆盖（缺字段、非 409、无响应体等边界）。
*/

import type { AppConfig } from '@/types'

/** 服务端 409 响应体 */
export interface ConfigConflictPayload {
  error: string
  revision: number
  updated_at: string
  config: AppConfig
}

/** 冲突时统一使用的提示文案 */
export const CONFIG_CONFLICT_MESSAGE =
  '配置已被他人修改，已为你载入最新版本，请确认后重新提交'

/** 最小结构类型：避免为了判断状态码而依赖 axios 类型 */
interface ErrorLike {
  response?: { status?: number; data?: unknown } | null
}

/** 该错误是否为配置版本冲突（HTTP 409） */
export function isConfigConflict(error: unknown): boolean {
  if (!error || typeof error !== 'object') return false
  return (error as ErrorLike).response?.status === 409
}

/**
 * 从 409 错误里取出服务端回传的最新配置与 revision。
 * 响应体不完整时返回 null，调用方退回到「重新 GET 一次」的兜底路径。
 */
export function conflictPayload(error: unknown): ConfigConflictPayload | null {
  if (!isConfigConflict(error)) return null
  const data = (error as ErrorLike).response?.data
  if (!data || typeof data !== 'object') return null
  const raw = data as Partial<ConfigConflictPayload>
  if (typeof raw.revision !== 'number' || !raw.config) return null
  return {
    error: typeof raw.error === 'string' ? raw.error : CONFIG_CONFLICT_MESSAGE,
    revision: raw.revision,
    updated_at: typeof raw.updated_at === 'string' ? raw.updated_at : '',
    config: raw.config as AppConfig
  }
}
