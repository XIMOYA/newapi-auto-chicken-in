/*
server/config_write_guard_test.go
配置写入的凭据保护测试（审计发现 H-1 / H-2）

守住两条底线：
- TaBiAI 的 new_api_refresh 由后台签到持续轮转，陈旧请求体不得把它写回旧代次
  （旧代次重用会被站点判为重放，整条会话被撤销）
- 打码占位符 "***" 绝不能落库，任何写入路径漏了 UnmaskConfig 都要被兜底拦下
*/
package main

import (
	"encoding/json"
	"net/http"
	"path/filepath"
	"strconv"
	"testing"
)

// seedConfig 直接写库，绕过接口，用来摆出「库里已有什么」的初始状态。
func seedConfig(t *testing.T, srv *Server, accounts []Account, mutate func(*Config)) {
	t.Helper()
	cfg := DefaultConfig()
	cfg.Accounts = accounts
	if mutate != nil {
		mutate(&cfg)
	}
	if _, err := SaveConfig(srv.db, cfg); err != nil {
		t.Fatalf("seedConfig: %v", err)
	}
}

func accountByName(t *testing.T, srv *Server, name string) Account {
	t.Helper()
	cfg, _, err := LoadConfig(srv.db)
	if err != nil {
		t.Fatalf("LoadConfig: %v", err)
	}
	for _, a := range cfg.Accounts {
		if a.Name == name {
			return a
		}
	}
	t.Fatalf("账号 %q 不存在", name)
	return Account{}
}

// ---------------------------------------------------------------------------
// H-1：陈旧请求体不得覆盖轮转后的 TaBiAI 凭据
// ---------------------------------------------------------------------------

func TestNilRevisionPutKeepsStoredTabiAICookie(t *testing.T) {
	srv := newTestServer(t)
	seedConfig(t, srv, []Account{
		{Name: "tabi", URL: "https://a.com", LoginMethod: LoginMethodTabiAI,
			Cookie: "new_api_refresh=gen1", Enabled: true},
	}, nil)

	// 后台签到轮转到 gen2
	if ok, err := updateAccountCookie(srv.db, "tabi", "new_api_refresh=gen2"); err != nil || !ok {
		t.Fatalf("轮转失败: ok=%v err=%v", ok, err)
	}

	// 外部脚本拿着几小时前的快照（gen1）提交，不带 revision
	body := `{"config":{"accounts":[{"name":"tabi","url":"https://a.com",` +
		`"login_method":"tabiai","cookie":"new_api_refresh=gen1","enabled":true}]}}`
	if rr := authedRequest(t, srv, "PUT", "/api/config", body); rr.Code != http.StatusOK {
		t.Fatalf("PUT 应成功 = %d, %s", rr.Code, rr.Body.String())
	}

	if got := accountByName(t, srv, "tabi").Cookie; got != "new_api_refresh=gen2" {
		t.Fatalf("轮转后的凭据被陈旧请求体覆盖: %q，期望 gen2", got)
	}
}

func TestNilRevisionPutStillAllowsNewAccountCookie(t *testing.T) {
	// 空库首次写入：库中没有同名账号可保留，请求体是唯一来源
	srv := newTestServer(t)
	body := `{"config":{"accounts":[{"name":"fresh","url":"https://a.com",` +
		`"login_method":"tabiai","cookie":"new_api_refresh=sid.first","enabled":true}]}}`
	if rr := authedRequest(t, srv, "PUT", "/api/config", body); rr.Code != http.StatusOK {
		t.Fatalf("PUT 应成功 = %d, %s", rr.Code, rr.Body.String())
	}
	if got := accountByName(t, srv, "fresh").Cookie; got != "new_api_refresh=sid.first" {
		t.Fatalf("新增账号的凭据被误清: %q", got)
	}
}

