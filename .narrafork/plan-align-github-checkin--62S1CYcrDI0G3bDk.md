# 严格对齐 AgentRoutercheckin 的 GitHub 签到流程

## 现状与问题定位

参考项目 `F:\github项目库\AgentRoutercheckin\checkin.py` 的已验证流程是固定三步：

1. `GET {site}/api/oauth/state?mode=login`，响应 `data` 直接作为 OAuth `state`。
2. `GET https://github.com/login/oauth/authorize`，参数为 `client_id`、`scope=user:email`、`state`；请求头只按参考项目带 `user_session`、`__Host-user_session_same_site`、`logged_in`，禁止跟随重定向，从 302 `Location` 取 `code`。
3. `GET {site}/api/oauth/github?code=&state=&mode=login`，回调响应中的 `data.checked_in` 判定本次是否签到成功；随后用同一会话请求 `/api/user/self` 获取最新用户名和额度。

当前实现偏离了参考项目：

- Python `GithubOAuthClient.fetch_state()` 先发新版 `POST /api/oauth/state`，并解析 `flow_token`。
- Python/Go 都增加了 `/api/status` 动态读取 `github_client_id`，不再严格使用参考项目的默认/显式 Client ID。
- 这会导致目标站点按 AgentRoutercheckin 的旧 OAuth 合约处理时，登录 state、client_id 或授权上下文不一致。

普通 NewAPI 签到链路本身保持：`/api/user/self` 建立用户上下文 → POST 候选签到接口 → 统一 `ApiResult`、重试、过盾、额度和汇总。GitHub 模式没有独立 POST 签到接口，签到动作必须由 OAuth callback 完成；“和普通 NewAPI 一样”落实为复用同一 Runner 结果处理、额度读取、用户 ID 缓存和重试/过盾框架。

## 实施方案

### 1. Python GitHub OAuth 客户端严格恢复参考协议

修改 `newapi_checkin/github_oauth.py`：

- 将 state 路径恢复为 `/api/oauth/state?mode=login`，只执行参考项目的 GET 请求。
- 移除 `POST /api/oauth/state`、`flow_token` 解析和 `/api/status` 动态 Client ID 请求，避免再发送参考项目没有的协议。
- `fetch_state()` 只接受 `success=true` 且 `data` 为非空字符串，并保留现有 Cloudflare/WAF/认证/非 JSON 的 `ApiResult` 分类。
- `fetch_github_code()` 使用 `account.effective_github_client_id`；空配置时沿用 `DEFAULT_GITHUB_CLIENT_ID`，仍保留显式 Client ID 的兼容能力，但用户只需填写 `github_user_session`。
- 将 GitHub 请求头、Cookie 名称、`allow_redirects=False`、302 code 提取、登录页判定对齐参考实现；保留当前 `curl_cffi`、代理、TLS impersonate 和站点过盾能力。
- 保留 OAuth callback：`GET /api/oauth/github?code=&state=&mode=login`。
- 保留 callback 后的 `/api/user/self`，用它补齐最新用户名、user_id、quota；`checked_in=true` 返回 `SUCCESS`，否则返回 `ALREADY_DONE`。
- `dry_run/cookie_test` 继续只执行 state + GitHub authorize，不调用 OAuth callback，避免测试模式误签到。

### 2. Runner 对齐普通 NewAPI 的结果收尾

修改 `newapi_checkin/runner.py`：

- 保留 GitHub 分支的 OAuth callback 作为真实签到入口，不改成普通 NewAPI 的 POST 签到接口。
- 在 GitHub OAuth 成功或已签到后，像普通 NewAPI 分支一样把返回的 `user_id` 写回账号和 `SessionStore`，保证下一次运行复用用户上下文。
- 保持现有统一 `ApiResult`、`SummaryRow`、额度展示、代理换出口、Cloudflare/AI 过盾、时间盒和跳过策略；不改变普通 NewAPI 分支。
- GitHub 过盾后仍进入公开 OAuth state 页面，收割 CF session 后回到同一 OAuth 三步流程。

### 3. Go 可用性检查与参考协议保持一致

修改 `server/cookie_checker.go`：

- GitHub Cookie 可用性检查的前两步改为参考项目的 GET `/api/oauth/state?mode=login` + GitHub authorize。
- 移除 POST state、`flow_token` 和 `/api/status` 动态 Client ID；Client ID 使用账号显式值或同一默认值。
- 保持该接口只检测 Cookie、只取 OAuth code、不调用 callback 的现有安全契约；它不是实际签到接口。

### 4. 测试与文档

修改/补充：

- `tests/test_github_oauth.py`：断言 state 为 GET + `mode=login`、默认/显式 Client ID、Cookie 三字段、OAuth callback、`checked_in=true/false`、`fetch_self` 用户和额度；保留“cookie-test 不调用 callback”测试。
- `tests/test_runner_e2e.py`：增加 GitHub 成功后 user_id 写入 SessionStore 的断言，确认 GitHub 和普通 NewAPI 共用结果收尾。
- `server/cookie_checker_test.go`：改为验证参考项目的 GET state 与默认 Client ID，并保留回调不被调用、失效重定向分类和旧接口安全边界。
- `docs/api-contract.md`：将 GitHub OAuth 描述改回参考项目的固定三步 GET 流程，删除新版 POST/动态 `/api/status` 的说明，明确实际签到 callback 与 cookie-test 的区别。

不删除普通 NewAPI Cookie 的任何现有实现，不删除 GitHub Cookie 配置字段；只纠正 GitHub OAuth 请求协议，使其与参考项目一致。

## 验证计划

1. `python -m pytest -q tests/test_github_oauth.py tests/test_runner_e2e.py`
2. `python -m pytest -q`
3. `cd server && gofmt -w ... && go vet ./... && go test ./...`
4. `cd web && npm run type-check && npm run test -- --run && npm run build`
5. 使用真实可用的 `user_session` 运行一次实际签到链路；若凭据仍被 GitHub 判定失效，只报告凭据阻塞，不把 cookie-test 通过冒充真实签到成功。
6. 验证通过后按仓库风格自动创建本地 git commit，不 push。
