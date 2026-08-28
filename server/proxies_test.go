/*
server/proxies_test.go
代理池数据层与解析逻辑测试
*/
package main

import (
	"context"
	"net/http"
	"testing"
)

// zdayeSample 站大爷（zdaye）风格的 JSON 源：ip 与 port 是分开字段，
// 混有 socks5 / http / socks4 三种协议。
const zdayeSample = `{
  "code": "10001",
  "msg": "获取成功。警告声明：免费代理仅供学习测试...",
  "data": {
    "count": 4,
    "proxy_list": [
      {"ip": "103.136.106.5", "port": 1081, "adr": "亚太地区", "protocol": "socks5", "level": "高匿"},
      {"ip": "147.45.221.112", "port": 8080, "adr": "俄罗斯", "protocol": "http", "level": "高匿"},
      {"ip": "176.53.182.170", "port": 1080, "adr": "俄罗斯", "protocol": "socks5", "level": "高匿"},
      {"ip": "1.2.3.4", "port": 9999, "adr": "测试", "protocol": "socks4", "level": "高匿"}
    ]
  }
}`

func TestParseProxyJSON_Zdaye(t *testing.T) {
	got := parseProxyLines(zdayeSample)
	want := []string{
		"socks5://103.136.106.5:1081", // socks5 必须带前缀，否则下游按 http 连必然失败
		"147.45.221.112:8080",         // http 保持裸地址，兼容库里历史数据
		"socks5://176.53.182.170:1080",
		// socks4 被丢弃：net/http 的 Proxy 不认它，留着只会白占测通配额
	}
	if len(got) != len(want) {
		t.Fatalf("解析 zdaye JSON = %v, want %v", got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("第 %d 条 = %q, want %q", i, got[i], want[i])
		}
	}
}

func TestParseProxyJSON_PortAsStringAndTopLevelArray(t *testing.T) {
	// port 写成字符串、以及顶层直接是数组，两种都要吃得下
	got := parseProxyLines(`[{"ip":"1.2.3.4","port":"3128","protocol":"http"},
	                          {"ip":"5.6.7.8","port":1080,"protocol":"socks5"}]`)
	if len(got) != 2 || got[0] != "1.2.3.4:3128" || got[1] != "socks5://5.6.7.8:1080" {
		t.Fatalf("解析 = %v", got)
	}
}

func TestParseProxyJSON_EmptyListNoRegexFallback(t *testing.T) {
	// 空 proxy_list 是「已按 JSON 处理但无可用条目」，不能回落正则去 JSON 文本里乱抓
	if got := parseProxyLines(`{"data":{"count":0,"proxy_list":[]}}`); len(got) != 0 {
		t.Fatalf("空列表应返回空，实际 %v", got)
	}
}

func TestParseProxyJSON_NonProxyJSONFallsBackToRegex(t *testing.T) {
	// 不含 proxy_list 的 JSON 回落正则；正则在这段纯文本里抓不到 ip:port
	if got := parseProxyLines(`{"code":"10001","msg":"no data"}`); len(got) != 0 {
		t.Fatalf("无 proxy_list 的 JSON 应解析为空，实际 %v", got)
	}
}

func TestParseProxyJSON_InvalidEntriesFiltered(t *testing.T) {
	got := parseProxyLines(`{"data":{"proxy_list":[
		{"ip":"999.1.1.1","port":80,"protocol":"http"},
		{"ip":"1.2.3.4","port":70000,"protocol":"http"},
		{"ip":"5.6.7.8","port":80,"protocol":"http"}]}}`)
	if len(got) != 1 || got[0] != "5.6.7.8:80" {
		t.Fatalf("过滤非法后 = %v, want 仅 5.6.7.8:80", got)
	}
}

func TestProxyFromRequest_SchemeHandling(t *testing.T) {
	cases := []struct {
		addr string
		want string
	}{
		{"1.2.3.4:8080", "http://1.2.3.4:8080"},            // 裸地址默认 http
		{"socks5://1.2.3.4:1080", "socks5://1.2.3.4:1080"}, // 已带 scheme 原样解析
	}
	for _, c := range cases {
		ctx := context.WithValue(context.Background(), ctxKeyProxyAddr{}, c.addr)
		req := &http.Request{}
		u, err := proxyFromRequest(req.WithContext(ctx))
		if err != nil {
			t.Fatalf("addr %q: 解析出错 %v", c.addr, err)
		}
		if u == nil || u.String() != c.want {
			t.Fatalf("addr %q -> %v, want %q", c.addr, u, c.want)
		}
	}
}

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

