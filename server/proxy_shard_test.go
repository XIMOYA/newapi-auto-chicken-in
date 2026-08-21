/*
server/proxy_shard_test.go
按分片轮转发牌：Actions 每 30 个账号一个 job 时，各 job 拿到的代理不能重叠

重叠的后果不是报错而是静默降质：几个 job 都从优选列表头部取，最优的那几个出口 IP 同时
承载数倍账号，等于把「一个账号一个 IP」这条设计悄悄作废。所以分片的不重叠、不丢项、
质量均摊这三件事都要有断言盯着。
*/
package main

import (
	"net/http"
	"strings"
	"testing"
)

func TestParseShardParam(t *testing.T) {
	cases := []struct {
		raw        string
		index, tot int
		wantErr    bool
	}{
		{"", 0, 0, false}, // 不传 = 不分片
		{"1/3", 1, 3, false},
		{"3/3", 3, 3, false},
		{" 2 / 4 ", 2, 4, false}, // 两侧空白容忍
		{"1/1", 1, 1, false},
		{"0/3", 0, 0, true}, // 序号从 1 起
		{"4/3", 0, 0, true}, // 越界
		{"-1/3", 0, 0, true},
		{"1/0", 0, 0, true},
		{"1/-2", 0, 0, true},
		{"abc", 0, 0, true},
		{"1", 0, 0, true}, // 缺分母
		{"1/", 0, 0, true},
		{"/3", 0, 0, true},
		{"1/2/3", 0, 0, true},
	}
	for _, tc := range cases {
		t.Run(tc.raw, func(t *testing.T) {
			index, total, err := parseShardParam(tc.raw)
			if tc.wantErr {
				if err == nil {
					t.Fatalf("应报错，却拿到 index=%d total=%d", index, total)
				}
				return
			}
			if err != nil {
				t.Fatalf("不该报错: %v", err)
			}
			if index != tc.index || total != tc.tot {
				t.Fatalf("= (%d, %d), want (%d, %d)", index, total, tc.index, tc.tot)
			}
		})
	}
}

func TestShardAddrsRoundRobin(t *testing.T) {
	addrs := []string{"a", "b", "c", "d", "e", "f", "g"}

	if got := shardAddrs(addrs, 1, 3); strings.Join(got, ",") != "a,d,g" {
		t.Errorf("第 1 片 = %v, want a,d,g", got)
	}
	if got := shardAddrs(addrs, 2, 3); strings.Join(got, ",") != "b,e" {
		t.Errorf("第 2 片 = %v, want b,e", got)
	}
	if got := shardAddrs(addrs, 3, 3); strings.Join(got, ",") != "c,f" {
		t.Errorf("第 3 片 = %v, want c,f", got)
	}
}

// 三件事一起验：不重叠、不丢项、每片都能分到头部的优质代理
func TestShardAddrsCoversEverythingWithoutOverlap(t *testing.T) {
	addrs := make([]string, 100)
	for i := range addrs {
		addrs[i] = string(rune('A'+i%26)) + string(rune('0'+i/26))
	}
	const total = 4
	seen := map[string]int{}
	for index := 1; index <= total; index++ {
		part := shardAddrs(addrs, index, total)
		for _, a := range part {
			seen[a]++
		}
		// 每片的第一个都来自原列表前 total 名 —— 说明优选没被某一片吃独食
		if len(part) > 0 && part[0] != addrs[index-1] {
			t.Errorf("第 %d 片的首个应是原列表第 %d 个, got %s", index, index, part[0])
		}
	}
	if len(seen) != len(addrs) {
		t.Fatalf("覆盖了 %d 个地址, want %d", len(seen), len(addrs))
	}
	for addr, n := range seen {
		if n != 1 {
			t.Fatalf("地址 %s 出现在 %d 个分片里，应当只属于一片", addr, n)
		}
	}
}

func TestShardAddrsDegenerateCases(t *testing.T) {
	addrs := []string{"a", "b", "c"}
	// total <= 1 或参数越界时原样返回：宁可不分片，也不要返回空列表让客户端没代理可用
	if got := shardAddrs(addrs, 1, 1); len(got) != 3 {
		t.Errorf("total=1 应原样返回, got %v", got)
	}
	if got := shardAddrs(addrs, 1, 0); len(got) != 3 {
		t.Errorf("total=0 应原样返回, got %v", got)
	}
	if got := shardAddrs(addrs, 5, 3); len(got) != 3 {
		t.Errorf("序号越界应原样返回, got %v", got)
	}
	// 分片数多于地址数：靠后的分片拿到空列表是正常的
	if got := shardAddrs(addrs, 4, 5); len(got) != 0 {
		t.Errorf("第 4 片应为空, got %v", got)
	}
	if got := shardAddrs(nil, 1, 3); len(got) != 0 {
		t.Errorf("空输入应得空输出, got %v", got)
	}
}

