<!--
web/src/views/HttpSettingsView.vue
页面：HTTP 配置
职责：编辑 http 模块（impersonate/timeout/verify）
数据来源：GET/PUT /api/config
-->
<template>
  <div class="page-container">
    <config-card
      title="HTTP 配置"
      description="配置 HTTP 请求行为：TLS 指纹模拟、请求超时与 SSL 证书校验。"
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
        <n-form-item label="TLS 指纹模拟 Impersonate">
          <n-select
            v-model:value="form.impersonate"
            :options="impersonateOptions"
            placeholder="选择浏览器指纹"
          />
        </n-form-item>
        <n-form-item label="请求超时（秒）">
          <n-input-number v-model:value="form.timeout" :min="1" :max="600" class="num-input" />
        </n-form-item>
        <n-form-item label="校验 SSL 证书">
          <n-switch v-model:value="form.verify" />
          <span class="switch-tip">{{ form.verify ? '开启（推荐）' : '关闭（跳过证书校验）' }}</span>
        </n-form-item>
      </n-form>
    </config-card>
  </div>
</template>

<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { NForm, NFormItem, NInputNumber, NSwitch, NSelect, useMessage, type SelectOption } from 'naive-ui'
import ConfigCard from '@/components/ConfigCard.vue'
import { useConfigStore } from '@/stores/config'
import { useDirtyGuard } from '@/composables/useDirtyGuard'
import { deepClone } from '@/utils/clone'
import { extractErrorMessage } from '@/utils/error'
import type { AppConfig, HttpConfig } from '@/types'

const configStore = useConfigStore()
const message = useMessage()

const saving = ref(false)
const initialized = ref(false)

// 脏检测：表单与已保存快照不一致时显示「未保存修改」，离开前弹确认
const savedSnapshot = ref('')
const isDirty = computed(() => JSON.stringify(form) !== savedSnapshot.value)
useDirtyGuard(() => isDirty.value)

const impersonateOptions: SelectOption[] = [
  { label: 'chrome（Chrome 指纹）', value: 'chrome' },
  { label: 'chrome110', value: 'chrome110' },
  { label: 'firefox', value: 'firefox' },
  { label: 'safari', value: 'safari' },
  { label: 'edge', value: 'edge' },
  { label: 'none（不模拟）', value: 'none' }
]

const form = reactive<HttpConfig>({
  impersonate: 'chrome',
  timeout: 20,
  verify: true
})

function initForm(cfg: AppConfig) {
  const h = cfg.http
  form.impersonate = h.impersonate
  form.timeout = h.timeout
  form.verify = h.verify
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
    next.http = { ...form }
    await configStore.save(next)
    savedSnapshot.value = JSON.stringify(form)
    message.success('HTTP 配置已保存')
  } catch (e) {
    message.error(extractErrorMessage(e, 'HTTP 配置保存失败'))
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
</style>
