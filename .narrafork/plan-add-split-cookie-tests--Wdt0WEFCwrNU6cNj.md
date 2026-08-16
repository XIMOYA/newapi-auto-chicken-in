# Web 独立 Cookie 可用性测试实施计划

## 目标

在现有管理后台新增“Cookie 测试”页面，参考 `F:\github项目库\newapi-cookie-check` 的 NewAPI Cookie 判定逻辑，并将两种凭据严格分开：

- NewAPI Cookie：只检测 `login_method=newapi_cookie` 账号的 `accounts[].cookie`。
- GitHub Cookie：只检测 `login_method=github_cookie` 账号的 `accounts[].github_user_session`，执行站点 OAuth state + GitHub authorize，**不执行 OAuth callback，不触发签到**。

浏览器端不接触明文 Cookie；前端只提交要检测的账号名称，Go 后端从数据库读取敏感配置并返回不含 Cookie 的检测结果。

## 方案

### 后端

1. 新增 `server/cookie_checker.go`：
   - 定义统一结果状态：`valid`、`invalid`、`abnormal`、`skipped`。
   - 实现参考项目的 NewAPI 检测：
     - 普通 Cookie 直接 `GET /api/user/self`。
     - 含 `new_api_refresh=` 的 Cookie 先 `POST /api/user/auth/refresh`，提取 access token / Set-Cookie，再保留原始 Cookie 请求 self。
     - 按 HTTP 状态、`success`、`data.id` 和认证失败关键词分类。
   - 实现独立 GitHub 检测：
     - `GET {site}/api/oauth/state?mode=login` 获取 state。
     - 请求 GitHub `/login/oauth/authorize`，发送 `user_session`、`__Host-user_session_same_site`、`logged_in=yes` Cookie，禁止跟随重定向并检查 OAuth code。
     - 不调用 `{site}/api/oauth/github` 回调，确保不会执行签到。
   - 支持账号代理、HTTP 超时和 TLS 校验配置；固定最大并发 5，按配置顺序返回结果。
   - 结果只包含账号名、URL、状态、消息、用户 ID（若有）、耗时，不回传 Cookie、token、state 或 OAuth code。

2. 修改 `server/handlers.go`：
   - 注册 JWT 保护的两个独立接口：
     - `POST /api/cookie-tests/newapi`
     - `POST /api/cookie-tests/github`
   - 请求体统一为 `{ "account_names": ["可选账号名"] }`；为空时检测该模式下全部启用账号。
   - 每个接口只接受/筛选对应登录方式；没有匹配的启用账号返回明确错误。
   - 返回 `mode`、`checked_at`、汇总统计和结果数组。

3. 新增 `server/cookie_checker_test.go`：
   - 用 `httptest` 覆盖 NewAPI 直接有效/失效、refresh + Authorization + 原始 Cookie 保留、非 JSON/服务器错误分类。
   - 覆盖 GitHub state + authorize code 检查、失效重定向、不会调用 callback。
   - 覆盖接口的模式隔离、账号筛选和响应不泄露敏感字段。

4. 修改 `docs/api-contract.md`：补充两个 Cookie 测试接口、状态含义、模式隔离和“只检查不签到”约定。

### 前端

1. 修改 `web/src/types/index.ts`：增加 Cookie 测试模式、结果、汇总和响应类型。
2. 新增 `web/src/api/cookieTests.ts`：分别封装 `testNewAPICookies` 与 `testGithubCookies`，不共用模糊接口，保证前端调用语义清晰。
3. 新增 `web/src/components/CookieTestPanel.vue`：可复用但状态独立的账号表格面板，显示掩码凭据状态、选择账号、检测按钮、状态标签、消息和耗时；只展示启用的对应登录方式账号。
4. 新增 `web/src/views/CookieTestsView.vue`：使用两个独立 Tab（NewAPI Cookie / GitHub Cookie），各自维护选择、加载状态和结果，提示检测不会执行签到。
5. 修改 `web/src/router/index.ts` 与 `web/src/layouts/AdminLayout.vue`：加入“Cookie 测试”导航和受保护路由 `/cookie-tests`。

## 验证

- `cd server && gofmt -w *.go && go test ./... && go vet ./...`
- `cd web && npm run type-check && npm run test -- --run && npm run build`
- 如需手工验证，启动 Go 后端和 Vite，登录后分别打开两个 Tab，确认：
  - NewAPI Tab 不出现 GitHub 账号，反之亦然。
  - 两个检测按钮各自只更新自己的结果。
  - GitHub 检测不会请求站点 OAuth callback。
  - 网络错误/失效/有效均显示中文状态，结果中不出现 Cookie/token。
