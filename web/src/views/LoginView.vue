<!--
web/src/views/LoginView.vue
页面：登录
职责：
- 居中卡片式登录（用户名/密码），含表单校验与加载态
- 登录成功后拉取一次配置初始化 store，再跳转 /
数据来源：POST /api/login、GET /api/config
-->
<template>
  <div class="login-page">
    <div class="login-bg"></div>
    <div class="login-card">
      <div class="login-brand">
        <n-icon size="40" class="brand-icon"><rocket-outline /></n-icon>
        <h1 class="brand-title">NewAPI 签到</h1>
        <p class="brand-sub">配置管理平台</p>
      </div>

      <n-form ref="formRef" :model="form" :rules="rules" size="large" @keydown.enter.prevent="handleLogin">
        <n-form-item path="username" :show-label="false">
          <n-input v-model:value="form.username" placeholder="用户名" :disabled="loading" autocomplete="username">
            <template #prefix><n-icon><person-outline /></n-icon></template>
          </n-input>
        </n-form-item>
        <n-form-item path="password" :show-label="false">
          <n-input v-model:value="form.password" type="password" show-password-on="click" placeholder="密码" :disabled="loading" autocomplete="current-password">
            <template #prefix><n-icon><lock-closed-outline /></n-icon></template>
          </n-input>
        </n-form-item>
        <n-button type="primary" block size="large" :loading="loading" class="login-btn" @click="handleLogin">
          登 录
        </n-button>
      </n-form>

      <p class="login-tip">默认账号见后端配置，登录后请及时修改默认密码</p>
    </div>
  </div>
</template>

<script setup lang="ts">
import { reactive, ref } from 'vue'
import { NForm, NFormItem, NInput, NButton, NIcon, useMessage, type FormInst, type FormRules } from 'naive-ui'
import { RocketOutline, PersonOutline, LockClosedOutline } from '@vicons/ionicons5'
import { useRoute, useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { useConfigStore } from '@/stores/config'

const route = useRoute()
const router = useRouter()
const authStore = useAuthStore()
const configStore = useConfigStore()
const message = useMessage()

const formRef = ref<FormInst | null>(null)
const loading = ref(false)
const form = reactive({ username: '', password: '' })

const rules: FormRules = {
  username: { required: true, message: '请输入用户名', trigger: ['input', 'blur'] },
  password: { required: true, message: '请输入密码', trigger: ['input', 'blur'] }
}

async function handleLogin() {
  formRef.value?.validate(async (errors) => {
    if (errors) return
    loading.value = true
    try {
      await authStore.login(form.username.trim(), form.password)
      // 登录后先拉取一次配置初始化 store
      try {
        await configStore.fetchConfig()
      } catch {
        // 配置拉取失败不阻塞登录，AdminLayout 会兜底重试
      }
      message.success('登录成功，欢迎回来！')
      const redirect = (route.query.redirect as string) || '/'
      router.push(redirect)
    } catch (e) {
      const msg = (e as { response?: { data?: { error?: string } } })?.response?.data?.error
      message.error(msg || '登录失败，请检查用户名和密码')
    } finally {
      loading.value = false
    }
  })
}
</script>

<style scoped>
.login-page {
  position: relative;
  display: flex;
  align-items: center;
  justify-content: center;
  height: 100vh;
  overflow: hidden;
  background: linear-gradient(135deg, #0e1c3f 0%, #1e3f8f 55%, #2b6cb0 100%);
}

.login-bg {
  position: absolute;
  inset: 0;
  background:
    radial-gradient(circle at 20% 20%, rgba(255, 255, 255, 0.08) 0%, transparent 40%),
    radial-gradient(circle at 80% 80%, rgba(255, 255, 255, 0.06) 0%, transparent 45%);
}

.login-card {
  position: relative;
  z-index: 1;
  width: 400px;
  padding: 40px 36px 28px;
  background: rgba(255, 255, 255, 0.97);
  border-radius: 14px;
  box-shadow: 0 24px 60px rgba(6, 18, 48, 0.45);
}

.login-brand {
  text-align: center;
  margin-bottom: 28px;
}

.brand-icon {
  color: #1e5eff;
}

.brand-title {
  margin: 12px 0 4px;
  font-size: 22px;
  color: #1f2d3d;
  letter-spacing: 1px;
}

.brand-sub {
  margin: 0;
  font-size: 13px;
  color: #8492a6;
}

.login-btn {
  margin-top: 8px;
  font-size: 15px;
  letter-spacing: 6px;
}

.login-tip {
  margin: 18px 0 0;
  text-align: center;
  font-size: 12px;
  color: #a3aec0;
}
</style>
