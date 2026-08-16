# 代理网络换 IP 不计入账号时间盒

## 目标

调整账号级重试逻辑：当一次尝试明确返回 `NETWORK_ERROR`（代理 IP/网络连接问题）时，成功更换代理所消耗的时间不计入该账号的 `ACCOUNT_DEADLINE_SECONDS` 时间盒；WAF 硬封禁、源站失败、Cloudflare/Turnstile 盾类重试仍按现有墙钟时间盒计算。

## 当前实现与风险

- `newapi_checkin/runner.py::_run_account_with_retries()` 在进入账号重试时创建绝对截止时间 `deadline = time.monotonic() + ACCOUNT_DEADLINE_SECONDS`。
- `NETWORK_ERROR` 分支会立即调用 `_swap_pooled_proxy()` 并继续尝试，但换 IP 耗时已经自然消耗了这个绝对截止时间；后续若进入盾类重试，剩余时间会被错误缩短。
- `WAF_BLOCKED`/源站失败分支沿用现有处理，换 IP 后还会等待 5 秒；这些耗时不能被排除。
- `_swap_pooled_proxy()` 返回布尔值，能够区分是否真的成功换到新代理；因此只对网络异常分支中成功的换 IP 计入“暂停时长”。

## 实施步骤

1. **修改 `newapi_checkin/runner.py`**
   - 保留现有绝对截止时间模型，避免扩大改动范围。
   - 在 `NETWORK_ERROR` 分支调用 `_swap_pooled_proxy()` 前记录 `time.monotonic()`，调用结束后若成功换到新 IP，则将该次换 IP 的耗时加回 `deadline`。
   - 对换 IP 失败不延长时间盒，因为没有完成 IP 切换；仍立即按现有逻辑跳过账号。
   - 不修改源站失败/WAF 分支的截止时间处理，确保 WAF 换 IP及其 5 秒等待仍计入时间盒。
   - 更新 `_run_account_with_retries()` 的 docstring 和相关注释，明确“网络异常换 IP 耗时不计入；WAF/盾类换 IP耗时计入”。
   - 对计时差值使用非负值，避免测试替换时钟或系统时钟异常导致截止时间缩短。

2. **补充 `tests/test_speed_guards.py` 回归测试**
   - 新增网络异常场景：将账号时间盒缩短到 1 秒，模拟第一次 `NETWORK_ERROR` 时换 IP 消耗 2 秒，随后返回 `CF_BLOCKED` 再成功；断言仍能继续到成功，证明网络换 IP耗时被排除。
   - 新增 WAF 对照场景：同样模拟换 IP消耗 2 秒，但第一次结果为 `WAF_BLOCKED`，随后进入盾类失败；断言时间盒已耗尽并停止，证明 WAF 换 IP耗时仍计入。
   - 使用可控的 `time.monotonic()` 假时钟和现有 `_EndlessPool`/测试辅助函数，避免真实等待；保留现有网络换 IP无限重试、无新 IP 跳过、WAF 五次换 IP等测试。

3. **验证**
   - 先运行相关测试：`pytest -q tests/test_speed_guards.py`。
   - 再运行全量 Python 测试：`pytest -q`。
   - 若测试通过，再按仓库约定运行 Go 与前端验证（如当前环境可用）：`go vet ./...`、`go test ./...`、前端 type-check/Vitest/build。
   - 检查 `git diff` 和 `git status`，只保留本次相关修改；除非用户另行要求，不 push、不创建额外提交。

## 验收标准

- 代理网络连接失败后，成功更换 IP 的耗时不会减少账号剩余时间盒。
- WAF 硬封禁或盾类重试期间的换 IP耗时仍会减少账号剩余时间盒。
- 没有新 IP、手动代理和现有重试次数/换 IP次数语义不变。
- 新增回归测试通过，且全量测试不回归。
