# NewAPI 配置管理平台 · 前后端接口契约

> 本契约是 Go 后端与 Vue 前端并行开发的**唯一依据**，两端必须严格遵循。
> 所有接口均返回 JSON；错误统一格式 `{"error": "..."}`。

## 通用约定

- 基础路径：`/api`
- 鉴权方式：
  - 管理端：`Authorization: Bearer <JWT>`（登录后获得）
  - 拉取端：`Authorization: Bearer <API_KEY>`（仅 `/api/config/raw`）
- 错误码：401 未认证 / 403 无权限 / 400 参数错误 / 404 不存在 / 500 服务器错误
- 配置对象结构 = 完整 config JSON（含 `accounts` / `ai` / `browser` / `http` / `defaults` / `proxy_pool` / `notify` / `config_sync` / `security` 顶层键），见 `config.example.json` 为基底

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
- `accounts[].cookie`：非空时返回 `"***"`（值不返回，编辑时原样保留）
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
- 校验：`accounts` 必须有 `name` / `url` / `cookie`；URL 需 http(s) 开头
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

---

## 配置对象默认结构（后端初始化时内置）

```json
{
  "accounts": [],
  "ai": { "enabled": false, "base_url": "", "api_key": "", "model": "gpt-4o-mini", "timeout": 60, "max_retries": 2 },
  "browser": { "driver": "camoufox", "headless": "virtual", "humanize": true, "timeout": 60, "keep_artifacts_on_fail": true, "locale": "zh-CN", "window": [1280, 800], "executable_path": null },
  "http": { "impersonate": "chrome", "timeout": 20, "verify": true },
  "defaults": { "retry": 2, "interval_seconds": [3, 8] },
  "proxy_pool": { "enabled": false, "test_url": "https://api.ipify.org", "timeout": 8, "max_workers": 25, "max_proxies": 250, "ip_swap_limit": 2, "sources": [] },
  "notify": { "email": { "enabled": false, "smtp_host": "smtp.aliyun.com", "smtp_port": 465, "use_ssl": true, "username": "", "password": "", "from_addr": "", "to_addrs": [], "subject_prefix": "NewAPI 签到日报", "timeout": 20 } },
  "config_sync": { "enabled": false, "url": "", "method": "GET", "token": "", "token_header": "Authorization", "token_prefix": "Bearer", "headers": {}, "body": null, "response_field": "", "timeout": 20, "auto_before_checkin": true },
  "security": { "encryption_enabled": false, "config_key": "", "encrypted_file": "data/config.encrypted.json" }
}
```
