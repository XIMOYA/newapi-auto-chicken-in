/*
server/github_fingerprint.go
每个 GitHub 账号对外呈现的固定客户端指纹。

为什么要这个：GitHub 的 user_session 是绑「设备特征」的。同一条 session 忽然换了
User-Agent、或者 UA 说自己是 Chrome 却不发 sec-ch-ua，都会被判成异常会话，最坏直接
作废 session —— 自救链路当场断掉。改造前全平台共用一个硬编码 UA
（cookie_checker.go:43），几个账号在 GitHub 眼里像同一台机器上的同一个浏览器，
一旦其中一个被盯上，其余的特征完全一致。

所以：一个账号一份指纹，从它自己的 seed 确定性派生，seed 落库因此永不漂移。

关键约束是**内部自洽**，不是「随机就好」：
  - UA 里的 Chrome 大版本必须与 sec-ch-ua 里的版本完全一致，差一个数字就是伪造特征
  - Firefox 压根不发 sec-ch-ua 那一族头（那是 Chromium 的东西），给它配上反而更可疑
  - 平台要对得上：UA 写 Macintosh 而 sec-ch-ua-platform 报 "Windows" 同理

因此这里用「整档模板」而不是逐字段随机组合 —— 逐字段随机必然产出不可能的组合。
*/
package main

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"strings"
)

// githubFingerprint 一次请求要带的一整套客户端特征。
// SecChUA 为空表示这一档不发 sec-ch-ua 族（Firefox），调用方据此跳过。
type githubFingerprint struct {
	UserAgent       string
	AcceptLanguage  string
	SecChUA         string
	SecChUAMobile   string
	SecChUAPlatform string
}

// fingerprintProfile 一档完整自洽的浏览器画像。
type fingerprintProfile struct {
	// uaTemplate 里的 %s 会被同一个大版本号填进去，保证 UA 与 sec-ch-ua 同版本
	uaTemplate string
	// secChUATemplate 同样吃那个版本号；留空表示这一档不发 sec-ch-ua 族
	secChUATemplate string
	platform        string
	// versions 近期的大版本号。挑一个固定用，不随时间漂 ——
	// 版本号自己会跳变也是一种异常特征
	versions []string
}

// fingerprintProfiles 可选画像。都是桌面档：这些账号的用途是网页登录态，
// 报移动端 UA 反而会让 GitHub 的页面走移动版流程，多一层不必要的差异。
var fingerprintProfiles = []fingerprintProfile{
	{
		uaTemplate: "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
			"(KHTML, like Gecko) Chrome/%s.0.0.0 Safari/537.36",
		secChUATemplate: `"Chromium";v="%s", "Not(A:Brand";v="24", "Google Chrome";v="%s"`,
		platform:        `"Windows"`,
		versions:        []string{"140", "141", "142"},
	},
	{
		uaTemplate: "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 " +
			"(KHTML, like Gecko) Chrome/%s.0.0.0 Safari/537.36",
		secChUATemplate: `"Chromium";v="%s", "Not(A:Brand";v="24", "Google Chrome";v="%s"`,
		platform:        `"macOS"`,
		versions:        []string{"140", "141", "142"},
	},
	{
		uaTemplate: "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
			"(KHTML, like Gecko) Chrome/%s.0.0.0 Safari/537.36 Edg/%s.0.0.0",
		secChUATemplate: `"Chromium";v="%s", "Not(A:Brand";v="24", "Microsoft Edge";v="%s"`,
		platform:        `"Windows"`,
		versions:        []string{"140", "141", "142"},
	},
	{
		// Firefox：刻意不配 secChUATemplate。它不实现 Client Hints，
		// 带上那族头就是自证伪造
		uaTemplate: "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:%s.0) " +
			"Gecko/20100101 Firefox/%s.0",
		platform: `"Windows"`,
		versions: []string{"132", "133", "134"},
	},
}

// fingerprintLanguages 可选的语言偏好。跟着画像一起固定下来 ——
// 同一条 session 今天报中文明天报英文，本身就是异常。
var fingerprintLanguages = []string{
	"zh-CN,zh;q=0.9,en;q=0.8",
	"zh-CN,zh;q=0.9",
	"en-US,en;q=0.9",
	"zh-TW,zh;q=0.9,en;q=0.8",
}

// deriveGitHubFingerprint 由 seed 确定性派生一套指纹。
//
// 同一个 seed 永远得到同一套结果 —— 这是整件事的前提：进程重启、配置改动、
// 甚至换一台机器部署，这个账号在 GitHub 眼里都还是那台设备。
//
// seed 为空时返回零值，调用方应回落到全局默认 UA（老配置还没补 seed 的迁移期）。
func deriveGitHubFingerprint(seed string) githubFingerprint {
	if strings.TrimSpace(seed) == "" {
		return githubFingerprint{}
	}
	sum := sha256.Sum256([]byte(seed))
	profile := fingerprintProfiles[int(sum[0])%len(fingerprintProfiles)]
	version := profile.versions[int(sum[1])%len(profile.versions)]
	lang := fingerprintLanguages[int(sum[2])%len(fingerprintLanguages)]

	fp := githubFingerprint{
		UserAgent:       fillFingerprintTemplate(profile.uaTemplate, version),
		AcceptLanguage:  lang,
		SecChUAPlatform: profile.platform,
		SecChUAMobile:   "?0", // 全是桌面档
	}
	if profile.secChUATemplate != "" {
		fp.SecChUA = fillFingerprintTemplate(profile.secChUATemplate, version)
	} else {
		// Firefox 档：连 platform/mobile 也不发，Client Hints 是整族一起有或一起没有
		fp.SecChUAPlatform = ""
		fp.SecChUAMobile = ""
	}
	return fp
}

// fillFingerprintTemplate 把模板里所有 %s 都填成同一个版本号。
// 模板里出现几次不确定（Edge 的 UA 有两处、sec-ch-ua 也有两处），
// 用 strings.Count 数出来再填，避免 Sprintf 参数个数对不上。
func fillFingerprintTemplate(template, version string) string {
	n := strings.Count(template, "%s")
	args := make([]any, n)
	for i := range args {
		args[i] = version
	}
	return fmt.Sprintf(template, args...)
}

// newFingerprintSeed 生成一个新 seed。
//
// 由账号名加一个固定盐派生而不是取随机数：这样即使某次迁移漏了落库、或者用户
// 手工清空了这个字段，重新算出来的还是同一套指纹，不会因为一次意外就换设备。
// 盐的作用只是让 seed 不等于账号名本身（避免有人靠账号名反推指纹）。
func newFingerprintSeed(accountName string) string {
	sum := sha256.Sum256([]byte("newapi-checkin-github-fp/v1/" + strings.TrimSpace(accountName)))
	return hex.EncodeToString(sum[:])[:32]
}

// applyGitHubFingerprint 把指纹写进请求头。
//
// seed 缺失（零值指纹）时什么都不做，调用方之前设的默认 UA 保持不变 ——
// 迁移期的账号宁可继续用老的全局 UA，也不要突然变成「没有 UA」。
func applyGitHubFingerprint(header map[string][]string, fp githubFingerprint) {
	set := func(key, value string) {
		if value != "" {
			header[key] = []string{value}
		}
	}
	if fp.UserAgent == "" {
		return
	}
	set("User-Agent", fp.UserAgent)
	set("Accept-Language", fp.AcceptLanguage)
	set("Sec-Ch-Ua", fp.SecChUA)
	set("Sec-Ch-Ua-Mobile", fp.SecChUAMobile)
	set("Sec-Ch-Ua-Platform", fp.SecChUAPlatform)
}
