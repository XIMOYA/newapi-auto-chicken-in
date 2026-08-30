<!--
web/src/views/GitHubAccountsView.vue
页面：GitHub 账号池（统一凭据池）
职责：
- 列表展示 github_accounts：用户名 / user_session 是否已设置 / OAuth Client ID / 被哪些站点账号引用
- 增删改走 POST /api/github-accounts/ops（提交操作而非整份快照，不会覆盖后台刚轮转的凭据）
- 每条一个「检测」按钮：POST /api/github-accounts/check 探测 user_session 还能不能授权，
  结果三态（有效 / 已失效 / 无法判断）用不同颜色区分
- 删除前若还有账号在引用，服务端返回 400，提示原样展示给用户
数据来源：
- GET /api/config（经 config store，user_session 已打码成 "***"）
- POST /api/github-accounts/ops
- POST /api/github-accounts/check
-->
<template>
  <div class="page-container github-accounts-page">
    <n-card :bordered="false" class="pool-card">
      <template #header>
        <div class="card-header">
          <span class="card-title">GitHub 账号池</span>
          <n-tag v-if="pool.length" size="small" type="info" :bordered="false">共 {{ pool.length }} 个账号</n-tag>
        </div>
      </template>
      <template #header-extra>
        <n-button type="primary" size="small" @click="openCreate">
          <template #icon><n-icon><add-outline /></n-icon></template>
          新增 GitHub 账号
        </n-button>
      </template>

      <n-alert type="info" :bordered="false" class="pool-tip">
        <template #icon><n-icon><information-circle-outline /></n-icon></template>
        一个 GitHub 账号存一份 user_session，多个站点账号按名字引用它 —— 换 session 只改这里一处。
        站点账号在「配置总览 → 编辑账号」里选择要引用哪一个。
      </n-alert>

      <n-data-table
        :columns="columns"
        :data="tableData"
        :loading="configStore.loading || saving"
        :row-key="(row: PoolRow) => row.name"
        :pagination="pagination"
        striped
        :bordered="false"
        size="small"
        :scroll-x="1080"
      >
        <template #empty>
          <n-empty
            v-if="!configStore.loading"
            class="table-empty"
            description="还没有 GitHub 账号，点击右上角「新增 GitHub 账号」添加一份共用的 user_session"
          />
        </template>
      </n-data-table>
    </n-card>

    <github-account-modal
      v-model:show="modalVisible"
      :account="editingAccount"
      :submitting="saving"
      @submit="handleSubmit"
    />
  </div>
</template>

<script setup lang="ts">
import { computed, h, reactive, ref } from 'vue'
import {
  NCard, NButton, NIcon, NDataTable, NAlert, NEmpty, NTag, NTooltip, NSpace,
  useDialog, useMessage, type DataTableColumns, type PaginationProps
} from 'naive-ui'
import {
  AddOutline, InformationCircleOutline, CreateOutline, TrashOutline, PulseOutline
} from '@vicons/ionicons5'
import GithubAccountModal from '@/components/GitHubAccountModal.vue'
import { useConfigStore } from '@/stores/config'
import { checkGitHubAccount } from '@/api/githubAccounts'
import { deepClone } from '@/utils/clone'
import { extractErrorMessage } from '@/utils/error'
import type { Account, GitHubAccount, GitHubAccountOp, GitHubCheckStatus } from '@/types'

/** 表格行：池子记录 + 引用它的站点账号名（引用关系只在这一页展示，不进类型定义） */
interface PoolRow extends GitHubAccount {
  referencedBy: string[]
}

/** 一次检测的结论，按池子账号名存 */
interface CheckRecord {
  status: GitHubCheckStatus
  message: string
  /** 实际被探测的站点 URL */
  site: string
  authorizedClientId: string
  checkedAt: number
}

const configStore = useConfigStore()
const dialog = useDialog()
const message = useMessage()

const pool = computed<GitHubAccount[]>(() => configStore.config?.github_accounts ?? [])
const accounts = computed<Account[]>(() => configStore.config?.accounts ?? [])

const saving = ref(false)
const modalVisible = ref(false)
const editingAccount = ref<GitHubAccount | null>(null)
/** 正在检测的账号名：探测要几十秒且服务端串行，同一时刻只放一个走 */
const checkingName = ref('')
const checkResults = reactive<Record<string, CheckRecord>>({})

const tableData = computed<PoolRow[]>(() =>
  pool.value.map((g) => ({
    ...g,
    referencedBy: accounts.value.filter((a) => a.github_account === g.name).map((a) => a.name)
  }))
)

const pagination = reactive<PaginationProps>({
  pageSize: 10,
  showSizePicker: true,
  pageSizes: [10, 20, 50],
  onUpdatePageSize: (size: number) => {
    pagination.pageSize = size
  }
})

