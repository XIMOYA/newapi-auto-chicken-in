/*
server/proxies_test.go
代理池数据层与解析逻辑测试
*/
package main

import (
	"testing"
)

func TestParseProxyLines_Plain(t *testing.T) {
	lines := parseProxyLines("1.2.3.4:8080\n5.6.7.8:3128\n")
	if len(lines) != 2 {
		t.Fatalf("解析纯文本 = %v, want 2 条", lines)
	}
	if lines[0] != "1.2.3.4:8080" {
		t.Fatalf("第一条 = %q", lines[0])
	}
}

func TestParseProxyLines_HTML(t *testing.T) {
	html := `<script>var a="1.2.3.4:8080<br>5.6.7.8:3128<br>"</script>`
	lines := parseProxyLines(html)
	if len(lines) != 2 {
		t.Fatalf("解析 HTML = %v, want 2 条", lines)
	}
}

func TestParseProxyLines_FiltersInvalid(t *testing.T) {
	lines := parseProxyLines("999.1.1.1:80\n1.2.3.4:99999\n1.2.3.4\n1.2.3.4:8080\n")
	if len(lines) != 1 || lines[0] != "1.2.3.4:8080" {
		t.Fatalf("过滤非法后 = %v, want 仅 1.2.3.4:8080", lines)
	}
}

func TestValidIP(t *testing.T) {
	cases := map[string]bool{
		"1.2.3.4":         true,
		"255.255.255.255": true,
		"256.1.1.1":       false,
		"1.2.3":           false,
		"1.2.3.4.5":       false,
		"abc":             false,
		"":                false,
	}
	for ip, want := range cases {
		if validIP(ip) != want {
			t.Errorf("validIP(%q) = %v, want %v", ip, !want, want)
		}
	}
}

func TestValidPort(t *testing.T) {
	cases := map[string]bool{
		"80":    true,
		"3128":  true,
		"65535": true,
		"0":     false,
		"65536": false,
		"abc":   false,
		"":      false,
	}
	for p, want := range cases {
		if validPort(p) != want {
			t.Errorf("validPort(%q) = %v, want %v", p, !want, want)
		}
	}
}

func TestSortProxies(t *testing.T) {
	entries := []ProxyEntry{
		{Addr: "a", Alive: false, LatencyMs: 5},
		{Addr: "b", Alive: true, LatencyMs: 100},
		{Addr: "c", Alive: true, LatencyMs: 10},
	}
	sortProxies(entries)
	// alive 在前，延迟升序：c(10) b(100) a(dead)
	if entries[0].Addr != "c" || entries[1].Addr != "b" || entries[2].Addr != "a" {
		t.Fatalf("排序结果 = %v", entries)
	}
}

func TestProxyManager_ListAndStats(t *testing.T) {
	srv := newTestServer(t)
	// 直接插入几条数据验证查询
	entries := []ProxyEntry{
		{Source: "s1", Addr: "1.1.1.1:80", LatencyMs: 10, Alive: true, LastChecked: "now", LastAliveAt: "now"},
		{Source: "s1", Addr: "2.2.2.2:80", LatencyMs: 20, Alive: false, LastChecked: "now"},
		{Source: "s2", Addr: "3.3.3.3:80", LatencyMs: 30, Alive: true, LastChecked: "now", LastAliveAt: "now"},
	}
	if err := srv.proxies.replaceAll(entries); err != nil {
		t.Fatalf("replaceAll: %v", err)
	}
	alive, err := srv.proxies.ListProxies(true, 100)
	if err != nil {
		t.Fatal(err)
	}
	if len(alive) != 2 {
		t.Fatalf("可用代理 = %d, want 2", len(alive))
	}
	st, err := srv.proxies.Stats()
	if err != nil {
		t.Fatal(err)
	}
	if st.Total != 3 || st.Alive != 2 {
		t.Fatalf("stats = %+v, want total=3 alive=2", st)
	}
	if st.BySource["s1"] != 2 {
		t.Fatalf("by_source s1 = %d, want 2", st.BySource["s1"])
	}
	addrs := srv.proxies.AvailableAddrs(10)
	if len(addrs) != 2 {
		t.Fatalf("available addrs = %v", addrs)
	}
}

// 优选：available 必须按速度优先、延迟其次排序（最快的放前面给 Actions 用）
func TestProxyManager_AvailableOrderedByQuality(t *testing.T) {
	srv := newTestServer(t)
	entries := []ProxyEntry{
		{Source: "s1", Addr: "slow:80", LatencyMs: 50, Alive: true, SpeedBps: 100_000, LastChecked: "now", LastAliveAt: "now"},
		{Source: "s1", Addr: "fast:80", LatencyMs: 200, Alive: true, SpeedBps: 5_000_000, LastChecked: "now", LastAliveAt: "now"},
		{Source: "s2", Addr: "mid:80", LatencyMs: 30, Alive: true, SpeedBps: 800_000, LastChecked: "now", LastAliveAt: "now"},
	}
	if err := srv.proxies.replaceAll(entries); err != nil {
		t.Fatalf("replaceAll: %v", err)
	}
	addrs := srv.proxies.AvailableAddrs(10)
	if len(addrs) != 3 {
		t.Fatalf("available = %v, want 3 条", addrs)
	}
	// 速度高的在前：fast(5MB/s) > mid(800KB/s) > slow(100KB/s)
	if addrs[0] != "fast:80" || addrs[1] != "mid:80" || addrs[2] != "slow:80" {
		t.Fatalf("available 排序错误（应速度优先）: %v", addrs)
	}
}

