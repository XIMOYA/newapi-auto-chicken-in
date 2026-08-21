# NewAPI 配置管理平台 · 前后端接口契约

> 本契约是 Go 后端与 Vue 前端并行开发的**唯一依据**，两端必须严格遵循。
> 所有接口均返回 JSON；错误统一格式 `{"error": "..."}`。

## 通用约定

- 基础路径：`/api`
- 鉴权方式：
  - 管理端：`Authorization: Bearer <JWT>`（登录后获得）
  - 拉取端：`Authorization: Bearer <API_KEY>`，客户端专用通道：`/api/config/raw`、
    `/api/proxies/available`、`/api/proxies/feedback`、`/api/accounts/{name}/refresh-cookie`、
    `/api/run-state/start|heartbeat|stop`（这几个只认 API Key，JWT 调返回 401）
- 错误码：401 未认证 / 403 无权限 / 400 参数错误 / 404 不存在 / 500 服务器错误
- 配置对象结构 = 完整 config JSON（含 `accounts` / `sites` / `ai` / `browser` / `http` / `defaults` / `proxy_pool` / `notify` / `config_sync` / `security` 顶层键），见 `config.example.json` 为基底

---

## 1. 健康检查

`GET /api/health` （无需鉴权）

```json
{ "ok": true, "version": "1.0.0", "time": "2026-08-12T10:00:00Z" }
```

## 2. 登录

`POST /api/login` （无需鉴权）

请求：
```json
{ "username": "admin", "password": "xxx" }
```

响应（200）：
```json
{ "token": "<JWT>", "username": "admin", "expires_in": 604800 }
```

错误（401）：`{"error": "用户名或密码错误"}`

## 3. 获取配置（管理端，敏感打码）

`GET /api/config` （JWT 或 API Key）

响应（200）：
```json
{
  "config": { "...完整配置对象，敏感字段打码..." },
  "updated_at": "2026-08-12T10:00:00Z",
  "revision": 12
}
```

打码规则：
- `accounts[].cookie`：站点登录 Cookie，非空时返回 `"***"`（值不返回，编辑时原样保留）
- `accounts[].github_user_session`：GitHub `user_session`，非空时返回 `"***"`（值不返回，编辑时原样保留）；现仅用于签发 `new_api_refresh`
- `accounts[].github_client_id`：OAuth Client ID，非敏感，正常返回
- `ai.api_key`：非空时返回 `"***"`
- `notify.email.password`：非空时返回 `"***"`
- `config_sync.token`：非空时返回 `"***"`
- `proxy_pool.remote_token`：Actions 预取令牌，等同 API Key，非空时返回 `"***"`
- `security.config_key`：配置加密密钥，非空时返回 `"***"`
- `accounts[].proxy`：**有意不打码**，明文返回。地址是运维辨识出口的必要信息，而该字段在
  前端是普通输入框；打码后无法区分「只改 host、保留认证」与「整个换新」，会把 `"***"`
  写坏可用代理。含账号密码的代理请改用 IP 白名单授权
- `proxy_pool.sources`：正常返回（非敏感）

> 前端提交时，`"***"` 会被后端识别为「未修改，保留原值」，不覆盖。
> `revision` 是乐观锁版本号，保存时必须原样带回（见 §4）。
> 账号的增删改请优先用 §4.1 的增量接口，不要整份提交（见那一节的原因说明）。

## 3.1 获取配置版本号（轻量）

`GET /api/config/revision` （JWT 或 API Key）

响应（200）：`{ "revision": 13 }`

用途：多人同时使用时，前端每几秒轮询一次，版本号变了才去拉整份配置，把别人的改动
静默换上，不必弹窗催用户刷新。整份配置有几十 KB，只为「变没变」拉它太浪费。

有意**不返回** `updated_at`：凭据轮转会更新它却不推进 `revision`（见 §凭据轮转与版本号），
一并返回很容易被误当作变更信号，结果后台轮转又触发一次无谓的全量刷新。

## 4. 保存配置（管理端）

`PUT /api/config` （JWT 或 API Key）

请求体：
```json
{ "config": { "...完整配置对象..." }, "revision": 12 }
```

