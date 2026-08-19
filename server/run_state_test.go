/*
server/run_state_test.go
签到运行状态与凭据操作锁的测试

守的是一条容易被忽视的因果链：TaBiAI 凭据检测本身就是一次真 refresh，签到进程
同时在推进代次，两边一撞就会被站点判重放、整条会话被撤销。所以锁必须真的拦住，
而且必须能自己解开 —— Actions 被强杀时没人会来发「结束」。
*/
package main

import (
	"net/http"
	"testing"
	"time"
)

// apiKeyToken 建一个 API Key 并返回明文（只在创建时返回一次）。
func apiKeyToken(t *testing.T, srv *Server, jwt string) string {
	t.Helper()
	rr := doReq(t, srv, http.MethodPost, "/api/keys", jwt, map[string]string{"name": "run-state"})
	if rr.Code != http.StatusOK {
		t.Fatalf("创建 API Key 失败 = %d, %s", rr.Code, rr.Body.String())
	}
	var resp struct {
		Key string `json:"key"`
	}
	decodeJSON(t, rr, &resp)
	if resp.Key == "" {
		t.Fatal("创建 API Key 未返回明文")
	}
	return resp.Key
}

// runStateOf 读当前锁状态。
func runStateOf(t *testing.T, srv *Server, jwt string) RunState {
	t.Helper()
	rr := doReq(t, srv, http.MethodGet, "/api/run-state", jwt, nil)
	if rr.Code != http.StatusOK {
		t.Fatalf("查询状态失败 = %d, %s", rr.Code, rr.Body.String())
	}
	var state RunState
	decodeJSON(t, rr, &state)
	return state
}

// startRun 上报开跑。
func startRun(t *testing.T, srv *Server, key, source string) {
	t.Helper()
	rr := doReq(t, srv, http.MethodPost, "/api/run-state/start", key,
		map[string]string{"source": source})
	if rr.Code != http.StatusOK {
		t.Fatalf("上报开跑失败 = %d, %s", rr.Code, rr.Body.String())
	}
}

// ---------------------------------------------------------------------------
// 状态本身
// ---------------------------------------------------------------------------

func TestRunStateStartsIdle(t *testing.T) {
	srv := newTestServer(t)
	jwt := loginToken(t, srv)
	state := runStateOf(t, srv, jwt)
	if state.Running {
		t.Error("全新库不该是「签到中」")
	}
	if state.StaleAfterSeconds <= 0 || state.HeartbeatSeconds <= 0 {
		t.Errorf("必须把心跳间隔与过期时长告诉客户端: %+v", state)
	}
	if state.HeartbeatSeconds >= state.StaleAfterSeconds {
		t.Errorf("心跳间隔要明显小于过期时长，否则正常运行也会被判死: %+v", state)
	}
}

func TestRunStateStartThenStop(t *testing.T) {
	srv := newTestServer(t)
	jwt := loginToken(t, srv)
	key := apiKeyToken(t, srv, jwt)

	startRun(t, srv, key, "github-actions")
	state := runStateOf(t, srv, jwt)
	if !state.Running {
		t.Fatal("上报开跑后应处于签到中")
	}
	if state.Source != "github-actions" {
		t.Errorf("来源应如实记录: %q", state.Source)
	}
	if state.StartedAt == "" || state.HeartbeatAt == "" {
		t.Errorf("开始与心跳时间都要有: %+v", state)
	}

	if rr := doReq(t, srv, http.MethodPost, "/api/run-state/stop", key, nil); rr.Code != http.StatusOK {
		t.Fatalf("上报收尾失败 = %d", rr.Code)
	}
	if runStateOf(t, srv, jwt).Running {
		t.Error("收尾后应立即解锁")
	}
}

func TestRunStateStopIsIdempotent(t *testing.T) {
	// 客户端在 finally 里发 stop，可能和强制解锁撞上；重复 stop 不能报错
	srv := newTestServer(t)
	jwt := loginToken(t, srv)
	key := apiKeyToken(t, srv, jwt)
	for i := 0; i < 3; i++ {
		if rr := doReq(t, srv, http.MethodPost, "/api/run-state/stop", key, nil); rr.Code != http.StatusOK {
			t.Fatalf("第 %d 次 stop = %d", i+1, rr.Code)
		}
	}
}

