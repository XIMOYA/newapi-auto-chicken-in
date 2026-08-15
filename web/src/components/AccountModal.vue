<!--
web/src/components/AccountModal.vue
组件：签到账号 新增/编辑 弹窗
职责：
- 字段：name/url/cookie/user_id/proxy/checkin_path/browser_path/enabled
- cookie 打码处理：服务端返回 "***" 时显示「已设置」，留空则提交原值
- 表单校验：名称必填、URL 必须 http(s) 开头、用户 ID 为可选安全整数
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
      <n-form-item label="选择站点" path="site_key">
        <n-select
          v-model:value="siteKey"
          :options="siteOptions"
          placeholder="可从站点预设选择（也可手动输入 URL）"
          clearable
          filterable
          :disabled="isEdit"
          @update:value="applySite"
        />
      </n-form-item>
      <n-form-item label="账号名称" path="name">
        <n-input v-model:value="form.name" placeholder="例如：站点A" />
      </n-form-item>
      <n-form-item label="站点 URL" path="url">
        <n-input v-model:value="form.url" placeholder="https://newapi.example.com" />
      </n-form-item>
      <n-form-item label="Cookie" path="cookie">
        <div class="cookie-field">
          <masked-input
            v-model="form.cookie"
            :original-value="originalCookie"
            type="textarea"
            :autosize="{ minRows: 2, maxRows: 6 }"
            placeholder="粘贴完整 Cookie（如 session=...）"
            :custom-tip="isEdit ? '该账号 Cookie 已设置（出于安全原因接口不回传明文），留空保持不变，输入新值可修改' : 'Cookie 可稍后补充；未设置时该账号无法依赖 Cookie 完成签到'"
          />
          <n-button
            v-if="isEdit && isMaskedCookie"
            size="tiny"
            secondary
            type="info"
            class="reveal-btn"
            @click="passwordConfirmVisible = true"
          >
            <template #icon><n-icon><eye-outline /></n-icon></template>
            查看明文
          </n-button>
        </div>
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

  <password-confirm-modal
    v-model:show="passwordConfirmVisible"
    :loading="revealing"
    @confirm="handleRevealCookie"
  />
  <cookie-reveal-modal v-model:show="revealVisible" :cookie="revealedCookie" :account-name="form.name" />
</template>

<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { NForm, NFormItem, NInput, NModal, NSwitch, NButton, NSelect, NIcon, useMessage, type FormInst, type FormRules, type SelectOption } from 'naive-ui'
import { EyeOutline } from '@vicons/ionicons5'
import MaskedInput from './MaskedInput.vue'
import PasswordConfirmModal from './PasswordConfirmModal.vue'
import CookieRevealModal from './CookieRevealModal.vue'
import { verifyPassword } from '@/api/auth'
import { exportConfig } from '@/api/export'
import { extractErrorMessage } from '@/utils/error'
import type { Account, Site } from '@/types'

const props = defineProps<{
  show: boolean
  /** 编辑对象；null 表示新增 */
  account: Account | null
  /** 站点预设列表（供新增时选择自动带出路径） */
  sites?: Site[]
  submitting?: boolean
}>()

const emit = defineEmits<{
  (e: 'update:show', value: boolean): void
  (e: 'submit', payload: Account): void
}>()

const isEdit = computed(() => props.account !== null)

// 站点预设下拉选项
const siteOptions = computed<SelectOption[]>(() =>
  (props.sites ?? []).map((s, i) => ({
    label: `${s.name}（${s.url}）`,
    value: i
  }))
)
const siteKey = ref<number | null>(null)

/** 选择站点预设：自动带出 URL / 签到路径 / 浏览器路径（用户可改） */
function applySite(index: number | null) {
  if (index == null) return
  const site = (props.sites ?? [])[index]
  if (!site) return
  form.url = site.url
  if (site.checkin_path) form.checkin_path = site.checkin_path
  if (site.browser_path) form.browser_path = site.browser_path
}

