<!--
web/src/components/AccountModal.vue
组件：签到账号 新增/编辑 弹窗
职责：
- 字段：name/url/login_method/cookie/github_user_session/user_id/proxy/checkin_path/browser_path/enabled
- 凭据按登录方式取不同含义：newapi_cookie 存站点 Cookie 头，tabiai 存 new_api_refresh 的值
- github_user_session 不再是登录凭据，只作签发 new_api_refresh 的原料，因此仅在 tabiai 下出现
- tabiai 可直接在弹窗里一键签发凭据（服务端走 GitHub OAuth 换新 new_api_refresh 并落库）
- cookie 打码处理：服务端返回 "***" 时显示「已设置」，留空则提交原值
- 表单校验：名称必填、URL 必须 http(s) 开头、用户 ID 为可选安全整数
数据来源：
- 父组件传入 account（null 表示新增）
- POST /api/tabiai/issue-cookie（一键签发凭据）
- POST /api/auth/verify-password + GET /api/export（查看明文凭据）
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
      <n-form-item label="登录方式" path="login_method">
        <n-select v-model:value="form.login_method" :options="loginMethodOptions" />
      </n-form-item>
      <n-form-item :label="cookieFieldLabel" path="cookie">
        <div class="cookie-field">
          <masked-input
            v-model="form.cookie"
            :original-value="originalCookie"
            type="textarea"
            :autosize="{ minRows: 2, maxRows: 6 }"
            :placeholder="cookiePlaceholder"
            :custom-tip="cookieTip"
          />
          <n-space v-if="isEdit" :size="8" class="cookie-actions">
            <n-button
              v-if="isMaskedCookie"
              size="tiny"
              secondary
              type="info"
              @click="openReveal('cookie')"
            >
              <template #icon><n-icon><eye-outline /></n-icon></template>
              查看明文
            </n-button>
            <n-button
              v-if="isTabiAI"
              size="tiny"
              secondary
              type="primary"
              :loading="issuing"
              @click="handleIssueCookie"
            >
              <template #icon><n-icon><key-outline /></n-icon></template>
              一键签发凭据
            </n-button>
          </n-space>
          <div v-if="isTabiAI && !isEdit" class="field-note">
            先保存账号，再打开编辑弹窗即可用「一键签发凭据」自动换一条 new_api_refresh。
          </div>
        </div>
      </n-form-item>
      <n-form-item v-if="isTabiAI" label="GitHub user_session" path="github_user_session">
        <div class="cookie-field">
          <masked-input
            v-model="form.github_user_session"
            :original-value="originalGithubUserSession"
            type="textarea"
            :autosize="{ minRows: 2, maxRows: 6 }"
            placeholder="可选，粘贴 GitHub user_session Cookie 值"
            :custom-tip="githubSessionTip"
          />
          <n-button
            v-if="isEdit && isMaskedGithubUserSession"
            size="tiny"
            secondary
            type="info"
            class="reveal-btn"
            @click="openReveal('github_user_session')"
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
        <div class="field-col">
          <n-input v-model:value="form.proxy" placeholder="可选，例如 http://127.0.0.1:7890" />
          <div class="field-note">
            该字段不打码，会以明文回传给已登录管理员；含账号密码的代理建议改用 IP 白名单授权。
          </div>
        </div>
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
import { NForm, NFormItem, NInput, NModal, NSwitch, NButton, NSelect, NIcon, NSpace, useDialog, useMessage, type FormInst, type FormRules, type SelectOption } from 'naive-ui'
import { EyeOutline, KeyOutline } from '@vicons/ionicons5'
import MaskedInput from './MaskedInput.vue'
import PasswordConfirmModal from './PasswordConfirmModal.vue'
import CookieRevealModal from './CookieRevealModal.vue'
import { verifyPassword } from '@/api/auth'
import { issueTabiAICookie } from '@/api/cookieTests'
import { exportConfig } from '@/api/export'
import { extractErrorMessage } from '@/utils/error'
import type { Account, LoginMethod, Site } from '@/types'

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
  /** 服务端已直接改写该账号的 cookie（一键签发），父组件需要重新拉配置刷新 revision */
  (e: 'credential-issued', accountName: string): void
}>()

const isEdit = computed(() => props.account !== null)

