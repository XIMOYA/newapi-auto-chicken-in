/*
server/config.go
NewAPI 签到配置管理平台 · 配置对象模型

职责：
- 定义与接口契约一致的完整配置结构体（accounts / ai / browser / http / defaults / proxy_pool / notify / config_sync / security）
- 提供默认配置（契约文档「配置对象默认结构」）
- 敏感字段打码 / 还原：accounts[].cookie、ai.api_key、notify.email.password、config_sync.token
- 配置校验：accounts 必填 name/url/cookie，url 需 http(s) 开头
*/
package main

import (
	"fmt"
	"strings"
)

// MaskPlaceholder 敏感字段占位符：前端展示打码值，后端识别为「未修改，保留原值」。
const MaskPlaceholder = "***"

// currentConfigVersion 用于一次性迁移旧默认值。旧配置没有该字段，视为 0。
const currentConfigVersion = 1

// Config 完整配置对象 = 契约文档中的顶层结构。
type Config struct {
	ConfigVersion int           `json:"config_version"`
	Accounts      []Account     `json:"accounts"`
	Sites         []Site        `json:"sites"`
	AI            AIConfig      `json:"ai"`
	Browser       BrowserConfig `json:"browser"`
	HTTP          HTTPConfig    `json:"http"`
	Defaults      Defaults      `json:"defaults"`
	ProxyPool     ProxyPool     `json:"proxy_pool"`
	Notify        Notify        `json:"notify"`
	ConfigSync    ConfigSync    `json:"config_sync"`
	Security      Security      `json:"security"`
}

// Site 站点预设：供新增账号时快速选择，自动带出 URL 与接口路径。
type Site struct {
	Name        string  `json:"name"`
	URL         string  `json:"url"`
	CheckinPath *string `json:"checkin_path"`
	BrowserPath *string `json:"browser_path"`
}

// Account 签到账号：name/url/cookie 为必填（契约校验要求），其余为可选。
type Account struct {
	Name        string  `json:"name"`
	URL         string  `json:"url"`
	Cookie      string  `json:"cookie"`
	UserID      *int64  `json:"user_id"`
	Proxy       *string `json:"proxy"`
	CheckinPath *string `json:"checkin_path"`
	BrowserPath *string `json:"browser_path"`
	Enabled     bool    `json:"enabled"`
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

// Defaults 全局默认值。
type Defaults struct {
	Retry           int   `json:"retry"`
	IntervalSeconds []int `json:"interval_seconds"`
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
		Defaults: Defaults{
			Retry:           2,
			IntervalSeconds: []int{3, 8},
		},
		ProxyPool: ProxyPool{
			Enabled:           false,
			TestURL:           "https://api.ipify.org",
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
// 打码字段：accounts[].cookie、ai.api_key、notify.email.password、config_sync.token；
// 非空才打码，空值原样保留；proxy_pool.sources 等非敏感字段正常返回。
func MaskConfig(cfg *Config) *Config {
	m := cloneConfig(cfg)
	for i := range m.Accounts {
		if m.Accounts[i].Cookie != "" {
			m.Accounts[i].Cookie = MaskPlaceholder
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
	return m
}

// UnmaskConfig 把输入配置中的 "***" 占位符还原为库中旧值（深合并）。
// 规则：仅敏感字段值为 "***" 时保留旧值；其余字段一律以输入为准（含清空、新增、删除账号）。
func UnmaskConfig(in, old *Config) *Config {
	out := cloneConfig(in)
	for i := range out.Accounts {
		if out.Accounts[i].Cookie == MaskPlaceholder {
			if i < len(old.Accounts) {
				out.Accounts[i].Cookie = old.Accounts[i].Cookie
			}
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
	return out
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

	cp.Defaults.IntervalSeconds = make([]int, len(c.Defaults.IntervalSeconds))
	copy(cp.Defaults.IntervalSeconds, c.Defaults.IntervalSeconds)

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
// - accounts 每个账号必须提供 name / url / cookie
// - url 必须以 http:// 或 https:// 开头
// - sites 每个站点必须提供 name / url
// 返回第一个错误信息（供 400 响应使用）。
func ValidateConfig(cfg *Config) error {
	if cfg == nil {
		return fmt.Errorf("config 不能为空")
	}
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
		if strings.TrimSpace(a.Cookie) == "" {
			return fmt.Errorf("accounts[%d].cookie 不能为空", i)
		}
	}
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
	}
	return nil
}
