/*
server/proxy_subscription.go
机场订阅解析：把订阅响应体变成节点列表。

形态（按 KIQ 确认的来源）：GET 订阅 URL 拿回一整块 base64，解开是每行一条
vless:// 。两个必须容错的现实细节：
  - 编码变体不统一：有的机场用 URL-safe 字符集（- _ 代替 + /），很多还省掉 =
    填充。只用 base64.StdEncoding 会直接报错，等于整个订阅解析出 0 条。
  - 有的机场按 76 字符折行，也有的压根不编码直接给明文 vless:// 行。

判定顺序上订阅放在最前面是安全的：base64 的字符集里没有 '.' 和 ':'，而免费代理
列表恰恰靠这两个字符（ip:port）被 ipPortRe 抓出来 —— 两种形态不会互相误判。
反过来 "1.2.3.4:8080" 这样的明文也不可能被 base64 解码成功。

只认 vless。将来要加 vmess/trojan 得在 supportedNodeSchemes 里扩，但每加一种都要
先确认 xray outbound 那边生成也跟上：解析出来却起不了进程，等于把死节点混进池子，
比不支持更糟。
*/
package main

import (
	"encoding/base64"
	"net/url"
	"strings"
	"unicode/utf8"
)

// supportedNodeSchemes 当前能真正用起来的节点协议（小写，带 "://"）。
var supportedNodeSchemes = []string{"vless://"}

// decodeMaybeBase64 尽力把整块 base64 解开；不像 base64 就返回 ok=false。
//
// 空白全部剔掉再解 —— 折行的订阅体里换行不属于编码内容。四种变体都试是因为
// 「有无填充」和「标准/URL-safe」是两个独立的维度，机场四种组合都见得到。
func decodeMaybeBase64(s string) (string, bool) {
	compact := strings.Map(func(r rune) rune {
		switch r {
		case '\n', '\r', '\t', ' ':
			return -1
		}
		return r
	}, s)
	if compact == "" {
		return "", false
	}
	for _, enc := range []*base64.Encoding{
		base64.StdEncoding, base64.RawStdEncoding,
		base64.URLEncoding, base64.RawURLEncoding,
	} {
		raw, err := enc.DecodeString(compact)
		// 必须是合法 UTF-8：解码「碰巧成功」但结果是二进制垃圾的情况真实存在，
		// 那种东西按行切出来只会污染池子
		if err == nil && utf8.Valid(raw) {
			return string(raw), true
		}
	}
	return "", false
}

// validNodeURI 判断一条节点链接能不能用。
//
// 三个硬条件：协议在支持列表里、userinfo 段有 uuid、有主机和端口。缺 uuid 的条目
// 连不上却会占掉测活配额和池子名额，不如当场丢掉。
func validNodeURI(uri string) bool {
	trimmed := strings.TrimSpace(uri)
	lower := strings.ToLower(trimmed)
	supported := false
	for _, scheme := range supportedNodeSchemes {
		if strings.HasPrefix(lower, scheme) {
			supported = true
			break
		}
	}
	if !supported {
		return false
	}
	u, err := url.Parse(trimmed)
	if err != nil {
		return false
	}
	if u.User == nil || u.User.Username() == "" {
		return false
	}
	return u.Hostname() != "" && u.Port() != ""
}

// parseSubscriptionNodes 从明文里逐行取出可用节点，顺序保留、就地去重。
//
// 保留顺序的理由与免费源一致：机场普遍把推荐/低倍率的节点排在前面，用 map 迭代
// 会把这个顺序打乱，提前停时拿到的就是随机子集。
func parseSubscriptionNodes(text string) []string {
	out := []string{}
	seen := make(map[string]bool)
	for _, line := range strings.Split(text, "\n") {
		node := strings.TrimSpace(line)
		if node == "" || !validNodeURI(node) || seen[node] {
			continue
		}
		seen[node] = true
		out = append(out, node)
	}
	return out
}

// parseSubscription 从订阅响应体取出节点列表；不是订阅格式时返回 nil，
// 让调用方回落到原有的 JSON / 正则解析。
//
// 先试明文再试 base64：明文分支代价极低，而且有机场确实不编码。
func parseSubscription(body string) []string {
	if nodes := parseSubscriptionNodes(body); len(nodes) > 0 {
		return nodes
	}
	if decoded, ok := decodeMaybeBase64(body); ok {
		if nodes := parseSubscriptionNodes(decoded); len(nodes) > 0 {
			return nodes
		}
	}
	return nil
}
