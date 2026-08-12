/*
server/embed.go
NewAPI 签到配置管理平台 · 前端静态资源嵌入

把 web/dist 的构建产物嵌入 Go 二进制，实现「单文件 = 前端 + 后端」。
embed_dist 目录由构建脚本（scripts/build-config-platform.sh）在 go build 前
生成；仓库里保留 .gitkeep 占位，保证未构建前端时也能编译通过。
运行时静态服务优先级：外部 web/dist（开发热更新）> 嵌入的 embed_dist。
*/
package main

import "embed"

//go:embed all:embed_dist
var embeddedDist embed.FS

// hasEmbeddedFrontend 判断二进制内是否嵌入了前端（index.html 存在）。
func hasEmbeddedFrontend() bool {
	_, err := embeddedDist.ReadFile("embed_dist/index.html")
	return err == nil
}
