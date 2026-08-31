/*
server/github_binding.go
GitHub 账号与出口节点的绑定：分配、粘住、失败才换。

为什么要粘住一个出口：GitHub 的 user_session 绑设备特征，其中「从哪个 IP 出现」
权重很高。同一条 session 今天在日本、明天在美国，是触发风控最快的方式之一，
最坏直接作废 session。所以一个账号认一个出口，只有这个出口真的不可用了才换 ——
换出口本身是有代价的动作，不是负载均衡。

与代理池既有的 acquire 语义正好相反：那边是「每次签到尽量分散、坏了就换」，
因为签到面对的是站点风控（同 IP 多账号会被拦）；这边是「一条 session 一个出口、
能不换就不换」，因为面对的是 GitHub 的会话风控。两种诉求不能共用一套分配器。
*/
package main

import (
	"database/sql"
	"log"
	"strings"
)

/*
boundOutbounds 读出当前被 GitHub 账号绑定的全部出口地址（去重、保序）。

刷新代理池时要拿它把绑定的出口并进候选，否则「粘住一个出口」根本立不住：
判定出口可用的依据是它在不在可用清单里，而清单来自每轮刷新的测通结果 ——
不复测就永远回不了清单，绑定于是只能活一轮。
*/
func (m *ProxyManager) boundOutbounds() ([]string, error) {
	cfg, _, err := LoadConfig(m.db)
	if err != nil {
		return nil, err
	}
	seen := make(map[string]bool, len(cfg.GitHubAccounts))
	out := make([]string, 0, len(cfg.GitHubAccounts))
	for _, g := range cfg.GitHubAccounts {
		addr := strings.TrimSpace(g.ProxyAddr)
		if addr == "" || seen[addr] {
			continue
		}
		seen[addr] = true
		out = append(out, addr)
	}
	return out, nil
}

/*
mergeBoundOutbounds 把绑定的出口并进候选清单，排在最前面。

与 mergeExistingCandidates 的区别：那个只捞**存活**的老代理，这个连上一轮判死的
也要捞回来 —— 判死可能只是那一轮网络抖了一下，而代价不对称：漏捞会让一个
GitHub 账号无谓地换出口（换 IP 是风控信号），多测一条只是多花一次测活配额。

排在最前面是因为测通可能「达标提前停」，绑定的出口必须优先拿到测活机会。
*/
func mergeBoundOutbounds(fresh []proxyCandidate, bound []string) []proxyCandidate {
	if len(bound) == 0 {
		return fresh
	}
	seen := make(map[string]bool, len(fresh))
	for _, c := range fresh {
		seen[c.addr] = true
	}
	head := make([]proxyCandidate, 0, len(bound))
	for _, addr := range bound {
		if addr == "" || seen[addr] {
			continue
		}
		seen[addr] = true
		head = append(head, proxyCandidate{addr, "github-binding"})
	}
	if len(head) == 0 {
		return fresh
	}
	return append(head, fresh...)
}

/*
persistGitHubOutbound 把绑定变更落库（定点写，不整份覆盖）。

与 updateAccountCookie 共用 configWriteMu：定点写和整份写必须互斥，否则整份写的
陈旧快照会把这里刚落的绑定抹回去。

按名字重新定位而不是复用调用方那份 cfg：签发可能持续几十秒，期间池子可能已经被
改过，拿旧快照整份写回会覆盖掉别人的改动。
*/
func persistGitHubOutbound(db *sql.DB, name, addr string) error {
	configWriteMu.Lock()
	defer configWriteMu.Unlock()

	cfg, _, err := loadConfigLocked(db)
	if err != nil {
		return err
	}
	ref := findGitHubAccount(&cfg, name)
	if ref == nil {
		return nil // 账号已被删，没什么可写的
	}
	if ref.ProxyAddr == addr {
		return nil
	}
	ref.ProxyAddr = addr
	_, err = saveConfigLocked(db, cfg)
	return err
}

/*
prepareGitHubOutbound 为一次签发/探测准备出口：确保有绑定、落库、返回能用的代理地址。

第二个返回值是给 http 客户端用的代理地址，空串表示这次走直连 —— 要么池子没有可用
节点，要么绑的是 VLESS 这类还需要本地起 xray 才能用的协议。直连不是失败：签发链路
原本就是平台 IP 直连，退回去只是回到改造前的行为。
*/
func (s *Server) prepareGitHubOutbound(cfg *Config, poolName string) (bound, proxy string) {
	if strings.TrimSpace(poolName) == "" {
		return "", ""
	}
	addr, changed := ensureGitHubOutbound(cfg, poolName, s.proxies.AvailableAddrs(0))
	if changed {
		if err := persistGitHubOutbound(s.db, poolName, addr); err != nil {
			// 落库失败不该中断签发：这一轮先用着，下一轮会重新分配
			log.Printf("[github-accounts] 账号 %q 的出口绑定落库失败（本轮仍使用）: %v",
				poolName, err)
		}
	}
	usable, ok := githubOutboundProxy(addr)
	if addr != "" && !ok {
		log.Printf("[github-accounts] 账号 %q 绑定的出口 %s 暂不可直接使用"+
			"（需要本地起 xray），本次走直连", poolName, proxyDisplay(addr))
	}
	if !ok {
		return addr, ""
	}
	return addr, usable
}

// releaseAndPersistGitHubOutbound 解绑并落库。用于「实际签发失败」之后。
func (s *Server) releaseAndPersistGitHubOutbound(cfg *Config, poolName, reason string) {
	if !releaseGitHubOutbound(cfg, poolName, reason) {
		return
	}
	if err := persistGitHubOutbound(s.db, poolName, ""); err != nil {
		log.Printf("[github-accounts] 账号 %q 解绑落库失败: %v", poolName, err)
	}
}

