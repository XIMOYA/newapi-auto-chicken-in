/*
server/tabiai_keepalive_test.go
凭据保活的测试：暂停/恢复判据、签到避让、轮转落库、间隔夹取，
以及保活自己占住运行锁这条反向保护。

不打真实站点：需要发请求的用例都用 httptest 起一个假 TaBiAI，
只验证"我们这一侧的决策对不对"。
*/
package main

import (
	"context"
	"database/sql"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"
	"time"
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

// 下面几个用例守的是「代理不通要换 IP 重试」这条链路。
//
// 注意：链路类失败没有独立状态词，走的是 cookieTestProxyIssue —— 状态记 abnormal、
// 额外把 retryable 置为 true。判定要看 retryable 而不是找一个 proxy_issue 状态。

// seedAliveProxies 往池里塞几个「标记为可用」的地址。它们实际上连不通，
// 正好用来逼保活走换代理那条路。
func seedAliveProxies(t *testing.T, srv *Server, addrs []string) {
	t.Helper()
	entries := make([]ProxyEntry, 0, len(addrs))
	for _, addr := range addrs {
		entries = append(entries, ProxyEntry{
			Source: "test", Addr: addr, LatencyMs: 10, Alive: true,
			LastChecked: "now", LastAliveAt: "now",
		})
	}
	if err := srv.proxies.replaceAll(entries); err != nil {
		t.Fatalf("塞代理失败: %v", err)
	}
}

func TestKeepaliveRetriesOnProxyFailure(t *testing.T) {
	srv := newTestServer(t)
	// 三个地址全都连不通：应该逐个换着试，而不是一次就收手
	seedAliveProxies(t, srv, []string{"127.0.0.1:9", "127.0.0.1:10", "127.0.0.1:11"})

	acc := tabiAccount("tabi-1", "sid.gen1")
	cfg, _, _ := LoadConfig(srv.db)
	results, used := srv.keepalive.runWithProxyRetry(context.Background(), &cfg, []Account{acc})
	if len(results) != 1 || len(used) != 1 {
		t.Fatalf("返回长度应与 targets 对齐，实际 results=%d used=%d", len(results), len(used))
	}
	if !results[0].retryable {
		t.Fatalf("代理连不通应记成链路类失败（retryable），实际 %q: %s",
			results[0].State, results[0].Message)
	}
	// 最后一次用的代理要带出来，界面上才知道是经谁失败的
	if used[0] == "" {
		t.Fatal("池里有代理时不该记成直连")
	}
}

func TestKeepaliveNonProxyFailureIsNotRetried(t *testing.T) {
	srv := newTestServer(t)
	// 站点明确回 401 AUTH_SESSION_REVOKED：凭据死了，换 IP 也是一样的结果
	site := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusUnauthorized)
		_, _ = w.Write([]byte(`{"success":false,"code":"AUTH_SESSION_REVOKED","message":"Unauthorized"}`))
	}))
	defer site.Close()

	acc := tabiAccount("tabi-1", "sid.gen1")
	acc.URL = site.URL
	cfg, _, _ := LoadConfig(srv.db)
	results, _ := srv.keepalive.runWithProxyRetry(context.Background(), &cfg, []Account{acc})
	if results[0].State != cookieTestStateInvalid {
		t.Fatalf("会话被撤销应判 invalid，实际 %q（%s）", results[0].State, results[0].Message)
	}
	if results[0].retryable {
		t.Fatal("凭据失效不该被标成可重试，否则会白换一圈代理")
	}
}

func TestKeepaliveDirectConnectionDoesNotLoop(t *testing.T) {
	srv := newTestServer(t)
	// 池子是空的：发牌拿到空串（直连）。此时重试没有意义，必须一轮就停
	site := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusBadGateway)
	}))
	defer site.Close()

	acc := tabiAccount("tabi-1", "sid.gen1")
	acc.URL = site.URL
	cfg, _, _ := LoadConfig(srv.db)
	done := make(chan struct{})
	go func() {
		srv.keepalive.runWithProxyRetry(context.Background(), &cfg, []Account{acc})
		close(done)
	}()
	select {
	case <-done:
	case <-time.After(20 * time.Second):
		t.Fatal("池子为空时不该反复重试，疑似死循环")
	}
}

// --------------------------------------------------------------------------- //
// 反向保护：保活自己也占住那把运行锁
//
// 只在开头查一次 run_state 挡不住「保活跑到一半签到才启动」。保活把锁占上之后：
//   - 网页端的凭据检测/签发被 409 挡下，不会和保活抢同一条 sid；
//   - 签到客户端开跑前从 GET /api/run-state 看见 source 是 tabiai-keepalive，
//     知道这是「该等的」而不是同伴分片（对分片互等会直接死锁）。
// --------------------------------------------------------------------------- //