func TestRunStateHeartbeatKeepsItAlive(t *testing.T) {
	srv := newTestServer(t)
	jwt := loginToken(t, srv)
	key := apiKeyToken(t, srv, jwt)
	startRun(t, srv, key, "本机")

	before := runStateOf(t, srv, jwt).HeartbeatAt
	// 库里存的是 RFC3339 秒级精度，等一秒才能看出时间真的推进了
	time.Sleep(1100 * time.Millisecond)
	rr := doReq(t, srv, http.MethodPost, "/api/run-state/heartbeat", key, nil)
	if rr.Code != http.StatusOK {
		t.Fatalf("心跳失败 = %d, %s", rr.Code, rr.Body.String())
	}
	var resp struct {
		Running bool `json:"running"`
	}
	decodeJSON(t, rr, &resp)
	if !resp.Running {
		t.Error("锁还在时心跳应返回 running=true")
	}
	if after := runStateOf(t, srv, jwt).HeartbeatAt; after == before {
		t.Errorf("心跳没有推进时间: %q", after)
	}
}

func TestRunStateHeartbeatAfterUnlockReportsGone(t *testing.T) {
	// 管理员强制解锁后，客户端要能从心跳响应里知道「我的锁没了」
	srv := newTestServer(t)
	jwt := loginToken(t, srv)
	key := apiKeyToken(t, srv, jwt)
	startRun(t, srv, key, "本机")

	if rr := doReq(t, srv, http.MethodPost, "/api/run-state/unlock", jwt, nil); rr.Code != http.StatusOK {
		t.Fatalf("强制解锁失败 = %d", rr.Code)
	}
	rr := doReq(t, srv, http.MethodPost, "/api/run-state/heartbeat", key, nil)
	if rr.Code != http.StatusOK {
		t.Fatalf("心跳应仍返回 200（这不是错误）= %d", rr.Code)
	}
	var resp struct {
		Running bool `json:"running"`
	}
	decodeJSON(t, rr, &resp)
	if resp.Running {
		t.Error("锁已被解除，心跳应返回 running=false")
	}
}

func TestRunStateStaleHeartbeatUnlocksItself(t *testing.T) {
	// 这是整个设计的关键：Actions 被强杀不会发 stop，锁必须自己过期，
	// 否则网页端会被永久锁死到只能改库
	srv := newTestServer(t)
	jwt := loginToken(t, srv)
	key := apiKeyToken(t, srv, jwt)
	startRun(t, srv, key, "被强杀的任务")
	if !runStateOf(t, srv, jwt).Running {
		t.Fatal("前置条件：应处于签到中")
	}

	// 直接把心跳时间改成远超过期阈值，模拟进程再也没上报
	stale := time.Now().UTC().Add(-2 * runStateStaleAfter).Format(time.RFC3339)
	if _, err := srv.db.Exec(`UPDATE run_state SET heartbeat_at = ? WHERE id = ?`,
		stale, runStateRowID); err != nil {
		t.Fatal(err)
	}
	if runStateOf(t, srv, jwt).Running {
		t.Error("心跳过期后应视为已结束")
	}
}

func TestRunStateBrokenHeartbeatFailsOpen(t *testing.T) {
	// 坏数据不能把平台锁死：解析不出时间就当已结束，宁可放开也不要只能改库才能恢复
	srv := newTestServer(t)
	jwt := loginToken(t, srv)
	key := apiKeyToken(t, srv, jwt)
	startRun(t, srv, key, "脏数据")
	if _, err := srv.db.Exec(`UPDATE run_state SET heartbeat_at = ? WHERE id = ?`,
		"不是时间", runStateRowID); err != nil {
		t.Fatal(err)
	}
	if runStateOf(t, srv, jwt).Running {
		t.Error("心跳时间无法解析时应放开锁")
	}
}

func TestRunStateRestartOverwritesPreviousRun(t *testing.T) {
	// 上一轮没能正常收尾时，新一轮的 start 要能接管，而不是被旧记录挡住
	srv := newTestServer(t)
	jwt := loginToken(t, srv)
	key := apiKeyToken(t, srv, jwt)
	startRun(t, srv, key, "第一轮")
	time.Sleep(1100 * time.Millisecond)
	startRun(t, srv, key, "第二轮")

	state := runStateOf(t, srv, jwt)
	if state.Source != "第二轮" {
		t.Errorf("来源应被新一轮覆盖: %q", state.Source)
	}
	if !state.Running {
		t.Error("新一轮应处于签到中")
	}
}

// ---------------------------------------------------------------------------
// 锁的实际效果
// ---------------------------------------------------------------------------

func TestTabiAICookieTestIsLockedWhileRunning(t *testing.T) {
	srv := newTestServer(t)
	jwt := loginToken(t, srv)
	key := apiKeyToken(t, srv, jwt)
	startRun(t, srv, key, "github-actions")

	rr := doReq(t, srv, http.MethodPost, "/api/cookie-tests/tabiai", jwt,
		map[string]any{"account_names": []string{}})
	if rr.Code != http.StatusConflict {
		t.Fatalf("签到期间 TaBiAI 检测应被拒（409），实际 %d: %s", rr.Code, rr.Body.String())
	}
	var resp struct {
		Error    string   `json:"error"`
		RunState RunState `json:"run_state"`
	}
	decodeJSON(t, rr, &resp)
	if resp.Error == "" {
		t.Error("必须给出可读的拒绝原因")
	}
	if !resp.RunState.Running || resp.RunState.Source != "github-actions" {
		t.Errorf("响应应带回状态，省前端一次往返: %+v", resp.RunState)
	}
	// 任务不能被真正启动
	if srv.cookieTests.IsRunning() {
		t.Error("被拦下的请求不该启动检测任务")
	}
}

