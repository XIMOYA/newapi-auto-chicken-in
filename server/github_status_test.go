/*
server/github_status_test.go
GitHub 账号状态判定的测试。

判定错了的代价不对称，所以两个方向都要守：
  - 误判成 suspended/banned → 一个好账号被踢出池子
  - 漏判 → 一个死账号留在池子里，每轮签发都失败

因此除了正例，还专门守「403 与看不懂的 200 必须归 unknown」——
那两种最容易被写成「就当封禁吧」。
*/
package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestClassifyGitHubProfileResponse(t *testing.T) {
	cases := []struct {
		name     string
		status   int
		location string
		body     string
		want     string
		usable   bool
	}{
		{"登录态正常", 200, "", `<html><body class="logged-in">Sign out</body></html>`,
			githubStatusActive, true},
		{"停用提示", 200, "", "Your account has been suspended", githubStatusSuspended, false},
		{"封禁提示", 200, "", "This account has been terminated", githubStatusBanned, false},
		{"停用优先于登录态", 200, "", `<body class="logged-in">account is suspended</body>`,
			githubStatusSuspended, false},
		{"跳登录页", 302, "https://github.com/login?return_to=x", "", githubStatusExpired, false},
		{"跳 session", 302, "/session", "", githubStatusExpired, false},
		{"401", 401, "", "", githubStatusExpired, false},
		// 403 最容易被写成「就当封禁」—— 出口被限流也是 403，误判会踢掉好账号
		{"403 无特征归 unknown", 403, "", "rate limited", githubStatusUnknown, false},
		// 200 但看不出登录态：GitHub 改版会走到这里，不能当成账号有问题
		{"200 无登录态特征", 200, "", "<html><body>hello</body></html>", githubStatusUnknown, false},
		{"未预期的跳转", 302, "/somewhere-else", "", githubStatusUnknown, false},
		{"500", 500, "", "", githubStatusUnknown, false},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got := classifyGitHubProfileResponse(c.status, c.location, c.body)
			if got.Status != c.want {
				t.Fatalf("status = %q, want %q（message: %s）", got.Status, c.want, got.Message)
			}
			if got.Usable != c.usable {
				t.Errorf("usable = %v, want %v", got.Usable, c.usable)
			}
			if got.Message == "" {
				t.Error("每种结论都该带一句人话说明")
			}
		})
	}
}

func TestOnlyActiveIsUsable(t *testing.T) {
	// usable 是「值得留在池子里」的唯一依据，只有 active 能为真。
	// 写错会让停用账号被当成可用，每轮签发白失败
	for _, body := range []string{
		"Your account has been suspended",
		"This account has been terminated",
		"<html>hello</html>",
	} {
		if classifyGitHubProfileResponse(200, "", body).Usable {
			t.Errorf("非 active 不该 usable: %q", body)
		}
	}
	if !classifyGitHubProfileResponse(200, "", "Sign out").Usable {
		t.Error("active 应 usable")
	}
}

func TestStatusEndpointByNameAndBySession(t *testing.T) {
	// 假 GitHub：按请求带的 user_session 决定返回什么，用来同时验证
	// 「按 name 从池子取凭据」和「直传 user_session（入池前先判）」两条入参
	fake := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		cookie := r.Header.Get("Cookie")
		switch {
		case containsAny(cookie, []string{"user_session=good"}):
			_, _ = w.Write([]byte(`<html><body class="logged-in">Sign out</body></html>`))
		case containsAny(cookie, []string{"user_session=dead"}):
			_, _ = w.Write([]byte("Your account has been suspended"))
		default:
			w.Header().Set("Location", "https://github.com/login")
			w.WriteHeader(http.StatusFound)
		}
	}))
	defer fake.Close()

	srv := newTestServer(t)
	srv.githubProfileURL = fake.URL
	seedPool(t, srv, []GitHubAccount{
		{Name: "Good", UserSession: "good", Fingerprint: newFingerprintSeed("Good")},
		{Name: "Dead", UserSession: "dead"},
	}, nil)

	read := func(body map[string]any) githubStatusResult {
		t.Helper()
		rr := doReq(t, srv, http.MethodPost, "/api/github-accounts/status",
			loginToken(t, srv), body)
		if rr.Code != http.StatusOK {
			t.Fatalf("探测应成功 = %d, %s", rr.Code, rr.Body.String())
		}
		var resp struct {
			Result githubStatusResult `json:"result"`
		}
		if err := json.Unmarshal(rr.Body.Bytes(), &resp); err != nil {
			t.Fatal(err)
		}
		// 凭据绝不能回到响应里
		if containsAny(rr.Body.String(), []string{"user_session=good", "user_session=dead"}) {
			t.Fatalf("响应里出现了明文凭据: %s", rr.Body.String())
		}
		return resp.Result
	}

	if got := read(map[string]any{"name": "Good"}); got.Status != githubStatusActive || !got.Usable {
		t.Errorf("Good 应 active+usable，实际 %+v", got)
	}
	if got := read(map[string]any{"name": "Dead"}); got.Status != githubStatusSuspended || got.Usable {
		t.Errorf("Dead 应 suspended 且不可用，实际 %+v", got)
	}
	// 入池前先判：只传 user_session，不需要账号已在池子里
	if got := read(map[string]any{"user_session": "good"}); got.Status != githubStatusActive {
		t.Errorf("直传凭据应能判定，实际 %+v", got)
	}
	if got := read(map[string]any{"user_session": "unknown-one"}); got.Status != githubStatusExpired {
		t.Errorf("陌生凭据应判失效，实际 %+v", got)
	}

	// 入参校验与不存在的账号
	if rr := doReq(t, srv, http.MethodPost, "/api/github-accounts/status",
		loginToken(t, srv), map[string]any{}); rr.Code != http.StatusBadRequest {
		t.Errorf("两个入参都空应 400，实际 %d", rr.Code)
	}
	if rr := doReq(t, srv, http.MethodPost, "/api/github-accounts/status",
		loginToken(t, srv), map[string]any{"name": "不存在"}); rr.Code != http.StatusNotFound {
		t.Errorf("不存在的账号应 404，实际 %d", rr.Code)
	}
	if rr := doReq(t, srv, http.MethodPost, "/api/github-accounts/status", "",
		map[string]any{"name": "Good"}); rr.Code != http.StatusUnauthorized {
		t.Errorf("无 token 应 401，实际 %d", rr.Code)
	}
}