/**
 * 三态的界面呈现。刻意给三种不同颜色 —— unknown 是「测不出来」而不是「失效」，
 * 混成同一个灰色会让用户把限流当成凭据过期，白白去重新抓一份 session。
 */
const CHECK_PRESENTATION: Record<GitHubCheckStatus, { label: string; type: 'success' | 'error' | 'warning' }> = {
  ok: { label: '有效', type: 'success' },
  expired: { label: '已失效', type: 'error' },
  unknown: { label: '无法判断', type: 'warning' }
}

function formatTime(ts: number) {
  return new Date(ts).toLocaleString('zh-CN', { hour12: false })
}

/** 检测结果单元格：三态徽章 + 悬浮看服务端原话（站点、client_id、失败原因） */
function renderCheckCell(row: PoolRow) {
  if (checkingName.value === row.name) {
    return h(NTag, { size: 'small', type: 'info', bordered: false }, { default: () => '检测中…' })
  }
  const record = checkResults[row.name]
  if (!record) return h('span', { class: 'muted' }, '未检测')
  // 服务端将来多一种状态时，宁可显示原始值也不要整格崩掉
  const view = CHECK_PRESENTATION[record.status] ?? { label: record.status, type: 'warning' as const }
  return h(NTooltip, { trigger: 'hover', style: 'max-width: 360px' }, {
    trigger: () =>
      h('div', { class: 'check-cell' }, [
        h(NTag, { size: 'small', type: view.type, bordered: false }, { default: () => view.label }),
        h('span', { class: 'check-time' }, formatTime(record.checkedAt))
      ]),
    default: () =>
      h('div', { class: 'check-detail' }, [
        h('div', {}, record.message || '（服务端没有给出说明）'),
        record.site ? h('div', { class: 'check-detail-sub' }, `探测站点：${record.site}`) : null,
        record.authorizedClientId
          ? h('div', { class: 'check-detail-sub' }, `Client ID：${record.authorizedClientId}`)
          : null
      ])
  })
}

const columns: DataTableColumns<PoolRow> = [
  {
    title: 'GitHub 用户名',
    key: 'name',
    width: 170,
    ellipsis: { tooltip: true },
    render: (row) => h('span', { class: 'pool-name' }, row.name)
  },
  {
    title: 'user_session',
    key: 'user_session',
    width: 150,
    render: (row) =>
      row.user_session
        ? h('span', { class: 'cookie-masked' }, '已设置（***）')
        : h('span', { class: 'muted' }, '未设置')
  },
  {
    title: 'OAuth Client ID',
    key: 'client_id',
    width: 220,
    ellipsis: { tooltip: true },
    render: (row) =>
      row.client_id
        ? h('span', { class: 'mono-inline' }, row.client_id)
        : h('span', { class: 'muted' }, '自动探测')
  },
  {
    title: '被引用',
    key: 'referencedBy',
    width: 190,
    render: (row) => {
      if (!row.referencedBy.length) {
        // 没被引用不只是「闲置」：检测拿不到站点上下文，服务端会直接 400
        return h('span', { class: 'muted' }, '无账号引用（不能检测）')
      }
      return h(NTooltip, { trigger: 'hover', style: 'max-width: 320px' }, {
        trigger: () =>
          h(NTag, { size: 'small', type: 'info', bordered: false }, {
            default: () => `${row.referencedBy.length} 个账号`
          }),
        default: () => row.referencedBy.join('、')
      })
    }
  },
  { title: '检测结果', key: 'check', width: 170, render: renderCheckCell },
  {
    title: '操作',
    key: 'actions',
    width: 210,
    fixed: 'right',
    render: (row) =>
      h(NSpace, { size: 6, wrap: false }, {
        default: () => [
          h(
            NButton,
            {
              size: 'tiny',
              secondary: true,
              type: 'info',
              // 探测在服务端串行，多点几次只是排队等几十秒，直接按住不放行
              loading: checkingName.value === row.name,
              disabled: checkingName.value !== '' || !row.referencedBy.length,
              onClick: () => handleCheck(row)
            },
            { icon: () => h(NIcon, null, { default: () => h(PulseOutline) }), default: () => '检测' }
          ),
          h(
            NButton,
            { size: 'tiny', type: 'primary', secondary: true, onClick: () => openEdit(row) },
            { icon: () => h(NIcon, null, { default: () => h(CreateOutline) }), default: () => '编辑' }
          ),
          h(
            NButton,
            { size: 'tiny', type: 'error', secondary: true, onClick: () => confirmDelete(row) },
            { icon: () => h(NIcon, null, { default: () => h(TrashOutline) }), default: () => '删除' }
          )
        ]
      })
  }
]
function openCreate() {
  editingAccount.value = null
  modalVisible.value = true
}

function openEdit(row: PoolRow) {
  // 只带池子自己的三个字段，referencedBy 是本页算出来的展示信息
  editingAccount.value = deepClone({
    name: row.name,
    user_session: row.user_session,
    client_id: row.client_id
  })
  modalVisible.value = true
}

