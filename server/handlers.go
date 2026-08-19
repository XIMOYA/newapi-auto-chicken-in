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
	"errors"
	"fmt"
	"io/fs"
	"log"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

// serverVersion 健康检查接口返回的版本号。
const serverVersion = "1.0.0"

// Server 服务依赖：数据库连接、JWT 签名密钥、代理池管理器与登录限流器。
type Server struct {
	db            *sql.DB
	jwtSecret     []byte
	proxies       *ProxyManager
	cookieTests   *CookieTestRunner
	loginLim      *loginLimiter
	exportTickets *exportTicketStore
}

// NewServer 构造服务实例；jwtSecret 为 JWT 签名密钥（main 已校验长度）。
func NewServer(db *sql.DB, jwtSecret string) *Server {
	proxies := NewProxyManager(db)
	return &Server{
		db:            db,
		jwtSecret:     []byte(jwtSecret),
		proxies:       proxies,
		cookieTests:   NewCookieTestRunner(proxies, db),
		loginLim:      newLoginLimiter(),
		exportTickets: newExportTicketStore(),
	}
}

// routes 注册全部 HTTP 路由并返回根 Handler。
// API 路由优先匹配；"/" 兜底交给静态文件处理器。
// 整体包一层基础安全响应头中间件。
func (s *Server) routes() http.Handler {
	mux := http.NewServeMux()

	mux.HandleFunc("GET /api/health", s.handleHealth)
	mux.HandleFunc("POST /api/login", s.handleLogin)

	mux.HandleFunc("GET /api/config", s.requireJWT(s.handleGetConfig))
	mux.HandleFunc("GET /api/config/revision", s.requireJWT(s.handleConfigRevision))
	mux.HandleFunc("PUT /api/config", s.requireJWT(s.handlePutConfig))
	mux.HandleFunc("POST /api/config/import", s.requireJWT(s.handleImportConfig))
	mux.HandleFunc("GET /api/config/raw", s.requireAPIKey(s.handleRawConfig))

	// 账号级增量操作：多人同时编辑账号列表走这里，不走整份覆盖
	mux.HandleFunc("POST /api/accounts/ops", s.requireJWT(s.handleAccountOps))

	mux.HandleFunc("GET /api/keys", s.requireJWT(s.handleListKeys))
	mux.HandleFunc("POST /api/keys", s.requireJWT(s.handleCreateKey))
	mux.HandleFunc("DELETE /api/keys/{id}", s.requireJWT(s.handleDeleteKey))

	mux.HandleFunc("GET /api/export", s.requireJWT(s.handleExport))
	mux.HandleFunc("PUT /api/password", s.requireJWT(s.handlePassword))
	mux.HandleFunc("POST /api/auth/verify-password", s.requireJWT(s.handleVerifyPassword))

	// Cookie 可用性测试：站点 Cookie 与 TaBiAI 严格分开，检测在后台跑、前端轮询。
	mux.HandleFunc("POST /api/cookie-tests/newapi", s.requireJWT(s.handleNewAPICookieTest))
	mux.HandleFunc("POST /api/cookie-tests/tabiai", s.requireJWT(s.handleTabiAICookieTest))
	mux.HandleFunc("GET /api/cookie-tests/status", s.requireJWT(s.handleCookieTestStatus))
	mux.HandleFunc("POST /api/cookie-tests/stop", s.requireJWT(s.handleStopCookieTest))

	// TaBiAI 凭据维护：签发（GitHub OAuth 三步换 new_api_refresh）与回写（Python 侧轮转后同步）
	mux.HandleFunc("POST /api/tabiai/issue-cookie", s.requireJWT(s.handleIssueTabiAICookie))
	mux.HandleFunc("POST /api/accounts/{name}/refresh-cookie", s.requireAPIKey(s.handleWriteBackRefreshCookie))

	// 签到运行状态：客户端用 API Key 上报开跑/心跳/收尾，网页端据此锁住高危凭据操作。
	// 强制解锁走 JWT —— 那是管理员在界面上的决定，不该让持有 API Key 的客户端能做。
	mux.HandleFunc("POST /api/run-state/start", s.requireAPIKey(s.handleRunStateStart))
	mux.HandleFunc("POST /api/run-state/heartbeat", s.requireAPIKey(s.handleRunStateHeartbeat))
	mux.HandleFunc("POST /api/run-state/stop", s.requireAPIKey(s.handleRunStateStop))
	mux.HandleFunc("GET /api/run-state", s.requireJWT(s.handleGetRunState))
	mux.HandleFunc("POST /api/run-state/unlock", s.requireJWT(s.handleRunStateUnlock))

	// 代理池管理
	mux.HandleFunc("GET /api/proxies", s.requireJWT(s.handleListProxies))
	mux.HandleFunc("GET /api/proxies/available", s.requireAPIKey(s.handleAvailableProxies))
	mux.HandleFunc("GET /api/proxies/stats", s.requireJWT(s.handleProxyStats))
	mux.HandleFunc("POST /api/proxies/refresh", s.requireJWT(s.handleRefreshProxies))
	mux.HandleFunc("POST /api/proxies/speedtest", s.requireJWT(s.handleSpeedTestProxies))

	mux.Handle("/", s.staticHandler())
	return securityHeaders(mux)
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
// 带登录失败限流：按「对端 IP + 用户名」统计，1 分钟 5 次失败后指数退避。
func (s *Server) handleLogin(w http.ResponseWriter, r *http.Request) {
	var req struct {
		Username string `json:"username"`
		Password string `json:"password"`
	}
	if err := readJSON(w, r, &req); err != nil {
		writeError(w, http.StatusBadRequest, "请求体不是合法的 JSON")
		return
	}
	username := strings.TrimSpace(req.Username)
	if username == "" || req.Password == "" {
		writeError(w, http.StatusBadRequest, "用户名和密码不能为空")
		return
	}

	key := loginKey(r, username)
	if retryAfter, allowed := s.loginLim.check(key); !allowed {
		w.Header().Set("Retry-After", fmt.Sprintf("%.0f", retryAfter.Seconds()))
		writeError(w, http.StatusTooManyRequests, "登录尝试过于频繁，请稍后再试")
		return
	}

	user, err := GetUserByUsername(s.db, username)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "服务器内部错误")
		return
	}
	if user == nil || !CheckPassword(user.PasswordHash, req.Password) {
		s.loginLim.recordFailure(key)
		writeError(w, http.StatusUnauthorized, "用户名或密码错误")
		return
	}
	s.loginLim.recordSuccess(key)

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

