/*
server/hardening_test.go
安全与稳定性加固相关测试：
- main.go    ：默认凭据、JWT 环境校验（生产必填、非生产随机）
- auth.go    ：登录失败限流（1 分钟 5 次 + 指数退避 + 成功清除）
- handlers.go：登录限流 HTTP 行为、安全响应头、刷新/测速冲突 409
- config.go  ：UnmaskConfig 按账号名还原（排序错位不串号）
- proxies.go ：刷新/测速互斥、测速独立 120s context、未运行 last_run 空值
*/
package main

import (
	"context"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// ---------------------------------------------------------------------------
// main.go：默认凭据
// ---------------------------------------------------------------------------

// 空 users 表且未提供初始凭据时必须失败（不再有内置默认管理员密码）。
func TestEnsureAdminRequiresCredentials(t *testing.T) {
	db, err := OpenDB(filepath.Join(t.TempDir(), "admin.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = db.Close() })

	if err := EnsureAdmin(db, "", ""); err == nil {
		t.Fatal("空用户名+空密码应报错")
	}
	if err := EnsureAdmin(db, "admin", ""); err == nil {
		t.Fatal("缺密码应报错")
	}
	if err := EnsureAdmin(db, "", "pass1234"); err == nil {
		t.Fatal("缺用户名应报错")
	}
	if err := EnsureAdmin(db, "admin", "s3cret-pass"); err != nil {
		t.Fatalf("提供完整凭据应成功: %v", err)
	}
	// 已有账号时照常启动（升级场景），无需再传凭据
	if err := EnsureAdmin(db, "", ""); err != nil {
		t.Fatalf("已有账号时不应再要求凭据: %v", err)
	}
}

// 生产代码中不得残留内置默认密码（测试文件里显式传入的密码不算）。
func TestNoDefaultPasswordInSource(t *testing.T) {
	matches, err := filepath.Glob("*.go")
	if err != nil {
		t.Fatal(err)
	}
	for _, f := range matches {
		if strings.HasSuffix(f, "_test.go") {
			continue
		}
		data, err := os.ReadFile(f)
		if err != nil {
			t.Fatal(err)
		}
		if strings.Contains(string(data), "admin123456") {
			t.Errorf("%s 包含默认密码 admin123456", f)
		}
	}
}

// ---------------------------------------------------------------------------
// main.go：JWT 环境
// ---------------------------------------------------------------------------

func TestResolveJWTSecret(t *testing.T) {
	long := strings.Repeat("k", 32)
	if _, err := resolveJWTSecret("production", ""); err == nil {
		t.Error("production + 空密钥应报错")
	}
	if _, err := resolveJWTSecret("production", "short"); err == nil {
		t.Error("production + 短密钥应报错")
	}
	got, err := resolveJWTSecret("production", long)
	if err != nil || got != long {
		t.Errorf("production + 合法密钥应原样返回: %q, %v", got, err)
	}
	// 大小写/空白宽容
	if _, err := resolveJWTSecret(" Production ", long); err != nil {
		t.Errorf("production 大小写/空白应宽容: %v", err)
	}

	// 非生产：未设置 → 随机生成 64 位 hex（调用方不打印密钥本身）
	r1, err := resolveJWTSecret("", "")
	if err != nil {
		t.Fatal(err)
	}
	if len(r1) != 64 {
		t.Errorf("随机密钥长度 = %d, want 64", len(r1))
	}
	r2, _ := resolveJWTSecret("dev", "")
	if r1 == r2 {
		t.Error("两次随机生成不应相同")
	}
	if got, err := resolveJWTSecret("dev", long); err != nil || got != long {
		t.Errorf("非生产 + 显式密钥应原样返回: %q, %v", got, err)
	}
}

// ---------------------------------------------------------------------------
// auth.go：登录失败限流（单元层，假时钟）
// ---------------------------------------------------------------------------

func TestLoginLimiter(t *testing.T) {
	now := time.Now()
	l := newLoginLimiter()
	l.now = func() time.Time { return now }

	key := "1.2.3.4|admin"
	// 前 4 次失败不拦截
	for i := 0; i < 4; i++ {
		l.recordFailure(key)
		if _, allowed := l.check(key); !allowed {
			t.Fatalf("第 %d 次失败后不应被拦截", i+1)
		}
	}
	// 第 5 次失败：进入 1s 退避
	l.recordFailure(key)
	if _, allowed := l.check(key); allowed {
		t.Fatal("第 5 次失败后应进入退避")
	}
	now = now.Add(1 * time.Second)
	if _, allowed := l.check(key); !allowed {
		t.Fatal("退避 1s 后应放行")
	}
	// 继续失败：退避翻倍 2s（第 6 次）
	l.recordFailure(key)
	if _, allowed := l.check(key); allowed {
		t.Fatal("第 6 次失败后应 2s 退避")
	}
	now = now.Add(1500 * time.Millisecond)
	if _, allowed := l.check(key); allowed {
		t.Fatal("1.5s 时仍应被拦截")
	}
	now = now.Add(1 * time.Second)
	if _, allowed := l.check(key); !allowed {
		t.Fatal("2.5s 后应放行")
	}
	// 成功清除：立即恢复
	l.recordSuccess(key)
	if _, allowed := l.check(key); !allowed {
		t.Fatal("成功清除后应放行")
	}
	// 窗口过期：超过 1 分钟后自动重置
	l.recordFailure(key)
	now = now.Add(time.Minute)
	if _, allowed := l.check(key); !allowed {
		t.Fatal("窗口过期后应放行")
	}
	// 退避封顶 16s：连续刷到 10 次失败（首次重建窗口后 failures 从 1 递增）
	for i := 0; i < 10; i++ {
		l.recordFailure(key)
	}
	if _, allowed := l.check(key); allowed {
		t.Fatal("连续失败后应被拦截")
	}
	if wait, allowed := l.check(key); !allowed && wait > 16*time.Second {
		t.Errorf("退避时长 = %v, 应封顶 16s", wait)
	}
}

// 内存上限：条目超过上限时清理过期窗口，防止无限增长。
func TestLoginLimiterTrimMemory(t *testing.T) {
	now := time.Now()
	l := newLoginLimiter()
	l.maxEntries = 3
	l.now = func() time.Time { return now }

	for i := 0; i < 3; i++ {
		l.recordFailure("ip-a|u" + string(rune('a'+i)))
	}
	// 超过上限再插入：触发 trim，此时窗口未过期且仍达上限 → 整体清空兜底
	l.recordFailure("ip-z|z")
	if len(l.entries) != 1 {
		t.Fatalf("超上限后应清空重建，得到 %d 条", len(l.entries))
	}
}

// ---------------------------------------------------------------------------
// handlers.go：登录限流（HTTP 层）
// ---------------------------------------------------------------------------

func TestLoginRateLimitHTTP(t *testing.T) {
	srv := newTestServer(t)
	// httptest.NewRequest 默认 RemoteAddr = 192.0.2.1:1234，连续请求同源
	for i := 0; i < 5; i++ {
		rr := doReq(t, srv, http.MethodPost, "/api/login", "", map[string]string{
			"username": "admin", "password": "wrong",
		})
		if rr.Code != http.StatusUnauthorized {
			t.Fatalf("第 %d 次错误登录 status = %d, want 401", i+1, rr.Code)
		}
	}
	rr := doReq(t, srv, http.MethodPost, "/api/login", "", map[string]string{
		"username": "admin", "password": "wrong",
	})
	if rr.Code != http.StatusTooManyRequests {
		t.Fatalf("第 6 次错误登录 status = %d, want 429", rr.Code)
	}
	if rr.Header().Get("Retry-After") == "" {
		t.Error("429 响应应带 Retry-After 头")
	}

	// 另一 IP（不同 RemoteAddr）不受影响
	req := httptest.NewRequest(http.MethodPost, "/api/login", strings.NewReader(`{"username":"admin","password":"wrong"}`))
	req.RemoteAddr = "9.9.9.9:1234"
	rec := httptest.NewRecorder()
	srv.routes().ServeHTTP(rec, req)
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("不同 IP 应不受限流影响, status = %d", rec.Code)
	}

	// 同一 IP 即使密码正确也会被退避拦截
	req2 := httptest.NewRequest(http.MethodPost, "/api/login", strings.NewReader(`{"username":"admin","password":"admin123456"}`))
	req2.RemoteAddr = "192.0.2.1:999"
	rec2 := httptest.NewRecorder()
	srv.routes().ServeHTTP(rec2, req2)
	if rec2.Code != http.StatusTooManyRequests {
		t.Fatalf("同一 IP 正确密码仍应被退避拦截, status = %d", rec2.Code)
	}

	// X-Forwarded-For 不可伪造身份：伪造头仍按 RemoteAddr 限流
	req3 := httptest.NewRequest(http.MethodPost, "/api/login", strings.NewReader(`{"username":"admin","password":"admin123456"}`))
	req3.RemoteAddr = "192.0.2.1:999"
	req3.Header.Set("X-Forwarded-For", "203.0.113.7")
	rec3 := httptest.NewRecorder()
	srv.routes().ServeHTTP(rec3, req3)
	if rec3.Code != http.StatusTooManyRequests {
		t.Fatalf("伪造 X-Forwarded-For 不应绕过限流, status = %d", rec3.Code)
	}
}

// 登录成功清除失败计数：失败 4 次后成功 1 次，再失败不会立刻触发退避。
func TestLoginLimiterSuccessResets(t *testing.T) {
	now := time.Now()
	l := newLoginLimiter()
	l.now = func() time.Time { return now }
	key := "1.1.1.1|admin"
	for i := 0; i < 4; i++ {
		l.recordFailure(key)
	}
	l.recordSuccess(key)
	l.recordFailure(key) // 重新计数第 1 次
	if _, allowed := l.check(key); !allowed {
		t.Fatal("成功清除后重新计数，不应被拦截")
	}
}

// ---------------------------------------------------------------------------
// handlers.go：安全响应头
// ---------------------------------------------------------------------------

func TestSecurityHeaders(t *testing.T) {
	srv := newTestServer(t)
	rr := doReq(t, srv, http.MethodGet, "/api/health", "", nil)
	for h, want := range map[string]string{
		"X-Content-Type-Options": "nosniff",
		"X-Frame-Options":        "DENY",
		"Referrer-Policy":        "no-referrer",
	} {
		if got := rr.Header().Get(h); got != want {
			t.Errorf("%s = %q, want %q", h, got, want)
		}
	}
}

// ---------------------------------------------------------------------------
// config.go：UnmaskConfig 按账号名还原（排序错位不串号）
// ---------------------------------------------------------------------------

func TestUnmaskConfigByNameNotIndex(t *testing.T) {
	old := DefaultConfig()
	old.Accounts = []Account{
		{Name: "A", URL: "https://a.com", Cookie: "cookie-a"},
		{Name: "B", URL: "https://b.com", Cookie: "cookie-b"},
	}
	in := DefaultConfig()
	// 前端把 B 排到 A 前面，且都带占位符：必须按名字还原，不能按下标
	in.Accounts = []Account{
		{Name: "B", URL: "https://b.com", Cookie: MaskPlaceholder},
		{Name: "A", URL: "https://a.com", Cookie: MaskPlaceholder},
	}
	out, err := UnmaskConfig(&in, &old)
	if err != nil {
		t.Fatalf("UnmaskConfig: %v", err)
	}
	if out.Accounts[0].Cookie != "cookie-b" {
		t.Errorf("B 的 cookie 未按名还原: %q", out.Accounts[0].Cookie)
	}
	if out.Accounts[1].Cookie != "cookie-a" {
		t.Errorf("A 的 cookie 未按名还原: %q", out.Accounts[1].Cookie)
	}
	// 旧配置不存在的账号名（改名场景）：必须报错，而不是把 "***" 字面量落库 ——
	// 那会让界面继续显示「已设置」而签到拿占位符当凭据用，属于静默数据损坏
	in.Accounts = append(in.Accounts, Account{Name: "C", URL: "https://c.com", Cookie: MaskPlaceholder})
	if _, err := UnmaskConfig(&in, &old); err == nil {
		t.Error("旧配置不存在的账号占位符应报错")
	}
}

// ---------------------------------------------------------------------------
// proxies.go：刷新/测速互斥
// ---------------------------------------------------------------------------

func TestRefreshSpeedTestMutex(t *testing.T) {
	srv := newTestServer(t)
	if !srv.proxies.beginRun("fetching") {
		t.Fatal("首次 beginRun 应成功")
	}
	if !srv.proxies.IsRunning() {
		t.Error("beginRun 后 IsRunning 应为 true")
	}
	// 互斥期间刷新/测速均直接拒绝（不会发起网络请求）
	if _, err := srv.proxies.RefreshProxies(ProxyPool{}, 0); err == nil {
		t.Error("互斥期间 RefreshProxies 应报错")
	}
	if _, err := srv.proxies.SpeedTest(context.Background(), nil, 5, ""); err == nil {
		t.Error("互斥期间 SpeedTest 应报错")
	}
	srv.proxies.endRun("")
	if srv.proxies.IsRunning() {
		t.Error("endRun 后 IsRunning 应为 false")
	}
	// 释放后可再次进入（测速侧）
	if !srv.proxies.beginRun("speedtest") {
		t.Fatal("释放后 beginRun 应成功")
	}
	srv.proxies.endRun("")
}

// HTTP 层：刷新/测速互斥时返回 409。
func TestSpeedTestRefreshConflictHTTP(t *testing.T) {
	srv := newTestServer(t)
	token := loginToken(t, srv)
	srv.proxies.beginRun("fetching")
	rr := doReq(t, srv, http.MethodPost, "/api/proxies/speedtest", token, map[string]any{"proxies": []string{}})
	if rr.Code != http.StatusConflict {
		t.Fatalf("互斥期间 speedtest status = %d, want 409", rr.Code)
	}
	rr = doReq(t, srv, http.MethodPost, "/api/proxies/refresh", token, nil)
	if rr.Code != http.StatusConflict {
		t.Fatalf("互斥期间 refresh status = %d, want 409", rr.Code)
	}
	srv.proxies.endRun("")
}

// ---------------------------------------------------------------------------
// proxies.go/handlers.go：测速独立 context 与 last_run 空值
// ---------------------------------------------------------------------------

// 测速后台使用独立 120 秒 context（不随 HTTP 请求结束/取消而中断）。
func TestSpeedTestBackgroundTimeout(t *testing.T) {
	if speedTestBackgroundTimeout != 120*time.Second {
		t.Errorf("speedTestBackgroundTimeout = %v, want 120s", speedTestBackgroundTimeout)
	}
}

// 代理池从未运行：last_run 返回空串，不暴露 Go 时间零点；运行后返回合法 RFC3339。
func TestProxyManagerLastRunZero(t *testing.T) {
	srv := newTestServer(t)
	if got := srv.proxies.LastRunRFC3339(); got != "" {
		t.Errorf("未运行时 last_run = %q, want 空串", got)
	}
	if srv.proxies.LastRun().IsZero() == false {
		t.Error("未运行时 LastRun() 应为零值")
	}
	srv.proxies.beginRun("fetching")
	srv.proxies.endRun("")
	if got := srv.proxies.LastRunRFC3339(); got == "" {
		t.Error("运行完成后 last_run 应为非空 RFC3339")
	} else if _, err := time.Parse(time.RFC3339, got); err != nil {
		t.Errorf("last_run 不是合法 RFC3339: %q", got)
	}
}
