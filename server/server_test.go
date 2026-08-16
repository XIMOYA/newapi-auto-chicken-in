/*
server/server_test.go
NewAPI 签到配置管理平台 · 单元与接口测试

覆盖范围：
- config.go：默认结构、敏感字段打码/还原、配置校验
- auth.go ：JWT 签发/解析、API Key 生成与哈希、bcrypt 密码校验
- handlers.go：health / login / config(读写+打码+raw) / keys / export / password 全流程接口测试

说明：DB 一律使用 t.TempDir() 下的临时 SQLite 文件，互不干扰。
*/
package main

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// ---------------------------------------------------------------------------
// 测试辅助
// ---------------------------------------------------------------------------

// newTestServer 构造一个带临时数据库的测试 Server，并预置管理员与默认配置。
func newTestServer(t *testing.T) *Server {
	t.Helper()
	db, err := OpenDB(filepath.Join(t.TempDir(), "test.db"))
	if err != nil {
		t.Fatalf("OpenDB: %v", err)
	}
	t.Cleanup(func() { _ = db.Close() })
	if err := EnsureAdmin(db, "admin", "admin123456"); err != nil {
		t.Fatalf("EnsureAdmin: %v", err)
	}
	if err := EnsureDefaultConfig(db); err != nil {
		t.Fatalf("EnsureDefaultConfig: %v", err)
	}
	return NewServer(db, strings.Repeat("s", 32))
}

// doReq 构造并执行一次 HTTP 请求，返回响应记录器。
func doReq(t *testing.T, srv *Server, method, path, token string, body any) *httptest.ResponseRecorder {
	t.Helper()
	var rd io.Reader
	if body != nil {
		b, err := json.Marshal(body)
		if err != nil {
			t.Fatalf("marshal body: %v", err)
		}
		rd = bytes.NewReader(b)
	}
	req := httptest.NewRequest(method, path, rd)
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	rr := httptest.NewRecorder()
	srv.routes().ServeHTTP(rr, req)
	return rr
}

// loginToken 登录并返回 JWT。
func loginToken(t *testing.T, srv *Server) string {
	t.Helper()
	rr := doReq(t, srv, http.MethodPost, "/api/login", "", map[string]string{
		"username": "admin",
		"password": "admin123456",
	})
	if rr.Code != http.StatusOK {
		t.Fatalf("login status = %d, body = %s", rr.Code, rr.Body.String())
	}
	var resp struct {
		Token string `json:"token"`
	}
	decodeJSON(t, rr, &resp)
	if resp.Token == "" {
		t.Fatal("login 返回空 token")
	}
	return resp.Token
}

// decodeJSON 解析响应 JSON 到 dst，失败即终止测试。
func decodeJSON(t *testing.T, rr *httptest.ResponseRecorder, dst any) {
	t.Helper()
	if err := json.Unmarshal(rr.Body.Bytes(), dst); err != nil {
		t.Fatalf("解析响应 JSON 失败: %v, body = %s", err, rr.Body.String())
	}
}

// putTestConfig 以默认结构 + 两个账号提交一次配置（helper，校验成功路径）。
func putTestConfig(t *testing.T, srv *Server, token string) {
	t.Helper()
	cfg := DefaultConfig()
	cfg.Accounts = []Account{
		{Name: "站点A", URL: "https://a.example.com", Cookie: "cookie-1", Enabled: true},
		{Name: "站点B", URL: "http://b.example.com", Cookie: "cookie-2"},
	}
	cfg.AI.APIKey = "sk-abc123"
	cfg.ConfigSync.Token = "sync-token"
	cfg.Notify.Email.Password = "mail-pass"
	rr := doReq(t, srv, http.MethodPut, "/api/config", token, map[string]any{"config": cfg})
	if rr.Code != http.StatusOK {
		t.Fatalf("PUT config status = %d, body = %s", rr.Code, rr.Body.String())
	}
}

// ---------------------------------------------------------------------------
// config.go：默认结构
// ---------------------------------------------------------------------------

