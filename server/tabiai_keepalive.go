/*
server/tabiai_keepalive.go
TaBiAI 凭据自动保活：每隔一段时间主动 refresh 一次，让代次保持滚动并立刻落库

为什么需要它：
  - new_api_refresh 的 secret 每 refresh 一次就换一代，旧代只有分钟级宽限窗口。
    一旦某一代过了窗口还被使用，服务端判为重放，整条会话被撤销（AUTH_SESSION_REVOKED），
    只能重新签发。
  - 签到一天只跑一次，中间十几个小时里那一代一直躺着不动。任何一个"第三方"
    （本机 daemon、网页端检测、另一台机器）碰一下就可能让平台手里这代作废。
    主动按节奏刷新并立刻落库，能把这个窗口压到一个刷新间隔之内。

两条硬规则：
  1. 签到在跑就整轮跳过。保活的 refresh 和签到的 refresh 抢同一条 sid，撞上就是
     把账号打死，绝不是"这次保活失败"这么轻。判活直接复用 run_state 的心跳。
  2. 凭据已经失效的账号立刻暂停，不再刷。继续刷既救不回来，还会把日志刷满。
     暂停时记下当时那一代的值，之后发现库里的值被改过（人工重新签发/粘贴）
     就自动恢复 —— 恢复条件就是"编辑后的第一次刷新"。

刷新走的是和网页端「TaBiAI 凭据检测」完全相同的那条路（runCookieTestPass +
checkTabiAICookie），包括代理发牌规则，所以两者的行为和判定口径天然一致。
*/
package main

import (
	"context"
	"database/sql"
	"fmt"
	"log"
	"net/http"
	"strings"
	"sync"
	"time"
)

const (
	// tabiaiKeepaliveRowID 设置是单行表：整个平台一份保活策略。
	tabiaiKeepaliveRowID = 1
	// tabiaiKeepaliveDefaultMinutes 默认间隔 90 分钟（1.5 小时）。
	// 比签到频繁得多，但每次都会把新代次立刻落库，换来的是"最多 90 分钟的暴露窗口"。
	tabiaiKeepaliveDefaultMinutes = 90
	// tabiaiKeepaliveMinMinutes 间隔下限。刷太勤没有收益，只会多消耗代次、
	// 也更容易撞上站点对 refresh 的频率限制。
	tabiaiKeepaliveMinMinutes = 15
	// tabiaiKeepaliveMaxMinutes 间隔上限。超过半天就失去"压缩暴露窗口"的意义了。
	tabiaiKeepaliveMaxMinutes = 720
	// tabiaiKeepaliveTickSeconds 协程的巡检节拍。不直接按间隔睡：设置随时可能被网页端
	// 改小，睡满 90 分钟会让新设置迟迟不生效。每分钟醒一次看"该不该跑了"。
	tabiaiKeepaliveTickSeconds = 60
	// tabiaiKeepaliveRunTimeout 单轮总超时。账号再多也不该无限期占着这个协程。
	tabiaiKeepaliveRunTimeout = 10 * time.Minute
)

// TabiAIKeepaliveSetting 保活策略。
type TabiAIKeepaliveSetting struct {
	Enabled bool `json:"enabled"`
	// Minutes 刷新间隔（分钟）
	Minutes   int    `json:"minutes"`
	UpdatedAt string `json:"updated_at"`
}

// TabiAIKeepaliveRow 单个账号的保活状态，直接喂给前端表格。
type TabiAIKeepaliveRow struct {
	AccountName string `json:"account_name"`
	// LastRunAt 最后一次刷新时间（RFC3339，UTC）
	LastRunAt string `json:"last_run_at"`
	// State 沿用 cookie 检测的状态词：ok / invalid / proxy_issue / abnormal
	State   string `json:"state"`
	Message string `json:"message"`
	// Rotated 上一次刷新站点是否真的换了代次。一直为 false 说明回写链路可能有问题
	Rotated bool `json:"rotated"`
	// Paused 凭据失效后被暂停；要等人工改过凭据才会自动恢复
	Paused   bool   `json:"paused"`
	PausedAt string `json:"paused_at"`
	// ProxyAddr 上一次刷新用的代理，空串表示直连
	ProxyAddr string `json:"proxy_addr"`
}

