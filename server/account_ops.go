/*
server/account_ops.go
账号级增量操作：POST /api/accounts/ops

为什么需要它：
  - 网页原本把「改一个账号」表达成「提交整份配置」，配上单一 revision 乐观锁，
    两个人同时加账号必然一个 409；就算放宽锁，后提交者的快照里没有对方刚加的账号，
    会把它整份覆盖抹掉。
  - 改成「按账号名重放操作」后，A 加账号 X 与 B 加账号 Y 作用在不同 name 上，
    各自在服务端最新配置上重放，互不影响；「删除」表达的是意图而不是快照，
    所以别人删掉的账号不会被我的提交复活。

改名也靠它解决：upsert 带 previous_name，服务端据此定位旧记录，
让 UnmaskConfig 仍能按旧名找回打码字段的真值 —— 用户不必再重填凭据。
*/
package main

import (
	"fmt"
	"log"
	"net/http"
	"strings"
)

// 账号操作类型。
const (
	accountOpUpsert     = "upsert"
	accountOpDelete     = "delete"
	accountOpSetEnabled = "set_enabled"
)

// maxAccountOpsPerRequest 单请求最多重放多少条操作。
// 批量启停/删除是按选中项逐条下发的，留足余量即可；上限只为挡住异常请求。
const maxAccountOpsPerRequest = 500

// AccountOp 一条账号操作。
type AccountOp struct {
	Type string `json:"type"`
	// Name 用于 delete / set_enabled 定位账号
	Name string `json:"name"`
	// PreviousName 仅 upsert 用：非空表示这是改名，据此定位旧记录并还原打码字段
	PreviousName string `json:"previous_name"`
	// Account 仅 upsert 用：完整账号对象
	Account *Account `json:"account"`
	// Enabled 仅 set_enabled 用
	Enabled bool `json:"enabled"`
}

// applyAccountOps 在给定配置上按顺序重放操作。
//
// 返回被跳过的操作说明（例如目标账号已被别人删掉）——这类情况不算错误：
// 并发编辑下别人先删掉了我要改的账号是正常的，报错只会让用户困惑。
func applyAccountOps(cfg *Config, ops []AccountOp) ([]string, error) {
	var skipped []string
	indexOf := func(name string) int {
		for i := range cfg.Accounts {
			if cfg.Accounts[i].Name == name {
				return i
			}
		}
		return -1
	}

	for i, op := range ops {
		switch strings.TrimSpace(op.Type) {
		case accountOpUpsert:
			if op.Account == nil {
				return nil, fmt.Errorf("ops[%d] 类型 upsert 缺少 account", i)
			}
			acct := *op.Account
			if strings.TrimSpace(acct.Name) == "" {
				return nil, fmt.Errorf("ops[%d].account.name 不能为空", i)
			}
			// 改名：先按旧名定位，就地替换那一条，保持它在列表中的位置
			if prev := strings.TrimSpace(op.PreviousName); prev != "" && prev != acct.Name {
				if at := indexOf(prev); at >= 0 {
					cfg.Accounts[at] = acct
					continue
				}
				// 旧名已不存在（别人删了或已被改过）：退化成按新名 upsert
				skipped = append(skipped, fmt.Sprintf(
					"账号 %q 已不存在，%q 按新增处理", prev, acct.Name))
			}
			if at := indexOf(acct.Name); at >= 0 {
				cfg.Accounts[at] = acct
			} else {
				cfg.Accounts = append(cfg.Accounts, acct)
			}

		case accountOpDelete:
			name := strings.TrimSpace(op.Name)
			if name == "" {
				return nil, fmt.Errorf("ops[%d] 类型 delete 缺少 name", i)
			}
			at := indexOf(name)
			if at < 0 {
				skipped = append(skipped, fmt.Sprintf("账号 %q 已不存在，跳过删除", name))
				continue
			}
			cfg.Accounts = append(cfg.Accounts[:at], cfg.Accounts[at+1:]...)

		case accountOpSetEnabled:
			name := strings.TrimSpace(op.Name)
			if name == "" {
				return nil, fmt.Errorf("ops[%d] 类型 set_enabled 缺少 name", i)
			}
			at := indexOf(name)
			if at < 0 {
				skipped = append(skipped, fmt.Sprintf("账号 %q 已不存在，跳过启停", name))
				continue
			}
			cfg.Accounts[at].Enabled = op.Enabled

		default:
			return nil, fmt.Errorf("ops[%d].type 只能是 %s / %s / %s（当前 %q）",
				i, accountOpUpsert, accountOpDelete, accountOpSetEnabled, op.Type)
		}
	}
	return skipped, nil
}

