/*
server/site_apikey.go
为每个签到账号在自己站点上备一条调用令牌（API Key），并汇总取出

为什么要单独一条链路：站点的「创建令牌」接口不回传 key，而「令牌列表」里的 key 是
打码的（new-api 的 GetAllTokens 走 buildMaskedTokenResponses → MaskTokenKey，形如
abcd**********wxyz）。所以「创建后回查列表取值」这条直觉路线拿到的是一串长得很像
key 的废字符串 —— 必须再打一次 POST /api/token/{id}/key 才拿到全值。

完整四步（端点与字段名都对着 new-api 源码核过，不是猜的）：

	第一步 GET  /api/token/?p=1&page_size=100   按 name 找我们约定的那条
	第二步 没有 → POST /api/token/              只带 name 与 expired_time:-1，配额和
	                                            模型限制一律不设，走站点默认
	第三步 再列一次拿到它的 id
	第四步 POST /api/token/{id}/key             取完整 key

老站点的列表接口直接吐全值（没有打码那层），所以第 1 步命中且 key 里没有 * 时就直接
用，不再打第 4 步 —— 新旧站点都不用配。

跟签到的关系：没有关系。签到用的是 accounts[].cookie，这条链路纯粹是「顺手把每个
站点的可用 key 攒起来」。但它会消耗 refresh 代次（见 openAPIKeySession），所以同样
要躲开正在跑的签到。
*/
package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"strings"
	"sync"
)

const (
	// apiKeyTokenName 令牌名固定，这就是「没有才创建」的判据。
	// 站点侧限制 name ≤ 50 字符，固定值不会碰到。
	apiKeyTokenName = "newapi-auto-checkin"
	// 分页参数是 p（页码，1 起）+ page_size，见 new-api 的 common/page_info.go。
	// page_size 超过 100 会被站点截到 100，这里直接按上限取
	apiKeyListPath   = "/api/token/?p=1&page_size=100"
	apiKeyCreatePath = "/api/token/"
	// apiKeyPrefix 站点存的是不带前缀的原始值，鉴权中间件做 TrimPrefix(key, "sk-")。
	// 落库时补上前缀：那才是能直接粘进 Authorization: Bearer 的形态
	apiKeyPrefix = "sk-"
)

// 单账号的处置结果。与 provision 那套刻意保持同样的形状，前端可以复用渲染。
const (
	apiKeyReused  = "reused"                 // 站点上已有这条令牌，取到了值
	apiKeyCreated = "created"                // 新建并取到了值
	apiKeyFailed  = "failed"                 // 网络/站点报错，或取不到全值
	apiKeyNoCreds = "skipped_no_credentials" // 账号没有站点凭据，压根没法登录
)

type apiKeyOutcome struct {
	AccountName string `json:"account"`
	Status      string `json:"status"`
	Message     string `json:"message,omitempty"`
}

// siteToken 站点令牌列表里的一条。只取用得上的三个字段。
type siteToken struct {
	ID   int
	Name string
	Key  string // 新站点是打码的，老站点是全值
}

// masked 判断这条 key 是不是被站点打码了。
//
// 判据是「含 *」而不是长度或前缀：MaskTokenKey 的输出必然带星号，而真 key 是
// GenerateKey 生成的 base62，永远不含星号。用它区分新旧站点，两边都不用配开关。
func (t siteToken) masked() bool {
	return strings.Contains(t.Key, "*")
}

/*
apiKeySession 一个账号在它站点上的已鉴权会话。

rotated 必须被调用方落库。tabiai 账号的凭据是 refresh token，站点实现了
rotation + 重放检测：openAPIKeySession 里那一次 refresh **必然消耗一代 secret**
并下发下一代。不落库的话，下一轮保活拿旧代去 refresh 就是 AUTH_SESSION_REVOKED，
整条会话报废 —— 这个坑 checkTabiAICookie 踩过，那边有更长的说明。
*/
type apiKeySession struct {
	client  *http.Client
	base    string
	cookie  string // 实际要发出去的 Cookie 头
	auth    string // Authorization，refresh 换到 access token 时才有
	userID  *int64
	rotated string // refresh 轮转出的新 new_api_refresh 值，非空就必须落库
}