并发保护（乐观锁）：
- 带 `revision` 时，只有它与库中当前版本一致才写入，写入后版本 +1
- 版本不一致返回 **409**，说明期间有别人保存过；响应体直接带上最新配置，前端可原地接管
- 不带 `revision` 时保持早期的无条件覆盖行为，仅为兼容既有外部脚本；**Web 端一定带**
- 不带 `revision` 时，`login_method=tabiai` 账号的 `cookie` **不会被覆盖**，一律保留库中现值。
  该字段由后台签到持续轮转，用陈旧请求体写回旧代次会触发站点重放检测、整条会话被撤销。
  要显式改 tabiai 凭据请带上 `revision`，或用 `issue-cookie` / `refresh-cookie` 接口。
  `newapi_cookie` 的 session 是静态凭据，不受此限制，可正常修改
  （历史上正是"整份覆盖 + 无版本校验"导致多人操作时 Cookie 被陈旧快照静默清空）

规则：
- 后端把 `"***"` 占位符还原为旧值（深合并）
- 占位符**无法还原**时返回 400 而不是把 `"***"` 字面量落库。还原按 `accounts[].name` 匹配，
  所以整份提交时改名会找不到旧值。**账号改名请走 §4.1**，那里带 `previous_name`，不必重填凭据
- 校验：`accounts` 必须有 `name` / `url`；`login_method` 只能是 `newapi_cookie` 或 `tabiai`（旧值 `github_cookie` 自动归一化为 `tabiai`），缺省按 `newapi_cookie` 处理；凭据字段均可为空（实际运行时只检查当前登录方式对应字段）；`sites` 必须有 `name` / `url`；URL 需 http(s) 开头
- `accounts[].name` 与 `sites[].name` **不可重复**（name 是敏感字段还原与导入合并的匹配键）
- 校验通过才落库

响应（200）：`{ "ok": true, "updated_at": "2026-08-12T10:00:00Z", "revision": 13 }`
错误（400）：`{ "error": "具体校验错误信息" }`
错误（409）：
```json
{
  "error": "配置已被他人修改，请重新载入最新版本后再提交",
  "revision": 15,
  "updated_at": "2026-08-12T10:05:00Z",
  "config": { "...当前最新配置，敏感字段打码..." }
}
```

> 启动时会自动清理库中遗留的 `"***"` 字面量敏感字段（早期还原缺陷的产物）并记日志；
> 受影响字段在界面上显示为「未设置」，需要重新填写 —— 它们本来就已经不是有效凭据了。

## 4.1 账号增量操作（管理端，推荐）

`POST /api/accounts/ops` （JWT 或 API Key）

请求体：
```json
{
  "ops": [
    { "type": "upsert", "account": { "name": "新名字", "url": "https://a.com", "...": "..." },
      "previous_name": "旧名字" },
    { "type": "delete", "name": "B" },
    { "type": "set_enabled", "name": "C", "enabled": false }
  ]
}
```

为什么要有它：整份 `PUT` 配上单一 `revision`，两个人同时加账号必然有一个吃 409；
就算放宽锁，后提交者的快照里没有对方刚加的账号，会把它整份覆盖抹掉。改成提交
「操作」后，服务端在**最新**配置上按账号名重放，A 加账号 X 与 B 加账号 Y 落在不同
`name` 上，互不影响。

- `type` 只能是 `upsert` / `delete` / `set_enabled`，按数组顺序重放；单请求最多 500 条
- **不接受也不需要 `revision`**：重放基准就是服务端当前值，因此不会返回 409
- `upsert` 的 `previous_name` 用于改名：服务端据此定位旧记录、就地替换（保持列表位置），
  并让 `"***"` 仍能按旧名还原 —— **改名不必重填凭据**。新名字与其他账号重复时返回 400
- 目标账号已被别人删掉时不报错，放进 `skipped` 说明并继续；`previous_name` 指向的账号
  不存在时退化为按新名新增
- 与 `PUT` 共享全部写入保护：`"***"` 还原、`ValidateConfig` 校验、
  `login_method=tabiai` 账号的轮转 `cookie` 不被陈旧请求体覆盖
- 账号变更是用户可见改动，落库后 `revision` +1，别人的轮询因此能感知到

响应（200）：
```json
{
  "ok": true,
  "config": { "...重放后的最新配置，敏感字段打码..." },
  "updated_at": "2026-08-12T10:00:00Z",
  "revision": 14,
  "skipped": ["账号 \"X\" 已不存在，跳过删除"]
}
```
错误（400）：`{ "error": "具体校验错误信息" }`（未知 `type`、缺 `name`、名字重复、URL 非法等）

> 站点预设（`sites`）与各设置页仍走 §4 的整份保存：它们没有并发热点，
> 而各页面保存前会 `deepClone` 当前已同步到最新的配置、只覆盖自己那个区块，
> 所以既不会误报 409，也不会抹掉别人改的其他区块。

