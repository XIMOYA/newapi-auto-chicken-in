/*
server/account_ops_test.go
账号级增量操作（POST /api/accounts/ops）测试

这些用例守的是两件事：
- 多人同时编辑账号列表不能互相覆盖（这是 ops 存在的唯一理由）
- 改名不再需要重填凭据（打码字段按 previous_name 还原）
*/
package main

import (
	"encoding/json"
	"fmt"
	"net/http"
	"strings"
	"sync"
	"testing"
)

// opsBody 构造 ops 请求体。
func opsBody(ops ...string) string {
	return `{"ops":[` + strings.Join(ops, ",") + `]}`
}

func upsertOp(name, url, method, cookie string, enabled bool, previousName string) string {
	prev := ""
	if previousName != "" {
		prev = fmt.Sprintf(`"previous_name":%q,`, previousName)
	}
	return fmt.Sprintf(`{"type":"upsert",%s"account":{"name":%q,"url":%q,`+
		`"login_method":%q,"cookie":%q,"enabled":%t}}`,
		prev, name, url, method, cookie, enabled)
}

func deleteOp(name string) string {
	return fmt.Sprintf(`{"type":"delete","name":%q}`, name)
}

func setEnabledOp(name string, enabled bool) string {
	return fmt.Sprintf(`{"type":"set_enabled","name":%q,"enabled":%t}`, name, enabled)
}

// postOps 发送 ops 请求并返回响应。
func postOps(t *testing.T, srv *Server, body string) *http.Response {
	t.Helper()
	rr := authedRequest(t, srv, "POST", "/api/accounts/ops", body)
	return rr.Result()
}

func opsSkipped(t *testing.T, srv *Server, body string) []string {
	t.Helper()
	rr := authedRequest(t, srv, "POST", "/api/accounts/ops", body)
	if rr.Code != http.StatusOK {
		t.Fatalf("ops 应成功 = %d, %s", rr.Code, rr.Body.String())
	}
	var resp struct {
		Skipped []string `json:"skipped"`
	}
	if err := json.Unmarshal(rr.Body.Bytes(), &resp); err != nil {
		t.Fatal(err)
	}
	return resp.Skipped
}

func accountNames(t *testing.T, srv *Server) []string {
	t.Helper()
	cfg, _, err := LoadConfig(srv.db)
	if err != nil {
		t.Fatal(err)
	}
	names := make([]string, 0, len(cfg.Accounts))
	for _, a := range cfg.Accounts {
		names = append(names, a.Name)
	}
	return names
}

// ---------------------------------------------------------------------------
// 基本增删改
// ---------------------------------------------------------------------------

func TestAccountOpsUpsertAddsAndUpdates(t *testing.T) {
	srv := newTestServer(t)

	// 新增
	if rr := authedRequest(t, srv, "POST", "/api/accounts/ops",
		opsBody(upsertOp("A", "https://a.com", "newapi_cookie", "session=a", true, ""))); rr.Code != http.StatusOK {
		t.Fatalf("新增失败 = %d, %s", rr.Code, rr.Body.String())
	}
	if got := accountByName(t, srv, "A"); got.Cookie != "session=a" || !got.Enabled {
		t.Fatalf("新增内容不对: %+v", got)
	}

	// 同名更新
	if rr := authedRequest(t, srv, "POST", "/api/accounts/ops",
		opsBody(upsertOp("A", "https://a2.com", "newapi_cookie", "session=a2", false, ""))); rr.Code != http.StatusOK {
		t.Fatalf("更新失败 = %d, %s", rr.Code, rr.Body.String())
	}
	got := accountByName(t, srv, "A")
	if got.URL != "https://a2.com" || got.Cookie != "session=a2" || got.Enabled {
		t.Fatalf("更新内容不对: %+v", got)
	}
	if names := accountNames(t, srv); len(names) != 1 {
		t.Fatalf("同名更新不该新增一条: %v", names)
	}
}

