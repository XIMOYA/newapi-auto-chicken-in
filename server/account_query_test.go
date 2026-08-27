/*
server/account_query_test.go
单账号查询端点测试：GET /api/accounts/{name}（脱敏摘要）与 /raw（明文）

守的是三件事：
  - 脱敏端点绝不漏 cookie 明文，但摘要要足够核实代次
  - 明文端点只认 API Key，且真的给明文（客户端靠它做回写后的精确比对）
  - 账号定位规则与回写端点完全一致 —— 否则「写得进、核不到」
*/
package main

import (
	"crypto/sha256"
	"encoding/hex"
	"net/http"
	"net/url"
	"strings"
	"testing"
)

const testTabiCookie = "new_api_refresh=sid.gen7"

// seedQueryAccounts 造两个账号：一个 tabiai（带轮转凭据），一个凭据为空。
func seedQueryAccounts(t *testing.T, srv *Server) {
	t.Helper()
	seedConfig(t, srv, []Account{
		{Name: "tabi", URL: "https://a.com", LoginMethod: LoginMethodTabiAI,
			Cookie: testTabiCookie, GithubUserSession: "gh-session-明文", Enabled: true},
		{Name: "空凭据", URL: "https://b.com", LoginMethod: LoginMethodNewAPICookie,
			Cookie: "", Enabled: false},
	}, nil)
}

func fingerprintOf(value string) string {
	sum := sha256.Sum256([]byte(value))
	return hex.EncodeToString(sum[:])[:cookieFingerprintLength]
}

// accountDigestResponse 脱敏端点的响应形状。
type accountDigestResponse struct {
	Account struct {
		Name              string `json:"name"`
		URL               string `json:"url"`
		LoginMethod       string `json:"login_method"`
		Cookie            string `json:"cookie"`
		GithubUserSession string `json:"github_user_session"`
		Enabled           bool   `json:"enabled"`
	} `json:"account"`
	CookieDigest struct {
		Fingerprint string `json:"fingerprint"`
		Length      int    `json:"length"`
		HasRefresh  bool   `json:"has_refresh"`
	} `json:"cookie_digest"`
	UpdatedAt string `json:"updated_at"`
}

func TestGetAccountMasksCookieButGivesVerifiableDigest(t *testing.T) {
	srv := newTestServer(t)
	seedQueryAccounts(t, srv)
	jwt := loginToken(t, srv)

	rr := doReq(t, srv, http.MethodGet, "/api/accounts/tabi", jwt, nil)
	if rr.Code != http.StatusOK {
		t.Fatalf("查询账号失败 = %d, %s", rr.Code, rr.Body.String())
	}
	// 明文一个字节都不能出现在响应里
	if strings.Contains(rr.Body.String(), testTabiCookie) {
		t.Fatalf("脱敏端点漏出了 cookie 明文: %s", rr.Body.String())
	}
	if strings.Contains(rr.Body.String(), "gh-session-明文") {
		t.Fatalf("脱敏端点漏出了 github_user_session 明文: %s", rr.Body.String())
	}

	var resp accountDigestResponse
	decodeJSON(t, rr, &resp)
	if resp.Account.Name != "tabi" || resp.Account.URL != "https://a.com" {
		t.Fatalf("非敏感字段应原样返回: %+v", resp.Account)
	}
	if resp.Account.Cookie != MaskPlaceholder || resp.Account.GithubUserSession != MaskPlaceholder {
		t.Fatalf("敏感字段应打码: cookie=%q session=%q",
			resp.Account.Cookie, resp.Account.GithubUserSession)
	}
	if resp.CookieDigest.Fingerprint != fingerprintOf(testTabiCookie) {
		t.Errorf("指纹 = %q, 期望 %q", resp.CookieDigest.Fingerprint, fingerprintOf(testTabiCookie))
	}
	if resp.CookieDigest.Length != len(testTabiCookie) {
		t.Errorf("长度 = %d, 期望 %d", resp.CookieDigest.Length, len(testTabiCookie))
	}
	if !resp.CookieDigest.HasRefresh {
		t.Error("带 new_api_refresh 的凭据 has_refresh 应为 true")
	}
	if resp.UpdatedAt == "" {
		t.Error("应带出配置更新时间，供人工核对回写是什么时候落库的")
	}
}

