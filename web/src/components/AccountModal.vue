<!--
web/src/components/AccountModal.vue
组件：签到账号 新增/编辑 弹窗
职责：
- 字段：name/url/cookie/user_id/proxy/checkin_path/browser_path/enabled
- cookie 打码处理：服务端返回 "***" 时显示「已设置」，留空则提交原值
- 表单校验：名称必填、URL 必须 http(s) 开头、新增时 cookie 必填
数据来源：父组件传入 account（null 表示新增）
-->
<template>
  <n-modal
    :show="show"
    preset="card"
    :title="isEdit ? '编辑签到账号' : '新增签到账号'"
    style="width: 640px"
    :mask-closable="false"
    transition-preset="fade-in-scale-up"
    @update:show="(v: boolean) => emit('update:show', v)"
  >
    <n-form ref="formRef" :model="form" :rules="rules" label-placement="left" :label-width="110">
      <n-form-item label="账号名称" path="name">
        <n-input v-model:value="form.name" placeholder="例如：站点A" />
      </n-form-item>
      <n-form-item label="站点 URL" path="url">
        <n-input v-model:value="form.url" placeholder="https://newapi.example.com" />
      </n-form-item>
      <n-form-item label="Cookie" path="cookie">
        <masked-input
          v-model="form.cookie"
          :original-value="originalCookie"
          type="textarea"
          :autosize="{ minRows: 2, maxRows: 6 }"
          placeholder="粘贴完整 Cookie（如 session=...）"
          custom-tip="该账号 Cookie 已设置（出于安全原因接口不回传明文），留空保持不变，输入新值可修改"
        />
      </n-form-item>
      <n-form-item label="用户 ID" path="user_id">
        <n-input v-model:value="form.user_id" placeholder="可选，留空自动识别" />
      </n-form-item>
      <n-form-item label="手动隧道(proxy)" path="proxy">
        <n-input v-model:value="form.proxy" placeholder="可选，例如 http://127.0.0.1:7890" />
      </n-form-item>
      <n-form-item label="签到路径" path="checkin_path">
        <n-input v-model:value="form.checkin_path" placeholder="可选，例如 /user/checkin" />
      </n-form-item>
      <n-form-item label="浏览器路径" path="browser_path">
        <n-input v-model:value="form.browser_path" placeholder="可选，例如 /dashboard" />
      </n-form-item>
      <n-form-item label="启用" path="enabled">
        <n-switch v-model:value="form.enabled" />
      </n-form-item>
    </n-form>
    <template #footer>
      <div class="modal-footer">
        <n-button @click="emit('update:show', false)">取消</n-button>
        <n-button type="primary" :loading="submitting" @click="handleSubmit">保存</n-button>
      </div>
    </template>
  </n-modal>
</template>

<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { NForm, NFormItem, NInput, NModal, NSwitch, NButton, type FormInst, type FormRules } from 'naive-ui'
import MaskedInput from './MaskedInput.vue'
import type { Account } from '@/types'

const props = defineProps<{
  show: boolean
  /** 编辑对象；null 表示新增 */
  account: Account | null
  submitting?: boolean
}>()

const emit = defineEmits<{
  (e: 'update:show', value: boolean): void
  (e: 'submit', payload: Account): void
}>()

const isEdit = computed(() => props.account !== null)

// 服务端返回的原始 cookie（可能是 "***"），用于打码判断与「留空保持不变」
const originalCookie = computed(() => (props.account ? props.account.cookie : ''))

interface AccountForm {
  name: string
  url: string
  cookie: string
  user_id: string
  proxy: string
  checkin_path: string
  browser_path: string
  enabled: boolean
}

const formRef = ref<FormInst | null>(null)
const form = reactive<AccountForm>({
  name: '',
  url: '',
  cookie: '',
  user_id: '',
  proxy: '',
  checkin_path: '',
  browser_path: '',
  enabled: true
})

watch(
  () => props.show,
  (visible) => {
    if (!visible) return
    if (props.account) {
      form.name = props.account.name
      form.url = props.account.url
      form.cookie = props.account.cookie // 可能是 "***"
      form.user_id = props.account.user_id == null ? '' : String(props.account.user_id)
      form.proxy = props.account.proxy ?? ''
      form.checkin_path = props.account.checkin_path ?? ''
      form.browser_path = props.account.browser_path ?? ''
      form.enabled = props.account.enabled
    } else {
      form.name = ''
      form.url = ''
      form.cookie = ''
      form.user_id = ''
      form.proxy = ''
      form.checkin_path = ''
      form.browser_path = ''
      form.enabled = true
    }
    formRef.value?.restoreValidation()
  }
)

const rules: FormRules = {
  name: { required: true, message: '请输入账号名称', trigger: ['input', 'blur'] },
  url: [
    { required: true, message: '请输入站点 URL', trigger: ['input', 'blur'] },
    {
      validator: (_rule, value: string) => {
        if (!value) return true
        return /^https?:\/\//i.test(value) ? true : new Error('URL 必须以 http:// 或 https:// 开头')
      },
      trigger: ['input', 'blur']
    }
  ],
  cookie: {
    validator: (_rule, value: string) => {
      // 编辑时 cookie 留空表示「保留原值」；新增时必填
      if (isEdit.value && value === '') return true
      if (value === '') return new Error('请输入 Cookie（新增账号必填）')
      return true
    },
    trigger: ['blur', 'change']
  }
}

function handleSubmit() {
  formRef.value?.validate((errors) => {
    if (errors) return
    // cookie：编辑时留空 → 提交原值（"***" 或原明文），保持服务端「原样保留」语义
    const finalCookie = isEdit.value && form.cookie === '' ? originalCookie.value : form.cookie
    const payload: Account = {
      name: form.name.trim(),
      url: form.url.trim(),
      cookie: finalCookie,
      user_id: form.user_id.trim() === '' ? null : form.user_id.trim(),
      proxy: form.proxy.trim() === '' ? null : form.proxy.trim(),
      checkin_path: form.checkin_path.trim() === '' ? null : form.checkin_path.trim(),
      browser_path: form.browser_path.trim() === '' ? null : form.browser_path.trim(),
      enabled: form.enabled
    }
    emit('submit', payload)
  })
}
</script>

<style scoped>
.modal-footer {
  display: flex;
  justify-content: flex-end;
  gap: 12px;
}
</style>
