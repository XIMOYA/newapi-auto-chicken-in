/*
server/site_provision.go
按站点 URL 为池子里的 GitHub 账号批量创建签到账号：POST /api/sites/provision

为什么需要它：新接一个站点时，要为每个 GitHub 账号在那个站点上建一个签到账号 ——
手工做法是逐个开浏览器点「用 GitHub 登录」，站点在 OAuth 首次登录时自动注册用户。
而平台这边已经有整套纯 HTTP 的 OAuth 三步（issueTabiAIRefreshCookie），拿 user_session
就能换回站点凭据，注册与登录在站点侧本来就是同一条路，所以不需要浏览器。

三种结局，处置完全不同：
  - 成功：站点下发了凭据 → upsert 一条 accounts[] 条目（名字走 composeAccountName，
    即「GitHub名（域名）」），凭据一并落库
  - 站点关闭注册：跳过且**不重试** —— 重试改变不了站点的开关，只是白等
  - 其他失败：重试到 provisionMaxAttempts 次才放弃。网络抖动、限流、站点偶发 5xx
    都属于这一类，一次失败就跳过会漏掉本来能建成的账号

已存在的账号直接跳过，不重新签发：签发会换出新凭据代次，把还能用的会话作废掉。
*/
package main

import (
	"context"
	"log"
	"net/http"
	"strings"
	"sync"
)

// provisionMaxAttempts 单个账号的尝试次数上限（首次 + 重试）。
// KIQ 的口径是「其他情况重试两次不行才跳过」，即总共尝试三次。
const provisionMaxAttempts = 3

/*
registrationClosedMarkers 站点「关闭注册」的特征词。

判定刻意做成关键词匹配而不是认某个错误码：各家 new-api 分支的文案不统一，而且这条
判定错了的代价不对称 —— 误判成「关闭注册」会让本来能建的账号被永久跳过（不重试），
而漏判只是多重试两次然后跳过。所以宁可漏判。

命中任一即认定关闭。中英文都列上：站点可能按 Accept-Language 返回不同语言。
*/
var registrationClosedMarkers = []string{
	"关闭注册", "注册已关闭", "禁止注册", "未开放注册", "不允许注册",
	"管理员关闭了新用户注册", "管理员未开启",
	"registration is disabled", "registration disabled", "registration closed",
	"registration is not allowed", "sign up is disabled", "signup disabled",
	"new registration", // 「new registration is not allowed」这类变体
}

// looksLikeRegistrationClosed 判断一条失败原因是不是「站点关闭注册」。
func looksLikeRegistrationClosed(message string) bool {
	lower := strings.ToLower(message)
	for _, marker := range registrationClosedMarkers {
		if strings.Contains(lower, strings.ToLower(marker)) {
			return true
		}
	}
	return false
}

// provisionOutcome 单个账号的处置结果。
type provisionOutcome struct {
	// GitHubAccount 池子里的账号名
	GitHubAccount string `json:"github_account"`
	// AccountName 建成的站点账号名（跳过时为空）
	AccountName string `json:"account_name,omitempty"`
	// Status created / exists / skipped_registration_closed / failed
	Status string `json:"status"`
	// Attempts 实际尝试了几次
	Attempts int    `json:"attempts"`
	Message  string `json:"message,omitempty"`
}

const (
	provisionCreated = "created"
	provisionExists  = "exists"
	provisionClosed  = "skipped_registration_closed"
	provisionFailed  = "failed"
	provisionNoCreds = "skipped_no_credentials"
)

// provisionIssuer 签发一条站点凭据。抽成函数类型是为了让测试能注入假实现 ——
// 真实实现要访问 GitHub 与站点，端点级测试没法也不该真去连。
type provisionIssuer func(ctx context.Context, account Account, fp githubFingerprint) (string, error)

