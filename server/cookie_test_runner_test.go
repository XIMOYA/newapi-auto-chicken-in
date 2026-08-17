/*
server/cookie_test_runner_test.go
后台检测任务的行为测试：轮次重试、失败分类、账号代理降级、手动停止。
*/
package main

import (
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

// waitRunnerDone 等 runner 结束并返回快照。
func waitRunnerDone(t *testing.T, runner *CookieTestRunner, within time.Duration) CookieTestStatus {
	t.Helper()
	deadline := time.Now().Add(within)
	for {
		snapshot := runner.Snapshot()
		if !snapshot.Running {
			return snapshot
		}
		if time.Now().After(deadline) {
			runner.Stop()
			t.Fatalf("等待任务结束超时: %+v", snapshot)
		}
		time.Sleep(50 * time.Millisecond)
	}
}

func runnerConfig(url string, account Account) *Config {
	cfg := DefaultConfig()
	cfg.HTTP.Timeout = 5
	account.URL = url
	account.Enabled = true
	cfg.Accounts = []Account{account}
	return &cfg
}

func TestRunnerRetriesChallengeUntilSiteAnswers(t *testing.T) {
	var calls atomic.Int32
	site := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		// 前两次返回 Cloudflare 挑战页（链路问题），第三次才给真实 JSON
		if calls.Add(1) <= 2 {
			w.Header().Set("Server", "cloudflare")
			w.Header().Set("Cf-Ray", "ray-id")
			w.WriteHeader(http.StatusForbidden)
			_, _ = w.Write([]byte("<html><title>Just a moment...</title>__cf_chl</html>"))
			return
		}
		_ = writeJSONBody(w, map[string]any{
			"success": true,
			"data":    map[string]any{"id": 7, "username": "kiq"},
		})
	}))
	defer site.Close()

	runner := NewCookieTestRunner(nil, nil) // 无代理池：每轮直连
	cfg := runnerConfig(site.URL, Account{Name: "retry", Cookie: "session=x"})
	if err := runner.Start(cfg, LoginMethodNewAPICookie, nil); err != nil {
		t.Fatalf("Start: %v", err)
	}
	status := waitRunnerDone(t, runner, 30*time.Second)

	if len(status.Results) != 1 || status.Results[0].State != cookieTestStateValid {
		t.Fatalf("最终应为 valid: %+v", status.Results)
	}
	if status.Results[0].Attempts != 3 {
		t.Fatalf("应重试到第 3 次才成功，实际 attempts = %d", status.Results[0].Attempts)
	}
	if status.Summary.Valid != 1 || status.Summary.Total != 1 {
		t.Fatalf("summary 不对: %+v", status.Summary)
	}
}

func TestRunnerDoesNotRetryOriginFailure(t *testing.T) {
	var calls atomic.Int32
	site := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		w.WriteHeader(http.StatusUnauthorized)
		_, _ = w.Write([]byte(`{"success":false,"message":"unauthorized"}`))
	}))
	defer site.Close()

	runner := NewCookieTestRunner(nil, nil)
	cfg := runnerConfig(site.URL, Account{Name: "origin", Cookie: "session=dead"})
	if err := runner.Start(cfg, LoginMethodNewAPICookie, nil); err != nil {
		t.Fatalf("Start: %v", err)
	}
	status := waitRunnerDone(t, runner, 15*time.Second)

	if status.Results[0].State != cookieTestStateInvalid {
		t.Fatalf("源站明确拒绝应为 invalid: %+v", status.Results[0])
	}
	if got := status.Results[0].Attempts; got != 1 {
		t.Fatalf("源站问题不该重试，attempts = %d", got)
	}
	if got := calls.Load(); got != 1 {
		t.Fatalf("站点应只被请求 1 次，实际 %d", got)
	}
}

// writeJSONBody 测试用的极简 JSON 写出（避免依赖 handlers 的内部实现）。
func writeJSONBody(w http.ResponseWriter, payload map[string]any) error {
	w.Header().Set("Content-Type", "application/json")
	_, err := fmt.Fprintf(w, `{"success":%v,"data":{"id":%v,"username":%q}}`,
		payload["success"],
		payload["data"].(map[string]any)["id"],
		payload["data"].(map[string]any)["username"])
	return err
}

func TestRunnerDegradesOwnProxyToPoolAfterRepeatedFailures(t *testing.T) {
	site := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = writeJSONBody(w, map[string]any{
			"success": true,
			"data":    map[string]any{"id": 9, "username": "pooled"},
		})
	}))
	defer site.Close()

	// 账号自带代理指向必然拒绝连接的端口；代理池为空，降级后走直连正好能连上 httptest
	dead := "http://127.0.0.1:1"
	runner := NewCookieTestRunner(nil, nil)
	cfg := runnerConfig(site.URL, Account{Name: "own-proxy", Cookie: "session=x", Proxy: &dead})
	if err := runner.Start(cfg, LoginMethodNewAPICookie, nil); err != nil {
		t.Fatalf("Start: %v", err)
	}
	status := waitRunnerDone(t, runner, 40*time.Second)

	row := status.Results[0]
	if row.State != cookieTestStateValid {
		t.Fatalf("降级后应成功: %+v", row)
	}
	if row.Attempts != cookieTestOwnProxyRounds+1 {
		t.Fatalf("应在自带代理失败 %d 轮后降级，实际 attempts = %d", cookieTestOwnProxyRounds, row.Attempts)
	}
	if !strings.Contains(row.Message, "已切换代理池") {
		t.Fatalf("降级提示缺失: %q", row.Message)
	}
}

func TestRunnerStopMarksUnfinishedAsSkipped(t *testing.T) {
	site := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		// 永远返回挑战页：账号会一直留在重试队列里
		w.Header().Set("Server", "cloudflare")
		w.WriteHeader(http.StatusForbidden)
		_, _ = w.Write([]byte("<html>sorry, you have been blocked</html>"))
	}))
	defer site.Close()

	runner := NewCookieTestRunner(nil, nil)
	cfg := runnerConfig(site.URL, Account{Name: "endless", Cookie: "session=x"})
	if err := runner.Start(cfg, LoginMethodNewAPICookie, nil); err != nil {
		t.Fatalf("Start: %v", err)
	}
	// 等到至少完成一轮，确认它确实在重试而不是立刻定终态
	deadline := time.Now().Add(10 * time.Second)
	for runner.Snapshot().Round < 2 {
		if time.Now().After(deadline) {
			runner.Stop()
			t.Fatal("任务没有进入第 2 轮，说明挑战页未被判为可重试")
		}
		time.Sleep(50 * time.Millisecond)
	}

	runner.Stop()
	status := waitRunnerDone(t, runner, 15*time.Second)
	if !status.Stopped {
		t.Fatal("快照应标记 stopped")
	}
	row := status.Results[0]
	if row.State != cookieTestStateSkipped {
		t.Fatalf("停止后未完成账号应为 skipped: %+v", row)
	}
	if !strings.Contains(row.Message, "已手动停止") || !strings.Contains(row.Message, "共尝试") {
		t.Fatalf("停止说明不完整: %q", row.Message)
	}
	if row.Attempts < 1 {
		t.Fatalf("应保留已尝试次数: %d", row.Attempts)
	}
}
