/*
server/proxy_subscription_test.go
机场订阅解析测试。

除了「能解出来」，重点守两件容易出事的：
  - 编码变体：URL-safe 字符集 + 省掉 = 填充是机场里的常态，只支持
    base64.StdEncoding 的话整个订阅会解出 0 条，而且不报错 —— 是最难查的那种坏
  - 不能干扰存量：免费代理源的 IP 列表、JSON 源必须还走原来的分支，
    订阅分支插在最前面不许把它们抢走
*/
package main

import (
	"encoding/base64"
	"strings"
	"testing"
)

const (
	subNodeA = "vless://11111111-1111-4000-8000-000000000000@a.example.com:443?type=ws&security=tls#香港01"
	subNodeB = "vless://22222222-2222-4000-8000-000000000000@b.example.com:8443?type=tcp#新加坡02"
)

func TestParseSubscriptionAcceptsAllBase64Variants(t *testing.T) {
	plain := subNodeA + "\n" + subNodeB + "\n"
	// 四种组合都得认：标准/URL-safe × 带填充/不带填充
	variants := map[string]string{
		"标准带填充":        base64.StdEncoding.EncodeToString([]byte(plain)),
		"标准无填充":        base64.RawStdEncoding.EncodeToString([]byte(plain)),
		"URL-safe 带填充": base64.URLEncoding.EncodeToString([]byte(plain)),
		"URL-safe 无填充": base64.RawURLEncoding.EncodeToString([]byte(plain)),
	}
	for name, body := range variants {
		t.Run(name, func(t *testing.T) {
			got := parseSubscription(body)
			if len(got) != 2 || got[0] != subNodeA || got[1] != subNodeB {
				t.Fatalf("解析 = %v", got)
			}
		})
	}
}

func TestParseSubscriptionHandlesWrappedAndPlain(t *testing.T) {
	plain := subNodeA + "\n" + subNodeB
	encoded := base64.StdEncoding.EncodeToString([]byte(plain))

	// 机场常按 76 字符折行，换行不属于编码内容
	var wrapped strings.Builder
	for i := 0; i < len(encoded); i += 20 {
		end := i + 20
		if end > len(encoded) {
			end = len(encoded)
		}
		wrapped.WriteString(encoded[i:end])
		wrapped.WriteString("\r\n")
	}
	if got := parseSubscription(wrapped.String()); len(got) != 2 {
		t.Fatalf("折行的 base64 应能解析, got %v", got)
	}
	// 也有机场压根不编码
	if got := parseSubscription(plain); len(got) != 2 {
		t.Fatalf("明文订阅应能解析, got %v", got)
	}
	// 顺序必须保留：机场把低倍率/推荐节点排在前面
	if got := parseSubscription(plain); got[0] != subNodeA {
		t.Errorf("顺序被打乱: %v", got)
	}
}

func TestParseSubscriptionSkipsUnusableEntries(t *testing.T) {
	// 混进来的东西一律跳过，但不能因为其中一条坏就整份放弃
	body := strings.Join([]string{
		subNodeA,
		"ss://YWVzLTI1Ni1nY206cGFzcw@c.example.com:8388#不支持的协议",
		"vmess://eyJ2IjoiMiJ9#同样不支持",
		"vless://@d.example.com:443#缺uuid",
		"vless://33333333-3333-4000-8000-000000000000@e.example.com#缺端口",
		"vless://44444444-4444-4000-8000-000000000000@:443#缺主机",
		"这是一行说明文字",
		"",
		subNodeB,
		subNodeA, // 重复，应去重
	}, "\n")

	got := parseSubscription(base64.StdEncoding.EncodeToString([]byte(body)))
	want := []string{subNodeA, subNodeB}
	if len(got) != len(want) {
		t.Fatalf("解析 = %v, want %v", got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Errorf("第 %d 条 = %q, want %q", i, got[i], want[i])
		}
	}
}

func TestParseSubscriptionRejectsNonSubscription(t *testing.T) {
	// 返回 nil 才会让调用方回落到 JSON / 正则分支；返回空切片会把存量源全部废掉
	cases := []struct{ name, body string }{
		{"免费代理 IP 列表", "1.2.3.4:8080\n5.6.7.8:3128\n"},
		{"JSON 源", `{"data":{"proxy_list":[{"ip":"1.2.3.4","port":"8080"}]}}`},
		{"89ip 那种 HTML", "<html><body>1.2.3.4:8080<br>5.6.7.8:80</body></html>"},
		{"空响应", ""},
		{"全空白", "  \n\t\n"},
		{"解得开但是二进制垃圾", base64.StdEncoding.EncodeToString([]byte{0x00, 0x01, 0xff, 0xfe})},
		{"能解开但里面没有节点", base64.StdEncoding.EncodeToString([]byte("hello world\nnothing here\n"))},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if got := parseSubscription(c.body); got != nil {
				t.Fatalf("应判定为「不是订阅」并返回 nil，实际 %v", got)
			}
		})
	}
}

func TestParseProxyLinesKeepsLegacySources(t *testing.T) {
	// 回归保护：订阅分支插在最前面，不许把存量两种形态抢走
	if got := parseProxyLines("1.2.3.4:8080\n5.6.7.8:3128"); len(got) != 2 ||
		got[0] != "1.2.3.4:8080" {
		t.Fatalf("纯文本 IP 列表被订阅分支抢走了: %v", got)
	}
	if got := parseProxyLines(
		`[{"ip":"1.2.3.4","port":"3128","protocol":"http"},
		  {"ip":"5.6.7.8","port":1080,"protocol":"socks5"}]`); len(got) != 2 ||
		got[1] != "socks5://5.6.7.8:1080" {
		t.Fatalf("JSON 源被订阅分支抢走了: %v", got)
	}

	// 订阅内容进来则走订阅分支
	sub := base64.StdEncoding.EncodeToString([]byte(subNodeA + "\n" + subNodeB))
	if got := parseProxyLines(sub); len(got) != 2 || got[0] != subNodeA {
		t.Fatalf("订阅没走订阅分支: %v", got)
	}
}

func TestValidNodeURI(t *testing.T) {
	good := []string{
		subNodeA,
		"vless://11111111-1111-4000-8000-000000000000@1.2.3.4:443",
		"VLESS://11111111-1111-4000-8000-000000000000@a.com:443", // scheme 大小写不敏感
		"vless://11111111-1111-4000-8000-000000000000@[2001:db8::1]:443#IPv6",
	}
	for _, uri := range good {
		if !validNodeURI(uri) {
			t.Errorf("应接受 %q", uri)
		}
	}
	bad := []struct{ uri, why string }{
		{"", "空"},
		{"vless://a.com:443", "没有 uuid：连不上却会占掉测活配额"},
		{"vless://uuid@a.com", "没有端口"},
		{"vless://uuid@:443", "没有主机"},
		{"trojan://uuid@a.com:443", "协议还没接 xray outbound，解析出来也起不来"},
		{"1.2.3.4:8080", "普通代理不是节点"},
		{"http://user:pass@a.com:8080", "普通代理不是节点"},
	}
	for _, c := range bad {
		if validNodeURI(c.uri) {
			t.Errorf("应拒绝 %q（%s）", c.uri, c.why)
		}
	}
}