const loginMethodOptions = [
  { label: '站点 Cookie', value: 'newapi_cookie' as LoginMethod },
  { label: 'TaBiAI 凭据', value: 'tabiai' as LoginMethod }
]

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

// 一键签发是服务端直接改库：弹窗里的凭据快照随即过期，标记后按「已设置」对待，
// 避免保存时用旧快照把刚签发的凭据覆盖回去
const issuedCookie = ref(false)

// 服务端返回的原始敏感字段（可能是 "***"），用于打码判断与「留空保持不变」
const originalCookie = computed(() => (issuedCookie.value ? '***' : props.account ? props.account.cookie : ''))
const originalGithubUserSession = computed(() => (props.account ? props.account.github_user_session : ''))

interface AccountForm {
  name: string
  url: string
  login_method: LoginMethod
  cookie: string
  github_user_session: string
  github_client_id: string
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
  login_method: 'newapi_cookie',
  cookie: '',
  github_user_session: '',
  github_client_id: '',
  user_id: '',
  proxy: '',
  checkin_path: '',
  browser_path: '',
  enabled: true
})

const isTabiAI = computed(() => form.login_method === 'tabiai')

// cookie 字段一份表单两种含义：newapi_cookie 存整条 Cookie 头，tabiai 存 new_api_refresh 的值
const cookieFieldLabel = computed(() => (isTabiAI.value ? 'TaBiAI 凭据' : '站点 Cookie'))
const cookiePlaceholder = computed(() =>
  isTabiAI.value
    ? '粘贴 new_api_refresh 的值（形如 sid.secret，可带 new_api_refresh= 前缀）'
    : '粘贴站点完整 Cookie（如 session=...）'
)
const cookieTip = computed(() => {
  if (isTabiAI.value) {
    return isEdit.value
      ? 'TaBiAI 凭据已设置（接口不回传明文），留空保持不变。该值每次 refresh 都会被站点轮转，签到与检测都会自动写回最新代次'
      : '填 new_api_refresh 的值；也可留空，保存后用「一键签发凭据」自动获取'
  }
  return isEdit.value
    ? '站点 Cookie 已设置（接口不回传明文），留空保持不变，输入新值可修改'
    : '可稍后补充；该登录方式运行时需要有效 Cookie'
})
// user_session 已经不是登录凭据了，文案必须讲清它现在的唯一用途，否则用户会以为它还能登录
const githubSessionTip = computed(() =>
  isEdit.value
    ? 'GitHub user_session 已设置（接口不回传明文），留空保持不变。它不是登录凭据，只在「一键签发凭据」时用来走一次 OAuth 换 new_api_refresh'
    : '可选，只填 user_session 值（Client ID 留空则用内置默认值）。它不是登录凭据，只用于帮你签发 new_api_refresh'
)