func TestAccountOpsDeleteAndSetEnabled(t *testing.T) {
	srv := newTestServer(t)
	seedConfig(t, srv, []Account{
		{Name: "A", URL: "https://a.com", LoginMethod: LoginMethodNewAPICookie, Cookie: "ca", Enabled: true},
		{Name: "B", URL: "https://b.com", LoginMethod: LoginMethodNewAPICookie, Cookie: "cb", Enabled: true},
		{Name: "C", URL: "https://c.com", LoginMethod: LoginMethodNewAPICookie, Cookie: "cc", Enabled: true},
	}, nil)

	// 批量启停 + 删除混在一个请求里，按顺序重放
	body := opsBody(setEnabledOp("A", false), setEnabledOp("B", false), deleteOp("C"))
	if rr := authedRequest(t, srv, "POST", "/api/accounts/ops", body); rr.Code != http.StatusOK {
		t.Fatalf("ops 失败 = %d, %s", rr.Code, rr.Body.String())
	}
	if accountByName(t, srv, "A").Enabled || accountByName(t, srv, "B").Enabled {
		t.Error("A/B 应已停用")
	}
	names := accountNames(t, srv)
	if len(names) != 2 {
		t.Fatalf("C 应被删除，剩余 %v", names)
	}
	// 未被操作的字段不受影响
	if accountByName(t, srv, "A").Cookie != "ca" {
		t.Error("set_enabled 不该动其他字段")
	}
}

// ---------------------------------------------------------------------------
// 改名（需求 1 的核心）
// ---------------------------------------------------------------------------

func TestAccountOpsRenameKeepsCredentials(t *testing.T) {
	// 改名时前端提交的 cookie 仍是占位符 "***"，服务端要靠 previous_name 找回真值。
	// 修复前这里会返回 400「账号改名后需要重新填写站点 Cookie」。
	srv := newTestServer(t)
	seedConfig(t, srv, []Account{
		{Name: "old-name", URL: "https://a.com", LoginMethod: LoginMethodTabiAI,
			Cookie: "new_api_refresh=secret", GithubUserSession: "gh-session", Enabled: true},
	}, nil)

	body := opsBody(fmt.Sprintf(
		`{"type":"upsert","previous_name":"old-name","account":{"name":"new-name",`+
			`"url":"https://a.com","login_method":"tabiai","cookie":%q,`+
			`"github_user_session":%q,"enabled":true}}`, MaskPlaceholder, MaskPlaceholder))
	rr := authedRequest(t, srv, "POST", "/api/accounts/ops", body)
	if rr.Code != http.StatusOK {
		t.Fatalf("改名应成功 = %d, %s", rr.Code, rr.Body.String())
	}

	got := accountByName(t, srv, "new-name")
	if got.Cookie != "new_api_refresh=secret" {
		t.Errorf("改名后 cookie 未还原: %q", got.Cookie)
	}
	if got.GithubUserSession != "gh-session" {
		t.Errorf("改名后 user_session 未还原: %q", got.GithubUserSession)
	}
	if names := accountNames(t, srv); len(names) != 1 || names[0] != "new-name" {
		t.Errorf("改名应就地替换而非新增: %v", names)
	}
}

func TestAccountOpsRenameKeepsListPosition(t *testing.T) {
	// 改名要就地替换，不能把账号挪到列表末尾（否则用户会以为顺序乱了）
	srv := newTestServer(t)
	seedConfig(t, srv, []Account{
		{Name: "first", URL: "https://a.com", LoginMethod: LoginMethodNewAPICookie, Cookie: "c1", Enabled: true},
		{Name: "middle", URL: "https://b.com", LoginMethod: LoginMethodNewAPICookie, Cookie: "c2", Enabled: true},
		{Name: "last", URL: "https://c.com", LoginMethod: LoginMethodNewAPICookie, Cookie: "c3", Enabled: true},
	}, nil)

	body := opsBody(upsertOp("renamed", "https://b.com", "newapi_cookie", MaskPlaceholder, true, "middle"))
	if rr := authedRequest(t, srv, "POST", "/api/accounts/ops", body); rr.Code != http.StatusOK {
		t.Fatalf("改名失败 = %d, %s", rr.Code, rr.Body.String())
	}
	names := accountNames(t, srv)
	want := []string{"first", "renamed", "last"}
	for i := range want {
		if names[i] != want[i] {
			t.Fatalf("位置变了: %v，期望 %v", names, want)
		}
	}
	if accountByName(t, srv, "renamed").Cookie != "c2" {
		t.Error("改名后凭据未还原")
	}
}

