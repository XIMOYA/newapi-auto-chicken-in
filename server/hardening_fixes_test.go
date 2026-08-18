/*
server/hardening_fixes_test.go
审计发现 H-3 / M-5 / L-1 / M-4 的回归测试

这些断言存在的原因是具体的安全缺口，放宽它们等于把漏洞放回来：
- H-3：NCF_ENV 拼错会静默走非生产分支，那里以前不校验密钥长度
- M-5：初始管理员密码只判非空，与改密码的 8 位要求自相矛盾
- L-1：登录退避被 60 秒统计窗口截断，指数退避形同虚设
- M-4：代理源响应体无上限，上游异常即可打到 OOM
*/
package main

import (
	"fmt"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// ---------------------------------------------------------------------------
// H-3：JWT 密钥强度对所有环境生效
// ---------------------------------------------------------------------------

func TestResolveJWTSecretRejectsWeakInAllEnvs(t *testing.T) {
	strong := strings.Repeat("k", minJWTSecretLen)
	cases := []struct {
		name    string
		env     string
		secret  string
		wantErr bool
		// wantRandom：期望自动生成随机密钥（长度为 64 的 hex）
		wantRandom bool
	}{
		{name: "生产 + 强密钥", env: "production", secret: strong},
		{name: "生产 + 弱密钥", env: "production", secret: "short", wantErr: true},
		{name: "生产 + 空密钥", env: "production", secret: "", wantErr: true},
		// 下面这些 env 值都不等于 "production"，会走非生产分支 —— 正是运维容易写错的形式
		{name: "env 写成 prod + 弱密钥", env: "prod", secret: "secret", wantErr: true},
		{name: "env 写成 Production_1 + 弱密钥", env: "Production_1", secret: "changeme", wantErr: true},
		{name: "env 为空 + 弱密钥", env: "", secret: "123456", wantErr: true},
		{name: "env 写成 prod + 强密钥", env: "prod", secret: strong},
		{name: "env 为空 + 不设密钥则随机生成", env: "", secret: "", wantRandom: true},
		{name: "大小写与空白不影响生产判定", env: "  PRODUCTION  ", secret: strong},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, err := resolveJWTSecret(tc.env, tc.secret)
			if tc.wantErr {
				if err == nil {
					t.Fatalf("应拒绝弱密钥，却通过了（secret 长度 %d）", len(tc.secret))
				}
				return
			}
			if err != nil {
				t.Fatalf("不该报错: %v", err)
			}
			if tc.wantRandom {
				if len(got) != 64 {
					t.Fatalf("自动生成的密钥长度 = %d，期望 64（32 字节 hex）", len(got))
				}
				return
			}
			if got != tc.secret {
				t.Fatalf("应原样返回配置的密钥，实际 %q", got)
			}
		})
	}
}

func TestResolveJWTSecretBoundary(t *testing.T) {
	// 边界：正好 minJWTSecretLen 通过，少一个字符就拒绝
	if _, err := resolveJWTSecret("", strings.Repeat("x", minJWTSecretLen)); err != nil {
		t.Fatalf("恰好 %d 字符应通过: %v", minJWTSecretLen, err)
	}
	if _, err := resolveJWTSecret("", strings.Repeat("x", minJWTSecretLen-1)); err == nil {
		t.Fatalf("%d 字符应被拒绝", minJWTSecretLen-1)
	}
}

// ---------------------------------------------------------------------------
// M-5：初始管理员密码强度与改密码规则一致
// ---------------------------------------------------------------------------

