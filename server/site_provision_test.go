/*
server/site_provision_test.go
按站点 URL 批量建签到账号的测试。

三条分支的处置完全不同，各自都得守住：
  - 站点关闭注册 → 跳过且**只尝试一次**。重试改变不了站点开关，白等还多打站点几次
  - 其他失败 → 尝试满 provisionMaxAttempts 次才放弃
  - 账号已存在 → 直接跳过，绝不重新签发（重签换出新代次，把还能用的会话作废）

签发那一跳用注入的假实现：真实现要连 GitHub 和站点，端点测试不该也没法真去连。
*/
package main

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

const provSite = "https://prov.example.com"

// httptestServerForAuthorize 造一个假 GitHub authorize：直接 302 回站点回调，
// 让整条 OAuth 三步在本地跑通，不碰真实网络。
func httptestServerForAuthorize(t *testing.T, siteURL string) string {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		state := r.URL.Query().Get("state")
		w.Header().Set("Location", siteURL+"/oauth/github?code=code-prov&state="+state)
		w.WriteHeader(http.StatusFound)
	}))
	t.Cleanup(srv.Close)
	return srv.URL
}

func TestLooksLikeRegistrationClosed(t *testing.T) {
	closed := []string{
		"OAuth 回调失败: 管理员关闭了新用户注册",
		"注册已关闭",
		"registration is disabled",
		"Sign up is disabled for this site",
		"新用户注册未开放注册",
	}
	for _, msg := range closed {
		if !looksLikeRegistrationClosed(msg) {
			t.Errorf("应判为关闭注册: %q", msg)
		}
	}
	// 判错的代价不对称：误判会让本来能建的账号被永久跳过，所以宁可漏判
	notClosed := []string{
		"", "OAuth state 网络错误: timeout",
		"GitHub authorize HTTP 403，当前出口被 GitHub 限制",
		"站点未下发凭据",
		"GitHub 要求重新登录，user_session 已失效",
	}
	for _, msg := range notClosed {
		if looksLikeRegistrationClosed(msg) {
			t.Errorf("不该判为关闭注册: %q", msg)
		}
	}
}

func TestProvisionOneAccountRetryPolicy(t *testing.T) {
	cfg := &Config{GitHubAccounts: []GitHubAccount{
		{Name: "Steven", UserSession: "sess", Fingerprint: newFingerprintSeed("Steven")},
	}}
	pool := cfg.GitHubAccounts[0]

	// 关闭注册：只尝试一次
	calls := 0
	out, cookie := provisionOneAccount(context.Background(), cfg, pool, provSite, nil,
		func(context.Context, Account, githubFingerprint) (string, error) {
			calls++
			return "", fmt.Errorf("OAuth 回调失败: 管理员关闭了新用户注册")
		})
	if out.Status != provisionClosed {
		t.Fatalf("状态 = %q, want %q", out.Status, provisionClosed)
	}
	if calls != 1 || out.Attempts != 1 {
		t.Errorf("关闭注册不该重试，实际调用 %d 次、attempts=%d", calls, out.Attempts)
	}
	if cookie != "" {
		t.Error("失败不该回传凭据")
	}

	// 其他失败：尝试满上限
	calls = 0
	out, _ = provisionOneAccount(context.Background(), cfg, pool, provSite, nil,
		func(context.Context, Account, githubFingerprint) (string, error) {
			calls++
			return "", fmt.Errorf("OAuth state 网络错误: timeout")
		})
	if out.Status != provisionFailed {
		t.Fatalf("状态 = %q, want %q", out.Status, provisionFailed)
	}
	if calls != provisionMaxAttempts || out.Attempts != provisionMaxAttempts {
		t.Errorf("应尝试 %d 次，实际 %d 次（attempts=%d）",
			provisionMaxAttempts, calls, out.Attempts)
	}

	// 中途成功：不再多打
	calls = 0
	out, cookie = provisionOneAccount(context.Background(), cfg, pool, provSite, nil,
		func(context.Context, Account, githubFingerprint) (string, error) {
			calls++
			if calls < 2 {
				return "", fmt.Errorf("站点 502")
			}
			return "new_api_refresh=ok", nil
		})
	if out.Status != provisionCreated || cookie != "new_api_refresh=ok" {
		t.Fatalf("应在第 2 次成功，实际 status=%q cookie=%q", out.Status, cookie)
	}
	if calls != 2 {
		t.Errorf("成功后不该继续尝试，实际 %d 次", calls)
	}
	if out.AccountName != "Steven（prov.example.com）" {
		t.Errorf("账号名 = %q，应是「GitHub名（域名）」", out.AccountName)
	}
}

