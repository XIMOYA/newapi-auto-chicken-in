<!--
web/src/layouts/AdminLayout.vue
布局：管理后台主框架
职责：
- 深蓝色侧边栏导航（配置总览/AI/浏览器/HTTP/全局默认/代理池/邮件通知/API Key/导出）
- 顶部栏：当前页面标题 + 用户名下拉（修改密码/退出登录）
- 首次进入时拉取 GET /api/config 初始化 config store
数据来源：GET /api/config
-->
<template>
  <n-layout has-sider class="admin-layout">
    <n-layout-sider
      bordered
      :width="224"
      :collapsed-width="72"
      collapse-mode="width"
      show-trigger="bar"
      :native-scrollbar="false"
      class="admin-sider"
    >
      <div class="brand" :class="{ 'brand--collapsed': collapsed }">
        <transition name="pop-number" appear>
          <n-icon size="26" class="brand-icon"><rocket-outline /></n-icon>
        </transition>
        <transition name="fade">
          <div v-if="!collapsed" class="brand-text">
            <div class="brand-title">NewAPI 签到</div>
            <div class="brand-sub">配置管理平台</div>
          </div>
        </transition>
      </div>
      <n-menu
        :value="activeKey"
        :options="menuOptions"
        :inverted="true"
        :collapsed="collapsed"
        :collapsed-width="72"
        :collapsed-icon-size="20"
        @update:value="handleMenuSelect"
      />
    </n-layout-sider>

    <n-layout class="admin-main">
      <n-layout-header bordered class="admin-header">
        <div class="header-left">
          <n-icon size="18" class="header-title-icon"><cube-outline /></n-icon>
          <span class="header-title">{{ currentTitle }}</span>
        </div>
        <div class="header-right">
          <n-tag v-if="configStore.updatedAt" size="small" type="info" :bordered="false" class="update-tag">
            配置更新于 {{ formatTime(configStore.updatedAt) }}
          </n-tag>
          <n-dropdown :options="userMenuOptions" trigger="click" @select="handleUserMenuSelect">
            <n-button quaternary class="user-btn">
              <template #icon>
                <n-icon><person-circle-outline /></n-icon>
              </template>
              <span>{{ authStore.username || '管理员' }}</span>
              <n-icon size="14" class="arrow-icon"><chevron-down-outline /></n-icon>
            </n-button>
          </n-dropdown>
        </div>
      </n-layout-header>

      <n-layout-content class="admin-content" :native-scrollbar="false">
        <router-view v-if="configReady" />
        <div v-else class="loading-wrap">
          <n-spin size="large" />
          <div class="loading-text">正在加载配置…</div>
        </div>
      </n-layout-content>    </n-layout>
  </n-layout>

  <password-modal v-model:show="passwordModalVisible" />
</template>

<script setup lang="ts">
import { computed, h, ref } from 'vue'
import { onMounted } from 'vue'
import { NLayout, NLayoutSider, NLayoutHeader, NLayoutContent, NMenu, NIcon, NButton, NDropdown, NTag, NSpin, useDialog, useMessage, type MenuOption, type DropdownOption } from 'naive-ui'
import { useRoute, useRouter } from 'vue-router'
import {
  HomeOutline,
  SparklesOutline,
  PlanetOutline,
  GlobeOutline,
  SettingsOutline,
  LayersOutline,
  MailOutline,
  KeyOutline,
  DownloadOutline,
  RocketOutline,
  CubeOutline,
  PersonCircleOutline,
  ChevronDownOutline,
  LogOutOutline,
  LockClosedOutline
} from '@vicons/ionicons5'
import PasswordModal from '@/components/PasswordModal.vue'
import { useAuthStore } from '@/stores/auth'
import { useConfigStore } from '@/stores/config'

const route = useRoute()
const router = useRouter()
const authStore = useAuthStore()
const configStore = useConfigStore()
const dialog = useDialog()
const message = useMessage()

const collapsed = ref(false)
const passwordModalVisible = ref(false)
const configReady = ref(false)

const renderIcon = (icon: unknown) => () => h(NIcon, null, { default: () => h(icon as never) })