## 5. 拉取配置（Actions 用，明文）

`GET /api/config/raw` （API Key）

响应（200）：**直接返回完整明文 config JSON 对象**（不是包裹结构）

```json
{ "accounts": [...], "ai": {...}, ... }
```

错误（401）：`{"error": "无效的 API Key"}`

> 说明：`remote_sync.py` 的 `_select_payload` 能自动识别「对象本身就是配置」的情况，因此直接返回配置对象最兼容。

## 6. API Key 管理（JWT）

### 列表
`GET /api/keys`

```json
{ "keys": [ { "id": 1, "name": "github-actions", "prefix": "ncf_abc123", "created_at": "...", "last_used_at": null } ] }
```
> `prefix` 为 key 前 8 位（形如 `ncf_xxxx`），用于识别；完整值不返回。

### 创建
`POST /api/keys` 请求体：`{ "name": "github-actions" }`

响应（200）：
```json
{ "id": 2, "name": "github-actions", "key": "ncf_<完整密钥，仅此一次展示>" }
```

### 删除
`DELETE /api/keys/{id}` → `{ "ok": true }`

## 7. 导出（JWT 需一次性票据 / API Key 免票据）

`GET /api/export`

**必须携带 `X-Export-Ticket` 头**，票据由 `POST /api/auth/verify-password` 签发：单次使用、
2 分钟过期、绑定签发时的用户。仅有 JWT 是不够的 —— 该接口返回全部明文凭据，
若不做绑定，拿到 token 就能跳过密码确认直接拉走。

响应（200）：
```json
{ "json": "{ 完整明文 config JSON 字符串 }" }
```
错误（403）：`{ "error": "导出需要先通过密码确认（票据缺失、已使用或已过期），请重新验证密码" }`

> 前端展示后提供「复制」按钮；离开页面即清空明文，不做自动拉取。

### 二次密码确认

`POST /api/auth/verify-password` 请求体：`{ "password": "当前管理员密码" }`

响应（200）：
```json
{ "ok": true, "ticket": "<32 字节 hex>", "expires_in": 120 }
```
错误（400）：`{ "error": "密码错误" }`（不返回票据）

## 8. 修改密码（JWT）

`PUT /api/password` 请求体：`{ "old_password": "x", "new_password": "y" }`

响应（200）：`{ "ok": true }`
错误（400）：`{ "error": "旧密码错误" }` 或 `{ "error": "新密码至少 8 个字符" }`

## 9. Cookie 可用性测试（JWT 或 API Key）

站点 Cookie 与 TaBiAI 凭据使用两个独立的**启动**接口，共用一个后台任务与一套状态/停止接口。前端只提交账号名称，后端从数据库读取明文凭据；响应不会返回 Cookie 或 token。

执行模型：
- 启动接口立即返回，检测在服务端后台按**轮次**执行；同一时刻只允许一个任务，冲突返回 409
- 账号级并发 5；每轮给所有未定论账号各尝试一次，**代理类失败留到下一轮无限重试**，**只有源站类结论才判终态**
- 出口来源：`accounts[].proxy` 优先；未配置时从服务器代理池（`proxies` 表 alive 记录）轮转取，不受 `proxy_pool.enabled` 影响；池子为空则该轮直连
- 账号自带代理连续 2 轮失败后自动降级为代理池出口，`message` 会带「账号代理连续失败，已切换代理池」
- 手动停止后，仍未定论的账号写成 `skipped`，`message` 为「已手动停止（共尝试 N 次，最后失败：…）」

失败分类（决定是否重试）：
- **代理类（重试）**：连接/超时/TLS 等传输层错误；代理自身 407/502/504；非 JSON 响应；403/429/503 且带 `cloudflare`/`cf-ray`/`cdn-cgi` 特征或挑战页文案
- **源站类（终态）**：站点用合法 JSON 明确拒绝凭据（401/403）；站点 URL 无效

### 启动：站点 Cookie

`POST /api/cookie-tests/newapi`

请求体：
```json
{ "account_names": ["可选账号名"] }
```

只检测 `login_method=newapi_cookie` 且启用的账号。`account_names` 为空数组时检测该类型的全部启用账号。普通 Cookie 请求 `/api/user/self`；包含 `new_api_refresh=` 时先执行 refresh，再用新凭据验证 self。

### 启动：TaBiAI 凭据

`POST /api/cookie-tests/tabiai`

