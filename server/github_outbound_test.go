/*
server/github_outbound_test.go
出口绑定的分配、粘住、释放逻辑测试。

守的核心是「粘住」：已绑定且仍可用时**必须原样返回且不报告变更**。
换出口对 GitHub 是一次「这条 session 换了 IP」的信号，只有原出口真的不可用了才值得付。
*/
package main

import "testing"

const (
	nodeA = "vless://11111111-1111-4000-8000-000000000000@a.example.com:443#节点A"
	nodeB = "vless://22222222-2222-4000-8000-000000000000@b.example.com:443#节点B"
	nodeC = "1.2.3.4:8080"
)

func TestPickGitHubOutboundAvoidsSharing(t *testing.T) {
	candidates := []string{nodeA, nodeB, nodeC}

	// 优先挑没被占的：同一出口挂多个账号等于告诉 GitHub「这几个是同一个人」
	if got := pickGitHubOutbound(candidates, map[string]bool{nodeA: true}); got != nodeB {
		t.Errorf("应跳过已占用的 nodeA，实际 %q", got)
	}
	// 全被占：退回第一条去共用。共用总比没出口好，没出口就只能直连
	all := map[string]bool{nodeA: true, nodeB: true, nodeC: true}
	if got := pickGitHubOutbound(candidates, all); got != nodeA {
		t.Errorf("全占用时应退回第一条，实际 %q", got)
	}
	// 空白项要跳过，别把空串当成一个出口分出去
	if got := pickGitHubOutbound([]string{"", "   ", nodeC}, nil); got != nodeC {
		t.Errorf("应跳过空白项，实际 %q", got)
	}
	if got := pickGitHubOutbound(nil, nil); got != "" {
		t.Errorf("空清单应返回空，实际 %q", got)
	}
}

func TestTakenGitHubOutboundsExcludesSelf(t *testing.T) {
	cfg := &Config{GitHubAccounts: []GitHubAccount{
		{Name: "Steven", ProxyAddr: nodeA},
		{Name: "Alice", ProxyAddr: nodeB},
		{Name: "没绑的"},
	}}
	taken := takenGitHubOutbounds(cfg, "Steven")
	if taken[nodeA] {
		t.Error("自己占的那条不该算进 taken，否则每次都会被判成冲突而换绑")
	}
	if !taken[nodeB] {
		t.Error("别人占的应算进 taken")
	}
	if len(taken) != 1 {
		t.Errorf("taken 应只有 1 条，实际 %v", taken)
	}
}

func TestEnsureGitHubOutboundSticksToBinding(t *testing.T) {
	cfg := &Config{GitHubAccounts: []GitHubAccount{
		{Name: "Steven", ProxyAddr: nodeA},
		{Name: "Alice"},
	}}
	alive := []string{nodeA, nodeB, nodeC}

	// 已绑定且仍在可用清单里 → 原样返回、不报变更。这就是「粘住」
	addr, changed := ensureGitHubOutbound(cfg, "Steven", alive)
	if addr != nodeA || changed {
		t.Fatalf("应粘住已有绑定，实际 addr=%q changed=%v", addr, changed)
	}
	// 反复调用同样不许动
	for i := 0; i < 3; i++ {
		if addr, changed = ensureGitHubOutbound(cfg, "Steven", alive); addr != nodeA || changed {
			t.Fatalf("重复调用动了绑定: addr=%q changed=%v", addr, changed)
		}
	}

	// 没绑过的账号 → 首次分配，且避开 Steven 已占的 nodeA
	addr, changed = ensureGitHubOutbound(cfg, "Alice", alive)
	if !changed || addr == "" {
		t.Fatalf("应给 Alice 分配出口，实际 addr=%q changed=%v", addr, changed)
	}
	if addr == nodeA {
		t.Error("不该分到 Steven 已占的出口")
	}
	if got := findGitHubAccount(cfg, "Alice").ProxyAddr; got != addr {
		t.Errorf("分配结果没落到配置上: %q vs %q", got, addr)
	}
}

func TestEnsureGitHubOutboundSwapsOnlyWhenDead(t *testing.T) {
	cfg := &Config{GitHubAccounts: []GitHubAccount{{Name: "Steven", ProxyAddr: nodeA}}}

	// 绑的那条不在可用清单里了 → 换绑
	addr, changed := ensureGitHubOutbound(cfg, "Steven", []string{nodeB, nodeC})
	if !changed || addr != nodeB {
		t.Fatalf("原出口失效时应换绑到 nodeB，实际 addr=%q changed=%v", addr, changed)
	}

	// 可用清单为空（代理池可能正在刷新）→ 保持现状，绝不清成直连
	addr, changed = ensureGitHubOutbound(cfg, "Steven", nil)
	if changed || addr != nodeB {
		t.Fatalf("清单为空时应保持现状，实际 addr=%q changed=%v", addr, changed)
	}

	// 账号不存在：不报错也不产生副作用
	if addr, changed = ensureGitHubOutbound(cfg, "不存在", []string{nodeC}); addr != "" || changed {
		t.Errorf("未知账号应返回空，实际 addr=%q changed=%v", addr, changed)
	}
}

