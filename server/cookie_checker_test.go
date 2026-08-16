package main

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"sync/atomic"
	"testing"
)

func TestCheckNewAPICookieDirectValid(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != cookieTestSelfPath {
			t.Fatalf("path = %s, want %s", r.URL.Path, cookieTestSelfPath)
		}
		if got := r.Header.Get("Cookie"); got != "session=good" {
			t.Fatalf("cookie = %q", got)
		}
		_ = json.NewEncoder(w).Encode(map[string]any{
			"success": true,
			"data":    map[string]any{"id": 123, "username": "tester"},
		})
	}))
	defer server.Close()

	result := checkNewAPICookie(context.Background(), HTTPConfig{Timeout: 5, Verify: true}, Account{
		Name:   "direct",
		URL:    server.URL,
		Cookie: "session=good",
	})
	if result.State != cookieTestStateValid {
		t.Fatalf("state = %q, message = %q", result.State, result.Message)
	}
	if result.UserID == nil || *result.UserID != 123 {
		t.Fatalf("user_id = %v", result.UserID)
	}
	if result.Message != "tester" {
		t.Fatalf("message = %q", result.Message)
	}
}

func TestCheckNewAPICookieUnauthorized(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusUnauthorized)
		_, _ = w.Write([]byte(`{"success":false,"message":"unauthorized"}`))
	}))
	defer server.Close()

	result := checkNewAPICookie(context.Background(), HTTPConfig{Timeout: 5, Verify: true}, Account{
		Name:   "invalid",
		URL:    server.URL,
		Cookie: "session=dead",
	})
	if result.State != cookieTestStateInvalid {
		t.Fatalf("state = %q, message = %q", result.State, result.Message)
	}
}

func TestCheckNewAPICookieRefreshPreservesOriginalCookie(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case cookieTestRefreshPath:
			if r.Method != http.MethodPost {
				t.Fatalf("refresh method = %s", r.Method)
			}
			if got := r.Header.Get("Cookie"); got != "new_api_refresh=refresh-token" {
				t.Fatalf("refresh cookie = %q", got)
			}
			_ = json.NewEncoder(w).Encode(map[string]any{
				"success": true,
				"data": map[string]any{
					"access_token": "access-token",
					"token_type":   "Bearer",
					"user":         map[string]any{"id": 456},
				},
			})
		case cookieTestSelfPath:
			if got := r.Header.Get("Authorization"); got != "Bearer access-token" {
				t.Fatalf("authorization = %q", got)
			}
			if got := r.Header.Get("Cookie"); got != "new_api_refresh=refresh-token" {
				t.Fatalf("self cookie = %q", got)
			}
			if strings.Contains(r.Header.Get("Cookie"), "access-token") {
				t.Fatal("access token must not be rewritten into Cookie")
			}
			_ = json.NewEncoder(w).Encode(map[string]any{
				"success": true,
				"data":    map[string]any{"id": 456, "username": "refresh-user"},
			})
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()

	result := checkNewAPICookie(context.Background(), HTTPConfig{Timeout: 5, Verify: true}, Account{
		Name:   "refresh",
		URL:    server.URL,
		Cookie: "new_api_refresh=refresh-token",
	})
	if result.State != cookieTestStateValid {
		t.Fatalf("state = %q, message = %q", result.State, result.Message)
	}
	if result.UserID == nil || *result.UserID != 456 {
		t.Fatalf("user_id = %v", result.UserID)
	}
}

func TestCheckNewAPICookieAbnormalResponses(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusBadGateway)
		_, _ = w.Write([]byte("upstream failure"))
	}))
	defer server.Close()

	result := checkNewAPICookie(context.Background(), HTTPConfig{Timeout: 5, Verify: true}, Account{
		Name:   "abnormal",
		URL:    server.URL,
		Cookie: "session=server-error",
	})
	if result.State != cookieTestStateAbnormal {
		t.Fatalf("state = %q, message = %q", result.State, result.Message)
	}
	if strings.Contains(result.Message, "upstream failure") {
		t.Fatalf("response body leaked into message: %q", result.Message)
	}
}

