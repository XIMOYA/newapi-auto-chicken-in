<!--
web/src/views/ExportView.vue
页面：配置导出 / 导入
职责：
- 导出：密码确认后展示完整 config.json 明文（textarea）+ 一键复制；离开页面即清空
- 导入：粘贴 JSON 或上传文件 → 选择模式（覆盖 / 合并）→ 确认后提交
  - overwrite：整体覆盖当前配置
  - merge：账号/站点按 name 合并（同名更新、新名追加），其余模块保留
数据来源：POST /api/auth/verify-password（换一次性票据）；GET /api/export；POST /api/config/import
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
        导出完整明文 config.json（含 Cookie 等敏感信息）。GitHub Actions 推荐将其转换为
        base64 后保存到 Secret <n-text code>CONFIG_JSON_B64</n-text>，避免 GitHub 对结构化 JSON
        自动脱敏时误把账号名、IP、user_id 或普通数字替换为星号；<n-text code>CONFIG_JSON</n-text>
        仍作为兼容兜底。
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
        <n-button type="primary" :loading="loading" @click="requestExport">
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
        导入将写入配置（含 Cookie 等敏感信息）。请粘贴或上传一份完整的
        <n-text code>config.json</n-text>（可用上方「导出」功能获得）。
        <b>覆盖模式</b>会整体替换当前配置；<b>合并模式</b>可勾选要导入的模块，
        勾选哪些就更新哪些，未勾选的保持不变。
      </n-alert>

      <n-radio-group v-model:value="importMode" class="import-mode">
        <n-radio-button value="merge">合并（选择模块）</n-radio-button>
        <n-radio-button value="overwrite">覆盖（整体替换）</n-radio-button>
      </n-radio-group>

      <!-- 合并模式的模块勾选 -->
      <div v-if="importMode === 'merge'" class="module-check">
        <div class="module-check-title">选择要导入的模块：</div>
        <n-checkbox-group v-model:value="selectedModules" class="module-check-group">
          <n-checkbox v-for="mod in availableModules" :key="mod.key" :value="mod.key" class="module-check-item">
            {{ mod.label }}
          </n-checkbox>
        </n-checkbox-group>
        <div class="module-check-actions">
          <n-button size="tiny" quaternary @click="selectAllModules">全选</n-button>
          <n-button size="tiny" quaternary @click="selectedModules = []">清空</n-button>
        </div>
      </div>

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

    <password-confirm-modal
      v-model:show="passwordConfirmVisible"
      :loading="loading"
      tip="导出内容包含全部站点 Cookie、AI api_key 与邮箱密码等明文，请输入管理员密码确认身份。"
      @confirm="fetchExport"
    />
  </div>
</template>

<script setup lang="ts">
import { computed, onUnmounted, ref } from 'vue'
import {
  NCard, NButton, NIcon, NInput, NTag, NAlert, NText, NSpin, NRadioGroup, NRadioButton, NUpload,
  NCheckbox, NCheckboxGroup, useDialog, useMessage
} from 'naive-ui'
import {
  RefreshOutline, CopyOutline, InformationCircleOutline, WarningOutline,
  CloudUploadOutline, AlertCircleOutline, DownloadOutline
} from '@vicons/ionicons5'
import PasswordConfirmModal from '@/components/PasswordConfirmModal.vue'
import { exportConfig, importConfig } from '@/api/export'
import { verifyPassword } from '@/api/auth'
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

/** 全部可导入模块（与后端 configModuleKeys 对齐） */
const moduleOptions: { key: string; label: string }[] = [
  { key: 'accounts', label: '账号' },
  { key: 'sites', label: '站点预设' },
  { key: 'ai', label: 'AI 配置' },
  { key: 'browser', label: '浏览器配置' },
  { key: 'http', label: 'HTTP 配置' },
  { key: 'defaults', label: '全局默认' },
  { key: 'proxy_pool', label: '代理池' },
  { key: 'notify', label: '邮件通知' },
  { key: 'config_sync', label: '配置同步' },
  { key: 'security', label: '安全' }
]

/** 当前导入 JSON 中实际存在的模块（用于勾选展示） */
const availableModules = ref<{ key: string; label: string }[]>([])
const selectedModules = ref<string[]>([])

function selectAllModules() {
  selectedModules.value = availableModules.value.map((m) => m.key)
}

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

// 导出的是全量明文（所有站点 Cookie、api_key、SMTP 密码），因此不再进页面就自动拉，
// 改为点一下、验一次密码换取一次性票据后才展示 —— 与「查看单个 Cookie」的保护强度对齐
const passwordConfirmVisible = ref(false)

function requestExport() {
  passwordConfirmVisible.value = true
}

async function fetchExport(password: string) {
  loading.value = true
  try {
    const { ticket } = await verifyPassword(password)
    const res = await exportConfig(ticket)
    jsonText.value = res.json
    passwordConfirmVisible.value = false
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
    // 依据 JSON 中实际存在的模块更新勾选列表（默认全选）
    availableModules.value = moduleOptions.filter((m) => m.key in obj)
    selectedModules.value = availableModules.value.map((m) => m.key)
    return true
  } catch {
    parseError.value = 'JSON 解析失败，请检查格式'
    return false
  }
}

function clearImport() {
  importText.value = ''
  parseError.value = ''
  availableModules.value = []
  selectedModules.value = []
}

function handleImport() {
  if (!validateImport()) return
  const parsed = JSON.parse(importText.value) as Record<string, unknown>
  const mode = importMode.value
  const count = (parsed.accounts as unknown[]).length
  let summary: string
  if (mode === 'merge') {
    const picked = selectedModules.value
    if (picked.length === 0) {
      message.warning('请至少勾选一个要导入的模块')
      return
    }
    const labels = picked.map((k) => moduleOptions.find((m) => m.key === k)?.label ?? k)
    summary = `将按名称合并导入以下模块：${labels.join('、')}（共 ${count} 个账号）。未勾选的模块保持不变。`
  } else {
    summary = `将整体替换当前全部配置为导入内容（共 ${count} 个账号）。此操作不可撤销！`
  }
  dialog.warning({
    title: `确认导入（${mode === 'merge' ? '合并' : '覆盖'}）`,
    content: summary,
    positiveText: '确认导入',
    negativeText: '取消',
    onPositiveClick: async () => {
      importing.value = true
      try {
        const params: ImportParams = {
          config: parsed,
          mode,
          modules: mode === 'merge' ? selectedModules.value : undefined
        }
        const res = await importConfig(params)
        message.success(`导入成功（${mode === 'merge' ? '合并' : '覆盖'}）`)
        jsonText.value = ''
        clearImport()
        // 导入成功后不自动重拉明文（那会再要一次密码确认），需要时手动点「获取配置」
        jsonText.value = ''
        void res
      } catch (e) {
        message.error(extractErrorMessage(e, '导入失败'))
      } finally {
        importing.value = false
      }
    }
  })
}

// 离开页面时清掉明文，别让它一直留在 DOM 与内存里
onUnmounted(() => {
  jsonText.value = ''
})
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

.module-check {
  margin-bottom: 16px;
  padding: 12px 14px;
  background: #f7f8fb;
  border-radius: 8px;
}

.module-check-title {
  font-size: 13px;
  color: #48566a;
  margin-bottom: 10px;
}

.module-check-group {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 18px;
}

.module-check-item {
  margin-right: 0;
}

.module-check-actions {
  margin-top: 8px;
  display: flex;
  gap: 4px;
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