// loginKey 构造登录限流键：对端 IP + 用户名。
// 只信任 RemoteAddr（不解析 X-Forwarded-For，防止伪造来源绕过限流）。
func loginKey(r *http.Request, username string) string {
	return clientIP(r) + "|" + username
}

// clientIP 返回请求对端 IP（RemoteAddr 的 host 部分）；解析失败时原样返回。
func clientIP(r *http.Request) string {
	host, _, err := net.SplitHostPort(r.RemoteAddr)
	if err != nil {
		return r.RemoteAddr
	}
	return host
}

// ---------------------------------------------------------------------------
// 辅助
// ---------------------------------------------------------------------------
// handleConfigRevision GET /api/config/revision（JWT）—— 只返回乐观锁版本号。
//
// 供前端轮询做「多人编辑无感同步」：配置本身可能有几十 KB，而判断「有没有变」
// 只需要一个整数。只查一行一列，可以放心用短间隔轮询。
//
// 有意不返回 updated_at：凭据轮转会更新它但不推进 revision（见
// saveConfigLockedKeepRevision），若把它一起返回，很容易被误用来判断变更，
// 结果又让后台轮转触发全量刷新。
func (s *Server) handleConfigRevision(w http.ResponseWriter, r *http.Request) {
	_, _, revision, err := LoadConfigWithRevision(s.db)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "服务器内部错误")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"revision": revision})
}

// handleGetConfig GET /api/config（JWT）—— 返回打码后的配置、更新时间与乐观锁版本号。
func (s *Server) handleGetConfig(w http.ResponseWriter, r *http.Request) {
	cfg, updatedAt, revision, err := LoadConfigWithRevision(s.db)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "服务器内部错误")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"config":     MaskConfig(&cfg),
		"updated_at": updatedAt,
		"revision":   revision,
	})
}

