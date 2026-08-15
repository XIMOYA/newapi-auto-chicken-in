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
	"context"
	"database/sql"
	"encoding/json"
	"io/fs"
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

// Server 服务依赖：数据库连接、JWT 签名密钥与代理池管理器。
type Server struct {
	db        *sql.DB
	jwtSecret []byte
	proxies   *ProxyManager
}

// NewServer 构造服务实例；jwtSecret 为 JWT 签名密钥（main 已校验长度）。
func NewServer(db *sql.DB, jwtSecret string) *Server {
	return &Server{db: db, jwtSecret: []byte(jwtSecret), proxies: NewProxyManager(db)}
}

// routes 注册全部 HTTP 路由并返回根 Handler。
// API 路由优先匹配；"/" 兜底交给静态文件处理器。
func (s *Server) routes() http.Handler {
	mux := http.NewServeMux()

	mux.HandleFunc("GET /api/health", s.handleHealth)
	mux.HandleFunc("POST /api/login", s.handleLogin)

	mux.HandleFunc("GET /api/config", s.requireJWT(s.handleGetConfig))
	mux.HandleFunc("PUT /api/config", s.requireJWT(s.handlePutConfig))
	mux.HandleFunc("POST /api/config/import", s.requireJWT(s.handleImportConfig))
	mux.HandleFunc("GET /api/config/raw", s.requireAPIKey(s.handleRawConfig))

	mux.HandleFunc("GET /api/keys", s.requireJWT(s.handleListKeys))
	mux.HandleFunc("POST /api/keys", s.requireJWT(s.handleCreateKey))
	mux.HandleFunc("DELETE /api/keys/{id}", s.requireJWT(s.handleDeleteKey))

	mux.HandleFunc("GET /api/export", s.requireJWT(s.handleExport))
	mux.HandleFunc("PUT /api/password", s.requireJWT(s.handlePassword))
	mux.HandleFunc("POST /api/auth/verify-password", s.requireJWT(s.handleVerifyPassword))

	// 代理池管理
	mux.HandleFunc("GET /api/proxies", s.requireJWT(s.handleListProxies))
	mux.HandleFunc("GET /api/proxies/available", s.requireAPIKey(s.handleAvailableProxies))
	mux.HandleFunc("GET /api/proxies/stats", s.requireJWT(s.handleProxyStats))
	mux.HandleFunc("POST /api/proxies/refresh", s.requireJWT(s.handleRefreshProxies))
	mux.HandleFunc("POST /api/proxies/speedtest", s.requireJWT(s.handleSpeedTestProxies))

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
// 辅助
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

// handleImportConfig POST /api/config/import（JWT）—— 导入配置。
// body: {"config": {...}, "mode": "overwrite" | "merge", "modules": ["accounts", ...]}
//   - overwrite：整体覆盖（modules 忽略）
//   - merge：仅合并 modules 列出的模块（未列出的保留现有）
//     其中 accounts / sites 按 name 合并（同名更新、新名追加），
//     其余标量模块（ai/browser/http/defaults/proxy_pool/notify/config_sync/security）
//     若导入 JSON 中存在该模块则整体覆盖。
func (s *Server) handleImportConfig(w http.ResponseWriter, r *http.Request) {
	var req struct {
		Config  json.RawMessage `json:"config"`
		Mode    string          `json:"mode"`
		Modules []string        `json:"modules"`
	}
	if err := readJSON(w, r, &req); err != nil {
		writeError(w, http.StatusBadRequest, "请求体不是合法的 JSON")
		return
	}
	if len(req.Config) == 0 {
		writeError(w, http.StatusBadRequest, "config 不能为空")
		return
	}
	mode := strings.ToLower(strings.TrimSpace(req.Mode))
	if mode != "overwrite" && mode != "merge" {
		writeError(w, http.StatusBadRequest, "mode 必须是 overwrite 或 merge")
		return
	}

	// 解析导入配置，并记录其顶层实际存在的模块键
	var in Config
	if err := json.Unmarshal(req.Config, &in); err != nil {
		writeError(w, http.StatusBadRequest, "config 解析失败")
		return
	}
	present := map[string]bool{}
	var keys map[string]json.RawMessage
	if err := json.Unmarshal(req.Config, &keys); err == nil {
		for k := range keys {
			present[k] = true
		}
	}

	oldCfg, _, err := LoadConfig(s.db)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "服务器内部错误")
		return
	}

	var target Config
	if mode == "merge" {
		for _, m := range req.Modules {
			if !isValidModule(m) {
				writeError(w, http.StatusBadRequest, "modules 含未知模块: "+m)
				return
			}
		}
		target = mergeConfigWithModules(&in, &oldCfg, present, req.Modules)
	} else {
		target = in
	}
	if err := ValidateConfig(&target); err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}

	updatedAt, err := SaveConfig(s.db, target)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "服务器内部错误")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"ok":         true,
		"mode":       mode,
		"modules":    req.Modules,
		"updated_at": updatedAt,
	})
}

