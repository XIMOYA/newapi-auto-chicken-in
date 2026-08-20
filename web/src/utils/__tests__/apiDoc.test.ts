/*
web/src/utils/__tests__/apiDoc.test.ts
覆盖 API 文档渲染：锚点 id 的形态约束、内部链接重定向、<br> 白名单与占位符转义、
代码块抽取，最后拿真实的 docs/newapi-config-api.md 跑一遍端到端。
*/
import { readFileSync } from 'node:fs'
import { fileURLToPath, URL } from 'node:url'
import { describe, expect, it } from 'vitest'
import { githubSlug, renderApiDoc } from '../apiDoc'

const realDoc = readFileSync(
  fileURLToPath(new URL('../../../../docs/newapi-config-api.md', import.meta.url)),
  'utf-8'
)

describe('githubSlug', () => {
  // 文档目录里的锚点是照 GitHub 规则手写的，算法对不上内部链接就会全部失效
  it('与文档目录里已有的锚点写法一致', () => {
    expect(githubSlug('1. 基础')).toBe('1-基础')
    expect(githubSlug('4. Cookie 可用性检测')).toBe('4-cookie-可用性检测')
    expect(githubSlug('附录 A：数据模型')).toBe('附录-a数据模型')
    expect(githubSlug('POST /api/accounts/ops')).toBe('post-apiaccountsops')
    expect(githubSlug('敏感字段')).toBe('敏感字段')
  })

  it('保留下划线和连字符，其余标点去掉', () => {
    expect(githubSlug('run_state 表')).toBe('run_state-表')
    expect(githubSlug('X-Export-Ticket 票据')).toBe('x-export-ticket-票据')
    expect(githubSlug('（可选）字段？')).toBe('可选字段')
  })
})

describe('renderApiDoc 锚点', () => {
  /*
  这条是防回归的重点：NAnchor 要靠选择器查元素，而 GitHub slug 会产出 #1-基础、
  #4-cookie-可用性检测 这种数字开头的 id，querySelector 直接抛 SyntaxError。
  所以 DOM 上必须是另起的纯 ASCII、字母开头的 id。
  */
  it('标题 id 一律是字母开头的 ASCII', () => {
    const { headings } = renderApiDoc('## 1. 基础\n\n## 附录 A：数据模型\n')
    expect(headings).toHaveLength(2)
    for (const h of headings) {
      expect(h.id).toMatch(/^doc-h-\d+$/)
    }
  })

  it('只把 h2/h3 收进目录，h1 只挂 id 不进目录', () => {
    const { headings, html } = renderApiDoc('# 大标题\n\n## 二级\n\n### 三级\n\n#### 四级\n')
    expect(headings.map((h) => [h.level, h.title])).toEqual([
      [2, '二级'],
      [3, '三级']
    ])
    expect(html).toContain('<h1 id="doc-h-1">大标题</h1>')
  })
})

describe('renderApiDoc 内部链接', () => {
  it('把 GitHub slug 链接改指到实际挂上的 id', () => {
    const { html, headings } = renderApiDoc('见 [附录 B](#附录-b校验错误文案)\n\n## 附录 B：校验错误文案\n')
    expect(html).toContain(`href="#${headings[0].id}"`)
    expect(html).not.toContain('href="#附录-b校验错误文案"')
  })

  it('对不上的锚点原样留着，不去悄悄兜住', () => {
    const { html } = renderApiDoc('[没有这节](#nope)\n\n## 有的\n')
    expect(html).toContain('href="#nope"')
  })

  it('不动外链', () => {
    const { html } = renderApiDoc('[站点](https://tabitoken.com)\n')
    expect(html).toContain('href="https://tabitoken.com"')
  })
})

describe('renderApiDoc 转义边界', () => {
  it('表格里当换行用的 <br> 变成真的换行标签', () => {
    const { html } = renderApiDoc('| 端点 |\n| --- |\n| `GET /a`<br>`GET /b` |\n')
    expect(html).toContain('<br />')
  })

  /*
  文档正文里 <JWT 或 API Key>、<mode> 是占位符而不是标签。这也是不能图省事开
  html: true 的原因——浏览器会把这种未知标签直接吞掉，读者看到的就是半句话。
  */
  it('占位符继续保持转义，不会被当标签吃掉', () => {
    const { html } = renderApiDoc('形如 <JWT 或 API Key> 的占位符\n')
    expect(html).toContain('&lt;JWT 或 API Key&gt;')
    expect(html).not.toContain('<JWT')
  })

  it('放开的只有 br，其他标签仍旧转义', () => {
    const { html } = renderApiDoc('<script>alert(1)</script> 与 a<br>b\n')
    expect(html).toContain('&lt;script&gt;')
    expect(html).not.toContain('<script>')
    expect(html).toContain('<br />')
  })
})

describe('renderApiDoc 代码块', () => {
  it('抽出原文并给按钮标上下标', () => {
    const { html, codeBlocks } = renderApiDoc('```bash\ncurl -s http://x/api/health\n```\n')
    expect(codeBlocks).toEqual(['curl -s http://x/api/health\n'])
    expect(html).toContain('data-code-index="0"')
    expect(html).toContain('doc-code-lang">bash<')
  })

  it('多个代码块按出现顺序编号', () => {
    const { codeBlocks, html } = renderApiDoc('```\na\n```\n\n```\nb\n```\n')
    expect(codeBlocks).toEqual(['a\n', 'b\n'])
    expect(html).toContain('data-code-index="1"')
  })

  // 收集用的是闭包数组，实例每次新建，反复调用不该越滚越多
  it('反复渲染同一份文档结果稳定', () => {
    const first = renderApiDoc(realDoc)
    const second = renderApiDoc(realDoc)
    expect(second.codeBlocks).toHaveLength(first.codeBlocks.length)
    expect(second.headings).toHaveLength(first.headings.length)
    expect(second.html).toBe(first.html)
  })
})

describe('真实文档', () => {
  it('目录条数与标题数量对得上', () => {
    const { headings } = renderApiDoc(realDoc)
    const h2 = headings.filter((h) => h.level === 2)
    const h3 = headings.filter((h) => h.level === 3)
    expect(h2.length).toBeGreaterThanOrEqual(15)
    expect(h3.length).toBeGreaterThanOrEqual(30)
    expect(h2.map((h) => h.title)).toContain('认证方式')
  })

  it('锚点 id 不重复', () => {
    const { headings } = renderApiDoc(realDoc)
    const ids = headings.map((h) => h.id)
    expect(new Set(ids).size).toBe(ids.length)
  })

  /*
  文档里 19 处内部链接（目录 15 条 + 正文 4 条）必须全部命中，一条都不该剩。
  留下未重写的锚点说明标题被改名或 slug 算法跟文档写法脱节了，点下去不会动。
  */
  it('所有站内锚点都已重写，没有失效残留', () => {
    const { html } = renderApiDoc(realDoc)
    const leftover = [...html.matchAll(/href="#([^"]*)"/g)]
      .map((m) => m[1])
      .filter((href) => !/^doc-h-\d+$/.test(href))
    expect(leftover).toEqual([])
  })
})
