package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net"
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

// --------------------------------------------------------------------------- //
// 代次抢救：Set-Cookie 一旦带回新一代，这一发无论被判成什么都不能丢代次、也不能重试
//
// new_api_refresh 是 sid.secret 形态，每 refresh 一次换一代并带重放检测。实测
// （2026-08-23 对 tabitoken.cc）旧代重放的安全窗口只有 20~45 秒：放 20 秒重放仍幂等
// 成功，放 45 秒直接 AUTH_SESSION_REVOKED —— 整条会话撤销、所有账号重新签发，中间
// 没有 AUTH_UNAUTHORIZED 之类的温和过渡。
//
// 而 header 里出现 new_api_refresh 就是「源站确实处理了这一发」的铁证（CDN 挑战页不会
// 下发这个 cookie），它一口气定了两件事：
//   - 新代次必须落到 rotatedCookie，漏一次平台就永远停在旧代，下一轮保活直接报废；
//   - 手里那份旧代快照同时作废，retryable 必须关掉 —— 换一轮代理光建连就要几秒到几十
//     秒，拿旧代重试是在赌整条会话。
// --------------------------------------------------------------------------- //

// tabiaiRotatedSetCookie 站点轮转时下发的 Set-Cookie，属性照抄实测抓包。
const tabiaiRotatedSetCookie = "new_api_refresh=sid.gen2; Path=/api/user/auth; " +
	"Max-Age=2591999; HttpOnly; SameSite=Strict"

// tabiaiRotatedCookie 上面那条 Set-Cookie 归一化后应落库的值。
const tabiaiRotatedCookie = "new_api_refresh=sid.gen2"

// checkTabiAIAgainst 拿假站点跑一次凭据检测，手里的凭据固定是旧代 sid.gen1。
func checkTabiAIAgainst(t *testing.T, handler http.HandlerFunc) CookieTestResult {
	t.Helper()
	site := httptest.NewServer(handler)
	defer site.Close()
	return checkTabiAICookie(context.Background(), HTTPConfig{Timeout: 5, Verify: true}, Account{
		Name: "tabiai", URL: site.URL, Cookie: "new_api_refresh=sid.gen1",
	}, "")
}

func TestCheckTabiAICookieNeverDropsRotatedCookie(t *testing.T) {
	cases := []struct {
		name        string
		wantState   string
		wantMessage string
		handler     http.HandlerFunc
	}{
		{
			// 最要命的一种：CDN 挑战页的特征和源站的 Set-Cookie 同时出现。
			// 矛盾信号下只能信 Set-Cookie —— 挑战页不可能凭空造出一个 new_api_refresh
			name:        "挑战页特征齐全但 header 带回新代次",
			wantState:   cookieTestStateAbnormal,
			wantMessage: "疑似 CDN/WAF 拦截",
			handler: func(w http.ResponseWriter, _ *http.Request) {
				w.Header().Set("Server", "cloudflare")
				w.Header().Set("Cf-Ray", "8f0e1d2c3b4a5678-HKG")
				w.Header().Add("Set-Cookie", tabiaiRotatedSetCookie)
				w.WriteHeader(http.StatusForbidden)
				_, _ = w.Write([]byte("<html><title>Just a moment...</title>__cf_chl</html>"))
			},
		},
		{
			// 站点已经撤销整条会话，但这一发照样消耗了代次。不落库的话人工重新签发前
			// 库里还是旧代，排查时会误以为"平台压根没刷过"
			name:        "401 AUTH_SESSION_REVOKED 仍带回新代次",
			wantState:   cookieTestStateInvalid,
			wantMessage: "会话已被撤销",
			handler: func(w http.ResponseWriter, _ *http.Request) {
				w.Header().Add("Set-Cookie", tabiaiRotatedSetCookie)
				w.WriteHeader(http.StatusUnauthorized)
				_, _ = w.Write([]byte(`{"success":false,"code":"AUTH_SESSION_REVOKED","message":"Unauthorized"}`))
			},
		},
		{
			name:        "401 AUTH_UNAUTHORIZED 仍带回新代次",
			wantState:   cookieTestStateInvalid,
			wantMessage: "凭据已失效",
			handler: func(w http.ResponseWriter, _ *http.Request) {
				w.Header().Add("Set-Cookie", tabiaiRotatedSetCookie)
				w.WriteHeader(http.StatusUnauthorized)
				_, _ = w.Write([]byte(`{"success":false,"code":"AUTH_UNAUTHORIZED","message":"access token 无效"}`))
			},
		},
		{
			name:        "正常成功",
			wantState:   cookieTestStateValid,
			wantMessage: "LIKIQ",
			handler: func(w http.ResponseWriter, _ *http.Request) {
				w.Header().Add("Set-Cookie", tabiaiRotatedSetCookie)
				_ = json.NewEncoder(w).Encode(tabiaiRefreshBody(8259, "LIKIQ"))
			},
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			result := checkTabiAIAgainst(t, tc.handler)
			if result.State != tc.wantState {
				t.Fatalf("state = %q，期望 %q（message = %q）", result.State, tc.wantState, result.Message)
			}
			if !strings.Contains(result.Message, tc.wantMessage) {
				t.Errorf("message = %q，应包含 %q", result.Message, tc.wantMessage)
			}
			if result.rotatedCookie != tabiaiRotatedCookie {
				t.Errorf("新代次被丢掉了：rotatedCookie = %q，期望 %q",
					result.rotatedCookie, tabiaiRotatedCookie)
			}
			if result.retryable {
				t.Error("代次已推进，手里的旧代快照是废纸，绝不能换代理重试")
			}
		})
	}
}

