/*
server/panic_guard_test.go
后台协程的 panic 兜底与 Cookie 检测的崩溃收尾

守两件事：
  - 后台任务里的 panic 不能把进程带走。Go 里未捕获的 goroutine panic 会直接终止程序，
    而 net/http 只兜得住请求协程 —— 平台一挂，所有在跑的签到 job 的预取、凭据回写、
    run-state 上报会同时断掉。
  - Cookie 检测崩了之后 running 必须回到 false。停在 true 的话 IsRunning() 恒真，
    之后每次检测请求都被 409 挡掉，用户只能重启服务才能恢复。
*/
package main

import (
	"errors"
	"strings"
	"sync"
	"testing"
)

func TestRecoverPanicKeepsGoroutineAlive(t *testing.T) {
	var wg sync.WaitGroup
	wg.Add(1)
	done := false
	go func() {
		defer wg.Done()
		defer recoverPanic("测试用后台任务")
		panic("boom")
	}()
	wg.Wait()
	// 能走到这里就说明 panic 被兜住了：没兜住的话整个测试进程已经挂了
	done = true
	if !done {
		t.Fatal("unreachable")
	}
}

// wg.Done 必须照常执行，否则 Wait 会永久阻塞——这是加兜底时最容易踩的顺序问题
func TestRecoverPanicStillReleasesWaitGroup(t *testing.T) {
	var wg sync.WaitGroup
	for i := 0; i < 3; i++ {
		wg.Add(1)
		go func(n int) {
			defer wg.Done()
			defer recoverPanic("并发任务")
			if n == 1 {
				panic("only one of them explodes")
			}
		}(i)
	}
	wg.Wait() // 挂住就说明 defer 顺序写错了
}

func TestRecoverPanicWithReportsReason(t *testing.T) {
	var got string
	func() {
		defer recoverPanicWith("带回调的任务", func(reason string) { got = reason })
		panic("库连不上")
	}()
	if !strings.Contains(got, "库连不上") {
		t.Fatalf("回调收到的原因 = %q, want 含「库连不上」", got)
	}
}

func TestRecoverPanicWithNilCallbackIsSafe(t *testing.T) {
	func() {
		defer recoverPanicWith("无回调", nil)
		panic("nothing to report")
	}()
}

// 没有 panic 时不该误触发回调
func TestRecoverPanicWithStaysQuietOnSuccess(t *testing.T) {
	called := false
	func() {
		defer recoverPanicWith("正常收尾", func(string) { called = true })
	}()
	if called {
		t.Fatal("没有 panic 却调了收尾回调")
	}
}

/*
Cookie 检测崩溃后的收尾：任务标结束、原因进 last_error、未决账号标 abnormal。

不做这一步的话 running 停在 true，IsRunning() 恒真，之后每次检测请求都被 409 挡掉，
只能重启服务；而且界面上那几行会永远显示「检测中」，用户会一直等一个不会来的结果。
*/
func TestCookieTestFailFromPanicRestoresState(t *testing.T) {
	r := NewCookieTestRunner(nil, nil)
	r.running = true
	r.rows = []CookieTestResult{
		{Name: "跑着的", State: cookieTestStateRunning},
		{Name: "排队的", State: cookieTestStatePending},
		{Name: "已出结论的", State: cookieTestStateValid, Message: "正常"},
	}

	r.failFromPanic("index out of range")

	if r.IsRunning() {
		t.Fatal("崩溃收尾后 running 必须回到 false，否则检测功能被永久锁死")
	}
	snap := r.Snapshot()
	if !strings.Contains(snap.LastError, "index out of range") {
		t.Errorf("last_error 应带上原因, got %q", snap.LastError)
	}
	if snap.Results[0].State != cookieTestStateAbnormal {
		t.Errorf("跑着的账号应标 abnormal, got %q", snap.Results[0].State)
	}
	if snap.Results[1].State != cookieTestStateAbnormal {
		t.Errorf("排队的账号应标 abnormal, got %q", snap.Results[1].State)
	}
	// 已经有结论的不该被覆盖：那是真实的检测结果
	if snap.Results[2].State != cookieTestStateValid || snap.Results[2].Message != "正常" {
		t.Errorf("已出结论的行被改动了: %+v", snap.Results[2])
	}
	if snap.Running {
		t.Error("快照里的 running 也该是 false")
	}
}

// 崩过一次之后还能重新开始，不需要重启服务
func TestCookieTestCanRestartAfterPanic(t *testing.T) {
	r := NewCookieTestRunner(nil, nil)
	r.running = true
	r.failFromPanic("boom")
	cfg := &Config{Accounts: []Account{
		{Name: "A", URL: "https://a.example.com", Cookie: "c", Enabled: true},
	}}
	if err := r.Start(cfg, LoginMethodNewAPICookie, nil); err != nil {
		t.Fatalf("崩溃收尾后应能重新启动: %v", err)
	}
	r.Stop()
}

/*
启动竞态要返回 409 而不是 400。

handler 先查一次 IsRunning 再调 Start，两者之间有窗口；被抢先时 Start 会返回
ErrCookieTestBusy。前端是靠 409 才认出「有任务在跑」并转去轮询状态的，给 400 只会
弹一句错误、界面停在原地，而文案还一模一样，排查起来很费劲。
*/
func TestCookieTestBusyIsSentinel(t *testing.T) {
	r := NewCookieTestRunner(nil, nil)
	cfg := &Config{Accounts: []Account{
		{Name: "A", URL: "https://a.example.com", Cookie: "c", Enabled: true},
	}}
	if err := r.Start(cfg, LoginMethodNewAPICookie, nil); err != nil {
		t.Fatalf("首次启动: %v", err)
	}
	defer r.Stop()

	err := r.Start(cfg, LoginMethodNewAPICookie, nil)
	if err == nil {
		t.Fatal("重复启动应报错")
	}
	if !errors.Is(err, ErrCookieTestBusy) {
		t.Fatalf("应是 ErrCookieTestBusy 以便 handler 转 409, got %v", err)
	}
}

// 参数类错误不能被误判成「忙」，否则会拿到 409 让人以为有任务在跑
func TestCookieTestArgErrorIsNotBusy(t *testing.T) {
	r := NewCookieTestRunner(nil, nil)
	cfg := &Config{Accounts: []Account{
		{Name: "A", URL: "https://a.example.com", Cookie: "c", Enabled: true},
	}}
	err := r.Start(cfg, LoginMethodNewAPICookie, []string{"根本不存在的账号"})
	if err == nil {
		t.Fatal("选了不存在的账号应报错")
	}
	if errors.Is(err, ErrCookieTestBusy) {
		t.Fatalf("参数错误不该被判成忙: %v", err)
	}
}
