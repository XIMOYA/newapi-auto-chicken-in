<!--
docs/github-accounts-api.md
文档：GitHub 账号池与代理池的对外 API 契约
职责：
- 给第三方客户端（另一个项目）提供对接所需的端点、请求/响应结构、认证方式
- 明确凭据打码规则与「哪些字段是服务端状态、提交了也会被忽略」
- 记录三态判定、指纹反查、改名语义这些容易踩的地方
来源：本文件描述的全部端点均已在 server/ 实现并有测试覆盖，非计划稿。
-->

# GitHub 账号池 API 契约

面向第三方客户端。所有端点由 `newapi-config-server` 提供，基址即平台地址。

## 1. 认证

两种凭证，都走 `Authorization: Bearer <token>`：

| 凭证 | 怎么拿 | 适用 |
|---|---|---|
| JWT | `POST /api/login`，body `{"username":"admin","password":"..."}`，响应 `{"token":"..."}` | 网页端、交互式工具 |
| API Key | 网页端「API Key」页创建（`POST /api/keys` 只认 JWT） | 脚本、CI、其他服务 |

本文涉及的端点标注为「双认证」的，两种都接受。

失败一律返回统一错误格式，HTTP 状态码带语义：

```json
{"error": "人话说明"}
```

`401` 凭证无效、`400` 请求有问题（错误信息可直接展示给用户）、`404` 目标不存在、`409` 有冲突操作正在进行、`500` 服务端内部错误（信息已脱敏，细节只在服务端日志）。

## 2. 数据模型

### 2.1 GitHubAccount

一份 GitHub 网页会话供多个站点账号共用。出现在 `GET /api/config` 响应的 `config.github_accounts[]`。

```json
{
  "name": "Steven",
  "user_session": "***",
  "client_id": "",
  "fingerprint": "9f2c1ab8...",
  "proxy_addr": "vless://***@node.example.com:443#香港01"
}
```

| 字段 | 含义 | 谁能写 |
|---|---|---|
| `name` | GitHub 用户名。**同时是引用键**，`accounts[].github_account` 按它找凭据 | 客户端 |
| `user_session` | GitHub 网页会话 cookie。**凭据**，出站一律打码成 `"***"` | 客户端 |
| `client_id` | 站点 OAuth 应用 ID，留空则由站点 `/api/status` 探测。不是凭据，明文下发 | 客户端 |
| `fingerprint` | 客户端指纹 seed，决定这个账号对外呈现的 UA 等特征 | **仅服务端** |
| `proxy_addr` | 绑定的固定出口。出站经展示脱敏 | **仅服务端** |

后两个字段提交上来会被忽略，详见 §5。

### 2.2 账号对池子的引用

`config.accounts[]` 有一个 `github_account: string` 字段，按 `name` 引用池子。签发凭据时的取值优先级是**池子优先、账号自带的旧字段兜底**：

```
accounts[].github_account 指向的池子条目 → accounts[].github_user_session（迁移期兜底）
```

两处都取不到时签发端点返回 `400`。空串表示不引用池子。

## 3. 增删改：`POST /api/github-accounts/ops`

双认证。**不要用 `PUT /api/config` 改池子** —— 整份提交会把后台刚轮转的凭据按陈旧快照覆盖回去。

```json
{"ops": [ /* 1..500 条 */ ]}
```

### 3.1 新增 / 更新

```json
{"type": "upsert", "account": {"name": "Steven", "user_session": "abc", "client_id": ""}}
```

- `name` 已存在即更新，不存在即新增
- `user_session` 不能为空（`400`）；可以填 `"***"` 表示「保持库里的原值」
- 只想改 `client_id` 时，`user_session` 就填 `"***"`

### 3.2 改名

```json
{"type": "upsert", "previous_name": "旧名", "account": {"name": "新名", "user_session": "***", "client_id": ""}}
```

服务端会连带做三件事，不需要客户端自己处理：

1. 把引用它的 `accounts[].github_account` 一起改成新名
2. 按 `previous_name` 找回 `"***"` 对应的真实 `user_session`
3. 把 `fingerprint` 与 `proxy_addr` 搬到新记录上