请求体格式与站点 Cookie 相同，但只检测 `login_method=tabiai` 且启用的账号。检测只做一件事：拿 `accounts[].cookie` 里的 `new_api_refresh` 去 `POST /api/user/auth/refresh`，看还能不能换出 access token。**不执行签到，不消耗 Turnstile 配额。**

凭据为空时该账号直接判 `skipped`，`message` 为「缺少 TaBiAI 凭据（new_api_refresh），可用「签发 cookie」或从浏览器复制」。

站点错误码到结论的映射（依据 `docs/签到原理.md` 实测）：

| HTTP | `code` | 结论 | 说明 |
|---|---|---|---|
| 200 | — | 有效 | 换到了 access token |
| 401/403 | `AUTH_SESSION_REVOKED` | 失效 | 整条会话已被撤销（旧代次重放或用户在别处登出），必须重新签发 |
| 401/403 | `AUTH_UNAUTHORIZED` | 失效 | 当前代次失效：已过期，或被更新后的代次取代 |
| ≥500 / 非 JSON | — | 代理类 | 计入重试，不判终态 |

**代次轮转**：refresh 每次成功都会通过 `Set-Cookie` 下发下一代凭据。检测过程无论成功与否，只要响应里带了新代次就会立刻写回 `accounts[].cookie`——旧代次超出宽限窗口后再用会被判重放，进而撤销整条会话，所以不允许"检测完就丢掉新值"。

两个启动接口的响应（200）：
```json
{ "ok": true, "mode": "tabiai", "started": true }
```
错误（409）：`{ "error": "已有 Cookie 检测任务在进行中" }`
错误（400）：`{ "error": "没有匹配 tabiai 的启用账号" }`

「已在跑」一律是 409，包括「查完 `IsRunning` 到真正启动之间被别人抢先」那条竞态路径 ——
它以前跟参数错误一起走 400，文案还一模一样，前端认 409 才转轮询，撞上就只弹个错误、
界面停在原地。现在这条路径返回哨兵错误 `ErrCookieTestBusy`，由 handler 统一映射成 409。

### 进度与结果

`GET /api/cookie-tests/status`

响应（200）：
```json
{
  "running": true,
  "stopped": false,
  "mode": "newapi_cookie",
  "round": 3,
  "started_at": "2026-08-16T14:00:00Z",
  "checked_at": "",
  "duration_sec": 42,
  "last_error": "",
  "summary": { "total": 2, "valid": 1, "invalid": 0, "abnormal": 0, "skipped": 0 },
  "results": [
    {
      "name": "站点A",
      "url": "https://example.com",
      "state": "valid",
      "user_id": 123,
      "duration_ms": 321,
      "message": "tester",
      "attempts": 1,
      "proxy": "1.2.3.4:8080"
    }
  ]
}
```

- `mode` 为空串表示服务启动后还没跑过任何检测
- `checked_at` 仅在任务结束后有值；`round` 为当前轮次
- `attempts` 为该账号已尝试次数，`proxy` 为最后一次使用的出口（空串=直连）
- `summary` 只统计四类终态，`pending`/`running` 不计入但计入 `total`

### 停止

`POST /api/cookie-tests/stop` → `{ "ok": true }`（幂等，无任务在跑时也返回 ok）

状态含义：`valid` 有效、`invalid` 凭据失效、`abnormal` 源站层面的异常响应、`skipped` 未配置对应 Cookie 或被手动停止、`pending` 排队中、`running` 本轮检测中。

## 10. 代理池实测反馈（Actions 用）

`POST /api/proxies/feedback`（**API Key** 认证，不是 JWT；与 `/api/run-state/*`、`/api/proxies/available` 同组）

客户端跑完签到后回传本轮每个代理的成败计数，服务端据此做优选排序。

请求体：
```json
{
  "source": "github-actions",
  "items": [
    { "addr": "1.2.3.4:8080", "ok": 2, "net_fail": 1, "block_fail": 0 }
  ]
}
```

响应（200）：`{ "ok": true, "accepted": 1, "skipped": 0 }`
错误（400）：`{ "error": "请求体不是合法的 JSON" }` / `{ "error": "items 不能为空" }` / `{ "error": "items 条数超过上限 2000" }`
错误（500）：`{ "error": "写入代理反馈失败" }`

