<!--
web/src/views/CookieTestsView.vue
页面：Cookie 可用性测试
职责：
- 站点 Cookie 与 TaBiAI 凭据使用两个独立 Tab；后端同一时刻只跑一个任务
- 检测在服务端后台执行，本页每 2s 轮询进度，可手动停止
- 代理类失败由服务端换代理无限重试，只做凭据可用性检查，不执行真正签到
数据来源：
- POST /api/cookie-tests/newapi
- POST /api/cookie-tests/tabiai
- GET  /api/cookie-tests/status
- POST /api/cookie-tests/stop
-->
<template>
  <div class="page-container cookie-tests-page">
    <n-alert type="warning" :bordered="false" class="intro-alert">
      检测在服务端后台执行并走服务器代理池：遇到代理或 CDN 拦截会自动换出口重试，只有站点/凭据本身给出结论才会停止，
      因此需要时可点「停止检测」。浏览器不会接触 Cookie 明文。
    </n-alert>

    <n-tabs v-model:value="activeMode" type="line" animated>
      <n-tab-pane name="newapi_cookie" tab="站点 Cookie">
        <div class="tab-summary">
          <n-space :size="8" align="center">
            <n-tag size="small" type="info" :bordered="false">可检测 {{ newapiAccounts.length }} 个启用账号</n-tag>
            <template v-if="newapiSummary">
              <n-tag size="small" type="success" :bordered="false">有效 {{ newapiSummary.valid }}</n-tag>
              <n-tag size="small" type="error" :bordered="false">失效 {{ newapiSummary.invalid }}</n-tag>
              <n-tag size="small" type="warning" :bordered="false">异常 {{ newapiSummary.abnormal }}</n-tag>
              <n-tag v-if="newapiSummary.skipped" size="small" :bordered="false">跳过 {{ newapiSummary.skipped }}</n-tag>
              <span v-if="newapiCheckedAt" class="checked-at">上次检测：{{ formatTime(newapiCheckedAt) }}</span>
            </template>
          </n-space>
        </div>
        <cookie-test-panel
          mode="newapi_cookie"
          :accounts="newapiAccounts"
          :results="newapiResults"
          :loading="starting === 'newapi_cookie'"
          :running="runningMode === 'newapi_cookie'"
          :stopping="stopping"
          :busy="busyWith('newapi_cookie')"
          :round="runningMode === 'newapi_cookie' ? round : 0"
          :settled="newapiSettled"
          :total="newapiTotal"
          @run="runNewAPITest"
          @stop="handleStop"
        />
      </n-tab-pane>

      <n-tab-pane name="tabiai" tab="TaBiAI 凭据">
        <div class="tab-summary">
          <n-space :size="8" align="center">
            <n-tag size="small" type="info" :bordered="false">可检测 {{ tabiaiAccounts.length }} 个启用账号</n-tag>
            <template v-if="tabiaiSummary">
              <n-tag size="small" type="success" :bordered="false">有效 {{ tabiaiSummary.valid }}</n-tag>
              <n-tag size="small" type="error" :bordered="false">失效 {{ tabiaiSummary.invalid }}</n-tag>
              <n-tag size="small" type="warning" :bordered="false">异常 {{ tabiaiSummary.abnormal }}</n-tag>
              <n-tag v-if="tabiaiSummary.skipped" size="small" :bordered="false">跳过 {{ tabiaiSummary.skipped }}</n-tag>
              <span v-if="tabiaiCheckedAt" class="checked-at">上次检测：{{ formatTime(tabiaiCheckedAt) }}</span>
            </template>
          </n-space>
        </div>
        <cookie-test-panel
          mode="tabiai"
          :accounts="tabiaiAccounts"
          :results="tabiaiResults"
          :loading="starting === 'tabiai'"
          :running="runningMode === 'tabiai'"
          :stopping="stopping"
          :busy="busyWith('tabiai')"
          :round="runningMode === 'tabiai' ? round : 0"
          :settled="tabiaiSettled"
          :total="tabiaiTotal"
          @run="runTabiAITest"
          @stop="handleStop"
        />
      </n-tab-pane>
    </n-tabs>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { NAlert, NSpace, NTabPane, NTabs, NTag, useMessage } from 'naive-ui'
import CookieTestPanel from '@/components/CookieTestPanel.vue'
import {
  getCookieTestStatus,
  startNewAPICookieTest,
  startTabiAICookieTest,
  stopCookieTest
} from '@/api/cookieTests'
import { useConfigStore } from '@/stores/config'
import { extractErrorMessage } from '@/utils/error'
import {
  belongsToMode,
  isBusyWithOtherMode,
  justFinished,
  snapshotFromStatus,
  type CookieTestSnapshot
} from '@/utils/cookieTestPolling'
import type { Account, CookieTestMode, CookieTestResult, CookieTestStatus, CookieTestSummary } from '@/types'

const POLL_INTERVAL = 2000

const configStore = useConfigStore()
const message = useMessage()

const activeMode = ref<CookieTestMode>('newapi_cookie')
const starting = ref<CookieTestMode | ''>('')
const stopping = ref(false)

