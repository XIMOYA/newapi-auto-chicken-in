/*
server/tabiai_test.go
TaBiAI 凭据维护测试：轮转落盘、回写端点、GitHub OAuth 三步签发。
*/
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

// tabiaiRotatingSite 每次 refresh 下发递增代次，用于验证「新值必须落库」。
func tabiaiRotatingSite(t *testing.T, gen *atomic.Int32) *httptest.Server {
	t.Helper()
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != cookieTestRefreshPath {
			http.NotFound(w, r)
			return
		}
		next := gen.Add(1)
		w.Header().Add("Set-Cookie",
			"new_api_refresh=sid.gen"+string(rune('0'+next))+"; Path=/api/user/auth; HttpOnly")
		_ = json.NewEncoder(w).Encode(tabiaiRefreshBody(int(next), "user"))
	}))
}

// TestRunnerPersistsRotatedTabiAICookie 现存缺陷的直接回归：
// 检测会消耗一代凭据，若不落库，下次就会踩旧代并可能被判重放。
func TestRunnerPersistsRotatedTabiAICookie(t *testing.T) {
	srv := newTestServer(t)
	var gen atomic.Int32
	site := tabiaiRotatingSite(t, &gen)
	defer site.Close()

	cfg := DefaultConfig()
	cfg.HTTP.Timeout = 5
	cfg.Accounts = []Account{{
		Name: "tabi", URL: site.URL, LoginMethod: LoginMethodTabiAI,
		Cookie: "new_api_refresh=sid.gen0", Enabled: true,
	}}
	if _, err := SaveConfig(srv.db, cfg); err != nil {
		t.Fatalf("SaveConfig: %v", err)
	}

	runner := NewCookieTestRunner(nil, srv.db)
	loaded, _, err := LoadConfig(srv.db)
	if err != nil {
		t.Fatalf("LoadConfig: %v", err)
	}
	if err := runner.Start(&loaded, LoginMethodTabiAI, nil); err != nil {
		t.Fatalf("Start: %v", err)
	}
	status := waitRunnerDone(t, runner, 20*time.Second)

	if status.Results[0].State != cookieTestStateValid {
		t.Fatalf("检测应通过: %+v", status.Results[0])
	}
	if !strings.Contains(status.Results[0].Message, "已自动保存") {
		t.Errorf("结果应说明凭据已轮转保存: %q", status.Results[0].Message)
	}
	saved, _, err := LoadConfig(srv.db)
	if err != nil {
		t.Fatalf("LoadConfig: %v", err)
	}
	if saved.Accounts[0].Cookie != "new_api_refresh=sid.gen1" {
		t.Fatalf("轮转后的新凭据未落库，仍是 %q", saved.Accounts[0].Cookie)
	}
}

func TestRunnerPersistsRotatedCookiePerAccount(t *testing.T) {
	srv := newTestServer(t)
	var genA, genB atomic.Int32
	siteA := tabiaiRotatingSite(t, &genA)
	defer siteA.Close()
	siteB := tabiaiRotatingSite(t, &genB)
	defer siteB.Close()

	cfg := DefaultConfig()
	cfg.HTTP.Timeout = 5
	cfg.Accounts = []Account{
		{Name: "a", URL: siteA.URL, LoginMethod: LoginMethodTabiAI, Cookie: "new_api_refresh=a.gen0", Enabled: true},
		{Name: "b", URL: siteB.URL, LoginMethod: LoginMethodTabiAI, Cookie: "new_api_refresh=b.gen0", Enabled: true},
	}
	if _, err := SaveConfig(srv.db, cfg); err != nil {
		t.Fatalf("SaveConfig: %v", err)
	}

	runner := NewCookieTestRunner(nil, srv.db)
	loaded, _, _ := LoadConfig(srv.db)
	if err := runner.Start(&loaded, LoginMethodTabiAI, nil); err != nil {
		t.Fatalf("Start: %v", err)
	}
	waitRunnerDone(t, runner, 20*time.Second)

	saved, _, _ := LoadConfig(srv.db)
	// 两个账号并发检测，各自的新值都要在，不能互相覆盖
	if saved.Accounts[0].Cookie != "new_api_refresh=sid.gen1" {
		t.Errorf("账号 a 的新凭据丢失: %q", saved.Accounts[0].Cookie)
	}
	if saved.Accounts[1].Cookie != "new_api_refresh=sid.gen1" {
		t.Errorf("账号 b 的新凭据丢失: %q", saved.Accounts[1].Cookie)
	}
}

