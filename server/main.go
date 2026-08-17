/*
server/main.go
NewAPI 签到配置管理平台 · 服务入口

职责：
- 读取环境变量（NCF_DB_PATH / NCF_JWT_SECRET / NCF_ADMIN_USER / NCF_ADMIN_PASS / NCF_HTTP_ADDR / NCF_ENV）
- 初始化 SQLite（建表、初始管理员、默认配置）
- 注册路由并启动 HTTP 服务（带读/写超时与优雅关闭）
- 托管前端静态文件（web/dist 存在时）

环境变量：
- NCF_DB_PATH    SQLite 文件路径，默认 ./data/config.db
- NCF_JWT_SECRET JWT 签名密钥（至少 32 字符；NCF_ENV=production 时必填）
- NCF_ADMIN_USER / NCF_ADMIN_PASS 初始管理员账号密码（仅 users 表为空时生效；无内置默认弱密码）
- NCF_HTTP_ADDR  监听地址，默认 127.0.0.1:8080
- NCF_ENV        环境标记；production 时强制要求显式设置 NCF_JWT_SECRET
*/
package main

import (
	"context"
	"crypto/rand"
	"database/sql"
	"encoding/hex"
	"errors"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"
)

// resolveJWTSecret 根据环境与已设置的密钥决定最终 JWT 签名密钥：
// - NCF_ENV=production：必须显式提供至少 32 字符密钥，否则报错（绝不随机生成）。
// - 其他环境：未设置时随机生成（调用方不得打印密钥本身）；弱密钥仅警告照常使用。
func resolveJWTSecret(env, secret string) (string, error) {
	if isProduction(env) {
		if len(secret) < 32 {
			return "", fmt.Errorf("NCF_ENV=production 时必须设置 NCF_JWT_SECRET（至少 32 字符）")
		}
		return secret, nil
	}
	if secret == "" {
		return randomHex(32), nil
	}
	return secret, nil
}

// isProduction 判断 NCF_ENV 是否为 production（忽略大小写与空白）。
func isProduction(env string) bool {
	return strings.EqualFold(strings.TrimSpace(env), "production")
}

func main() {
	// 自动加载 .env 文件（若存在），便于宝塔/手动部署：目录里放 .env 即可配置
	if p := findEnvFile(); p != "" {
		loadEnvFile(p)
	}

	dbPath := getenv("NCF_DB_PATH", "./data/config.db")
	addr := getenv("NCF_HTTP_ADDR", "127.0.0.1:8080")

	// JWT 密钥：生产必须显式配置；非生产未设置时随机生成（绝不打印密钥内容）。
	env := os.Getenv("NCF_ENV")
	jwtSecret, err := resolveJWTSecret(env, os.Getenv("NCF_JWT_SECRET"))
	if err != nil {
		log.Fatalf("JWT 密钥配置错误: %v", err)
	}
	if envSecret := os.Getenv("NCF_JWT_SECRET"); !isProduction(env) {
		if envSecret == "" {
			log.Printf("NCF_JWT_SECRET 未设置，已自动生成随机密钥（重启后登录态失效；生产环境请用 NCF_ENV=production + NCF_JWT_SECRET 固定）")
		} else if len(envSecret) < 32 {
			log.Printf("NCF_JWT_SECRET 长度不足 32 字符（当前 %d）；建议使用至少 32 字符的强密钥", len(envSecret))
		}
	}

	// 初始管理员：不再提供默认弱密码。
	// NCF_ADMIN_PASS 未设置时，users 表已有账号则照常启动（升级场景）；
	// users 表为空则直接失败，提示通过环境变量提供初始凭据。
	adminUser := getenv("NCF_ADMIN_USER", "admin")
	adminPass := os.Getenv("NCF_ADMIN_PASS")

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
	// 清理早期还原缺陷留下的 "***" 字面量凭据（幂等，无遗留时不写库）
	if err := SanitizeConfigSecrets(db); err != nil {
		log.Fatalf("清理占位符凭据失败: %v", err)
	}

	srv := NewServer(db, jwtSecret)
	log.Printf("NewAPI 签到配置管理平台已启动，监听 %s（数据库: %s）", addr, dbPath)
	if dist := findDistDir(); dist != "" {
		log.Printf("托管前端静态文件（外部目录，开发热更新）: %s", dist)
	} else if hasEmbeddedFrontend() {
		log.Printf("前端已嵌入二进制（单文件部署）")
	} else {
		log.Printf("未找到前端静态文件，仅提供 API 服务")
	}

	// 代理池后台刷新：按 proxy_pool.refresh_minutes 周期抓取+测通（可配置，<=0 关闭）
	startProxyRefresher(db, srv.proxies)

	// HTTP 服务：统一超时防慢速攻击与连接耗尽
	httpSrv := &http.Server{
		Addr:              addr,
		Handler:           srv.routes(),
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       30 * time.Second,
		WriteTimeout:      120 * time.Second,
		IdleTimeout:       60 * time.Second,
	}

	// 优雅关闭：收到 SIGINT/SIGTERM 后停止接收新连接，等待存量请求完成
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	errCh := make(chan error, 1)
	go func() {
		if err := httpSrv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			errCh <- err
		}
	}()

	select {
	case err := <-errCh:
		log.Fatalf("HTTP 服务异常退出: %v", err)
	case <-ctx.Done():
		log.Printf("收到退出信号，正在优雅关闭 HTTP 服务…")
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		if err := httpSrv.Shutdown(shutdownCtx); err != nil {
			log.Printf("优雅关闭超时或失败: %v", err)
		}
		log.Printf("HTTP 服务已关闭")
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
