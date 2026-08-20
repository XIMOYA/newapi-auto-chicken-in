<!--
web/src/views/ApiDocView.vue
页面：API 文档
职责：
- 渲染仓库里的 docs/newapi-config-api.md，跟随二进制一起发布，不额外起文档站
- 右侧目录跟随正文滚动（NAnchor 监听的是本页自己的滚动容器，不是外层布局的）
- 代码块右上角一键复制：按钮由渲染器注入，这里用事件委托接，v-html 出来的节点绑不了 Vue 事件
数据来源：无（md 原文在构建期打进 bundle）
-->
<template>
  <div class="doc-page">
    <div ref="bodyRef" class="doc-body">
      <n-alert type="info" :bordered="false" class="doc-hint">
        <template #icon><n-icon><information-circle-outline /></n-icon></template>
        本页渲染的是仓库里的 <n-text code>docs/newapi-config-api.md</n-text>，与随二进制发布的
        是同一份，共 {{ tocHeadings.length }} 个小节。运维类接口 JWT 与 API Key 通用，
        改密码、增删 Key、整份导入仍只认 JWT。
      </n-alert>
      <article class="markdown-body" v-html="rendered.html" @click="handleBodyClick" />
    </div>

    <aside class="doc-toc">
      <div class="doc-toc-title">目录</div>
      <n-anchor
        v-if="scrollTarget"
        :offset-target="offsetTarget"
        type="block"
        :bound="28"
        :show-rail="false"
        ignore-gap
        class="doc-anchor"
      >
        <n-anchor-link
          v-for="heading in tocHeadings"
          :key="heading.id"
          :title="heading.title"
          :href="`#${heading.id}`"
          :class="heading.level === 3 ? 'toc-sub' : 'toc-top'"
        />
      </n-anchor>
    </aside>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { NAlert, NAnchor, NAnchorLink, NIcon, NText, useMessage } from 'naive-ui'
import { InformationCircleOutline } from '@vicons/ionicons5'
import rawDoc from '@docs/newapi-config-api.md?raw'
import { renderApiDoc } from '@/utils/apiDoc'
import { copyText } from '@/utils/clipboard'

const message = useMessage()
const rendered = renderApiDoc(rawDoc)
// md 自带的「目录」章节渲染后就在正文顶部，右侧再列一条同名项属于重复
const tocHeadings = rendered.headings.filter((heading) => heading.title !== '目录')

const bodyRef = ref<HTMLElement | null>(null)
/*
NAnchor 判定当前小节，靠的是 getOffset(标题, offsetTarget) 算出的相对位置，默认基准是
document，也就是视口坐标。本页正文在自己的滚动容器里，基准不换就永远算不出 top <= bound，
高亮一次都不会亮。listen-to 解决不了这件事 —— BaseAnchor 是在 document 上按捕获阶段监听
scroll 的，内层容器的滚动它本来就收得到，listen-to 只服务 affix 定位。
*/
const scrollTarget = ref<HTMLElement | null>(null)
const offsetTarget = () => scrollTarget.value as HTMLElement

onMounted(() => {
  scrollTarget.value = bodyRef.value
})

async function handleBodyClick(event: MouseEvent) {
  const hit = (event.target as HTMLElement | null)?.closest<HTMLElement>('.doc-copy')
  if (!hit) return
  const index = Number(hit.dataset.codeIndex)
  const code = rendered.codeBlocks[index]
  if (code === undefined) return
  const ok = await copyText(code)
  if (ok) {
    message.success('已复制到剪贴板')
  } else {
    message.error('复制失败，请手动选中')
  }
}
</script>

<style scoped>
/*
两栏各自滚动，别去用外层 admin-content 的滚动条：NAnchor 需要一个明确的滚动容器，
借外层的就得去猜 naive-ui 内部那层 scrollbar 的 class，它一升级就可能变名字。
108px = 顶栏 60 + admin-content 上下 padding 24×2，都是 AdminLayout 里写死的常量。
*/
.doc-page {
  display: flex;
  gap: 16px;
  height: calc(100vh - 108px);
}

.doc-body {
  flex: 1;
  min-width: 0;
  overflow-y: auto;
  padding: 20px 24px 40px;
  background: #fff;
  border-radius: 6px;
  box-shadow: 0 1px 2px rgba(15, 31, 71, 0.06);
}

.doc-hint {
  margin-bottom: 18px;
}

