/*
server/dual_auth_test.go
双认证（JWT 或 API Key）的边界测试

这批断言守两件事，方向相反、同样重要：
  - 运维类端点必须对 API Key 放行，否则脚本还得去模拟登录换 JWT
  - 控制平面端点必须对 API Key 保持拒绝：改密码、增删 API Key、整份导入。
    一把躺在 CI secrets 里的 Key 若能造新 Key 或改密码，泄露即等于永久失守
*/
package main

import (
	"net/http"
	"strings"
	"testing"
)

// dualAuthCase 一个端点在两种凭据下的期望状态。
type dualAuthCase struct {
	name, method, path string
	body               any
	// notWant 用「不应该是这个码」表达：多数端点在鉴权通过后会因为业务前置条件
	// 返回 400/409/404，逐个去对业务码既脆弱又偏离本测试的关注点
	notWant int
}

func TestAPIKeyAllowedOnOperationalEndpoints(t *testing.T) {
	srv := newTestServer(t)
	jwt := loginToken(t, srv)
	key := apiKeyToken(t, srv, jwt)

	cases := []dualAuthCase{
		{"读配置", http.MethodGet, "/api/config", nil, http.StatusUnauthorized},
		{"读版本号", http.MethodGet, "/api/config/revision", nil, http.StatusUnauthorized},
		{"账号增量操作", http.MethodPost, "/api/accounts/ops",
			map[string]any{"ops": []map[string]any{
				{"type": "delete", "name": "不存在的账号"}}}, http.StatusUnauthorized},
		{"检测状态", http.MethodGet, "/api/cookie-tests/status", nil, http.StatusUnauthorized},
		{"停止检测", http.MethodPost, "/api/cookie-tests/stop", nil, http.StatusUnauthorized},
		{"代理列表", http.MethodGet, "/api/proxies", nil, http.StatusUnauthorized},
		{"代理统计", http.MethodGet, "/api/proxies/stats", nil, http.StatusUnauthorized},
		{"运行状态", http.MethodGet, "/api/run-state", nil, http.StatusUnauthorized},
		{"强制解锁", http.MethodPost, "/api/run-state/unlock", nil, http.StatusUnauthorized},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			rr := doReq(t, srv, tc.method, tc.path, key, tc.body)
			if rr.Code == tc.notWant {
				t.Fatalf("API Key 应被放行，却拿到 %d: %s", rr.Code, rr.Body.String())
			}
		})
	}
}

func TestAPIKeyRejectedOnControlPlaneEndpoints(t *testing.T) {
	// 这几个是控制平面：能改身份、能造新凭证、能一次覆盖全部配置。
	// 放开它们等于让 CI secrets 泄露直接升级成永久夺权，所以必须只认 JWT。
	srv := newTestServer(t)
	jwt := loginToken(t, srv)
	key := apiKeyToken(t, srv, jwt)

	cases := []struct {
		name, method, path string
		body               any
	}{
		{"改管理员密码", http.MethodPut, "/api/password",
			map[string]string{"old_password": "admin123456", "new_password": "whatever12345"}},
		{"列出 API Key", http.MethodGet, "/api/keys", nil},
		{"创建 API Key", http.MethodPost, "/api/keys", map[string]string{"name": "提权"}},
		{"删除 API Key", http.MethodDelete, "/api/keys/1", nil},
		{"整份导入配置", http.MethodPost, "/api/config/import",
			map[string]any{"content": "{}"}},
		{"二次密码确认", http.MethodPost, "/api/auth/verify-password",
			map[string]string{"password": "admin123456"}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			rr := doReq(t, srv, tc.method, tc.path, key, tc.body)
			if rr.Code != http.StatusUnauthorized {
				t.Fatalf("API Key 不该能调这个端点，实际 %d: %s", rr.Code, rr.Body.String())
			}
		})
	}

	// 密码没被改掉：上面那次尝试必须是彻底无效的
	if rr := doReq(t, srv, http.MethodPost, "/api/login", "", map[string]string{
		"username": "admin", "password": "admin123456",
	}); rr.Code != http.StatusOK {
		t.Error("原密码应仍然可用，说明改密码请求确实没生效")
	}
}

func TestExportSkipsTicketForAPIKeyOnly(t *testing.T) {
	srv := newTestServer(t)
	jwt := loginToken(t, srv)
	key := apiKeyToken(t, srv, jwt)
	seedConfig(t, srv, []Account{
		{Name: "A", URL: "https://a.com", LoginMethod: LoginMethodNewAPICookie,
			Cookie: "session=plain-secret", Enabled: true},
	}, nil)

	// API Key：免票据，直接拿到明文
	rr := doReq(t, srv, http.MethodGet, "/api/export", key, nil)
	if rr.Code != http.StatusOK {
		t.Fatalf("API Key 导出应免票据，实际 %d: %s", rr.Code, rr.Body.String())
	}
	var resp struct {
		JSON string `json:"json"`
	}
	decodeJSON(t, rr, &resp)
	if !strings.Contains(resp.JSON, "session=plain-secret") {
		t.Error("导出内容应是明文配置")
	}

	// JWT：票据仍然必需。这是网页端的二次确认，不能因为放开 API Key 就一起松掉
	if noTicket := doReq(t, srv, http.MethodGet, "/api/export", jwt, nil); noTicket.Code != http.StatusForbidden {
		t.Fatalf("JWT 无票据导出应 403，实际 %d: %s", noTicket.Code, noTicket.Body.String())
	}
}

func TestDualAuthRejectsGarbageCredentials(t *testing.T) {
	srv := newTestServer(t)
	for _, token := range []string{"", "随便一串", "Bearer", strings.Repeat("x", 200)} {
		rr := doReq(t, srv, http.MethodGet, "/api/config", token, nil)
		if rr.Code != http.StatusUnauthorized {
			t.Errorf("token=%q 应 401，实际 %d", token, rr.Code)
		}
	}
}

func TestDeletedAPIKeyLosesOperationalAccess(t *testing.T) {
	// Key 被吊销后必须立刻失效，不能因为双认证多了一条路就留后门
	srv := newTestServer(t)
	jwt := loginToken(t, srv)

	rr := doReq(t, srv, http.MethodPost, "/api/keys", jwt, map[string]string{"name": "临时"})
	if rr.Code != http.StatusOK {
		t.Fatalf("创建 Key 失败 = %d", rr.Code)
	}
	var created struct {
		ID  int64  `json:"id"`
		Key string `json:"key"`
	}
	decodeJSON(t, rr, &created)

	if ok := doReq(t, srv, http.MethodGet, "/api/config", created.Key, nil); ok.Code != http.StatusOK {
		t.Fatalf("新建的 Key 应可用，实际 %d", ok.Code)
	}
	if del := doReq(t, srv, http.MethodDelete, "/api/keys/"+itoa(created.ID), jwt, nil); del.Code != http.StatusOK {
		t.Fatalf("删除 Key 失败 = %d: %s", del.Code, del.Body.String())
	}
	if after := doReq(t, srv, http.MethodGet, "/api/config", created.Key, nil); after.Code != http.StatusUnauthorized {
		t.Fatalf("已删除的 Key 应立刻失效，实际 %d", after.Code)
	}
}

// itoa 避免为一次拼路径引入 strconv 的额外导入噪音。
func itoa(v int64) string {
	if v == 0 {
		return "0"
	}
	var buf [20]byte
	i := len(buf)
	for v > 0 {
		i--
		buf[i] = byte('0' + v%10)
		v /= 10
	}
	return string(buf[i:])
}
