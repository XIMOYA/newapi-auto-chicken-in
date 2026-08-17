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

func TestCheckGithubCookieGetsCodeWithoutCallback(t *testing.T) {
	var callbackCalls atomic.Int32
	site := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/api/oauth/github" {
			callbackCalls.Add(1)
			http.Error(w, "callback must not be called", http.StatusInternalServerError)
			return
		}
		if r.URL.Path != cookieTestOAuthStatePath {
			http.NotFound(w, r)
			return
		}
		if r.Method != http.MethodPost {
			t.Fatalf("OAuth state request = %s %s", r.Method, r.URL.String())
		}
		var payload map[string]string
		if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
			t.Fatalf("decode OAuth state payload: %v", err)
		}
		if payload["provider"] != "github" || payload["intent"] != "login" {
			t.Fatalf("OAuth state payload = %#v", payload)
		}
		// 站点实测结构：state 在 data.flow_token
		_ = json.NewEncoder(w).Encode(map[string]any{
			"success": true,
			"data":    map[string]any{"flow_token": "state-123", "expires_at": 1786906774},
		})
	}))
	defer site.Close()

	authorize := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Query().Get("client_id") != "configured-client-id" {
			t.Fatalf("client_id = %q", r.URL.Query().Get("client_id"))
		}
		if r.URL.Query().Get("state") != "state-123" {
			t.Fatalf("state = %q", r.URL.Query().Get("state"))
		}
		if r.URL.Query().Get("scope") != "user:email" {
			t.Fatalf("scope = %q", r.URL.Query().Get("scope"))
		}
		if got := r.Header.Get("Cookie"); got != "user_session=github-session; __Host-user_session_same_site=github-session; logged_in=yes" {
			t.Fatalf("cookie = %q", got)
		}
		w.Header().Set("Location", "https://example.test/oauth/callback?code=secret-code&state=state-123")
		w.WriteHeader(http.StatusFound)
	}))
	defer authorize.Close()

	result := checkGithubCookieWithAuthorizeURL(context.Background(), HTTPConfig{Timeout: 5, Verify: true}, Account{
		Name:              "github",
		URL:               site.URL,
		GithubClientID:    "configured-client-id",
		GithubUserSession: "github-session",
	}, authorize.URL, "")
	if result.State != cookieTestStateValid {
		t.Fatalf("state = %q, message = %q", result.State, result.Message)
	}
	if strings.Contains(result.Message, "secret-code") || callbackCalls.Load() != 0 {
		t.Fatalf("GitHub code/callback leaked or callback called: message=%q calls=%d", result.Message, callbackCalls.Load())
	}
}

func TestCheckGithubCookieStateWithoutFlowTokenIsAbnormal(t *testing.T) {
	site := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]any{
			"success": true,
			"data":    map[string]any{"expires_at": 1786906774},
		})
	}))
	defer site.Close()
	authorize := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		t.Fatal("authorize 不应在 state 缺失时被调用")
	}))
	defer authorize.Close()

	result := checkGithubCookieWithAuthorizeURL(context.Background(), HTTPConfig{Timeout: 5, Verify: true}, Account{
		Name:              "github-no-token",
		URL:               site.URL,
		GithubClientID:    "configured-client-id",
		GithubUserSession: "github-session",
	}, authorize.URL, "")
	if result.State != cookieTestStateAbnormal || !strings.Contains(result.Message, "flow_token") {
		t.Fatalf("state = %q, message = %q", result.State, result.Message)
	}
}

func TestCheckGithubCookieInvalidRedirect(t *testing.T) {
	site := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]any{
			"success": true,
			"data":    map[string]any{"flow_token": "state-123"},
		})
	}))
	defer site.Close()
	authorize := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Location", "https://github.com/login?return_to=%2Foauth")
		w.WriteHeader(http.StatusFound)
	}))
	defer authorize.Close()

	result := checkGithubCookieWithAuthorizeURL(context.Background(), HTTPConfig{Timeout: 5, Verify: true}, Account{
		Name:              "github-invalid",
		URL:               site.URL,
		GithubClientID:    "configured-client-id",
		GithubUserSession: "dead-session",
	}, authorize.URL, "")
	if result.State != cookieTestStateInvalid {
		t.Fatalf("state = %q, message = %q", result.State, result.Message)
	}
}

