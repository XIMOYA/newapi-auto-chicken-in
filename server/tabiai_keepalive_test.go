/*
server/tabiai_keepalive_test.go
凭据保活的测试：暂停/恢复判据、签到避让、轮转落库、间隔夹取。

不打真实站点：需要发请求的用例都用 httptest 起一个假 TaBiAI，
只验证"我们这一侧的决策对不对"。
*/
package main

import (
	"context"
	"database/sql"
	"net/http"
	"testing"
)

// keepaliveDB 起一个带完整表结构的服务实例，返回它的库。
// OpenDB 里已经建了保活表，这里不再单独建，顺带验证接线没漏。
func keepaliveDB(t *testing.T) *sql.DB {
	t.Helper()
	return newTestServer(t).db
}

func TestClampKeepaliveMinutes(t *testing.T) {
	cases := []struct {
		in, want int
	}{
		{0, tabiaiKeepaliveDefaultMinutes},   // 关闭用 enabled=false 表达，0 按默认处理
		{-5, tabiaiKeepaliveDefaultMinutes},
		{1, tabiaiKeepaliveMinMinutes},       // 刷太勤只是多消耗代次
		{90, 90},
		{5000, tabiaiKeepaliveMaxMinutes},
	}
	for _, c := range cases {
		if got := clampKeepaliveMinutes(c.in); got != c.want {
			t.Errorf("clampKeepaliveMinutes(%d) = %d, 期望 %d", c.in, got, c.want)
		}
	}
}

func TestKeepaliveSettingDefaultsAndSave(t *testing.T) {
	db := keepaliveDB(t)
	got, err := LoadTabiAIKeepaliveSetting(db)
	if err != nil {
		t.Fatalf("读默认策略失败: %v", err)
	}
	if !got.Enabled || got.Minutes != tabiaiKeepaliveDefaultMinutes {
		t.Fatalf("默认策略应为启用 + %d 分钟，实际 %+v", tabiaiKeepaliveDefaultMinutes, got)
	}

	saved, err := SaveTabiAIKeepaliveSetting(db, TabiAIKeepaliveSetting{Enabled: false, Minutes: 3})
	if err != nil {
		t.Fatalf("保存策略失败: %v", err)
	}
	if saved.Enabled || saved.Minutes != tabiaiKeepaliveMinMinutes {
		t.Fatalf("保存后应为关闭 + 夹到下限，实际 %+v", saved)
	}
	again, _ := LoadTabiAIKeepaliveSetting(db)
	if again.Enabled || again.Minutes != tabiaiKeepaliveMinMinutes {
		t.Fatalf("重新读取不一致: %+v", again)
	}
}

// tabiAccount 造一个启用的 tabiai 账号。
func tabiAccount(name, cookie string) Account {
	return Account{Name: name, URL: "https://tabi.example.com",
		LoginMethod: LoginMethodTabiAI, Cookie: cookie, Enabled: true}
}

func TestKeepaliveTargetsFiltersByMethodAndState(t *testing.T) {
	cfg := &Config{Accounts: []Account{
		tabiAccount("tabi-1", "sid.gen1"),
		{Name: "cookie-号", URL: "https://x.example.com",
			LoginMethod: LoginMethodNewAPICookie, Cookie: "session=x", Enabled: true},
		func() Account { a := tabiAccount("停用号", "sid.gen1"); a.Enabled = false; return a }(),
		tabiAccount("空凭据", ""),
	}}
	targets, resumed := tabiaiKeepaliveTargets(cfg, map[string]string{},
		map[string]TabiAIKeepaliveRow{})
	if len(targets) != 1 || targets[0].Name != "tabi-1" {
		t.Fatalf("只该挑出启用且有凭据的 tabiai 账号，实际 %+v", targets)
	}
	if len(resumed) != 0 {
		t.Fatalf("没有暂停过的账号不该出现恢复记录: %v", resumed)
	}
}

func TestKeepalivePausedAccountStaysPausedUntilEdited(t *testing.T) {
	states := map[string]TabiAIKeepaliveRow{"tabi-1": {AccountName: "tabi-1", Paused: true}}
	paused := map[string]string{"tabi-1": "sid.dead"}

	// 凭据还是那一代死值：继续暂停，一次都不该再刷
	cfg := &Config{Accounts: []Account{tabiAccount("tabi-1", "sid.dead")}}
	targets, resumed := tabiaiKeepaliveTargets(cfg, paused, states)
	if len(targets) != 0 || len(resumed) != 0 {
		t.Fatalf("凭据没被改动时应继续暂停，实际 targets=%d resumed=%v", len(targets), resumed)
	}

	// 人工重新签发过：下一轮就要恢复，这就是「编辑后的第一次刷新」
	cfg = &Config{Accounts: []Account{tabiAccount("tabi-1", "sid.fresh")}}
	targets, resumed = tabiaiKeepaliveTargets(cfg, paused, states)
	if len(targets) != 1 {
		t.Fatalf("凭据被改动后应恢复刷新，实际 targets=%d", len(targets))
	}
	if len(resumed) != 1 || resumed[0] != "tabi-1" {
		t.Fatalf("恢复记录不对: %v", resumed)
	}
}

