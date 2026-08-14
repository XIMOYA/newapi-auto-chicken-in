/*
server/import_test.go
配置导入（overwrite / merge + 模块勾选）逻辑测试
*/
package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// authedRequest 构造带 JWT 认证的测试请求。
func authedRequest(t *testing.T, srv *Server, method, path, body string) *httptest.ResponseRecorder {
	t.Helper()
	var req *http.Request
	if body != "" {
		req = httptest.NewRequest(method, path, strings.NewReader(body))
		req.Header.Set("Content-Type", "application/json")
	} else {
		req = httptest.NewRequest(method, path, nil)
	}
	token := loginForTest(t, srv)
	req.Header.Set("Authorization", "Bearer "+token)
	rr := httptest.NewRecorder()
	srv.routes().ServeHTTP(rr, req)
	return rr
}

// loginForTest 通过登录接口获取真实 JWT。
func loginForTest(t *testing.T, srv *Server) string {
	t.Helper()
	req := httptest.NewRequest("POST", "/api/login", strings.NewReader(`{"username":"admin","password":"admin123456"}`))
	req.Header.Set("Content-Type", "application/json")
	rr := httptest.NewRecorder()
	srv.routes().ServeHTTP(rr, req)
	if rr.Code != http.StatusOK {
		t.Fatalf("登录失败: %d %s", rr.Code, rr.Body.String())
	}
	var resp struct {
		Token string `json:"token"`
	}
	if err := json.Unmarshal(rr.Body.Bytes(), &resp); err != nil {
		t.Fatalf("解析登录响应: %v", err)
	}
	return resp.Token
}

// importBody 构造导入请求体。
func importBody(mode string, modules []string, cfg string) string {
	mods, _ := json.Marshal(modules)
	return `{"mode":"` + mode + `","modules":` + string(mods) + `,"config":` + cfg + `}`
}

func TestImportConfig_Overwrite(t *testing.T) {
	srv := newTestServer(t)
	body := importBody("overwrite", nil, `{"accounts":[{"name":"A","url":"https://a.com","cookie":"ck","enabled":true}]}`)
	rr := authedRequest(t, srv, "POST", "/api/config/import", body)
	if rr.Code != http.StatusOK {
		t.Fatalf("overwrite 导入返回 %d: %s", rr.Code, rr.Body.String())
	}
	cfg, _, err := LoadConfig(srv.db)
	if err != nil {
		t.Fatal(err)
	}
	if len(cfg.Accounts) != 1 || cfg.Accounts[0].Name != "A" {
		t.Fatalf("overwrite 后账号数 = %d, want 1", len(cfg.Accounts))
	}
}

func TestImportConfig_MergeAccounts(t *testing.T) {
	srv := newTestServer(t)
	put := `{"config":{"accounts":[{"name":"X","url":"https://x.com","cookie":"cx","enabled":true}]}}`
	if rr := authedRequest(t, srv, "PUT", "/api/config", put); rr.Code != http.StatusOK {
		t.Fatalf("PUT 初始失败: %d", rr.Code)
	}
	body := importBody("merge", []string{"accounts"},
		`{"accounts":[{"name":"X","url":"https://x.com","cookie":"new-cx","enabled":false},{"name":"Y","url":"https://y.com","cookie":"cy","enabled":true}]}`)
	rr := authedRequest(t, srv, "POST", "/api/config/import", body)
	if rr.Code != http.StatusOK {
		t.Fatalf("merge 导入返回 %d: %s", rr.Code, rr.Body.String())
	}
	cfg, _, err := LoadConfig(srv.db)
	if err != nil {
		t.Fatal(err)
	}
	if len(cfg.Accounts) != 2 {
		t.Fatalf("merge 后账号数 = %d, want 2", len(cfg.Accounts))
	}
	byName := map[string]Account{}
	for _, a := range cfg.Accounts {
		byName[a.Name] = a
	}
	if got := byName["X"]; got.Cookie != "new-cx" || got.Enabled {
		t.Fatalf("X 应被同名覆盖: %+v", got)
	}
	if _, ok := byName["Y"]; !ok {
		t.Fatal("Y 应被追加")
	}
}

