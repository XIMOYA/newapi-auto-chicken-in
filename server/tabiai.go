/*
server/tabiai.go
TaBiAI 凭据维护：签发（GitHub OAuth 三步换 new_api_refresh）与回写（Python 侧轮转后同步）。

为什么需要签发工具：
  - TaBiAI 的凭据 new_api_refresh 是 HttpOnly cookie，用户只能去 DevTools 里手工复制。
  - 站点支持 GitHub OAuth 登录，走三步就能拿到一条全新会话的 cookie。GitHub OAuth 已不再
    是本项目的「登录方式」，但保留这个小工具能省掉手工复制。

为什么需要回写端点：
  - 站点对 refresh token 做轮转 + 重放检测。Python 侧签到每轮都会推进代次，若不把新值同步回
    管理平台，网页端下次检测就会用旧代，可能直接把整条会话打死。
*/
package main

import (
	"bytes"
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"net/http/cookiejar"
	"net/url"
	"strings"
)

const (
	tabiaiOAuthStatePath    = "/api/oauth/state"
	tabiaiOAuthCallbackPath = "/api/oauth/github"
	tabiaiStatusPath        = "/api/status"
	tabiaiGithubAuthorize   = "https://github.com/login/oauth/authorize"
)

// updateAccountCookie 只更新指定账号的 cookie 字段并落库，不动其他字段。
// 返回 false 表示账号不存在。
//
// 与整份写入共用 configWriteMu（见 db.go）：定点写和整份写必须互斥，
// 否则整份写的陈旧快照会把这里刚落库的新一代凭据抹回旧值。
func updateAccountCookie(db *sql.DB, name, cookie string) (bool, error) {
	configWriteMu.Lock()
	defer configWriteMu.Unlock()

	cfg, _, err := loadConfigLocked(db)
	if err != nil {
		return false, err
	}
	found := false
	for i := range cfg.Accounts {
		if cfg.Accounts[i].Name == name {
			cfg.Accounts[i].Cookie = cookie
			found = true
			break
		}
	}
	if !found {
		return false, nil
	}
	// 轮转不推进 revision：否则一轮 Cookie 检测就能让所有打开的编辑页撞 409，
	// 而用户改的往往是与凭据无关的字段（详见 saveConfigLockedKeepRevision 注释）
	if _, err := saveConfigLockedKeepRevision(db, cfg); err != nil {
		return false, err
	}
	return true, nil
}

// handleWriteBackRefreshCookie POST /api/accounts/{name}/refresh-cookie（API Key）
// Python 侧签到 refresh 成功后把新一代 cookie 同步回来，保持平台与本机代次一致。
func (s *Server) handleWriteBackRefreshCookie(w http.ResponseWriter, r *http.Request) {
	name := strings.TrimSpace(r.PathValue("name"))
	if name == "" {
		writeError(w, http.StatusBadRequest, "账号名不能为空")
		return
	}
	var req struct {
		Cookie string `json:"cookie"`
	}
	if err := readJSON(w, r, &req); err != nil {
		writeError(w, http.StatusBadRequest, "请求体不是合法的 JSON")
		return
	}
	cookie := strings.TrimSpace(req.Cookie)
	if cookie == "" {
		writeError(w, http.StatusBadRequest, "cookie 不能为空")
		return
	}
	if cookie == MaskPlaceholder {
		writeError(w, http.StatusBadRequest, "cookie 不能是占位符")
		return
	}

	found, err := updateAccountCookie(s.db, name, normalizeTabiAIRefreshCookie(cookie))
	if err != nil {
		writeError(w, http.StatusInternalServerError, "服务器内部错误")
		return
	}
	if !found {
		writeError(w, http.StatusNotFound, "账号不存在: "+name)
		return
	}
	log.Printf("[tabiai] 账号 %q 的 refresh cookie 已由外部回写更新", name)
	writeJSON(w, http.StatusOK, map[string]any{"ok": true})
}

