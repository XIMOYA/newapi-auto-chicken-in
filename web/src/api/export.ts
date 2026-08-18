/*
web/src/api/export.ts
导出/导入接口封装（对应契约 §7 + 导入扩展）
*/
import http from './http'
import type { ExportResult, ImportParams, ImportResult } from '@/types'

/**
 * 拉取完整明文配置。ticket 由 verifyPassword() 签发，单次有效、2 分钟过期，
 * 通过 X-Export-Ticket 头提交；缺失或复用会被服务端 403 拒绝。
 */
export function exportConfig(ticket: string): Promise<ExportResult> {
  return http
    .get<ExportResult>('/export', { headers: { 'X-Export-Ticket': ticket } })
    .then((r) => r.data)
}

export function importConfig(params: ImportParams): Promise<ImportResult> {
  return http.post<ImportResult>('/config/import', params).then((r) => r.data)
}
