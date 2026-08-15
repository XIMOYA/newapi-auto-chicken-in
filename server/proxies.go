/*
server/proxies.go
服务器端代理池：SQLite 数据层 + 抓取/测通/刷新逻辑

职责：
- proxies 表：保存抓取到的代理条目（来源 / host:port / 延迟 / 存活状态 / 时间）
- FetchProxiesFromSources：并发抓取所有配置的 sources，解析 host:port
- TestProxyLatency：对单个代理打 test_url 测通并返回延迟（毫秒）
- RefreshProxies：全量刷新流程（抓取 → 去重 → 并发测通 → 按延迟排序 → 保存；saveLimit<=0 不限制）
- 后台协程：按 refresh_minutes 周期调用 RefreshProxies（由 main 启动）

安全边界：测通只打配置的 test_url（默认 api.ipify.org），绝不打目标站点。
*/
package main

import (
	"context"
	"database/sql"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"net/url"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
)

// ProxyEntry 一条代理记录。
type ProxyEntry struct {
	ID          int64  `json:"id"`
	Source      string `json:"source"`
	Addr        string `json:"addr"` // host:port
	LatencyMs   int    `json:"latency_ms"`
	Alive       bool   `json:"alive"`
	LastChecked string `json:"last_checked_at"`
	LastAliveAt string `json:"last_alive_at,omitempty"`
	SpeedBps    int64  `json:"speed_bps"` // 实测下载字节/秒（0=未测速）
}

var ipPortRe = regexp.MustCompile(`\b(\d{1,3}(?:\.\d{1,3}){3}):(\d{1,5})\b`)

// createProxiesTable 建 proxies 表（幂等）。
func createProxiesTable(db *sql.DB) error {
	_, err := db.Exec(`CREATE TABLE IF NOT EXISTS proxies (
		id            INTEGER PRIMARY KEY AUTOINCREMENT,
		source        TEXT    NOT NULL,
		addr          TEXT    NOT NULL,
		latency_ms    INTEGER NOT NULL DEFAULT 0,
		alive         INTEGER NOT NULL DEFAULT 0,
		last_checked  TEXT    NOT NULL,
		last_alive_at TEXT,
		speed_bps     INTEGER NOT NULL DEFAULT 0
	)`)
	if err != nil {
		return fmt.Errorf("建 proxies 表失败: %w", err)
	}
	// 老库迁移：表已存在但缺 speed_bps 列时补上（幂等，报错说明列已在则忽略）
	cols, err := db.Query(`PRAGMA table_info(proxies)`)
	if err == nil {
		hasSpeed := false
		for cols.Next() {
			var cid int
			var cname, ctype string
			var notnull, pk int
			var dflt any
			if cols.Scan(&cid, &cname, &ctype, &notnull, &dflt, &pk) == nil && cname == "speed_bps" {
				hasSpeed = true
			}
		}
		cols.Close()
		if !hasSpeed {
			if _, err := db.Exec(`ALTER TABLE proxies ADD COLUMN speed_bps INTEGER NOT NULL DEFAULT 0`); err != nil {
				return fmt.Errorf("迁移 proxies.speed_bps 列失败: %w", err)
			}
		}
	}
	_, err = db.Exec(`CREATE INDEX IF NOT EXISTS idx_proxies_addr ON proxies(addr)`)
	if err != nil {
		return fmt.Errorf("建 proxies 索引失败: %w", err)
	}
	_, err = db.Exec(`CREATE INDEX IF NOT EXISTS idx_proxies_alive ON proxies(alive)`)
	if err != nil {
		return fmt.Errorf("建 proxies alive 索引失败: %w", err)
	}
	return nil
}

// parseProxyLines 从文本/HTML 提取 host:port 列表（兼容纯文本行与 89ip HTML）。
func parseProxyLines(text string) []string {
	out := []string{}
	for _, m := range ipPortRe.FindAllStringSubmatch(text, -1) {
		ip, port := m[1], m[2]
		if !validIP(ip) || !validPort(port) {
			continue
		}
		out = append(out, ip+":"+port)
	}
	return out
}

