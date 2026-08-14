<!--
web/src/components/PasswordConfirmModal.vue
组件：二次确认密码弹窗
职责：
- 查看明文 Cookie / 其他高敏操作前的密码确认
- 输入密码 → 点击确认 → emit('confirm', password)
数据来源：无（纯 UI）
-->
<template>
  <n-modal
    :show="show"
    preset="card"
    title="安全确认"
    style="width: 400px"
    :mask-closable="false"
    transition-preset="fade-in-scale-up"
    @update:show="(v: boolean) => emit('update:show', v)"
  >
    <p class="confirm-tip">{{ tip }}</p>
    <n-input
      v-model:value="password"
      type="password"
      show-password-on="click"
      placeholder="请输入管理员密码以继续"
      :disabled="loading"
      autocomplete="current-password"
      @keydown.enter.prevent="handleConfirm"
    />
    <template #footer>
      <div class="modal-footer">
        <n-button :disabled="loading" @click="emit('update:show', false)">取消</n-button>
        <n-button type="primary" :loading="loading" @click="handleConfirm">确认</n-button>
      </div>
    </template>
  </n-modal>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue'
import { NModal, NInput, NButton } from 'naive-ui'

const props = withDefaults(
  defineProps<{
    show: boolean
    tip?: string
    loading?: boolean
  }>(),
  {
    tip: '此操作将显示敏感信息（如 Cookie 明文），请输入管理员密码确认身份。',
    loading: false
  }
)

const emit = defineEmits<{
  (e: 'update:show', value: boolean): void
  (e: 'confirm', password: string): void
}>()

const password = ref('')

watch(
  () => props.show,
  (v) => {
    if (v) password.value = ''
  }
)

function handleConfirm() {
  if (!password.value) return
  emit('confirm', password.value)
}
</script>

<style scoped>
.confirm-tip {
  margin: 0 0 14px;
  font-size: 13px;
  color: #48566a;
  line-height: 1.6;
}

.modal-footer {
  display: flex;
  justify-content: flex-end;
  gap: 12px;
}
</style>
