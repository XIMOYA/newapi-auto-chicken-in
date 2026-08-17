/*
server/cookie_test_runner.go
Web 端 Cookie 可用性检测的后台任务执行器。

为什么需要它：
  - 代理类失败要无限重试，同步 HTTP 请求撑不住（前端 axios 30s 就断），必须后台跑 + 轮询。
  - 单账号死循环会占死并发位，所以按「轮次」调度：每轮给所有未完成账号各试一次，
    代理类失败留到下一轮，源站类失败当场定终态。

职责：
- 互斥执行（同一时刻只有一个检测任务），启动/停止/快照
- 从 ProxyManager 的可用代理里轮转发牌，池子空则本轮直连
- 账号自带代理连续失败 2 轮后降级到代理池
*/
package main

import (
	"context"
	"database/sql"
	"fmt"
	"log"
	"strings"
	"sync"
	"time"
)

const (
	// cookieTestRoundGap 轮次间隔：避免代理全挂时空转打爆代理池
	cookieTestRoundGap = 2 * time.Second
	// cookieTestOwnProxyRounds 账号自带代理连续失败多少轮后降级到代理池
	cookieTestOwnProxyRounds = 2
)

// CookieTestStatus 检测任务对外快照。
type CookieTestStatus struct {
	Running     bool               `json:"running"`
	Stopped     bool               `json:"stopped"`
	Mode        string             `json:"mode"`
	Round       int                `json:"round"`
	StartedAt   string             `json:"started_at"`
	CheckedAt   string             `json:"checked_at"`
	DurationSec int                `json:"duration_sec"`
	LastError   string             `json:"last_error"`
	Summary     CookieTestSummary  `json:"summary"`
	Results     []CookieTestResult `json:"results"`
}

// CookieTestRunner 后台检测任务，仿 ProxyManager 的 running/progress 模式。
type CookieTestRunner struct {
	mu sync.RWMutex
	pm *ProxyManager
	// db 用于把 TaBiAI 轮转出的新 refresh cookie 写回配置；为 nil 时只检测不落库（测试用）
	db         *sql.DB
	running    bool
	stopped    bool
	mode       string
	round      int
	startedAt  time.Time
	finishedAt time.Time
	lastErr    string
	rows       []CookieTestResult
	cancel     context.CancelFunc
}

func NewCookieTestRunner(pm *ProxyManager, db *sql.DB) *CookieTestRunner {
	return &CookieTestRunner{pm: pm, db: db}
}

func (r *CookieTestRunner) IsRunning() bool {
	r.mu.RLock()
	defer r.mu.RUnlock()
	return r.running
}

// Snapshot 返回当前任务状态；结果切片是拷贝，调用方可安全序列化。
func (r *CookieTestRunner) Snapshot() CookieTestStatus {
	r.mu.RLock()
	defer r.mu.RUnlock()
	rows := make([]CookieTestResult, len(r.rows))
	copy(rows, r.rows)
	status := CookieTestStatus{
		Running:   r.running,
		Stopped:   r.stopped,
		Mode:      r.mode,
		Round:     r.round,
		LastError: r.lastErr,
		Summary:   summarizeCookieTestResults(rows),
		Results:   rows,
	}
	if !r.startedAt.IsZero() {
		status.StartedAt = r.startedAt.UTC().Format(time.RFC3339)
		end := time.Now()
		if !r.running && !r.finishedAt.IsZero() {
			end = r.finishedAt
		}
		status.DurationSec = int(end.Sub(r.startedAt).Seconds())
	}
	if !r.finishedAt.IsZero() {
		status.CheckedAt = r.finishedAt.UTC().Format(time.RFC3339)
	}
	return status
}