func TestDefaultConfig(t *testing.T) {
	cfg := DefaultConfig()
	if cfg.Accounts == nil || len(cfg.Accounts) != 0 {
		t.Errorf("Accounts 应为空 slice，得到 %#v", cfg.Accounts)
	}
	if cfg.AI.Model != "gpt-4o-mini" || cfg.AI.Timeout != 60 || cfg.AI.MaxRetries != 2 {
		t.Errorf("AI 默认值不符合契约: %+v", cfg.AI)
	}
	if len(cfg.Browser.Window) != 2 || cfg.Browser.Window[0] != 1280 || cfg.Browser.Window[1] != 800 {
		t.Errorf("Browser.Window 默认值不符合契约: %v", cfg.Browser.Window)
	}
	if cfg.Browser.ExecutablePath != nil {
		t.Errorf("Browser.ExecutablePath 应为 null，得到 %v", *cfg.Browser.ExecutablePath)
	}
	if !cfg.HTTP.Verify {
		t.Error("HTTP.Verify 默认应为 true")
	}
	if cfg.ProxyPool.Sources == nil || len(cfg.ProxyPool.Sources) != 0 {
		t.Errorf("ProxyPool.Sources 应为空 slice，得到 %#v", cfg.ProxyPool.Sources)
	}
	if cfg.ProxyPool.TestURL != "https://api.ipify.org" {
		t.Errorf("ProxyPool.TestURL 默认值不符合契约: %q", cfg.ProxyPool.TestURL)
	}
	if cfg.ProxyPool.SaveLimit != 0 {
		t.Errorf("ProxyPool.SaveLimit 默认应为 0（不限量），得到 %d", cfg.ProxyPool.SaveLimit)
	}
	if cfg.ProxyPool.IPSwapLimit != 10 {
		t.Errorf("ProxyPool.IPSwapLimit 默认应为 10，得到 %d", cfg.ProxyPool.IPSwapLimit)
	}
	if cfg.ConfigVersion != currentConfigVersion {
		t.Errorf("ConfigVersion 默认应为 %d，得到 %d", currentConfigVersion, cfg.ConfigVersion)
	}
	if cfg.Notify.Email.SubjectPrefix != "NewAPI 签到日报" || cfg.Notify.Email.SMTPHost != "smtp.aliyun.com" {
		t.Errorf("Notify.Email 默认值不符合契约: %+v", cfg.Notify.Email)
	}
	if cfg.ConfigSync.TokenHeader != "Authorization" || cfg.ConfigSync.TokenPrefix != "Bearer" {
		t.Errorf("ConfigSync 默认值不符合契约: %+v", cfg.ConfigSync)
	}
	if !cfg.ConfigSync.AutoBeforeCheckin {
		t.Error("ConfigSync.AutoBeforeCheckin 默认应为 true")
	}
	if cfg.Security.EncryptedFile != "data/config.encrypted.json" {
		t.Errorf("Security.EncryptedFile 默认值不符合契约: %q", cfg.Security.EncryptedFile)
	}
}

// ---------------------------------------------------------------------------
// config.go：打码 / 还原
// ---------------------------------------------------------------------------

func TestMaskConfig(t *testing.T) {
	cfg := DefaultConfig()
	cfg.Accounts = []Account{{
		Name: "A", URL: "https://a.com", LoginMethod: LoginMethodGitHubCookie,
		Cookie: "secret-cookie", GithubUserSession: "github-secret", GithubClientID: "client-id",
	}}
	cfg.AI.APIKey = "sk-x"
	cfg.Notify.Email.Password = "pw"
	cfg.ConfigSync.Token = "tok"
	cfg.ProxyPool.Sources = []string{"http://p1"}

	m := MaskConfig(&cfg)

	if m.Accounts[0].Cookie != MaskPlaceholder {
		t.Errorf("cookie 未打码: %q", m.Accounts[0].Cookie)
	}
	if m.Accounts[0].GithubUserSession != MaskPlaceholder {
		t.Errorf("github_user_session 未打码: %q", m.Accounts[0].GithubUserSession)
	}
	if m.Accounts[0].GithubClientID != "client-id" || m.Accounts[0].LoginMethod != LoginMethodGitHubCookie {
		t.Errorf("GitHub 非敏感字段被改动: %+v", m.Accounts[0])
	}
	if m.AI.APIKey != MaskPlaceholder {
		t.Errorf("ai.api_key 未打码: %q", m.AI.APIKey)
	}
	if m.Notify.Email.Password != MaskPlaceholder {
		t.Errorf("notify.email.password 未打码: %q", m.Notify.Email.Password)
	}
	if m.ConfigSync.Token != MaskPlaceholder {
		t.Errorf("config_sync.token 未打码: %q", m.ConfigSync.Token)
	}
	if len(m.ProxyPool.Sources) != 1 || m.ProxyPool.Sources[0] != "http://p1" {
		t.Errorf("proxy_pool.sources 不应被打码: %v", m.ProxyPool.Sources)
	}
	if m.Accounts[0].Name != "A" || m.Accounts[0].URL != "https://a.com" {
		t.Errorf("非敏感字段被改动: %+v", m.Accounts[0])
	}
	// 深拷贝：原对象不受影响
	if cfg.Accounts[0].Cookie != "secret-cookie" || cfg.AI.APIKey != "sk-x" {
		t.Error("MaskConfig 修改了原配置对象（非深拷贝）")
	}
}

