/*
web/src/api/http.ts
Axios 实例与拦截器
职责：
- baseURL 固定 /api
- 请求拦截：自动附加 Authorization: Bearer <token>
- 响应拦截：401 时清空登录态并跳转 /login
数据来源：localStorage 中的 token（经 Pinia auth store）
*/
import axios, { type AxiosInstance } from 'axios'
import { useAuthStore } from '@/stores/auth'
import router from '@/router'

const http: AxiosInstance = axios.create({
  baseURL: '/api',
  timeout: 30000
})

// 请求拦截：附加 JWT
http.interceptors.request.use((config) => {
  const auth = useAuthStore()
  if (auth.token) {
    config.headers.Authorization = `Bearer ${auth.token}`
  }
  return config
})

// 响应拦截：401 统一登出
http.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error.response && error.response.status === 401) {
      const auth = useAuthStore()
      auth.clear()
      if (router.currentRoute.value.path !== '/login') {
        router.push({ path: '/login', query: { redirect: router.currentRoute.value.fullPath } })
      }
    }
    return Promise.reject(error)
  }
)

export default http
