/*
server/github_accounts.go
统一 GitHub 账号：一个 GitHub 账号（一份 user_session）可以对应多个站点账号

为什么这么改：
  - 以前 user_session 存在每个 accounts[] 条目里，同一个 GitHub 账号在 N 个站点上
    就要重复填 N 份；换 session 得改 N 处，漏一处那个站点的自救链路就断了。
  - 现在收敛成一份：github_accounts[] 存凭据，accounts[].github_account 按名字引用。

账号名不再手填，由「GitHub 账号名（站点域名）」自动生成，例如 Steven（tabiai.cc）。
用域名而不是完整 URL：名字要过 slugify 当 sessions.json 的键和 profile 目录名，
完整 URL 会变成 Steven_https___tabiai_cc_ 这种又长又难认的东西。
*/
package main

import (
	"fmt"
	"net/url"
	"strings"
)

// GitHubAccount 一个 GitHub 账号的凭据，供多个站点账号共用。
type GitHubAccount struct {
	// Name GitHub 用户名。同时是 accounts[].github_account 的引用键，
	// 也是自动生成的站点账号名的前半段
	Name string `json:"name"`
	// UserSession GitHub 网页会话 cookie（user_session）。签发站点凭据的原料
	UserSession string `json:"user_session"`
	// ClientID 站点 OAuth 应用 ID。留空时由站点 /api/status 探测
	ClientID string `json:"client_id"`
}

// accountNameOpen / accountNameClose 名字里包住域名的括号。
// 用全角是刻意的：站点名和域名里都不会出现全角括号，解析回旧名时不会歧义。
const (
	accountNameOpen  = "（"
	accountNameClose = "）"
)

// siteHostOf 从站点 URL 取出域名（不带 scheme、端口保留）。
// 取不出来时返回空串，调用方据此判定「凑不出规范名字」。
func siteHostOf(rawURL string) string {
	trimmed := strings.TrimSpace(rawURL)
	if trimmed == "" {
		return ""
	}
	parsed, err := url.Parse(trimmed)
	if err != nil || parsed.Host == "" {
		return ""
	}
	return parsed.Host
}

// composeAccountName 拼出规范账号名：GitHub 名（域名）。
// 任一段缺失就返回空串 —— 宁可让调用方保留旧名，也不要造出「（）」这种残缺名字。
func composeAccountName(githubName, siteURL string) string {
	name := strings.TrimSpace(githubName)
	host := siteHostOf(siteURL)
	if name == "" || host == "" {
		return ""
	}
	return name + accountNameOpen + host + accountNameClose
}

// isComposedAccountName 判断一个名字是否已经是规范格式。
// 迁移靠它做幂等：已经是新格式的账号跳过，不会反复改名。
func isComposedAccountName(name string) bool {
	trimmed := strings.TrimSpace(name)
	if !strings.HasSuffix(trimmed, accountNameClose) {
		return false
	}
	at := strings.Index(trimmed, accountNameOpen)
	// 括号必须在中间：前面得有 GitHub 名，里面得有域名
	return at > 0 && at < len(trimmed)-len(accountNameOpen)-len(accountNameClose)
}

// findGitHubAccount 按名字查 GitHub 账号（精确匹配，与账号定位同一套口径）。
func findGitHubAccount(cfg *Config, name string) *GitHubAccount {
	target := strings.TrimSpace(name)
	if target == "" {
		return nil
	}
	for i := range cfg.GitHubAccounts {
		if cfg.GitHubAccounts[i].Name == target {
			return &cfg.GitHubAccounts[i]
		}
	}
	return nil
}

// resolveAccountSession 取该站点账号该用的 GitHub 凭据。
//
// 优先级：引用的 github_accounts 条目 > 账号自己残留的旧字段。
// 保留旧字段兜底是为了迁移期间不断供 —— 老配置还没填 github_accounts 时，
// 签发链路照旧能用 accounts[].github_user_session 跑起来。
func resolveAccountSession(cfg *Config, account Account) (session, clientID string) {
	if ref := findGitHubAccount(cfg, account.GitHubAccount); ref != nil {
		session = strings.TrimSpace(ref.UserSession)
		clientID = strings.TrimSpace(ref.ClientID)
	}
	if session == "" {
		session = strings.TrimSpace(account.GithubUserSession)
	}
	if clientID == "" {
		clientID = strings.TrimSpace(account.GithubClientID)
	}
	return session, clientID
}

// effectiveGitHubCredentials 返回一份把 GitHub 凭据填成「实际生效值」的账号副本。
//
// 为什么是填副本，而不是给下游多传两个参数：签发链路里
// issueTabiAIRefreshCookie / resolveGithubClientID / newTabiAIOAuthClient
// 有好几处各自读这两个字段，改签名要动整条链路连带它的测试；而「凭据是从池子来
// 还是从账号旧字段来」属于配置层的事，签发链路不需要知道。
//
// 副本只用于本次签发，绝不落库 —— 真把解析结果写回配置就等于悄悄迁移了数据，
// 用户下次打开界面会发现账号里凭空多出一份凭据，池子也就白建了。
func effectiveGitHubCredentials(cfg *Config, account Account) Account {
	session, clientID := resolveAccountSession(cfg, account)
	effective := account
	effective.GithubUserSession = session
	effective.GithubClientID = clientID
	return effective
}

// planAccountRenames 算出「旧名 -> 新名」映射，只包含真正需要改的。
//
// 跳过三种情况：没引用 GitHub 账号的、拼不出规范名的、已经是规范名的。
// 重名直接报错而不是加序号：用户确认过「同一个 GitHub 账号在同一站点不会有多个」，
// 真撞上说明配置有问题，静默加序号只会把问题藏起来。
func planAccountRenames(cfg *Config) (map[string]string, error) {
	renames := make(map[string]string)
	taken := make(map[string]string) // 新名 -> 来源旧名
	for i := range cfg.Accounts {
		account := cfg.Accounts[i]
		if strings.TrimSpace(account.GitHubAccount) == "" {
			continue
		}
		want := composeAccountName(account.GitHubAccount, account.URL)
		if want == "" || want == account.Name {
			continue
		}
		if from, dup := taken[want]; dup {
			return nil, fmt.Errorf("账号 %q 与 %q 会重名为 %q（同一个 GitHub 账号在同一站点只应有一个）",
				account.Name, from, want)
		}
		taken[want] = account.Name
		renames[account.Name] = want
	}
	return renames, nil
}
