/*
server/github_account_ops_test.go
GitHub 凭据池增删改端点（POST /api/github-accounts/ops）的行为测试。

重点盯三件在别处不会暴露的事：
  - 引用完整性：池子被 accounts[].github_account 按名字引用，改名要连带搬引用，
    删除要先确认没人引用。断了引用不会报错 —— resolveAccountSession 会静默回落
    账号自带的旧字段，签到看着还在跑，实际拿过期凭据去签发。
  - 打码还原：前端回传的 user_session 是 "***"，改名时新名在旧配置里查不到，
    必须靠 previous_name 先把查表基准改名（unmaskWithPoolRenames）。
  - 失败不落库：任何一条校验不过，整批都不该改动数据库。
*/
package main

import (
	"encoding/json"
	"fmt"
	"net/http"
	"strings"
	"testing"
)

// ---------------------------------------------------------------------------
// 测试辅助
// ---------------------------------------------------------------------------

func ghOpsBody(ops ...string) string {
	return `{"ops":[` + strings.Join(ops, ",") + `]}`
}

// ghUpsert 拼一条 upsert。previousName 为空时不带该字段，模拟前端「不是改名」的提交。
func ghUpsert(name, session, clientID, previousName string) string {
	prev := ""
	if previousName != "" {
		prev = fmt.Sprintf(`"previous_name":%q,`, previousName)
	}
	return fmt.Sprintf(`{"type":"upsert",%s"account":{"name":%q,"user_session":%q,"client_id":%q}}`,
		prev, name, session, clientID)
}

func ghDelete(name string) string {
	return fmt.Sprintf(`{"type":"delete","name":%q}`, name)
}

// seedPool 种一份「池子 + 引用它的站点账号」的初始配置。
func seedPool(t *testing.T, srv *Server, pool []GitHubAccount, accounts []Account) {
	t.Helper()
	seedConfig(t, srv, accounts, func(c *Config) { c.GitHubAccounts = pool })
}

// poolFromDB 读回库里的池子。
func poolFromDB(t *testing.T, srv *Server) []GitHubAccount {
	t.Helper()
	cfg, _, err := LoadConfig(srv.db)
	if err != nil {
		t.Fatalf("LoadConfig: %v", err)
	}
	return cfg.GitHubAccounts
}

// poolEntry 按名字取池子里的一条，找不到就让测试失败（调用方都是在断言它存在）。
func poolEntry(t *testing.T, srv *Server, name string) GitHubAccount {
	t.Helper()
	for _, g := range poolFromDB(t, srv) {
		if g.Name == name {
			return g
		}
	}
	t.Fatalf("池子里没有 %q，实际 %+v", name, poolFromDB(t, srv))
	return GitHubAccount{}
}

// ghOpsSkipped 发一批操作，要求成功，返回被跳过的说明。
func ghOpsSkipped(t *testing.T, srv *Server, body string) []string {
	t.Helper()
	rr := authedRequest(t, srv, "POST", "/api/github-accounts/ops", body)
	if rr.Code != http.StatusOK {
		t.Fatalf("ops 应成功 = %d, %s", rr.Code, rr.Body.String())
	}
	var resp struct {
		Skipped []string `json:"skipped"`
	}
	if err := json.Unmarshal(rr.Body.Bytes(), &resp); err != nil {
		t.Fatalf("解析响应: %v", err)
	}
	return resp.Skipped
}

// ---------------------------------------------------------------------------
// 增 / 改
// ---------------------------------------------------------------------------