func TestMaskConfigEmptyStaysEmpty(t *testing.T) {
	cfg := DefaultConfig() // 敏感字段均为空
	m := MaskConfig(&cfg)
	if m.Accounts != nil && len(m.Accounts) != 0 {
		t.Errorf("空 accounts 不应打码出占位账号")
	}
	if m.AI.APIKey != "" {
		t.Errorf("空的 ai.api_key 不应被打码: %q", m.AI.APIKey)
	}
	if m.ConfigSync.Token != "" {
		t.Errorf("空的 config_sync.token 不应被打码: %q", m.ConfigSync.Token)
	}
}

func TestUnmaskConfig(t *testing.T) {
	old := DefaultConfig()
	old.Accounts = []Account{{
		Name: "A", URL: "https://a.com", LoginMethod: LoginMethodGitHubCookie,
		Cookie: "old-cookie", GithubUserSession: "old-github-session", GithubClientID: "old-client-id",
	}}
	old.AI.APIKey = "old-key"
	old.Notify.Email.Password = "old-pass"
	old.ConfigSync.Token = "old-token"

	in := DefaultConfig()
	in.Accounts = []Account{
		{
			Name: "A", URL: "https://a.com", LoginMethod: LoginMethodGitHubCookie,
			Cookie: MaskPlaceholder, GithubUserSession: MaskPlaceholder,
		}, // "***" → 按账号名分别还原两类 Cookie
		{Name: "B", URL: "https://b.com", Cookie: "new-cookie"}, // 新账号，非占位符
	}
	in.AI.APIKey = MaskPlaceholder        // 还原
	in.Notify.Email.Password = "new-pass" // 非占位符，保留新值
	in.ConfigSync.Token = MaskPlaceholder // 还原

	out := UnmaskConfig(&in, &old)

	if out.Accounts[0].Cookie != "old-cookie" {
		t.Errorf("占位符 cookie 未还原: %q", out.Accounts[0].Cookie)
	}
	if out.Accounts[0].GithubUserSession != "old-github-session" {
		t.Errorf("占位符 github_user_session 未还原: %q", out.Accounts[0].GithubUserSession)
	}
	if out.Accounts[0].Name != "A" {
		t.Errorf("非敏感字段应以输入为准: %q", out.Accounts[0].Name)
	}
	if out.Accounts[1].Cookie != "new-cookie" {
		t.Errorf("新账号 cookie 不应被还原: %q", out.Accounts[1].Cookie)
	}
	if out.AI.APIKey != "old-key" {
		t.Errorf("占位符 api_key 未还原: %q", out.AI.APIKey)
	}
	if out.Notify.Email.Password != "new-pass" {
		t.Errorf("非占位符 password 应保留新值: %q", out.Notify.Email.Password)
	}
	if out.ConfigSync.Token != "old-token" {
		t.Errorf("占位符 config_sync.token 未还原: %q", out.ConfigSync.Token)
	}
	// 深拷贝：输入对象不受影响
	if in.Accounts[0].Cookie != MaskPlaceholder || in.Accounts[0].GithubUserSession != MaskPlaceholder {
		t.Error("UnmaskConfig 修改了输入配置对象（非深拷贝）")
	}
}

func TestUnmaskConfigAccountIndexOutOfRange(t *testing.T) {
	old := DefaultConfig()
	old.Accounts = []Account{{Name: "A", URL: "https://a.com", Cookie: "old-cookie"}}
	in := DefaultConfig()
	in.Accounts = []Account{
		{Name: "A", URL: "https://a.com", Cookie: MaskPlaceholder},
		{Name: "B", URL: "https://b.com", Cookie: MaskPlaceholder}, // 旧配置无此索引
	}
	out := UnmaskConfig(&in, &old)
	if out.Accounts[0].Cookie != "old-cookie" {
		t.Errorf("占位符 cookie 未还原: %q", out.Accounts[0].Cookie)
	}
	if out.Accounts[1].Cookie != MaskPlaceholder {
		t.Errorf("索引越界时占位符应保持原样，得到 %q", out.Accounts[1].Cookie)
	}
}

// ---------------------------------------------------------------------------
// config.go：校验
// ---------------------------------------------------------------------------

func TestAccountLoginMethodDefaultsAndValidation(t *testing.T) {
	cfg := DefaultConfig()
	cfg.Accounts = []Account{{Name: "NewAPI", URL: "https://a.com"}}
	if err := ValidateConfig(&cfg); err != nil {
		t.Fatalf("缺省登录方式不应报错: %v", err)
	}
	if cfg.Accounts[0].LoginMethod != LoginMethodNewAPICookie {
		t.Fatalf("缺省登录方式 = %q, want %q", cfg.Accounts[0].LoginMethod, LoginMethodNewAPICookie)
	}

	cfg.Accounts[0].LoginMethod = LoginMethodGitHubCookie
	cfg.Accounts[0].GithubUserSession = "session"
	if err := ValidateConfig(&cfg); err != nil {
		t.Fatalf("GitHub 登录方式不应报错: %v", err)
	}

	cfg.Accounts[0].LoginMethod = "unknown"
	if err := ValidateConfig(&cfg); err == nil || !strings.Contains(err.Error(), "login_method") {
		t.Fatalf("未知登录方式应被拒绝: %v", err)
	}
}