func TestGetAccountEmptyCookieHasEmptyDigest(t *testing.T) {
	srv := newTestServer(t)
	seedQueryAccounts(t, srv)
	jwt := loginToken(t, srv)

	rr := doReq(t, srv, http.MethodGet, "/api/accounts/"+url.PathEscape("空凭据"), jwt, nil)
	if rr.Code != http.StatusOK {
		t.Fatalf("查询空凭据账号失败 = %d, %s", rr.Code, rr.Body.String())
	}
	var resp accountDigestResponse
	decodeJSON(t, rr, &resp)
	if resp.CookieDigest.Fingerprint != "" || resp.CookieDigest.Length != 0 ||
		resp.CookieDigest.HasRefresh {
		t.Fatalf("空 cookie 的摘要应是空值形态: %+v", resp.CookieDigest)
	}
	// 空值不该被打成 "***"，否则界面会显示「已设置」
	if resp.Account.Cookie != "" {
		t.Errorf("空 cookie 不应打码: %q", resp.Account.Cookie)
	}
}

func TestGetAccountRawReturnsPlaintextForAPIKeyOnly(t *testing.T) {
	srv := newTestServer(t)
	seedQueryAccounts(t, srv)
	jwt := loginToken(t, srv)
	key := apiKeyToken(t, srv, jwt)

	rr := doReq(t, srv, http.MethodGet, "/api/accounts/tabi/raw", key, nil)
	if rr.Code != http.StatusOK {
		t.Fatalf("明文查询失败 = %d, %s", rr.Code, rr.Body.String())
	}
	var account Account
	decodeJSON(t, rr, &account)
	if account.Cookie != testTabiCookie {
		t.Fatalf("明文端点应返回真实凭据，实际 %q", account.Cookie)
	}
	if account.Name != "tabi" {
		t.Errorf("账号名 = %q", account.Name)
	}

	// 分级：明文端点只认 API Key，JWT（网页端）必须被拒
	if withJWT := doReq(t, srv, http.MethodGet, "/api/accounts/tabi/raw", jwt, nil); withJWT.Code != http.StatusUnauthorized {
		t.Fatalf("明文端点不该对 JWT 放行，实际 %d: %s", withJWT.Code, withJWT.Body.String())
	}
	if anon := doReq(t, srv, http.MethodGet, "/api/accounts/tabi/raw", "", nil); anon.Code != http.StatusUnauthorized {
		t.Fatalf("明文端点必须鉴权，实际 %d", anon.Code)
	}
}

func TestGetAccountAcceptsBothCredentials(t *testing.T) {
	// 脱敏端点是双认证：网页端拿 JWT 核实，客户端/脚本拿 API Key 也该能查
	srv := newTestServer(t)
	seedQueryAccounts(t, srv)
	jwt := loginToken(t, srv)
	key := apiKeyToken(t, srv, jwt)

	for name, token := range map[string]string{"JWT": jwt, "API Key": key} {
		if rr := doReq(t, srv, http.MethodGet, "/api/accounts/tabi", token, nil); rr.Code != http.StatusOK {
			t.Errorf("%s 应被放行，实际 %d: %s", name, rr.Code, rr.Body.String())
		}
	}
	if anon := doReq(t, srv, http.MethodGet, "/api/accounts/tabi", "", nil); anon.Code != http.StatusUnauthorized {
		t.Errorf("脱敏端点必须鉴权，实际 %d", anon.Code)
	}
}

