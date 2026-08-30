/*
server/github_status.go
GitHub 账号自身状态探测：可登录 / 已停用 / 已封禁。

为什么单独一条链路：`/api/github-accounts/check` 判的是「这条 session 能不能在某个
站点完成 OAuth 授权」，需要站点上下文。而「这个 GitHub 账号本身是不是被 GitHub 停用
了」跟站点无关，直接问 GitHub 就行 —— 而且必须能在**入池之前**判，被停用/封禁的账号
加进池子只会白占名额、每轮签发都失败。

判定口径（宽进宽出，宁可归 unknown 也不误判）：
  - GET https://github.com/settings/profile 带 user_session
  - 200 且正文有登录态特征 → active
  - 302 到 /login 或 /session → session 失效（expired），账号本身状态未知
  - 正文/跳转出现 suspended / account has been suspended → suspended
  - 403 且正文有封禁特征 → banned
  - 其余（网络失败、限流、看不懂的页面）→ unknown

为什么用 settings/profile 而不是 /：首页对未登录用户也返回 200，无法据此判定登录态；
settings 页必须登录才给，且账号被停用时 GitHub 会明确渲染停用提示。
*/
package main

import (
	"context"
	"fmt"
	"io"
	"log"
	"net/http"
	"strings"
)

// GitHub 账号状态。
const (
	githubStatusActive    = "active"
	githubStatusSuspended = "suspended"
	githubStatusBanned    = "banned"
	githubStatusExpired   = "expired"
	githubStatusUnknown   = "unknown"
)

// githubProfileURL 判定登录态与账号状态用的页面。
const githubProfileURL = "https://github.com/settings/profile"

// githubSuspendedMarkers 账号被停用的正文特征。
// GitHub 对停用账号会渲染明确提示，各处措辞略有差异，列几种常见的。
var githubSuspendedMarkers = []string{
	"account has been suspended",
	"account is suspended",
	"your account has been flagged",
	"this account has been suspended",
	"账号已被暂停",
}

// githubBannedMarkers 账号被封禁/终止的正文特征。
// 与 suspended 分开是因为处置不同：suspended 有申诉恢复的可能，banned 基本没有。
var githubBannedMarkers = []string{
	"account has been terminated",
	"account was disabled",
	"account has been disabled",
	"permanently suspended",
}

// githubLoggedInMarkers 登录态特征：settings 页只有登录用户拿得到。
// 用多个特征取并集，单个 DOM 变动不至于让整条判定失效。
var githubLoggedInMarkers = []string{
	"logged-in", "user-session", "sign out", "退出登录",
	"公共资料", "public profile", "settings/profile",
}

// githubStatusResult 一次账号状态探测的结论。
type githubStatusResult struct {
	// Status active / suspended / banned / expired / unknown
	Status  string `json:"status"`
	Message string `json:"message"`
	// Usable 是否值得留在池子里。只有 active 为 true ——
	// suspended/banned 的账号每轮签发都会失败，留着只是白占名额
	Usable bool `json:"usable"`
}

// containsAny 正文里是否命中任一特征词（大小写不敏感）。
func containsAny(body string, markers []string) bool {
	lower := strings.ToLower(body)
	for _, marker := range markers {
		if strings.Contains(lower, strings.ToLower(marker)) {
			return true
		}
	}
	return false
}

/*
classifyGitHubProfileResponse 按响应判定账号状态。

抽成纯函数是为了能不联网测全部分支 —— 这条判定的每个分支都对应一种真实处置，
靠联网碰运气覆盖不到。

location 是 3xx 的 Location 头，body 是正文（调用方已截断）。
*/
func classifyGitHubProfileResponse(status int, location, body string) githubStatusResult {
	// 停用/封禁优先判：GitHub 对这类账号可能仍返回 200，只是正文换成提示页
	if containsAny(body, githubBannedMarkers) {
		return githubStatusResult{Status: githubStatusBanned,
			Message: "GitHub 提示该账号已被终止/禁用"}
	}
	if containsAny(body, githubSuspendedMarkers) {
		return githubStatusResult{Status: githubStatusSuspended,
			Message: "GitHub 提示该账号已被暂停"}
	}

	switch {
	case status >= 300 && status < 400:
		target := strings.ToLower(location)
		if strings.Contains(target, "/login") || strings.Contains(target, "/session") {
			return githubStatusResult{Status: githubStatusExpired,
				Message: "GitHub 要求重新登录，user_session 已失效"}
		}
		return githubStatusResult{Status: githubStatusUnknown,
			Message: fmt.Sprintf("GitHub 返回了未预期的跳转（HTTP %d → %s）", status, location)}
	case status == http.StatusUnauthorized:
		return githubStatusResult{Status: githubStatusExpired,
			Message: "GitHub 拒绝了这条 user_session（HTTP 401）"}
	case status == http.StatusForbidden:
		// 403 既可能是账号被限制，也可能是出口被 GitHub 限流 —— 正文没特征就归 unknown，
		// 误判成封禁会让一个好账号被踢出池子
		return githubStatusResult{Status: githubStatusUnknown,
			Message: "GitHub 返回 403（可能是出口被限制，也可能账号受限），无法判定"}
	case status == http.StatusOK:
		if containsAny(body, githubLoggedInMarkers) {
			return githubStatusResult{Status: githubStatusActive,
				Message: "已登录，账号可用", Usable: true}
		}
		return githubStatusResult{Status: githubStatusUnknown,
			Message: "GitHub 返回 200 但看不出登录态（页面结构可能已变）"}
	default:
		return githubStatusResult{Status: githubStatusUnknown,
			Message: fmt.Sprintf("GitHub 返回 HTTP %d", status)}
	}
}

