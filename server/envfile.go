/*
server/envfile.go
.env 文件加载（零依赖轻量实现）

职责：
- 启动时自动加载 .env 文件，把 KEY=VALUE 写入进程环境变量
- 方便宝塔/手动部署：在程序目录放 .env 即可配置，无需 export
- 规则：
  - 每行 KEY=VALUE，支持 # 注释与空行
  - VALUE 支持双引号 / 单引号包裹（自动去除包裹引号）
  - 不覆盖已存在的环境变量（.env 作为默认值）
  - 查找顺序：NCF_ENV_FILE 显式指定 → 当前工作目录 .env → 可执行文件同目录 .env
*/
package main

import (
	"bufio"
	"log"
	"os"
	"path/filepath"
	"strings"
)

// findEnvFile 定位要加载的 .env 文件；找不到返回空字符串。
func findEnvFile() string {
	if p := os.Getenv("NCF_ENV_FILE"); p != "" {
		return p
	}
	if _, err := os.Stat(".env"); err == nil {
		return ".env"
	}
	if exe, err := os.Executable(); err == nil {
		p := filepath.Join(filepath.Dir(exe), ".env")
		if _, err := os.Stat(p); err == nil {
			return p
		}
	}
	return ""
}

// loadEnvFile 解析并加载 .env 文件到进程环境变量（不覆盖已存在变量）。
func loadEnvFile(path string) {
	f, err := os.Open(path)
	if err != nil {
		log.Printf("跳过环境文件（无法读取 %s）: %v", path, err)
		return
	}
	defer f.Close()

	scanner := bufio.NewScanner(f)
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		key, value, ok := strings.Cut(line, "=")
		if !ok {
			continue
		}
		key = strings.TrimSpace(key)
		value = strings.TrimSpace(value)
		if key == "" {
			continue
		}
		if len(value) >= 2 {
			if (value[0] == '"' && value[len(value)-1] == '"') ||
				(value[0] == '\'' && value[len(value)-1] == '\'') {
				value = value[1 : len(value)-1]
			}
		}
		if _, exists := os.LookupEnv(key); exists {
			continue
		}
		os.Setenv(key, value)
	}
	log.Printf("已加载环境文件: %s", path)
}