func validIP(ip string) bool {
	parts := strings.Split(ip, ".")
	if len(parts) != 4 {
		return false
	}
	for _, p := range parts {
		if len(p) == 0 || len(p) > 3 {
			return false
		}
		for _, c := range p {
			if c < '0' || c > '9' {
				return false
			}
		}
		n := 0
		for _, c := range p {
			n = n*10 + int(c-'0')
		}
		if n > 255 {
			return false
		}
	}
	return true
}

func validPort(port string) bool {
	if len(port) == 0 || len(port) > 5 {
		return false
	}
	for _, c := range port {
		if c < '0' || c > '9' {
			return false
		}
	}
	n := 0
	for _, c := range port {
		n = n*10 + int(c-'0')
	}
	return n >= 1 && n <= 65535
}

// ProxyManager 代理池管理：并发安全，持有 DB 引用与最近一次刷新结果。
type ProxyManager struct {
	db      *sql.DB
	mu      sync.RWMutex
	lastRun time.Time
	lastErr string
	running bool
	// 实时进度：抓取/测通/测速 阶段计数，供页面轮询展示
	progress ProxyProgress
}

// ProxyProgress 刷新/测速的实时进度（前端轮询 /api/proxies/stats 展示）。
type ProxyProgress struct {
	Running     bool   `json:"running"`
	Stage       string `json:"stage"`      // fetching | testing | speedtest | done
	Fetched     int    `json:"fetched"`    // 去重后的候选总数
	Candidates  int    `json:"candidates"` // 本次候选数（=Fetched）
	Tested      int    `json:"tested"`     // 已测完条数
	Alive       int    `json:"alive"`      // 当前可用条数
	Target      int    `json:"target"`     // 目标可用数（达到即提前结束）
	StartedAt   string `json:"started_at"`
	DurationSec int    `json:"duration_sec"`
}

func NewProxyManager(db *sql.DB) *ProxyManager {
	return &ProxyManager{db: db}
}

// LastRun / LastError / IsRunning 供状态接口展示。
func (m *ProxyManager) LastRun() time.Time { m.mu.RLock(); defer m.mu.RUnlock(); return m.lastRun }
func (m *ProxyManager) LastError() string  { m.mu.RLock(); defer m.mu.RUnlock(); return m.lastErr }
func (m *ProxyManager) IsRunning() bool    { m.mu.RLock(); defer m.mu.RUnlock(); return m.running }

// Progress 返回当前进度快照。
func (m *ProxyManager) Progress() ProxyProgress {
	m.mu.RLock()
	defer m.mu.RUnlock()
	return m.progress
}

func (m *ProxyManager) setProgress(p ProxyProgress) {
	m.mu.Lock()
	m.progress = p
	m.mu.Unlock()
}

func (m *ProxyManager) updateProgress(fn func(*ProxyProgress)) {
	m.mu.Lock()
	fn(&m.progress)
	m.mu.Unlock()
}

// fetchSource 抓取单个代理源，返回 host:port 列表。
// - 抓源超时与测通超时解耦：源文件可能几百 KB，8s 太紧会误杀慢源，用独立超时
// - 失败自动重试 1 次：网络抖动可恢复，避免一次失败就废掉整个源
func fetchSource(url string, timeout int) []string {
	fetchTimeout := timeout * 3
	if fetchTimeout < 15 {
		fetchTimeout = 15
	}
	if fetchTimeout > 60 {
		fetchTimeout = 60
	}
	for attempt := 1; attempt <= 2; attempt++ {
		items := fetchSourceOnce(url, fetchTimeout)
		if items != nil {
			return items
		}
		if attempt == 1 {
			log.Printf("[proxy] 源 %s 第 1 次抓取失败，重试…", url)
		}
	}
	return nil
}