func TestGitHubOpsUpsertAddsAndUpdates(t *testing.T) {
	srv := newTestServer(t)

	if rr := authedRequest(t, srv, "POST", "/api/github-accounts/ops",
		ghOpsBody(ghUpsert("Steven", "sess-1", "cid-1", ""))); rr.Code != http.StatusOK {
		t.Fatalf("新增失败 = %d, %s", rr.Code, rr.Body.String())
	}
	if got := poolEntry(t, srv, "Steven"); got.UserSession != "sess-1" || got.ClientID != "cid-1" {
		t.Fatalf("新增内容不对: %+v", got)
	}

	// 同名再提交是更新，不是追加
	if rr := authedRequest(t, srv, "POST", "/api/github-accounts/ops",
		ghOpsBody(ghUpsert("Steven", "sess-2", "cid-2", ""))); rr.Code != http.StatusOK {
		t.Fatalf("更新失败 = %d, %s", rr.Code, rr.Body.String())
	}
	pool := poolFromDB(t, srv)
	if len(pool) != 1 {
		t.Fatalf("同名提交应更新而不是追加，实际 %+v", pool)
	}
	if pool[0].UserSession != "sess-2" || pool[0].ClientID != "cid-2" {
		t.Fatalf("更新内容不对: %+v", pool[0])
	}
}

func TestGitHubOpsUpdateKeepsMaskedSession(t *testing.T) {
	// 只改 client_id 的场景：前端拿到的 user_session 是 "***"，原样回传。
	// 还原缺失的话，库里会存下 "***" 字面量，界面显示「已填写」而签发必然失败。
	srv := newTestServer(t)
	seedPool(t, srv, []GitHubAccount{{Name: "Steven", UserSession: "real-sess", ClientID: "old-cid"}}, nil)

	rr := authedRequest(t, srv, "POST", "/api/github-accounts/ops",
		ghOpsBody(ghUpsert("Steven", MaskPlaceholder, "new-cid", "")))
	if rr.Code != http.StatusOK {
		t.Fatalf("更新应成功 = %d, %s", rr.Code, rr.Body.String())
	}
	got := poolEntry(t, srv, "Steven")
	if got.UserSession != "real-sess" {
		t.Errorf("session 应保持真值，实际 %q", got.UserSession)
	}
	if got.ClientID != "new-cid" {
		t.Errorf("client_id 应更新为 new-cid，实际 %q", got.ClientID)
	}
}

// ---------------------------------------------------------------------------
// 改名与引用完整性
// ---------------------------------------------------------------------------

func TestGitHubOpsRenameMovesReferences(t *testing.T) {
	// 改名后引用没搬走，那些账号的 github_account 就指向空气：
	// resolveAccountSession 静默回落账号自带的旧字段，不会有任何报错
	srv := newTestServer(t)
	seedPool(t, srv,
		[]GitHubAccount{{Name: "Steven", UserSession: "real-sess", ClientID: "cid"}},
		[]Account{
			{Name: "Steven（a.com）", URL: "https://a.com", LoginMethod: LoginMethodTabiAI,
				GitHubAccount: "Steven", Enabled: true},
			{Name: "Steven（b.com）", URL: "https://b.com", LoginMethod: LoginMethodTabiAI,
				GitHubAccount: "Steven", Enabled: true},
			{Name: "别人（c.com）", URL: "https://c.com", LoginMethod: LoginMethodTabiAI,
				GitHubAccount: "Other", Enabled: true},
		})

	// 改名同时 session 仍是打码值，两件事要一起成立
	rr := authedRequest(t, srv, "POST", "/api/github-accounts/ops",
		ghOpsBody(ghUpsert("StevenNew", MaskPlaceholder, "cid", "Steven")))
	if rr.Code != http.StatusOK {
		t.Fatalf("改名应成功 = %d, %s", rr.Code, rr.Body.String())
	}

	if got := poolEntry(t, srv, "StevenNew"); got.UserSession != "real-sess" {
		t.Errorf("改名后 session 应还原为真值，实际 %q", got.UserSession)
	}
	cfg, _, err := LoadConfig(srv.db)
	if err != nil {
		t.Fatal(err)
	}
	if len(cfg.GitHubAccounts) != 1 {
		t.Fatalf("改名不该新增记录，实际 %+v", cfg.GitHubAccounts)
	}
	for _, a := range cfg.Accounts {
		switch a.Name {
		case "Steven（a.com）", "Steven（b.com）":
			if a.GitHubAccount != "StevenNew" {
				t.Errorf("账号 %q 的引用没搬走，实际 %q", a.Name, a.GitHubAccount)
			}
		case "别人（c.com）":
			if a.GitHubAccount != "Other" {
				t.Errorf("不相关账号的引用被改动了: %q", a.GitHubAccount)
			}
		}
	}
}