/*
probeGitHubAccountStatus 带 user_session 请求 GitHub 判定账号状态。

不跟随重定向：跳转本身就是判定依据（跳 /login 说明 session 没了）。
profileURL 供测试注入；生产传 githubProfileURL。
*/
func probeGitHubAccountStatus(ctx context.Context, httpCfg HTTPConfig, session string,
	fp githubFingerprint, profileURL string) githubStatusResult {
	if strings.TrimSpace(session) == "" {
		return githubStatusResult{Status: githubStatusUnknown, Message: "没有 user_session，无法探测"}
	}
	// 与签发链路同一套客户端构造：强制直连、不跟随重定向
	client, err := newTabiAIOAuthClient(Account{URL: profileURL}, httpCfg)
	if err != nil {
		return githubStatusResult{Status: githubStatusUnknown, Message: err.Error()}
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, profileURL, nil)
	if err != nil {
		return githubStatusResult{Status: githubStatusUnknown, Message: "构造请求失败: " + err.Error()}
	}
	req.Header.Set("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8")
	req.Header.Set("User-Agent", cookieTestDefaultUA)
	applyGitHubFingerprint(req.Header, fp)
	clean := sanitizeCookieTestHeader(session)
	req.Header.Set("Cookie", "user_session="+clean+
		"; __Host-user_session_same_site="+clean+"; logged_in=yes")

	resp, err := client.Do(req)
	if err != nil {
		return githubStatusResult{Status: githubStatusUnknown,
			Message: "GitHub 网络错误: " + shortCookieTestError(err)}
	}
	defer resp.Body.Close()
	// 只读前 64 KiB：判定只看特征词，整页拉下来纯属浪费
	raw, _ := io.ReadAll(io.LimitReader(resp.Body, 64<<10))
	return classifyGitHubProfileResponse(resp.StatusCode,
		resp.Header.Get("Location"), string(raw))
}

/*
handleCheckGitHubStatus POST /api/github-accounts/status

body: {"name": "Steven"} 或 {"user_session": "尚未入池的凭据"}

两种入参对应两个场景：按 name 查池子里已有的账号；直接传 user_session 则用于
**入池之前**先判一次 —— 被停用/封禁的账号不该加进池子，加了只会白占名额、
每轮签发都失败。
*/
func (s *Server) handleCheckGitHubStatus(w http.ResponseWriter, r *http.Request) {
	var req struct {
		Name        string `json:"name"`
		UserSession string `json:"user_session"`
	}
	if err := readJSON(w, r, &req); err != nil {
		writeError(w, http.StatusBadRequest, "请求体不是合法的 JSON")
		return
	}
	name := strings.TrimSpace(req.Name)
	session := strings.TrimSpace(req.UserSession)
	if name == "" && session == "" {
		writeError(w, http.StatusBadRequest, "name 与 user_session 至少给一个")
		return
	}

	cfg, _, err := LoadConfig(s.db)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "服务器内部错误")
		return
	}
	fp := githubFingerprint{}
	if name != "" {
		pool := findGitHubAccount(&cfg, name)
		if pool == nil {
			writeError(w, http.StatusNotFound, "GitHub 账号不存在: "+name)
			return
		}
		if session == "" {
			session = pool.UserSession
		}
		fp = deriveGitHubFingerprint(pool.Fingerprint)
	}

	// 与站点探测共用同一把锁：两者都在打 GitHub，并发只会更快撞限流
	githubCheckMu.Lock()
	defer githubCheckMu.Unlock()

	result := probeGitHubAccountStatus(r.Context(), cfg.HTTP, session, fp,
		s.githubProfileURLOrDefault())
	log.Printf("[github-accounts] 状态探测 %q → %s", name, result.Status)
	writeJSON(w, http.StatusOK, map[string]any{
		"ok": true, "name": name, "result": result,
	})
}

// githubProfileURLOrDefault 状态探测用的页面地址；测试钩子留空时回落官方地址。
func (s *Server) githubProfileURLOrDefault() string {
	if s.githubProfileURL != "" {
		return s.githubProfileURL
	}
	return githubProfileURL
}
