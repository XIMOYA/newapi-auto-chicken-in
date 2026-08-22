/*
web/src/api/__tests__/tabiaiKeepalive.test.ts
凭据保活接口与展示映射的测试。

重点不在"能不能发请求"，而在两处容易写歪的地方：
- 状态词到中文标签/徽章配色的映射，页面和这里共用同一份，改一处不会漏另一处
- 未刷过（空状态）要显示"尚未刷新"而不是空白或原样英文
*/
import { describe, expect, it, vi, beforeEach } from 'vitest'
import http from '../http'
import {
  getTabiAIKeepalive,
  keepaliveStateLabel,
  keepaliveStateType,
  runTabiAIKeepalive,
  saveTabiAIKeepalive
} from '../tabiaiKeepalive'

vi.mock('../http', () => ({
  default: { get: vi.fn(), put: vi.fn(), post: vi.fn() }
}))

const mocked = http as unknown as {
  get: ReturnType<typeof vi.fn>
  put: ReturnType<typeof vi.fn>
  post: ReturnType<typeof vi.fn>
}

const sampleStatus = {
  setting: { enabled: true, minutes: 90, updated_at: '2026-08-21T10:00:00Z' },
  accounts: [],
  last_run_at: '2026-08-21T10:00:00Z',
  running: false,
  skipped_by_checkin: false,
  next_run_at: '2026-08-21T11:30:00Z'
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('接口调用', () => {
  it('GET 读状态', async () => {
    mocked.get.mockResolvedValue({ data: sampleStatus })
    await expect(getTabiAIKeepalive()).resolves.toEqual(sampleStatus)
    expect(mocked.get).toHaveBeenCalledWith('/tabiai/keepalive')
  })

  it('PUT 只提交开关与间隔', async () => {
    mocked.put.mockResolvedValue({ data: sampleStatus })
    await saveTabiAIKeepalive({ enabled: false, minutes: 120 })
    expect(mocked.put).toHaveBeenCalledWith('/tabiai/keepalive', {
      enabled: false,
      minutes: 120
    })
  })

  it('POST 手动跑一轮并带回计数', async () => {
    mocked.post.mockResolvedValue({
      data: { ok_count: 3, paused_count: 1, failed_count: 0, status: sampleStatus }
    })
    const result = await runTabiAIKeepalive()
    expect(mocked.post).toHaveBeenCalledWith('/tabiai/keepalive/run')
    expect(result.ok_count).toBe(3)
    expect(result.paused_count).toBe(1)
  })

  it('请求失败原样抛出，交给页面提示', async () => {
    mocked.get.mockRejectedValue(new Error('409'))
    await expect(getTabiAIKeepalive()).rejects.toThrow('409')
  })
})

describe('状态展示映射', () => {
  it('已知状态词有中文标签', () => {
    expect(keepaliveStateLabel('valid')).toBe('正常')
    expect(keepaliveStateLabel('invalid')).toBe('凭据失效')
    expect(keepaliveStateLabel('abnormal')).toBe('链路或响应异常')
    expect(keepaliveStateLabel('skipped')).toBe('已跳过')
  })

  it('空状态表示还没轮到它，不是空白', () => {
    expect(keepaliveStateLabel('')).toBe('尚未刷新')
  })

  it('未知状态词原样显示，不吞掉信息', () => {
    expect(keepaliveStateLabel('brand_new')).toBe('brand_new')
  })

  it('徽章配色按严重程度区分', () => {
    expect(keepaliveStateType('valid')).toBe('success')
    expect(keepaliveStateType('invalid')).toBe('error')
    expect(keepaliveStateType('abnormal')).toBe('warning')
    expect(keepaliveStateType('')).toBe('default')
  })
})
