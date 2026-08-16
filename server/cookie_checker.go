/*
server/cookie_checker.go
NewAPI 签到配置管理平台 · Cookie 可用性检测

职责：
- 参考 newapi-cookie-check 实现 NewAPI Cookie 的 direct / refresh 检测
- 独立实现 GitHub Cookie 的 OAuth state + authorize 凭据检查
- 只返回脱敏后的检测结果，不把 Cookie、token、state 或 OAuth code 返回给前端
*/
package main

import (
	"context"
	"crypto/tls"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
)

const (
	cookieTestStateValid    = "valid"
	cookieTestStateInvalid  = "invalid"
	cookieTestStateAbnormal = "abnormal"
	cookieTestStateSkipped  = "skipped"

	cookieTestSelfPath            = "/api/user/self"
	cookieTestRefreshPath         = "/api/user/auth/refresh"
	cookieTestOAuthStatePath      = "/api/oauth/state"
	cookieTestOAuthStateLegacyURL = "/api/oauth/state?mode=login"
	cookieTestStatusPath          = "/api/status"
	cookieTestGithubAuthorize     = "https://github.com/login/oauth/authorize"
	cookieTestRefreshTokenKey     = "new_api_refresh="
	cookieTestDefaultClientID     = "Ov23lidtiR4LeVZvVRNL"
	cookieTestDefaultUA           = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

var cookieTestAuthMarkers = []string{
	"未登录", "无权限", "登录已过期", "无效的凭证", "请先登录", "身份验证",
	"unauthorized", "auth_unauthorized", "auth_session_revoked",
	"invalid credential", "not logged in", "forbidden",
}

// CookieTestResult 单个账号的 Cookie 检测结果。
type CookieTestResult struct {
	Name       string `json:"name"`
	URL        string `json:"url"`
	State      string `json:"state"`
	UserID     *int64 `json:"user_id"`
	DurationMS int64  `json:"duration_ms"`
	Message    string `json:"message"`
}

// CookieTestSummary 一轮检测的状态汇总。
type CookieTestSummary struct {
	Total    int `json:"total"`
	Valid    int `json:"valid"`
	Invalid  int `json:"invalid"`
	Abnormal int `json:"abnormal"`
	Skipped  int `json:"skipped"`
}

// CookieTestResponse Cookie 检测接口响应；不包含任何敏感凭据。
type CookieTestResponse struct {
	Mode      string             `json:"mode"`
	CheckedAt string             `json:"checked_at"`
	Summary   CookieTestSummary  `json:"summary"`
	Results   []CookieTestResult `json:"results"`
}

func summarizeCookieTestResults(results []CookieTestResult) CookieTestSummary {
	summary := CookieTestSummary{Total: len(results)}
	for _, result := range results {
		switch result.State {
		case cookieTestStateValid:
			summary.Valid++
		case cookieTestStateInvalid:
			summary.Invalid++
		case cookieTestStateAbnormal:
			summary.Abnormal++
		case cookieTestStateSkipped:
			summary.Skipped++
		}
	}
	return summary
}

// runCookieTests 并发检测指定登录方式的启用账号，结果顺序与配置顺序一致。
func runCookieTests(ctx context.Context, cfg *Config, mode string, names []string) ([]CookieTestResult, error) {
	if cfg == nil {
		return nil, fmt.Errorf("配置不能为空")
	}
	if mode != LoginMethodNewAPICookie && mode != LoginMethodGitHubCookie {
		return nil, fmt.Errorf("不支持的 Cookie 测试类型: %s", mode)
	}

	nameSet := make(map[string]struct{}, len(names))
	for _, name := range names {
		name = strings.TrimSpace(name)
		if name != "" {
			nameSet[name] = struct{}{}
		}
	}

	targets := make([]Account, 0, len(cfg.Accounts))
	for _, account := range cfg.Accounts {
		method := strings.TrimSpace(account.LoginMethod)
		if method == "" {
			method = LoginMethodNewAPICookie
		}
		if !account.Enabled || method != mode {
			continue
		}
		if len(nameSet) > 0 {
			if _, ok := nameSet[account.Name]; !ok {
				continue
			}
		}
		targets = append(targets, account)
	}
	if len(targets) == 0 {
		return nil, fmt.Errorf("没有匹配 %s 的启用账号", cookieTestModeLabel(mode))
	}

	results := make([]CookieTestResult, len(targets))
	sem := make(chan struct{}, 5)
	var wg sync.WaitGroup
	for i, account := range targets {
		wg.Add(1)
		go func(index int, target Account) {
			defer wg.Done()
			select {
			case sem <- struct{}{}:
			case <-ctx.Done():
				results[index] = cookieTestResult(target, cookieTestStateSkipped, "检测已取消", nil)
				return
			}
			defer func() { <-sem }()
			results[index] = checkCookieAccount(ctx, cfg.HTTP, target, mode)
		}(i, account)
	}
	wg.Wait()
	return results, nil
}

func cookieTestModeLabel(mode string) string {
	if mode == LoginMethodGitHubCookie {
		return "github_cookie"
	}
	return "newapi_cookie"
}

func checkCookieAccount(ctx context.Context, httpCfg HTTPConfig, account Account, mode string) CookieTestResult {
	started := time.Now()
	var result CookieTestResult
	if mode == LoginMethodGitHubCookie {
		if strings.TrimSpace(account.GithubUserSession) == "" {
			result = cookieTestResult(account, cookieTestStateSkipped, "缺少 GitHub Cookie（github_user_session）", nil)
		} else {
			result = checkGithubCookie(ctx, httpCfg, account)
		}
	} else if strings.TrimSpace(account.Cookie) == "" {
		result = cookieTestResult(account, cookieTestStateSkipped, "缺少 NewAPI Cookie（cookie）", nil)
	} else {
		result = checkNewAPICookie(ctx, httpCfg, account)
	}
	result.DurationMS = time.Since(started).Milliseconds()
	return result
}

func cookieTestResult(account Account, state, message string, userID *int64) CookieTestResult {
	return CookieTestResult{
		Name:    account.Name,
		URL:     account.URL,
		State:   state,
		UserID:  userID,
		Message: message,
	}
}

func checkNewAPICookie(ctx context.Context, httpCfg HTTPConfig, account Account) CookieTestResult {
	base, err := cookieTestBaseURL(account.URL)
	if err != nil {
		return cookieTestResult(account, cookieTestStateAbnormal, "站点 URL 无效: "+err.Error(), nil)
	}
	client, err := newCookieTestHTTPClient(account, httpCfg, true)
	if err != nil {
		return cookieTestResult(account, cookieTestStateAbnormal, "HTTP 客户端配置失败: "+err.Error(), nil)
	}
	if strings.Contains(account.Cookie, cookieTestRefreshTokenKey) {
		return checkNewAPIWithRefresh(ctx, client, base, account)
	}
	return checkNewAPIDirect(ctx, client, base, account)
}

func checkNewAPIDirect(ctx context.Context, client *http.Client, base string, account Account) CookieTestResult {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, base+cookieTestSelfPath, nil)
	if err != nil {
		return cookieTestResult(account, cookieTestStateAbnormal, "构造 self 请求失败: "+err.Error(), nil)
	}
	setCookieTestCommonHeaders(req, base, account.Cookie)
	setCookieTestUserID(req, account.UserID)

	resp, err := client.Do(req)
	if err != nil {
		return cookieTestResult(account, cookieTestStateAbnormal, "网络错误: "+shortCookieTestError(err), nil)
	}
	body, readErr := readCookieTestBody(resp)
	if readErr != nil {
		return cookieTestResult(account, cookieTestStateAbnormal, "读取响应失败: "+readErr.Error(), nil)
	}
	return classifyCookieTestSelf(account, resp.StatusCode, body, nil)
}

