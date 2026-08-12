<!--
web/src/App.vue
根组件：全局主题 / 中文语言包 / 消息与对话框 Provider
职责：
- NConfigProvider 注入深蓝色主题与中文 locale
- 提供 useMessage / useDialog 等全局 API
-->
<template>
  <n-config-provider :locale="zhCN" :date-locale="dateZhCN" :theme-overrides="themeOverrides" :style="{ height: '100%' }">
    <n-message-provider>
      <n-dialog-provider>
        <n-notification-provider>
          <n-loading-bar-provider>
            <!-- 路由切换：上移淡入过渡（2 段移动，方向一致） -->
            <router-view v-slot="{ Component }">
              <transition name="fade-slide" mode="out-in" appear>
                <component :is="Component" />
              </transition>
            </router-view>
          </n-loading-bar-provider>
        </n-notification-provider>
      </n-dialog-provider>
    </n-message-provider>
  </n-config-provider>
</template>

<script setup lang="ts">
import { NConfigProvider, NMessageProvider, NDialogProvider, NNotificationProvider, NLoadingBarProvider, zhCN, dateZhCN, type GlobalThemeOverrides } from 'naive-ui'

// 深蓝色主色调统一主题
const themeOverrides: GlobalThemeOverrides = {
  common: {
    primaryColor: '#1e5eff',
    primaryColorHover: '#3f79ff',
    primaryColorPressed: '#174bcb',
    primaryColorSuppl: '#1e5eff',
    infoColor: '#1e5eff',
    successColor: '#18a058',
    warningColor: '#f0a020',
    errorColor: '#d03050',
    borderRadius: '6px',
    borderRadiusSmall: '4px',
    fontFamily: "'PingFang SC', 'Microsoft YaHei', 'Helvetica Neue', Helvetica, Arial, sans-serif"
  },
  Button: {
    borderRadiusMedium: '6px'
  },
  Card: {
    borderRadius: '10px'
  },
  Menu: {
    itemHeight: '44px',
    borderRadius: '6px'
  }
}
</script>
