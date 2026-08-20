/*
server/proxy_feedback.go
代理反馈：GitHub Actions 侧用过之后回传的成败计数

职责：
- proxy_feedback 表：按 addr 累计 ok / net_fail / block_fail 与最后一次成败时间
- RecordProxyFeedback：客户端一次上报，按 addr 累加（upsert）
- FeedbackByAddr：全量读出，供列表按「Actions 实测表现」排序分档
- PruneProxyFeedback：清掉长期没再出现的行，免费代理换得快，不清会一直涨

为什么单独一张表而不是给 proxies 加列：proxies 每次刷新都被 replaceAll 清表重插，
计数放进去会跟着被抹掉；而且一个代理从上游列表消失几天后又回来时，它过去的表现
仍然有参考价值，不该跟着 proxies 的生命周期一起没。

安全边界：这里只接收客户端汇报的数字，服务器自己不会拿代理去打目标站点。
*/
package main

import (
	"database/sql"
	"fmt"
	"sort"
	"strings"
	"time"
)

// ProxyFeedback 一个代理在 Actions 侧的累计表现。
type ProxyFeedback struct {
	Addr       string `json:"addr"`
	OK         int    `json:"ok"`         // 经它签到成功的次数
	NetFail    int    `json:"net_fail"`   // 网络层连不上（代理本身不通）
	BlockFail  int    `json:"block_fail"` // 连得上但被目标站拦（IP 声誉问题）
	LastOKAt   string `json:"last_ok_at,omitempty"`
	LastFailAt string `json:"last_fail_at,omitempty"`
	UpdatedAt  string `json:"updated_at"`
}

// Total 该代理被记录过的总次数。
func (f ProxyFeedback) Total() int { return f.OK + f.NetFail + f.BlockFail }

// Fails 失败次数：连不上和被拦都得换 IP，对签到而言后果一样。
func (f ProxyFeedback) Fails() int { return f.NetFail + f.BlockFail }

// ProxyFeedbackItem 客户端一次上报里的单条计数。
type ProxyFeedbackItem struct {
	Addr      string `json:"addr"`
	OK        int    `json:"ok"`
	NetFail   int    `json:"net_fail"`
	BlockFail int    `json:"block_fail"`
}

// maxFeedbackItems 单次上报允许的条数上限。一轮签到最多也就用掉几十个代理，
// 给到 2000 足够宽松；再多按超限拒掉，免得一次请求把库刷爆。
const maxFeedbackItems = 2000

// feedbackRetentionDays 反馈保留天数：超过这么久没再更新的行会被清理。
const feedbackRetentionDays = 14

// createProxyFeedbackTable 建 proxy_feedback 表（幂等）。
func createProxyFeedbackTable(db *sql.DB) error {
	_, err := db.Exec(`CREATE TABLE IF NOT EXISTS proxy_feedback (
		addr         TEXT    PRIMARY KEY,
		ok           INTEGER NOT NULL DEFAULT 0,
		net_fail     INTEGER NOT NULL DEFAULT 0,
		block_fail   INTEGER NOT NULL DEFAULT 0,
		last_ok_at   TEXT,
		last_fail_at TEXT,
		updated_at   TEXT    NOT NULL
	)`)
	if err != nil {
		return fmt.Errorf("建 proxy_feedback 表失败: %w", err)
	}
	if _, err := db.Exec(`CREATE INDEX IF NOT EXISTS idx_proxy_feedback_updated ON proxy_feedback(updated_at)`); err != nil {
		return fmt.Errorf("建 proxy_feedback 索引失败: %w", err)
	}
	return nil
}

/*
validFeedbackAddr 判断上报来的地址能不能收。

只做形状检查：非空、无空白、含且仅含一个冒号、两段都不空。不校验是不是合法 IP ——
上游源里出现过域名形式的代理，客户端能用就该能记。真正的过滤在于这个 addr 是否
出现在 proxies 表里，排序时对不上号的行自然不会影响任何结果。
*/
func validFeedbackAddr(addr string) bool {
	if addr == "" || len(addr) > 255 {
		return false
	}
	if strings.ContainsAny(addr, " \t\r\n") {
		return false
	}
	host, port, found := strings.Cut(addr, ":")
	if !found || host == "" || port == "" {
		return false
	}
	return !strings.Contains(port, ":")
}