第 3 条是刻意的：不搬 `fingerprint` 就等于改个名让这个账号在 GitHub 眼里换了台设备；不搬 `proxy_addr` 就等于换了出口 IP。两者都可能触发会话风控。

`previous_name` 不存在返回 `400`（不会退化成新增 —— 那样只会存下一条 `"***"` 当凭据的废记录）；新名已被占用同样 `400`。

### 3.3 删除

```json
{"type": "delete", "name": "Steven"}
```

- 还有站点账号引用时返回 `400`，错误信息里带引用数量，请先改掉那些账号的引用
- 目标已不存在**不算错误**：并发编辑下这是正常的，会出现在响应的 `skipped` 里

### 3.4 响应

```json
{
  "ok": true,
  "config": { /* 打码后的完整配置，可直接替换本地副本 */ },
  "updated_at": "2026-08-30T12:00:00Z",
  "revision": 42,
  "skipped": ["delete \"X\"：已不存在"]
}
```

`skipped` 非空时建议提示用户，那是并发编辑导致的跳过。

## 4. 可用性探测：`POST /api/github-accounts/check`

双认证。探测某个池子账号的 `user_session` 在它引用的站点上还能不能完成 GitHub 授权。

```json
{"name": "Steven"}
```

**这个请求会实际访问 GitHub，可能耗时几十秒**，客户端超时请设到 180 秒以上。服务端内部串行执行（GitHub OAuth 端点对固定出口有明显限流），并发调用会排队。

```json
{
  "ok": true,
  "name": "Steven",
  "site": "https://a.example.com",
  "result": {
    "status": "ok",
    "message": "GitHub 返回授权 code，user_session 有效",
    "authorized_client_id": "Ov23li..."
  }
}
```

### 4.1 三态语义

| status | 含义 | 该怎么处置 |
|---|---|---|
| `ok` | GitHub 返回了授权 code，会话有效 | 无需动作 |
| `expired` | GitHub 要求重新登录，会话已失效 | 去 GitHub 重新取 `user_session` 填回来 |
| `unknown` | 判定不了：出口被 GitHub 限流、站点 OAuth 流程出错、网络失败 | 稍后重试，**不要**据此判定凭据失效 |

`unknown` 与 `expired` 的处置完全不同，界面上请分开呈现。

探测只走到「拿到授权 code 就停」，不兑换 —— 不消耗站点侧的授权码配额，也不会给你库里多出一条新凭据。

### 4.2 前置条件

池子账号必须至少被一个站点账号引用（探测需要站点上下文）。未被引用时返回 `400`；账号名不存在返回 `404`。

## 5. 服务端状态字段：`fingerprint` 与 `proxy_addr`

这两个字段是**服务端运行状态而不是用户配置**，客户端提交的值不会生效。

### 5.1 fingerprint

指纹 seed，首次出现该账号时由服务端按账号名派生并落库，之后永不变动。它决定这个账号对外呈现的一整套客户端特征（User-Agent、`Accept-Language`、`Sec-CH-UA` 族），签发与探测链路的每一跳都用它。

之所以要固定：GitHub 的会话绑设备特征，同一条 session 忽然换 UA 会被判成异常会话。之所以是「一整套」：UA 里的浏览器大版本必须与 `Sec-CH-UA` 里的一致，Firefox 不能带 `Sec-CH-UA`（它不实现 Client Hints）—— 不自洽的组合比不伪装更可疑。

它不是凭据，明文下发，客户端可以展示。

### 5.2 proxy_addr

该账号绑定的固定出口，取自代理池。语义是**粘住**：只有这个出口不可用了才换，不做负载均衡。理由同上 —— 会话换 IP 是风控高权重信号。

出站时经过展示脱敏（VLESS 节点的 uuid 是接入凭据）：

```
vless://<uuid>@node.example.com:443?type=ws#香港01
  ↓
vless://***@node.example.com:443#香港01
```

客户端拿不到完整值，也不需要 —— 出站请求由服务端发起。

### 5.3 回写行为

