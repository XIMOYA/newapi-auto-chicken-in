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

NewAPI Cookie 与 GitHub Cookie 必须通过两个独立接口检测。前端只提交账号名称，后端从数据库读取明文凭据；响应不会返回 Cookie、token、OAuth state 或 code。

### NewAPI Cookie

`POST /api/cookie-tests/newapi`

请求体：
```json
{ "account_names": ["可选账号名"] }
```

只检测 `login_method=newapi_cookie` 且启用的账号。`account_names` 为空数组时检测该类型的全部启用账号。普通 Cookie 请求 `/api/user/self`；包含 `new_api_refresh=` 时先执行 refresh，再用新凭据验证 self。

### GitHub Cookie

`POST /api/cookie-tests/github`

请求体格式与 NewAPI Cookie 相同，但只检测 `login_method=github_cookie` 且启用的账号。检测流程为站点 OAuth state + GitHub authorize，并在取得 OAuth code 后结束，**不会调用站点 OAuth callback，也不会执行签到**。兼容新版站点的 `POST /api/oauth/state`（`provider=github`、`intent=login`）与旧版 GET 接口；GitHub `client_id` 优先从站点 `/api/status` 动态读取，配置字段仅作为兼容回退。

响应（200）：
```json
{
  "mode": "newapi_cookie",
  "checked_at": "2026-08-16T14:00:00Z",
  "summary": { "total": 1, "valid": 1, "invalid": 0, "abnormal": 0, "skipped": 0 },
  "results": [
    {
      "name": "站点A",
      "url": "https://example.com",
      "state": "valid",
      "user_id": 123,
      "duration_ms": 321,
      "message": "tester"
    }
  ]
}
```

状态含义：`valid` 有效、`invalid` 凭据失效、`abnormal` 网络/服务器/响应异常、`skipped` 对应 Cookie 未配置或检测被取消。

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

- `newapi_cookie`：使用 `cookie`，沿用现有 NewAPI Cookie + 浏览器/AI 过盾链路。
- `github_cookie`：使用 `github_user_session`，按 GitHub OAuth state/authorize/callback 链路签到；站点质询仍复用浏览器/AI 过盾。
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
