/*
server/config.go
NewAPI 签到配置管理平台 · 配置对象模型

职责：
- 定义与接口契约一致的完整配置结构体（accounts / ai / browser / http / proxy_pool / notify / config_sync / security）
- 提供默认配置（契约文档「配置对象默认结构」）
- 敏感字段打码 / 还原：accounts[].cookie、ai.api_key、notify.email.password、config_sync.token
- 配置校验：accounts 必填 name/url，cookie 可为空，url 需 http(s) 开头
*/
package main

import (
	"fmt"
	"strings"
)

// MaskPlaceholder 敏感字段占位符：前端展示打码值，后端识别为「未修改，保留原值」。
const MaskPlaceholder = "***"

const (
	LoginMethodNewAPICookie = "newapi_cookie"
	// LoginMethodTabiAI TaBiAI（New API 分支）：凭据是 new_api_refresh cookie，
	// 走 POST /api/user/auth/refresh 换短期 access token，业务接口只认 Bearer。
	LoginMethodTabiAI = "tabiai"

	// legacyLoginMethodGitHubCookie 已废弃的 GitHub OAuth 登录方式，仅用于识别旧配置并迁移。
	legacyLoginMethodGitHubCookie = "github_cookie"
)

// currentConfigVersion 用于一次性迁移旧默认值。旧配置没有该字段，视为 0。
// v3：登录方式 github_cookie 并入 tabiai（GitHub OAuth 不再是登录方式）
const currentConfigVersion = 3

// Config 完整配置对象 = 契约文档中的顶层结构。
type Config struct {
	ConfigVersion int       `json:"config_version"`
	Accounts      []Account `json:"accounts"`
	// GitHubAccounts 统一的 GitHub 凭据池，供多个站点账号按名字引用（见 github_accounts.go）
	GitHubAccounts []GitHubAccount `json:"github_accounts"`
	Sites          []Site          `json:"sites"`
	AI             AIConfig        `json:"ai"`
	Browser        BrowserConfig   `json:"browser"`
	HTTP           HTTPConfig      `json:"http"`
	ProxyPool      ProxyPool       `json:"proxy_pool"`
	Notify         Notify          `json:"notify"`
	ConfigSync     ConfigSync      `json:"config_sync"`
	Security       Security        `json:"security"`
}

// Site 站点预设：供新增账号时快速选择，自动带出 URL 与接口路径。
type Site struct {
	Name        string  `json:"name"`
	URL         string  `json:"url"`
	CheckinPath *string `json:"checkin_path"`
	BrowserPath *string `json:"browser_path"`
}

// Account 签到账号：name/url 为必填；两种 Cookie 均可暂不设置，其余字段可选。
type Account struct {
	Name        string `json:"name"`
	URL         string `json:"url"`
	LoginMethod string `json:"login_method"`
	// Cookie 站点凭据。newapi_cookie 为完整 Cookie 头；
	// tabiai 为 new_api_refresh 值（sid.secret，每次 refresh 后会被轮转覆盖）
	Cookie string `json:"cookie"`
	// GithubUserSession / GithubClientID 不再是登录凭据，
	// 仅供「签发 TaBiAI cookie」小工具走 GitHub OAuth 三步时使用
	GithubUserSession string `json:"github_user_session"`
	GithubClientID    string `json:"github_client_id"`
	// GitHubAccount 引用 github_accounts[].name。非空时凭据从那里取，
	// 账号名也由「该 GitHub 名（本站域名）」自动生成，不再手填。
	// 上面两个旧字段保留：老配置迁移期间仍作兜底，见 resolveAccountSession
	GitHubAccount string  `json:"github_account"`
	UserID        *int64  `json:"user_id"`
	Proxy         *string `json:"proxy"`
	CheckinPath   *string `json:"checkin_path"`
	BrowserPath   *string `json:"browser_path"`
	Enabled       bool    `json:"enabled"`
}