// 后端只有一个任务，这里按模式分别缓存最近一次结果，切 Tab 不会互相覆盖
const newapiResults = ref<CookieTestResult[]>([])
const tabiaiResults = ref<CookieTestResult[]>([])
const newapiSummary = ref<CookieTestSummary | null>(null)
const tabiaiSummary = ref<CookieTestSummary | null>(null)
const newapiCheckedAt = ref('')
const tabiaiCheckedAt = ref('')

const snapshot = ref<CookieTestSnapshot | null>(null)
const round = ref(0)
let prevSnapshot: CookieTestSnapshot | null = null
let timer: ReturnType<typeof setInterval> | null = null

const accounts = computed<Account[]>(() => configStore.config?.accounts ?? [])
const newapiAccounts = computed(() => accounts.value.filter((a) => a.enabled && a.login_method !== 'tabiai'))
const tabiaiAccounts = computed(() => accounts.value.filter((a) => a.enabled && a.login_method === 'tabiai'))

const runningMode = computed<CookieTestMode | ''>(() =>
  snapshot.value?.running ? (snapshot.value.mode as CookieTestMode) : ''
)
const newapiSettled = computed(() => settledOf('newapi_cookie', newapiSummary.value))
const tabiaiSettled = computed(() => settledOf('tabiai', tabiaiSummary.value))
const newapiTotal = computed(() => newapiSummary.value?.total ?? newapiAccounts.value.length)
const tabiaiTotal = computed(() => tabiaiSummary.value?.total ?? tabiaiAccounts.value.length)

function settledOf(mode: CookieTestMode, summary: CookieTestSummary | null) {
  if (runningMode.value === mode && snapshot.value) return snapshot.value.settled
  if (!summary) return 0
  return summary.valid + summary.invalid + summary.abnormal + summary.skipped
}

function busyWith(mode: CookieTestMode) {
  return isBusyWithOtherMode(snapshot.value, mode)
}

function formatTime(value: string) {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString('zh-CN', { hour12: false })
}

/** 把一次状态响应落到对应模式的缓存里 */
function applyStatus(status: CookieTestStatus) {
  const next = snapshotFromStatus(status)
  snapshot.value = next
  round.value = next.round

  if (belongsToMode(next, 'newapi_cookie')) {
    newapiResults.value = status.results
    newapiSummary.value = status.summary
    if (status.checked_at) newapiCheckedAt.value = status.checked_at
  } else if (belongsToMode(next, 'tabiai')) {
    tabiaiResults.value = status.results
    tabiaiSummary.value = status.summary
    if (status.checked_at) tabiaiCheckedAt.value = status.checked_at
  }

  if (justFinished(prevSnapshot, next)) {
    stopPolling()
    stopping.value = false
    const label = next.mode === 'tabiai' ? 'TaBiAI 凭据' : '站点 Cookie'
    const { valid, invalid, abnormal, skipped } = status.summary
    const extra = skipped ? `，跳过 ${skipped}` : ''
    const prefix = next.stopped ? `${label}检测已停止` : `${label}检测完成`
    message.success(`${prefix}：有效 ${valid}，失效 ${invalid}，异常 ${abnormal}${extra}`)
    if (status.last_error) message.warning(status.last_error)
  }
  prevSnapshot = next
}

async function pollOnce() {
  try {
    applyStatus(await getCookieTestStatus())
  } catch (error) {
    stopPolling()
    message.error(extractErrorMessage(error, '读取检测进度失败'))
  }
}

function startPolling() {
  if (timer !== null) return
  timer = setInterval(pollOnce, POLL_INTERVAL)
}

function stopPolling() {
  if (timer !== null) {
    clearInterval(timer)
    timer = null
  }
}

async function start(mode: CookieTestMode, accountNames: string[]) {
  starting.value = mode
  try {
    if (mode === 'tabiai') {
      await startTabiAICookieTest(accountNames)
    } else {
      await startNewAPICookieTest(accountNames)
    }
    // 立即拉一次，让表格马上进入「排队中/检测中」，不用等第一个轮询周期
    prevSnapshot = null
    await pollOnce()
    startPolling()
  } catch (error) {
    message.error(extractErrorMessage(error, '启动检测失败'))
  } finally {
    starting.value = ''
  }
}

function runNewAPITest(accountNames: string[]) {
  void start('newapi_cookie', accountNames)
}

function runTabiAITest(accountNames: string[]) {
  void start('tabiai', accountNames)
}

async function handleStop() {
  stopping.value = true
  try {
    await stopCookieTest()
    await pollOnce()
  } catch (error) {
    message.error(extractErrorMessage(error, '停止检测失败'))
    stopping.value = false
  }
}

onMounted(async () => {
  // 页面刷新后若后台任务仍在跑，直接接管展示并继续轮询
  await pollOnce()
  if (snapshot.value?.running) {
    prevSnapshot = snapshot.value
    if (snapshot.value.mode === 'tabiai' || snapshot.value.mode === 'newapi_cookie') {
      activeMode.value = snapshot.value.mode
    }
    startPolling()
  }
})

onBeforeUnmount(stopPolling)
</script>

<style scoped>
.cookie-tests-page {
  display: flex;
  flex-direction: column;
  gap: 16px;
  max-width: 1280px;
}

.intro-alert {
  margin-bottom: 0;
}

.tab-summary {
  margin: 2px 0 12px;
}

.checked-at {
  color: #8492a6;
  font-size: 12px;
}
</style>