/*
isOutboundFailure 判断一条签发失败是不是「出口的问题」。

只有出口的问题才值得解绑换一个 —— 凭据失效、站点关闭注册、client_id 缺失换出口
都没用，解了只会白白让这个账号换 IP（换 IP 本身就是风控信号）。

判据是网络层与限流特征。宁可漏判：漏判只是这个出口多用一轮，误判会让好出口被换掉。
*/
func isOutboundFailure(message string) bool {
	lower := strings.ToLower(message)
	for _, marker := range []string{
		"网络错误", "timeout", "超时", "connection", "eof", "refused",
		"no such host", "tls", "proxy", "被 github 限制", "429", "502", "503", "504",
	} {
		if strings.Contains(lower, strings.ToLower(marker)) {
			return true
		}
	}
	return false
}

// pickGitHubOutbound 为一个账号挑一个出口。
//
// candidates 是按优选顺序排好的可用地址（AvailableAddrs 的输出），taken 是已经被
// 别的账号占用的地址集合。
//
// 优先挑没被占用的：同一个出口挂多个 GitHub 账号，等于告诉 GitHub「这几个账号是
// 同一台机器上的同一个人」，一个被风控另一个大概率连坐。候选不够时才允许共用 ——
// 共用总比没有出口好，没出口就只能直连，那是最糟的选项。
func pickGitHubOutbound(candidates []string, taken map[string]bool) string {
	if len(candidates) == 0 {
		return ""
	}
	for _, addr := range candidates {
		if addr = strings.TrimSpace(addr); addr != "" && !taken[addr] {
			return addr
		}
	}
	// 全被占了：退回优选列表里的第一条，让它去共用
	for _, addr := range candidates {
		if addr = strings.TrimSpace(addr); addr != "" {
			return addr
		}
	}
	return ""
}

// takenGitHubOutbounds 统计当前已被占用的出口，可选排除某个账号自己。
func takenGitHubOutbounds(cfg *Config, exceptName string) map[string]bool {
	taken := make(map[string]bool, len(cfg.GitHubAccounts))
	for _, g := range cfg.GitHubAccounts {
		if strings.TrimSpace(g.Name) == exceptName {
			continue
		}
		if addr := strings.TrimSpace(g.ProxyAddr); addr != "" {
			taken[addr] = true
		}
	}
	return taken
}

// githubOutboundProxy 把绑定的出口变成 Go HTTP 客户端能直接用的代理地址。
//
// 第二个返回值为 false 表示这条出口现在用不了，调用方应回落直连而不是硬用：
// VLESS 之类的节点必须先在本地起 xray 转成 socks5 才能给 net/http 用，那一层还没接。
// 硬用的后果是每次签发都在一个必然失败的地址上超时，比直连糟得多。
func githubOutboundProxy(addr string) (string, bool) {
	trimmed := strings.TrimSpace(addr)
	if trimmed == "" {
		return "", false
	}
	switch proxyProtocolOf(trimmed) {
	case "http", "socks5":
		return trimmed, true
	default:
		return "", false
	}
}

// ensureGitHubOutbound 保证账号有一个可用的出口绑定，返回该用的地址。
//
// 只在两种情况下动绑定：压根还没绑过、或者绑的那个已经不在可用清单里了。
// 已绑定且仍可用时原样返回 —— 这就是「粘住」，也是这个函数存在的全部理由。
//
// alive 是当前可用地址清单（含存活判定与优选排序）。清单为空时返回已有绑定：
// 代理池可能只是刚好在刷新，把绑定清掉换成直连反而更危险。
func ensureGitHubOutbound(cfg *Config, name string, alive []string) (addr string, changed bool) {
	ref := findGitHubAccount(cfg, name)
	if ref == nil {
		return "", false
	}
	current := strings.TrimSpace(ref.ProxyAddr)
	if len(alive) == 0 {
		return current, false
	}
	if current != "" {
		for _, candidate := range alive {
			if strings.TrimSpace(candidate) == current {
				return current, false // 还活着，继续用
			}
		}
	}
	picked := pickGitHubOutbound(alive, takenGitHubOutbounds(cfg, name))
	if picked == "" || picked == current {
		return current, false
	}
	ref.ProxyAddr = picked
	if current == "" {
		log.Printf("[github-accounts] 账号 %q 分配固定出口 %s", name, proxyDisplay(picked))
	} else {
		// 换绑要留痕：GitHub 那边看到的就是这条 session 换了 IP，
		// 排查「为什么突然要重新登录」时这行日志是关键线索
		log.Printf("[github-accounts] 账号 %q 的出口 %s 已不可用，换到 %s",
			name, proxyDisplay(current), proxyDisplay(picked))
	}
	return picked, true
}

// releaseGitHubOutbound 解除绑定，让下次签发重新分配。
//
// 调用时机是「这个出口在实际签发/探测里失败了」，而不是「测活没测通」——
// 测活失败可能只是测速站点抖了一下，为此换掉一个 GitHub 账号的固定出口不值得。
func releaseGitHubOutbound(cfg *Config, name, reason string) bool {
	ref := findGitHubAccount(cfg, name)
	if ref == nil || strings.TrimSpace(ref.ProxyAddr) == "" {
		return false
	}
	log.Printf("[github-accounts] 账号 %q 解除出口 %s 的绑定：%s",
		name, proxyDisplay(ref.ProxyAddr), reason)
	ref.ProxyAddr = ""
	return true
}
