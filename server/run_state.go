/*
server/run_state.go
签到运行状态：Actions/本机客户端跑签到期间锁住网页端的凭据操作

为什么需要它：
  - TaBiAI 的 new_api_refresh 有代次轮转 + 重放检测。签到进程每 refresh 一次就消耗
    一代并拿到下一代，而网页端的「TaBiAI 凭据检测」同样是一次真 refresh。
  - 两边同时动同一条 sid：谁手里的代次旧了，下次用就会被判重放，整条会话被撤销
    （AUTH_SESSION_REVOKED），只能重新签发。这不是「本次检测失败」，是把账号打死。
  - 所以签到期间网页端必须被拦住。客户端在 config_sync 里已经配了平台地址与 API Key，
    顺手上报「我开始跑了/还在跑/跑完了」成本极低。

为什么用心跳而不是布尔开关：
Actions 有 6 小时硬上限，超时是被平台强杀，客户端没有机会发「结束」；网络抖动、
进程崩溃同理。一个只会被显式关闭的锁迟早会永久锁死网页端。因此判活看
「最后一次心跳距今多久」，超过 runStateStaleAfter 就视为已结束。
*/
package main

import (
	"database/sql"
	"errors"
	"fmt"
	"log"
	"net/http"
	"strings"
	"time"
)

// runStateRowID 单行表：整个平台同时只跟踪一个签到进程。
// 多台机器同时跑同一份配置本身就会撞代次，不是这里要支持的场景。
const runStateRowID = 1

const (
	// runStateHeartbeatSeconds 建议客户端多久上报一次心跳。
	// 通过 start 响应下发，客户端不必硬编码；改这里就能全局调整。
	runStateHeartbeatSeconds = 60
	// runStateStaleAfter 超过这么久没心跳就认为签到已经结束（进程被杀/崩了）。
	// 取心跳间隔的数倍，容忍偶发的网络抖动与 GC 停顿。
	runStateStaleAfter = 5 * time.Minute
)

// RunState 一次签到运行的状态。
type RunState struct {
	// Running 是判活结论：库里有记录且心跳没过期
	Running bool `json:"running"`
	// Source 客户端自报的来源（如 github-actions / 本机主机名），只用于界面展示
	Source    string `json:"source"`
	StartedAt string `json:"started_at"`
	// HeartbeatAt 最后一次心跳时间；判活就看它
	HeartbeatAt string `json:"heartbeat_at"`
	// StaleAfterSeconds 多久没心跳算结束，供前端算出「预计还锁多久」
	StaleAfterSeconds int `json:"stale_after_seconds"`
	// HeartbeatSeconds 建议的心跳间隔，供客户端使用
	HeartbeatSeconds int `json:"heartbeat_seconds"`
	// Holders 当前有几个进程持有这把锁。分片并行时会大于 1，界面上能看出跑了几个 job
	Holders int `json:"holders"`
}

// createRunStateTable 建表（幂等）。
func createRunStateTable(db *sql.DB) error {
	_, err := db.Exec(`CREATE TABLE IF NOT EXISTS run_state (
		id           INTEGER PRIMARY KEY CHECK (id = ` + fmt.Sprint(runStateRowID) + `),
		source       TEXT    NOT NULL,
		started_at   TEXT    NOT NULL,
		heartbeat_at TEXT    NOT NULL,
		holders      INTEGER NOT NULL DEFAULT 1
	)`)
	if err != nil {
		return fmt.Errorf("建 run_state 表失败: %w", err)
	}
	// 老库迁移：表已存在但缺 holders 列时补上。旧库里的那一行是单实例留下的，
	// 默认 1 正好等价于原来的语义。
	cols, err := db.Query(`PRAGMA table_info(run_state)`)
	if err == nil {
		hasHolders := false
		for cols.Next() {
			var cid int
			var cname, ctype string
			var notnull, pk int
			var dflt any
			if cols.Scan(&cid, &cname, &ctype, &notnull, &dflt, &pk) == nil && cname == "holders" {
				hasHolders = true
			}
		}
		cols.Close()
		if !hasHolders {
			if _, err := db.Exec(`ALTER TABLE run_state ADD COLUMN holders INTEGER NOT NULL DEFAULT 1`); err != nil {
				return fmt.Errorf("迁移 run_state.holders 列失败: %w", err)
			}
		}
	}
	return nil
}