- `source` 可选，缺省或空白时服务端日志里记为 `unknown`；`items` 必填且非空，条数上限 2000
- `ok` = 经该代理签到成功次数；`net_fail` = 网络层连不上；`block_fail` = 连得上但被目标站/盾拦下
- 计数**只增不减**，服务端不接受覆盖或清零；同一轮重复上报会把计数记重，客户端保证一轮只调一次
- 被丢弃（计入 `skipped`）的条目：地址为空/超过 255 字符/含空白/缺端口/多个冒号、任一计数为负、三项计数全为 0
- 同一次请求里同一个 `addr` 出现多次，先在服务端合并再落库
- 数据存独立表 `proxy_feedback`（`addr` 主键 + 三项计数 + `last_ok_at` / `last_fail_at` / `updated_at`），
  不随代理刷新被清空；超过 **14 天**没再更新的行会在下一次刷新时被清理

### 优选排序（影响 `GET /api/proxies?alive=1` 与 `GET /api/proxies/available`）

原来是 SQL 里 `ORDER BY alive DESC, speed_bps DESC, latency_ms ASC`。现在存活优先，然后按 Actions 实测表现分四档，同档内才回落到测速和延迟：

| 档位 | 判定 |
|---|---|
| 实测能成 | 有反馈，`ok > 0` 且失败次数不超过成功次数 |
| 还没试过 | 没有反馈记录，或计数全为 0 |
| 时好时坏 | `ok > 0` 但失败次数超过成功次数；或只失败过一次（样本不足，不判死） |
| 试过全败 | `ok == 0` 且总次数 ≥ 2 |

- 同档内依次比：净成功数（`ok` 减失败）降序 → `speed_bps` 降序 → `latency_ms` 升序 → `addr` 字典序（保证顺序稳定）
- 失败次数把 `net_fail` 和 `block_fail` 一起算：对签到来说两者都得换 IP
- `limit` 现在在**精排之后**才截断。以前交给 SQL LIMIT 会先按测速砍掉一批，砍掉的可能正是实测最稳的
- 反馈表为空或读取出错时退回原来的测速/延迟顺序；展示含 dead 的全量列表（不带 `alive=1`）仍是 SQL 顺序
- 服务端 Cookie 检测取代理也走这个列表（`AvailableAddrs`），所以它也会优先拿到实测能成的出口

为什么反馈档压在测速之前：服务器测出来的快慢是它自己网络位置的快慢，而代理真正被用在 Actions runner（Azure 网络）那边，两边对同一个免费代理的可达性经常不是一回事。

### 分片发牌：`GET /api/proxies/available?shard=I/N`

Actions 每 30 个账号拆一个 job 并行跑时，几个 job 拿到同一份优选列表又都从头取，最优的那几个出口 IP 会被同时分给不同账号 —— 「一个账号一个 IP」就悄悄作废了。带上 `shard=I/N` 后服务端**按轮转**只发第 I 份：第 1 片拿第 1、4、7 名，第 2 片拿第 2、5、8 名。

- 轮转而不是切连续块：切块会让第 1 片吃掉所有最优代理、后面的片只剩次品
- 各片互不重叠，合起来正好覆盖全量
- 参数写错（`0/3`、`4/3`、`abc`、缺分母、总片数 ≤ 0）返回 **400**，不静默当作不分片 —— 客户端会误以为自己独占一批代理
- 客户端侧是 `python main.py --shard I/N`，workflow 由 matrix 分片号自动传入
- `--shard` 在客户端同时决定「跑哪些账号」：账号名不能经 job output 传递（来自 secret 解出的配置，GitHub 会判定 output 可能含 secret 而整个跳过，下游 `fromJson('')` 报 empty input），所以 matrix 只放序号，名单由各 job 按连续块自己切

### `GET /api/proxies` 的 `?source=xxx`

按来源 URL 精确过滤，空串等于不过滤。过滤发生在 `limit` 截断**之前**：`limit` 的语义是「要几条」而不是「从前几条里挑」，先截断再过滤会让某个来源明明有几百条可用、页面上只剩零星几条。

### `proxy_pool` 新增字段（客户端消费，平台只负责存取下发）

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `report_feedback` | bool | `true` | 跑完是否回传各代理成败计数（`POST /api/proxies/feedback`） |
| `preflight_check` | bool | `true` | 签到前在客户端本机快测一遍预取列表，剔掉当场连不上的 |
| `preflight_limit` | int | `60` | 自筛只测列表最前面多少条；`0` = 不自筛。Python 侧夹到 0..500 |
| `preflight_seconds` | int | `15` | 自筛整体时间盒（秒），到点就用已有结论。Python 侧夹到 1..120 |

自筛探测打的仍是 `proxy_pool.test_url`，不碰目标站点。两个语义要点：

