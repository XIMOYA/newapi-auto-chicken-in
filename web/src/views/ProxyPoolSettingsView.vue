<!--
web/src/views/ProxyPoolSettingsView.vue
页面：代理池配置
职责：编辑 proxy_pool 模块（enabled/test_url/timeout/max_workers/max_proxies/ip_swap_limit/sources）
- sources 为动态列表（增删）
数据来源：GET/PUT /api/config
-->
<template>
  <div class="page-container">
    <config-card
      title="代理池配置"
      description="配置代理池：定期抓取、测试并维护可用隧道(proxy)列表，供签到任务自动换隧道使用。"
      :loading="!configStore.config"
      :saving="saving"
      :updated-at="configStore.updatedAt"
      :show-reset="true"
      compact
      :dirty="isDirty"
      @save="handleSave"
      @reset="handleReset"
    >
      <n-form label-placement="top" :show-require-mark="false" class="settings-form">
        <n-form-item label="启用代理池">
          <n-switch v-model:value="form.enabled" />
          <span class="switch-tip">{{ form.enabled ? '已启用' : '已停用' }}</span>
        </n-form-item>
        <n-form-item label="隧道连通性测试 URL">
          <n-input v-model:value="form.test_url" placeholder="例如 https://api.ipify.org" />
        </n-form-item>
        <n-form-item label="测试超时（秒）">
          <n-input-number v-model:value="form.timeout" :min="1" :max="120" class="num-input" />
        </n-form-item>
        <n-form-item label="最大并发测试数">
          <n-input-number v-model:value="form.max_workers" :min="1" :max="500" class="num-input" />
        </n-form-item>
        <n-form-item label="代理池容量上限">
          <n-input-number v-model:value="form.max_proxies" :min="1" :max="5000" class="num-input" />
        </n-form-item>
        <n-form-item label="IP 更换阈值（次）">
          <n-input-number v-model:value="form.ip_swap_limit" :min="0" :max="100" class="num-input" />
          <span class="switch-tip">单个隧道使用达到该次数后更换</span>
        </n-form-item>
        <n-form-item label="代理来源 Sources">
          <dynamic-string-list
            v-model="form.sources"
            placeholder="例如 https://proxylist.example.com/list.txt"
            add-label="添加代理来源"
          />
        </n-form-item>

        <n-divider>服务器端代理池（预取）</n-divider>
        <n-alert type="info" :bordered="false" class="server-proxy-tip">
          <template #icon><n-icon><server-outline /></n-icon></template>
          配置网站服务会定期抓取以上代理源、测通并保存可用列表。GitHub Actions 签到前可
          <n-text code>GET /api/proxies/available</n-text> 直接预取现成列表，省去现场抓取测通。
        </n-alert>
        <n-form-item label="后台刷新间隔（分钟）">
          <n-input-number v-model:value="form.refresh_minutes" :min="0" :max="1440" class="num-input" />
          <span class="switch-tip">0 = 关闭后台自动刷新（仍可手动刷新）</span>
        </n-form-item>
        <n-form-item label="可用代理保存数量">
          <n-input-number v-model:value="form.save_limit" :min="1" :max="2000" class="num-input" />
          <span class="switch-tip">最多保留多少条测通可用的代理</span>
        </n-form-item>
        <n-form-item label="后台刷新时测通">
          <n-switch v-model:value="form.auto_test" />
          <span class="switch-tip">{{ form.auto_test ? '测通 + 测延迟' : '仅抓取不测通' }}</span>
        </n-form-item>
        <n-form-item label="Actions 预取地址">
          <n-input v-model:value="form.remote_url" placeholder="https://你的域名/api/proxies/available（Actions 预取用）" />
        </n-form-item>
      </n-form>
    </config-card>

    <!-- 代理池实时状态：统计 + 列表 + 手动刷新 -->
    <n-card :bordered="false" class="proxy-list-card">
      <template #header>
        <div class="card-header">
          <span class="card-title">代理池状态</span>
          <n-tag v-if="stats" size="small" type="info" :bordered="false">可用 {{ stats.alive }} / 共 {{ stats.total }}</n-tag>
        </div>
      </template>
      <template #header-extra>
        <n-button size="small" :loading="refreshing" @click="handleManualRefresh">
          <template #icon><n-icon><refresh-outline /></n-icon></template>
          立即刷新
        </n-button>
      </template>

      <div class="proxy-stats-row">
        <n-statistic label="可用代理" :value="stats?.alive ?? 0">
          <template #suffix>条</template>
        </n-statistic>
        <n-statistic label="总条目" :value="stats?.total ?? 0">
          <template #suffix>条</template>
        </n-statistic>
        <n-statistic label="上次刷新" :value="formatTime(stats?.last_run)">
        </n-statistic>
        <n-tag v-if="stats?.running" type="warning" :bordered="false">刷新中…</n-tag>
        <n-alert v-if="stats?.last_error" type="error" :bordered="false" class="proxy-err">
          上次刷新失败: {{ stats.last_error }}
        </n-alert>
      </div>

      <n-space :size="8" class="proxy-filter">
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
      </n-space>

      <n-data-table
        :columns="proxyColumns"
        :data="filteredProxies"
        :loading="listLoading"
        :pagination="proxyPagination"
        striped
        :bordered="false"
        size="small"
        :scroll-x="820"
      >
        <template #empty>
          <n-empty v-if="!listLoading" description="暂无代理数据，点击右上角「立即刷新」抓取" />
        </template>
      </n-data-table>
    </n-card>
  </div>