// TabiAIKeepaliveStatus GET 接口的整体返回。
type TabiAIKeepaliveStatus struct {
	Setting TabiAIKeepaliveSetting `json:"setting"`
	// Accounts 当前配置里所有启用的 tabiai 账号（含从未刷过的，State 为空）
	Accounts []TabiAIKeepaliveRow `json:"accounts"`
	// LastRunAt 最近一轮的开始时间；Running 表示此刻正有一轮在跑
	LastRunAt string `json:"last_run_at"`
	Running   bool   `json:"running"`
	// SkippedByCheckin 上一轮是否因为签到正在跑而被整轮跳过
	SkippedByCheckin bool `json:"skipped_by_checkin"`
	// NextRunAt 预计下次刷新时间，供前端显示倒计时
	NextRunAt string `json:"next_run_at"`
}

// createTabiAIKeepaliveTables 建表（幂等）。设置与每账号状态分两张表：
// 前者是单行策略，后者按账号名主键，账号被删掉时残留一行也无害（查询时按当前配置过滤）。
func createTabiAIKeepaliveTables(db *sql.DB) error {
	stmts := []string{
		`CREATE TABLE IF NOT EXISTS tabiai_keepalive_setting (
			id         INTEGER PRIMARY KEY CHECK (id = ` + fmt.Sprint(tabiaiKeepaliveRowID) + `),
			enabled    INTEGER NOT NULL DEFAULT 1,
			minutes    INTEGER NOT NULL DEFAULT ` + fmt.Sprint(tabiaiKeepaliveDefaultMinutes) + `,
			updated_at TEXT    NOT NULL
		)`,
		`CREATE TABLE IF NOT EXISTS tabiai_keepalive_state (
			account_name  TEXT PRIMARY KEY,
			last_run_at   TEXT    NOT NULL DEFAULT '',
			state         TEXT    NOT NULL DEFAULT '',
			message       TEXT    NOT NULL DEFAULT '',
			rotated       INTEGER NOT NULL DEFAULT 0,
			paused        INTEGER NOT NULL DEFAULT 0,
			paused_cookie TEXT    NOT NULL DEFAULT '',
			paused_at     TEXT    NOT NULL DEFAULT '',
			proxy_addr    TEXT    NOT NULL DEFAULT ''
		)`,
	}
	for _, st := range stmts {
		if _, err := db.Exec(st); err != nil {
			return fmt.Errorf("建 tabiai 保活表失败: %w", err)
		}
	}
	return nil
}

// LoadTabiAIKeepaliveSetting 读策略；没有记录时返回默认值（启用 + 90 分钟）并落一行。
func LoadTabiAIKeepaliveSetting(db *sql.DB) (TabiAIKeepaliveSetting, error) {
	out := TabiAIKeepaliveSetting{Enabled: true, Minutes: tabiaiKeepaliveDefaultMinutes}
	if db == nil {
		return out, nil
	}
	var enabled, minutes int
	var updatedAt string
	err := db.QueryRow(`SELECT enabled, minutes, updated_at FROM tabiai_keepalive_setting
		WHERE id = ?`, tabiaiKeepaliveRowID).Scan(&enabled, &minutes, &updatedAt)
	if err == sql.ErrNoRows {
		now := time.Now().UTC().Format(time.RFC3339)
		if _, ierr := db.Exec(`INSERT INTO tabiai_keepalive_setting (id, enabled, minutes, updated_at)
			VALUES (?, 1, ?, ?)`, tabiaiKeepaliveRowID, tabiaiKeepaliveDefaultMinutes, now); ierr != nil {
			return out, fmt.Errorf("初始化 tabiai 保活策略失败: %w", ierr)
		}
		out.UpdatedAt = now
		return out, nil
	}
	if err != nil {
		return out, fmt.Errorf("读取 tabiai 保活策略失败: %w", err)
	}
	out.Enabled = enabled != 0
	out.Minutes = clampKeepaliveMinutes(minutes)
	out.UpdatedAt = updatedAt
	return out, nil
}