func TestValidateConfig(t *testing.T) {
	valid := DefaultConfig()
	valid.Accounts = []Account{{Name: "A", URL: "https://a.com", Cookie: "c"}}
	if err := ValidateConfig(&valid); err != nil {
		t.Errorf("合法配置不应报错: %v", err)
	}

	cases := []struct {
		name   string
		mutate func(*Config)
		want   string // 错误信息应包含的子串
	}{
		{"缺 name", func(c *Config) { c.Accounts[0].Name = "  " }, "name"},
		{"缺 url", func(c *Config) { c.Accounts[0].URL = "" }, "url"},
		{"url 非 http", func(c *Config) { c.Accounts[0].URL = "ftp://x.com" }, "http"},
		{"url 大写 http", func(c *Config) { c.Accounts[0].URL = "HTTPS://X.COM" }, ""}, // 大小写宽容
		{"空 cookie 合法", func(c *Config) { c.Accounts[0].Cookie = "" }, ""},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			cfg := DefaultConfig()
			cfg.Accounts = []Account{{Name: "A", URL: "https://a.com", Cookie: "c"}}
			tc.mutate(&cfg)
			err := ValidateConfig(&cfg)
			if tc.want == "" {
				if err != nil {
					t.Errorf("不应报错: %v", err)
				}
				return
			}
			if err == nil {
				t.Fatalf("应报错但通过了")
			}
			if !strings.Contains(err.Error(), tc.want) {
				t.Errorf("错误信息 %q 应包含 %q", err.Error(), tc.want)
			}
		})
	}
}

func TestPutConfigAllowsEmptyCookie(t *testing.T) {
	srv := newTestServer(t)
	token := loginToken(t, srv)
	cfg := DefaultConfig()
	cfg.Accounts = []Account{{
		Name:    "待补 Cookie 的账号",
		URL:     "https://example.com",
		Cookie:  "",
		Enabled: true,
	}}

	rr := doReq(t, srv, http.MethodPut, "/api/config", token, map[string]any{"config": cfg})
	if rr.Code != http.StatusOK {
		t.Fatalf("PUT config with empty cookie status = %d, body = %s", rr.Code, rr.Body.String())
	}

	saved, _, err := LoadConfig(srv.db)
	if err != nil {
		t.Fatalf("LoadConfig: %v", err)
	}
	if len(saved.Accounts) != 1 || saved.Accounts[0].Cookie != "" {
		t.Fatalf("空 Cookie 账号未按原样保存: %+v", saved.Accounts)
	}
}

// ---------------------------------------------------------------------------
// auth.go：JWT
// ---------------------------------------------------------------------------

func TestJWT(t *testing.T) {
	secret := []byte(strings.Repeat("k", 32))
	token, expiresIn, err := SignToken("admin", secret)
	if err != nil {
		t.Fatalf("SignToken: %v", err)
	}
	if expiresIn != int64(tokenTTL.Seconds()) {
		t.Errorf("expires_in = %d, 应为 %d", expiresIn, int64(tokenTTL.Seconds()))
	}
	username, err := ParseToken(token, secret)
	if err != nil {
		t.Fatalf("ParseToken: %v", err)
	}
	if username != "admin" {
		t.Errorf("解析出用户名 %q，应为 admin", username)
	}

	// 错误密钥解析失败
	if _, err := ParseToken(token, []byte(strings.Repeat("x", 32))); err == nil {
		t.Error("错误密钥解析应失败")
	}
	// 篡改 token 解析失败
	if _, err := ParseToken(token[:len(token)-4]+"AAAA", secret); err == nil {
		t.Error("篡改 token 应解析失败")
	}
	// 过期 token 解析失败
	expired, _, err := signTokenWithTTL("admin", secret, -time.Hour)
	if err != nil {
		t.Fatalf("signTokenWithTTL: %v", err)
	}
	if _, err := ParseToken(expired, secret); err == nil {
		t.Error("过期 token 应解析失败")
	}
}

// ---------------------------------------------------------------------------
// auth.go：API Key
// ---------------------------------------------------------------------------

func TestGenerateAPIKey(t *testing.T) {
	plain, hash, prefix, err := GenerateAPIKey()
	if err != nil {
		t.Fatalf("GenerateAPIKey: %v", err)
	}
	if len(plain) != 36 {
		t.Errorf("key 长度 = %d，应为 36（ncf_ + 32 hex）", len(plain))
	}
	if !strings.HasPrefix(plain, APIKeyPrefix) {
		t.Errorf("key 应以 %q 开头: %q", APIKeyPrefix, plain)
	}
	if prefix != plain[:8] {
		t.Errorf("prefix = %q, 应为 %q", prefix, plain[:8])
	}
	if hash != HashAPIKey(plain) {
		t.Error("返回哈希与重新计算不一致")
	}
	if HashAPIKey(plain) == HashAPIKey(plain+"x") {
		t.Error("不同 key 不应碰撞")
	}
	plain2, _, _, err := GenerateAPIKey()
	if err != nil {
		t.Fatalf("GenerateAPIKey #2: %v", err)
	}
	if plain == plain2 {
		t.Error("两次生成的 key 不应相同")
	}
}

