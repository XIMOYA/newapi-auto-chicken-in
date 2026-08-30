/*
server/proxy_display.go
代理地址的展示脱敏与稳定指纹。

起因：VLESS 节点的地址是一整条 URI，形如

	vless://<uuid>@host:443?type=ws&security=tls#香港01

那个 uuid 是接入凭据，等于密码。而 addr 现在同时担着四个身份 —— 去重键、
proxy_feedback 主键、下发给客户端的值、界面显示值。前三个都必须是完整 URI 才成立
（去重要能区分「同 host:port 不同 uuid」的两个节点，反馈要对得上号，客户端要拿它
直接起 xray），只有第四个是问题所在。所以 addr 一个字节不动，只在出站那一层换成
脱敏形态，并给每条配一个稳定指纹当操作键。

指纹位数刻意与 account_query.go 的 cookie 指纹一致：界面上两种指纹长度相同，
运维扫一眼不会看错，也省得再造一个常量。
*/
package main

import (
	"crypto/sha256"
	"encoding/hex"
	"log"
	"net/url"
	"strings"
)

// proxyDisplay 把代理地址转成可以安全显示的形态。
//
// 只抹 userinfo 段 —— 凭据都在那里：VLESS 的 uuid、带认证 http 代理的 user:pass。
// reality 的 pbk 是公钥、sid 不是秘密，留在 query 里也无所谓，但 query 又长又对
// 「认出这是哪个节点」没帮助，所以展示时一并省掉。
//
// 没有 userinfo 的地址（裸 host:port、socks5://host:port）原样返回：它们本来就
// 没有凭据，改动只会让界面上的地址跟库里对不上，反而妨碍排查。
func proxyDisplay(addr string) string {
	trimmed := strings.TrimSpace(addr)
	if trimmed == "" {
		return ""
	}
	if i := strings.Index(trimmed, "://"); i <= 0 {
		return trimmed
	}
	u, err := url.Parse(trimmed)
	// 解析不了就原样给出：这种地址本来也不该在库里，把它显示出来才好排查，
	// 抹成 *** 只会让人不知道坏在哪
	if err != nil || u.Host == "" || u.User == nil {
		return trimmed
	}
	out := u.Scheme + "://***@" + u.Host
	// fragment 是机场给的节点备注（「香港01 倍率1.0」这种），是认节点的主要线索
	if u.Fragment != "" {
		out += "#" + u.Fragment
	}
	return out
}

// proxyFingerprint 代理地址的稳定短指纹，供界面在拿不到明文时指定操作目标。
//
// 必须由**完整** addr 算出，不能拿脱敏后的结果算：同一机场里
// vless://uuidA@host:443#香港01 和 vless://uuidB@host:443#香港01 脱敏后长得一模一样，
// 指纹要是也一样，界面上就没法区分，测速/删除会打到错误的那一条。
func proxyFingerprint(addr string) string {
	if addr == "" {
		return ""
	}
	sum := sha256.Sum256([]byte(addr))
	return hex.EncodeToString(sum[:])[:cookieFingerprintLength]
}

// proxyView 出站展示用的代理条目。
//
// JSON 键与 ProxyEntry 保持一字不差，只是 addr 换成脱敏形态、另外多带两个字段 ——
// 前端不认识新字段会自动忽略，老调用方行为不变。库里绝大多数是没有 userinfo 的
// http/socks5 代理，它们的 addr 原样输出，所以这层对存量数据是完全透明的。
type proxyView struct {
	ID     int64  `json:"id"`
	Source string `json:"source"`
	// Addr 脱敏后的地址。要完整值请走 GET /api/proxies/available（只认 API Key）
	Addr string `json:"addr"`
	// Protocol 协议名，供界面分类与筛选
	Protocol string `json:"protocol"`
	// Fingerprint 完整地址的短指纹：界面拿不到明文时用它指定操作目标
	Fingerprint string `json:"fingerprint"`
	LatencyMs   int    `json:"latency_ms"`
	Alive       bool   `json:"alive"`
	LastChecked string `json:"last_checked_at"`
	LastAliveAt string `json:"last_alive_at,omitempty"`
	SpeedBps    int64  `json:"speed_bps"`
}

// proxyViewOf 把库里的一条转成出站形态。
func proxyViewOf(e ProxyEntry) proxyView {
	return proxyView{
		ID:          e.ID,
		Source:      e.Source,
		Addr:        proxyDisplay(e.Addr),
		Protocol:    proxyProtocolOf(e.Addr),
		Fingerprint: proxyFingerprint(e.Addr),
		LatencyMs:   e.LatencyMs,
		Alive:       e.Alive,
		LastChecked: e.LastChecked,
		LastAliveAt: e.LastAliveAt,
		SpeedBps:    e.SpeedBps,
	}
}

// proxyViewsOf 批量转换。返回非 nil 切片，空列表在 JSON 里是 [] 而不是 null ——
// 前端少一个判空分支。
func proxyViewsOf(entries []ProxyEntry) []proxyView {
	out := make([]proxyView, 0, len(entries))
	for _, e := range entries {
		out = append(out, proxyViewOf(e))
	}
	return out
}

/*
resolveProxyRefs 把界面传来的「地址或指纹」列表还原成库里的真实地址。

为什么要兼容两种形态：GET /api/proxies 出站时带凭据的 addr 被脱敏了，界面手里只有
指纹；而存量 http/socks5 代理的 addr 从没被改过，老前端仍然直接传地址。先按地址
精确匹配、匹配不上再按指纹查，两种调用方都不用改。

认不出的原样往下传，不在这里丢掉 —— 下游本来就会忽略库里没有的地址，而丢了会让
响应里的「测了几条」对不上用户选的条数，看着像点了没反应。
*/
func (m *ProxyManager) resolveProxyRefs(refs []string) []string {
	if len(refs) == 0 {
		return refs
	}
	entries, err := m.queryProxies(false, 0)
	if err != nil {
		// 读不出库就整批原样放过：至少存量地址那条路还能正常工作
		log.Printf("[proxy] 解析测速目标时读库失败，按原样处理: %v", err)
		return refs
	}
	known := make(map[string]bool, len(entries))
	byFingerprint := make(map[string]string, len(entries))
	for _, e := range entries {
		known[e.Addr] = true
		byFingerprint[proxyFingerprint(e.Addr)] = e.Addr
	}

	out := make([]string, 0, len(refs))
	for _, ref := range refs {
		trimmed := strings.TrimSpace(ref)
		if trimmed == "" {
			continue
		}
		if known[trimmed] {
			out = append(out, trimmed)
			continue
		}
		if addr, ok := byFingerprint[trimmed]; ok {
			out = append(out, addr)
			continue
		}
		out = append(out, trimmed)
	}
	return out
}

// proxyProtocolOf 从地址推出协议名，用于分类展示与筛选。
//
// 裸 host:port 归为 http：池子里绝大多数是 http 代理，parseProxyLines 也一直
// 按这个约定产出（proxies.go:145-146），这里保持同一口径。
func proxyProtocolOf(addr string) string {
	trimmed := strings.TrimSpace(addr)
	if trimmed == "" {
		return ""
	}
	i := strings.Index(trimmed, "://")
	if i <= 0 {
		return "http"
	}
	return strings.ToLower(trimmed[:i])
}