- 时间盒内没拿到结论的代理**照旧保留**，不会因为超时被误删（把「没测到」当成「不通」会误删好代理）
- 安全阀：候选全军覆没（一个都没测通）时**放弃整个自筛结果**，也不记进反馈。那更可能是本机到
  `test_url` 的链路断了或该地址不可达，而不是代理集体暴死；真剔光会让所有账号因「无可用代理」被跳过

被剔掉的代理按 `net_fail` 记进本轮反馈，平台下次排序就知道这些出口在 runner 那边不通。

---

## 账号登录字段

每个 `accounts[]` 至少包含 `name`、`url`，并可使用以下登录字段：

```json
{
  "name": "站点A",
  "url": "https://xxx.com",
  "login_method": "newapi_cookie",
  "cookie": "",
  "github_user_session": "",
  "github_client_id": ""
}
```

- `newapi_cookie`：使用 `cookie`，沿用现有站点 Cookie + 浏览器/AI 过盾链路。
- `tabiai`：`cookie` 里存 `new_api_refresh` 的值（`sid.secret`，可带或不带 `new_api_refresh=` 前缀）。签到链路是 refresh 换 access token → 查当月状态 → 带 Turnstile token 签到；**该值会被每次 refresh 轮转覆盖**，见下方「TaBiAI 凭据轮转」。
- `github_user_session` / `github_client_id`：GitHub OAuth 已不再是登录方式，这两个字段退化为「签发 `new_api_refresh` 的原料」——`POST /api/tabiai/issue-cookie` 用它们走一次 OAuth 帮账号换出凭据。字段本身保留，老配置可原样加载。
- 旧值 `github_cookie` 由数据库迁移（config v3）自动改判为 `tabiai`，配置文件与环境变量两条入口也都会归一化，不会报错。
- 可用性检查分开执行：`python main.py --cookie-test newapi_cookie` 或 `python main.py --cookie-test tabiai`（旧写法 `--cookie-test github_cookie` 仍可用，等价于 `tabiai`）；检查命令只做 refresh，不执行签到、不消耗 Turnstile 配额。

## TaBiAI 凭据轮转（重要）

`new_api_refresh` 有**代次轮转 + 重放检测**：每次 `POST /api/user/auth/refresh` 成功都会通过 `Set-Cookie` 下发下一代 secret。旧代次超出宽限窗口后再使用，会先返回 `AUTH_UNAUTHORIZED`；被判定为重放时整条会话直接撤销（`AUTH_SESSION_REVOKED`），只能重新签发。

因此三端都必须做到「拿到新代次就立刻持久化」：

- **Go 平台**：Cookie 检测拿到 `Set-Cookie` 后立即定点写回 `accounts[].cookie`（无论本次检测判定成功还是失败）
- **Python 客户端**：refresh 成功后立刻写 `data/sessions.json`（不走节流落盘），随后尽力回写平台
- **回写端点**：`POST /api/accounts/{name}/refresh-cookie`（API Key 认证），见下节

平台与本机代次不一致时，谁先用旧代次谁就会失败。所以本机签到成功后必须让平台也拿到新值，否则网页端的凭据检测会紧接着报「已失效」。

### 回写凭据

`POST /api/accounts/{name}/refresh-cookie`（**API Key** 认证，不是 JWT）

请求体：`{ "cookie": "new_api_refresh=sid.secret" }`

响应（200）：`{ "ok": true }`
错误（400）：`{ "error": "cookie 不能为空" }` / `{ "error": "cookie 不能是占位符" }`
错误（404）：`{ "error": "账号不存在: <名字>" }`

Python 侧默认按 `config_sync.url` 同源推导该地址；也可用 `config_sync.writeback_url` 显式指定（支持 `{name}` 占位符）。回写失败不影响本次签到（本地 `sessions.json` 已存新值），但会打警告提示平台仍持有旧代次。

### 签到期间锁住网页端凭据操作

`POST /api/run-state/start`（**API Key**）请求体 `{ "source": "GitHub Actions（me/repo）" }`
`POST /api/run-state/heartbeat`（**API Key**）响应 `{ "ok": true, "running": true }`
`POST /api/run-state/stop`（**API Key**）响应 `{ "ok": true }`
`GET /api/run-state`（JWT 或 API Key）响应：
```json
{
  "running": true,
  "source": "GitHub Actions（me/repo）",
  "started_at": "2026-08-18T11:00:00Z",
  "heartbeat_at": "2026-08-18T11:04:00Z",
  "stale_after_seconds": 300,
  "heartbeat_seconds": 60,
  "holders": 3
}
```
`POST /api/run-state/unlock`（JWT 或 API Key）响应 `{ "ok": true }` —— 管理员强制解锁，
**一次清空全部持有者**。