func checkNewAPIWithRefresh(ctx context.Context, client *http.Client, base string, account Account) CookieTestResult {
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, base+cookieTestRefreshPath, nil)
	if err != nil {
		return cookieTestResult(account, cookieTestStateAbnormal, "构造 refresh 请求失败: "+err.Error(), nil)
	}
	setCookieTestCommonHeaders(req, base, account.Cookie)
	req.Header.Set("Content-Type", "application/json")

	resp, err := client.Do(req)
	if err != nil {
		return cookieTestResult(account, cookieTestStateAbnormal, "refresh 网络错误: "+shortCookieTestError(err), nil)
	}
	body, readErr := readCookieTestBody(resp)
	if readErr != nil {
		return cookieTestResult(account, cookieTestStateAbnormal, "读取 refresh 响应失败: "+readErr.Error(), nil)
	}
	if result, done := classifyCookieTestRefresh(account, resp.StatusCode, body); done {
		return result
	}

	bundle := parseCookieTestRefreshBundle(account.Cookie, resp.Header.Values("Set-Cookie"), body)
	if bundle.authorization == "" && !bundle.hasSessionCookie {
		return cookieTestResult(account, cookieTestStateAbnormal, "refresh 成功但未返回 access token 或 session cookie", bundle.userID)
	}

	selfReq, err := http.NewRequestWithContext(ctx, http.MethodGet, base+cookieTestSelfPath, nil)
	if err != nil {
		return cookieTestResult(account, cookieTestStateAbnormal, "构造 self 请求失败: "+err.Error(), bundle.userID)
	}
	setCookieTestCommonHeaders(selfReq, base, bundle.cookieHeader)
	if bundle.authorization != "" {
		selfReq.Header.Set("Authorization", bundle.authorization)
	}
	setCookieTestUserID(selfReq, account.UserID)

	selfResp, err := client.Do(selfReq)
	if err != nil {
		return cookieTestResult(account, cookieTestStateAbnormal, "self 网络错误: "+shortCookieTestError(err), bundle.userID)
	}
	selfBody, readErr := readCookieTestBody(selfResp)
	if readErr != nil {
		return cookieTestResult(account, cookieTestStateAbnormal, "读取 self 响应失败: "+readErr.Error(), bundle.userID)
	}
	return classifyCookieTestSelf(account, selfResp.StatusCode, selfBody, bundle.userID)
}

