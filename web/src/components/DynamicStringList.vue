<!--
web/src/components/DynamicStringList.vue
组件：动态字符串列表（代理池 sources / 邮件通知 to_addrs 复用）
职责：支持逐项编辑、删除、末尾追加
用法：v-model 绑定 string[]；addLabel 自定义添加按钮文案
-->
<template>
  <div class="dynamic-list">
    <div v-for="index in items.length" :key="index" class="list-row">
      <n-input v-model:value="items[index]" :placeholder="placeholder" />
      <n-button type="error" secondary size="small" :disabled="!items.length" @click="remove(index)">
        <template #icon><n-icon><trash-outline /></n-icon></template>
        删除
      </n-button>
    </div>
    <n-button dashed block size="small" class="add-btn" @click="add">
      <template #icon><n-icon><add-outline /></n-icon></template>
      {{ addLabel }}
    </n-button>
  </div>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue'
import { NInput, NButton, NIcon } from 'naive-ui'
import { TrashOutline, AddOutline } from '@vicons/ionicons5'

const props = withDefaults(
  defineProps<{
    modelValue: string[]
    placeholder?: string
    addLabel?: string
  }>(),
  {
    placeholder: '请输入',
    addLabel: '添加一项'
  }
)

const emit = defineEmits<{
  (e: 'update:modelValue', value: string[]): void
}>()

const items = ref<string[]>([...props.modelValue])

// 外部（父组件/重置）更新时同步到本地
watch(
  () => props.modelValue,
  (val) => {
    items.value = [...val]
  },
  { deep: true }
)

// 本地编辑变化时回传父组件（内容相同则跳过，避免与外部同步互相触发死循环）
watch(
  items,
  (val) => {
    const same =
      val.length === props.modelValue.length && val.every((v, i) => v === props.modelValue[i])
    if (!same) emit('update:modelValue', [...val])
  },
  { deep: true }
)

function add() {
  items.value.push('')
}

function remove(index: number) {
  items.value.splice(index, 1)
}
</script>

<style scoped>
.list-row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}

.add-btn {
  margin-top: 4px;
}
</style>