/*
StartRun 记录签到开始，并把持有者计数加一。

计数是为了让 GitHub Actions 的分片并行能用：每 30 个账号拆一个 job，几个 job 各自
start/stop，最先跑完的那个不能把还在跑的锁掉。减到 0 才真正释放。

上一轮记录已经心跳过期时，计数**重置为 1** 而不是累加 —— 那说明上次的进程被杀了没能
stop，累加会让泄漏的计数永远压住锁，只能改库才能恢复。
*/
func StartRun(db *sql.DB, source string) (RunState, error) {
	now := time.Now().UTC().Format(time.RFC3339)
	tx, err := db.Begin()
	if err != nil {
		return RunState{}, fmt.Errorf("记录签到开始失败: %w", err)
	}
	defer tx.Rollback() //nolint:errcheck

	var holders int
	var heartbeatAt string
	err = tx.QueryRow(`SELECT holders, heartbeat_at FROM run_state WHERE id = ?`,
		runStateRowID).Scan(&holders, &heartbeatAt)
	switch {
	case errors.Is(err, sql.ErrNoRows):
		if _, err := tx.Exec(`INSERT INTO run_state (id, source, started_at, heartbeat_at, holders)
			VALUES (?, ?, ?, ?, 1)`, runStateRowID, strings.TrimSpace(source), now, now); err != nil {
			return RunState{}, fmt.Errorf("记录签到开始失败: %w", err)
		}
	case err != nil:
		return RunState{}, fmt.Errorf("记录签到开始失败: %w", err)
	case runStateHeartbeatExpired(heartbeatAt):
		// 上一轮没能正常收尾：当成新的一轮，计数从 1 重新起算
		if _, err := tx.Exec(`UPDATE run_state SET source = ?, started_at = ?, heartbeat_at = ?,
			holders = 1 WHERE id = ?`, strings.TrimSpace(source), now, now, runStateRowID); err != nil {
			return RunState{}, fmt.Errorf("记录签到开始失败: %w", err)
		}
	default:
		// 同一轮里又进来一个持有者（分片 job）：计数加一，started_at 保留最早那次
		if _, err := tx.Exec(`UPDATE run_state SET source = ?, heartbeat_at = ?,
			holders = holders + 1 WHERE id = ?`,
			strings.TrimSpace(source), now, runStateRowID); err != nil {
			return RunState{}, fmt.Errorf("记录签到开始失败: %w", err)
		}
	}
	if err := tx.Commit(); err != nil {
		return RunState{}, fmt.Errorf("记录签到开始失败: %w", err)
	}
	return LoadRunState(db)
}

// TouchRun 更新心跳。返回 false 说明库里没有记录（已被 stop 或强制解锁），
// 客户端据此知道自己的锁已经不在了。
func TouchRun(db *sql.DB) (bool, error) {
	now := time.Now().UTC().Format(time.RFC3339)
	res, err := db.Exec(`UPDATE run_state SET heartbeat_at = ? WHERE id = ?`, now, runStateRowID)
	if err != nil {
		return false, fmt.Errorf("更新签到心跳失败: %w", err)
	}
	affected, err := res.RowsAffected()
	if err != nil {
		return false, fmt.Errorf("更新签到心跳失败: %w", err)
	}
	return affected > 0, nil
}

// StopRun 清除运行记录。已经没有记录时也算成功（幂等，客户端可以放心重试）。
/*
StopRun 交还一个持有者。还有别人在跑就只减计数，减到最后一个才真正删除记录。

分片并行时最先跑完的 job 不能把锁整个删掉 —— 网页端会以为签到结束了，跑去动 TaBiAI
凭据，和还在跑的 job 撞代次。
*/
func StopRun(db *sql.DB) error {
	res, err := db.Exec(`UPDATE run_state SET holders = holders - 1
		WHERE id = ? AND holders > 1`, runStateRowID)
	if err != nil {
		return fmt.Errorf("清除签到状态失败: %w", err)
	}
	affected, err := res.RowsAffected()
	if err == nil && affected > 0 {
		// 还有其他持有者，锁继续留着
		return nil
	}
	if _, err := db.Exec(`DELETE FROM run_state WHERE id = ?`, runStateRowID); err != nil {
		return fmt.Errorf("清除签到状态失败: %w", err)
	}
	return nil
}

// ForceStopRun 管理员强制解锁：一次清空全部持有者，不管计数是几。
//
// 与 StopRun 分开是因为语义不同 —— 客户端 stop 是「我这一份跑完了」，而强制解锁是
// 「我确认它们都已经停了」。这里要是也只减一，分片并行时管理员得点 N 次才解得开。
func ForceStopRun(db *sql.DB) error {
	if _, err := db.Exec(`DELETE FROM run_state WHERE id = ?`, runStateRowID); err != nil {
		return fmt.Errorf("强制解锁失败: %w", err)
	}
	return nil
}

// LoadRunState 读当前状态并就地判活。
//
// 心跳过期的记录不删：留着能在界面上显示「上次运行于…」，也方便排查是被强杀还是正常结束。
func LoadRunState(db *sql.DB) (RunState, error) {
	state := RunState{
		StaleAfterSeconds: int(runStateStaleAfter.Seconds()),
		HeartbeatSeconds:  runStateHeartbeatSeconds,
	}
	var source, startedAt, heartbeatAt string
	var holders int
	err := db.QueryRow(`SELECT source, started_at, heartbeat_at, holders FROM run_state WHERE id = ?`,
		runStateRowID).Scan(&source, &startedAt, &heartbeatAt, &holders)
	if errors.Is(err, sql.ErrNoRows) {
		return state, nil
	}
	if err != nil {
		return state, fmt.Errorf("读取签到状态失败: %w", err)
	}
	state.Source = source
	state.StartedAt = startedAt
	state.HeartbeatAt = heartbeatAt
	state.Holders = holders
	state.Running = !runStateHeartbeatExpired(heartbeatAt)
	return state, nil
}

