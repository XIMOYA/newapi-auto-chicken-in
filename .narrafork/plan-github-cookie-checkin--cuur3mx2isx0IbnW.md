# GitHub Cookie 登录与签到链路审查结论

## 目标

严格核对 `F:\github项目库\AgentRoutercheckin\checkin.py` 的 GitHub Cookie 登录/签到协议，确认当前 `newapi-auto-chicken-in` 是否已经按相同方式实现；GitHub Cookie 登录后的流程继续复用当前 NewAPI 的统一运行框架，不擅自改动普通 NewAPI Cookie 功能。

## 已完成的代码审查

### 参考项目固定流程

参考项目的真实流程是：

1. `GET {site}/api/oauth/state?mode=login`，从 JSON 的 `data` 取 OAuth state。
2. `GET https://github.com/login/oauth/authorize`，参数为 `client_id`、`scope=user:email`、`state`；请求头带 `user_session`、`__Host-user_session_same_site`、`logged_in=yes`，禁止跟随重定向，从 3xx 的 `Location` 查询参数提取 `code`。
3. `GET {site}/api/oauth/github?code=&state=&mode=login`，回调 JSON 的 `data.checked_in` 表示本轮是否新增签到；随后用同一个会话请求 `/api/user/self` 获取最新用户和额度。

### 当前项目核对结果

当前 `HEAD` 为 `b0e1f94 (fix(cookie): 对齐 AgentRoutercheckin GitHub OAuth 签到流程)`，核心实现已存在并且与参考流程一致：

- `newapi_checkin/github_oauth.py`
  - state 使用 GET `/api/oauth/state?mode=login`；
  - GitHub authorize 使用参考项目的三组 Cookie、scope 和 state，并关闭自动重定向；
  - callback 使用 GET `/api/oauth/github`，带 `code/state/mode=login`；
  - callback 成功后复用同一 HTTP 会话读取 `/api/user/self`；
  - `checked_in=true` 映射为成功，成功但未新增签到映射为已签到；
  - `dry-run` / `--cookie-test github_cookie` 不调用 callback，避免测试模式误签到。
- `newapi_checkin/runner.py`
  - GitHub 账号按 `github_user_session` 校验凭据；
  - GitHub 与 NewAPI 共用账号调度、代理换出口、Cloudflare/AI 过盾、重试时间盒、结果汇总、用户 ID 缓存和通知；
  - GitHub 过盾后回到同一 OAuth 链路；普通 NewAPI Cookie 分支未被替换。
- `newapi_checkin/cf/solver.py`
  - GitHub 模式只处理站点过盾并收割 CF 会话，再交回 OAuth 客户端；
  - NewAPI 模式继续执行原有页内登录态和签到接口流程。
- Go 配置服务和 Web 管理端已经支持 `login_method=github_cookie`、`github_user_session` 的保存、脱敏、校验和独立 Cookie 检测。

## 验证结果

已运行且通过：

- `python -m pytest -q tests/test_github_oauth.py tests/test_runner_e2e.py tests/test_config.py tests/test_gui.py`：59 passed
- `python -m pytest -q`：435 passed
- `server` 目录执行 `go test ./...`：通过

前端串联的类型检查、测试和构建暂未执行，因为构建会写入产物；且当前用户已选择不扩大本次修改范围。

## 本次实施范围

根据 KIQ 的确认，本次不修改任何代码：

- 不重新调整已经对齐的 Python/Go OAuth 请求协议；
- 不修改普通 NewAPI Cookie 登录和签到实现；
- 不补充 PySide6 桌面端账号编辑框的 GitHub 字段；
- 不修改前端、文档、配置格式或已有功能。

结论：当前项目的核心 GitHub Cookie 登录/签到实现已满足“严格按照 AgentRoutercheckin”以及“登录后复用 NewAPI 统一签到运行框架”的要求，本轮无需产生代码差异。
