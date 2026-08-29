<!--
web/src/views/CookieTestsView.vue
页面：Cookie 可用性测试
职责：
- 站点 Cookie 与 TaBiAI 凭据使用两个独立 Tab；后端同一时刻只跑一个任务
- 检测在服务端后台执行，本页每 2s 轮询进度，可手动停止
- 代理类失败由服务端换代理无限重试，只做凭据可用性检查，不执行真正签到
- 签到进程在跑时锁住 TaBiAI 检测：那也是一次真 refresh，两边同时推进代次会让
  旧代被判重放、整条会话被站点撤销。站点 Cookie 是静态凭据，不受此限
数据来源：
- POST /api/cookie-tests/newapi
- POST /api/cookie-tests/tabiai
- GET  /api/cookie-tests/status
- POST /api/cookie-tests/stop
- GET  /api/run-state
- POST /api/run-state/unlock
-->
<template>
  <div class="page-container cookie-tests-page">
    <n-alert type="warning" :bordered="false" class="intro-alert">
      检测在服务端后台执行并走服务器代理池：遇到代理或 CDN 拦截会自动换出口重试，只有站点/凭据本身给出结论才会停止，
      因此需要时可点「停止检测」。浏览器不会接触 Cookie 明文。
    </n-alert>

    <!-- 签到锁：只影响 TaBiAI，所以放在页面顶部统一说明，Tab 里再禁用按钮 -->
    <n-alert v-if="runLock.running" type="error" :bordered="false" class="lock-alert">
      <template #header>TaBiAI 凭据操作已锁定</template>
      <div class="lock-body">
        <div>{{ lockSummary }}</div>
        <div v-if="runLock.started_at" class="lock-meta">
          开始于 {{ formatTime(runLock.started_at) }}
          <span v-if="runLock.heartbeat_at">· 最后心跳 {{ formatTime(runLock.heartbeat_at) }}</span>
        </div>
        <n-button size="small" type="error" ghost :loading="unlocking" @click="confirmUnlock">
          确认没在跑？强制解锁
        </n-button>
      </div>
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
            <n-tag v-if="runLock.running" size="small" type="error" :bordered="false">
              签到进行中，已锁定
            </n-tag>
            <template v-if="tabiaiSummary">
              <n-tag size="small" type="success" :bordered="false">有效 {{ tabiaiSummary.valid }}</n-tag>
              <n-tag size="small" type="error" :bordered="false">失效 {{ tabiaiSummary.invalid }}</n-tag>
              <n-tag size="small" type="warning" :bordered="false">异常 {{ tabiaiSummary.abnormal }}</n-tag>
              <n-tag v-if="tabiaiSummary.skipped" size="small" :bordered="false">跳过 {{ tabiaiSummary.skipped }}</n-tag>
              <span v-if="tabiaiCheckedAt" class="checked-at">上次检测：{{ formatTime(tabiaiCheckedAt) }}</span>
            </template>
          </n-space>
        </div>

        <!-- 一键签发：名单来自 GET /api/tabiai/expired（读库里保活的判定，不跑检测），
             所以刷新页面也在，不依赖本次是否点过「开始检测」 -->
        <n-card size="small" class="reissue-card">
          <n-space align="center" :size="10" wrap>
            <n-button size="small" :loading="loadingExpired" :disabled="reissuing"
                      @click="loadExpired">
              查询失效账号
            </n-button>
            <n-button size="small" type="warning" :loading="reissuing"
                      :disabled="!reissuableNames.length || runLock.running"
                      @click="reissueAllExpired">
              一键签发失效账号（{{ reissuableNames.length }}）
            </n-button>
            <span v-if="expiredLoadedAt" class="checked-at">
              失效 {{ expired.length }} 个<template v-if="expiredWithoutSession.length">
                ，其中 {{ expiredWithoutSession.length }} 个未填 user_session 需人工处理</template>
            </span>
            <n-tag v-if="runLock.running" size="small" type="error" :bordered="false">
              签到进行中，人工签发已锁定
            </n-tag>
          </n-space>
          <div v-if="reissueLog.length" class="reissue-log">
            <div v-for="line in reissueLog" :key="line.name"
                 :class="['reissue-line', line.ok ? 'is-ok' : 'is-fail']">
              {{ line.ok ? '✓' : '✕' }} {{ line.name }}<template v-if="line.detail">：{{ line.detail }}</template>
            </div>
          </div>
        </n-card>

        <cookie-test-panel
          mode="tabiai"
          :accounts="tabiaiAccounts"
          :results="tabiaiResults"
          :loading="starting === 'tabiai'"
          :running="runningMode === 'tabiai'"
          :stopping="stopping"
          :busy="busyWith('tabiai') || runLock.running"
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
import { NAlert, NButton, NCard, NSpace, NTabPane, NTabs, NTag, useDialog, useMessage } from 'naive-ui'
import CookieTestPanel from '@/components/CookieTestPanel.vue'
import {
  getCookieTestStatus,
  issueTabiAICookie,
  listExpiredTabiAI,
  startNewAPICookieTest,
  startTabiAICookieTest,
  stopCookieTest,
  type ExpiredTabiAIAccount
} from '@/api/cookieTests'
import { getRunState, unlockRunState } from '@/api/runState'
import { useConfigStore } from '@/stores/config'
import { extractErrorMessage } from '@/utils/error'
import {
  belongsToMode,
  isBusyWithOtherMode,
  justFinished,
  snapshotFromStatus,
  type CookieTestSnapshot
} from '@/utils/cookieTestPolling'
import {
  RUN_LOCK_POLL_INTERVAL,
  idleRunState,
  runLockFromError,
  runLockSummary,
  shouldPollRunLock
} from '@/utils/runLock'
import type {
  Account, CookieTestMode, CookieTestResult, CookieTestStatus, CookieTestSummary, RunState
} from '@/types'

