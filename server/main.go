/*
server/main.go
NewAPI 签到配置管理平台 · 服务入口

职责：
- 读取环境变量（NCF_DB_PATH / NCF_JWT_SECRET / NCF_ADMIN_USER / NCF_ADMIN_PASS / NCF_HTTP_ADDR）
- 初始化 SQLite（建表、初始管理员、默认配置）
- 注册路由并启动 HTTP 服务
- 托管前端静态文件（web/dist 存在时）

环境变量：
- NCF_DB_PATH    SQLite 文件路径，默认 ./data/config.db
- NCF_JWT_SECRET JWT 签名密钥（至少 32 字符，必填）
- NCF_ADMIN_USER / NCF_ADMIN_PASS 初始管理员账号密码（仅 users 表为空时生效）
- NCF_HTTP_ADDR  监听地址，默认 127.0.0.1:8080
*/
package main

import (
	"crypto/rand"
	"database/sql"
	"encoding/hex"
	"log"
	"net/http"
	"os"
	"time"
)

func main() {
	// 自动加载 .env 文件（若存在），便于宝塔/手动部署：目录里放 .env 即可配置
	if p := findEnvFile(); p != "" {
		loadEnvFile(p)
	}

	dbPath := getenv("NCF_DB_PATH", "./data/config.db")
	addr := getenv("NCF_HTTP_ADDR", "127.0.0.1:8080")

	// JWT 密钥：未设置时自动生成随机密钥，保证开箱即用；
	// 重启后已签发的登录态会失效，生产环境请用环境变量固定。
	jwtSecret := os.Getenv("NCF_JWT_SECRET")
	if jwtSecret == "" {
		jwtSecret = randomHex(32)
		log.Printf("NCF_JWT_SECRET 未设置，已自动生成随机密钥（重启后登录态失效；生产环境请固定该值）")
	} else if len(jwtSecret) < 32 {
		log.Printf("NCF_JWT_SECRET 长度不足 32 字符（当前 %d），已按所设值使用；建议使用至少 32 字符的强密钥", len(jwtSecret))
	}
	adminUser := getenv("NCF_ADMIN_USER", "admin")
	adminPass := getenv("NCF_ADMIN_PASS", "admin123456")

	db, err := OpenDB(dbPath)
	if err != nil {
		log.Fatalf("初始化数据库失败: %v", err)
	}
	defer func() {
		if err := db.Close(); err != nil {
			log.Printf("关闭数据库失败: %v", err)
		}
	}()

	if err := EnsureAdmin(db, adminUser, adminPass); err != nil {
		log.Fatalf("初始化管理员失败: %v", err)
	}
	if err := EnsureDefaultConfig(db); err != nil {
		log.Fatalf("初始化默认配置失败: %v", err)
	}
	// 已有库可能还带着旧默认值（save_limit=100 / ip_swap_limit=5），升级一次
	if err := MigrateConfig(db); err != nil {
		log.Fatalf("升级旧配置失败: %v", err)
	}

	srv := NewServer(db, jwtSecret)
	log.Printf("NewAPI 签到配置管理平台已启动，监听 %s（数据库: %s）", addr, dbPath)
	log.Printf("初始管理员: %s / %s（仅首次创建，建议登录后修改密码）", adminUser, adminPass)
	if dist := findDistDir(); dist != "" {
		log.Printf("托管前端静态文件（外部目录，开发热更新）: %s", dist)
	} else if hasEmbeddedFrontend() {
		log.Printf("前端已嵌入二进制（单文件部署）")
	} else {
		log.Printf("未找到前端静态文件，仅提供 API 服务")
	}

	// 代理池后台刷新：按 proxy_pool.refresh_minutes 周期抓取+测通（可配置，<=0 关闭）
	startProxyRefresher(db, srv.proxies)

	if err := http.ListenAndServe(addr, srv.routes()); err != nil {
		log.Fatalf("HTTP 服务异常退出: %v", err)
	}
}

// startProxyRefresher 启动后台协程：按配置周期刷新代理池。
// refresh_minutes <= 0 时仅保留手动刷新能力。
func startProxyRefresher(db *sql.DB, mgr *ProxyManager) {
	go func() {
		// 启动后立即刷一次，让页面/Actions 第一时间有数据
		cfg, _, err := LoadConfig(db)
		if err == nil {
			if _, rerr := mgr.RefreshProxies(cfg.ProxyPool, cfg.ProxyPool.SaveLimit); rerr != nil {
				log.Printf("[proxy] 启动刷新失败: %v", rerr)
			}
		}
		for {
			cfg, _, err = LoadConfig(db)
			if err != nil {
				time.Sleep(60 * time.Second)
				continue
			}
			interval := cfg.ProxyPool.RefreshMinutes
			if interval <= 0 {
				// 后台刷新关闭：睡长一点再检查（避免空转），随时可被手动触发
				time.Sleep(10 * time.Minute)
				continue
			}
			time.Sleep(time.Duration(interval) * time.Minute)
			if _, rerr := mgr.RefreshProxies(cfg.ProxyPool, cfg.ProxyPool.SaveLimit); rerr != nil {
				log.Printf("[proxy] 周期刷新失败: %v", rerr)
			}
		}
	}()
}

// randomHex 生成 n 字节的随机十六进制字符串。
func randomHex(n int) string {
	buf := make([]byte, n)
	if _, err := rand.Read(buf); err != nil {
		panic(err)
	}
	return hex.EncodeToString(buf)
}

// getenv 读取环境变量，为空时返回默认值。
func getenv(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}
