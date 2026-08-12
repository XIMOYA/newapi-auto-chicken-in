/*
web/src/api/config.ts
配置接口封装（对应契约 §3 获取配置 / §4 保存配置 / §5 拉取配置）
*/
import http from './http'
import type { AppConfig, ConfigResponse, SaveConfigResult } from '@/types'

export function getConfig(): Promise<ConfigResponse> {
  return http.get<ConfigResponse>('/config').then((r) => r.data)
}

export function saveConfig(config: AppConfig): Promise<SaveConfigResult> {
  return http.put<SaveConfigResult>('/config', { config }).then((r) => r.data)
}

export function getRawConfig(token: string): Promise<AppConfig> {
  return http
    .get<AppConfig>('/config/raw', { headers: { Authorization: `Bearer ${token}` } })
    .then((r) => r.data)
}
