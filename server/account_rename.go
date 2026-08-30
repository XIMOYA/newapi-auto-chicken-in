/*
server/account_rename.go
账号改名迁移：把账号名统一成「GitHub 名（站点域名）」，并把认名字的存储一起改过去

为什么不挂在 MigrateConfig 的 config_version 门控里：
那种迁移一辈子只跑一次，但用户会持续在网页端新增账号，新账号同样需要生成规范名字。
所以这里做成每次启动都跑的幂等操作（跟 SanitizeConfigSecrets 一个路子）——
planAccountRenames 会跳过已经规范的账号，没有要改的就直接返回。

改名会让所有「以账号名为键」的存储对不上号，这些必须一起动：
  - tabiai_keepalive_state 表主键  → 保活状态、暂停标记（本文件负责）
  - account_rename_map 表          → 落映射，供客户端改 sessions.json / profile 目录，也是回滚依据
  - sessions.json 的 slug 键        → CF 会话缓存、user_id 缓存（客户端按映射自己改）
  - profile 目录名                  → 手动过盾的成果（同上）
*/
package main

import (
	"database/sql"
	"fmt"
	"log"
	"strings"
)

// ensureAccountRenameTable 建映射表。
//
// 主键是旧名而不是自增 id：同一个旧名重复迁移只会覆盖同一行，天然幂等。
// 留着历史映射不清理 —— 它是唯一能回答「这个账号以前叫什么」的地方，
// 客户端可能隔很久才跑一次，删早了它就找不到自己的旧缓存了。
func ensureAccountRenameTable(db *sql.DB) error {
	_, err := db.Exec(`
		CREATE TABLE IF NOT EXISTS account_rename_map (
			old_name   TEXT PRIMARY KEY,
			new_name   TEXT NOT NULL,
			renamed_at DATETIME DEFAULT CURRENT_TIMESTAMP
		)`)
	return err
}

// AccountRename 一条改名记录。
type AccountRename struct {
	OldName string `json:"old_name"`
	NewName string `json:"new_name"`
}

// LoadAccountRenames 读出全部映射，供客户端同步 sessions.json 与 profile 目录。
func LoadAccountRenames(db *sql.DB) ([]AccountRename, error) {
	if err := ensureAccountRenameTable(db); err != nil {
		return nil, err
	}
	rows, err := db.Query(`SELECT old_name, new_name FROM account_rename_map ORDER BY renamed_at`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []AccountRename
	for rows.Next() {
		var item AccountRename
		if err := rows.Scan(&item.OldName, &item.NewName); err != nil {
			return nil, err
		}
		out = append(out, item)
	}
	return out, rows.Err()
}

// MigrateAccountNames 把账号名规范化，并把认名字的存储一起改过去。幂等，每次启动都可以跑。
//
// 全程在一个事务里：改名改一半是最坏的结果 —— 配置里是新名、保活表里还是旧名，
// 那个账号的保活状态就永久孤立了。宁可整笔回滚，下次启动重试。
func MigrateAccountNames(db *sql.DB) error {
	if err := ensureAccountRenameTable(db); err != nil {
		return err
	}
	cfg, _, err := LoadConfig(db)
	if err != nil {
		return err
	}
	renames, err := planAccountRenames(&cfg)
	if err != nil {
		// 重名等配置问题：报出来但不阻断启动，用户改完配置下次启动自动重试。
		// 平台本身还能用（网页端要能打开，用户才有机会去改那个重名账号）
		log.Printf("[rename] 账号改名计划有问题，本次跳过: %v", err)
		return nil
	}
	if len(renames) == 0 {
		return nil
	}

	tx, err := db.Begin()
	if err != nil {
		return err
	}
	defer func() { _ = tx.Rollback() }() // 已 Commit 的事务再 Rollback 是空操作

	for old, want := range renames {
		// 保活状态跟着账号走。用 UPDATE OR REPLACE：万一新名那行已存在（正常不会，
		// 但配置被手改过就可能），让旧名那行的状态覆盖过去，而不是撞主键直接失败
		if _, err := tx.Exec(
			`UPDATE OR REPLACE tabiai_keepalive_state SET account_name = ? WHERE account_name = ?`,
			want, old); err != nil {
			return fmt.Errorf("迁移保活状态 %q -> %q: %w", old, want, err)
		}
		if _, err := tx.Exec(
			`INSERT INTO account_rename_map (old_name, new_name) VALUES (?, ?)
			 ON CONFLICT(old_name) DO UPDATE SET new_name = excluded.new_name`,
			old, want); err != nil {
			return fmt.Errorf("记录改名映射 %q -> %q: %w", old, want, err)
		}
	}
	if err := tx.Commit(); err != nil {
		return err
	}

	// 配置放在事务外写：SaveConfig 自己管版本号与修订号，塞进上面的事务会打乱它的语义。
	// 万一这一步失败，保活表已经是新名、配置还是旧名 —— 下次启动 planAccountRenames
	// 会重新算出同一份映射，UPDATE OR REPLACE 和 ON CONFLICT 都幂等，重跑能自愈
	for i := range cfg.Accounts {
		if want, ok := renames[cfg.Accounts[i].Name]; ok {
			cfg.Accounts[i].Name = want
		}
	}
	if _, err := SaveConfig(db, cfg); err != nil {
		return fmt.Errorf("写回改名后的配置: %w", err)
	}

	pairs := make([]string, 0, len(renames))
	for old, want := range renames {
		pairs = append(pairs, fmt.Sprintf("%s -> %s", old, want))
	}
	log.Printf("[rename] 账号名已规范化（客户端下次同步时会一起改 sessions.json 与 profile 目录）: %s",
		strings.Join(pairs, "；"))
	return nil
}