func fetchSourceOnce(url string, timeout int) []string {
	client := &http.Client{Timeout: time.Duration(timeout) * time.Second}
	resp, err := client.Get(url)
	if err != nil {
		log.Printf("[proxy] 抓取源失败 %s: %v", url, err)
		return nil
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		log.Printf("[proxy] 源 %s HTTP %d", url, resp.StatusCode)
		return nil
	}
	// 读全部 body（文本可能较大，但远小于 Go 默认内存限制）
	buf := new(strings.Builder)
	if _, err := copyBody(buf, resp.Body); err != nil {
		log.Printf("[proxy] 读源 %s 失败: %v", url, err)
		return nil
	}
	return parseProxyLines(buf.String())
}

// testProxyLatency 测一个代理：GET test_url，返回延迟毫秒；失败返回 -1。
// 使用 ctx 控制生命周期：时间盒一到，进行中的请求立即取消，不拖住刷新流程。
func testProxyLatency(ctx context.Context, addr, testURL string, timeout int) int {
	proxyURL := "http://" + addr
	reqCtx := ctx
	if timeout > 0 {
		// 单条也有超时上限；与整体时间盒取更早生效的一个
		var cancel context.CancelFunc
		reqCtx, cancel = context.WithTimeout(ctx, time.Duration(timeout)*time.Second)
		defer cancel()
	}
	req, err := http.NewRequestWithContext(reqCtx, "GET", testURL, nil)
	if err != nil {
		return -1
	}
	client := &http.Client{
		Transport: &http.Transport{
			Proxy: http.ProxyURL(mustParseURL(proxyURL)),
		},
	}
	start := time.Now()
	resp, err := client.Do(req)
	if err != nil {
		return -1
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return -1
	}
	// 读一点 body 确保链路真的通
	one := make([]byte, 16)
	_, _ = resp.Body.Read(one)
	return int(time.Since(start).Milliseconds())
}