// ---------------------------------------------------------------------------
// handlers.go：健康检查 / 登录
// ---------------------------------------------------------------------------

func TestHealth(t *testing.T) {
	srv := newTestServer(t)
	rr := doReq(t, srv, http.MethodGet, "/api/health", "", nil)
	if rr.Code != http.StatusOK {
		t.Fatalf("status = %d", rr.Code)
	}
	var resp struct {
		OK      bool   `json:"ok"`
		Version string `json:"version"`
		Time    string `json:"time"`
	}
	decodeJSON(t, rr, &resp)
	if !resp.OK || resp.Version != serverVersion || resp.Time == "" {
		t.Errorf("响应不符合契约: %+v", resp)
	}
}

func TestLogin(t *testing.T) {
	srv := newTestServer(t)

	// 成功
	rr := doReq(t, srv, http.MethodPost, "/api/login", "", map[string]string{
		"username": "admin", "password": "admin123456",
	})
	if rr.Code != http.StatusOK {
		t.Fatalf("status = %d, body = %s", rr.Code, rr.Body.String())
	}
	var resp struct {
		Token     string `json:"token"`
		Username  string `json:"username"`
		ExpiresIn int64  `json:"expires_in"`
	}
	decodeJSON(t, rr, &resp)
	if resp.Token == "" || resp.Username != "admin" || resp.ExpiresIn != 604800 {
		t.Errorf("响应不符合契约: %+v", resp)
	}

	// 密码错误
	rr = doReq(t, srv, http.MethodPost, "/api/login", "", map[string]string{
		"username": "admin", "password": "wrong",
	})
	if rr.Code != http.StatusUnauthorized {
		t.Fatalf("错误密码 status = %d, body = %s", rr.Code, rr.Body.String())
	}
	var e struct {
		Error string `json:"error"`
	}
	decodeJSON(t, rr, &e)
	if e.Error != "用户名或密码错误" {
		t.Errorf("错误信息 = %q", e.Error)
	}

	// 缺少参数
	rr = doReq(t, srv, http.MethodPost, "/api/login", "", map[string]string{
		"username": "", "password": "",
	})
	if rr.Code != http.StatusBadRequest {
		t.Fatalf("缺参 status = %d", rr.Code)
	}
}

// ---------------------------------------------------------------------------
// handlers.go：配置读写（打码 / 还原 / 校验 / raw）
// ---------------------------------------------------------------------------