func TestWriteBackRefreshCookieEndpoint(t *testing.T) {
	srv := newTestServer(t)

	cfg := DefaultConfig()
	cfg.Accounts = []Account{
		{Name: "tabi", URL: "https://a.com", LoginMethod: LoginMethodTabiAI, Cookie: "new_api_refresh=old", Enabled: true},
		{Name: "other", URL: "https://b.com", LoginMethod: LoginMethodNewAPICookie, Cookie: "session=keep", Enabled: true},
	}
	if _, err := SaveConfig(srv.db, cfg); err != nil {
		t.Fatalf("SaveConfig: %v", err)
	}
	key := issueTestAPIKey(t, srv)

	resp := doReq(t, srv, http.MethodPost, "/api/accounts/tabi/refresh-cookie", key,
		map[string]any{"cookie": "new_api_refresh=fresh"})
	if resp.Code != http.StatusOK {
		t.Fatalf("回写失败 = %d, %s", resp.Code, resp.Body.String())
	}
	saved, _, _ := LoadConfig(srv.db)
	if saved.Accounts[0].Cookie != "new_api_refresh=fresh" {
		t.Fatalf("目标账号未更新: %q", saved.Accounts[0].Cookie)
	}
	if saved.Accounts[1].Cookie != "session=keep" {
		t.Fatalf("其他账号被误改: %q", saved.Accounts[1].Cookie)
	}

	// 账号不存在 -> 404；占位符 -> 400
	missing := doReq(t, srv, http.MethodPost, "/api/accounts/nope/refresh-cookie", key,
		map[string]any{"cookie": "new_api_refresh=x"})
	if missing.Code != http.StatusNotFound {
		t.Fatalf("不存在的账号应 404，实际 %d", missing.Code)
	}
	placeholder := doReq(t, srv, http.MethodPost, "/api/accounts/tabi/refresh-cookie", key,
		map[string]any{"cookie": MaskPlaceholder})
	if placeholder.Code != http.StatusBadRequest {
		t.Fatalf("占位符应 400，实际 %d", placeholder.Code)
	}

	// 无凭据必须拒绝
	unauth := doReq(t, srv, http.MethodPost, "/api/accounts/tabi/refresh-cookie", "",
		map[string]any{"cookie": "new_api_refresh=y"})
	if unauth.Code == http.StatusOK {
		t.Fatal("回写端点必须鉴权")
	}
}

// TestWriteBackRefreshCookieEncodedName 验证与 Python 客户端的路径编码对接。
// Python 侧用 urllib quote(name, safe="") 拼地址，中文与空格会变成 %XX；
// 这里按同样方式编码后请求，确认 Go 的 PathValue 能还原出原账号名。
func TestWriteBackRefreshCookieEncodedName(t *testing.T) {
	srv := newTestServer(t)

	const name = "我的 站点/A"
	cfg := DefaultConfig()
	cfg.Accounts = []Account{
		{Name: name, URL: "https://a.com", LoginMethod: LoginMethodTabiAI,
			Cookie: "new_api_refresh=old", Enabled: true},
	}
	if _, err := SaveConfig(srv.db, cfg); err != nil {
		t.Fatalf("SaveConfig: %v", err)
	}
	key := issueTestAPIKey(t, srv)

	path := "/api/accounts/" + url.PathEscape(name) + "/refresh-cookie"
	resp := doReq(t, srv, http.MethodPost, path, key,
		map[string]any{"cookie": "new_api_refresh=gen2"})
	if resp.Code != http.StatusOK {
		t.Fatalf("编码账号名回写失败 = %d, %s（path=%s）", resp.Code, resp.Body.String(), path)
	}
	saved, _, _ := LoadConfig(srv.db)
	if saved.Accounts[0].Cookie != "new_api_refresh=gen2" {
		t.Fatalf("账号未更新: %q", saved.Accounts[0].Cookie)
	}
}

// issueTestAPIKey 造一个可用的 API Key 明文（回写端点用 API Key 鉴权）。
func issueTestAPIKey(t *testing.T, srv *Server) string {
	t.Helper()
	plain, hash, prefix, err := GenerateAPIKey()
	if err != nil {
		t.Fatalf("GenerateAPIKey: %v", err)
	}
	if _, err := CreateAPIKey(srv.db, "tabiai-writeback", hash, prefix); err != nil {
		t.Fatalf("CreateAPIKey: %v", err)
	}
	return plain
}

