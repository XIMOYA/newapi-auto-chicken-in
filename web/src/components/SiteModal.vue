<!--
web/src/components/SiteModal.vue
组件：站点预设 新增/编辑 弹窗
职责：
- 字段：name / url / checkin_path / browser_path
- 表单校验：名称必填、URL 必须 http(s) 开头
数据来源：父组件传入 site（null 表示新增）
-->
<template>
  <n-modal
    :show="show"
    preset="card"
    :title="isEdit ? '编辑站点预设' : '新增站点预设'"
    style="width: 560px"
    :mask-closable="false"
    transition-preset="fade-in-scale-up"
    @update:show="(v: boolean) => emit('update:show', v)"
  >
    <n-form ref="formRef" :model="form" :rules="rules" label-placement="left" :label-width="100">
      <n-form-item label="站点名称" path="name">
        <n-input v-model:value="form.name" placeholder="例如：GoRouter" />
      </n-form-item>
      <n-form-item label="站点 URL" path="url">
        <n-input v-model:value="form.url" placeholder="https://gorouter.app" />
      </n-form-item>
      <n-form-item label="签到路径" path="checkin_path">
        <n-input v-model:value="form.checkin_path" placeholder="可选，例如 /api/user/checkin" />
      </n-form-item>
      <n-form-item label="浏览器入口" path="browser_path">
        <n-input v-model:value="form.browser_path" placeholder="可选，例如 /dashboard" />
      </n-form-item>
    </n-form>
    <template #footer>
      <div class="modal-footer">
        <n-button @click="emit('update:show', false)">取消</n-button>
        <n-button type="primary" @click="handleSubmit">保存</n-button>
      </div>
    </template>
  </n-modal>
</template>

<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { NForm, NFormItem, NInput, NModal, NButton, type FormInst, type FormRules } from 'naive-ui'
import type { Site } from '@/types'

const props = defineProps<{
  show: boolean
  /** 编辑对象；null 表示新增 */
  site: Site | null
}>()

const emit = defineEmits<{
  (e: 'update:show', value: boolean): void
  (e: 'submit', payload: Site): void
}>()

const isEdit = computed(() => props.site !== null)

interface SiteForm {
  name: string
  url: string
  checkin_path: string
  browser_path: string
}

const formRef = ref<FormInst | null>(null)
const form = reactive<SiteForm>({
  name: '',
  url: '',
  checkin_path: '',
  browser_path: ''
})

watch(
  () => props.show,
  (visible) => {
    if (!visible) return
    if (props.site) {
      form.name = props.site.name
      form.url = props.site.url
      form.checkin_path = props.site.checkin_path ?? ''
      form.browser_path = props.site.browser_path ?? ''
    } else {
      form.name = ''
      form.url = ''
      form.checkin_path = ''
      form.browser_path = ''
    }
    formRef.value?.restoreValidation()
  }
)

const rules: FormRules = {
  name: { required: true, message: '请输入站点名称', trigger: ['input', 'blur'] },
  url: [
    { required: true, message: '请输入站点 URL', trigger: ['input', 'blur'] },
    {
      validator: (_rule, value: string) => {
        if (!value) return true
        return /^https?:\/\//i.test(value) ? true : new Error('URL 必须以 http:// 或 https:// 开头')
      },
      trigger: ['input', 'blur']
    }
  ]
}

function handleSubmit() {
  formRef.value?.validate((errors) => {
    if (errors) return
    const payload: Site = {
      name: form.name.trim(),
      url: form.url.trim(),
      checkin_path: form.checkin_path.trim() === '' ? null : form.checkin_path.trim(),
      browser_path: form.browser_path.trim() === '' ? null : form.browser_path.trim()
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
