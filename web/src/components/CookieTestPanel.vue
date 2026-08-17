<!--
web/src/components/CookieTestPanel.vue
单个 Cookie 类型的可用性测试面板。
站点 Cookie 与 TaBiAI 凭据由父页面分别挂载本组件，各自维护选择与结果。
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
        <n-tag v-if="running" size="small" type="warning" :bordered="false">
          第 {{ round }} 轮 · 已定论 {{ settled }}/{{ total }}
        </n-tag>
        <n-tag v-else-if="selectedKeys.length" size="small" type="primary" :bordered="false">
          已选 {{ selectedKeys.length }} 项
        </n-tag>
        <n-button v-if="running" type="error" size="small" :loading="stopping" @click="emit('stop')">
          <template #icon><n-icon><stop-circle-outline /></n-icon></template>
          停止检测
        </n-button>
        <n-button
          v-else
          type="primary"
          size="small"
          :loading="loading"
          :disabled="!accounts.length || busy"
          @click="handleRun"
        >
          <template #icon><n-icon><refresh-outline /></n-icon></template>
          {{ selectedKeys.length ? `检测选中 ${selectedKeys.length} 个` : `检测全部 ${accounts.length} 个` }}
        </n-button>
      </n-space>
    </template>

    <n-alert :type="busy ? 'warning' : 'info'" :bordered="false" class="panel-alert">
      {{ busy ? '另一类 Cookie 检测正在进行，请先等它结束或停止后再开始本类检测。' : hint }}
    </n-alert>

    <n-data-table
      v-model:checked-row-keys="selectedKeys"
      :columns="columns"
      :data="rows"
      :loading="loading"
      :row-key="rowKey"
      :bordered="false"
      :scroll-x="1200"
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
import { CheckmarkCircleOutline, RefreshOutline, StopCircleOutline } from '@vicons/ionicons5'
import type { Account, CookieTestMode, CookieTestResult, CookieTestState } from '@/types'

interface Props {
  mode: CookieTestMode
  accounts: Account[]
  results: CookieTestResult[]
  loading: boolean
  /** 本模式的后台任务是否在跑 */
  running?: boolean
  /** 停止请求是否在途 */
  stopping?: boolean
  /** 另一模式的任务占用中，本模式暂时不能启动 */
  busy?: boolean
  /** 当前轮次 */
  round?: number
  /** 已定论账号数 */
  settled?: number
  /** 参与本次检测的账号总数 */
  total?: number
}

interface CookieTestRow extends Account {
  result: CookieTestResult | null
}

const props = defineProps<Props>()
const emit = defineEmits<{
  run: [accountNames: string[]]
  stop: []
}>()

const running = computed(() => props.running === true)
const stopping = computed(() => props.stopping === true)
const busy = computed(() => props.busy === true)
const round = computed(() => props.round ?? 0)
const settled = computed(() => props.settled ?? 0)
const total = computed(() => props.total ?? props.accounts.length)

const selectedKeys = ref<string[]>([])
const modeLabel = computed(() => props.mode === 'tabiai' ? 'TaBiAI 凭据' : '站点 Cookie')
const hint = computed(() => props.mode === 'tabiai'
  ? '只拿 new_api_refresh 去 POST /api/user/auth/refresh，看还能不能换出 access token；不执行签到，不消耗 Turnstile 配额。凭据每次 refresh 都会轮转，检测拿到新代次会立刻写回账号配置。检测走服务器代理池，遇到代理/CDN 拦截会自动换出口无限重试，只有站点或凭据本身给出结论才会停止。'
  : '直接验证 /api/user/self；检测到 new_api_refresh 时会先刷新凭据再验证。检测走服务器代理池，遇到代理/CDN 拦截会自动换出口无限重试，只有站点或凭据本身给出结论才会停止；不会执行签到。')
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
  if (busy.value) return
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
    case 'pending': return '排队中'
    case 'running': return '检测中'
    default: return '未测试'
  }
}

function statusType(state: CookieTestState | undefined): TagProps['type'] {
  switch (state) {
    case 'valid': return 'success'
    case 'invalid': return 'error'
    case 'abnormal': return 'warning'
    case 'skipped': return 'default'
    case 'pending': return 'default'
    case 'running': return 'info'
    default: return 'info'
  }
}

// 两种模式的凭据都落在 cookie 字段上（tabiai 存 new_api_refresh 的值），
// 后端检测也只看这一个字段，所以这里不再按模式分流
function credentialState(row: CookieTestRow) {
  return row.cookie ? '已设置' : '未设置'
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
    title: '尝试',
    key: 'attempts',
    width: 80,
    render: (row) => {
      const attempts = row.result?.attempts ?? 0
      if (!attempts) return h('span', { class: 'muted' }, '—')
      return h('span', { class: attempts > 1 ? 'attempts-many' : '' }, `${attempts} 次`)
    }
  },
  {
    title: '出口',
    key: 'proxy',
    width: 170,
    ellipsis: { tooltip: true },
    render: (row) => {
      const proxy = row.result?.proxy ?? ''
      if (!row.result) return h('span', { class: 'muted' }, '—')
      return proxy
        ? h('span', { class: 'mono-inline' }, proxy)
        : h('span', { class: 'muted' }, '直连')
    }
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

.mono-inline {
  font-family: 'JetBrains Mono', Consolas, Monaco, monospace;
  font-size: 12px;
}

.attempts-many {
  color: #f0a020;
  font-weight: 600;
}
</style>
