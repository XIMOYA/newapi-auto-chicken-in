<!--
web/src/views/OverviewView.vue
页面：配置总览
职责：
- 顶部统计卡：账号总数 / 启用数 / 代理池开关 / 邮件通知开关
- 账号管理表格：搜索、多选、启停开关、打码凭据、手动隧道、编辑/删除
- 凭据列按登录方式展示：站点 Cookie 只有一项，TaBiAI 另附 user_session（签发原料）
- 新增/编辑账号弹窗；批量启用/停用/删除（带确认）
数据来源：
- GET /api/config（经 config store）
- POST /api/accounts/ops（账号增删改：提交操作而非整份快照，多人同时编辑互不覆盖）
- PUT /api/config（站点预设仍整份保存，走 revision 乐观锁）
-->
<template>
  <div class="overview">
    <!-- 统计卡 -->
    <n-grid :cols="4" :x-gap="16" :y-gap="16" responsive="screen" item-responsive>
      <n-grid-item v-for="card in statCards" :key="card.label" span="4 s:2 m:1">
        <transition name="fade-slide" appear>
          <n-card :bordered="false" class="stat-card hover-lift">
            <div class="stat-inner">
              <div class="stat-icon" :style="{ background: card.iconBg, color: card.iconColor }">
                <n-icon size="22"><component :is="card.icon" /></n-icon>
              </div>
              <div class="stat-meta">
                <div class="stat-label">{{ card.label }}</div>
                <transition name="pop-number" appear>
                  <div class="stat-value" :key="card.value">{{ card.value }}</div>
                </transition>
              </div>
            </div>
          </n-card>
        </transition>
      </n-grid-item>
    </n-grid>

    <!-- 站点预设 -->
    <n-card :bordered="false" class="sites-card">
      <template #header>
        <div class="card-header">
          <div class="card-title">站点预设</div>
          <n-tag v-if="sites.length" size="small" type="info" :bordered="false">共 {{ sites.length }} 个站点</n-tag>
        </div>
      </template>
      <template #header-extra>
        <n-button size="small" type="primary" @click="openSiteCreate">
          <template #icon><n-icon><add-outline /></n-icon></template>
          新增站点
        </n-button>
      </template>

      <n-data-table
        :columns="siteColumns"
        :data="siteTableData"
        :loading="configStore.loading || saving"
        :row-key="(row: SiteRow) => row._index"
        :pagination="sitePagination"
        striped
        :bordered="false"
        size="small"
        :scroll-x="820"
      >
        <template #empty>
          <n-empty
            v-if="!configStore.loading"
            class="table-empty"
            description="暂无站点预设。配置常用站点后，新增账号时可快速选择并自动带出路径。"
          />
        </template>
      </n-data-table>
    </n-card>

    <!-- 账号管理 -->
    <n-card :bordered="false" class="accounts-card">
      <template #header>
        <div class="card-header">
          <div class="card-title">
            签到账号
            <n-tag v-if="selectedKeys.length" size="small" type="primary" :bordered="false">已选 {{ selectedKeys.length }} 项</n-tag>
          </div>
        </div>
      </template>

      <template #header-extra>
        <n-space :size="8" align="center" class="account-header-actions">
          <n-input
            v-model:value="accountSearch"
            size="small"
            clearable
            placeholder="搜索账号名称、站点 URL 或手动隧道"
            class="account-search"
          >
            <template #prefix><n-icon><search-outline /></n-icon></template>
          </n-input>
          <n-button size="small" :disabled="!selectedKeys.length" :loading="saving" @click="batchToggle(true)">
            批量启用
          </n-button>
          <n-button size="small" :disabled="!selectedKeys.length" :loading="saving" @click="batchToggle(false)">
            批量停用
          </n-button>
          <n-button size="small" type="error" :disabled="!selectedKeys.length" :loading="saving" @click="batchDelete">
            批量删除
          </n-button>
          <n-button size="small" type="primary" @click="openCreateModal">
            <template #icon><n-icon><add-outline /></n-icon></template>
            新增账号
          </n-button>
        </n-space>
      </template>

      <n-data-table
        :columns="columns"
        :data="tableData"
        :loading="configStore.loading || saving"
        :row-key="(row: AccountRow) => row._index"
        v-model:checked-row-keys="selectedKeys"
        :pagination="pagination"
        striped
        :bordered="false"
        size="small"
        :scroll-x="980"
      >
        <template #empty>
          <n-empty
            v-if="!configStore.loading"
            class="table-empty"
            :description="accountEmptyDescription"
          />
        </template>
      </n-data-table>
    </n-card>

    <account-modal
      v-model:show="modalVisible"
      :account="editingAccount"
      :submitting="submitting"
      :sites="sites"
      @submit="handleAccountSubmit"
      @credential-issued="handleCredentialIssued"
    />
    <site-modal v-model:show="siteModalVisible" :site="editingSite" @submit="handleSiteSubmit" />

    <!-- 查看明文 Cookie：二次确认 -->
    <password-confirm-modal
      v-model:show="passwordConfirmVisible"
      :loading="revealing"
      @confirm="handleRevealCookie"
    />
    <cookie-reveal-modal v-model:show="revealVisible" :cookie="revealedCookie" :account-name="cookieRevealTarget" />
  </div>