const menuOptions: MenuOption[] = [
  { label: '配置总览', key: '/', icon: renderIcon(HomeOutline) },
  { label: 'AI 配置', key: '/settings/ai', icon: renderIcon(SparklesOutline) },
  { label: '浏览器配置', key: '/settings/browser', icon: renderIcon(PlanetOutline) },
  { label: 'HTTP 配置', key: '/settings/http', icon: renderIcon(GlobeOutline) },
  { label: '全局默认', key: '/settings/defaults', icon: renderIcon(SettingsOutline) },
  { label: '代理池配置', key: '/settings/proxy-pool', icon: renderIcon(SettingsOutline) },
  { label: '代理管理', key: '/proxies', icon: renderIcon(LayersOutline) },
  { label: '邮件通知', key: '/settings/notify', icon: renderIcon(MailOutline) },
  { label: 'API Key 管理', key: '/keys', icon: renderIcon(KeyOutline) },
  { label: '导出配置', key: '/export', icon: renderIcon(DownloadOutline) }
]

const activeKey = computed(() => {
  if (route.path === '/') return '/'
  return route.path
})

const currentTitle = computed(() => {
  const hit = menuOptions.find((o) => o.key === activeKey.value)
  return hit ? String(hit.label) : '配置管理'
})

const userMenuOptions: DropdownOption[] = [
  { label: '修改密码', key: 'password', icon: () => h(NIcon, null, { default: () => h(LockClosedOutline) }) },
  { type: 'divider', key: 'd1' },
  { label: '退出登录', key: 'logout', icon: () => h(NIcon, null, { default: () => h(LogOutOutline) }) }
]

function handleMenuSelect(key: string) {
  if (key === activeKey.value) return
  router.push(key)
}

function handleUserMenuSelect(key: string) {
  if (key === 'password') {
    passwordModalVisible.value = true
    return
  }
  if (key === 'logout') {
    dialog.warning({
      title: '退出登录',
      content: '确定要退出当前账号吗？',
      positiveText: '退出',
      negativeText: '取消',
      onPositiveClick: () => {
        authStore.clear()
        message.success('已退出登录')
        router.push('/login')
      }
    })
  }
}

function formatTime(t: string) {
  if (!t) return ''
  const d = new Date(t)
  if (Number.isNaN(d.getTime())) return t
  return d.toLocaleString('zh-CN', { hour12: false })
}

// 进入后台后初始化配置（登录页跳转前也可能已拉取过，此处兜底）
onMounted(async () => {
  try {
    if (!configStore.config) {
      await configStore.fetchConfig()
    }
  } catch {
    // 拦截器已处理 401；其他错误由各页面展示
  } finally {
    configReady.value = true
  }
})
</script>

<style scoped>
.admin-layout {
  height: 100vh;
}

.admin-sider {
  background: linear-gradient(180deg, #142a5c 0%, #0f1f47 100%) !important;
}

.brand {
  display: flex;
  align-items: center;
  gap: 10px;
  height: 60px;
  padding: 0 18px;
  color: #fff;
  border-bottom: 1px solid rgba(255, 255, 255, 0.08);
}

.brand-icon {
  color: #6ea8ff;
  flex-shrink: 0;
}

.brand-title {
  font-size: 16px;
  font-weight: 700;
  letter-spacing: 0.5px;
  line-height: 1.2;
}

.brand-sub {
  font-size: 11px;
  color: rgba(255, 255, 255, 0.55);
  line-height: 1.2;
}

.fade-enter-active,
.fade-leave-active {
  transition: opacity 0.2s ease;
}
.fade-enter-from,
.fade-leave-to {
  opacity: 0;
}

.admin-main {
  display: flex;
  flex-direction: column;
  height: 100vh;
  background: #f0f2f7;
}

.admin-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  height: 60px;
  padding: 0 20px;
  background: #fff;
}

.header-left {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 15px;
  font-weight: 600;
  color: #1f2d3d;
}

.header-title-icon {
  color: #1e5eff;
}

.header-right {
  display: flex;
  align-items: center;
  gap: 14px;
}

.update-tag {
  margin-right: 4px;
}

.user-btn {
  display: flex;
  align-items: center;
  gap: 2px;
  color: #1f2d3d;
  font-weight: 500;
}

.arrow-icon {
  color: #8492a6;
}

.admin-content {
  flex: 1;
  padding: 24px;
  overflow: auto;
}

.loading-wrap {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  min-height: 50vh;
  gap: 16px;
  color: #8492a6;
  font-size: 13px;
}
</style>