func TestKeepalivePausedIgnoresSurroundingWhitespace(t *testing.T) {
	states := map[string]TabiAIKeepaliveRow{"tabi-1": {AccountName: "tabi-1", Paused: true}}
	paused := map[string]string{"tabi-1": "sid.dead"}
	// 只是首尾多了空白，不算「被编辑过」——否则复制粘贴带个换行就会误恢复
	cfg := &Config{Accounts: []Account{tabiAccount("tabi-1", "  sid.dead\n")}}
	if targets, _ := tabiaiKeepaliveTargets(cfg, paused, states); len(targets) != 0 {
		t.Fatalf("首尾空白不该被当成编辑过，实际 targets=%d", len(targets))
	}
}

func TestKeepaliveStateRoundTrip(t *testing.T) {
	db := keepaliveDB(t)
	row := TabiAIKeepaliveRow{
		AccountName: "tabi-1", LastRunAt: "2026-08-21T10:00:00Z", State: cookieTestStateValid,
		Message: "凭据有效", Rotated: true, Paused: false, ProxyAddr: "1.2.3.4:8080",
	}
	if err := saveKeepaliveState(db, row, ""); err != nil {
		t.Fatalf("写状态失败: %v", err)
	}
	states, pausedCookies, err := loadKeepaliveStates(db)
	if err != nil {
		t.Fatalf("读状态失败: %v", err)
	}
	got, ok := states["tabi-1"]
	if !ok {
		t.Fatal("没读回 tabi-1 的状态")
	}
	if got.State != cookieTestStateValid || !got.Rotated || got.ProxyAddr != "1.2.3.4:8080" {
		t.Fatalf("状态字段丢失: %+v", got)
	}
	if pausedCookies["tabi-1"] != "" {
		t.Fatalf("未暂停时不该留凭据快照: %q", pausedCookies["tabi-1"])
	}

	// 同一账号再写一次：应覆盖而不是插出第二行
	row.State = cookieTestStateInvalid
	row.Paused = true
	if err := saveKeepaliveState(db, row, "sid.dead"); err != nil {
		t.Fatalf("覆盖写失败: %v", err)
	}
	states, pausedCookies, _ = loadKeepaliveStates(db)
	if len(states) != 1 || !states["tabi-1"].Paused {
		t.Fatalf("覆盖写后状态不对: %+v", states)
	}
	if pausedCookies["tabi-1"] != "sid.dead" {
		t.Fatalf("暂停时必须记下那一代的值，实际 %q", pausedCookies["tabi-1"])
	}
}

func TestKeepaliveSkipsWhileCheckinRunning(t *testing.T) {
	srv := newTestServer(t)
	if _, err := StartRun(srv.db, "github-actions"); err != nil {
		t.Fatalf("StartRun: %v", err)
	}
	// 签到在跑：保活必须整轮避让。两边抢同一条 sid 会把账号打死，
	// 这不是「本次保活失败」那么轻的事
	ok, paused, failed := srv.keepalive.RunOnce(context.Background(), "测试")
	if ok != 0 || paused != 0 || failed != 0 {
		t.Fatalf("签到运行期间应整轮跳过，实际 ok=%d paused=%d failed=%d", ok, paused, failed)
	}
	status, err := srv.keepalive.Status()
	if err != nil {
		t.Fatalf("Status: %v", err)
	}
	if !status.SkippedByCheckin {
		t.Fatal("跳过原因应记为「被签到挡下」，供界面解释为什么没刷")
	}
}

func TestKeepaliveStatusListsEnabledTabiAccounts(t *testing.T) {
	srv := newTestServer(t)
	cfg, _, err := LoadConfig(srv.db)
	if err != nil {
		t.Fatalf("LoadConfig: %v", err)
	}
	cfg.Accounts = []Account{
		tabiAccount("tabi-1", "sid.gen1"),
		{Name: "cookie-号", URL: "https://x.example.com",
			LoginMethod: LoginMethodNewAPICookie, Cookie: "session=x", Enabled: true},
	}
	if _, err := SaveConfig(srv.db, cfg); err != nil {
		t.Fatalf("SaveConfig: %v", err)
	}
	status, err := srv.keepalive.Status()
	if err != nil {
		t.Fatalf("Status: %v", err)
	}
	if len(status.Accounts) != 1 || status.Accounts[0].AccountName != "tabi-1" {
		t.Fatalf("只该列出启用的 tabiai 账号，实际 %+v", status.Accounts)
	}
	// 从没刷过的账号也要占一行，界面上才看得出「还没轮到它」
	if status.Accounts[0].State != "" || status.Accounts[0].LastRunAt != "" {
		t.Fatalf("未刷过的账号应是空状态: %+v", status.Accounts[0])
	}
}

func TestKeepaliveAPIRequiresJWT(t *testing.T) {
	srv := newTestServer(t)
	for _, tc := range []struct{ method, path string }{
		{http.MethodGet, "/api/tabiai/keepalive"},
		{http.MethodPut, "/api/tabiai/keepalive"},
		{http.MethodPost, "/api/tabiai/keepalive/run"},
	} {
		rr := doReq(t, srv, tc.method, tc.path, "", nil)
		if rr.Code != http.StatusUnauthorized {
			t.Errorf("%s %s 未带 JWT 应 401，实际 %d", tc.method, tc.path, rr.Code)
		}
	}
}
