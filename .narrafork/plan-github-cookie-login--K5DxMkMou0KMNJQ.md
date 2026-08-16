# GitHub Cookie 双登录接入计划

## 目标

在现有 `newapi-auto-chicken-in` 中新增两种账号登录方式，并保持原有 NewAPI Cookie + Cloudflare/Turnstile/AI 过验证链路不退化：

- `newapi_cookie`：现有方式，使用 `accounts[].cookie`，继续走 `/api/user/self`、`New-Api-User`、签到接口和浏览器/AI 过盾链路。
- `github_cookie`：参考 `AgentRoutercheckin` 的 GitHub OAuth 回调签到方式，使用 `accounts[].github_user_session`（必要时配置 `github_client_id`），执行 state → GitHub authorize → site OAuth callback；站点 Cloudflare 质询仍可复用现有浏览器/AI 过盾链路。

配置字段由 Go 后端、Vue 前端和 Python 运行时同步维护，`newapi_cookie` 为默认值；旧配置只有 `cookie` 时行为保持不变。

## 设计约定

1. 账号字段统一采用：
   - `login_method`: `newapi_cookie` 或 `github_cookie`，默认 `newapi_cookie`。
   - `cookie`: NewAPI 站点登录 Cookie，敏感字段。
   - `github_user_session`: GitHub `user_session` 值，敏感字段。
   - `github_client_id`: GitHub OAuth Client ID，可选；Python 端为空时使用参考项目兼容的默认 Client ID。
2. 兼容解析：
   - 缺少 `login_method` 时默认 `newapi_cookie`。
   - 为兼容参考项目/旧导入数据，若没有 `login_method`、`cookie` 为空但存在 `github_user_session` 或 `user_session`，Python 配置解析推断为 `github_cookie`；显式 `login_method` 始终优先。
   - GitHub Cookie 字段兼容读取 `github_user_session`、`user_session`、`github_cookie`；写出配置时只使用标准字段。
   - NewAPI 旧字段 `userId`、`browserPath`、`checkinPath` 等继续保持现有兼容行为。
3. 保存配置允许 Cookie 暂为空，便于先建账号再补密钥；实际运行/可用性检查只对所选登录方式检查对应凭据，不再用“缺 NewAPI cookie”误伤 GitHub 账号。
4. Go 管理端不直接访问目标站点，也不在浏览器端发送 Cookie；可用性检查落在 Python runner/CLI，避免管理服务承担签到运行时和敏感 Cookie 出网职责。两种检查通过显式模式分别执行：`--cookie-test newapi_cookie` 与 `--cookie-test github_cookie`。

## 实施步骤

### 1. Python 配置模型与 GitHub OAuth 客户端

修改 `newapi_checkin/config.py`：

- 增加登录方式常量及默认 GitHub Client ID。
- 扩展 `Account` dataclass，加入 `login_method`、`github_user_session`、`github_client_id`，保留现有字段和属性。
- 在 `_build_accounts()` 中完成登录方式/字段别名归一化、默认值和基本合法性校验。
- 在 `_apply_env()` 中增加按账号名覆盖 `LOGIN_METHOD`、`GITHUB_USER_SESSION`、`GITHUB_CLIENT_ID` 的环境变量支持；继续兼容现有 `COOKIE`、`PROXY` 覆盖。
- 在 `migrate_legacy()` 中显式写出 `login_method: newapi_cookie`，避免旧数组迁移后行为变化。
- 账号解析错误信息明确指出缺少的是 NewAPI Cookie 还是 GitHub Cookie，不在配置加载阶段强制要求敏感值非空。

新增 `newapi_checkin/github_oauth.py`：

- 使用已有 `curl_cffi`，复用 HTTP 超时、代理、TLS impersonate、UA 和 Cloudflare 响应识别，不引入新第三方依赖。
- 封装 GitHub OAuth 三步请求：
  1. `GET {site}/api/oauth/state?mode=login`，解析 `data` 为 state；
  2. `GET https://github.com/login/oauth/authorize`，带 `client_id`、`scope=user:email`、`state`，使用 `user_session`、`__Host-user_session_same_site`、`logged_in=yes` Cookie，禁止自动跟随重定向并从 `Location` 提取 code；
  3. `GET {site}/api/oauth/github?code=&state=&mode=login`，解析回调结果。
