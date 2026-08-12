<!--
web/src/components/ConfigCard.vue
组件：配置卡片外壳
职责：
- 统一的卡片标题 / 描述 / 更新时间标签 / 加载态 / 底部操作区
- dirty 状态：有未保存修改时显示「未保存修改」角标
- updatedAt 变化时（保存成功）标签闪烁绿色反馈
- 通过插槽承载各设置页表单
用法：
<ConfigCard title="AI 配置" description="..." :loading="loading" :saving="saving"
            :updated-at="updatedAt" :dirty="isDirty" @save="onSave" @reset="onReset">…表单…</ConfigCard>
-->
<template>
  <n-card :title="title" :bordered="false" class="config-card" :class="{ 'config-card--compact': compact }">
    <template v-if="description || updatedAt || dirty" #header-extra>
      <n-tag v-if="dirty" size="small" type="warning" :bordered="false" class="dirty-tag">未保存修改</n-tag>
      <n-tag v-if="updatedAt" size="small" type="info" :bordered="false" class="updated-tag"
             :class="{ 'flash-updated': flash }">
        更新于 {{ updatedAt }}
      </n-tag>
    </template>

    <n-spin :show="loading" size="large">
      <p v-if="description" class="card-description">{{ description }}</p>
      <slot />
      <div class="card-footer">
        <slot name="footer">
          <n-button type="primary" :loading="saving" :disabled="loading" @click="$emit('save')">
            <template #icon><n-icon><save-outline /></n-icon></template>
            保存配置
          </n-button>
          <n-tooltip v-if="showReset" :disabled="loading || saving">
            <template #trigger>
              <n-button :disabled="loading || saving" @click="$emit('reset')">恢复已保存值</n-button>
            </template>
            放弃本次修改，恢复到上次保存的内容
          </n-tooltip>
        </slot>
      </div>
    </n-spin>
  </n-card>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue'
import { NButton, NCard, NIcon, NSpin, NTag, NTooltip } from 'naive-ui'
import { SaveOutline } from '@vicons/ionicons5'

const props = defineProps<{
  title: string
  description?: string
  loading?: boolean
  saving?: boolean
  updatedAt?: string
  showReset?: boolean
  compact?: boolean
  /** 是否有未保存修改（有则显示角标） */
  dirty?: boolean
}>()

defineEmits<{
  (e: 'save'): void
  (e: 'reset'): void
}>()

// 保存成功后 updatedAt 变化 → 标签短暂闪烁绿色，给用户「已保存」的确认反馈
const flash = ref(false)
watch(
  () => props.updatedAt,
  () => {
    if (!props.updatedAt) return
    flash.value = true
    window.setTimeout(() => {
      flash.value = false
    }, 1500)
  }
)
</script>

<style scoped>
.card-description {
  margin: 0 0 18px;
  color: #8492a6;
  font-size: 13px;
  line-height: 1.6;
}

.card-footer {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-top: 24px;
  padding-top: 18px;
  border-top: 1px dashed #e4e9f2;
}

.updated-tag {
  margin-right: 4px;
  transition: background-color 0.3s ease, color 0.3s ease;
}

.flash-updated {
  background-color: #18a058 !important;
  color: #fff !important;
}

.dirty-tag {
  margin-right: 8px;
}

.config-card--compact {
  max-width: 720px;
}
</style>
