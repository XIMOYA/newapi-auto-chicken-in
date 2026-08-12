<!--
web/src/views/BrowserSettingsView.vue
页面：浏览器配置
职责：编辑 browser 模块（driver/headless/humanize/timeout/keep_artifacts_on_fail/locale/window/executable_path）
- window 数组用两个 NInputNumber（宽 × 高）
数据来源：GET/PUT /api/config
-->
<template>
  <div class="page-container">
    <config-card
      title="浏览器配置"
      description="配置自动化浏览器行为：驱动类型、无头模式、窗口尺寸、超时与失败保留产物等。"
      :loading="!configStore.config"
      :saving="saving"
      :updated-at="configStore.updatedAt"
      :show-reset="true"
      compact
      :dirty="isDirty"
      @save="handleSave"
      @reset="handleReset"
    >
      <n-form label-placement="top" :show-require-mark="false" class="settings-form">
        <n-form-item label="浏览器驱动 Driver">
          <n-select
            v-model:value="form.driver"
            :options="driverOptions"
            placeholder="选择浏览器驱动"
          />
        </n-form-item>
        <n-form-item label="无头模式 Headless">
          <n-select v-model:value="form.headless" :options="headlessOptions" placeholder="选择无头模式" />
        </n-form-item>
        <n-form-item label="人性化操作">
          <n-switch v-model:value="form.humanize" />
          <span class="switch-tip">{{ form.humanize ? '开启（模拟真人操作节奏）' : '关闭' }}</span>
        </n-form-item>
        <n-form-item label="页面加载超时（秒）">
          <n-input-number v-model:value="form.timeout" :min="1" :max="600" class="num-input" />
        </n-form-item>
        <n-form-item label="失败时保留调试产物">
          <n-switch v-model:value="form.keep_artifacts_on_fail" />
          <span class="switch-tip">{{ form.keep_artifacts_on_fail ? '保留（截图/日志）' : '不保留' }}</span>
        </n-form-item>
        <n-form-item label="浏览器语言 Locale">
          <n-input v-model:value="form.locale" placeholder="例如 zh-CN" />
        </n-form-item>
        <n-form-item label="窗口尺寸（宽 × 高）">
          <div class="window-row">
            <n-input-number v-model:value="windowWidth" :min="320" :max="5120" class="num-input" />
            <span class="mul">×</span>
            <n-input-number v-model:value="windowHeight" :min="320" :max="5120" class="num-input" />
            <span class="switch-tip">px</span>
          </div>
        </n-form-item>
        <n-form-item label="浏览器可执行文件路径（可选）">
          <n-input v-model:value="form.executable_path" placeholder="留空使用默认安装路径" />
        </n-form-item>
      </n-form>
    </config-card>
  </div>
</template>

<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { NForm, NFormItem, NInput, NInputNumber, NSwitch, NSelect, useMessage, type SelectOption } from 'naive-ui'
import ConfigCard from '@/components/ConfigCard.vue'
import { useConfigStore } from '@/stores/config'
import { useDirtyGuard } from '@/composables/useDirtyGuard'
import { deepClone } from '@/utils/clone'
import { extractErrorMessage } from '@/utils/error'
import type { AppConfig, BrowserConfig } from '@/types'

const configStore = useConfigStore()
const message = useMessage()

const saving = ref(false)
const initialized = ref(false)

// 脏检测：表单与已保存快照不一致时显示「未保存修改」，离开前弹确认
const savedSnapshot = ref('')
const isDirty = computed(() => JSON.stringify(form) !== savedSnapshot.value)
useDirtyGuard(() => isDirty.value)

const driverOptions: SelectOption[] = [
  { label: 'camoufox（反指纹浏览器，推荐）', value: 'camoufox' },
  { label: 'patchright（无头浏览器备选）', value: 'patchright' }
]

const headlessOptions: SelectOption[] = [
  { label: 'virtual（虚拟显示）', value: 'virtual' },
  { label: 'true（无头）', value: 'true' },
  { label: 'false（有头，显示窗口）', value: 'false' }
]

const form = reactive<BrowserConfig>({
  driver: 'camoufox',
  headless: 'virtual',
  humanize: true,
  timeout: 60,
  keep_artifacts_on_fail: true,
  locale: 'zh-CN',
  window: [1280, 800],
  executable_path: null
})

const windowWidth = computed({
  get: () => form.window[0],
  set: (v) => {
    form.window[0] = v ?? 1280
  }
})
const windowHeight = computed({
  get: () => form.window[1],
  set: (v) => {
    form.window[1] = v ?? 800
  }
})

function initForm(cfg: AppConfig) {
  const b = cfg.browser
  form.driver = b.driver
  form.headless = b.headless
  form.humanize = b.humanize
  form.timeout = b.timeout
  form.keep_artifacts_on_fail = b.keep_artifacts_on_fail
  form.locale = b.locale
  form.window = [b.window?.[0] ?? 1280, b.window?.[1] ?? 800]
  form.executable_path = b.executable_path ?? null
  savedSnapshot.value = JSON.stringify(form)
}

watch(
  () => configStore.config,
  (cfg) => {
    if (cfg && !initialized.value) {
      initForm(cfg)
      initialized.value = true
    }
  },
  { immediate: true }
)

function handleReset() {
  if (configStore.config) initForm(configStore.config)
}

async function handleSave() {
  if (!configStore.config) return
  saving.value = true
  try {
    const next = deepClone(configStore.config)
    next.browser = {
      ...form,
      window: [form.window[0], form.window[1]],
      executable_path: form.executable_path && form.executable_path.trim() !== '' ? form.executable_path.trim() : null
    }
    await configStore.save(next)
    savedSnapshot.value = JSON.stringify(form)
    message.success('浏览器配置已保存')
  } catch (e) {
    message.error(extractErrorMessage(e, '浏览器配置保存失败'))
  } finally {
    saving.value = false
  }
}
</script>

<style scoped>
.switch-tip {
  margin-left: 10px;
  font-size: 13px;
  color: #8492a6;
}

.settings-form {
  max-width: 560px;
}

.num-input {
  width: 200px;
}

.window-row {
  display: flex;
  align-items: center;
  gap: 10px;
}

.mul {
  color: #8492a6;
}
</style>