func TestImportConfig_MergeSites(t *testing.T) {
	srv := newTestServer(t)
	put := `{"config":{"sites":[{"name":"S1","url":"https://s1.com"}]}}`
	if rr := authedRequest(t, srv, "PUT", "/api/config", put); rr.Code != http.StatusOK {
		t.Fatalf("PUT 初始失败: %d", rr.Code)
	}
	body := importBody("merge", []string{"sites"},
		`{"sites":[{"name":"S1","url":"https://s1.com","checkin_path":"/api/x"},{"name":"S2","url":"https://s2.com"}]}`)
	rr := authedRequest(t, srv, "POST", "/api/config/import", body)
	if rr.Code != http.StatusOK {
		t.Fatalf("merge 站点返回 %d: %s", rr.Code, rr.Body.String())
	}
	cfg, _, err := LoadConfig(srv.db)
	if err != nil {
		t.Fatal(err)
	}
	if len(cfg.Sites) != 2 {
		t.Fatalf("merge 后站点数 = %d, want 2", len(cfg.Sites))
	}
}

// 核心：模块勾选——只勾 proxy_pool 时，账号不动、代理池更新
func TestImportConfig_MergeModuleSelection(t *testing.T) {
	srv := newTestServer(t)
	// 初始：一个账号 + 默认代理池（disabled）
	put := `{"config":{"accounts":[{"name":"A","url":"https://a.com","cookie":"c","enabled":true}]}}`
	if rr := authedRequest(t, srv, "PUT", "/api/config", put); rr.Code != http.StatusOK {
		t.Fatalf("PUT 初始失败: %d", rr.Code)
	}
	// 只勾 proxy_pool 导入：账号 A 应保留，代理池应更新为 enabled+来源
	body := importBody("merge", []string{"proxy_pool"},
		`{"accounts":[{"name":"B","url":"https://b.com","cookie":"cb","enabled":true}],"proxy_pool":{"enabled":true,"test_url":"https://api.ipify.org","timeout":8,"max_workers":25,"max_proxies":250,"ip_swap_limit":2,"sources":["https://example.com/list.txt"]}}`)
	rr := authedRequest(t, srv, "POST", "/api/config/import", body)
	if rr.Code != http.StatusOK {
		t.Fatalf("模块勾选导入返回 %d: %s", rr.Code, rr.Body.String())
	}
	cfg, _, err := LoadConfig(srv.db)
	if err != nil {
		t.Fatal(err)
	}
	// 账号未勾选 → 只保留 A
	if len(cfg.Accounts) != 1 || cfg.Accounts[0].Name != "A" {
		t.Fatalf("未勾选 accounts 不应改动: %+v", cfg.Accounts)
	}
	// 代理池已勾选 → 更新
	if !cfg.ProxyPool.Enabled {
		t.Fatal("proxy_pool 应被更新为 enabled")
	}
	if len(cfg.ProxyPool.Sources) != 1 || cfg.ProxyPool.Sources[0] != "https://example.com/list.txt" {
		t.Fatalf("proxy_pool sources 应被更新: %+v", cfg.ProxyPool.Sources)
	}
}

// modules 未传（null）→ 向后兼容：默认全部模块合并
func TestImportConfig_MergeNilModules(t *testing.T) {
	srv := newTestServer(t)
	put := `{"config":{"accounts":[{"name":"A","url":"https://a.com","cookie":"c","enabled":true}],"proxy_pool":{"enabled":true}}}`
	if rr := authedRequest(t, srv, "PUT", "/api/config", put); rr.Code != http.StatusOK {
		t.Fatalf("PUT 初始失败: %d", rr.Code)
	}
	body := importBody("merge", nil, `{"accounts":[{"name":"B","url":"https://b.com","cookie":"cb","enabled":true}],"proxy_pool":{"enabled":false}}`)
	rr := authedRequest(t, srv, "POST", "/api/config/import", body)
	if rr.Code != http.StatusOK {
		t.Fatalf("merge 返回 %d: %s", rr.Code, rr.Body.String())
	}
	cfg, _, err := LoadConfig(srv.db)
	if err != nil {
		t.Fatal(err)
	}
	// modules 为 null → 默认全部：账号应合并为 2 个，proxy_pool 按导入覆盖
	if len(cfg.Accounts) != 2 {
		t.Fatalf("modules=null 默认全部，账号应合并为 2 个: %+v", cfg.Accounts)
	}
	if cfg.ProxyPool.Enabled {
		t.Fatalf("modules=null 默认全部，proxy_pool 应按导入覆盖为 false")
	}
}