// SaveTabiAIKeepaliveSetting 写策略。间隔会被夹到 [15, 720] 分钟。
func SaveTabiAIKeepaliveSetting(db *sql.DB, in TabiAIKeepaliveSetting) (TabiAIKeepaliveSetting, error) {
	if db == nil {
		return in, fmt.Errorf("数据库不可用")
	}
	minutes := clampKeepaliveMinutes(in.Minutes)
	now := time.Now().UTC().Format(time.RFC3339)
	enabled := 0
	if in.Enabled {
		enabled = 1
	}
	if _, err := db.Exec(`INSERT INTO tabiai_keepalive_setting (id, enabled, minutes, updated_at)
		VALUES (?, ?, ?, ?)
		ON CONFLICT(id) DO UPDATE SET enabled = excluded.enabled,
			minutes = excluded.minutes, updated_at = excluded.updated_at`,
		tabiaiKeepaliveRowID, enabled, minutes, now); err != nil {
		return in, fmt.Errorf("保存 tabiai 保活策略失败: %w", err)
	}
	return TabiAIKeepaliveSetting{Enabled: in.Enabled, Minutes: minutes, UpdatedAt: now}, nil
}

// clampKeepaliveMinutes 把间隔夹进合理区间；0 或负数一律按默认值处理
// （关闭保活用 enabled=false 表达，不要用间隔 0，那会让语义含糊）。
func clampKeepaliveMinutes(minutes int) int {
	if minutes <= 0 {
		return tabiaiKeepaliveDefaultMinutes
	}
	if minutes < tabiaiKeepaliveMinMinutes {
		return tabiaiKeepaliveMinMinutes
	}
	if minutes > tabiaiKeepaliveMaxMinutes {
		return tabiaiKeepaliveMaxMinutes
	}
	return minutes
}

// loadKeepaliveStates 读所有账号的保活状态，按账号名索引。
func loadKeepaliveStates(db *sql.DB) (map[string]TabiAIKeepaliveRow, map[string]string, error) {
	states := make(map[string]TabiAIKeepaliveRow)
	// pausedCookies 单独返回：它是内部判据（暂停时那一代的值），不该出现在 API 响应里
	pausedCookies := make(map[string]string)
	if db == nil {
		return states, pausedCookies, nil
	}
	rows, err := db.Query(`SELECT account_name, last_run_at, state, message, rotated,
		paused, paused_cookie, paused_at, proxy_addr FROM tabiai_keepalive_state`)
	if err != nil {
		return states, pausedCookies, fmt.Errorf("读取 tabiai 保活状态失败: %w", err)
	}
	defer rows.Close()
	for rows.Next() {
		var r TabiAIKeepaliveRow
		var rotated, paused int
		var pausedCookie string
		if err := rows.Scan(&r.AccountName, &r.LastRunAt, &r.State, &r.Message, &rotated,
			&paused, &pausedCookie, &r.PausedAt, &r.ProxyAddr); err != nil {
			return states, pausedCookies, fmt.Errorf("读取 tabiai 保活状态失败: %w", err)
		}
		r.Rotated = rotated != 0
		r.Paused = paused != 0
		states[r.AccountName] = r
		pausedCookies[r.AccountName] = pausedCookie
	}
	return states, pausedCookies, rows.Err()
}

// saveKeepaliveState 写单个账号的本轮结果。
func saveKeepaliveState(db *sql.DB, row TabiAIKeepaliveRow, pausedCookie string) error {
	if db == nil {
		return nil
	}
	rotated, paused := 0, 0
	if row.Rotated {
		rotated = 1
	}
	if row.Paused {
		paused = 1
	}
	_, err := db.Exec(`INSERT INTO tabiai_keepalive_state (account_name, last_run_at, state,
			message, rotated, paused, paused_cookie, paused_at, proxy_addr)
		VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
		ON CONFLICT(account_name) DO UPDATE SET last_run_at = excluded.last_run_at,
			state = excluded.state, message = excluded.message, rotated = excluded.rotated,
			paused = excluded.paused, paused_cookie = excluded.paused_cookie,
			paused_at = excluded.paused_at, proxy_addr = excluded.proxy_addr`,
		row.AccountName, row.LastRunAt, row.State, row.Message, rotated, paused,
		pausedCookie, row.PausedAt, row.ProxyAddr)
	if err != nil {
		return fmt.Errorf("保存 tabiai 保活状态失败: %w", err)
	}
	return nil
}

