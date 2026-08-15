/*
server/available_proxies_test.go
职责：验证 API Key 鉴权下的可用隧道 IP 接口。
覆盖：缺省返回全部可用 IP，显式 limit 参数仍按指定数量截断。
*/
package main

import (
	"fmt"
	"net/http"
	"testing"
)

func TestAvailableProxiesDefaultReturnsAll(t *testing.T) {
	srv := newTestServer(t)
	entries := make([]ProxyEntry, 0, 101)
	for i := 1; i <= 101; i++ {
		entries = append(entries, ProxyEntry{
			Source:      "test",
			Addr:        fmt.Sprintf("10.0.0.%d:8080", i),
			LatencyMs:   i,
			Alive:       true,
			LastChecked: "now",
			LastAliveAt: "now",
		})
	}
	if err := srv.proxies.replaceAll(entries); err != nil {
		t.Fatalf("replaceAll: %v", err)
	}

	plain, hash, prefix, err := GenerateAPIKey()
	if err != nil {
		t.Fatalf("GenerateAPIKey: %v", err)
	}
	if _, err := CreateAPIKey(srv.db, "available-test", hash, prefix); err != nil {
		t.Fatalf("CreateAPIKey: %v", err)
	}

	var all struct {
		Proxies []string `json:"proxies"`
		Count   int      `json:"count"`
	}
	rr := doReq(t, srv, http.MethodGet, "/api/proxies/available", plain, nil)
	if rr.Code != http.StatusOK {
		t.Fatalf("default available status = %d, body = %s", rr.Code, rr.Body.String())
	}
	decodeJSON(t, rr, &all)
	if all.Count != 101 || len(all.Proxies) != 101 {
		t.Fatalf("default available count = %d / %d, want 101", all.Count, len(all.Proxies))
	}

	var limited struct {
		Proxies []string `json:"proxies"`
		Count   int      `json:"count"`
	}
	rr = doReq(t, srv, http.MethodGet, "/api/proxies/available?limit=7", plain, nil)
	if rr.Code != http.StatusOK {
		t.Fatalf("limited available status = %d, body = %s", rr.Code, rr.Body.String())
	}
	decodeJSON(t, rr, &limited)
	if limited.Count != 7 || len(limited.Proxies) != 7 {
		t.Fatalf("limited available count = %d / %d, want 7", limited.Count, len(limited.Proxies))
	}
}