func TestIssueTabiAIRefreshCookieThreeSteps(t *testing.T) {
	var stateCalls, statusCalls, callbackCalls atomic.Int32
	var site *httptest.Server
	site = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case tabiaiOAuthStatePath:
			stateCalls.Add(1)
			if r.Method != http.MethodPost {
				t.Fatalf("state method = %s", r.Method)
			}
			_ = json.NewEncoder(w).Encode(map[string]any{
				"success": true,
				"data":    map[string]any{"flow_token": "flow-1", "expires_at": 1786906774},
			})
		case tabiaiStatusPath:
			statusCalls.Add(1)
			_ = json.NewEncoder(w).Encode(map[string]any{
				"success": true,
				"data":    map[string]any{"github_client_id": "site-client"},
			})
		case tabiaiOAuthCallbackPath:
			callbackCalls.Add(1)
			if got := r.URL.Query().Get("code"); got != "code-1" {
				t.Fatalf("callback code = %q", got)
			}
			if got := r.URL.Query().Get("state"); got != "flow-1" {
				t.Fatalf("callback state = %q", got)
			}
			w.Header().Add("Set-Cookie",
				"new_api_refresh=newsid.newsecret; Path=/api/user/auth; HttpOnly")
			_ = json.NewEncoder(w).Encode(map[string]any{"success": true, "data": map[string]any{"id": 1}})
		default:
			http.NotFound(w, r)
		}
	}))
	defer site.Close()

	authorize := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if got := r.URL.Query().Get("client_id"); got != "site-client" {
			t.Fatalf("client_id = %q", got)
		}
		if got := r.URL.Query().Get("state"); got != "flow-1" {
			t.Fatalf("state = %q", got)
		}
		if !strings.Contains(r.Header.Get("Cookie"), "user_session=gh-session") {
			t.Fatalf("未带 GitHub user_session: %q", r.Header.Get("Cookie"))
		}
		w.Header().Set("Location", site.URL+"/oauth/github?code=code-1&state=flow-1")
		w.WriteHeader(http.StatusFound)
	}))
	defer authorize.Close()

	cookie, err := issueTabiAIRefreshCookie(context.Background(), HTTPConfig{Timeout: 5, Verify: true}, Account{
		Name: "tabi", URL: site.URL, GithubUserSession: "gh-session",
	}, authorize.URL)
	if err != nil {
		t.Fatalf("签发失败: %v", err)
	}
	if cookie != "new_api_refresh=newsid.newsecret" {
		t.Fatalf("签发到的 cookie = %q", cookie)
	}
	if stateCalls.Load() != 1 || statusCalls.Load() != 1 || callbackCalls.Load() != 1 {
		t.Fatalf("三步调用次数异常: state=%d status=%d callback=%d",
			stateCalls.Load(), statusCalls.Load(), callbackCalls.Load())
	}
}

func TestIssueTabiAICookieRequiresGithubSession(t *testing.T) {
	srv := newTestServer(t)
	token := loginToken(t, srv)
	cfg := DefaultConfig()
	cfg.Accounts = []Account{{
		Name: "tabi", URL: "https://a.com", LoginMethod: LoginMethodTabiAI, Enabled: true,
	}}
	if _, err := SaveConfig(srv.db, cfg); err != nil {
		t.Fatalf("SaveConfig: %v", err)
	}

	resp := doReq(t, srv, http.MethodPost, "/api/tabiai/issue-cookie", token,
		map[string]any{"account_name": "tabi"})
	if resp.Code != http.StatusBadRequest {
		t.Fatalf("缺 user_session 应 400，实际 %d, %s", resp.Code, resp.Body.String())
	}
	if !strings.Contains(resp.Body.String(), "user_session") {
		t.Errorf("错误信息应指明缺少 user_session: %s", resp.Body.String())
	}

	missing := doReq(t, srv, http.MethodPost, "/api/tabiai/issue-cookie", token,
		map[string]any{"account_name": "nope"})
	if missing.Code != http.StatusNotFound {
		t.Fatalf("不存在的账号应 404，实际 %d", missing.Code)
	}
}

func TestExtractGithubAuthorizeCodeErrors(t *testing.T) {
	if _, err := extractGithubAuthorizeCode(302, "https://github.com/login?return_to=x"); err == nil ||
		!strings.Contains(err.Error(), "user_session 已失效") {
		t.Fatalf("跳登录页应判为 session 失效: %v", err)
	}
	if _, err := extractGithubAuthorizeCode(200, ""); err == nil ||
		!strings.Contains(err.Error(), "授权该 OAuth 应用") {
		t.Fatalf("200 应提示需要授权应用: %v", err)
	}
	if _, err := extractGithubAuthorizeCode(403, ""); err == nil ||
		!strings.Contains(err.Error(), "被 GitHub 限制") {
		t.Fatalf("403 应提示出口被限制: %v", err)
	}
	code, err := extractGithubAuthorizeCode(302, "https://site/oauth/github?code=abc&state=s")
	if err != nil || code != "abc" {
		t.Fatalf("正常 302 应取到 code: %q %v", code, err)
	}
}