func TestAccountOpsRenameRejectsDuplicate(t *testing.T) {
	// 「不冲突其他账号」这条约束由 ValidateConfig 的唯一性校验兜住
	srv := newTestServer(t)
	seedConfig(t, srv, []Account{
		{Name: "A", URL: "https://a.com", LoginMethod: LoginMethodNewAPICookie, Cookie: "ca", Enabled: true},
		{Name: "B", URL: "https://b.com", LoginMethod: LoginMethodNewAPICookie, Cookie: "cb", Enabled: true},
	}, nil)

	body := opsBody(upsertOp("B", "https://a.com", "newapi_cookie", MaskPlaceholder, true, "A"))
	rr := authedRequest(t, srv, "POST", "/api/accounts/ops", body)
	if rr.Code != http.StatusBadRequest {
		t.Fatalf("改成已存在的名字应 400，实际 %d: %s", rr.Code, rr.Body.String())
	}
	// 失败不该污染库
	names := accountNames(t, srv)
	if len(names) != 2 || names[0] != "A" || names[1] != "B" {
		t.Fatalf("失败的改名改动了库: %v", names)
	}
	if accountByName(t, srv, "A").Cookie != "ca" {
		t.Error("失败的改名动了凭据")
	}
}

// ---------------------------------------------------------------------------
// 并发（需求 2 的核心）
// ---------------------------------------------------------------------------

func TestAccountOpsConcurrentAddsBothSurvive(t *testing.T) {
	// 两个人同时加不同账号：整份覆盖时代必然一个 409 或被抹掉，ops 下都该活着
	srv := newTestServer(t)

	var wg sync.WaitGroup
	names := []string{"alice", "bob", "carol", "dave", "erin"}
	errs := make([]int, len(names))
	for i, name := range names {
		wg.Add(1)
		go func(i int, name string) {
			defer wg.Done()
			rr := authedRequest(t, srv, "POST", "/api/accounts/ops",
				opsBody(upsertOp(name, "https://"+name+".com", "newapi_cookie",
					"session="+name, true, "")))
			errs[i] = rr.Code
		}(i, name)
	}
	wg.Wait()

	for i, code := range errs {
		if code != http.StatusOK {
			t.Errorf("账号 %q 的并发新增返回 %d", names[i], code)
		}
	}
	got := accountNames(t, srv)
	if len(got) != len(names) {
		t.Fatalf("并发新增后应有 %d 个账号，实际 %d 个: %v", len(names), len(got), got)
	}
	for _, name := range names {
		if a := accountByName(t, srv, name); a.Cookie != "session="+name {
			t.Errorf("账号 %q 的凭据被串了: %q", name, a.Cookie)
		}
	}
}

func TestAccountOpsDeleteIsNotResurrected(t *testing.T) {
	// A 删掉 X 之后，B 的 upsert（B 的界面上还有 X）不该把 X 复活 ——
	// 因为 B 提交的是「改我这条」的意图，不是整份快照
	srv := newTestServer(t)
	seedConfig(t, srv, []Account{
		{Name: "X", URL: "https://x.com", LoginMethod: LoginMethodNewAPICookie, Cookie: "cx", Enabled: true},
		{Name: "Y", URL: "https://y.com", LoginMethod: LoginMethodNewAPICookie, Cookie: "cy", Enabled: true},
	}, nil)

	// A：删 X
	if rr := authedRequest(t, srv, "POST", "/api/accounts/ops", opsBody(deleteOp("X"))); rr.Code != http.StatusOK {
		t.Fatalf("删除失败 = %d", rr.Code)
	}
	// B：改 Y（B 的界面里 X 还在，但它不会被提交）
	if rr := authedRequest(t, srv, "POST", "/api/accounts/ops",
		opsBody(upsertOp("Y", "https://y2.com", "newapi_cookie", MaskPlaceholder, true, ""))); rr.Code != http.StatusOK {
		t.Fatalf("更新 Y 失败 = %d", rr.Code)
	}

	names := accountNames(t, srv)
	if len(names) != 1 || names[0] != "Y" {
		t.Fatalf("X 被复活了: %v", names)
	}
	if accountByName(t, srv, "Y").URL != "https://y2.com" {
		t.Error("Y 的修改没生效")
	}
}