// handleIssueTabiAICookie POST /api/tabiai/issue-cookie（JWT 或 API Key）
// 用账号里保存的 GitHub user_session 走三步 OAuth，为该账号签发一条全新的 new_api_refresh。
func (s *Server) handleIssueTabiAICookie(w http.ResponseWriter, r *http.Request) {
	// 签发会换出一条全新的 sid，签到进程手里那条当场作废。跑签到时必须拦住，
	// 否则正在进行的整轮 TaBiAI 账号都会集体失败。
	if s.guardRunningCheckin(w) {
		return
	}
	var req struct {
		AccountName string `json:"account_name"`
	}
	if err := readJSON(w, r, &req); err != nil {
		writeError(w, http.StatusBadRequest, "请求体不是合法的 JSON")
		return
	}
	name := strings.TrimSpace(req.AccountName)
	if name == "" {
		writeError(w, http.StatusBadRequest, "account_name 不能为空")
		return
	}

	cfg, _, err := LoadConfig(s.db)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "服务器内部错误")
		return
	}
	var target *Account
	for i := range cfg.Accounts {
		if cfg.Accounts[i].Name == name {
			target = &cfg.Accounts[i]
			break
		}
	}
	if target == nil {
		writeError(w, http.StatusNotFound, "账号不存在: "+name)
		return
	}
	if strings.TrimSpace(target.GithubUserSession) == "" {
		writeError(w, http.StatusBadRequest,
			"该账号未填写 GitHub user_session，无法自动签发；请填写后重试，或直接从浏览器复制 new_api_refresh")
		return
	}

	cookie, err := issueTabiAIRefreshCookie(r.Context(), cfg.HTTP, *target, tabiaiGithubAuthorize)
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	found, err := updateAccountCookie(s.db, name, cookie)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "服务器内部错误")
		return
	}
	if !found {
		writeError(w, http.StatusNotFound, "账号不存在: "+name)
		return
	}
	log.Printf("[tabiai] 已为账号 %q 签发新的 refresh cookie", name)
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "account_name": name})
}