func TestNilRevisionPutStillAllowsNewAPICookieChange(t *testing.T) {
	// newapi_cookie 的 session 是静态凭据、不会轮转，用户改它是正当操作，不该被拦
	srv := newTestServer(t)
	seedConfig(t, srv, []Account{
		{Name: "site", URL: "https://a.com", LoginMethod: LoginMethodNewAPICookie,
			Cookie: "session=old", Enabled: true},
	}, nil)

	body := `{"config":{"accounts":[{"name":"site","url":"https://a.com",` +
		`"login_method":"newapi_cookie","cookie":"session=new","enabled":true}]}}`
	if rr := authedRequest(t, srv, "PUT", "/api/config", body); rr.Code != http.StatusOK {
		t.Fatalf("PUT 应成功 = %d, %s", rr.Code, rr.Body.String())
	}
	if got := accountByName(t, srv, "site").Cookie; got != "session=new" {
		t.Fatalf("站点 Cookie 应能正常修改，实际 %q", got)
	}
}

func TestRevisionPutCanStillChangeTabiAICookie(t *testing.T) {
	// 带 revision 是「显式修改凭据」的正规入口，必须仍然生效
	srv := newTestServer(t)
	seedConfig(t, srv, []Account{
		{Name: "tabi", URL: "https://a.com", LoginMethod: LoginMethodTabiAI,
			Cookie: "new_api_refresh=gen1", Enabled: true},
	}, nil)

	_, _, revision, err := LoadConfigWithRevision(srv.db)
	if err != nil {
		t.Fatal(err)
	}
	body := `{"revision":` + strconv.FormatInt(revision, 10) + `,"config":{"accounts":[{"name":"tabi",` +
		`"url":"https://a.com","login_method":"tabiai","cookie":"new_api_refresh=manual","enabled":true}]}}`
	if rr := authedRequest(t, srv, "PUT", "/api/config", body); rr.Code != http.StatusOK {
		t.Fatalf("带 revision 的 PUT 应成功 = %d, %s", rr.Code, rr.Body.String())
	}
	if got := accountByName(t, srv, "tabi").Cookie; got != "new_api_refresh=manual" {
		t.Fatalf("带 revision 时应允许改凭据，实际 %q", got)
	}
}

func TestImportKeepsStoredTabiAICookie(t *testing.T) {
	srv := newTestServer(t)
	seedConfig(t, srv, []Account{
		{Name: "tabi", URL: "https://a.com", LoginMethod: LoginMethodTabiAI,
			Cookie: "new_api_refresh=gen1", Enabled: true},
	}, nil)
	if ok, err := updateAccountCookie(srv.db, "tabi", "new_api_refresh=gen2"); err != nil || !ok {
		t.Fatalf("轮转失败: ok=%v err=%v", ok, err)
	}

	// 用户导入几小时前的导出文件（里面是 gen1）
	body := importBody("overwrite", nil,
		`{"accounts":[{"name":"tabi","url":"https://a.com","login_method":"tabiai",`+
			`"cookie":"new_api_refresh=gen1","enabled":true}]}`)
	if rr := authedRequest(t, srv, "POST", "/api/config/import", body); rr.Code != http.StatusOK {
		t.Fatalf("导入应成功 = %d, %s", rr.Code, rr.Body.String())
	}
	if got := accountByName(t, srv, "tabi").Cookie; got != "new_api_refresh=gen2" {
		t.Fatalf("导入把轮转后的凭据打回了 %q，期望 gen2", got)
	}
}

