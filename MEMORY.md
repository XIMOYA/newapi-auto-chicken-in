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

1. ~~**Python: GitHub 账号自动填入编排**~~ → 已完成，见 §五（改动在工作树里，未提交）
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

---

## 五、GitHub 账号自动填入编排（本轮完成，**改动在工作树里，未提交**）

新增 `newapi_checkin/github_provision.py` + `tests/test_github_provision.py`（86 例），
`config.py` 加 `github_provision` 配置节，`remote_sync._LOCAL_CONTROLLED_KEYS` 加同名键，
`config.example.json` 补样例。Python 全量 **1229 passed**。

### 查证到的实证（不是约定）

- **写回只能走 `POST /api/github-accounts/ops`**。Python 侧 `Config` 压根没有
  `github_accounts` 这一节 —— 池子只存在平台库里，客户端是读取方。端点按
  `config_sync.url` 同源推导，与 `remote_sync._issue_endpoint` 同一套惯例。
  字段名以 `server/github_account_ops.go` 的 `GitHubAccountOp` 为准；
  `fingerprint`/`proxy_addr` 是服务端状态，不提交。
- **验收只认响应体 `ok`**，不认 HTTP 状态码（网关会代答 200），并且 `skipped` 非空也判负。
- 平台状态复核用 `{"user_session": ...}` 而不是 `{"name": ...}`：账号还没入池，按 name 查会 404。
- Python 侧**没有**账号状态探测实现（`215dbda` 只做了 Go 侧），所以本地判定自己写：
  浏览器 goto `settings/profile` → `classify_profile_page`，分支顺序照抄 Go 的
  `classifyGitHubProfileResponse`，特征词表复用 `github_login.classify_account_page`。

### 三条最容易改坏的约定

1. **状态不是 active 就绝不写回**。被停用/封禁的账号照样能登录、照样下发 user_session，
   入池后每轮签发都失败。判不出（unknown）同样不入池 —— 丢一个待入池账号只浪费一次登录，
   放进一个坏账号是每轮都要付的账。
2. **取验证码的 since 必须在提交登录之前取**，第二次取码要换新的 since。
   取晚了会把「提交后一秒送达的那封」判成旧邮件、死等一封不会来的新邮件；
   不换 since 就会一直填同一个错码。
3. **填完设备验证码后时间盒重新起算**。取码本身可能烧掉几十秒，不重置会把刚填上码、
   马上就要成功的登录判死。

### 变异测试记录（22 个变异点，全部确认变红后还原）

判定顺序错乱→2 红；不认「打回登录页」→2 红；去掉正文长度闸→1 红；只信 HTTP 状态码→1 红；
无视 skipped→1 红；空 session 也发请求→1 红；不校验 usable→1 红；since 取在提交后→1 红；
第二次不换 since→1 红；不重置时间盒→1 红；放宽取码次数→1 红；去掉状态过滤→7 红；
无视复核开关→1 红；code_provider 忘了取 `[0]`→1 红；没收件箱也去登录→1 红；
写回失败当成功→1 红；明文凭据进结果→1 红；登录失败报成功→4 红；不看 config_sync 开关→1 红；
无视 only→1 红；本地控制名单去掉本节→1 红；去掉 build_config 前置校验→1 红；
不同源推导→2 红；profile 目录不按账号分→2 红；goto 不吞超时→1 红；不拦空用户名→1 红。

**抓到两个假绿，都值得记住**：
- 「空正文不能判 active」那条：原用例用的是 `""`/`<html></html>`，去掉长度闸后照样绿
  （正文里也没有登录态特征词）。补了一条 `<html class="logged-in">` —— GitHub 的 html
  标签本来就带这个 class，**半加载的页面是真会走到这里的**。
- 前置校验的用例原来用正常假驱动，去掉校验后测试掉进真实登录循环干等超时，
  表现为「卡住」而不是「红」。换成爆炸工厂（起浏览器即断言失败）才能快速变红。

### 待 KIQ 确认（都没有实证，我按最保守的做法留了配置项）

1. **收件箱前缀 == GitHub 用户名？** `Remail.find_email` 做的是「deliveryEmail 的 @ 前缀
   与目标名全等」的严格匹配。两者不一致时必须配 `accounts[].email_name`，否则一定落空。
2. **出口**：`provision_accounts(proxy=...)` 默认直连，没接代理池。平台侧才是持有
   「账号→出口绑定」的一方（`github_binding.go`），客户端自己挑出口会跟平台的绑定打架。
   顺带一个固有缝隙：本机登录用的出口与平台后续签发用的绑定出口**必然不同**，
   平台第一次用这条 session 时就换了 IP。
3. **平台状态复核默认关**（`platform_status_recheck: false`）：开了会让这条刚登录出来的
   session 立刻从平台的 IP 再出现一次，会话换 IP 是风控高权重信号。
4. **没做「已在池子里就跳过」**：`GET /api/config` 下发的 `user_session` 是打码的 `"***"`，
   判不出库里那条还有效没有，所以只能按 name 判，那又会拦住「想刷新失效凭据」的重跑。
   目前每次跑都会 upsert 覆盖（平台语义天然幂等）。
5. **没挂 CLI 入口**：`main.py` 是 flag 风格，加 `--github-provision` 属于改主入口，
   等你点头再动。现在只能 `from newapi_checkin.github_provision import provision_accounts` 调用。
6. **浏览器端到端没跑过**：需要真实 GitHub 账号 + ReMail key + 出口，与 `407a491` 同一状态。