const POLL_INTERVAL = 2000

const configStore = useConfigStore()
const dialog = useDialog()
const message = useMessage()

const activeMode = ref<CookieTestMode>('newapi_cookie')
const starting = ref<CookieTestMode | ''>('')
const stopping = ref(false)

// ---- 一键签发失效凭据 ----
// 名单来自 GET /api/tabiai/expired（读库里保活写下的判定，不触发检测），
// 所以刷新页面还在，也不必先跑一遍几十秒的凭据检测。
const expired = ref<ExpiredTabiAIAccount[]>([])
const expiredLoadedAt = ref('')
const loadingExpired = ref(false)
const reissuing = ref(false)
const reissueLog = ref<Array<{ name: string; ok: boolean; detail: string }>>([])

// 只有填了 user_session 的才能自动签发；没填的列出来提示人工处理，不混进批量里
const reissuableNames = computed(() =>
  expired.value.filter((a) => a.has_user_session).map((a) => a.name)
)
const expiredWithoutSession = computed(() => expired.value.filter((a) => !a.has_user_session))

// 后端只有一个任务，这里按模式分别缓存最近一次结果，切 Tab 不会互相覆盖
const newapiResults = ref<CookieTestResult[]>([])
const tabiaiResults = ref<CookieTestResult[]>([])
const newapiSummary = ref<CookieTestSummary | null>(null)
const tabiaiSummary = ref<CookieTestSummary | null>(null)
const newapiCheckedAt = ref('')
const tabiaiCheckedAt = ref('')

const snapshot = ref<CookieTestSnapshot | null>(null)
const round = ref(0)

// 签到锁：只影响 TaBiAI。running 由后端判活，这里不自己拿时间算
const runLock = ref<RunState>(idleRunState())
const unlocking = ref(false)
// 让「还剩几分钟」跟着时钟走，否则页面开着不动文案就一直是刚拉到时的值
const lockClock = ref(Date.now())
const lockSummary = computed(() => runLockSummary(runLock.value, lockClock.value))
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
    // 按钮禁用只能挡住「已知在锁」的情况：签到恰好在两次轮询之间开跑时仍会撞 409。
    // 后端会把最新锁状态一起回传，直接换上，省一次往返
    const locked = runLockFromError(error)
    if (locked) {
      runLock.value = locked
      lockClock.value = Date.now()
    }
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

// ---------------------------------------------------------------------------
// 一键签发失效凭据
// ---------------------------------------------------------------------------

async function loadExpired() {
  loadingExpired.value = true
  try {
    const result = await listExpiredTabiAI()
    expired.value = result.accounts
    expiredLoadedAt.value = result.checked_at || new Date().toISOString()
    reissueLog.value = []
    if (!result.count) {
      message.success('没有凭据失效的账号')
    } else if (!reissuableNames.value.length) {
      message.warning(`${result.count} 个账号失效，但都没填 GitHub user_session，只能人工粘贴新凭据`)
    } else {
      message.info(`${result.count} 个账号失效，其中 ${reissuableNames.value.length} 个可自动签发`)
    }
  } catch (error) {
    message.error(extractErrorMessage(error, '查询失效账号失败'))
  } finally {
    loadingExpired.value = false
  }
}

