/*
server/auth.go
NewAPI 签到配置管理平台 · 认证与鉴权

职责：
- bcrypt 密码哈希与校验
- JWT 签发 / 解析（HS256，密钥来自 NCF_JWT_SECRET）
- API Key 生成（格式 ncf_ + 32 位随机 hex，库中仅存 sha256 哈希，明文创建时只返回一次）
- 鉴权中间件：JWT（管理端）与 API Key（拉取端 /api/config/raw）
*/
package main

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"net/http"
	"strings"
	"sync"
	"time"

	"github.com/golang-jwt/jwt/v5"
	"golang.org/x/crypto/bcrypt"
)

// APIKeyPrefix API Key 固定前缀，用于识别与展示。
const APIKeyPrefix = "ncf_"

// tokenTTL 管理端 JWT 有效期：7 天（604800 秒），与接口契约 expires_in 一致。
const tokenTTL = 7 * 24 * time.Hour

// tokenClaims JWT 载荷：携带用户名，便于修改密码等场景定位账号。
type tokenClaims struct {
	Username string `json:"username"`
	jwt.RegisteredClaims
}

// ---------------------------------------------------------------------------
// 密码
// ---------------------------------------------------------------------------

// HashPassword 使用 bcrypt（默认成本）生成密码哈希。
func HashPassword(password string) (string, error) {
	hash, err := bcrypt.GenerateFromPassword([]byte(password), bcrypt.DefaultCost)
	if err != nil {
		return "", fmt.Errorf("bcrypt 哈希失败: %w", err)
	}
	return string(hash), nil
}

// CheckPassword 校验明文密码与 bcrypt 哈希是否匹配。
func CheckPassword(hash, password string) bool {
	return bcrypt.CompareHashAndPassword([]byte(hash), []byte(password)) == nil
}

// ---------------------------------------------------------------------------
// JWT
// ---------------------------------------------------------------------------

// SignToken 为指定用户名签发 HS256 JWT，返回 token 与过期秒数（604800）。
func SignToken(username string, secret []byte) (string, int64, error) {
	return signTokenWithTTL(username, secret, tokenTTL)
}

// signTokenWithTTL 签发带自定义有效期的 JWT（测试过期场景用）。
func signTokenWithTTL(username string, secret []byte, ttl time.Duration) (string, int64, error) {
	now := time.Now()
	claims := tokenClaims{
		Username: username,
		RegisteredClaims: jwt.RegisteredClaims{
			Subject:   username,
			IssuedAt:  jwt.NewNumericDate(now),
			ExpiresAt: jwt.NewNumericDate(now.Add(ttl)),
		},
	}
	tok := jwt.NewWithClaims(jwt.SigningMethodHS256, claims)
	signed, err := tok.SignedString(secret)
	if err != nil {
		return "", 0, fmt.Errorf("签发 JWT: %w", err)
	}
	return signed, int64(ttl.Seconds()), nil
}

// ParseToken 解析并校验 JWT，返回其中的用户名。
func ParseToken(tokenStr string, secret []byte) (string, error) {
	tok, err := jwt.ParseWithClaims(tokenStr, &tokenClaims{}, func(t *jwt.Token) (any, error) {
		if _, ok := t.Method.(*jwt.SigningMethodHMAC); !ok {
			return nil, fmt.Errorf("非预期的签名算法: %v", t.Header["alg"])
		}
		return secret, nil
	}, jwt.WithValidMethods([]string{jwt.SigningMethodHS256.Alg()}))
	if err != nil {
		return "", fmt.Errorf("解析 JWT: %w", err)
	}
	claims, ok := tok.Claims.(*tokenClaims)
	if !ok || !tok.Valid {
		return "", fmt.Errorf("无效的 JWT")
	}
	return claims.Username, nil
}

// ---------------------------------------------------------------------------
// API Key
// ---------------------------------------------------------------------------

