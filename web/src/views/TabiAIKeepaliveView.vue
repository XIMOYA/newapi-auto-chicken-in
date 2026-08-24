<!--
web/src/views/TabiAIKeepaliveView.vue
页面：TaBiAI 凭据保活
职责：
- 展示与修改保活策略（开关 + 间隔分钟数，服务端会夹到 15~720）
- 展示每个 tabiai 账号最后一次刷新的结果、是否真的换了代次、用的哪个代理
- 支持按需核实平台库里那条凭据的指纹，确认客户端回写的代次确实落了库
- 凭据失效的账号会被暂停，页面要说清恢复条件：改过凭据后的第一次刷新自动恢复
- 支持立刻手动刷一轮；签到进行中服务端会整轮避让，此时按钮禁用并给出说明
为什么需要保活：
- new_api_refresh 的 secret 每 refresh 一次换一代，旧代只有分钟级宽限窗口。
  签到一天一次，中间十几个小时那一代一直躺着，任何第三方碰一下就可能让它作废。
  按节奏主动刷新并立刻落库，把暴露窗口压到一个间隔之内
数据来源：
- GET  /api/tabiai/keepalive
- PUT  /api/tabiai/keepalive
- POST /api/tabiai/keepalive/run
- GET  /api/run-state
- GET  /api/accounts/{name}（「核实凭据」按钮：只取 cookie 摘要，明文不下发）
-->
<template>
  <div class="page-container keepalive-page">
    <n-alert type="info" :bordered="false" class="intro-alert">
      保活会按间隔主动 refresh 一次 TaBiAI 凭据，让代次保持滚动并立刻存回数据库。
      签到运行期间自动整轮避让 —— 两边同时推进代次会让旧代被判重放，整条会话被站点撤销。
    </n-alert>

    <n-alert v-if="status.skipped_by_checkin" type="warning" :bordered="false" class="intro-alert">
      上一轮因为签到正在运行而被跳过，这是刻意的避让，不是故障。
    </n-alert>

    <n-card title="保活策略" size="small" class="section-card">
      <n-form label-placement="left" label-width="96" :show-feedback="false">
        <n-space vertical size="large">
          <n-form-item label="自动保活">
            <n-switch v-model:value="form.enabled" :disabled="saving" />
            <span class="hint">关闭后只能手动刷新</span>
          </n-form-item>
          <n-form-item label="刷新间隔">
            <n-input-number
              v-model:value="form.minutes"
              :min="15"
              :max="720"
              :step="15"
              :disabled="saving || !form.enabled"
              style="width: 160px"
            >
              <template #suffix>分钟</template>
            </n-input-number>
            <span class="hint">默认 90（1.5 小时）；太勤只会多消耗代次</span>
          </n-form-item>
          <n-space>
            <n-button type="primary" :loading="saving" @click="save">保存策略</n-button>
            <n-button
              :loading="running"
              :disabled="runLock.running"
              @click="runNow"
            >
              立刻刷新一轮
            </n-button>
          </n-space>
          <div v-if="runLock.running" class="hint danger">
            签到正在运行，手动刷新已禁用；等它跑完再来。
          </div>
        </n-space>
      </n-form>
    </n-card>

    <n-card title="运行情况" size="small" class="section-card">
      <n-descriptions :column="3" label-placement="top" size="small">
        <n-descriptions-item label="上次刷新">
          {{ status.last_run_at ? formatTime(status.last_run_at) : '尚未刷新' }}
        </n-descriptions-item>
        <n-descriptions-item label="下次预计">
          {{ nextRunText }}
        </n-descriptions-item>
        <n-descriptions-item label="当前状态">
          <n-tag v-if="status.running" type="info" size="small">正在刷新</n-tag>
          <n-tag v-else-if="!status.setting.enabled" size="small">已关闭</n-tag>
          <n-tag v-else type="success" size="small">待机中</n-tag>
        </n-descriptions-item>
      </n-descriptions>
    </n-card>

    <n-card title="账号状态" size="small" class="section-card">
      <template #header-extra>
        <n-space align="center" :size="8">
          <span v-if="digestUpdatedAt" class="hint">落库时间 {{ formatTime(digestUpdatedAt) }}</span>
          <n-button size="tiny" :loading="verifying" @click="verifyDigests">核实凭据</n-button>
        </n-space>
      </template>
      <n-data-table
        :columns="columns"
        :data="status.accounts"
        :bordered="false"
        :loading="loading"
        size="small"
        :row-class-name="rowClass"
      />
      <template #footer>
        <span class="hint">
          「已换代次」为否且状态正常时，说明站点这次没下发新 secret（宽限窗口内幂等），属正常现象；
          长期为否才需要怀疑回写链路。
          「平台凭据」点上面的按钮才拉取：指纹是 cookie 的 sha256 前 12 位，拿它和客户端日志里
          回写的那一代比对，一致就说明凭据确实落了库；明文不会下发到浏览器。
        </span>
      </template>
    </n-card>
  </div>
