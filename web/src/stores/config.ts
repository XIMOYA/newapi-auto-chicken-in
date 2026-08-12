/*
web/src/stores/config.ts
配置状态：当前配置对象 + updated_at
职责：
- 登录后 / 每次进入受保护页面前拉取 GET /api/config
- 保存配置（PUT /api/config）后同步本地
数据来源：GET/PUT /api/config
*/
import { defineStore } from 'pinia'
import { ref } from 'vue'
import { getConfig, saveConfig } from '@/api/config'
import type { AppConfig } from '@/types'

export const useConfigStore = defineStore('config', () => {
  const config = ref<AppConfig | null>(null)
  const updatedAt = ref<string>('')
  const loading = ref(false)

  async function fetchConfig() {
    loading.value = true
    try {
      const res = await getConfig()
      config.value = res.config
      updatedAt.value = res.updated_at
      return res
    } finally {
      loading.value = false
    }
  }

  async function save(next: AppConfig) {
    await saveConfig(next)
    // 保存成功后重新拉取：敏感字段（cookie/api_key/password/token）以
    // 后端打码为准，避免明文残留在前端内存/界面上
    await fetchConfig()
    return { updated_at: updatedAt.value }
  }

  return { config, updatedAt, loading, fetchConfig, save }
})
