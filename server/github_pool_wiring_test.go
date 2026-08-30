/*
server/github_pool_wiring_test.go
统一 GitHub 账号池到签发链路的接线测试。

为什么单独一个文件：池子那几个 commit 只建了数据结构和端点，
resolveAccountSession 一度在生产代码里零调用点 —— 凭据存进池子、签发却仍只读
账号自带的旧字段，表现为「界面上填了、签发还说没填」。这里守的就是那条接线：

  - 账号自己没填、引用的池子里有 → 签发链路真的把池子那份 session 发给 GitHub
  - 两处都没有 → 报错要说清「池子里也没有」，否则用户会一直去翻账号表单
  - 旧配置（只有账号字段、没建池子）行为一字不变，迁移期不断供
  - 解析只产生副本，绝不把结果写回配置
*/
package main

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestEffectiveGitHubCredentialsPrefersPoolWithoutMutating(t *testing.T) {
	cfg := &Config{
		GitHubAccounts: []GitHubAccount{{Name: "Steven", UserSession: "pool-sess", ClientID: "pool-cid"}},
	}
	account := Account{
		Name: "Steven（a.com）", URL: "https://a.com", GitHubAccount: "Steven",
		GithubUserSession: "old-sess", GithubClientID: "old-cid",
	}

	got := effectiveGitHubCredentials(cfg, account)
	if got.GithubUserSession != "pool-sess" || got.GithubClientID != "pool-cid" {
		t.Fatalf("应取池子的凭据，实际 %q/%q", got.GithubUserSession, got.GithubClientID)
	}
	// 其余字段照抄，副本要能直接交给签发链路用
	if got.Name != account.Name || got.URL != account.URL {
		t.Errorf("副本丢了账号本身的字段: %+v", got)
	}
	// 原账号不能被改 —— 写回去等于悄悄迁移数据，用户下次开界面会发现
	// 账号里凭空多出一份凭据，池子也就白建了
	if account.GithubUserSession != "old-sess" || account.GithubClientID != "old-cid" {
		t.Fatalf("原账号被污染: %q/%q", account.GithubUserSession, account.GithubClientID)
	}

	// 没引用池子的老账号：原样回落自己的字段
	legacy := effectiveGitHubCredentials(cfg, Account{
		Name: "老账号", GithubUserSession: "old-sess", GithubClientID: "old-cid"})
	if legacy.GithubUserSession != "old-sess" || legacy.GithubClientID != "old-cid" {
		t.Fatalf("老配置应回落自身字段，实际 %q/%q",
			legacy.GithubUserSession, legacy.GithubClientID)
	}
}

// poolOAuthSite 造一个能跑完三步 OAuth 的假站点。
// wantClientID 是断言用的：站点的 /api/status 会返回它，authorize 那边核对。
func poolOAuthSite(t *testing.T) *httptest.Server {
	t.Helper()
	var site *httptest.Server
	site = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case tabiaiOAuthStatePath:
			_ = json.NewEncoder(w).Encode(map[string]any{
				"success": true,
				"data":    map[string]any{"flow_token": "flow-pool"},
			})
		case tabiaiStatusPath:
			// 池子提供了 client_id 时不该走到这里；留着是为了让「没提供」的场景也能跑
			_ = json.NewEncoder(w).Encode(map[string]any{
				"success": true,
				"data":    map[string]any{"github_client_id": "site-fallback-cid"},
			})
		case tabiaiOAuthCallbackPath:
			w.Header().Add("Set-Cookie",
				"new_api_refresh=poolsid.poolsecret; Path=/api/user/auth; HttpOnly")
			_ = json.NewEncoder(w).Encode(map[string]any{"success": true})
		default:
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(site.Close)
	return site
}

func TestIssueUsesPoolSessionAndClientID(t *testing.T) {
	// 端到端：账号自身两个字段全空，凭据只存在池子里。
	// 断言点在 authorize 这一跳 —— 那是真正发给 GitHub 的请求，
	// 它带的 Cookie 就是签发实际使用的 session，骗不了人。
	site := poolOAuthSite(t)

	var sawSession, sawClientID string
	authorize := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		sawSession = r.Header.Get("Cookie")
		sawClientID = r.URL.Query().Get("client_id")
		w.Header().Set("Location", site.URL+"/oauth/github?code=code-pool&state=flow-pool")
		w.WriteHeader(http.StatusFound)
	}))
	defer authorize.Close()

	cfg := &Config{
		GitHubAccounts: []GitHubAccount{
			{Name: "Steven", UserSession: "pool-sess", ClientID: "pool-cid"},
		},
	}
	account := Account{
		Name: "Steven（site）", URL: site.URL, LoginMethod: LoginMethodTabiAI,
		GitHubAccount: "Steven", Enabled: true,
	}

	cookie, err := issueTabiAIRefreshCookie(context.Background(),
		HTTPConfig{Timeout: 5, Verify: true},
		effectiveGitHubCredentials(cfg, account), authorize.URL)
	if err != nil {
		t.Fatalf("用池子凭据签发失败: %v", err)
	}
	if cookie != "new_api_refresh=poolsid.poolsecret" {
		t.Fatalf("签发到的 cookie = %q", cookie)
	}
	if !strings.Contains(sawSession, "user_session=pool-sess") {
		t.Errorf("发给 GitHub 的不是池子里的 session: %q", sawSession)
	}
	// 池子填了 client_id 就该直接用，不再去站点 /api/status 探测
	if sawClientID != "pool-cid" {
		t.Errorf("client_id 应用池子的 pool-cid，实际 %q", sawClientID)
	}
}