// AIConfig AI 辅助配置。
type AIConfig struct {
	Enabled    bool   `json:"enabled"`
	BaseURL    string `json:"base_url"`
	APIKey     string `json:"api_key"`
	Model      string `json:"model"`
	Timeout    int    `json:"timeout"`
	MaxRetries int    `json:"max_retries"`
}

// BrowserConfig 浏览器自动化配置。
type BrowserConfig struct {
	Driver              string  `json:"driver"`
	Headless            string  `json:"headless"`
	Humanize            bool    `json:"humanize"`
	Timeout             int     `json:"timeout"`
	KeepArtifactsOnFail bool    `json:"keep_artifacts_on_fail"`
	Locale              string  `json:"locale"`
	Window              []int   `json:"window"`
	ExecutablePath      *string `json:"executable_path"`
}

// HTTPConfig HTTP 请求客户端配置。
type HTTPConfig struct {
	Impersonate string `json:"impersonate"`
	Timeout     int    `json:"timeout"`
	Verify      bool   `json:"verify"`
}

// ProxyPool 隧道（代理）池配置。
type ProxyPool struct {
	Enabled     bool     `json:"enabled"`
	TestURL     string   `json:"test_url"`
	Timeout     int      `json:"timeout"`
	MaxWorkers  int      `json:"max_workers"`
	MaxProxies  int      `json:"max_proxies"`
	IPSwapLimit int      `json:"ip_swap_limit"`
	Sources     []string `json:"sources"`
	// 服务器端代理池后台刷新：服务常驻抓取+测通，供页面展示与 Actions 预取。
	// RefreshMinutes <= 0 表示关闭后台刷新（仅手动/页面触发）。
	RefreshMinutes int  `json:"refresh_minutes"`
	SaveLimit      int  `json:"save_limit"` // 保留多少条可用代理；0 = 不限制（测通多少存多少）
	AutoTest       bool `json:"auto_test"`  // 后台刷新时是否测通+测延迟（默认 true）
	// Actions 预取：Python 端配置的 remote_url / remote_token 等字段，这里透传保存。
	// 实际 Actions 通过 /api/proxies/available 取，鉴权用同一个 API Key（remote_token）。
	RemoteURL         string `json:"remote_url"`
	RemoteToken       string `json:"remote_token"`
	RemoteTokenHeader string `json:"remote_token_header"`
	RemoteTokenPrefix string `json:"remote_token_prefix"`
	// ReportFeedback：Actions 跑完是否把每个代理的成败计数回传（POST /api/proxies/feedback）。
	// 关掉之后优选只剩服务器自测的延迟/测速，而那测的是本机到代理的链路，与 runner
	// 在 Azure 那边的可达性并不是一回事。
	ReportFeedback bool `json:"report_feedback"`
	// 开跑前自筛：客户端拉到预取列表后先在本机快测一遍，当场剔掉连不上的再用。
	// 打的仍是 TestURL，不碰目标站点；PreflightSeconds 是整体时间盒，到点就收手。
	PreflightCheck   bool `json:"preflight_check"`
	PreflightLimit   int  `json:"preflight_limit"`
	PreflightSeconds int  `json:"preflight_seconds"`
	// MaxAccountsPerIP 同一出口 IP 最多给几个账号用，<=0 视为不限。
	//
	// 客户端据此分配代理，也据此折算要预取多少个：占用量 = ceil(账号数 / 这个值)，
	// 再加固定余量。共用是正常策略而不是降级 —— 4 个账号共用一个出口通常不会招来
	// 更多质询，换来的是预取与测通量大幅下降。发现盾突然变难过时先把它调小。
	MaxAccountsPerIP int `json:"max_accounts_per_ip"`
	// SpeedTestURL 测下载速度用的端点，和 TestURL（只测通不通）是两回事。
	//
	// 吞吐按**实际读到的字节数**除以耗时算，所以换成别的地址不需要同步改什么"预期
	// 大小"。但地址得能稳定吐出足够的数据：几 KB 的页面测出来的数字受握手开销主导，
	// 排序意义不大。默认那个 Cloudflare 端点专门干这个用，全球都快。
	SpeedTestURL string `json:"speed_test_url"`
}