`PUT /api/config` 整份提交时：

- 提交的 `proxy_addr` 含 `***`（即界面原样回传的脱敏值）→ 服务端换回库里的真值
- 提交的 `proxy_addr` 为空但库里有 → 补回库里的值
- 提交的 `fingerprint` 一律以库里为准

**整份 `PUT` 无法表达改名**：它不带 `previous_name`，服务端无从知道新旧名的对应关系，改名会丢掉这两个字段。改名请走 §3.2。

## 6. 签发与失效名单

### 6.1 `POST /api/tabiai/issue-cookie`

双认证。为账号签发一条新的站点凭据（走 GitHub OAuth 三步）。

```json
{"account_name": "Steven（a.example.com）", "for_running_checkin": false}
```

- 凭据取值走 §2.2 的优先级；两处都没有时 `400`，错误信息会点明「池子里也没有」
- 出站用该账号池子条目的固定指纹
- 签到进行中调用会被 `409` 拦住（签发会换出新的凭据代次，让正在跑的那轮当场作废）。签到进程自己调用时传 `for_running_checkin: true` 放行
- 响应 `{"ok": true, "account_name": "..."}`。**明文凭据只回给 API Key 调用方**（签到客户端要拿它立刻重试），JWT 调用方不下发

### 6.2 `GET /api/tabiai/expired`

双认证。列出凭据已失效、可以一键签发的账号。

```json
{
  "accounts": [
    {"name": "...", "state": "invalid", "paused": false, "message": "凭据已失效",
     "last_run_at": "...", "has_user_session": true}
  ],
  "count": 1,
  "checked_at": "..."
}
```

`has_user_session` 看的是**实际生效的凭据**（池子优先、旧字段兜底），不是只看账号自带字段。响应不含任何凭据值。

## 7. 代理池相关变更

### 7.1 `GET /api/proxies`（双认证）

每条新增两个字段：

```json
{"id": 1, "source": "...", "addr": "vless://***@node.example.com:443#香港01",
 "protocol": "vless", "fingerprint": "a1b2c3d4e5f6",
 "latency_ms": 80, "alive": true, "last_checked_at": "...", "speed_bps": 0}
```

- `protocol`：`http` / `socks5` / `vless`。裸 `1.2.3.4:8080` 归为 `http`
- `fingerprint`：完整地址的 12 位短指纹，**当操作键用**
- `addr`：带凭据的地址已脱敏（VLESS 的 uuid、`http://user:pass@` 的密码）。裸 `host:port` 和无认证 `socks5://` 原样不变

为什么需要指纹：同一机场只差 uuid 的两条节点，脱敏后**长得一模一样**，只有指纹能区分。

### 7.2 `POST /api/proxies/speedtest`（双认证）

```json
{"proxies": ["a1b2c3d4e5f6", "1.2.3.4:8080"]}
```

每一项先按 `addr` 精确匹配，匹配不上再按 `fingerprint` 反查。所以：

- 对被脱敏的条目**必须传 `fingerprint`**（传脱敏后的 addr 匹配不上）
- 对未脱敏的条目两者都行
- 最简单的做法是统一传 `fingerprint`

响应 `{"ok": true, "tested": N, "url": "测速地址"}`。测速在后台跑，与刷新互斥（冲突返回 `409`）。

### 7.3 `GET /api/proxies/available`（仅 API Key）

给客户端预取用，**不脱敏**、返回完整地址：

```json
{"proxies": ["1.2.3.4:8080", "vless://uuid@host:443?type=ws#香港01"], "count": 2, "checked_at": "..."}
```

支持 `?limit=N`、`?shard=I/N`（按轮转切片，各片不重叠）。这里只认 API Key —— 与 `/api/config/raw` 同一条边界。

### 7.4 `POST /api/proxies/feedback`（仅 API Key）

```json
{"source": "github-actions",
 "items": [{"addr": "vless://uuid@host:443#香港01", "ok": 1, "net_fail": 0, "block_fail": 0}]}
```

