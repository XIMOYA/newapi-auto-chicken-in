/*
server/proxy_vless.go
单条 vless:// 分享链接的解析。

订阅解析（proxy_subscription.go）只负责把一整包 base64 拆成 URI 列表，这里负责
读懂其中一条：拆出接入身份（uuid / host / port）和传输参数。

设计上刻意只把「身份必需」的三样具名，传输参数一律保留在 Params 里原样带着。
理由是分享链接的参数集合由各家客户端事实约定，随传输方式还在扩（reality 的
pbk/sid、ws 的 path/host、grpc 的 serviceName...）。把它们逐个具名会制造一种
「已经支持完整」的错觉 —— 漏掉一个就是静默丢配置，节点起来了却连不上。
留在 Params 里，等生成 xray outbound 时按当次真正需要的字段去取，缺什么当场看得见。

Raw 保留原始 URI：它就是 proxies 表里的 addr，也是 proxy_feedback 的主键，
必须能原样回到那两处（见 proxy_display.go 开头对 addr 四重身份的说明）。
*/
package main

import (
	"fmt"
	"net/url"
	"strings"
)

// vlessNode 一条 VLESS 节点的解析结果。
type vlessNode struct {
	// Raw 原始 URI，等同库里的 addr
	Raw string
	// UUID 接入凭据。它是密码，任何出站展示都必须先过 proxyDisplay
	UUID string
	Host string
	Port string
	// Tag 机场给的节点备注（URI 的 fragment），认节点主要靠它
	Tag string
	// Params 全量 query 原文，不做筛选也不做归一
	Params url.Values
}

// Network 传输方式（type 参数）。缺省是 tcp —— 分享链接省略 type 时各家客户端
// 都按 tcp 处理。
func (n vlessNode) Network() string {
	if v := strings.TrimSpace(n.Params.Get("type")); v != "" {
		return strings.ToLower(v)
	}
	return "tcp"
}

// Security 传输层安全（security 参数）。缺省是 none。
// 注意别把它和 encryption 搞混：VLESS 的 encryption 恒为 none（协议本身不加密，
// 靠传输层），security 才是 tls / reality 那一档。
func (n vlessNode) Security() string {
	if v := strings.TrimSpace(n.Params.Get("security")); v != "" {
		return strings.ToLower(v)
	}
	return "none"
}

// ServerName 握手用的域名：优先 sni，其次 ws 的 host 参数，最后回落节点 host。
// 回落到 host 是有意的 —— 纯 tls 节点常常不写 sni，此时 SNI 就该是连接地址本身。
func (n vlessNode) ServerName() string {
	for _, key := range []string{"sni", "host"} {
		if v := strings.TrimSpace(n.Params.Get(key)); v != "" {
			return v
		}
	}
	return n.Host
}

// parseVlessURI 解析一条 vless:// 链接。
//
// 只做解析不做能力判断：某种传输方式将来支不支持，是生成 xray 配置那一层的事。
// 这里失败只有三种情况 —— 不是 vless、URI 本身坏了、缺了接入必需的三样。
func parseVlessURI(uri string) (vlessNode, error) {
	trimmed := strings.TrimSpace(uri)
	if !strings.HasPrefix(strings.ToLower(trimmed), "vless://") {
		return vlessNode{}, fmt.Errorf("不是 vless 链接")
	}
	u, err := url.Parse(trimmed)
	if err != nil {
		return vlessNode{}, fmt.Errorf("链接无法解析: %w", err)
	}
	if u.User == nil || strings.TrimSpace(u.User.Username()) == "" {
		return vlessNode{}, fmt.Errorf("缺少 uuid")
	}
	host, port := u.Hostname(), u.Port()
	if host == "" {
		return vlessNode{}, fmt.Errorf("缺少主机")
	}
	if port == "" {
		return vlessNode{}, fmt.Errorf("缺少端口")
	}
	return vlessNode{
		Raw:    trimmed,
		UUID:   strings.TrimSpace(u.User.Username()),
		Host:   host,
		Port:   port,
		Tag:    u.Fragment, // url.Parse 已经解过码，中文备注在这里是可读的
		Params: u.Query(),
	}, nil
}