/*
openAPIKeySession 登录站点，拿到一个能打令牌接口的会话。

两条路，取决于账号凭据的形态：
  - 含 new_api_refresh= ：先 POST /api/user/auth/refresh 换 session/access token。
    这一步会推进代次，rotated 带出来
  - 其他（完整 Cookie 头）：直接用，不需要额外请求

代次抢救先于任何 return：body 读失败或被判成挑战页时，Set-Cookie 里可能已经躺着
新代次了。站点侧代次已经推进，平台却因为早退没落库，就会永远停在旧代。
*/
func openAPIKeySession(ctx context.Context, httpCfg HTTPConfig, account Account,
	proxyAddr string) (*apiKeySession, error) {
	base, err := cookieTestBaseURL(account.URL)
	if err != nil {
		return nil, fmt.Errorf("站点 URL 无效: %w", err)
	}
	client, err := newCookieTestHTTPClient(account, httpCfg, true, proxyAddr)
	if err != nil {
		return nil, fmt.Errorf("HTTP 客户端配置失败: %w", err)
	}
	sess := &apiKeySession{client: client, base: base, userID: account.UserID}

	if !strings.Contains(account.Cookie, cookieTestRefreshTokenKey) {
		sess.cookie = account.Cookie
		return sess, nil
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, base+cookieTestRefreshPath, nil)
	if err != nil {
		return nil, fmt.Errorf("构造 refresh 请求失败: %w", err)
	}
	setCookieTestCommonHeaders(req, base, normalizeTabiAIRefreshCookie(account.Cookie))
	req.Header.Set("Content-Type", "application/json")

	resp, err := client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("refresh 网络错误: %s", shortCookieTestError(err))
	}
	sess.rotated = extractTabiAIRefreshCookie(resp.Header.Values("Set-Cookie"))
	body, readErr := readCookieTestBody(resp)
	if readErr != nil {
		// 带着 sess 一起返回错误：调用方要能拿到 rotated 去落库
		return sess, fmt.Errorf("读取 refresh 响应失败: %w", readErr)
	}
	if cookieTestLooksLikeChallenge(resp.StatusCode, resp.Header, body) {
		return sess, fmt.Errorf("refresh 未放行当前出口（HTTP %d，疑似 CDN/WAF 拦截）", resp.StatusCode)
	}
	if data, ok := cookieTestJSONMap(body); ok {
		if success, _ := data["success"].(bool); !success {
			return sess, fmt.Errorf("refresh 被拒: %s",
				cookieTestMessageOr(cookieTestMessage(data), fmt.Sprintf("HTTP %d", resp.StatusCode)))
		}
	}

	bundle := parseCookieTestRefreshBundle(account.Cookie, resp.Header.Values("Set-Cookie"), body)
	if bundle.authorization == "" && !bundle.hasSessionCookie {
		return sess, fmt.Errorf("refresh 成功但未返回 access token 或 session cookie")
	}
	sess.cookie = bundle.cookieHeader
	sess.auth = bundle.authorization
	if bundle.userID != nil {
		sess.userID = bundle.userID
	}
	return sess, nil
}

/*
do 发一个请求并把响应体读回来。所有令牌接口都走它，保证鉴权头与挑战页判定一致。

站点被 CDN 挡住时返回的是 HTML 挑战页而不是 JSON，直接 Unmarshal 会得到一个
「JSON 解析失败」的误导性错误。这里先判挑战页，让错误信息说清是出口被拦。
*/
func (s *apiKeySession) do(ctx context.Context, method, path string, body []byte) ([]byte, error) {
	var reader io.Reader
	if body != nil {
		reader = bytes.NewReader(body)
	}
	req, err := http.NewRequestWithContext(ctx, method, s.base+path, reader)
	if err != nil {
		return nil, fmt.Errorf("构造请求失败: %w", err)
	}
	setCookieTestCommonHeaders(req, s.base, s.cookie)
	setCookieTestUserID(req, s.userID)
	if s.auth != "" {
		req.Header.Set("Authorization", s.auth)
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := s.client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("网络错误: %s", shortCookieTestError(err))
	}
	payload, readErr := readCookieTestBody(resp)
	if readErr != nil {
		return nil, fmt.Errorf("读取响应失败: %w", readErr)
	}
	if cookieTestLooksLikeChallenge(resp.StatusCode, resp.Header, payload) {
		return nil, fmt.Errorf("站点未放行当前出口（HTTP %d，疑似 CDN/WAF 拦截）", resp.StatusCode)
	}
	if resp.StatusCode == http.StatusUnauthorized || resp.StatusCode == http.StatusForbidden {
		return nil, fmt.Errorf("站点拒绝了这次调用（HTTP %d），凭据可能已失效", resp.StatusCode)
	}
	return payload, nil
}

