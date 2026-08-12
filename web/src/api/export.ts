/*
web/src/api/export.ts
导出接口封装（对应契约 §7）
*/
import http from './http'
import type { ExportResult } from '@/types'

export function exportConfig(): Promise<ExportResult> {
  return http.get<ExportResult>('/export').then((r) => r.data)
}