</template>

<script setup lang="ts">
import { computed, h, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import {
  NAlert,
  NButton,
  NCard,
  NDataTable,
  NDescriptions,
  NDescriptionsItem,
  NForm,
  NFormItem,
  NInputNumber,
  NSpace,
  NSwitch,
  NTag,
  useMessage
} from 'naive-ui'
import type { DataTableColumns } from 'naive-ui'
import {
  getTabiAIKeepalive,
  keepaliveStateLabel,
  keepaliveStateType,
  runTabiAIKeepalive,
  saveTabiAIKeepalive,
  type TabiAIKeepaliveRow,
  type TabiAIKeepaliveStatus
} from '@/api/tabiaiKeepalive'
import { getRunState } from '@/api/runState'
import { getAccountDetail } from '@/api/config'
import type { AccountCookieDigest } from '@/types'
import { extractErrorMessage } from '@/utils/error'
import { RUN_LOCK_POLL_INTERVAL, idleRunState } from '@/utils/runLock'

const message = useMessage()

const loading = ref(false)
const saving = ref(false)
const running = ref(false)
const verifying = ref(false)
const runLock = ref(idleRunState())

// 按账号名存 cookie 摘要：手动点「核实凭据」才填，不跟着状态轮询走
const digests = ref<Record<string, AccountCookieDigest>>({})
// 整份配置的更新时间；凭据轮转不推进 revision 但会更新它，所以它就是「最近一次回写落库」的时刻
const digestUpdatedAt = ref('')

const status = ref<TabiAIKeepaliveStatus>({
  setting: { enabled: true, minutes: 90, updated_at: '' },
  accounts: [],
  last_run_at: '',
  running: false,
  skipped_by_checkin: false,
  next_run_at: ''
})

// form 与 status.setting 分开：编辑中的值不该被轮询回来的响应覆盖掉
const form = reactive({ enabled: true, minutes: 90 })

let timer: ReturnType<typeof setInterval> | null = null

function formatTime(value: string) {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString('zh-CN', { hour12: false })
}

const nextRunText = computed(() => {
  if (!status.value.setting.enabled) return '已关闭自动保活'
  if (!status.value.next_run_at) return '启动后的第一轮即将执行'
  return formatTime(status.value.next_run_at)
})

const columns = computed<DataTableColumns<TabiAIKeepaliveRow>>(() => [
  { title: '账号', key: 'account_name', minWidth: 140, ellipsis: { tooltip: true } },
  {
    title: '状态',
    key: 'state',
    width: 110,
    render: (row) =>
      h(
        NTag,
        { size: 'small', type: keepaliveStateType(row.state), bordered: false },
        { default: () => keepaliveStateLabel(row.state) }
      )
  },
  {
    title: '自动刷新',
    key: 'paused',
    width: 110,
    render: (row) =>
      row.paused
        ? h(NTag, { size: 'small', type: 'error', bordered: false }, { default: () => '已暂停' })
        : h(NTag, { size: 'small', type: 'success', bordered: false }, { default: () => '进行中' })
  },
  {
    title: '已换代次',
    key: 'rotated',
    width: 96,
    render: (row) => (row.state ? (row.rotated ? '是' : '否') : '—')
  },
  {
    title: '上次刷新',
    key: 'last_run_at',
    width: 172,
    render: (row) => formatTime(row.last_run_at)
  },
  {
    title: '出口',
    key: 'proxy_addr',
    width: 150,
    render: (row) => (row.state ? row.proxy_addr || '直连' : '—')
  },
  {
    // 平台库里那条凭据长什么样。指纹一致就说明客户端回写的代次确实落了库，
    // 而 has_refresh 为否比指纹更要紧：键都没了，那条根本不是能用的凭据
    title: '平台凭据',
    key: 'digest',
    width: 150,
    render: (row) => {
      const digest = digests.value[row.account_name]
      if (!digest) return h('span', { class: 'digest-idle' }, '未核实')
      if (!digest.length) {
        return h(NTag, { size: 'small', type: 'error', bordered: false }, { default: () => '库里为空' })
      }
      return h('div', null, [
        h('code', { class: 'digest-code' }, digest.fingerprint),
        digest.has_refresh
          ? null
          : h(
              'div',
              { style: 'color:#d03050;font-size:12px;margin-top:2px;' },
              '缺 new_api_refresh 键'
            )
      ])
    }
  },
  {
    title: '说明',
    key: 'message',
    minWidth: 220,
    render: (row) => {
      if (row.paused) {
        // 暂停是终态，必须把恢复条件写在这里 —— 否则用户不知道该做什么
        return h('div', null, [
          h('div', null, row.message || '凭据已失效'),
          h(
            'div',
            { style: 'color:#d03050;font-size:12px;margin-top:2px;' },
            '已停止自动刷新；重新签发或粘贴新凭据后，下一轮会自动恢复'
          )
        ])
      }
      return row.message || (row.state ? '—' : '等待第一次刷新')
    }
  }
])

function rowClass(row: TabiAIKeepaliveRow) {
  return row.paused ? 'row-paused' : ''
}

function applyStatus(next: TabiAIKeepaliveStatus, syncForm = false) {
  status.value = next
  if (syncForm) {
    form.enabled = next.setting.enabled
    form.minutes = next.setting.minutes
  }
}

async function load(syncForm = false) {
  loading.value = true
  try {
    applyStatus(await getTabiAIKeepalive(), syncForm)
  } catch (err) {
    message.error(extractErrorMessage(err, '读取保活状态失败'))
  } finally {
    loading.value = false
  }
}

async function refreshRunLock() {
  try {
    runLock.value = await getRunState()
  } catch {
    // 锁状态拿不到不影响主功能：按未锁定处理，真撞上时服务端会回 409
    runLock.value = idleRunState()
  }
}

async function save() {
  saving.value = true
  try {
    applyStatus(await saveTabiAIKeepalive({ enabled: form.enabled, minutes: form.minutes }), true)
    message.success('保活策略已保存')
  } catch (err) {
    message.error(extractErrorMessage(err, '保存失败'))
  } finally {
    saving.value = false
  }
}

async function runNow() {
  running.value = true
  try {
    const result = await runTabiAIKeepalive()
    applyStatus(result.status)
    message.success(
      `刷新完成：正常 ${result.ok_count} / 暂停 ${result.paused_count} / 异常 ${result.failed_count}`
    )
  } catch (err) {
    message.error(extractErrorMessage(err, '刷新失败，签到进行中请稍后再试'))
    await refreshRunLock()
  } finally {
    running.value = false
  }
}

/**
 * 拉一遍每个账号的 cookie 摘要，用来核实回写有没有真的落库。
 *
 * 手动触发而不是并进上面那个轮询：这是一人一次的核对动作，跟着轮询走会给每个账号
 * 每轮都发一个请求，纯属白费。allSettled 而不是 all —— 某个账号刚被删掉会 404，
 * 不该让整批核实一起失败。
 */
async function verifyDigests() {
  const names = status.value.accounts.map((row) => row.account_name).filter(Boolean)
  if (!names.length) {
    message.info('还没有 tabiai 账号')
    return
  }
  verifying.value = true
  try {
    const results = await Promise.allSettled(names.map((name) => getAccountDetail(name)))
    const next: Record<string, AccountCookieDigest> = {}
    const failed: string[] = []
    results.forEach((result, index) => {
      if (result.status === 'fulfilled') {
        next[names[index]] = result.value.cookie_digest
        if (result.value.updated_at) digestUpdatedAt.value = result.value.updated_at
      } else {
        failed.push(names[index])
      }
    })
    digests.value = next
    if (failed.length) {
      message.warning(`${names.length - failed.length}/${names.length} 个账号核实完成，失败：${failed.join('、')}`)
    } else {
      message.success(`${names.length} 个账号的凭据指纹已取回`)
    }
  } catch (err) {
    message.error(extractErrorMessage(err, '核实失败'))
  } finally {
    verifying.value = false
  }
}

onMounted(async () => {
  await Promise.all([load(true), refreshRunLock()])
  // 轮询只为跟上后台那一轮的进度与签到锁变化，间隔沿用运行锁那套节奏
  timer = setInterval(() => {
    void load()
    void refreshRunLock()
  }, RUN_LOCK_POLL_INTERVAL)
})

onBeforeUnmount(() => {
  if (timer) clearInterval(timer)
  timer = null
})
</script>

<style scoped>
.keepalive-page {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.section-card {
  border-radius: 10px;
}

.hint {
  margin-left: 12px;
  color: #94a3b8;
  font-size: 12px;
}

.hint.danger {
  margin-left: 0;
  color: #d03050;
}

/* 指纹是拿来和日志逐字比对的，等宽字体才不会看错 0/O、1/l */
:deep(.digest-code) {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12px;
  color: #334155;
  background: #f1f5f9;
  padding: 1px 5px;
  border-radius: 4px;
}

/* 表格单元格里的占位文字：不能复用 .hint，那个带 12px 左边距、在格子里会歪 */
:deep(.digest-idle) {
  color: #94a3b8;
  font-size: 12px;
}

:deep(.row-paused td) {
  background: #fff7f7;
}
</style>
