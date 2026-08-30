/*
server/github_binding_test.go
指纹 seed 与出口绑定的持久化测试。

两件事都属于「服务端运行状态」而不是用户配置，所以守的重点是：
  - 指纹 seed 一旦分配就不许再变。重算就等于给这个账号换了台设备，
    而 GitHub 的 session 绑设备特征 —— 那正是我们要避免的
  - 出口绑定不许被客户端提交覆盖。它出站时被 proxyDisplay 脱敏过
    （vless 的 uuid 是凭据），原样存回去会把绑定写成一条不可用的假地址
*/
package main

import (
	"encoding/json"
	"net/http"
	"strings"
	"testing"
)

const bindTestNode = "vless://d0d8d1d0-9999-4000-8000-000000000000@node.example.com:443#香港01"

func TestEnsureGitHubFingerprintsIsIdempotent(t *testing.T) {
	cfg := &Config{GitHubAccounts: []GitHubAccount{
		{Name: "Steven"},
		{Name: "Alice", Fingerprint: "已经有了别动我"},
		{Name: "   "}, // 名字空的交给 ValidateConfig 拦，这里不越权补
	}}
	filled := ensureGitHubFingerprints(cfg)
	if len(filled) != 1 || filled[0] != "Steven" {
		t.Fatalf("应只补 Steven，实际 %v", filled)
	}
	if cfg.GitHubAccounts[0].Fingerprint == "" {
		t.Error("Steven 没被补上 seed")
	}
	if cfg.GitHubAccounts[1].Fingerprint != "已经有了别动我" {
		t.Error("已有 seed 被覆盖了 —— 那等于给账号换了台设备")
	}
	if cfg.GitHubAccounts[2].Fingerprint != "" {
		t.Error("空名字的条目不该被补")
	}

	// 再跑一次不该有任何变化
	seed := cfg.GitHubAccounts[0].Fingerprint
	if again := ensureGitHubFingerprints(cfg); len(again) != 0 {
		t.Errorf("第二次应无事可做，实际 %v", again)
	}
	if cfg.GitHubAccounts[0].Fingerprint != seed {
		t.Error("重复调用改了已有 seed")
	}
}

func TestKeepGitHubRuntimeFieldsIgnoresClientValue(t *testing.T) {
	stored := Config{GitHubAccounts: []GitHubAccount{
		{Name: "Steven", ProxyAddr: bindTestNode},
		{Name: "Alice", ProxyAddr: "1.2.3.4:8080"},
	}}
	// 客户端提交的脱敏值（界面原样回传的形态）必须被换成库里的真值
	incoming := &Config{GitHubAccounts: []GitHubAccount{
		{Name: "Alice", ProxyAddr: "1.2.3.4:8080"},
		{Name: "Steven", ProxyAddr: "vless://***@node.example.com:443#香港01"},
		{Name: "新账号", ProxyAddr: "自己指定的出口:1080"},
	}}
	keepGitHubRuntimeFields(incoming, stored)

	byName := map[string]string{}
	for _, g := range incoming.GitHubAccounts {
		byName[g.Name] = g.ProxyAddr
	}
	// 按名字匹配而不是下标：顺序变了也要对上
	if byName["Steven"] != bindTestNode {
		t.Errorf("脱敏值应被换回库里的真值，实际 %q", byName["Steven"])
	}
	if byName["Alice"] != "1.2.3.4:8080" {
		t.Errorf("Alice 的绑定被改了: %q", byName["Alice"])
	}

	// ops 路径的形态：incoming 带的是服务端刚搬过来的真值，库里还没有这个名字。
	// 这种必须保留 —— 无条件覆盖会在改名时把刚搬好的绑定抹掉
	renamed := &Config{GitHubAccounts: []GitHubAccount{
		{Name: "StevenNew", ProxyAddr: bindTestNode},
	}}
	keepGitHubRuntimeFields(renamed, stored)
	if renamed.GitHubAccounts[0].ProxyAddr != bindTestNode {
		t.Errorf("改名后服务端搬过来的绑定被抹了: %q",
			renamed.GitHubAccounts[0].ProxyAddr)
	}

	// 没带绑定但库里有：补回去，免得一次普通保存就把绑定清了
	blank := &Config{GitHubAccounts: []GitHubAccount{{Name: "Steven"}}}
	keepGitHubRuntimeFields(blank, stored)
	if blank.GitHubAccounts[0].ProxyAddr != bindTestNode {
		t.Errorf("空值应从库里补回，实际 %q", blank.GitHubAccounts[0].ProxyAddr)
	}
}

func TestMaskConfigHidesBoundNodeUUID(t *testing.T) {
	cfg := &Config{GitHubAccounts: []GitHubAccount{
		{Name: "Steven", UserSession: "real-sess", ProxyAddr: bindTestNode,
			Fingerprint: "seed-abc"},
	}}
	masked := MaskConfig(cfg)
	got := masked.GitHubAccounts[0]

	if strings.Contains(got.ProxyAddr, "d0d8d1d0-9999") {
		t.Fatalf("绑定的节点 uuid 明文下发了: %q", got.ProxyAddr)
	}
	// 仍要看得出绑的是哪个节点，否则运维没法判断绑定是否合理
	if !strings.Contains(got.ProxyAddr, "node.example.com:443") ||
		!strings.Contains(got.ProxyAddr, "香港01") {
		t.Errorf("脱敏后认不出节点了: %q", got.ProxyAddr)
	}
	// 指纹 seed 只决定 UA，不是凭据，照常下发让界面能显示派生结果
	if got.Fingerprint != "seed-abc" {
		t.Errorf("指纹 seed 不该被打码: %q", got.Fingerprint)
	}
	// 原配置不许被污染
	if cfg.GitHubAccounts[0].ProxyAddr != bindTestNode {
		t.Errorf("MaskConfig 改了原配置的绑定: %q", cfg.GitHubAccounts[0].ProxyAddr)
	}
}