// runStateHeartbeatExpired 心跳是否已过期。
//
// 解析不出时间时按「已过期」处理：宁可放开网页端，也不要因为一条坏数据把平台锁死到
// 只能改库才能恢复。
func runStateHeartbeatExpired(heartbeatAt string) bool {
	at, err := time.Parse(time.RFC3339, heartbeatAt)
	if err != nil {
		return true
	}
	return time.Since(at) > runStateStaleAfter
}

// runLockMessage 被锁住时给用户看的说明。要讲清「为什么拦」和「大概多久能用」，
// 否则用户只会觉得平台坏了。
func runLockMessage(state RunState) string {
	remaining := "稍后"
	if at, err := time.Parse(time.RFC3339, state.HeartbeatAt); err == nil {
		left := runStateStaleAfter - time.Since(at)
		if left > 0 {
			remaining = fmt.Sprintf("约 %d 分钟后", int(left.Minutes())+1)
		}
	}
	who := state.Source
	if who == "" {
		who = "签到客户端"
	}
	return fmt.Sprintf("%s 正在签到，为避免 TaBiAI 凭据代次冲突已暂时锁定该操作。"+
		"签到结束后自动解锁；若确认对方已停止，可在「Cookie 测试」页强制解锁（%s自动失效）",
		who, remaining)
}

// ---------------------------------------------------------------------------
// HTTP 接口
// ---------------------------------------------------------------------------

// handleRunStateStart POST /api/run-state/start（API Key）—— 客户端上报开始签到。
func (s *Server) handleRunStateStart(w http.ResponseWriter, r *http.Request) {
	var req struct {
		Source string `json:"source"`
	}
	// 允许空体：source 只用于界面展示，缺了不影响锁本身
	_ = readJSON(w, r, &req)
	state, err := StartRun(s.db, req.Source)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "服务器内部错误")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "run_state": state})
}

// handleRunStateHeartbeat POST /api/run-state/heartbeat（API Key）—— 续期。
//
// running=false 是有效信息而不是错误：说明管理员强制解锁了，客户端可以据此
// 在日志里提醒「网页端可能正在动同一条凭据」。
func (s *Server) handleRunStateHeartbeat(w http.ResponseWriter, r *http.Request) {
	alive, err := TouchRun(s.db)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "服务器内部错误")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "running": alive})
}

// handleRunStateStop POST /api/run-state/stop（API Key）—— 签到收尾，立即解锁。
func (s *Server) handleRunStateStop(w http.ResponseWriter, r *http.Request) {
	if err := StopRun(s.db); err != nil {
		writeError(w, http.StatusInternalServerError, "服务器内部错误")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true})
}

// handleGetRunState GET /api/run-state（JWT 或 API Key）—— 前端查询锁状态。
func (s *Server) handleGetRunState(w http.ResponseWriter, r *http.Request) {
	state, err := LoadRunState(s.db)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "服务器内部错误")
		return
	}
	writeJSON(w, http.StatusOK, state)
}

// handleRunStateUnlock POST /api/run-state/unlock（JWT 或 API Key）—— 管理员强制解锁。
//
// 留这个出口是因为心跳机制本身也可能出岔子（客户端时钟错、上报地址配错），
// 没有它就只能等 5 分钟或者改库。代价是能手滑绕过保护，所以前端要给足警示。
func (s *Server) handleRunStateUnlock(w http.ResponseWriter, r *http.Request) {
	if err := ForceStopRun(s.db); err != nil {
		writeError(w, http.StatusInternalServerError, "服务器内部错误")
		return
	}
	log.Printf("[run-state] 管理员强制解锁签到状态")
	writeJSON(w, http.StatusOK, map[string]any{"ok": true})
}

// guardRunningCheckin 高危凭据操作的前置拦截：签到进行中就拒绝。
//
// 返回 true 表示已经写过响应，调用方必须立即 return。
func (s *Server) guardRunningCheckin(w http.ResponseWriter) bool {
	state, err := LoadRunState(s.db)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "服务器内部错误")
		return true
	}
	if !state.Running {
		return false
	}
	// 沿用 409 冲突语义（与配置乐观锁一致），并把状态一并回传，
	// 前端不用再多打一次 /api/run-state 就能渲染「谁在跑、还剩多久」
	writeJSON(w, http.StatusConflict, map[string]any{
		"error":     runLockMessage(state),
		"run_state": state,
	})
	return true
}
