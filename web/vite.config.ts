import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath, URL } from 'node:url'

// https://vite.dev/config/
export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
      // API 文档页直接吃 docs/ 下的 md 原文，避免在 web 里另存一份导致两边说法不一致
      '@docs': fileURLToPath(new URL('../docs', import.meta.url))
    }
  },
  server: {
    port: 5173,
    host: true,
    fs: {
      // 仓库根没有 package.json，vite 探测出的 workspace root 就停在 web/，
      // 不放开的话 dev 模式下读不到 ../docs。这里只点名 docs 一个目录：host 是 true，
      // 写成 '..' 等于把 data/ 里的 sqlite 一起端给局域网。
      allow: ['.', '../docs']
    },
    proxy: {
      // 开发环境将 /api 代理到本地后端（Go 服务默认端口 8080）
      '/api': {
        target: 'http://127.0.0.1:8080',
        changeOrigin: true
      }
    }
  },
  build: {
    outDir: 'dist',
    chunkSizeWarningLimit: 1500
  }
})