func TestCheckGithubCookieLoadsClientIDFromSiteStatus(t *testing.T) {
	var stateCalls atomic.Int32
	var statusCalls atomic.Int32
	site := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case cookieTestOAuthStatePath:
			stateCalls.Add(1)
			if r.Method != http.MethodPost {
				t.Fatalf("state method = %s", r.Method)
			}
			if got := r.Header.Get("Content-Type"); got != "application/json" {
				t.Fatalf("state content type = %q", got)
			}
			_ = json.NewEncoder(w).Encode(map[string]any{
				"success": true,
				"data":    map[string]any{"flow_token": "site-state"},
			})
		case cookieTestStatusPath:
			statusCalls.Add(1)
			if r.Method != http.MethodGet {
				t.Fatalf("status method = %s", r.Method)
			}
			_ = json.NewEncoder(w).Encode(map[string]any{
				"success": true,
				"data":    map[string]any{"github_client_id": "site-client-id"},
			})
		default:
			http.NotFound(w, r)
		}
	}))
	defer site.Close()

	authorize := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Query().Get("client_id") != "site-client-id" {
			t.Fatalf("client_id = %q", r.URL.Query().Get("client_id"))
		}
		if r.URL.Query().Get("state") != "site-state" {
			t.Fatalf("state = %q", r.URL.Query().Get("state"))
		}
		w.Header().Set("Location", "https://example.test/oauth/callback?code=site-code")
		w.WriteHeader(http.StatusFound)
	}))
	defer authorize.Close()

	result := checkGithubCookieWithAuthorizeURL(context.Background(), HTTPConfig{Timeout: 5, Verify: true}, Account{
		Name:              "github-status-client-id",
		URL:               site.URL,
		GithubUserSession: "github-session",
	}, authorize.URL, "")
	if result.State != cookieTestStateValid {
		t.Fatalf("state = %q, message = %q", result.State, result.Message)
	}
	if stateCalls.Load() != 1 || statusCalls.Load() != 1 {
		t.Fatalf("calls = state:%d status:%d", stateCalls.Load(), statusCalls.Load())
	}
}

func TestCheckGithubCookieMissingClientIDIsAbnormal(t *testing.T) {
	site := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case cookieTestOAuthStatePath:
			_ = json.NewEncoder(w).Encode(map[string]any{
				"success": true,
				"data":    map[string]any{"flow_token": "site-state"},
			})
		case cookieTestStatusPath:
			_ = json.NewEncoder(w).Encode(map[string]any{
				"success": true,
				"data":    map[string]any{"github_oauth": true},
			})
		default:
			http.NotFound(w, r)
		}
	}))
	defer site.Close()
	authorize := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		t.Fatal("authorize 不应在缺少 client_id 时被调用")
	}))
	defer authorize.Close()

	result := checkGithubCookieWithAuthorizeURL(context.Background(), HTTPConfig{Timeout: 5, Verify: true}, Account{
		Name:              "github-no-client-id",
		URL:               site.URL,
		GithubUserSession: "github-session",
	}, authorize.URL, "")
	if result.State != cookieTestStateAbnormal || !strings.Contains(result.Message, "github_client_id") {
		t.Fatalf("state = %q, message = %q", result.State, result.Message)
	}
}

func TestCookieTestEndpointsKeepModesSeparate(t *testing.T) {
	srv := newTestServer(t)
	token := loginToken(t, srv)
	cfg := DefaultConfig()
	cfg.Accounts = []Account{
		{Name: "newapi-account", URL: "http://127.0.0.1:1", LoginMethod: LoginMethodNewAPICookie, Enabled: true},
		{Name: "github-account", URL: "http://127.0.0.1:1", LoginMethod: LoginMethodGitHubCookie, Enabled: true},
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
		{"/api/cookie-tests/github", LoginMethodGitHubCookie, "github-account"},
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