/*
provisionOneAccount 为一个 GitHub 账号在给定站点上建号，返回处置结果。

siteURL 已经过校验；existing 是当前配置里已有的账号名集合。

重试只针对「其他失败」：关闭注册与凭据缺失都是终态，重试不会改变结果，
白等还会多打几次站点。
*/
func provisionOneAccount(ctx context.Context, cfg *Config, pool GitHubAccount,
	siteURL string, existing map[string]bool, issue provisionIssuer) (provisionOutcome, string) {
	out := provisionOutcome{GitHubAccount: pool.Name}

	accountName := composeAccountName(pool.Name, siteURL)
	if accountName == "" {
		out.Status = provisionFailed
		out.Message = "凑不出规范账号名（GitHub 名或站点域名缺失）"
		return out, ""
	}
	out.AccountName = accountName

	if existing[accountName] {
		out.Status = provisionExists
		out.Message = "账号已存在，未重新签发（重签会作废还能用的会话）"
		return out, ""
	}
	if strings.TrimSpace(pool.UserSession) == "" {
		out.Status = provisionNoCreds
		out.Message = "该 GitHub 账号没有 user_session，无法签发"
		return out, ""
	}

	// 构造一个「临时账号」交给签发链路：它引用池子里这条记录，
	// 于是凭据与指纹都从池子取（与 issue-cookie 端点同一套口径）
	probe := Account{
		Name:          accountName,
		URL:           siteURL,
		LoginMethod:   LoginMethodTabiAI,
		GitHubAccount: pool.Name,
		Enabled:       true,
	}
	fp := deriveGitHubFingerprint(pool.Fingerprint)

	for attempt := 1; attempt <= provisionMaxAttempts; attempt++ {
		out.Attempts = attempt
		cookie, err := issue(ctx, effectiveGitHubCredentials(cfg, probe), fp)
		if err == nil && strings.TrimSpace(cookie) != "" {
			out.Status = provisionCreated
			return out, cookie
		}
		reason := "站点未下发凭据"
		if err != nil {
			reason = err.Error()
		}
		if looksLikeRegistrationClosed(reason) {
			out.Status = provisionClosed
			out.Message = reason
			return out, ""
		}
		out.Status = provisionFailed
		out.Message = reason
		if attempt < provisionMaxAttempts {
			log.Printf("[provision] %q 第 %d/%d 次失败，重试: %s",
				pool.Name, attempt, provisionMaxAttempts, reason)
		}
	}
	return out, ""
}

// provisionMu 串行化整个批量建号：它会对同一个 GitHub 账号连续打 OAuth，
// 两批并发跑等于主动喂 GitHub 限流。
var provisionMu sync.Mutex