/*
body 读到一半断了，但 header 里的新代次必须捞出来。

这条是原来最隐蔽的漏点：读 body 失败时早退分支直接 return，而 Set-Cookie 明明已经躺在
resp.Header 里了 —— 站点侧代次已推进，平台却因为没落库永远停在旧代。

触发手段是劫持连接手写响应：Content-Length 报 4096 却只发十几个字节就断开。客户端解析完
header（Set-Cookie 到手）再读 body 时必然拿到 unexpected EOF。让 handler 正常写是做不出来
的 —— net/http 会自己把 Content-Length 对齐上。
*/
func TestCheckTabiAICookieBodyReadFailureRescuesRotatedCookie(t *testing.T) {
	result := checkTabiAIAgainst(t, func(w http.ResponseWriter, _ *http.Request) {
		hijacker, ok := w.(http.Hijacker)
		if !ok {
			t.Error("httptest 的 ResponseWriter 应支持 Hijack")
			return
		}
		conn, buf, err := hijacker.Hijack()
		if err != nil {
			t.Errorf("Hijack: %v", err)
			return
		}
		defer conn.Close()
		_, _ = buf.WriteString("HTTP/1.1 200 OK\r\n" +
			"Content-Type: application/json\r\n" +
			"Set-Cookie: " + tabiaiRotatedSetCookie + "\r\n" +
			"Content-Length: 4096\r\n\r\n" +
			`{"success":`)
		_ = buf.Flush()
	})

	if result.State != cookieTestStateAbnormal {
		t.Fatalf("state = %q，期望 %q（message = %q）",
			result.State, cookieTestStateAbnormal, result.Message)
	}
	if !strings.Contains(result.Message, "读取 refresh 响应失败") {
		t.Fatalf("这一发应判成读响应失败，实际 message = %q", result.Message)
	}
	if result.rotatedCookie != tabiaiRotatedCookie {
		t.Errorf("body 读失败也不能丢代次：rotatedCookie = %q，期望 %q",
			result.rotatedCookie, tabiaiRotatedCookie)
	}
	if result.retryable {
		t.Error("header 已经带回新代次，说明源站处理过这一发，不能拿旧代重试")
	}
}

// 请求已经整条写进连接（handler 都跑起来了），响应一个字节都没回就断开。
//
// 站点到底处理没处理无从判断，这种「不可知」绝不能换代理重试：重试用的是本轮开头从库里
// 读出的旧代快照，站点真处理过的话拿它再发一次就是主动触发重放检测。保活 90 分钟一轮，
// 少刷一轮毫无损失，赌一次却要重新签发所有账号。
func TestCheckTabiAICookieMidFlightDisconnectIsNotRetryable(t *testing.T) {
	result := checkTabiAIAgainst(t, func(w http.ResponseWriter, _ *http.Request) {
		conn, _, err := w.(http.Hijacker).Hijack()
		if err != nil {
			t.Errorf("Hijack: %v", err)
			return
		}
		_ = conn.Close()
	})

	if result.State != cookieTestStateAbnormal {
		t.Fatalf("state = %q，期望 %q（message = %q）",
			result.State, cookieTestStateAbnormal, result.Message)
	}
	if !strings.Contains(result.Message, "refresh 网络错误") {
		t.Fatalf("应记成 refresh 网络错误，实际 message = %q", result.Message)
	}
	if result.retryable {
		t.Error("连接中途断开时站点可能已经处理完了，换代理重试等于拿整条会话赌重放窗口")
	}
	if result.rotatedCookie != "" {
		t.Errorf("压根没收到响应头，不该凭空冒出新代次: %q", result.rotatedCookie)
	}
}