</template>

<script setup lang="ts">
import { computed, h, reactive, ref, watch } from 'vue'
import {
  NGrid, NGridItem, NCard, NIcon, NButton, NSpace, NDataTable, NTag, NSwitch, NEmpty, NInput,
  useDialog, useMessage, type DataTableColumns, type PaginationProps
} from 'naive-ui'
import {
  PeopleOutline, CheckmarkCircleOutline, LayersOutline, MailOutline, AddOutline,
  EyeOutline, CreateOutline, TrashOutline, PlanetOutline, SearchOutline
} from '@vicons/ionicons5'
import AccountModal from '@/components/AccountModal.vue'
import SiteModal from '@/components/SiteModal.vue'
import PasswordConfirmModal from '@/components/PasswordConfirmModal.vue'
import CookieRevealModal from '@/components/CookieRevealModal.vue'
import { useConfigStore } from '@/stores/config'
import { verifyPassword } from '@/api/auth'
import { exportConfig } from '@/api/export'
import { deepClone } from '@/utils/clone'
import { extractErrorMessage } from '@/utils/error'
import type { Account, AccountOp, LoginMethod, Site } from '@/types'

interface AccountRow extends Account {
  _index: number
}

interface SiteRow extends Site {
  _index: number
}

const configStore = useConfigStore()
const dialog = useDialog()
const message = useMessage()

const accounts = computed<Account[]>(() => configStore.config?.accounts ?? [])
const sites = computed<Site[]>(() => configStore.config?.sites ?? [])
const accountCount = computed(() => accounts.value.length)
const enabledCount = computed(() => accounts.value.filter((a) => a.enabled).length)
const proxyPoolEnabled = computed(() => !!configStore.config?.proxy_pool?.enabled)
const notifyEnabled = computed(() => !!configStore.config?.notify?.email?.enabled)

/** 登录方式的界面标签：表格 tag、搜索匹配串、提示语共用一个来源 */
function loginMethodLabel(method: LoginMethod) {
  return method === 'tabiai' ? 'TaBiAI 凭据' : '站点 Cookie'
}

/** 统计卡数据（含图标与配色），配合 v-for 渲染 + 进入动画 */
const statCards = computed(() => [
  {
    label: '账号总数',
    value: accountCount.value,
    icon: PeopleOutline,
    iconBg: '#e8f0ff',
    iconColor: '#1e5eff'
  },
  {
    label: '启用中',
    value: enabledCount.value,
    icon: CheckmarkCircleOutline,
    iconBg: '#e8f9ef',
    iconColor: '#18a058'
  },
  {
    label: '代理池开关',
    value: proxyPoolEnabled.value ? '已启用' : '已停用',
    icon: LayersOutline,
    iconBg: '#fdf3e7',
    iconColor: '#f0a020'
  },
  {
    label: '邮件通知开关',
    value: notifyEnabled.value ? '已启用' : '已停用',
    icon: MailOutline,
    iconBg: '#f0ecfe',
    iconColor: '#7c5cf0'
  }
])