func newCookieTestHTTPClient(account Account, httpCfg HTTPConfig, followRedirects bool) (*http.Client, error) {
	timeout := time.Duration(httpCfg.Timeout) * time.Second
	if timeout <= 0 {
		timeout = 20 * time.Second
	}

	transport := &http.Transport{
		TLSClientConfig:     &tls.Config{InsecureSkipVerify: !httpCfg.Verify}, // #nosec G402 -- 由管理端 HTTP 配置显式控制
		ForceAttemptHTTP2:   true,
		MaxIdleConns:        100,
		MaxIdleConnsPerHost: 16,
		IdleConnTimeout:     90 * time.Second,
		Proxy:               http.ProxyFromEnvironment,
	}
	if account.Proxy != nil && strings.TrimSpace(*account.Proxy) != "" {
		proxyURL, err := url.Parse(strings.TrimSpace(*account.Proxy))
		if err != nil || proxyURL.Scheme == "" || proxyURL.Host == "" {
			return nil, fmt.Errorf("代理地址无效")
		}
		transport.Proxy = http.ProxyURL(proxyURL)
	}

	client := &http.Client{Transport: transport, Timeout: timeout}
	if !followRedirects {
		client.CheckRedirect = func(_ *http.Request, _ []*http.Request) error {
			return http.ErrUseLastResponse
		}
	}
	return client, nil
}

func cookieTestBaseURL(raw string) (string, error) {
	trimmed := strings.TrimSpace(raw)
	u, err := url.Parse(trimmed)
	if err != nil || u.Host == "" || (u.Scheme != "http" && u.Scheme != "https") {
		return "", fmt.Errorf("必须是有效的 http(s) URL")
	}
	return strings.TrimRight(u.String(), "/"), nil
}

func setCookieTestCommonHeaders(req *http.Request, base, cookie string) {
	req.Header.Set("Accept", "application/json, text/plain, */*")
	req.Header.Set("Accept-Language", "zh-CN,zh;q=0.9,en;q=0.8")
	req.Header.Set("Referer", sanitizeCookieTestHeader(base+"/"))
	req.Header.Set("Origin", sanitizeCookieTestHeader(base))
	req.Header.Set("User-Agent", cookieTestDefaultUA)
	if cookie != "" {
		req.Header.Set("Cookie", sanitizeCookieTestHeader(cookie))
	}
}

func setCookieTestUserID(req *http.Request, userID *int64) {
	if userID != nil && *userID > 0 {
		req.Header.Set("New-Api-User", strconv.FormatInt(*userID, 10))
	}
}

