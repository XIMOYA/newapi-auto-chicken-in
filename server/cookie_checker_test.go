package main

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func TestCheckNewAPICookieDirectValid(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != cookieTestSelfPath {
			t.Fatalf("path = %s, want %s", r.URL.Path, cookieTestSelfPath)
		}
		if got := r.Header.Get("Cookie"); got != "session=good" {
			t.Fatalf("cookie = %q", got)
		}
		_ = json.NewEncoder(w).Encode(map[string]any{
			"success": true,
			"data":    map[string]any{"id": 123, "username": "tester"},
		})
	}))
	defer server.Close()

	result := checkNewAPICookie(context.Background(), HTTPConfig{Timeout: 5, Verify: true}, Account{
		Name:   "direct",
		URL:    server.URL,
		Cookie: "session=good",
	}, "")
	if result.State != cookieTestStateValid {
		t.Fatalf("state = %q, message = %q", result.State, result.Message)
	}
	if result.UserID == nil || *result.UserID != 123 {
		t.Fatalf("user_id = %v", result.UserID)
	}
	if result.Message != "tester" {
		t.Fatalf("message = %q", result.Message)
	}
}

func TestCheckNewAPICookieUnauthorized(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusUnauthorized)
		_, _ = w.Write([]byte(`{"success":false,"message":"unauthorized"}`))
	}))
	defer server.Close()

	result := checkNewAPICookie(context.Background(), HTTPConfig{Timeout: 5, Verify: true}, Account{
		Name:   "invalid",
		URL:    server.URL,
		Cookie: "session=dead",
	}, "")
	if result.State != cookieTestStateInvalid {
		t.Fatalf("state = %q, message = %q", result.State, result.Message)
	}
}

func TestCheckNewAPICookieRefreshPreservesOriginalCookie(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case cookieTestRefreshPath:
			if r.Method != http.MethodPost {
				t.Fatalf("refresh method = %s", r.Method)
			}
			if got := r.Header.Get("Cookie"); got != "new_api_refresh=refresh-token" {
				t.Fatalf("refresh cookie = %q", got)
			}
			_ = json.NewEncoder(w).Encode(map[string]any{
				"success": true,
				"data": map[string]any{
					"access_token": "access-token",
					"token_type":   "Bearer",
					"user":         map[string]any{"id": 456},
				},
			})
		case cookieTestSelfPath:
			if got := r.Header.Get("Authorization"); got != "Bearer access-token" {
				t.Fatalf("authorization = %q", got)
			}
			if got := r.Header.Get("Cookie"); got != "new_api_refresh=refresh-token" {
				t.Fatalf("self cookie = %q", got)
			}
			if strings.Contains(r.Header.Get("Cookie"), "access-token") {
				t.Fatal("access token must not be rewritten into Cookie")
			}
			_ = json.NewEncoder(w).Encode(map[string]any{
				"success": true,
				"data":    map[string]any{"id": 456, "username": "refresh-user"},
			})
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()

	result := checkNewAPICookie(context.Background(), HTTPConfig{Timeout: 5, Verify: true}, Account{
		Name:   "refresh",
		URL:    server.URL,
		Cookie: "new_api_refresh=refresh-token",
	}, "")
	if result.State != cookieTestStateValid {
		t.Fatalf("state = %q, message = %q", result.State, result.Message)
	}
	if result.UserID == nil || *result.UserID != 456 {
		t.Fatalf("user_id = %v", result.UserID)
	}
}

func TestCheckNewAPICookieAbnormalResponses(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusBadGateway)
		_, _ = w.Write([]byte("upstream failure"))
	}))
	defer server.Close()

	result := checkNewAPICookie(context.Background(), HTTPConfig{Timeout: 5, Verify: true}, Account{
		Name:   "abnormal",
		URL:    server.URL,
		Cookie: "session=server-error",
	}, "")
	if result.State != cookieTestStateAbnormal {
		t.Fatalf("state = %q, message = %q", result.State, result.Message)
	}
	if strings.Contains(result.Message, "upstream failure") {
		t.Fatalf("response body leaked into message: %q", result.Message)
	}
}

// tabiaiRefreshBody 站点 refresh 成功时的响应体（结构见 docs/签到原理.md 2.2）。
func tabiaiRefreshBody(userID int, username string) map[string]any {
	return map[string]any{
		"success": true,
		"data": map[string]any{
			"access_token":      "eyJhbGciOiJIUzI1NiJ9.fake",
			"access_expires_at": 1786976078,
			"user":              map[string]any{"id": userID, "username": username},
		},
	}
}

