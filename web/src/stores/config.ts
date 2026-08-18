/*
web/src/stores/config.ts
配置状态：当前配置对象 + updated_at + 乐观锁 revision
职责：
- 登录后 / 每次进入受保护页面前拉取 GET /api/config
- 保存配置（PUT /api/config）时带上 revision；服务端返回 409 说明配置已被他人修改，
  这里自动接管最新版本（顺带回滚本地误改），再抛出统一提示交给各页面的 catch 展示
- 账号增删改走 POST /api/accounts/ops：提交的是操作而不是整份快照，多人同时加账号
  互不覆盖，也不会 409
- 后台轮询 GET /api/config/revision：版本号变了就静默换上最新配置。各设置页的表单是
  进入时深拷贝的本地副本，不会被冲掉；但保存时 deepClone 拿到的是刚同步的最新配置，
  于是只覆盖本页那个区块，既不误报 409 也不会抹掉别人改的其他区块
数据来源：GET/PUT /api/config、POST /api/accounts/ops、GET /api/config/revision
*/
import { defineStore } from 'pinia'
import { ref } from 'vue'
import { applyAccountOps, getConfig, getConfigRevision, saveConfig } from '@/api/config'
import { CONFIG_CONFLICT_MESSAGE, conflictPayload, isConfigConflict } from '@/utils/configConflict'
import type { AccountOp, AppConfig } from '@/types'

export const useConfigStore = defineStore('config', () => {
  const config = ref<AppConfig | null>(null)
  const updatedAt = ref<string>('')
  const revision = ref<number>(0)
  const loading = ref(false)
  /** 最近一次静默同步的时间戳：给界面一个「刚接管了他人改动」的信号 */
  const lastSyncedAt = ref(0)

  async function fetchConfig() {
    loading.value = true
    try {
      const res = await getConfig()
      config.value = res.config
      updatedAt.value = res.updated_at
      revision.value = res.revision ?? 0
      return res
    } finally {
      loading.value = false
    }
  }

  async function save(next: AppConfig) {
    try {
      await saveConfig(next, revision.value)
    } catch (error) {
      if (isConfigConflict(error)) {
        // 409：优先用响应体里回传的最新配置，省一次往返；结构不全时退回重新拉取
        const payload = conflictPayload(error)
        if (payload) {
          config.value = payload.config
          updatedAt.value = payload.updated_at
          revision.value = payload.revision
        } else {
          await fetchConfig()
        }
        throw new Error(CONFIG_CONFLICT_MESSAGE)
      }
      throw error
    }
    // 保存成功后重新拉取：敏感字段（cookie/api_key/password/token）以
    // 后端打码为准，避免明文残留在前端内存/界面上
    await fetchConfig()
    return { updated_at: updatedAt.value, revision: revision.value }
  }

  /**
   * 提交账号操作。响应直接回传服务端重放后的最新打码配置，换上就行 ——
   * 这顺带把别人刚加的账号一起带了回来，不必再拉一次全量。
   */
  async function submitAccountOps(ops: AccountOp[]) {
    const res = await applyAccountOps(ops)
    config.value = res.config
    updatedAt.value = res.updated_at
    revision.value = res.revision
    return res
  }

  /**
   * 比对服务端版本号，变了就静默拉最新配置。
   * 返回是否真的同步过，交给调用方决定要不要提示。
   */
  async function syncIfStale() {
    const { revision: latest } = await getConfigRevision()
    if (latest === revision.value) return false
    await fetchConfig()
    lastSyncedAt.value = Date.now()
    return true
  }

  return {
    config, updatedAt, revision, loading, lastSyncedAt,
    fetchConfig, save, submitAccountOps, syncIfStale
  }
})
