<!--
web/src/views/DefaultsSettingsView.vue
页面：全局默认配置
职责：编辑 defaults 模块（retry/interval_seconds），interval_seconds 用两个 NInputNumber
数据来源：GET/PUT /api/config
-->
<template>
  <div class="page-container">
    <config-card
      title="全局默认配置"
      description="配置签到任务的全局默认值：失败重试次数、每次签到之间的随机间隔（秒）区间。"
      :loading="!configStore.config"
      :saving="saving"
      :updated-at="configStore.updatedAt"
      :show-reset="true"
      compact
      @save="handleSave"
      @reset="handleReset"
    >
      <n-form label-placement="top" :show-require-mark="false" class="settings-form">
        <n-form-item label="失败重试次数 Retry">
          <n-input-number v-model:value="form.retry" :min="0" :max="20" class="num-input" />
        </n-form-item>
        <n-form-item label="签到间隔（秒）— 最小值">
          <n-input-number v-model:value="intervalMin" :min="0" :max="3600" class="num-input" />
        </n-form-item>
        <n-form-item label="签到间隔（秒）— 最大值">
          <n-input-number v-model:value="intervalMax" :min="0" :max="3600" class="num-input" />
          <span class="switch-tip">实际间隔在 [min, max] 区间内随机</span>
        </n-form-item>
      </n-form>
    </config-card>
  </div>
</template>

<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { NForm, NFormItem, NInputNumber, useMessage } from 'naive-ui'
import ConfigCard from '@/components/ConfigCard.vue'
import { useConfigStore } from '@/stores/config'
import { deepClone } from '@/utils/clone'
import { extractErrorMessage } from '@/utils/error'
import type { AppConfig, DefaultsConfig } from '@/types'

const configStore = useConfigStore()
const message = useMessage()

const saving = ref(false)
const initialized = ref(false)

const form = reactive<DefaultsConfig>({
  retry: 2,
  interval_seconds: [3, 8]
})

const intervalMin = computed({
  get: () => form.interval_seconds[0],
  set: (v) => {
    form.interval_seconds[0] = v ?? 3
  }
})
const intervalMax = computed({
  get: () => form.interval_seconds[1],
  set: (v) => {
    form.interval_seconds[1] = v ?? 8
  }
})

function initForm(cfg: AppConfig) {
  const d = cfg.defaults
  form.retry = d.retry
  form.interval_seconds = [d.interval_seconds?.[0] ?? 3, d.interval_seconds?.[1] ?? 8]
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
  const [min, max] = form.interval_seconds
  if (min > max) {
    message.warning('签到间隔最小值不能大于最大值')
    return
  }
  saving.value = true
  try {
    const next = deepClone(configStore.config)
    next.defaults = { ...form }
    await configStore.save(next)
    message.success('全局默认配置已保存')
  } catch (e) {
    message.error(extractErrorMessage(e, '全局默认配置保存失败'))
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