func TestConfigCRUDFlow(t *testing.T) {
	srv := newTestServer(t)
	token := loginToken(t, srv)

	// 初始：默认配置（打码后敏感字段为空）
	rr := doReq(t, srv, http.MethodGet, "/api/config", token, nil)
	if rr.Code != http.StatusOK {
		t.Fatalf("GET config status = %d", rr.Code)
	}
	var initial struct {
		Config    Config `json:"config"`
		UpdatedAt string `json:"updated_at"`
	}
	decodeJSON(t, rr, &initial)
	if initial.UpdatedAt == "" {
		t.Error("updated_at 不应为空")
	}

	// 保存带敏感信息的配置
	putTestConfig(t, srv, token)

	// 管理端读取：敏感字段打码
	rr = doReq(t, srv, http.MethodGet, "/api/config", token, nil)
	if rr.Code != http.StatusOK {
		t.Fatalf("GET config status = %d", rr.Code)
	}
	var got struct {
		Config Config `json:"config"`
	}
	decodeJSON(t, rr, &got)
	if len(got.Config.Accounts) != 2 {
		t.Fatalf("accounts 数量 = %d，应为 2", len(got.Config.Accounts))
	}
	if got.Config.Accounts[0].Cookie != MaskPlaceholder {
		t.Errorf("cookie 未打码: %q", got.Config.Accounts[0].Cookie)
	}
	if got.Config.AI.APIKey != MaskPlaceholder {
		t.Errorf("ai.api_key 未打码: %q", got.Config.AI.APIKey)
	}
	if got.Config.Notify.Email.Password != MaskPlaceholder {
		t.Errorf("notify.email.password 未打码: %q", got.Config.Notify.Email.Password)
	}
	if got.Config.ConfigSync.Token != MaskPlaceholder {
		t.Errorf("config_sync.token 未打码: %q", got.Config.ConfigSync.Token)
	}
	if got.Config.Accounts[0].Name != "站点A" {
		t.Errorf("非敏感字段被改动: %q", got.Config.Accounts[0].Name)
	}

	// 创建 API Key 并拉取明文
	rr = doReq(t, srv, http.MethodPost, "/api/keys", token, map[string]string{"name": "actions"})
	if rr.Code != http.StatusOK {
		t.Fatalf("POST keys status = %d, body = %s", rr.Code, rr.Body.String())
	}
	var created struct {
		ID   int64  `json:"id"`
		Key  string `json:"key"`
		Name string `json:"name"`
	}
	decodeJSON(t, rr, &created)
	if !strings.HasPrefix(created.Key, "ncf_") {
		t.Fatalf("创建返回的 key 格式异常: %q", created.Key)
	}

	rr = doReq(t, srv, http.MethodGet, "/api/config/raw", created.Key, nil)
	if rr.Code != http.StatusOK {
		t.Fatalf("raw status = %d, body = %s", rr.Code, rr.Body.String())
	}
	var raw Config
	decodeJSON(t, rr, &raw)
	if len(raw.Accounts) != 2 || raw.Accounts[0].Cookie != "cookie-1" {
		t.Errorf("raw 未返回明文 cookie: %+v", raw.Accounts)
	}
	if raw.AI.APIKey != "sk-abc123" || raw.ConfigSync.Token != "sync-token" {
		t.Errorf("raw 未返回完整明文: api_key=%q token=%q", raw.AI.APIKey, raw.ConfigSync.Token)
	}

	// 用 "***" 占位 + 新值混合提交：cookie 还原旧值、api_key 采用新值
	cfg := DefaultConfig()
	cfg.Accounts = []Account{
		{Name: "站点A", URL: "https://a.example.com", Cookie: MaskPlaceholder},
		{Name: "站点B", URL: "http://b.example.com", Cookie: MaskPlaceholder},
	}
	cfg.AI.APIKey = "sk-new-key"
	cfg.ConfigSync.Token = "sync-token"
	cfg.Notify.Email.Password = "mail-pass"
	rr = doReq(t, srv, http.MethodPut, "/api/config", token, map[string]any{"config": cfg})
	if rr.Code != http.StatusOK {
		t.Fatalf("PUT config(占位) status = %d, body = %s", rr.Code, rr.Body.String())
	}

	rr = doReq(t, srv, http.MethodGet, "/api/config/raw", created.Key, nil)
	if rr.Code != http.StatusOK {
		t.Fatalf("raw #2 status = %d", rr.Code)
	}
	var raw2 Config
	decodeJSON(t, rr, &raw2)
	if raw2.Accounts[0].Cookie != "cookie-1" || raw2.Accounts[1].Cookie != "cookie-2" {
		t.Errorf("占位符 cookie 未被还原为旧值: %+v", raw2.Accounts)
	}
	if raw2.AI.APIKey != "sk-new-key" {
		t.Errorf("新 api_key 未生效: %q", raw2.AI.APIKey)
	}

	// 校验失败：url 非 http(s)
	bad := DefaultConfig()
	bad.Accounts = []Account{{Name: "X", URL: "ftp://x.com", Cookie: "c"}}
	rr = doReq(t, srv, http.MethodPut, "/api/config", token, map[string]any{"config": bad})
	if rr.Code != http.StatusBadRequest {
		t.Fatalf("非法 url status = %d, body = %s", rr.Code, rr.Body.String())
	}
	var badErr struct {
		Error string `json:"error"`
	}
	decodeJSON(t, rr, &badErr)
	if !strings.Contains(badErr.Error, "http") {
		t.Errorf("校验错误信息 = %q", badErr.Error)
	}

	// config 缺失
	rr = doReq(t, srv, http.MethodPut, "/api/config", token, map[string]any{})
	if rr.Code != http.StatusBadRequest {
		t.Fatalf("缺失 config status = %d", rr.Code)
	}
}

// ---------------------------------------------------------------------------
// handlers.go：API Key 管理
// ---------------------------------------------------------------------------