// Notify 通知配置。
type Notify struct {
	Email EmailConfig `json:"email"`
}

// EmailConfig 邮件通知配置。
type EmailConfig struct {
	Enabled       bool     `json:"enabled"`
	SMTPHost      string   `json:"smtp_host"`
	SMTPPort      int      `json:"smtp_port"`
	UseSSL        bool     `json:"use_ssl"`
	Username      string   `json:"username"`
	Password      string   `json:"password"`
	FromAddr      string   `json:"from_addr"`
	ToAddrs       []string `json:"to_addrs"`
	SubjectPrefix string   `json:"subject_prefix"`
	Timeout       int      `json:"timeout"`
}

// ConfigSync 配置同步（远端拉取配置）配置。
type ConfigSync struct {
	Enabled           bool              `json:"enabled"`
	URL               string            `json:"url"`
	Method            string            `json:"method"`
	Token             string            `json:"token"`
	TokenHeader       string            `json:"token_header"`
	TokenPrefix       string            `json:"token_prefix"`
	Headers           map[string]string `json:"headers"`
	Body              any               `json:"body"`
	ResponseField     string            `json:"response_field"`
	Timeout           int               `json:"timeout"`
	AutoBeforeCheckin bool              `json:"auto_before_checkin"`
}

// Security 安全配置。
type Security struct {
	EncryptionEnabled bool   `json:"encryption_enabled"`
	ConfigKey         string `json:"config_key"`
	EncryptedFile     string `json:"encrypted_file"`
}

// DefaultConfig 返回契约文档规定的「配置对象默认结构」。
func DefaultConfig() Config {
	return Config{
		ConfigVersion: currentConfigVersion,
		Accounts:      []Account{},
		Sites:         []Site{},
		AI: AIConfig{
			Enabled:    false,
			BaseURL:    "",
			APIKey:     "",
			Model:      "gpt-4o-mini",
			Timeout:    60,
			MaxRetries: 2,
		},
		Browser: BrowserConfig{
			Driver:              "camoufox",
			Headless:            "virtual",
			Humanize:            true,
			Timeout:             60,
			KeepArtifactsOnFail: true,
			Locale:              "zh-CN",
			Window:              []int{1280, 800},
			ExecutablePath:      nil,
		},
		HTTP: HTTPConfig{
			Impersonate: "chrome",
			Timeout:     20,
			Verify:      true,
		},
		ProxyPool: ProxyPool{
			Enabled:           false,
			TestURL:           "https://agentrouter.org/",
			Timeout:           8,
			MaxWorkers:        25,
			MaxProxies:        250,
			IPSwapLimit:       10,
			Sources:           []string{},
			RefreshMinutes:    30,
			SaveLimit:         0,
			AutoTest:          true,
			RemoteURL:         "",
			RemoteToken:       "",
			RemoteTokenHeader: "Authorization",
			RemoteTokenPrefix: "Bearer",
			ReportFeedback:    true,
			PreflightCheck:    true,
			PreflightLimit:    60,
			PreflightSeconds:  15,
			MaxAccountsPerIP:  4,
			SpeedTestURL:      DefaultSpeedTestURL,
		},
		Notify: Notify{
			Email: EmailConfig{
				Enabled:       false,
				SMTPHost:      "smtp.aliyun.com",
				SMTPPort:      465,
				UseSSL:        true,
				Username:      "",
				Password:      "",
				FromAddr:      "",
				ToAddrs:       []string{},
				SubjectPrefix: "NewAPI 签到日报",
				Timeout:       20,
			},
		},
		ConfigSync: ConfigSync{
			Enabled:           false,
			URL:               "",
			Method:            "GET",
			Token:             "",
			TokenHeader:       "Authorization",
			TokenPrefix:       "Bearer",
			Headers:           map[string]string{},
			Body:              nil,
			ResponseField:     "",
			Timeout:           20,
			AutoBeforeCheckin: true,
		},
		Security: Security{
			EncryptionEnabled: false,
			ConfigKey:         "",
			EncryptedFile:     "data/config.encrypted.json",
		},
	}
}

