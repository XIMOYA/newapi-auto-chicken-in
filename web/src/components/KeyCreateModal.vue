<!--
web/src/components/KeyCreateModal.vue
组件：API Key 创建弹窗
职责：
- 第一步：输入 Key 名称
- 第二步：展示创建成功的完整 Key（仅此一次）+ 复制按钮
- 提示 Actions 拉取地址使用方式
数据来源：POST /api/keys
-->
<template>
  <n-modal
    :show="show"
    preset="card"
    :title="createdKey ? 'API Key 创建成功' : '创建 API Key'"
    style="width: 560px"
    :mask-closable="false"
    transition-preset="fade-in-scale-up"
    @update:show="handleClose"
  >
    <n-form v-if="!createdKey" ref="formRef" :model="form" :rules="rules" label-placement="left" :label-width="90">
      <n-form-item label="Key 名称" path="name">
        <n-input v-model:value="form.name" placeholder="例如：github-actions" @keydown.enter.prevent="handleCreate" />
      </n-form-item>
    </n-form>

    <template v-else>
      <n-alert type="warning" :bordered="false" class="key-alert">
        <template #icon>
          <n-icon><key-outline /></n-icon>
        </template>
        完整 Key 仅在创建时展示这一次，请立即复制并妥善保存（GitHub Actions 的 Secret 中也会用到）。
      </n-alert>

      <div class="key-box">
        <n-input :value="createdKey" readonly type="textarea" :rows="3" class="mono-text" />
        <n-button type="primary" size="small" class="copy-btn" @click="copyKey">
          <template #icon><n-icon><copy-outline /></n-icon></template>
          复制 Key
        </n-button>
      </div>

      <n-alert type="info" :bordered="false" class="key-alert">
        <template #icon>
          <n-icon><cloud-download-outline /></n-icon>
        </template>
        <div>
          <div>Actions 拉取地址：</div>
          <n-text code class="raw-url">https://你的域名/api/config/raw</n-text>
          <div class="raw-tip">在 GitHub Actions 的 Secret 中配置 <n-text code>CONFIG_API_KEY</n-text> 为此 Key，拉取配置时携带 <n-text code>Authorization: Bearer &lt;此 Key&gt;</n-text> 即可。</div>
        </div>
      </n-alert>
    </template>

    <template #footer>
      <div v-if="!createdKey" class="modal-footer">
        <n-button @click="handleClose">取消</n-button>
        <n-button type="primary" :loading="creating" @click="handleCreate">创建</n-button>
      </div>
      <div v-else class="modal-footer">
        <n-button type="primary" @click="handleClose">完成</n-button>
      </div>
    </template>
  </n-modal>
</template>

<script setup lang="ts">
import { reactive, ref, watch } from 'vue'
import { NModal, NForm, NFormItem, NInput, NButton, NAlert, NIcon, NText, type FormInst, type FormRules } from 'naive-ui'
import { KeyOutline, CopyOutline, CloudDownloadOutline } from '@vicons/ionicons5'
import { useMessage } from 'naive-ui'
import { copyText } from '@/utils/clipboard'

const props = defineProps<{
  show: boolean
  creating?: boolean
}>()

const emit = defineEmits<{
  (e: 'update:show', value: boolean): void
  (e: 'create', name: string): void
}>()

const message = useMessage()
const formRef = ref<FormInst | null>(null)
const form = reactive({ name: '' })
const createdKey = ref<string>('')

const rules: FormRules = {
  name: { required: true, message: '请输入 Key 名称', trigger: ['input', 'blur'] }
}

watch(
  () => props.show,
  (visible) => {
    if (visible) {
      form.name = ''
      createdKey.value = ''
      formRef.value?.restoreValidation()
    }
  }
)

function handleCreate() {
  formRef.value?.validate((errors) => {
    if (errors) return
    emit('create', form.name.trim())
  })
}

/** 创建成功后由父组件调用，展示完整 Key */
function showCreatedKey(key: string) {
  createdKey.value = key
}

async function copyKey() {
  if (!createdKey.value) return
  const ok = await copyText(createdKey.value)
  if (ok) message.success('Key 已复制到剪贴板')
  else message.error('复制失败，请手动选择复制')
}

function handleClose() {
  emit('update:show', false)
}

defineExpose({ showCreatedKey })
</script>

<style scoped>
.modal-footer {
  display: flex;
  justify-content: flex-end;
  gap: 12px;
}

.key-alert {
  margin-bottom: 16px;
}

.key-box {
  position: relative;
  margin-bottom: 16px;
}

.key-box .copy-btn {
  position: absolute;
  right: 8px;
  bottom: 8px;
}

.raw-url {
  user-select: all;
}

.raw-tip {
  margin-top: 8px;
  color: #606f7e;
  font-size: 12px;
  line-height: 1.7;
}
</style>