func TestGetAccountMissingReturns404(t *testing.T) {
	srv := newTestServer(t)
	seedQueryAccounts(t, srv)
	jwt := loginToken(t, srv)
	key := apiKeyToken(t, srv, jwt)

	cases := []struct {
		path, token string
	}{
		{"/api/accounts/nope", jwt},
		{"/api/accounts/nope/raw", key},
	}
	for _, tc := range cases {
		rr := doReq(t, srv, http.MethodGet, tc.path, tc.token, nil)
		if rr.Code != http.StatusNotFound {
			t.Fatalf("%s 应 404，实际 %d: %s", tc.path, rr.Code, rr.Body.String())
		}
		var body struct {
			Error string `json:"error"`
		}
		decodeJSON(t, rr, &body)
		if !strings.Contains(body.Error, "账号不存在") {
			t.Errorf("%s 错误体应与其它 handler 一致，实际 %q", tc.path, body.Error)
		}
	}
}

// TestAccountLookupMatchesWritebackRule 定位规则必须和回写端点同一套。
// 只要有一处不一样（大小写、trim、编码），就会出现「回写找得到、核实找不到」，
// 而客户端会把这种情况当成回写没生效，把成功的轮次判成失败。
func TestAccountLookupMatchesWritebackRule(t *testing.T) {
	srv := newTestServer(t)
	const name = "我的 站点/A"
	seedConfig(t, srv, []Account{
		{Name: name, URL: "https://a.com", LoginMethod: LoginMethodTabiAI,
			Cookie: "new_api_refresh=old", Enabled: true},
	}, nil)
	jwt := loginToken(t, srv)
	key := apiKeyToken(t, srv, jwt)

	escaped := url.PathEscape(name)
	// 同一个探针在三个端点上必须得到同样的判定（大小写敏感、trim 口径一致）
	for _, probe := range []string{"我的 站点/a", "  " + name + "  ", "别的名字"} {
		p := url.PathEscape(probe)
		write := doReq(t, srv, http.MethodPost, "/api/accounts/"+p+"/refresh-cookie", key,
			map[string]any{"cookie": "new_api_refresh=x"})
		masked := doReq(t, srv, http.MethodGet, "/api/accounts/"+p, jwt, nil)
		raw := doReq(t, srv, http.MethodGet, "/api/accounts/"+p+"/raw", key, nil)
		if masked.Code != write.Code || raw.Code != write.Code {
			t.Fatalf("probe=%q 三个端点判定不一致: 回写 %d, 脱敏 %d, 明文 %d",
				probe, write.Code, masked.Code, raw.Code)
		}
	}

	// 回写 -> 读回：明文端点必须给出刚写进去的那一代，脱敏端点的指纹随之变化
	before := doReq(t, srv, http.MethodGet, "/api/accounts/"+escaped, jwt, nil)
	var beforeBody accountDigestResponse
	decodeJSON(t, before, &beforeBody)

	const fresh = "new_api_refresh=sid.gen9"
	if wb := doReq(t, srv, http.MethodPost, "/api/accounts/"+escaped+"/refresh-cookie", key,
		map[string]any{"cookie": fresh}); wb.Code != http.StatusOK {
		t.Fatalf("回写失败 = %d, %s", wb.Code, wb.Body.String())
	}
	rawResp := doReq(t, srv, http.MethodGet, "/api/accounts/"+escaped+"/raw", key, nil)
	if rawResp.Code != http.StatusOK {
		t.Fatalf("读回失败 = %d, %s", rawResp.Code, rawResp.Body.String())
	}
	var account Account
	decodeJSON(t, rawResp, &account)
	if account.Cookie != fresh {
		t.Fatalf("读回的凭据 = %q, 期望 %q", account.Cookie, fresh)
	}

	after := doReq(t, srv, http.MethodGet, "/api/accounts/"+escaped, jwt, nil)
	var afterBody accountDigestResponse
	decodeJSON(t, after, &afterBody)
	if afterBody.CookieDigest.Fingerprint != fingerprintOf(fresh) {
		t.Errorf("回写后指纹 = %q, 期望 %q",
			afterBody.CookieDigest.Fingerprint, fingerprintOf(fresh))
	}
	if afterBody.CookieDigest.Fingerprint == beforeBody.CookieDigest.Fingerprint {
		t.Error("指纹没变化，人工核实就看不出代次换了没换")
	}
}

