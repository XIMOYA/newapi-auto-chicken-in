/*
server/db.go
NewAPI 签到配置管理平台 · SQLite 数据层

职责：
- 打开 SQLite（modernc.org/sqlite，纯 Go 无 CGO）
- 建表：users / api_keys / config 三张表
- 提供用户、API Key、配置的数据访问函数

数据模型：
- users     : 管理员账号（密码存 bcrypt 哈希）
- api_keys  : 供 Actions 拉取配置用的 API Key（仅存 sha256 哈希 + 前缀，明文创建时只返回一次）
- config    : 单行（id=1）完整明文配置 JSON，updated_at 记录最后保存时间（RFC3339）
*/
package main

import (
	"database/sql"
	"encoding/json"
	"fmt"
	"log"
	"os"
	"path/filepath"
	"strings"
	"time"

	_ "modernc.org/sqlite"
)

// configRowID config 表固定单行的主键。
const configRowID = 1

// User 管理员账号记录。
type User struct {
	ID           int64
	Username     string
	PasswordHash string
	CreatedAt    string
}

// APIKeyRow API Key 记录（库中只存哈希，不存明文）。
type APIKeyRow struct {
	ID         int64
	Name       string
	KeyHash    string
	Prefix     string
	CreatedAt  string
	LastUsedAt *string // 最近一次被 /api/config/raw 使用的时间，未使用过为 nil
}

// OpenDB 打开（必要时创建）SQLite 数据库，并确保表结构就绪。
// path 为数据库文件路径；所在目录不存在时会自动创建。
func OpenDB(path string) (*sql.DB, error) {
	dir := filepath.Dir(path)
	if dir != "" && dir != "." {
		if err := os.MkdirAll(dir, 0o755); err != nil {
			return nil, fmt.Errorf("创建数据库目录: %w", err)
		}
	}

	dsn := path
	if !strings.Contains(dsn, "?") {
		// busy_timeout 避免写锁竞争时立即报错；WAL 提升并发读写体验
		dsn += "?_pragma=busy_timeout(5000)&_pragma=journal_mode(WAL)"
	}
	db, err := sql.Open("sqlite", dsn)
	if err != nil {
		return nil, fmt.Errorf("打开数据库: %w", err)
	}
	// 配置管理平台并发量低，单连接串行读写最稳妥，避免 SQLITE_BUSY
	db.SetMaxOpenConns(1)

	if err := db.Ping(); err != nil {
		_ = db.Close()
		return nil, fmt.Errorf("连接数据库: %w", err)
	}
	if err := createSchema(db); err != nil {
		_ = db.Close()
		return nil, err
	}
	return db, nil
}

// createSchema 创建 users / api_keys / config 三张表（幂等）。
func createSchema(db *sql.DB) error {
	stmts := []string{
		`CREATE TABLE IF NOT EXISTS users (
			id            INTEGER PRIMARY KEY AUTOINCREMENT,
			username      TEXT    NOT NULL UNIQUE,
			password_hash TEXT    NOT NULL,
			created_at    TEXT    NOT NULL
		)`,
		`CREATE TABLE IF NOT EXISTS api_keys (
			id           INTEGER PRIMARY KEY AUTOINCREMENT,
			name         TEXT    NOT NULL,
			key_hash     TEXT    NOT NULL UNIQUE,
			prefix       TEXT    NOT NULL,
			created_at   TEXT    NOT NULL,
			last_used_at TEXT
		)`,
		`CREATE TABLE IF NOT EXISTS config (
			id         INTEGER PRIMARY KEY CHECK (id = ` + fmt.Sprint(configRowID) + `),
			data       TEXT NOT NULL,
			updated_at TEXT NOT NULL
		)`,
	}
	for _, st := range stmts {
		if _, err := db.Exec(st); err != nil {
			return fmt.Errorf("建表失败: %w", err)
		}
	}
	if err := createProxiesTable(db); err != nil {
		return err
	}
	return nil
}

// ---------------------------------------------------------------------------
// 用户
// ---------------------------------------------------------------------------

