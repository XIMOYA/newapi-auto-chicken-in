<!--
MEMORY.md
会话记忆：本轮开发的进展、已定决策、未决问题与下一步
职责：
- 让新会话能从断点接着干，而不用重读全部代码和历史对话
- 只记「不看代码就想不起来 / 想错的东西」：决策理由、踩过的坑、待确认项
- 不记代码细节 —— 代码本身和它的注释才是唯一事实来源
维护约定：
- 每完成一个功能块就更新，别攒到最后
- 已完成项保留一句话结论即可，重点留给「未决」和「坑」
-->

# 当前会话记忆（xray / VLESS 接入 + 站点 API Key）

## 一、本轮已完成（都已提交，全量测试绿）

| 提交 | 内容 |
|---|---|
| `375957e` | Go：xray 配置生成器（单进程多 socks 入站 + vless 出站 + tag 路由）+ 单测 |
| `ae163d5` | Python：`newapi_checkin/xray.py` xray 管理器（定位二进制、起进程、节点→本地 socks5、回收）+ 27 用例 |
| `e1f5931` | Python：代理池接进 xray，**节点标识与本地入口两个键彻底分开** |
| `d239a1f` | 修：体检链路（sweep/preflight）不再拿「本机没装 xray」诬陷 VLESS 节点 |
| `ed9aa97` | CI：三处 workflow 装 xray（版本 pin + 缓存 + 装完就地验证）+ `scripts/install-xray.sh` |

Python 全量 **1143 passed**。

### 这几块里最容易改坏的三条约定

1. **节点标识 ≠ 本地入口，两个键不许混用。**
   - `account.proxy` = 拿去拨号的地址（VLESS 时是 `socks5://127.0.0.1:随机端口`）
   - `account.proxy_identity` = 稳定标识（原始 `vless://` URI）
   - `account.proxy_key` = 记账/比对统一走这个（回落 `proxy`，兼容手动配置）
   - 翻译只发生在 `ProxyPool.dial_target` 一处；`mark_bad`/`mark_ok`/`feedback_snapshot` 全按标识。
   - 混用的后果：全池成功率反馈塌到 `127.0.0.1` 一个键上，平台侧优选排序当场失效，**日志上看不出异常**。

2. **过盾缓存必须按标识比对。**
   本地端口从固定起点分配，*不同节点很可能先后拿到同一个端口*。按拨号地址比对会把 A 节点的 `cf_clearance` 判给 B 节点用，然后静默被盾拦。`_harvest` 存标识、`check` 比标识，两侧都有用例守。

3. **体检链路对「没有本地入口」的节点是跳过不记账，不是记失败。**
   `sweep_remote` 的结论直接回传平台做排序；用「本机没装 xray」记一堆 `net_fail` 等于把好节点排到最后，比不测更坏。`preflight` 整体跳过 VLESS（它跑在 xray 起来之前，硬测会把整池机场节点当场拉黑清零）。

### 变异测试记录（改回旧行为确认变红后还原）
不记标识→3 红；`check` 用拨号地址→1 红；`_harvest` 用拨号地址→1 红；swap 不分两键→1 红；无 xray 回落原 URI→1 红；`finally` 不回收→1 红；不剔除没入口的节点→1 红；proxy-sweep 不装 xray→1 红；两边版本不一致→1 红；脚本改装到 `lib/`→1 红。

**两次假绿是变异测试抓出来的，值得记住**：
- `init_proxy_pool` 起 xray 那行漏掉时，所有直接调 `_start_xray` 的用例照样全绿 → 补了两条走真实 `init_proxy_pool` 的用例
- `preflight` 跳过 VLESS 那条，只放 VLESS 时「无一测通就整体放弃自筛」的保护会替测试兜住 → 必须混一个测得通的普通代理进去

### 自己造的坑（已修，记着别再犯）
`Edit` 的子串匹配把 `init_proxy_pool` except 分支里的 `self._pool = None` 缩进从 12 空格改成 8 空格，等于代理池初始化完立刻被无条件清空。全量测试当场 30 红。**改缩进敏感位置后要 `git diff` 核对**。

---

## 二、进行中：站点 API Key（`spec://tasks.json` 里标 doing）

### 协议已全部查证（QuantumNous/new-api 主干源码，不是猜的）

路由 `router/api-router.go`，组前缀 `/api/token`，整组 `middleware.UserAuth()`（站点 session cookie 就够）：
```go
tokenRoute.GET("/",         controller.GetAllTokens)
tokenRoute.POST("/",        controller.AddToken)
tokenRoute.POST("/:id/key", controller.GetTokenKey)
```

- 分页（`common/page_info.go`）：`p`（页码，**1 起**）+ `page_size`，兼容 `ps`/`size`；`page_size > 100` 被截到 100。
- 创建请求体（`AddToken` 绑 `model.Token`）：`name`、`remain_quota`、`unlimited_quota`、`expired_time`（`-1`=永不过期）、`model_limits_enabled`、`model_limits`（**字符串不是数组**）、`allow_ips`、`group`。name 超 50 字符直接拒。
- 创建响应：`{"success":true,"message":""}` —— **没有 data，拿不到 key**。
- **列表接口的 key 是打码的**：`GetAllTokens` → `buildMaskedTokenResponses` → `MaskTokenKey`，形如 `abcd**********wxyz`。所以「创建后回查列表取 key」这条路走不通，会拿到一串长得很像 key 的废字符串。
- `common.GenerateKey()` 存的是**不带 `sk-` 前缀**的原始值，鉴权中间件做 `TrimPrefix(key, "sk-")`。

