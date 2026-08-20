/*
server/proxy_feedback_test.go
代理反馈的累加、排序分档与端点边界

这批断言守的是优选的因果链：Actions 回传的成败要能累加进库、脏数据要被挡在门外、
排序要让「实测能成」的代理压过「服务器自测很快」的代理。链子断在哪一环，优选都会
退回原来那套跟 Actions 无关的延迟/测速排序。
*/
package main

import (
	"net/http"
	"testing"
)

func aliveEntry(addr string, latency int, speed int64) ProxyEntry {
	return ProxyEntry{
		Source: "s1", Addr: addr, LatencyMs: latency, Alive: true,
		SpeedBps: speed, LastChecked: "now", LastAliveAt: "now",
	}
}

func TestRecordProxyFeedbackAccumulates(t *testing.T) {
	srv := newTestServer(t)
	m := srv.proxies

	if _, _, err := m.RecordProxyFeedback([]ProxyFeedbackItem{
		{Addr: "1.1.1.1:80", OK: 1},
		{Addr: "2.2.2.2:80", NetFail: 1},
	}); err != nil {
		t.Fatalf("首次上报: %v", err)
	}
	// 第二次上报必须叠加而不是覆盖
	if _, _, err := m.RecordProxyFeedback([]ProxyFeedbackItem{
		{Addr: "1.1.1.1:80", OK: 2, BlockFail: 1},
	}); err != nil {
		t.Fatalf("二次上报: %v", err)
	}
	fb, err := m.FeedbackByAddr()
	if err != nil {
		t.Fatal(err)
	}
	got := fb["1.1.1.1:80"]
	if got.OK != 3 || got.BlockFail != 1 || got.NetFail != 0 {
		t.Fatalf("累加结果 = %+v, want ok=3 block=1 net=0", got)
	}
	if got.LastOKAt == "" || got.LastFailAt == "" {
		t.Errorf("成败时间都该有值: %+v", got)
	}
	if fb["2.2.2.2:80"].NetFail != 1 {
		t.Errorf("另一个代理的计数被串了: %+v", fb["2.2.2.2:80"])
	}
}

// 纯失败的上报不该把「上次成功是什么时候」冲掉
func TestFeedbackKeepsLastOKAcrossFailures(t *testing.T) {
	srv := newTestServer(t)
	m := srv.proxies
	if _, _, err := m.RecordProxyFeedback([]ProxyFeedbackItem{{Addr: "a:80", OK: 1}}); err != nil {
		t.Fatal(err)
	}
	fb, _ := m.FeedbackByAddr()
	firstOKAt := fb["a:80"].LastOKAt
	if firstOKAt == "" {
		t.Fatal("首次成功没记时间")
	}
	if _, _, err := m.RecordProxyFeedback([]ProxyFeedbackItem{{Addr: "a:80", NetFail: 2}}); err != nil {
		t.Fatal(err)
	}
	fb, _ = m.FeedbackByAddr()
	if fb["a:80"].LastOKAt != firstOKAt {
		t.Errorf("last_ok_at 被纯失败的上报改掉了: %q -> %q", firstOKAt, fb["a:80"].LastOKAt)
	}
	if fb["a:80"].LastFailAt == "" {
		t.Error("失败时间该被记上")
	}
}

func TestRecordProxyFeedbackRejectsGarbage(t *testing.T) {
	srv := newTestServer(t)
	accepted, skipped, err := srv.proxies.RecordProxyFeedback([]ProxyFeedbackItem{
		{Addr: "", OK: 1},                    // 空地址
		{Addr: "no-port", OK: 1},             // 缺端口
		{Addr: "a:80:90", OK: 1},             // 多个冒号
		{Addr: "has space:80", OK: 1},        // 带空白
		{Addr: "neg:80", OK: -5},             // 负数
		{Addr: "empty:80"},                   // 三项全 0，没有信息量
		{Addr: "good:80", OK: 1, NetFail: 1}, // 唯一合法的
	})
	if err != nil {
		t.Fatal(err)
	}
	if accepted != 1 || skipped != 6 {
		t.Fatalf("accepted=%d skipped=%d, want 1 / 6", accepted, skipped)
	}
	fb, _ := srv.proxies.FeedbackByAddr()
	if len(fb) != 1 || fb["good:80"].OK != 1 {
		t.Fatalf("只该收下 good:80, got %+v", fb)
	}
}

// 同一次上报里同一个地址出现多次，应先合并再落库
func TestRecordProxyFeedbackMergesDuplicates(t *testing.T) {
	srv := newTestServer(t)
	if _, _, err := srv.proxies.RecordProxyFeedback([]ProxyFeedbackItem{
		{Addr: "dup:80", OK: 1},
		{Addr: "dup:80", NetFail: 1},
		{Addr: "dup:80", OK: 2},
	}); err != nil {
		t.Fatal(err)
	}
	fb, _ := srv.proxies.FeedbackByAddr()
	if fb["dup:80"].OK != 3 || fb["dup:80"].NetFail != 1 {
		t.Fatalf("合并结果 = %+v, want ok=3 net=1", fb["dup:80"])
	}
}

