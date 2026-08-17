# NewAPI 配置管理平台 · 前后端接口契约

> 本契约是 Go 后端与 Vue 前端并行开发的**唯一依据**，两端必须严格遵循。
> 所有接口均返回 JSON；错误统一格式 `{"error": "..."}`。

## 通用约定

- 基础路径：`/api`
- 鉴权方式：
  - 管理端：`Authorization: Bearer <JWT>`（登录后获得）
  - 拉取端：`Authorization: Bearer <API_KEY>`（仅 `/api/config/raw`）
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

`GET /api/config` （JWT）

响应（200）：
```json
{
  "config": { "...完整配置对象，敏感字段打码..." },
  "updated_at": "2026-08-12T10:00:00Z"
}
```

打码规则：
- `accounts[].cookie`：NewAPI 登录 Cookie，非空时返回 `"***"`（值不返回，编辑时原样保留）
- `accounts[].github_user_session`：GitHub `user_session`，非空时返回 `"***"`（值不返回，编辑时原样保留）
- `accounts[].github_client_id`：OAuth Client ID，非敏感，正常返回
- `ai.api_key`：非空时返回 `"***"`
- `notify.email.password`：非空时返回 `"***"`
- `config_sync.token`：非空时返回 `"***"`
- `proxy_pool.sources`：正常返回（非敏感）

> 前端提交时，`"***"` 会被后端识别为「未修改，保留原值」，不覆盖。

## 4. 保存配置（管理端）

`PUT /api/config` （JWT）

请求体：
```json
{ "config": { "...完整配置对象..." } }
```

规则：
- 后端把 `"***"` 占位符还原为旧值（深合并）
- 校验：`accounts` 必须有 `name` / `url`；`login_method` 只能是 `newapi_cookie` 或 `github_cookie`，缺省按 `newapi_cookie` 处理；两种 Cookie 均可为空（实际运行时只检查当前登录方式对应字段）；`sites` 必须有 `name` / `url`；URL 需 http(s) 开头
- 校验通过才落库

响应（200）：`{ "ok": true, "updated_at": "2026-08-12T10:00:00Z" }`
错误（400）：`{ "error": "具体校验错误信息" }`

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

## 7. 导出（JWT）

`GET /api/export` → 响应（200）：
```json
{ "json": "{ 完整明文 config JSON 字符串 }" }
```
> 前端展示后提供「复制」按钮。

## 8. 修改密码（JWT）

`PUT /api/password` 请求体：`{ "old_password": "x", "new_password": "y" }`

响应（200）：`{ "ok": true }`
错误（400）：`{ "error": "旧密码错误" }` 或 `{ "error": "新密码至少 8 个字符" }`

## 9. Cookie 可用性测试（JWT）

站点 Cookie 与 GitHub OAuth 使用两个独立的**启动**接口，共用一个后台任务与一套状态/停止接口。前端只提交账号名称，后端从数据库读取明文凭据；响应不会返回 Cookie、token、OAuth state 或 code。

执行模型：
- 启动接口立即返回，检测在服务端后台按**轮次**执行；同一时刻只允许一个任务，冲突返回 409
- 账号级并发 5；每轮给所有未定论账号各尝试一次，**代理类失败留到下一轮无限重试**，**只有源站类结论才判终态**
- 出口来源：`accounts[].proxy` 优先；未配置时从服务器代理池（`proxies` 表 alive 记录）轮转取，不受 `proxy_pool.enabled` 影响；池子为空则该轮直连
- 账号自带代理连续 2 轮失败后自动降级为代理池出口，`message` 会带「账号代理连续失败，已切换代理池」
- 手动停止后，仍未定论的账号写成 `skipped`，`message` 为「已手动停止（共尝试 N 次，最后失败：…）」