func TestEnsureAdminRejectsWeakPassword(t *testing.T) {
	db, err := OpenDB(filepath.Join(t.TempDir(), "admin.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()

	if err := EnsureAdmin(db, "admin", "1234"); err == nil {
		t.Fatal("4 位密码应被拒绝（与改密码的 8 位要求保持一致）")
	}
	// 拒绝后不该留下半个账号
	if u, err := GetUserByUsername(db, "admin"); err != nil || u != nil {
		t.Fatalf("失败的初始化不该建账号: user=%v err=%v", u, err)
	}
	if err := EnsureAdmin(db, "admin", strings.Repeat("p", minPasswordLen)); err != nil {
		t.Fatalf("达标密码应通过: %v", err)
	}
}

func TestEnsureAdminDoesNotOverwriteExisting(t *testing.T) {
	// 升级路径：users 表已有账号时，环境变量不该能重置密码（否则是提权面）
	db, err := OpenDB(filepath.Join(t.TempDir(), "admin.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()

	if err := EnsureAdmin(db, "admin", "original-pass"); err != nil {
		t.Fatal(err)
	}
	if err := EnsureAdmin(db, "admin", "attacker-pass"); err != nil {
		t.Fatalf("已有账号时应静默跳过: %v", err)
	}
	user, err := GetUserByUsername(db, "admin")
	if err != nil || user == nil {
		t.Fatalf("账号应存在: %v", err)
	}
	if !CheckPassword(user.PasswordHash, "original-pass") {
		t.Fatal("原密码被环境变量覆盖了")
	}
	if CheckPassword(user.PasswordHash, "attacker-pass") {
		t.Fatal("新传入的密码生效了，存在通过 env 重置管理员密码的提权面")
	}
}

func TestChangePasswordEnforcesSameMinLength(t *testing.T) {
	srv := newTestServer(t)
	token := loginToken(t, srv)
	short := strings.Repeat("a", minPasswordLen-1)
	rr := doReq(t, srv, http.MethodPut, "/api/password", token, map[string]string{
		"old_password": "admin123456",
		"new_password": short,
	})
	if rr.Code != http.StatusBadRequest {
		t.Fatalf("过短的新密码应 400，实际 %d", rr.Code)
	}
	if !strings.Contains(rr.Body.String(), fmt.Sprint(minPasswordLen)) {
		t.Errorf("错误信息应说明长度要求，实际: %s", rr.Body.String())
	}
}

// ---------------------------------------------------------------------------
// L-1：登录退避不被统计窗口重置截断
// ---------------------------------------------------------------------------

func TestLoginBackoffSurvivesWindowExpiry(t *testing.T) {
	lim := newLoginLimiter()
	base := time.Now()
	clock := base
	lim.now = func() time.Time { return clock }

	// 连续失败到触发退避
	for i := 0; i < loginMaxFailures; i++ {
		lim.recordFailure("ip|user")
	}
	if _, allowed := lim.check("ip|user"); allowed {
		t.Fatal("达到阈值后应进入退避")
	}

	// 把时间推到「统计窗口已过期、但退避还没结束」的位置。
	// 修复前这里会因为窗口过期直接放行，让指数退避形同虚设。
	clock = base.Add(loginWindow + time.Second)
	lim.recordFailure("ip|user") // 重新计数会刷新 firstFail，但封禁不该被抹掉

	for i := 0; i < loginMaxFailures-1; i++ {
		lim.recordFailure("ip|user")
	}
	retryAfter, allowed := lim.check("ip|user")
	if allowed {
		t.Fatal("重新达到阈值后应再次进入退避")
	}
	if retryAfter <= 0 {
		t.Fatalf("应返回剩余等待时长，实际 %v", retryAfter)
	}

	// 退避结束后才放行
	clock = clock.Add(loginMaxBackoff + time.Second)
	if _, allowed := lim.check("ip|user"); !allowed {
		t.Fatal("退避到期后应放行")
	}
}

func TestLoginBackoffClearedOnSuccess(t *testing.T) {
	lim := newLoginLimiter()
	for i := 0; i < loginMaxFailures; i++ {
		lim.recordFailure("ip|user")
	}
	lim.recordSuccess("ip|user")
	if _, allowed := lim.check("ip|user"); !allowed {
		t.Fatal("登录成功后应清除退避")
	}
}

// ---------------------------------------------------------------------------
// M-4：代理源体积上限与 enabled 开关
// ---------------------------------------------------------------------------

func TestProxySourceBodyIsCapped(t *testing.T) {
	// 上游返回远超上限的响应：必须截断读取而不是全塞进内存
	var served int64
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		line := []byte("1.2.3.4:8080\n")
		for served < maxProxySourceBytes*3 {
			n, err := w.Write(line)
			served += int64(n)
			if err != nil {
				return
			}
		}
	}))
	defer srv.Close()

	got := fetchSourceOnce(srv.URL, 30)
	if len(got) == 0 {
		t.Fatal("截断后仍应解析出可用代理，而不是整体放弃")
	}
	// 每行 13 字节，上限内最多这么多行；留一行余量避免边界抖动
	maxLines := int(maxProxySourceBytes/13) + 1
	if len(got) > maxLines {
		t.Fatalf("解析出 %d 条，超过 %d MiB 上限对应的 %d 条，说明没有截断",
			len(got), maxProxySourceBytes>>20, maxLines)
	}
}

func TestProxySourceNormalResponseUnaffected(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte("1.1.1.1:80\n2.2.2.2:8080\n"))
	}))
	defer srv.Close()

	got := fetchSourceOnce(srv.URL, 30)
	if len(got) != 2 {
		t.Fatalf("正常响应应解析出 2 条，实际 %d: %v", len(got), got)
	}
}