// apiKeyEnvelope 站点统一响应信封。data 留成 RawMessage：列表和取值两个接口的
// data 结构不同，各自解析。
type apiKeyEnvelope struct {
	Success bool            `json:"success"`
	Message string          `json:"message"`
	Data    json.RawMessage `json:"data"`
}

// decodeAPIKeyEnvelope 拆信封并把 success=false 转成错误。
func decodeAPIKeyEnvelope(payload []byte) (*apiKeyEnvelope, error) {
	var env apiKeyEnvelope
	if err := json.Unmarshal(payload, &env); err != nil {
		return nil, fmt.Errorf("响应不是合法 JSON: %w", err)
	}
	if !env.Success {
		return nil, fmt.Errorf("站点返回失败: %s",
			cookieTestMessageOr(env.Message, "（站点未给出原因）"))
	}
	return &env, nil
}

// SPLICE_CALLS

/*
listTokens 列出当前用户的令牌。

data 有两种形状要都吃下：新版是分页对象 {"page":1,"page_size":100,"items":[...]}，
老版直接就是数组 [...]。判 data 的第一个非空白字符是 '[' 还是 '{' 来分流 ——
比先试一种失败再试另一种干净，也不会把「真的解析失败」吞掉。
*/
func (s *apiKeySession) listTokens(ctx context.Context) ([]siteToken, error) {
	payload, err := s.do(ctx, http.MethodGet, apiKeyListPath, nil)
	if err != nil {
		return nil, err
	}
	env, err := decodeAPIKeyEnvelope(payload)
	if err != nil {
		return nil, err
	}
	raw := bytes.TrimSpace(env.Data)
	if len(raw) == 0 || bytes.Equal(raw, []byte("null")) {
		return nil, nil // 一条都没有，不是错误
	}

	type tokenItem struct {
		ID   int    `json:"id"`
		Name string `json:"name"`
		Key  string `json:"key"`
	}
	var items []tokenItem
	if raw[0] == '{' {
		var paged struct {
			Items []tokenItem `json:"items"`
		}
		if err := json.Unmarshal(raw, &paged); err != nil {
			return nil, fmt.Errorf("令牌列表解析失败: %w", err)
		}
		items = paged.Items
	} else {
		if err := json.Unmarshal(raw, &items); err != nil {
			return nil, fmt.Errorf("令牌列表解析失败: %w", err)
		}
	}

	out := make([]siteToken, 0, len(items))
	for _, it := range items {
		out = append(out, siteToken{ID: it.ID, Name: strings.TrimSpace(it.Name), Key: it.Key})
	}
	return out, nil
}

// createToken 建一条令牌。
//
// 请求体刻意只带 name 与 expired_time:-1（永不过期）：配额与模型限制一律不设，
// 让站点用自己的默认值。填错这些字段的后果是「创建成功但 key 不可用」，而各站点
// 的默认策略本来就不一样，猜不如不填。
//
// 站点这个接口不回传 key，成功只代表建好了，取值要另外走 fetchKey。
func (s *apiKeySession) createToken(ctx context.Context, name string) error {
	body, err := json.Marshal(map[string]any{
		"name":         name,
		"expired_time": -1,
	})
	if err != nil {
		return fmt.Errorf("构造请求体失败: %w", err)
	}
	payload, err := s.do(ctx, http.MethodPost, apiKeyCreatePath, body)
	if err != nil {
		return err
	}
	_, err = decodeAPIKeyEnvelope(payload)
	return err
}

// fetchKey 取一条令牌的完整值。POST 不是 GET —— 站点把它当敏感操作，
// 挂了 CriticalRateLimit，所以批量场景必须串行。
func (s *apiKeySession) fetchKey(ctx context.Context, id int) (string, error) {
	payload, err := s.do(ctx, http.MethodPost, fmt.Sprintf("/api/token/%d/key", id), nil)
	if err != nil {
		return "", err
	}
	env, err := decodeAPIKeyEnvelope(payload)
	if err != nil {
		return "", err
	}
	var data struct {
		Key string `json:"key"`
	}
	if err := json.Unmarshal(env.Data, &data); err != nil {
		return "", fmt.Errorf("取值响应解析失败: %w", err)
	}
	key := strings.TrimSpace(data.Key)
	if key == "" {
		return "", fmt.Errorf("站点返回成功但 key 为空")
	}
	return key, nil
}