func TestAccountOpsMissingTargetIsSkippedNotError(t *testing.T) {
	// 别人已经删掉的账号：跳过并在响应里说明，不该报错让用户困惑
	srv := newTestServer(t)
	seedConfig(t, srv, []Account{
		{Name: "A", URL: "https://a.com", LoginMethod: LoginMethodNewAPICookie, Cookie: "ca", Enabled: true},
	}, nil)

	skipped := opsSkipped(t, srv, opsBody(deleteOp("ghost"), setEnabledOp("phantom", false)))
	if len(skipped) != 2 {
		t.Fatalf("应报告 2 条跳过，实际 %v", skipped)
	}
	for _, s := range skipped {
		if !strings.Contains(s, "已不存在") {
			t.Errorf("跳过说明应指出原因: %q", s)
		}
	}
	if names := accountNames(t, srv); len(names) != 1 {
		t.Fatalf("现有账号不该受影响: %v", names)
	}
}

func TestAccountOpsRenameOfMissingFallsBackToInsert(t *testing.T) {
	// A 正在编辑 X 时 B 删掉了 X：A 保存时按新增处理，并报告这次退化
	srv := newTestServer(t)

	body := opsBody(upsertOp("new-name", "https://a.com", "newapi_cookie", "session=fresh", true, "gone"))
	skipped := opsSkipped(t, srv, body)
	if len(skipped) != 1 || !strings.Contains(skipped[0], "按新增处理") {
		t.Fatalf("应报告退化为新增，实际 %v", skipped)
	}
	if got := accountByName(t, srv, "new-name"); got.Cookie != "session=fresh" {
		t.Fatalf("退化新增的内容不对: %+v", got)
	}
}

// ---------------------------------------------------------------------------
// 与既有保护的协同
// ---------------------------------------------------------------------------

func TestAccountOpsPreservesRotatedCookie(t *testing.T) {
	// ops 也走 SaveConfigKeepingCookies：陈旧的明文 cookie 不该覆盖轮转值
	srv := newTestServer(t)
	seedConfig(t, srv, []Account{
		{Name: "tabi", URL: "https://a.com", LoginMethod: LoginMethodTabiAI,
			Cookie: "new_api_refresh=gen1", Enabled: true},
	}, nil)
	if ok, err := updateAccountCookie(srv.db, "tabi", "new_api_refresh=gen2"); err != nil || !ok {
		t.Fatalf("轮转失败: ok=%v err=%v", ok, err)
	}

	body := opsBody(upsertOp("tabi", "https://a.com", "tabiai", "new_api_refresh=gen1", true, ""))
	if rr := authedRequest(t, srv, "POST", "/api/accounts/ops", body); rr.Code != http.StatusOK {
		t.Fatalf("ops 失败 = %d, %s", rr.Code, rr.Body.String())
	}
	if got := accountByName(t, srv, "tabi").Cookie; got != "new_api_refresh=gen2" {
		t.Fatalf("轮转值被陈旧请求覆盖成 %q", got)
	}
}

func TestAccountOpsBumpsRevision(t *testing.T) {
	// 账号变更是用户可见的，必须推进版本号，别人才能通过轮询感知到
	srv := newTestServer(t)
	_, _, before, err := LoadConfigWithRevision(srv.db)
	if err != nil {
		t.Fatal(err)
	}
	if rr := authedRequest(t, srv, "POST", "/api/accounts/ops",
		opsBody(upsertOp("A", "https://a.com", "newapi_cookie", "ca", true, ""))); rr.Code != http.StatusOK {
		t.Fatalf("ops 失败 = %d", rr.Code)
	}
	_, _, after, err := LoadConfigWithRevision(srv.db)
	if err != nil {
		t.Fatal(err)
	}
	if after <= before {
		t.Errorf("ops 应推进 revision（%d -> %d）", before, after)
	}
}

