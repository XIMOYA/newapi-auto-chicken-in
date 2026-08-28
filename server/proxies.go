/*
server/proxies.go
服务器端代理池：SQLite 数据层 + 抓取/测通/刷新逻辑

职责：
- proxies 表：保存抓取到的代理条目（来源 / host:port / 延迟 / 存活状态 / 时间）
- FetchProxiesFromSources：并发抓取所有配置的 sources，解析 host:port
- TestProxyLatency：对单个代理打 test_url 测通并返回延迟（毫秒）
- RefreshProxies：刷新流程（抓取 → 去重 → 并入库里存活的老代理 → 并发测通 → 按延迟排序 → 保存；saveLimit<=0 不限制）
- 后台协程：按 refresh_minutes 周期调用 RefreshProxies（由 main 启动）

安全边界：测通只打配置的 test_url（默认 agentrouter.org，站长自己的站点），
绝不碰账号所在的第三方签到站 —— 几百个出口 IP 密集访问会被当成扫描。
*/
package main

import (
	"context"
	"database/sql"
	"encoding/json"
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
	ID     int64  `json:"id"`
	Source string `json:"source"`
	// Addr 代理地址。多数是裸 host:port（默认按 http 代理使用）；socks5 源解析出来的
	// 会带 scheme 前缀（socks5://host:port），测通与下发全链路按完整地址处理。
	Addr        string `json:"addr"`
	LatencyMs   int    `json:"latency_ms"`
	Alive       bool   `json:"alive"`
	LastChecked string `json:"last_checked_at"`
	LastAliveAt string `json:"last_alive_at,omitempty"`
	SpeedBps    int64  `json:"speed_bps"` // 实测下载字节/秒（0=未测速）
}

var ipPortRe = regexp.MustCompile(`\b(\d{1,3}(?:\.\d{1,3}){3}):(\d{1,5})\b`)