// handlePutConfig PUT /api/config（JWT）—— 还原 "***" 占位符、校验后落库。
//
// 并发保护：请求带 revision 时走乐观锁，版本不一致返回 409 并回传当前最新配置，
// 避免多人/多标签页各自用陈旧快照整份覆盖（曾导致别人刚填的 Cookie 被静默清空）。
//
// 不带 revision 时保持无条件覆盖行为以兼容既有外部脚本，但 accounts[].cookie 会
// 保留库中现有值 —— 该字段由后台签到持续轮转，用陈旧请求体写回旧代次会触发站点
// 重放检测、导致整条会话被撤销。要改凭据请带上 revision，或用签发/回写接口。
func (s *Server) handlePutConfig(w http.ResponseWriter, r *http.Request) {
	var req struct {
		Config   *Config `json:"config"`
		Revision *int64  `json:"revision"`
	}
	if err := readJSON(w, r, &req); err != nil {
		writeError(w, http.StatusBadRequest, "请求体不是合法的 JSON")
		return
	}
	if req.Config == nil {
		writeError(w, http.StatusBadRequest, "config 不能为空")
		return
	}

	oldCfg, _, currentRevision, err := LoadConfigWithRevision(s.db)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "服务器内部错误")
		return
	}
	merged, err := UnmaskConfig(req.Config, &oldCfg)
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	if err := ValidateConfig(merged); err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}

	if req.Revision == nil {
		log.Printf("[config] PUT /api/config 未携带 revision，按无条件覆盖处理（并发保护未生效）")
		updatedAt, saveErr := SaveConfigKeepingCookies(s.db, *merged)
		if saveErr != nil {
			writeError(w, http.StatusInternalServerError, "服务器内部错误")
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{
			"ok":         true,
			"updated_at": updatedAt,
			"revision":   currentRevision + 1,
		})
		return
	}

	updatedAt, newRevision, saveErr := SaveConfigIfMatch(s.db, *merged, *req.Revision)
	if errors.Is(saveErr, ErrConfigRevisionConflict) {
		latest, latestUpdatedAt, latestRevision, loadErr := LoadConfigWithRevision(s.db)
		if loadErr != nil {
			writeError(w, http.StatusInternalServerError, "服务器内部错误")
			return
		}
		writeJSON(w, http.StatusConflict, map[string]any{
			"error":      "配置已被他人修改，请重新载入最新版本后再提交",
			"revision":   latestRevision,
			"updated_at": latestUpdatedAt,
			"config":     MaskConfig(&latest),
		})
		return
	}
	if saveErr != nil {
		writeError(w, http.StatusInternalServerError, "服务器内部错误")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"ok":         true,
		"updated_at": updatedAt,
		"revision":   newRevision,
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
	// 导入的往往是从本平台导出的 JSON：GET /api/config 里的敏感字段是 "***"。
	// 不还原就会把占位符字面量当真值落库，不可逆地摧毁 api_key / SMTP 密码等凭据。
	restored, err := UnmaskConfig(&target, &oldCfg)
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	target = *restored
	if err := ValidateConfig(&target); err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}

	// 走保留凭据的写入：导入文件通常是几小时前的快照，而 cookie 由后台持续轮转
	updatedAt, err := SaveConfigKeepingCookies(s.db, target)
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
	// 签发一次性票据：GET /api/export 会校验并销毁它，
	// 这样「查看明文前先验密码」在服务端真正生效，而不是只靠前端自觉调用。
	ticket, expiresIn, err := s.exportTickets.issue(username)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "服务器内部错误")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"ok":         true,
		"ticket":     ticket,
		"expires_in": expiresIn,
	})
}

// handleNewAPICookieTest POST /api/cookie-tests/newapi（JWT）—— 启动站点 Cookie 检测任务。
func (s *Server) handleNewAPICookieTest(w http.ResponseWriter, r *http.Request) {
	s.handleCookieTest(w, r, LoginMethodNewAPICookie)
}

// handleTabiAICookieTest POST /api/cookie-tests/tabiai（JWT）—— 启动 TaBiAI 凭据检测任务。
//
// 这个检测是一次真实的 refresh，会消耗一代 new_api_refresh。签到进程正在跑时两边
// 都在推进代次，谁手里的旧了下次就会被判重放、整条会话被撤销，所以先拦一道。
func (s *Server) handleTabiAICookieTest(w http.ResponseWriter, r *http.Request) {
	if s.guardRunningCheckin(w) {
		return
	}
	s.handleCookieTest(w, r, LoginMethodTabiAI)
}

