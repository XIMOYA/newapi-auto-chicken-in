/*
server/handlers.go
NewAPI 签到配置管理平台 · HTTP 处理器与路由

职责：
- Server 结构体（持有 DB 连接与 JWT 密钥）
- 全部 HTTP 路由注册（Go 1.22+ 方法路由）
- 各接口 handler：health / login / config(读写+raw) / keys / export / password
- 通用 JSON 读写辅助、前端静态文件托管（web/dist 存在则 serve，否则 JSON 提示）
*/
package main

import (
	"database/sql"
	"encoding/json"
	"log"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

// serverVersion 健康检查接口返回的版本号。
const serverVersion = "1.0.0"

// Server 服务依赖：数据库连接与 JWT 签名密钥。
type Server struct {
	db        *sql.DB
	jwtSecret []byte
}

// NewServer 构造服务实例；jwtSecret 为 JWT 签名密钥（main 已校验长度）。
func NewServer(db *sql.DB, jwtSecret string) *Server {
	return &Server{db: db, jwtSecret: []byte(jwtSecret)}
}

// routes 注册全部 HTTP 路由并返回根 Handler。
// API 路由优先匹配；"/" 兜底交给静态文件处理器。
func (s *Server) routes() http.Handler {
	mux := http.NewServeMux()

	mux.HandleFunc("GET /api/health", s.handleHealth)
	mux.HandleFunc("POST /api/login", s.handleLogin)

	mux.HandleFunc("GET /api/config", s.requireJWT(s.handleGetConfig))
	mux.HandleFunc("PUT /api/config", s.requireJWT(s.handlePutConfig))
	mux.HandleFunc("GET /api/config/raw", s.requireAPIKey(s.handleRawConfig))

	mux.HandleFunc("GET /api/keys", s.requireJWT(s.handleListKeys))
	mux.HandleFunc("POST /api/keys", s.requireJWT(s.handleCreateKey))
	mux.HandleFunc("DELETE /api/keys/{id}", s.requireJWT(s.handleDeleteKey))

	mux.HandleFunc("GET /api/export", s.requireJWT(s.handleExport))
	mux.HandleFunc("PUT /api/password", s.requireJWT(s.handlePassword))

	mux.Handle("/", s.staticHandler())
	return mux
}

// ---------------------------------------------------------------------------
// 健康检查 / 登录
// ---------------------------------------------------------------------------

// handleHealth GET /api/health —— 无需鉴权。
func (s *Server) handleHealth(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{
		"ok":      true,
		"version": serverVersion,
		"time":    time.Now().UTC().Format(time.RFC3339),
	})
}

// handleLogin POST /api/login —— 无需鉴权，校验账号密码后签发 JWT。
func (s *Server) handleLogin(w http.ResponseWriter, r *http.Request) {
	var req struct {
		Username string `json:"username"`
		Password string `json:"password"`
	}
	if err := readJSON(w, r, &req); err != nil {
		writeError(w, http.StatusBadRequest, "请求体不是合法的 JSON")
		return
	}
	if strings.TrimSpace(req.Username) == "" || req.Password == "" {
		writeError(w, http.StatusBadRequest, "用户名和密码不能为空")
		return
	}

	user, err := GetUserByUsername(s.db, req.Username)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "服务器内部错误")
		return
	}
	if user == nil || !CheckPassword(user.PasswordHash, req.Password) {
		writeError(w, http.StatusUnauthorized, "用户名或密码错误")
		return
	}

	token, expiresIn, err := SignToken(user.Username, s.jwtSecret)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "服务器内部错误")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"token":      token,
		"username":   user.Username,
		"expires_in": expiresIn,
	})
}

// ---------------------------------------------------------------------------
// 配置
// ---------------------------------------------------------------------------

// handleGetConfig GET /api/config（JWT）—— 返回打码后的配置与更新时间。
func (s *Server) handleGetConfig(w http.ResponseWriter, r *http.Request) {
	cfg, updatedAt, err := LoadConfig(s.db)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "服务器内部错误")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"config":     MaskConfig(&cfg),
		"updated_at": updatedAt,
	})
}

