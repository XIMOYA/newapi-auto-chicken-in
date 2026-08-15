<!--
web/src/views/ProxiesView.vue
页面：代理管理（独立页）
职责：
- 统计卡：可用 / 总数 / 平均延迟 / 上次刷新
- 代理列表表格：地址 / 来源 / 延迟(ms) / 测速(KB/s·MB/s) / 状态 / 最后存活
- 筛选（全部/可用 + 按来源）+ 排序（延迟/速度）
- 测速：手动对全部可用或勾选代理跑 Cloudflare 下载测速，结果写库
- Actions 预取：列表按质量排序（speed_bps 优先），实现优选
数据来源：GET /api/proxies、GET /api/proxies/stats、POST /api/proxies/speedtest
-->
<template>
  <div class="page-container proxies-page">
    <!-- 统计行 -->
    <n-grid :cols="4" :x-gap="16" :y-gap="16" responsive="screen" item-responsive>
      <n-grid-item span="4 s:2 m:1">
        <n-card :bordered="false" class="stat-card">
          <div class="stat-inner">
            <div class="stat-icon" style="background: #e8f9ef; color: #18a058">
              <n-icon size="22"><checkmark-circle-outline /></n-icon>
            </div>
            <div class="stat-meta">
              <div class="stat-label">可用代理</div>
              <div class="stat-value">{{ stats?.alive ?? 0 }}</div>
            </div>
          </div>
        </n-card>
      </n-grid-item>
      <n-grid-item span="4 s:2 m:1">
        <n-card :bordered="false" class="stat-card">
          <div class="stat-inner">
            <div class="stat-icon" style="background: #e8f0ff; color: #1e5eff">
              <n-icon size="22"><layers-outline /></n-icon>
            </div>
            <div class="stat-meta">
              <div class="stat-label">总条目</div>
              <div class="stat-value">{{ stats?.total ?? 0 }}</div>
            </div>
          </div>
        </n-card>
      </n-grid-item>
      <n-grid-item span="4 s:2 m:1">
        <n-card :bordered="false" class="stat-card">
          <div class="stat-inner">
            <div class="stat-icon" style="background: #fdf3e7; color: #f0a020">
              <n-icon size="22"><speedometer-outline /></n-icon>
            </div>
            <div class="stat-meta">
              <div class="stat-label">平均延迟</div>
              <div class="stat-value">{{ avgLatency }}</div>
            </div>
          </div>
        </n-card>
      </n-grid-item>
      <n-grid-item span="4 s:2 m:1">
        <n-card :bordered="false" class="stat-card">
          <div class="stat-inner">
            <div class="stat-icon" style="background: #f0ecfe; color: #7c5cf0">
              <n-icon size="22"><time-outline /></n-icon>
            </div>
            <div class="stat-meta">
              <div class="stat-label">上次刷新</div>
              <div class="stat-value stat-value-sm">{{ formatTime(stats?.last_run) }}</div>
            </div>
          </div>
        </n-card>
      </n-grid-item>
    </n-grid>

    <!-- 工具栏 -->
    <n-card :bordered="false" class="toolbar-card">
      <n-space :size="8" align="center" class="toolbar">
        <n-radio-group v-model:value="aliveFilter" size="small">
          <n-radio-button value="all">全部</n-radio-button>
          <n-radio-button value="alive">仅可用</n-radio-button>
        </n-radio-group>
        <n-select
          v-model:value="sourceFilter"
          :options="sourceOptions"
          placeholder="按来源筛选"
          clearable
          size="small"
          class="source-select"
        />
        <n-select
          v-model:value="sortBy"
          :options="sortOptions"
          placeholder="排序"
          size="small"
          class="sort-select"
        />
        <n-tag v-if="stats?.running" type="warning" :bordered="false">刷新中…</n-tag>
        <n-alert v-if="stats?.last_error" type="error" :bordered="false" class="toolbar-err">
          {{ stats.last_error }}
        </n-alert>
        <div class="toolbar-actions">
          <n-button size="small" :loading="refreshing" @click="handleManualRefresh">
            <template #icon><n-icon><refresh-outline /></n-icon></template>
            立即刷新
          </n-button>
          <n-button size="small" type="primary" :loading="speedTesting" :disabled="!speedCandidates.length" @click="handleSpeedTest">
            <template #icon><n-icon><flash-outline /></n-icon></template>
            测速{{ selectedKeys.length ? `（勾选 ${selectedKeys.length}）` : '（全部可用）' }}
          </n-button>
        </div>
      </n-space>
    </n-card>

    <!-- 代理列表表格 -->
    <n-card :bordered="false" class="proxy-table-card">
      <n-data-table
        :columns="proxyColumns"
        :data="filteredProxies"
        :loading="listLoading"
        :row-key="(row: ProxyEntry) => row.id"
        v-model:selected-row-keys="selectedKeys"
        :pagination="proxyPagination"
        striped
        :bordered="false"
        size="small"
        :scroll-x="900"
      >
        <template #empty>
          <n-empty v-if="!listLoading" description="暂无代理数据，点击「立即刷新」抓取" />
        </template>
      </n-data-table>
    </n-card>
  </div>
