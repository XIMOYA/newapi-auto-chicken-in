/*
server/xray_config.go
xray 配置生成：把一批 VLESS 节点变成「N 个本地 socks 入站 + N 个 vless 出站 + 路由」。

一个 xray 进程扛全部节点，每个节点分一个本地端口 —— 这是唯一可行的量级方案：
测活并发能到几十上百条，一条一个进程起不来。

结构按 npm 上 vless-to-xray 的实现核对过（它做的是同一件事），不是凭记忆写：

	inbounds[]      {listen, port, protocol, tag}
	outbounds[]     {protocol:"vless", settings.vnext[{address,port,users[{id,encryption,flow}]}],
	                 streamSettings{network, security, realitySettings{...}}, tag}
	routing.rules[] {type:"field", inboundTag:[入站tag], outboundTag:出站tag}

入站用 socks 而不是 http：产出的地址形态是 socks5://127.0.0.1:port，与项目里
proxyProtocolOf 的既有口径一致（裸地址算 http、socks5 显式带前缀）。

只支持 security 为 reality / tls 的节点。ws / grpc 传输的确切字段名没有可靠依据，
猜着写会生成能编译、单测也绿、但真连不上的配置 —— 那是最难查的坏。遇到就明确拒绝
并说清原因，等有真实节点样例再补。
*/
package main

import (
	"encoding/json"
	"fmt"
	"strconv"
	"strings"
)

// xrayLocalHost 本地入站监听地址。只听回环 —— 这些端口是明文 socks，
// 听 0.0.0.0 等于把代理白送给同网段任何人。
const xrayLocalHost = "127.0.0.1"

// xraySupportedSecurity 目前能正确生成配置的传输安全类型。
var xraySupportedSecurity = map[string]bool{"reality": true, "tls": true}

// xrayInbound 一个本地 socks 入站。
type xrayInbound struct {
	Listen   string `json:"listen"`
	Port     int    `json:"port"`
	Protocol string `json:"protocol"`
	Tag      string `json:"tag"`
}

// xrayVlessUser vnext 里的用户。encryption 恒为 none —— VLESS 协议本身不加密，
// 靠传输层（tls/reality），写别的值 xray 会直接拒绝启动。
type xrayVlessUser struct {
	ID         string `json:"id"`
	Encryption string `json:"encryption"`
	Flow       string `json:"flow,omitempty"`
}

// xrayVnext 出站的目标服务器。
type xrayVnext struct {
	Address string          `json:"address"`
	Port    int             `json:"port"`
	Users   []xrayVlessUser `json:"users"`
}

// xrayRealitySettings reality 的握手参数。
// publicKey/shortId 来自节点的 pbk/sid，它们是公开值不是凭据。
type xrayRealitySettings struct {
	Fingerprint string `json:"fingerprint"`
	ServerName  string `json:"serverName"`
	PublicKey   string `json:"publicKey"`
	ShortID     string `json:"shortId"`
	SpiderX     string `json:"spiderX,omitempty"`
}

// xrayTLSSettings 纯 tls 的握手参数。
type xrayTLSSettings struct {
	ServerName  string   `json:"serverName"`
	Fingerprint string   `json:"fingerprint,omitempty"`
	ALPN        []string `json:"alpn,omitempty"`
}

// xrayStreamSettings 传输层设置。两个 settings 字段互斥，按 security 二选一。
type xrayStreamSettings struct {
	Network         string               `json:"network"`
	Security        string               `json:"security"`
	RealitySettings *xrayRealitySettings `json:"realitySettings,omitempty"`
	TLSSettings     *xrayTLSSettings     `json:"tlsSettings,omitempty"`
}

// xrayOutbound 一个 vless 出站。
type xrayOutbound struct {
	Protocol       string             `json:"protocol"`
	Settings       xrayOutboundInner  `json:"settings"`
	StreamSettings xrayStreamSettings `json:"streamSettings"`
	Tag            string             `json:"tag"`
}

type xrayOutboundInner struct {
	Vnext []xrayVnext `json:"vnext"`
}

// xrayRoutingRule 把某个入站的流量固定送给某个出站。
// 这是「一个进程多节点」能成立的关键：靠 tag 一对一绑定。
type xrayRoutingRule struct {
	Type        string   `json:"type"`
	InboundTag  []string `json:"inboundTag"`
	OutboundTag string   `json:"outboundTag"`
}

type xrayRouting struct {
	Rules []xrayRoutingRule `json:"rules"`
}

type xrayLog struct {
	LogLevel string `json:"loglevel"`
}

// XrayConfig 一份完整的 xray 配置。
type XrayConfig struct {
	Log       xrayLog        `json:"log"`
	Inbounds  []xrayInbound  `json:"inbounds"`
	Outbounds []xrayOutbound `json:"outbounds"`
	Routing   xrayRouting    `json:"routing"`
}

// XrayBinding 一个节点与它的本地入口的对应关系。
//
// NodeAddr 是节点的原始 vless:// URI —— 它同时是 proxies 表的 addr、
// proxy_feedback 的主键，全链路认它。LocalProxy 是给 HTTP 客户端用的本地地址。
// 这两个键必须分开：测活/反馈按 NodeAddr 记账，实际连接走 LocalProxy，
// 混用会让反馈全部记到 127.0.0.1 上，优选排序当场失效。
type XrayBinding struct {
	NodeAddr   string `json:"node_addr"`
	LocalProxy string `json:"local_proxy"`
	InboundTag string `json:"inbound_tag"`
}

