/*
web/src/router/index.ts
路由配置与守卫
职责：
- 定义 /login 与侧边栏布局下全部子路由
- 无 token 访问受保护页 → 重定向 /login（携带 redirect）
- 已登录访问 /login → 重定向 /
数据来源：无
*/
import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'
import { useAuthStore } from '@/stores/auth'

const routes: RouteRecordRaw[] = [
  {
    path: '/login',
    name: 'login',
    component: () => import('@/views/LoginView.vue'),
    meta: { title: '登录', public: true }
  },
  {
    path: '/',
    component: () => import('@/layouts/AdminLayout.vue'),
    children: [
      {
        path: '',
        name: 'overview',
        component: () => import('@/views/OverviewView.vue'),
        meta: { title: '配置总览' }
      },
      {
        path: 'settings/ai',
        name: 'settings-ai',
        component: () => import('@/views/AISettingsView.vue'),
        meta: { title: 'AI 配置' }
      },
      {
        path: 'settings/browser',
        name: 'settings-browser',
        component: () => import('@/views/BrowserSettingsView.vue'),
        meta: { title: '浏览器配置' }
      },
      {
        path: 'settings/http',
        name: 'settings-http',
        component: () => import('@/views/HttpSettingsView.vue'),
        meta: { title: 'HTTP 配置' }
      },
      {
        path: 'settings/defaults',
        name: 'settings-defaults',
        component: () => import('@/views/DefaultsSettingsView.vue'),
        meta: { title: '全局默认配置' }
      },
      {
        path: 'settings/proxy-pool',
        name: 'settings-proxy-pool',
        component: () => import('@/views/ProxyPoolSettingsView.vue'),
        meta: { title: '代理池配置' }
      },
      {
        path: 'proxies',
        name: 'proxies',
        component: () => import('@/views/ProxiesView.vue'),
        meta: { title: '代理管理' }
      },
      {
        path: 'settings/notify',
        name: 'settings-notify',
        component: () => import('@/views/NotifySettingsView.vue'),
        meta: { title: '邮件通知配置' }
      },
      {
        path: 'keys',
        name: 'keys',
        component: () => import('@/views/KeysView.vue'),
        meta: { title: 'API Key 管理' }
      },
      {
        path: 'export',
        name: 'export',
        component: () => import('@/views/ExportView.vue'),
        meta: { title: '配置导出' }
      }
    ]
  },
  {
    path: '/:pathMatch(.*)*',
    redirect: '/'
  }
]

const router = createRouter({
  history: createWebHistory(),
  routes
})

// 全局守卫：登录态检查
router.beforeEach((to) => {
  const auth = useAuthStore()
  if (!to.meta.public && !auth.token) {
    return { path: '/login', query: { redirect: to.fullPath } }
  }
  if (to.path === '/login' && auth.token) {
    return { path: '/' }
  }
  return true
})

router.afterEach((to) => {
  const title = to.meta.title as string | undefined
  document.title = title ? `${title} · NewAPI 签到配置管理平台` : 'NewAPI 签到配置管理平台'
})

export default router