// holdKeepaliveLock 用保活的 source 占住运行锁，等价于 RunOnce 内部那次 StartRun。
func holdKeepaliveLock(t *testing.T, srv *Server) {
	t.Helper()
	state, err := StartRun(srv.db, tabiaiKeepaliveSource)
	if err != nil {
		t.Fatalf("保活占锁失败: %v", err)
	}
	if !state.Running || state.Source != tabiaiKeepaliveSource {
		t.Fatalf("占锁后状态不对: %+v", state)
	}
}

// seedTabiAccount 往库里放一个启用的 tabiai 账号；不填 github_user_session，
// 万一锁失效也只会被业务校验拦住，不会真的去 GitHub 走一遍 OAuth。
func seedTabiAccount(t *testing.T, srv *Server, name, url string) {
	t.Helper()
	seedConfig(t, srv, []Account{{
		Name: name, URL: url, LoginMethod: LoginMethodTabiAI,
		Cookie: "new_api_refresh=sid.gen1", Enabled: true,
	}}, nil)
}

func TestKeepaliveLockBlocksWebCredentialOps(t *testing.T) {
	srv := newTestServer(t)
	jwt := loginToken(t, srv)
	seedTabiAccount(t, srv, "tabi-1", "https://tabi.example.com")
	holdKeepaliveLock(t, srv)

	for _, tc := range []struct {
		name, path string
		body       any
	}{
		{"凭据检测", "/api/cookie-tests/tabiai", map[string]any{"account_names": []string{"tabi-1"}}},
		{"凭据签发", "/api/tabiai/issue-cookie", map[string]string{"account_name": "tabi-1"}},
		{"手动再跑一轮保活", "/api/tabiai/keepalive/run", nil},
	} {
		t.Run(tc.name, func(t *testing.T) {
			rr := doReq(t, srv, http.MethodPost, tc.path, jwt, tc.body)
			if rr.Code != http.StatusConflict {
				t.Fatalf("保活占锁期间应 409，实际 %d: %s", rr.Code, rr.Body.String())
			}
			var resp struct {
				Error    string   `json:"error"`
				RunState RunState `json:"run_state"`
			}
			decodeJSON(t, rr, &resp)
			if resp.Error == "" {
				t.Error("必须给出可读的拒绝原因")
			}
			// 状态一并回传，前端能直接说清「是保活在动凭据」而不是笼统一句被占用
			if !resp.RunState.Running || resp.RunState.Source != tabiaiKeepaliveSource {
				t.Errorf("响应应带回保活持锁的状态: %+v", resp.RunState)
			}
		})
	}
	// 被拦下的请求不能已经把检测任务启起来
	if srv.cookieTests.IsRunning() {
		t.Error("409 之后不该有检测任务在跑")
	}
}

/*
签到客户端要能从 GET /api/run-state 认出「现在是保活在跑」。

这是它让路的唯一依据：客户端只对 tabiai-keepalive 这个 source 等待，看到别的
source（分片同伴）必须照跑，否则几个分片互等就直接死锁。JWT 和 API Key 都要能读到
—— 网页端用前者，Python 客户端用后者。
*/
func TestKeepaliveLockIsVisibleToCheckinClient(t *testing.T) {
	srv := newTestServer(t)
	jwt := loginToken(t, srv)
	key := apiKeyToken(t, srv, jwt)
	holdKeepaliveLock(t, srv)

	// source 是跨语言契约：Python 侧按字面量比对，值改了那边就静默失效
	if got := tabiaiKeepaliveSource; got != "tabiai-keepalive" {
		t.Fatalf("source 字面量变了，客户端要同步改: %q", got)
	}
	for _, tc := range []struct{ name, token string }{
		{"网页端 JWT", jwt},
		{"客户端 API Key", key},
	} {
		t.Run(tc.name, func(t *testing.T) {
			state := runStateOf(t, srv, tc.token)
			if !state.Running {
				t.Fatalf("保活持锁时应为运行中: %+v", state)
			}
			if state.Source != tabiaiKeepaliveSource {
				t.Errorf("source = %q，应为 %q", state.Source, tabiaiKeepaliveSource)
			}
			if state.Holders != 1 {
				t.Errorf("保活只占一个持有者，实际 %d", state.Holders)
			}
			// 客户端据此算「还要等多久」，缺了只能死等或者硬上
			if state.StaleAfterSeconds <= 0 || state.HeartbeatAt == "" {
				t.Errorf("过期时长与心跳时间都要下发: %+v", state)
			}
		})
	}
}