// EnsureAdmin 在 users 表为空时创建初始管理员（密码以 bcrypt 哈希落库）。
// users 表已有数据时直接忽略，避免覆盖已有账号。
func EnsureAdmin(db *sql.DB, username, password string) error {
	var count int
	if err := db.QueryRow(`SELECT COUNT(*) FROM users`).Scan(&count); err != nil {
		return fmt.Errorf("检查用户表: %w", err)
	}
	if count > 0 {
		return nil
	}
	if username == "" || password == "" {
		return fmt.Errorf("users 表为空且未提供初始管理员凭据，请设置 NCF_ADMIN_USER / NCF_ADMIN_PASS")
	}
	hash, err := HashPassword(password)
	if err != nil {
		return fmt.Errorf("生成密码哈希: %w", err)
	}
	if _, err := db.Exec(
		`INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)`,
		username, hash, time.Now().UTC().Format(time.RFC3339),
	); err != nil {
		return fmt.Errorf("创建初始管理员: %w", err)
	}
	return nil
}

// GetUserByUsername 按用户名查询用户；不存在时返回 (nil, nil)。
func GetUserByUsername(db *sql.DB, username string) (*User, error) {
	var u User
	err := db.QueryRow(
		`SELECT id, username, password_hash, created_at FROM users WHERE username = ?`, username,
	).Scan(&u.ID, &u.Username, &u.PasswordHash, &u.CreatedAt)
	if err == sql.ErrNoRows {
		return nil, nil
	}
	if err != nil {
		return nil, fmt.Errorf("查询用户: %w", err)
	}
	return &u, nil
}

// UpdateUserPassword 更新指定用户的密码哈希。
func UpdateUserPassword(db *sql.DB, userID int64, passwordHash string) error {
	if _, err := db.Exec(`UPDATE users SET password_hash = ? WHERE id = ?`, passwordHash, userID); err != nil {
		return fmt.Errorf("更新密码: %w", err)
	}
	return nil
}

// ---------------------------------------------------------------------------
// API Key
// ---------------------------------------------------------------------------

// ListAPIKeys 返回全部 API Key（按创建顺序），不含明文。
func ListAPIKeys(db *sql.DB) ([]APIKeyRow, error) {
	rows, err := db.Query(
		`SELECT id, name, key_hash, prefix, created_at, last_used_at FROM api_keys ORDER BY id`,
	)
	if err != nil {
		return nil, fmt.Errorf("查询 API Key 列表: %w", err)
	}
	defer rows.Close()

	keys := make([]APIKeyRow, 0)
	for rows.Next() {
		var k APIKeyRow
		if err := rows.Scan(&k.ID, &k.Name, &k.KeyHash, &k.Prefix, &k.CreatedAt, &k.LastUsedAt); err != nil {
			return nil, fmt.Errorf("读取 API Key 行: %w", err)
		}
		keys = append(keys, k)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("遍历 API Key: %w", err)
	}
	return keys, nil
}

// CreateAPIKey 插入一条 API Key 记录（仅哈希与前缀），返回自增 ID。
func CreateAPIKey(db *sql.DB, name, keyHash, prefix string) (int64, error) {
	res, err := db.Exec(
		`INSERT INTO api_keys (name, key_hash, prefix, created_at) VALUES (?, ?, ?, ?)`,
		name, keyHash, prefix, time.Now().UTC().Format(time.RFC3339),
	)
	if err != nil {
		return 0, fmt.Errorf("创建 API Key: %w", err)
	}
	id, err := res.LastInsertId()
	if err != nil {
		return 0, fmt.Errorf("读取 API Key ID: %w", err)
	}
	return id, nil
}

// DeleteAPIKey 按 ID 删除 API Key；返回是否确有删除（false 表示不存在）。
func DeleteAPIKey(db *sql.DB, id int64) (bool, error) {
	res, err := db.Exec(`DELETE FROM api_keys WHERE id = ?`, id)
	if err != nil {
		return false, fmt.Errorf("删除 API Key: %w", err)
	}
	n, err := res.RowsAffected()
	if err != nil {
		return false, fmt.Errorf("读取删除结果: %w", err)
	}
	return n > 0, nil
}

// GetAPIKeyByHash 按哈希查询 API Key（用于 Bearer 鉴权）；不存在返回 (nil, nil)。
func GetAPIKeyByHash(db *sql.DB, keyHash string) (*APIKeyRow, error) {
	var k APIKeyRow
	err := db.QueryRow(
		`SELECT id, name, key_hash, prefix, created_at, last_used_at FROM api_keys WHERE key_hash = ?`,
		keyHash,
	).Scan(&k.ID, &k.Name, &k.KeyHash, &k.Prefix, &k.CreatedAt, &k.LastUsedAt)
	if err == sql.ErrNoRows {
		return nil, nil
	}
	if err != nil {
		return nil, fmt.Errorf("按哈希查询 API Key: %w", err)
	}
	return &k, nil
}

