# 两级登录方式与 GitHub 双协议支持

## 目标

在不破坏现有 NewAPI Cookie 和 AgentRoutercheckin GitHub 流程的前提下，增加 TaBi GitHub OAuth 协议，并让用户通过两级选项选择：

1. 一级：站点 Cookie（现有 NewAPI Cookie）或 GitHub Cookie；
2. 二级：当选择 GitHub Cookie 时，再选择 AgentRoutercheckin 协议或 TaBi 协议。

当前稳定配置值 `newapi_cookie` / `github_cookie` 已被 Python、Go、Web、CLI、迁移和测试共同使用，不直接删除或强制改名；新增协议字段并保留旧值兼容，避免旧配置失效。Web/API 的用户可见名称统一为“站点 Cookie”和“GitHub OAuth”，选择 GitHub OAuth 后再选择“Agent 协议”或“TaBi 协议”；内部使用 `github_protocol` 区分两种 GitHub 协议。

## 已确认的协议差异

### AgentRoutercheckin 协议（现有实现保持不变）

- GET `/api/oauth/state?mode=login`；
- GitHub `/login/oauth/authorize` 携带 `client_id`、`scope=user:email`、`state` 和 `user_session` 三组 Cookie；
- 不跟随重定向，从 Location 提取 code；
- GET `/api/oauth/github?code=&state=&mode=login` 完成回调签到；
- 空 Client ID 使用现有参考项目默认值。

### TaBi 协议（新增）

已通过 `https://tabitoken.com/` 的实际登录页和接口确认：

- POST `/api/oauth/state`，JSON 为 `{ "provider": "github", "intent": "login" }`；
- 站点 `/api/status` 的 `data.github_client_id` 提供该站点的 GitHub Client ID；账号显式配置的 `github_client_id` 优先；
- GitHub authorize 仍使用 `scope=user:email`、state 和 GitHub Cookie；
- GitHub 302 跳转到站点 `/oauth/github` 前端路由；站点后续仍使用 `/api/oauth/github` API 回调，现有 callback 参数保持 `code/state/mode=login`；
- Cookie 可用性检查仍只执行 state + authorize，不调用 callback，不触发签到。

TaBi 的 state 是 POST，不能像 Agent 协议一样直接把 state URL 当浏览器导航地址；浏览器过盾入口改用 TaBi 登录页 `/sign-in`，过盾后再由 HTTP 客户端执行 POST state。

## 实施步骤

### 1. Python 配置模型增加 GitHub 协议字段

修改 `newapi_checkin/config.py`：

- 增加 `GITHUB_PROTOCOL_AGENT = "agent"`、`GITHUB_PROTOCOL_TABI = "tabi"` 及允许值集合；
- `Account` 增加 `github_protocol`，默认 `agent`；
- `_build_accounts()` 解析 `github_protocol`，兼容 `github_flow` / `oauth_protocol` 别名；空值默认 Agent，保留旧账号行为；
- `_apply_env()` 增加 `CHECKIN_ACCOUNT_<NAME>_GITHUB_PROTOCOL`；
- `migrate_legacy()` 为旧账号写入默认 Agent 协议；
- 保留 `login_method` 现有值和原有别名，普通 NewAPI Cookie 逻辑不改。

### 2. Python GitHub OAuth 客户端增加协议分支

修改 `newapi_checkin/github_oauth.py`：

- 将现有 Agent 三步流程封装为明确的 Agent 分支，保持请求语义和 Cookie 构造不变；
- 新增 TaBi state POST 请求，解析 `success/data`，复用现有 Cloudflare、WAF、认证和非 JSON 结果分类；
- 新增 TaBi Client ID 解析：显式 `github_client_id` 优先，否则请求 `/api/status` 读取 `data.github_client_id`；缺失时返回可诊断失败，不错误使用 Agent 默认 Client ID；
- `fetch_github_code()` 接收协议解析后的 Client ID，authorize 和 Cookie 逻辑共用；
- callback、`checked_in` 判定、`/api/user/self`、额度读取、dry-run/cookie-test 语义保持一致；
- 日志只记录协议名称和脱敏信息，不打印 Cookie、state 或 code。

### 3. 浏览器过盾和 Runner 接入 TaBi 协议

