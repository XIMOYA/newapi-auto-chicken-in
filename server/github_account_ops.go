// server/github_account_ops.go
// GitHub 账号池的增删改端点：POST /api/github-accounts/ops
//
// 为什么不复用 PUT /api/config：整份提交存在「陈旧快照覆盖」风险 —— 客户端读到
// 配置后、提交前，后台签到可能已轮转了某账号的 new_api_refresh，整份写回会抹掉
// 新代次、触发站点重放检测。这里只描述「这一次要做什么」。
//
// 提交路径与 account_ops.go 保持一致（持锁重读 → 重放 → 还原打码 → 校验 → 落库），
// 额外担一件那边不需要管的事：池子是被 accounts[] 引用的，
// 改名要连带更新引用、删除要先确认没人引用。引用断裂是静默的 ——
// resolveAccountSession 会悄悄回落账号自带的旧字段，签到看着还在跑，实际用过期凭据。
package main

import (
	"fmt"
	"log"
	"net/http"
	"strings"
)

// GitHubAccountOp 一条池子操作。字段命名与 AccountOp 对齐，减少前端两套心智负担。
type GitHubAccountOp struct {
	// Type 操作类型：upsert / delete
	Type string `json:"type"`
	// Name 用于 delete 定位
	Name string `json:"name"`
	// PreviousName 仅 upsert 用：非空表示改名，据此定位旧记录并同步引用
	PreviousName string `json:"previous_name"`
	// Account 仅 upsert 用
	Account *GitHubAccount `json:"account"`
}

// findGitHubAccountIndexByName 在切片里按名字定位，找不到返回 -1。
func findGitHubAccountIndexByName(pool []GitHubAccount, name string) int {
	target := strings.TrimSpace(name)
	for i := range pool {
		if pool[i].Name == target {
			return i
		}
	}
	return -1
}

// countAccountsUsingGitHub 数有多少站点账号引用了这个 GitHub 账号。
func countAccountsUsingGitHub(cfg *Config, name string) int {
	target := strings.TrimSpace(name)
	n := 0
	for i := range cfg.Accounts {
		if strings.TrimSpace(cfg.Accounts[i].GitHubAccount) == target {
			n++
		}
	}
	return n
}

// applyGitHubAccountOps 在给定配置上按顺序重放操作。
//
// 返回被跳过的说明：delete 时目标已不存在属于并发编辑的正常情况，
// 报错只会让用户困惑（与 applyAccountOps 同一取舍）。
func applyGitHubAccountOps(cfg *Config, ops []GitHubAccountOp) ([]string, error) {
	var skipped []string
	for _, op := range ops {
		switch strings.ToLower(strings.TrimSpace(op.Type)) {
		case "upsert":
			if err := applyGitHubUpsert(cfg, op); err != nil {
				return nil, err
			}
		case "delete":
			name := strings.TrimSpace(op.Name)
			idx := findGitHubAccountIndexByName(cfg.GitHubAccounts, name)
			if idx < 0 {
				skipped = append(skipped, fmt.Sprintf("delete %q：已不存在", name))
				continue
			}
			// 有引用时拒绝，不做级联：级联要么把引用置空（那些账号会静默回落
			// 旧字段继续跑，用户以为删干净了），要么连站点账号一起删（用户没要求）
			if used := countAccountsUsingGitHub(cfg, name); used > 0 {
				return nil, fmt.Errorf(
					"还有 %d 个站点账号在用 GitHub 账号 %q，请先改掉它们的引用再删除", used, name)
			}
			cfg.GitHubAccounts = append(
				cfg.GitHubAccounts[:idx], cfg.GitHubAccounts[idx+1:]...)
		default:
			return nil, fmt.Errorf("未知操作类型: %q", op.Type)
		}
	}
	return skipped, nil
}

// inheritGitHubRuntimeFields 把旧记录的服务端运行状态搬到新记录上。
//
// Fingerprint 与 ProxyAddr 都不是用户提交的字段（前端压根不发），所以每次 upsert
// 重建记录时必须显式搬过来。漏掉的后果正好打破这两件事的立意：
//   - 不搬 Fingerprint：ensureGitHubFingerprints 会按新名字重新派生 seed，
//     改个名就等于给这个账号换了台设备，而 GitHub 的 session 绑设备特征
//   - 不搬 ProxyAddr：keepGitHubRuntimeFields 按名字匹配，改名后对不上就丢绑定，
//     账号会被重新分配出口 —— 而固定出口本来就是为了不让 session 换 IP
func inheritGitHubRuntimeFields(incoming *GitHubAccount, old GitHubAccount) {
	if strings.TrimSpace(incoming.Fingerprint) == "" {
		incoming.Fingerprint = old.Fingerprint
	}
	if strings.TrimSpace(incoming.ProxyAddr) == "" {
		incoming.ProxyAddr = old.ProxyAddr
	}
}