// 保活跑完必须彻底放开，否则网页端要干等到心跳过期（5 分钟）才恢复。
func TestKeepaliveLockReleaseRestoresWebOps(t *testing.T) {
	srv := newTestServer(t)
	jwt := loginToken(t, srv)
	// 假站点：解锁后的检测任务有个真实去处，不用碰外网
	site := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusUnauthorized)
		_, _ = w.Write([]byte(`{"success":false,"code":"AUTH_UNAUTHORIZED","message":"expired"}`))
	}))
	defer site.Close()
	seedTabiAccount(t, srv, "tabi-1", site.URL)

	holdKeepaliveLock(t, srv)
	if rr := doReq(t, srv, http.MethodPost, "/api/cookie-tests/tabiai", jwt,
		map[string]any{"account_names": []string{"tabi-1"}}); rr.Code != http.StatusConflict {
		t.Fatalf("前置条件：占锁期间应 409，实际 %d", rr.Code)
	}

	// RunOnce 收尾时 defer 的就是这一句
	if err := StopRun(srv.db); err != nil {
		t.Fatalf("释放运行锁失败: %v", err)
	}
	if state := runStateOf(t, srv, jwt); state.Running || state.Holders != 0 {
		t.Fatalf("释放后应完全解锁: %+v", state)
	}

	// 签发：放开后应进到业务校验（该账号没填 user_session → 400），
	// 用它验证「锁不再拦」比检测更干净 —— 不会起后台任务
	issue := doReq(t, srv, http.MethodPost, "/api/tabiai/issue-cookie", jwt,
		map[string]string{"account_name": "tabi-1"})
	if issue.Code != http.StatusBadRequest {
		t.Fatalf("解锁后签发应进入业务校验（400），实际 %d: %s", issue.Code, issue.Body.String())
	}

	// 检测：真的能启起来
	rr := doReq(t, srv, http.MethodPost, "/api/cookie-tests/tabiai", jwt,
		map[string]any{"account_names": []string{"tabi-1"}})
	if rr.Code != http.StatusOK {
		t.Fatalf("解锁后检测应恢复可用，实际 %d: %s", rr.Code, rr.Body.String())
	}
	srv.cookieTests.Stop()
}

/*
RunOnce 全流程跑一遍，确认它真的把锁占上又还了回去。

在假站点的 refresh handler 里回头读一次 run_state：那一刻正是保活在动 sid，
锁必须已经在手上。只看运行前后的状态是看不出来的 —— 忘了 StartRun 也照样是
「跑完锁是空的」。
*/
func TestKeepaliveRunOnceHoldsRunLock(t *testing.T) {
	srv := newTestServer(t)
	var mu sync.Mutex
	var observed RunState
	var observeErr error
	site := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != cookieTestRefreshPath {
			http.NotFound(w, r)
			return
		}
		state, err := LoadRunState(srv.db)
		mu.Lock()
		observed, observeErr = state, err
		mu.Unlock()
		// 轮转下一代，顺带验证这一轮不是空转
		w.Header().Add("Set-Cookie",
			"new_api_refresh=sid.gen2; Path=/api/user/auth; Max-Age=2591999; HttpOnly; SameSite=Strict")
		_ = json.NewEncoder(w).Encode(tabiaiRefreshBody(8259, "KIQ"))
	}))
	defer site.Close()
	seedTabiAccount(t, srv, "tabi-1", site.URL)

	if state, err := LoadRunState(srv.db); err != nil || state.Running {
		t.Fatalf("前置条件：锁应是空闲的（err=%v state=%+v）", err, state)
	}
	ok, paused, failed := srv.keepalive.RunOnce(context.Background(), "测试")
	if ok != 1 || paused != 0 || failed != 0 {
		t.Fatalf("这一轮应刷新成功，实际 ok=%d paused=%d failed=%d", ok, paused, failed)
	}

	mu.Lock()
	got, gotErr := observed, observeErr
	mu.Unlock()
	if gotErr != nil {
		t.Fatalf("刷新途中读锁失败: %v", gotErr)
	}
	if !got.Running || got.Source != tabiaiKeepaliveSource {
		t.Fatalf("发 refresh 的那一刻必须已持锁: %+v", got)
	}
	if got.Holders != 1 {
		t.Errorf("保活只该占一个持有者，实际 %d", got.Holders)
	}
	if state, err := LoadRunState(srv.db); err != nil || state.Running || state.Holders != 0 {
		t.Fatalf("收尾应把锁还回去（err=%v state=%+v）", err, state)
	}
	if cookie := accountByName(t, srv, "tabi-1").Cookie; cookie != "new_api_refresh=sid.gen2" {
		t.Errorf("轮转后的新凭据应立刻落库，实际 %q", cookie)
	}
}

