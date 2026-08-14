/*
server/envfile_test.go
.env 文件加载逻辑测试
*/
package main

import (
	"os"
	"path/filepath"
	"testing"
)

func writeTempEnv(t *testing.T, content string) string {
	t.Helper()
	dir := t.TempDir()
	p := filepath.Join(dir, ".env")
	if err := os.WriteFile(p, []byte(content), 0o600); err != nil {
		t.Fatalf("写入 .env 失败: %v", err)
	}
	return p
}

func TestLoadEnvFile_Basic(t *testing.T) {
	p := writeTempEnv(t, "NCF_JWT_SECRET=abc123\nNCF_ADMIN_USER=myadmin\n")
	// 清理可能存在的同名变量，确保测试干净
	os.Unsetenv("NCF_JWT_SECRET")
	os.Unsetenv("NCF_ADMIN_USER")

	loadEnvFile(p)

	if got := os.Getenv("NCF_JWT_SECRET"); got != "abc123" {
		t.Errorf("NCF_JWT_SECRET = %q, want abc123", got)
	}
	if got := os.Getenv("NCF_ADMIN_USER"); got != "myadmin" {
		t.Errorf("NCF_ADMIN_USER = %q, want myadmin", got)
	}
}

func TestLoadEnvFile_CommentsAndBlank(t *testing.T) {
	p := writeTempEnv(t, "# 注释\n\n  \nNCF_HTTP_ADDR=0.0.0.0:9090\n")
	os.Unsetenv("NCF_HTTP_ADDR")

	loadEnvFile(p)

	if got := os.Getenv("NCF_HTTP_ADDR"); got != "0.0.0.0:9090" {
		t.Errorf("NCF_HTTP_ADDR = %q, want 0.0.0.0:9090", got)
	}
}

func TestLoadEnvFile_QuotedValue(t *testing.T) {
	p := writeTempEnv(t, `NCF_ADMIN_PASS="pa ss'word"`+"\n")
	os.Unsetenv("NCF_ADMIN_PASS")

	loadEnvFile(p)

	if got := os.Getenv("NCF_ADMIN_PASS"); got != "pa ss'word" {
		t.Errorf("NCF_ADMIN_PASS = %q, want pa ss'word", got)
	}
}

func TestLoadEnvFile_DoesNotOverrideExisting(t *testing.T) {
	p := writeTempEnv(t, "NCF_ADMIN_USER=from_env_file\n")
	t.Setenv("NCF_ADMIN_USER", "from_system")

	loadEnvFile(p)

	if got := os.Getenv("NCF_ADMIN_USER"); got != "from_system" {
		t.Errorf("NCF_ADMIN_USER = %q, want from_system（系统环境变量优先）", got)
	}
}

func TestLoadEnvFile_Nonexistent(t *testing.T) {
	// 不存在的文件不应 panic，也不应报错
	loadEnvFile(filepath.Join(t.TempDir(), "not-exist.env"))
}
