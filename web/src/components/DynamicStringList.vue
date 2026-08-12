<!--
web/src/components/DynamicStringList.vue
组件：动态字符串列表（代理池 sources / 邮件通知 to_addrs 复用）
职责：支持逐项编辑、删除、末尾追加；增删带过渡动画
用法：v-model 绑定 string[]；addLabel 自定义添加按钮文案
内部用带自增 id 的对象列表驱动 transition-group，保证增删动画正确
-->
<template>
  <div class="dynamic-list">
    <transition-group name="list" tag="div" class="list-wrap">
      <div v-for="item in items" :key="item.id" class="list-row">
        <n-input v-model:value="item.value" :placeholder="placeholder" />
        <n-button type="error" secondary size="small" @click="remove(item.id)">
          <template #icon><n-icon><trash-outline /></n-icon></template>
          删除
        </n-button>
      </div>
    </transition-group>
    <n-button dashed block size="small" class="add-btn press-scale" @click="add">
      <template #icon><n-icon><add-outline /></n-icon></template>
      {{ addLabel }}
    </n-button>
  </div>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue'
import { NInput, NButton, NIcon } from 'naive-ui'
import { TrashOutline, AddOutline } from '@vicons/ionicons5'

interface ListItem {
  id: number
  value: string
}

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

let nextId = 1
const items = ref<ListItem[]>([])

// 把外部 string[] 同步为内部 {id,value}[]（外部更新/重置时）
function syncFromExternal(val: string[]) {
  items.value = val.map((v) => ({ id: nextId++, value: v }))
}

syncFromExternal(props.modelValue)

watch(
  () => props.modelValue,
  (val) => {
    // 仅当内容与当前内部列表不一致时重建（避免与本地编辑互相触发）
    const cur = items.value.map((i) => i.value)
    const same = val.length === cur.length && val.every((v, i) => v === cur[i])
    if (!same) syncFromExternal(val)
  },
  { deep: true }
)

// 本地编辑变化时回传父组件
watch(
  items,
  (val) => {
    const values = val.map((i) => i.value)
    const same =
      values.length === props.modelValue.length &&
      values.every((v, i) => v === props.modelValue[i])
    if (!same) emit('update:modelValue', values)
  },
  { deep: true }
)

function add() {
  items.value.push({ id: nextId++, value: '' })
}

function remove(id: number) {
  items.value = items.value.filter((i) => i.id !== id)
}
</script>

<style scoped>
.list-wrap {
  width: 100%;
}

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
