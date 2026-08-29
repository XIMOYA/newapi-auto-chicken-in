/*
server/tabiai_expired_test.go
凭据失效名单端点测试：GET /api/tabiai/expired

守的是三件事：
  - 判定口径从严：只有 invalid / paused 算失效，proxy_issue / abnormal 不算
    （那是代理或网络问题，凭据可能还好，签发它等于白白作废一条可用凭据）
  - 响应不含任何凭据值，但要带 has_user_session（决定能不能自动签发）
  - 双认证：网页端一键签发用 JWT，脚本用 API Key，两者都得放行
*/
package main

import (
	"net/http"
	"testing"
)

type expiredListResponse struct {
	Accounts []struct {
		Name           string `json:"name"`
		State          string `json:"state"`
		Paused         bool   `json:"paused"`
		Message        string `json:"message"`
		LastRunAt      string `json:"last_run_at"`
		HasUserSession bool   `json:"has_user_session"`
	} `json:"accounts"`
	Count     int    `json:"count"`
	CheckedAt string `json:"checked_at"`
}

// seedExpiredFixture 造 5 个 tabiai 账号覆盖各种状态，外加一个非 tabiai 账号。
func seedExpiredFixture(t *testing.T, srv *Server) {
	t.Helper()
	seedConfig(t, srv, []Account{
		{Name: "失效有session", URL: "https://a.com", LoginMethod: LoginMethodTabiAI,
			Cookie: "new_api_refresh=sid.a", GithubUserSession: "gh-a", Enabled: true},
		{Name: "失效无session", URL: "https://b.com", LoginMethod: LoginMethodTabiAI,
			Cookie: "new_api_refresh=sid.b", Enabled: true},
		{Name: "被暂停", URL: "https://c.com", LoginMethod: LoginMethodTabiAI,
			Cookie: "new_api_refresh=sid.c", GithubUserSession: "gh-c", Enabled: true},
		{Name: "代理问题", URL: "https://d.com", LoginMethod: LoginMethodTabiAI,
			Cookie: "new_api_refresh=sid.d", GithubUserSession: "gh-d", Enabled: true},
		{Name: "正常", URL: "https://e.com", LoginMethod: LoginMethodTabiAI,
			Cookie: "new_api_refresh=sid.e", GithubUserSession: "gh-e", Enabled: true},
		{Name: "非tabiai", URL: "https://f.com", LoginMethod: LoginMethodNewAPICookie,
			Cookie: "session=x", Enabled: true},
	}, nil)

	for _, row := range []TabiAIKeepaliveRow{
		{AccountName: "失效有session", State: cookieTestStateInvalid, Message: "凭据已失效",
			LastRunAt: "2026-08-28T07:00:00Z"},
		{AccountName: "失效无session", State: cookieTestStateInvalid, Message: "凭据已失效",
			LastRunAt: "2026-08-28T06:00:00Z"},
		{AccountName: "被暂停", State: cookieTestStateAbnormal, Paused: true,
			Message: "已暂停", LastRunAt: "2026-08-28T05:00:00Z"},
		{AccountName: "代理问题", State: "proxy_issue", Message: "代理不通",
			LastRunAt: "2026-08-28T07:00:00Z"},
		{AccountName: "正常", State: cookieTestStateValid, Message: "凭据有效",
			LastRunAt: "2026-08-28T07:00:00Z"},
	} {
		if err := saveKeepaliveState(srv.db, row, ""); err != nil {
			t.Fatalf("写保活状态 %q 失败: %v", row.AccountName, err)
		}
	}
}