- 对非 JSON、Cloudflare/ WAF、GitHub 跳登录页、缺失 code、HTTP/网络异常分别转换为现有 `ApiResult` 状态，确保 runner 的换 IP/跳过策略可复用。
- 回调成功后用同一 session 拉 `/api/user/self`（若响应提供用户 ID则带 `New-Api-User`），补充用户名/额度信息；`data.checked_in=true` 归类为 `SUCCESS`，成功但今日已签到归类为 `ALREADY_DONE`。
- 提供不执行 OAuth 回调的凭据检查函数：只完成站点 state + GitHub authorize/code，确保 `--cookie-test github_cookie` 不触发签到；NewAPI Cookie 检查只请求 `/api/user/self`。

### 2. Runner 双链路与 AI 过验证接入

修改 `newapi_checkin/runner.py`：

- 在 `RunOptions` 增加 `cookie_test: Optional[str]`，取值限制为两种标准登录方式。
- `_checkin_account()` 按 `account.login_method` 选择凭据；NewAPI 只检查 `cookie`，GitHub 只检查 `github_user_session`，缺失时返回对应中文原因。
- `_attempt()` 增加 GitHub 分支：调用 `GithubOAuthClient` 的正常签到或 dry-run 可用性检查；仍返回已有 `ApiResult`，因此网络异常、WAF、Cloudflare、认证失败、已签到和成功都沿用现有时间盒/换 IP/汇总逻辑。
- 保留 NewAPI 分支的 S0 缓存、S1 直连、`New-Api-User` 和现有签到路径探测逻辑不变。
- 让 `_solve()`/`_solve_guarded()` 把账号登录方式传给过盾流程；GitHub 模式遇到站点 Cloudflare 时，浏览器进入公开 OAuth state 地址（而非需要站点登录的 dashboard），不把“站点未登录”误判为失败。
- 修改 `newapi_checkin/cf/solver.py`：
  - NewAPI 模式保持现有 Cookie 注入、localStorage、页内签到（S4）行为；
  - GitHub 模式只处理站点 Cloudflare/Turnstile，成功后收割站点 CF session 并交回 GitHub OAuth HTTP 客户端，不尝试无效的页内 NewAPI Cookie 签到；
  - GitHub 模式仍允许 AI S3、人工 S5 和现有截图/失败产物；仅跳过 NewAPI 登录态检查与 S4 业务接口调用。
- `ApiClient` 不承担 GitHub OAuth 逻辑；仅补充可复用的 Cookie/响应分类辅助（如确有必要），避免两套认证语义混在一起。
- 增加 `Runner` 的独立 Cookie 检查路径：按 `cookie_test` 过滤账号，只跑选定类型，仍可启用代理池、浏览器和 AI 过盾，但绝不调用真正签到回调；无匹配账号时给出明确结果。

修改 `main.py`：

- 增加互斥参数 `--cookie-test {newapi_cookie,github_cookie}`，帮助文本说明两种检查必须分别执行。
- 将该参数传入 `RunOptions`；普通运行仍允许两种登录方式混合，未指定时不改变现有行为。
- 更新入口注释/帮助文本，说明 `--dry-run` 在普通运行中保持兼容，显式 Cookie 检查用于分别验证两种凭据。

### 3. Go 后端配置、迁移、脱敏与接口契约

修改 `server/config.go`：

- `Account` 增加 `LoginMethod`、`GithubUserSession`、`GithubClientID`，默认登录方式为 `newapi_cookie`。
- `MaskConfig`、`UnmaskConfig` 同时处理 `cookie` 与 `github_user_session`，按账号名称还原，不能因一方为空影响另一方。
- `ValidateConfig` 规范化空登录方式为默认值，并拒绝未知登录方式；两种 Cookie 允许为空，保持前端先保存账号的能力。
- `cloneConfig` 继续深拷贝账号指针字段，新增字符串字段随账号复制。

修改 `server/db.go`、`server/main.go`、迁移测试：

- 将配置版本从 1 升到 2。
- v1/v0 迁移时只为缺失/空的 `login_method` 填 `newapi_cookie`，不改用户显式选择；保留既有代理池默认值迁移和幂等性。
- 启动时继续执行 `MigrateConfig`，确保旧数据库读取后立即补齐默认字段。

修改 `server/server_test.go`、`server/migrate_test.go`：