// handlePutConfig PUT /api/config（JWT）—— 还原 "***" 占位符、校验后落库。
func (s *Server) handlePutConfig(w http.ResponseWriter, r *http.Request) {
	var req struct {
		Config *Config `json:"config"`
	}
	if err := readJSON(w, r, &req); err != nil {
		writeError(w, http.StatusBadRequest, "请求体不是合法的 JSON")
		return
	}
	if req.Config == nil {
		writeError(w, http.StatusBadRequest, "config 不能为空")
		return
	}

	oldCfg, _, err := LoadConfig(s.db)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "服务器内部错误")
		return
	}
	merged := UnmaskConfig(req.Config, &oldCfg)
	if err := ValidateConfig(merged); err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}

	updatedAt, err := SaveConfig(s.db, *merged)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "服务器内部错误")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"ok":         true,
		"updated_at": updatedAt,
	})
}

// handleRawConfig GET /api/config/raw（API Key）—— 直接返回完整明文配置对象（非包裹结构）。
func (s *Server) handleRawConfig(w http.ResponseWriter, r *http.Request) {
	cfg, _, err := LoadConfig(s.db)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "服务器内部错误")
		return
	}
	writeJSON(w, http.StatusOK, cfg)
}

// ---------------------------------------------------------------------------
// API Key 管理
// ---------------------------------------------------------------------------

// handleListKeys GET /api/keys（JWT）—— 返回 Key 列表，仅含前缀不含明文。
func (s *Server) handleListKeys(w http.ResponseWriter, r *http.Request) {
	keys, err := ListAPIKeys(s.db)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "服务器内部错误")
		return
	}
	items := make([]map[string]any, 0, len(keys))
	for _, k := range keys {
		items = append(items, map[string]any{
			"id":           k.ID,
			"name":         k.Name,
			"prefix":       k.Prefix,
			"created_at":   k.CreatedAt,
			"last_used_at": k.LastUsedAt,
		})
	}
	writeJSON(w, http.StatusOK, map[string]any{"keys": items})
}

// handleCreateKey POST /api/keys（JWT）—— 创建 API Key，明文仅此一次返回。
func (s *Server) handleCreateKey(w http.ResponseWriter, r *http.Request) {
	var req struct {
		Name string `json:"name"`
	}
	if err := readJSON(w, r, &req); err != nil {
		writeError(w, http.StatusBadRequest, "请求体不是合法的 JSON")
		return
	}
	name := strings.TrimSpace(req.Name)
	if name == "" {
		writeError(w, http.StatusBadRequest, "name 不能为空")
		return
	}

	plain, hash, prefix, err := GenerateAPIKey()
	if err != nil {
		writeError(w, http.StatusInternalServerError, "服务器内部错误")
		return
	}
	id, err := CreateAPIKey(s.db, name, hash, prefix)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "服务器内部错误")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"id":   id,
		"name": name,
		"key":  plain,
	})
}

// handleDeleteKey DELETE /api/keys/{id}（JWT）—— 删除指定 API Key。
func (s *Server) handleDeleteKey(w http.ResponseWriter, r *http.Request) {
	id, err := strconv.ParseInt(r.PathValue("id"), 10, 64)
	if err != nil || id <= 0 {
		writeError(w, http.StatusBadRequest, "id 必须是正整数")
		return
	}
	ok, err := DeleteAPIKey(s.db, id)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "服务器内部错误")
		return
	}
	if !ok {
		writeError(w, http.StatusNotFound, "API Key 不存在")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true})
}

// ---------------------------------------------------------------------------
// 导出 / 修改密码
// ---------------------------------------------------------------------------

// handleExport GET /api/export（JWT）—— 返回完整明文配置的 JSON 字符串。
func (s *Server) handleExport(w http.ResponseWriter, r *http.Request) {
	cfg, _, err := LoadConfig(s.db)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "服务器内部错误")
		return
	}
	data, err := json.Marshal(cfg)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "服务器内部错误")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"json": string(data)})
}