/*
RecordProxyFeedback 累加一次上报，返回收下与丢弃的条数。

同一个 addr 在一次上报里出现多次时先在内存里合并，避免同一事务里对同一主键
反复 upsert。计数为负数一律丢弃（客户端出 bug 不该把库写坏），三项全 0 的条目
也没有信息量，同样丢掉。
*/
func (m *ProxyManager) RecordProxyFeedback(items []ProxyFeedbackItem) (int, int, error) {
	if len(items) > maxFeedbackItems {
		return 0, 0, fmt.Errorf("单次上报条数 %d 超过上限 %d", len(items), maxFeedbackItems)
	}
	merged := make(map[string]*ProxyFeedbackItem, len(items))
	order := make([]string, 0, len(items))
	skipped := 0
	for _, it := range items {
		addr := strings.TrimSpace(it.Addr)
		if !validFeedbackAddr(addr) || it.OK < 0 || it.NetFail < 0 || it.BlockFail < 0 {
			skipped++
			continue
		}
		if it.OK == 0 && it.NetFail == 0 && it.BlockFail == 0 {
			skipped++
			continue
		}
		cur, seen := merged[addr]
		if !seen {
			cur = &ProxyFeedbackItem{Addr: addr}
			merged[addr] = cur
			order = append(order, addr)
		}
		cur.OK += it.OK
		cur.NetFail += it.NetFail
		cur.BlockFail += it.BlockFail
	}
	if len(order) == 0 {
		return 0, skipped, nil
	}
	if err := m.upsertFeedback(order, merged); err != nil {
		return 0, skipped, err
	}
	return len(order), skipped, nil
}

// upsertFeedback 单事务累加。last_ok_at / last_fail_at 只在对应方向真有增量时才动，
// 这样「上次成功是什么时候」不会被一次纯失败的上报冲掉。
func (m *ProxyManager) upsertFeedback(order []string, merged map[string]*ProxyFeedbackItem) error {
	now := time.Now().UTC().Format(time.RFC3339)
	tx, err := m.db.Begin()
	if err != nil {
		return fmt.Errorf("开启事务: %w", err)
	}
	defer tx.Rollback() //nolint:errcheck

	stmt, err := tx.Prepare(`
		INSERT INTO proxy_feedback (addr, ok, net_fail, block_fail, last_ok_at, last_fail_at, updated_at)
		VALUES (?, ?, ?, ?, ?, ?, ?)
		ON CONFLICT(addr) DO UPDATE SET
			ok           = ok + excluded.ok,
			net_fail     = net_fail + excluded.net_fail,
			block_fail   = block_fail + excluded.block_fail,
			last_ok_at   = COALESCE(excluded.last_ok_at, last_ok_at),
			last_fail_at = COALESCE(excluded.last_fail_at, last_fail_at),
			updated_at   = excluded.updated_at`)
	if err != nil {
		return fmt.Errorf("准备 upsert: %w", err)
	}
	defer stmt.Close()

	for _, addr := range order {
		it := merged[addr]
		var okAt, failAt any
		if it.OK > 0 {
			okAt = now
		}
		if it.NetFail+it.BlockFail > 0 {
			failAt = now
		}
		if _, err := stmt.Exec(addr, it.OK, it.NetFail, it.BlockFail, okAt, failAt, now); err != nil {
			return fmt.Errorf("写入反馈 %s: %w", addr, err)
		}
	}
	if err := tx.Commit(); err != nil {
		return fmt.Errorf("提交事务: %w", err)
	}
	return nil
}