func TestGitHubOpsRenameKeepsPoolPosition(t *testing.T) {
	// 改名要就地替换，不能挪到末尾 —— 界面按这个顺序显示，挪位置用户会以为乱了
	srv := newTestServer(t)
	seedPool(t, srv, []GitHubAccount{
		{Name: "first", UserSession: "s1"},
		{Name: "middle", UserSession: "s2"},
		{Name: "last", UserSession: "s3"},
	}, nil)

	if rr := authedRequest(t, srv, "POST", "/api/github-accounts/ops",
		ghOpsBody(ghUpsert("renamed", MaskPlaceholder, "", "middle"))); rr.Code != http.StatusOK {
		t.Fatalf("改名失败 = %d, %s", rr.Code, rr.Body.String())
	}
	pool := poolFromDB(t, srv)
	want := []string{"first", "renamed", "last"}
	if len(pool) != len(want) {
		t.Fatalf("数量变了: %+v", pool)
	}
	for i := range want {
		if pool[i].Name != want[i] {
			t.Fatalf("位置变了: %+v，期望 %v", pool, want)
		}
	}
	if pool[1].UserSession != "s2" {
		t.Errorf("改名后 session 应还原，实际 %q", pool[1].UserSession)
	}
}

func TestGitHubOpsRenameRejectsMissingAndDuplicate(t *testing.T) {
	// 与 accounts ops 的取舍不同：那边旧名没了会退化成新增，这里必须报错。
	// 池子的 upsert 携带的 session 通常是 "***"，退化成新增只会存下一条无效凭据。
	srv := newTestServer(t)
	seedPool(t, srv, []GitHubAccount{
		{Name: "A", UserSession: "sa"},
		{Name: "B", UserSession: "sb"},
	}, nil)

	cases := []struct{ name, body string }{
		{"旧名不存在", ghOpsBody(ghUpsert("C", "sc", "", "gone"))},
		{"改成已存在的名字", ghOpsBody(ghUpsert("B", MaskPlaceholder, "", "A"))},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			rr := authedRequest(t, srv, "POST", "/api/github-accounts/ops", tc.body)
			if rr.Code != http.StatusBadRequest {
				t.Fatalf("应 400，实际 %d: %s", rr.Code, rr.Body.String())
			}
		})
	}
	// 失败不该污染库
	pool := poolFromDB(t, srv)
	if len(pool) != 2 || pool[0].Name != "A" || pool[1].Name != "B" {
		t.Fatalf("失败的改名改动了库: %+v", pool)
	}
	if pool[0].UserSession != "sa" || pool[1].UserSession != "sb" {
		t.Fatalf("失败的改名动了凭据: %+v", pool)
	}
}

// ---------------------------------------------------------------------------
// 删除
// ---------------------------------------------------------------------------

func TestGitHubOpsDeleteUnreferenced(t *testing.T) {
	srv := newTestServer(t)
	seedPool(t, srv, []GitHubAccount{
		{Name: "keep", UserSession: "s1"},
		{Name: "drop", UserSession: "s2"},
	}, nil)

	if rr := authedRequest(t, srv, "POST", "/api/github-accounts/ops",
		ghOpsBody(ghDelete("drop"))); rr.Code != http.StatusOK {
		t.Fatalf("删除失败 = %d, %s", rr.Code, rr.Body.String())
	}
	pool := poolFromDB(t, srv)
	if len(pool) != 1 || pool[0].Name != "keep" {
		t.Fatalf("删除结果不对: %+v", pool)
	}
}