// 测速值只对这轮仍然存活的代理沿用，缺失或为 0 的一律不动
func TestApplyKnownSpeeds(t *testing.T) {
	results := []ProxyEntry{
		{Addr: "alive:80", Alive: true},
		{Addr: "dead:80", Alive: false},
		{Addr: "unmeasured:80", Alive: true},
		{Addr: "zero:80", Alive: true},
	}
	speeds := map[string]int64{
		"alive:80": 3_000_000,
		"dead:80":  9_000_000,
		"zero:80":  0,
	}
	if kept := applyKnownSpeeds(results, speeds); kept != 1 {
		t.Fatalf("kept = %d, want 1", kept)
	}
	if results[0].SpeedBps != 3_000_000 {
		t.Errorf("存活代理应沿用旧测速值, got %d", results[0].SpeedBps)
	}
	if results[1].SpeedBps != 0 {
		t.Errorf("已死代理不该沿用旧测速值, got %d", results[1].SpeedBps)
	}
	if results[2].SpeedBps != 0 || results[3].SpeedBps != 0 {
		t.Errorf("没有旧值或旧值为 0 时不该改动: %d %d", results[2].SpeedBps, results[3].SpeedBps)
	}
}

func TestSpeedByAddrOnlyReturnsMeasured(t *testing.T) {
	srv := newTestServer(t)
	entries := []ProxyEntry{
		{Source: "s1", Addr: "measured:80", Alive: true, SpeedBps: 1_500_000, LastChecked: "now", LastAliveAt: "now"},
		{Source: "s1", Addr: "untested:80", Alive: true, SpeedBps: 0, LastChecked: "now", LastAliveAt: "now"},
	}
	if err := srv.proxies.replaceAll(entries); err != nil {
		t.Fatalf("replaceAll: %v", err)
	}
	speeds, err := srv.proxies.speedByAddr()
	if err != nil {
		t.Fatal(err)
	}
	if len(speeds) != 1 || speeds["measured:80"] != 1_500_000 {
		t.Fatalf("speedByAddr = %v, want 仅 measured:80", speeds)
	}
}

/*
走一遍刷新真正的落库路径：refresh 构造的记录不带测速值，replaceAll 又是清表重插，
所以必须靠 speedByAddr + applyKnownSpeeds 把上一轮的成果接过来。这条挂了就意味着
开着后台刷新时，页面上手动测速白测。
*/
func TestSpeedSurvivesRefreshReplace(t *testing.T) {
	srv := newTestServer(t)
	measured := []ProxyEntry{
		{Source: "s1", Addr: "keep:80", LatencyMs: 40, Alive: true, SpeedBps: 2_500_000, LastChecked: "t1", LastAliveAt: "t1"},
		{Source: "s1", Addr: "gone:80", LatencyMs: 60, Alive: true, SpeedBps: 900_000, LastChecked: "t1", LastAliveAt: "t1"},
	}
	if err := srv.proxies.replaceAll(measured); err != nil {
		t.Fatalf("首轮 replaceAll: %v", err)
	}

	// 模拟下一轮刷新：keep 仍然测通、gone 这轮没抓到、newcomer 是新来的，都不带测速值
	refreshed := []ProxyEntry{
		{Source: "s1", Addr: "keep:80", LatencyMs: 45, Alive: true, LastChecked: "t2", LastAliveAt: "t2"},
		{Source: "s2", Addr: "newcomer:80", LatencyMs: 20, Alive: true, LastChecked: "t2", LastAliveAt: "t2"},
	}
	speeds, err := srv.proxies.speedByAddr()
	if err != nil {
		t.Fatal(err)
	}
	applyKnownSpeeds(refreshed, speeds)
	if err := srv.proxies.replaceAll(refreshed); err != nil {
		t.Fatalf("二轮 replaceAll: %v", err)
	}

	got, err := srv.proxies.ListProxies(true, 100)
	if err != nil {
		t.Fatal(err)
	}
	byAddr := map[string]ProxyEntry{}
	for _, e := range got {
		byAddr[e.Addr] = e
	}
	if byAddr["keep:80"].SpeedBps != 2_500_000 {
		t.Errorf("keep:80 测速值应跨刷新保留, got %d", byAddr["keep:80"].SpeedBps)
	}
	if byAddr["keep:80"].LatencyMs != 45 {
		t.Errorf("延迟应更新为本轮实测值, got %d", byAddr["keep:80"].LatencyMs)
	}
	if byAddr["newcomer:80"].SpeedBps != 0 {
		t.Errorf("新代理没测过速就该是 0, got %d", byAddr["newcomer:80"].SpeedBps)
	}
	if _, still := byAddr["gone:80"]; still {
		t.Error("这轮没抓到的代理不该留在库里")
	}
	// 测速值带过来之后，排序结果也得跟着变：keep 有速度，newcomer 只有低延迟
	if addrs := srv.proxies.AvailableAddrs(10); len(addrs) != 2 || addrs[0] != "keep:80" {
		t.Errorf("有测速值的应排在前面, got %v", addrs)
	}
}