// Start 启动一次检测；已有任务在跑时返回错误（调用方转 409）。
func (r *CookieTestRunner) Start(cfg *Config, mode string, names []string) error {
	targets, err := selectCookieTestTargets(cfg, mode, names)
	if err != nil {
		return err
	}

	r.mu.Lock()
	if r.running {
		r.mu.Unlock()
		return fmt.Errorf("已有 Cookie 检测任务在进行中")
	}
	// 后台 context 自持 cancel：不随触发它的 HTTP 请求结束而中断
	ctx, cancel := context.WithCancel(context.Background())
	r.running = true
	r.stopped = false
	r.mode = mode
	r.round = 0
	r.startedAt = time.Now()
	r.finishedAt = time.Time{}
	r.lastErr = ""
	r.cancel = cancel
	r.rows = make([]CookieTestResult, len(targets))
	for i, target := range targets {
		r.rows[i] = cookieTestResult(target, cookieTestStatePending, "排队中", nil)
	}
	r.mu.Unlock()

	go r.loop(ctx, cfg, mode, targets)
	return nil
}

// Stop 请求停止；未完成的账号在 loop 收尾时统一写成 skipped。
func (r *CookieTestRunner) Stop() {
	r.mu.Lock()
	cancel := r.cancel
	if r.running {
		r.stopped = true
	}
	r.mu.Unlock()
	if cancel != nil {
		cancel()
	}
}

// loop 轮次调度：每轮给所有未完成账号各试一次，代理类失败留到下一轮。
func (r *CookieTestRunner) loop(ctx context.Context, cfg *Config, mode string, targets []Account) {
	source := newCookieTestProxySource(r.pm)
	// pending 保存「仍需重试的账号」在 r.rows 中的下标
	pending := make([]int, 0, len(targets))
	for i := range targets {
		pending = append(pending, i)
	}
	// attempts / ownProxyFails 按下标累计，跨轮保留
	attempts := make([]int, len(targets))
	ownProxyFails := make([]int, len(targets))

	for len(pending) > 0 {
		if ctx.Err() != nil {
			break
		}
		r.beginRound(pending)

		round := make([]Account, 0, len(pending))
		proxies := make([]string, 0, len(pending))
		for _, index := range pending {
			target := targets[index]
			// 自带代理连续失败到阈值后降级：清掉 Proxy 字段，改由池子发牌
			degraded := false
			if hasOwnProxy(target) && ownProxyFails[index] >= cookieTestOwnProxyRounds {
				target.Proxy = nil
				degraded = true
			}
			addr := ""
			if !hasOwnProxy(target) {
				addr = source.Next(ctx)
			}
			round = append(round, target)
			proxies = append(proxies, addr)
			if degraded {
				r.markDegraded(index)
			}
		}

		next := 0
		results := runCookieTestPass(ctx, cfg, mode, round, func() string {
			addr := proxies[next]
			next++
			return addr
		})

		stillPending := make([]int, 0, len(pending))
		for i, index := range pending {
			result := results[i]
			attempts[index]++
			result.Attempts = attempts[index]
			if hasOwnProxy(round[i]) && result.retryable {
				ownProxyFails[index]++
			}
			// TaBiAI 的 refresh 一定会轮转凭据：站点下发了新代次就必须立刻落库，
			// 否则下一次（签到或检测）用旧代会被判重放，整条会话会被撤销。
			if result.rotatedCookie != "" {
				r.persistRotatedCookie(round[i].Name, result.rotatedCookie, &result)
			}
			// 被取消时不写终态，交给收尾统一处理成 skipped
			if ctx.Err() != nil && result.State == cookieTestStateSkipped {
				r.updateRow(index, func(row *CookieTestResult) {
					row.Attempts = attempts[index]
				})
				stillPending = append(stillPending, index)
				continue
			}
			r.setRow(index, result)
			if result.retryable {
				stillPending = append(stillPending, index)
			}
		}
		pending = stillPending

		if len(pending) == 0 || ctx.Err() != nil {
			break
		}
		select {
		case <-ctx.Done():
		case <-time.After(cookieTestRoundGap):
		}
	}

	r.finish(pending)
}

// hasOwnProxy 账号是否显式配置了自己的代理。
func hasOwnProxy(account Account) bool {
	return account.Proxy != nil && strings.TrimSpace(*account.Proxy) != ""
}

