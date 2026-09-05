/*
server/xray_config_test.go
xray 配置生成的测试。

配置结构错了的后果是「xray 能启动、单测也绿、但请求连不上」—— 最难查的那种坏。
所以这里逐字段核对结构（对照 npm vless-to-xray 的实现），并守住三件事：
  - 端口不重复、每个入站都有唯一 tag、路由一对一绑定（多节点共用一个进程的前提）
  - 不支持的节点必须被跳过并给出原因，不能静默丢
  - 生成的 JSON 里不能出现除 vnext.users.id 之外的 uuid 泄漏点（日志/tag/跳过原因）
*/
package main

import (
	"encoding/json"
	"strings"
	"testing"
)

const (
	xrayUUID1 = "11111111-1111-4000-8000-000000000000"
	xrayUUID2 = "22222222-2222-4000-8000-000000000000"
)

// mustNode 解析一条 vless URI，失败直接让测试挂 —— 样例写错了得当场知道。
func mustNode(t *testing.T, uri string) vlessNode {
	t.Helper()
	node, err := parseVlessURI(uri)
	if err != nil {
		t.Fatalf("样例 URI 解析失败 %q: %v", uri, err)
	}
	return node
}

func TestBuildXrayConfigRealityNode(t *testing.T) {
	node := mustNode(t, "vless://"+xrayUUID1+"@a.example.com:443"+
		"?type=tcp&security=reality&sni=www.microsoft.com&fp=chrome"+
		"&pbk=PUBKEY123&sid=ab12&flow=xtls-rprx-vision#香港01")

	cfg, bindings, skipped := BuildXrayConfig([]vlessNode{node}, 20800)
	if len(skipped) != 0 {
		t.Fatalf("这条节点应被支持，实际跳过: %v", skipped)
	}
	if len(cfg.Inbounds) != 1 || len(cfg.Outbounds) != 1 || len(cfg.Routing.Rules) != 1 {
		t.Fatalf("三段数量应各为 1: in=%d out=%d rules=%d",
			len(cfg.Inbounds), len(cfg.Outbounds), len(cfg.Routing.Rules))
	}

	in := cfg.Inbounds[0]
	if in.Listen != "127.0.0.1" {
		t.Errorf("只能听回环，实际 %q —— 听 0.0.0.0 等于把代理白送给同网段", in.Listen)
	}
	if in.Port != 20800 || in.Protocol != "socks" {
		t.Errorf("入站 = port %d proto %q", in.Port, in.Protocol)
	}

	out := cfg.Outbounds[0]
	if out.Protocol != "vless" {
		t.Errorf("出站协议 = %q", out.Protocol)
	}
	if len(out.Settings.Vnext) != 1 {
		t.Fatalf("vnext 应有 1 条")
	}
	vnext := out.Settings.Vnext[0]
	if vnext.Address != "a.example.com" || vnext.Port != 443 {
		t.Errorf("vnext 目标 = %s:%d", vnext.Address, vnext.Port)
	}
	if len(vnext.Users) != 1 || vnext.Users[0].ID != xrayUUID1 {
		t.Fatalf("users = %+v", vnext.Users)
	}
	// encryption 恒为 none：写别的值 xray 直接拒绝启动
	if vnext.Users[0].Encryption != "none" {
		t.Errorf("encryption = %q, want none", vnext.Users[0].Encryption)
	}
	if vnext.Users[0].Flow != "xtls-rprx-vision" {
		t.Errorf("flow 应透传节点给的值，实际 %q", vnext.Users[0].Flow)
	}

	stream := out.StreamSettings
	if stream.Network != "tcp" || stream.Security != "reality" {
		t.Errorf("stream = %+v", stream)
	}
	if stream.RealitySettings == nil {
		t.Fatal("reality 节点必须有 realitySettings")
	}
	r := stream.RealitySettings
	if r.PublicKey != "PUBKEY123" || r.ShortID != "ab12" {
		t.Errorf("reality 参数丢了: %+v", r)
	}
	if r.ServerName != "www.microsoft.com" {
		t.Errorf("serverName 应取 sni，实际 %q", r.ServerName)
	}
	if r.Fingerprint != "chrome" {
		t.Errorf("fingerprint = %q", r.Fingerprint)
	}
	// tls 与 reality 的 settings 互斥，同时出现 xray 行为未定义
	if stream.TLSSettings != nil {
		t.Error("reality 节点不该同时带 tlsSettings")
	}

	// 路由必须把这个入站一对一绑到这个出站 —— 单进程多节点全靠它
	rule := cfg.Routing.Rules[0]
	if rule.Type != "field" || len(rule.InboundTag) != 1 ||
		rule.InboundTag[0] != in.Tag || rule.OutboundTag != out.Tag {
		t.Fatalf("路由没把 %q 绑到 %q: %+v", in.Tag, out.Tag, rule)
	}

	if len(bindings) != 1 {
		t.Fatalf("应产出 1 条绑定")
	}
	b := bindings[0]
	// 两个键必须分开：反馈按原始 URI 记账，连接走本地地址
	if b.NodeAddr != node.Raw {
		t.Errorf("NodeAddr 应是原始 URI，实际 %q", b.NodeAddr)
	}
	if b.LocalProxy != "socks5://127.0.0.1:20800" {
		t.Errorf("LocalProxy = %q", b.LocalProxy)
	}
}