// accountListResponse 清单端点的响应形状。
type accountListResponse struct {
	Accounts []struct {
		Name        string  `json:"name"`
		URL         string  `json:"url"`
		LoginMethod string  `json:"login_method"`
		Enabled     bool    `json:"enabled"`
		HasCookie   bool    `json:"has_cookie"`
		Proxy       *string `json:"proxy"`
	} `json:"accounts"`
	Count     int    `json:"count"`
	UpdatedAt string `json:"updated_at"`
	Revision  int64  `json:"revision"`
}

func TestListAccountsGivesNamesWithoutAnyCredential(t *testing.T) {
	srv := newTestServer(t)
	seedQueryAccounts(t, srv)
	jwt := loginToken(t, srv)

	rr := doReq(t, srv, http.MethodGet, "/api/accounts", jwt, nil)
	if rr.Code != http.StatusOK {
		t.Fatalf("清单查询失败 = %d, %s", rr.Code, rr.Body.String())
	}
	// 清单连打码占位符都不该出现：它压根不包含凭据字段
	body := rr.Body.String()
	for _, leaked := range []string{testTabiCookie, "gh-session-明文", MaskPlaceholder} {
		if strings.Contains(body, leaked) {
			t.Fatalf("清单不该出现 %q：%s", leaked, body)
		}
	}

	var resp accountListResponse
	decodeJSON(t, rr, &resp)
	if resp.Count != 2 || len(resp.Accounts) != 2 {
		t.Fatalf("应返回 2 个账号，实际 count=%d len=%d", resp.Count, len(resp.Accounts))
	}
	// 顺序与配置一致：界面按这个顺序显示，重排会让「第 N 个」对不上
	if resp.Accounts[0].Name != "tabi" || resp.Accounts[1].Name != "空凭据" {
		t.Fatalf("顺序应与配置一致: %q, %q", resp.Accounts[0].Name, resp.Accounts[1].Name)
	}
	first := resp.Accounts[0]
	if first.URL != "https://a.com" || first.LoginMethod != LoginMethodTabiAI || !first.Enabled {
		t.Errorf("元数据不对: %+v", first)
	}
	if !first.HasCookie {
		t.Error("配了凭据的账号 has_cookie 应为 true")
	}
	if second := resp.Accounts[1]; second.HasCookie || second.Enabled {
		t.Errorf("空凭据且停用的账号: has_cookie=%v enabled=%v", second.HasCookie, second.Enabled)
	}
	if resp.UpdatedAt == "" {
		t.Error("应带出配置更新时间")
	}
}

func TestListAccountsRevisionMatchesConfigRevision(t *testing.T) {
	srv := newTestServer(t)
	seedQueryAccounts(t, srv)
	jwt := loginToken(t, srv)

	list := doReq(t, srv, http.MethodGet, "/api/accounts", jwt, nil)
	var listResp accountListResponse
	decodeJSON(t, list, &listResp)

	rev := doReq(t, srv, http.MethodGet, "/api/config/revision", jwt, nil)
	var revResp struct {
		Revision int64 `json:"revision"`
	}
	decodeJSON(t, rev, &revResp)

	// 两处必须一致：调用方拿清单里的 revision 直接去做 PUT /api/config 的乐观锁参数
	if listResp.Revision != revResp.Revision {
		t.Fatalf("清单 revision=%d 与 /api/config/revision=%d 不一致",
			listResp.Revision, revResp.Revision)
	}
}