// UpdateAPIKeyLastUsed 记录 API Key 最近使用时间（成功通过鉴权时调用）。
func UpdateAPIKeyLastUsed(db *sql.DB, id int64) error {
	if _, err := db.Exec(
		`UPDATE api_keys SET last_used_at = ? WHERE id = ?`,
		time.Now().UTC().Format(time.RFC3339), id,
	); err != nil {
		return fmt.Errorf("更新 API Key 使用时间: %w", err)
	}
	return nil
}

// ---------------------------------------------------------------------------
// 配置
// ---------------------------------------------------------------------------

// EnsureDefaultConfig 在 config 表无记录时写入默认配置（幂等）。
func EnsureDefaultConfig(db *sql.DB) error {
	var id int
	err := db.QueryRow(`SELECT id FROM config WHERE id = ?`, configRowID).Scan(&id)
	if err == nil {
		return nil
	}
	if err != sql.ErrNoRows {
		return fmt.Errorf("检查配置表: %w", err)
	}
	if _, err := SaveConfig(db, DefaultConfig()); err != nil {
		return err
	}
	return nil
}

// MigrateConfig 一次性升级旧配置里过时的默认值（幂等，靠 config_version 判定）。
//
// v0 -> v1：
//   - proxy_pool.save_limit  旧默认 100 -> 0（不限制，上游有多少可用就存多少）
//   - proxy_pool.ip_swap_limit 旧默认 5 -> 10
//
// 只在 config_version 缺失/为 0 时跑一次，跑完就写入版本号；之后用户在界面上
// 改成什么都不会再被覆盖。旧库里这两个值等于旧默认值时才动，用户显式改成别的
// 值一律保留。
func MigrateConfig(db *sql.DB) error {
	cfg, _, err := LoadConfig(db)
	if err != nil {
		return err
	}
	if cfg.ConfigVersion >= currentConfigVersion {
		return nil
	}
	var changed []string
	if cfg.ProxyPool.SaveLimit == 100 {
		cfg.ProxyPool.SaveLimit = 0
		changed = append(changed, "save_limit 100 -> 0（不限制）")
	}
	if cfg.ProxyPool.IPSwapLimit == 5 {
		cfg.ProxyPool.IPSwapLimit = 10
		changed = append(changed, "ip_swap_limit 5 -> 10")
	}
	cfg.ConfigVersion = currentConfigVersion
	if _, err := SaveConfig(db, cfg); err != nil {
		return err
	}
	if len(changed) > 0 {
		log.Printf("[config] 旧配置默认值已升级: %s", strings.Join(changed, "；"))
	}
	return nil
}

// LoadConfig 读取当前配置与其更新时间（RFC3339 字符串）。
func LoadConfig(db *sql.DB) (Config, string, error) {
	var data, updatedAt string
	err := db.QueryRow(`SELECT data, updated_at FROM config WHERE id = ?`, configRowID).
		Scan(&data, &updatedAt)
	if err != nil {
		return Config{}, "", fmt.Errorf("读取配置: %w", err)
	}
	var cfg Config
	if err := json.Unmarshal([]byte(data), &cfg); err != nil {
		return Config{}, "", fmt.Errorf("解析配置 JSON: %w", err)
	}
	return cfg, updatedAt, nil
}

// SaveConfig 以「完整替换」方式写入配置（单行 id=1），返回新的 updated_at。
func SaveConfig(db *sql.DB, cfg Config) (string, error) {
	data, err := json.Marshal(cfg)
	if err != nil {
		return "", fmt.Errorf("序列化配置: %w", err)
	}
	updatedAt := time.Now().UTC().Format(time.RFC3339)
	_, err = db.Exec(
		`INSERT INTO config (id, data, updated_at) VALUES (?, ?, ?)
		 ON CONFLICT(id) DO UPDATE SET data = excluded.data, updated_at = excluded.updated_at`,
		configRowID, string(data), updatedAt,
	)
	if err != nil {
		return "", fmt.Errorf("保存配置: %w", err)
	}
	return updatedAt, nil
}