/*
刷新时把老池并进候选：免费源列表天天变，昨天好用的出口今天可能压根不在源里。
整表替换会让这种代理直接消失——即使它还通着。并进来一起过测通，能通就留、不通淘汰，
池子于是收敛在「持续可用的那批」而不是「今天恰好被源收录的那批」。
*/
func TestMergeExistingCandidates(t *testing.T) {
	fresh := []proxyCandidate{
		{"9.9.9.9:80", "src-new"},
		{"8.8.8.8:80", "src-new"},
	}

	t.Run("老池不在新源里也要保留并排在前面", func(t *testing.T) {
		existing := []ProxyEntry{
			{Addr: "1.1.1.1:80", Source: "src-old"},
			{Addr: "2.2.2.2:80", Source: "src-old"},
		}
		got := mergeExistingCandidates(fresh, existing)
		if len(got) != 4 {
			t.Fatalf("合并后 = %d 条, want 4: %+v", len(got), got)
		}
		// 前面必须是老的：saveLimit 提前停时先给已验证过的机会
		if got[0].addr != "1.1.1.1:80" || got[1].addr != "2.2.2.2:80" {
			t.Fatalf("老池没排在前面: %+v", got)
		}
		if got[2].addr != "9.9.9.9:80" || got[3].addr != "8.8.8.8:80" {
			t.Fatalf("新源候选顺序被打乱: %+v", got)
		}
	})

	t.Run("老池保留自己的来源标记", func(t *testing.T) {
		got := mergeExistingCandidates(fresh, []ProxyEntry{{Addr: "1.1.1.1:80", Source: "src-old"}})
		if got[0].source != "src-old" {
			t.Fatalf("来源被改成 %q, want src-old", got[0].source)
		}
	})

	t.Run("新源里已有的不重复排队", func(t *testing.T) {
		existing := []ProxyEntry{
			{Addr: "9.9.9.9:80", Source: "src-old"}, // 和 fresh 撞了
			{Addr: "1.1.1.1:80", Source: "src-old"},
		}
		got := mergeExistingCandidates(fresh, existing)
		if len(got) != 3 {
			t.Fatalf("合并后 = %d 条, want 3（撞的那条只测一次）: %+v", len(got), got)
		}
		seen := map[string]int{}
		for _, c := range got {
			seen[c.addr]++
		}
		if seen["9.9.9.9:80"] != 1 {
			t.Fatalf("9.9.9.9:80 出现 %d 次, want 1", seen["9.9.9.9:80"])
		}
		// 撞的那条按新来源记账，所以它仍留在 fresh 的位置上
		if got[1].addr != "9.9.9.9:80" || got[1].source != "src-new" {
			t.Fatalf("撞库那条应保留新来源: %+v", got)
		}
	})

	t.Run("老池内部重复也去掉", func(t *testing.T) {
		existing := []ProxyEntry{
			{Addr: "1.1.1.1:80", Source: "a"},
			{Addr: "1.1.1.1:80", Source: "b"},
		}
		got := mergeExistingCandidates(nil, existing)
		if len(got) != 1 || got[0].source != "a" {
			t.Fatalf("老池去重失败: %+v", got)
		}
	})

	t.Run("空地址被跳过", func(t *testing.T) {
		got := mergeExistingCandidates(nil, []ProxyEntry{{Addr: "", Source: "x"}})
		if len(got) != 0 {
			t.Fatalf("空地址不该进候选: %+v", got)
		}
	})

	t.Run("老池为空时原样返回", func(t *testing.T) {
		got := mergeExistingCandidates(fresh, nil)
		if len(got) != 2 || got[0].addr != "9.9.9.9:80" {
			t.Fatalf("老池为空时不该改动 fresh: %+v", got)
		}
	})
}

// 只有存活的老代理才参与复测：已经判死的没必要再占测通配额和时间
func TestRefreshOnlyRecheckesAliveProxies(t *testing.T) {
	srv := newTestServer(t)
	if err := srv.proxies.replaceAll([]ProxyEntry{
		{Source: "s", Addr: "1.1.1.1:80", Alive: true, LastChecked: "now", LastAliveAt: "now"},
		{Source: "s", Addr: "2.2.2.2:80", Alive: false, LastChecked: "now"},
	}); err != nil {
		t.Fatalf("replaceAll: %v", err)
	}
	alive, err := srv.proxies.queryProxies(true, 0)
	if err != nil {
		t.Fatal(err)
	}
	got := mergeExistingCandidates(nil, alive)
	if len(got) != 1 || got[0].addr != "1.1.1.1:80" {
		t.Fatalf("只该复测存活的那条, got %+v", got)
	}
}

// 测速地址取配置，空值回落默认端点
func TestSpeedTestURLOf(t *testing.T) {
	if got := speedTestURLOf(ProxyPool{}); got != DefaultSpeedTestURL {
		t.Fatalf("空配置应回落默认, got %q", got)
	}
	if got := speedTestURLOf(ProxyPool{SpeedTestURL: "   "}); got != DefaultSpeedTestURL {
		t.Fatalf("空白配置应回落默认, got %q", got)
	}
	custom := "https://agentrouter.org/bigfile"
	if got := speedTestURLOf(ProxyPool{SpeedTestURL: custom}); got != custom {
		t.Fatalf("自定义地址应生效, got %q", got)
	}
	if got := speedTestURLOf(ProxyPool{SpeedTestURL: "  " + custom + "  "}); got != custom {
		t.Fatalf("首尾空白应被去掉, got %q", got)
	}
}