watch(
  () => props.show,
  (visible) => {
    if (!visible) return
    // 每次重新打开都以父组件传入的快照为准，上一轮的签发标记不能带过来
    issuedCookie.value = false
    if (props.account) {
      form.name = props.account.name
      form.url = props.account.url
      form.login_method = props.account.login_method || 'newapi_cookie'
      form.cookie = props.account.cookie // 可能是 "***"
      form.github_user_session = props.account.github_user_session || ''
      form.github_client_id = props.account.github_client_id || ''
      form.user_id = props.account.user_id == null ? '' : String(props.account.user_id)
      form.proxy = props.account.proxy ?? ''
      form.checkin_path = props.account.checkin_path ?? ''
      form.browser_path = props.account.browser_path ?? ''
      form.enabled = props.account.enabled
      siteKey.value = null
    } else {
      form.name = ''
      form.url = ''
      form.login_method = 'newapi_cookie'
      form.cookie = ''
      form.github_user_session = ''
      form.github_client_id = ''
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
const dialog = useDialog()
const passwordConfirmVisible = ref(false)
const revealing = ref(false)
const revealVisible = ref(false)
const revealedCookie = ref('')
const revealField = ref<'cookie' | 'github_user_session'>('cookie')

/** 编辑且对应 Cookie 是打码占位时，才显示「查看明文」按钮 */
const isMaskedCookie = computed(() => originalCookie.value !== '' && form.cookie === '***')
const isMaskedGithubUserSession = computed(
  () => originalGithubUserSession.value !== '' && form.github_user_session === '***'
)

/** 提示语里的字段名：cookie 在两种登录方式下含义不同，不能一律叫「站点 Cookie」 */
const revealFieldLabel = computed(() => {
  if (revealField.value === 'github_user_session') return 'GitHub user_session'
  return isTabiAI.value ? 'TaBiAI 凭据' : '站点 Cookie'
})

function openReveal(field: 'cookie' | 'github_user_session') {
  revealField.value = field
  passwordConfirmVisible.value = true
}

async function handleRevealCookie(password: string) {
  revealing.value = true
  try {
    // 票据只能用一次，必须紧接着交给 exportConfig
    const { ticket } = await verifyPassword(password)
    // 密码确认通过 → 拉取完整明文配置，取当前账号对应的 Cookie
    const res = await exportConfig(ticket)
    const cfg = JSON.parse(res.json) as { accounts?: Account[] }
    const target = (cfg.accounts ?? []).find((a) => a.name === props.account?.name)
    const value = target?.[revealField.value]
    if (!target || typeof value !== 'string' || !value) {
      message.error(`未找到该账号的${revealFieldLabel.value}`)
      passwordConfirmVisible.value = false
      return
    }
    revealedCookie.value = value
    passwordConfirmVisible.value = false
    revealVisible.value = true
  } catch (e) {
    message.error(extractErrorMessage(e, '密码验证失败'))
  } finally {
    revealing.value = false
  }
}

// ---- TaBiAI：一键签发凭据 ----
const issuing = ref(false)

/**
 * 服务端按「账号名」在已落库的配置里定位账号，再用它的 github_user_session 走一次
 * OAuth 换出新的 new_api_refresh 并直接写回 cookie。所以：
 * - 只有已保存的账号能签发（新增时按钮不出现）
 * - 表单里刚改过名字还没保存时必须先保存，否则签发的是改名前那条记录
 */
async function handleIssueCookie() {
  if (!props.account) return
  const savedName = props.account.name
  if (form.name.trim() !== savedName) {
    dialog.warning({
      title: '请先保存改名',
      content: `签发按已保存的账号名「${savedName}」执行，当前表单已改成「${form.name.trim()}」。`
        + '请先保存改名再签发，避免改到另一条记录。',
      positiveText: '知道了'
    })
    return
  }
  issuing.value = true
  try {
    await issueTabiAICookie(savedName)
    // 服务端已把新凭据落库：表单切回占位符，保存时走「保持服务端值」这条路
    issuedCookie.value = true
    form.cookie = '***'
    message.success(`账号「${savedName}」已签发新的 TaBiAI 凭据`)
    // 落库动作抬高了配置 revision，父组件必须重新拉一次，否则接着保存会撞乐观锁
    emit('credential-issued', savedName)
  } catch (e) {
    message.error(extractErrorMessage(e, '签发 TaBiAI 凭据失败'))
  } finally {
    issuing.value = false
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
    // 两类 Cookie：编辑时留空 → 提交对应原值（"***" 或原明文），保持服务端「原样保留」语义
    const finalCookie = isEdit.value && form.cookie === '' ? originalCookie.value : form.cookie
    const finalGithubUserSession = isEdit.value && form.github_user_session === ''
      ? originalGithubUserSession.value
      : form.github_user_session

    // 改名不必重填凭据：提交时会带上原名（previous_name），
    // 服务端据此定位旧记录，把 "***" 还原成已保存的真值
    const payload: Account = {
      name: form.name.trim(),
      url: form.url.trim(),
      login_method: form.login_method,
      cookie: finalCookie,
      github_user_session: finalGithubUserSession,
      github_client_id: form.github_client_id.trim(),
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

.cookie-actions {
  margin-top: 8px;
}

/* 新增账号时提示「先保存再签发」，与打码提示区分开，不抢眼 */
.field-note {
  margin-top: 6px;
  color: #8492a6;
  font-size: 12px;
  line-height: 1.5;
}

/* 输入框下面要挂说明文字时，让它们竖排且撑满表单项宽度 */
.field-col {
  display: flex;
  flex-direction: column;
  width: 100%;
}
</style>