func TestGitHubOpsDeleteRejectedWhenReferenced(t *testing.T) {
	// 不做级联：置空引用会让那些账号静默回落旧字段继续跑（用户以为删干净了），
	// 连站点账号一起删则超出了用户的指令范围
	srv := newTestServer(t)
	seedPool(t, srv,
		[]GitHubAccount{{Name: "Steven", UserSession: "real-sess"}},
		[]Account{
			{Name: "Steven（a.com）", URL: "https://a.com", LoginMethod: LoginMethodTabiAI,
				GitHubAccount: "Steven", Enabled: true},
			{Name: "Steven（b.com）", URL: "https://b.com", LoginMethod: LoginMethodTabiAI,
				GitHubAccount: "Steven", Enabled: true},
		})

	rr := authedRequest(t, srv, "POST", "/api/github-accounts/ops", ghOpsBody(ghDelete("Steven")))
	if rr.Code != http.StatusBadRequest {
		t.Fatalf("有引用时应 400，实际 %d: %s", rr.Code, rr.Body.String())
	}
	// 报错要说清有几个在用，用户才知道要去改哪些
	if !strings.Contains(rr.Body.String(), "2") {
		t.Errorf("错误信息应告知引用数量: %s", rr.Body.String())
	}
	if pool := poolFromDB(t, srv); len(pool) != 1 {
		t.Fatalf("拒绝后不该删除: %+v", pool)
	}
}

func TestGitHubOpsDeleteMissingIsSkippedNotError(t *testing.T) {
	// 别人已经删掉的记录：跳过并在响应里说明。并发编辑下这是正常情况
	srv := newTestServer(t)
	seedPool(t, srv, []GitHubAccount{{Name: "A", UserSession: "sa"}}, nil)

	skipped := ghOpsSkipped(t, srv, ghOpsBody(ghDelete("ghost")))
	if len(skipped) != 1 || !strings.Contains(skipped[0], "已不存在") {
		t.Fatalf("应报告 1 条跳过，实际 %v", skipped)
	}
	if pool := poolFromDB(t, srv); len(pool) != 1 {
		t.Fatalf("现有记录不该受影响: %+v", pool)
	}
}

// ---------------------------------------------------------------------------
// 输入校验与认证
// ---------------------------------------------------------------------------

func TestGitHubOpsValidatesInput(t *testing.T) {
	srv := newTestServer(t)
	cases := []struct{ name, body string }{
		{"空 ops", `{"ops":[]}`},
		{"未知类型", `{"ops":[{"type":"drop","name":"A"}]}`},
		{"upsert 缺 account", `{"ops":[{"type":"upsert"}]}`},
		{"upsert 空名字", ghOpsBody(ghUpsert("", "sess", "", ""))},
		{"upsert 空 session", ghOpsBody(ghUpsert("A", "", "", ""))},
		{"upsert 名字只有空格", ghOpsBody(ghUpsert("   ", "sess", "", ""))},
		{"请求体不是 JSON", `{`},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			rr := authedRequest(t, srv, "POST", "/api/github-accounts/ops", tc.body)
			if rr.Code != http.StatusBadRequest {
				t.Fatalf("应 400，实际 %d: %s", rr.Code, rr.Body.String())
			}
		})
	}
	if pool := poolFromDB(t, srv); len(pool) != 0 {
		t.Fatalf("非法请求不该落库: %+v", pool)
	}
}

func TestGitHubOpsRejectsOversizedBatch(t *testing.T) {
	srv := newTestServer(t)
	ops := make([]string, maxAccountOpsPerRequest+1)
	for i := range ops {
		ops[i] = ghUpsert(fmt.Sprintf("gh-%d", i), "sess", "", "")
	}
	rr := authedRequest(t, srv, "POST", "/api/github-accounts/ops", ghOpsBody(ops...))
	if rr.Code != http.StatusBadRequest {
		t.Fatalf("超限应 400，实际 %d", rr.Code)
	}
}

func TestGitHubOpsRequiresAuth(t *testing.T) {
	srv := newTestServer(t)
	rr := doReq(t, srv, http.MethodPost, "/api/github-accounts/ops", "",
		map[string]any{"ops": []map[string]any{{"type": "delete", "name": "A"}}})
	if rr.Code != http.StatusUnauthorized {
		t.Fatalf("无 token 应 401，实际 %d", rr.Code)
	}
}