// 反过来：拨号阶段就失败（端口 1 没人听）说明请求从未写出，代次绝无可能推进，
// 换代理重试必须保留 —— 代理池里有死地址是常态，砍掉这层容错保活会频繁白跑。
func TestCheckTabiAICookieDialFailureStaysRetryable(t *testing.T) {
	result := checkTabiAICookie(context.Background(), HTTPConfig{Timeout: 5, Verify: true}, Account{
		Name: "dial-fail", URL: "http://127.0.0.1:1", Cookie: "new_api_refresh=sid.gen1",
	}, "")

	if result.State != cookieTestStateAbnormal {
		t.Fatalf("state = %q，期望 %q（message = %q）",
			result.State, cookieTestStateAbnormal, result.Message)
	}
	if !result.retryable {
		t.Fatalf("拨号失败请求从未发出，必须允许换代理重试（message = %q）", result.Message)
	}
	if result.rotatedCookie != "" {
		t.Errorf("连都没连上，不该有新代次: %q", result.rotatedCookie)
	}
}

/*
tabiaiRefreshNeverSent 的分流表。

它决定「client.Do 直接报错（连 header 都没拿到）时能不能换代理重试」，判断从严：只有能
确认请求字节还没写出去的才算安全。误判成安全的代价是整条会话被撤销，误判成危险只是白跑
一轮 90 分钟间隔的保活 —— 这个不对称就是这张表的全部依据。

包装层次照抄 net/http 真实形态（*url.Error 裹 *net.OpError），因为生产代码用的是
errors.As，包少一层就测不到该测的分支。
*/
func TestTabiAIRefreshNeverSentSplitsByStage(t *testing.T) {
	post := func(err error) error {
		return &url.Error{Op: "Post", URL: "https://tabitoken.cc" + cookieTestRefreshPath, Err: err}
	}
	refused := errors.New("connect: connection refused")
	cases := []struct {
		name string
		err  error
		want bool
	}{
		{"nil", nil, false},
		{"DNS 解析失败（裹在 dial 里）", post(&net.OpError{Op: "dial", Net: "tcp",
			Err: &net.DNSError{Err: "no such host", Name: "nope.invalid", IsNotFound: true}}), true},
		{"裸 DNSError", post(&net.DNSError{Err: "server misbehaving", Name: "tabitoken.cc"}), true},
		{"拨号被拒", post(&net.OpError{Op: "dial", Net: "tcp", Err: refused}), true},
		{"连代理失败", post(&net.OpError{Op: "proxyconnect", Net: "tcp", Err: refused}), true},
		// 各平台底层错误类型不统一（Windows connectex / Linux ECONNREFUSED），
		// 类型都没匹配上时靠 net/http 的固定前缀兜底
		{"只有错误串提到 proxyconnect",
			post(errors.New("proxyconnect tcp: dial tcp 10.0.0.1:8080: i/o timeout")), true},
		// 下面这些说明连接已经建立、字节已经在路上，站点可能已经处理完了
		{"读阶段失败", post(&net.OpError{Op: "read", Net: "tcp",
			Err: errors.New("connection reset by peer")}), false},
		{"写阶段失败", post(&net.OpError{Op: "write", Net: "tcp",
			Err: errors.New("broken pipe")}), false},
		{"整体超时", post(context.DeadlineExceeded), false},
		{"多层包装的超时", fmt.Errorf("刷新失败: %w", post(context.DeadlineExceeded)), false},
		{"说不清阶段的普通错误", errors.New("boom"), false},
	}
	for _, tc := range cases {
		if got := tabiaiRefreshNeverSent(tc.err); got != tc.want {
			t.Errorf("%s: tabiaiRefreshNeverSent(%v) = %v，期望 %v", tc.name, tc.err, got, tc.want)
		}
	}
}