func TestRotationAndBulkWriteShareOneLock(t *testing.T) {
	// 定点写与整份写必须互斥：并发跑一批，最终值必须是某次完整写入的结果，不能是撕裂状态
	db, err := OpenDB(filepath.Join(t.TempDir(), "lock.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()

	cfg := DefaultConfig()
	cfg.Accounts = []Account{
		{Name: "tabi", URL: "https://a.com", LoginMethod: LoginMethodTabiAI,
			Cookie: "new_api_refresh=gen0", Enabled: true},
	}
	if _, err := SaveConfig(db, cfg); err != nil {
		t.Fatal(err)
	}

	done := make(chan struct{})
	go func() {
		defer close(done)
		for i := 0; i < 20; i++ {
			if _, err := updateAccountCookie(db, "tabi", "new_api_refresh=rot"); err != nil {
				t.Errorf("轮转写失败: %v", err)
				return
			}
		}
	}()
	for i := 0; i < 20; i++ {
		snapshot := cfg
		snapshot.AI.Timeout = 40 + i
		if _, err := SaveConfigKeepingCookies(db, snapshot); err != nil {
			t.Fatalf("整份写失败: %v", err)
		}
	}
	<-done

	final, _, err := LoadConfig(db)
	if err != nil {
		t.Fatal(err)
	}
	// 整份写始终保留库中 tabiai 凭据，所以轮转值不可能丢
	if final.Accounts[0].Cookie != "new_api_refresh=rot" {
		t.Fatalf("并发后凭据为 %q，期望轮转值 rot", final.Accounts[0].Cookie)
	}
}

// ---------------------------------------------------------------------------
// H-2：import 必须还原打码占位符
// ---------------------------------------------------------------------------

func TestImportUnmasksOtherSecrets(t *testing.T) {
	srv := newTestServer(t)
	seedConfig(t, srv, []Account{
		{Name: "A", URL: "https://a.com", LoginMethod: LoginMethodNewAPICookie,
			Cookie: "session=real", Enabled: true},
	}, func(c *Config) {
		c.AI.APIKey = "sk-real-key"
		c.Notify.Email.Password = "smtp-real"
		c.ConfigSync.Token = "sync-real"
		c.ProxyPool.RemoteToken = "proxy-real"
		c.Security.ConfigKey = "cfgkey-real"
	})

	// 直接把 GET /api/config 的打码响应导回去 —— 这是最容易踩的用法
	getRR := authedRequest(t, srv, "GET", "/api/config", "")
	if getRR.Code != http.StatusOK {
		t.Fatalf("读取配置失败: %d", getRR.Code)
	}
	var got struct {
		Config json.RawMessage `json:"config"`
	}
	if err := json.Unmarshal(getRR.Body.Bytes(), &got); err != nil {
		t.Fatal(err)
	}

	body := importBody("overwrite", nil, string(got.Config))
	if rr := authedRequest(t, srv, "POST", "/api/config/import", body); rr.Code != http.StatusOK {
		t.Fatalf("导入应成功 = %d, %s", rr.Code, rr.Body.String())
	}

	saved, _, err := LoadConfig(srv.db)
	if err != nil {
		t.Fatal(err)
	}
	checks := map[string]string{
		"ai.api_key":              saved.AI.APIKey,
		"notify.email.password":   saved.Notify.Email.Password,
		"config_sync.token":       saved.ConfigSync.Token,
		"proxy_pool.remote_token": saved.ProxyPool.RemoteToken,
		"security.config_key":     saved.Security.ConfigKey,
		"accounts[0].cookie":      saved.Accounts[0].Cookie,
	}
	want := map[string]string{
		"ai.api_key":              "sk-real-key",
		"notify.email.password":   "smtp-real",
		"config_sync.token":       "sync-real",
		"proxy_pool.remote_token": "proxy-real",
		"security.config_key":     "cfgkey-real",
		"accounts[0].cookie":      "session=real",
	}
	for field, actual := range checks {
		if actual == MaskPlaceholder {
			t.Errorf("%s 落库成了占位符字面量，真实凭据已被摧毁", field)
			continue
		}
		if actual != want[field] {
			t.Errorf("%s = %q，期望还原为 %q", field, actual, want[field])
		}
	}
}

func TestImportRejectsUnresolvablePlaceholder(t *testing.T) {
	// 改名 + 占位符：旧配置里找不到同名账号，无法还原，必须 400 而不是把 "***" 落库
	srv := newTestServer(t)
	seedConfig(t, srv, []Account{
		{Name: "old-name", URL: "https://a.com", LoginMethod: LoginMethodNewAPICookie,
			Cookie: "session=real", Enabled: true},
	}, nil)

	body := importBody("overwrite", nil,
		`{"accounts":[{"name":"new-name","url":"https://a.com",`+
			`"login_method":"newapi_cookie","cookie":"***","enabled":true}]}`)
	rr := authedRequest(t, srv, "POST", "/api/config/import", body)
	if rr.Code != http.StatusBadRequest {
		t.Fatalf("无法还原的占位符应返回 400，实际 %d: %s", rr.Code, rr.Body.String())
	}
	// 原账号必须完好无损
	if got := accountByName(t, srv, "old-name").Cookie; got != "session=real" {
		t.Fatalf("失败的导入污染了库: %q", got)
	}
}

func TestSaveConfigKeepingCookiesSanitizesLeftovers(t *testing.T) {
	// 兜底防线：即使调用方漏了 UnmaskConfig，"***" 也不该落库
	db, err := OpenDB(filepath.Join(t.TempDir(), "sanitize.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if err := EnsureDefaultConfig(db); err != nil {
		t.Fatal(err)
	}

	cfg := DefaultConfig()
	cfg.Accounts = []Account{
		{Name: "A", URL: "https://a.com", LoginMethod: LoginMethodNewAPICookie,
			Cookie: MaskPlaceholder, Enabled: true},
	}
	cfg.AI.APIKey = MaskPlaceholder
	if _, err := SaveConfigKeepingCookies(db, cfg); err != nil {
		t.Fatal(err)
	}

	saved, _, err := LoadConfig(db)
	if err != nil {
		t.Fatal(err)
	}
	if saved.Accounts[0].Cookie == MaskPlaceholder || saved.AI.APIKey == MaskPlaceholder {
		t.Fatalf("占位符未被兜底清理: cookie=%q api_key=%q",
			saved.Accounts[0].Cookie, saved.AI.APIKey)
	}
}

// ---------------------------------------------------------------------------
// 凭据轮转不占用 revision（多人编辑无感同步的前提）
// ---------------------------------------------------------------------------

func TestRotationDoesNotBumpRevision(t *testing.T) {
	// 线上现象：一轮 Cookie 检测给每个 tabiai 账号轮转一次凭据，每次都 bump revision，
	// 于是所有打开的编辑页全部失效 —— 而用户改的往往是 AI 超时这类无关字段。
	db, err := OpenDB(filepath.Join(t.TempDir(), "rev.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()

	cfg := DefaultConfig()
	cfg.Accounts = []Account{
		{Name: "tabi", URL: "https://a.com", LoginMethod: LoginMethodTabiAI,
			Cookie: "new_api_refresh=gen1", Enabled: true},
	}
	if _, err := SaveConfig(db, cfg); err != nil {
		t.Fatal(err)
	}
	_, _, before, err := LoadConfigWithRevision(db)
	if err != nil {
		t.Fatal(err)
	}

	// 连续轮转多次，模拟一轮检测扫过多个账号
	for i := 0; i < 3; i++ {
		if ok, err := updateAccountCookie(db, "tabi", "new_api_refresh=gen2"); err != nil || !ok {
			t.Fatalf("轮转失败: ok=%v err=%v", ok, err)
		}
	}

	stored, _, after, err := LoadConfigWithRevision(db)
	if err != nil {
		t.Fatal(err)
	}
	if after != before {
		t.Errorf("轮转推进了 revision（%d -> %d），打开的编辑页会被无关写入打断", before, after)
	}
	// 值本身必须真的写进去了
	if stored.Accounts[0].Cookie != "new_api_refresh=gen2" {
		t.Errorf("轮转值没落库: %q", stored.Accounts[0].Cookie)
	}
}

func TestUserEditsStillBumpRevision(t *testing.T) {
	// 对照：用户可见的变更仍然要推进版本号，否则乐观锁就失效了
	srv := newTestServer(t)
	seedConfig(t, srv, []Account{
		{Name: "A", URL: "https://a.com", LoginMethod: LoginMethodNewAPICookie,
			Cookie: "session=x", Enabled: true},
	}, nil)
	_, _, before, err := LoadConfigWithRevision(srv.db)
	if err != nil {
		t.Fatal(err)
	}

	body := `{"revision":` + strconv.FormatInt(before, 10) + `,"config":{"accounts":[{"name":"A",` +
		`"url":"https://a.com","login_method":"newapi_cookie","cookie":"***","enabled":false}]}}`
	if rr := authedRequest(t, srv, "PUT", "/api/config", body); rr.Code != http.StatusOK {
		t.Fatalf("保存失败 = %d, %s", rr.Code, rr.Body.String())
	}
	_, _, after, err := LoadConfigWithRevision(srv.db)
	if err != nil {
		t.Fatal(err)
	}
	if after <= before {
		t.Errorf("用户编辑应推进 revision（%d -> %d）", before, after)
	}
}

func TestRevisionPutStillSafeAfterSilentRotation(t *testing.T) {
	// 改动「轮转不 bump revision」唯一可能破坏 H-1 的地方：
	// 客户端读到 revision=N，后台静默轮转 gen1->gen2（revision 仍是 N），
	// 客户端带 revision=N 提交且 cookie 为占位符 —— 版本号匹配，会真的写入。
	// 保护来自 handlePutConfig 重读最新 oldCfg 后的 UnmaskConfig 还原。
	srv := newTestServer(t)
	seedConfig(t, srv, []Account{
		{Name: "tabi", URL: "https://a.com", LoginMethod: LoginMethodTabiAI,
			Cookie: "new_api_refresh=gen1", Enabled: true},
	}, nil)

	_, _, revision, err := LoadConfigWithRevision(srv.db)
	if err != nil {
		t.Fatal(err)
	}
	if ok, err := updateAccountCookie(srv.db, "tabi", "new_api_refresh=gen2"); err != nil || !ok {
		t.Fatalf("轮转失败: ok=%v err=%v", ok, err)
	}
	// 前提确认：轮转没改版本号，所以下面这次提交不会被 409 挡住
	_, _, afterRotate, err := LoadConfigWithRevision(srv.db)
	if err != nil {
		t.Fatal(err)
	}
	if afterRotate != revision {
		t.Fatalf("前提不成立：轮转把 revision 从 %d 改成了 %d", revision, afterRotate)
	}

	// 前端提交的 cookie 恒为占位符（界面上看到的就是 "***"）
	body := `{"revision":` + strconv.FormatInt(revision, 10) + `,"config":{"accounts":[{"name":"tabi",` +
		`"url":"https://a.com","login_method":"tabiai","cookie":"***","enabled":true}]}}`
	if rr := authedRequest(t, srv, "PUT", "/api/config", body); rr.Code != http.StatusOK {
		t.Fatalf("版本号匹配应保存成功 = %d, %s", rr.Code, rr.Body.String())
	}
	if got := accountByName(t, srv, "tabi").Cookie; got != "new_api_refresh=gen2" {
		t.Fatalf("占位符被还原成了旧代次 %q，期望 gen2", got)
	}
}

func TestRevisionPutWritesExplicitPlaintextCookie(t *testing.T) {
	// 反向断言：带 revision 且提交明文（非占位符）时，按用户意图写入。
	// 这是「显式修改凭据」的正规入口，把行为钉住，避免以后被误判为 bug。
	srv := newTestServer(t)
	seedConfig(t, srv, []Account{
		{Name: "tabi", URL: "https://a.com", LoginMethod: LoginMethodTabiAI,
			Cookie: "new_api_refresh=gen1", Enabled: true},
	}, nil)

	_, _, revision, err := LoadConfigWithRevision(srv.db)
	if err != nil {
		t.Fatal(err)
	}
	body := `{"revision":` + strconv.FormatInt(revision, 10) + `,"config":{"accounts":[{"name":"tabi",` +
		`"url":"https://a.com","login_method":"tabiai","cookie":"new_api_refresh=manual","enabled":true}]}}`
	if rr := authedRequest(t, srv, "PUT", "/api/config", body); rr.Code != http.StatusOK {
		t.Fatalf("保存失败 = %d, %s", rr.Code, rr.Body.String())
	}
	if got := accountByName(t, srv, "tabi").Cookie; got != "new_api_refresh=manual" {
		t.Fatalf("显式提交的明文凭据未生效: %q", got)
	}
}
