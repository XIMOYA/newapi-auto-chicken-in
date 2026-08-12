/*
web/src/utils/error.ts
工具函数：提取后端错误信息
*/
import type { ApiError } from '@/types'

export function extractErrorMessage(e: unknown, fallback: string): string {
  const data = (e as { response?: { data?: ApiError } })?.response?.data
  if (data && typeof data.error === 'string' && data.error !== '') return data.error
  const msg = (e as { message?: string })?.message
  return msg || fallback
}
