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
	"bytes"
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
	cookieTestStatePending  = "pending"
	cookieTestStateRunning  = "running"

	cookieTestSelfPath        = "/api/user/self"
	cookieTestRefreshPath     = "/api/user/auth/refresh"
	cookieTestOAuthStatePath  = "/api/oauth/state"
	cookieTestStatusPath      = "/api/status"
	cookieTestGithubAuthorize = "https://github.com/login/oauth/authorize"
	cookieTestRefreshTokenKey = "new_api_refresh="
	cookieTestDefaultUA       = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36 Edg/151.0.0.0"

	// cookieTestConcurrency 账号级并发上限
	cookieTestConcurrency = 5
)

// cookieTestChallengeStatuses 与 Python cf/detect.py 的 CHALLENGE_STATUSES 保持一致。
var cookieTestChallengeStatuses = []int{403, 429, 503}

// cookieTestCFHints / cookieTestChallengeMarkers 对齐 Python cf/detect.py 的 _CF_HINTS 与标题标记，
// 用来区分「链路被挡在站点外」和「站点自己在回答」。
var cookieTestCFHints = []string{"cloudflare", "cf-ray", "cdn-cgi"}

var cookieTestChallengeMarkers = []string{
	"just a moment", "attention required", "checking your browser", "请稍候",
	"cdn-cgi/challenge-platform", "__cf_chl", "cf-browser-verification",
	"you have been blocked", "error 1020", "sorry, you have been blocked",
}

// cookieTestProxyStatuses 代理自身报错的状态码：换一个代理有意义。
var cookieTestProxyStatuses = []int{407, 502, 504}

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
	// Attempts 已尝试轮次；Proxy 最后一次使用的代理（host:port，空=直连）
	Attempts int    `json:"attempts"`
	Proxy    string `json:"proxy"`
	// retryable 仅在服务内部使用：true 表示失败发生在链路层（代理/CF 拦截），换代理重试有意义
	retryable bool `json:"-"`
	// degradeNote 内部备注：账号代理降级到代理池后，附加在 message 前的提示
	degradeNote string `json:"-"`
}

// CookieTestSummary 一轮检测的状态汇总。
type CookieTestSummary struct {
	Total    int `json:"total"`
	Valid    int `json:"valid"`
	Invalid  int `json:"invalid"`
	Abnormal int `json:"abnormal"`
	Skipped  int `json:"skipped"`
}

// summarizeCookieTestResults 汇总四类终态；pending / running 属于中间态，不计入任何一类，
// 但仍计入 Total，这样前端"总数 - 已定论"就是还在跑的数量。
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
// 全程直连（不经代理池），供既有调用方与单轮直连场景使用。
func runCookieTests(ctx context.Context, cfg *Config, mode string, names []string) ([]CookieTestResult, error) {
	targets, err := selectCookieTestTargets(cfg, mode, names)
	if err != nil {
		return nil, err
	}
	return runCookieTestPass(ctx, cfg, mode, targets, func() string { return "" }), nil
}

// selectCookieTestTargets 按登录方式与账号名筛出待检测账号（启用且方式匹配）。
func selectCookieTestTargets(cfg *Config, mode string, names []string) ([]Account, error) {
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
	return targets, nil
}

// runCookieTestPass 跑一轮：并发 cookieTestConcurrency，每个账号从 nextProxy 领一个代理地址。
// nextProxy 返回空串表示该次直连。结果下标与 targets 对应。
func runCookieTestPass(ctx context.Context, cfg *Config, mode string,
	targets []Account, nextProxy func() string) []CookieTestResult {
	results := make([]CookieTestResult, len(targets))
	sem := make(chan struct{}, cookieTestConcurrency)
	var wg sync.WaitGroup
	for i, account := range targets {
		proxyAddr := ""
		if nextProxy != nil {
			proxyAddr = nextProxy()
		}
		wg.Add(1)
		go func(index int, target Account, proxy string) {
			defer wg.Done()
			select {
			case sem <- struct{}{}:
			case <-ctx.Done():
				cancelled := cookieTestResult(target, cookieTestStateSkipped, "检测已取消", nil)
				cancelled.Proxy = proxy
				results[index] = cancelled
				return
			}
			defer func() { <-sem }()
			results[index] = checkCookieAccount(ctx, cfg.HTTP, target, mode, proxy)
		}(i, account, proxyAddr)
	}
	wg.Wait()
	return results
}

func cookieTestModeLabel(mode string) string {
	if mode == LoginMethodGitHubCookie {
		return "github_cookie"
	}
	return "newapi_cookie"
}