// handlePassword PUT /api/password（JWT）—— 校验旧密码后更新密码。
func (s *Server) handlePassword(w http.ResponseWriter, r *http.Request) {
	var req struct {
		OldPassword string `json:"old_password"`
		NewPassword string `json:"new_password"`
	}
	if err := readJSON(w, r, &req); err != nil {
		writeError(w, http.StatusBadRequest, "请求体不是合法的 JSON")
		return
	}

	username, _ := r.Context().Value(ctxKeyUsername).(string)
	user, err := GetUserByUsername(s.db, username)
	if err != nil || user == nil {
		writeError(w, http.StatusInternalServerError, "服务器内部错误")
		return
	}
	if !CheckPassword(user.PasswordHash, req.OldPassword) {
		writeError(w, http.StatusBadRequest, "旧密码错误")
		return
	}
	if len(req.NewPassword) < 8 {
		writeError(w, http.StatusBadRequest, "新密码至少 8 个字符")
		return
	}

	hash, err := HashPassword(req.NewPassword)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "服务器内部错误")
		return
	}
	if err := UpdateUserPassword(s.db, user.ID, hash); err != nil {
		writeError(w, http.StatusInternalServerError, "服务器内部错误")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true})
}

// ---------------------------------------------------------------------------
// 辅助
// ---------------------------------------------------------------------------

// writeJSON 以指定状态码输出 JSON 响应（统一 Content-Type）。
func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	if err := json.NewEncoder(w).Encode(v); err != nil {
		log.Printf("writeJSON: 响应编码失败: %v", err)
	}
}

// writeError 输出契约规定的统一错误格式 {"error": "..."}。
func writeError(w http.ResponseWriter, status int, msg string) {
	writeJSON(w, status, map[string]string{"error": msg})
}

// readJSON 读取请求体并解析 JSON（限制 1 MiB，防止超大请求体）。
func readJSON(w http.ResponseWriter, r *http.Request, dst any) error {
	r.Body = http.MaxBytesReader(w, r.Body, 1<<20)
	if err := json.NewDecoder(r.Body).Decode(dst); err != nil {
		return err
	}
	return nil
}

// staticHandler 托管前端静态文件：
// - 找到 web/dist：普通文件直接返回；未命中的非 API 路径回退 index.html（SPA history 路由）
// - 找不到 web/dist：返回 JSON 提示，服务仍可用于纯 API 场景
func (s *Server) staticHandler() http.Handler {
	dist := findDistDir()
	if dist == "" {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			// 未知 API 路径返回 JSON 404，而不是「纯 API 模式」提示
			if strings.HasPrefix(r.URL.Path, "/api/") {
				writeError(w, http.StatusNotFound, "接口不存在")
				return
			}
			writeJSON(w, http.StatusOK, map[string]string{
				"message": "前端静态文件未构建（web/dist 不存在），当前仅提供 API 服务",
			})
		})
	}

	fileServer := http.FileServer(http.Dir(dist))
	indexPath := filepath.Join(dist, "index.html")
	hasIndex := func() bool {
		st, err := os.Stat(indexPath)
		return err == nil && !st.IsDir()
	}()

	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		// 未知 API 路径返回 JSON 404，而不是回退到前端页面
		if strings.HasPrefix(r.URL.Path, "/api/") {
			writeError(w, http.StatusNotFound, "接口不存在")
			return
		}
		// 文件真实存在时直接返回
		p := strings.TrimPrefix(filepath.Clean(r.URL.Path), string(filepath.Separator))
		if p != "" {
			if st, err := os.Stat(filepath.Join(dist, p)); err == nil && !st.IsDir() {
				fileServer.ServeHTTP(w, r)
				return
			}
		}
		// 其余路径回退到 index.html，支持 Vue history 路由
		if !hasIndex {
			writeError(w, http.StatusNotFound, "静态文件未找到")
			return
		}
		r2 := r.Clone(r.Context())
		r2.URL.Path = "/"
		fileServer.ServeHTTP(w, r2)
	})
}

// findDistDir 定位前端静态文件目录 web/dist：
// 优先 <cwd 的父目录>/web/dist（从 server/ 启动，即项目根/web/dist），
// 其次 <cwd>/web/dist（从项目根启动）；均不存在返回空串。
func findDistDir() string {
	cwd, err := os.Getwd()
	if err != nil {
		return ""
	}
	candidates := make([]string, 0, 2)
	if filepath.Base(cwd) == "server" {
		candidates = append(candidates, filepath.Join(filepath.Dir(cwd), "web", "dist"))
	}
	candidates = append(candidates, filepath.Join(cwd, "web", "dist"))
	for _, c := range candidates {
		if st, err := os.Stat(c); err == nil && st.IsDir() {
			return c
		}
	}
	return ""
}