</template>

<script setup lang="ts">
import { computed, h, onMounted, reactive, ref } from 'vue'
import {
  NCard, NButton, NIcon, NTag, NDataTable, NRadioGroup, NRadioButton,
  NSelect, NSpace, NEmpty, NGrid, NGridItem, NAlert, useMessage,
  type DataTableColumns, type PaginationProps, type SelectOption
} from 'naive-ui'
import {
  CheckmarkCircleOutline, LayersOutline, SpeedometerOutline, TimeOutline,
  RefreshOutline, FlashOutline
} from '@vicons/ionicons5'
import { listProxies, getProxyStats, refreshProxies, speedTestProxies } from '@/api/proxies'
import { extractErrorMessage } from '@/utils/error'
import type { ProxyEntry, ProxyStatsResult } from '@/types'

const message = useMessage()

// ---- 状态 ----
const stats = ref<ProxyStatsResult | null>(null)
const proxies = ref<ProxyEntry[]>([])
const listLoading = ref(false)
const refreshing = ref(false)
const speedTesting = ref(false)
const aliveFilter = ref<'all' | 'alive'>('all')
const sourceFilter = ref<string | null>(null)
const sortBy = ref<'latency' | 'speed'>('latency')
const selectedKeys = ref<number[]>([])

// ---- 统计 ----
const avgLatency = computed(() => {
  const alive = proxies.value.filter((p) => p.alive)
  if (!alive.length) return '—'
  const sum = alive.reduce((acc, p) => acc + (p.latency_ms || 0), 0)
  return `${Math.round(sum / alive.length)} ms`
})

const formatTime = (t?: string) => {
  if (!t) return '—'
  const d = new Date(t)
  if (Number.isNaN(d.getTime())) return t
  return d.toLocaleString('zh-CN', { hour12: false })
}

// ---- 测速格式化（KB/s / MB/s） ----
function formatSpeed(bps: number) {
  if (!bps || bps <= 0) return '—'
  if (bps >= 1024 * 1024) return `${(bps / 1024 / 1024).toFixed(2)} MB/s`
  if (bps >= 1024) return `${(bps / 1024).toFixed(0)} KB/s`
  return `${bps} B/s`
}

// ---- 筛选与排序 ----
const sourceOptions = computed<SelectOption[]>(() => {
  const set = new Set<string>()
  proxies.value.forEach((p) => set.add(p.source))
  return [...set].map((s) => ({ label: s, value: s }))
})

const sortOptions: SelectOption[] = [
  { label: '按延迟排序', value: 'latency' },
  { label: '按速度排序', value: 'speed' }
]

const filteredProxies = computed(() => {
  let out = proxies.value
  if (aliveFilter.value === 'alive') out = out.filter((p) => p.alive)
  if (sourceFilter.value) out = out.filter((p) => p.source === sourceFilter.value)
  const arr = [...out]
  if (sortBy.value === 'speed') {
    arr.sort((a, b) => (b.speed_bps || 0) - (a.speed_bps || 0) || (a.latency_ms || 0) - (b.latency_ms || 0))
  } else {
    arr.sort((a, b) => (a.latency_ms || 0) - (b.latency_ms || 0))
  }
  return arr
})

const speedCandidates = computed(() => {
  if (selectedKeys.value.length) {
    return proxies.value.filter((p) => selectedKeys.value.includes(p.id) && p.alive)
  }
  return filteredProxies.value.filter((p) => p.alive)
})

