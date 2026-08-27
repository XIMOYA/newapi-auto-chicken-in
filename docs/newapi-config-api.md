# NewAPI 签到配置管理平台 · API 文档

> 自建配置管理平台的完整 HTTP 接口手册。本文由 `server/` 下的 Go 源码逐字段核对生成，
> 字段名、枚举值、错误文案均与实现一致（含实现与源码注释不符之处，见文末附录 C）。

| 项 | 值 |
| --- | --- |
| 服务 | newapi-config-server（单二进制，前端已嵌入） |
| Base URL | `http(s)://<你的部署地址>`，接口一律 `/api/...` 前缀 |
| 数据库 | SQLite，默认 `./data/config.db` |
| 端点总数 | 31 |
| 认证 | JWT（管理员）/ API Key（自动化）/ 二者任一（运维类） |
| 响应类型 | `application/json; charset=utf-8` |

## 目录

- [认证方式](#认证方式)
- [通用约定](#通用约定)
- [1. 基础](#1-基础)
- [2. 配置读写](#2-配置读写)
- [3. 账号管理](#3-账号管理)
- [4. Cookie 可用性检测](#4-cookie-可用性检测)
- [5. TaBiAI 凭据维护](#5-tabiai-凭据维护)
- [6. 签到运行状态锁](#6-签到运行状态锁)
- [7. 代理池](#7-代理池)
- [8. API Key 管理](#8-api-key-管理)
- [9. 密码与二次确认](#9-密码与二次确认)
- [附录 A：数据模型](#附录-a数据模型)
- [附录 B：校验错误文案](#附录-b校验错误文案)
- [附录 C：实现与注释不符的已知点](#附录-c实现与注释不符的已知点)
- [附录 D：常见任务](#附录-d常见任务)

## 认证方式

两种凭据都放在同一个请求头里：

```
Authorization: Bearer <JWT 或 API Key>
```

**JWT** 由 `POST /api/login` 用管理员账密换取，有效期 7 天（`expires_in: 604800`）。
改密码不会吊销已签发的 JWT —— 它是无状态的，旧 token 到期前仍然可用。

**API Key** 在网页端「API Key」页创建，或用 `POST /api/keys`。明文形如
`ncf_` + 32 位 hex（共 36 字符），**只在创建响应里出现一次**，库中只存 sha256 哈希。
删除后立即失效。

### 每个端点接受哪种凭据

| 范围 | 端点 | 说明 |
| --- | --- | --- |
| **双认证**<br>JWT 或 API Key | `GET /api/config`<br>`GET /api/config/revision`<br>`PUT /api/config`<br>`GET /api/export`<br>`GET /api/accounts`<br>`POST /api/accounts/ops`<br>`GET /api/accounts/{name}`<br>`POST /api/cookie-tests/newapi`<br>`POST /api/cookie-tests/tabiai`<br>`GET /api/cookie-tests/status`<br>`POST /api/cookie-tests/stop`<br>`POST /api/tabiai/issue-cookie`<br>`GET /api/run-state`<br>`POST /api/run-state/unlock`<br>`GET /api/proxies`<br>`GET /api/proxies/stats`<br>`POST /api/proxies/refresh`<br>`POST /api/proxies/speedtest` | 日常运维。脚本用一把 API Key 就能干完，不必模拟登录换 JWT |
| **仅 API Key** | `GET /api/config/raw`<br>`GET /api/accounts/{name}/raw`<br>`GET /api/proxies/available`<br>`POST /api/proxies/feedback`<br>`POST /api/accounts/{name}/refresh-cookie`<br>`POST /api/run-state/start`<br>`POST /api/run-state/heartbeat`<br>`POST /api/run-state/stop` | 客户端专用通道。JWT 调这些会 401 |
| **仅 JWT** | `POST /api/config/import`<br>`GET /api/keys`<br>`POST /api/keys`<br>`DELETE /api/keys/{id}`<br>`PUT /api/password`<br>`POST /api/auth/verify-password`<br>`GET /api/tabiai/keepalive`<br>`PUT /api/tabiai/keepalive`<br>`POST /api/tabiai/keepalive/run` | 控制平面 + 网页端专属。一把躺在 CI secrets 里的 Key 若能改密码或造新 Key，泄露就等于永久失守；保活三个接口客户端没有调它的场合 |
| **无需认证** | `GET /api/health`<br>`POST /api/login` | |

> 控制平面的边界不是绝对隔离：`PUT /api/config` 已放开给 API Key，它能整份覆盖配置，
> 因此限制 `POST /api/config/import` 主要是少一个入口，不构成实质隔离。
> 真正只有 JWT 能做的是改密码和增删 API Key。

### 鉴权失败的文案

| 中间件 | 情形 | 状态码 | 文案 |
| --- | --- | --- | --- |
| 双认证 | 没带 Bearer 头 | 401 | `未认证` |
| 双认证 | token 既非有效 JWT 也非已知 Key | 401 | `未认证：需要有效的登录令牌或 API Key` |
| 仅 JWT | 缺失或无效 | 401 | `未认证` |
| 仅 API Key | 缺失或无效 | 401 | `无效的 API Key` |

API Key 通过鉴权时会顺手更新 `last_used_at`（更新失败被忽略，不影响主流程）。

## 通用约定

错误响应统一为：

```json
{ "error": "具体原因" }
```

只有两处例外会多带字段：配置乐观锁冲突（`revision` / `updated_at` / `config`）
和签到锁拦截（`run_state`），各自在对应端点说明。

- 请求体上限 **1 MiB**。超限与非法 JSON 都归同一个 400 `请求体不是合法的 JSON`
- 未匹配的 `/api/` 路径返回 404 `接口不存在`
- 所有响应带安全头：`X-Content-Type-Options: nosniff`、`X-Frame-Options: DENY`、
  `Referrer-Policy: no-referrer`、`X-XSS-Protection: 0`
- 时间一律 UTC RFC3339（如 `2026-08-20T03:14:00Z`）
- 路径里的账号名需 percent-encode（中文名、含 `/` 的名字都要）

## 1. 基础

### GET /api/health

无需认证，无参数。用于探活。

```json
{ "ok": true, "version": "1.0.0", "time": "2026-08-20T03:14:00Z" }
```

### POST /api/login

无需认证。

| 字段 | 类型 | 必填 | 约束 |
| --- | --- | --- | --- |
| username | string | 是 | trim 后不能为空 |
| password | string | 是 | 不能为空串，无长度上限 |

**200**

```json
{ "token": "<JWT>", "username": "admin", "expires_in": 604800 }
```

| 状态码 | 文案 |
| --- | --- |
| 400 | `请求体不是合法的 JSON` / `用户名和密码不能为空` |
| 401 | `用户名或密码错误`（用户不存在与密码错误不区分） |
| 429 | `登录尝试过于频繁，请稍后再试`，附 `Retry-After` 头 |
| 500 | `服务器内部错误` |

限流按「客户端 IP + 用户名」计：1 分钟窗口内 5 次失败起进入指数退避 1/2/4/8/16 秒，
成功登录立即清空该键。**不解析 `X-Forwarded-For`**，反代后面所有请求会算作同一 IP。

## 2. 配置读写

### GET /api/config

双认证。返回打码后的完整配置 + 乐观锁版本号。

```json
{
  "config": { "accounts": [], "sites": [], "ai": {}, "...": "..." },
  "updated_at": "2026-08-20T03:14:00Z",
  "revision": 42
}
```

7 个敏感字段非空时返回 `"***"`（清单见[附录 A](#敏感字段)）。空值原样返回空串，
不会伪装成已设置。`accounts[].proxy` **有意不打码**，原因见附录 A。

**500** `服务器内部错误`

### GET /api/config/revision

双认证。只回一个整数，供轮询判断配置有没有被别人改过。

```json
{ "revision": 42 }
```

整份配置有几十 KB，只为「变没变」拉它太浪费。**有意不返回 `updated_at`**：
凭据轮转会更新它却不推进 `revision`，一并返回容易被误当成变更信号。

### PUT /api/config

双认证。整份保存。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| config | object | 是 | 完整配置对象 |
| revision | int | 否 | 乐观锁版本号，Web 端一定带 |

带 `revision` 时只有它与库中当前值一致才写入，写入后 +1。**不带则无条件覆盖**，
仅为兼容既有外部脚本；此时 `login_method=tabiai` 账号的 `cookie` 一律保留库中现值
（该字段由后台签到持续轮转，用陈旧请求体写回旧代次会触发站点重放检测、整条会话被撤销）。

**200** `{ "ok": true, "updated_at": "...", "revision": 43 }`

**409**（版本不一致，响应体直接带上最新配置，前端可原地接管）

```json
{
  "error": "配置已被他人修改，请重新载入最新版本后再提交",
  "revision": 45,
  "updated_at": "2026-08-20T03:20:00Z",
  "config": { "...当前最新，敏感字段打码..." }
}
```

**400** 占位符无法还原、或校验失败（[附录 B](#附录-b校验错误文案)）。
**500** `服务器内部错误`。

> 账号的增删改建议走 [`POST /api/accounts/ops`](#post-apiaccountsops)，不要整份提交 ——
> 那样两个人同时加账号必然有一个吃 409。

### GET /api/config/raw

**仅 API Key**。给 Actions / 客户端拉配置用。

**200** 直接返回 Config 对象本身 —— 没有包裹层、**不打码**、含全部明文凭据。

```json
{ "accounts": [ { "name": "A", "cookie": "session=真实明文", "...": "..." } ], "ai": {} }
```

**500** `服务器内部错误`

### GET /api/export

双认证。导出整份明文配置，用于备份。

- **API Key 调用免票据**，直接拿结果
- **JWT 调用必须带 `X-Export-Ticket`**：先用 `POST /api/auth/verify-password` 换一张，
  一次性、120 秒有效、绑定签发时的用户名，用后即删

**200**

```json
{ "json": "{\"accounts\":[...],\"ai\":{...}}" }
```

注意 `json` 是**字符串**，拿到后需再 parse 一次。

**403** `导出需要先通过密码确认（票据缺失、已使用或已过期），请重新验证密码`
**500** `服务器内部错误`

### POST /api/config/import

**仅 JWT**。整份导入，支持覆盖与按模块合并。

| 字段 | 类型 | 必填 | 约束 |
| --- | --- | --- | --- |
| config | object | 是 | 非空，且能反序列化成 Config |
| mode | string | 是 | `overwrite` 或 `merge`（trim + 转小写后比较） |
| modules | []string | 否 | 仅 merge 生效 |

`modules` 合法值（9 个）：`accounts` `sites` `ai` `browser` `http`
`proxy_pool` `notify` `config_sync` `security`。不含 `config_version`。

语义：未传或 `null` = 全部模块（兼容旧客户端）；`[]` = 一个模块都不导入；
overwrite 模式下 `modules` 被忽略但仍原样回显。

merge 规则：`accounts` / `sites` 按 `name` 合并（同名整条替换，新名追加）；
其余标量段落必须「被勾选**且**导入 JSON 里确实存在该 key」才整体覆盖，否则保留现值。

**200** `{ "ok": true, "mode": "merge", "modules": ["accounts"], "updated_at": "..." }`

| 状态码 | 文案 |
| --- | --- |
| 400 | `请求体不是合法的 JSON` / `config 不能为空` / `mode 必须是 overwrite 或 merge` / `config 解析失败` / `modules 含未知模块: X` |
| 400 | 占位符无法还原、或校验失败（[附录 B](#附录-b校验错误文案)） |
| 500 | `服务器内部错误` |

落库前会跑还原：值为 `"***"` 的敏感字段取库中旧值，所以导入自家导出的文件不会毁凭据。
同时走「保留轮转 cookie」通道，`login_method=tabiai` 账号的 `cookie` 一律保留库中现值。
写入会让 `revision` +1，但导入本身不参与乐观锁。

## 3. 账号管理

这一章的三个端点是配对使用的，构成「找名字 → 查数据 → 改数据」的完整链路：

| 想做什么 | 用哪个 |
| --- | --- |
| 列出有哪些账号 | `GET /api/accounts` |
| 按名字查一个账号 | `GET /api/accounts/{name}`（脱敏）/ `/raw`（明文，仅 API Key） |
| 按名字改一个账号 | `POST /api/accounts/ops` 的 `upsert` |

### GET /api/accounts

双认证。账号名清单 + 常用元数据，**不含任何凭据字段**（连打码占位符都不会出现）。

在这之前想知道「有哪些账号」只能拉整份 `GET /api/config` —— 为了几个名字搬走几十 KB
配置，脚本侧尤其别扭。

**200**

```json
{
  "accounts": [
    { "name": "Steven", "url": "https://a.com", "login_method": "tabiai",
      "enabled": true, "has_cookie": true, "proxy": null }
  ],
  "count": 1,
  "updated_at": "2026-08-24T10:00:00Z",
  "revision": 42
}
```

| 字段 | 说明 |
| --- | --- |
| has_cookie | 只回布尔，不回摘要。清单是拿来找名字的，为每个账号算一次 sha256 纯属浪费；真要核对某一条的代次就去 `GET /api/accounts/{name}` 拿指纹 |
| proxy | 账号自带的固定出口，**没配时是 `null`**（表示走代理池或直连），不是空串 |
| revision | 与 `GET /api/config/revision` 同一个值，可以直接拿去做 `PUT /api/config` 的乐观锁参数，省一次往返 |

账号顺序**与配置里的顺序一致，不排序** —— 网页端的账号列表就按这个顺序显示，
这里再排一遍会让「清单第 3 个」和「界面第 3 个」对不上。

空配置时 `accounts` 是 `[]` 而不是 `null`。

### POST /api/accounts/ops

双认证。**账号增删改的推荐入口**，提交的是「操作」而不是整份快照。

```json
{
  "ops": [
    { "type": "upsert", "account": { "name": "新名字", "url": "https://a.com" },
      "previous_name": "旧名字" },
    { "type": "delete", "name": "B" },
    { "type": "set_enabled", "name": "C", "enabled": false }
  ]
}
```

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| ops[].type | string | 是 | `upsert` / `delete` / `set_enabled` |
| ops[].account | object | upsert 必填 | 完整 Account 对象 |
| ops[].previous_name | string | 否 | 仅 upsert：改名时填原名 |
| ops[].name | string | delete / set_enabled 必填 | 目标账号名 |
| ops[].enabled | bool | set_enabled 必填 | |

为什么存在：整份 `PUT` 配上单一 `revision`，两个人同时加账号必然有一个吃 409；
就算放宽锁，后提交者的快照里没有对方刚加的账号，会把它整份覆盖抹掉。改成提交操作后，
服务端在**最新**配置上按账号名重放，作用在不同 `name` 上互不影响。

- 按数组顺序重放，单请求最多 **500** 条
- **不接受也不需要 `revision`**，因此不会返回 409
- `previous_name` 用于改名：据此定位旧记录、就地替换（保持列表位置），并让 `"***"`
  仍能按旧名还原 —— **改名不必重填凭据**
- 目标账号已被别人删掉时不报错，放进 `skipped` 继续；`previous_name` 指向的账号不存在时
  退化为按新名新增
- 与 `PUT` 共享全部写入保护：`"***"` 还原、配置校验、tabiai 轮转 cookie 不被陈旧请求体覆盖
- 落库后 `revision` +1

**200**

```json
{
  "ok": true,
  "config": { "...重放后的最新配置，敏感字段打码..." },
  "updated_at": "2026-08-20T03:14:00Z",
  "revision": 44,
  "skipped": ["账号 \"X\" 已不存在，跳过删除"]
}
```

**400** 未知 `type`、缺 `name`、`ops` 为空或超 500 条、账号名重复、URL 非法等。
**500** `服务器内部错误`。

### POST /api/accounts/{name}/refresh-cookie

**仅 API Key**。定点更新某个账号的 `new_api_refresh`，供客户端回写。

两种场合都会调它，走的是同一条落盘 + 回写链路（`runner._tabiai_rotate_callback`）：

- **代次轮转后** —— 每次 refresh 成功站点都会下发下一代 secret
- **客户端自行签发后** —— refresh 被拒时客户端用账号的 `github_user_session` 走
  GitHub OAuth 签发一条新凭据（与平台 `POST /api/tabiai/issue-cookie` 同一套三步协议，
  但出口是客户端自己的代理），签发结果必须立刻同步过来，否则平台的保活会拿着
  已作废的旧代次去撞重放检测

```json
{ "cookie": "new_api_refresh=sid.secret" }
```

**200** `{ "ok": true }`

| 状态码 | 文案 |
| --- | --- |
| 400 | `账号名不能为空` / `请求体不是合法的 JSON` / `cookie 不能为空` / `cookie 不能是占位符` |
| 404 | `账号不存在: <名字>` |
| 500 | `服务器内部错误` |

**不推进 `revision`**：凭据轮转是后台行为，每个 tabiai 账号跑一次就 +1 的话，
正在改 AI 或邮件配置的人一保存就撞 409，而那些字段跟 cookie 毫无关系。

**不受签到锁影响**：回写通道必须始终畅通，否则签到进程轮转出的新代次没法同步回来。

**客户端按响应体里的 `ok: true` 验收，不看状态码。** 自建反代时要让这个响应原样透传：
网关或鉴权层替服务端回一个 200 空壳，会让客户端误判为已同步，而库里还是旧代次。

客户端侧的三条约定（`newapi_checkin/remote_sync.py`）：

- **重试 3 次**，退避 1s / 2s。打的是自己的平台不是目标站点，重试无风控代价，
  而漏掉一次的代价是整条会话被撤销。
- **不走代理**，即使启用了 `proxy_pool`。没有伪装出口的需要，套代理只是多一个失败点。
- 三次都没确认成功时**该账号这一轮判为失败**，即使签到本身成了。因为平台会拿旧代次
  去保活/检测并撞重放，报成成功等于把这个隐患藏进一封全绿的汇总邮件里。
- 回写被验收后**再读回核实一次**（见下面两个查询端点）：库里确认不是这一代同样判负，
  核实本身没拿到结论则只记 debug 放过。

### GET /api/accounts/{name}

双认证。单个账号的脱敏配置 + cookie 核实摘要，给网页端**人工核实回写结果**用。

**200**

```json
{
  "account": { "name": "Steven", "url": "https://a.com", "login_method": "tabiai",
               "cookie": "***", "github_user_session": "***", "enabled": true },
  "cookie_digest": { "fingerprint": "9f2a1c7b4e05", "length": 96, "has_refresh": true },
  "updated_at": "2026-08-24T10:00:00Z"
}
```

`account` 走的是与 `GET /api/config` **同一套打码规则**（`MaskConfig`），所以 cookie 与
`github_user_session` 非空时一律是 `"***"`，明文一个字节都不下发浏览器。

| 摘要字段 | 含义 |
| --- | --- |
| fingerprint | cookie 明文的 sha256 十六进制**前 12 位**；给人眼比对「代次换没换」，不是防碰撞哈希 |
| length | 明文字节长度 |
| has_refresh | 是否含 `new_api_refresh=` 这个键。只有源站会下发它，指纹长度都在而键没了说明库里那条不是可用凭据 |

cookie 为空时摘要是空值形态：`{ "fingerprint": "", "length": 0, "has_refresh": false }`
（此时 `account.cookie` 也保持空串，不打成 `"***"`，否则界面会显示「已设置」）。

`updated_at` 是整份配置的更新时间 —— 库里没有按账号的时间戳。凭据轮转不推进 `revision`
但**会**更新它，所以这个值恰好能反映「最近一次回写是什么时候落库的」。

| 状态码 | 文案 |
| --- | --- |
| 400 | `账号名不能为空` |
| 404 | `账号不存在: <名字>` |
| 500 | `服务器内部错误` |

### GET /api/accounts/{name}/raw

**仅 API Key**。返回该账号的**明文** Account 对象（没有包裹层），给客户端做回写后的精确比对。

```json
{ "name": "Steven", "url": "https://a.com", "login_method": "tabiai",
  "cookie": "new_api_refresh=sid.secret", "enabled": true }
```

明文暴露面没有新增：API Key 持有者本来就能 `GET /api/config/raw` 拉走整份明文，
这里只是按账号切了一片。错误码与上面那个端点一致。

两个端点的账号定位规则与 `POST /api/accounts/{name}/refresh-cookie` **完全一致**：
路径参数 trim 后与 `accounts[].name` 精确比较，**大小写敏感**，库里的名字不 trim。
必须同一套口径 —— 否则会出现「回写找得到、核实找不到」，客户端会把成功的回写判成失败。

客户端侧（`newapi_checkin/remote_sync.py` 的 `_verify_writeback`）的三条约定：

- **确认库里不是这一代**（含被存成空串）→ 与「回写失败」同一条判负路径。平台此刻攥着
  旧代次，它的保活下次 refresh 就会撞重放、整条会话被撤销
- **核实没拿到结论**（推不出读回地址、网络错、老版本平台 404、响应形状不对）→ 只记
  一条 debug 放过。回写已被平台的 `ok` 明确确认，加固失灵不该反过来造出一批假故障
- 比对前两边都 `strip()`，并按平台的归一化规则（裸 `sid.secret` 补 `new_api_refresh=`
  前缀）兜一层；读回同样**不走代理**，超时沿用 `config_sync.timeout`

## 4. Cookie 可用性检测

检测在服务端后台跑，端点立即返回，结果靠轮询 status。两个模式共用同一个 runner 和
同一把互斥位 —— newapi 在跑时打 tabiai 也会 409。

### POST /api/cookie-tests/newapi ｜ POST /api/cookie-tests/tabiai

双认证。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| account_names | []string | 否 | 空或缺省 = 该模式下所有 `enabled` 账号；给值则只测名字命中的 |

请求体**必须是合法 JSON**，至少 `{}`（空 body 会 400）。

**200** `{ "ok": true, "mode": "tabiai", "started": true }`

| 状态码 | 文案 |
| --- | --- |
| 409 | `已有 Cookie 检测任务在进行中`（含「查完到启动之间被抢先」那条竞态） |
| 409 | 仅 tabiai：签到进行中被锁（响应体多带 `run_state`，见下） |
| 400 | `请求体不是合法的 JSON` / `没有匹配 <mode> 的启用账号` |
| 500 | `服务器内部错误` |

签到锁的 409 响应体：

```json
{
  "error": "GitHub Actions 正在签到，为避免 TaBiAI 凭据代次冲突已暂时锁定该操作。签到结束后自动解锁；若确认对方已停止，可在「Cookie 测试」页强制解锁（约 3 分钟后自动失效）",
  "run_state": { "running": true, "source": "GitHub Actions", "...": "..." }
}
```

**只有 tabiai 会被锁**。站点 Cookie 是静态凭据，不轮转也没有重放检测。

⚠ **tabiai 每次检测都是一次真实 refresh**，必然消耗一代 `new_api_refresh` 并轮转。
新值自动写回账号（不推进 `revision`）。这也是它需要被签到锁保护的原因。

运行参数：账号级并发 5，轮次间隔 2 秒，代理从代理池可用列表轮转发牌，池空则本轮直连。
链路类失败（代理 407/502/504、CF/WAF 特征、非 JSON 响应）会**无限重试**换代理再试，
直到成功、拿到源站结论、或被手动停止。

### GET /api/cookie-tests/status

双认证。无参数，**恒 200、无错误分支**。响应体就是状态对象本身，没有包裹层。

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| running | bool | 是否有任务在跑 |
| stopped | bool | 本轮是否被请求过停止（任务结束后仍保留） |
| mode | string | `newapi_cookie` / `tabiai`；从未跑过为 `""` |
| round | int | 当前或最后一次轮次，从 1 开始；未开始为 0 |
| started_at | string | 本轮开始时间；从未跑过为 `""` |
| checked_at | string | 结束时间；仍在跑或从未跑为 `""` |
| duration_sec | int | 跑动中 = 至今耗时；结束后 = 总耗时 |
| last_error | string | 任务级错误。只有检测任务内部崩溃时才有值（形如`检测任务内部异常，已中止：…`）；正常跑完恒 `""` |
| summary | object | `total` / `valid` / `invalid` / `abnormal` / `skipped` |
| results | array | 顺序与 `config.accounts` 一致，字段见下 |

`pending` 与 `running` 不计入四类但计入 `total`，所以
`total − (valid+invalid+abnormal+skipped)` = 还在跑的数量。

`results[]` 每项：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| name | string | 账号名 |
| url | string | 站点 URL |
| state | string | `pending` `running` `valid` `invalid` `abnormal` `skipped` |
| user_id | int64 / null | 站点返回的用户 ID，取不到为 null |
| duration_ms | int64 | 最近一次检测耗时；pending/running 为 0 |
| message | string | 人话结论或失败原因（网络错误类截断到 160 字符 + `…`） |
| attempts | int | 累计尝试轮数（跨轮累加） |
| proxy | string | 本次出口。池子代理为裸 `host:port`；账号自带代理打码成 `http://***@host`；`""` = 直连 |

state 语义：

- `pending` 排队中 · `running` 本轮进行中
- `valid` 凭据有效，`message` 是站点用户名（取不到则 `Cookie 有效` / `TaBiAI 凭据有效`）
- `invalid` 源站给出了鉴权失败结论
- `abnormal` 链路 / 格式 / 5xx 异常，其中链路类会自动换代理重试
- `skipped` 缺凭据、被取消、或被手动停止

轮转后的新凭据**绝不下发前端**（内部字段带 `json:"-"`）。tabiai 模式落库成功会在
`message` 追加 `（凭据已轮转，新值已自动保存）`，失败则追加
`（警告：新凭据未能保存，下次检测可能失败，请重新签发）`。

账号自带代理连续失败 2 轮后降级到代理池，此后每轮 `message` 前缀
`账号代理连续失败，已切换代理池；`。

### POST /api/cookie-tests/stop

双认证。不读请求体，无参数。

**200** `{ "ok": true }` —— 恒成功、**完全幂等**，没有任务在跑也返回 ok。

未定论的行在收尾时统一改成 `skipped`，`message` 形如
`已手动停止（共尝试 N 次，最后失败：<原因>）`。

## 5. TaBiAI 凭据维护

### POST /api/tabiai/issue-cookie

双认证。用账号里保存的 `github_user_session` 走三步 GitHub OAuth，
为该账号签发一条**全新的** `new_api_refresh`。

```json
{ "account_name": "Steven" }
```

**200** `{ "ok": true, "account_name": "Steven" }`

| 状态码 | 文案 |
| --- | --- |
| 409 | 签到进行中被锁（响应体带 `run_state`，同上） |
| 400 | `请求体不是合法的 JSON` / `account_name 不能为空` |
| 400 | `该账号未填写 GitHub user_session，无法自动签发；请填写后重试，或直接从浏览器复制 new_api_refresh` |
| 400 | OAuth 三步的具体失败原因（见下） |
| 404 | `账号不存在: <名字>` |
| 500 | `服务器内部错误` |

OAuth 失败文案按阶段分布，都是 400：

- **准备**：`站点 URL 无效: ...`、`HTTP 客户端配置失败: ...`
- **取 flow_token**：`OAuth state 网络错误: ...`、
  `OAuth state HTTP %d 非 JSON 响应（站点可能拦截了当前出口）`、`OAuth state 成功但未返回 flow_token`
- **解析 client_id**：`站点状态未返回 github_client_id，请在账号里手动填写`
- **GitHub authorize**：`GitHub 要求重新登录，user_session 已失效`、
  `GitHub authorize HTTP %d，当前出口被 GitHub 限制，稍后再试`、
  `GitHub 未返回授权重定向（HTTP %d），可能需要先在 GitHub 授权该 OAuth 应用`、`GitHub 未返回 code: <原因>`
- **回调**：`OAuth 回调失败: ...`、`OAuth 回调成功但站点未下发 new_api_refresh（HTTP %d）`

细节：`client_id` 优先取账号的 `github_client_id`，缺省则读站点 `/api/status` 的
`data.github_client_id`，**绝不使用内置默认值**（用错会授权到别人的 OAuth 应用）。
成功后新 cookie 直接写进该账号，**不推进 `revision`**。

⚠ 签发会换出全新 sid，**签到进程手里那条当场作废** —— 这就是它被签到锁拦住的原因。

### GET /api/tabiai/keepalive

**仅 JWT**（网页端专属，客户端没有调它的场合）。保活策略与每个 tabiai 账号最近一轮的结果。

```json
{
  "setting": { "enabled": true, "minutes": 90, "updated_at": "2026-08-21T10:00:00Z" },
  "accounts": [
    { "account_name": "TaBiAI-1", "last_run_at": "2026-08-21T10:00:00Z",
      "state": "valid", "message": "凭据有效", "rotated": true,
      "paused": false, "paused_at": "", "proxy_addr": "1.2.3.4:8080" }
  ],
  "last_run_at": "2026-08-21T10:00:00Z",
  "running": false,
  "skipped_by_checkin": false,
  "next_run_at": "2026-08-21T11:30:00Z"
}
```

| 字段 | 含义 |
| --- | --- |
| state | 沿用 Cookie 检测的状态词（`valid` / `invalid` / `proxy_issue` / `abnormal`）；空串表示这个账号还没被刷过 |
| rotated | 这一轮站点有没有真的下发新代次。为 false 且 state 正常属正常现象（宽限窗口内幂等），长期为 false 才该怀疑回写链路 |
| paused | 凭据失效后被暂停自动刷新。改过凭据后的第一次刷新自动恢复 |
| skipped_by_checkin | 上一轮因为签到正在运行而整轮避让。这是刻意设计，不是故障 |

### PUT /api/tabiai/keepalive

**仅 JWT**。请求体 `{ "enabled": true, "minutes": 90 }`，响应同 GET。

间隔被夹在 **15~720 分钟**（默认 90，见 `tabiaiKeepaliveMinMinutes` / `MaxMinutes`）：刷太勤只是
多消耗代次，超过半天就失去压缩暴露窗口的意义。**关闭保活用 `enabled: false` 表达，不要传
`minutes: 0`** —— 0 和负数一律被当成「没填」按默认值 90 处理。

### POST /api/tabiai/keepalive/run

**仅 JWT**。立刻同步跑一轮。

**200** `{ "ok_count": 3, "paused_count": 1, "failed_count": 0, "status": { ... } }`
（`status` 与 GET 的响应体同构）

| 状态码 | 文案 |
| --- | --- |
| 409 | 签到进行中被锁（响应体带 `run_state`，同其他被锁操作） |
| 500 | `服务器内部错误` |

保活的设计背景、暂停/恢复规则与代理策略见根目录 `README.md` 的「凭据保活与额度报告」一节。

## 6. 签到运行状态锁

签到进程跑起来时在平台上占一把锁，网页端据此禁止动 TaBiAI 凭据。

为什么需要：网页端的 TaBiAI 凭据检测本身就是一次真 refresh，签到进程同时也在推进代次。
两边一撞，谁手里的代次旧了下次就会被判重放、整条会话被撤销（`AUTH_SESSION_REVOKED`），
只能重新签发。这不是「本次检测失败」，是把账号打死。

### POST /api/run-state/start

**仅 API Key**。客户端上报开跑。

```json
{ "source": "GitHub Actions（me/repo）" }
```

`source` 可省略（只用于界面展示「谁在跑」）。

**200** `{ "ok": true, "run_state": { ... } }`

这把锁是**引用计数**的：每次 start 让 `holders` 加一。GitHub Actions 每 30 个账号拆一个
job 并行跑时，几个 job 各自 start，谁先跑完都不会把还在跑的锁掉。

上一轮的记录心跳已过期时，start 会把 `holders` **重置为 1** 而不是继续累加 —— 心跳过期
说明那些进程已经不在了，累加会让泄漏的计数永远压住锁，只能改库才能恢复。

### POST /api/run-state/heartbeat

**仅 API Key**。续期。

**200** `{ "ok": true, "running": true }`

`running: false` **不是错误**，说明管理员强制解锁了 —— 此刻网页端可能正在动同一条凭据，
客户端应当把这件事记进日志。

心跳不分持有者：任何一个 job 发的心跳都会刷新同一条记录，这也意味着只要还有一个分片
活着，锁就不会因为超时被判死。

### POST /api/run-state/stop

**仅 API Key**。交还一个持有者。**200** `{ "ok": true }`，幂等，重复调用无害。

`holders` 减到 0 才真正解锁。分片并行时最先跑完的 job 只是把计数减一，锁继续留着 ——
提前放开的话网页端会以为签到结束了，跑去动 TaBiAI 凭据，和还在跑的 job 撞代次。

多余的 stop（客户端重试、收尾走了两遍）不会把计数压成负数，也不影响下一轮重新上锁。

### GET /api/run-state

双认证。

```json
{
  "running": true,
  "source": "GitHub Actions（me/repo）",
  "started_at": "2026-08-20T03:00:00Z",
  "heartbeat_at": "2026-08-20T03:04:00Z",
  "stale_after_seconds": 300,
  "heartbeat_seconds": 60,
  "holders": 3
}
```

`running` 是服务端算好的判活结论（有记录且心跳未过期），**客户端直接用，不要自己拿时间判**。
空闲时 `running` 为 false，其余字符串字段为空、`holders` 为 0。

`holders` 是当前持有这把锁的进程数，分片并行时大于 1。网页端会在提示里带上它，
免得用户以为解锁按钮没生效。

**`source` 为 `tabiai-keepalive` 表示服务端的凭据保活正在跑**（不是签到客户端）。
签到客户端开跑前查这个端点，只在识别到这个 source 时等待（上限 300 秒，超时带告警
继续）。判定必须按 source 而不是 `running`：分片并行时同伴 job 也持锁，按 `running`
判会让分片互等死锁。这个字面量在 Go、Python、Web 三端各有一份常量，改要一起改。

单行表只记一个 `source`。客户端超时放行后再 start 会让 `holders` 变 2（互斥仍然正确），
但 `source` 被覆盖成客户端的名字，此时从这里看不出保活也在跑 —— 只影响观测。

### POST /api/run-state/unlock

双认证。管理员强制解锁。**200** `{ "ok": true }`

**一次清空全部持有者**，不管 `holders` 是几 —— 这是「我确认它们都停了」的兜底，
要是也只减一，分片并行时得点 N 次才解得开。

留这个出口是因为心跳机制本身也可能出岔子（客户端时钟错、上报地址配错），
没有它就只能等过期或改库。前端会做二次确认并讲明风险。

### 判活为什么看心跳而不是布尔开关

Actions 有 6 小时硬上限，超时是被平台强杀，客户端没有机会发 `stop`；网络抖动、
进程崩溃同理。一把只会被显式关闭的锁迟早会永久锁死网页端。

- 超过 `stale_after_seconds`（300 秒）没心跳，自动视为已结束
- 心跳时间解析失败时按「已结束」处理（fail-open）：宁可放开，也不要因为一条坏数据
  锁到只能改库才能恢复
- `heartbeat_seconds`（60 秒）由平台下发，客户端不必硬编码
- 客户端只在这一轮**含 tabiai 账号**且启用了配置同步时才上报；全是站点 Cookie 的一轮
  锁网页端毫无收益

## 7. 代理池

### GET /api/proxies

双认证。

| 查询参数 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| alive | string | — | 值为 `1` 时只返回可用；其他值或缺省 = 全部 |
| limit | int | 500 | `0` = 不限制；负数或非数字被忽略、保持 500 |
| source | string | — | 按来源 URL 精确匹配过滤；空串等于不过滤 |

**200** `{ "proxies": [ ... ], "total": 123 }`

⚠ `total` 是**本次返回条数**，不是库里总数。

带 `source` 时过滤发生在截断**之前**：`limit` 的语义是「要几条」，不是「从前几条里挑」。
先截断再过滤会让某个来源明明有几百条可用、页面上只剩零星几条。

`proxies[]` 每项：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| id | int64 | 主键 |
| source | string | 来源 URL |
| addr | string | `host:port` |
| latency_ms | int | 连通延迟，0 = 未测出 |
| alive | bool | 是否可用 |
| last_checked_at | string | 最近一次检测时间 |
| last_alive_at | string | 最近一次可用时间（dead 行会省略该字段） |
| speed_bps | int64 | 实测下载字节每秒，0 = 未测速 |

排序看有没有带 `alive=1`：

- 带 `alive=1` 走[优选排序](#优选排序实测表现分四档) —— 存活优先，再按 Actions 实测表现分四档，
  同档内才回落到测速与延迟。此时 `limit` 在**精排之后**才截断
- 不带 `alive=1`（展示含 dead 的全量）保持 SQL 顺序 `alive DESC, speed_bps DESC, latency_ms ASC`，
  前端拿到后自己还会再排一次

**500** `查询代理列表失败`。

### GET /api/proxies/available

**仅 API Key**。给客户端取可用出口。

| 查询参数 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| limit | int | 0 | 仅 > 0 生效；0 = 有多少返回多少 |
| shard | string | — | `I/N` 形式，只领第 I 份（共 N 份）；写错返回 400 |

**200** `{ "proxies": ["1.2.3.4:8080"], "count": 1, "checked_at": "..." }`

只含 `alive` 的条目，顺序就是[优选排序](#优选排序实测表现分四档)（与 `GET /api/proxies?alive=1` 同一套），
客户端从前往后取即可；`limit` 在**精排之后**才截断。`checked_at` 是最近一次刷新或测速的完成时间，
从未跑过为 `""`。无错误分支：查库失败时返回 `{"proxies": null, "count": 0}`。

#### shard：给分片并行的 job 各发一份

Actions 每 30 个账号拆一个 job 并行跑时，几个 job 拿到的是同一份按优选排好的列表、又都
从头取 —— 最优的那几个出口 IP 会被同时分给不同账号，等于把「一个账号一个 IP」悄悄作废。
带上 `?shard=I/N` 后服务端**按轮转**发牌：第 1 片拿第 1、4、7 名，第 2 片拿第 2、5、8 名。

轮转而不是切连续块，是为了让每片都能均匀分到各档质量；切块会让第 1 片吃掉所有最优代理、
后面的片只剩次品。各片之间完全不重叠，合起来正好覆盖全量。

参数写错（`0/3`、`4/3`、`abc`、缺分母、总片数 ≤ 0）一律返回 **400**，不静默当作不分片 ——
客户端会以为自己领到的是独占的一批，实际上和别的 job 撞了，这种错必须在第一次调用就暴露。
客户端侧对应 `python main.py --shard I/N`，workflow 里由 matrix 的分片号自动传入。

`--shard` 在客户端管两件事：挑出本片该跑的账号，以及只领本片的代理。账号名单**不经
job output 传递** —— 它来自 secret 解出的配置，GitHub 判定 output「可能含 secret」时会
整个跳过该输出，下游 `fromJson('')` 直接报 empty input。所以 matrix 里只放分片序号，
名单由每个 job 按同一规则自己切（连续块，各片不重叠且合起来覆盖全部）。

### GET /api/proxies/stats

双认证。无参数。

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| total | int | 库中总条数（含 dead） |
| alive | int | 可用条数 |
| by_source | map[string]int | 按来源统计条数（含 dead） |
| last_run | string | 最近一次刷新/测速完成时间；从未跑过为 `""` |
| last_error | string | 最近一次失败信息（成功刷新会清空） |
| running | bool | 刷新或测速是否在进行（两者共用一把互斥位） |
| progress | object | 见下 |

`progress`：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| running | bool | |
| stage | string | `fetching` `testing` `speedtest` `done`；从未跑过为 `""` |
| fetched | int | 去重后候选总数 |
| candidates | int | 刷新时 = fetched；测速时 = 待测条数 |
| tested | int | 已测条数 |
| alive | int | 刷新时 = 当前可用数；**测速阶段被复用为「写库成功条数」** |
| target | int | 刷新时 = `save_limit`（<=0 时为 0）；测速时 = 待测条数 |
| started_at | string | |
| duration_sec | int | 只在任务结束时写入 |

**500** `统计代理失败`

### POST /api/proxies/refresh

双认证。**不解析请求体**，可以不带 body。

**200** `{ "ok": true, "message": "代理池刷新已开始" }`

| 状态码 | 文案 |
| --- | --- |
| 409 | `代理池刷新或测速已在进行中` |
| 500 | `服务器内部错误` |

立即返回，刷新在后台跑：抓源 → 按源轮转去重 → **并入库里存活的老代理** → 并发测通 → 排序 →
沿用上一轮测速值 → 清表重写。
并发数取 `proxy_pool.max_workers`（<=0 则 25）；`sources` 为空时用 6 个内置默认源；
`save_limit > 0` 时可用数达标即提前收手并截断，`<=0` 表示测完全部候选、全量落库。
绝对上限 20 分钟。进度只能通过 `GET /api/proxies/stats` 轮询。

**老代理会被复测而不是丢弃**：免费源的列表天天变，一个昨天还好用的出口今天可能压根不在源里。
只测新抓的会让这种代理凭空消失——即使它还通着。所以刷新前先把库里 `alive=1` 的地址捞回来，
排在候选最前面和新抓的一起过测通：能通就留下（与新测通的合并去重），不通自然淘汰。

放最前面是因为 `save_limit > 0` 时会「达标提前停」——先给已验证过的机会，能更快凑够数，
也让稳定的出口优先留下；它们的存活率本来就高于刚从源里抓来的。已经判死（`alive=0`）的不参与，
免得白占测通配额。

三件容易被忽略的收尾动作：

- **测速值跨刷新保留**：刷新自己只测连通性和延迟，落库前会把库里 `speed_bps > 0` 的旧值按 `addr`
  捞回来填进新记录，所以页面上手动测速的成果不会被下一次刷新清零。只给这轮仍然测通的代理沿用 ——
  都没测通还留着旧速度，只会让死代理在排序里插到前面
- **顺手清理过期反馈**：删掉 `proxy_feedback` 里超过 **14 天**没再更新的行。反馈存在独立表，
  不随 `proxies` 清表被抹掉，所以需要单独有个过期出口，免得随免费代理无限膨胀

### POST /api/proxies/speedtest

双认证。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| proxies | []string | 否 | `host:port` 列表；空或缺省 = 对全部可用代理测速 |

请求体**必须是合法 JSON**（空 body 会 400），字段本身可省略。

**200** `{ "ok": true, "tested": 3, "url": "https://speed.cloudflare.com/__down?bytes=1048576" }`

⚠ `tested` 是**请求体里给的条数**，不是实际测了多少（空数组时为 0）。

| 状态码 | 文案 |
| --- | --- |
| 400 | `请求体不是合法的 JSON`（**先于 409 判断**） |
| 409 | `代理池刷新或测速已在进行中` |
| 500 | `服务器内部错误` |

后台跑，独立 120 秒 context（不随 HTTP 请求取消）。单条超时取 `proxy_pool.timeout`
（<=0 则 15 秒），并发 8，只对库中 `alive` 且落在前 2000 条内的代理测。
结果写入 `speed_bps`，刷新时会被沿用而不是清零（见上）。与刷新共用互斥位。

### POST /api/proxies/feedback

**仅 API Key**。GitHub Actions 客户端跑完签到后回传本轮每个代理的成败计数，
服务端据此做[优选排序](#优选排序实测表现分四档)。客户端是否回传由 `proxy_pool.report_feedback` 决定。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| source | string | 否 | 谁在上报，只进服务端日志；缺省或全空白记为 `unknown` |
| items | array | 是 | 非空，最多 **2000** 条（`maxFeedbackItems`） |
| items[].addr | string | 是 | `host:port` |
| items[].ok | int | 否 | 经该代理签到成功的次数 |
| items[].net_fail | int | 否 | 网络层连不上（代理本身不通） |
| items[].block_fail | int | 否 | 连得上但被目标站 / 盾拦下（出口 IP 声誉问题） |

```json
{
  "source": "github-actions",
  "items": [
    {"addr": "1.2.3.4:8080", "ok": 2, "net_fail": 1, "block_fail": 0}
  ]
}
```

**200** `{ "ok": true, "accepted": 1, "skipped": 0 }` —— `accepted` 收下几条，`skipped` 丢了几条。

| 状态码 | 文案 |
| --- | --- |
| 400 | `请求体不是合法的 JSON` / `items 不能为空` / `items 条数超过上限 2000` |
| 500 | `写入代理反馈失败` |

语义要点（这里最容易搞错）：

- **计数只增不减**，服务端不接受覆盖也不接受清零。同一轮重复上报会把计数记重，
  客户端必须保证一轮只调一次；想重置只能等 14 天过期被清掉
- 被丢弃（计入 `skipped`）的条目：地址为空 / 超过 255 字符 / 含空白 / 缺端口 / 有多个冒号、
  任一计数为负、三项计数全为 0
- 同一次请求里同一个 `addr` 出现多次，先在服务端合并再落库，`accepted` 数的是合并后的条数
- 地址只做形状校验，不要求已经存在于 `proxies` 表：上游源里出现过域名形式的代理，
  客户端能用就该能记，排序时对不上号的行自然不影响任何结果
- 数据存独立表 `proxy_feedback`（字段见[附录 A](#proxyfeedback)），**不随代理刷新被清空**；
  超过 **14 天**没再更新的行会在下一次刷新时被清理

### 优选排序：实测表现分四档

`GET /api/proxies?alive=1` 与 `GET /api/proxies/available` 共用这套顺序：
存活优先 → Actions 实测表现分档 → 净成功数 → 测速 → 延迟 → `addr`。

档位（`server/proxy_feedback.go` 的 `rankOf`，越靠前越先被分配给账号）：

| 档位 | 判定 |
| --- | --- |
| 实测能成 | 有反馈，`ok > 0` 且失败次数不超过成功次数 |
| 还没试过 | 没有反馈记录，或计数全为 0 |
| 时好时坏 | `ok > 0` 但失败次数超过成功次数；或只失败过一次（样本不足，不判死） |
| 试过全败 | `ok == 0` 且总次数 ≥ 2 |

同档内依次比：净成功数（`ok` 减失败）降序 → `speed_bps` 降序 → `latency_ms` 升序 →
`addr` 字典序。最后一级只为顺序稳定 —— 每次请求返回的顺序抖来抖去，多账号并发分配会踩到
不同代理，出了问题也难复现。

失败次数把 `net_fail` 与 `block_fail` 一起算：对签到来说连不上和被拦都得换 IP。
分档而不是直接按失败率排，是因为样本量差得太远 —— 只失败过一次和失败二十次的失败率都是
100%，前者很可能只是当时网络抖了一下。

反馈档位压在测速和延迟之前：服务器测出来的快慢是它自己网络位置的快慢，而代理真正被用在
Actions runner（Azure 网络）那边，两边对同一个免费代理的可达性经常不是一回事。

两个边界：`limit` 在**精排之后**才截断（交给 SQL `LIMIT` 会先按测速砍掉一批，砍掉的可能
正是实测最稳的那些）；反馈表还是空的、或读取反馈出错时退回原来的测速 / 延迟顺序，不让整个
列表跟着失败。

## 8. API Key 管理

这三个端点**只认 JWT**。让一把 Key 能造出新 Key 等于永久提权，
CI secrets 一旦泄露就再也收不回控制权。

### GET /api/keys

**200**

```json
{ "keys": [ { "id": 1, "name": "actions", "prefix": "ncf_ab12",
              "created_at": "...", "last_used_at": null } ] }
```

按 id 升序，无 Key 时返回 `{"keys":[]}`。`last_used_at` 从未使用为 `null`。
不返回哈希，也不返回明文。**500** `服务器内部错误`。

### POST /api/keys

```json
{ "name": "actions" }
```

`name` trim 后非空，无长度上限，允许重名。

**200** `{ "id": 2, "name": "actions", "key": "ncf_<32 位 hex>" }`

⚠ **`key` 只在这一次响应里出现**，库中只存 sha256 哈希，之后任何接口都拿不回明文。

**400** `请求体不是合法的 JSON` / `name 不能为空`。**500** `服务器内部错误`。

### DELETE /api/keys/{id}

路径参数 `id` 必须是正整数。

**200** `{ "ok": true }`

| 状态码 | 文案 |
| --- | --- |
| 400 | `id 必须是正整数` |
| 404 | `API Key 不存在` |
| 500 | `服务器内部错误` |

**不幂等**：重复删同一个 id，第二次得 404。删除后该 Key 立刻失效。

## 9. 密码与二次确认

这两个端点**只认 JWT** —— 那是账号身份本身。

### PUT /api/password

```json
{ "old_password": "旧密码", "new_password": "新密码至少8位" }
```

**200** `{ "ok": true }`

| 状态码 | 文案 |
| --- | --- |
| 400 | `请求体不是合法的 JSON` / `旧密码错误` / `新密码至少 8 个字符` |
| 500 | `服务器内部错误` |

校验顺序是**先验旧密码、后校验新密码长度**。长度按**字节**计，中文更容易达标。
改密码不会吊销已签发的 JWT。

### POST /api/auth/verify-password

```json
{ "password": "当前密码" }
```

**200** `{ "ok": true, "ticket": "<64 位 hex>", "expires_in": 120 }`

票据一次性、120 秒有效、**绑定签发时的用户名**，供 `GET /api/export` 用
`X-Export-Ticket` 头消费。消费即删，同一张不能导出两次。

**400** `请求体不是合法的 JSON` / `密码错误`。**500** `服务器内部错误`。

## 附录 A：数据模型

### Account

| 字段 | 类型 | 必填 | 约束 / 说明 | GET /api/config 打码 |
| --- | --- | --- | --- | --- |
| name | string | 是 | trim 后非空，**全局唯一**（凭据还原与合并的匹配键） | 否 |
| url | string | 是 | 必须 `http://` 或 `https://` 开头 | 否 |
| login_method | string | 否 | `newapi_cookie` / `tabiai`；空串规范化为前者；旧值 `github_cookie` 启动时改判为 `tabiai` | 否 |
| cookie | string | 否 | newapi_cookie = 完整 Cookie 头；tabiai = `new_api_refresh` 值（裸 `sid.secret` 或完整形式都接受） | **是** |
| github_user_session | string | 否 | 仅供签发 TaBiAI cookie 走 OAuth 用，不再是登录凭据 | **是** |
| github_client_id | string | 否 | 站点 OAuth 应用 ID，缺省时从站点 `/api/status` 读 | 否 |
| user_id | int64 / null | 否 | >0 时作为 `New-Api-User` 头发送 | 否 |
| proxy | string / null | 否 | 账号专属代理，优先级高于代理池 | **有意不打码** |
| checkin_path | string / null | 否 | 覆盖站点默认签到路径 | 否 |
| browser_path | string / null | 否 | 覆盖浏览器路径 | 否 |
| enabled | bool | 否 | false 的账号不参与检测 | 否 |

`proxy` 不打码的原因：前端是普通输入框而非打码输入框，打成 `http://***@host` 后
用户改 host 再提交就无法判断该还原谁，会把字面量 `***` 写进代理配置。
代价是带认证的代理会明文回传给已登录管理员 —— 含账号密码的代理建议改用 IP 白名单授权。

### 敏感字段

`MaskConfig` / `UnmaskConfig` 共用同一套清单，7 个：

1. `accounts[].cookie`
2. `accounts[].github_user_session`
3. `ai.api_key`
4. `notify.email.password`
5. `config_sync.token`
6. `proxy_pool.remote_token`
7. `security.config_key`

规则：

- 打码只发生在 `GET /api/config` 与 `GET /api/accounts/{name}`，**非空才替换为 `"***"`**，空值原样保留空串
- 提交时值为 `"***"` = 「未修改，保留库中旧值」
- accounts 的两个字段按**账号名**匹配还原（不按下标），改名后找不到旧值直接 400，
  不会把字面量 `***` 落库 —— 所以改名请走 `POST /api/accounts/ops` 带 `previous_name`
- 启动时会把库里遗留的 `"***"` 字面量清空并记日志
- `GET /api/config/raw`、`GET /api/accounts/{name}/raw` 与 `GET /api/export` **不打码**

### Config 顶层段落

| 字段 | 类型 | 职责 |
| --- | --- | --- |
| config_version | int | 迁移用版本号，当前 3。**不属于可导入模块** |
| accounts | []Account | 签到账号列表 |
| sites | []Site | 站点预设（`name` / `url` / `checkin_path` / `browser_path`），新增账号时快速带出路径 |
| ai | object | `enabled` `base_url` `api_key` `model` `timeout` `max_retries` |
| browser | object | `driver` `headless` `humanize` `timeout` `keep_artifacts_on_fail` `locale` `window` `executable_path` |
| http | object | `impersonate` `timeout` `verify`（检测请求也复用） |
| proxy_pool | object | 代理池抓取/测通/刷新参数 + Actions 预取的 `remote_*` 透传字段 + 客户端侧的回传与自筛开关 |
| notify | object | 目前只有 `email` 一个子段（SMTP 全套） |
| config_sync | object | 从远端拉配置：`url` `method` `token` `token_header` `headers` `body` `response_field` 等 |
| security | object | `encryption_enabled` `config_key` `encrypted_file` |

⚠ **平台不托管 `tabiai` 段**。`config.example.json` 里的 `"tabiai"`
（`enabled` / `cdp_url` / `token_timeout` / `token_interval_minutes` / `keep_page`）
是 Python 客户端的本机配置，Go 的 Config 结构体没有对应成员，
通过 `PUT /api/config` 或导入提交会被静默丢弃。它是机器级设置（`cdp_url` 指向本机端口），
本来就不该由平台下发。

### ProxyFeedback

`proxy_feedback` 表一行 = 一个代理在 Actions 侧的累计表现，由
[`POST /api/proxies/feedback`](#post-apiproxiesfeedback) 累加写入，
供[优选排序](#优选排序实测表现分四档)分档。它不出现在任何配置里，也不随 `GET /api/config` 下发。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| addr | string | `host:port`，**主键** |
| ok | int | 经它签到成功的累计次数 |
| net_fail | int | 网络层连不上的累计次数 |
| block_fail | int | 连得上但被目标站 / 盾拦下的累计次数 |
| last_ok_at | string | 最近一次成功时间；从没成过为空 |
| last_fail_at | string | 最近一次失败时间；从没失败过为空 |
| updated_at | string | 最近一次被上报的时间，过期清理按它算 |

- 三项计数只累加，没有覆盖或清零的入口
- `last_ok_at` / `last_fail_at` 只在对应方向真有增量时才更新，一次纯失败的上报不会把
  「上次成功是什么时候」冲掉
- 单独一张表而不是给 `proxies` 加列：`proxies` 每次刷新都被清表重插，计数放进去会跟着抹掉；
  而且一个代理从上游列表消失几天后又回来，它过去的表现仍然有参考价值
- 唯一的过期出口是刷新时清掉 `updated_at` 超过 14 天的行

### proxy_pool 的客户端侧开关

这四个字段存在平台上、经 `GET /api/config/raw` 下发，但**只有 Python 客户端消费**，
Go 侧除了存取之外不读它们 —— 在网页端改完不会影响服务器自己的刷新和测速行为。

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| report_feedback | bool | `true` | 跑完是否回传各代理成败计数（`POST /api/proxies/feedback`） |
| preflight_check | bool | `true` | 签到前在客户端本机快测一遍预取列表，剔掉当场连不上的 |
| preflight_limit | int | `60` | 自筛只测列表最前面多少条；`0` = 不自筛。Python 侧夹到 0..500 |
| preflight_seconds | int | `15` | 自筛整体时间盒（秒），到点就用已有结论。Python 侧夹到 1..120 |
| max_accounts_per_ip | int | `4` | 同一出口 IP 最多给几个账号用，`<=0` 不限。客户端按 `ceil(账号数 / 它) + 50` 折算预取量。Python 侧夹到 0..64 |
| speed_test_url | string | Cloudflare `__down?bytes=1048576` | 测**下载速度**用的端点，和 test_url（只测通不通）是两回事。留空回落默认。吞吐按实际下载字节数算，换地址不用同步改「预期大小」 |

自筛探测打的仍是 `proxy_pool.test_url`，不碰目标站点。两个语义要点：

- 时间盒内没拿到结论的代理**照旧保留**，不会因为超时被误删 —— 把「没测到」当成「不通」会误删好代理
- 安全阀：候选**全军覆没**（一个都没测通）时放弃整个自筛结果，也不记进反馈。那更可能是本机到
  `test_url` 的链路断了或该地址本身不可达，而不是代理集体暴死；真剔光会让所有账号因
  「无可用代理」被跳过，一整轮白跑

被剔掉的代理按 `net_fail` 记进本轮反馈，所以平台下次排序就知道这些出口在 runner 那边不通。

## 附录 B：校验错误文案

`PUT /api/config` 与 `POST /api/config/import` 共用，全部返回 400：

- `config 不能为空`
- `accounts[%d].name 不能为空`
- `accounts[%d].url 不能为空`
- `accounts[%d].url 必须以 http:// 或 https:// 开头`
- `accounts[%d].login_method 只能是 newapi_cookie 或 tabiai`
- `accounts[%d].name 与 accounts[%d] 重复：%q（账号名用于匹配已保存的 Cookie，必须唯一）`
- `sites[%d].name 不能为空`
- `sites[%d].url 不能为空`
- `sites[%d].url 必须以 http:// 或 https:// 开头`
- `sites[%d].name 与 sites[%d] 重复：%q（站点名用于导入合并，必须唯一）`

占位符无法还原时的文案：

- `accounts[%d].cookie 无法还原：旧配置中没有名为 "X" 的账号（账号改名后需要重新填写站点 Cookie）`
- `accounts[%d].github_user_session 无法还原：旧配置中没有名为 "X" 的账号（账号改名后需要重新填写 GitHub Cookie）`

## 附录 C：实现与注释不符的已知点

阅读源码时容易被注释带偏的地方，本文按**实际实现**记录：

1. Config 没有 `tabiai` 段（见附录 A 末尾）。那是客户端本机专属配置 ——
   `cdp_url` 默认指向 `127.0.0.1:9222`，用途是接管你自己开着的 Chrome 去取 Turnstile
   token，Actions runner 上没有这个东西。它在客户端的 `_LOCAL_CONTROLLED_KEYS` 里，
   平台永不下发。所以平台不保存它是有意为之，不是漏字段。
   唯一要注意的是：网页端导出的配置不含这段，本机用户拿它覆盖 `config.json` 时，
   `tabiai.enabled` 会回落到 `false`


## 附录 D：常见任务

以下示例把地址和 Key 抽成变量：

```bash
BASE="https://你的部署地址"
KEY="ncf_你的APIKey"
AUTH="Authorization: Bearer $KEY"
```

**列出所有账号名**（不带任何凭据，可以放心贴日志）

```bash
curl "$BASE/api/accounts" -H "$AUTH"
# 只要名字
curl -s "$BASE/api/accounts" -H "$AUTH" | jq -r '.accounts[].name'
# 只看 tabiai 且启用的
curl -s "$BASE/api/accounts" -H "$AUTH" \
  | jq -r '.accounts[] | select(.login_method=="tabiai" and .enabled) | .name'
```

**按名字改一个账号**（改 URL、代理、启停等；改凭据用上面那个专门端点）

先拉当前值，改完把**完整对象**放进一条 `upsert`。打码字段（`cookie` /
`github_user_session`）保持 `"***"` 原样回传即可 —— 服务端按账号名找回真值，
不必重填凭据：

```bash
ACCT=$(curl -s "$BASE/api/accounts/Steven" -H "$AUTH" | jq '.account')
NEW=$(echo "$ACCT" | jq '.proxy="http://1.2.3.4:8080"')
curl -X POST "$BASE/api/accounts/ops" -H "$AUTH" -H "Content-Type: application/json" \
  -d "$(jq -n --argjson a "$NEW" '{ops:[{type:"upsert",account:$a}]}')"
```

改名要额外带 `previous_name`，否则会被当成新增一个账号：

```bash
curl -X POST "$BASE/api/accounts/ops" -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"ops":[{"type":"upsert","previous_name":"Steven",
               "account":{"name":"Steven-新","url":"https://a.com",
                          "login_method":"tabiai","cookie":"***","enabled":true}}]}'
```

只想启停某个账号时不必传整条，用 `set_enabled` 更省事：

```bash
curl -X POST "$BASE/api/accounts/ops" -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"ops":[{"type":"set_enabled","name":"Steven","enabled":false}]}'
```

**填 / 换某个账号的 TaBiAI 凭据**（最常用）
```bash
curl -X POST "$BASE/api/accounts/Steven/refresh-cookie" \
  -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"cookie":"new_api_refresh=sid.secret"}'
```

**核实凭据真的落库了**（回写后紧跟一步；`ok: true` 只是服务端自述）

```bash
# 明文精确比对（API Key）
curl "$BASE/api/accounts/Steven/raw" -H "$AUTH"
# 只想看摘要、不想让明文出现在终端历史里（JWT 或 API Key）
curl "$BASE/api/accounts/Steven" -H "$AUTH"
```

**加一个账号**

```bash
curl -X POST "$BASE/api/accounts/ops" \
  -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"ops":[{"type":"upsert","account":{
        "name":"新站点","url":"https://x.com",
        "login_method":"newapi_cookie","cookie":"session=abc","enabled":true}}]}'
```

**给账号改名（不必重填凭据）**

```bash
curl -X POST "$BASE/api/accounts/ops" \
  -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"ops":[{"type":"upsert","previous_name":"旧名",
        "account":{"name":"新名","url":"https://x.com",
                   "login_method":"tabiai","cookie":"***","enabled":true}}]}'
```

`cookie` 传 `"***"` 表示沿用已保存的值，配合 `previous_name` 就能安全改名。

**批量停用账号**

```bash
curl -X POST "$BASE/api/accounts/ops" -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"ops":[{"type":"set_enabled","name":"A","enabled":false},
              {"type":"set_enabled","name":"B","enabled":false}]}'
```

**跑一次 Cookie 检测并轮询结果**

```bash
curl -X POST "$BASE/api/cookie-tests/newapi" -H "$AUTH" \
  -H "Content-Type: application/json" -d '{}'

# 每 2 秒轮询，直到 running 变 false
curl -s "$BASE/api/cookie-tests/status" -H "$AUTH" | jq '{running,summary}'
```

**备份整份配置**

```bash
curl -s "$BASE/api/export" -H "$AUTH" | jq -r '.json' > backup-$(date +%F).json
```

用 API Key 免票据。恢复请在网页端「导入」页操作 —— 导入只认 JWT。

**拉可用代理喂给自己的脚本**

```bash
curl -s "$BASE/api/proxies/available?limit=20" -H "$AUTH" | jq -r '.proxies[]'
```

返回顺序已经是优选排序，从前往后取就行。

**回传这批代理好不好用（下次预取的顺序据此调整）**

```bash
curl -X POST "$BASE/api/proxies/feedback" -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"source":"my-script","items":[
        {"addr":"1.2.3.4:8080","ok":2,"net_fail":0,"block_fail":0},
        {"addr":"5.6.7.8:3128","ok":0,"net_fail":1,"block_fail":0}]}'
```

计数只增不减，一轮跑完只调一次 —— 重复上报会把同一轮记重。

**刷新代理池并等它跑完**

```bash
curl -X POST "$BASE/api/proxies/refresh" -H "$AUTH"
while [ "$(curl -s "$BASE/api/proxies/stats" -H "$AUTH" | jq -r '.running')" = "true" ]; do
  curl -s "$BASE/api/proxies/stats" -H "$AUTH" | jq -c '.progress'
  sleep 3
done
```

**看有没有签到在跑（决定能不能动 tabiai 凭据）**

```bash
curl -s "$BASE/api/run-state" -H "$AUTH" | jq '{running,source,heartbeat_at}'
```

**只关心配置有没有被改过**

```bash
curl -s "$BASE/api/config/revision" -H "$AUTH" | jq -r '.revision'
```

比拉整份 `/api/config` 便宜得多，适合高频轮询。