func TestGithubOutboundProxyOnlyUsableProtocols(t *testing.T) {
	// http/socks5 能直接交给 net/http；vless 要先在本地起 xray，那一层还没接。
	// 硬用会让每次签发都在一个必然失败的地址上超时，比直连糟得多
	usable := map[string]string{
		"1.2.3.4:8080":          "1.2.3.4:8080",
		"http://1.2.3.4:3128":   "http://1.2.3.4:3128",
		"socks5://1.2.3.4:1080": "socks5://1.2.3.4:1080",
	}
	for addr, want := range usable {
		got, ok := githubOutboundProxy(addr)
		if !ok || got != want {
			t.Errorf("githubOutboundProxy(%q) = %q,%v，want %q,true", addr, got, ok, want)
		}
	}
	for _, addr := range []string{"", "   ", nodeA, "trojan://x@a.com:443"} {
		if got, ok := githubOutboundProxy(addr); ok {
			t.Errorf("%q 现在还用不了，应回落直连，实际给了 %q", addr, got)
		}
	}
}

func TestIsOutboundFailure(t *testing.T) {
	// 只有出口的问题才值得解绑：凭据失效、关闭注册换出口都没用，
	// 解了只会白白让这个账号换 IP，而换 IP 本身就是风控信号
	outbound := []string{
		"OAuth state 网络错误: timeout",
		"GitHub authorize 网络错误: connection refused",
		"取 OAuth state 失败: HTTP 502",
		"GitHub authorize HTTP 429，当前出口被 GitHub 限制，稍后再试",
		"dial tcp: no such host",
	}
	for _, msg := range outbound {
		if !isOutboundFailure(msg) {
			t.Errorf("应判为出口问题: %q", msg)
		}
	}
	notOutbound := []string{
		"GitHub 要求重新登录，user_session 已失效",
		"OAuth 回调失败: 管理员关闭了新用户注册",
		"站点状态未返回 github_client_id，请在账号里手动填写",
		"OAuth 回调成功但站点未下发 new_api_refresh（HTTP 200）",
	}
	for _, msg := range notOutbound {
		if isOutboundFailure(msg) {
			t.Errorf("不该判为出口问题（换出口解决不了）: %q", msg)
		}
	}
}

func TestReleaseGitHubOutboundClearsBinding(t *testing.T) {
	cfg := &Config{GitHubAccounts: []GitHubAccount{
		{Name: "Steven", ProxyAddr: nodeA},
		{Name: "没绑的"},
	}}
	if !releaseGitHubOutbound(cfg, "Steven", "签发时连不上") {
		t.Fatal("应解除绑定")
	}
	if got := findGitHubAccount(cfg, "Steven").ProxyAddr; got != "" {
		t.Errorf("绑定没被清掉: %q", got)
	}
	// 本来就没绑、或账号不存在：返回 false，调用方据此判断要不要落库
	if releaseGitHubOutbound(cfg, "没绑的", "x") {
		t.Error("没绑定时应返回 false")
	}
	if releaseGitHubOutbound(cfg, "不存在", "x") {
		t.Error("未知账号应返回 false")
	}
}

func TestMergeBoundOutboundsPutsBoundFirst(t *testing.T) {
	fresh := []proxyCandidate{{addr: "1.1.1.1:80", source: "s1"}, {addr: nodeB, source: "s2"}}
	merged := mergeBoundOutbounds(fresh, []string{nodeA, nodeC, nodeB})
	if len(merged) != 4 {
		t.Fatalf("应合并出 4 条（nodeB 已在 fresh 里去重），实际 %d: %+v", len(merged), merged)
	}
	if merged[0].addr != nodeA || merged[1].addr != nodeC {
		t.Fatalf("绑定的出口应排最前，实际 %+v", merged)
	}
	if merged[0].source != "github-binding" {
		t.Errorf("绑定的出口应标记来源 github-binding，实际 %q", merged[0].source)
	}
	if merged[2].addr != "1.1.1.1:80" {
		t.Errorf("fresh 顺序应保持，实际 %+v", merged)
	}
	// 空绑定原样返回
	if out := mergeBoundOutbounds(fresh, nil); len(out) != len(fresh) {
		t.Errorf("空绑定不该改动候选")
	}
}

func TestBoundOutboundsDedupAndReadsConfig(t *testing.T) {
	srv := newTestServer(t)
	seedPool(t, srv, []GitHubAccount{
		{Name: "A", ProxyAddr: nodeA},
		{Name: "B", ProxyAddr: nodeA}, // 两个账号绑同一出口：去重
		{Name: "C", ProxyAddr: nodeC},
		{Name: "D"}, // 没绑
	}, nil)
	got, err := srv.proxies.boundOutbounds()
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 2 || got[0] != nodeA || got[1] != nodeC {
		t.Fatalf("boundOutbounds = %v", got)
	}
}