// RefreshProxies 全量刷新：抓取 → 去重 → 并发测通 → 按延迟排序 → 保存。
// 返回可用代理数；saveLimit<=0 表示不限制（上游抓到多少就全测，测通多少存多少）。
//
// 结束策略（目标驱动，不做死时间盒）：
//   - saveLimit>0 时，测通过程中一旦可用数达到 saveLimit 就提前收手（免费代理够用即可，省时间）
//   - 达不到 saveLimit（或未限制）就继续测完全部候选，直到耗尽（不中途放弃）
//   - 仅保留一个宽泛的绝对上限 REFRESH_ABSOLUTE_MAX，防止极端卡死拖死后台
func (m *ProxyManager) RefreshProxies(cfg ProxyPool, saveLimit int) (int, error) {
	m.mu.Lock()
	if m.running {
		m.mu.Unlock()
		return 0, fmt.Errorf("代理池刷新已在进行中")
	}
	m.running = true
	started := time.Now()
	m.mu.Unlock()
	defer func() {
		m.mu.Lock()
		m.running = false
		m.lastRun = time.Now()
		m.progress.Running = false
		m.progress.Stage = "done"
		m.progress.DurationSec = int(time.Since(started).Seconds())
		m.mu.Unlock()
	}()

	sources := cfg.Sources
	if len(sources) == 0 {
		sources = defaultProxySources
	}
	// saveLimit <= 0 表示不限制：上游抓到多少就全测、测通多少就全存
	unlimited := saveLimit <= 0
	testURL := cfg.TestURL
	if testURL == "" {
		testURL = "https://api.ipify.org"
	}
	timeout := cfg.Timeout
	if timeout <= 0 {
		timeout = 8
	}

	// 1) 并发抓取所有源（阶段：fetching）。每个源写自己的下标，不需要加锁。
	progressTarget := saveLimit
	if unlimited {
		progressTarget = 0
	}
	m.setProgress(ProxyProgress{Running: true, Stage: "fetching", Target: progressTarget, StartedAt: started.UTC().Format(time.RFC3339)})
	perSource := make([][]string, len(sources))
	var wg sync.WaitGroup
	for i, src := range sources {
		wg.Add(1)
		go func(idx int, s string) {
			defer wg.Done()
			perSource[idx] = fetchSource(s, timeout)
		}(i, src)
	}
	wg.Wait()

	// 2) 按源轮转合并去重：免费代理源普遍把「刚验过/存活率高」的排在列表前面，
	//    用 map 迭代会把这个顺序打乱，导致提前停时测到的是随机子集。
	type item struct{ addr, source string }
	all := make([]item, 0)
	seen := map[string]bool{}
	for round := 0; ; round++ {
		progressed := false
		for i, entries := range perSource {
			if round >= len(entries) {
				continue
			}
			progressed = true
			addr := entries[round]
			if seen[addr] {
				continue
			}
			seen[addr] = true
			all = append(all, item{addr, sources[i]})
		}
		if !progressed {
			break
		}
	}
	if len(all) == 0 {
		m.mu.Lock()
		m.lastErr = "所有代理源均未返回可用条目"
		m.mu.Unlock()
		return 0, nil
	}

	// 3) 并发测通（saveLimit > 0 时达标提前停；不限制时测完全部候选）
	workers := cfg.MaxWorkers
	if workers <= 0 {
		workers = 25
	}
	// 宽泛绝对上限：只防极端卡死，正常情况由「达标提前停」或「测完」结束
	ctx, cancel := context.WithTimeout(context.Background(), REFRESH_ABSOLUTE_MAX)
	defer cancel()

	m.setProgress(ProxyProgress{
		Running: true, Stage: "testing", Fetched: len(all), Candidates: len(all),
		Target: progressTarget, StartedAt: started.UTC().Format(time.RFC3339),
	})

	results := make([]ProxyEntry, 0, len(all))
	aliveCount := 0
	var mu2 sync.Mutex
	sem := make(chan struct{}, workers)
	var wg2 sync.WaitGroup
	stop := false
dispatch:
	for _, it := range all {
		mu2.Lock()
		halted := stop
		mu2.Unlock()
		if halted {
			break
		}
		// 信号量必须在**派发之前**获取：放在 goroutine 里获取等于不限流，
		// 几千个候选会瞬间 spawn 出几千个 goroutine 全堵在这里，
		// 而且 stop 标志根本来不及生效。
		select {
		case sem <- struct{}{}:
			// 抢到名额，正常派发；槽位由 goroutine 的 defer 归还（严格 1:1）
		case <-ctx.Done():
			// 整体时间盒已到：这一支没有占到信号量，绝不能继续往下走
			// 去 `<-sem`，那会误释放其他 goroutine 的槽位（channel 空时
			// 甚至永久阻塞）。直接跳出派发循环不再 spawn；
			// 已派发的 goroutine 各自检查 ctx.Err() 后归还自己的槽位退出。
			break dispatch
		}
		wg2.Add(1)
		go func(it item) {
			defer wg2.Done()
			defer func() { <-sem }()
			// 抢到名额到真正开工之间，可能有别的 goroutine 已把 alive 推到
			// saveLimit 并置位 stop：此时立即退出并归还名额，别让「达标即停」失效
			mu2.Lock()
			halted := stop
			mu2.Unlock()
			if halted {
				return
			}
			if ctx.Err() != nil {
				return
			}
			latency := testProxyLatency(ctx, it.addr, testURL, timeout)
			alive := latency >= 0
			mu2.Lock()
			results = append(results, ProxyEntry{
				Source:      it.source,
				Addr:        it.addr,
				LatencyMs:   maxInt(0, latency),
				Alive:       alive,
				LastChecked: time.Now().UTC().Format(time.RFC3339),
				LastAliveAt: time.Now().UTC().Format(time.RFC3339),
			})
			if alive {
				aliveCount++
			}
			m.updateProgress(func(p *ProxyProgress) {
				p.Tested = len(results)
				p.Alive = aliveCount
				p.Stage = "testing"
			})
			// 达标提前停：只在显式配了 saveLimit 时生效
			if !unlimited && aliveCount >= saveLimit {
				stop = true
			}
			mu2.Unlock()
		}(it)
	}
	wg2.Wait()
	if unlimited {
		log.Printf("[proxy] 刷新完成: 抓取 %d 候选，测通 %d 条，可用 %d 条（不限数量，耗时 %.0fs）",
			len(all), len(results), aliveCount, time.Since(started).Seconds())
	} else {
		log.Printf("[proxy] 刷新完成: 抓取 %d 候选，测通 %d 条，可用 %d 条（目标 %d，耗时 %.0fs）",
			len(all), len(results), aliveCount, saveLimit, time.Since(started).Seconds())
	}

	// 4) 排序：alive 在前，按延迟升序；dead 在后
	sortProxies(results)

	// 5) 截断保存：只在显式配了 saveLimit 时截断；不限制时全量落库
	if !unlimited && len(results) > saveLimit {
		results = results[:saveLimit]
	}

	// 5) 写库：清空旧表再插入（简单、一致；并发写用单连接串行，安全）
	if err := m.replaceAll(results); err != nil {
		return 0, err
	}
	aliveCount = 0
	for _, r := range results {
		if r.Alive {
			aliveCount++
		}
	}
	m.mu.Lock()
	m.lastErr = ""
	m.progress.Alive = aliveCount
	m.progress.Tested = len(results)
	m.mu.Unlock()
	return aliveCount, nil
}