// GenerateAPIKey 生成新 API Key：ncf_ + 32 位随机 hex（共 36 字符）。
// 返回明文（仅此一次展示）、sha256 哈希（存库）、前缀（前 8 位，如 ncf_xxxx）。
func GenerateAPIKey() (plain, hash, prefix string, err error) {
	buf := make([]byte, 16)
	if _, err := rand.Read(buf); err != nil {
		return "", "", "", fmt.Errorf("生成随机密钥: %w", err)
	}
	plain = APIKeyPrefix + hex.EncodeToString(buf)
	return plain, HashAPIKey(plain), plain[:8], nil
}

// HashAPIKey 计算 API Key 的 sha256 十六进制哈希，用于库中存储与比对。
// API Key 为高熵随机值，sha256 足够安全且支持按哈希直接索引查询。
func HashAPIKey(plain string) string {
	sum := sha256.Sum256([]byte(plain))
	return hex.EncodeToString(sum[:])
}

// ---------------------------------------------------------------------------
// 登录失败限流
// ---------------------------------------------------------------------------

// 登录限流参数：1 分钟窗口内最多 5 次失败；超过后指数退避 1/2/4/8/16 秒。
const (
	loginMaxFailures       = 5
	loginWindow            = 1 * time.Minute
	loginMaxBackoff        = 16 * time.Second
	loginDefaultMaxEntries = 10000 // 内存上限：条目过多时清理过期窗口并兜底清空
)

// loginFailEntry 单个「IP+用户名」的失败记录。
type loginFailEntry struct {
	failures     int       // 当前窗口内失败次数
	firstFail    time.Time // 窗口起点（首次失败时间）
	blockedUntil time.Time // 指数退避：下次允许尝试的最早时间
}

// loginLimiter 登录失败限流器（并发安全）。
// 键 = RemoteAddr(IP) + 用户名；不信任 X-Forwarded-For。
type loginLimiter struct {
	mu         sync.Mutex
	now        func() time.Time
	entries    map[string]*loginFailEntry
	maxEntries int
}

func newLoginLimiter() *loginLimiter {
	return &loginLimiter{
		now:        time.Now,
		entries:    make(map[string]*loginFailEntry),
		maxEntries: loginDefaultMaxEntries,
	}
}

// check 查询当前键是否被退避拦截；allowed=false 时返回还需等待的时长。
//
// 封禁判断必须排在窗口过期之前：否则退避最长只能持续到 firstFail+loginWindow，
// 指数退避（最高 16s）会被 60 秒的窗口重置截断，实测放宽到约 10 次/分钟。
// 正在封禁期内就该拒绝，与统计窗口是否过期无关。
func (l *loginLimiter) check(key string) (retryAfter time.Duration, allowed bool) {
	l.mu.Lock()
	defer l.mu.Unlock()
	now := l.now()
	e, ok := l.entries[key]
	if !ok {
		return 0, true
	}
	if now.Before(e.blockedUntil) {
		return e.blockedUntil.Sub(now), false
	}
	// 未处于封禁期：窗口是否过期都放行，过期由下一次 recordFailure 重新计数
	return 0, true
}

// recordFailure 记录一次失败；达到阈值后按失败次数指数退避
// （第 5 次起 1s，之后 2/4/8/16s，封顶 16s）。
func (l *loginLimiter) recordFailure(key string) {
	l.mu.Lock()
	defer l.mu.Unlock()
	now := l.now()
	l.trimLocked(now)
	e, ok := l.entries[key]
	if !ok || now.Sub(e.firstFail) >= loginWindow {
		e = &loginFailEntry{firstFail: now}
		l.entries[key] = e
	}
	e.failures++
	if e.failures >= loginMaxFailures {
		n := e.failures - loginMaxFailures
		if n > 5 {
			n = 5 // 1<<5=32s，比较后封顶 16s
		}
		backoff := time.Duration(1<<uint(n)) * time.Second
		if backoff > loginMaxBackoff {
			backoff = loginMaxBackoff
		}
		e.blockedUntil = now.Add(backoff)
	}
}

// recordSuccess 登录成功：清除该键的失败记录，重新计数。
func (l *loginLimiter) recordSuccess(key string) {
	l.mu.Lock()
	defer l.mu.Unlock()
	delete(l.entries, key)
}