// handleAccountOps POST /api/accounts/ops（JWT 或 API Key）—— 按账号名重放增量操作。
//
// 与 PUT /api/config 的区别：这里不接受整份快照、不做 revision 比对，
// 而是在服务端读到的**最新**配置上重放操作。多人同时加不同账号因此都能成功。
func (s *Server) handleAccountOps(w http.ResponseWriter, r *http.Request) {
	var req struct {
		Ops []AccountOp `json:"ops"`
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

	updatedAt, revision, skipped, err := s.commitAccountOps(req.Ops)
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
		log.Printf("[accounts] 部分操作被跳过（并发编辑）: %s", strings.Join(skipped, "；"))
	}
	// 回传最新的打码配置，前端直接换上，省一次往返
	writeJSON(w, http.StatusOK, map[string]any{
		"ok":         true,
		"config":     MaskConfig(&latest),
		"updated_at": updatedAt,
		"revision":   revision,
		"skipped":    skipped,
	})
}

// accountOpsBadRequest 标记「该报 400 而不是 500」的错误。
type accountOpsBadRequest struct{ error }

// commitAccountOps 持锁读最新配置、重放操作、还原打码字段、校验后落库。
func (s *Server) commitAccountOps(ops []AccountOp) (string, int64, []string, error) {
	configWriteMu.Lock()
	defer configWriteMu.Unlock()

	base, _, err := loadConfigLocked(s.db)
	if err != nil {
		return "", 0, nil, err
	}
	target := *cloneConfig(&base)
	skipped, err := applyAccountOps(&target, ops)
	if err != nil {
		return "", 0, nil, accountOpsBadRequest{err}
	}

	// 还原 "***"：旧值来源是刚读到的最新配置。
	// 改名场景已在 applyAccountOps 里就地替换过，所以这里按新名就能查到旧值 ——
	// 前提是 UnmaskConfig 的匹配基准也用同一份 base，见下面的重命名映射处理。
	restored, err := unmaskWithRenames(&target, &base, ops)
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

// unmaskWithRenames 在还原打码字段前，把「改名」告知 UnmaskConfig。
//
// UnmaskConfig 按账号名匹配旧值，改名后新名在旧配置里不存在，直接调用会报
// 「无法还原」。这里先按 previous_name 在旧配置的副本里把对应账号改成新名，
// 让匹配得以成立 —— 副本只用于查表，不会落库。
func unmaskWithRenames(target, base *Config, ops []AccountOp) (*Config, error) {
	renames := make(map[string]string, len(ops)) // 新名 -> 旧名
	for _, op := range ops {
		if strings.TrimSpace(op.Type) != accountOpUpsert || op.Account == nil {
			continue
		}
		prev := strings.TrimSpace(op.PreviousName)
		if prev != "" && prev != op.Account.Name {
			renames[op.Account.Name] = prev
		}
	}
	if len(renames) == 0 {
		return UnmaskConfig(target, base)
	}
	lookup := cloneConfig(base)
	for newName, oldName := range renames {
		for i := range lookup.Accounts {
			if lookup.Accounts[i].Name == oldName {
				lookup.Accounts[i].Name = newName
				break
			}
		}
	}
	return UnmaskConfig(target, lookup)
}
