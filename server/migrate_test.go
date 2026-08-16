/*
server/migrate_test.go
旧配置默认值升级（MigrateConfig）测试
*/
package main

import (
	"testing"
)

// writeCfg 把一份配置直接写进库，用于伪造「旧版本遗留配置」。
func writeCfg(t *testing.T, srv *Server, cfg Config) {
	t.Helper()
	if _, err := SaveConfig(srv.db, cfg); err != nil {
		t.Fatalf("SaveConfig: %v", err)
	}
}

func TestMigrateConfig_UpgradesOldDefaults(t *testing.T) {
	srv := newTestServer(t)
	old := DefaultConfig()
	old.ConfigVersion = 0 // 旧库没有这个字段，反序列化后就是 0
	old.Accounts = []Account{{Name: "旧账号", URL: "https://a.com", Cookie: "cookie"}}
	old.ProxyPool.SaveLimit = 100
	old.ProxyPool.IPSwapLimit = 5
	writeCfg(t, srv, old)

	if err := MigrateConfig(srv.db); err != nil {
		t.Fatalf("MigrateConfig: %v", err)
	}

	cfg, _, err := LoadConfig(srv.db)
	if err != nil {
		t.Fatalf("LoadConfig: %v", err)
	}
	if cfg.ProxyPool.SaveLimit != 0 {
		t.Errorf("save_limit = %d, want 0（不限量）", cfg.ProxyPool.SaveLimit)
	}
	if cfg.ProxyPool.IPSwapLimit != 10 {
		t.Errorf("ip_swap_limit = %d, want 10", cfg.ProxyPool.IPSwapLimit)
	}
	if cfg.Accounts[0].LoginMethod != LoginMethodNewAPICookie {
		t.Errorf("旧账号 login_method = %q, want %q", cfg.Accounts[0].LoginMethod, LoginMethodNewAPICookie)
	}
	if cfg.ConfigVersion != currentConfigVersion {
		t.Errorf("config_version = %d, want %d", cfg.ConfigVersion, currentConfigVersion)
	}
}

func TestMigrateConfig_KeepsExplicitValues(t *testing.T) {
	srv := newTestServer(t)
	old := DefaultConfig()
	old.ConfigVersion = 0
	old.ProxyPool.SaveLimit = 50 // 用户显式设的，不是旧默认值
	old.ProxyPool.IPSwapLimit = 3
	writeCfg(t, srv, old)

	if err := MigrateConfig(srv.db); err != nil {
		t.Fatalf("MigrateConfig: %v", err)
	}

	cfg, _, err := LoadConfig(srv.db)
	if err != nil {
		t.Fatalf("LoadConfig: %v", err)
	}
	if cfg.ProxyPool.SaveLimit != 50 || cfg.ProxyPool.IPSwapLimit != 3 {
		t.Errorf("用户显式值被改动: save_limit=%d ip_swap_limit=%d",
			cfg.ProxyPool.SaveLimit, cfg.ProxyPool.IPSwapLimit)
	}
	if cfg.ConfigVersion != currentConfigVersion {
		t.Errorf("config_version = %d, want %d", cfg.ConfigVersion, currentConfigVersion)
	}
}

func TestMigrateConfig_KeepsExplicitLoginMethod(t *testing.T) {
	srv := newTestServer(t)
	cfg := DefaultConfig()
	cfg.ConfigVersion = 1
	cfg.Accounts = []Account{{
		Name: "GitHub", URL: "https://a.com", LoginMethod: LoginMethodGitHubCookie,
		GithubUserSession: "session",
	}}
	writeCfg(t, srv, cfg)

	if err := MigrateConfig(srv.db); err != nil {
		t.Fatalf("MigrateConfig: %v", err)
	}
	got, _, err := LoadConfig(srv.db)
	if err != nil {
		t.Fatalf("LoadConfig: %v", err)
	}
	if got.Accounts[0].LoginMethod != LoginMethodGitHubCookie {
		t.Fatalf("显式登录方式被覆盖: %q", got.Accounts[0].LoginMethod)
	}
	if got.ConfigVersion != currentConfigVersion {
		t.Fatalf("config_version = %d, want %d", got.ConfigVersion, currentConfigVersion)
	}
}

func TestMigrateConfig_IsIdempotent(t *testing.T) {
	srv := newTestServer(t)
	old := DefaultConfig()
	old.ConfigVersion = 0
	old.ProxyPool.SaveLimit = 100
	old.ProxyPool.IPSwapLimit = 5
	writeCfg(t, srv, old)

	for i := 0; i < 3; i++ {
		if err := MigrateConfig(srv.db); err != nil {
			t.Fatalf("第 %d 次 MigrateConfig: %v", i+1, err)
		}
	}
	cfg, _, err := LoadConfig(srv.db)
	if err != nil {
		t.Fatalf("LoadConfig: %v", err)
	}
	if cfg.ProxyPool.SaveLimit != 0 || cfg.ProxyPool.IPSwapLimit != 10 {
		t.Errorf("重复迁移结果不稳定: save_limit=%d ip_swap_limit=%d",
			cfg.ProxyPool.SaveLimit, cfg.ProxyPool.IPSwapLimit)
	}
}

// 已迁移过的库里，用户后来把值改回 100/5 也不能再被覆盖
func TestMigrateConfig_DoesNotTouchCurrentVersion(t *testing.T) {
	srv := newTestServer(t)
	cur := DefaultConfig()
	cur.ConfigVersion = currentConfigVersion
	cur.ProxyPool.SaveLimit = 100
	cur.ProxyPool.IPSwapLimit = 5
	writeCfg(t, srv, cur)

	if err := MigrateConfig(srv.db); err != nil {
		t.Fatalf("MigrateConfig: %v", err)
	}
	cfg, _, err := LoadConfig(srv.db)
	if err != nil {
		t.Fatalf("LoadConfig: %v", err)
	}
	if cfg.ProxyPool.SaveLimit != 100 || cfg.ProxyPool.IPSwapLimit != 5 {
		t.Errorf("已是当前版本却被改动: save_limit=%d ip_swap_limit=%d",
			cfg.ProxyPool.SaveLimit, cfg.ProxyPool.IPSwapLimit)
	}
}