- 覆盖默认登录方式、未知方式拒绝、两种敏感 Cookie 分别打码/还原、同名账号还原不串值。
- 覆盖旧配置无 `login_method` 的迁移、已明确设置 GitHub 模式时不被迁移覆盖、PUT/RAW/export 保留两种字段。

修改 `docs/api-contract.md`：

- 更新账号对象结构、默认结构、敏感字段打码规则、保存校验规则，明确 `login_method` 枚举和两种 Cookie 的职责。
- 记录 Python CLI 的两种独立可用性检查语义，说明 Go 管理端只保存/脱敏配置，不直接发起目标站点检测。

同步修改 `config.example.json` 和 `docs/远程配置管理平台设计方案.md`：

- 示例账号加入 `login_method`、`github_user_session`、`github_client_id`；默认仍为 NewAPI Cookie。
- 设计文档补充两种登录字段、敏感性、GitHub OAuth 流程、AI 过盾复用边界和独立检查命令。

### 4. Vue 前端配置编辑

修改 `web/src/types/index.ts`：

- `Account` 与契约同步增加 `login_method`、`github_user_session`、`github_client_id`。
- 增加登录方式字面量类型/辅助类型，避免组件里散落字符串。

修改 `web/src/components/AccountModal.vue`：

- 增加登录方式选择，新增账号默认 `newapi_cookie`。
- NewAPI 模式显示/编辑 `cookie`；GitHub 模式显示/编辑 `github_user_session` 和 `github_client_id`；切换方式不清空另一套字段，保证前后端独立保存。
- 两套 Cookie 都使用 `MaskedInput`，分别支持“留空保持原值”、已设置提示和密码二次确认查看明文；导出明文时按账号名取对应字段。
- 保留现有名称、URL、user_id、代理、路径、启停校验，并在提交时带上完整双 Cookie 字段和标准默认值。

修改 `web/src/views/OverviewView.vue`：

- 账号表格增加“登录方式”列和对应 Cookie 状态展示，避免把 GitHub Cookie 误显示成 NewAPI Cookie。
- 搜索条件纳入登录方式/两类 Cookie 的设置状态（不显示明文）。
- 现有编辑、删除、批量启停和敏感 Cookie 查看流程继续兼容。

必要时同步 `web/src/api/config.ts` 的类型封装；不新增无法由 Go/Python 运行时支持的浏览器直连检测按钮。

### 5. 回归测试

Python：

- `tests/test_config.py`：默认登录方式、显式 GitHub 方式、参考字段别名推断、环境变量覆盖、旧配置迁移、两种 Cookie 缺失提示。
- 新增 `tests/test_github_oauth.py`：用 fake curl session/response 覆盖 state 解析、GitHub 302 code 提取、登录跳转/缺 code、回调成功/已签到/失败、非 JSON/Cloudflare/网络异常，以及“凭据检查不调用 callback”。
- `tests/test_runner_e2e.py` 或相邻 runner 单测：NewAPI 与 GitHub 分支选择、GitHub 过盾后交回 OAuth、`--cookie-test` 按类型隔离、普通运行可混合两种账号、缺失一类 Cookie 不影响另一类账号。
- 如需 fake driver，覆盖 GitHub 模式跳过 S4/登录态误判，保证现有 NewAPI solver 回归测试仍通过。

Go：

- 扩展现有 `server/server_test.go`、`server/migrate_test.go`，覆盖契约字段、脱敏/还原、迁移和校验。

Vue：

- 若现有测试基础设施适合，补充 AccountModal 的默认方式、切换方式不丢字段、两类掩码字段提交语义；否则至少通过 `npm run type-check` 与构建验证模板/类型契约。

## 验证与提交

按以下顺序执行：

1. `python -m pytest -q`
2. `cd server && go vet ./... && go test ./...`
3. `cd web && npm run type-check && npm run test -- --run && npm run build`
4. 检查 `git diff`，只暂存本任务相关文件，不还原用户已有改动。
5. 按主题创建中文 Conventional Commits，建议拆为：
   - `feat(auth): 接入 NewAPI 与 GitHub Cookie 双登录链路`
   - `feat(config): 同步双 Cookie 配置与旧版本默认迁移`
   - `feat(web): 支持账号登录方式与双 Cookie 脱敏编辑`
   - `test(auth): 补充双 Cookie 可用性与回归测试`
   若实际改动范围较小可合并，但每个提交正文说明“为什么”保留双链路/独立检查。
6. 提交前验证全部通过才提交；只创建本地提交，绝不 push。
