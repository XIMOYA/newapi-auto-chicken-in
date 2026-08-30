/*
server/proxy_display_test.go
展示脱敏与指纹的测试。

守两条底线：
  - 脱敏结果里不能残留 uuid（VLESS 的接入凭据），但必须还认得出是哪个节点
  - 指纹必须由完整地址算出：同机场同端口同备注、只有 uuid 不同的两条节点，
    脱敏后长得一模一样，指纹一样就意味着界面上的操作会打到错误的那一条
*/
package main

import (
	"encoding/json"
	"net/http"
	"strings"
	"testing"
)

const testVlessUUID = "d0d8d1d0-1111-4000-8000-000000000000"

func TestProxyDisplayHidesCredentials(t *testing.T) {
	cases := []struct{ in, want, why string }{
		{
			"vless://" + testVlessUUID + "@a.example.com:443?type=ws&security=tls&sni=a.example.com#香港01",
			"vless://***@a.example.com:443#香港01",
			"uuid 抹掉，host:port 和备注留着 —— 那是认节点的线索",
		},
		{
			"vless://" + testVlessUUID + "@a.example.com:443?type=tcp",
			"vless://***@a.example.com:443",
			"没有备注就只到 host:port",
		},
		{
			"http://user:pass@1.2.3.4:8080",
			"http://***@1.2.3.4:8080",
			"带认证的 http 代理，密码同样不该回传界面",
		},
		{"1.2.3.4:8080", "1.2.3.4:8080", "裸地址没有凭据，原样显示"},
		{"socks5://1.2.3.4:1080", "socks5://1.2.3.4:1080", "无认证 socks5 原样显示"},
		{"", "", "空进空出"},
		{"   ", "", "全空白按空处理"},
		{"://坏地址", "://坏地址", "解析不了的原样给出，抹成 *** 反而看不出坏在哪"},
	}
	for _, c := range cases {
		if got := proxyDisplay(c.in); got != c.want {
			t.Errorf("proxyDisplay(%q) = %q, want %q（%s）", c.in, got, c.want, c.why)
		}
	}
}

func TestProxyDisplayNeverLeaksUUID(t *testing.T) {
	// 上面那组是逐个比对期望值，这里是兜底：不管形态怎么变，uuid 不许出现在结果里
	addrs := []string{
		"vless://" + testVlessUUID + "@a.example.com:443?type=ws#节点",
		"vless://" + testVlessUUID + "@1.2.3.4:8443",
		"vless://" + testVlessUUID + "@[2001:db8::1]:443#IPv6节点",
		"trojan://" + testVlessUUID + "@a.example.com:443#将来可能加的协议",
	}
	for _, addr := range addrs {
		got := proxyDisplay(addr)
		if strings.Contains(got, testVlessUUID) {
			t.Errorf("脱敏后仍带凭据: %q -> %q", addr, got)
		}
		if !strings.Contains(got, "***") {
			t.Errorf("有 userinfo 却没打码: %q -> %q", addr, got)
		}
	}
}

func TestProxyFingerprintDistinguishesSameLookingNodes(t *testing.T) {
	// 这是整个方案能不能成立的关键：机场里同一落地常有多条只差 uuid 的节点，
	// 它们脱敏后完全一样，只能靠指纹区分
	a := "vless://" + testVlessUUID + "@a.example.com:443#香港01"
	b := "vless://d0d8d1d0-2222-4000-8000-000000000000@a.example.com:443#香港01"

	if proxyDisplay(a) != proxyDisplay(b) {
		t.Fatalf("这组样例的脱敏结果本该一样，测不出要测的东西: %q vs %q",
			proxyDisplay(a), proxyDisplay(b))
	}
	fa, fb := proxyFingerprint(a), proxyFingerprint(b)
	if fa == fb {
		t.Fatalf("只差 uuid 的两条节点指纹相同 = %q，界面操作会打错目标", fa)
	}
	// 同一地址反复算必须稳定，否则界面刷新一次操作键就失效
	if proxyFingerprint(a) != fa {
		t.Error("同一地址的指纹不稳定")
	}
	if len(fa) != cookieFingerprintLength {
		t.Errorf("指纹长度 = %d, want %d（与 cookie 指纹同位数）", len(fa), cookieFingerprintLength)
	}
	if proxyFingerprint("") != "" {
		t.Error("空地址应给空指纹")
	}
	// 指纹本身不能反推出凭据
	if strings.Contains(fa, testVlessUUID) {
		t.Error("指纹里出现了 uuid")
	}
}