// persistRotatedCookie 把 refresh 轮转出的新凭据定点写回配置。
// 写入按账号名精确更新且全局串行（见 updateAccountCookie），不整份覆盖，
// 因此多个账号并发检测时不会互相踩掉对方的新值。
func (r *CookieTestRunner) persistRotatedCookie(name, cookie string, result *CookieTestResult) {
	if r.db == nil {
		return
	}
	found, err := updateAccountCookie(r.db, name, cookie)
	switch {
	case err != nil:
		log.Printf("[cookie-test] 账号 %q 的新 refresh cookie 落库失败: %v", name, err)
		result.Message += "（警告：新凭据未能保存，下次检测可能失败，请重新签发）"
	case !found:
		log.Printf("[cookie-test] 账号 %q 已不在配置中，跳过写回新 refresh cookie", name)
	default:
		log.Printf("[cookie-test] 账号 %q 的 refresh cookie 已轮转并保存", name)
		result.Message += "（凭据已轮转，新值已自动保存）"
	}
}

// beginRound 轮次自增，并把本轮要跑的账号标成 running。
func (r *CookieTestRunner) beginRound(pending []int) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.round++
	for _, index := range pending {
		if index < len(r.rows) {
			r.rows[index].State = cookieTestStateRunning
			r.rows[index].Message = fmt.Sprintf("第 %d 轮检测中", r.round)
		}
	}
}

func (r *CookieTestRunner) setRow(index int, result CookieTestResult) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if index < len(r.rows) {
		// 保留降级提示：message 前缀由 markDegraded 写入，重试结果拼在后面
		if prefix := r.rows[index].degradeNote; prefix != "" {
			result.degradeNote = prefix
			result.Message = prefix + result.Message
		}
		r.rows[index] = result
	}
}

func (r *CookieTestRunner) updateRow(index int, fn func(*CookieTestResult)) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if index < len(r.rows) {
		fn(&r.rows[index])
	}
}

// markDegraded 记录「已从账号代理切到代理池」，后续每轮结果都带上该前缀。
func (r *CookieTestRunner) markDegraded(index int) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if index < len(r.rows) && r.rows[index].degradeNote == "" {
		r.rows[index].degradeNote = "账号代理连续失败，已切换代理池；"
	}
}

// finish 收尾：把仍未定终态的账号写成 skipped，并结束任务。
func (r *CookieTestRunner) finish(pending []int) {
	r.mu.Lock()
	defer r.mu.Unlock()
	for _, index := range pending {
		if index >= len(r.rows) {
			continue
		}
		row := r.rows[index]
		reason := strings.TrimSpace(row.Message)
		if row.State == cookieTestStateRunning || row.State == cookieTestStatePending {
			if reason == "" || strings.Contains(reason, "轮检测中") || reason == "排队中" {
				reason = "尚未取得站点结论"
			}
		}
		note := row.degradeNote
		r.rows[index] = CookieTestResult{
			Name:       row.Name,
			URL:        row.URL,
			State:      cookieTestStateSkipped,
			UserID:     row.UserID,
			DurationMS: row.DurationMS,
			Message: fmt.Sprintf("%s已手动停止（共尝试 %d 次，最后失败：%s）",
				note, row.Attempts, reason),
			Attempts:    row.Attempts,
			Proxy:       row.Proxy,
			degradeNote: note,
		}
	}
	r.running = false
	r.finishedAt = time.Now()
	if len(pending) > 0 {
		log.Printf("[cookie-test] 任务结束：%d 个账号在停止时仍未取得结论", len(pending))
	}
}

// cookieTestProxySource 代理发牌器：从代理池可用列表里轮转取地址。
// 列表取空就重新拉一次；池子为空时返回空串（本轮直连），避免任务干等。
type cookieTestProxySource struct {
	mu    sync.Mutex
	pm    *ProxyManager
	addrs []string
	next  int
}

func newCookieTestProxySource(pm *ProxyManager) *cookieTestProxySource {
	return &cookieTestProxySource{pm: pm}
}

func (s *cookieTestProxySource) Next(ctx context.Context) string {
	if s == nil || s.pm == nil || ctx.Err() != nil {
		return ""
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.next >= len(s.addrs) {
		// limit=0：池子里有多少可用就全部拿来轮转
		s.addrs = s.pm.AvailableAddrs(0)
		s.next = 0
	}
	if len(s.addrs) == 0 {
		return ""
	}
	addr := s.addrs[s.next]
	s.next++
	return addr
}
