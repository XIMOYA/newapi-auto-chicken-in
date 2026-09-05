/*
server/site_apikey_test.go
测试：站点 API Key 四步链路

这些断言守的是「拿到一串长得像 key 的废字符串」这类静默故障 —— 它们不会报错，
只会在你把 key 配到别处之后才发现不能用。所以每条都盯着一个具体的坑：

	打码值被当成全值、sk- 前缀重复拼、已有令牌被重建作废、refresh 代次丢失。

用 httptest 起假站点而不是注入函数类型：这条链路的风险大半在「HTTP 层面对不对」
（方法是 POST 还是 GET、鉴权头带没带、信封拆得对不对），换成假函数就全测不到了。
*/
package main

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// apiKeyTestSession 造一个直连假站点的会话，省去每个用例重复搭 client。
func apiKeyTestSession(t *testing.T, baseURL string) *apiKeySession {
	t.Helper()
	sess, err := openAPIKeySession(context.Background(),
		HTTPConfig{Timeout: 5, Verify: true},
		Account{Name: "A", URL: baseURL, Cookie: "session=good"}, "")
	if err != nil {
		t.Fatalf("openAPIKeySession() error = %v", err)
	}
	return sess
}

// pagedTokens 按新版站点的分页形状回一批令牌。
func pagedTokens(items ...map[string]any) string {
	body, _ := json.Marshal(map[string]any{
		"success": true,
		"data":    map[string]any{"page": 1, "page_size": 100, "items": items},
	})
	return string(body)
}

func TestEnsureAPIKeyReusesExistingTokenAndFetchesFullValue(t *testing.T) {
	var hits []string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		hits = append(hits, r.Method+" "+r.URL.Path)
		switch {
		case r.Method == http.MethodGet && r.URL.Path == "/api/token/":
			// 新版站点的列表把 key 打码，直接用就废了
			_, _ = w.Write([]byte(pagedTokens(map[string]any{
				"id": 7, "name": apiKeyTokenName, "key": "abcd**********wxyz",
			})))
		case r.Method == http.MethodPost && r.URL.Path == "/api/token/7/key":
			_, _ = w.Write([]byte(`{"success":true,"data":{"key":"realkey123"}}`))
		default:
			t.Errorf("意外请求: %s %s", r.Method, r.URL.Path)
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	defer server.Close()

	key, created, err := ensureAccountAPIKey(context.Background(), apiKeyTestSession(t, server.URL))
	if err != nil {
		t.Fatalf("ensureAccountAPIKey() error = %v", err)
	}
	if created {
		t.Error("已有令牌不该被重建：重建会换出新值，把用户配到别处的旧 key 作废")
	}
	if key != "sk-realkey123" {
		t.Errorf("key = %q, want sk-realkey123", key)
	}
	for _, h := range hits {
		if strings.HasPrefix(h, "POST /api/token/") && h == "POST /api/token/" {
			t.Error("命中已有令牌时不该再创建")
		}
	}
}

func TestEnsureAPIKeyNeverReturnsTheMaskedValue(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.Method == http.MethodGet:
			_, _ = w.Write([]byte(pagedTokens(map[string]any{
				"id": 3, "name": apiKeyTokenName, "key": "sk-1234**********abcd",
			})))
		default:
			_, _ = w.Write([]byte(`{"success":true,"data":{"key":"full-value"}}`))
		}
	}))
	defer server.Close()

	key, _, err := ensureAccountAPIKey(context.Background(), apiKeyTestSession(t, server.URL))
	if err != nil {
		t.Fatalf("ensureAccountAPIKey() error = %v", err)
	}
	if strings.Contains(key, "*") {
		t.Fatalf("key = %q，打码值被当成了全值 —— 这种 key 配到别处才会发现不可用", key)
	}
	if key != "sk-full-value" {
		t.Errorf("key = %q, want sk-full-value", key)
	}
}