func readCookieTestBody(resp *http.Response) ([]byte, error) {
	defer resp.Body.Close()
	return io.ReadAll(io.LimitReader(resp.Body, 4<<20))
}

func classifyCookieTestRefresh(account Account, status int, body []byte) (CookieTestResult, bool) {
	data, hasJSON := cookieTestJSONMap(body)
	message := ""
	code := ""
	if hasJSON {
		message = cookieTestMessage(data)
		code = cookieTestString(data["code"])
	}
	lower := strings.ToLower(message + " " + code)
	if status == http.StatusUnauthorized || status == http.StatusForbidden {
		return cookieTestResult(account, cookieTestStateInvalid, fmt.Sprintf("refresh 失败: HTTP %d", status), nil), true
	}
	if cookieTestContainsAny(lower, cookieTestAuthMarkers) {
		return cookieTestResult(account, cookieTestStateInvalid, "refresh 失败: "+cookieTestMessageOr(message, fmt.Sprintf("HTTP %d", status)), nil), true
	}
	if status >= 500 {
		return cookieTestResult(account, cookieTestStateAbnormal, fmt.Sprintf("refresh HTTP %d 服务器错误", status), nil), true
	}
	if status >= 400 {
		return cookieTestResult(account, cookieTestStateAbnormal, "refresh 失败: "+cookieTestMessageOr(message, fmt.Sprintf("HTTP %d", status)), nil), true
	}
	if hasJSON {
		if success, ok := data["success"].(bool); ok && !success {
			return cookieTestResult(account, cookieTestStateAbnormal, "refresh 失败: "+cookieTestMessageOr(message, fmt.Sprintf("HTTP %d", status)), nil), true
		}
	}
	return CookieTestResult{}, false
}

type cookieTestRefreshBundle struct {
	authorization    string
	cookieHeader     string
	hasSessionCookie bool
	userID           *int64
}

func parseCookieTestRefreshBundle(original string, setCookies []string, body []byte) cookieTestRefreshBundle {
	bundle := cookieTestRefreshBundle{}
	cookies := parseCookieTestCookies(original)
	for _, raw := range setCookies {
		name, value := parseCookieTestSetCookie(raw)
		if name == "" {
			continue
		}
		if name == "session" || name == "access" || name == "access_token" {
			bundle.hasSessionCookie = true
		}
		cookies[name] = value
	}
	bundle.cookieHeader = buildCookieTestHeader(cookies)

	if data, ok := cookieTestJSONMap(body); ok {
		if token := cookieTestExtractAccessToken(data); token != "" {
			bundle.authorization = cookieTestAuthorizationHeader(cookieTestString(data["token_type"]), token)
		}
		bundle.userID = cookieTestExtractUserID(data)
	}
	return bundle
}

func cookieTestExtractAccessToken(data map[string]any) string {
	for _, key := range []string{"access_token", "accessToken", "token"} {
		if value := cookieTestExtractField(data, key); value != "" {
			return value
		}
	}
	return ""
}

func cookieTestExtractField(data map[string]any, key string) string {
	if value := cookieTestString(data[key]); value != "" {
		return value
	}
	if nested, ok := data["data"].(map[string]any); ok {
		return cookieTestExtractField(nested, key)
	}
	return ""
}

func cookieTestExtractUserID(data map[string]any) *int64 {
	nested, ok := data["data"].(map[string]any)
	if !ok {
		return nil
	}
	user, ok := nested["user"].(map[string]any)
	if !ok {
		return nil
	}
	id, ok := cookieTestInt64(user["id"])
	if !ok || id <= 0 {
		return nil
	}
	return &id
}

func cookieTestAuthorizationHeader(tokenType, token string) string {
	tokenType = strings.TrimSpace(tokenType)
	if tokenType == "" {
		tokenType = "Bearer"
	}
	return sanitizeCookieTestHeader(tokenType + " " + token)
}