失败分类（决定是否重试）：
- **代理类（重试）**：连接/超时/TLS 等传输层错误；代理自身 407/502/504；非 JSON 响应；403/429/503 且带 `cloudflare`/`cf-ray`/`cdn-cgi` 特征或挑战页文案；GitHub authorize 返回 403/429
- **源站类（终态）**：站点用合法 JSON 明确拒绝凭据（401/403）；站点 URL 无效；`OAuth state 成功但未返回 flow_token`；`站点状态未返回 github_client_id`；GitHub 重定向到 `/login`；GitHub 返回 `error`；GitHub authorize 返回 200（需先在 GitHub 授权该应用）

### 启动：站点 Cookie

`POST /api/cookie-tests/newapi`

请求体：
```json
{ "account_names": ["可选账号名"] }
```

只检测 `login_method=newapi_cookie` 且启用的账号。`account_names` 为空数组时检测该类型的全部启用账号。普通 Cookie 请求 `/api/user/self`；包含 `new_api_refresh=` 时先执行 refresh，再用新凭据验证 self。

### 启动：GitHub OAuth

`POST /api/cookie-tests/github`

请求体格式与站点 Cookie 相同，但只检测 `login_method=github_cookie` 且启用的账号。检测流程（按 New API `v1.0.0-rc.23` 实测确认）：

1. POST 站点 `/api/oauth/state`，请求体 `{"provider":"github","intent":"login"}` → 从 `data.flow_token` 取 state
2. Client ID 取 `accounts[].github_client_id`，为空时 GET 站点 `/api/status` 读 `data.github_client_id`
3. GET GitHub `/login/oauth/authorize?client_id=&scope=user:email&state=` → 从 302 取 OAuth code

取得 code 后即结束，**不会调用站点 OAuth callback，也不会执行签到**；响应不会返回 code、state 或 Cookie。

两个启动接口的响应（200）：
```json
{ "ok": true, "mode": "github_cookie", "started": true }
```
错误（409）：`{ "error": "已有 Cookie 检测任务在进行中" }`
错误（400）：`{ "error": "没有匹配 github_cookie 的启用账号" }`

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
- `github_cookie`：使用 `github_user_session` 走 GitHub OAuth（POST state → authorize → callback）；`github_client_id` 留空时从站点 `/api/status` 读取；站点质询仍复用浏览器/AI 过盾。
- 可用性检查分开执行：`python main.py --cookie-test newapi_cookie` 或 `python main.py --cookie-test github_cookie`；检查命令不会执行真正签到回调。

## 配置对象默认结构（后端初始化时内置）

```json
{
  "accounts": [],
  "sites": [],
  "ai": { "enabled": false, "base_url": "", "api_key": "", "model": "gpt-4o-mini", "timeout": 60, "max_retries": 2 },
  "browser": { "driver": "camoufox", "headless": "virtual", "humanize": true, "timeout": 60, "keep_artifacts_on_fail": true, "locale": "zh-CN", "window": [1280, 800], "executable_path": null },
  "http": { "impersonate": "chrome", "timeout": 20, "verify": true },
  "defaults": { "retry": 2, "interval_seconds": [3, 8] },
  "proxy_pool": { "enabled": false, "test_url": "https://api.ipify.org", "timeout": 8, "max_workers": 25, "max_proxies": 250, "ip_swap_limit": 10, "sources": [], "refresh_minutes": 30, "save_limit": 0, "auto_test": true, "remote_url": "", "remote_token": "", "remote_token_header": "Authorization", "remote_token_prefix": "Bearer" },
  "notify": { "email": { "enabled": false, "smtp_host": "smtp.aliyun.com", "smtp_port": 465, "use_ssl": true, "username": "", "password": "", "from_addr": "", "to_addrs": [], "subject_prefix": "NewAPI 签到日报", "timeout": 20 } },
  "config_sync": { "enabled": false, "url": "", "method": "GET", "token": "", "token_header": "Authorization", "token_prefix": "Bearer", "headers": {}, "body": null, "response_field": "", "timeout": 20, "auto_before_checkin": true },
  "security": { "encryption_enabled": false, "config_key": "", "encrypted_file": "data/config.encrypted.json" }
}
```
