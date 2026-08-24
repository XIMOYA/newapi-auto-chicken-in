/*
server/account_query.go
按账号查询配置：GET /api/accounts/{name}（脱敏摘要）与 GET /api/accounts/{name}/raw（明文）

为什么需要这两个端点：
  - 回写端点 POST /api/accounts/{name}/refresh-cookie 只能告诉调用方「服务端说它收下了」。
    要堵掉「以为写成功了其实没写」这个故障模式，就得能再拉一次读回核实：
    明文切片给 Python 客户端做精确比对，脱敏摘要给网页端做人工核实。
  - 旧代次一旦被站点判重放，整条会话会被 AUTH_SESSION_REVOKED 报废，
    所以「平台收了但没存」必须当场发现，不能等到下一轮保活撞上去才知道。

鉴权分级沿用既有约定，没有自创：
  - 脱敏读双认证（同 GET /api/config）：cookie 一律不出明文，只给核实摘要
  - 明文读只认 API Key（同 GET /api/config/raw）：明文暴露面不新增 ——
    API Key 持有者本来就能拉整份明文，这里只是按账号切一片
*/
package main

import (
	"crypto/sha256"
	"encoding/hex"
	"net/http"
	"strings"
)

// cookieFingerprintLength 指纹取 sha256 十六进制的前多少位。
// 12 位（48 bit）足够人眼比对「代次换了没换」，又短到能整条贴进工单里；
// 它不是防碰撞用的哈希，只是给同一个值做个稳定标签。
const cookieFingerprintLength = 12

// accountCookieDigest cookie 的核实摘要：不含任何明文，但足够判断两边是不是同一代。
//
// HasRefresh 单独给出来的理由：new_api_refresh 这个键只有源站会下发，
// 长度和指纹对得上但键没了，说明库里那条压根不是可用凭据。
type accountCookieDigest struct {
	Fingerprint string `json:"fingerprint"`
	Length      int    `json:"length"`
	HasRefresh  bool   `json:"has_refresh"`
}

// cookieDigestOf 算摘要；空 cookie 返回空值形态（指纹空串、长度 0、has_refresh false）。
func cookieDigestOf(cookie string) accountCookieDigest {
	if cookie == "" {
		return accountCookieDigest{}
	}
	sum := sha256.Sum256([]byte(cookie))
	return accountCookieDigest{
		Fingerprint: hex.EncodeToString(sum[:])[:cookieFingerprintLength],
		Length:      len(cookie),
		// 复用 cookie 检测那边的键常量（含等号），与 normalizeTabiAIRefreshCookie
		// 判断「这条值里带没带键」的口径保持一致
		HasRefresh: strings.Contains(cookie, cookieTestRefreshTokenKey),
	}
}

// lookupAccountByPath 按路径参数定位账号，顺带把配置的更新时间带出来。
// 返回 ok=false 时响应已经写完，调用方直接 return。
//
// 匹配规则与 handleWriteBackRefreshCookie 严格一致：路径参数 trim 后与 accounts[].name
// 精确比较（大小写敏感，不 trim 库里的名字）。两边必须同一套规则 —— 否则会出现
// 「回写找得到、核实找不到」这种最难查的不一致，客户端会把成功的回写误判成失败。
func (s *Server) lookupAccountByPath(w http.ResponseWriter, r *http.Request) (Account, string, bool) {
	name := strings.TrimSpace(r.PathValue("name"))
	if name == "" {
		writeError(w, http.StatusBadRequest, "账号名不能为空")
		return Account{}, "", false
	}
	cfg, updatedAt, err := LoadConfig(s.db)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "服务器内部错误")
		return Account{}, "", false
	}
	for i := range cfg.Accounts {
		if cfg.Accounts[i].Name == name {
			return cfg.Accounts[i], updatedAt, true
		}
	}
	writeError(w, http.StatusNotFound, "账号不存在: "+name)
	return Account{}, "", false
}

// handleGetAccount GET /api/accounts/{name}（JWT 或 API Key）
// 单个账号的脱敏配置 + cookie 核实摘要。给网页端人工核对回写结果用。
//
// updated_at 是整份配置的更新时间（库里只有这一个时间戳，没有按账号的）。
// 凭据轮转走 saveConfigLockedKeepRevision，它不推进 revision 但**会**更新 updated_at，
// 所以这个值恰好能反映「最近一次回写是什么时候落库的」。
func (s *Server) handleGetAccount(w http.ResponseWriter, r *http.Request) {
	account, updatedAt, ok := s.lookupAccountByPath(w, r)
	if !ok {
		return
	}
	// 打码复用 MaskConfig：把这一条塞进临时 Config 走同一套规则，
	// 免得这里自己列一遍敏感字段、以后加字段时漏掉一个就把明文漏出去
	masked := MaskConfig(&Config{Accounts: []Account{account}})
	body := map[string]any{
		"account":       masked.Accounts[0],
		"cookie_digest": cookieDigestOf(account.Cookie),
	}
	if updatedAt != "" {
		body["updated_at"] = updatedAt
	}
	writeJSON(w, http.StatusOK, body)
}

// handleGetAccountRaw GET /api/accounts/{name}/raw（API Key）
// 直接返回该账号的明文配置对象（非包裹结构，与 GET /api/config/raw 的风格一致）。
// 客户端回写凭据后拉它做精确比对：库里存的到底是不是刚写进去的那一代。
func (s *Server) handleGetAccountRaw(w http.ResponseWriter, r *http.Request) {
	account, _, ok := s.lookupAccountByPath(w, r)
	if !ok {
		return
	}
	writeJSON(w, http.StatusOK, account)
}
