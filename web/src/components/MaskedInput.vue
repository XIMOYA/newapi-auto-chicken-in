<!--
web/src/components/MaskedInput.vue
组件：打码字段输入框
职责：
- 后端对敏感字段（cookie/api_key/password/token）非空时返回 "***" 且不回传明文
- 已设置时展示提示「已设置（留空保持不变）」，输入新值可覆盖，留空提交原值
- 配合父组件：提交时若值为空则回退为 originalValue
-->
<template>
  <div class="masked-input">
    <n-input
      v-model:value="inputValue"
      :type="type"
      :placeholder="isMasked ? '已设置，留空保持不变（输入新值可修改）' : placeholder"
      :clearable="true"
      :disabled="disabled"
      :autosize="autosize"
    />
    <n-alert v-if="isMasked" type="info" :bordered="false" class="masked-tip">
      <template #icon>
        <n-icon><lock-closed-outline /></n-icon>
      </template>
      <span v-if="customTip">{{ customTip }}</span>
      <span v-else>已设置（点击输入框可修改，留空则保持原值）</span>
    </n-alert>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { NAlert, NIcon, NInput } from 'naive-ui'
import { LockClosedOutline } from '@vicons/ionicons5'

const props = withDefaults(
  defineProps<{
    /** 服务端返回的原始值（可能为 "***" 或空字符串） */
    originalValue: string
    /** 表单当前值 */
    modelValue: string
    placeholder?: string
    type?: 'text' | 'password' | 'textarea'
    customTip?: string
    disabled?: boolean
    /**
     * textarea 类型的自动高度：true = 跟随内容；
     * { minRows, maxRows } = 最小/最大行数（默认 1 行起步，与其他单行框等高）
     */
    autosize?: boolean | { minRows: number; maxRows: number }
  }>(),
  {
    placeholder: '',
    type: 'text',
    customTip: '',
    disabled: false,
    autosize: undefined
  }
)

const emit = defineEmits<{
  (e: 'update:modelValue', value: string): void
}>()

// 仅当原始值非空且当前值仍是占位符 "***" 时，视为「已设置未修改」
const isMasked = computed(() => props.originalValue !== '' && props.modelValue === '***')

const inputValue = computed<string>({
  get: () => (isMasked.value ? '' : props.modelValue),
  set: (v) => emit('update:modelValue', v)
})
</script>

<style scoped>
/* 关键：MaskedInput 外包了一层 div，不再是 NFormItem 的直接子级，
   Naive UI 的「输入控件自动撑满宽度」规则不会生效。
   必须显式让 wrapper 与内部 NInput 都占满 100%，否则宽度塌陷。 */
.masked-input {
  width: 100%;
}
.masked-input :deep(.n-input) {
  width: 100%;
}
</style>
