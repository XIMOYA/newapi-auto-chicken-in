/*
web/src/api/export.ts
导出/导入接口封装（对应契约 §7 + 导入扩展）
*/
import http from './http'
import type { ExportResult, ImportParams, ImportResult } from '@/types'

export function exportConfig(): Promise<ExportResult> {
  return http.get<ExportResult>('/export').then((r) => r.data)
}

export function importConfig(params: ImportParams): Promise<ImportResult> {
  return http.post<ImportResult>('/config/import', params).then((r) => r.data)
}
