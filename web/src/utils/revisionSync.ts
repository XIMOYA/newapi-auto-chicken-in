/*
web/src/utils/revisionSync.ts
纯函数工具：配置版本号轮询的时机判断。

为什么存在：
- 多人同时用这套后台时，别人加的账号、改的设置不该等到我手动刷新才出现。
  轮询 GET /api/config/revision（只返回一个整数）比轮询整份配置便宜得多。
- 但轮询不能无条件发：标签页在后台时没人看、上一发还没回来时再发只会堆请求、
  store 自己正在拉配置时轮询纯属重复劳动。
- 判断抽成纯函数，Vitest 可以直接覆盖这些边界，不必去模拟定时器和 DOM。
*/

/** 轮询间隔：快到「感觉是实时的」，又不至于让常开的页面一直打服务端 */
export const REVISION_POLL_INTERVAL = 5000

export interface RevisionPollContext {
  /** 标签页是否不可见（document.hidden） */
  hidden: boolean
  /** 上一次轮询请求是否还在飞 */
  inFlight: boolean
  /** store 是否正在拉取整份配置 */
  loading: boolean
}

/**
 * 这一拍该不该发轮询请求。
 *
 * 三个条件都是「跳过这一拍」而不是「停止轮询」：定时器继续跑，
 * 等标签页回到前台或上一发落地后自然恢复。
 */
export function shouldPollRevision(ctx: RevisionPollContext): boolean {
  if (ctx.hidden) return false
  if (ctx.inFlight) return false
  if (ctx.loading) return false
  return true
}