// REFRESH_ABSOLUTE_MAX 刷新的绝对上限：只防极端卡死，正常情况下由
// 「达到 saveLimit 提前停」或「测完全部候选」结束，不会走到这里。
const REFRESH_ABSOLUTE_MAX = 20 * time.Minute

// replaceAll 清空 proxies 表并写入新结果（单事务）。
func (m *ProxyManager) replaceAll(entries []ProxyEntry) error {
	tx, err := m.db.Begin()
	if err != nil {
		return fmt.Errorf("开启事务: %w", err)
	}
	defer tx.Rollback() //nolint:errcheck

	if _, err := tx.Exec(`DELETE FROM proxies`); err != nil {
		return fmt.Errorf("清空 proxies: %w", err)
	}
	stmt, err := tx.Prepare(`INSERT INTO proxies (source, addr, latency_ms, alive, last_checked, last_alive_at, speed_bps) VALUES (?, ?, ?, ?, ?, ?, ?)`)
	if err != nil {
		return fmt.Errorf("准备插入: %w", err)
	}
	defer stmt.Close()
	for _, e := range entries {
		lastAlive := sql.NullString{}
		if e.Alive {
			lastAlive = sql.NullString{String: e.LastAliveAt, Valid: true}
		}
		if _, err := stmt.Exec(e.Source, e.Addr, e.LatencyMs, boolToInt(e.Alive), e.LastChecked, lastAlive, e.SpeedBps); err != nil {
			return fmt.Errorf("插入代理: %w", err)
		}
	}
	if err := tx.Commit(); err != nil {
		return fmt.Errorf("提交事务: %w", err)
	}
	return nil
}

