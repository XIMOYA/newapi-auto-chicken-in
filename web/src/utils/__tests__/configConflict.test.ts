/*
web/src/utils/__tests__/configConflict.test.ts
覆盖 409 版本冲突的识别与响应体解析边界。
*/
import { describe, expect, it } from 'vitest'
import {
  CONFIG_CONFLICT_MESSAGE,
  conflictPayload,
  isConfigConflict
} from '../configConflict'
import type { AppConfig } from '@/types'

const fakeConfig = { accounts: [] } as unknown as AppConfig

function axiosError(status: number, data?: unknown) {
  return { response: { status, data } }
}

describe('isConfigConflict', () => {
  it('409 判为冲突', () => {
    expect(isConfigConflict(axiosError(409, {}))).toBe(true)
  })

  it('其他状态码不算冲突', () => {
    expect(isConfigConflict(axiosError(400, {}))).toBe(false)
    expect(isConfigConflict(axiosError(500))).toBe(false)
  })

  it('网络错误（无 response）不算冲突', () => {
    expect(isConfigConflict(new Error('Network Error'))).toBe(false)
    expect(isConfigConflict({ response: null })).toBe(false)
  })

  it('非对象输入不炸', () => {
    expect(isConfigConflict(null)).toBe(false)
    expect(isConfigConflict(undefined)).toBe(false)
    expect(isConfigConflict('409')).toBe(false)
  })
})

describe('conflictPayload', () => {
  it('取出最新 revision 与配置', () => {
    const payload = conflictPayload(
      axiosError(409, {
        error: '配置已被他人修改，请重新载入最新版本后再提交',
        revision: 7,
        updated_at: '2026-08-17T00:00:00Z',
        config: fakeConfig
      })
    )
    expect(payload).not.toBeNull()
    expect(payload!.revision).toBe(7)
    expect(payload!.updated_at).toBe('2026-08-17T00:00:00Z')
    expect(payload!.config).toBe(fakeConfig)
  })

  it('非 409 返回 null', () => {
    expect(conflictPayload(axiosError(400, { revision: 1, config: fakeConfig }))).toBeNull()
  })

  it('缺 revision 或 config 时返回 null，让调用方走重新拉取兜底', () => {
    expect(conflictPayload(axiosError(409, { config: fakeConfig }))).toBeNull()
    expect(conflictPayload(axiosError(409, { revision: 3 }))).toBeNull()
    expect(conflictPayload(axiosError(409, 'plain text'))).toBeNull()
    expect(conflictPayload(axiosError(409))).toBeNull()
  })

  it('revision 为 0 是合法版本，不能被当成缺失', () => {
    const payload = conflictPayload(axiosError(409, { revision: 0, config: fakeConfig }))
    expect(payload).not.toBeNull()
    expect(payload!.revision).toBe(0)
  })

  it('error 字段缺失时回退到统一文案', () => {
    const payload = conflictPayload(axiosError(409, { revision: 2, config: fakeConfig }))
    expect(payload!.error).toBe(CONFIG_CONFLICT_MESSAGE)
  })
})