func TestKeysCRUDFlow(t *testing.T) {
	srv := newTestServer(t)
	token := loginToken(t, srv)

	// 创建
	rr := doReq(t, srv, http.MethodPost, "/api/keys", token, map[string]string{"name": "github-actions"})
	if rr.Code != http.StatusOK {
		t.Fatalf("POST keys status = %d, body = %s", rr.Code, rr.Body.String())
	}
	var created struct {
		ID   int64  `json:"id"`
		Name string `json:"name"`
		Key  string `json:"key"`
	}
	decodeJSON(t, rr, &created)
	if created.ID <= 0 || created.Name != "github-actions" || created.Key == "" {
		t.Errorf("创建响应不符合契约: %+v", created)
	}

	// 列表：只含前缀
	rr = doReq(t, srv, http.MethodGet, "/api/keys", token, nil)
	if rr.Code != http.StatusOK {
		t.Fatalf("GET keys status = %d", rr.Code)
	}
	var list struct {
		Keys []struct {
			ID         int64   `json:"id"`
			Name       string  `json:"name"`
			Prefix     string  `json:"prefix"`
			CreatedAt  string  `json:"created_at"`
			LastUsedAt *string `json:"last_used_at"`
		} `json:"keys"`
	}
	decodeJSON(t, rr, &list)
	if len(list.Keys) != 1 {
		t.Fatalf("keys 数量 = %d，应为 1", len(list.Keys))
	}
	k := list.Keys[0]
	if k.Prefix != created.Key[:8] {
		t.Errorf("prefix = %q, 应为 %q", k.Prefix, created.Key[:8])
	}
	if strings.Contains(rr.Body.String(), created.Key) {
		t.Error("列表响应不应包含完整明文 key")
	}
	if k.LastUsedAt != nil {
		t.Errorf("未使用过的 key last_used_at 应为 null，得到 %v", *k.LastUsedAt)
	}
	if k.CreatedAt == "" {
		t.Error("created_at 不应为空")
	}

	// 使用 key 拉取 raw 后 last_used_at 应更新
	rr = doReq(t, srv, http.MethodGet, "/api/config/raw", created.Key, nil)
	if rr.Code != http.StatusOK {
		t.Fatalf("raw status = %d", rr.Code)
	}
	rr = doReq(t, srv, http.MethodGet, "/api/keys", token, nil)
	decodeJSON(t, rr, &list)
	if list.Keys[0].LastUsedAt == nil || *list.Keys[0].LastUsedAt == "" {
		t.Error("使用后 last_used_at 应被更新")
	}

	// 空 name 拒绝
	rr = doReq(t, srv, http.MethodPost, "/api/keys", token, map[string]string{"name": "  "})
	if rr.Code != http.StatusBadRequest {
		t.Fatalf("空 name status = %d", rr.Code)
	}

	// 删除
	rr = doReq(t, srv, http.MethodDelete, "/api/keys/1", token, nil)
	if rr.Code != http.StatusOK {
		t.Fatalf("DELETE keys status = %d, body = %s", rr.Code, rr.Body.String())
	}
	var delResp struct {
		OK bool `json:"ok"`
	}
	decodeJSON(t, rr, &delResp)
	if !delResp.OK {
		t.Error("删除响应 ok 应为 true")
	}

	// 重复删除 → 404
	rr = doReq(t, srv, http.MethodDelete, "/api/keys/1", token, nil)
	if rr.Code != http.StatusNotFound {
		t.Fatalf("重复删除 status = %d，应为 404", rr.Code)
	}

	// 删除后 key 失效
	rr = doReq(t, srv, http.MethodGet, "/api/config/raw", created.Key, nil)
	if rr.Code != http.StatusUnauthorized {
		t.Fatalf("已删除 key raw status = %d，应为 401", rr.Code)
	}
	var rawErr struct {
		Error string `json:"error"`
	}
	decodeJSON(t, rr, &rawErr)
	if rawErr.Error != "无效的 API Key" {
		t.Errorf("raw 401 错误信息 = %q", rawErr.Error)
	}

	// 非法 id
	rr = doReq(t, srv, http.MethodDelete, "/api/keys/abc", token, nil)
	if rr.Code != http.StatusBadRequest {
		t.Fatalf("非法 id status = %d", rr.Code)
	}
}

// ---------------------------------------------------------------------------
// handlers.go：导出 / 修改密码
// ---------------------------------------------------------------------------

func TestExport(t *testing.T) {
	srv := newTestServer(t)
	token := loginToken(t, srv)
	putTestConfig(t, srv, token)

	rr := doReq(t, srv, http.MethodGet, "/api/export", token, nil)
	if rr.Code != http.StatusOK {
		t.Fatalf("GET export status = %d", rr.Code)
	}
	var resp struct {
		JSON string `json:"json"`
	}
	decodeJSON(t, rr, &resp)
	if resp.JSON == "" {
		t.Fatal("json 字段为空")
	}
	// json 字段内的字符串应能解析为完整明文配置
	var cfg Config
	if err := json.Unmarshal([]byte(resp.JSON), &cfg); err != nil {
		t.Fatalf("导出 json 无法解析: %v", err)
	}
	if len(cfg.Accounts) != 2 || cfg.Accounts[0].Cookie != "cookie-1" {
		t.Errorf("导出内容不完整: %+v", cfg.Accounts)
	}
	if cfg.AI.APIKey != "sk-abc123" {
		t.Errorf("导出内容应含明文 api_key: %q", cfg.AI.APIKey)
	}
}