这把锁是引用计数的（`holders`）：Actions 分片并行时每个 job 各自 start/stop，减到 0 才
真正解锁，最先跑完的分片不会把还在跑的锁掉。上一轮心跳已过期时 start 会把计数重置为 1
而不是累加，避免被强杀的进程留下的计数永远压住锁。心跳不分持有者，任一 job 续期即可。

为什么要它：网页端的 TaBiAI 凭据检测**本身就是一次真 refresh**，签到进程同时也在
推进代次。两边一撞，谁手里的代次旧了下次就会被判重放，整条会话被撤销
（`AUTH_SESSION_REVOKED`），只能重新签发。这不是「本次检测失败」，是把账号打死。

- 锁定期间 `POST /api/cookie-tests/tabiai` 与 `POST /api/tabiai/issue-cookie` 返回
  **409**，响应体为 `{ "error": "...可读说明...", "run_state": { ...同上... } }`，
  前端可直接用回传状态刷新视图
- `POST /api/cookie-tests/newapi` **不锁**：站点 Cookie 是静态凭据，不轮转也没有重放检测
- 判活看心跳而非布尔开关：Actions 有 6 小时硬上限，超时是被平台强杀，客户端没有机会
  发 `stop`。超过 `stale_after_seconds` 没心跳就自动视为已结束，否则一把只能显式关闭的
  锁迟早会永久锁死网页端
- 心跳时间解析失败时按「已结束」处理（fail-open）：宁可放开，也不要因为一条坏数据
  锁到只能改库才能恢复
- `heartbeat_seconds` 由平台下发，客户端不必硬编码间隔
- 客户端只在这一轮**含 tabiai 账号**且 `config_sync.enabled` 时才上报；全是站点 Cookie
  的一轮锁网页端毫无收益
- 强制解锁是危险出口，留着是因为心跳机制本身也可能出岔子（客户端时钟错、上报地址配错），
  没有它就只能等过期或改库。前端必须二次确认并讲清风险

### 签发凭据


`POST /api/tabiai/issue-cookie`（JWT 或 API Key）

请求体：`{ "account_name": "TaBiAI" }`

用账号里保存的 `github_user_session` 走三步 OAuth，为该账号签发一条全新的 `new_api_refresh` 并写入 `accounts[].cookie`。

响应（200）：`{ "ok": true, "account_name": "TaBiAI" }`
错误（400）：`{ "error": "该账号未填写 GitHub user_session，无法自动签发；请填写后重试，或直接从浏览器复制 new_api_refresh" }` 或 OAuth 过程中的具体错误
错误（404）：`{ "error": "账号不存在: <名字>" }`

## Turnstile 与 tabiai 配置段（仅 Python 客户端）

签到接口 `POST /api/user/checkin?turnstile=<token>` 强校验 Turnstile，且**业务失败也返回 HTTP 200**，判定必须读 body 的 `success`。实测只有用户日常在用的真实 Chrome 能出 token，脚本新启的浏览器环境（含 patchright 补过指纹的）会被拒。因此客户端通过 CDP 接管已开着的 Chrome：

```json
"tabiai": {
  "enabled": false,
  "cdp_url": "http://127.0.0.1:9222",
  "token_timeout": 120,
  "token_interval_minutes": 21,
  "keep_page": false
}
```

- `enabled`：**不是签到的硬门槛**，只决定是否先试「CDP 接管真实 Chrome」这条首选路。
  关闭（或连不上 CDP）时会退到脚本浏览器的过盾链去代取 token，见下一节。
  该段是机器级配置、不参与远程配置合并，所以 Actions 里它必然是 false，也不需要开
- `cdp_url`：先用 `chrome --remote-debugging-port=9222` 启动并保持登录状态；客户端只新开一个标签页，取完就关，不动用户其它窗口，断开连接也不会杀掉浏览器进程
- `token_interval_minutes`：站点对同一环境有频率限制（实测约 20 分钟内反复 reset 拿不到新 token），多账号必须串行 + 间隔，默认 21 分钟
- `keep_page`：排查问题时保留标签页

该段属于**机器级设置**（`cdp_url` 指向本机端口），因此和 `security` / `config_sync` 一样不参与远程配置合并，远端下发不会覆盖本地值。

### TaBiAI 也走完整 S0-S4（页内签到）