func classifyCookieTestSelf(account Account, status int, body []byte, fallbackID *int64) CookieTestResult {
	data, hasJSON := cookieTestJSONMap(body)
	if !hasJSON {
		switch {
		case status == http.StatusUnauthorized || status == http.StatusForbidden:
			return cookieTestResult(account, cookieTestStateInvalid, fmt.Sprintf("HTTP %d", status), fallbackID)
		case status >= 500:
			return cookieTestResult(account, cookieTestStateAbnormal, fmt.Sprintf("HTTP %d 服务器错误", status), fallbackID)
		default:
			return cookieTestResult(account, cookieTestStateAbnormal, fmt.Sprintf("HTTP %d 非 JSON 响应", status), fallbackID)
		}
	}

	message := cookieTestMessageOr(cookieTestMessage(data), fmt.Sprintf("HTTP %d", status))
	if status == http.StatusUnauthorized || status == http.StatusForbidden {
		return cookieTestResult(account, cookieTestStateInvalid, message, fallbackID)
	}
	if status >= 500 {
		return cookieTestResult(account, cookieTestStateAbnormal, fmt.Sprintf("HTTP %d 服务器错误", status), fallbackID)
	}

	success, _ := data["success"].(bool)
	if !success {
		if cookieTestContainsAny(strings.ToLower(message), cookieTestAuthMarkers) {
			return cookieTestResult(account, cookieTestStateInvalid, message, fallbackID)
		}
		return cookieTestResult(account, cookieTestStateAbnormal, message, fallbackID)
	}

	payload, ok := data["data"].(map[string]any)
	if !ok {
		return cookieTestResult(account, cookieTestStateAbnormal, "响应缺少 data", fallbackID)
	}
	id, ok := cookieTestInt64(payload["id"])
	if !ok || id <= 0 {
		if fallbackID == nil || *fallbackID <= 0 {
			return cookieTestResult(account, cookieTestStateAbnormal, "响应缺少 data.id", nil)
		}
		id = *fallbackID
	}
	userID := &id
	username := cookieTestString(payload["username"])
	if username == "" {
		username = cookieTestString(payload["display_name"])
	}
	if username == "" {
		username = "Cookie 有效"
	}
	return cookieTestResult(account, cookieTestStateValid, username, userID)
}

func checkGithubCookie(ctx context.Context, httpCfg HTTPConfig, account Account) CookieTestResult {
	return checkGithubCookieWithAuthorizeURL(ctx, httpCfg, account, cookieTestGithubAuthorize)
}

// checkGithubCookieWithAuthorizeURL 为测试注入 authorize 地址；生产路径使用 GitHub 官方地址。
func checkGithubCookieWithAuthorizeURL(ctx context.Context, httpCfg HTTPConfig, account Account, authorizeURL string) CookieTestResult {
	base, err := cookieTestBaseURL(account.URL)
	if err != nil {
		return cookieTestResult(account, cookieTestStateAbnormal, "站点 URL 无效: "+err.Error(), nil)
	}
	client, err := newCookieTestHTTPClient(account, httpCfg, false)
	if err != nil {
		return cookieTestResult(account, cookieTestStateAbnormal, "HTTP 客户端配置失败: "+err.Error(), nil)
	}

	state, stateResult := fetchCookieTestOAuthState(ctx, client, base, account)
	if stateResult != nil {
		return *stateResult
	}

	u, err := url.Parse(authorizeURL)
	if err != nil {
		return cookieTestResult(account, cookieTestStateAbnormal, "GitHub authorize 地址无效", nil)
	}
	query := u.Query()
	query.Set("client_id", resolveCookieTestGithubClientID(ctx, client, base, account))
	query.Set("scope", "user:email")
	query.Set("state", state)
	u.RawQuery = query.Encode()

	authorizeReq, err := http.NewRequestWithContext(ctx, http.MethodGet, u.String(), nil)
	if err != nil {
		return cookieTestResult(account, cookieTestStateAbnormal, "构造 GitHub authorize 请求失败: "+err.Error(), nil)
	}
	authorizeReq.Header.Set("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8")
	authorizeReq.Header.Set("Accept-Language", "zh-CN,zh;q=0.9,en;q=0.8")
	authorizeReq.Header.Set("User-Agent", cookieTestDefaultUA)
	authorizeReq.Header.Set("Cookie", buildCookieTestHeader(map[string]string{
		"__Host-user_session_same_site": sanitizeCookieTestHeader(account.GithubUserSession),
		"logged_in":                     "yes",
		"user_session":                  sanitizeCookieTestHeader(account.GithubUserSession),
	}))
	authorizeReq.Header.Set("Referer", sanitizeCookieTestHeader(base+"/login"))

	authorizeResp, err := client.Do(authorizeReq)
	if err != nil {
		return cookieTestResult(account, cookieTestStateAbnormal, "GitHub authorize 网络错误: "+shortCookieTestError(err), nil)
	}
	defer authorizeResp.Body.Close()
	return classifyCookieTestGithubAuthorize(account, authorizeResp.StatusCode, authorizeResp.Header.Get("Location"))
}