</template>

<script setup lang="ts">
import { computed, h, onMounted, reactive, ref, watch } from 'vue'
import {
  NForm, NFormItem, NInput, NInputNumber, NSwitch, NDivider, NAlert, NIcon, NText,
  NCard, NButton, NTag, NStatistic, NDataTable, NRadioGroup, NRadioButton, NSelect, NSpace, NEmpty,
  useMessage, type DataTableColumns, type PaginationProps, type SelectOption
} from 'naive-ui'
import { ServerOutline, RefreshOutline } from '@vicons/ionicons5'
import ConfigCard from '@/components/ConfigCard.vue'
import DynamicStringList from '@/components/DynamicStringList.vue'
import { useConfigStore } from '@/stores/config'
import { useDirtyGuard } from '@/composables/useDirtyGuard'
import { deepClone } from '@/utils/clone'
import { extractErrorMessage } from '@/utils/error'
import { listProxies, getProxyStats, refreshProxies } from '@/api/proxies'
import type { AppConfig, ProxyPoolConfig, ProxyEntry, ProxyStatsResult } from '@/types'

const configStore = useConfigStore()
const message = useMessage()

const saving = ref(false)
const initialized = ref(false)

// 脏检测：表单与已保存快照不一致时显示「未保存修改」，离开前弹确认
const savedSnapshot = ref('')
const isDirty = computed(() => JSON.stringify(form) !== savedSnapshot.value)
useDirtyGuard(() => isDirty.value)

const form = reactive<ProxyPoolConfig>({
  enabled: false,
  test_url: 'https://api.ipify.org',
  timeout: 8,
  max_workers: 25,
  max_proxies: 250,
  ip_swap_limit: 2,
  sources: [],
  refresh_minutes: 30,
  save_limit: 100,
  auto_test: true,
  remote_url: ''
})

function initForm(cfg: AppConfig) {
  const p = cfg.proxy_pool
  form.enabled = p.enabled
  form.test_url = p.test_url
  form.timeout = p.timeout
  form.max_workers = p.max_workers
  form.max_proxies = p.max_proxies
  form.ip_swap_limit = p.ip_swap_limit
  form.sources = [...(p.sources ?? [])]
  form.refresh_minutes = p.refresh_minutes ?? 30
  form.save_limit = p.save_limit ?? 100
  form.auto_test = p.auto_test ?? true
  form.remote_url = p.remote_url ?? ''
  savedSnapshot.value = JSON.stringify(form)
}

watch(
  () => configStore.config,
  (cfg) => {
    if (cfg && !initialized.value) {
      initForm(cfg)
      initialized.value = true
    }
  },
  { immediate: true }
)

function handleReset() {
  if (configStore.config) initForm(configStore.config)
}

