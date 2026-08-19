/*
web/src/api/runState.ts
签到运行状态接口封装（对应契约 §签到运行状态锁）

签到进程用 API Key 上报开跑/心跳/收尾，那部分由 Python 客户端负责；
网页端只需要这两个：查锁状态、以及管理员强制解锁。
*/
import http from './http'
import type { RunState } from '@/types'

export function getRunState(): Promise<RunState> {
  return http.get<RunState>('/run-state').then((r) => r.data)
}

/**
 * 强制解锁。
 *
 * 这是危险操作：如果签到其实还在跑，解锁后去动 TaBiAI 凭据会撞代次，
 * 整条会话会被站点撤销。调用方必须先做二次确认。
 */
export function unlockRunState(): Promise<{ ok: boolean }> {
  return http.post<{ ok: boolean }>('/run-state/unlock').then((r) => r.data)
}