// tabiaiKeepaliveTargets 从配置里挑出该刷的账号。
//
// 暂停规则在这里落地：已暂停的账号，只有当库里的凭据和"暂停时那一代"不同了
// （说明人工重新签发或粘贴过）才重新纳入 —— 这就是"编辑后的第一次刷新"。
func tabiaiKeepaliveTargets(cfg *Config, pausedCookies map[string]string,
	states map[string]TabiAIKeepaliveRow) ([]Account, []string) {
	targets := make([]Account, 0, len(cfg.Accounts))
	resumed := make([]string, 0)
	for _, acc := range cfg.Accounts {
		if acc.LoginMethod != LoginMethodTabiAI || !acc.Enabled {
			continue
		}
		if strings.TrimSpace(acc.Cookie) == "" {
			continue
		}
		st, ok := states[acc.Name]
		if ok && st.Paused {
			if strings.TrimSpace(acc.Cookie) == strings.TrimSpace(pausedCookies[acc.Name]) {
				continue // 凭据没被动过，继续暂停
			}
			resumed = append(resumed, acc.Name)
		}
		targets = append(targets, acc)
	}
	return targets, resumed
}

// TabiAIKeepalive 保活协程的运行时状态。只有一份实例，由 main 创建后交给 handlers 查询。
type TabiAIKeepalive struct {
	db *sql.DB
	pm *ProxyManager

	mu               sync.Mutex
	running          bool
	lastRunAt        time.Time
	skippedByCheckin bool
}

// NewTabiAIKeepalive 造一个保活器。db 为空时所有操作退化成空转（测试里用得上）。
func NewTabiAIKeepalive(db *sql.DB, pm *ProxyManager) *TabiAIKeepalive {
	return &TabiAIKeepalive{db: db, pm: pm}
}

// Start 起后台协程。照代理池后台刷新那套写：panic 兜住之后不能让协程静默消失，
// 否则保活永久停摆而界面上看不出异常，隔一分钟重进循环。
func (k *TabiAIKeepalive) Start() {
	if k == nil || k.db == nil {
		return
	}
	go func() {
		for {
			k.loop()
			log.Printf("[tabiai-keepalive] 后台协程异常退出，60 秒后重启")
			time.Sleep(60 * time.Second)
		}
	}()
}

// loop 正常情况下永不返回；一旦返回说明内部 panic 已被兜住。
func (k *TabiAIKeepalive) loop() {
	defer recoverPanic("TaBiAI 凭据保活")
	ticker := time.NewTicker(tabiaiKeepaliveTickSeconds * time.Second)
	defer ticker.Stop()
	for range ticker.C {
		setting, err := LoadTabiAIKeepaliveSetting(k.db)
		if err != nil {
			log.Printf("[tabiai-keepalive] 读策略失败: %v", err)
			continue
		}
		if !setting.Enabled {
			continue
		}
		if !k.due(setting.Minutes) {
			continue
		}
		ctx, cancel := context.WithTimeout(context.Background(), tabiaiKeepaliveRunTimeout)
		k.RunOnce(ctx, "定时")
		cancel()
	}
}

// due 判断是否到了该跑的时候。没跑过就立刻跑一轮 —— 平台刚启动时正好把当前状态摸清。
func (k *TabiAIKeepalive) due(minutes int) bool {
	k.mu.Lock()
	defer k.mu.Unlock()
	if k.running {
		return false
	}
	if k.lastRunAt.IsZero() {
		return true
	}
	return time.Since(k.lastRunAt) >= time.Duration(clampKeepaliveMinutes(minutes))*time.Minute
}

