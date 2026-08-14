<!--
web/src/views/KeysView.vue
页面：API Key 管理
职责：
- 表格展示 Key：名称/前缀/创建时间/最后使用时间/操作（删除）
- 创建弹窗：输入名称 → 创建成功后展示一次完整 Key 明文 + 复制按钮
- 删除需二次确认
数据来源：GET/POST/DELETE /api/keys
-->
<template>
  <div class="page-container keys-page">
    <n-card :bordered="false" class="keys-card">
      <template #header>
        <div class="card-header">
          <span class="card-title">API Key 管理</span>
        </div>
      </template>
      <template #header-extra>
        <n-button type="primary" size="small" @click="modalVisible = true">
          <template #icon><n-icon><key-outline /></n-icon></template>
          创建 API Key
        </n-button>
      </template>

      <n-alert type="info" :bordered="false" class="keys-tip">
        <template #icon><n-icon><information-circle-outline /></n-icon></template>
        API Key 用于 GitHub Actions 从 <n-text code>https://你的域名/api/config/raw</n-text> 拉取完整配置，请妥善保管。完整 Key 仅在创建时展示一次。
      </n-alert>

      <n-data-table
        :columns="columns"
        :data="keys"
        :loading="loading"
        :bordered="false"
        :pagination="pagination"
        striped
        :scroll-x="760"
      >
        <template #empty>
          <n-empty
            v-if="!loading"
            class="table-empty"
            description="暂无 API Key，点击右上角「创建 API Key」创建"
          />
        </template>
      </n-data-table>
    </n-card>

    <key-create-modal ref="modalRef" v-model:show="modalVisible" :creating="creating" @create="handleCreate" />
  </div>
</template>

<script setup lang="ts">
import { h, onMounted, reactive, ref } from 'vue'
import {
  NCard, NButton, NIcon, NDataTable, NAlert, NText, NEmpty,
  useDialog, useMessage, type DataTableColumns, type PaginationProps
} from 'naive-ui'
import { KeyOutline, InformationCircleOutline, TrashOutline } from '@vicons/ionicons5'
import KeyCreateModal from '@/components/KeyCreateModal.vue'
import { listKeys, createKey, deleteKey } from '@/api/keys'
import { extractErrorMessage } from '@/utils/error'
import type { ApiKey } from '@/types'

const dialog = useDialog()
const message = useMessage()

const keys = ref<ApiKey[]>([])
const loading = ref(false)
const creating = ref(false)
const modalVisible = ref(false)
const modalRef = ref<InstanceType<typeof KeyCreateModal> | null>(null)

// 分页必须用响应式对象 + 显式 onUpdatePageSize 写回，
// 否则 Naive UI 的每页条数选择器（10/20/50）切换后状态不会更新
const pagination = reactive<PaginationProps>({
  pageSize: 10,
  showSizePicker: true,
  pageSizes: [10, 20, 50],
  onUpdatePageSize: (size: number) => {
    pagination.pageSize = size
  }
})

function formatTime(t: string | null) {
  if (!t) return '—'
  const d = new Date(t)
  if (Number.isNaN(d.getTime())) return t
  return d.toLocaleString('zh-CN', { hour12: false })
}

const columns: DataTableColumns<ApiKey> = [
  { title: '名称', key: 'name', width: 180, ellipsis: { tooltip: true } },
  {
    title: '前缀',
    key: 'prefix',
    width: 180,
    render: (row) => h('span', { class: 'mono-prefix' }, row.prefix)
  },
  { title: '创建时间', key: 'created_at', width: 190, render: (row) => formatTime(row.created_at) },
  { title: '最后使用时间', key: 'last_used_at', width: 190, render: (row) => formatTime(row.last_used_at) },
  {
    title: '操作',
    key: 'actions',
    width: 100,
    fixed: 'right',
    render: (row) =>
      h(
        NButton,
        { size: 'tiny', type: 'error', secondary: true, onClick: () => confirmDelete(row) },
        { icon: () => h(NIcon, null, { default: () => h(TrashOutline) }), default: () => '删除' }
      )
  }
]

async function fetchKeys() {
  loading.value = true
  try {
    const res = await listKeys()
    keys.value = res.keys
  } catch (e) {
    message.error(extractErrorMessage(e, '获取 API Key 列表失败'))
  } finally {
    loading.value = false
  }
}

async function handleCreate(name: string) {
  creating.value = true
  try {
    const res = await createKey(name)
    message.success(`API Key「${name}」创建成功`)
    await fetchKeys()
    // 展示一次完整 Key
    modalRef.value?.showCreatedKey(res.key)
  } catch (e) {
    message.error(extractErrorMessage(e, '创建 API Key 失败'))
    modalVisible.value = false
  } finally {
    creating.value = false
  }
}

function confirmDelete(row: ApiKey) {
  dialog.warning({
    title: '删除 API Key',
    content: `确定要删除 API Key「${row.name}」吗？删除后将立即失效，使用该 Key 的 Actions 拉取配置会失败。`,
    positiveText: '删除',
    negativeText: '取消',
    onPositiveClick: async () => {
      try {
        await deleteKey(row.id)
        message.success(`API Key「${row.name}」已删除`)
        await fetchKeys()
      } catch (e) {
        message.error(extractErrorMessage(e, '删除 API Key 失败'))
      }
    }
  })
}

onMounted(fetchKeys)
</script>

<style scoped>
.keys-card {
  background: #fff;
}

.card-header {
  display: flex;
  align-items: center;
}

.card-title {
  font-size: 16px;
  font-weight: 600;
  color: #1f2d3d;
}

.keys-tip {
  margin-bottom: 18px;
}

.mono-prefix {
  font-family: 'JetBrains Mono', Consolas, monospace;
  font-size: 12px;
  color: #48566a;
}

.table-empty {
  padding: 40px 0;
}
</style>