func TestCheckGithubCookieGetsCodeWithoutCallback(t *testing.T) {
	var callbackCalls atomic.Int32
	site := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/api/oauth/github" {
			callbackCalls.Add(1)
			http.Error(w, "callback must not be called", http.StatusInternalServerError)
			return
		}
		if r.URL.Path != "/api/oauth/state" {
			http.NotFound(w, r)
			return
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"success": true, "data": "state-123"})
	}))
	defer site.Close()

	authorize := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Query().Get("state") != "state-123" {
			t.Fatalf("state = %q", r.URL.Query().Get("state"))
		}
		if r.URL.Query().Get("scope") != "user:email" {
			t.Fatalf("scope = %q", r.URL.Query().Get("scope"))
		}
		cookie := r.Header.Get("Cookie")
		for _, part := range []string{"user_session=github-session", "__Host-user_session_same_site=github-session", "logged_in=yes"} {
			if !strings.Contains(cookie, part) {
				t.Fatalf("cookie %q missing %q", cookie, part)
			}
		}
		w.Header().Set("Location", "https://example.test/oauth/callback?code=secret-code&state=state-123")
		w.WriteHeader(http.StatusFound)
	}))
	defer authorize.Close()

	result := checkGithubCookieWithAuthorizeURL(context.Background(), HTTPConfig{Timeout: 5, Verify: true}, Account{
		Name:              "github",
		URL:               site.URL,
		GithubUserSession: "github-session",
	}, authorize.URL)
	if result.State != cookieTestStateValid {
		t.Fatalf("state = %q, message = %q", result.State, result.Message)
	}
	if strings.Contains(result.Message, "secret-code") || callbackCalls.Load() != 0 {
		t.Fatalf("GitHub code/callback leaked or callback called: message=%q calls=%d", result.Message, callbackCalls.Load())
	}
}

func TestCheckGithubCookieInvalidRedirect(t *testing.T) {
	site := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]any{"success": true, "data": "state-123"})
	}))
	defer site.Close()
	authorize := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Location", "https://github.com/login?return_to=%2Foauth")
		w.WriteHeader(http.StatusFound)
	}))
	defer authorize.Close()

	result := checkGithubCookieWithAuthorizeURL(context.Background(), HTTPConfig{Timeout: 5, Verify: true}, Account{
		Name:              "github-invalid",
		URL:               site.URL,
		GithubUserSession: "dead-session",
	}, authorize.URL)
	if result.State != cookieTestStateInvalid {
		t.Fatalf("state = %q, message = %q", result.State, result.Message)
	}
}

func TestCookieTestEndpointsKeepModesSeparate(t *testing.T) {
	srv := newTestServer(t)
	token := loginToken(t, srv)
	cfg := DefaultConfig()
	cfg.Accounts = []Account{
		{Name: "newapi-account", URL: "http://127.0.0.1:1", LoginMethod: LoginMethodNewAPICookie, Enabled: true},
		{Name: "github-account", URL: "http://127.0.0.1:1", LoginMethod: LoginMethodGitHubCookie, Enabled: true},
	}
	if _, err := SaveConfig(srv.db, cfg); err != nil {
		t.Fatalf("SaveConfig: %v", err)
	}

	newapiResp := doReq(t, srv, http.MethodPost, "/api/cookie-tests/newapi", token, map[string]any{"account_names": []string{}})
	if newapiResp.Code != http.StatusOK {
		t.Fatalf("NewAPI endpoint status = %d, body = %s", newapiResp.Code, newapiResp.Body.String())
	}
	var newapiResult CookieTestResponse
	decodeJSON(t, newapiResp, &newapiResult)
	if newapiResult.Mode != LoginMethodNewAPICookie || len(newapiResult.Results) != 1 || newapiResult.Results[0].Name != "newapi-account" {
		t.Fatalf("NewAPI response not isolated: %+v", newapiResult)
	}

	githubResp := doReq(t, srv, http.MethodPost, "/api/cookie-tests/github", token, map[string]any{"account_names": []string{}})
	if githubResp.Code != http.StatusOK {
		t.Fatalf("GitHub endpoint status = %d, body = %s", githubResp.Code, githubResp.Body.String())
	}
	var githubResult CookieTestResponse
	decodeJSON(t, githubResp, &githubResult)
	if githubResult.Mode != LoginMethodGitHubCookie || len(githubResult.Results) != 1 || githubResult.Results[0].Name != "github-account" {
		t.Fatalf("GitHub response not isolated: %+v", githubResult)
	}
}

func TestCookieTestBaseURLRejectsNonHTTP(t *testing.T) {
	if _, err := cookieTestBaseURL("ftp://example.com"); err == nil {
		t.Fatal("ftp URL should be rejected")
	}
	if _, err := cookieTestBaseURL("https://example.com/path/"); err != nil {
		t.Fatalf("valid URL rejected: %v", err)
	}
	if _, err := url.Parse("https://example.com"); err != nil {
		t.Fatal(err)
	}
}