func TestEnsureAPIKeyUsesLegacyPlainListedValueDirectly(t *testing.T) {
	var fetched bool
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.HasSuffix(r.URL.Path, "/key") {
			fetched = true
		}
		if r.Method == http.MethodGet {
			// 老版本站点没有打码那层，列表直接给全值
			_, _ = w.Write([]byte(pagedTokens(map[string]any{
				"id": 1, "name": apiKeyTokenName, "key": "legacyplain",
			})))
			return
		}
		t.Errorf("意外请求: %s %s", r.Method, r.URL.Path)
	}))
	defer server.Close()

	key, _, err := ensureAccountAPIKey(context.Background(), apiKeyTestSession(t, server.URL))
	if err != nil {
		t.Fatalf("ensureAccountAPIKey() error = %v", err)
	}
	if fetched {
		t.Error("列表已给出全值时不该再打取值接口（它挂着 CriticalRateLimit）")
	}
	if key != "sk-legacyplain" {
		t.Errorf("key = %q, want sk-legacyplain", key)
	}
}

func TestEnsureAPIKeyCreatesThenLooksUpIDAndFetchesValue(t *testing.T) {
	var (
		listed  int
		created bool
		body    map[string]any
	)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.Method == http.MethodGet && r.URL.Path == "/api/token/":
			listed++
			if !created {
				// 第一次列表：空的，所以要走创建
				_, _ = w.Write([]byte(pagedTokens()))
				return
			}
			_, _ = w.Write([]byte(pagedTokens(map[string]any{
				"id": 42, "name": apiKeyTokenName, "key": "aaaa**********zzzz",
			})))
		case r.Method == http.MethodPost && r.URL.Path == "/api/token/":
			created = true
			_ = json.NewDecoder(r.Body).Decode(&body)
			// 站点这个接口不回传 key，只说建好了
			_, _ = w.Write([]byte(`{"success":true,"message":""}`))
		case r.Method == http.MethodPost && r.URL.Path == "/api/token/42/key":
			_, _ = w.Write([]byte(`{"success":true,"data":{"key":"brandnew"}}`))
		default:
			t.Errorf("意外请求: %s %s", r.Method, r.URL.Path)
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	defer server.Close()

	key, wasCreated, err := ensureAccountAPIKey(context.Background(), apiKeyTestSession(t, server.URL))
	if err != nil {
		t.Fatalf("ensureAccountAPIKey() error = %v", err)
	}
	if !wasCreated {
		t.Error("这次是新建的，created 应为 true")
	}
	if key != "sk-brandnew" {
		t.Errorf("key = %q, want sk-brandnew", key)
	}
	if listed != 2 {
		t.Errorf("列表打了 %d 次，应为 2 次（建之前找一次、建完回查 id 一次）", listed)
	}
	if got := body["name"]; got != apiKeyTokenName {
		t.Errorf("请求体 name = %v, want %s", got, apiKeyTokenName)
	}
	// 配额与模型限制一律不设，走站点默认。带上它们等于用猜的值覆盖站点策略
	for _, unwanted := range []string{"remain_quota", "unlimited_quota", "model_limits", "model_limits_enabled"} {
		if _, ok := body[unwanted]; ok {
			t.Errorf("请求体不该带 %s（KIQ 明确要求不设配额与模型限制）", unwanted)
		}
	}
	if got, ok := body["expired_time"].(float64); !ok || got != -1 {
		t.Errorf("expired_time = %v, want -1（永不过期）", body["expired_time"])
	}
}