/*
RunOnce 跑一轮保活。trigger 只用于日志（定时 / 手动）。

返回 (刷新成功数, 暂停数, 失败数)。签到正在跑时整轮跳过并返回 (0,0,0) —— 那不是失败，
是必须的避让：保活的 refresh 和签到的 refresh 抢同一条 sid，撞上会把账号打死。
*/
func (k *TabiAIKeepalive) RunOnce(ctx context.Context, trigger string) (int, int, int) {
	if k == nil || k.db == nil {
		return 0, 0, 0
	}
	k.mu.Lock()
	if k.running {
		k.mu.Unlock()
		log.Printf("[tabiai-keepalive] 上一轮还没跑完，跳过本次（%s）", trigger)
		return 0, 0, 0
	}
	k.running = true
	k.lastRunAt = time.Now()
	k.mu.Unlock()
	defer func() {
		k.mu.Lock()
		k.running = false
		k.mu.Unlock()
	}()

	// 硬规则一：签到在跑就整轮避让
	if state, err := LoadRunState(k.db); err == nil && state.Running {
		k.mu.Lock()
		k.skippedByCheckin = true
		k.mu.Unlock()
		log.Printf("[tabiai-keepalive] 签到正在运行（来源 %s），本轮跳过（%s）",
			state.Source, trigger)
		return 0, 0, 0
	}
	k.mu.Lock()
	k.skippedByCheckin = false
	k.mu.Unlock()

	cfg, _, err := LoadConfig(k.db)
	if err != nil {
		log.Printf("[tabiai-keepalive] 读配置失败: %v", err)
		return 0, 0, 0
	}
	states, pausedCookies, err := loadKeepaliveStates(k.db)
	if err != nil {
		log.Printf("[tabiai-keepalive] 读状态失败: %v", err)
		return 0, 0, 0
	}
	targets, resumed := tabiaiKeepaliveTargets(&cfg, pausedCookies, states)
	for _, name := range resumed {
		log.Printf("[tabiai-keepalive] 账号 %q 的凭据已被更新，恢复自动刷新", name)
	}
	if len(targets) == 0 {
		log.Printf("[tabiai-keepalive] 没有需要刷新的 tabiai 账号（%s）", trigger)
		return 0, 0, 0
	}

	source := newCookieTestProxySource(k.pm)
	proxies := make([]string, 0, len(targets))
	for _, target := range targets {
		addr := ""
		// 账号自带代理时按它自己的走，和 cookie 检测同一规则
		if !hasOwnProxy(target) {
			addr = source.Next(ctx)
		}
		proxies = append(proxies, addr)
	}
	next := 0
	results := runCookieTestPass(ctx, &cfg, LoginMethodTabiAI, targets, func() string {
		addr := proxies[next]
		next++
		return addr
	})

	now := time.Now().UTC().Format(time.RFC3339)
	ok, paused, failed := 0, 0, 0
	for i, result := range results {
		name := targets[i].Name
		row := TabiAIKeepaliveRow{
			AccountName: name,
			LastRunAt:   now,
			State:       result.State,
			Message:     result.Message,
			Rotated:     result.rotatedCookie != "",
			ProxyAddr:   proxies[i],
		}
		// 站点换了代次就必须立刻落库，否则下一次（签到或检测）用旧代会被判重放
		if result.rotatedCookie != "" {
			if found, uerr := updateAccountCookie(k.db, name, result.rotatedCookie); uerr != nil {
				log.Printf("[tabiai-keepalive] 账号 %q 新凭据落库失败: %v", name, uerr)
				row.Message += "（警告：新凭据未能保存，下次刷新可能失败）"
			} else if !found {
				log.Printf("[tabiai-keepalive] 账号 %q 已不在配置中，跳过写回", name)
			}
		}
		pausedCookie := pausedCookies[name]
		if result.State == cookieTestStateInvalid {
			// 凭据已死：暂停并记下这一代的值，等人工改过再恢复
			row.Paused = true
			row.PausedAt = now
			pausedCookie = strings.TrimSpace(targets[i].Cookie)
			if result.rotatedCookie != "" {
				pausedCookie = strings.TrimSpace(result.rotatedCookie)
			}
			paused++
			log.Printf("[tabiai-keepalive] 账号 %q 凭据失效，已暂停自动刷新：%s", name, result.Message)
		} else if result.State == cookieTestStateValid {
			pausedCookie = ""
			ok++
		} else {
			failed++
		}
		if serr := saveKeepaliveState(k.db, row, pausedCookie); serr != nil {
			log.Printf("[tabiai-keepalive] 账号 %q 状态落库失败: %v", name, serr)
		}
	}
	log.Printf("[tabiai-keepalive] %s刷新 %d 个账号：正常 %d / 暂停 %d / 异常 %d",
		trigger, len(targets), ok, paused, failed)
	return ok, paused, failed
}