func fetchCookieTestOAuthState(ctx context.Context, client *http.Client, base string, account Account) (string, *CookieTestResult) {
	postReq, err := http.NewRequestWithContext(ctx, http.MethodPost, base+cookieTestOAuthStatePath,
		strings.NewReader(`{"provider":"github","intent":"login"}`))
	if err != nil {
		result := cookieTestResult(account, cookieTestStateAbnormal, "构造 OAuth state 请求失败: "+err.Error(), nil)
		return "", &result
	}
	setCookieTestCommonHeaders(postReq, base, "")
	postReq.Header.Set("Content-Type", "application/json")
	postResp, err := client.Do(postReq)
	if err != nil {
		result := cookieTestResult(account, cookieTestStateAbnormal, "OAuth state 网络错误: "+shortCookieTestError(err), nil)
		return "", &result
	}
	postBody, readErr := readCookieTestBody(postResp)
	if readErr != nil {
		result := cookieTestResult(account, cookieTestStateAbnormal, "读取 OAuth state 响应失败: "+readErr.Error(), nil)
		return "", &result
	}
	state, stateResult := classifyCookieTestOAuthState(account, postResp.StatusCode, postBody)
	if stateResult == nil {
		return state, nil
	}
	if !cookieTestShouldFallbackOAuthState(postResp.StatusCode) {
		return "", stateResult
	}

	legacyReq, err := http.NewRequestWithContext(ctx, http.MethodGet, base+cookieTestOAuthStateLegacyURL, nil)
	if err != nil {
		result := cookieTestResult(account, cookieTestStateAbnormal, "构造兼容 OAuth state 请求失败: "+err.Error(), nil)
		return "", &result
	}
	setCookieTestCommonHeaders(legacyReq, base, "")
	legacyResp, err := client.Do(legacyReq)
	if err != nil {
		result := cookieTestResult(account, cookieTestStateAbnormal, "兼容 OAuth state 网络错误: "+shortCookieTestError(err), nil)
		return "", &result
	}
	legacyBody, readErr := readCookieTestBody(legacyResp)
	if readErr != nil {
		result := cookieTestResult(account, cookieTestStateAbnormal, "读取兼容 OAuth state 响应失败: "+readErr.Error(), nil)
		return "", &result
	}
	return classifyCookieTestOAuthState(account, legacyResp.StatusCode, legacyBody)
}

func cookieTestShouldFallbackOAuthState(status int) bool {
	return status == http.StatusBadRequest || status == http.StatusNotFound ||
		status == http.StatusMethodNotAllowed || status == http.StatusNotImplemented
}

func resolveCookieTestGithubClientID(ctx context.Context, client *http.Client, base string, account Account) string {
	if clientID := fetchCookieTestGithubClientID(ctx, client, base); clientID != "" {
		return clientID
	}
	if clientID := strings.TrimSpace(account.GithubClientID); clientID != "" {
		return clientID
	}
	return cookieTestDefaultClientID
}

func fetchCookieTestGithubClientID(ctx context.Context, client *http.Client, base string) string {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, base+cookieTestStatusPath, nil)
	if err != nil {
		return ""
	}
	setCookieTestCommonHeaders(req, base, "")
	resp, err := client.Do(req)
	if err != nil {
		return ""
	}
	body, err := readCookieTestBody(resp)
	if err != nil || resp.StatusCode < http.StatusOK || resp.StatusCode >= http.StatusMultipleChoices {
		return ""
	}
	data, ok := cookieTestJSONMap(body)
	if !ok {
		return ""
	}
	return cookieTestExtractGithubClientID(data)
}