func TestRankOf(t *testing.T) {
	cases := []struct {
		name  string
		fb    ProxyFeedback
		known bool
		want  proxyRank
	}{
		{"没有任何记录", ProxyFeedback{}, false, rankUnknown},
		{"有行但计数全 0", ProxyFeedback{}, true, rankUnknown},
		{"成过且没失败", ProxyFeedback{OK: 3}, true, rankProven},
		{"成过失败没过半", ProxyFeedback{OK: 3, NetFail: 1}, true, rankProven},
		{"成败各一半算过关", ProxyFeedback{OK: 2, NetFail: 2}, true, rankProven},
		{"失败过半", ProxyFeedback{OK: 1, NetFail: 3}, true, rankFlaky},
		{"只失败一次不判死", ProxyFeedback{NetFail: 1}, true, rankFlaky},
		{"失败两次且从没成过", ProxyFeedback{NetFail: 2}, true, rankBroken},
		{"被拦也算失败", ProxyFeedback{BlockFail: 3}, true, rankBroken},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := rankOf(tc.fb, tc.known); got != tc.want {
				t.Fatalf("rankOf = %d, want %d", got, tc.want)
			}
		})
	}
}

/*
排序的核心断言：实测能成的代理要压过服务器自测更快的代理。

fast 在服务器视角是最优的（5MB/s），但 Actions 用它两次都连不上；slow 只有 100KB/s，
可实测成过 3 次。真实分配顺序必须是 slow 在前 —— 服务器测的是自己到代理的链路，
Actions runner 在 Azure，两边可达性本来就不是一回事。
*/
func TestSortProxiesByFeedbackPrefersProven(t *testing.T) {
	entries := []ProxyEntry{
		aliveEntry("fast:80", 20, 5_000_000),
		aliveEntry("slow:80", 300, 100_000),
		aliveEntry("fresh:80", 50, 800_000),
	}
	fb := map[string]ProxyFeedback{
		"fast:80": {Addr: "fast:80", NetFail: 2},
		"slow:80": {Addr: "slow:80", OK: 3},
		// fresh:80 故意没有反馈，应该落在 proven 之后、broken 之前
	}
	sortProxiesByFeedback(entries, fb)
	got := []string{entries[0].Addr, entries[1].Addr, entries[2].Addr}
	want := []string{"slow:80", "fresh:80", "fast:80"}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("排序 = %v, want %v", got, want)
		}
	}
}

// 同档内没有反馈可比时，仍旧回落到原来的测速优先、延迟其次
func TestSortProxiesByFeedbackFallsBackToSpeed(t *testing.T) {
	entries := []ProxyEntry{
		aliveEntry("b:80", 10, 1_000_000),
		aliveEntry("a:80", 10, 3_000_000),
		aliveEntry("c:80", 5, 1_000_000),
	}
	sortProxiesByFeedback(entries, map[string]ProxyFeedback{})
	if entries[0].Addr != "a:80" {
		t.Fatalf("测速最高的应在首位, got %s", entries[0].Addr)
	}
	if entries[1].Addr != "c:80" {
		t.Fatalf("同测速时延迟低的在前, got %s", entries[1].Addr)
	}
}

// 已死代理永远排在存活之后，反馈再好也不能翻上来
func TestSortProxiesByFeedbackKeepsDeadLast(t *testing.T) {
	dead := ProxyEntry{Source: "s", Addr: "dead:80", LatencyMs: 1, Alive: false, LastChecked: "now"}
	entries := []ProxyEntry{dead, aliveEntry("live:80", 999, 0)}
	sortProxiesByFeedback(entries, map[string]ProxyFeedback{
		"dead:80": {Addr: "dead:80", OK: 99},
	})
	if entries[0].Addr != "live:80" {
		t.Fatalf("存活代理必须在前, got %s", entries[0].Addr)
	}
}

// AvailableAddrs 是客户端真正取的那条路，反馈必须在这里生效
func TestAvailableAddrsHonorsFeedback(t *testing.T) {
	srv := newTestServer(t)
	if err := srv.proxies.replaceAll([]ProxyEntry{
		aliveEntry("fast:80", 20, 5_000_000),
		aliveEntry("proven:80", 400, 50_000),
	}); err != nil {
		t.Fatal(err)
	}
	// 没有反馈时按测速排：fast 在前
	if addrs := srv.proxies.AvailableAddrs(10); addrs[0] != "fast:80" {
		t.Fatalf("无反馈时应测速优先, got %v", addrs)
	}
	if _, _, err := srv.proxies.RecordProxyFeedback([]ProxyFeedbackItem{
		{Addr: "proven:80", OK: 4},
		{Addr: "fast:80", NetFail: 3},
	}); err != nil {
		t.Fatal(err)
	}
	addrs := srv.proxies.AvailableAddrs(10)
	if len(addrs) != 2 || addrs[0] != "proven:80" {
		t.Fatalf("有反馈后实测能成的该排前面, got %v", addrs)
	}
}