// HTTP 层：分片参数要真的作用到 /available 的返回上，写错要当场 400
func TestAvailableProxiesShardEndpoint(t *testing.T) {
	srv := newTestServer(t)
	jwt := loginToken(t, srv)
	key := apiKeyToken(t, srv, jwt)

	entries := make([]ProxyEntry, 0, 9)
	for i := 1; i <= 9; i++ {
		// 延迟递增，保证落库后的优选顺序稳定可预期
		entries = append(entries, aliveEntry(string(rune('a'+i-1))+":80", i*10, 0))
	}
	if err := srv.proxies.replaceAll(entries); err != nil {
		t.Fatal(err)
	}

	all := availableAddrs(t, srv, key, "")
	if len(all) != 9 {
		t.Fatalf("不分片应返回 9 条, got %v", all)
	}

	first := availableAddrs(t, srv, key, "?shard=1/3")
	second := availableAddrs(t, srv, key, "?shard=2/3")
	third := availableAddrs(t, srv, key, "?shard=3/3")
	if len(first) != 3 || len(second) != 3 || len(third) != 3 {
		t.Fatalf("三片各应 3 条, got %d/%d/%d", len(first), len(second), len(third))
	}
	union := map[string]int{}
	for _, part := range [][]string{first, second, third} {
		for _, a := range part {
			union[a]++
		}
	}
	if len(union) != 9 {
		t.Fatalf("三片合起来应覆盖全部 9 条, got %d", len(union))
	}
	for a, n := range union {
		if n != 1 {
			t.Fatalf("%s 落在 %d 个分片里", a, n)
		}
	}
	// 每片的头名都该来自全局前三，否则说明切的是连续块而不是轮转
	if first[0] != all[0] || second[0] != all[1] || third[0] != all[2] {
		t.Errorf("轮转顺序不对: %s / %s / %s（全局前三 %v）",
			first[0], second[0], third[0], all[:3])
	}

	for _, bad := range []string{"?shard=0/3", "?shard=4/3", "?shard=abc", "?shard=1", "?shard=1/0"} {
		rr := doReq(t, srv, http.MethodGet, "/api/proxies/available"+bad, key, nil)
		if rr.Code != http.StatusBadRequest {
			t.Errorf("%s 应返回 400, got %d: %s", bad, rr.Code, rr.Body.String())
		}
	}
}

func availableAddrs(t *testing.T, srv *Server, key, query string) []string {
	t.Helper()
	rr := doReq(t, srv, http.MethodGet, "/api/proxies/available"+query, key, nil)
	if rr.Code != http.StatusOK {
		t.Fatalf("GET available%s = %d: %s", query, rr.Code, rr.Body.String())
	}
	var body struct {
		Proxies []string `json:"proxies"`
		Count   int      `json:"count"`
	}
	decodeJSON(t, rr, &body)
	if body.Count != len(body.Proxies) {
		t.Fatalf("count=%d 与 proxies 长度 %d 不一致", body.Count, len(body.Proxies))
	}
	return body.Proxies
}

/*
?source=xxx 按来源过滤。

注释一直宣称支持这个参数，代码里却没读过它 —— 前端只好把全部代理拉回来自己筛，
而列表默认只给 500 条，来源分布不均时某个源明明有几百条可用，页面上却只剩零星几条。
过滤要在截断之前做：limit 的语义是「要几条」，不是「从前几条里挑」。
*/
func TestListProxiesSourceFilter(t *testing.T) {
	srv := newTestServer(t)
	jwt := loginToken(t, srv)

	entries := []ProxyEntry{
		{Source: "s1", Addr: "a:80", LatencyMs: 10, Alive: true, LastChecked: "now", LastAliveAt: "now"},
		{Source: "s2", Addr: "b:80", LatencyMs: 20, Alive: true, LastChecked: "now", LastAliveAt: "now"},
		{Source: "s1", Addr: "c:80", LatencyMs: 30, Alive: true, LastChecked: "now", LastAliveAt: "now"},
		{Source: "s2", Addr: "d:80", LatencyMs: 40, Alive: false, LastChecked: "now"},
	}
	if err := srv.proxies.replaceAll(entries); err != nil {
		t.Fatal(err)
	}

	all := listProxies(t, srv, jwt, "")
	if len(all) != 4 {
		t.Fatalf("不过滤应返回 4 条, got %d", len(all))
	}

	s1 := listProxies(t, srv, jwt, "?source=s1")
	if len(s1) != 2 {
		t.Fatalf("source=s1 应返回 2 条, got %d", len(s1))
	}
	for _, e := range s1 {
		if e.Source != "s1" {
			t.Errorf("混进了别的来源: %+v", e)
		}
	}

	// 与 alive=1 叠加
	aliveS2 := listProxies(t, srv, jwt, "?source=s2&alive=1")
	if len(aliveS2) != 1 || aliveS2[0].Addr != "b:80" {
		t.Fatalf("source+alive 叠加结果不对: %+v", aliveS2)
	}

	// 过滤在截断之前：limit=1 时该从筛完的结果里取第一条，而不是先砍到 1 条再筛
	limited := listProxies(t, srv, jwt, "?source=s1&limit=1")
	if len(limited) != 1 || limited[0].Source != "s1" {
		t.Fatalf("limit 应作用在过滤后的结果上: %+v", limited)
	}

	if got := listProxies(t, srv, jwt, "?source=不存在的源"); len(got) != 0 {
		t.Fatalf("没有匹配时应返回空, got %+v", got)
	}
	// 空串等于不过滤，别把它当成「来源名是空字符串」
	if got := listProxies(t, srv, jwt, "?source="); len(got) != 4 {
		t.Fatalf("source 为空应视为不过滤, got %d", len(got))
	}
}

func listProxies(t *testing.T, srv *Server, jwt, query string) []ProxyEntry {
	t.Helper()
	rr := doReq(t, srv, http.MethodGet, "/api/proxies"+query, jwt, nil)
	if rr.Code != http.StatusOK {
		t.Fatalf("GET /api/proxies%s = %d: %s", query, rr.Code, rr.Body.String())
	}
	var body struct {
		Proxies []ProxyEntry `json:"proxies"`
		Total   int          `json:"total"`
	}
	decodeJSON(t, rr, &body)
	if body.Total != len(body.Proxies) {
		t.Fatalf("total=%d 与 proxies 长度 %d 不一致", body.Total, len(body.Proxies))
	}
	return body.Proxies
}
