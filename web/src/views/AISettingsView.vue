<!--
web/src/views/AISettingsView.vue
页面：AI 配置
职责：编辑 ai 模块（enabled/base_url/api_key/model/timeout/max_retries）
- api_key 为打码字段（"***"）：显示「已设置」，留空保持不变，输入新值可修改
数据来源：GET/PUT /api/config
-->
<template>
  <div class="page-container">
    <config-card
      title="AI 配置"
      description="配置 AI 能力模块：用于自动化签到流程中的智能判断与处理（如打码、验证码识别等）。"
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
        <n-form-item label="启用 AI 模块">
          <n-switch v-model:value="form.enabled" />
          <span class="switch-tip">{{ form.enabled ? '已启用' : '已停用' }}</span>
        </n-form-item>
        <n-form-item label="接口地址 Base URL">
          <n-input v-model:value="form.base_url" placeholder="例如 https://你的站点.com/v1" />
        </n-form-item>
        <n-form-item label="API Key">
          <masked-input v-model="form.api_key" :original-value="originalApiKey" placeholder="sk-xxx" />
        </n-form-item>
        <n-form-item label="模型 Model">
          <n-input v-model:value="form.model" placeholder="例如 gpt-4o-mini" />
        </n-form-item>
        <n-form-item label="超时时间（秒）">
          <n-input-number v-model:value="form.timeout" :min="1" :max="600" class="num-input" />
        </n-form-item>
        <n-form-item label="最大重试次数">
          <n-input-number v-model:value="form.max_retries" :min="0" :max="20" class="num-input" />
        </n-form-item>
      </n-form>
    </config-card>
  </div>
</template>

<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { NForm, NFormItem, NInput, NInputNumber, NSwitch, useMessage } from 'naive-ui'
import ConfigCard from '@/components/ConfigCard.vue'
import MaskedInput from '@/components/MaskedInput.vue'
import { useConfigStore } from '@/stores/config'
import { useDirtyGuard } from '@/composables/useDirtyGuard'
import { deepClone } from '@/utils/clone'
import { extractErrorMessage } from '@/utils/error'
import type { AIConfig, AppConfig } from '@/types'

const configStore = useConfigStore()
const message = useMessage()

const saving = ref(false)
const initialized = ref(false)
const originalApiKey = ref('')

// 脏检测：表单与已保存快照不一致时显示「未保存修改」，离开前弹确认
const savedSnapshot = ref('')
const isDirty = computed(() => JSON.stringify(form) !== savedSnapshot.value)
useDirtyGuard(() => isDirty.value)

const form = reactive<AIConfig>({
  enabled: false,
  base_url: '',
  api_key: '',
  model: '',
  timeout: 60,
  max_retries: 2
})

function initForm(cfg: AppConfig) {
  const ai = cfg.ai
  form.enabled = ai.enabled
  form.base_url = ai.base_url
  form.api_key = ai.api_key
  form.model = ai.model
  form.timeout = ai.timeout
  form.max_retries = ai.max_retries
  originalApiKey.value = ai.api_key
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
    next.ai = {
      ...form,
      // 打码字段：留空表示不修改，回退为原始值（"***" 或原值）
      api_key: form.api_key === '' ? originalApiKey.value : form.api_key
    }
    await configStore.save(next)
    savedSnapshot.value = JSON.stringify(form)
    message.success('AI 配置已保存')
  } catch (e) {
    message.error(extractErrorMessage(e, 'AI 配置保存失败'))
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
