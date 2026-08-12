<!--
web/src/views/ExportView.vue
页面：配置导出
职责：
- 展示完整 config.json 明文（textarea）
- 一键复制 + 重新获取
- 提示保存到 GitHub Secret CONFIG_JSON 作为兜底
数据来源：GET /api/export
-->
<template>
  <div class="page-container export-page">
    <n-card :bordered="false" class="export-card">
      <template #header>
        <div class="card-header">
          <span class="card-title">导出完整配置</span>
          <n-tag v-if="jsonText" size="small" type="success" :bordered="false">已获取 {{ sizeLabel }}</n-tag>
        </div>
      </template>

      <n-alert type="info" :bordered="false" class="export-tip">
        <template #icon><n-icon><information-circle-outline /></n-icon></template>
        导出完整明文 config.json（含 Cookie 等敏感信息）。请将内容保存到 GitHub Secret
        <n-text code>CONFIG_JSON</n-text> 作为兜底，供 GitHub Actions 在无法从远程拉取时直接使用。
      </n-alert>

      <n-spin :show="loading">
        <n-input
          v-model:value="jsonText"
          type="textarea"
          :autosize="{ minRows: 18, maxRows: 32 }"
          readonly
          class="mono-text export-textarea"
          placeholder="点击下方「获取配置」按钮拉取完整配置…"
        />
      </n-spin>

      <div class="export-actions">
        <n-button type="primary" :loading="loading" @click="fetchExport">
          <template #icon><n-icon><refresh-outline /></n-icon></template>
          获取配置
        </n-button>
        <n-button type="success" :disabled="!jsonText" @click="copyJson">
          <template #icon><n-icon><copy-outline /></n-icon></template>
          一键复制
        </n-button>
      </div>
    </n-card>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { NCard, NButton, NIcon, NInput, NTag, NAlert, NText, NSpin, useMessage } from 'naive-ui'
import { RefreshOutline, CopyOutline, InformationCircleOutline } from '@vicons/ionicons5'
import { exportConfig } from '@/api/export'
import { extractErrorMessage } from '@/utils/error'
import { copyText } from '@/utils/clipboard'

const message = useMessage()
const loading = ref(false)
const jsonText = ref('')

const sizeLabel = computed(() => {
  const bytes = new Blob([jsonText.value]).size
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(2)} MB`
})

async function fetchExport() {
  loading.value = true
  try {
    const res = await exportConfig()
    jsonText.value = res.json
    message.success('完整配置已获取')
  } catch (e) {
    message.error(extractErrorMessage(e, '获取导出配置失败'))
  } finally {
    loading.value = false
  }
}

async function copyJson() {
  if (!jsonText.value) return
  const ok = await copyText(jsonText.value)
  if (ok) message.success('完整配置已复制到剪贴板')
  else message.error('复制失败，请手动选择复制')
}

onMounted(fetchExport)
</script>

<style scoped>
.export-card {
  background: #fff;
}

.card-header {
  display: flex;
  align-items: center;
  gap: 12px;
}

.card-title {
  font-size: 16px;
  font-weight: 600;
  color: #1f2d3d;
}

.export-tip {
  margin-bottom: 18px;
}

.export-textarea {
  font-family: 'JetBrains Mono', Consolas, 'Courier New', monospace !important;
  font-size: 12px !important;
  line-height: 1.6 !important;
}

.export-actions {
  display: flex;
  gap: 12px;
  margin-top: 18px;
}
</style>
