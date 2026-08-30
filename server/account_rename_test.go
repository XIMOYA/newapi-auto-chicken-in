// server/account_rename_test.go
// 账号改名迁移的测试：改名本身、保活状态跟着走、映射落表、幂等、无改动时不动。
//
// 保活状态跟着走是最要紧的一条 —— 它是整个迁移里唯一会丢数据的地方：
// 配置改成新名而保活表还是旧名，那个账号的保活状态就永久孤立了。
package main

import (
	"database/sql"
	"testing"
)

// seedRenameCase 造一个「待改名账号 + 它的保活状态」的场景。
// 保活行直接用 SQL 插，这里只关心 account_name 这一列跟不跟着改。
func seedRenameCase(t *testing.T, db *sql.DB, accountName, githubName, siteURL string) {
	t.Helper()
	cfg, _, err := LoadConfig(db)
	if err != nil {
		t.Fatalf("读配置: %v", err)
	}
	cfg.GitHubAccounts = []GitHubAccount{{Name: githubName, UserSession: "sess"}}
	cfg.Accounts = []Account{{
		Name:          accountName,
		URL:           siteURL,
		LoginMethod:   LoginMethodTabiAI,
		GitHubAccount: githubName,
	}}
	if _, err := SaveConfig(db, cfg); err != nil {
		t.Fatalf("存配置: %v", err)
	}
	// 先确认新字段真能往返，否则后面的断言会指向错误的方向
	back, _, err := LoadConfig(db)
	if err != nil {
		t.Fatalf("回读配置: %v", err)
	}
	if len(back.GitHubAccounts) != 1 || back.Accounts[0].GitHubAccount != githubName {
		t.Fatalf("新字段没能往返: pool=%d ref=%q",
			len(back.GitHubAccounts), back.Accounts[0].GitHubAccount)
	}
	if _, err := db.Exec(
		`INSERT INTO tabiai_keepalive_state (account_name, state) VALUES (?, ?)`,
		accountName, "ok"); err != nil {
		t.Fatalf("造保活状态: %v", err)
	}
}

// keepaliveNames 列出保活表里现存的账号名。
func keepaliveNames(t *testing.T, db *sql.DB) []string {
	t.Helper()
	rows, err := db.Query(`SELECT account_name FROM tabiai_keepalive_state ORDER BY account_name`)
	if err != nil {
		t.Fatalf("查保活表: %v", err)
	}
	defer rows.Close()
	var out []string
	for rows.Next() {
		var name string
		if err := rows.Scan(&name); err != nil {
			t.Fatalf("扫描: %v", err)
		}
		out = append(out, name)
	}
	return out
}

func TestMigrateAccountNamesRenamesAndCarriesState(t *testing.T) {
	db := newTestServer(t).db
	seedRenameCase(t, db, "旧名A", "Steven", "https://tabiai.cc")

	if err := MigrateAccountNames(db); err != nil {
		t.Fatalf("迁移失败: %v", err)
	}
	const want = "Steven（tabiai.cc）"

	cfg, _, err := LoadConfig(db)
	if err != nil {
		t.Fatalf("读配置: %v", err)
	}
	if cfg.Accounts[0].Name != want {
		t.Errorf("配置里的账号名 = %q, want %q", cfg.Accounts[0].Name, want)
	}
	// 最关键：保活状态必须跟到新名下，旧名那行不能残留
	if names := keepaliveNames(t, db); len(names) != 1 || names[0] != want {
		t.Errorf("保活表里的账号名 = %v, want [%s]", names, want)
	}
	renames, err := LoadAccountRenames(db)
	if err != nil {
		t.Fatalf("读映射: %v", err)
	}
	if len(renames) != 1 || renames[0].OldName != "旧名A" || renames[0].NewName != want {
		t.Errorf("改名映射 = %+v", renames)
	}
}

func TestMigrateAccountNamesIsIdempotent(t *testing.T) {
	db := newTestServer(t).db
	seedRenameCase(t, db, "旧名A", "Steven", "https://tabiai.cc")

	for i := 0; i < 3; i++ {
		if err := MigrateAccountNames(db); err != nil {
			t.Fatalf("第 %d 次迁移失败: %v", i+1, err)
		}
	}
	// 重复跑不能把已规范的名字再套一层括号，也不能堆出重复的保活行或映射
	cfg, _, err := LoadConfig(db)
	if err != nil {
		t.Fatalf("读配置: %v", err)
	}
	if len(cfg.Accounts) != 1 || cfg.Accounts[0].Name != "Steven（tabiai.cc）" {
		t.Errorf("重复迁移后账号 = %+v", cfg.Accounts)
	}
	if names := keepaliveNames(t, db); len(names) != 1 {
		t.Errorf("重复迁移后保活行 = %v", names)
	}
	renames, err := LoadAccountRenames(db)
	if err != nil {
		t.Fatalf("读映射: %v", err)
	}
	if len(renames) != 1 {
		t.Errorf("重复迁移后映射条数 = %d, want 1", len(renames))
	}
}

func TestMigrateAccountNamesSkipsWhenNothingToDo(t *testing.T) {
	db := newTestServer(t).db
	cfg, _, err := LoadConfig(db)
	if err != nil {
		t.Fatalf("读配置: %v", err)
	}
	// 没引用 GitHub 账号的老账号：一个字都不该动，也不该产生映射
	cfg.Accounts = []Account{{
		Name: "手填的名字", URL: "https://a.com", LoginMethod: LoginMethodNewAPICookie,
	}}
	if _, err := SaveConfig(db, cfg); err != nil {
		t.Fatalf("存配置: %v", err)
	}
	if err := MigrateAccountNames(db); err != nil {
		t.Fatalf("迁移失败: %v", err)
	}
	after, _, err := LoadConfig(db)
	if err != nil {
		t.Fatalf("读配置: %v", err)
	}
	if after.Accounts[0].Name != "手填的名字" {
		t.Errorf("不该被改名, 实际 %q", after.Accounts[0].Name)
	}
	renames, err := LoadAccountRenames(db)
	if err != nil {
		t.Fatalf("读映射: %v", err)
	}
	if len(renames) != 0 {
		t.Errorf("不该产生映射, 实际 %+v", renames)
	}
}

func TestMigrateAccountNamesSurvivesCollision(t *testing.T) {
	db := newTestServer(t).db
	cfg, _, err := LoadConfig(db)
	if err != nil {
		t.Fatalf("读配置: %v", err)
	}
	cfg.GitHubAccounts = []GitHubAccount{{Name: "Steven", UserSession: "sess"}}
	// 同一 GitHub 账号 + 同一域名 = 重名。应整笔跳过而不是报错中断启动，
	// 否则用户连打开界面去改这个重名账号的机会都没有
	cfg.Accounts = []Account{
		{Name: "x", URL: "https://a.com", GitHubAccount: "Steven", LoginMethod: LoginMethodTabiAI},
		{Name: "y", URL: "https://a.com/other", GitHubAccount: "Steven", LoginMethod: LoginMethodTabiAI},
	}
	if _, err := SaveConfig(db, cfg); err != nil {
		t.Fatalf("存配置: %v", err)
	}
	if err := MigrateAccountNames(db); err != nil {
		t.Fatalf("重名不该让迁移返回错误（应只 log 并跳过）: %v", err)
	}
	after, _, err := LoadConfig(db)
	if err != nil {
		t.Fatalf("读配置: %v", err)
	}
	if after.Accounts[0].Name != "x" || after.Accounts[1].Name != "y" {
		t.Errorf("重名时不该改名, 实际 %q/%q", after.Accounts[0].Name, after.Accounts[1].Name)
	}
}