// 从现有账号/预设提取的公共路径（供手动输入 URL 时兜底默认）
const commonCheckinPath = computed(() => {
  const fromSites = (props.sites ?? []).map((s) => s.checkin_path).filter((p): p is string => !!p)
  const candidates = [...fromSites]
  if (candidates.length) {
    const counts = new Map<string, number>()
    for (const c of candidates) counts.set(c, (counts.get(c) ?? 0) + 1)
    return [...counts.entries()].sort((a, b) => b[1] - a[1])[0][0]
  }
  return ''
})

/** 手动输入 URL 时：匹配站点预设带出路径；否则用最常见的 checkin_path 兜底 */
function watchUrlInput(value: string) {
  const url = value.trim()
  if (!url) return
  const matched = (props.sites ?? []).find((s) => s.url.replace(/\/+$/, '') === url.replace(/\/+$/, ''))
  if (matched) {
    if (matched.checkin_path) form.checkin_path = matched.checkin_path
    if (matched.browser_path) form.browser_path = matched.browser_path
  } else if (!form.checkin_path && commonCheckinPath.value) {
    form.checkin_path = commonCheckinPath.value
  }
}

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
      siteKey.value = null
    } else {
      form.name = ''
      form.url = ''
      form.cookie = ''
      form.user_id = ''
      form.proxy = ''
      form.checkin_path = ''
      form.browser_path = ''
      form.enabled = true
      siteKey.value = null
    }
    formRef.value?.restoreValidation()
  }
)

// 手动输入 URL 时尝试带出路径（新增模式下）
watch(
  () => form.url,
  (v) => {
    if (!isEdit.value) watchUrlInput(v)
  }
)

// ---- 查看明文 Cookie（二次确认）----
const message = useMessage()
const passwordConfirmVisible = ref(false)
const revealing = ref(false)
const revealVisible = ref(false)
const revealedCookie = ref('')

/** 编辑且 Cookie 是打码占位时，才显示「查看明文」按钮 */
const isMaskedCookie = computed(() => originalCookie.value !== '' && form.cookie === '***')

async function handleRevealCookie(password: string) {
  revealing.value = true
  try {
    await verifyPassword(password)
    // 密码确认通过 → 拉取完整明文配置，取当前账号的 cookie
    const res = await exportConfig()
    const cfg = JSON.parse(res.json) as { accounts?: Account[] }
    const target = (cfg.accounts ?? []).find((a) => a.name === props.account?.name)
    if (!target || !target.cookie) {
      message.error('未找到该账号的 Cookie')
      passwordConfirmVisible.value = false
      return
    }
    revealedCookie.value = target.cookie
    passwordConfirmVisible.value = false
    revealVisible.value = true
  } catch (e) {
    message.error(extractErrorMessage(e, '密码验证失败'))
  } finally {
    revealing.value = false
  }
}

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
  user_id: {
    validator: (_rule, value: string) => {
      const text = value.trim()
      if (!text) return true
      if (!/^-?\d+$/.test(text) || !Number.isSafeInteger(Number(text))) {
        return new Error('用户 ID 必须是安全整数')
      }
      return true
    },
    trigger: ['input', 'blur']
  }
}

function normalizeUserID(v: string): number | null {
  const text = v.trim()
  return text ? Number(text) : null
}

// 提交时补充 path 校验：不强制，但若是 "***" 也视为未设置
function normalizePath(v: string) {
  const t = v.trim()
  if (!t || t === '***') return null
  return t.startsWith('/') ? t : '/' + t
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
      user_id: normalizeUserID(form.user_id),
      proxy: form.proxy.trim() === '' ? null : form.proxy.trim(),
      checkin_path: normalizePath(form.checkin_path),
      browser_path: normalizePath(form.browser_path),
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

.cookie-field {
  width: 100%;
}

.reveal-btn {
  margin-top: 8px;
}
</style>