// handleCookieTest 启动后台检测任务并立即返回；结果由 GET /api/cookie-tests/status 轮询。
func (s *Server) handleCookieTest(w http.ResponseWriter, r *http.Request, mode string) {
	var req struct {
		AccountNames []string `json:"account_names"`
	}
	if err := readJSON(w, r, &req); err != nil {
		writeError(w, http.StatusBadRequest, "请求体不是合法的 JSON")
		return
	}
	if s.cookieTests.IsRunning() {
		writeError(w, http.StatusConflict, "已有 Cookie 检测任务在进行中")
		return
	}
	cfg, _, err := LoadConfig(s.db)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "服务器内部错误")
		return
	}
	if err := s.cookieTests.Start(&cfg, mode, req.AccountNames); err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "mode": mode, "started": true})
}

// handleCookieTestStatus GET /api/cookie-tests/status（JWT）—— 当前任务进度与实时结果。
func (s *Server) handleCookieTestStatus(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, s.cookieTests.Snapshot())
}

// handleStopCookieTest POST /api/cookie-tests/stop（JWT）—— 请求停止当前任务（幂等）。
func (s *Server) handleStopCookieTest(w http.ResponseWriter, r *http.Request) {
	s.cookieTests.Stop()
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

// handleExport GET /api/export（JWT + 一次性票据）—— 返回完整明文配置的 JSON 字符串。
//
// 除 JWT 外还要求 X-Export-Ticket：票据由 POST /api/auth/verify-password 签发，
// 单次使用、2 分钟过期、绑定用户。没有这层绑定的话，拿到 JWT 就能直接拉走全部
// 明文凭据，前端那步密码确认等于装饰。
func (s *Server) handleExport(w http.ResponseWriter, r *http.Request) {
	username, _ := r.Context().Value(ctxKeyUsername).(string)
	if !s.exportTickets.consume(r.Header.Get("X-Export-Ticket"), username) {
		writeError(w, http.StatusForbidden,
			"导出需要先通过密码确认（票据缺失、已使用或已过期），请重新验证密码")
		return
	}
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
	if len(req.NewPassword) < minPasswordLen {
		writeError(w, http.StatusBadRequest, fmt.Sprintf("新密码至少 %d 个字符", minPasswordLen))
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
// 支持 ?alive=1 只返回可用、?limit=N 数量（0 = 不限制）、?source=xxx 按来源过滤。
func (s *Server) handleListProxies(w http.ResponseWriter, r *http.Request) {
	aliveOnly := r.URL.Query().Get("alive") == "1"
	// 页面展示默认给 500 条够用；显式传 ?limit=0 才拉全量
	limit := 500
	if v := r.URL.Query().Get("limit"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n >= 0 {
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
	// 默认不限制：上游有多少可用就全部返回。?limit=N 可显式限制。
	limit := 0
	if v := r.URL.Query().Get("limit"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 {
			limit = n
		}
	}
	addrs := s.proxies.AvailableAddrs(limit)
	writeJSON(w, http.StatusOK, map[string]any{
		"proxies":    addrs,
		"count":      len(addrs),
		"checked_at": s.proxies.LastRunRFC3339(),
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
		"last_run":   s.proxies.LastRunRFC3339(),
		"last_error": s.proxies.LastError(),
		"running":    s.proxies.IsRunning(),
		"progress":   s.proxies.Progress(),
	})
}

// handleRefreshProxies POST /api/proxies/refresh（JWT）—— 手动触发一次刷新。
func (s *Server) handleRefreshProxies(w http.ResponseWriter, r *http.Request) {
	if s.proxies.IsRunning() {
		writeError(w, http.StatusConflict, "代理池刷新或测速已在进行中")
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
	if s.proxies.IsRunning() {
		writeError(w, http.StatusConflict, "代理池刷新或测速已在进行中")
		return
	}
	cfg, _, err := LoadConfig(s.db)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "服务器内部错误")
		return
	}
	timeout := cfg.ProxyPool.Timeout
	go func() {
		// 测速后台使用独立 120 秒 context：不随 HTTP 请求结束/取消而中断
		ctx, cancel := context.WithTimeout(context.Background(), speedTestBackgroundTimeout)
		defer cancel()
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

// securityHeaders 为所有响应追加基础安全响应头（防 MIME 嗅探 / 点击劫持 / 来源泄露）。
func securityHeaders(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		h := w.Header()
		h.Set("X-Content-Type-Options", "nosniff")
		h.Set("X-Frame-Options", "DENY")
		h.Set("Referrer-Policy", "no-referrer")
		// 现代浏览器已内置 XSS 防护，显式禁用遗留的 XSS Auditor 避免误报
		h.Set("X-XSS-Protection", "0")
		next.ServeHTTP(w, r)
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
