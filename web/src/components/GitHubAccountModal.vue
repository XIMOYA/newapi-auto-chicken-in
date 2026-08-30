<!--
web/src/components/GitHubAccountModal.vue
组件：GitHub 凭据池条目 新增/编辑 弹窗
职责：
- 字段：name（引用键）/ user_session（凭据，打码）/ client_id（站点 OAuth 应用 ID，非凭据）
- user_session 走 MaskedInput：服务端只回 "***"，留空表示保持原值
- 改名不必重填凭据：提交时带 previous_name，服务端据此找回真值（见 ops 端点）
- 校验：name 必填；新增时 user_session 必填（服务端空值直接 400）
数据来源：父组件传入 account（null 表示新增）
-->
<template>
  <n-modal
    :show="show"
    preset="card"
    :title="isEdit ? '编辑 GitHub 账号' : '新增 GitHub 账号'"
    style="width: 600px"
    :mask-closable="false"
    transition-preset="fade-in-scale-up"
    @update:show="(v: boolean) => emit('update:show', v)"
  >
    <n-form ref="formRef" :model="form" :rules="rules" label-placement="left" :label-width="130">
      <n-form-item label="GitHub 用户名" path="name">
        <div class="field-col">
          <n-input v-model:value="form.name" placeholder="例如：Steven" />
          <div class="field-note">
            这个名字是引用键：站点账号靠它找凭据，账号名也会自动生成成「{{ form.name || 'Steven' }}（站点域名）」。
            改名会由服务端连带改掉引用它的账号，不必手动改。
          </div>
        </div>
      </n-form-item>
      <n-form-item label="user_session" path="user_session">
        <masked-input
          v-model="form.user_session"
          :original-value="originalUserSession"
          type="textarea"
          :autosize="{ minRows: 2, maxRows: 6 }"
          placeholder="粘贴 GitHub 的 user_session Cookie 值"
          :custom-tip="sessionTip"
        />
      </n-form-item>
      <n-form-item label="OAuth Client ID" path="client_id">
        <div class="field-col">
          <n-input v-model:value="form.client_id" placeholder="可选，留空由站点 /api/status 自动探测" />
          <div class="field-note">站点 OAuth 应用的公开标识，不是凭据，所以明文显示。</div>
        </div>
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
import { NForm, NFormItem, NInput, NModal, NButton, type FormInst, type FormRules } from 'naive-ui'
import MaskedInput from './MaskedInput.vue'
import type { GitHubAccount } from '@/types'

const props = defineProps<{
  show: boolean
  /** 编辑对象；null 表示新增 */
  account: GitHubAccount | null
  submitting?: boolean
}>()

const emit = defineEmits<{
  (e: 'update:show', value: boolean): void
  (e: 'submit', payload: GitHubAccount): void
}>()

const isEdit = computed(() => props.account !== null)

// 服务端回传的原始值（非空时是 "***"），用于打码判断与「留空保持不变」
const originalUserSession = computed(() => (props.account ? props.account.user_session : ''))

const sessionTip = computed(() =>
  isEdit.value
    ? 'user_session 已设置（接口不回传明文），留空保持不变。改名时也不用重填 —— 服务端按原名找回真值'
    : '必填。它是签发站点凭据的原料，池子里存一份就够，所有引用它的站点账号共用'
)

const formRef = ref<FormInst | null>(null)
const form = reactive<GitHubAccount>({
  name: '',
  user_session: '',
  client_id: ''
})

watch(
  () => props.show,
  (visible) => {
    if (!visible) return
    if (props.account) {
      form.name = props.account.name
      form.user_session = props.account.user_session // 可能是 "***"
      form.client_id = props.account.client_id ?? ''
    } else {
      form.name = ''
      form.user_session = ''
      form.client_id = ''
    }
    formRef.value?.restoreValidation()
  }
)

const rules: FormRules = {
  name: { required: true, message: '请输入 GitHub 用户名', trigger: ['input', 'blur'] },
  user_session: {
    validator: () => {
      // 编辑时留空 = 保持原值，只有「原本也没有」才算漏填。
      // 服务端对空 user_session 直接 400，前端先拦一道少一次往返
      if (isEdit.value && originalUserSession.value !== '') return true
      return form.user_session.trim() ? true : new Error('user_session 不能为空')
    },
    trigger: ['input', 'blur']
  }
}

function handleSubmit() {
  formRef.value?.validate((errors) => {
    if (errors) return
    // 留空 → 回填原值（"***"），保持服务端「原样保留」语义；
    // 改名时这个占位符会由 previous_name 兜住，用户不必重填凭据
    const session = form.user_session.trim() === '' ? originalUserSession.value : form.user_session.trim()
    emit('submit', {
      name: form.name.trim(),
      user_session: session,
      client_id: form.client_id.trim()
    })
  })
}
</script>

<style scoped>
.modal-footer {
  display: flex;
  justify-content: flex-end;
  gap: 12px;
}

/* 输入框下面挂说明文字时让它们竖排且撑满表单项宽度（与 AccountModal 同一处理） */
.field-col {
  display: flex;
  flex-direction: column;
  width: 100%;
}

.field-note {
  margin-top: 6px;
  color: #8492a6;
  font-size: 12px;
  line-height: 1.5;
}
</style>
