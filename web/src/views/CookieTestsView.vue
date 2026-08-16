<!--
web/src/views/CookieTestsView.vue
页面：Cookie 可用性测试
职责：
- 站点 Cookie 与 GitHub OAuth 使用两个独立 Tab 和独立请求状态
- 只检测当前登录方式下的启用账号
- 只做凭据可用性检查，不执行真正签到
-->
<template>
  <div class="page-container cookie-tests-page">
    <n-alert type="warning" :bordered="false" class="intro-alert">
      站点 Cookie 与 GitHub OAuth 分开检测，结果不会互相覆盖。检测在服务端使用已保存的敏感凭据，浏览器不会接触 Cookie 明文。
    </n-alert>

    <n-tabs v-model:value="activeMode" type="line" animated>
      <n-tab-pane name="newapi_cookie" tab="站点 Cookie">
        <div class="tab-summary">
          <n-space :size="8" align="center">
            <n-tag size="small" type="info" :bordered="false">可检测 {{ newapiAccounts.length }} 个启用账号</n-tag>
            <template v-if="newapiResponse">
              <n-tag size="small" type="success" :bordered="false">有效 {{ newapiResponse.summary.valid }}</n-tag>
              <n-tag size="small" type="error" :bordered="false">失效 {{ newapiResponse.summary.invalid }}</n-tag>
              <n-tag size="small" type="warning" :bordered="false">异常 {{ newapiResponse.summary.abnormal }}</n-tag>
              <span class="checked-at">上次检测：{{ formatTime(newapiResponse.checked_at) }}</span>
            </template>
          </n-space>
        </div>
        <cookie-test-panel
          mode="newapi_cookie"
          :accounts="newapiAccounts"
          :results="newapiResponse?.results ?? []"
          :loading="newapiLoading"
          @run="runNewAPITest"
        />
      </n-tab-pane>

      <n-tab-pane name="github_cookie" tab="GitHub OAuth">
        <div class="tab-summary">
          <n-space :size="8" align="center">
            <n-tag size="small" type="info" :bordered="false">可检测 {{ githubAccounts.length }} 个启用账号</n-tag>
            <template v-if="githubResponse">
              <n-tag size="small" type="success" :bordered="false">有效 {{ githubResponse.summary.valid }}</n-tag>
              <n-tag size="small" type="error" :bordered="false">失效 {{ githubResponse.summary.invalid }}</n-tag>
              <n-tag size="small" type="warning" :bordered="false">异常 {{ githubResponse.summary.abnormal }}</n-tag>
              <span class="checked-at">上次检测：{{ formatTime(githubResponse.checked_at) }}</span>
            </template>
          </n-space>
        </div>
        <cookie-test-panel
          mode="github_cookie"
          :accounts="githubAccounts"
          :results="githubResponse?.results ?? []"
          :loading="githubLoading"
          @run="runGithubTest"
        />
      </n-tab-pane>
    </n-tabs>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { NAlert, NSpace, NTabPane, NTabs, NTag, useMessage } from 'naive-ui'
import CookieTestPanel from '@/components/CookieTestPanel.vue'
import { testGithubCookies, testNewAPICookies } from '@/api/cookieTests'
import { useConfigStore } from '@/stores/config'
import { extractErrorMessage } from '@/utils/error'
import type { Account, CookieTestResponse } from '@/types'

const configStore = useConfigStore()
const message = useMessage()

const activeMode = ref<'newapi_cookie' | 'github_cookie'>('newapi_cookie')
const newapiLoading = ref(false)
const githubLoading = ref(false)
const newapiResponse = ref<CookieTestResponse | null>(null)
const githubResponse = ref<CookieTestResponse | null>(null)

const accounts = computed<Account[]>(() => configStore.config?.accounts ?? [])
const newapiAccounts = computed(() => accounts.value.filter((account) => account.enabled && account.login_method !== 'github_cookie'))
const githubAccounts = computed(() => accounts.value.filter((account) => account.enabled && account.login_method === 'github_cookie'))

function formatTime(value: string) {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString('zh-CN', { hour12: false })
}

function summaryMessage(label: string, response: CookieTestResponse) {
  const { valid, invalid, abnormal, skipped } = response.summary
  const extra = skipped ? `，跳过 ${skipped}` : ''
  return `${label}检测完成：有效 ${valid}，失效 ${invalid}，异常 ${abnormal}${extra}`
}

async function runNewAPITest(accountNames: string[]) {
  newapiLoading.value = true
  try {
    const response = await testNewAPICookies(accountNames)
    newapiResponse.value = response
    message.success(summaryMessage('站点 Cookie', response))
  } catch (error) {
    message.error(extractErrorMessage(error, '站点 Cookie 检测失败'))
  } finally {
    newapiLoading.value = false
  }
}

async function runGithubTest(accountNames: string[]) {
  githubLoading.value = true
  try {
    const response = await testGithubCookies(accountNames)
    githubResponse.value = response
    message.success(summaryMessage('GitHub OAuth', response))
  } catch (error) {
    message.error(extractErrorMessage(error, 'GitHub OAuth 检测失败'))
  } finally {
    githubLoading.value = false
  }
}
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
