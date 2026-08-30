/*
server/github_accounts_test.go
统一 GitHub 账号：名字生成规则、幂等判定、凭据解析优先级、改名计划
*/
package main

import "testing"

func TestComposeAccountName(t *testing.T) {
	cases := []struct{ gh, url, want string }{
		{"Steven", "https://tabiai.cc", "Steven（tabiai.cc）"},
		{"Steven", "https://tabiai.cc/profile", "Steven（tabiai.cc）"}, // 只取域名，路径无关
		{"Steven", "http://a.com:8080", "Steven（a.com:8080）"},        // 端口保留，它是出口的一部分
		{" Steven ", "https://a.com", "Steven（a.com）"},               // 两侧空白剔掉
		{"Steven", "", ""},        // 缺 URL：不造残缺名字
		{"", "https://a.com", ""}, // 缺 GitHub 名同理
		{"Steven", "不是URL", ""},
	}
	for _, c := range cases {
		if got := composeAccountName(c.gh, c.url); got != c.want {
			t.Errorf("composeAccountName(%q,%q) = %q, want %q", c.gh, c.url, got, c.want)
		}
	}
}

func TestIsComposedAccountName(t *testing.T) {
	yes := []string{"Steven（tabiai.cc）", "a（b）"}
	no := []string{"Steven", "（tabiai.cc）", "Steven（）", "Steven(tabiai.cc)", ""}
	for _, s := range yes {
		if !isComposedAccountName(s) {
			t.Errorf("%q 应判为规范名", s)
		}
	}
	for _, s := range no {
		if isComposedAccountName(s) {
			t.Errorf("%q 不该判为规范名", s)
		}
	}
}

func TestResolveAccountSessionPrefersPool(t *testing.T) {
	cfg := &Config{
		GitHubAccounts: []GitHubAccount{{Name: "Steven", UserSession: "pool-sess", ClientID: "pool-cid"}},
	}
	// 引用了池子：用池子的，账号里的旧值被忽略
	s, c := resolveAccountSession(cfg, Account{
		GitHubAccount: "Steven", GithubUserSession: "old-sess", GithubClientID: "old-cid"})
	if s != "pool-sess" || c != "pool-cid" {
		t.Fatalf("应优先用池子的凭据，实际 %q/%q", s, c)
	}

	// 没引用池子：回落账号自己的旧字段（迁移期间不断供）
	s, c = resolveAccountSession(cfg, Account{GithubUserSession: "old-sess", GithubClientID: "old-cid"})
	if s != "old-sess" || c != "old-cid" {
		t.Fatalf("应回落旧字段，实际 %q/%q", s, c)
	}

	// 引用了但池子里那条没填 session：仍回落旧字段，不能把签发链路断掉
	cfg.GitHubAccounts[0].UserSession = ""
	if s, _ = resolveAccountSession(cfg, Account{
		GitHubAccount: "Steven", GithubUserSession: "old-sess"}); s != "old-sess" {
		t.Fatalf("池子为空时应回落旧字段，实际 %q", s)
	}
}

func TestPlanAccountRenames(t *testing.T) {
	cfg := &Config{Accounts: []Account{
		{Name: "旧名A", URL: "https://a.com", GitHubAccount: "Steven"},
		{Name: "Steven（b.com）", URL: "https://b.com", GitHubAccount: "Steven"}, // 已规范，跳过
		{Name: "没引用", URL: "https://c.com"},                                    // 没引用 GitHub 账号，跳过
		{Name: "缺URL", GitHubAccount: "Steven"},                                // 拼不出名字，跳过
	}}
	renames, err := planAccountRenames(cfg)
	if err != nil {
		t.Fatalf("不该报错: %v", err)
	}
	if len(renames) != 1 || renames["旧名A"] != "Steven（a.com）" {
		t.Fatalf("改名计划 = %v", renames)
	}
}

func TestValidateConfigChecksPoolNames(t *testing.T) {
	// 名字是引用键：重名时 UnmaskConfig 建的 map 留最后一条、findGitHubAccount
	// 返回第一条，用户会看到「改了 session 但签发还用旧的」，且全程没有报错。
	// ops 端点自己挡住了这两种输入，但整份 PUT /api/config 绕过它，只能在这里兜住。
	cases := []struct {
		name string
		pool []GitHubAccount
		bad  bool
	}{
		{"正常", []GitHubAccount{{Name: "A", UserSession: "sa"}, {Name: "B"}}, false},
		{"空池子", nil, false},
		{"空名字", []GitHubAccount{{Name: "", UserSession: "sa"}}, true},
		{"名字只有空格", []GitHubAccount{{Name: "   "}}, true},
		{"重名", []GitHubAccount{{Name: "A", UserSession: "s1"}, {Name: "A", UserSession: "s2"}}, true},
		// 名字两侧空白在比对时剔掉，否则 "A" 和 "A " 会被当成两条而实际引用同一个键
		{"空白差异也算重名", []GitHubAccount{{Name: "A"}, {Name: "A "}}, true},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			cfg := DefaultConfig()
			cfg.GitHubAccounts = c.pool
			err := ValidateConfig(&cfg)
			if c.bad && err == nil {
				t.Fatal("应报错")
			}
			if !c.bad && err != nil {
				t.Fatalf("不该报错: %v", err)
			}
		})
	}
}

func TestPlanAccountRenamesRejectsCollision(t *testing.T) {
	// 同一个 GitHub 账号 + 同一域名 = 重名。报错而不是加序号：
	// 用户确认过这种情况不该存在，静默加序号只会把配置问题藏起来
	cfg := &Config{Accounts: []Account{
		{Name: "x", URL: "https://a.com", GitHubAccount: "Steven"},
		{Name: "y", URL: "https://a.com/other", GitHubAccount: "Steven"},
	}}
	if _, err := planAccountRenames(cfg); err == nil {
		t.Fatal("重名应报错")
	}
}