func TestWithAPIKeyPrefixNeverDoublesUp(t *testing.T) {
	// sk-sk-xxx 会让 key 直接失效，而它在日志里看着像个正常的 key
	cases := map[string]string{
		"plain":      "sk-plain",
		"sk-already": "sk-already",
		"  spaced  ": "sk-spaced",
		"":           "",
		"   ":        "",
		"sk-":        "sk-",
	}
	for in, want := range cases {
		if got := withAPIKeyPrefix(in); got != want {
			t.Errorf("withAPIKeyPrefix(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestSiteTokenMaskedDetection(t *testing.T) {
	if (siteToken{Key: "abcd**********wxyz"}).masked() != true {
		t.Error("含 * 应判为打码")
	}
	if (siteToken{Key: "plainkey123"}).masked() != false {
		t.Error("不含 * 应判为全值")
	}
	// 空值也算不上打码，但上游会因为拿不到值而报错，这里只守判据本身
	if (siteToken{Key: ""}).masked() != false {
		t.Error("空值不该判为打码")
	}
}

func TestMaskConfigMasksSiteAPIKey(t *testing.T) {
	// 站点 API Key 能直接调用付费接口，比签到 cookie 的滥用面更大。
	// 漏打码就是明文下发浏览器 —— 池子 session 漏打码那个坑（606d56e）刚修过
	cfg := DefaultConfig()
	cfg.Accounts = []Account{{
		Name: "A", URL: "https://a.com", Cookie: "c", APIKey: "sk-realsecret",
	}}
	m := MaskConfig(&cfg)
	if m.Accounts[0].APIKey != MaskPlaceholder {
		t.Errorf("api_key 未打码: %q", m.Accounts[0].APIKey)
	}
	if cfg.Accounts[0].APIKey != "sk-realsecret" {
		t.Error("MaskConfig 改动了原配置对象（非深拷贝）")
	}
}

func TestUnmaskConfigRestoresSiteAPIKeyByName(t *testing.T) {
	old := DefaultConfig()
	old.Accounts = []Account{
		{Name: "A", URL: "https://a.com", Cookie: "ca", APIKey: "sk-aaa"},
		{Name: "B", URL: "https://b.com", Cookie: "cb", APIKey: "sk-bbb"},
	}
	// 前端把打码值原样回传，且顺序与库里相反 —— 按下标还原会串号
	in := DefaultConfig()
	in.Accounts = []Account{
		{Name: "B", URL: "https://b.com", Cookie: MaskPlaceholder, APIKey: MaskPlaceholder},
		{Name: "A", URL: "https://a.com", Cookie: MaskPlaceholder, APIKey: MaskPlaceholder},
	}
	out, err := UnmaskConfig(&in, &old)
	if err != nil {
		t.Fatalf("UnmaskConfig() error = %v", err)
	}
	if out.Accounts[0].APIKey != "sk-bbb" || out.Accounts[1].APIKey != "sk-aaa" {
		t.Errorf("api_key 按名字还原错位: %q / %q",
			out.Accounts[0].APIKey, out.Accounts[1].APIKey)
	}
}

func TestUnmaskConfigLeavesAPIKeyEmptyWhenAccountIsNew(t *testing.T) {
	// 刻意比 cookie 宽松：API Key 不是签到必需品，为它整份 PUT 失败不划算。
	// 留空之后重跑一次 POST /api/sites/apikeys 就能补回来
	old := DefaultConfig()
	old.Accounts = []Account{{Name: "A", URL: "https://a.com", Cookie: "ca", APIKey: "sk-aaa"}}
	in := DefaultConfig()
	in.Accounts = []Account{{Name: "NEW", URL: "https://n.com", Cookie: "cn", APIKey: MaskPlaceholder}}

	out, err := UnmaskConfig(&in, &old)
	if err != nil {
		t.Fatalf("新账号的 api_key 占位符不该让整份还原失败: %v", err)
	}
	if out.Accounts[0].APIKey != "" {
		t.Errorf("api_key = %q, want 空（找不到旧值就留空，不能把 *** 存进去）",
			out.Accounts[0].APIKey)
	}
}

func TestMaskAPIKeyForDisplayKeepsEnoughToIdentify(t *testing.T) {
	got := maskAPIKeyForDisplay("sk-abcdefghijklmnop")
	if strings.Contains(got, "abcdefghij") {
		t.Errorf("脱敏后仍能看出中段: %q", got)
	}
	if !strings.HasPrefix(got, "sk-abc") || !strings.HasSuffix(got, "mnop") {
		t.Errorf("头尾应保留以便核对: %q", got)
	}
	// 太短的值留几位就等于泄露，一律全打码
	for _, short := range []string{"sk-abc", "sk-", "x"} {
		masked := maskAPIKeyForDisplay(short)
		if strings.Trim(masked, "*") != "" {
			t.Errorf("maskAPIKeyForDisplay(%q) = %q，短值应全打码", short, masked)
		}
	}
}