async function handleSave() {
  if (!configStore.config) return
  saving.value = true
  try {
    const next = deepClone(configStore.config)
    next.proxy_pool = {
      ...form,
      sources: form.sources.map((s) => s.trim()).filter((s) => s !== '')
    }
    await configStore.save(next)
    savedSnapshot.value = JSON.stringify(form)
    message.success('代理池配置已保存')
  } catch (e) {
    message.error(extractErrorMessage(e, '代理池配置保存失败'))
  } finally {
    saving.value = false
  }
}

// ---- 代理池状态 ----
const stats = ref<ProxyStatsResult | null>(null)
const proxies = ref<ProxyEntry[]>([])
const listLoading = ref(false)
const refreshing = ref(false)
const aliveFilter = ref<'all' | 'alive'>('all')
const sourceFilter = ref<string | null>(null)

const proxyPagination: PaginationProps = reactive({
  pageSize: 20,
  showSizePicker: true,
  pageSizes: [10, 20, 50, 100],
  onUpdatePageSize: (size: number) => {
    proxyPagination.pageSize = size
  }
})

const sourceOptions = computed<SelectOption[]>(() => {
  const set = new Set<string>()
  proxies.value.forEach((p) => set.add(p.source))
  return [...set].map((s) => ({ label: s, value: s }))
})

const filteredProxies = computed(() => {
  let out = proxies.value
  if (aliveFilter.value === 'alive') out = out.filter((p) => p.alive)
  if (sourceFilter.value) out = out.filter((p) => p.source === sourceFilter.value)
  return out
})

function formatTime(t?: string) {
  if (!t) return '—'
  const d = new Date(t)
  if (Number.isNaN(d.getTime())) return t
  return d.toLocaleString('zh-CN', { hour12: false })
}

function latencyLabel(ms: number, alive: boolean) {
  if (!alive) return '—'
  return `${ms} ms`
}

const proxyColumns: DataTableColumns<ProxyEntry> = [
  {
    title: '代理地址',
    key: 'addr',
    width: 180,
    render: (row) => h('span', { class: 'mono-inline' }, row.addr)
  },
  {
    title: '来源',
    key: 'source',
    width: 320,
    ellipsis: { tooltip: true },
    render: (row) => h('span', { class: 'source-cell' }, row.source)
  },
  {
    title: '延迟',
    key: 'latency_ms',
    width: 100,
    render: (row) => latencyLabel(row.latency_ms, row.alive)
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
    width: 180,
    render: (row) => (row.last_alive_at ? formatTime(row.last_alive_at) : '—')
  }
]

async function loadProxyData() {
  listLoading.value = true
  try {
    const [listRes, statsRes] = await Promise.all([listProxies({ limit: 500 }), getProxyStats()])
    proxies.value = listRes.proxies
    stats.value = statsRes
  } catch (e) {
    message.error(extractErrorMessage(e, '获取代理池状态失败'))
  } finally {
    listLoading.value = false
  }
}

async function handleManualRefresh() {
  refreshing.value = true
  try {
    await refreshProxies()
    message.success('代理池刷新已开始，稍等几秒再查看')
    // 延迟 3 秒后拉取最新结果（给后台一点抓取时间）
    setTimeout(() => loadProxyData(), 3000)
  } catch (e) {
    message.error(extractErrorMessage(e, '触发刷新失败'))
  } finally {
    refreshing.value = false
  }
}

onMounted(loadProxyData)
</script>

<style scoped>
.switch-tip {
  margin-left: 10px;
  font-size: 13px;
  color: #8492a6;
}

.settings-form {
  max-width: 560px;
}

.proxy-list-card {
  background: #fff;
}

.card-header {
  display: flex;
  align-items: center;
  gap: 12px;
}

.card-title {
  font-size: 16px;
  font-weight: 600;
  color: #1f2d3d;
}

.proxy-stats-row {
  display: flex;
  align-items: center;
  gap: 32px;
  margin-bottom: 16px;
  flex-wrap: wrap;
}

.proxy-err {
  margin-top: 8px;
  max-width: 420px;
}

.proxy-filter {
  margin-bottom: 12px;
}

.source-select {
  width: 240px;
}

.source-cell {
  font-size: 12px;
  color: #8492a6;
}

.mono-inline {
  font-family: 'JetBrains Mono', Consolas, monospace;
  font-size: 12px;
}

.num-input {
  width: 200px;
}
</style>