// MaskConfig 返回敏感字段被替换为 "***" 的深拷贝配置（仅用于 GET /api/config）。
// 打码字段：accounts[].cookie、accounts[].github_user_session、ai.api_key、
// notify.email.password、config_sync.token、proxy_pool.remote_token、
// security.config_key；非空才打码，空值原样保留。
//
// accounts[].proxy 有意不打码：地址是运维辨识出口的必要信息，而该字段在前端是普通
// 输入框（不是 MaskedInput）。若打成 http://***@host，用户想「只改 host、保留认证」
// 时提交回来是 http://***@newhost —— host 变了就无从判断该还原谁，结果会把字面量
// "***" 写进代理里，静默弄坏一个本来可用的出口。因此带认证的代理请改用 IP 白名单
// 授权，或接受它会明文回传给已登录管理员这一事实。
func MaskConfig(cfg *Config) *Config {
	m := cloneConfig(cfg)
	for i := range m.Accounts {
		if m.Accounts[i].Cookie != "" {
			m.Accounts[i].Cookie = MaskPlaceholder
		}
		if m.Accounts[i].GithubUserSession != "" {
			m.Accounts[i].GithubUserSession = MaskPlaceholder
		}
	}
	if m.AI.APIKey != "" {
		m.AI.APIKey = MaskPlaceholder
	}
	if m.Notify.Email.Password != "" {
		m.Notify.Email.Password = MaskPlaceholder
	}
	if m.ConfigSync.Token != "" {
		m.ConfigSync.Token = MaskPlaceholder
	}
	// 代理池远程令牌等同 API Key，配置加密密钥更不能明文下发浏览器
	if m.ProxyPool.RemoteToken != "" {
		m.ProxyPool.RemoteToken = MaskPlaceholder
	}
	if m.Security.ConfigKey != "" {
		m.Security.ConfigKey = MaskPlaceholder
	}
	return m
}

// UnmaskConfig 把输入配置中的 "***" 占位符还原为库中旧值（深合并）。
// 规则：仅敏感字段值为 "***" 时保留旧值；其余字段一律以输入为准（含清空、新增、删除账号）。
// 账号 cookie 按「账号名」匹配旧配置还原，避免前端调整账号顺序时按下标还原导致错位。
//
// 占位符在旧配置里找不到对应账号（典型是账号被改名）时**不再把 "***" 字面量落库**：
// 那样界面会继续显示「已设置」，而签到实际拿着 "***" 当凭据用，属于静默数据损坏。
// 这里直接返回可读错误，让调用方以 400 告诉用户重新填写。
func UnmaskConfig(in, old *Config) (*Config, error) {
	out := cloneConfig(in)

	oldCookieByName := make(map[string]string, len(old.Accounts))
	oldGithubSessionByName := make(map[string]string, len(old.Accounts))
	for _, a := range old.Accounts {
		if a.Name != "" {
			oldCookieByName[a.Name] = a.Cookie
			oldGithubSessionByName[a.Name] = a.GithubUserSession
		}
	}
	for i := range out.Accounts {
		name := out.Accounts[i].Name
		if out.Accounts[i].Cookie == MaskPlaceholder {
			c, ok := oldCookieByName[name]
			if !ok {
				return nil, fmt.Errorf(
					"accounts[%d].cookie 无法还原：旧配置中没有名为 %q 的账号（账号改名后需要重新填写站点 Cookie）", i, name)
			}
			out.Accounts[i].Cookie = c
		}
		if out.Accounts[i].GithubUserSession == MaskPlaceholder {
			c, ok := oldGithubSessionByName[name]
			if !ok {
				return nil, fmt.Errorf(
					"accounts[%d].github_user_session 无法还原：旧配置中没有名为 %q 的账号（账号改名后需要重新填写 GitHub Cookie）", i, name)
			}
			out.Accounts[i].GithubUserSession = c
		}
		if strings.TrimSpace(out.Accounts[i].LoginMethod) == "" {
			out.Accounts[i].LoginMethod = LoginMethodNewAPICookie
		}
	}

	if out.AI.APIKey == MaskPlaceholder {
		out.AI.APIKey = old.AI.APIKey
	}
	if out.Notify.Email.Password == MaskPlaceholder {
		out.Notify.Email.Password = old.Notify.Email.Password
	}
	if out.ConfigSync.Token == MaskPlaceholder {
		out.ConfigSync.Token = old.ConfigSync.Token
	}
	if out.ProxyPool.RemoteToken == MaskPlaceholder {
		out.ProxyPool.RemoteToken = old.ProxyPool.RemoteToken
	}
	if out.Security.ConfigKey == MaskPlaceholder {
		out.Security.ConfigKey = old.Security.ConfigKey
	}
	return out, nil
}

