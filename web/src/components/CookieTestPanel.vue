<!--
web/src/components/CookieTestPanel.vue
单个 Cookie 类型的可用性测试面板。
站点 Cookie 与 GitHub OAuth 由父页面分别挂载本组件，各自维护选择与结果。
-->
<template>
  <n-card :bordered="false" class="cookie-test-panel">
    <template #header>
      <div class="panel-title">
        <span>{{ modeLabel }} 可用性测试</span>
        <n-tag size="small" type="info" :bordered="false">仅检测启用账号</n-tag>
      </div>
    </template>
    <template #header-extra>
      <n-space :size="8" align="center">
        <n-tag v-if="selectedKeys.length" size="small" type="primary" :bordered="false">
          已选 {{ selectedKeys.length }} 项
        </n-tag>
        <n-button type="primary" size="small" :loading="loading" :disabled="!accounts.length" @click="handleRun">
          <template #icon><n-icon><refresh-outline /></n-icon></template>
          {{ selectedKeys.length ? `检测选中 ${selectedKeys.length} 个` : `检测全部 ${accounts.length} 个` }}
        </n-button>
      </n-space>
    </template>

    <n-alert type="info" :bordered="false" class="panel-alert">
      {{ hint }}
    </n-alert>

    <n-data-table
      v-model:checked-row-keys="selectedKeys"
      :columns="columns"
      :data="rows"
      :loading="loading"
      :row-key="rowKey"
      :bordered="false"
      :scroll-x="900"
      striped
      size="small"
    >
      <template #empty>
        <n-empty :description="`暂无可检测的${modeLabel}账号，请先在配置总览中添加并启用对应登录方式`" />
      </template>
    </n-data-table>
  </n-card>
</template>

<script setup lang="ts">
import { computed, h, ref, watch } from 'vue'
import {
  NAlert, NButton, NCard, NDataTable, NEmpty, NIcon, NSpace, NTag,
  type DataTableColumns, type TagProps
} from 'naive-ui'
import { CheckmarkCircleOutline, RefreshOutline } from '@vicons/ionicons5'
import type { Account, CookieTestMode, CookieTestResult, CookieTestState } from '@/types'

interface Props {
  mode: CookieTestMode
  accounts: Account[]
  results: CookieTestResult[]
  loading: boolean
}

interface CookieTestRow extends Account {
  result: CookieTestResult | null
}

const props = defineProps<Props>()
const emit = defineEmits<{
  run: [accountNames: string[]]
}>()

const selectedKeys = ref<string[]>([])
const modeLabel = computed(() => props.mode === 'github_cookie' ? 'GitHub OAuth' : '站点 Cookie')
const hint = computed(() => props.mode === 'github_cookie'
  ? '只执行站点 OAuth state（POST /api/oauth/state）与 GitHub authorize：Client ID 取账号配置，未填则读站点 /api/status。取得授权 code 即视为凭据可用，不会调用站点 OAuth 回调或执行签到。'
  : '直接验证 /api/user/self；检测到 new_api_refresh 时会先刷新凭据再验证，不会执行签到。')
const resultByName = computed(() => new Map(props.results.map((result) => [result.name, result])))
const rows = computed<CookieTestRow[]>(() => props.accounts.map((account) => ({
  ...account,
  result: resultByName.value.get(account.name) ?? null
})))

watch(() => props.mode, () => {
  selectedKeys.value = []
})
watch(rows, (nextRows) => {
  const names = new Set(nextRows.map((row) => row.name))
  selectedKeys.value = selectedKeys.value.filter((name) => names.has(name))
})

function rowKey(row: CookieTestRow) {
  return row.name
}

function handleRun() {
  emit('run', selectedKeys.value)
}

function formatDuration(value: number) {
  if (!value || value < 1) return '<1 ms'
  return `${value} ms`
}

function statusLabel(state: CookieTestState | undefined) {
  switch (state) {
    case 'valid': return '有效'
    case 'invalid': return '失效'
    case 'abnormal': return '异常'
    case 'skipped': return '跳过'
    default: return '未测试'
  }
}

function statusType(state: CookieTestState | undefined): TagProps['type'] {
  switch (state) {
    case 'valid': return 'success'
    case 'invalid': return 'error'
    case 'abnormal': return 'warning'
    case 'skipped': return 'default'
    default: return 'info'
  }
}

function credentialState(row: CookieTestRow) {
  const value = props.mode === 'github_cookie' ? row.github_user_session : row.cookie
  return value ? '已设置' : '未设置'
}

const columns: DataTableColumns<CookieTestRow> = [
  { type: 'selection', width: 44, fixed: 'left' },
  {
    title: '账号',
    key: 'name',
    width: 160,
    fixed: 'left',
    ellipsis: { tooltip: true }
  },
  {
    title: '站点 URL',
    key: 'url',
    width: 220,
    ellipsis: { tooltip: true },
    render: (row) => h('a', {
      href: row.url,
      target: '_blank',
      rel: 'noopener',
      class: 'url-link'
    }, row.url)
  },
  {
    title: '凭据',
    key: 'credential',
    width: 100,
    render: (row) => h(NTag, {
      size: 'small',
      type: credentialState(row) === '已设置' ? 'success' : 'default',
      bordered: false
    }, { default: () => credentialState(row) })
  },
  {
    title: '检测状态',
    key: 'state',
    width: 110,
    render: (row) => h(NTag, {
      size: 'small',
      type: statusType(row.result?.state),
      bordered: false
    }, {
      icon: row.result?.state === 'valid' ? () => h(NIcon, null, { default: () => h(CheckmarkCircleOutline) }) : undefined,
      default: () => statusLabel(row.result?.state)
    })
  },
  {
    title: '结果说明',
    key: 'message',
    minWidth: 280,
    ellipsis: { tooltip: true },
    render: (row) => row.result?.message || h('span', { class: 'muted' }, '尚未检测')
  },
  {
    title: '用户 ID',
    key: 'user_id',
    width: 100,
    render: (row) => row.result?.user_id ? String(row.result.user_id) : h('span', { class: 'muted' }, '—')
  },
  {
    title: '耗时',
    key: 'duration_ms',
    width: 100,
    render: (row) => row.result ? formatDuration(row.result.duration_ms) : h('span', { class: 'muted' }, '—')
  }
]
</script>

<style scoped>
.cookie-test-panel {
  background: #fff;
}

.panel-title {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 16px;
  font-weight: 600;
  color: #1f2d3d;
}

.panel-alert {
  margin-bottom: 16px;
}

.url-link {
  color: #1e5eff;
  text-decoration: none;
}

.url-link:hover {
  text-decoration: underline;
}

.muted {
  color: #a3aec0;
}
</style>
