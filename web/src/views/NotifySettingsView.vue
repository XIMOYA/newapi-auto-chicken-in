<!--
web/src/views/NotifySettingsView.vue
页面：邮件通知配置
职责：编辑 notify.email 模块（enabled/smtp_host/smtp_port/use_ssl/username/password/from_addr/to_addrs/subject_prefix/timeout）
- password 为打码字段（"***"）：显示「已设置」，留空保持不变，输入新值可修改
- to_addrs 为动态列表（增删）
数据来源：GET/PUT /api/config
-->
<template>
  <div class="page-container">
    <config-card
      title="邮件通知配置"
      description="配置签到日报邮件通知：SMTP 服务信息、收件人列表与邮件主题前缀。"
      :loading="!configStore.config"
      :saving="saving"
      :updated-at="configStore.updatedAt"
      :show-reset="true"
      compact
      @save="handleSave"
      @reset="handleReset"
    >
      <n-form label-placement="top" :show-require-mark="false" class="settings-form">
        <n-form-item label="启用邮件通知">
          <n-switch v-model:value="form.enabled" />
          <span class="switch-tip">{{ form.enabled ? '已启用' : '已停用' }}</span>
        </n-form-item>
        <n-form-item label="SMTP 服务器">
          <n-input v-model:value="form.smtp_host" placeholder="例如 smtp.aliyun.com" />
        </n-form-item>
        <n-form-item label="SMTP 端口">
          <n-input-number v-model:value="form.smtp_port" :min="1" :max="65535" class="num-input" />
        </n-form-item>
        <n-form-item label="使用 SSL/TLS">
          <n-switch v-model:value="form.use_ssl" />
          <span class="switch-tip">{{ form.use_ssl ? 'SSL（推荐，如 465 端口）' : '普通连接（如 25/587 端口）' }}</span>
        </n-form-item>
        <n-form-item label="发件账号">
          <n-input v-model:value="form.username" placeholder="发件邮箱账号" />
        </n-form-item>
        <n-form-item label="SMTP 授权码 / 密码">
          <masked-input
            v-model="form.password"
            :original-value="originalPassword"
            type="password"
            placeholder="SMTP 授权码或密码"
            custom-tip="邮箱密码已设置（出于安全原因接口不回传明文），留空保持不变，输入新值可修改"
          />
        </n-form-item>
        <n-form-item label="发件人地址 From">
          <n-input v-model:value="form.from_addr" placeholder="发件人邮箱地址" />
        </n-form-item>
        <n-form-item label="收件人列表 To">
          <dynamic-string-list
            v-model="form.to_addrs"
            placeholder="收件人邮箱地址"
            add-label="添加收件人"
          />
        </n-form-item>
        <n-form-item label="邮件主题前缀">
          <n-input v-model:value="form.subject_prefix" placeholder="例如 NewAPI 签到日报" />
        </n-form-item>
        <n-form-item label="发送超时（秒）">
          <n-input-number v-model:value="form.timeout" :min="1" :max="600" class="num-input" />
        </n-form-item>
      </n-form>
    </config-card>
  </div>
</template>

<script setup lang="ts">
import { reactive, ref, watch } from 'vue'
import { NForm, NFormItem, NInput, NInputNumber, NSwitch, useMessage } from 'naive-ui'
import ConfigCard from '@/components/ConfigCard.vue'
import MaskedInput from '@/components/MaskedInput.vue'
import DynamicStringList from '@/components/DynamicStringList.vue'
import { useConfigStore } from '@/stores/config'
import { deepClone } from '@/utils/clone'
import { extractErrorMessage } from '@/utils/error'
import type { AppConfig, NotifyEmailConfig } from '@/types'

const configStore = useConfigStore()
const message = useMessage()

const saving = ref(false)
const initialized = ref(false)
const originalPassword = ref('')

const form = reactive<NotifyEmailConfig>({
  enabled: false,
  smtp_host: '',
  smtp_port: 465,
  use_ssl: true,
  username: '',
  password: '',
  from_addr: '',
  to_addrs: [],
  subject_prefix: 'NewAPI 签到日报',
  timeout: 20
})

function initForm(cfg: AppConfig) {
  const e = cfg.notify?.email
  if (!e) return
  form.enabled = e.enabled
  form.smtp_host = e.smtp_host
  form.smtp_port = e.smtp_port
  form.use_ssl = e.use_ssl
  form.username = e.username
  form.password = e.password
  form.from_addr = e.from_addr
  form.to_addrs = [...(e.to_addrs ?? [])]
  form.subject_prefix = e.subject_prefix
  form.timeout = e.timeout
  originalPassword.value = e.password
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
    next.notify = {
      email: {
        ...form,
        // 打码字段：留空表示不修改，回退为原始值（"***" 或原值）
        password: form.password === '' ? originalPassword.value : form.password,
        to_addrs: form.to_addrs.map((t) => t.trim()).filter((t) => t !== '')
      }
    }
    await configStore.save(next)
    message.success('邮件通知配置已保存')
  } catch (e) {
    message.error(extractErrorMessage(e, '邮件通知配置保存失败'))
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