func TestCheckTabiAICookieValidCarriesRotatedCookie(t *testing.T) {
	var selfCalls atomic.Int32
	site := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == cookieTestSelfPath {
			// refresh 响应已带 user，检测不该再多打一次 self
			selfCalls.Add(1)
			http.Error(w, "self must not be called", http.StatusInternalServerError)
			return
		}
		if r.URL.Path != cookieTestRefreshPath {
			http.NotFound(w, r)
			return
		}
		if r.Method != http.MethodPost {
			t.Fatalf("refresh method = %s", r.Method)
		}
		if got := r.Header.Get("Cookie"); got != "new_api_refresh=sid.gen1" {
			t.Fatalf("refresh cookie = %q", got)
		}
		// 轮转：下发下一代 secret
		w.Header().Add("Set-Cookie",
			"new_api_refresh=sid.gen2; Path=/api/user/auth; Max-Age=2591999; HttpOnly; SameSite=Strict")
		_ = json.NewEncoder(w).Encode(tabiaiRefreshBody(8259, "LIKIQ"))
	}))
	defer site.Close()

	result := checkTabiAICookie(context.Background(), HTTPConfig{Timeout: 5, Verify: true}, Account{
		Name:   "tabiai",
		URL:    site.URL,
		Cookie: "new_api_refresh=sid.gen1",
	}, "")
	if result.State != cookieTestStateValid {
		t.Fatalf("state = %q, message = %q", result.State, result.Message)
	}
	if result.Message != "LIKIQ" {
		t.Fatalf("应展示用户名: %q", result.Message)
	}
	if result.UserID == nil || *result.UserID != 8259 {
		t.Fatalf("user_id = %v", result.UserID)
	}
	if result.rotatedCookie != "new_api_refresh=sid.gen2" {
		t.Fatalf("未带出轮转后的新凭据: %q", result.rotatedCookie)
	}
	if selfCalls.Load() != 0 {
		t.Fatalf("不应额外请求 self，实际 %d 次", selfCalls.Load())
	}
}

func TestCheckTabiAICookieAcceptsBareValue(t *testing.T) {
	site := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		// 用户只填裸 sid.secret 时也要补成合法 cookie 头
		if got := r.Header.Get("Cookie"); got != "new_api_refresh=sid.bare" {
			t.Fatalf("refresh cookie = %q", got)
		}
		_ = json.NewEncoder(w).Encode(tabiaiRefreshBody(1, "bare"))
	}))
	defer site.Close()

	result := checkTabiAICookie(context.Background(), HTTPConfig{Timeout: 5, Verify: true}, Account{
		Name:   "bare",
		URL:    site.URL,
		Cookie: "sid.bare",
	}, "")
	if result.State != cookieTestStateValid {
		t.Fatalf("state = %q, message = %q", result.State, result.Message)
	}
}

func TestCheckTabiAICookieSessionRevokedIsInvalid(t *testing.T) {
	site := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusUnauthorized)
		_, _ = w.Write([]byte(`{"code":"AUTH_SESSION_REVOKED","message":"Unauthorized","success":false}`))
	}))
	defer site.Close()

	result := checkTabiAICookie(context.Background(), HTTPConfig{Timeout: 5, Verify: true}, Account{
		Name: "revoked", URL: site.URL, Cookie: "new_api_refresh=sid.old",
	}, "")
	if result.State != cookieTestStateInvalid {
		t.Fatalf("state = %q", result.State)
	}
	if !strings.Contains(result.Message, "会话已被撤销") {
		t.Fatalf("应指明会话被撤销: %q", result.Message)
	}
	if result.retryable {
		t.Fatal("凭据问题不该换代理重试")
	}
}

func TestCheckTabiAICookieUnauthorizedIsInvalid(t *testing.T) {
	site := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusUnauthorized)
		_, _ = w.Write([]byte(`{"code":"AUTH_UNAUTHORIZED","message":"access token 无效","success":false}`))
	}))
	defer site.Close()

	result := checkTabiAICookie(context.Background(), HTTPConfig{Timeout: 5, Verify: true}, Account{
		Name: "expired", URL: site.URL, Cookie: "new_api_refresh=sid.x",
	}, "")
	if result.State != cookieTestStateInvalid || !strings.Contains(result.Message, "凭据已失效") {
		t.Fatalf("state = %q, message = %q", result.State, result.Message)
	}
}

func TestCheckTabiAICookieChallengeIsRetryable(t *testing.T) {
	site := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Server", "cloudflare")
		w.Header().Set("Cf-Ray", "ray")
		w.WriteHeader(http.StatusForbidden)
		_, _ = w.Write([]byte("<html><title>Just a moment...</title>__cf_chl</html>"))
	}))
	defer site.Close()

	result := checkTabiAICookie(context.Background(), HTTPConfig{Timeout: 5, Verify: true}, Account{
		Name: "blocked", URL: site.URL, Cookie: "new_api_refresh=sid.x",
	}, "")
	if !result.retryable || result.State != cookieTestStateAbnormal {
		t.Fatalf("CDN 拦截应判为可重试的链路问题: state=%q retryable=%v", result.State, result.retryable)
	}
}

func TestCheckTabiAICookieMissingAccessTokenIsAbnormal(t *testing.T) {
	site := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]any{
			"success": true,
			"data":    map[string]any{"user": map[string]any{"id": 1}},
		})
	}))
	defer site.Close()

	result := checkTabiAICookie(context.Background(), HTTPConfig{Timeout: 5, Verify: true}, Account{
		Name: "no-token", URL: site.URL, Cookie: "new_api_refresh=sid.x",
	}, "")
	if result.State != cookieTestStateAbnormal || !strings.Contains(result.Message, "access_token") {
		t.Fatalf("state = %q, message = %q", result.State, result.Message)
	}
}