### 因此完整链路必须四步
1. `GET /api/token/?p=1&page_size=100`，按 `name` 找
2. 没有 → `POST /api/token/`
3. 再列一次，拿到该条的 `id`
4. `POST /api/token/{id}/key` → `{"success":true,"data":{"key":"<完整值>"}}`

### KIQ 已确认的决策（全部拍定，可直接实现）
1. **落库存 `sk-<key>`**（能直接粘去用的形态，带不带前缀站点都认）。
2. **不需要配额与模型限制**：`remain_quota`/`model_limits` 都不设，走站点默认。
   → 请求体只带 `name` + `expired_time: -1`（永不过期），其余交给站点默认值。
3. **走 B 方案**（KIQ 明确回答「B」）：每个签到账号在自己站点上建**一条** key，没有才建，
   最后把所有账号的 key 汇总成一份清单。**不是**遍历站点上已有的所有 token 逐条取值。
4. token 名字用固定值 `newapi-auto-checkin` —— 这就是「没有才创建」的判据。
5. 汇总端点做两个：`GET /api/sites/apikeys` 双认证但 key 打码（看哪些账号有/缺），
   `GET /api/sites/apikeys/raw` 只认 API Key 给全值。与 `/api/config/raw`、
   `/api/accounts/{name}/raw` 同一套惯例：含明文凭据的一律只认 API Key。

### 兼容旧站点的做法（已定）
老版本 new-api 列表接口直接吐完整 key。做成「列表里的 key 含 `*` 就判为打码、走第 4 步；不含就直接用」，新旧站点都不用配。

### 实现落点（设计已定，正在开写）
- 新文件 `server/site_apikey.go` + `server/site_apikey_test.go`
- `Account` 加字段 `APIKey string \`json:"api_key"\``
- **`MaskConfig` 必须打码它、`UnmaskConfig` 按账号名还原** —— 跟 `Cookie` 完全同一套口径。
  漏了就是明文下发前端，这个坑踩过一次（`606d56e` 修的就是池子 session 漏打码）。
- 核心函数 `ensureAccountAPIKey`，四步链路见上。HTTP 调用注入函数类型以便离线测试
  （照 `site_provision.go` 的 `provisionIssuer` 做法）
- 响应信封解析复用 `cookieTestJSONMap` / `cookieTestMessage`（`server/cookie_checker.go`）
- 请求头复用 `setCookieTestCommonHeaders` / `setCookieTestUserID`；客户端用
  `newCookieTestHTTPClient`（代理优先级：账号自带 > 池子 > 环境变量）
- 鉴权两条路：cookie 含 `new_api_refresh=` 时先 `POST /api/user/auth/refresh` 换 session
  （复用 `parseCookieTestRefreshBundle`），否则直接带 cookie
- ⚠️ **refresh 会消耗一代 secret**：轮转出的新 cookie 必须落库，否则下轮保活拿旧代去用
  就是 `AUTH_SESSION_REVOKED`、整条会话报废。沿用 `checkTabiAICookie` 的处置
  （`extractTabiAIRefreshCookie` + 代次抢救必须先于任何 return）
- 批量端点 `POST /api/sites/apikeys`，body `{"only": [...]}`。**串行**执行：
  `POST /:id/key` 挂着 `CriticalRateLimit()`，并发会撞限流。开头过 `guardRunningCheckin`
- 落库沿用 `saveProvisionedAccounts` 的模式：**持锁重读最新配置再改**，
  不能拿开头那份直接写回（中间几分钟保活可能已轮转过别的账号凭据）

---

## 三、未开始

1. **Python: GitHub 账号自动填入编排**（ReMail → 浏览器登录 → 状态过滤 → 写回池子）
   地基已就绪：`bba6290` ReMail 多 key 客户端、`407a491` 浏览器 GitHub 登录判定、`215dbda` 账号自身状态探测。信息齐全，可直接做。
2. **重建 linux-amd64 单文件二进制并提交**（前端有改动时必做，参照 `aa41b2f`/`b54fece` 的做法）
3. 最后汇总报告需要 KIQ 确认的点

---

## 四、环境与协作约定

- Windows 原生环境 + Git Bash（MSYS2）。**禁用** `findstr`/`dir`/`type` 等 cmd 内建（`/flag` 会被 MSYS2 转成路径）。用 Grep/Glob/Read/Write/Edit 工具。
- 不许提 WSL。
- 代码顶部写文件路径 + 职责注释；注释要像真开发者写的，不要 AI 讲解腔。
- **改代码前先给分析和方案，等 KIQ 同意再动。禁止删除任何已有功能实现。**
- 每次改完要审查 + 详细测试。
- 提交信息写「为什么」，把踩过的坑和变异测试结论写进去。
- 称呼用户为 **KIQ**。
- 子代理默认跟随主代理的模型与思考强度。