.doc-toc {
  width: 252px;
  flex-shrink: 0;
  overflow-y: auto;
  padding: 16px 8px 24px 16px;
  background: #fff;
  border-radius: 6px;
  box-shadow: 0 1px 2px rgba(15, 31, 71, 0.06);
}

.doc-toc-title {
  font-size: 13px;
  font-weight: 600;
  color: #1f2d3d;
  margin-bottom: 10px;
}

.doc-anchor :deep(.toc-sub) {
  padding-left: 14px;
}

/* v-html 出来的节点不带 scoped 属性，正文排版一律走 :deep */
.markdown-body :deep(h1) {
  font-size: 22px;
  margin: 4px 0 16px;
  color: #142a5c;
}

.markdown-body :deep(h2) {
  font-size: 18px;
  margin: 30px 0 12px;
  padding-bottom: 8px;
  border-bottom: 1px solid #e6eaf2;
  color: #142a5c;
}

.markdown-body :deep(h3) {
  font-size: 15px;
  margin: 22px 0 10px;
  color: #1f3a6e;
}

.markdown-body :deep(h4) {
  font-size: 14px;
  margin: 16px 0 8px;
  color: #1f2d3d;
}

.markdown-body :deep(p),
.markdown-body :deep(li) {
  font-size: 13.5px;
  line-height: 1.75;
  color: #37455c;
}

.markdown-body :deep(a) {
  color: #1e5eff;
  text-decoration: none;
}

.markdown-body :deep(a:hover) {
  text-decoration: underline;
}

.markdown-body :deep(blockquote) {
  margin: 14px 0;
  padding: 10px 14px;
  border-left: 3px solid #6ea8ff;
  background: #f5f8ff;
  border-radius: 0 4px 4px 0;
}

.markdown-body :deep(blockquote p) {
  margin: 0;
}

.markdown-body :deep(table) {
  width: 100%;
  border-collapse: collapse;
  margin: 14px 0;
  font-size: 12.5px;
}

.markdown-body :deep(th),
.markdown-body :deep(td) {
  border: 1px solid #e6eaf2;
  padding: 7px 10px;
  text-align: left;
  vertical-align: top;
  line-height: 1.7;
  color: #37455c;
}

.markdown-body :deep(th) {
  background: #f7f9fc;
  color: #1f2d3d;
  font-weight: 600;
  white-space: nowrap;
}

.markdown-body :deep(code) {
  font-family: 'JetBrains Mono', Consolas, Monaco, monospace;
  font-size: 12px;
  padding: 1px 5px;
  border-radius: 3px;
  background: #f0f3f9;
  color: #c7254e;
  word-break: break-all;
}

.markdown-body :deep(hr) {
  border: none;
  border-top: 1px solid #e6eaf2;
  margin: 26px 0;
}

.markdown-body :deep(strong) {
  color: #1f2d3d;
  font-weight: 600;
}

/* 代码块：容器负责定位，复制按钮悬浮在右上角，鼠标移上去才显形 */
.markdown-body :deep(.doc-code) {
  position: relative;
  margin: 14px 0;
}

.markdown-body :deep(.doc-code pre) {
  margin: 0;
  padding: 12px 14px;
  overflow-x: auto;
  background: #1b2436;
  border-radius: 5px;
}

.markdown-body :deep(.doc-code code) {
  background: transparent;
  color: #d6e2f5;
  padding: 0;
  font-size: 12px;
  line-height: 1.7;
  word-break: normal;
  white-space: pre;
}

.markdown-body :deep(.doc-code-lang) {
  position: absolute;
  top: 8px;
  right: 62px;
  font-size: 11px;
  color: #6c7c99;
  user-select: none;
}

.markdown-body :deep(.doc-copy) {
  position: absolute;
  top: 6px;
  right: 8px;
  padding: 2px 9px;
  font-size: 11px;
  color: #9fb0cc;
  background: rgba(255, 255, 255, 0.08);
  border: 1px solid rgba(255, 255, 255, 0.16);
  border-radius: 4px;
  cursor: pointer;
  opacity: 0;
  transition: opacity 0.15s ease, color 0.15s ease;
}

.markdown-body :deep(.doc-code:hover .doc-copy) {
  opacity: 1;
}

.markdown-body :deep(.doc-copy:hover) {
  color: #fff;
  background: rgba(110, 168, 255, 0.28);
}
</style>
