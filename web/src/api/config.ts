/*
web/src/api/config.ts
配置接口封装（对应契约 §3 获取配置 / §4 保存配置 / §5 拉取配置）
*/
import http from './http'
import type { AppConfig, ConfigResponse, SaveConfigResult } from '@/types'

export function getConfig(): Promise<ConfigResponse> {
  return http.get<ConfigResponse>('/config').then((r) => r.data)
}

// revision 走乐观锁：服务端版本不一致时返回 409，避免用陈旧快照整份覆盖别人的改动
export function saveConfig(config: AppConfig, revision?: number): Promise<SaveConfigResult> {
  const body: { config: AppConfig; revision?: number } = { config }
  if (typeof revision === 'number') body.revision = revision
  return http.put<SaveConfigResult>('/config', body).then((r) => r.data)
}

export function getRawConfig(token: string): Promise<AppConfig> {
  return http
    .get<AppConfig>('/config/raw', { headers: { Authorization: `Bearer ${token}` } })
    .then((r) => r.data)
}
