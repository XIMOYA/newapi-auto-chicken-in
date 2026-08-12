/*
web/src/api/keys.ts
API Key 接口封装（对应契约 §6）
*/
import http from './http'
import type { ApiKey, CreateKeyResult } from '@/types'

export function listKeys(): Promise<{ keys: ApiKey[] }> {
  return http.get<{ keys: ApiKey[] }>('/keys').then((r) => r.data)
}

export function createKey(name: string): Promise<CreateKeyResult> {
  return http.post<CreateKeyResult>('/keys', { name }).then((r) => r.data)
}

export function deleteKey(id: number): Promise<{ ok: boolean }> {
  return http.delete<{ ok: boolean }>(`/keys/${id}`).then((r) => r.data)
}