func TestCookieTestEndpointsKeepModesSeparate(t *testing.T) {
	srv := newTestServer(t)
	token := loginToken(t, srv)
	cfg := DefaultConfig()
	cfg.Accounts = []Account{
		{Name: "newapi-account", URL: "http://127.0.0.1:1", LoginMethod: LoginMethodNewAPICookie, Enabled: true},
		{Name: "tabiai-account", URL: "http://127.0.0.1:1", LoginMethod: LoginMethodTabiAI, Enabled: true},
	}
	if _, err := SaveConfig(srv.db, cfg); err != nil {
		t.Fatalf("SaveConfig: %v", err)
	}

	// 两个模式各跑一次：启动 -> 等任务落地 -> 取快照，断言结果集互不串台
	for _, tc := range []struct {
		path string
		mode string
		name string
	}{
		{"/api/cookie-tests/newapi", LoginMethodNewAPICookie, "newapi-account"},
		{"/api/cookie-tests/tabiai", LoginMethodTabiAI, "tabiai-account"},
	} {
		start := doReq(t, srv, http.MethodPost, tc.path, token, map[string]any{"account_names": []string{}})
		if start.Code != http.StatusOK {
			t.Fatalf("%s status = %d, body = %s", tc.path, start.Code, start.Body.String())
		}
		status := waitCookieTestDone(t, srv, token)
		if status.Mode != tc.mode || len(status.Results) != 1 || status.Results[0].Name != tc.name {
			t.Fatalf("%s 结果未隔离: %+v", tc.path, status)
		}
	}
}

// waitCookieTestDone 轮询状态接口直到任务结束（两个账号都指向不可用端口，很快定论）。
func waitCookieTestDone(t *testing.T, srv *Server, token string) CookieTestStatus {
	t.Helper()
	deadline := time.Now().Add(20 * time.Second)
	for {
		resp := doReq(t, srv, http.MethodGet, "/api/cookie-tests/status", token, nil)
		if resp.Code != http.StatusOK {
			t.Fatalf("status 接口 = %d, body = %s", resp.Code, resp.Body.String())
		}
		var snapshot CookieTestStatus
		decodeJSON(t, resp, &snapshot)
		if !snapshot.Running {
			return snapshot
		}
		if time.Now().After(deadline) {
			// 连接不可用端口本应秒级定论；超时说明重试没有收敛，主动停止避免测试悬挂
			doReq(t, srv, http.MethodPost, "/api/cookie-tests/stop", token, nil)
			t.Fatalf("等待检测结束超时: %+v", snapshot)
		}
		time.Sleep(200 * time.Millisecond)
	}
}

func TestCookieTestStartRejectsConcurrentRun(t *testing.T) {
	srv := newTestServer(t)
	token := loginToken(t, srv)
	cfg := DefaultConfig()
	// 指向不可路由地址，让首个任务停在代理类重试上，从而稳定处于 running
	cfg.Accounts = []Account{
		{Name: "slow", URL: "http://192.0.2.1:9", LoginMethod: LoginMethodNewAPICookie, Cookie: "session=x", Enabled: true},
	}
	cfg.HTTP.Timeout = 1
	if _, err := SaveConfig(srv.db, cfg); err != nil {
		t.Fatalf("SaveConfig: %v", err)
	}
	defer srv.cookieTests.Stop()

	first := doReq(t, srv, http.MethodPost, "/api/cookie-tests/newapi", token, map[string]any{})
	if first.Code != http.StatusOK {
		t.Fatalf("首次启动 = %d, body = %s", first.Code, first.Body.String())
	}
	second := doReq(t, srv, http.MethodPost, "/api/cookie-tests/newapi", token, map[string]any{})
	if second.Code != http.StatusConflict {
		t.Fatalf("并发启动应返回 409，实际 = %d, body = %s", second.Code, second.Body.String())
	}

	stop := doReq(t, srv, http.MethodPost, "/api/cookie-tests/stop", token, nil)
	if stop.Code != http.StatusOK {
		t.Fatalf("stop = %d", stop.Code)
	}
	status := waitCookieTestDone(t, srv, token)
	if !status.Stopped || status.Results[0].State != cookieTestStateSkipped {
		t.Fatalf("停止后应写成 skipped: %+v", status.Results)
	}
	if !strings.Contains(status.Results[0].Message, "已手动停止") {
		t.Fatalf("停止说明缺失: %q", status.Results[0].Message)
	}
}

func TestCookieTestBaseURLRejectsNonHTTP(t *testing.T) {
	if _, err := cookieTestBaseURL("ftp://example.com"); err == nil {
		t.Fatal("ftp URL should be rejected")
	}
	if _, err := cookieTestBaseURL("https://example.com/path/"); err != nil {
		t.Fatalf("valid URL rejected: %v", err)
	}
	if _, err := url.Parse("https://example.com"); err != nil {
		t.Fatal(err)
	}
}
