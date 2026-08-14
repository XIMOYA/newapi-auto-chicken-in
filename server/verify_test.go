/*
server/verify_test.go
二次确认密码接口测试
*/
package main

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestVerifyPassword_OK(t *testing.T) {
	srv := newTestServer(t)
	body := `{"password":"admin123456"}`
	rr := authedRequest(t, srv, "POST", "/api/auth/verify-password", body)
	if rr.Code != http.StatusOK {
		t.Fatalf("正确密码应 200, got %d: %s", rr.Code, rr.Body.String())
	}
}

func TestVerifyPassword_Wrong(t *testing.T) {
	srv := newTestServer(t)
	body := `{"password":"wrong-pass"}`
	rr := authedRequest(t, srv, "POST", "/api/auth/verify-password", body)
	if rr.Code != http.StatusBadRequest {
		t.Fatalf("错误密码应 400, got %d", rr.Code)
	}
}

func TestVerifyPassword_RequiresJWT(t *testing.T) {
	srv := newTestServer(t)
	req := httptest.NewRequest("POST", "/api/auth/verify-password", strings.NewReader(`{"password":"x"}`))
	rr := httptest.NewRecorder()
	srv.routes().ServeHTTP(rr, req)
	if rr.Code != http.StatusUnauthorized {
		t.Fatalf("无 token 应 401, got %d", rr.Code)
	}
}
