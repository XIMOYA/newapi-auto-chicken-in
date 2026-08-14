<!--
web/src/views/ExportView.vue
页面：配置导出 / 导入
职责：
- 导出：展示完整 config.json 明文（textarea）+ 一键复制 + 重新获取
- 导入：粘贴 JSON 或上传文件 → 选择模式（覆盖 / 合并）→ 确认后提交
  - overwrite：整体覆盖当前配置
  - merge：账号/站点按 name 合并（同名更新、新名追加），其余模块保留
数据来源：GET /api/export；POST /api/config/import
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

    <n-card :bordered="false" class="import-card">
      <template #header>
        <div class="card-header">
          <span class="card-title">导入配置</span>
          <n-tag v-if="importText" size="small" type="info" :bordered="false">已输入 {{ importSizeLabel }}</n-tag>
        </div>
      </template>

      <n-alert type="warning" :bordered="false" class="import-tip">
        <template #icon><n-icon><warning-outline /></n-icon></template>
        导入将写入完整配置（含 Cookie 等敏感信息）。请粘贴或上传一份完整的
        <n-text code>config.json</n-text>（可用下方「导出」功能获得）。
        <b>覆盖模式</b>会整体替换当前配置；<b>合并模式</b>只增加/更新账号与站点，其余模块保持不变。
      </n-alert>

      <n-radio-group v-model:value="importMode" class="import-mode">
        <n-radio-button value="merge">合并（增加）</n-radio-button>
        <n-radio-button value="overwrite">覆盖（整体替换）</n-radio-button>
      </n-radio-group>

      <n-upload
        :show-file-list="false"
        accept=".json,application/json,text/plain"
        :max="1"
        class="import-upload"
        @change="handleFileChange"
      >
        <n-button dashed>
          <template #icon><n-icon><cloud-upload-outline /></n-icon></template>
          选择 JSON 文件
        </n-button>
      </n-upload>

      <n-input
        v-model:value="importText"
        type="textarea"
        :autosize="{ minRows: 10, maxRows: 20 }"
        class="mono-text import-textarea"
        placeholder='在此粘贴 config.json 内容，或点击上方「选择 JSON 文件」…'
      />

      <n-alert v-if="parseError" type="error" :bordered="false" class="import-error">
        <template #icon><n-icon><alert-circle-outline /></n-icon></template>
        {{ parseError }}
      </n-alert>

      <div class="export-actions">
        <n-button type="primary" :disabled="!importText" :loading="importing" @click="handleImport">
          <template #icon><n-icon><download-outline /></n-icon></template>
          开始导入
        </n-button>
        <n-button v-if="importText" quaternary @click="clearImport">清空</n-button>
      </div>
    </n-card>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import {
  NCard, NButton, NIcon, NInput, NTag, NAlert, NText, NSpin, NRadioGroup, NRadioButton, NUpload,
  useDialog, useMessage
} from 'naive-ui'
import {
  RefreshOutline, CopyOutline, InformationCircleOutline, WarningOutline,
  CloudUploadOutline, AlertCircleOutline, DownloadOutline
} from '@vicons/ionicons5'
import { exportConfig, importConfig } from '@/api/export'
import { extractErrorMessage } from '@/utils/error'
import { copyText } from '@/utils/clipboard'
import type { ImportParams } from '@/types'

const message = useMessage()
const dialog = useDialog()
const loading = ref(false)
const jsonText = ref('')

const importText = ref('')
const importMode = ref<'merge' | 'overwrite'>('merge')
const importing = ref(false)
const parseError = ref('')

const sizeLabel = computed(() => {
  const bytes = new Blob([jsonText.value]).size
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(2)} MB`
})

const importSizeLabel = computed(() => {
  const bytes = new Blob([importText.value]).size
  if (bytes < 1024) return `${bytes} B`
  return `${(bytes / 1024).toFixed(1)} KB`
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

function handleFileChange(options: { file?: { file?: File | null }; fileList?: unknown[] }) {
  const file = options.file?.file
  if (!file) return
  const reader = new FileReader()
  reader.onload = () => {
    importText.value = String(reader.result ?? '')
    validateImport()
  }
  reader.onerror = () => message.error('读取文件失败')
  reader.readAsText(file)
}

/** 解析并校验导入内容，返回是否合法 */
function validateImport(): boolean {
  parseError.value = ''
  const text = importText.value.trim()
  if (!text) return false
  try {
    const obj = JSON.parse(text)
    if (!obj || typeof obj !== 'object' || Array.isArray(obj)) {
      parseError.value = '导入内容必须是 JSON 对象（config.json）'
      return false
    }
    if (!('accounts' in obj)) {
      parseError.value = '缺少 accounts 字段，请确认是完整的 config.json'
      return false
    }
    if (!Array.isArray(obj.accounts)) {
      parseError.value = 'accounts 必须是数组'
      return false
    }
    return true
  } catch {
    parseError.value = 'JSON 解析失败，请检查格式'
    return false
  }
}

function clearImport() {
  importText.value = ''
  parseError.value = ''
}

function handleImport() {
  if (!validateImport()) return
  const parsed = JSON.parse(importText.value) as Record<string, unknown>
  const mode = importMode.value
  const count = (parsed.accounts as unknown[]).length
  const summary =
    mode === 'merge'
      ? `将按名称合并导入 ${count} 个账号（同名更新、新名追加），其余配置保持不变。`
      : `将整体替换当前全部配置为导入内容（共 ${count} 个账号）。此操作不可撤销！`
  dialog.warning({
    title: `确认导入（${mode === 'merge' ? '合并' : '覆盖'}）`,
    content: summary,
    positiveText: '确认导入',
    negativeText: '取消',
    onPositiveClick: async () => {
      importing.value = true
      try {
        const params: ImportParams = { config: parsed, mode }
        const res = await importConfig(params)
        message.success(`导入成功（${mode === 'merge' ? '合并' : '覆盖'}）`)
        jsonText.value = ''
        clearImport()
        // 刷新导出示意
        fetchExport()
        void res
      } catch (e) {
        message.error(extractErrorMessage(e, '导入失败'))
      } finally {
        importing.value = false
      }
    }
  })
}

onMounted(fetchExport)
</script>

<style scoped>
.export-page {
  display: flex;
  flex-direction: column;
  gap: 16px;
  max-width: 1080px;
}

.export-card,
.import-card {
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

.export-tip,
.import-tip {
  margin-bottom: 18px;
}

.export-textarea,
.import-textarea {
  font-family: 'JetBrains Mono', Consolas, 'Courier New', monospace !important;
  font-size: 12px !important;
  line-height: 1.6 !important;
}

.import-mode {
  margin-bottom: 16px;
}

.import-upload {
  margin-bottom: 12px;
}

.import-error {
  margin-top: 12px;
}

.export-actions {
  display: flex;
  gap: 12px;
  margin-top: 18px;
}
</style>