func TestListProxiesMasksCredentialsAndAddsFingerprint(t *testing.T) {
	srv := newTestServer(t)
	vless := "vless://" + testVlessUUID + "@a.example.com:443?type=ws&security=tls#香港01"
	plain := "1.2.3.4:8080"
	if err := srv.proxies.replaceAll([]ProxyEntry{
		aliveEntry(vless, 80, 0),
		aliveEntry(plain, 20, 0),
	}); err != nil {
		t.Fatalf("replaceAll: %v", err)
	}

	rr := doReq(t, srv, http.MethodGet, "/api/proxies", loginToken(t, srv), nil)
	if rr.Code != http.StatusOK {
		t.Fatalf("查询代理列表失败 = %d, %s", rr.Code, rr.Body.String())
	}
	// 最要紧的一条：整个响应体里不许出现 uuid
	if strings.Contains(rr.Body.String(), testVlessUUID) {
		t.Fatalf("界面响应里出现了节点 uuid（接入凭据）: %s", rr.Body.String())
	}

	var resp struct {
		Proxies []struct {
			Addr        string `json:"addr"`
			Protocol    string `json:"protocol"`
			Fingerprint string `json:"fingerprint"`
		} `json:"proxies"`
		Total int `json:"total"`
	}
	if err := json.Unmarshal(rr.Body.Bytes(), &resp); err != nil {
		t.Fatal(err)
	}
	if resp.Total != 2 || len(resp.Proxies) != 2 {
		t.Fatalf("条数不对: total=%d len=%d", resp.Total, len(resp.Proxies))
	}
	seen := map[string]string{} // protocol -> addr
	for _, p := range resp.Proxies {
		seen[p.Protocol] = p.Addr
		if p.Fingerprint == "" {
			t.Errorf("每条都该带指纹，%q 没有", p.Addr)
		}
	}
	if got := seen["vless"]; got != "vless://***@a.example.com:443#香港01" {
		t.Errorf("VLESS 条目脱敏形态不对: %q", got)
	}
	// 存量代理必须原样，否则界面上的地址跟库里对不上，反而妨碍排查
	if got := seen["http"]; got != plain {
		t.Errorf("裸地址不该被改动: %q", got)
	}
}

func TestResolveProxyRefsAcceptsFingerprintAndAddr(t *testing.T) {
	// 界面对 VLESS 只有指纹、对存量代理仍直接传地址，两种都得认。
	// 认不出的原样往下传，否则响应里「测了几条」会跟用户选的条数对不上
	srv := newTestServer(t)
	vless := "vless://" + testVlessUUID + "@a.example.com:443#香港01"
	plain := "1.2.3.4:8080"
	if err := srv.proxies.replaceAll([]ProxyEntry{
		aliveEntry(vless, 80, 0),
		aliveEntry(plain, 20, 0),
	}); err != nil {
		t.Fatalf("replaceAll: %v", err)
	}

	got := srv.proxies.resolveProxyRefs([]string{
		proxyFingerprint(vless), // 指纹 → 还原成完整 URI
		plain,                   // 地址 → 原样
		"  ",                    // 空白项丢掉
		"陌生:1",                  // 库里没有 → 原样
	})
	want := []string{vless, plain, "陌生:1"}
	if len(got) != len(want) {
		t.Fatalf("解析结果 = %v, want %v", got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Errorf("第 %d 项 = %q, want %q", i, got[i], want[i])
		}
	}

	// 空列表在下游是「测全部可用代理」的意思，不能被动过
	if out := srv.proxies.resolveProxyRefs(nil); len(out) != 0 {
		t.Errorf("空列表应原样返回，实际 %v", out)
	}
}

func TestProxyProtocolOf(t *testing.T) {
	cases := []struct{ in, want string }{
		{"1.2.3.4:8080", "http"}, // 裸地址按 http，与 parseProxyLines 的约定一致
		{"socks5://1.2.3.4:1080", "socks5"},
		{"SOCKS5://1.2.3.4:1080", "socks5"}, // 大小写归一，免得同一协议分成两类
		{"vless://uuid@a.com:443", "vless"},
		{"", ""},
	}
	for _, c := range cases {
		if got := proxyProtocolOf(c.in); got != c.want {
			t.Errorf("proxyProtocolOf(%q) = %q, want %q", c.in, got, c.want)
		}
	}
}