func TestIssueTabiAICookieIsLockedWhileRunning(t *testing.T) {
	// 签发会换出全新 sid，签到进程手里那条当场作废，比检测更危险
	srv := newTestServer(t)
	jwt := loginToken(t, srv)
	key := apiKeyToken(t, srv, jwt)
	startRun(t, srv, key, "本机")

	rr := doReq(t, srv, http.MethodPost, "/api/tabiai/issue-cookie", jwt,
		map[string]string{"account_name": "任意"})
	if rr.Code != http.StatusConflict {
		t.Fatalf("签到期间签发应被拒（409），实际 %d: %s", rr.Code, rr.Body.String())
	}
}

func TestNewAPICookieTestIsNotLocked(t *testing.T) {
	// 站点 Cookie 是静态凭据，不轮转也没有重放检测，没有理由跟着一起锁
	srv := newTestServer(t)
	jwt := loginToken(t, srv)
	key := apiKeyToken(t, srv, jwt)
	seedConfig(t, srv, []Account{
		{Name: "A", URL: "https://a.com", LoginMethod: LoginMethodNewAPICookie, Cookie: "ca", Enabled: true},
	}, nil)
	startRun(t, srv, key, "github-actions")

	rr := doReq(t, srv, http.MethodPost, "/api/cookie-tests/newapi", jwt,
		map[string]any{"account_names": []string{"A"}})
	if rr.Code == http.StatusConflict {
		t.Fatalf("站点 Cookie 检测不该被签到锁拦下: %s", rr.Body.String())
	}
	srv.cookieTests.Stop()
}

func TestTabiAICookieTestWorksAfterStop(t *testing.T) {
	srv := newTestServer(t)
	jwt := loginToken(t, srv)
	key := apiKeyToken(t, srv, jwt)
	seedConfig(t, srv, []Account{
		{Name: "T", URL: "https://t.com", LoginMethod: LoginMethodTabiAI, Cookie: "new_api_refresh=x", Enabled: true},
	}, nil)

	startRun(t, srv, key, "github-actions")
	if rr := doReq(t, srv, http.MethodPost, "/api/run-state/stop", key, nil); rr.Code != http.StatusOK {
		t.Fatalf("收尾失败 = %d", rr.Code)
	}
	rr := doReq(t, srv, http.MethodPost, "/api/cookie-tests/tabiai", jwt,
		map[string]any{"account_names": []string{"T"}})
	if rr.Code == http.StatusConflict {
		t.Fatalf("签到结束后应放开: %s", rr.Body.String())
	}
	srv.cookieTests.Stop()
}

// ---------------------------------------------------------------------------
// 鉴权边界
// ---------------------------------------------------------------------------

func TestRunStateAuthBoundaries(t *testing.T) {
	srv := newTestServer(t)
	jwt := loginToken(t, srv)
	key := apiKeyToken(t, srv, jwt)

	cases := []struct {
		name, method, path, token string
		want                      int
	}{
		// 上报三件事属于客户端，用 API Key
		{"start 用 JWT 应拒", http.MethodPost, "/api/run-state/start", jwt, http.StatusUnauthorized},
		{"heartbeat 用 JWT 应拒", http.MethodPost, "/api/run-state/heartbeat", jwt, http.StatusUnauthorized},
		{"stop 用 JWT 应拒", http.MethodPost, "/api/run-state/stop", jwt, http.StatusUnauthorized},
		// 查询与强制解锁属于管理员，用 JWT
		{"查询用 API Key 应拒", http.MethodGet, "/api/run-state", key, http.StatusUnauthorized},
		{"强制解锁用 API Key 应拒", http.MethodPost, "/api/run-state/unlock", key, http.StatusUnauthorized},
		{"无凭据查询应拒", http.MethodGet, "/api/run-state", "", http.StatusUnauthorized},
		{"无凭据上报应拒", http.MethodPost, "/api/run-state/start", "", http.StatusUnauthorized},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			rr := doReq(t, srv, tc.method, tc.path, tc.token, nil)
			if rr.Code != tc.want {
				t.Fatalf("状态 = %d，期望 %d: %s", rr.Code, tc.want, rr.Body.String())
			}
		})
	}
}
