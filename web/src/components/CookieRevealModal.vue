<!--
web/src/components/CookieRevealModal.vue
组件：明文 Cookie 展示弹窗
职责：
- 展示指定账号的明文 Cookie（readonly textarea）+ 一键复制
数据来源：父组件传入 cookie 明文
-->
<template>
  <n-modal
    :show="show"
    preset="card"
    :title="`账号「${accountName}」的 Cookie（明文）`"
    style="width: 560px"
    :mask-closable="false"
    transition-preset="fade-in-scale-up"
    @update:show="(v: boolean) => emit('update:show', v)"
  >
    <n-alert type="warning" :bordered="false" class="reveal-tip">
      <template #icon><n-icon><lock-closed-outline /></n-icon></template>
      明文 Cookie 仅在本次弹窗内展示。请勿复制到不安全环境；关闭弹窗后不会保留。
    </n-alert>
    <n-input
      :value="cookie"
      type="textarea"
      readonly
      :autosize="{ minRows: 4, maxRows: 10 }"
      class="mono-text reveal-input"
    />
    <template #footer>
      <div class="modal-footer">
        <n-button type="primary" @click="copyCookie">
          <template #icon><n-icon><copy-outline /></n-icon></template>
          复制 Cookie
        </n-button>
        <n-button @click="emit('update:show', false)">关闭</n-button>
      </div>
    </template>
  </n-modal>
</template>

<script setup lang="ts">
import { NModal, NInput, NButton, NAlert, NIcon, useMessage } from 'naive-ui'
import { LockClosedOutline, CopyOutline } from '@vicons/ionicons5'
import { copyText } from '@/utils/clipboard'

const props = defineProps<{
  show: boolean
  cookie: string
  accountName: string
}>()

const emit = defineEmits<{
  (e: 'update:show', value: boolean): void
}>()

const message = useMessage()

async function copyCookie() {
  const ok = await copyText(props.cookie)
  if (ok) message.success('Cookie 已复制到剪贴板')
  else message.error('复制失败，请手动选择复制')
}
</script>

<style scoped>
.reveal-tip {
  margin-bottom: 14px;
}

.reveal-input {
  font-family: 'JetBrains Mono', Consolas, 'Courier New', monospace !important;
  font-size: 12px !important;
  line-height: 1.6 !important;
}

.modal-footer {
  display: flex;
  justify-content: flex-end;
  gap: 12px;
}
</style>