`addr` 必须是**完整原始地址**（`available` 给你的那个），不是脱敏值也不是指纹 —— 它是反馈表的主键。地址形状校验接受裸 `host:port`、带 scheme 的 `socks5://`/`http://`、以及完整 `vless://` URI，上限 1024 字节。

计数只增不减，服务端不接受覆盖或清零。单次最多 2000 条。响应 `{"ok": true, "accepted": N, "skipped": M}`。

## 8. 对接顺序建议

1. `POST /api/login` 拿 JWT，或用现成 API Key
2. `GET /api/config` 读 `config.github_accounts` 与 `config.accounts`，注意凭据字段是 `"***"`
3. 新账号入池前先 `POST /api/github-accounts/status` 判一次（停用/封禁的不入池）
4. 改池子走 `POST /api/github-accounts/ops`，响应里的 `config` 直接替换本地副本
5. 查会话是否还活着走 `POST /api/github-accounts/check`，按三态分开处置
6. 需要新的站点凭据走 `POST /api/tabiai/issue-cookie`
7. 新接一个站点时走 `POST /api/sites/provision` 批量建号

## 9. 新增端点（补记）

### 9.1 `POST /api/github-accounts/status`

双认证。判定**账号自身**状态，与站点无关。body 二选一：

```json
{"name": "Steven"}                 // 按池子账号名（凭据从池子取）
{"user_session": "尚未入池的凭据"}   // 入池之前先判一次
```

```json
{"ok": true, "name": "Steven",
 "result": {"status": "active", "message": "已登录，账号可用", "usable": true}}
```

`status` 五态：`active` / `suspended` / `banned` / `expired` / `unknown`。
`usable` 只在 `active` 为真，是「值得留在池子里」的唯一依据 ——
`suspended` 有申诉恢复的可能、`banned` 基本没有，两者都别加进池子。
`expired` 是 session 失效（账号本身未知），`unknown` 是出口被限流/页面变化判不了。
该请求会实际访问 GitHub，可能数十秒，客户端超时放宽到 180s 以上。

### 9.2 `POST /api/sites/provision`

双认证。按站点 URL 为池子里的 GitHub 账号批量建签到账号（站点在 OAuth 首次登录时
自动注册用户，所以注册和登录是同一趟流程，不需要浏览器）。

```json
{"url": "https://a.example.com", "only": ["Steven"]}
```

`only` 可选，只处理这几个账号；留空处理池子全部。**会真的为每个账号签发一条站点
凭据**，请先确认该站点是新接入的，避免覆盖已有账号。已存在的账号会被跳过，不会
重新签发。整批串行执行且过签到锁，可能耗时几分钟，客户端超时放宽。

```json
{"ok": true, "url": "...", "created": 2, "total": 3,
 "results": [
   {"github_account": "Steven", "account_name": "Steven（a.example.com）",
    "status": "created", "attempts": 1},
   {"github_account": "NoCred", "status": "skipped_no_credentials", "attempts": 0}
 ]}
```

`status` 取值与处置：
- `created`：建号成功，`account_name` 是规范名「GitHub名（域名）」，已落库
- `exists`：站点账号已存在，跳过
- `skipped_registration_closed`：站点关闭注册，**只尝试一次不重试**
- `skipped_no_credentials`：该 GitHub 账号没有 user_session
- `failed`：其他失败（网络/限流/站点 5xx），已重试到 `provisionMaxAttempts`（3）次

## 10. 已知边界

- 池子账号名（引用键）必须非空且唯一，服务端在保存时校验，重名返回 `400`
- 单次 ops 最多 500 条
- 探测串行执行，多个账号排队；单次可能几十秒
- `proxy_addr`（出口绑定）现在是**真实生效**的：签发/探测/批量建号都走账号绑定的
  固定出口，只在原出口不可用或链路失败时才换。仍有两个限制：
  - 当前池子里的 http/socks5 节点可直接用；VLESS 节点需先在本地起 xray 转 socks5，
    那层还没接，绑到 VLESS 时本次走直连并记日志
  - 绑定出口在每次代理刷新时被并入候选复测，所以判死但网络抖了一下的绑定
    不会因为一次刷新就被换掉
