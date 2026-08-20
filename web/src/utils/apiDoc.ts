/*
web/src/utils/apiDoc.ts
工具：API 文档 Markdown 渲染
职责：
- 把 docs/newapi-config-api.md 的原文渲染成能直接 v-html 的 HTML
- 给标题另分配一套 ASCII 锚点 id：NAnchor 靠选择器查元素，而 GitHub 风格 slug
  会产出 #1-基础 这种数字开头的 id，选择器解析直接抛错，所以不能拿来当 DOM id
- 文档里 [附录 B](#附录-b校验错误文案) 这类内部链接跟着改指到新 id，否则点了不动
- 关掉 html：正文有 <JWT 或 API Key>、<mode> 这种占位符，放开 html 会被浏览器
  当成未知标签直接吞掉；只把表格里当换行用的 <br> 单独放回来
- 顺手抽出代码块原文，页面的「复制」按钮按下标取用，省得从 DOM 里抠文本
数据来源：无（纯字符串处理，输入是 md 原文）
*/
import MarkdownIt, { type Token } from 'markdown-it'

export interface DocHeading {
  id: string
  title: string
  level: number
}

export interface RenderedApiDoc {
  html: string
  headings: DocHeading[]
  codeBlocks: string[]
}

/*
GitHub 生成锚点的规则：转小写、去掉 ASCII 标点（保留 - 和 _）、空白折成连字符。
字符类里的四段分别跳过了 0x2D(-) 和 0x5F(_)，后面接的是文档里实际用到的全角标点。
文档目录里的 #post-apiaccountsops、#附录-a数据模型 都是按这套规则写的，算法得对得上，
否则内部链接找不到目标。
*/
const PUNCT_RE = /[!-,.-/:-@[-^`{-~？！，。；：、（）【】《》“”‘’·—…]/g

export function githubSlug(text: string): string {
  return text.trim().toLowerCase().replace(PUNCT_RE, '').replace(/\s+/g, '-')
}

// <br> 的三种写法，文档表格里用它把长列表压进单元格
const BR_RE = /<br\s*\/?>/gi

const ANCHOR_ID_PREFIX = 'doc-h-'

// inline token 里能贡献可见文字的类型，取标题纯文本时只认这几种
const TEXT_TOKEN_TYPES = new Set(['text', 'code_inline'])

function inlineText(inline: Token): string {
  if (!inline.children) return inline.content
  return inline.children
    .filter((child) => TEXT_TOKEN_TYPES.has(child.type))
    .map((child) => child.content)
    .join('')
}

/*
把 text token 里的 <br> 拆成独立的 html_inline token。
renderer 对 html_inline 是无条件原样输出的，不看 options.html，所以这里手工插进去的
标签能生效，而解析阶段留下的其他 <xxx> 仍然保持转义 —— 相当于只给 <br> 开了个白名单。
*/
function restoreLineBreaks(children: Token[], TokenCtor: typeof Token): Token[] {
  const out: Token[] = []
  let touched = false
  for (const child of children) {
    if (child.type !== 'text') {
      out.push(child)
      continue
    }
    // 只用 split 判断有没有命中：BR_RE 带 g，用 test 会留下 lastIndex 影响下一次调用
    const segments = child.content.split(BR_RE)
    if (segments.length === 1) {
      out.push(child)
      continue
    }
    touched = true
    segments.forEach((segment, idx) => {
      if (idx > 0) {
        const br = new TokenCtor('html_inline', '', 0)
        br.content = '<br />'
        out.push(br)
      }
      if (!segment) return
      const text = new TokenCtor('text', '', 0)
      text.content = segment
      out.push(text)
    })
  }
  return touched ? out : children
}

/*
文档内部链接写的是 GitHub slug，DOM 上挂的却是 doc-h-N，这里按映射表改 href。
表里查不到的（指向 h4、或者锚点本身写错了）保持原样让它自然失效，不去悄悄兜住 ——
点不动比跳到错误位置更容易被发现。
*/
function redirectAnchors(children: Token[], slugToId: Map<string, string>): void {
  for (const child of children) {
    if (child.type !== 'link_open') continue
    const href = child.attrGet('href')
    // attrGet 声明成 string | number，数字型属性值不可能是锚点，直接跳过
    if (typeof href !== 'string' || !href.startsWith('#')) continue
    const target = slugToId.get(decodeURIComponent(href.slice(1)).toLowerCase())
    if (target) child.attrSet('href', `#${target}`)
  }
}

export function renderApiDoc(markdown: string): RenderedApiDoc {
  const md = new MarkdownIt({ html: false, linkify: false, breaks: false })
  const headings: DocHeading[] = []
  const codeBlocks: string[] = []

  md.core.ruler.push('api_doc_transform', (state) => {
    const slugToId = new Map<string, string>()
    let seq = 0
    // 先给标题挂 id，同时记下 slug → id，内部链接下一轮才好改
    state.tokens.forEach((token, idx) => {
      if (token.type !== 'heading_open') return
      const inline = state.tokens[idx + 1]
      const title = inline && inline.type === 'inline' ? inlineText(inline) : ''
      const id = `${ANCHOR_ID_PREFIX}${++seq}`
      token.attrSet('id', id)
      const slug = githubSlug(title)
      if (slug && !slugToId.has(slug)) slugToId.set(slug, id)
      const level = Number(token.tag.slice(1))
      if (level === 2 || level === 3) headings.push({ id, title, level })
    })
    state.tokens.forEach((token) => {
      if (token.type !== 'inline' || !token.children) return
      redirectAnchors(token.children, slugToId)
      token.children = restoreLineBreaks(token.children, state.Token)
    })
    return true
  })

  // 代码块原文存进数组交给页面，按钮只带下标，省了往 data 属性里塞转义文本
  md.renderer.rules.fence = (tokens, idx) => {
    const raw = tokens[idx].content
    const index = codeBlocks.length
    codeBlocks.push(raw)
    const lang = tokens[idx].info.trim()
    const langTag = lang ? `<span class="doc-code-lang">${md.utils.escapeHtml(lang)}</span>` : ''
    return (
      '<div class="doc-code">' +
      langTag +
      `<button class="doc-copy" type="button" data-code-index="${index}">复制</button>` +
      `<pre><code>${md.utils.escapeHtml(raw)}</code></pre>` +
      '</div>'
    )
  }

  return { html: md.render(markdown), headings, codeBlocks }
}