const accountSearch = ref('')
const tableData = computed<AccountRow[]>(() => {
  const keyword = accountSearch.value.trim().toLowerCase()
  return accounts.value
    .map((a, i) => ({ ...a, _index: i }))
    .filter((row) => {
      if (!keyword) return true
      // 中文标签与原始枚举值都塞进匹配串，搜「tabiai」和搜「站点」都能命中
      const loginLabel = `${loginMethodLabel(row.login_method)} ${row.login_method}`
      const credentialState = row.cookie ? `${loginLabel} 已设置` : `${loginLabel} 未设置`
      return [row.name, row.url, row.proxy, row.checkin_path, row.browser_path, loginLabel, credentialState].some(
        (value) => value?.toLowerCase().includes(keyword) ?? false
      )
    })
})
const accountEmptyDescription = computed(() =>
  accountSearch.value.trim() ? '没有找到匹配的签到账号' : '暂无签到账号，点击右上角「新增账号」开始添加'
)
const siteTableData = computed<SiteRow[]>(() => sites.value.map((s, i) => ({ ...s, _index: i })))

const selectedKeys = ref<number[]>([])
const saving = ref(false)
const modalVisible = ref(false)
const submitting = ref(false)
const editingAccount = ref<Account | null>(null)
/** 编辑时对应的账号索引（-1 表示新增） */
const editingIndex = ref(-1)

// ---- 查看明文 Cookie（二次确认）----
const passwordConfirmVisible = ref(false)
const revealing = ref(false)
const revealVisible = ref(false)
const revealedCookie = ref('')
const cookieRevealTarget = ref('')
const cookieRevealField = ref<'cookie' | 'github_user_session'>('cookie')
// 提示语里要说清看的是哪个凭据；点击时就把标签记下来，免得回调里再翻账号
const cookieRevealLabel = ref('')

async function handleRevealCookie(password: string) {
  revealing.value = true
  try {
    // 票据只能用一次，必须紧接着交给 exportConfig
    const { ticket } = await verifyPassword(password)
    const res = await exportConfig(ticket)
    const cfg = JSON.parse(res.json) as { accounts?: Account[] }
    const target = (cfg.accounts ?? []).find((a) => a.name === cookieRevealTarget.value)
    const value = target?.[cookieRevealField.value]
    if (!target || typeof value !== 'string' || !value) {
      message.error(`未找到该账号的${cookieRevealLabel.value || '凭据'}`)
      passwordConfirmVisible.value = false
      return
    }
    revealedCookie.value = value
    passwordConfirmVisible.value = false
    revealVisible.value = true
  } catch (e) {
    message.error(extractErrorMessage(e, '密码验证失败'))
  } finally {
    revealing.value = false
  }
}

// 分页必须用响应式对象 + 显式 onUpdatePageSize 写回，
// 否则 Naive UI 的每页条数选择器（10/20/50）切换后状态不会更新
const pagination = reactive<PaginationProps>({
  page: 1,
  pageSize: 10,
  showSizePicker: true,
  pageSizes: [10, 20, 50],
  onUpdatePage: (page: number) => {
    pagination.page = page
  },
  onUpdatePageSize: (size: number) => {
    pagination.pageSize = size
    pagination.page = 1
  }
})
watch(accountSearch, () => {
  pagination.page = 1
})
const sitePagination = reactive<PaginationProps>({
  pageSize: 10,
  showSizePicker: true,
  pageSizes: [10, 20, 50],
  onUpdatePageSize: (size: number) => {
    sitePagination.pageSize = size
  }
})

// ---- 站点预设表格列 ----
const siteColumns: DataTableColumns<SiteRow> = [
  {
    title: '名称',
    key: 'name',
    width: 160,
    render: (row) =>
      h('span', { class: 'site-name' }, [
        h(NIcon, { size: 15, class: 'site-name-icon' }, { default: () => h(PlanetOutline) }),
        ` ${row.name}`
      ])
  },
  {
    title: '站点 URL',
    key: 'url',
    width: 240,
    render: (row) =>
      h('a', { href: row.url, target: '_blank', rel: 'noopener', class: 'url-link' }, row.url)
  },
  { title: '签到路径', key: 'checkin_path', width: 200, render: (row) => (row.checkin_path ? h('span', { class: 'mono-inline' }, row.checkin_path) : h('span', { class: 'muted' }, '未设置')) },
  { title: '浏览器入口', key: 'browser_path', width: 180, render: (row) => (row.browser_path ? h('span', { class: 'mono-inline' }, row.browser_path) : h('span', { class: 'muted' }, '/dashboard')) },
  {
    title: '操作',
    key: 'actions',
    width: 140,
    fixed: 'right',
    render: (row) =>
      h('div', { class: 'table-actions' }, [
        h(
          NButton,
          { size: 'tiny', type: 'primary', secondary: true, onClick: () => openSiteEdit(row) },
          { icon: () => h(NIcon, null, { default: () => h(CreateOutline) }), default: () => '编辑' }
        ),
        h(
          NButton,
          { size: 'tiny', type: 'error', secondary: true, onClick: () => confirmSiteDelete(row) },
          { icon: () => h(NIcon, null, { default: () => h(TrashOutline) }), default: () => '删除' }
        )
      ])
  }
]