func TestPassword(t *testing.T) {
	srv := newTestServer(t)
	token := loginToken(t, srv)

	// 旧密码错误
	rr := doReq(t, srv, http.MethodPut, "/api/password", token, map[string]string{
		"old_password": "wrong-old", "new_password": "newpass123",
	})
	if rr.Code != http.StatusBadRequest {
		t.Fatalf("旧密码错误 status = %d", rr.Code)
	}
	var e struct {
		Error string `json:"error"`
	}
	decodeJSON(t, rr, &e)
	if e.Error != "旧密码错误" {
		t.Errorf("错误信息 = %q", e.Error)
	}

	// 新密码过短
	rr = doReq(t, srv, http.MethodPut, "/api/password", token, map[string]string{
		"old_password": "admin123456", "new_password": "short",
	})
	if rr.Code != http.StatusBadRequest {
		t.Fatalf("短密码 status = %d", rr.Code)
	}
	decodeJSON(t, rr, &e)
	if e.Error != "新密码至少 8 个字符" {
		t.Errorf("错误信息 = %q", e.Error)
	}

	// 成功修改
	rr = doReq(t, srv, http.MethodPut, "/api/password", token, map[string]string{
		"old_password": "admin123456", "new_password": "newpass123",
	})
	if rr.Code != http.StatusOK {
		t.Fatalf("修改密码 status = %d, body = %s", rr.Code, rr.Body.String())
	}
	var okResp struct {
		OK bool `json:"ok"`
	}
	decodeJSON(t, rr, &okResp)
	if !okResp.OK {
		t.Error("修改密码响应 ok 应为 true")
	}

	// 旧密码登录失败，新密码登录成功
	rr = doReq(t, srv, http.MethodPost, "/api/login", "", map[string]string{
		"username": "admin", "password": "admin123456",
	})
	if rr.Code != http.StatusUnauthorized {
		t.Errorf("旧密码仍可登录 status = %d", rr.Code)
	}
	rr = doReq(t, srv, http.MethodPost, "/api/login", "", map[string]string{
		"username": "admin", "password": "newpass123",
	})
	if rr.Code != http.StatusOK {
		t.Errorf("新密码无法登录 status = %d", rr.Code)
	}
}

// ---------------------------------------------------------------------------
// handlers.go：鉴权边界
// ---------------------------------------------------------------------------

func TestAuthRequired(t *testing.T) {
	srv := newTestServer(t)

	// 无 token 访问管理端接口 → 401
	for _, tc := range []struct{ method, path string }{
		{http.MethodGet, "/api/config"},
		{http.MethodPut, "/api/config"},
		{http.MethodGet, "/api/keys"},
		{http.MethodPost, "/api/keys"},
		{http.MethodDelete, "/api/keys/1"},
		{http.MethodGet, "/api/export"},
		{http.MethodPut, "/api/password"},
	} {
		rr := doReq(t, srv, tc.method, tc.path, "", nil)
		if rr.Code != http.StatusUnauthorized {
			t.Errorf("%s %s 无 token status = %d，应为 401", tc.method, tc.path, rr.Code)
		}
	}

	// 无效 JWT → 401
	rr := doReq(t, srv, http.MethodGet, "/api/config", "not-a-jwt", nil)
	if rr.Code != http.StatusUnauthorized {
		t.Errorf("无效 JWT status = %d，应为 401", rr.Code)
	}

	// raw 无 key / 无效 key → 401
	rr = doReq(t, srv, http.MethodGet, "/api/config/raw", "", nil)
	if rr.Code != http.StatusUnauthorized {
		t.Errorf("raw 无 key status = %d，应为 401", rr.Code)
	}
	rr = doReq(t, srv, http.MethodGet, "/api/config/raw", "ncf_invalidkey", nil)
	if rr.Code != http.StatusUnauthorized {
		t.Errorf("raw 无效 key status = %d，应为 401", rr.Code)
	}

	// 未知 API 路径 → 404（不落入静态文件兜底）
	rr = doReq(t, srv, http.MethodGet, "/api/nonexistent", "", nil)
	if rr.Code != http.StatusNotFound {
		t.Errorf("未知 API 路径 status = %d，应为 404", rr.Code)
	}
	var e struct {
		Error string `json:"error"`
	}
	decodeJSON(t, rr, &e)
	if e.Error == "" {
		t.Error("未知 API 路径应返回 JSON 错误")
	}
}

// ---------------------------------------------------------------------------
// main.go：getenv
// ---------------------------------------------------------------------------

func TestGetenv(t *testing.T) {
	t.Setenv("NCF_TEST_ENV", "v")
	if got := getenv("NCF_TEST_ENV", "def"); got != "v" {
		t.Errorf("已有环境变量应取环境值: %q", got)
	}
	if got := getenv("NCF_TEST_ENV_NOT_SET", "def"); got != "def" {
		t.Errorf("未设置环境变量应取默认值: %q", got)
	}
}