/*
handleProvisionSite POST /api/sites/provision（JWT 或 API Key）

body: {"url": "https://a.example.com", "only": ["Steven"]}
  - url  必填，站点地址
  - only 可选，只处理这几个 GitHub 账号；留空处理池子全部

同步执行：整批可能几十秒到几分钟，但结果必须一次看完 —— 拆成异步任务就要再造一套
状态轮询，而这是个人工触发的低频操作。客户端超时请放宽。
*/
func (s *Server) handleProvisionSite(w http.ResponseWriter, r *http.Request) {
	var req struct {
		URL  string   `json:"url"`
		Only []string `json:"only"`
	}
	if err := readJSON(w, r, &req); err != nil {
		writeError(w, http.StatusBadRequest, "请求体不是合法的 JSON")
		return
	}
	siteURL := strings.TrimSpace(req.URL)
	lower := strings.ToLower(siteURL)
	if siteURL == "" {
		writeError(w, http.StatusBadRequest, "url 不能为空")
		return
	}
	if !strings.HasPrefix(lower, "http://") && !strings.HasPrefix(lower, "https://") {
		writeError(w, http.StatusBadRequest, "url 必须以 http:// 或 https:// 开头")
		return
	}
	if siteHostOf(siteURL) == "" {
		writeError(w, http.StatusBadRequest, "url 里取不出域名")
		return
	}
	// 建号会为每个账号签发一条新凭据，与签到抢代次；签到进行中一律拦住
	if s.guardRunningCheckin(w) {
		return
	}

	provisionMu.Lock()
	defer provisionMu.Unlock()

	cfg, _, err := LoadConfig(s.db)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "服务器内部错误")
		return
	}
	targets := selectProvisionTargets(&cfg, req.Only)
	if len(targets) == 0 {
		writeError(w, http.StatusBadRequest,
			"没有可处理的 GitHub 账号（池子为空，或 only 里的名字都不存在）")
		return
	}

	existing := make(map[string]bool, len(cfg.Accounts))
	for _, a := range cfg.Accounts {
		existing[a.Name] = true
	}
	issue := func(ctx context.Context, account Account, fp githubFingerprint) (string, error) {
		// 每个账号走它自己绑定的固定出口。批量建号是「一个 GitHub 会话连续对多个
		// 站点授权」，出口必须始终一致，否则等于告诉 GitHub 这个会话在到处跑
		_, outbound := s.prepareGitHubOutbound(&cfg, account.GitHubAccount)
		return issueTabiAIRefreshCookie(ctx, cfg.HTTP, account,
			s.githubAuthorizeURLOrDefault(), fp, outbound)
	}

	results := make([]provisionOutcome, 0, len(targets))
	created := make(map[string]string, len(targets)) // 账号名 → 凭据
	for _, pool := range targets {
		out, cookie := provisionOneAccount(r.Context(), &cfg, pool, siteURL, existing, issue)
		results = append(results, out)
		if out.Status == provisionCreated {
			created[out.AccountName] = cookie
			existing[out.AccountName] = true // 同批次内不重复建
		}
		log.Printf("[provision] %s @ %s → %s（尝试 %d 次）%s",
			pool.Name, siteURL, out.Status, out.Attempts, out.Message)
	}

	if len(created) > 0 {
		if err := s.saveProvisionedAccounts(siteURL, created); err != nil {
			writeError(w, http.StatusInternalServerError, "凭据已签发但写库失败: "+err.Error())
			return
		}
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"ok":      true,
		"url":     siteURL,
		"created": len(created),
		"total":   len(results),
		"results": results,
	})
}

// selectProvisionTargets 按 only 过滤池子；only 为空则返回全部。
// 不存在的名字静默忽略：批量操作里为一个拼错的名字整批失败不划算，
// 结果里的 total 与传入数量对不上就是提示。
func selectProvisionTargets(cfg *Config, only []string) []GitHubAccount {
	if len(only) == 0 {
		return cfg.GitHubAccounts
	}
	want := make(map[string]bool, len(only))
	for _, name := range only {
		if trimmed := strings.TrimSpace(name); trimmed != "" {
			want[trimmed] = true
		}
	}
	out := make([]GitHubAccount, 0, len(want))
	for _, g := range cfg.GitHubAccounts {
		if want[strings.TrimSpace(g.Name)] {
			out = append(out, g)
		}
	}
	return out
}

// saveProvisionedAccounts 持锁重读最新配置后追加新账号，再落库。
//
// 不能拿上面那份 cfg 直接写回：签发过程可能持续几分钟，期间后台保活可能已经轮转过
// 别的账号的凭据，整份写回会把那些新代次抹掉、触发站点重放检测。
func (s *Server) saveProvisionedAccounts(siteURL string, created map[string]string) error {
	configWriteMu.Lock()
	defer configWriteMu.Unlock()

	latest, _, err := loadConfigLocked(s.db)
	if err != nil {
		return err
	}
	target := *cloneConfig(&latest)
	have := make(map[string]bool, len(target.Accounts))
	for _, a := range target.Accounts {
		have[a.Name] = true
	}
	for name, cookie := range created {
		if have[name] {
			continue // 期间别人建了同名的，不覆盖
		}
		githubName, _, _ := strings.Cut(name, accountNameOpen)
		target.Accounts = append(target.Accounts, Account{
			Name:          name,
			URL:           siteURL,
			LoginMethod:   LoginMethodTabiAI,
			Cookie:        cookie,
			GitHubAccount: strings.TrimSpace(githubName),
			Enabled:       true,
		})
	}
	if err := ValidateConfig(&target); err != nil {
		return err
	}
	_, err = saveConfigKeepingCookiesLocked(s.db, target)
	return err
}