// trimLocked 控制内存增长：条目达到上限时先清理过期窗口的条目，
// 仍超上限则整体清空（限流是临时防护，宁可重置也不无限占用内存）。
func (l *loginLimiter) trimLocked(now time.Time) {
	if len(l.entries) < l.maxEntries {
		return
	}
	for k, e := range l.entries {
		if now.Sub(e.firstFail) >= loginWindow {
			delete(l.entries, k)
		}
	}
	if len(l.entries) >= l.maxEntries {
		l.entries = make(map[string]*loginFailEntry)
	}
}

// ---------------------------------------------------------------------------
// 鉴权中间件
// ---------------------------------------------------------------------------

// ctxKey 请求上下文中自定义键的类型，避免与其他包冲突。
type ctxKey string

const (
	ctxKeyUsername = ctxKey("username")
	ctxKeyAPIKeyID = ctxKey("api_key_id")
)

// bearerToken 从 Authorization 头提取 Bearer token；格式不符返回 false。
func bearerToken(r *http.Request) (string, bool) {
	const prefix = "Bearer "
	h := r.Header.Get("Authorization")
	if len(h) > len(prefix) && strings.EqualFold(h[:len(prefix)], prefix) {
		return strings.TrimSpace(h[len(prefix):]), true
	}
	return "", false
}

// requireJWT 管理端鉴权中间件：校验 Authorization: Bearer <JWT>，
// 通过后把用户名写入请求上下文，供 handler 读取。
func (s *Server) requireJWT(next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		tok, ok := bearerToken(r)
		if !ok {
			writeError(w, http.StatusUnauthorized, "未认证")
			return
		}
		username, err := ParseToken(tok, s.jwtSecret)
		if err != nil {
			writeError(w, http.StatusUnauthorized, "未认证")
			return
		}
		ctx := context.WithValue(r.Context(), ctxKeyUsername, username)
		next(w, r.WithContext(ctx))
	}
}

// requireJWTOrAPIKey 双认证中间件：JWT（网页端管理员）或 API Key（自动化脚本）任一即可。
//
// 为什么需要它：网页端的运维功能原本只认 JWT，而 JWT 要用密码换、两小时过期，
// 脚本没法用。放开这批端点后，Actions / 本机脚本能用同一把 API Key 干完
// 「拉配置、改账号、跑检测、管代理」这些事，不必再去模拟登录。
//
// 有意不放开的是「控制平面」：改密码、增删 API Key、整份导入。那几个一旦被冒用
// 就能永久夺取平台控制权，而 API Key 是要躺在 CI secrets 里的，风险等级不同。
//
// 先试 JWT 再试 API Key：网页端请求量远大于脚本，让常见路径少走一次数据库查询。
func (s *Server) requireJWTOrAPIKey(next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		tok, ok := bearerToken(r)
		if !ok {
			writeError(w, http.StatusUnauthorized, "未认证")
			return
		}
		if username, err := ParseToken(tok, s.jwtSecret); err == nil {
			ctx := context.WithValue(r.Context(), ctxKeyUsername, username)
			next(w, r.WithContext(ctx))
			return
		}
		keyHash := HashAPIKey(tok)
		row, err := GetAPIKeyByHash(s.db, keyHash)
		if err != nil {
			writeError(w, http.StatusInternalServerError, "服务器内部错误")
			return
		}
		if row == nil {
			// 不区分「JWT 过期」和「Key 不存在」：对外统一措辞，别给探测者额外线索
			writeError(w, http.StatusUnauthorized, "未认证：需要有效的登录令牌或 API Key")
			return
		}
		_ = UpdateAPIKeyLastUsed(s.db, row.ID)
		ctx := context.WithValue(r.Context(), ctxKeyAPIKeyID, row.ID)
		next(w, r.WithContext(ctx))
	}
}

// isAPIKeyRequest 判断这次请求是用 API Key 进来的（而不是 JWT）。
// 导出接口用它决定要不要检查一次性票据：票据本质是「二次密码确认」，
// 而 API Key 调用方压根没有交互式输密码的场合。
func isAPIKeyRequest(r *http.Request) bool {
	_, ok := r.Context().Value(ctxKeyAPIKeyID).(int64)
	return ok
}

