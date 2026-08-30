/*
server/proxy_vless_test.go
vless:// 链接解析测试。

重点守三件：
  - 接入必需的三样（uuid / host / port）缺一样就报错，不产出半成品节点
  - 传输参数原样保留 —— 解析不该偷偷丢或改 query，否则生成 outbound 时
    「节点起来了却连不上」且无从排查
  - 中文 fragment（机场备注）要解出可读文本
*/
package main

import (
	"strings"
	"testing"
)

const vlessTestUUID = "d0d8d1d0-1111-4000-8000-000000000000"

func TestParseVlessURIIdentity(t *testing.T) {
	uri := "vless://" + vlessTestUUID + "@a.example.com:443?type=ws&security=tls#香港01"
	node, err := parseVlessURI(uri)
	if err != nil {
		t.Fatalf("解析失败: %v", err)
	}
	if node.UUID != vlessTestUUID || node.Host != "a.example.com" || node.Port != "443" {
		t.Errorf("身份字段 = %q %q %q", node.UUID, node.Host, node.Port)
	}
	if node.Tag != "香港01" {
		t.Errorf("备注应解出可读文本，实际 %q", node.Tag)
	}
	// Raw 必须与输入逐字一致：它就是库里的 addr / 反馈主键
	if node.Raw != uri {
		t.Errorf("Raw 与输入不一致: %q", node.Raw)
	}
}

func TestParseVlessURIHelpers(t *testing.T) {
	node := vlessNode{Params: map[string][]string{"type": {"ws"}}}
	if node.Network() != "ws" {
		t.Errorf("Network = %q, want ws", node.Network())
	}
	if node.Security() != "none" {
		t.Errorf("缺省 Security = %q, want none", node.Security())
	}

	reality := vlessNode{
		Host:   "a.example.com",
		Params: map[string][]string{"security": {"reality"}, "sni": {"sni.example.com"}},
	}
	if reality.Security() != "reality" {
		t.Errorf("Security = %q, want reality", reality.Security())
	}
	if reality.ServerName() != "sni.example.com" {
		t.Errorf("ServerName = %q, want sni", reality.ServerName())
	}

	wsHost := vlessNode{Host: "a.example.com",
		Params: map[string][]string{"host": {"ws.example.com"}}}
	if wsHost.ServerName() != "ws.example.com" {
		t.Errorf("ServerName 应先看 host 参数，实际 %q", wsHost.ServerName())
	}

	tlsOnly := vlessNode{Host: "b.example.com", Params: map[string][]string{"security": {"tls"}}}
	if tlsOnly.ServerName() != "b.example.com" {
		t.Errorf("无 sni/host 时应回落节点 host，实际 %q", tlsOnly.ServerName())
	}
}

func TestParseVlessURIPreservesParams(t *testing.T) {
	// 传输参数原样保留，一个都不能少 —— reality 节点丢一个 pbk 就等于废了
	uri := "vless://" + vlessTestUUID + "@c.example.com:443" +
		"?encryption=none&flow=xtls-rprx-vision&security=reality&sni=www.c.example.com" +
		"&fp=chrome&pbk=ABCDEF&sid=0123&type=tcp&headerType=none"
	node, err := parseVlessURI(uri)
	if err != nil {
		t.Fatal(err)
	}
	want := map[string]string{
		"encryption": "none", "flow": "xtls-rprx-vision", "security": "reality",
		"sni": "www.c.example.com", "fp": "chrome", "pbk": "ABCDEF",
		"sid": "0123", "type": "tcp", "headerType": "none",
	}
	for k, v := range want {
		if got := node.Params.Get(k); got != v {
			t.Errorf("参数 %s = %q, want %q", k, got, v)
		}
	}
}

func TestParseVlessURIVariants(t *testing.T) {
	// IPv6 主机：Hostname() 不带方括号，端口照常
	ipv6 := "vless://" + vlessTestUUID + "@[2001:db8::1]:443?type=tcp"
	node, err := parseVlessURI(ipv6)
	if err != nil || node.Host != "2001:db8::1" || node.Port != "443" {
		t.Fatalf("IPv6 解析 = %+v, %v", node, err)
	}
	// 最小可用节点：只有身份三样，没任何传输参数
	minimal := "vless://" + vlessTestUUID + "@d.example.com:8443"
	if _, err := parseVlessURI(minimal); err != nil {
		t.Fatalf("最小节点不应失败: %v", err)
	}
	// scheme 大小写不敏感（分享链接偶尔有大写 VLESS）
	if _, err := parseVlessURI("VLESS://" + vlessTestUUID + "@e.com:443"); err != nil {
		t.Fatalf("大写 scheme 不应失败: %v", err)
	}
}

func TestParseVlessURIErrors(t *testing.T) {
	cases := []struct{ uri, why string }{
		{"", "空"},
		{"trojan://uuid@a.com:443", "不是 vless"},
		{"1.2.3.4:8080", "不是 vless"},
		{"vless://@a.com:443", "缺 uuid"},
		{"vless://uuid@a.com", "缺端口"},
		{"vless://uuid@:443", "缺主机"},
	}
	for _, c := range cases {
		if _, err := parseVlessURI(c.uri); err == nil {
			t.Errorf("应报错: %q（%s）", c.uri, c.why)
		}
	}
	// 缺主机时错误信息要能说清是主机而不是别的
	if _, err := parseVlessURI("vless://uuid@:443"); err != nil && !strings.Contains(err.Error(), "主机") {
		t.Errorf("错误信息应指明缺主机: %v", err)
	}
}