// ---- 站点预设 CRUD ----
const siteModalVisible = ref(false)
const editingSite = ref<Site | null>(null)
const editingSiteIndex = ref(-1)

function openSiteCreate() {
  editingSite.value = null
  editingSiteIndex.value = -1
  siteModalVisible.value = true
}

function openSiteEdit(row: SiteRow) {
  editingSite.value = deepClone(row)
  editingSiteIndex.value = row._index
  siteModalVisible.value = true
}

async function handleSiteSubmit(payload: Site) {
  try {
    if (editingSiteIndex.value >= 0 && sites.value[editingSiteIndex.value]) {
      sites.value[editingSiteIndex.value] = payload
    } else {
      sites.value.push(payload)
    }
    await persistAccounts(editingSiteIndex.value >= 0 ? `站点预设「${payload.name}」已更新` : `站点预设「${payload.name}」已添加`)
    siteModalVisible.value = false
  } catch {
    // persistAccounts 内部已提示错误
  }
}

function confirmSiteDelete(row: SiteRow) {
  dialog.warning({
    title: '删除站点预设',
    content: `确定要删除站点预设「${row.name}」吗？已存在的账号不受影响。`,
    positiveText: '删除',
    negativeText: '取消',
    onPositiveClick: () => {
      sites.value.splice(row._index, 1)
      persistAccounts(`站点预设「${row.name}」已删除`)
    }
  })
}

/** 凭据字段的界面标签：cookie 的含义随登录方式变，user_session 只在 TaBiAI 下出现 */
function credentialFieldLabel(method: LoginMethod, field: 'cookie' | 'github_user_session') {
  return field === 'github_user_session' ? 'GitHub user_session' : loginMethodLabel(method)
}

function viewCookie(row: AccountRow, field: 'cookie' | 'github_user_session') {
  const value = row[field]
  const label = credentialFieldLabel(row.login_method, field)
  if (value === '' || value == null) {
    message.info(`账号「${row.name}」未设置${label}`)
    return
  }
  // 高敏操作：先弹密码确认，通过后再拉明文展示
  cookieRevealTarget.value = row.name
  cookieRevealField.value = field
  cookieRevealLabel.value = label
  passwordConfirmVisible.value = true
}

/**
 * 一键签发凭据由服务端直接改库，本地这份配置（含 revision）立刻过期。
 * 必须重新拉一次，否则用户接着保存会撞乐观锁 409。
 */
async function handleCredentialIssued(accountName: string) {
  try {
    await configStore.fetchConfig()
    // 弹窗还开着，把编辑对象换成刷新后的那条，避免继续用旧快照提交
    if (editingIndex.value >= 0) {
      const latest = accounts.value.find((a) => a.name === accountName)
      if (latest) editingAccount.value = deepClone(latest)
    }
  } catch (e) {
    message.error(extractErrorMessage(e, '签发成功但配置刷新失败，请手动刷新页面'))
  }
}

function toggleEnabled(row: AccountRow, value: boolean) {
  void submitOps(
    [{ type: 'set_enabled', name: row.name, enabled: value }],
    `${value ? '启用' : '停用'}账号「${row.name}」`
  )
}

