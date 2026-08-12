<!--
web/src/components/ConfigCard.vue
组件：配置卡片外壳
职责：
- 统一的卡片标题 / 描述 / 更新时间标签 / 加载态 / 底部操作区
- 通过插槽承载各设置页表单
用法：
<ConfigCard title="AI 配置" description="..." :loading="loading" :saving="saving" :updated-at="updatedAt" @save="onSave" @reset="onReset">…表单…</ConfigCard>
-->
<template>
  <n-card :title="title" :bordered="false" class="config-card" :class="{ 'config-card--compact': compact }">
    <template v-if="description || updatedAt" #header-extra>
      <n-tag v-if="updatedAt" size="small" type="info" :bordered="false" class="updated-tag">
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
          <n-button v-if="showReset" :disabled="loading || saving" @click="$emit('reset')">恢复初始值</n-button>
        </slot>
      </div>
    </n-spin>
  </n-card>
</template>

<script setup lang="ts">
import { NButton, NCard, NIcon, NSpin, NTag } from 'naive-ui'
import { SaveOutline } from '@vicons/ionicons5'

defineProps<{
  title: string
  description?: string
  loading?: boolean
  saving?: boolean
  updatedAt?: string
  showReset?: boolean
  compact?: boolean
}>()

defineEmits<{
  (e: 'save'): void
  (e: 'reset'): void
}>()
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
}

.config-card--compact {
  max-width: 720px;
}
</style>
