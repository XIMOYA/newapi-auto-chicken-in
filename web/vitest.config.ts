import { defineConfig } from 'vitest/config'

// Vitest 独立配置：纯函数/工具测试走 node 环境，无需浏览器与 vue 插件。
// vite.config.ts 中的构建配置（别名 @ -> src）对纯函数测试非必需；
// 若后续补 store/组件测试需要 DOM 或别名，再在这里按需扩展。
export default defineConfig({
  test: {
    environment: 'node',
    include: ['src/**/*.test.ts']
  }
})