func cookieTestExtractGithubClientID(data map[string]any) string {
	for _, key := range []string{"github_client_id", "githubClientId"} {
		if clientID := cookieTestString(data[key]); clientID != "" {
			return clientID
		}
	}
	if nested, ok := data["data"].(map[string]any); ok {
		return cookieTestExtractGithubClientID(nested)
	}
	return ""
}

func classifyCookieTestOAuthState(account Account, status int, body []byte) (string, *CookieTestResult) {
	data, hasJSON := cookieTestJSONMap(body)
	if !hasJSON {
		if status == http.StatusUnauthorized || status == http.StatusForbidden {
			result := cookieTestResult(account, cookieTestStateInvalid, fmt.Sprintf("OAuth state HTTP %d", status), nil)
			return "", &result
		}
		if status >= 500 {
			result := cookieTestResult(account, cookieTestStateAbnormal, fmt.Sprintf("OAuth state HTTP %d 服务器错误", status), nil)
			return "", &result
		}
		result := cookieTestResult(account, cookieTestStateAbnormal, fmt.Sprintf("OAuth state HTTP %d 非 JSON 响应", status), nil)
		return "", &result
	}
	message := cookieTestMessage(data)
	if status == http.StatusUnauthorized || status == http.StatusForbidden {
		result := cookieTestResult(account, cookieTestStateInvalid, cookieTestMessageOr(message, fmt.Sprintf("OAuth state HTTP %d", status)), nil)
		return "", &result
	}
	if status >= 500 {
		result := cookieTestResult(account, cookieTestStateAbnormal, fmt.Sprintf("OAuth state HTTP %d 服务器错误", status), nil)
		return "", &result
	}
	if success, ok := data["success"].(bool); !ok || !success {
		lower := strings.ToLower(message)
		state := cookieTestStateAbnormal
		if cookieTestContainsAny(lower, cookieTestAuthMarkers) {
			state = cookieTestStateInvalid
		}
		result := cookieTestResult(account, state, cookieTestMessageOr(message, "OAuth state 获取失败"), nil)
		return "", &result
	}
	state := cookieTestExtractOAuthState(data)
	if state == "" {
		result := cookieTestResult(account, cookieTestStateAbnormal, "OAuth state 成功但返回为空", nil)
		return "", &result
	}
	return state, nil
}

func cookieTestExtractOAuthState(data map[string]any) string {
	if state := cookieTestString(data["data"]); state != "" {
		return state
	}
	for _, key := range []string{"flow_token", "flowToken", "state"} {
		if state := cookieTestString(data[key]); state != "" {
			return state
		}
	}
	if nested, ok := data["data"].(map[string]any); ok {
		for _, key := range []string{"flow_token", "flowToken", "state"} {
			if state := cookieTestString(nested[key]); state != "" {
				return state
			}
		}
	}
	return ""
}

func classifyCookieTestGithubAuthorize(account Account, status int, location string) CookieTestResult {
	redirect := status == http.StatusMovedPermanently || status == http.StatusFound ||
		status == http.StatusSeeOther || status == http.StatusTemporaryRedirect ||
		status == http.StatusPermanentRedirect
	if location != "" {
		if parsed, err := url.Parse(location); err == nil && strings.Contains(strings.ToLower(parsed.Path), "/login") {
			return cookieTestResult(account, cookieTestStateInvalid, "GitHub 要求重新登录，user_session 已失效", nil)
		}
	}
	if redirect {
		parsed, err := url.Parse(location)
		if err == nil {
			if strings.Contains(strings.ToLower(parsed.Path), "/login") {
				return cookieTestResult(account, cookieTestStateInvalid, "GitHub 要求重新登录，user_session 已失效", nil)
			}
			if code := parsed.Query().Get("code"); code != "" {
				return cookieTestResult(account, cookieTestStateValid, "GitHub Cookie 有效（已取得 OAuth code，未执行签到）", nil)
			}
			if reason := parsed.Query().Get("error_description"); reason != "" {
				return cookieTestResult(account, cookieTestStateInvalid, "GitHub 未返回 code: "+sanitizeCookieTestMessage(reason), nil)
			}
			if reason := parsed.Query().Get("error"); reason != "" {
				return cookieTestResult(account, cookieTestStateInvalid, "GitHub 未返回 code: "+sanitizeCookieTestMessage(reason), nil)
			}
		}
		return cookieTestResult(account, cookieTestStateAbnormal, fmt.Sprintf("GitHub 未返回授权 code（HTTP %d）", status), nil)
	}
	if status == http.StatusUnauthorized || status == http.StatusForbidden {
		return cookieTestResult(account, cookieTestStateInvalid, fmt.Sprintf("GitHub authorize HTTP %d，user_session 可能已失效", status), nil)
	}
	if status >= 500 {
		return cookieTestResult(account, cookieTestStateAbnormal, fmt.Sprintf("GitHub authorize HTTP %d 服务器错误", status), nil)
	}
	return cookieTestResult(account, cookieTestStateAbnormal, fmt.Sprintf("GitHub 未返回授权重定向（HTTP %d）", status), nil)
}