const columns: DataTableColumns<AccountRow> = [
  { type: 'selection', width: 44, fixed: 'left' },
  { title: '名称', key: 'name', width: 130, fixed: 'left', ellipsis: { tooltip: true } },
  {
    title: '站点 URL',
    key: 'url',
    width: 210,
    render: (row) =>
      h('a', { href: row.url, target: '_blank', rel: 'noopener', class: 'url-link' }, row.url)
  },
  {
    title: '登录方式',
    key: 'login_method',
    width: 150,
    render: (row) => h(NTag, { size: 'small', type: row.login_method === 'tabiai' ? 'warning' : 'info' }, {
      default: () => loginMethodLabel(row.login_method)
    })
  },
  {
    title: '启用',
    key: 'enabled',
    width: 84,
    render: (row) =>
      h(NSwitch, {
        value: row.enabled,
        size: 'small',
        onUpdateValue: (v: boolean) => toggleEnabled(row, v)
      })
  },
  {
    title: '手动隧道(proxy)',
    key: 'proxy',
    width: 200,
    ellipsis: { tooltip: true },
    render: (row) => (row.proxy ? h('span', { class: 'mono-inline' }, row.proxy) : h('span', { class: 'muted' }, '未设置'))
  },
  {
    title: '当前凭据',
    key: 'credential',
    width: 210,
    render: (row) => {
      // 两种登录方式的凭据都落在 cookie 字段（tabiai 存的是 new_api_refresh）；
      // user_session 已退化为签发原料，但仍要留一个查看入口，所以 TaBiAI 多列一行
      const fields: Array<'cookie' | 'github_user_session'> =
        row.login_method === 'tabiai' ? ['cookie', 'github_user_session'] : ['cookie']
      return h('div', { class: 'credential-cell' }, fields.map((field) => {
        const value = row[field]
        const label = credentialFieldLabel(row.login_method, field)
        const text = value === '***' ? `${label}（已设置）` : value ? `${label}已设置` : `${label}未设置`
        return h('div', { class: 'cookie-cell' }, [
          h('span', { class: value ? 'cookie-masked' : 'muted' }, text),
          h(
            NButton,
            { size: 'tiny', quaternary: true, circle: true, type: 'info', onClick: () => viewCookie(row, field), class: 'view-cookie-btn' },
            { icon: () => h(NIcon, null, { default: () => h(EyeOutline) }) }
          )
        ])
      }))
    }
  },
  {
    title: '操作',
    key: 'actions',
    width: 140,
    fixed: 'right',
    render: (row) =>
      h('div', { class: 'table-actions' }, [
        h(
          NButton,
          { size: 'tiny', type: 'primary', secondary: true, onClick: () => openEditModal(row) },
          { icon: () => h(NIcon, null, { default: () => h(CreateOutline) }), default: () => '编辑' }
        ),
        h(
          NButton,
          { size: 'tiny', type: 'error', secondary: true, onClick: () => confirmDelete(row) },
          { icon: () => h(NIcon, null, { default: () => h(TrashOutline) }), default: () => '删除' }
        )
      ])
  }
]

function openCreateModal() {
  editingAccount.value = null
  editingIndex.value = -1
  modalVisible.value = true
}

function openEditModal(row: AccountRow) {
  editingAccount.value = deepClone(row)
  editingIndex.value = row._index
  modalVisible.value = true
}

async function handleAccountSubmit(payload: Account) {
  submitting.value = true
  // 编辑时带上原名：服务端据此定位旧记录，改名也能找回打码字段的真值
  const previousName = editingAccount.value?.name
  try {
    await submitOps(
      [previousName && previousName !== payload.name
        ? { type: 'upsert', account: payload, previous_name: previousName }
        : { type: 'upsert', account: payload }],
      editingIndex.value >= 0 ? `账号「${payload.name}」已更新` : `账号「${payload.name}」已添加`
    )
    modalVisible.value = false
  } catch {
    // submitOps 内部已提示错误，弹窗保持打开让用户改
  } finally {
    submitting.value = false
  }
}

function confirmDelete(row: AccountRow) {
  dialog.warning({
    title: '删除账号',
    content: `确定要删除签到账号「${row.name}」吗？该操作不可恢复。`,
    positiveText: '删除',
    negativeText: '取消',
    onPositiveClick: () => {
      selectedKeys.value = []
      void submitOps([{ type: 'delete', name: row.name }], `账号「${row.name}」已删除`)
    }
  })
}

function batchToggle(enable: boolean) {
  if (!selectedKeys.value.length) return
  const action = enable ? '启用' : '停用'
  dialog.info({
    title: `批量${action}`,
    content: `确定要对选中的 ${selectedKeys.value.length} 个账号执行批量${action}吗？`,
    positiveText: action,
    negativeText: '取消',
    onPositiveClick: () => {
      // 选中项存的是行下标，先换成账号名再下发：别人插入/删除账号后下标会错位，名字不会
      const names = selectedNames()
      if (!names.length) return
      selectedKeys.value = []
      void submitOps(
        names.map((name) => ({ type: 'set_enabled' as const, name, enabled: enable })),
        `已批量${action} ${names.length} 个账号`
      )
    }
  })
}