// SanitizeMaskLeftovers 清理历史遗留的 "***" 字面量。
// "***" 不可能是真实凭据；它落库只可能来自早期版本的还原缺陷。留着会让界面显示
// 「已设置」而签到静默失败，因此清空并返回被清理的字段路径，供启动迁移记日志。
func SanitizeMaskLeftovers(cfg *Config) []string {
	if cfg == nil {
		return nil
	}
	var cleaned []string
	for i := range cfg.Accounts {
		if cfg.Accounts[i].Cookie == MaskPlaceholder {
			cfg.Accounts[i].Cookie = ""
			cleaned = append(cleaned, fmt.Sprintf("accounts[%d].cookie", i))
		}
		if cfg.Accounts[i].GithubUserSession == MaskPlaceholder {
			cfg.Accounts[i].GithubUserSession = ""
			cleaned = append(cleaned, fmt.Sprintf("accounts[%d].github_user_session", i))
		}
	}
	if cfg.AI.APIKey == MaskPlaceholder {
		cfg.AI.APIKey = ""
		cleaned = append(cleaned, "ai.api_key")
	}
	if cfg.Notify.Email.Password == MaskPlaceholder {
		cfg.Notify.Email.Password = ""
		cleaned = append(cleaned, "notify.email.password")
	}
	if cfg.ConfigSync.Token == MaskPlaceholder {
		cfg.ConfigSync.Token = ""
		cleaned = append(cleaned, "config_sync.token")
	}
	if cfg.ProxyPool.RemoteToken == MaskPlaceholder {
		cfg.ProxyPool.RemoteToken = ""
		cleaned = append(cleaned, "proxy_pool.remote_token")
	}
	if cfg.Security.ConfigKey == MaskPlaceholder {
		cfg.Security.ConfigKey = ""
		cleaned = append(cleaned, "security.config_key")
	}
	return cleaned
}

// cloneConfig 深拷贝配置，避免打码/还原逻辑修改到传入对象的共享内存。
// Body 为 any 类型仅做引用拷贝（本服务从不原地修改其内容，读取安全）。
func cloneConfig(c *Config) *Config {
	if c == nil {
		return nil
	}
	cp := *c

	cp.Accounts = make([]Account, len(c.Accounts))
	for i, a := range c.Accounts {
		cp.Accounts[i] = a
		if a.UserID != nil {
			v := *a.UserID
			cp.Accounts[i].UserID = &v
		}
		if a.Proxy != nil {
			v := *a.Proxy
			cp.Accounts[i].Proxy = &v
		}
		if a.CheckinPath != nil {
			v := *a.CheckinPath
			cp.Accounts[i].CheckinPath = &v
		}
		if a.BrowserPath != nil {
			v := *a.BrowserPath
			cp.Accounts[i].BrowserPath = &v
		}
	}

	cp.Sites = make([]Site, len(c.Sites))
	for i, s := range c.Sites {
		cp.Sites[i] = s
		if s.CheckinPath != nil {
			v := *s.CheckinPath
			cp.Sites[i].CheckinPath = &v
		}
		if s.BrowserPath != nil {
			v := *s.BrowserPath
			cp.Sites[i].BrowserPath = &v
		}
	}

	cp.Browser.Window = make([]int, len(c.Browser.Window))
	copy(cp.Browser.Window, c.Browser.Window)

	if c.Browser.ExecutablePath != nil {
		v := *c.Browser.ExecutablePath
		cp.Browser.ExecutablePath = &v
	}

	cp.ProxyPool.Sources = make([]string, len(c.ProxyPool.Sources))
	copy(cp.ProxyPool.Sources, c.ProxyPool.Sources)

	cp.Notify.Email.ToAddrs = make([]string, len(c.Notify.Email.ToAddrs))
	copy(cp.Notify.Email.ToAddrs, c.Notify.Email.ToAddrs)

	if c.ConfigSync.Headers != nil {
		cp.ConfigSync.Headers = make(map[string]string, len(c.ConfigSync.Headers))
		for k, v := range c.ConfigSync.Headers {
			cp.ConfigSync.Headers[k] = v
		}
	}
	cp.ConfigSync.Body = c.ConfigSync.Body

	return &cp
}

