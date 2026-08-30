/*
server/github_account_check.go
GitHub 账号可用性检测：POST /api/github-accounts/check

为什么需要它：user_session 在 GitHub 那边失效是静默的 —— 签到看着还在跑，
实际每次自救都拿它去授权却换来 /login。这个端点主动探测一个池子账号的 user_session
在某个站点上还能不能授权。

判定只走到「拿到 code 就停」：授权 code 是签发的中间产物，多兑换一次不会伤害
session，但没必要。三态：
  - ok      ：GitHub 返回了授权 code，session 有效
  - expired ：GitHub 要求重新登录（跳 /login），session 已失效
  - unknown ：出口被 GitHub 限流、站点 OAuth 流程出错、网络失败等，
    session 本身是否有效无法下结论

为什么必须同步而不是后台任务：单次检查就几十秒，点一次看一次结果，不需要
任务队列和轮询那套状态管理。
*/
package main

import (
	"context"
	"log"
	"net/http"
	"strings"
	"sync"
)

// githubCheckResult 一次检测的结论。
type githubCheckResult struct {
	Status  string `json:"status"` // ok / expired / unknown
	Message string `json:"message"`
	// AuthorizedClientID 站点的 OAuth 应用 ID。探测站点时需要它，回显出来
	// 用户能直接看出测的是不是自己配的那个应用
	AuthorizedClientID string `json:"authorized_client_id,omitempty"`
}

// poolReferencingAccount 取第一个引用了该 GitHub 账号的站点账号。
// 检测要落在「某个站点」上 —— 凭据本身不挂在站点上，引用它的账号才有站点上下文。
func poolReferencingAccount(cfg *Config, poolName string) (*Account, bool) {
	for i := range cfg.Accounts {
		if strings.TrimSpace(cfg.Accounts[i].GitHubAccount) == poolName {
			return &cfg.Accounts[i], true
		}
	}
	return nil, false
}

// checkTabiAIGithubSession 探测 session 在给定站点上还能不能完成授权。
//
// 只走前两步（取 flow_token、换授权 code），拿到 code 就停不兑换 —— 检测不该
// 消耗站点侧的授权码配额，更不该无意中把一条新的 new_api_refresh 落到库里。
// authorizeURL 供测试注入；生产传 GitHub 官方地址。
func checkTabiAIGithubSession(ctx context.Context, httpCfg HTTPConfig, account Account,
	authorizeURL string, fp githubFingerprint) githubCheckResult {
	base, err := cookieTestBaseURL(account.URL)
	if err != nil {
		return githubCheckResult{Status: "unknown", Message: "站点 URL 无效: " + err.Error()}
	}
	client, err := newTabiAIOAuthClient(account, httpCfg)
	if err != nil {
		return githubCheckResult{Status: "unknown", Message: err.Error()}
	}

	// 第 1 步：取 flow_token。这一步挂在站点上 —— 站点都连不上时，session 本身
	// 是否有效无从判断，归 unknown
	state, err := fetchTabiAIOAuthState(ctx, client, base, fp)
	if err != nil {
		return githubCheckResult{Status: "unknown", Message: err.Error()}
	}

	// 第 2 步：带 session 换授权 code。这里才真正判定 session 的状态。
	// code 拿到即止：检测不兑换，不该消耗站点侧的授权码配额
	if _, err := fetchGithubAuthorizeCode(ctx, client, base, account, state, authorizeURL, fp); err != nil {
		status := "unknown"
		if strings.Contains(err.Error(), "已失效") {
			status = "expired"
		}
		return githubCheckResult{Status: status, Message: err.Error()}
	}
	return githubCheckResult{
		Status:             "ok",
		Message:            "GitHub 返回授权 code，user_session 有效",
		AuthorizedClientID: strings.TrimSpace(account.GithubClientID),
	}
}

// handleCheckGitHubAccount POST /api/github-accounts/check
// body: {"name": "Steven"} —— 按池子账号名探测它引用的站点。
func (s *Server) handleCheckGitHubAccount(w http.ResponseWriter, r *http.Request) {
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

	cfg, _, err := LoadConfig(s.db)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "服务器内部错误")
		return
	}
	pool := findGitHubAccount(&cfg, name)
	if pool == nil {
		writeError(w, http.StatusNotFound, "GitHub 账号不存在: "+name)
		return
	}
	ref, ok := poolReferencingAccount(&cfg, pool.Name)
	if !ok {
		writeError(w, http.StatusBadRequest,
			"GitHub 账号 "+name+" 还没有被任何站点账号引用，无法确定探测哪个站点")
		return
	}

	// 凭据取「实际生效值」：池子优先、账号旧字段兜底。复测不落库，不影响原配置
	effective := effectiveGitHubCredentials(&cfg, *ref)

	// 探测会实际请求 GitHub，串行做防止触发 OAuth 端点的限流
	githubCheckMu.Lock()
	defer githubCheckMu.Unlock()

	result := checkTabiAIGithubSession(r.Context(), cfg.HTTP, effective,
		s.githubAuthorizeURLOrDefault(), effectiveGitHubFingerprint(&cfg, *ref))
	log.Printf("[github-accounts] 探测 %q（站点 %s）: %s",
		pool.Name, ref.URL, result.Status)
	writeJSON(w, http.StatusOK, map[string]any{
		"ok":     true,
		"name":   pool.Name,
		"site":   ref.URL,
		"result": result,
	})
}

// githubAuthorizeURLOrDefault 探测时用的 GitHub authorize 地址。
// 测试钩子留空时回落官方地址，生产路径与硬编码时行为一致。
func (s *Server) githubAuthorizeURLOrDefault() string {
	if s.githubAuthorizeURL != "" {
		return s.githubAuthorizeURL
	}
	return tabiaiGithubAuthorize
}

// githubCheckMu 串行化 GitHub 账号探测：GitHub OAuth 端点对固定出口有明确限流，
// 几个检测并发打过去等于主动喂限流，探测任务本身还不存在并发价值。
var githubCheckMu sync.Mutex