/*
xrayNodeSupported 判断一个节点能不能生成有效配置。

不支持时返回原因 —— 调用方要把它记进日志/结果，用户才知道为什么某个节点没被用上。
静默跳过会让人以为节点数对不上是 bug。
*/
func xrayNodeSupported(node vlessNode) (bool, string) {
	security := node.Security()
	if !xraySupportedSecurity[security] {
		return false, fmt.Sprintf("security=%q 暂不支持（只支持 reality / tls）", security)
	}
	// 传输方式只放 tcp：ws/grpc/xhttp 的字段名没有可靠依据，
	// 猜着生成会得到「能启动但连不上」的配置
	if network := node.Network(); network != "tcp" {
		return false, fmt.Sprintf("传输方式 type=%q 暂不支持（只支持 tcp）", network)
	}
	if node.Params.Get("pbk") == "" && security == "reality" {
		return false, "reality 节点缺少 pbk（publicKey），无法握手"
	}
	return true, ""
}

// xrayTagFor 生成入站/出站的 tag。用序号而不是节点备注：
// 备注里可能有空格、引号、emoji，做 tag 要额外转义；序号稳定又好对账。
func xrayTagFor(prefix string, index int) string {
	return prefix + "-" + strconv.Itoa(index)
}

/*
BuildXrayConfig 把一批节点变成一份 xray 配置与节点→本地入口的映射。

startPort 是第一个入站端口，依次递增。不支持的节点不进配置，原因收在 skipped 里。
全部节点都不支持时返回空配置（Inbounds 为空）而不是报错 —— 调用方据此决定
「压根不用起 xray」，这比抛错好处理。
*/
func BuildXrayConfig(nodes []vlessNode, startPort int) (XrayConfig, []XrayBinding, []string) {
	cfg := XrayConfig{
		Log:       xrayLog{LogLevel: "warning"},
		Inbounds:  []xrayInbound{},
		Outbounds: []xrayOutbound{},
		Routing:   xrayRouting{Rules: []xrayRoutingRule{}},
	}
	bindings := make([]XrayBinding, 0, len(nodes))
	var skipped []string

	port := startPort
	for _, node := range nodes {
		if ok, why := xrayNodeSupported(node); !ok {
			// 原因里带上脱敏后的节点标识，不能带 uuid
			skipped = append(skipped, proxyDisplay(node.Raw)+": "+why)
			continue
		}
		index := len(bindings)
		inTag := xrayTagFor("in", index)
		outTag := xrayTagFor("out", index)

		cfg.Inbounds = append(cfg.Inbounds, xrayInbound{
			Listen:   xrayLocalHost,
			Port:     port,
			Protocol: "socks",
			Tag:      inTag,
		})
		cfg.Outbounds = append(cfg.Outbounds, buildXrayOutbound(node, outTag))
		cfg.Routing.Rules = append(cfg.Routing.Rules, xrayRoutingRule{
			Type:        "field",
			InboundTag:  []string{inTag},
			OutboundTag: outTag,
		})
		bindings = append(bindings, XrayBinding{
			NodeAddr:   node.Raw,
			LocalProxy: "socks5://" + xrayLocalHost + ":" + strconv.Itoa(port),
			InboundTag: inTag,
		})
		port++
	}
	return cfg, bindings, skipped
}

// buildXrayOutbound 造一条 vless 出站。
func buildXrayOutbound(node vlessNode, tag string) xrayOutbound {
	portNum, _ := strconv.Atoi(node.Port) // parseVlessURI 已保证是数字
	user := xrayVlessUser{ID: node.UUID, Encryption: "none"}
	// flow 只在节点显式给了才带。xtls-rprx-vision 之类填错会直接握手失败，
	// 不给默认值比给一个「常见值」安全
	if flow := strings.TrimSpace(node.Params.Get("flow")); flow != "" {
		user.Flow = flow
	}

	stream := xrayStreamSettings{
		Network:  node.Network(),
		Security: node.Security(),
	}
	switch node.Security() {
	case "reality":
		stream.RealitySettings = &xrayRealitySettings{
			Fingerprint: fallbackParam(node, "fp", "chrome"),
			ServerName:  node.ServerName(),
			PublicKey:   node.Params.Get("pbk"),
			ShortID:     node.Params.Get("sid"),
			SpiderX:     node.Params.Get("spx"),
		}
	case "tls":
		tls := &xrayTLSSettings{ServerName: node.ServerName()}
		if fp := strings.TrimSpace(node.Params.Get("fp")); fp != "" {
			tls.Fingerprint = fp
		}
		if alpn := strings.TrimSpace(node.Params.Get("alpn")); alpn != "" {
			tls.ALPN = strings.Split(alpn, ",")
		}
		stream.TLSSettings = tls
	}

	return xrayOutbound{
		Protocol:       "vless",
		Settings:       xrayOutboundInner{Vnext: []xrayVnext{{Address: node.Host, Port: portNum, Users: []xrayVlessUser{user}}}},
		StreamSettings: stream,
		Tag:            tag,
	}
}

// fallbackParam 取参数，空则用默认值。
func fallbackParam(node vlessNode, key, def string) string {
	if v := strings.TrimSpace(node.Params.Get(key)); v != "" {
		return v
	}
	return def
}

// MarshalXrayConfig 序列化成 xray 能读的 JSON（缩进便于排查）。
func MarshalXrayConfig(cfg XrayConfig) ([]byte, error) {
	return json.MarshalIndent(cfg, "", "  ")
}