// ValidateConfig 校验配置合法性（契约规则）：
// - accounts 每个账号必须提供 name / url；cookie 可为空
// - url 必须以 http:// 或 https:// 开头
// - sites 每个站点必须提供 name / url
// - accounts / sites 的 name 不可重复（name 是敏感字段还原与合并的匹配键）
// 返回第一个错误信息（供 400 响应使用）。
func ValidateConfig(cfg *Config) error {
	if cfg == nil {
		return fmt.Errorf("config 不能为空")
	}
	accountNames := make(map[string]int, len(cfg.Accounts))
	for i, a := range cfg.Accounts {
		if strings.TrimSpace(a.Name) == "" {
			return fmt.Errorf("accounts[%d].name 不能为空", i)
		}
		if strings.TrimSpace(a.URL) == "" {
			return fmt.Errorf("accounts[%d].url 不能为空", i)
		}
		lowerURL := strings.ToLower(strings.TrimSpace(a.URL))
		if !strings.HasPrefix(lowerURL, "http://") && !strings.HasPrefix(lowerURL, "https://") {
			return fmt.Errorf("accounts[%d].url 必须以 http:// 或 https:// 开头", i)
		}
		method := strings.ToLower(strings.TrimSpace(a.LoginMethod))
		if method == "" {
			method = LoginMethodNewAPICookie
			cfg.Accounts[i].LoginMethod = method
		}
		if method != LoginMethodNewAPICookie && method != LoginMethodTabiAI {
			return fmt.Errorf("accounts[%d].login_method 只能是 %s 或 %s", i,
				LoginMethodNewAPICookie, LoginMethodTabiAI)
		}
		if prev, dup := accountNames[a.Name]; dup {
			return fmt.Errorf("accounts[%d].name 与 accounts[%d] 重复：%q（账号名用于匹配已保存的 Cookie，必须唯一）",
				i, prev, a.Name)
		}
		accountNames[a.Name] = i
	}
	siteNames := make(map[string]int, len(cfg.Sites))
	for i, s := range cfg.Sites {
		if strings.TrimSpace(s.Name) == "" {
			return fmt.Errorf("sites[%d].name 不能为空", i)
		}
		if strings.TrimSpace(s.URL) == "" {
			return fmt.Errorf("sites[%d].url 不能为空", i)
		}
		lowerURL := strings.ToLower(strings.TrimSpace(s.URL))
		if !strings.HasPrefix(lowerURL, "http://") && !strings.HasPrefix(lowerURL, "https://") {
			return fmt.Errorf("sites[%d].url 必须以 http:// 或 https:// 开头", i)
		}
		if prev, dup := siteNames[s.Name]; dup {
			return fmt.Errorf("sites[%d].name 与 sites[%d] 重复：%q（站点名用于导入合并，必须唯一）",
				i, prev, s.Name)
		}
		siteNames[s.Name] = i
	}
	return nil
}
