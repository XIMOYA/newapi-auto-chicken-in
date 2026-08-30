/*
server/tabiai_expired.go
查询 refresh 凭据已失效的 TaBiAI 账号：GET /api/tabiai/expired

为什么单独开这个端点：
  - 「哪些账号的 new_api_refresh 过期了」这个信息其实一直存在 tabiai_keepalive_state
    表里（保活每轮都会写），但只能从 GET /api/tabiai/keepalive 拿，而那个端点只认 JWT，
    脚本/客户端用 API Key 调不了。
  - 另一条路是 POST /api/cookie-tests/tabiai 现跑一轮检测再读 status，但那要真打站点、
    耗时几十秒，而且 CookieTestRunner 的结果只在内存里，服务一重启就没了。
  - 网页端的「一键签发失效账号」需要一个毫秒级、可随时查的名单，不该逼用户每次先跑检测。

判定口径（刻意从严）：

	只有 state=invalid 或 paused 才算「凭据失效」。proxy_issue / abnormal 一律不算 ——
	那是代理不通或网络异常，凭据本身可能是好的；把它们也当过期去签发，等于白白作废
	一条还能用的凭据（签发会换出全新 sid，旧的当场失效）。
*/
package main

import (
	"net/http"
	"strings"
)

// expiredTabiAIAccount 一个凭据失效的账号。不含任何凭据值。
type expiredTabiAIAccount struct {
	Name  string `json:"name"`
	State string `json:"state"`
	// Paused 保活因凭据失效暂停了它；比 state 更强的信号（改过凭据才会自动恢复）
	Paused    bool   `json:"paused"`
	Message   string `json:"message"`
	LastRunAt string `json:"last_run_at"`
	// HasUserSession 决定这个账号能不能自动签发。没填的只能人工粘贴新凭据，
	// 一并返回省得调用方再挨个查一遍账号详情
	HasUserSession bool `json:"has_user_session"`
}

// handleListExpiredTabiAI GET /api/tabiai/expired（JWT 或 API Key）
// 返回凭据已失效的 tabiai 账号名单。只读库里保活写下的判定，不触发任何检测。
//
// 双认证的理由：网页端「一键签发」要用，签到客户端/脚本也要用（它们只有 API Key）。
// 响应里没有任何凭据字段，所以给 API Key 不新增暴露面。
func (s *Server) handleListExpiredTabiAI(w http.ResponseWriter, r *http.Request) {
	cfg, _, err := LoadConfig(s.db)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "服务器内部错误")
		return
	}
	states, _, err := loadKeepaliveStates(s.db)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "服务器内部错误")
		return
	}

	items := make([]expiredTabiAIAccount, 0)
	checkedAt := ""
	for i := range cfg.Accounts {
		account := cfg.Accounts[i]
		// 只看 tabiai：别的登录方式压根没有 new_api_refresh 这回事
		if account.LoginMethod != LoginMethodTabiAI {
			continue
		}
		row, ok := states[account.Name]
		if !ok {
			// 没有保活记录 = 还没被刷过，不能据此断定失效
			continue
		}
		if !isExpiredKeepaliveState(row) {
			continue
		}
		// 能不能自救看的是「实际生效的凭据」：账号自己没填、但引用的池子里有，
		// 界面上那个「一键签发」按钮就该是可点的
		session, _ := resolveAccountSession(&cfg, account)
		items = append(items, expiredTabiAIAccount{
			Name:           account.Name,
			State:          row.State,
			Paused:         row.Paused,
			Message:        row.Message,
			LastRunAt:      row.LastRunAt,
			HasUserSession: session != "",
		})
		// 取最近的一次刷新时间当整体的「判定时间」，让调用方知道这份名单有多新
		if row.LastRunAt > checkedAt {
			checkedAt = row.LastRunAt
		}
	}

	writeJSON(w, http.StatusOK, map[string]any{
		"accounts":   items,
		"count":      len(items),
		"checked_at": checkedAt,
	})
}

// isExpiredKeepaliveState 判定一条保活记录是否表示「凭据失效」。
//
// paused 单独算一种：保活遇到凭据失效会暂停该账号，此后不再刷新，所以它的 state
// 可能停留在当时的值。只看 state 会漏掉这批最需要处理的账号。
func isExpiredKeepaliveState(row TabiAIKeepaliveRow) bool {
	if row.Paused {
		return true
	}
	return strings.TrimSpace(row.State) == cookieTestStateInvalid
}
