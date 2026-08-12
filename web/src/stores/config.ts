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
    const res = await saveConfig(next)
    config.value = next
    updatedAt.value = res.updated_at
    return res
  }

  return { config, updatedAt, loading, fetchConfig, save }
})
