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
