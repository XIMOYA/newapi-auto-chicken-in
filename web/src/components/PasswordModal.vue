<!--
web/src/components/PasswordModal.vue
组件：修改密码弹窗
职责：旧密码校验 + 新密码至少 8 字符 + 两次输入一致（对应契约 §8）
数据来源：PUT /api/password
-->
<template>
  <n-modal
    :show="show"
    preset="card"
    title="修改密码"
    style="width: 460px"
    :mask-closable="false"
    transition-preset="fade-in-scale-up"
    @update:show="(v: boolean) => emit('update:show', v)"
  >
    <n-form ref="formRef" :model="form" :rules="rules" label-placement="left" :label-width="90">
      <n-form-item label="旧密码" path="old_password">
        <n-input v-model:value="form.old_password" type="password" show-password-on="click" placeholder="请输入当前密码" />
      </n-form-item>
      <n-form-item label="新密码" path="new_password">
        <n-input v-model:value="form.new_password" type="password" show-password-on="click" placeholder="至少 8 个字符" />
      </n-form-item>
      <n-form-item label="确认新密码" path="confirm">
        <n-input v-model:value="form.confirm" type="password" show-password-on="click" placeholder="再次输入新密码" @keydown.enter.prevent="handleSubmit" />
      </n-form-item>
    </n-form>
    <template #footer>
      <div class="modal-footer">
        <n-button @click="emit('update:show', false)">取消</n-button>
        <n-button type="primary" :loading="submitting" @click="handleSubmit">确认修改</n-button>
      </div>
    </template>
  </n-modal>
</template>

<script setup lang="ts">
import { reactive, ref, watch } from 'vue'
import { NModal, NForm, NFormItem, NInput, NButton, useMessage, type FormInst, type FormRules } from 'naive-ui'
import { changePassword } from '@/api/auth'

const props = defineProps<{
  show: boolean
}>()

const emit = defineEmits<{
  (e: 'update:show', value: boolean): void
}>()

const message = useMessage()
const formRef = ref<FormInst | null>(null)
const submitting = ref(false)
const form = reactive({
  old_password: '',
  new_password: '',
  confirm: ''
})

watch(
  () => props.show,
  (visible) => {
    if (visible) {
      form.old_password = ''
      form.new_password = ''
      form.confirm = ''
      formRef.value?.restoreValidation()
    }
  }
)

const rules: FormRules = {
  old_password: { required: true, message: '请输入旧密码', trigger: ['input', 'blur'] },
  new_password: [
    { required: true, message: '请输入新密码', trigger: ['input', 'blur'] },
    { min: 8, message: '新密码至少 8 个字符', trigger: ['input', 'blur'] }
  ],
  confirm: [
    { required: true, message: '请再次输入新密码', trigger: ['input', 'blur'] },
    {
      validator: (_rule, value: string) => {
        if (value !== form.new_password) return new Error('两次输入的新密码不一致')
        return true
      },
      trigger: ['input', 'blur']
    }
  ]
}

async function handleSubmit() {
  formRef.value?.validate(async (errors) => {
    if (errors) return
    submitting.value = true
    try {
      await changePassword({ old_password: form.old_password, new_password: form.new_password })
      message.success('密码修改成功')
      emit('update:show', false)
    } catch (e) {
      const msg = (e as { response?: { data?: { error?: string } } })?.response?.data?.error
      message.error(msg || '密码修改失败')
    } finally {
      submitting.value = false
    }
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