/**
 * 池子增删改统一出口。响应回传服务端重放后的最新配置（含 accounts 的引用改动），
 * store 直接换上；skipped 是并发编辑导致的跳过，不算错误但必须让用户看见。
 */
async function submitOps(ops: GitHubAccountOp[], successTip?: string) {
  saving.value = true
  try {
    const res = await configStore.submitGitHubAccountOps(ops)
    if (successTip) message.success(successTip)
    if (res.skipped?.length) {
      message.warning(`部分操作已跳过（可能有人同时在改）：${res.skipped.join('；')}`)
    }
    return res
  } catch (e) {
    // 删除时还有账号在引用、user_session 为空之类的 400，服务端已经把原因写清楚了
    message.error(extractErrorMessage(e, 'GitHub 账号保存失败'))
    throw e
  } finally {
    saving.value = false
  }
}
async function handleSubmit(payload: GitHubAccount) {
  const previousName = editingAccount.value?.name
  const renamed = !!previousName && previousName !== payload.name
  try {
    await submitOps(
      [renamed
        ? { type: 'upsert', account: payload, previous_name: previousName }
        : { type: 'upsert', account: payload }],
      previousName ? `GitHub 账号「${payload.name}」已更新` : `GitHub 账号「${payload.name}」已添加`
    )
    if (renamed && previousName) {
      // 结论是按名字存的，改名后旧键指的就不是这条了；session 也可能同时被换过
      delete checkResults[previousName]
      delete checkResults[payload.name]
      message.info(`引用「${previousName}」的站点账号已由服务端一并改为「${payload.name}」`)
    }
    modalVisible.value = false
  } catch {
    // submitOps 已提示，弹窗保持打开让用户改
  }
}

function confirmDelete(row: PoolRow) {
  const used = row.referencedBy.length
  dialog.warning({
    title: '删除 GitHub 账号',
    content: used
      ? `「${row.name}」还有 ${used} 个站点账号在引用（${row.referencedBy.join('、')}），`
        + '服务端会拒绝删除。请先把这些账号的引用改掉。'
      : `确定要从池子里删除「${row.name}」吗？该 user_session 将不再保存。`,
    positiveText: used ? '仍然尝试删除' : '删除',
    negativeText: '取消',
    onPositiveClick: async () => {
      try {
        await submitOps([{ type: 'delete', name: row.name }], `GitHub 账号「${row.name}」已删除`)
        delete checkResults[row.name]
      } catch {
        // submitOps 已提示（有引用时服务端会说明有几个在用）
      }
    }
  })
}
/**
 * 探测一条 user_session。
 *
 * 会实际请求 GitHub OAuth，单次几十秒，所以：按钮进 loading、期间其他行的检测也
 * 一并禁用（服务端本来就是串行的，并发点只是让人干等）。没被任何账号引用时
 * 服务端返回 400（无法确定探测哪个站点），那条提示原样展示。
 */
async function handleCheck(row: PoolRow) {
  if (checkingName.value) return
  checkingName.value = row.name
  try {
    const res = await checkGitHubAccount(row.name)
    checkResults[row.name] = {
      status: res.result.status,
      message: res.result.message,
      site: res.site,
      authorizedClientId: res.result.authorized_client_id ?? '',
      checkedAt: Date.now()
    }
    const view = CHECK_PRESENTATION[res.result.status]
    const tip = `「${row.name}」检测结果：${view?.label ?? res.result.status}`
    if (res.result.status === 'ok') message.success(tip)
    else if (res.result.status === 'expired') message.error(`${tip} —— 需要重新填 user_session`)
    else message.warning(`${tip}（不代表凭据失效，稍后可再试）`)
  } catch (e) {
    message.error(extractErrorMessage(e, `检测「${row.name}」失败`))
  } finally {
    checkingName.value = ''
  }
}
</script>

<style scoped>
.github-accounts-page {
  max-width: 1200px;
}

.pool-card {
  background: #fff;
}

.card-header {
  display: flex;
  align-items: center;
  gap: 10px;
}

.card-title {
  font-size: 16px;
  font-weight: 600;
  color: #1f2d3d;
}

.pool-tip {
  margin-bottom: 12px;
}

.pool-name {
  font-weight: 600;
  color: #1f2d3d;
}

.cookie-masked {
  font-family: 'JetBrains Mono', Consolas, monospace;
  color: #48566a;
}

.mono-inline {
  font-family: 'JetBrains Mono', Consolas, monospace;
  font-size: 12px;
  color: #48566a;
}

.check-cell {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.check-time {
  font-size: 11px;
  color: #a3aec0;
}

.check-detail {
  line-height: 1.6;
}

.check-detail-sub {
  font-size: 12px;
  opacity: 0.75;
}

.muted {
  color: #a3aec0;
}

.table-empty {
  padding: 40px 0;
}
</style>