CDP 需要一个开着的真实 Chrome，而 GitHub Actions 云端没有。过去这种情况下
tabiai 账号会就地判 `turnstile_required` 失败，整轮白跑。

现在 tabiai 与站点 Cookie 一样**走完整的 S0-S4**，签到在浏览器上下文里完成：

1. 进站前把 `new_api_refresh` 注入浏览器（以前有意不注入）
2. S2/S3 过站点盾（与站点 Cookie 共用同一套编排）
3. 页内 `POST /api/user/auth/refresh` 换 Bearer token
4. 页内查本月签到状态；已签就收工，**不去取 Turnstile**（token 有 20 分钟级频率限制）
5. 用 S4 那套机制现取 token：读页面已有 token → 几何点击 → AI 看截图代为点选 →
   从站点状态接口取公开 site key 挂载官方 widget → 交互等待循环
6. 页内 `POST /api/user/checkin?turnstile=<token>`，带 `Authorization: Bearer <jwt>`

为什么一定要在页内签：Turnstile token 短时一次性，站点中间件校验时会连带看请求环境。
把 token 取出来交给 curl_cffi 发等于「A 环境生成、B 环境使用」，本来就容易被拒；
页内直发让 token 在哪生成就在哪用掉，还顺带带上刚过盾的 `cf_clearance` 与真实浏览器指纹。

几个要点：
- **页内 refresh 同样会轮转凭据**。新代次通过 `Set-Cookie` 下发，而它属于 fetch API 的
  禁止读取响应头，页内 JS 拿不到，只能从浏览器 cookie jar 里取。取到后必须立刻交回上层
  落盘 + 回写平台，**哪怕这次 refresh 判定失败也要回写** —— 漏一次，下轮用旧代就会被判
  重放、整条会话被撤销
- 没有轮转回写通道（未传 `on_rotate`）时**不走页内**，宁可少一步也不能把新代次丢在浏览器里
- `--dry-run` / `--cookie-test` 不走页内签到：那两个只验证连通性
- 失败分流：认证类失败是定论直接带出；`CF_BLOCKED` 与网络异常退回 HTTP 链路重试；
  取不到 token 时**不硬发**签到请求（站点强校验，发出去只是白扔一次机会），
  交回主循环换 IP + 重开浏览器再来
- CDP 仍是首选，配了 `tabiai.enabled` 且能连上时优先用它
- **不保证成功**：Turnstile 对脚本浏览器 + 数据中心 IP 的信誉判定很严，失败提示会建议
  给该账号配住宅代理。它的价值是把「云端必失败」变成「有机会成功」
- 汇总表里命中策略现在会显示 S4（签到确实在页内完成）；退回 HTTP 链路时仍显示过盾所在的 S2/S3

## 配置对象默认结构（后端初始化时内置）

```json
{
  "accounts": [],
  "sites": [],
  "ai": { "enabled": false, "base_url": "", "api_key": "", "model": "gpt-4o-mini", "timeout": 60, "max_retries": 2 },
  "browser": { "driver": "camoufox", "headless": "virtual", "humanize": true, "timeout": 60, "keep_artifacts_on_fail": true, "locale": "zh-CN", "window": [1280, 800], "executable_path": null },
  "http": { "impersonate": "chrome", "timeout": 20, "verify": true },
  "defaults": { "retry": 2, "interval_seconds": [3, 8] },
  "proxy_pool": { "enabled": false, "test_url": "https://api.ipify.org", "timeout": 8, "max_workers": 25, "max_proxies": 250, "ip_swap_limit": 10, "sources": [], "refresh_minutes": 30, "save_limit": 0, "auto_test": true, "remote_url": "", "remote_token": "", "remote_token_header": "Authorization", "remote_token_prefix": "Bearer", "report_feedback": true, "preflight_check": true, "preflight_limit": 60, "preflight_seconds": 15 },
  "notify": { "email": { "enabled": false, "smtp_host": "smtp.aliyun.com", "smtp_port": 465, "use_ssl": true, "username": "", "password": "", "from_addr": "", "to_addrs": [], "subject_prefix": "NewAPI 签到日报", "timeout": 20 } },
  "config_sync": { "enabled": false, "url": "", "method": "GET", "token": "", "token_header": "Authorization", "token_prefix": "Bearer", "headers": {}, "body": null, "response_field": "", "timeout": 20, "auto_before_checkin": true },
  "security": { "encryption_enabled": false, "config_key": "", "encrypted_file": "data/config.encrypted.json" }
}
```
