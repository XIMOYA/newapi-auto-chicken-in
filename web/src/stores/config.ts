/*
web/src/stores/config.ts
配置状态：当前配置对象 + updated_at + 乐观锁 revision
职责：
- 登录后 / 每次进入受保护页面前拉取 GET /api/config
- 保存配置（PUT /api/config）时带上 revision；服务端返回 409 说明配置已被他人修改，
  这里自动接管最新版本（顺带回滚本地误改），再抛出统一提示交给各页面的 catch 展示
数据来源：GET/PUT /api/config
*/
import { defineStore } from 'pinia'
import { ref } from 'vue'
import { getConfig, saveConfig } from '@/api/config'
import { CONFIG_CONFLICT_MESSAGE, conflictPayload, isConfigConflict } from '@/utils/configConflict'
import type { AppConfig } from '@/types'

export const useConfigStore = defineStore('config', () => {
  const config = ref<AppConfig | null>(null)
  const updatedAt = ref<string>('')
  const revision = ref<number>(0)
  const loading = ref(false)

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

  return { config, updatedAt, revision, loading, fetchConfig, save }
})