// issueTabiAIRefreshCookie 走 GitHub OAuth 三步为账号签发一条新的 new_api_refresh。
// authorizeURL 供测试注入；生产传 GitHub 官方地址。
//
// 三步必须在同一个 HTTP session 内完成（state 与站点会话绑定），因此客户端带 cookiejar。
func issueTabiAIRefreshCookie(ctx context.Context, httpCfg HTTPConfig, account Account,
	authorizeURL string) (string, error) {
	base, err := cookieTestBaseURL(account.URL)
	if err != nil {
		return "", fmt.Errorf("站点 URL 无效: %w", err)
	}
	client, err := newTabiAIOAuthClient(account, httpCfg)
	if err != nil {
		return "", err
	}

	// 第 1 步：取 flow_token（与本 session 绑定）
	statePayload, _ := json.Marshal(map[string]string{"provider": "github", "intent": "login"})
	stateReq, err := http.NewRequestWithContext(ctx, http.MethodPost,
		base+tabiaiOAuthStatePath, bytes.NewReader(statePayload))
	if err != nil {
		return "", fmt.Errorf("构造 OAuth state 请求失败: %w", err)
	}
	setCookieTestCommonHeaders(stateReq, base, "")
	stateReq.Header.Set("Content-Type", "application/json")
	stateReq.Header.Set("Cache-Control", "no-store")
	stateResp, err := client.Do(stateReq)
	if err != nil {
		return "", fmt.Errorf("OAuth state 网络错误: %s", shortCookieTestError(err))
	}
	stateBody, err := readCookieTestBody(stateResp)
	if err != nil {
		return "", fmt.Errorf("读取 OAuth state 响应失败: %w", err)
	}
	stateData, ok := cookieTestJSONMap(stateBody)
	if !ok {
		return "", fmt.Errorf("OAuth state HTTP %d 非 JSON 响应（站点可能拦截了当前出口）", stateResp.StatusCode)
	}
	if success, _ := stateData["success"].(bool); !success {
		return "", fmt.Errorf("取 OAuth state 失败: %s",
			cookieTestMessageOr(cookieTestMessage(stateData), fmt.Sprintf("HTTP %d", stateResp.StatusCode)))
	}
	state := extractTabiAIFlowToken(stateData["data"])
	if state == "" {
		return "", fmt.Errorf("OAuth state 成功但未返回 flow_token")
	}

	// 第 2 步：带 GitHub user_session 换授权 code
	clientID, err := resolveGithubClientID(ctx, client, base, account)
	if err != nil {
		return "", err
	}
	u, err := url.Parse(authorizeURL)
	if err != nil {
		return "", fmt.Errorf("GitHub authorize 地址无效")
	}
	query := u.Query()
	query.Set("client_id", clientID)
	query.Set("scope", "user:email")
	query.Set("state", state)
	u.RawQuery = query.Encode()

	authorizeReq, err := http.NewRequestWithContext(ctx, http.MethodGet, u.String(), nil)
	if err != nil {
		return "", fmt.Errorf("构造 GitHub authorize 请求失败: %w", err)
	}
	authorizeReq.Header.Set("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8")
	authorizeReq.Header.Set("User-Agent", cookieTestDefaultUA)
	session := sanitizeCookieTestHeader(account.GithubUserSession)
	authorizeReq.Header.Set("Cookie", "user_session="+session+
		"; __Host-user_session_same_site="+session+"; logged_in=yes")
	authorizeResp, err := client.Do(authorizeReq)
	if err != nil {
		return "", fmt.Errorf("GitHub authorize 网络错误: %s", shortCookieTestError(err))
	}
	defer authorizeResp.Body.Close()
	code, err := extractGithubAuthorizeCode(authorizeResp.StatusCode, authorizeResp.Header.Get("Location"))
	if err != nil {
		return "", err
	}

	// 第 3 步：回调换 new_api_refresh（必须与第 1 步同 session）
	callbackURL := base + tabiaiOAuthCallbackPath + "?" + url.Values{
		"code":  []string{code},
		"state": []string{state},
	}.Encode()
	callbackReq, err := http.NewRequestWithContext(ctx, http.MethodGet, callbackURL, nil)
	if err != nil {
		return "", fmt.Errorf("构造 OAuth 回调请求失败: %w", err)
	}
	setCookieTestCommonHeaders(callbackReq, base, "")
	callbackReq.Header.Set("Referer", sanitizeCookieTestHeader(base+"/oauth/github"))
	callbackResp, err := client.Do(callbackReq)
	if err != nil {
		return "", fmt.Errorf("OAuth 回调网络错误: %s", shortCookieTestError(err))
	}
	callbackHeader := callbackResp.Header
	callbackBody, err := readCookieTestBody(callbackResp)
	if err != nil {
		return "", fmt.Errorf("读取 OAuth 回调响应失败: %w", err)
	}
	cookie := extractTabiAIRefreshCookie(callbackHeader.Values("Set-Cookie"))
	if cookie == "" {
		if data, ok := cookieTestJSONMap(callbackBody); ok {
			if success, _ := data["success"].(bool); !success {
				return "", fmt.Errorf("OAuth 回调失败: %s",
					cookieTestMessageOr(cookieTestMessage(data), fmt.Sprintf("HTTP %d", callbackResp.StatusCode)))
			}
		}
		return "", fmt.Errorf("OAuth 回调成功但站点未下发 new_api_refresh（HTTP %d）", callbackResp.StatusCode)
	}
	return cookie, nil
}

