/*
server/import_test.go
配置导入（overwrite / merge）逻辑测试
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
	// 直接用登录接口拿 token，保证与现有测试风格一致
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

func TestImportConfig_Overwrite(t *testing.T) {
	srv := newTestServer(t)
	body := `{"mode":"overwrite","config":{"accounts":[{"name":"A","url":"https://a.com","cookie":"ck","enabled":true}]}}`
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
	// 先放一个账号 X
	put := `{"config":{"accounts":[{"name":"X","url":"https://x.com","cookie":"cx","enabled":true}]}}`
	if rr := authedRequest(t, srv, "PUT", "/api/config", put); rr.Code != http.StatusOK {
		t.Fatalf("PUT 初始失败: %d", rr.Code)
	}
	// 导入 Y（新增）+ X（同名覆盖 cookie）
	body := `{"mode":"merge","config":{"accounts":[{"name":"X","url":"https://x.com","cookie":"new-cx","enabled":false},{"name":"Y","url":"https://y.com","cookie":"cy","enabled":true}]}}`
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
	body := `{"mode":"merge","config":{"sites":[{"name":"S1","url":"https://s1.com","checkin_path":"/api/x"},{"name":"S2","url":"https://s2.com"}]}}`
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

func TestImportConfig_InvalidMode(t *testing.T) {
	srv := newTestServer(t)
	body := `{"mode":"bad","config":{"accounts":[]}}`
	rr := authedRequest(t, srv, "POST", "/api/config/import", body)
	if rr.Code != http.StatusBadRequest {
		t.Fatalf("非法 mode 应 400, got %d", rr.Code)
	}
}

func TestImportConfig_RequiresJWT(t *testing.T) {
	srv := newTestServer(t)
	body := `{"mode":"overwrite","config":{"accounts":[]}}`
	req := httptest.NewRequest("POST", "/api/config/import", strings.NewReader(body))
	rr := httptest.NewRecorder()
	srv.routes().ServeHTTP(rr, req)
	if rr.Code != http.StatusUnauthorized {
		t.Fatalf("无 token 应 401, got %d", rr.Code)
	}
}

func TestMergeConfig_KeepsOtherModules(t *testing.T) {
	oldCfg := DefaultConfig()
	oldCfg.AI.APIKey = "keep-me"
	oldCfg.Accounts = []Account{{Name: "A", URL: "https://a.com", Cookie: "c", Enabled: true}}

	in := Config{Accounts: []Account{{Name: "B", URL: "https://b.com", Cookie: "c2", Enabled: true}}}
	merged := mergeConfig(&in, &oldCfg)

	if len(merged.Accounts) != 2 {
		t.Fatalf("合并后账号数 = %d, want 2", len(merged.Accounts))
	}
	if merged.AI.APIKey != "keep-me" {
		t.Fatalf("AI 模块应保留现有: %q", merged.AI.APIKey)
	}
}
