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
	}, authorize.URL, githubFingerprint{})
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

// proxyOf 取出 client 的 Transport 在请求某地址时会用的代理 URL（nil = 直连）。
func proxyOf(t *testing.T, c *http.Client) *url.URL {
	t.Helper()
	tr, ok := c.Transport.(*http.Transport)
	if !ok || tr.Proxy == nil {
		return nil
	}
	req := httptest.NewRequest(http.MethodGet, "https://github.com/login/oauth/authorize", nil)
	u, err := tr.Proxy(req)
	if err != nil {
		t.Fatalf("解析代理出错: %v", err)
	}
	return u
}

// TestNewTabiAIOAuthClient_AlwaysDirect 签发客户端**永远直连**，不吃任何代理配置。
//
// GitHub 的 OAuth 端点对机房 IP 有明显限流（403/429），而带着 user_session 从不断变化
// 的地址出现是触发账号风控最快的方式 —— 最坏把 user_session 直接作废。平台在固定 IP
// 上，在 GitHub 眼里是「常用设备」。所以这里连账号自带的固定代理也要绕开。
func TestNewTabiAIOAuthClient_AlwaysDirect(t *testing.T) {
	httpCfg := HTTPConfig{Timeout: 5, Verify: true}

	// 没配代理：直连（测试环境无 HTTP_PROXY，ProxyFromEnvironment 返回 nil）
	c, err := newTabiAIOAuthClient(Account{URL: "https://a.com"}, httpCfg)
	if err != nil {
		t.Fatalf("构造失败: %v", err)
	}
	if u := proxyOf(t, c); u != nil {
		t.Fatalf("应直连，实际走了 %v", u)
	}

	// 账号配了固定代理：签发仍然直连，account.Proxy 被刻意忽略
	own := "http://9.9.9.9:8080"
	c, err = newTabiAIOAuthClient(Account{URL: "https://a.com", Proxy: &own}, httpCfg)
	if err != nil {
		t.Fatalf("构造失败: %v", err)
	}
	if u := proxyOf(t, c); u != nil {
		t.Fatalf("账号自带代理也该被忽略，实际走了 %v", u)
	}

	// socks5 自带代理同样忽略（免得以为只挡 http 那种）
	socks := "socks5://1.2.3.4:1080"
	c, err = newTabiAIOAuthClient(Account{URL: "https://a.com", Proxy: &socks}, httpCfg)
	if err != nil {
		t.Fatalf("构造失败: %v", err)
	}
	if u := proxyOf(t, c); u != nil {
		t.Fatalf("socks5 自带代理也该被忽略，实际走了 %v", u)
	}
}

// TestIssueRefreshCookieIgnoresAccountProxy 端到端确认签发**不经过**账号自带代理。
// 起一个记录型代理并把账号的 proxy 指向它：只要它收到过任何请求，就说明忽略没生效。
func TestIssueRefreshCookieIgnoresAccountProxy(t *testing.T) {
	var proxied atomic.Int32
	proxy := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		// 走代理时，绝对 URL 或 CONNECT 会打到这里
		if r.Method == http.MethodConnect || strings.HasPrefix(r.RequestURI, "http") {
			proxied.Add(1)
		}
		w.WriteHeader(http.StatusOK)
	}))
	defer proxy.Close()

	own := proxy.URL // http://127.0.0.1:port
	_, _ = issueTabiAIRefreshCookie(context.Background(),
		HTTPConfig{Timeout: 3, Verify: true},
		Account{Name: "tabi", URL: "https://tabi.invalid", GithubUserSession: "gh", Proxy: &own},
		"https://github.com/login/oauth/authorize", githubFingerprint{})
	// 不关心签发成没成（站点是不可达的假域名），只确认那个代理压根没被用到
	if proxied.Load() != 0 {
		t.Fatalf("签发不该经过账号自带代理，实际经过了 %d 次", proxied.Load())
	}
}

// TestIssueCookieForRunningCheckinBypassesLock 签到进程自救时必须放行签到锁。
// 人工签发（网页端 JWT，不带标记）仍要被拦 —— 那是防手滑打断正在跑的那一轮。
func TestIssueCookieForRunningCheckinBypassesLock(t *testing.T) {
	srv := newTestServer(t)
	seedConfig(t, srv, []Account{
		{Name: "tabi", URL: "https://tabi.example.com", LoginMethod: LoginMethodTabiAI,
			Cookie: "new_api_refresh=sid.old", GithubUserSession: "gh-session", Enabled: true},
	}, nil)
	jwt := loginToken(t, srv)
	key := apiKeyToken(t, srv, jwt)

	// 占上签到锁
	if rr := doReq(t, srv, http.MethodPost, "/api/run-state/start", key,
		map[string]any{"source": "test"}); rr.Code != http.StatusOK {
		t.Fatalf("占锁失败 = %d, %s", rr.Code, rr.Body.String())
	}

	// 不带标记（网页端人工点）→ 409 被锁拦住
	locked := doReq(t, srv, http.MethodPost, "/api/tabiai/issue-cookie", jwt,
		map[string]any{"account_name": "tabi"})
	if locked.Code != http.StatusConflict {
		t.Fatalf("人工签发应被签到锁拦住，实际 %d: %s", locked.Code, locked.Body.String())
	}

	// 带标记（签到进程自救）→ 放行，走到 OAuth 阶段才因为站点不可达而 400，
	// 关键是**不再是 409**：说明锁已放行
	bypass := doReq(t, srv, http.MethodPost, "/api/tabiai/issue-cookie", key,
		map[string]any{"account_name": "tabi", "for_running_checkin": true})
	if bypass.Code == http.StatusConflict {
		t.Fatalf("带 for_running_checkin 不该被锁拦住：%s", bypass.Body.String())
	}
}

// TestIssueCookieDoesNotLeakCookieToJWT 明文只回给 API Key 调用方。
//
// 签到客户端（API Key）要拿新凭据立刻重试本轮；网页端走 JWT，签发完刷新页面就行，
// 不下发就少一个泄漏面。这里断言的是那条硬约束：**JWT 响应里不出现 cookie 字段**，
// 它与 OAuth 是否成功无关，所以不必搭假 GitHub。API Key 能拿到明文那一半由客户端侧
// 的 test_success_without_cookie_hints_at_api_key 反向守住（拿不到就报错提示配 Key）。
func TestIssueCookieDoesNotLeakCookieToJWT(t *testing.T) {
	srv := newTestServer(t)
	seedConfig(t, srv, []Account{
		{Name: "tabi", URL: "https://tabi.invalid", LoginMethod: LoginMethodTabiAI,
			Cookie: "new_api_refresh=sid.old", GithubUserSession: "gh-session",
			GithubClientID: "cid", Enabled: true},
	}, nil)
	jwt := loginToken(t, srv)

	withJWT := doReq(t, srv, http.MethodPost, "/api/tabiai/issue-cookie", jwt,
		map[string]any{"account_name": "tabi"})
	if contains(withJWT.Body.String(), `"cookie"`) {
		t.Fatalf("JWT 调用不该拿到明文 cookie：%s", withJWT.Body.String())
	}
}