// maxProxySourceBytes 单个代理源允许读入的最大字节数。
// 代理列表通常只有几十 KB，8 MiB 留足余量；主要用途是挡住上游异常/被投毒时的超大响应。
const maxProxySourceBytes = 8 << 20

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
//
// 先尝试按 JSON 结构化源解析（站大爷 zdaye 等：{data:{proxy_list:[{ip,port,protocol}]}}）——
// 那种响应里 ip 与 port 是分开的字段，正则的 ip:port 连写模式一条都抓不到。不是 JSON
// 或没有 proxy_list 字段时才回落到正则，纯文本/HTML 源行为完全不变。
func parseProxyLines(text string) []string {
	if items := parseProxyJSON(text); items != nil {
		// 是可识别的 JSON 代理列表（哪怕过滤后为空）就以它为准，不再回落正则乱抓
		return items
	}
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

// proxyJSONItem 结构化代理源里的一条。port 用 json.Number 兼容数字与字符串两种写法。
type proxyJSONItem struct {
	IP       string      `json:"ip"`
	Port     json.Number `json:"port"`
	Protocol string      `json:"protocol"`
}

// normalize 把一条 JSON 记录转成可用的代理地址；无法使用时返回空串。
//
// 协议决定前缀：
//   - socks5 → socks5://host:port（Go http.Transport 与 curl_cffi/Playwright 都原生支持）
//   - http/https/空 → 裸 host:port（沿用现状，池子里绝大多数是 http 代理）
//   - 其余（socks4 等）→ 跳过。net/http 的 Proxy 只认 socks5，socks4 即便留下也测不通，
//     不如当场丢掉，省下测通配额
func (it proxyJSONItem) normalize() string {
	ip := strings.TrimSpace(it.IP)
	port := strings.TrimSpace(it.Port.String())
	if !validIP(ip) || !validPort(port) {
		return ""
	}
	switch strings.ToLower(strings.TrimSpace(it.Protocol)) {
	case "socks5", "socks5h", "socks":
		return "socks5://" + ip + ":" + port
	case "", "http", "https":
		return ip + ":" + port
	default:
		return ""
	}
}

// parseProxyJSON 解析结构化代理源。返回 nil 表示「不是可识别的 JSON 代理列表」
// （交回正则处理）；返回非 nil（可能是空切片）表示「已按 JSON 处理完」，此时即使
// 过滤后为空也不该再回落正则，否则会在 JSON 文本里乱抓出 IP 片段。
func parseProxyJSON(text string) []string {
	trimmed := strings.TrimSpace(text)
	if trimmed == "" || (trimmed[0] != '{' && trimmed[0] != '[') {
		return nil
	}
	// 同时兼容 {data:{proxy_list}}（站大爷）、顶层 {proxy_list} 以及顶层就是数组
	var doc struct {
		ProxyList []proxyJSONItem `json:"proxy_list"`
		Data      struct {
			ProxyList []proxyJSONItem `json:"proxy_list"`
		} `json:"data"`
	}
	if err := json.Unmarshal([]byte(trimmed), &doc); err == nil {
		items := doc.Data.ProxyList
		if items == nil {
			items = doc.ProxyList
		}
		if items != nil {
			return normalizeProxyItems(items)
		}
	}
	// 顶层直接是数组的情况：[{ip,port,protocol}, ...]
	if trimmed[0] == '[' {
		var arr []proxyJSONItem
		if err := json.Unmarshal([]byte(trimmed), &arr); err == nil && arr != nil {
			return normalizeProxyItems(arr)
		}
	}
	return nil
}

func normalizeProxyItems(items []proxyJSONItem) []string {
	out := []string{}
	for _, it := range items {
		if addr := it.normalize(); addr != "" {
			out = append(out, addr)
		}
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

// beginRun 尝试获取「刷新/测速」互斥执行权；已有任务在进行时返回 false。
// 刷新与测速共用同一个互斥位：禁止并发清库/写库/覆盖同一进度对象。
func (m *ProxyManager) beginRun(stage string) bool {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.running {
		return false
	}
	m.running = true
	m.progress = ProxyProgress{
		Running:   true,
		Stage:     stage,
		StartedAt: time.Now().UTC().Format(time.RFC3339),
	}
	return true
}

// endRun 释放互斥执行权并记录完成时间；errMsg 非空时写入 lastErr。
func (m *ProxyManager) endRun(errMsg string) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.running = false
	m.lastRun = time.Now()
	m.progress.Running = false
	m.progress.Stage = "done"
	if t, err := time.Parse(time.RFC3339, m.progress.StartedAt); err == nil {
		m.progress.DurationSec = int(time.Since(t).Seconds())
	}
	if errMsg != "" {
		m.lastErr = errMsg
	}
}

// LastRun / LastError / IsRunning 供状态接口展示。
func (m *ProxyManager) LastRun() time.Time { m.mu.RLock(); defer m.mu.RUnlock(); return m.lastRun }
func (m *ProxyManager) LastError() string  { m.mu.RLock(); defer m.mu.RUnlock(); return m.lastErr }
func (m *ProxyManager) IsRunning() bool    { m.mu.RLock(); defer m.mu.RUnlock(); return m.running }

// LastRunRFC3339 返回最近一次刷新/测速完成时间（RFC3339）；从未运行过返回空串，
// 避免前端拿到 Go 时间零点（0001-01-01T00:00:00Z）。
func (m *ProxyManager) LastRunRFC3339() string {
	m.mu.RLock()
	defer m.mu.RUnlock()
	if m.lastRun.IsZero() {
		return ""
	}
	return m.lastRun.UTC().Format(time.RFC3339)
}

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
	// 限制读入体积：默认源是 6 个第三方 GitHub raw 地址，上游被投毒/劫持返回
	// 超大响应就能把服务打到 OOM（对比站点检测那边已有 4MiB 上限）。
	// 超限只截断不整体失败 —— 已读到的部分照样能解析出可用代理。
	buf := new(strings.Builder)
	read, err := copyBody(buf, io.LimitReader(resp.Body, maxProxySourceBytes))
	if err != nil {
		log.Printf("[proxy] 读源 %s 失败: %v", url, err)
		return nil
	}
	if read >= maxProxySourceBytes {
		log.Printf("[proxy] 源 %s 响应超过 %d MiB 上限，已截断读取（可能是上游异常）",
			url, maxProxySourceBytes>>20)
	}
	return parseProxyLines(buf.String())
}

// proxyTestTransport 测通/测速共用的 HTTP Transport：连接池跨代理复用，
// 避免每测一条代理就新建 Transport，堆积大量空闲连接与端口。
// 目标代理地址经请求上下文传入（见 ctxKeyProxyAddr / proxyFromRequest）。
var proxyTestTransport = &http.Transport{
	Proxy: proxyFromRequest,
	DialContext: (&net.Dialer{
		Timeout:   10 * time.Second,
		KeepAlive: 30 * time.Second,
	}).DialContext,
	MaxIdleConns:          256,
	MaxIdleConnsPerHost:   16,
	IdleConnTimeout:       90 * time.Second,
	TLSHandshakeTimeout:   10 * time.Second,
	ExpectContinueTimeout: 1 * time.Second,
}

// ctxKeyProxyAddr 请求上下文键：当前请求要经过的代理地址（host:port）。
type ctxKeyProxyAddr struct{}

// proxyFromRequest 从请求上下文读取代理地址；未设置时直连。
//
// 地址已带 scheme（socks5://host:port）就原样解析；裸 host:port 仍按 http 代理处理
// —— 池子里绝大多数是 http 代理，保持它们不写前缀，兼容库里的历史数据。
func proxyFromRequest(r *http.Request) (*url.URL, error) {
	if addr, ok := r.Context().Value(ctxKeyProxyAddr{}).(string); ok && addr != "" {
		if strings.Contains(addr, "://") {
			return url.Parse(addr)
		}
		return url.Parse("http://" + addr)
	}
	return nil, nil
}

// testProxyLatency 测一个代理：GET test_url，返回延迟毫秒；失败返回 -1。
// 使用 ctx 控制生命周期：时间盒一到，进行中的请求立即取消，不拖住刷新流程。
func testProxyLatency(ctx context.Context, addr, testURL string, timeout int) int {
	reqCtx := ctx
	if timeout > 0 {
		// 单条也有超时上限；与整体时间盒取更早生效的一个
		var cancel context.CancelFunc
		reqCtx, cancel = context.WithTimeout(ctx, time.Duration(timeout)*time.Second)
		defer cancel()
	}
	reqCtx = context.WithValue(reqCtx, ctxKeyProxyAddr{}, addr)
	req, err := http.NewRequestWithContext(reqCtx, "GET", testURL, nil)
	if err != nil {
		return -1
	}
	start := time.Now()
	resp, err := proxyTestTransport.RoundTrip(req)
	if err != nil {
		return -1
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return -1
	}
	// 读完整 body 确保链路真的通（请求受 ctx 超时约束），并支持连接复用
	_, _ = io.Copy(io.Discard, resp.Body)
	return int(time.Since(start).Milliseconds())
}

// RefreshProxies 全量刷新：抓取 → 去重 → 并发测通 → 按延迟排序 → 保存。
// 返回可用代理数；saveLimit<=0 表示不限制（上游抓到多少就全测，测通多少存多少）。
//
// 结束策略（目标驱动，不做死时间盒）：
//   - saveLimit>0 时，测通过程中一旦可用数达到 saveLimit 就提前收手（免费代理够用即可，省时间）
//   - 达不到 saveLimit（或未限制）就继续测完全部候选，直到耗尽（不中途放弃）
//   - 仅保留一个宽泛的绝对上限 REFRESH_ABSOLUTE_MAX，防止极端卡死拖死后台
func (m *ProxyManager) RefreshProxies(cfg ProxyPool, saveLimit int) (aliveCount int, retErr error) {
	// 刷新与测速共用互斥位：并发刷新/测速时直接拒绝，避免清库/写库互相踩踏
	if !m.beginRun("fetching") {
		return 0, fmt.Errorf("代理池刷新或测速已在进行中")
	}
	defer func() {
		msg := ""
		if retErr != nil {
			msg = retErr.Error()
		}
		m.endRun(msg)
	}()

	sources := cfg.Sources
	if len(sources) == 0 {
		sources = defaultProxySources
	}
	// saveLimit <= 0 表示不限制：上游抓到多少就全测、测通多少就全存
	unlimited := saveLimit <= 0
	testURL := cfg.TestURL
	if testURL == "" {
		testURL = "https://agentrouter.org/"
	}
	timeout := cfg.Timeout
	if timeout <= 0 {
		timeout = 8
	}

	// 1) 并发抓取所有源（阶段：fetching）。每个源写自己的下标，不需要加锁。
	started := time.Now()
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
			defer recoverPanic("代理源抓取")
			perSource[idx] = fetchSource(s, timeout)
		}(i, src)
	}
	wg.Wait()

	// 2) 按源轮转合并去重：免费代理源普遍把「刚验过/存活率高」的排在列表前面，
	//    用 map 迭代会把这个顺序打乱，导致提前停时测到的是随机子集。
	all := make([]proxyCandidate, 0)
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
			all = append(all, proxyCandidate{addr, sources[i]})
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

	// 2.5) 把库里现存的存活代理也拉进候选，放在最前面。
	//
	// 免费代理源的列表天天变，一个昨天还好用的出口今天可能压根不在源里了。原来的
	// 做法是整表替换，那种代理直接消失——即使它还通着。所以这里先把老池子捞回来，
	// 让它们和新抓的一起过测通：能通就留下（和新测通的合并去重），不通自然淘汰。
	if existing, eerr := m.queryProxies(true, 0); eerr == nil {
		before := len(all)
		all = mergeExistingCandidates(all, existing)
		if kept := len(all) - before; kept > 0 {
			log.Printf("[proxy] 把库里 %d 条存活代理并入候选复测（新源候选 %d 条）",
				kept, before)
		}
	} else {
		// 读不出老池子不该让整轮刷新失败：退化成原来的「只测新抓的」
		log.Printf("[proxy] 读取现存代理失败，本轮只测新抓的候选: %v", eerr)
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
	aliveCount = 0
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
		go func(it proxyCandidate) {
			defer wg2.Done()
			defer recoverPanic("代理测通")
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

	/*
		沿用上一轮的测速值：refresh 只测连通性和延迟，构造出来的 ProxyEntry 里
		SpeedBps 是零值，而 replaceAll 是清表重插 —— 不把旧值带过来的话，页面上
		手动测速的成果每刷新一次就被抹平一次，开了后台刷新等于白测。
		只给这轮仍然测通的代理沿用：死掉的代理留着旧速度没有意义。
	*/
	if speeds, serr := m.speedByAddr(); serr != nil {
		log.Printf("[proxy] 读取旧测速值失败，本轮 speed_bps 将重置为 0: %v", serr)
	} else if len(speeds) > 0 {
		kept := applyKnownSpeeds(results, speeds)
		log.Printf("[proxy] 沿用上一轮测速值 %d 条（旧库有 %d 条测过速）", kept, len(speeds))
	}

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

	// 顺手清掉过期反馈：免费代理换得快，不清这张表只会一直涨。
	// 清理失败不影响刷新结果，记一行日志就够。
	if n, perr := m.PruneProxyFeedback(feedbackRetentionDays); perr != nil {
		log.Printf("[proxy] 清理过期代理反馈失败: %v", perr)
	} else if n > 0 {
		log.Printf("[proxy] 清理 %d 条超过 %d 天未更新的代理反馈", n, feedbackRetentionDays)
	}
	return aliveCount, nil
}

// REFRESH_ABSOLUTE_MAX 刷新的绝对上限：只防极端卡死，正常情况下由
// 「达到 saveLimit 提前停」或「测完全部候选」结束，不会走到这里。
const REFRESH_ABSOLUTE_MAX = 20 * time.Minute

// speedByAddr 取当前库里已测过速的代理，addr -> speed_bps。
// 供刷新时把上一轮的测速结果带到新记录上（refresh 自己不测速）。
func (m *ProxyManager) speedByAddr() (map[string]int64, error) {
	rows, err := m.db.Query(`SELECT addr, speed_bps FROM proxies WHERE speed_bps > 0`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := make(map[string]int64)
	for rows.Next() {
		var addr string
		var speed int64
		if err := rows.Scan(&addr, &speed); err != nil {
			return nil, err
		}
		out[addr] = speed
	}
	return out, rows.Err()
}

/*
applyKnownSpeeds 把已知测速值填回这轮刷新结果，返回填了多少条。
只认存活的代理：这轮都没测通，留着上一轮的速度只会让它在排序里插到前面去。
*/
func applyKnownSpeeds(results []ProxyEntry, speeds map[string]int64) int {
	kept := 0
	for i := range results {
		if !results[i].Alive {
			continue
		}
		if s, ok := speeds[results[i].Addr]; ok && s > 0 {
			results[i].SpeedBps = s
			kept++
		}
	}
	return kept
}

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

/*
ListProxies 查询代理列表。aliveOnly=true 只返回可用。

只要可用的，就是优选路径（Actions 预取、页面「只看可用」），走 listAliveRanked：
先把全部存活代理取出来按 Actions 实测表现精排，再截断。把 LIMIT 交给 SQL 会先按
测速/延迟砍掉一批，砍掉的里面可能正是实测最稳的那些。
展示全部（含已死）时保持原来的 SQL 顺序，前端自己还会再排一次。
*/
func (m *ProxyManager) ListProxies(aliveOnly bool, limit int) ([]ProxyEntry, error) {
	if aliveOnly {
		return m.listAliveRanked(limit)
	}
	return m.queryProxies(false, limit)
}

// listAliveRanked 取全部存活代理，按反馈分档排序后再截断。
func (m *ProxyManager) listAliveRanked(limit int) ([]ProxyEntry, error) {
	entries, err := m.queryProxies(true, 0)
	if err != nil {
		return nil, err
	}
	if fb, ferr := m.FeedbackByAddr(); ferr != nil {
		// 反馈读不出来不该让整个列表失败：退回 SQL 给的测速/延迟顺序，行为等同改造前
		log.Printf("[proxy] 读取代理反馈失败，本次按测速/延迟排序: %v", ferr)
	} else if len(fb) > 0 {
		sortProxiesByFeedback(entries, fb)
	}
	if limit > 0 && len(entries) > limit {
		entries = entries[:limit]
	}
	return entries, nil
}

// queryProxies 按 SQL 顺序取记录：存活优先 -> 测速降序 -> 延迟升序。
func (m *ProxyManager) queryProxies(aliveOnly bool, limit int) ([]ProxyEntry, error) {
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

// proxyCandidate 一条待测候选：地址 + 它的来源标记。
type proxyCandidate struct{ addr, source string }

/*
mergeExistingCandidates 把库里现存的存活代理并进新抓来的候选，老的排在前面。

为什么要并：免费代理源的列表天天变，一个昨天还好用的出口今天可能压根不在源里了。
原来整表替换的做法会让这种代理直接消失——即使它还通着。并进来一起过测通，能通就留，
不通自然淘汰，池子于是收敛在「持续可用的那批」而不是「今天恰好被源收录的那批」。

为什么老的放最前面：saveLimit > 0 时测通会「达标提前停」，先测已验证过的能更快凑够
数，也让稳定的出口优先留下；它们的存活率本来就高于刚从源里抓来的。

去重按地址，新源里已有的不重复排队（保留新来源的记账）。fresh 自身在轮转合并阶段
已经去过重了，这里只需要防「老池 ∩ 新源」和老池内部的重复。
*/
func mergeExistingCandidates(fresh []proxyCandidate, existing []ProxyEntry) []proxyCandidate {
	if len(existing) == 0 {
		return fresh
	}
	seen := make(map[string]bool, len(fresh)+len(existing))
	for _, c := range fresh {
		seen[c.addr] = true
	}
	kept := make([]proxyCandidate, 0, len(existing))
	for _, e := range existing {
		if e.Addr == "" || seen[e.Addr] {
			continue
		}
		seen[e.Addr] = true
		kept = append(kept, proxyCandidate{e.Addr, e.Source})
	}
	if len(kept) == 0 {
		return fresh
	}
	return append(kept, fresh...)
}

// DefaultSpeedTestURL 测速端点的默认值：Cloudflare 官方的下行测速接口（1MB）。
//
// 可以在网页端改成别的地址（proxy_pool.speed_test_url）。吞吐是按实际读到的字节数
// 算的，所以换地址不用同步改任何"预期大小"；但目标得能稳定吐出足够数据——几 KB 的
// 页面测出来的数字受 TLS 握手开销主导，拿它排序没有意义。
const DefaultSpeedTestURL = "https://speed.cloudflare.com/__down?bytes=1048576"

// speedTestURLOf 取生效的测速地址，空值回落到默认端点。
func speedTestURLOf(cfg ProxyPool) string {
	if url := strings.TrimSpace(cfg.SpeedTestURL); url != "" {
		return url
	}
	return DefaultSpeedTestURL
}

// testProxySpeed 通过代理下载测速数据，返回实际吞吐（字节/秒）；失败返回 -1。
func testProxySpeed(ctx context.Context, addr, speedURL string, timeout int) int64 {
	reqCtx := ctx
	if timeout > 0 {
		var cancel context.CancelFunc
		reqCtx, cancel = context.WithTimeout(ctx, time.Duration(timeout)*time.Second)
		defer cancel()
	}
	reqCtx = context.WithValue(reqCtx, ctxKeyProxyAddr{}, addr)
	req, err := http.NewRequestWithContext(reqCtx, "GET", speedURL, nil)
	if err != nil {
		return -1
	}
	start := time.Now()
	resp, err := proxyTestTransport.RoundTrip(req)
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

// speedTestBackgroundTimeout 测速后台任务的独立绝对超时：
// 不随 HTTP 请求结束/取消而中断（handler 在独立 context 中执行测速）。
const speedTestBackgroundTimeout = 120 * time.Second

// SpeedTest 对指定代理列表测速并写库（返回成功更新的条目数）。
// addrs 为空表示对全部可用代理测速。
// 与 RefreshProxies 共用互斥位：并发刷新/测速时直接拒绝。
//
// speedURL 空串时回落到 DefaultSpeedTestURL —— 调用方通常直接传
// speedTestURLOf(cfg.ProxyPool)。
func (m *ProxyManager) SpeedTest(ctx context.Context, addrs []string, timeout int,
	speedURL string) (updated int, retErr error) {
	if strings.TrimSpace(speedURL) == "" {
		speedURL = DefaultSpeedTestURL
	}
	if !m.beginRun("speedtest") {
		return 0, fmt.Errorf("代理池刷新或测速已在进行中")
	}
	defer func() {
		msg := ""
		if retErr != nil {
			msg = retErr.Error()
		}
		m.endRun(msg)
	}()
	if timeout <= 0 {
		timeout = 15
	}

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
			defer recoverPanic("代理测速")
			defer func() { <-sem }()
			if ctx.Err() != nil {
				return
			}
			speed := testProxySpeed(ctx, e.Addr, speedURL, timeout)
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

/*
parseShardParam 解析 ?shard=I/N。空串表示不分片，返回 (0, 0, nil)。

格式故意做窄：只认 `正整数/正整数`，不接受负数、小数、空段。参数写错时宁可报错也不
静默当作不分片 —— 客户端会以为自己拿到的是独占的一批代理，实际上和别的 job 撞了，
这种错要在第一次调用就暴露出来。
*/
func parseShardParam(raw string) (int, int, error) {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return 0, 0, nil
	}
	left, right, found := strings.Cut(raw, "/")
	if !found {
		return 0, 0, fmt.Errorf("shard 需要 I/N 形式，收到 %q", raw)
	}
	index, err1 := strconv.Atoi(strings.TrimSpace(left))
	total, err2 := strconv.Atoi(strings.TrimSpace(right))
	if err1 != nil || err2 != nil {
		return 0, 0, fmt.Errorf("shard 的两段都必须是整数，收到 %q", raw)
	}
	if total < 1 {
		return 0, 0, fmt.Errorf("shard 的总片数必须 >= 1，收到 %d", total)
	}
	if index < 1 || index > total {
		return 0, 0, fmt.Errorf("shard 的序号必须在 1..%d 之间，收到 %d", total, index)
	}
	return index, total, nil
}

/*
shardAddrs 按轮转取第 index 片（1-based，共 total 片）。

轮转而不是切连续块：列表是按优选排好的，切块会让第 1 片吃掉所有最优代理、后面的片
只剩次品。轮转让每片都能均匀拿到各档质量 —— 第 1 片拿第 1、4、7 名，第 2 片拿第
2、5、8 名。各片之间完全不重叠，几个 job 并行也不会把同一个出口 IP 同时分给多个账号。
*/
func shardAddrs(addrs []string, index, total int) []string {
	if total <= 1 || index < 1 || index > total {
		return addrs
	}
	out := make([]string, 0, len(addrs)/total+1)
	for i := index - 1; i < len(addrs); i += total {
		out = append(out, addrs[i])
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