// applyGitHubUpsert 新增或更新一条池子记录。
//
// user_session 允许提交打码占位符：还原交给 UnmaskConfig 统一处理，
// 这里只保证「不能是空」—— 空 session 的唯一效果是让签发静默回落旧字段。
func applyGitHubUpsert(cfg *Config, op GitHubAccountOp) error {
	if op.Account == nil {
		return fmt.Errorf("upsert 操作缺少 account 对象")
	}
	name := strings.TrimSpace(op.Account.Name)
	if name == "" {
		return fmt.Errorf("GitHub 账号名不能为空")
	}
	if strings.TrimSpace(op.Account.UserSession) == "" {
		return fmt.Errorf("GitHub 账号 %q 的 user_session 不能为空", name)
	}

	incoming := GitHubAccount{
		Name:        name,
		UserSession: strings.TrimSpace(op.Account.UserSession),
		ClientID:    strings.TrimSpace(op.Account.ClientID),
	}
	prev := strings.TrimSpace(op.PreviousName)

	// 改名：定位旧记录、就地替换，并把引用它的站点账号一起改过去
	if prev != "" && prev != name {
		idx := findGitHubAccountIndexByName(cfg.GitHubAccounts, prev)
		if idx < 0 {
			return fmt.Errorf("GitHub 账号 %q 不存在，无法改名", prev)
		}
		if findGitHubAccountIndexByName(cfg.GitHubAccounts, name) >= 0 {
			return fmt.Errorf("GitHub 账号 %q 已存在", name)
		}
		// 指纹与出口绑定跟着这条记录走，不因为改名而重置
		inheritGitHubRuntimeFields(&incoming, cfg.GitHubAccounts[idx])
		cfg.GitHubAccounts[idx] = incoming
		for i := range cfg.Accounts {
			if strings.TrimSpace(cfg.Accounts[i].GitHubAccount) == prev {
				cfg.Accounts[i].GitHubAccount = name
			}
		}
		return nil
	}

	if idx := findGitHubAccountIndexByName(cfg.GitHubAccounts, name); idx >= 0 {
		// 同名更新同理：前端只回传三个字段，运行状态得自己保住
		inheritGitHubRuntimeFields(&incoming, cfg.GitHubAccounts[idx])
		cfg.GitHubAccounts[idx] = incoming
		return nil
	}
	cfg.GitHubAccounts = append(cfg.GitHubAccounts, incoming)
	return nil
}

// unmaskWithPoolRenames 在还原打码字段前把池子改名告知 UnmaskConfig。
//
// UnmaskConfig 按名字匹配旧值，改名后新名在旧配置里不存在会直接报「无法还原」。
// 这里先在旧配置副本里把对应记录改成新名让匹配成立；副本只用于查表，不落库。
// 逐条处理所有改名，不是只处理第一条 —— 一次批量里可以改多个账号的名字。
func unmaskWithPoolRenames(target, base *Config, ops []GitHubAccountOp) (*Config, error) {
	renamed := cloneConfig(base)
	for _, op := range ops {
		if !strings.EqualFold(strings.TrimSpace(op.Type), "upsert") || op.Account == nil {
			continue
		}
		prev := strings.TrimSpace(op.PreviousName)
		newName := strings.TrimSpace(op.Account.Name)
		if prev == "" || prev == newName {
			continue
		}
		if idx := findGitHubAccountIndexByName(renamed.GitHubAccounts, prev); idx >= 0 {
			renamed.GitHubAccounts[idx].Name = newName
		}
	}
	return UnmaskConfig(target, renamed)
}

// commitGitHubAccountOps 持锁读最新配置、重放操作、还原打码、校验后落库。
func (s *Server) commitGitHubAccountOps(ops []GitHubAccountOp) (string, int64, []string, error) {
	configWriteMu.Lock()
	defer configWriteMu.Unlock()

	base, _, err := loadConfigLocked(s.db)
	if err != nil {
		return "", 0, nil, err
	}
	target := *cloneConfig(&base)
	skipped, err := applyGitHubAccountOps(&target, ops)
	if err != nil {
		return "", 0, nil, accountOpsBadRequest{err}
	}
	restored, err := unmaskWithPoolRenames(&target, &base, ops)
	if err != nil {
		return "", 0, nil, accountOpsBadRequest{err}
	}
	if err := ValidateConfig(restored); err != nil {
		return "", 0, nil, accountOpsBadRequest{err}
	}
	updatedAt, err := saveConfigKeepingCookiesLocked(s.db, *restored)
	if err != nil {
		return "", 0, nil, err
	}
	_, _, revision, err := LoadConfigWithRevision(s.db)
	if err != nil {
		return "", 0, nil, err
	}
	return updatedAt, revision, skipped, nil
}

// handleGitHubAccountOps 处理一批池子操作。
func (s *Server) handleGitHubAccountOps(w http.ResponseWriter, r *http.Request) {
	var req struct {
		Ops []GitHubAccountOp `json:"ops"`
	}
	if err := readJSON(w, r, &req); err != nil {
		writeError(w, http.StatusBadRequest, "请求体不是合法的 JSON")
		return
	}
	if len(req.Ops) == 0 {
		writeError(w, http.StatusBadRequest, "ops 不能为空")
		return
	}
	if len(req.Ops) > maxAccountOpsPerRequest {
		writeError(w, http.StatusBadRequest,
			fmt.Sprintf("ops 最多 %d 条（当前 %d）", maxAccountOpsPerRequest, len(req.Ops)))
		return
	}

	updatedAt, revision, skipped, err := s.commitGitHubAccountOps(req.Ops)
	if err != nil {
		if _, bad := err.(accountOpsBadRequest); bad {
			writeError(w, http.StatusBadRequest, err.Error())
			return
		}
		writeError(w, http.StatusInternalServerError, "服务器内部错误")
		return
	}

	latest, _, _, err := LoadConfigWithRevision(s.db)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "服务器内部错误")
		return
	}
	if len(skipped) > 0 {
		log.Printf("[github-accounts] 部分操作被跳过（并发编辑）: %s", strings.Join(skipped, "；"))
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"ok":         true,
		"config":     MaskConfig(&latest),
		"updated_at": updatedAt,
		"revision":   revision,
		"skipped":    skipped,
	})
}