// newTabiAIOAuthClient 带 cookiejar 的客户端：三步 OAuth 的 state 与站点会话绑定，必须同一 session。
// 统一不跟随重定向 —— 第 2 步要读 Location 取 code，第 3 步只关心 Set-Cookie。
func newTabiAIOAuthClient(account Account, httpCfg HTTPConfig) (*http.Client, error) {
	client, err := newCookieTestHTTPClient(account, httpCfg, false, "")
	if err != nil {
		return nil, fmt.Errorf("HTTP 客户端配置失败: %w", err)
	}
	jar, err := cookiejar.New(nil)
	if err != nil {
		return nil, fmt.Errorf("初始化 cookie jar 失败: %w", err)
	}
	client.Jar = jar
	return client, nil
}

// resolveGithubClientID 站点自己的 OAuth 应用 ID：账号显式配置优先，其次读 /api/status。
// 绝不使用内置默认值 —— 用错 client_id 会授权到别人的应用上。
func resolveGithubClientID(ctx context.Context, client *http.Client, base string, account Account) (string, error) {
	if id := strings.TrimSpace(account.GithubClientID); id != "" {
		return id, nil
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, base+tabiaiStatusPath, nil)
	if err != nil {
		return "", fmt.Errorf("构造站点状态请求失败: %w", err)
	}
	setCookieTestCommonHeaders(req, base, "")
	resp, err := client.Do(req)
	if err != nil {
		return "", fmt.Errorf("站点状态网络错误: %s", shortCookieTestError(err))
	}
	body, err := readCookieTestBody(resp)
	if err != nil {
		return "", fmt.Errorf("读取站点状态失败: %w", err)
	}
	data, ok := cookieTestJSONMap(body)
	if !ok || resp.StatusCode >= 400 {
		return "", fmt.Errorf("站点状态 HTTP %d 非法响应", resp.StatusCode)
	}
	payload, _ := data["data"].(map[string]any)
	id := ""
	if payload != nil {
		id = strings.TrimSpace(cookieTestString(payload["github_client_id"]))
	}
	if id == "" {
		return "", fmt.Errorf("站点状态未返回 github_client_id，请在账号里手动填写")
	}
	return id, nil
}

// extractGithubAuthorizeCode 从 authorize 的 302 里取 code，并把常见失败翻译成人话。
func extractGithubAuthorizeCode(status int, location string) (string, error) {
	redirect := status == http.StatusMovedPermanently || status == http.StatusFound ||
		status == http.StatusSeeOther || status == http.StatusTemporaryRedirect ||
		status == http.StatusPermanentRedirect
	if location != "" {
		if parsed, err := url.Parse(location); err == nil &&
			strings.Contains(strings.ToLower(parsed.Path), "/login") {
			return "", fmt.Errorf("GitHub 要求重新登录，user_session 已失效")
		}
	}
	if !redirect {
		if status == http.StatusForbidden || status == http.StatusTooManyRequests {
			return "", fmt.Errorf("GitHub authorize HTTP %d，当前出口被 GitHub 限制，稍后再试", status)
		}
		return "", fmt.Errorf("GitHub 未返回授权重定向（HTTP %d），可能需要先在 GitHub 授权该 OAuth 应用", status)
	}
	parsed, err := url.Parse(location)
	if err != nil {
		return "", fmt.Errorf("GitHub 返回的跳转地址无法解析")
	}
	if code := parsed.Query().Get("code"); code != "" {
		return code, nil
	}
	reason := parsed.Query().Get("error_description")
	if reason == "" {
		reason = parsed.Query().Get("error")
	}
	if reason != "" {
		return "", fmt.Errorf("GitHub 未返回 code: %s", sanitizeCookieTestMessage(reason))
	}
	return "", fmt.Errorf("GitHub 未返回授权 code（HTTP %d）", status)
}

// extractTabiAIFlowToken 站点把 state 放在 data.flow_token；旧结构直接给字符串时也接受。
func extractTabiAIFlowToken(value any) string {
	if payload, ok := value.(map[string]any); ok {
		for _, key := range []string{"flow_token", "state"} {
			if token := strings.TrimSpace(cookieTestString(payload[key])); token != "" {
				return token
			}
		}
		return ""
	}
	return strings.TrimSpace(cookieTestString(value))
}