// FeedbackByAddr 全量读出反馈，addr -> 累计表现。
func (m *ProxyManager) FeedbackByAddr() (map[string]ProxyFeedback, error) {
	rows, err := m.db.Query(`SELECT addr, ok, net_fail, block_fail,
		COALESCE(last_ok_at,''), COALESCE(last_fail_at,''), updated_at FROM proxy_feedback`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := make(map[string]ProxyFeedback)
	for rows.Next() {
		var f ProxyFeedback
		if err := rows.Scan(&f.Addr, &f.OK, &f.NetFail, &f.BlockFail,
			&f.LastOKAt, &f.LastFailAt, &f.UpdatedAt); err != nil {
			return nil, err
		}
		out[f.Addr] = f
	}
	return out, rows.Err()
}

// PruneProxyFeedback 删掉 days 天没再更新过的反馈，返回删除行数。
// days <= 0 时不做任何事，避免配错把全表清空。
func (m *ProxyManager) PruneProxyFeedback(days int) (int64, error) {
	if days <= 0 {
		return 0, nil
	}
	cutoff := time.Now().UTC().AddDate(0, 0, -days).Format(time.RFC3339)
	res, err := m.db.Exec(`DELETE FROM proxy_feedback WHERE updated_at < ?`, cutoff)
	if err != nil {
		return 0, err
	}
	n, err := res.RowsAffected()
	if err != nil {
		return 0, nil // 驱动不支持计数不算错误，清理本身已经成功
	}
	return n, nil
}

/*
proxyRank 排序档位，数字越小越先分配给账号。

分档而不是直接按失败率排，是因为样本量差得太远：只失败过一次的代理和失败二十次的
代理，失败率都是 100%，但前者很可能只是当时网络抖了一下。分档能把「还没试过」和
「试过确实不行」分开，也不至于一次失败就把代理判死。
*/
type proxyRank int

const (
	rankProven  proxyRank = 0 // 成功过，且失败没过半
	rankUnknown proxyRank = 1 // Actions 还没用过它
	rankFlaky   proxyRank = 2 // 成功过但失败偏多，或只失败过一次（样本不足）
	rankBroken  proxyRank = 3 // 试过两次以上，一次没成
)

func rankOf(f ProxyFeedback, known bool) proxyRank {
	if !known || f.Total() == 0 {
		return rankUnknown
	}
	if f.OK == 0 {
		if f.Total() >= 2 {
			return rankBroken
		}
		return rankFlaky
	}
	if f.Fails() > f.OK {
		return rankFlaky
	}
	return rankProven
}

/*
sortProxiesByFeedback 优选排序：存活优先，然后按 Actions 实测表现分档。

同档内依次比净成功数、测速、延迟，最后拿 addr 兜底保证顺序稳定 —— 每次请求返回的
顺序抖来抖去的话，多账号并发分配会踩到不同代理，问题也难复现。

反馈档位压在测速和延迟之前：服务器测出来的快慢是自己网络位置的快慢，而 Actions
runner 在 Azure 那边，同一个代理两边可达性经常不一样。真实用过的结果比服务器
自测更有说服力。
*/
func sortProxiesByFeedback(entries []ProxyEntry, fb map[string]ProxyFeedback) {
	sort.SliceStable(entries, func(i, j int) bool {
		a, b := entries[i], entries[j]
		if a.Alive != b.Alive {
			return a.Alive
		}
		fa, aKnown := fb[a.Addr]
		fbEntry, bKnown := fb[b.Addr]
		if ra, rb := rankOf(fa, aKnown), rankOf(fbEntry, bKnown); ra != rb {
			return ra < rb
		}
		if na, nb := fa.OK-fa.Fails(), fbEntry.OK-fbEntry.Fails(); na != nb {
			return na > nb
		}
		if a.SpeedBps != b.SpeedBps {
			return a.SpeedBps > b.SpeedBps
		}
		if a.LatencyMs != b.LatencyMs {
			return a.LatencyMs < b.LatencyMs
		}
		return a.Addr < b.Addr
	})
}