func TestAccountOpsResponseCarriesMaskedConfig(t *testing.T) {
	// 响应直接回传最新打码配置，前端换上即可，省一次往返；但绝不能带明文
	srv := newTestServer(t)
	rr := authedRequest(t, srv, "POST", "/api/accounts/ops",
		opsBody(upsertOp("A", "https://a.com", "newapi_cookie", "session=secret", true, "")))
	if rr.Code != http.StatusOK {
		t.Fatalf("ops 失败 = %d", rr.Code)
	}
	if strings.Contains(rr.Body.String(), "session=secret") {
		t.Fatal("响应里出现了明文凭据")
	}
	var resp struct {
		Config   Config `json:"config"`
		Revision int64  `json:"revision"`
	}
	if err := json.Unmarshal(rr.Body.Bytes(), &resp); err != nil {
		t.Fatal(err)
	}
	if len(resp.Config.Accounts) != 1 || resp.Config.Accounts[0].Cookie != MaskPlaceholder {
		t.Fatalf("响应配置应为打码态: %+v", resp.Config.Accounts)
	}
	if resp.Revision <= 0 {
		t.Errorf("响应应带新 revision，实际 %d", resp.Revision)
	}
}

// ---------------------------------------------------------------------------
// 输入校验与鉴权
// ---------------------------------------------------------------------------

func TestAccountOpsValidatesInput(t *testing.T) {
	srv := newTestServer(t)
	cases := []struct{ name, body string }{
		{"空 ops", `{"ops":[]}`},
		{"未知类型", `{"ops":[{"type":"drop","name":"A"}]}`},
		{"upsert 缺 account", `{"ops":[{"type":"upsert"}]}`},
		{"upsert 空名字", `{"ops":[{"type":"upsert","account":{"name":"","url":"https://a.com"}}]}`},
		{"delete 缺 name", `{"ops":[{"type":"delete"}]}`},
		{"set_enabled 缺 name", `{"ops":[{"type":"set_enabled","enabled":true}]}`},
		{"url 非法", `{"ops":[{"type":"upsert","account":{"name":"A","url":"ftp://x"}}]}`},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			rr := authedRequest(t, srv, "POST", "/api/accounts/ops", tc.body)
			if rr.Code != http.StatusBadRequest {
				t.Fatalf("应 400，实际 %d: %s", rr.Code, rr.Body.String())
			}
		})
	}
}

func TestAccountOpsRequiresJWT(t *testing.T) {
	srv := newTestServer(t)
	resp := postOps(t, srv, opsBody(deleteOp("A")))
	_ = resp.Body.Close()
	// postOps 走的是 authedRequest，这里单独构造一个无 token 的请求
	rr := doReq(t, srv, http.MethodPost, "/api/accounts/ops", "",
		map[string]any{"ops": []map[string]any{{"type": "delete", "name": "A"}}})
	if rr.Code != http.StatusUnauthorized {
		t.Fatalf("无 token 应 401，实际 %d", rr.Code)
	}
}

func TestConfigRevisionEndpoint(t *testing.T) {
	srv := newTestServer(t)
	token := loginToken(t, srv)

	rr := doReq(t, srv, http.MethodGet, "/api/config/revision", token, nil)
	if rr.Code != http.StatusOK {
		t.Fatalf("状态 = %d, %s", rr.Code, rr.Body.String())
	}
	var light struct {
		Revision  int64  `json:"revision"`
		UpdatedAt string `json:"updated_at"`
	}
	decodeJSON(t, rr, &light)

	// 与全量接口的 revision 必须一致
	full := doReq(t, srv, http.MethodGet, "/api/config", token, nil)
	var heavy struct {
		Revision int64 `json:"revision"`
	}
	decodeJSON(t, full, &heavy)
	if light.Revision != heavy.Revision {
		t.Errorf("轻量端点 revision=%d 与全量 %d 不一致", light.Revision, heavy.Revision)
	}
	// 有意不返回 updated_at：轮转会改它但不改 revision，返回了容易被误用来判断变更
	if light.UpdatedAt != "" {
		t.Errorf("不该返回 updated_at，实际 %q", light.UpdatedAt)
	}
	if noAuth := doReq(t, srv, http.MethodGet, "/api/config/revision", "", nil); noAuth.Code != http.StatusUnauthorized {
		t.Errorf("无 token 应 401，实际 %d", noAuth.Code)
	}
}