func parseCookieTestCookies(raw string) map[string]string {
	cookies := make(map[string]string)
	for _, part := range strings.Split(raw, ";") {
		part = strings.TrimSpace(part)
		if part == "" {
			continue
		}
		name, value, ok := strings.Cut(part, "=")
		name = strings.TrimSpace(name)
		if ok && name != "" {
			cookies[name] = strings.TrimSpace(value)
		}
	}
	return cookies
}

func buildCookieTestHeader(cookies map[string]string) string {
	names := make([]string, 0, len(cookies))
	for name := range cookies {
		names = append(names, name)
	}
	sort.Strings(names)
	parts := make([]string, 0, len(names))
	for _, name := range names {
		parts = append(parts, sanitizeCookieTestHeader(name)+"="+sanitizeCookieTestHeader(cookies[name]))
	}
	return strings.Join(parts, "; ")
}

func parseCookieTestSetCookie(raw string) (string, string) {
	name, rest, ok := strings.Cut(raw, "=")
	if !ok {
		return "", ""
	}
	name = strings.TrimSpace(name)
	if name == "" {
		return "", ""
	}
	if index := strings.IndexByte(rest, ';'); index >= 0 {
		rest = rest[:index]
	}
	return name, strings.TrimSpace(rest)
}

func cookieTestJSONMap(body []byte) (map[string]any, bool) {
	var data map[string]any
	if err := json.Unmarshal(body, &data); err != nil || data == nil {
		return nil, false
	}
	return data, true
}

func cookieTestMessage(data map[string]any) string {
	return cookieTestMessageOr(cookieTestString(data["message"]), cookieTestString(data["error"]))
}

func cookieTestMessageOr(message, fallback string) string {
	if strings.TrimSpace(message) == "" {
		return fallback
	}
	return strings.TrimSpace(message)
}

func cookieTestString(value any) string {
	switch v := value.(type) {
	case string:
		return strings.TrimSpace(v)
	case json.Number:
		return v.String()
	case float64:
		if v == float64(int64(v)) {
			return strconv.FormatInt(int64(v), 10)
		}
		return strconv.FormatFloat(v, 'f', -1, 64)
	default:
		return ""
	}
}

func cookieTestInt64(value any) (int64, bool) {
	switch v := value.(type) {
	case int:
		return int64(v), true
	case int64:
		return v, true
	case float64:
		return int64(v), v == float64(int64(v))
	case json.Number:
		n, err := v.Int64()
		return n, err == nil
	case string:
		n, err := strconv.ParseInt(strings.TrimSpace(v), 10, 64)
		return n, err == nil
	default:
		return 0, false
	}
}

func cookieTestContainsAny(value string, markers []string) bool {
	for _, marker := range markers {
		if strings.Contains(value, strings.ToLower(marker)) {
			return true
		}
	}
	return false
}

func sanitizeCookieTestHeader(value string) string {
	return strings.NewReplacer("\r", "", "\n", "").Replace(strings.TrimSpace(value))
}

func sanitizeCookieTestMessage(value string) string {
	value = sanitizeCookieTestHeader(value)
	if len(value) > 160 {
		return value[:160] + "…"
	}
	return value
}

func shortCookieTestError(err error) string {
	if err == nil {
		return "未知错误"
	}
	message := sanitizeCookieTestMessage(err.Error())
	if message == "" {
		return "未知错误"
	}
	return message
}
