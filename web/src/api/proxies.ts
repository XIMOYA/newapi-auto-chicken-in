/*
web/src/api/proxies.ts
服务器端代理池接口封装
*/
import http from './http'
import type { ProxyEntry, ProxyStatsResult } from '@/types'

export interface ProxyListResult {
  proxies: ProxyEntry[]
  total: number
}

export function listProxies(params?: { alive?: boolean; limit?: number }): Promise<ProxyListResult> {
  return http.get<ProxyListResult>('/proxies', { params }).then((r) => r.data)
}

export function getProxyStats(): Promise<ProxyStatsResult> {
  return http.get<ProxyStatsResult>('/proxies/stats').then((r) => r.data)
}

export function refreshProxies(): Promise<{ ok: boolean; message: string }> {
  return http.post<{ ok: boolean; message: string }>('/proxies/refresh').then((r) => r.data)
}

export function speedTestProxies(params: { proxies: string[] }): Promise<{ ok: boolean; tested: number; url: string }> {
  return http.post<{ ok: boolean; tested: number; url: string }>('/proxies/speedtest', params).then((r) => r.data)
}
