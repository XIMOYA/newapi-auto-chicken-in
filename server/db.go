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
	"errors"
	"fmt"
	"log"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	_ "modernc.org/sqlite"
)

// configRowID config 表固定单行的主键。
const configRowID = 1

// minPasswordLen 管理员密码长度下限。初始管理员（NCF_ADMIN_PASS）与改密码接口
// 共用同一条规则 —— 否则首次部署能设 4 位数字，改密码却要求 8 位，规则自相矛盾，
// 而登录限流只有「同 IP+用户名 1 分钟 5 次」的强度，短口令仍可被穷举。
const minPasswordLen = 8

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

// createSchema 创建 users / api_keys / config / proxies / run_state 表（幂等）。
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
			updated_at TEXT NOT NULL,
			revision   INTEGER NOT NULL DEFAULT 0
		)`,
	}
	for _, st := range stmts {
		if _, err := db.Exec(st); err != nil {
			return fmt.Errorf("建表失败: %w", err)
		}
	}
	if err := migrateConfigRevisionColumn(db); err != nil {
		return err
	}
	if err := createProxiesTable(db); err != nil {
		return err
	}
	if err := createProxyFeedbackTable(db); err != nil {
		return err
	}
	if err := createRunStateTable(db); err != nil {
		return err
	}
	if err := createTabiAIKeepaliveTables(db); err != nil {
		return err
	}
	return nil
}

// migrateConfigRevisionColumn 老库补 config.revision 列（幂等）。
// revision 是乐观锁版本号：updated_at 只有秒级精度，同一秒内的两次保存无法区分，
// 因此并发控制必须用单调递增的整数而不是时间戳。
func migrateConfigRevisionColumn(db *sql.DB) error {
	cols, err := db.Query(`PRAGMA table_info(config)`)
	if err != nil {
		return nil // 查不到表信息时交由后续语句报错，不在这里阻断启动
	}
	hasRevision := false
	for cols.Next() {
		var cid int
		var cname, ctype string
		var notnull, pk int
		var dflt any
		if cols.Scan(&cid, &cname, &ctype, &notnull, &dflt, &pk) == nil && cname == "revision" {
			hasRevision = true
		}
	}
	cols.Close()
	if hasRevision {
		return nil
	}
	if _, err := db.Exec(`ALTER TABLE config ADD COLUMN revision INTEGER NOT NULL DEFAULT 0`); err != nil {
		return fmt.Errorf("迁移 config.revision 列失败: %w", err)
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
	if len(password) < minPasswordLen {
		return fmt.Errorf("NCF_ADMIN_PASS 至少需要 %d 个字符（当前 %d）", minPasswordLen, len(password))
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
// v0 -> v2：
//   - proxy_pool.save_limit  旧默认 100 -> 0（不限制，上游有多少可用就存多少）
//   - proxy_pool.ip_swap_limit 旧默认 5 -> 10
//   - accounts[].login_method 缺失 -> newapi_cookie
//
// 只在配置版本低于当前版本时运行，跑完就写入版本号；之后用户在界面上
// 改成什么都不会再被覆盖。旧库里代理池字段等于旧默认值时才动，用户显式改成别的
// 值一律保留；登录方式只有缺失时才补默认值。
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
	for i := range cfg.Accounts {
		if strings.TrimSpace(cfg.Accounts[i].LoginMethod) == "" {
			cfg.Accounts[i].LoginMethod = LoginMethodNewAPICookie
			changed = append(changed, fmt.Sprintf("accounts[%d].login_method -> %s", i, LoginMethodNewAPICookie))
		}
		// v3：GitHub OAuth 不再是登录方式。这类账号本就是 TaBiAI 站点，改判为 tabiai；
		// 保留 github_user_session，用户可用「签发 cookie」小工具一键取回 new_api_refresh。
		if strings.EqualFold(strings.TrimSpace(cfg.Accounts[i].LoginMethod), legacyLoginMethodGitHubCookie) {
			cfg.Accounts[i].LoginMethod = LoginMethodTabiAI
			changed = append(changed, fmt.Sprintf(
				"accounts[%d].login_method %s -> %s（需要签发一次 new_api_refresh）",
				i, legacyLoginMethodGitHubCookie, LoginMethodTabiAI))
		}
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

// SanitizeConfigSecrets 清理库里遗留的 "***" 字面量敏感字段（每次启动都跑，幂等）。
//
// 早期 UnmaskConfig 在账号改名后找不到旧值时会把占位符原样落库，结果界面继续显示
// 「已设置」而签到实际拿 "***" 当凭据，属于静默数据损坏。这里把它们清空，让界面
// 如实显示「未设置」，提醒用户重新填写。无需清理时不写库。
//
// 不放进 MigrateConfig 是因为后者有配置版本门槛，已升到当前版本的库不会再执行，
// 而损坏数据与配置版本无关。
func SanitizeConfigSecrets(db *sql.DB) error {
	cfg, _, err := LoadConfig(db)
	if err != nil {
		return err
	}
	cleaned := SanitizeMaskLeftovers(&cfg)
	filled := ensureGitHubFingerprints(&cfg)
	if len(cleaned) == 0 && len(filled) == 0 {
		return nil
	}
	if _, err := SaveConfig(db, cfg); err != nil {
		return err
	}
	if len(cleaned) > 0 {
		log.Printf("[config] 已清理遗留的占位符敏感字段（需要重新填写）: %s", strings.Join(cleaned, "、"))
	}
	if len(filled) > 0 {
		// 老配置里的池子账号没有指纹 seed，补上之后它们在 GitHub 眼里就各是一台设备。
		// seed 由账号名派生，所以这次补齐算出来的值与将来任何一次重算都一致
		log.Printf("[config] 已为 GitHub 账号分配固定客户端指纹: %s", strings.Join(filled, "、"))
	}
	return nil
}

// LoadConfig 读取当前配置与其更新时间（RFC3339 字符串）。
func LoadConfig(db *sql.DB) (Config, string, error) {
	cfg, updatedAt, _, err := LoadConfigWithRevision(db)
	return cfg, updatedAt, err
}

// LoadConfigWithRevision 额外返回乐观锁版本号，供 PUT /api/config 做冲突检测。
func LoadConfigWithRevision(db *sql.DB) (Config, string, int64, error) {
	var data, updatedAt string
	var revision int64
	err := db.QueryRow(`SELECT data, updated_at, revision FROM config WHERE id = ?`, configRowID).
		Scan(&data, &updatedAt, &revision)
	if err != nil {
		return Config{}, "", 0, fmt.Errorf("读取配置: %w", err)
	}
	var cfg Config
	if err := json.Unmarshal([]byte(data), &cfg); err != nil {
		return Config{}, "", 0, fmt.Errorf("解析配置 JSON: %w", err)
	}
	return cfg, updatedAt, revision, nil
}

// ErrConfigRevisionConflict 期望版本与库中当前版本不一致：调用方应转 409 并让用户重新加载。
var ErrConfigRevisionConflict = errors.New("配置已被他人修改")

// configWriteMu 串行化所有「读-改-写」式的整份配置写入，以及 cookie 的定点更新。
//
// 光锁写入是不够的：陈旧快照覆盖是「读到写之间」的竞态，
// 必须在持锁范围内重新读库，才能保证不抹掉这期间落库的轮转凭据。
var configWriteMu sync.Mutex

// SaveConfigKeepingCookies 无条件写入，但持锁重读后保留 tabiai 账号在库中的 cookie。
//
// 用于「不带 revision 的 PUT」与 import：这两条路径的请求体可能是很久以前的快照
// （导出文件、外部脚本缓存），而 TaBiAI 的 new_api_refresh 由后台签到持续轮转。
// 写回旧代次会触发站点重放检测，导致整条会话被撤销、必须人工重新签发 ——
// 代价远大于「这两条路径不能改 tabiai 凭据」的不便。
//
// 只保护 login_method=tabiai 的账号：newapi_cookie 的 session 是静态凭据，
// 不存在轮转，用户通过这两条路径修改它是正当操作，不该被拦。
// 库中没有同名账号时（新增账号）一律采用请求体的值。
//
// 要显式修改 tabiai 凭据请走带 revision 的 PUT、issue-cookie 或 refresh-cookie。
func SaveConfigKeepingCookies(db *sql.DB, cfg Config) (string, error) {
	configWriteMu.Lock()
	defer configWriteMu.Unlock()
	return saveConfigKeepingCookiesLocked(db, cfg)
}

// saveConfigKeepingCookiesLocked 是 SaveConfigKeepingCookies 的实现体，不自己加锁。
// 已持有 configWriteMu 的调用方（账号 ops）必须走这里，否则会自死锁。
func saveConfigKeepingCookiesLocked(db *sql.DB, cfg Config) (string, error) {
	stored, _, err := loadConfigLocked(db)
	if err != nil {
		return "", err
	}
	// 只收集会轮转的那批账号（tabiai），其余按请求体原样写入
	keepCookieByName := make(map[string]string, len(stored.Accounts))
	for _, a := range stored.Accounts {
		if a.Name == "" || !strings.EqualFold(strings.TrimSpace(a.LoginMethod), LoginMethodTabiAI) {
			continue
		}
		keepCookieByName[a.Name] = a.Cookie
	}
	var kept []string
	for i := range cfg.Accounts {
		old, ok := keepCookieByName[cfg.Accounts[i].Name]
		if !ok {
			continue
		}
		if cfg.Accounts[i].Cookie != old {
			kept = append(kept, cfg.Accounts[i].Name)
		}
		cfg.Accounts[i].Cookie = old
	}
	if len(kept) > 0 {
		log.Printf("[config] 已保留库中现有的 TaBiAI 凭据，忽略请求体里的旧值（账号: %s）；"+
			"要改凭据请用带 revision 的保存、签发或回写接口", strings.Join(kept, ", "))
	}
	// 兜底：任何路径漏掉 UnmaskConfig 时，"***" 也不该落库
	if cleaned := SanitizeMaskLeftovers(&cfg); len(cleaned) > 0 {
		log.Printf("[config] 拦截到未还原的占位符并清空: %s", strings.Join(cleaned, ", "))
	}
	// 出口绑定是服务端运行状态：提交上来的值一律丢掉，用库里的。
	// 出站时它被 proxyDisplay 脱敏过，原样存回去会把绑定写成一条假地址
	keepGitHubRuntimeFields(&cfg, stored)
	// 新加的池子账号在这里就把指纹 seed 补上，不必等下次重启
	if filled := ensureGitHubFingerprints(&cfg); len(filled) > 0 {
		log.Printf("[config] 已为 GitHub 账号分配固定客户端指纹: %s", strings.Join(filled, ", "))
	}
	return saveConfigLocked(db, cfg)
}

// SaveConfig 以「完整替换」方式无条件写入配置（单行 id=1），返回新的 updated_at。
//
// 仅供启动期迁移（MigrateConfig / SanitizeConfigSecrets / EnsureDefaultConfig）与测试使用：
// 那时进程单线程、还没开始接受请求，不存在并发写；而且迁移的职责本身就是改写字段，
// 走 SaveConfigKeepingCookies 的「保留库中 cookie」逻辑会与其意图冲突。
//
// 运行期的写入一律不要用它：来自 Web 的保存走 SaveConfigIfMatch（乐观锁），
// 不带 revision 的 PUT 与 import 走 SaveConfigKeepingCookies（持锁重读 + 保留凭据）。
func SaveConfig(db *sql.DB, cfg Config) (string, error) {
	return saveConfigLocked(db, cfg)
}

// saveConfigLocked 是 SaveConfig 的实现体，不自己加锁。
// 已持有 configWriteMu 的调用方（SaveConfigKeepingCookies / updateAccountCookie）
// 必须走这里，否则会自死锁。
func saveConfigLocked(db *sql.DB, cfg Config) (string, error) {
	data, err := json.Marshal(cfg)
	if err != nil {
		return "", fmt.Errorf("序列化配置: %w", err)
	}
	updatedAt := time.Now().UTC().Format(time.RFC3339)
	_, err = db.Exec(
		`INSERT INTO config (id, data, updated_at, revision) VALUES (?, ?, ?, 1)
		 ON CONFLICT(id) DO UPDATE SET data = excluded.data,
		   updated_at = excluded.updated_at, revision = config.revision + 1`,
		configRowID, string(data), updatedAt,
	)
	if err != nil {
		return "", fmt.Errorf("保存配置: %w", err)
	}
	return updatedAt, nil
}

// loadConfigLocked 读取配置，不自己加锁；供已持锁的调用方在锁内重读用。
func loadConfigLocked(db *sql.DB) (Config, string, error) {
	cfg, updatedAt, _, err := LoadConfigWithRevision(db)
	return cfg, updatedAt, err
}

// saveConfigLockedKeepRevision 写入配置但**不推进 revision**（调用方需已持有 configWriteMu）。
//
// 只给凭据轮转用。revision 是给「用户可见的编辑」做乐观锁的，而 TaBiAI 的
// new_api_refresh 由后台签到持续更新、在界面上永远显示为 "***" —— 用户既看不到也
// 管不着。若轮转也 bump 版本号，跑一轮 Cookie 检测就能让所有打开的编辑页全部失效
// （每个 tabiai 账号轮转一次 bump 一次），用户改的明明是 AI 超时这类无关字段。
//
// 不 bump 之后各写入路径各有防线，不共用一套兜底：
//   - 带 revision 的 PUT：handlePutConfig 会重新读最新 oldCfg，UnmaskConfig 把前端提交的
//     "***" 还原成轮转后的新值（前端提交的 cookie 恒为占位符），所以版本号没变也不会写回旧代次
//   - 不带 revision 的 PUT、import、账号 ops：靠 SaveConfigKeepingCookies 保留库中现值
func saveConfigLockedKeepRevision(db *sql.DB, cfg Config) (string, error) {
	data, err := json.Marshal(cfg)
	if err != nil {
		return "", fmt.Errorf("序列化配置: %w", err)
	}
	updatedAt := time.Now().UTC().Format(time.RFC3339)
	// 只更新 data 与 updated_at；revision 原样保留
	res, err := db.Exec(
		`UPDATE config SET data = ?, updated_at = ? WHERE id = ?`,
		string(data), updatedAt, configRowID,
	)
	if err != nil {
		return "", fmt.Errorf("保存配置: %w", err)
	}
	affected, err := res.RowsAffected()
	if err != nil {
		return "", fmt.Errorf("保存配置: %w", err)
	}
	if affected == 0 {
		// 配置行还不存在（首次运行尚未初始化）：退回带 bump 的插入
		return saveConfigLocked(db, cfg)
	}
	return updatedAt, nil
}

// SaveConfigIfMatch 仅当库中 revision 等于 expected 时写入（乐观锁）。
// 版本不匹配返回 ErrConfigRevisionConflict，调用方据此返回 409 并回传最新配置。
func SaveConfigIfMatch(db *sql.DB, cfg Config, expected int64) (string, int64, error) {
	data, err := json.Marshal(cfg)
	if err != nil {
		return "", 0, fmt.Errorf("序列化配置: %w", err)
	}
	updatedAt := time.Now().UTC().Format(time.RFC3339)
	res, err := db.Exec(
		`UPDATE config SET data = ?, updated_at = ?, revision = revision + 1
		 WHERE id = ? AND revision = ?`,
		string(data), updatedAt, configRowID, expected,
	)
	if err != nil {
		return "", 0, fmt.Errorf("保存配置: %w", err)
	}
	affected, err := res.RowsAffected()
	if err != nil {
		return "", 0, fmt.Errorf("保存配置: %w", err)
	}
	if affected == 0 {
		return "", 0, ErrConfigRevisionConflict
	}
	return updatedAt, expected + 1, nil
}