/*
ensureAccountAPIKey 保证账号在自己站点上有一条可用令牌，并返回它的完整值。

返回值 (key, created, err)。key 已带 sk- 前缀。

命中已有那条时不重建：站点侧的 key 是建的时候一次性生成的，重建会换出新值、把
用户可能已经配到别处的旧 key 作废。所以「有就用」不只是省一次请求。
*/
func ensureAccountAPIKey(ctx context.Context, sess *apiKeySession) (string, bool, error) {
	tokens, err := sess.listTokens(ctx)
	if err != nil {
		return "", false, err
	}
	if found, ok := findAPIKeyToken(tokens); ok {
		// 老站点列表直接吐全值，省掉第四步
		if !found.masked() {
			return withAPIKeyPrefix(found.Key), false, nil
		}
		key, err := sess.fetchKey(ctx, found.ID)
		if err != nil {
			return "", false, fmt.Errorf("取已有令牌的值失败: %w", err)
		}
		return withAPIKeyPrefix(key), false, nil
	}

	if err := sess.createToken(ctx, apiKeyTokenName); err != nil {
		return "", false, fmt.Errorf("创建令牌失败: %w", err)
	}
	// 创建接口不回传 key 也不回传 id，只能再列一次
	tokens, err = sess.listTokens(ctx)
	if err != nil {
		return "", true, fmt.Errorf("令牌已创建但回查列表失败: %w", err)
	}
	found, ok := findAPIKeyToken(tokens)
	if !ok {
		return "", true, fmt.Errorf("令牌已创建但列表里找不到名为 %q 的条目", apiKeyTokenName)
	}
	if !found.masked() {
		return withAPIKeyPrefix(found.Key), true, nil
	}
	key, err := sess.fetchKey(ctx, found.ID)
	if err != nil {
		return "", true, fmt.Errorf("令牌已创建但取值失败: %w", err)
	}
	return withAPIKeyPrefix(key), true, nil
}

// findAPIKeyToken 在列表里找我们约定名字的那条。名字两侧空白已在解析时去掉。
func findAPIKeyToken(tokens []siteToken) (siteToken, bool) {
	for _, t := range tokens {
		if t.Name == apiKeyTokenName {
			return t, true
		}
	}
	return siteToken{}, false
}

// withAPIKeyPrefix 补上 sk- 前缀，且只补一次。
//
// 站点存的是不带前缀的原始值，但老站点的列表接口有可能已经带上了；重复拼成
// sk-sk-xxx 会让 key 直接失效，而这种错误在日志里看着像个正常的 key。
func withAPIKeyPrefix(key string) string {
	trimmed := strings.TrimSpace(key)
	if trimmed == "" {
		return ""
	}
	if strings.HasPrefix(trimmed, apiKeyPrefix) {
		return trimmed
	}
	return apiKeyPrefix + trimmed
}

// apiKeyMu 串行化整批取 key。站点把 POST /api/token/{id}/key 当敏感操作，挂了
// CriticalRateLimit；并发打它等于主动触发限流，然后一批账号全部失败。
var apiKeyMu sync.Mutex