修改 `newapi_checkin/cf/solver.py` 及必要的 `runner.py`：

- Agent GitHub 账号继续进入 `/api/oauth/state?mode=login`；
- TaBi GitHub 账号进入 `/sign-in` 作为公开过盾页面，成功后收割 CF 会话并交回 OAuth HTTP 客户端；
- GitHub 两个协议继续共用 Runner 的 S0/S1/S2/S3/S4/S5、代理换 IP、时间盒、重试、缓存、用户 ID 和结果汇总；
- 不把 TaBi GitHub 账号错误送入 NewAPI Cookie 的页内签到分支；
- 不改变普通 NewAPI Cookie 的浏览器入口、localStorage、`New-Api-User` 和页内 POST 签到。

### 4. Go 配置和 Cookie 检测同步

修改 `server/config.go`、`server/db.go`、`server/cookie_checker.go`：

- `Account` 增加 `GithubProtocol`，默认 `agent`；
- 配置校验允许 `agent` / `tabi`，空值迁移为 `agent`；
- Cookie 检测根据账号协议选择 Agent GET state 或 TaBi POST state；
- TaBi 协议动态读取 `/api/status` 的 GitHub Client ID，显式配置优先；
- 保持 Cookie 检测只验证凭据，不执行 OAuth callback；
- 更新 Go 单元测试，覆盖两种协议、动态 Client ID、失效和非 JSON 响应。

### 5. Web/API 配置让用户两级选择

本次用户界面范围仅包含 Web 管理端、Go 配置 API 和 Python 运行时；按 KIQ 确认，PySide6 桌面账号编辑器本轮不改，避免扩大范围。

修改 `web/src/types/index.ts`、`web/src/components/AccountModal.vue`、必要的 `OverviewView.vue` / `CookieTestPanel.vue`：

- 增加 `GithubProtocol = 'agent' | 'tabi'` 和 `Account.github_protocol`；
- 一级显示“站点 Cookie” / “GitHub OAuth”；
- 选择“GitHub OAuth”时显示二级“Agent 协议” / “TaBi 协议”；选择“站点 Cookie”时隐藏二级选项但保留已有值；
- 新账号默认 GitHub 协议为 Agent；编辑旧账号缺字段时也默认 Agent；
- 保存、打码、导出、列表展示和 Cookie 检测提示同步协议名称；
- 不在浏览器端发送 Cookie 明文，不改变现有脱敏和二次确认流程；如果需要发布单文件 Go 管理端，按现有 `scripts/build-config-platform.sh` 重新生成 `server/embed_dist`，不手工修改嵌入产物。

### 6. 配置示例、契约和测试

修改 `config.example.json`、`docs/api-contract.md`、相关设计文档和测试：

- 增加 `github_protocol` 示例和两级选择说明；
- 明确 Agent 与 TaBi 的 state 请求差异、Client ID 来源和 callback 共同点；
- Python 测试覆盖配置默认/别名/环境变量、两种 OAuth state、TaBi 动态 Client ID、cookie-test 不回调、过盾入口选择和普通 NewAPI 回归；
- Go 测试覆盖配置迁移、校验、Cookie 检测两协议；
- Web 通过类型检查、测试和构建验证。

## 验证顺序

1. `python -m pytest -q tests/test_config.py tests/test_github_oauth.py tests/test_runner_e2e.py tests/test_driver_and_solver.py`
2. `python -m pytest -q`
3. `cd server && gofmt -w ... && go test ./...`
4. `cd web && npm run type-check && npm run test -- --run && npm run build`
5. 使用 fake session 验证两种协议的请求方法、路径、参数、Client ID 和 Cookie；不在自动测试中使用真实 Cookie 或执行真实回调。
6. 检查普通 NewAPI Cookie 回归和现有 Agent GitHub 配置兼容；未获额外要求不创建提交、不推送。

## 保护边界

- 不删除现有 NewAPI Cookie 功能；
- 不删除 AgentRoutercheckin GitHub 协议；
- 不把两种 GitHub state 协议强行合并；
- 不在日志、测试输出或前端响应中泄露 Cookie、OAuth state 或 code；
- 仅修改与登录方式选择和两种 GitHub 协议直接相关的文件，其他改动先向 KIQ 说明；PySide6 桌面端本轮明确不修改。