// ListProxies 查询代理列表。aliveOnly=true 只返回可用；orderByLatency=true 按延迟排序。
func (m *ProxyManager) ListProxies(aliveOnly bool, limit int) ([]ProxyEntry, error) {
	// limit <= 0 表示不限制：SQLite 里 LIMIT -1 就是「全部」
	if limit <= 0 {
		limit = -1
	}
	q := `SELECT id, source, addr, latency_ms, alive, last_checked, COALESCE(last_alive_at,''), speed_bps FROM proxies`
	if aliveOnly {
		q += ` WHERE alive = 1`
	}
	q += ` ORDER BY alive DESC, speed_bps DESC, latency_ms ASC LIMIT ?`
	rows, err := m.db.Query(q, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []ProxyEntry{}
	for rows.Next() {
		var e ProxyEntry
		if err := rows.Scan(&e.ID, &e.Source, &e.Addr, &e.LatencyMs, &e.Alive, &e.LastChecked, &e.LastAliveAt, &e.SpeedBps); err != nil {
			return nil, err
		}
		out = append(out, e)
	}
	return out, rows.Err()
}

// SpeedTestURL Cloudflare 官方测速端点（下载 1MB 数据）。
const SpeedTestURL = "https://speed.cloudflare.com/__down?bytes=1048576"

// SpeedTestBytes 期望下载的字节数（与 URL 一致，用于计算吞吐）。
const SpeedTestBytes = 1048576

// testProxySpeed 通过代理下载测速数据，返回实际吞吐（字节/秒）；失败返回 -1。
func testProxySpeed(ctx context.Context, addr, speedURL string, timeout int) int64 {
	proxyURL := "http://" + addr
	reqCtx := ctx
	if timeout > 0 {
		var cancel context.CancelFunc
		reqCtx, cancel = context.WithTimeout(ctx, time.Duration(timeout)*time.Second)
		defer cancel()
	}
	req, err := http.NewRequestWithContext(reqCtx, "GET", speedURL, nil)
	if err != nil {
		return -1
	}
	client := &http.Client{
		Transport: &http.Transport{
			Proxy: http.ProxyURL(mustParseURL(proxyURL)),
		},
	}
	start := time.Now()
	resp, err := client.Do(req)
	if err != nil {
		return -1
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return -1
	}
	// 读全部响应用来测真实下载吞吐
	var total int64
	buf := make([]byte, 64*1024)
	for {
		n, err := resp.Body.Read(buf)
		total += int64(n)
		if err != nil {
			if err == io.EOF {
				break
			}
			return -1
		}
	}
	elapsed := time.Since(start).Seconds()
	if elapsed <= 0 {
		return -1
	}
	return int64(float64(total) / elapsed)
}

// SpeedTest 对指定代理列表测速并写库（返回成功更新的条目数）。
// addrs 为空表示对全部可用代理测速。
func (m *ProxyManager) SpeedTest(ctx context.Context, addrs []string, timeout int) (int, error) {
	if timeout <= 0 {
		timeout = 15
	}
	started := time.Now()
	m.setProgress(ProxyProgress{Running: true, Stage: "speedtest", Target: len(addrs), StartedAt: started.UTC().Format(time.RFC3339)})
	defer func() {
		m.updateProgress(func(p *ProxyProgress) {
			p.Running = false
			p.Stage = "done"
			p.DurationSec = int(time.Since(started).Seconds())
		})
	}()

	entries, err := m.ListProxies(true, 2000)
	if err != nil {
		return 0, err
	}
	target := make([]ProxyEntry, 0, len(entries))
	if len(addrs) == 0 {
		target = entries
	} else {
		want := map[string]bool{}
		for _, a := range addrs {
			want[a] = true
		}
		for _, e := range entries {
			if want[e.Addr] {
				target = append(target, e)
			}
		}
	}
	if len(target) == 0 {
		return 0, nil
	}
	m.updateProgress(func(p *ProxyProgress) {
		p.Target = len(target)
		p.Candidates = len(target)
	})

	workers := 8
	if len(target) < workers {
		workers = len(target)
	}
	var mu sync.Mutex
	var updated int
	var done int
	var wg sync.WaitGroup
	sem := make(chan struct{}, workers)
	for _, e := range target {
		// 与 RefreshProxies 同理：信号量必须在**派发之前**获取。
		// save_limit=0 不限量后 target 可能有几千条，放在 goroutine 里抢
		// 等于不限流，会瞬间 spawn 出几千个 goroutine 全堵在信号量上。
		acquired := false
		select {
		case sem <- struct{}{}:
			acquired = true
		case <-ctx.Done():
		}
		if ctx.Err() != nil {
			if acquired {
				<-sem
			}
			break
		}
		wg.Add(1)
		go func(e ProxyEntry) {
			defer wg.Done()
			defer func() { <-sem }()
			if ctx.Err() != nil {
				return
			}
			speed := testProxySpeed(ctx, e.Addr, SpeedTestURL, timeout)
			mu.Lock()
			done++
			m.updateProgress(func(p *ProxyProgress) {
				p.Tested = done
				p.Stage = "speedtest"
			})
			if speed < 0 {
				mu.Unlock()
				return
			}
			if _, err := m.db.Exec(`UPDATE proxies SET speed_bps = ? WHERE id = ?`, speed, e.ID); err != nil {
				mu.Unlock()
				return
			}
			updated++
			m.updateProgress(func(p *ProxyProgress) { p.Alive = updated })
			mu.Unlock()
		}(e)
	}
	wg.Wait()
	return updated, nil
}

// ProxyStats 各源统计。
type ProxyStats struct {
	Total    int            `json:"total"`
	Alive    int            `json:"alive"`
	BySource map[string]int `json:"by_source"`
}

// Stats 统计可用/总数/按源分布。
func (m *ProxyManager) Stats() (ProxyStats, error) {
	var st ProxyStats
	st.BySource = map[string]int{}
	rows, err := m.db.Query(`SELECT source, alive, COUNT(*) FROM proxies GROUP BY source, alive`)
	if err != nil {
		return st, err
	}
	defer rows.Close()
	for rows.Next() {
		var src string
		var alive bool
		var n int
		if err := rows.Scan(&src, &alive, &n); err != nil {
			return st, err
		}
		st.Total += n
		st.BySource[src] += n
		if alive {
			st.Alive += n
		}
	}
	return st, rows.Err()
}

// AvailableAddrs 返回当前可用代理地址列表（供 Actions 预取）。
func (m *ProxyManager) AvailableAddrs(limit int) []string {
	// limit <= 0 表示不限制：上游有多少可用就返回多少
	entries, err := m.ListProxies(true, limit)
	if err != nil {
		return nil
	}
	out := make([]string, 0, len(entries))
	for _, e := range entries {
		out = append(out, e.Addr)
	}
	return out
}

// --- helpers ---

func sortProxies(entries []ProxyEntry) {
	// alive 优先 -> 延迟升序 -> addr 字典序。用标准库排序：不限数量之后
	// results 可能有几千条，手写插入排序是 O(n²)。
	sort.Slice(entries, func(i, j int) bool {
		a, b := entries[i], entries[j]
		if a.Alive != b.Alive {
			return a.Alive
		}
		if a.LatencyMs != b.LatencyMs {
			return a.LatencyMs < b.LatencyMs
		}
		return a.Addr < b.Addr
	})
}

func boolToInt(b bool) int {
	if b {
		return 1
	}
	return 0
}

func maxInt(a, b int) int {
	if a > b {
		return a
	}
	return b
}

func mustParseURL(raw string) *url.URL {
	u, err := url.Parse(raw)
	if err != nil {
		panic(err)
	}
	return u
}

var defaultProxySources = []string{
	"https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
	"https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
	"https://raw.githubusercontent.com/proxy4parsing/proxy-list/main/http.txt",
	"https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt",
	"https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-http.txt",
	"https://www.89ip.cn/tqdl.html?api=1&num=200",
}

func copyBody(dst *strings.Builder, src io.Reader) (int64, error) {
	// 简化实现：读进 []byte 再写入 builder
	buf := make([]byte, 32*1024)
	var total int64
	for {
		n, err := src.Read(buf)
		if n > 0 {
			dst.Write(buf[:n])
			total += int64(n)
		}
		if err != nil {
			if err == io.EOF {
				return total, nil
			}
			return total, err
		}
	}
}