// configModuleKeys 全部可导入的顶层模块。
var configModuleKeys = []string{
	"accounts", "sites", "ai", "browser", "http", "defaults",
	"proxy_pool", "notify", "config_sync", "security",
}

// isValidModule 判断模块名是否合法。
func isValidModule(m string) bool {
	for _, k := range configModuleKeys {
		if m == k {
			return true
		}
	}
	return false
}

// mergeConfigWithModules 按 modules 列表把导入配置合并进现有配置：
// - modules 为 nil（未传）→ 默认全部模块（向后兼容旧客户端）
// - modules 为空数组 → 不导入任何模块
// - accounts / sites：按 name 合并（同名覆盖、新名追加）
// - 标量模块：仅当导入 JSON 中存在且被勾选时整体覆盖，否则保留现有
func mergeConfigWithModules(in *Config, old *Config, present map[string]bool, modules []string) Config {
	out := cloneConfig(old)
	if modules == nil {
		modules = configModuleKeys
	}
	want := make(map[string]bool, len(modules))
	for _, m := range modules {
		want[m] = true
	}

	// accounts：按 name 合并
	if want["accounts"] && present["accounts"] {
		if out.Accounts == nil {
			out.Accounts = []Account{}
		}
		existing := make(map[string]int, len(out.Accounts))
		for i := range out.Accounts {
			existing[out.Accounts[i].Name] = i
		}
		for _, a := range in.Accounts {
			if i, ok := existing[a.Name]; ok {
				out.Accounts[i] = a
			} else {
				existing[a.Name] = len(out.Accounts)
				out.Accounts = append(out.Accounts, a)
			}
		}
	}

	// sites：按 name 合并
	if want["sites"] && present["sites"] {
		if out.Sites == nil {
			out.Sites = []Site{}
		}
		existingSites := make(map[string]int, len(out.Sites))
		for i := range out.Sites {
			existingSites[out.Sites[i].Name] = i
		}
		for _, s := range in.Sites {
			if i, ok := existingSites[s.Name]; ok {
				out.Sites[i] = s
			} else {
				existingSites[s.Name] = len(out.Sites)
				out.Sites = append(out.Sites, s)
			}
		}
	}

	// 标量模块：勾选且导入中存在 → 整体覆盖
	if want["ai"] && present["ai"] {
		out.AI = in.AI
	}
	if want["browser"] && present["browser"] {
		out.Browser = in.Browser
	}
	if want["http"] && present["http"] {
		out.HTTP = in.HTTP
	}
	if want["defaults"] && present["defaults"] {
		out.Defaults = in.Defaults
	}
	if want["proxy_pool"] && present["proxy_pool"] {
		out.ProxyPool = in.ProxyPool
	}
	if want["notify"] && present["notify"] {
		out.Notify = in.Notify
	}
	if want["config_sync"] && present["config_sync"] {
		out.ConfigSync = in.ConfigSync
	}
	if want["security"] && present["security"] {
		out.Security = in.Security
	}

	return *out
}

// handleVerifyPassword POST /api/auth/verify-password（JWT）—— 二次确认当前用户密码。
// 用于查看明文 Cookie / API Key 等高敏操作前的确认，避免误点泄露。
func (s *Server) handleVerifyPassword(w http.ResponseWriter, r *http.Request) {
	var req struct {
		Password string `json:"password"`
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
	if !CheckPassword(user.PasswordHash, req.Password) {
		writeError(w, http.StatusBadRequest, "密码错误")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true})
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
// 代理池管理接口
// ---------------------------------------------------------------------------

// handleListProxies GET /api/proxies（JWT）—— 代理列表（页面展示）。
// 支持 ?alive=1 只返回可用、?limit=N 数量、?source=xxx 按来源过滤。
func (s *Server) handleListProxies(w http.ResponseWriter, r *http.Request) {
	aliveOnly := r.URL.Query().Get("alive") == "1"
	limit := 500
	if v := r.URL.Query().Get("limit"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 && n <= 2000 {
			limit = n
		}
	}
	entries, err := s.proxies.ListProxies(aliveOnly, limit)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "查询代理列表失败")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"proxies": entries,
		"total":   len(entries),
	})
}

// handleAvailableProxies GET /api/proxies/available（API Key）—— Actions 预取可用代理列表。
// 返回 {"proxies": ["ip:port", ...], "count": N, "checked_at": "..."}。
func (s *Server) handleAvailableProxies(w http.ResponseWriter, r *http.Request) {
	limit := 100
	if v := r.URL.Query().Get("limit"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 && n <= 2000 {
			limit = n
		}
	}
	addrs := s.proxies.AvailableAddrs(limit)
	writeJSON(w, http.StatusOK, map[string]any{
		"proxies":    addrs,
		"count":      len(addrs),
		"checked_at": s.proxies.LastRun().UTC().Format(time.RFC3339),
	})
}

