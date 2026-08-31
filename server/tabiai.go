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
//
// 账号定位规则（路径参数 trim 后与 accounts[].name 精确比较）与读回核实端点
// lookupAccountByPath 共用一套：客户端写完会立刻拉 GET /api/accounts/{name}/raw 比对，
// 两边口径不一致就会「写得进、核不到」，把成功的回写误判成失败。
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
//
// 两个调用方，行为有别：
//   - 网页端（JWT）：人工点「签发」。签到进行中会被签到锁拦住 —— 那是防手滑，
//     人工签发换出的新 sid 会让正在跑的那一轮当场作废。
//   - 签到客户端（API Key + for_running_checkin）：refresh 过期时自救。必须放行签到锁，
//     否则自救永远撞 409；而且要把新凭据回给它，它得立刻拿去重试本轮。
//     签发是按账号的，只影响请求里这一个，不会波及同轮其他账号。
func (s *Server) handleIssueTabiAICookie(w http.ResponseWriter, r *http.Request) {
	var req struct {
		AccountName string `json:"account_name"`
		// ForRunningCheckin 由签到进程置 true：它自己就是那个「正在跑的签到」，
		// 被自己的锁拦住毫无意义。放行范围仅限本端点、且只作用于请求里那个账号。
		ForRunningCheckin bool `json:"for_running_checkin"`
	}
	if err := readJSON(w, r, &req); err != nil {
		writeError(w, http.StatusBadRequest, "请求体不是合法的 JSON")
		return
	}
	// 签发会换出一条全新的 sid，签到进程手里那条当场作废。人工签发时必须拦住，
	// 否则正在进行的整轮 TaBiAI 账号都会集体失败。
	if !req.ForRunningCheckin && s.guardRunningCheckin(w) {
		return
	}
	name := strings.TrimSpace(req.AccountName)
	if name == "" {
		writeError(w, http.StatusBadRequest, "account_name 不能为空")
		return
	}
	if req.ForRunningCheckin {
		log.Printf("[tabiai] 账号 %q 由签到进程请求签发（已放行签到锁）", name)
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
	// 凭据取「实际生效值」：引用的 GitHub 账号池优先，账号自带的旧字段兜底。
	// 副本只用于这一次签发，不落库（见 effectiveGitHubCredentials）
	effective := effectiveGitHubCredentials(&cfg, *target)
	if strings.TrimSpace(effective.GithubUserSession) == "" {
		writeError(w, http.StatusBadRequest,
			"该账号拿不到 GitHub user_session（自身没填，引用的 GitHub 账号池里也没有），"+
				"无法自动签发；请补上后重试，或直接从浏览器复制 new_api_refresh")
		return
	}

	// 出口取这个 GitHub 账号绑定的那个：同一条 session 必须始终从同一个 IP 出现
	_, outbound := s.prepareGitHubOutbound(&cfg, target.GitHubAccount)

	cookie, err := issueTabiAIRefreshCookie(r.Context(), cfg.HTTP, effective,
		s.githubAuthorizeURLOrDefault(), effectiveGitHubFingerprint(&cfg, *target), outbound)
	if err != nil {
		// 链路类失败才解绑：凭据失效换出口没用，而这个出口连不上 GitHub 的话
		// 留着下次还是失败。判据是错误信息里的网络/限流特征
		if outbound != "" && isOutboundFailure(err.Error()) {
			s.releaseAndPersistGitHubOutbound(&cfg, target.GitHubAccount,
				"签发时链路不通: "+err.Error())
		}
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
	body := map[string]any{"ok": true, "account_name": name}
	// 明文只回给 API Key 调用方（签到客户端要拿它立刻重试本轮）。
	// 网页端走 JWT，它签发完刷新页面就行，没有拿明文的必要 —— 不下发就少一个泄漏面。
	// 暴露面没有新增：API Key 持有者本来就能 GET /api/config/raw 拉走整份明文。
	if isAPIKeyRequest(r) {
		body["cookie"] = cookie
	}
	writeJSON(w, http.StatusOK, body)
}

// fetchTabiAIOAuthState 第 1 步：取 flow_token（与本次 HTTP session 绑定）。
// 后面第 3 步回调必须用同一个客户端带着这份 state 回去。
func fetchTabiAIOAuthState(ctx context.Context, client *http.Client, base string,
	fp githubFingerprint) (string, error) {
	statePayload, _ := json.Marshal(map[string]string{"provider": "github", "intent": "login"})
	stateReq, err := http.NewRequestWithContext(ctx, http.MethodPost,
		base+tabiaiOAuthStatePath, bytes.NewReader(statePayload))
	if err != nil {
		return "", fmt.Errorf("构造 OAuth state 请求失败: %w", err)
	}
	setCookieTestCommonHeaders(stateReq, base, "")
	// 站点这一跳也用同一份指纹：一个「设备」访问站点和 GitHub 时特征应当一致
	applyGitHubFingerprint(stateReq.Header, fp)
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
	return state, nil
}

// fetchGithubAuthorizeCode 第 2 步：带 GitHub user_session 换授权 code。
// 与第 1 步共用同一个 client，保证 state 与站点会话绑定。
// authorizeURL 供测试注入；生产传 GitHub 官方地址。
func fetchGithubAuthorizeCode(ctx context.Context, client *http.Client, base string,
	account Account, state, authorizeURL string, fp githubFingerprint) (string, error) {
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
	// 账号自己的固定指纹覆盖全局默认 UA。GitHub 的 session 绑设备特征，
	// 几个账号共用一个 UA 时其中一个被盯上，其余的特征完全一致
	applyGitHubFingerprint(authorizeReq.Header, fp)
	session := sanitizeCookieTestHeader(account.GithubUserSession)
	authorizeReq.Header.Set("Cookie", "user_session="+session+
		"; __Host-user_session_same_site="+session+"; logged_in=yes")
	authorizeResp, err := client.Do(authorizeReq)
	if err != nil {
		return "", fmt.Errorf("GitHub authorize 网络错误: %s", shortCookieTestError(err))
	}
	defer authorizeResp.Body.Close()
	return extractGithubAuthorizeCode(authorizeResp.StatusCode, authorizeResp.Header.Get("Location"))
}

// issueTabiAIRefreshCookie 走 GitHub OAuth 三步为账号签发一条新的 new_api_refresh。
// authorizeURL 供测试注入；生产传 GitHub 官方地址。
//
// 三步必须在同一个 HTTP session 内完成（state 与站点会话绑定），因此客户端带 cookiejar。
//
// **全程用平台自己的出口直连，不走任何代理**（既不用代理池，也忽略账号自带的
// account.Proxy）。这是刻意的：GitHub 的 OAuth 端点对机房 IP 有明显限流（403/429），
// 而带着 user_session 从不断变化的地址出现，正是触发 GitHub 账号风控最快的方式 ——
// 最坏会把 user_session 直接作废，自救链路彻底断掉。平台部署在固定 IP 上，
// 在 GitHub 眼里是「常用设备」，成功率和安全性都比套代理好。
//
// 客户端（Actions）签到时的凭据轮转与签到本身照旧走代理，那与这里无关。
func issueTabiAIRefreshCookie(ctx context.Context, httpCfg HTTPConfig, account Account,
	authorizeURL string, fp githubFingerprint, outbound string) (string, error) {
	base, err := cookieTestBaseURL(account.URL)
	if err != nil {
		return "", fmt.Errorf("站点 URL 无效: %w", err)
	}
	client, err := newTabiAIOAuthClient(account, httpCfg, outbound)
	if err != nil {
		return "", err
	}

	// 第 1 步：取 flow_token（与本 session 绑定）
	state, err := fetchTabiAIOAuthState(ctx, client, base, fp)
	if err != nil {
		return "", err
	}

	// 第 2 步：带 GitHub user_session 换授权 code
	code, err := fetchGithubAuthorizeCode(ctx, client, base, account, state, authorizeURL, fp)
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
	applyGitHubFingerprint(callbackReq.Header, fp)
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
//
// outbound 是这次要走的出口（空串=直连）。**账号自带的 account.Proxy 一律忽略**：
// 出口由 GitHub 账号的固定绑定决定，不是由站点账号决定 —— 同一个 GitHub 会话必须
// 始终从同一个 IP 出现，而 account.Proxy 是「这个站点账号签到时用哪个出口」，
// 两者诉求不同。Cookie 检测那边仍照旧用账号代理，那是在验「凭据在这个出口下能不能用」。
func newTabiAIOAuthClient(account Account, httpCfg HTTPConfig,
	outbound string) (*http.Client, error) {
	bound := account
	if trimmed := strings.TrimSpace(outbound); trimmed != "" {
		bound.Proxy = &trimmed
	} else {
		bound.Proxy = nil
	}
	client, err := newCookieTestHTTPClient(bound, httpCfg, false, "")
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