function batchDelete() {
  if (!selectedKeys.value.length) return
  dialog.warning({
    title: '批量删除',
    content: `确定要删除选中的 ${selectedKeys.value.length} 个账号吗？该操作不可恢复。`,
    positiveText: '删除',
    negativeText: '取消',
    onPositiveClick: () => {
      const names = selectedNames()
      if (!names.length) return
      selectedKeys.value = []
      void submitOps(
        names.map((name) => ({ type: 'delete' as const, name })),
        `已批量删除 ${names.length} 个账号`
      )
    }
  })
}

/** 把选中的行下标翻译成账号名，跳过已经不在列表里的（别人删掉的） */
function selectedNames(): string[] {
  return selectedKeys.value
    .map((i) => accounts.value[i]?.name)
    .filter((name): name is string => !!name)
}

/**
 * 账号增删改统一出口：提交操作而非整份配置。
 *
 * 服务端在最新配置上重放，所以两个人同时加账号都能成功；别人已删掉的账号会被
 * 跳过并在 skipped 里说明，这不算错误，提示一下就好。
 */
async function submitOps(ops: AccountOp[], successTip?: string) {
  saving.value = true
  try {
    const res = await configStore.submitAccountOps(ops)
    if (successTip) message.success(successTip)
    if (res.skipped?.length) {
      message.warning(`部分操作已跳过：${res.skipped.join('；')}`)
    }
  } catch (e) {
    message.error(extractErrorMessage(e, '账号配置保存失败'))
    throw e
  } finally {
    saving.value = false
  }
}

/** 站点预设仍走整份保存：它没有并发热点，沿用 revision 乐观锁即可 */
async function persistAccounts(successTip?: string) {
  if (!configStore.config) return
  saving.value = true
  try {
    const next = deepClone(configStore.config)
    await configStore.save(next)
    if (successTip) message.success(successTip)
  } catch (e) {
    message.error(extractErrorMessage(e, '账号配置保存失败'))
  } finally {
    saving.value = false
  }
}
</script>

<style scoped>
.overview {
  display: flex;
  flex-direction: column;
  gap: 16px;
  max-width: 1200px;
}

.stat-card {
  background: #fff;
}

.stat-card.hover-lift {
  transition: transform 0.28s cubic-bezier(0.22, 0.61, 0.36, 1), box-shadow 0.28s cubic-bezier(0.22, 0.61, 0.36, 1);
}
.stat-card.hover-lift:hover {
  transform: translateY(-3px);
  box-shadow: 0 10px 28px rgba(30, 58, 138, 0.12);
}

.stat-inner {
  display: flex;
  align-items: center;
  gap: 14px;
}

.stat-meta {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.stat-label {
  font-size: 13px;
  color: #8492a6;
}

.stat-value {
  font-size: 24px;
  font-weight: 700;
  color: #1f2d3d;
  line-height: 1.2;
}

.accounts-card {
  background: #fff;
}

.account-header-actions {
  max-width: 100%;
}

.account-search {
  width: 240px;
}

.sites-card {
  background: #fff;
}

.site-name {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-weight: 600;
  color: #1f2d3d;
}

.site-name-icon {
  color: #1e5eff;
}

.card-header {
  display: flex;
  align-items: center;
  gap: 10px;
}

.card-title {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 16px;
  font-weight: 600;
  color: #1f2d3d;
}

.url-link {
  color: #1e5eff;
  text-decoration: none;
}
.url-link:hover {
  text-decoration: underline;
}

.cookie-cell {
  display: flex;
  align-items: center;
  gap: 6px;
}

/* TaBiAI 账号要同时显示 new_api_refresh 与 user_session 两行 */
.credential-cell {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.cookie-masked {
  font-family: 'JetBrains Mono', Consolas, monospace;
  color: #48566a;
}

.muted {
  color: #a3aec0;
}

.mono-inline {
  font-family: 'JetBrains Mono', Consolas, monospace;
  font-size: 12px;
  color: #48566a;
}

.table-empty {
  padding: 40px 0;
}
</style>
