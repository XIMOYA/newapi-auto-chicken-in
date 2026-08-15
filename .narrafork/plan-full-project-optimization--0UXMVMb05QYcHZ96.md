# 全项目自上而下优化实施计划

## 已确认的边界

- 只修改当前仓库，不修改外部服务运行环境。
- 保留现有签到业务策略和当前源站/WAF 重试规则；本轮不重新调整业务次数。
- 网络异常仍保持“有新 IP 就继续换”的不限时语义，本轮不增加网络时间盒。
- API Key 本轮不增加禁用、过期或权限字段。
- 配置并发写入本轮不改，继续保留现有读改写行为。
- 现有生产签到 workflow 的默认超时和失败 artifact 行为不改；只新增独立 CI workflow。
- daemon 停止采用“最多等待 10 秒优雅收尾，之后再强制结束”。
- 登录限流固定为：按 `RemoteAddr + 用户名` 统计，1 分钟内最多 5 次失败；超出后指数退避 1/2/4/8/16 秒，成功登录清除该键记录；不信任 `X-Forwarded-For`。
- JWT：`NCF_ENV=production` 时必须显式设置 `NCF_JWT_SECRET`，缺失直接退出；非生产环境允许临时随机生成，但不打印密钥内容。
- 前端最小测试和 HTTP 连接资源复用纳入本轮。

## 第一阶段：配置安全与远程同步

修改：

- `newapi_checkin/remote_sync.py`
  - 保留本地 `security` 配置和加密状态。
  - 原配置加密时，用原密钥重新加密保存，禁止同步后静默明文落盘或删除加密文件。
  - 远端响应缺少本地关键模块时保留本地模块，并记录告警；显式空数组仍按远端明确意图处理。
- `tests/test_remote_sync.py`、`tests/test_config_store.py`
  - 覆盖加密配置同步、加密文件保留、远端缺模块和显式空数组语义。

## 第二阶段：Python 运行时资源与并发

修改：

- `newapi_checkin/runner.py`
  - 保留网络异常不限时换 IP策略。
  - 完善并行模式中断路径：取消未开始 future，尽快停止等待，并确保日志/结果刷新；不改变普通账号并发语义。
  - 在 `Runner.run()` 的清理路径关闭 AI 客户端。
  - 对 `_ip_cache` 设置有界清理，避免 daemon 多轮运行无界增长。
- `newapi_checkin/ai/vision.py`
  - AI 换代理成功后更新当前线程代理状态，避免下一次视觉请求重新尝试已失败代理。
  - cooldown 等待按剩余任务预算截断，避免超过 AI 任务墙钟上限。
- `newapi_checkin/daemon.py`、`newapi_checkin/logger.py`
  - stop 时先阻止新任务、等待当前任务最多 10 秒、刷新日志和已完成结果，再执行最终强制退出兜底。
  - daemon 定时/立即签到不再无条件把用户的 `virtual` headless 配置改成真 headless。
  - daemon 跨日期运行时自动切换日志文件。
- 测试：`tests/test_runner_parallel.py`、`tests/test_speed_guards.py`、`tests/test_ai_cooldown.py`、`tests/test_daemon.py`。

## 第三阶段：代理池与 HTTP 资源

修改：

- `newapi_checkin/proxy_pool.py`
  - 用受控派发替代一次性提交全部候选，避免大量 future 和后台请求堆积；继续保留全量测通和时间盒语义。
  - 测通完成后按实际延迟排序，保证 `acquire()` 的优先顺序与注释一致。
  - 统一布尔/数值配置解析和边界校验；废弃字段只保留兼容，不继续出现在误导性用户文案中。
- `server/proxies.go`、`server/handlers.go`
  - 刷新与测速采用统一任务互斥，禁止并发清库、写库和覆盖同一进度对象。
  - 后台测速使用独立的 120 秒绝对超时，不绑定已返回 HTTP 请求的 context。
  - 复用代理测速 HTTP Transport/连接资源，避免每个代理创建独立 Transport。
  - `last_run` 未执行时返回空值而非 Go 时间零点。
- 测试：`tests/test_proxy_pool.py`、`server/proxies_test.go`，补充 handler 和刷新/测速并发场景。

## 第四阶段：Go 服务安全与配置健壮性

修改：

- `server/main.go`、`scripts/deploy-config-platform.sh`
  - 删除 `admin/admin123456` 默认凭据和启动日志中的密码输出。
  - 用户表为空且未提供 `NCF_ADMIN_USER/NCF_ADMIN_PASS` 时拒绝启动；部署脚本生成随机初始密码并只通过安全提示输出。
  - 使用 `http.Server` 设置：ReadHeaderTimeout 5s、ReadTimeout 30s、WriteTimeout 120s、IdleTimeout 60s。
  - `NCF_ENV=production` 且缺少 `NCF_JWT_SECRET` 时启动失败；非生产允许随机临时密钥但不打印密钥内容。
- `server/auth.go`、`server/handlers.go`
  - 实现按 `RemoteAddr + 用户名` 的登录失败限流：1 分钟 5 次，之后指数退避 1/2/4/8/16 秒，成功清除记录；限制内存增长并在过期后清理。
  - 增加 `X-Content-Type-Options`、`X-Frame-Options` 等基础安全响应头。
  - 本轮不改 API Key 生命周期，也不改配置读改写并发控制。
  - `UnmaskConfig` 按账号名恢复敏感字段，避免账号排序导致 cookie 错位。
- `server/db.go`、Go 测试
  - 仅补充默认凭据迁移/启动行为和账号名恢复测试，不新增 API Key 字段。

## 第五阶段：前端测试、管理端修复与 CI

修改：

- `web/src/views/ProxiesView.vue`
  - 保存轮询前一轮运行状态，修复任务结束后列表不自动刷新的判断。
- `web/src/utils/proxyPolling.ts`（新增）
  - 抽取轮询完成判断为纯函数，便于测试。
- `web/package.json`、`web/package-lock.json`、前端测试文件
  - 引入最小 Vitest 配置和代理轮询/store 测试，不引入无关依赖升级。
- 新增 `.github/workflows/test.yml`
  - PR/推送运行 Python pytest、Go vet/test、前端 type-check/build/test。
  - 不修改现有每日签到 workflow 的 timeout 和失败 artifact 策略。
- `config.example.json`、`docs/api-contract.md`、部署脚本和注释
  - 同步实际默认值、废弃字段、加密同步、管理员凭据、JWT 环境变量、代理池字段和登录限流说明。
  - 修正 AI 示例启用状态与占位配置不一致问题。

## 明确不纳入本轮

- 网络异常时间盒或网络换 IP 次数限制。
- API Key 禁用/过期/权限系统。
- 配置 PUT 的乐观锁或进程内互斥。
- 现有生产 Actions 的超时和失败截图策略。
- 大规模重构浏览器驱动、AI 模型协议或签到接口策略。

## 验证与提交

按主题修改并运行对应测试；最终运行：

- `python -m pytest -q`
- `go vet ./...`
- `go test ./...`
- `npm run type-check`、`npm run test`、`npm run build`（`web/`）

检查敏感文件和 diff 后，按主题创建独立 commit，沿用 `type(scope): 中文描述`，不 push。
