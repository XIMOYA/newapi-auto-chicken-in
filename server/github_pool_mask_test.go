// server/github_pool_mask_test.go
// 共享凭据池的打码/还原/清理三件套测试。
//
// 加 Config.GitHubAccounts 时漏掉这三处会各有一种后果：
//   - MaskConfig 不打码   → 明文 session 下发前端
//   - UnmaskConfig 不还原 → 前端回传的 "***" 被当真值存库，签发静默失败
//   - SanitizeMaskLeftovers 不清理 → 库里残留 "***"，界面显示「已填写」但用不了
//
// 另外 cloneConfig 若没重建这个 slice，MaskConfig 会改到原配置的真实 session。
package main

import "testing"

// poolConfig 造一份带共享凭据池的配置。
func poolConfig() *Config {
	return &Config{
		GitHubAccounts: []GitHubAccount{
			{Name: "Steven", UserSession: "real-sess", ClientID: "cid"},
			{Name: "NoSess"},
		},
	}
}

func TestMaskConfigMasksPoolSession(t *testing.T) {
	cfg := poolConfig()
	masked := MaskConfig(cfg)

	if masked.GitHubAccounts[0].UserSession != MaskPlaceholder {
		t.Errorf("池子 session 应被打码, 实际 %q", masked.GitHubAccounts[0].UserSession)
	}
	// 空 session 不该被打成 "***"，否则界面会显示「已填写」
	if masked.GitHubAccounts[1].UserSession != "" {
		t.Errorf("空 session 不该打码, 实际 %q", masked.GitHubAccounts[1].UserSession)
	}
	// client_id 不是凭据，照常下发
	if masked.GitHubAccounts[0].ClientID != "cid" {
		t.Errorf("client_id 不该被打码, 实际 %q", masked.GitHubAccounts[0].ClientID)
	}
}

func TestMaskConfigDoesNotMutateOriginalPool(t *testing.T) {
	cfg := poolConfig()
	_ = MaskConfig(cfg)
	// cloneConfig 必须重建 GitHubAccounts slice。共享底层数组的话，
	// 上面这次打码会把内存里的真实 session 换成 "***"，之后任何保存都会落库
	if cfg.GitHubAccounts[0].UserSession != "real-sess" {
		t.Fatalf("MaskConfig 污染了原配置: %q", cfg.GitHubAccounts[0].UserSession)
	}
}

func TestUnmaskConfigRestoresPoolSession(t *testing.T) {
	old := poolConfig()
	incoming := &Config{
		GitHubAccounts: []GitHubAccount{
			{Name: "Steven", UserSession: MaskPlaceholder, ClientID: "cid2"},
		},
	}
	out, err := UnmaskConfig(incoming, old)
	if err != nil {
		t.Fatalf("还原失败: %v", err)
	}
	if out.GitHubAccounts[0].UserSession != "real-sess" {
		t.Errorf("session 未还原 = %q", out.GitHubAccounts[0].UserSession)
	}
	// 非打码字段按提交值走
	if out.GitHubAccounts[0].ClientID != "cid2" {
		t.Errorf("client_id 应采用提交值 = %q", out.GitHubAccounts[0].ClientID)
	}
}

func TestUnmaskConfigRejectsUnknownPoolName(t *testing.T) {
	old := poolConfig()
	// 改名却仍提交 "***"：旧配置里查不到新名，只能报错。
	// 静默存下 "***" 字面量会让界面显示「已填写」而签发失败
	incoming := &Config{
		GitHubAccounts: []GitHubAccount{{Name: "Renamed", UserSession: MaskPlaceholder}},
	}
	if _, err := UnmaskConfig(incoming, old); err == nil {
		t.Fatal("改名时提交 *** 应报错")
	}
}

func TestSanitizeMaskLeftoversCleansPool(t *testing.T) {
	cfg := &Config{
		GitHubAccounts: []GitHubAccount{{Name: "Steven", UserSession: MaskPlaceholder}},
	}
	cleaned := SanitizeMaskLeftovers(cfg)
	if cfg.GitHubAccounts[0].UserSession != "" {
		t.Errorf("残留的 *** 应被清空, 实际 %q", cfg.GitHubAccounts[0].UserSession)
	}
	found := false
	for _, path := range cleaned {
		if path == "github_accounts[0].user_session" {
			found = true
		}
	}
	if !found {
		t.Errorf("应报告被清理的字段路径, 实际 %v", cleaned)
	}
}