func TestGitHubOpsResponseCarriesMaskedConfig(t *testing.T) {
	// 响应回传最新配置供前端直接换上，绝不能带明文 session
	srv := newTestServer(t)
	rr := authedRequest(t, srv, "POST", "/api/github-accounts/ops",
		ghOpsBody(ghUpsert("Steven", "sess-plaintext", "cid", "")))
	if rr.Code != http.StatusOK {
		t.Fatalf("ops 失败 = %d, %s", rr.Code, rr.Body.String())
	}
	if strings.Contains(rr.Body.String(), "sess-plaintext") {
		t.Fatal("响应里出现了明文 session")
	}
	var resp struct {
		Config   Config `json:"config"`
		Revision int64  `json:"revision"`
	}
	if err := json.Unmarshal(rr.Body.Bytes(), &resp); err != nil {
		t.Fatal(err)
	}
	if len(resp.Config.GitHubAccounts) != 1 {
		t.Fatalf("响应里应带上池子: %+v", resp.Config.GitHubAccounts)
	}
	if resp.Config.GitHubAccounts[0].UserSession != MaskPlaceholder {
		t.Errorf("session 应打码，实际 %q", resp.Config.GitHubAccounts[0].UserSession)
	}
	// client_id 不是凭据，要原样回传，否则前端没法显示当前值
	if resp.Config.GitHubAccounts[0].ClientID != "cid" {
		t.Errorf("client_id 应原样回传，实际 %q", resp.Config.GitHubAccounts[0].ClientID)
	}
	if resp.Revision <= 0 {
		t.Errorf("应回传推进后的 revision，实际 %d", resp.Revision)
	}
}

// ---------------------------------------------------------------------------
// 与既有保护的协同
// ---------------------------------------------------------------------------

func TestGitHubOpsBatchRenamesAllRestored(t *testing.T) {
	// 一次批量里改多个名字：unmaskWithPoolRenames 必须逐条处理。
	// 只处理第一条的话，后面那些的 "***" 会因「旧名查不到」而整批 400
	srv := newTestServer(t)
	seedPool(t, srv, []GitHubAccount{
		{Name: "A", UserSession: "sa"},
		{Name: "B", UserSession: "sb"},
	}, nil)

	body := ghOpsBody(
		ghUpsert("A2", MaskPlaceholder, "", "A"),
		ghUpsert("B2", MaskPlaceholder, "", "B"),
	)
	if rr := authedRequest(t, srv, "POST", "/api/github-accounts/ops", body); rr.Code != http.StatusOK {
		t.Fatalf("批量改名应成功 = %d, %s", rr.Code, rr.Body.String())
	}
	if got := poolEntry(t, srv, "A2"); got.UserSession != "sa" {
		t.Errorf("A2 的 session 未还原 = %q", got.UserSession)
	}
	if got := poolEntry(t, srv, "B2"); got.UserSession != "sb" {
		t.Errorf("B2 的 session 未还原 = %q", got.UserSession)
	}
}

func TestGitHubOpsPreservesRotatedCookie(t *testing.T) {
	// 池子端点也走 saveConfigKeepingCookiesLocked：动池子不该碰到
	// 后台刚轮转出来的站点凭据
	srv := newTestServer(t)
	seedPool(t, srv,
		[]GitHubAccount{{Name: "Steven", UserSession: "real-sess"}},
		[]Account{{Name: "tabi", URL: "https://a.com", LoginMethod: LoginMethodTabiAI,
			Cookie: "new_api_refresh=gen1", GitHubAccount: "Steven", Enabled: true}})
	if ok, err := updateAccountCookie(srv.db, "tabi", "new_api_refresh=gen2"); err != nil || !ok {
		t.Fatalf("轮转失败: ok=%v err=%v", ok, err)
	}

	if rr := authedRequest(t, srv, "POST", "/api/github-accounts/ops",
		ghOpsBody(ghUpsert("Steven", "sess-new", "", ""))); rr.Code != http.StatusOK {
		t.Fatalf("ops 失败 = %d, %s", rr.Code, rr.Body.String())
	}
	if got := accountByName(t, srv, "tabi").Cookie; got != "new_api_refresh=gen2" {
		t.Fatalf("轮转出来的站点凭据被动了: %q", got)
	}
	if got := poolEntry(t, srv, "Steven").UserSession; got != "sess-new" {
		t.Fatalf("池子 session 应更新为 sess-new，实际 %q", got)
	}
}
