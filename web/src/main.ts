/*
web/src/main.ts
入口：创建 Vue 应用并挂载
职责：
- 注册 Pinia / Vue Router
- 导入全局样式
数据来源：无（纯装配）
*/
import { createApp } from 'vue'
import { createPinia } from 'pinia'
import App from './App.vue'
import router from './router'
import './styles/main.css'

const app = createApp(App)
app.use(createPinia())
app.use(router)
app.mount('#app')