/*
handleEnsureSiteAPIKeys POST /api/sites/apikeys（JWT 或 API Key）

body: {"only": ["账号名"]}  留空处理全部启用账号

同步执行：账号多时可能几十秒，但结果要一次看完。拆异步就得再造一套状态轮询，
而这是人工触发的低频操作。客户端超时请放宽。

会消耗 tabiai 账号的 refresh 代次，所以先过签到锁 —— 跟签到抢代次是会把账号打死
的事，不是「这次失败」这么轻。
*/
func (s *Server) handleEnsureSiteAPIKeys(w http.ResponseWriter, r *http.Request) {
	var req struct {
		Only []string `json:"only"`
	}
	if err := readJSON(w, r, &req); err != nil {
		writeError(w, http.StatusBadRequest, "请求体不是合法的 JSON")
		return
	}
	if s.guardRunningCheckin(w) {
		return
	}

	apiKeyMu.Lock()
	defer apiKeyMu.Unlock()

	cfg, _, err := LoadConfig(s.db)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "服务器内部错误")
		return
	}
	targets := selectAPIKeyTargets(&cfg, req.Only)
	if len(targets) == 0 {
		writeError(w, http.StatusBadRequest,
			"没有可处理的账号（没有启用账号，或 only 里的名字都不存在）")
		return
	}

	results := make([]apiKeyOutcome, 0, len(targets))
	keys := make(map[string]string, len(targets))    // 账号名 → sk-xxx
	rotated := make(map[string]string, len(targets)) // 账号名 → 新的 refresh 代次
	for _, account := range targets {
		// 直连，不发代理池地址。跟签发链路同一个决定（见 c344e1b）：这类操作要
		// 出口稳定，账号自带的 proxy 仍会在 newCookieTestHTTPClient 里生效
		out, key, rot := ensureOneAccountAPIKey(r.Context(), &cfg, account, "")
		results = append(results, out)
		if rot != "" {
			rotated[account.Name] = rot
		}
		if key != "" {
			keys[account.Name] = key
		}
		log.Printf("[apikey] %s → %s %s", account.Name, out.Status, out.Message)
	}

	// 代次必须落库，哪怕这一轮 key 一个都没拿到：站点侧代次已经推进，
	// 平台不记就永远停在旧代，下轮保活拿旧代去 refresh 就是整条会话报废
	if len(keys) > 0 || len(rotated) > 0 {
		if err := s.saveAccountAPIKeys(keys, rotated); err != nil {
			writeError(w, http.StatusInternalServerError, "取到了 key 但写库失败: "+err.Error())
			return
		}
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"ok":      true,
		"total":   len(results),
		"got":     len(keys),
		"results": results,
	})
}

/*
ensureOneAccountAPIKey 处理一个账号，返回 (处置结果, key, 轮转出的新代次)。

key 与 rotated 分开返回：rotated 即使在失败路径上也可能非空（refresh 成功、后面
的步骤挂了），调用方必须无条件落库。
*/
func ensureOneAccountAPIKey(ctx context.Context, cfg *Config, account Account,
	proxyAddr string) (apiKeyOutcome, string, string) {
	out := apiKeyOutcome{AccountName: account.Name}
	resolved := effectiveGitHubCredentials(cfg, account)
	if strings.TrimSpace(resolved.Cookie) == "" {
		out.Status = apiKeyNoCreds
		out.Message = "账号没有站点凭据，无法登录取 key"
		return out, "", ""
	}

	sess, err := openAPIKeySession(ctx, cfg.HTTP, resolved, proxyAddr)
	rot := ""
	if sess != nil {
		rot = sess.rotated
	}
	if err != nil {
		out.Status = apiKeyFailed
		out.Message = "登录站点失败: " + err.Error()
		return out, "", rot
	}

	key, created, err := ensureAccountAPIKey(ctx, sess)
	if err != nil {
		out.Status = apiKeyFailed
		out.Message = err.Error()
		return out, "", rot
	}
	out.Status = apiKeyReused
	if created {
		out.Status = apiKeyCreated
	}
	return out, key, rot
}

/*
saveAccountAPIKeys 落库：API Key 与轮转出的新代次走两条不同的路，不能合成一次写。

API Key 走 saveConfigKeepingCookiesLocked（持锁重读 + 按名定点改）。不能拿上层那份
cfg 整份写回：取 key 可能持续几十秒到几分钟，期间后台保活很可能已经轮转过别的账号
的凭据，整份写回会把那些新代次抹掉。这跟 saveProvisionedAccounts 是同一个理由。

代次必须走 updateAccountCookie，**不能塞进上面那次写入**：
saveConfigKeepingCookiesLocked 会按设计强制保留库中现有的 TaBiAI cookie、忽略传进
去的值（那是为了防止编辑页把旧凭据覆盖回去）。混在一起写，新代次会被它覆盖回旧值，
然后下一轮保活拿旧代去 refresh 就是 AUTH_SESSION_REVOKED、整条会话报废 ——
而且这个过程一声不响。

代次先写：它比 key 重要得多。key 丢了重跑一次就有，代次丢了账号就死了。
*/
func (s *Server) saveAccountAPIKeys(keys, rotated map[string]string) error {
	for name, rot := range rotated {
		if strings.TrimSpace(rot) == "" {
			continue
		}
		if _, err := updateAccountCookie(s.db, name, rot); err != nil {
			return fmt.Errorf("账号 %q 的新凭据代次落库失败: %w", name, err)
		}
	}
	if len(keys) == 0 {
		return nil
	}

	configWriteMu.Lock()
	defer configWriteMu.Unlock()

	latest, _, err := loadConfigLocked(s.db)
	if err != nil {
		return err
	}
	target := *cloneConfig(&latest)
	for i := range target.Accounts {
		if key, ok := keys[target.Accounts[i].Name]; ok && key != "" {
			target.Accounts[i].APIKey = key
		}
	}
	if err := ValidateConfig(&target); err != nil {
		return err
	}
	_, err = saveConfigKeepingCookiesLocked(s.db, target)
	return err
}