func TestListExpiredTabiAIOnlyInvalidAndPaused(t *testing.T) {
	srv := newTestServer(t)
	seedExpiredFixture(t, srv)
	jwt := loginToken(t, srv)

	rr := doReq(t, srv, http.MethodGet, "/api/tabiai/expired", jwt, nil)
	if rr.Code != http.StatusOK {
		t.Fatalf("查询失效名单失败 = %d, %s", rr.Code, rr.Body.String())
	}
	var resp expiredListResponse
	decodeJSON(t, rr, &resp)

	got := map[string]bool{}
	for _, a := range resp.Accounts {
		got[a.Name] = true
	}
	for _, want := range []string{"失效有session", "失效无session", "被暂停"} {
		if !got[want] {
			t.Errorf("应包含 %q：%+v", want, resp.Accounts)
		}
	}
	// proxy_issue / valid 都不算失效 —— 签发它们会白白作废一条可用凭据
	for _, unwanted := range []string{"代理问题", "正常", "非tabiai"} {
		if got[unwanted] {
			t.Errorf("不该包含 %q：%+v", unwanted, resp.Accounts)
		}
	}
	if resp.Count != 3 {
		t.Errorf("count = %d, 期望 3", resp.Count)
	}
	// checked_at 取最近一次刷新时间，让调用方知道名单有多新
	if resp.CheckedAt != "2026-08-28T07:00:00Z" {
		t.Errorf("checked_at = %q", resp.CheckedAt)
	}
}

func TestListExpiredTabiAICarriesUserSessionFlag(t *testing.T) {
	srv := newTestServer(t)
	seedExpiredFixture(t, srv)
	jwt := loginToken(t, srv)

	var resp expiredListResponse
	decodeJSON(t, doReq(t, srv, http.MethodGet, "/api/tabiai/expired", jwt, nil), &resp)
	for _, a := range resp.Accounts {
		switch a.Name {
		case "失效有session", "被暂停":
			if !a.HasUserSession {
				t.Errorf("%q 填了 user_session，应为 true", a.Name)
			}
		case "失效无session":
			if a.HasUserSession {
				t.Errorf("%q 没填 user_session，应为 false（只能人工粘贴）", a.Name)
			}
		}
	}
}

func TestListExpiredTabiAINeverLeaksCredentials(t *testing.T) {
	srv := newTestServer(t)
	seedExpiredFixture(t, srv)
	jwt := loginToken(t, srv)

	body := doReq(t, srv, http.MethodGet, "/api/tabiai/expired", jwt, nil).Body.String()
	for _, leaked := range []string{"new_api_refresh=sid.a", "gh-a", MaskPlaceholder} {
		if contains(body, leaked) {
			t.Fatalf("响应不该出现 %q：%s", leaked, body)
		}
	}
}

func TestListExpiredTabiAIAcceptsAPIKeyAndRequiresAuth(t *testing.T) {
	srv := newTestServer(t)
	seedExpiredFixture(t, srv)
	jwt := loginToken(t, srv)
	key := apiKeyToken(t, srv, jwt)

	// 脚本/客户端只有 API Key，必须放行
	if withKey := doReq(t, srv, http.MethodGet, "/api/tabiai/expired", key, nil); withKey.Code != http.StatusOK {
		t.Fatalf("应对 API Key 放行，实际 %d: %s", withKey.Code, withKey.Body.String())
	}
	if anon := doReq(t, srv, http.MethodGet, "/api/tabiai/expired", "", nil); anon.Code != http.StatusUnauthorized {
		t.Fatalf("必须鉴权，实际 %d", anon.Code)
	}
}

func TestListExpiredTabiAIEmptyIsArrayNotNull(t *testing.T) {
	srv := newTestServer(t)
	seedConfig(t, srv, []Account{
		{Name: "正常", URL: "https://a.com", LoginMethod: LoginMethodTabiAI,
			Cookie: "new_api_refresh=sid.a", Enabled: true},
	}, nil)
	jwt := loginToken(t, srv)

	rr := doReq(t, srv, http.MethodGet, "/api/tabiai/expired", jwt, nil)
	// 没有保活记录的账号不算失效（还没被刷过，不能凭空断定）
	if !contains(rr.Body.String(), `"accounts":[]`) {
		t.Fatalf("空名单应序列化成 []：%s", rr.Body.String())
	}
}

// contains 避免为一个子串判断引入 strings 依赖之外的东西。
func contains(haystack, needle string) bool {
	return len(needle) > 0 && len(haystack) >= len(needle) &&
		indexOfSubstring(haystack, needle) >= 0
}

func indexOfSubstring(haystack, needle string) int {
	for i := 0; i+len(needle) <= len(haystack); i++ {
		if haystack[i:i+len(needle)] == needle {
			return i
		}
	}
	return -1
}