func TestProvisionOneAccountSkipsExistingAndCredless(t *testing.T) {
	cfg := &Config{GitHubAccounts: []GitHubAccount{
		{Name: "Steven", UserSession: "sess"},
		{Name: "NoCred"},
	}}
	never := func(context.Context, Account, githubFingerprint) (string, error) {
		t.Fatal("这两种情况都不该发起签发")
		return "", nil
	}

	existing := map[string]bool{"Steven（prov.example.com）": true}
	out, _ := provisionOneAccount(context.Background(), cfg, cfg.GitHubAccounts[0],
		provSite, existing, never)
	if out.Status != provisionExists {
		t.Errorf("已存在的账号状态 = %q, want %q", out.Status, provisionExists)
	}

	out, _ = provisionOneAccount(context.Background(), cfg, cfg.GitHubAccounts[1],
		provSite, nil, never)
	if out.Status != provisionNoCreds {
		t.Errorf("无凭据的账号状态 = %q, want %q", out.Status, provisionNoCreds)
	}
}

func TestProvisionSiteEndpointCreatesAccounts(t *testing.T) {
	// 端到端：假站点跑完整 OAuth 三步，确认建成的账号真的落库、字段对
	site := poolOAuthSite(t)
	srv := newTestServer(t)
	fakeAuth := httptestServerForAuthorize(t, site.URL)
	srv.githubAuthorizeURL = fakeAuth

	seedPool(t, srv, []GitHubAccount{
		{Name: "Steven", UserSession: "sess-a", Fingerprint: newFingerprintSeed("Steven")},
		{Name: "Alice", UserSession: "sess-b", Fingerprint: newFingerprintSeed("Alice")},
		{Name: "NoCred"},
	}, nil)

	rr := doReq(t, srv, http.MethodPost, "/api/sites/provision", loginToken(t, srv),
		map[string]any{"url": site.URL})
	if rr.Code != http.StatusOK {
		t.Fatalf("建号应成功 = %d, %s", rr.Code, rr.Body.String())
	}
	var resp struct {
		Created int `json:"created"`
		Total   int `json:"total"`
		Results []struct {
			GitHubAccount string `json:"github_account"`
			AccountName   string `json:"account_name"`
			Status        string `json:"status"`
		} `json:"results"`
	}
	if err := json.Unmarshal(rr.Body.Bytes(), &resp); err != nil {
		t.Fatal(err)
	}
	if resp.Total != 3 {
		t.Fatalf("应处理 3 个账号，实际 %d", resp.Total)
	}
	if resp.Created != 2 {
		t.Fatalf("应建成 2 个（NoCred 没凭据），实际 %d：%s", resp.Created, rr.Body.String())
	}
	byName := map[string]string{}
	for _, r := range resp.Results {
		byName[r.GitHubAccount] = r.Status
	}
	if byName["NoCred"] != provisionNoCreds {
		t.Errorf("NoCred 状态 = %q", byName["NoCred"])
	}

	// 落库核对：名字、登录方式、引用、凭据都要对
	cfg, _, err := LoadConfig(srv.db)
	if err != nil {
		t.Fatal(err)
	}
	found := 0
	for _, a := range cfg.Accounts {
		if !strings.HasPrefix(a.Name, "Steven（") && !strings.HasPrefix(a.Name, "Alice（") {
			continue
		}
		found++
		if a.LoginMethod != LoginMethodTabiAI {
			t.Errorf("%s 的 login_method = %q", a.Name, a.LoginMethod)
		}
		if a.URL != site.URL {
			t.Errorf("%s 的 url = %q", a.Name, a.URL)
		}
		if a.Cookie == "" {
			t.Errorf("%s 没落凭据", a.Name)
		}
		// 必须引用池子，否则后续签发拿不到凭据
		if a.GitHubAccount == "" {
			t.Errorf("%s 没引用 GitHub 账号池", a.Name)
		}
	}
	if found != 2 {
		t.Fatalf("库里应有 2 个新账号，实际 %d", found)
	}

	// 再跑一次：全部应判 exists，不重新签发
	rr2 := doReq(t, srv, http.MethodPost, "/api/sites/provision", loginToken(t, srv),
		map[string]any{"url": site.URL})
	if rr2.Code != http.StatusOK {
		t.Fatalf("第二次调用 = %d", rr2.Code)
	}
	if !strings.Contains(rr2.Body.String(), provisionExists) {
		t.Errorf("第二次应判已存在: %s", rr2.Body.String())
	}
}