// requireAPIKey 拉取端鉴权中间件：校验 Authorization: Bearer <API_KEY>，
// 仅用于 /api/config/raw；鉴权通过后顺手更新该 Key 的 last_used_at。
func (s *Server) requireAPIKey(next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		tok, ok := bearerToken(r)
		if !ok {
			writeError(w, http.StatusUnauthorized, "无效的 API Key")
			return
		}
		keyHash := HashAPIKey(tok)
		row, err := GetAPIKeyByHash(s.db, keyHash)
		if err != nil {
			writeError(w, http.StatusInternalServerError, "服务器内部错误")
			return
		}
		if row == nil {
			writeError(w, http.StatusUnauthorized, "无效的 API Key")
			return
		}
		// 使用时间更新失败不影响主流程（仅记录），避免拖垮正常拉取
		_ = UpdateAPIKeyLastUsed(s.db, row.ID)
		ctx := context.WithValue(r.Context(), ctxKeyAPIKeyID, row.ID)
		next(w, r.WithContext(ctx))
	}
}

// ---------------------------------------------------------------------------
// 导出票据（/api/export 的二次确认）
// ---------------------------------------------------------------------------

// 导出票据参数：verify-password 成功后签发，单次使用，2 分钟内有效。
const (
	exportTicketTTL        = 2 * time.Minute
	exportTicketMaxEntries = 1000 // 内存上限：满了先清过期，仍满则整体丢弃重来
)

// exportTicketEntry 一张票据的归属与有效期。
type exportTicketEntry struct {
	username  string
	expiresAt time.Time
}

// exportTicketStore 管理 /api/export 的一次性票据（并发安全）。
//
// 为什么需要它：GET /api/export 返回全量明文配置（含所有站点 Cookie、api_key、
// SMTP 密码）。此前它只有 requireJWT，而前端调用的 POST /api/auth/verify-password
// 在服务端没有任何绑定 —— 拿着 JWT 直接 curl /api/export 就能跳过密码确认。
// 票据把「刚验过密码」这件事变成服务端可校验的状态，让二次确认真正生效。
type exportTicketStore struct {
	mu         sync.Mutex
	now        func() time.Time
	entries    map[string]exportTicketEntry
	maxEntries int
}

func newExportTicketStore() *exportTicketStore {
	return &exportTicketStore{
		now:        time.Now,
		entries:    make(map[string]exportTicketEntry),
		maxEntries: exportTicketMaxEntries,
	}
}

// issue 为指定用户签发一张票据，返回明文票据与有效期秒数。
func (s *exportTicketStore) issue(username string) (string, int, error) {
	buf := make([]byte, 32)
	if _, err := rand.Read(buf); err != nil {
		return "", 0, fmt.Errorf("生成导出票据: %w", err)
	}
	ticket := hex.EncodeToString(buf)

	s.mu.Lock()
	defer s.mu.Unlock()
	now := s.now()
	s.trimLocked(now)
	s.entries[ticket] = exportTicketEntry{username: username, expiresAt: now.Add(exportTicketTTL)}
	return ticket, int(exportTicketTTL.Seconds()), nil
}

// consume 校验并立即销毁票据：必须存在、未过期、且属于该用户。
// 用后即删，所以同一张票据无法导出两次。
func (s *exportTicketStore) consume(ticket, username string) bool {
	if ticket == "" {
		return false
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	e, ok := s.entries[ticket]
	if !ok {
		return false
	}
	delete(s.entries, ticket)
	if s.now().After(e.expiresAt) {
		return false
	}
	return e.username == username
}

// trimLocked 惰性清理：条目过多时先删过期项，仍然超限就整体丢弃。
// 票据是短时凭据，全部作废最多让用户重新输一次密码，不会造成数据损失。
func (s *exportTicketStore) trimLocked(now time.Time) {
	if len(s.entries) < s.maxEntries {
		return
	}
	for k, e := range s.entries {
		if now.After(e.expiresAt) {
			delete(s.entries, k)
		}
	}
	if len(s.entries) >= s.maxEntries {
		s.entries = make(map[string]exportTicketEntry)
	}
}
