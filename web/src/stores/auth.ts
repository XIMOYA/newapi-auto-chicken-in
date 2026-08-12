/*
web/src/stores/auth.ts
鉴权状态：token / username
职责：
- localStorage 持久化登录态
- 登录 / 登出
数据来源：POST /api/login
*/
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { login as loginApi } from '@/api/auth'

const TOKEN_KEY = 'newapi_token'
const USERNAME_KEY = 'newapi_username'

export const useAuthStore = defineStore('auth', () => {
  const token = ref<string>(localStorage.getItem(TOKEN_KEY) ?? '')
  const username = ref<string>(localStorage.getItem(USERNAME_KEY) ?? '')

  const isLoggedIn = computed(() => token.value !== '')

  function setAuth(newToken: string, newUsername: string) {
    token.value = newToken
    username.value = newUsername
    localStorage.setItem(TOKEN_KEY, newToken)
    localStorage.setItem(USERNAME_KEY, newUsername)
  }

  function clear() {
    token.value = ''
    username.value = ''
    localStorage.removeItem(TOKEN_KEY)
    localStorage.removeItem(USERNAME_KEY)
  }

  async function login(user: string, password: string) {
    const res = await loginApi({ username: user, password })
    setAuth(res.token, res.username)
  }

  return { token, username, isLoggedIn, setAuth, clear, login }
})