func checkCookieAccount(ctx context.Context, httpCfg HTTPConfig, account Account,
	mode string, proxyAddr string) CookieTestResult {
	started := time.Now()
	var result CookieTestResult
	if mode == LoginMethodGitHubCookie {
		if strings.TrimSpace(account.GithubUserSession) == "" {
			result = cookieTestResult(account, cookieTestStateSkipped, "缺少 GitHub Cookie（github_user_session）", nil)
		} else {
			result = checkGithubCookie(ctx, httpCfg, account, proxyAddr)
		}
	} else if strings.TrimSpace(account.Cookie) == "" {
		result = cookieTestResult(account, cookieTestStateSkipped, "缺少站点 Cookie（cookie）", nil)
	} else {
		result = checkNewAPICookie(ctx, httpCfg, account, proxyAddr)
	}
	result.DurationMS = time.Since(started).Milliseconds()
	result.Proxy = cookieTestEffectiveProxy(account, proxyAddr)
	return result
}

// cookieTestEffectiveProxy 返回本次实际生效的代理标识：账号自带代理优先，其次池子地址。
func cookieTestEffectiveProxy(account Account, proxyAddr string) string {
	if account.Proxy != nil && strings.TrimSpace(*account.Proxy) != "" {
		return maskCookieTestProxy(strings.TrimSpace(*account.Proxy))
	}
	return strings.TrimSpace(proxyAddr)
}

// maskCookieTestProxy 账号自带代理可能带用户名密码，展示前抹掉认证信息。
func maskCookieTestProxy(raw string) string {
	u, err := url.Parse(raw)
	if err != nil || u.Host == "" {
		return "已配置账号代理"
	}
	if u.User != nil {
		u.User = url.User("***")
	}
	return u.String()
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

// cookieTestProxyIssue 构造「链路层失败」结果：状态仍是 abnormal，但标记为可换代理重试。
func cookieTestProxyIssue(account Account, message string) CookieTestResult {
	result := cookieTestResult(account, cookieTestStateAbnormal, message, nil)
	result.retryable = true
	return result
}

// cookieTestLooksLikeChallenge 判断响应是否为「链路被挡在站点外」：
// 非 JSON 正文、CF/WAF 特征头或挑战页文案、挑战类状态码、代理自身错误码。
func cookieTestLooksLikeChallenge(status int, header http.Header, body []byte) bool {
	for _, code := range cookieTestProxyStatuses {
		if status == code {
			return true
		}
	}
	if _, ok := cookieTestJSONMap(body); ok {
		// 站点回了合法 JSON，说明请求已经到站点并被处理，属于源站在回答
		return false
	}
	lowerBody := strings.ToLower(string(body))
	if cookieTestContainsAny(lowerBody, cookieTestChallengeMarkers) {
		return true
	}
	if header != nil {
		joined := strings.ToLower(strings.Join([]string{
			header.Get("Server"), header.Get("Cf-Ray"), header.Get("Cf-Mitigated"),
		}, " "))
		if cookieTestContainsAny(joined, cookieTestCFHints) {
			return true
		}
	}
	for _, code := range cookieTestChallengeStatuses {
		if status == code {
			return true
		}
	}
	return false
}

func checkNewAPICookie(ctx context.Context, httpCfg HTTPConfig, account Account, proxyAddr string) CookieTestResult {
	base, err := cookieTestBaseURL(account.URL)
	if err != nil {
		return cookieTestResult(account, cookieTestStateAbnormal, "站点 URL 无效: "+err.Error(), nil)
	}
	client, err := newCookieTestHTTPClient(account, httpCfg, true, proxyAddr)
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
		return cookieTestProxyIssue(account, "网络错误: "+shortCookieTestError(err))
	}
	header := resp.Header
	body, readErr := readCookieTestBody(resp)
	if readErr != nil {
		return cookieTestProxyIssue(account, "读取响应失败: "+readErr.Error())
	}
	if cookieTestLooksLikeChallenge(resp.StatusCode, header, body) {
		return cookieTestProxyIssue(account,
			fmt.Sprintf("站点未放行当前出口（HTTP %d，疑似 CDN/WAF 拦截）", resp.StatusCode))
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
		return cookieTestProxyIssue(account, "refresh 网络错误: "+shortCookieTestError(err))
	}
	body, readErr := readCookieTestBody(resp)
	if readErr != nil {
		return cookieTestProxyIssue(account, "读取 refresh 响应失败: "+readErr.Error())
	}
	if cookieTestLooksLikeChallenge(resp.StatusCode, resp.Header, body) {
		return cookieTestProxyIssue(account,
			fmt.Sprintf("refresh 未放行当前出口（HTTP %d，疑似 CDN/WAF 拦截）", resp.StatusCode))
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
		result := cookieTestProxyIssue(account, "self 网络错误: "+shortCookieTestError(err))
		result.UserID = bundle.userID
		return result
	}
	selfBody, readErr := readCookieTestBody(selfResp)
	if readErr != nil {
		result := cookieTestProxyIssue(account, "读取 self 响应失败: "+readErr.Error())
		result.UserID = bundle.userID
		return result
	}
	if cookieTestLooksLikeChallenge(selfResp.StatusCode, selfResp.Header, selfBody) {
		result := cookieTestProxyIssue(account,
			fmt.Sprintf("self 未放行当前出口（HTTP %d，疑似 CDN/WAF 拦截）", selfResp.StatusCode))
		result.UserID = bundle.userID
		return result
	}
	return classifyCookieTestSelf(account, selfResp.StatusCode, selfBody, bundle.userID)
}

