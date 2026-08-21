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
	// rotatedCookie 内部字段：TaBiAI 的 refresh 会轮转凭据，这里带出新一代值供调用方落盘。
	// 绝不能序列化给前端 —— 它就是凭据本身。
	rotatedCookie string `json:"-"`
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
	if mode != LoginMethodNewAPICookie && mode != LoginMethodTabiAI {
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
			defer recoverPanic("Cookie 检测单账号")
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
	if mode == LoginMethodTabiAI {
		return "tabiai"
	}
	return "newapi_cookie"
}

func checkCookieAccount(ctx context.Context, httpCfg HTTPConfig, account Account,
	mode string, proxyAddr string) CookieTestResult {
	started := time.Now()
	var result CookieTestResult
	if mode == LoginMethodTabiAI {
		if strings.TrimSpace(account.Cookie) == "" {
			result = cookieTestResult(account, cookieTestStateSkipped,
				"缺少 TaBiAI 凭据（new_api_refresh），可用「签发 cookie」或从浏览器复制", nil)
		} else {
			result = checkTabiAICookie(ctx, httpCfg, account, proxyAddr)
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

// checkTabiAICookie 验证 TaBiAI 的 new_api_refresh 凭据。
//
// 全程纯 HTTP：POST /api/user/auth/refresh 只需要 cookie，不碰 Turnstile、不需要浏览器与 AI。
// refresh 成功即证明凭据有效，响应里已带 user 信息，因此不再多打一次 /api/user/self。
//
// 注意：站点实现了 refresh token rotation + 重放检测，这次请求**必然消耗一代 secret**并下发下一代。
// 新值通过 result.rotatedCookie 带出，调用方必须落盘，否则下次用旧代会被判重放、整条会话被撤销。
func checkTabiAICookie(ctx context.Context, httpCfg HTTPConfig, account Account, proxyAddr string) CookieTestResult {
	base, err := cookieTestBaseURL(account.URL)
	if err != nil {
		return cookieTestResult(account, cookieTestStateAbnormal, "站点 URL 无效: "+err.Error(), nil)
	}
	client, err := newCookieTestHTTPClient(account, httpCfg, true, proxyAddr)
	if err != nil {
		return cookieTestResult(account, cookieTestStateAbnormal, "HTTP 客户端配置失败: "+err.Error(), nil)
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, base+cookieTestRefreshPath, nil)
	if err != nil {
		return cookieTestResult(account, cookieTestStateAbnormal, "构造 refresh 请求失败: "+err.Error(), nil)
	}
	setCookieTestCommonHeaders(req, base, normalizeTabiAIRefreshCookie(account.Cookie))
	req.Header.Set("Content-Type", "application/json")

	resp, err := client.Do(req)
	if err != nil {
		return cookieTestProxyIssue(account, "refresh 网络错误: "+shortCookieTestError(err))
	}
	header := resp.Header
	body, readErr := readCookieTestBody(resp)
	if readErr != nil {
		return cookieTestProxyIssue(account, "读取 refresh 响应失败: "+readErr.Error())
	}
	if cookieTestLooksLikeChallenge(resp.StatusCode, header, body) {
		return cookieTestProxyIssue(account,
			fmt.Sprintf("站点未放行当前出口（refresh HTTP %d，疑似 CDN/WAF 拦截）", resp.StatusCode))
	}

	result := classifyTabiAIRefresh(account, resp.StatusCode, body)
	// 无论成功与否都把站点下发的新 cookie 带出去：只要服务端确实换了代次就必须落盘
	if rotated := extractTabiAIRefreshCookie(header.Values("Set-Cookie")); rotated != "" {
		result.rotatedCookie = rotated
	}
	return result
}

// classifyTabiAIRefresh 依据 refresh 响应判定凭据状态。
// 站点错误码见签到原理文档：AUTH_SESSION_REVOKED 表示整条会话已废，AUTH_UNAUTHORIZED 表示当前代次失效。
func classifyTabiAIRefresh(account Account, status int, body []byte) CookieTestResult {
	data, hasJSON := cookieTestJSONMap(body)
	if !hasJSON {
		if status >= 500 {
			return cookieTestProxyIssue(account, fmt.Sprintf("refresh HTTP %d 网关错误（非 JSON）", status))
		}
		return cookieTestProxyIssue(account, fmt.Sprintf("refresh HTTP %d 非 JSON 响应", status))
	}
	code := strings.ToUpper(strings.TrimSpace(cookieTestString(data["code"])))
	message := cookieTestMessage(data)

	if status == http.StatusUnauthorized || status == http.StatusForbidden {
		switch code {
		case "AUTH_SESSION_REVOKED":
			return cookieTestResult(account, cookieTestStateInvalid,
				"会话已被撤销（旧代次重放或用户登出了其他会话），需要重新签发 new_api_refresh", nil)
		case "AUTH_UNAUTHORIZED":
			return cookieTestResult(account, cookieTestStateInvalid,
				"凭据已失效：可能已过期，或被更新后的代次取代（请确认没有别处在用同一条会话）", nil)
		}
		return cookieTestResult(account, cookieTestStateInvalid,
			cookieTestMessageOr(message, fmt.Sprintf("refresh HTTP %d", status)), nil)
	}
	if status >= 500 {
		return cookieTestProxyIssue(account, fmt.Sprintf("refresh HTTP %d 服务器错误", status))
	}
	if success, ok := data["success"].(bool); !ok || !success {
		lower := strings.ToLower(message + " " + code)
		state := cookieTestStateAbnormal
		if cookieTestContainsAny(lower, cookieTestAuthMarkers) {
			state = cookieTestStateInvalid
		}
		return cookieTestResult(account, state, cookieTestMessageOr(message, "refresh 失败"), nil)
	}

	payload, _ := data["data"].(map[string]any)
	if payload == nil {
		return cookieTestResult(account, cookieTestStateAbnormal, "refresh 成功但响应缺少 data", nil)
	}
	// 这两个辅助函数接收的是整个响应体（内部自己下钻 data.*），别传 payload
	if cookieTestExtractAccessToken(data) == "" {
		return cookieTestResult(account, cookieTestStateAbnormal, "refresh 成功但未返回 access_token", nil)
	}
	userID := cookieTestExtractUserID(data)
	username := ""
	if user, ok := payload["user"].(map[string]any); ok {
		username = cookieTestString(user["username"])
		if username == "" {
			username = cookieTestString(user["display_name"])
		}
	}
	if username == "" {
		username = "TaBiAI 凭据有效"
	}
	return cookieTestResult(account, cookieTestStateValid, username, userID)
}

// normalizeTabiAIRefreshCookie 允许用户只填裸值 sid.secret，也允许填完整 new_api_refresh=...。
func normalizeTabiAIRefreshCookie(raw string) string {
	value := strings.TrimSpace(raw)
	if value == "" {
		return ""
	}
	if strings.Contains(value, cookieTestRefreshTokenKey) {
		return value
	}
	return cookieTestRefreshTokenKey + value
}

// extractTabiAIRefreshCookie 从 Set-Cookie 里取出新一代 new_api_refresh（含 name=value 形式）。
func extractTabiAIRefreshCookie(setCookies []string) string {
	for _, raw := range setCookies {
		name, value := parseCookieTestSetCookie(raw)
		if name == "new_api_refresh" && value != "" {
			return name + "=" + value
		}
	}
	return ""
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