// modules 为空数组 → 明确不导入任何模块
func TestImportConfig_MergeEmptyModules(t *testing.T) {
	srv := newTestServer(t)
	put := `{"config":{"accounts":[{"name":"A","url":"https://a.com","cookie":"c","enabled":true}],"proxy_pool":{"enabled":true}}}`
	if rr := authedRequest(t, srv, "PUT", "/api/config", put); rr.Code != http.StatusOK {
		t.Fatalf("PUT 初始失败: %d", rr.Code)
	}
	body := importBody("merge", []string{}, `{"accounts":[{"name":"B","url":"https://b.com","cookie":"cb","enabled":true}],"proxy_pool":{"enabled":false}}`)
	rr := authedRequest(t, srv, "POST", "/api/config/import", body)
	if rr.Code != http.StatusOK {
		t.Fatalf("merge 返回 %d: %s", rr.Code, rr.Body.String())
	}
	cfg, _, err := LoadConfig(srv.db)
	if err != nil {
		t.Fatal(err)
	}
	// 空数组 → 什么都不动：账号保持 1 个，proxy_pool 保持 enabled
	if len(cfg.Accounts) != 1 || cfg.Accounts[0].Name != "A" {
		t.Fatalf("modules=[] 不应改动账号: %+v", cfg.Accounts)
	}
	if !cfg.ProxyPool.Enabled {
		t.Fatal("modules=[] 不应改动 proxy_pool")
	}
}

func TestImportConfig_InvalidMode(t *testing.T) {
	srv := newTestServer(t)
	body := importBody("bad", nil, `{"accounts":[]}`)
	rr := authedRequest(t, srv, "POST", "/api/config/import", body)
	if rr.Code != http.StatusBadRequest {
		t.Fatalf("非法 mode 应 400, got %d", rr.Code)
	}
}

func TestImportConfig_InvalidModule(t *testing.T) {
	srv := newTestServer(t)
	body := importBody("merge", []string{"bogus"}, `{"accounts":[]}`)
	rr := authedRequest(t, srv, "POST", "/api/config/import", body)
	if rr.Code != http.StatusBadRequest {
		t.Fatalf("非法模块应 400, got %d", rr.Code)
	}
}

func TestImportConfig_RequiresJWT(t *testing.T) {
	srv := newTestServer(t)
	body := importBody("overwrite", nil, `{"accounts":[]}`)
	req := httptest.NewRequest("POST", "/api/config/import", strings.NewReader(body))
	rr := httptest.NewRecorder()
	srv.routes().ServeHTTP(rr, req)
	if rr.Code != http.StatusUnauthorized {
		t.Fatalf("无 token 应 401, got %d", rr.Code)
	}
}

func TestMergeConfigWithModules_KeepsOtherModules(t *testing.T) {
	oldCfg := DefaultConfig()
	oldCfg.AI.APIKey = "keep-me"
	oldCfg.Accounts = []Account{{Name: "A", URL: "https://a.com", Cookie: "c", Enabled: true}}

	in := Config{Accounts: []Account{{Name: "B", URL: "https://b.com", Cookie: "c2", Enabled: true}}}
	merged := mergeConfigWithModules(&in, &oldCfg, map[string]bool{"accounts": true}, []string{"accounts"})

	if len(merged.Accounts) != 2 {
		t.Fatalf("合并后账号数 = %d, want 2", len(merged.Accounts))
	}
	if merged.AI.APIKey != "keep-me" {
		t.Fatalf("AI 模块应保留现有: %q", merged.AI.APIKey)
	}
}