// newCookieTestHTTPClient 构造检测用客户端。
// 代理优先级：账号自带 proxy > 传入的池子地址 proxyAddr > 进程环境变量。
func newCookieTestHTTPClient(account Account, httpCfg HTTPConfig,
	followRedirects bool, proxyAddr string) (*http.Client, error) {
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
	} else if addr := strings.TrimSpace(proxyAddr); addr != "" {
		// 代理池里存的是裸 host:port，与 proxies.go 的 proxyFromRequest 保持同一套构造方式
		if !strings.Contains(addr, "://") {
			addr = "http://" + addr
		}
		proxyURL, err := url.Parse(addr)
		if err != nil || proxyURL.Host == "" {
			return nil, fmt.Errorf("代理池地址无效: %s", proxyAddr)
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
			// 非 JSON 的 5xx 更像 CDN/网关错误页，换出口有意义
			result := cookieTestProxyIssue(account, fmt.Sprintf("HTTP %d 网关错误（非 JSON）", status))
			result.UserID = fallbackID
			return result
		default:
			result := cookieTestProxyIssue(account, fmt.Sprintf("HTTP %d 非 JSON 响应", status))
			result.UserID = fallbackID
			return result
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

func checkGithubCookie(ctx context.Context, httpCfg HTTPConfig, account Account, proxyAddr string) CookieTestResult {
	return checkGithubCookieWithAuthorizeURL(ctx, httpCfg, account, cookieTestGithubAuthorize, proxyAddr)
}

// checkGithubCookieWithAuthorizeURL 为测试注入 authorize 地址；生产路径使用 GitHub 官方地址。
func checkGithubCookieWithAuthorizeURL(ctx context.Context, httpCfg HTTPConfig, account Account,
	authorizeURL string, proxyAddr string) CookieTestResult {
	base, err := cookieTestBaseURL(account.URL)
	if err != nil {
		return cookieTestResult(account, cookieTestStateAbnormal, "站点 URL 无效: "+err.Error(), nil)
	}
	client, err := newCookieTestHTTPClient(account, httpCfg, false, proxyAddr)
	if err != nil {
		return cookieTestResult(account, cookieTestStateAbnormal, "HTTP 客户端配置失败: "+err.Error(), nil)
	}

	statePayload, marshalErr := json.Marshal(map[string]string{
		"provider": "github",
		"intent":   "login",
	})
	if marshalErr != nil {
		return cookieTestResult(account, cookieTestStateAbnormal, "构造 OAuth state 请求失败: "+marshalErr.Error(), nil)
	}
	stateReq, err := http.NewRequestWithContext(ctx, http.MethodPost,
		base+cookieTestOAuthStatePath, bytes.NewReader(statePayload))
	if err != nil {
		return cookieTestResult(account, cookieTestStateAbnormal, "构造 OAuth state 请求失败: "+err.Error(), nil)
	}
	setCookieTestCommonHeaders(stateReq, base, "")
	stateReq.Header.Set("Content-Type", "application/json")
	stateReq.Header.Set("Cache-Control", "no-store")
	stateResp, err := client.Do(stateReq)
	if err != nil {
		return cookieTestProxyIssue(account, "OAuth state 网络错误: "+shortCookieTestError(err))
	}
	stateHeader := stateResp.Header
	stateBody, readErr := readCookieTestBody(stateResp)
	if readErr != nil {
		return cookieTestProxyIssue(account, "读取 OAuth state 响应失败: "+readErr.Error())
	}
	if cookieTestLooksLikeChallenge(stateResp.StatusCode, stateHeader, stateBody) {
		return cookieTestProxyIssue(account,
			fmt.Sprintf("站点未放行当前出口（OAuth state HTTP %d，疑似 CDN/WAF 拦截）", stateResp.StatusCode))
	}
	state, stateResult := classifyCookieTestOAuthState(account, stateResp.StatusCode, stateBody)
	if stateResult != nil {
		return *stateResult
	}

	u, err := url.Parse(authorizeURL)
	if err != nil {
		return cookieTestResult(account, cookieTestStateAbnormal, "GitHub authorize 地址无效", nil)
	}
	query := u.Query()
	clientID := strings.TrimSpace(account.GithubClientID)
	if clientID == "" {
		// 站点自己的 OAuth 应用 ID 由 /api/status 公开，不能套用别站的默认值
		statusReq, statusErr := http.NewRequestWithContext(ctx, http.MethodGet, base+cookieTestStatusPath, nil)
		if statusErr != nil {
			return cookieTestResult(account, cookieTestStateAbnormal, "构造站点状态请求失败: "+statusErr.Error(), nil)
		}
		setCookieTestCommonHeaders(statusReq, base, "")
		statusResp, doErr := client.Do(statusReq)
		if doErr != nil {
			return cookieTestProxyIssue(account, "站点状态网络错误: "+shortCookieTestError(doErr))
		}
		statusHeader := statusResp.Header
		statusBody, readErr := readCookieTestBody(statusResp)
		if readErr != nil {
			return cookieTestProxyIssue(account, "读取站点状态失败: "+readErr.Error())
		}
		if cookieTestLooksLikeChallenge(statusResp.StatusCode, statusHeader, statusBody) {
			return cookieTestProxyIssue(account,
				fmt.Sprintf("站点未放行当前出口（/api/status HTTP %d，疑似 CDN/WAF 拦截）", statusResp.StatusCode))
		}
		statusData, ok := cookieTestJSONMap(statusBody)
		if !ok || statusResp.StatusCode >= 400 {
			return cookieTestResult(account, cookieTestStateAbnormal, fmt.Sprintf("站点状态 HTTP %d 非法响应", statusResp.StatusCode), nil)
		}
		if payload, ok := statusData["data"].(map[string]any); ok {
			clientID = strings.TrimSpace(cookieTestString(payload["github_client_id"]))
		}
		if clientID == "" {
			return cookieTestResult(account, cookieTestStateAbnormal, "站点状态未返回 github_client_id", nil)
		}
	}
	query.Set("client_id", clientID)
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
	githubSession := sanitizeCookieTestHeader(account.GithubUserSession)
	authorizeReq.Header.Set("Cookie", "user_session="+githubSession+
		"; __Host-user_session_same_site="+githubSession+"; logged_in=yes")
	authorizeReq.Header.Set("Referer", sanitizeCookieTestHeader(base+"/login"))

	authorizeResp, err := client.Do(authorizeReq)
	if err != nil {
		return cookieTestProxyIssue(account, "GitHub authorize 网络错误: "+shortCookieTestError(err))
	}
	defer authorizeResp.Body.Close()
	return classifyCookieTestGithubAuthorize(account, authorizeResp.StatusCode, authorizeResp.Header.Get("Location"))
}

func classifyCookieTestOAuthState(account Account, status int, body []byte) (string, *CookieTestResult) {
	data, hasJSON := cookieTestJSONMap(body)
	if !hasJSON {
		if status == http.StatusUnauthorized || status == http.StatusForbidden {
			result := cookieTestResult(account, cookieTestStateInvalid, fmt.Sprintf("OAuth state HTTP %d", status), nil)
			return "", &result
		}
		// 非 JSON 一律视作链路被挡（CDN 错误页/挑战页），换出口重试
		result := cookieTestProxyIssue(account, fmt.Sprintf("OAuth state HTTP %d 非 JSON 响应", status))
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
	state := cookieTestFlowToken(data["data"])
	if state == "" {
		result := cookieTestResult(account, cookieTestStateAbnormal, "OAuth state 成功但未返回 flow_token", nil)
		return "", &result
	}
	return state, nil
}

// cookieTestFlowToken 站点把 state 放在 data.flow_token；旧结构直接给字符串时也接受。
func cookieTestFlowToken(value any) string {
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
	if status == http.StatusForbidden || status == http.StatusTooManyRequests {
		// GitHub 在风控当前出口 IP（而不是否定 Cookie），换出口有意义
		return cookieTestProxyIssue(account,
			fmt.Sprintf("GitHub authorize HTTP %d，当前出口被 GitHub 限制", status))
	}
	if status == http.StatusUnauthorized {
		return cookieTestResult(account, cookieTestStateInvalid, "GitHub authorize HTTP 401，user_session 已失效", nil)
	}
	if status >= 500 {
		return cookieTestProxyIssue(account, fmt.Sprintf("GitHub authorize HTTP %d 服务器错误", status))
	}
	return cookieTestResult(account, cookieTestStateAbnormal,
		fmt.Sprintf("GitHub 未返回授权重定向（HTTP %d），可能需要先在 GitHub 授权该 OAuth 应用", status), nil)
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