func TestIssueCookieEndpointAcceptsPoolSession(t *testing.T) {
	// 前置检查必须认池子。让假站点在第 1 步就失败，签发因此打不到真 GitHub，
	// 而错误信息足以区分「被前置检查拦下」和「已经开始签发」
	site := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]any{"success": false, "message": "站点拒绝"})
	}))
	defer site.Close()

	srv := newTestServer(t)
	seedPool(t, srv,
		[]GitHubAccount{{Name: "Steven", UserSession: "pool-sess"}},
		[]Account{{Name: "Steven（site）", URL: site.URL, LoginMethod: LoginMethodTabiAI,
			GitHubAccount: "Steven", Enabled: true}})

	rr := doReq(t, srv, http.MethodPost, "/api/tabiai/issue-cookie", loginToken(t, srv),
		map[string]any{"account_name": "Steven（site）"})
	if rr.Code != http.StatusBadRequest {
		t.Fatalf("站点故意失败时应 400，实际 %d: %s", rr.Code, rr.Body.String())
	}
	// 必须是正向断言：错误得来自假站点，才证明前置检查放行、签发真的开跑了。
	// 只断言「不含某句拦截文案」是无效的 —— 改文案就会假绿
	if body := rr.Body.String(); !strings.Contains(body, "站点拒绝") {
		t.Fatalf("签发没真正开跑（前置检查大概没认池子）: %s", body)
	}
}

func TestIssueCookieEndpointSaysPoolAlsoEmpty(t *testing.T) {
	// 引用了一个不存在的池子条目：账号自身也没填，两处都拿不到。
	// 错误信息必须点明池子，否则用户会一直在账号表单里找那个空字段
	srv := newTestServer(t)
	seedPool(t, srv,
		[]GitHubAccount{{Name: "别人", UserSession: "sess"}},
		[]Account{{Name: "孤儿", URL: "https://a.com", LoginMethod: LoginMethodTabiAI,
			GitHubAccount: "引用了不存在的", Enabled: true}})

	rr := doReq(t, srv, http.MethodPost, "/api/tabiai/issue-cookie", loginToken(t, srv),
		map[string]any{"account_name": "孤儿"})
	if rr.Code != http.StatusBadRequest {
		t.Fatalf("应 400，实际 %d: %s", rr.Code, rr.Body.String())
	}
	body := rr.Body.String()
	if !strings.Contains(body, "池") {
		t.Errorf("错误信息应提到 GitHub 账号池: %s", body)
	}
	if !strings.Contains(body, "user_session") {
		t.Errorf("错误信息应指明缺的是 user_session: %s", body)
	}
}

func TestExpiredListHasUserSessionFromPool(t *testing.T) {
	// 失效名单里的 has_user_session 决定界面上「一键签发」能不能点。
	// 只看账号字段的话，凭据搬进池子后这个按钮会全部变灰，用户以为自救没了
	srv := newTestServer(t)
	seedPool(t, srv,
		[]GitHubAccount{{Name: "Steven", UserSession: "pool-sess"}},
		[]Account{
			{Name: "靠池子", URL: "https://a.com", LoginMethod: LoginMethodTabiAI,
				Cookie: "new_api_refresh=sid.a", GitHubAccount: "Steven", Enabled: true},
			{Name: "两处都没有", URL: "https://b.com", LoginMethod: LoginMethodTabiAI,
				Cookie: "new_api_refresh=sid.b", Enabled: true},
		})
	for _, row := range []TabiAIKeepaliveRow{
		{AccountName: "靠池子", State: cookieTestStateInvalid, Message: "凭据已失效",
			LastRunAt: "2026-08-28T07:00:00Z"},
		{AccountName: "两处都没有", State: cookieTestStateInvalid, Message: "凭据已失效",
			LastRunAt: "2026-08-28T06:00:00Z"},
	} {
		if err := saveKeepaliveState(srv.db, row, ""); err != nil {
			t.Fatalf("写保活状态 %q 失败: %v", row.AccountName, err)
		}
	}

	rr := doReq(t, srv, http.MethodGet, "/api/tabiai/expired", loginToken(t, srv), nil)
	if rr.Code != http.StatusOK {
		t.Fatalf("查询失效名单失败 = %d, %s", rr.Code, rr.Body.String())
	}
	var resp expiredListResponse
	if err := json.Unmarshal(rr.Body.Bytes(), &resp); err != nil {
		t.Fatal(err)
	}
	got := make(map[string]bool, len(resp.Accounts))
	for _, a := range resp.Accounts {
		got[a.Name] = a.HasUserSession
	}
	if !got["靠池子"] {
		t.Error("引用池子的账号应可自动签发（has_user_session=true）")
	}
	if got["两处都没有"] {
		t.Error("两处都没凭据的账号不该显示为可自动签发")
	}
	// 响应绝不能带上凭据本身
	if strings.Contains(rr.Body.String(), "pool-sess") {
		t.Error("失效名单里出现了明文 session")
	}
}