// selectAPIKeyTargets 按 only 过滤启用账号；only 为空则取全部启用的。
//
// 停用账号跳过：它们本来就不参与签到，为它们建令牌只是白打站点。
// only 里不存在的名字静默忽略，与 provision 同口径 —— 结果里的 total 对不上就是提示。
func selectAPIKeyTargets(cfg *Config, only []string) []Account {
	want := make(map[string]bool, len(only))
	for _, name := range only {
		if trimmed := strings.TrimSpace(name); trimmed != "" {
			want[trimmed] = true
		}
	}
	out := make([]Account, 0, len(cfg.Accounts))
	for _, a := range cfg.Accounts {
		if !a.Enabled {
			continue
		}
		if len(want) > 0 && !want[strings.TrimSpace(a.Name)] {
			continue
		}
		out = append(out, a)
	}
	return out
}

// apiKeyEntry 汇总清单里的一条。
type apiKeyEntry struct {
	Account string `json:"account"`
	URL     string `json:"url"`
	APIKey  string `json:"api_key"`
	// Has 有没有取到过 key。打码视图里 api_key 是掩码，光看它判断不出「有值」
	// 还是「掩码恰好长这样」，所以显式给一个布尔
	Has bool `json:"has_key"`
}

/*
handleListSiteAPIKeys GET /api/sites/apikeys（JWT 或 API Key）

清单视图：key 一律打码。用来看「哪些账号有、哪些还缺」，不能用来复制。
要明文走下面那个 /raw，与 GET /api/config → GET /api/config/raw 同一套惯例。
*/
func (s *Server) handleListSiteAPIKeys(w http.ResponseWriter, r *http.Request) {
	entries, err := s.collectAPIKeyEntries(true)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "服务器内部错误")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"total": len(entries), "items": entries})
}

/*
handleListSiteAPIKeysRaw GET /api/sites/apikeys/raw（只认 API Key）

明文清单，一次性拷走用。跟 /api/config/raw、/api/accounts/{name}/raw 同级：
含明文凭据的端点一律不给 JWT —— 浏览器里的 token 更容易被顺走，而这些 key 能直接
调用付费接口。
*/
func (s *Server) handleListSiteAPIKeysRaw(w http.ResponseWriter, r *http.Request) {
	entries, err := s.collectAPIKeyEntries(false)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "服务器内部错误")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"total": len(entries), "items": entries})
}

// collectAPIKeyEntries 汇总所有启用账号的 key。mask 为真时打码。
//
// 没取到 key 的账号也列出来（api_key 空、has_key false）：清单的用处之一就是看
// 还差哪些，把它们过滤掉反而要人拿两份名单去对。
func (s *Server) collectAPIKeyEntries(mask bool) ([]apiKeyEntry, error) {
	cfg, _, err := LoadConfig(s.db)
	if err != nil {
		return nil, err
	}
	entries := make([]apiKeyEntry, 0, len(cfg.Accounts))
	for _, a := range cfg.Accounts {
		if !a.Enabled {
			continue
		}
		key := strings.TrimSpace(a.APIKey)
		entry := apiKeyEntry{Account: a.Name, URL: a.URL, Has: key != ""}
		switch {
		case key == "":
			entry.APIKey = ""
		case mask:
			entry.APIKey = maskAPIKeyForDisplay(key)
		default:
			entry.APIKey = key
		}
		entries = append(entries, entry)
	}
	return entries, nil
}

/*
maskAPIKeyForDisplay 展示用脱敏：留头留尾，中间打星。

不用 MaskPlaceholder（"***"）那种一刀切：清单的用途是核对，运维要能看出「这条是
不是我期望的那把 key」。留 6 位头（含 sk- 前缀）加 4 位尾，既能对号又不足以拼出原值。
太短的值一律全打码 —— 那种长度留几位就等于泄露。
*/
func maskAPIKeyForDisplay(key string) string {
	const head, tail = 6, 4
	if len(key) <= head+tail {
		return strings.Repeat("*", len(key))
	}
	return key[:head] + strings.Repeat("*", 10) + key[len(key)-tail:]
}