func TestListAccountsAcceptsAPIKeyAndRequiresAuth(t *testing.T) {
	srv := newTestServer(t)
	seedQueryAccounts(t, srv)
	jwt := loginToken(t, srv)
	key := apiKeyToken(t, srv, jwt)

	// 双认证：JWT 和 API Key 都该放行（脚本侧要用 Key 遍历账号）
	if withKey := doReq(t, srv, http.MethodGet, "/api/accounts", key, nil); withKey.Code != http.StatusOK {
		t.Fatalf("清单应对 API Key 放行，实际 %d: %s", withKey.Code, withKey.Body.String())
	}
	if anon := doReq(t, srv, http.MethodGet, "/api/accounts", "", nil); anon.Code != http.StatusUnauthorized {
		t.Fatalf("清单必须鉴权，实际 %d", anon.Code)
	}
}

func TestListAccountsOnEmptyConfigReturnsEmptyArray(t *testing.T) {
	srv := newTestServer(t)
	seedConfig(t, srv, []Account{}, nil)
	jwt := loginToken(t, srv)

	rr := doReq(t, srv, http.MethodGet, "/api/accounts", jwt, nil)
	if rr.Code != http.StatusOK {
		t.Fatalf("空配置查询失败 = %d, %s", rr.Code, rr.Body.String())
	}
	// 必须是 []，不能是 null —— 前端 v-for 拿到 null 会炸
	if !strings.Contains(rr.Body.String(), `"accounts":[]`) {
		t.Fatalf("空账号列表应序列化成 []：%s", rr.Body.String())
	}
	var resp accountListResponse
	decodeJSON(t, rr, &resp)
	if resp.Count != 0 {
		t.Errorf("count = %d, 期望 0", resp.Count)
	}
}

func TestListAccountsCarriesPerAccountProxy(t *testing.T) {
	srv := newTestServer(t)
	fixed := "http://1.2.3.4:8080"
	seedConfig(t, srv, []Account{
		{Name: "带代理", URL: "https://a.com", LoginMethod: LoginMethodNewAPICookie,
			Cookie: "c", Proxy: &fixed, Enabled: true},
		{Name: "走池子", URL: "https://b.com", LoginMethod: LoginMethodNewAPICookie,
			Cookie: "c", Enabled: true},
	}, nil)
	jwt := loginToken(t, srv)

	var resp accountListResponse
	decodeJSON(t, doReq(t, srv, http.MethodGet, "/api/accounts", jwt, nil), &resp)
	if resp.Accounts[0].Proxy == nil || *resp.Accounts[0].Proxy != fixed {
		t.Fatalf("账号自带代理应原样返回: %+v", resp.Accounts[0].Proxy)
	}
	// 没配代理时是 null，表示走代理池或直连 —— 不能变成空串，那会被当成「配了个空代理」
	if resp.Accounts[1].Proxy != nil {
		t.Fatalf("没配代理应为 null，实际 %q", *resp.Accounts[1].Proxy)
	}
}

// TestListAndDetailShareTheSameNames 清单里的名字必须能直接拿去查详情。
// 这两个端点是配对使用的：先列名字，再按名字查数据、按名字改数据。
func TestListAndDetailShareTheSameNames(t *testing.T) {
	srv := newTestServer(t)
	seedQueryAccounts(t, srv)
	jwt := loginToken(t, srv)

	var list accountListResponse
	decodeJSON(t, doReq(t, srv, http.MethodGet, "/api/accounts", jwt, nil), &list)
	if len(list.Accounts) == 0 {
		t.Fatal("清单为空，无法验证配对关系")
	}
	for _, item := range list.Accounts {
		path := "/api/accounts/" + url.PathEscape(item.Name)
		if rr := doReq(t, srv, http.MethodGet, path, jwt, nil); rr.Code != http.StatusOK {
			t.Fatalf("清单里的名字 %q 查详情失败 = %d, %s", item.Name, rr.Code, rr.Body.String())
		}
	}
}
