/*
server/github_fingerprint_test.go
固定客户端指纹的测试。

守的核心不是「随机得好不好」，而是**自洽**与**稳定**：
  - 稳定：同一个 seed 永远同一套。不稳定就等于每次请求都在换设备，
    比共用一个 UA 更容易触发 GitHub 的异常会话判定
  - 自洽：UA 里的大版本必须与 sec-ch-ua 里的一致；Firefox 不许带 Client Hints
    那一族头。差一个数字或多一个头都是自证伪造
*/
package main

import (
	"regexp"
	"strings"
	"testing"
)

func TestDeriveGitHubFingerprintIsStable(t *testing.T) {
	seed := newFingerprintSeed("Steven")
	first := deriveGitHubFingerprint(seed)
	if first.UserAgent == "" {
		t.Fatal("应派生出 UA")
	}
	// 反复算、以及「重启后重新算」都必须一模一样
	for i := 0; i < 5; i++ {
		again := deriveGitHubFingerprint(seed)
		if again != first {
			t.Fatalf("同一 seed 派生结果漂了:\n%+v\n%+v", first, again)
		}
	}
	// seed 由账号名派生，所以同名账号重算 seed 也能回到同一套指纹
	if deriveGitHubFingerprint(newFingerprintSeed("Steven")) != first {
		t.Error("同名账号重算 seed 后指纹变了")
	}
	// 不同账号必须是不同设备，否则等于没做隔离
	other := deriveGitHubFingerprint(newFingerprintSeed("Alice"))
	if other.UserAgent == first.UserAgent && other.AcceptLanguage == first.AcceptLanguage {
		t.Error("两个账号派生出了完全相同的指纹")
	}
}

func TestDeriveGitHubFingerprintEmptySeed(t *testing.T) {
	// 迁移期还没补 seed 的账号：返回零值，让调用方保持原有的全局默认 UA。
	// 绝不能返回一个「空 UA」，那比旧行为更糟
	for _, seed := range []string{"", "   "} {
		if fp := deriveGitHubFingerprint(seed); fp.UserAgent != "" {
			t.Errorf("空 seed 应给零值，实际 %+v", fp)
		}
	}
}

var chromeVersionRe = regexp.MustCompile(`Chrome/(\d+)\.`)
var edgeVersionRe = regexp.MustCompile(`Edg/(\d+)\.`)
var secChUAVersionRe = regexp.MustCompile(`"Chromium";v="(\d+)"`)

func TestFingerprintProfilesAreSelfConsistent(t *testing.T) {
	// 遍历足够多的 seed，把四档画像都覆盖到，逐档检查自洽性
	seen := map[string]bool{}
	for i := 0; i < 400; i++ {
		fp := deriveGitHubFingerprint(newFingerprintSeed(string(rune('A'+i%26)) + string(rune('a'+i/26))))
		if fp.UserAgent == "" {
			t.Fatal("派生失败")
		}
		isFirefox := strings.Contains(fp.UserAgent, "Firefox/")
		seen[map[bool]string{true: "firefox", false: "chromium"}[isFirefox]] = true

		if isFirefox {
			// Firefox 不实现 Client Hints，整族头都必须缺席
			if fp.SecChUA != "" || fp.SecChUAMobile != "" || fp.SecChUAPlatform != "" {
				t.Fatalf("Firefox 档不该带 Client Hints: %+v", fp)
			}
			continue
		}

		// Chromium 系：整族头都要有
		if fp.SecChUA == "" || fp.SecChUAMobile == "" || fp.SecChUAPlatform == "" {
			t.Fatalf("Chromium 档缺 Client Hints: %+v", fp)
		}
		// 版本必须完全一致 —— 这是最容易写错、也最容易被识破的一处
		uaVer := chromeVersionRe.FindStringSubmatch(fp.UserAgent)
		hintVer := secChUAVersionRe.FindStringSubmatch(fp.SecChUA)
		if uaVer == nil || hintVer == nil {
			t.Fatalf("取不出版本号: %+v", fp)
		}
		if uaVer[1] != hintVer[1] {
			t.Fatalf("UA 版本 %s 与 sec-ch-ua 版本 %s 不一致: %+v", uaVer[1], hintVer[1], fp)
		}
		// Edge 档：UA 里 Chrome 与 Edg 两个版本号也得一致
		if edge := edgeVersionRe.FindStringSubmatch(fp.UserAgent); edge != nil {
			if edge[1] != uaVer[1] {
				t.Fatalf("Edge 版本 %s 与 Chrome 版本 %s 不一致: %s", edge[1], uaVer[1], fp.UserAgent)
			}
			if !strings.Contains(fp.SecChUA, "Microsoft Edge") {
				t.Errorf("Edge 档的 sec-ch-ua 应报 Microsoft Edge: %s", fp.SecChUA)
			}
		}
		// 平台要对得上 UA
		if strings.Contains(fp.UserAgent, "Macintosh") && fp.SecChUAPlatform != `"macOS"` {
			t.Errorf("mac UA 配了 %s", fp.SecChUAPlatform)
		}
		if strings.Contains(fp.UserAgent, "Windows NT") && fp.SecChUAPlatform != `"Windows"` {
			t.Errorf("windows UA 配了 %s", fp.SecChUAPlatform)
		}
	}
	// 确认这轮真的把两大类都覆盖到了，否则上面的断言可能压根没跑到
	if !seen["firefox"] || !seen["chromium"] {
		t.Fatalf("样本没覆盖到全部画像: %v", seen)
	}
}

func TestApplyGitHubFingerprint(t *testing.T) {
	header := map[string][]string{"User-Agent": {"旧的全局 UA"}}
	// 零值指纹：一个头都不许动，让迁移期账号继续用原来的默认 UA
	applyGitHubFingerprint(header, githubFingerprint{})
	if header["User-Agent"][0] != "旧的全局 UA" {
		t.Fatalf("零值指纹不该改动请求头: %v", header)
	}

	fp := deriveGitHubFingerprint(newFingerprintSeed("Steven"))
	applyGitHubFingerprint(header, fp)
	if header["User-Agent"][0] != fp.UserAgent {
		t.Errorf("UA 未生效: %v", header["User-Agent"])
	}
	if header["Accept-Language"][0] != fp.AcceptLanguage {
		t.Errorf("Accept-Language 未生效: %v", header["Accept-Language"])
	}
	// Firefox 档不发 sec-ch-ua，此时不能往 header 里塞空值
	if fp.SecChUA == "" {
		if _, exists := header["Sec-Ch-Ua"]; exists {
			t.Error("这一档不该出现 Sec-Ch-Ua 头")
		}
	} else if header["Sec-Ch-Ua"][0] != fp.SecChUA {
		t.Errorf("Sec-Ch-Ua 未生效: %v", header["Sec-Ch-Ua"])
	}
}
