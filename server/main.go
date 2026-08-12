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
	"encoding/hex"
	"log"
	"net/http"
	"os"
)

func main() {
	dbPath := getenv("NCF_DB_PATH", "./data/config.db")
	addr := getenv("NCF_HTTP_ADDR", "127.0.0.1:8080")

	// JWT 密钥：未设置时自动生成随机密钥，保证开箱即用；
	// 重启后已签发的登录态会失效，生产环境请用环境变量固定。
	jwtSecret := os.Getenv("NCF_JWT_SECRET")
	if len(jwtSecret) < 32 {
		jwtSecret = randomHex(32)
		log.Printf("NCF_JWT_SECRET 未设置，已自动生成随机密钥（重启后登录态失效；生产环境请固定该值）")
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

	if err := http.ListenAndServe(addr, srv.routes()); err != nil {
		log.Fatalf("HTTP 服务异常退出: %v", err)
	}
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