func TestGitHubOpsKeepsFingerprintAndBindingAcrossRename(t *testing.T) {
	// 改名不许换设备、不许丢出口。这两件事是「固定指纹 + 固定出口」的立意本身：
	// 指纹被重新派生 = 在 GitHub 眼里换了台机器；绑定丢了 = session 会换 IP 出现
	srv := newTestServer(t)
	seedPool(t, srv, []GitHubAccount{
		{Name: "Steven", UserSession: "real-sess", ClientID: "cid",
			Fingerprint: "seed-locked", ProxyAddr: bindTestNode},
	}, []Account{{Name: "Steven（a.com）", URL: "https://a.com",
		LoginMethod: LoginMethodTabiAI, GitHubAccount: "Steven", Enabled: true}})

	// 前端只回传三个字段，session 还是占位符（改名时的真实形态）
	body := ghOpsBody(ghUpsert("StevenNew", MaskPlaceholder, "cid", "Steven"))
	if rr := authedRequest(t, srv, "POST", "/api/github-accounts/ops", body); rr.Code != http.StatusOK {
		t.Fatalf("改名应成功 = %d, %s", rr.Code, rr.Body.String())
	}

	got := poolEntry(t, srv, "StevenNew")
	if got.Fingerprint != "seed-locked" {
		t.Errorf("改名后指纹 seed 变了: %q（相当于换了台设备）", got.Fingerprint)
	}
	if got.ProxyAddr != bindTestNode {
		t.Errorf("改名后出口绑定丢了: %q", got.ProxyAddr)
	}
	if got.UserSession != "real-sess" {
		t.Errorf("session 未还原: %q", got.UserSession)
	}

	// 同名更新（只改 client_id）同样不能动这两个字段
	body = ghOpsBody(ghUpsert("StevenNew", MaskPlaceholder, "cid2", ""))
	if rr := authedRequest(t, srv, "POST", "/api/github-accounts/ops", body); rr.Code != http.StatusOK {
		t.Fatalf("更新应成功 = %d, %s", rr.Code, rr.Body.String())
	}
	after := poolEntry(t, srv, "StevenNew")
	if after.Fingerprint != "seed-locked" || after.ProxyAddr != bindTestNode {
		t.Errorf("同名更新动了运行状态: fingerprint=%q proxy=%q",
			after.Fingerprint, after.ProxyAddr)
	}
	if after.ClientID != "cid2" {
		t.Errorf("client_id 应更新: %q", after.ClientID)
	}
}

func TestConfigSaveKeepsBindingAndFillsFingerprint(t *testing.T) {
	// 端到端：走真实的保存路径，确认整份提交既不会抹掉绑定、也会自动补上 seed
	srv := newTestServer(t)
	seedPool(t, srv, []GitHubAccount{
		{Name: "Steven", UserSession: "real-sess", ProxyAddr: bindTestNode},
	}, nil)

	// 先确认启动期迁移会补 seed（seedConfig 走的是 SaveConfig，不补）
	if err := SanitizeConfigSecrets(srv.db); err != nil {
		t.Fatalf("启动迁移失败: %v", err)
	}
	afterMigrate := poolEntry(t, srv, "Steven")
	if afterMigrate.Fingerprint == "" {
		t.Fatal("启动迁移应给老账号补上指纹 seed")
	}
	if afterMigrate.ProxyAddr != bindTestNode {
		t.Fatalf("迁移不该动绑定: %q", afterMigrate.ProxyAddr)
	}

	// 界面拉配置 → 拿到脱敏值 → 原样提交回来（最容易出事的那条路）
	get := doReq(t, srv, http.MethodGet, "/api/config", loginToken(t, srv), nil)
	if get.Code != http.StatusOK {
		t.Fatalf("拉配置失败 = %d", get.Code)
	}
	if strings.Contains(get.Body.String(), "d0d8d1d0-9999") {
		t.Fatal("GET /api/config 泄漏了绑定节点的 uuid")
	}

	var wrapper struct {
		Config Config `json:"config"`
	}
	if err := json.Unmarshal(get.Body.Bytes(), &wrapper); err != nil {
		t.Fatalf("解析配置响应: %v", err)
	}
	// 确认拿到的确实是脱敏形态，否则下面这次回写测不到要测的东西
	if !strings.Contains(wrapper.Config.GitHubAccounts[0].ProxyAddr, "***") {
		t.Fatalf("界面拿到的绑定不是脱敏形态: %q",
			wrapper.Config.GitHubAccounts[0].ProxyAddr)
	}
	put := doReq(t, srv, http.MethodPut, "/api/config", loginToken(t, srv),
		map[string]any{"config": wrapper.Config})
	if put.Code != http.StatusOK {
		t.Fatalf("回写配置失败 = %d, %s", put.Code, put.Body.String())
	}

	final := poolEntry(t, srv, "Steven")
	if final.ProxyAddr != bindTestNode {
		t.Fatalf("整份回写把绑定写成了脱敏值: %q", final.ProxyAddr)
	}
	if final.UserSession != "real-sess" {
		t.Errorf("session 未还原: %q", final.UserSession)
	}
	if final.Fingerprint != afterMigrate.Fingerprint {
		t.Errorf("指纹 seed 在回写中变了: %q -> %q",
			afterMigrate.Fingerprint, final.Fingerprint)
	}
}