// Status 汇总当前状态给网页端。账号列表以**当前配置**为准：
// 配置里已删掉的账号即使库里还有残留状态也不展示，从未刷过的账号则补一行空状态，
// 这样界面上能看出"这个号还没轮到"而不是干脆不存在。
func (k *TabiAIKeepalive) Status() (TabiAIKeepaliveStatus, error) {
	out := TabiAIKeepaliveStatus{Accounts: []TabiAIKeepaliveRow{}}
	if k == nil || k.db == nil {
		return out, fmt.Errorf("保活器不可用")
	}
	setting, err := LoadTabiAIKeepaliveSetting(k.db)
	if err != nil {
		return out, err
	}
	out.Setting = setting

	cfg, _, err := LoadConfig(k.db)
	if err != nil {
		return out, err
	}
	states, _, err := loadKeepaliveStates(k.db)
	if err != nil {
		return out, err
	}
	for _, acc := range cfg.Accounts {
		if acc.LoginMethod != LoginMethodTabiAI || !acc.Enabled {
			continue
		}
		if row, ok := states[acc.Name]; ok {
			out.Accounts = append(out.Accounts, row)
			continue
		}
		out.Accounts = append(out.Accounts, TabiAIKeepaliveRow{AccountName: acc.Name})
	}

	k.mu.Lock()
	running, lastRun, skipped := k.running, k.lastRunAt, k.skippedByCheckin
	k.mu.Unlock()
	out.Running = running
	out.SkippedByCheckin = skipped
	if !lastRun.IsZero() {
		out.LastRunAt = lastRun.UTC().Format(time.RFC3339)
		if setting.Enabled {
			next := lastRun.Add(time.Duration(setting.Minutes) * time.Minute)
			out.NextRunAt = next.UTC().Format(time.RFC3339)
		}
	}
	return out, nil
}

// --------------------------------------------------------------------------- //
// HTTP 接口
// --------------------------------------------------------------------------- //

// handleGetTabiAIKeepalive GET /api/tabiai/keepalive —— 策略 + 每账号最后一次刷新状态。
func (s *Server) handleGetTabiAIKeepalive(w http.ResponseWriter, r *http.Request) {
	status, err := s.keepalive.Status()
	if err != nil {
		log.Printf("[tabiai-keepalive] 读状态失败: %v", err)
		writeError(w, http.StatusInternalServerError, "服务器内部错误")
		return
	}
	writeJSON(w, http.StatusOK, status)
}

// handlePutTabiAIKeepalive PUT /api/tabiai/keepalive —— 改开关与间隔。
// 间隔会被夹进 [15, 720] 分钟：刷太勤只是多消耗代次，超过半天就失去压缩暴露窗口的意义。
func (s *Server) handlePutTabiAIKeepalive(w http.ResponseWriter, r *http.Request) {
	var in TabiAIKeepaliveSetting
	if err := readJSON(w, r, &in); err != nil {
		writeError(w, http.StatusBadRequest, "请求体不是合法 JSON")
		return
	}
	saved, err := SaveTabiAIKeepaliveSetting(s.db, in)
	if err != nil {
		log.Printf("[tabiai-keepalive] 保存策略失败: %v", err)
		writeError(w, http.StatusInternalServerError, "服务器内部错误")
		return
	}
	log.Printf("[tabiai-keepalive] 策略已更新：启用=%v 间隔=%d 分钟", saved.Enabled, saved.Minutes)
	status, err := s.keepalive.Status()
	if err != nil {
		writeJSON(w, http.StatusOK, map[string]any{"setting": saved})
		return
	}
	writeJSON(w, http.StatusOK, status)
}

/*
handlePostTabiAIKeepaliveRun POST /api/tabiai/keepalive/run —— 立刻手动刷一轮。

同步跑完再返回：账号数量有限（十几个以内），前端等一两秒拿到最终结果，比返回
「已开始」再让它轮询要简单得多。签到进行中会被 RunOnce 内部整轮跳过，
这里额外先挡一次并回 409，让前端能直接告诉用户「签到在跑，稍后再试」。
*/
func (s *Server) handlePostTabiAIKeepaliveRun(w http.ResponseWriter, r *http.Request) {
	if s.guardRunningCheckin(w) {
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), tabiaiKeepaliveRunTimeout)
	defer cancel()
	ok, paused, failed := s.keepalive.RunOnce(ctx, "手动")
	status, err := s.keepalive.Status()
	if err != nil {
		writeError(w, http.StatusInternalServerError, "服务器内部错误")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"ok_count":     ok,
		"paused_count": paused,
		"failed_count": failed,
		"status":       status,
	})
}
