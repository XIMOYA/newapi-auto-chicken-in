/*
server/proxy_addr_test.go
代理地址形状的统一口径测试。

写这个文件的起因：查 VLESS 改造的落点时发现 validFeedbackAddr 的「有且仅有一个
冒号」规则把带 scheme 的地址全判死了 —— 而 parseProxyLines 本来就会产出
socks5://host:port（proxies_test.go:32 有断言），客户端也确实拿它去签到并回传。
也就是说**现在所有 socks5 代理的成功率反馈都在被静默丢弃**，优选排序里它们永远
停在 rankUnknown。这不是 VLESS 带来的新问题，是当下就在丢数据。
*/
package main

import "testing"

func TestValidFeedbackAddrAcceptsSchemedAddrs(t *testing.T) {
	good := []struct{ addr, why string }{
		{"1.2.3.4:8080", "裸地址：池子里绝大多数是这种"},
		{"socks5://1.2.3.4:1080", "parseProxyLines 真的会产出这种，以前被丢"},
		{"http://1.2.3.4:3128", "显式 http 前缀"},
		{"example.com:8080", "域名形式：上游源里出现过，注释里也说了要收"},
		{"vless://d0d8d1d0-0000-4000-8000-000000000000@a.example.com:443" +
			"?type=ws&security=tls&sni=a.example.com&path=%2Fws#香港01", "VLESS 节点"},
	}
	for _, c := range good {
		if !validFeedbackAddr(c.addr) {
			t.Errorf("应收下 %q（%s）", c.addr, c.why)
		}
	}

	bad := []struct{ addr, why string }{
		{"", "空"},
		{"   ", "全空白"},
		{"1.2.3.4", "没有端口"},
		{":8080", "缺主机"},
		{"1.2.3.4:", "缺端口"},
		{"1.2.3.4 :80", "夹了空格：客户端拼串出 bug 的典型形态"},
		{"http://", "有 scheme 没主机"},
		{"socks5://:1080", "有 scheme 没主机名"},
		{"://1.2.3.4:8080", "scheme 为空"},
	}
	for _, c := range bad {
		if validFeedbackAddr(c.addr) {
			t.Errorf("应拒掉 %q（%s）", c.addr, c.why)
		}
	}
}

func TestValidFeedbackAddrLengthCeiling(t *testing.T) {
	// VLESS 的 URI 带 uuid + 一堆传输参数 + 中文备注，255 字节根本不够，
	// 上限必须能装下真实节点；同时仍要挡住异常超长串（主键会跟着变长）
	long := "vless://d0d8d1d0-0000-4000-8000-000000000000@node-01.example.com:443" +
		"?encryption=none&flow=xtls-rprx-vision&security=reality&sni=www.example.com" +
		"&fp=chrome&pbk=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa&sid=0123abcd" +
		"&type=tcp&headerType=none#机场名称-香港-01-倍率1.0"
	if len(long) <= 255 {
		t.Fatalf("样例没有超过旧上限，测不出问题（len=%d）", len(long))
	}
	if !validFeedbackAddr(long) {
		t.Errorf("真实形态的 VLESS 节点没被收下（len=%d）", len(long))
	}

	tooLong := make([]byte, maxFeedbackAddrLen+1)
	for i := range tooLong {
		tooLong[i] = 'a'
	}
	if validFeedbackAddr("http://x.com:80/" + string(tooLong)) {
		t.Error("异常超长地址仍应被拒")
	}
}