func TestBuildXrayConfigMultipleNodesGetDistinctPorts(t *testing.T) {
	nodes := []vlessNode{
		mustNode(t, "vless://"+xrayUUID1+"@a.example.com:443?security=tls&sni=a.example.com#A"),
		mustNode(t, "vless://"+xrayUUID2+"@b.example.com:8443?security=reality&pbk=PK&sid=cd#B"),
	}
	cfg, bindings, skipped := BuildXrayConfig(nodes, 20900)
	if len(skipped) != 0 {
		t.Fatalf("两条都该支持: %v", skipped)
	}
	if len(cfg.Inbounds) != 2 || len(bindings) != 2 {
		t.Fatalf("in=%d bindings=%d", len(cfg.Inbounds), len(bindings))
	}
	// 端口撞了会让第二个入站启动失败，整个进程只有一半节点可用
	if cfg.Inbounds[0].Port == cfg.Inbounds[1].Port {
		t.Fatal("两个入站端口相同")
	}
	if cfg.Inbounds[0].Port != 20900 || cfg.Inbounds[1].Port != 20901 {
		t.Errorf("端口应从 startPort 递增，实际 %d/%d",
			cfg.Inbounds[0].Port, cfg.Inbounds[1].Port)
	}
	// tag 撞了路由会指向错误的出站 —— 流量走错节点且完全无感
	if cfg.Inbounds[0].Tag == cfg.Inbounds[1].Tag ||
		cfg.Outbounds[0].Tag == cfg.Outbounds[1].Tag {
		t.Fatal("tag 重复")
	}
	// 逐条核对绑定关系，不能交叉
	for i, rule := range cfg.Routing.Rules {
		if rule.InboundTag[0] != cfg.Inbounds[i].Tag || rule.OutboundTag != cfg.Outbounds[i].Tag {
			t.Fatalf("第 %d 条路由绑错了: %+v", i, rule)
		}
	}
	// tls 节点走 tlsSettings 而不是 realitySettings
	if cfg.Outbounds[0].StreamSettings.TLSSettings == nil {
		t.Error("tls 节点应有 tlsSettings")
	}
	if cfg.Outbounds[0].StreamSettings.RealitySettings != nil {
		t.Error("tls 节点不该有 realitySettings")
	}
}

func TestBuildXrayConfigSkipsUnsupported(t *testing.T) {
	nodes := []vlessNode{
		mustNode(t, "vless://"+xrayUUID1+"@ws.example.com:443?type=ws&security=tls&path=%2Fws#WS节点"),
		mustNode(t, "vless://"+xrayUUID1+"@none.example.com:443?security=none#无加密"),
		mustNode(t, "vless://"+xrayUUID1+"@bad.example.com:443?security=reality#缺pbk"),
		mustNode(t, "vless://"+xrayUUID2+"@ok.example.com:443?security=tls#能用的"),
	}
	cfg, bindings, skipped := BuildXrayConfig(nodes, 21000)
	if len(bindings) != 1 || len(cfg.Inbounds) != 1 {
		t.Fatalf("只有 1 条该进配置，实际 bindings=%d in=%d", len(bindings), len(cfg.Inbounds))
	}
	if len(skipped) != 3 {
		t.Fatalf("应跳过 3 条并各给原因，实际 %v", skipped)
	}
	joined := strings.Join(skipped, "\n")
	for _, want := range []string{"type=\"ws\"", "security=\"none\"", "pbk"} {
		if !strings.Contains(joined, want) {
			t.Errorf("跳过原因里应说明 %q: %s", want, joined)
		}
	}
	// 跳过原因会进日志，绝不能带 uuid
	if strings.Contains(joined, xrayUUID1) || strings.Contains(joined, xrayUUID2) {
		t.Fatalf("跳过原因泄漏了 uuid: %s", joined)
	}
	// 能用的那条要拿到 startPort，跳过的不占号
	if bindings[0].LocalProxy != "socks5://127.0.0.1:21000" {
		t.Errorf("跳过的节点不该占端口号，实际 %q", bindings[0].LocalProxy)
	}
}

func TestBuildXrayConfigEmptyWhenNothingSupported(t *testing.T) {
	// 全都不支持时给空配置而不是报错：调用方据此判定「压根不用起 xray」
	nodes := []vlessNode{
		mustNode(t, "vless://"+xrayUUID1+"@a.example.com:443?security=none#x"),
	}
	cfg, bindings, skipped := BuildXrayConfig(nodes, 21100)
	if len(cfg.Inbounds) != 0 || len(bindings) != 0 {
		t.Fatal("不该产出任何入站")
	}
	if len(skipped) != 1 {
		t.Fatalf("应给出跳过原因: %v", skipped)
	}
	// 空配置也要能正常序列化，且是 [] 而不是 null（xray 对 null 的处理不一致）
	raw, err := MarshalXrayConfig(cfg)
	if err != nil {
		t.Fatal(err)
	}
	var probe map[string]any
	if err := json.Unmarshal(raw, &probe); err != nil {
		t.Fatalf("生成的不是合法 JSON: %v", err)
	}
	if !strings.Contains(string(raw), `"inbounds": []`) {
		t.Errorf("空入站应序列化成 []，实际:\n%s", raw)
	}
}

func TestMarshalXrayConfigShape(t *testing.T) {
	// 整体形状核对：顶层四段齐全，uuid 只出现在 vnext.users.id 一处
	node := mustNode(t, "vless://"+xrayUUID1+"@a.example.com:443?security=reality&pbk=PK&sid=ab#节点")
	cfg, _, _ := BuildXrayConfig([]vlessNode{node}, 21200)
	raw, err := MarshalXrayConfig(cfg)
	if err != nil {
		t.Fatal(err)
	}
	text := string(raw)
	for _, key := range []string{`"log"`, `"inbounds"`, `"outbounds"`, `"routing"`, `"loglevel"`} {
		if !strings.Contains(text, key) {
			t.Errorf("缺少顶层字段 %s", key)
		}
	}
	if n := strings.Count(text, xrayUUID1); n != 1 {
		t.Errorf("uuid 应只出现 1 次（vnext.users.id），实际 %d 次", n)
	}
}