const proxyPagination: PaginationProps = reactive({
  pageSize: 20,
  showSizePicker: true,
  pageSizes: [10, 20, 50, 100],
  onUpdatePageSize: (size: number) => {
    proxyPagination.pageSize = size
  }
})

// ---- 表格列 ----
const proxyColumns: DataTableColumns<ProxyEntry> = [
  { type: 'selection', width: 44 },
  {
    title: '代理地址',
    key: 'addr',
    width: 170,
    render: (row) => h('span', { class: 'mono-inline' }, row.addr)
  },
  {
    title: '来源',
    key: 'source',
    width: 280,
    ellipsis: { tooltip: true },
    render: (row) => h('span', { class: 'source-cell' }, row.source)
  },
  {
    title: '延迟',
    key: 'latency_ms',
    width: 90,
    render: (row) => (row.alive ? h('span', {}, `${row.latency_ms || 0} ms`) : h('span', { class: 'muted' }, '—'))
  },
  {
    title: '速度',
    key: 'speed_bps',
    width: 110,
    render: (row) => (row.alive ? h('span', { class: row.speed_bps > 0 ? 'speed-cell' : 'muted' }, formatSpeed(row.speed_bps)) : h('span', { class: 'muted' }, '—'))
  },
  {
    title: '状态',
    key: 'alive',
    width: 90,
    render: (row) =>
      row.alive
        ? h(NTag, { type: 'success', size: 'small', bordered: false }, { default: () => '可用' })
        : h(NTag, { type: 'default', size: 'small', bordered: false }, { default: () => '失效' })
  },
  {
    title: '最后存活',
    key: 'last_alive_at',
    width: 170,
    render: (row) => (row.last_alive_at ? formatTime(row.last_alive_at) : '—')
  }
]

// ---- 数据加载 ----
async function loadProxyData() {
  listLoading.value = true
  try {
    const [listRes, statsRes] = await Promise.all([listProxies({ limit: 500 }), getProxyStats()])
    proxies.value = listRes.proxies
    stats.value = statsRes
  } catch (e) {
    message.error(extractErrorMessage(e, '获取代理列表失败'))
  } finally {
    listLoading.value = false
  }
}

async function handleManualRefresh() {
  refreshing.value = true
  try {
    await refreshProxies()
    message.success('代理池刷新已开始，稍候查看')
    setTimeout(() => loadProxyData(), 3000)
  } catch (e) {
    message.error(extractErrorMessage(e, '触发刷新失败'))
  } finally {
    refreshing.value = false
  }
}

async function handleSpeedTest() {
  const targets = speedCandidates.value
  if (!targets.length) {
    message.warning('没有可测速的可用代理')
    return
  }
  speedTesting.value = true
  try {
    await speedTestProxies({ proxies: targets.map((p) => p.addr) })
    message.success(`已对 ${targets.length} 个代理发起测速，稍候刷新查看结果`)
    setTimeout(() => loadProxyData(), 3000)
  } catch (e) {
    message.error(extractErrorMessage(e, '测速请求失败'))
  } finally {
    speedTesting.value = false
  }
}

onMounted(loadProxyData)
</script>

<style scoped>
.proxies-page {
  display: flex;
  flex-direction: column;
  gap: 16px;
  max-width: 1200px;
}

.stat-card {
  background: #fff;
}

.stat-inner {
  display: flex;
  align-items: center;
  gap: 12px;
}

.stat-icon {
  width: 42px;
  height: 42px;
  border-radius: 10px;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}

.stat-label {
  font-size: 12px;
  color: #8492a6;
}

.stat-value {
  font-size: 22px;
  font-weight: 700;
  color: #1f2d3d;
  font-family: 'JetBrains Mono', Consolas, monospace;
}

.stat-value-sm {
  font-size: 15px;
}

.toolbar-card,
.proxy-table-card {
  background: #fff;
}

.toolbar {
  display: flex;
  flex-wrap: wrap;
}

.toolbar-err {
  max-width: 320px;
}

.toolbar-actions {
  margin-left: auto;
  display: flex;
  gap: 8px;
}

.source-select {
  width: 220px;
}

.sort-select {
  width: 130px;
}

.source-cell {
  font-size: 12px;
  color: #8492a6;
}

.mono-inline {
  font-family: 'JetBrains Mono', Consolas, monospace;
  font-size: 12px;
}

.speed-cell {
  color: #18a058;
  font-weight: 600;
}

.muted {
  color: #c0c4cc;
}
</style>