/**
 * 串行逐个签发。
 *
 * 不并发是刻意的：签发要走 GitHub OAuth 三步，并发打 GitHub 容易触发限流
 * （403/429），一次失败一批还不如慢点全成。单个失败不中断后面的，
 * 每条结果都留在页面上，好对照哪个账号还需要人工处理。
 */
async function reissueAllExpired() {
  const names = reissuableNames.value
  if (!names.length) return
  reissuing.value = true
  reissueLog.value = []
  let okCount = 0
  try {
    for (const name of names) {
      try {
        await issueTabiAICookie(name)
        okCount += 1
        reissueLog.value.push({ name, ok: true, detail: '已签发新凭据' })
      } catch (error) {
        reissueLog.value.push({ name, ok: false, detail: extractErrorMessage(error, '签发失败') })
      }
    }
    if (okCount === names.length) {
      message.success(`${okCount} 个账号已重新签发`)
    } else {
      message.warning(`${okCount}/${names.length} 个签发成功，其余原因见下方列表`)
    }
    // 签发完重查一次：成功的账号保活状态还没更新，但失败的仍在名单里，
    // 重查能让「还剩谁要处理」一目了然
    await loadExpired()
  } finally {
    reissuing.value = false
  }
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

// ---------------------------------------------------------------------------
// 签到锁
// ---------------------------------------------------------------------------

let lockTimer: ReturnType<typeof setInterval> | null = null
let lockInFlight = false

async function pollRunLockOnce() {
  const ctx = {
    hidden: typeof document !== 'undefined' && document.hidden,
    inFlight: lockInFlight
  }
  if (!shouldPollRunLock(ctx)) return
  lockInFlight = true
  try {
    runLock.value = await getRunState()
    lockClock.value = Date.now()
  } catch {
    // 查不到锁状态时不改本地值也不刷错误：这只是个提示，别打断用户做别的事
  } finally {
    lockInFlight = false
  }
}

function startLockPolling() {
  if (lockTimer !== null) return
  lockTimer = setInterval(pollRunLockOnce, RUN_LOCK_POLL_INTERVAL)
}

function stopLockPolling() {
  if (lockTimer !== null) {
    clearInterval(lockTimer)
    lockTimer = null
  }
}

function confirmUnlock() {
  dialog.error({
    title: '强制解锁前请确认签到真的停了',
    content: () =>
      '这把锁是为了防止两边同时动同一条 new_api_refresh。'
      + '如果签到其实还在跑，解锁后再做检测或签发，站点会把旧代次判为重放，'
      + '整条 TaBiAI 会话被直接撤销 —— 届时所有 tabiai 账号都会签到失败，'
      + '必须重新签发凭据才能恢复。\n\n'
      + '只有在确认对方进程已经结束（例如 Actions 显示已完成/已取消）时才继续。',
    positiveText: '我确认已停止，强制解锁',
    negativeText: '再等等',
    onPositiveClick: () => {
      void doUnlock()
    }
  })
}

async function doUnlock() {
  unlocking.value = true
  try {
    await unlockRunState()
    await pollRunLockOnce()
    message.success('已强制解锁；若签到仍在运行，它下次心跳会重新上锁')
  } catch (error) {
    message.error(extractErrorMessage(error, '强制解锁失败'))
  } finally {
    unlocking.value = false
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
  await pollRunLockOnce()
  startLockPolling()
})

onBeforeUnmount(() => {
  stopPolling()
  stopLockPolling()
})
</script>

<style scoped>
.cookie-tests-page {
  display: flex;
  flex-direction: column;
  gap: 16px;
  max-width: 1280px;
}

/* 锁定提示要足够显眼：用户很可能是点了按钮没反应才来看这里 */
.lock-body {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 8px;
}

.lock-meta {
  color: #8492a6;
  font-size: 12px;
  line-height: 1.5;
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

.reissue-card {
  margin: 0 0 12px;
  border-radius: 10px;
}

.reissue-log {
  margin-top: 10px;
  max-height: 180px;
  overflow-y: auto;
  font-size: 12px;
  line-height: 1.7;
}

/* 成败用颜色区分，逐条列出来 —— 批量操作最怕「点完不知道哪个没成」 */
.reissue-line.is-ok {
  color: #18a058;
}

.reissue-line.is-fail {
  color: #d03050;
}
</style>