// limit 必须在精排之后才截断，否则砍掉的正是实测最稳的那些
func TestListProxiesRanksBeforeTruncating(t *testing.T) {
	srv := newTestServer(t)
	entries := []ProxyEntry{
		aliveEntry("s1:80", 10, 9_000_000),
		aliveEntry("s2:80", 20, 8_000_000),
		aliveEntry("winner:80", 900, 1_000),
	}
	if err := srv.proxies.replaceAll(entries); err != nil {
		t.Fatal(err)
	}
	if _, _, err := srv.proxies.RecordProxyFeedback([]ProxyFeedbackItem{
		{Addr: "winner:80", OK: 5},
	}); err != nil {
		t.Fatal(err)
	}
	got, err := srv.proxies.ListProxies(true, 1)
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 1 || got[0].Addr != "winner:80" {
		t.Fatalf("limit=1 应留下实测最稳的那条, got %+v", got)
	}
}

func TestProxyFeedbackEndpointAuthAndValidation(t *testing.T) {
	srv := newTestServer(t)
	jwt := loginToken(t, srv)
	key := apiKeyToken(t, srv, jwt)
	body := map[string]any{
		"source": "github-actions",
		"items":  []map[string]any{{"addr": "1.2.3.4:8080", "ok": 1}},
	}

	// 客户端专用通道：网页端的 JWT 调不通，和 run-state 上报一个待遇
	if rr := doReq(t, srv, http.MethodPost, "/api/proxies/feedback", jwt, body); rr.Code != http.StatusUnauthorized {
		t.Fatalf("JWT 上报 status = %d, want 401", rr.Code)
	}
	if rr := doReq(t, srv, http.MethodPost, "/api/proxies/feedback", "", body); rr.Code != http.StatusUnauthorized {
		t.Fatalf("无凭据 status = %d, want 401", rr.Code)
	}

	rr := doReq(t, srv, http.MethodPost, "/api/proxies/feedback", key, body)
	if rr.Code != http.StatusOK {
		t.Fatalf("API Key 上报 status = %d, want 200: %s", rr.Code, rr.Body.String())
	}
	fb, _ := srv.proxies.FeedbackByAddr()
	if fb["1.2.3.4:8080"].OK != 1 {
		t.Fatalf("上报没落库: %+v", fb)
	}

	// items 缺失或为空没有意义，直接 400，别让客户端以为写成功了
	if rr := doReq(t, srv, http.MethodPost, "/api/proxies/feedback", key,
		map[string]any{"source": "x", "items": []any{}}); rr.Code != http.StatusBadRequest {
		t.Fatalf("空 items status = %d, want 400", rr.Code)
	}

	oversized := make([]map[string]any, maxFeedbackItems+1)
	for i := range oversized {
		oversized[i] = map[string]any{"addr": "1.1.1.1:80", "ok": 1}
	}
	if rr := doReq(t, srv, http.MethodPost, "/api/proxies/feedback", key,
		map[string]any{"items": oversized}); rr.Code != http.StatusBadRequest {
		t.Fatalf("超限 status = %d, want 400", rr.Code)
	}
}

func TestPruneProxyFeedback(t *testing.T) {
	srv := newTestServer(t)
	m := srv.proxies
	if _, _, err := m.RecordProxyFeedback([]ProxyFeedbackItem{{Addr: "old:80", OK: 1}}); err != nil {
		t.Fatal(err)
	}
	// 手工把 updated_at 推到很久以前，模拟长期没再出现的代理
	if _, err := m.db.Exec(`UPDATE proxy_feedback SET updated_at = '2000-01-01T00:00:00Z' WHERE addr = 'old:80'`); err != nil {
		t.Fatal(err)
	}
	if _, _, err := m.RecordProxyFeedback([]ProxyFeedbackItem{{Addr: "new:80", OK: 1}}); err != nil {
		t.Fatal(err)
	}
	n, err := m.PruneProxyFeedback(feedbackRetentionDays)
	if err != nil {
		t.Fatal(err)
	}
	if n != 1 {
		t.Fatalf("清理行数 = %d, want 1", n)
	}
	fb, _ := m.FeedbackByAddr()
	if _, still := fb["old:80"]; still {
		t.Error("过期行没被清掉")
	}
	if _, ok := fb["new:80"]; !ok {
		t.Error("新行被误删")
	}
	// days <= 0 是配置写错的情形，绝不能顺手把全表清空
	if n, err := m.PruneProxyFeedback(0); err != nil || n != 0 {
		t.Fatalf("days=0 应该什么都不做, got n=%d err=%v", n, err)
	}
	if fb, _ := m.FeedbackByAddr(); len(fb) != 1 {
		t.Error("days=0 时表被动了")
	}
}