// handleProxyStats GET /api/proxies/stats（JWT）—— 统计与最近刷新状态。
func (s *Server) handleProxyStats(w http.ResponseWriter, r *http.Request) {
	st, err := s.proxies.Stats()
	if err != nil {
		writeError(w, http.StatusInternalServerError, "统计代理失败")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"total":      st.Total,
		"alive":      st.Alive,
		"by_source":  st.BySource,
		"last_run":   s.proxies.LastRun().UTC().Format(time.RFC3339),
		"last_error": s.proxies.LastError(),
		"running":    s.proxies.IsRunning(),
	})
}

// handleRefreshProxies POST /api/proxies/refresh（JWT）—— 手动触发一次刷新。
func (s *Server) handleRefreshProxies(w http.ResponseWriter, r *http.Request) {
	if s.proxies.IsRunning() {
		writeError(w, http.StatusConflict, "代理池刷新已在进行中")
		return
	}
	cfg, _, err := LoadConfig(s.db)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "服务器内部错误")
		return
	}
	limit := cfg.ProxyPool.SaveLimit
	go func() {
		alive, rerr := s.proxies.RefreshProxies(cfg.ProxyPool, limit)
		if rerr != nil {
			log.Printf("[proxy] 手动刷新失败: %v", rerr)
		} else {
			log.Printf("[proxy] 手动刷新完成，可用代理 %d 条", alive)
		}
	}()
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "message": "代理池刷新已开始"})
}

// handleSpeedTestProxies POST /api/proxies/speedtest（JWT）—— 对代理实测下载速度。
// body: {"proxies": ["ip:port", ...]}；proxies 为空 = 全部可用代理。
// 返回 {"ok": true, "tested": N, "url": "..."}。
func (s *Server) handleSpeedTestProxies(w http.ResponseWriter, r *http.Request) {
	var req struct {
		Proxies []string `json:"proxies"`
	}
	if err := readJSON(w, r, &req); err != nil {
		writeError(w, http.StatusBadRequest, "请求体不是合法的 JSON")
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 120*time.Second)
	defer cancel()
	cfg, _, err := LoadConfig(s.db)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "服务器内部错误")
		return
	}
	timeout := cfg.ProxyPool.Timeout
	go func() {
		updated, terr := s.proxies.SpeedTest(ctx, req.Proxies, timeout)
		if terr != nil {
			log.Printf("[proxy] 测速失败: %v", terr)
		} else {
			log.Printf("[proxy] 测速完成，更新 %d 条", updated)
		}
	}()
	writeJSON(w, http.StatusOK, map[string]any{
		"ok": true, "tested": len(req.Proxies), "url": "https://speed.cloudflare.com/__down?bytes=1048576",
	})
}

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

// staticHandler 托管前端静态文件，按优先级：
// 1) 外部 web/dist（开发热更新，免重新编译）
// 2) 嵌入二进制的 embed_dist（go:embed，单文件部署）
// 命中普通文件直接返回；未命中的非 API 路径回退 index.html（SPA history 路由）；
// 前端完全缺失时返回 JSON 提示，服务仍可用于纯 API 场景。
func (s *Server) staticHandler() http.Handler {
	if dist := findDistDir(); dist != "" {
		return serveFromDir(dist)
	}

	sub, err := fs.Sub(embeddedDist, "embed_dist")
	if err != nil {
		sub = nil
	}
	if sub == nil {
		return noFrontendHandler()
	}
	return serveFromFS(sub)
}

// serveFromDir 基于磁盘目录提供前端静态文件。
func serveFromDir(dist string) http.Handler {
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

// serveFromFS 基于嵌入式文件系统提供前端静态文件（单二进制场景）。
func serveFromFS(sub fs.FS) http.Handler {
	fileServer := http.FileServer(http.FS(sub))
	hasIndex := func() bool {
		_, err := fs.Stat(sub, "index.html")
		return err == nil
	}()

	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.HasPrefix(r.URL.Path, "/api/") {
			writeError(w, http.StatusNotFound, "接口不存在")
			return
		}
		p := strings.TrimPrefix(filepath.Clean(r.URL.Path), string(filepath.Separator))
		if p != "" && p != "." {
			if st, err := fs.Stat(sub, filepath.ToSlash(p)); err == nil && !st.IsDir() {
				fileServer.ServeHTTP(w, r)
				return
			}
		}
		if !hasIndex {
			writeError(w, http.StatusNotFound, "静态文件未找到（前端未嵌入，请用构建脚本重新编译）")
			return
		}
		r2 := r.Clone(r.Context())
		r2.URL.Path = "/"
		fileServer.ServeHTTP(w, r2)
	})
}

// noFrontendHandler 前端完全不可用时返回 JSON 提示（纯 API 模式）。
func noFrontendHandler() http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.HasPrefix(r.URL.Path, "/api/") {
			writeError(w, http.StatusNotFound, "接口不存在")
			return
		}
		writeJSON(w, http.StatusOK, map[string]string{
			"message": "前端静态文件未构建（web/dist 与嵌入资源均不存在），当前仅提供 API 服务",
		})
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
