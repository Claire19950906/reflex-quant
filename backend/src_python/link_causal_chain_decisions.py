#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""link_causal_chain_decisions.py — 因果链结果 ↔ 真实成交决策 的闭环链接 (G08)。

背景
----
causal_chain_outcomes 记的是"哪条链被哪条新闻触发", 508 行里几乎 0 行能追到
trade_decisions —— 链的对错只跟行情比, 从来不跟"当时到底影响了哪笔真实交易"比。
闭环改链 (需求 #8) 的前提就是这条链接。

做法 (决策驱动, 不是新闻驱动)
------------------------------
  对每条**成交决策**:
    1. 取它引用的那条新闻的标题/正文;
    2. 找出资产匹配、且触发词真的命中的因果链;
    3. 保证 (链, 新闻) 有一条 outcome 行, 并把这条决策挂上去。

多笔决策可能来自同一条新闻 (系统的做法就是一条新闻开多次仓), 所以
"链 → 决策"是一对多, 用链接表 causal_chain_outcome_decisions 承载;
causal_chain_outcomes.decision_id 记主决策 (第一条), 便于直接查询。

    python3 link_causal_chain_decisions.py --days 120
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from typing import Dict, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import causal_chain_evaluator as cce

DB_PATH = os.environ.get("TRIDENT_DB_PATH") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "trident_event_bus.db"
)

LINK_DDL = """
CREATE TABLE IF NOT EXISTS causal_chain_outcome_decisions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    outcome_id  INTEGER NOT NULL,
    decision_id INTEGER NOT NULL,
    chain_id    TEXT,
    asset       TEXT,
    action      TEXT,
    created_at  INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    UNIQUE(outcome_id, decision_id)
);
CREATE INDEX IF NOT EXISTS idx_outcome_decisions_dec ON causal_chain_outcome_decisions(decision_id);
CREATE INDEX IF NOT EXISTS idx_outcome_decisions_out  ON causal_chain_outcome_decisions(outcome_id);
"""


def ensure_schema(conn) -> None:
    conn.executescript(LINK_DDL)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(causal_chain_outcomes)").fetchall()}
    for name, decl in (("decision_id", "INTEGER"), ("decision_space", "TEXT")):
        if name not in cols:
            conn.execute("ALTER TABLE causal_chain_outcomes ADD COLUMN %s %s" % (name, decl))
    conn.commit()



def _chain_direction_quality(conn) -> Dict[str, Tuple[int, int]]:
    """{chain_id: (方向命中数, 样本数)} —— 链的历史判别力。

    v321.3 P1: 关键词匹配**不是瓶颈** (实测每决策 95.8 条候选链全部命中,
    各宽松档命中率完全一样) —— 质量问题在**排序**: 旧实现按"命中词数"取前 N,
    而命中词多 ≠ 相关。实测链的方向命中率差异极大 (355 条链样本>=3,
    从 **0% 到 100%**), 所以排序应该用**历史方向命中率**。
    """
    out: Dict[str, Tuple[int, int]] = {}
    try:
        rows = conn.execute(
            "SELECT chain_id, "
            "       SUM(CASE WHEN direction_hit=1 THEN 1 ELSE 0 END) h, "
            "       COUNT(*) n "
            "FROM causal_chain_outcomes "
            "WHERE direction_hit IS NOT NULL "
            "  AND status IN ('HIT','HIT_WEAK','MISS','NEUTRAL') "
            "GROUP BY chain_id"
        ).fetchall()
    except Exception as exc:
        # 读路径失败也要留痕 —— 静默返回空表会让「排序退化回词数」看起来正常。
        print(f"[LINK-CHAIN] ⚠️ 链判别力读取失败（原先静默吞掉）: "
              f"{type(exc).__name__}: {str(exc)[:120]}", flush=True)
        return out
    for r in rows:
        try:
            # 不转 int: chain_id 可能是非数字 (测试/自定义链), 转 int 会抛
            # ValueError 被 continue 吞掉 → 整张判别力表变空 → 排序静默退化。
            out[str(r["chain_id"])] = (int(r["h"] or 0), int(r["n"] or 0))
        except (TypeError, ValueError) as _cv_exc:
            print(f"[LINK-CHAIN] ⚠️ 链判别力解析失败（原先静默吞掉）: "
                  f"{type(_cv_exc).__name__}: {str(_cv_exc)[:80]}", flush=True)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--days", type=int, default=120)
    ap.add_argument("--max-chains-per-decision", type=int, default=3,
                    help="每条决策最多关联几条链（按判别力+命中词数取前 N）")
    ap.add_argument("--quality-min-samples", type=int, default=5,
                    help="链的判别力最少要多少条已评估样本才采信 (低于此 → 中性, 按词数排)")
    ap.add_argument("--quality-min-rate", type=float, default=0.40,
                    help="链的方向命中率低于此值 → 不挂 (明显比随机还差)")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=60000;")
    ensure_schema(conn)

    cutoff = int(time.time()) - args.days * 86400
    # ⚠️ v322: **只关联真实成交的决策**。
    # 原来这里是 `WHERE decision_ts >= ? AND news_id IS NOT NULL` —— 没过滤动作，
    # 而 trade_decisions 3241 条里 **REJECTED 占 3187 条(98.4%)**，
    # 真正成交只有 BUY 42 + SELL 7 + CLOSE 5 = 54 条。
    # 实跑一次的后果：链接表 2078 → 143635 行，其中 **141207 行(98.4%)是噪声**，
    # 还会多出几千条待对账 outcome → 未来带噪声反馈去改权重。
    decisions = conn.execute(
        """SELECT id, decision_ts, asset, news_id, final_action, intended_action, status
           FROM trade_decisions
           WHERE decision_ts >= ? AND news_id IS NOT NULL
             AND final_action IN ('BUY','SELL','CLOSE')
           ORDER BY decision_ts""",
        (cutoff,),
    ).fetchall()
    print("[INFO] 真实成交决策(窗口内, 带新闻, action∈BUY/SELL/CLOSE): %d"
          % len(decisions))

    # 新闻正文缓存: news_id → (news_events.id, ts, text)
    news_cache = {}
    for nid, ev_id, ts, headline, body in conn.execute(
        """SELECT news_id, id, ts, headline, body FROM news_events
           WHERE news_id IS NOT NULL"""
    ).fetchall():
        text = ((headline or "") + " " + (body or "")).strip()
        if not text:
            continue
        cur = news_cache.get(nid)
        if cur is None or len(text) > len(cur[2]):
            news_cache[nid] = (ev_id, ts, text)
    print("[INFO] 有正文的新闻: %d" % len(news_cache))

    chains = cce.load_active_chains(conn)
    by_asset = {}
    for ch in chains:
        by_asset.setdefault(ch.asset.upper(), []).append(ch)
    print("[INFO] 可评估链 %d, 覆盖资产 %s"
          % (len(chains), ",".join(sorted(by_asset.keys()))))

    chain_quality = _chain_direction_quality(conn)
    if chain_quality:
        rated = [v for v in chain_quality.values() if v[1] >= args.quality_min_samples]
        avg_rate = (sum(v[0] / v[1] for v in rated) / len(rated)) if rated else 0.0
        print("[INFO] 链判别力: %d 条有历史评估, 样本>=%d 的 %d 条, 平均方向命中率 %.1f%%"
              % (len(chain_quality), args.quality_min_samples, len(rated), avg_rate * 100))

    created_links = 0
    created_outcomes = 0
    matched_pairs = 0
    t0 = time.time()
    for i, dec in enumerate(decisions):
        news = news_cache.get(dec["news_id"])
        if not news:
            continue
        ev_id, ev_ts, text = news
        asset = (dec["asset"] or "").upper()
        cands = by_asset.get(asset) or []
        if not cands:
            continue
        # ⚠️ v322: 先收集全部命中，再按**命中词数**取前 N 条链。
        # 原来是无脑遍历所有候选链全建链接 → 实测每条决策挂到 **261 条链**
        # （403 条决策都是 261 条）—— 这不是关联，是笛卡尔积噪声。
        hits = []
        for ch in cands:
            try:
                hit = cce.meaningful_keyword_hit(text, ch.trigger_keywords, asset)
            except Exception:
                continue
            if not hit:
                continue
            q = chain_quality.get(str(ch.chain_id))
            if q is not None and q[1] >= args.quality_min_samples:
                rate = q[0] / q[1]
                # 样本足够但命中明显低于随机的链 → 不挂 (挂它是负价值)
                if rate < args.quality_min_rate:
                    continue
            else:
                rate = 0.5      # 无历史 → 中性, 靠词数排
            hits.append((rate, len(hit), ch, hit))
        if not hits:
            continue
        hits.sort(key=lambda x: (-x[0], -x[1]))
        if len(hits) > args.max_chains_per_decision:
            hits = hits[:args.max_chains_per_decision]
        for _rate, _score, ch, hit in hits:
            matched_pairs += 1
            deadline = int(ev_ts or dec["decision_ts"] or time.time()) + 5 * 86400
            cur = conn.execute(
                """INSERT OR IGNORE INTO causal_chain_outcomes
                     (chain_id, trigger_ts, trigger_source, trigger_ref_id,
                      trigger_headline, trigger_keywords_matched,
                      predicted_direction, predicted_mag_lo, predicted_mag_hi,
                      eval_deadline_ts, status, decision_id, decision_space, created_at)
                   VALUES (?, ?, 'news_events', ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, 'trade', ?)""",
                (ch.chain_id, int(ev_ts or 0), int(ev_id), text[:200], ",".join(hit),
                 ch.expected_direction, ch.magnitude_lo, ch.magnitude_hi,
                 deadline, int(dec["id"]), int(time.time())),
            )
            if cur.rowcount:
                created_outcomes += 1
                outcome_id = int(cur.lastrowid)
            else:
                row = conn.execute(
                    "SELECT id FROM causal_chain_outcomes WHERE chain_id=? AND trigger_ts=? "
                    "AND trigger_ref_id=?",
                    (ch.chain_id, int(ev_ts or 0), int(ev_id)),
                ).fetchone()
                if not row:
                    continue
                outcome_id = int(row[0])
                conn.execute(
                    "UPDATE causal_chain_outcomes SET decision_id=COALESCE(decision_id, ?), "
                    "decision_space=COALESCE(decision_space,'trade') WHERE id=?",
                    (int(dec["id"]), outcome_id),
                )
            link = conn.execute(
                "INSERT OR IGNORE INTO causal_chain_outcome_decisions "
                "(outcome_id, decision_id, chain_id, asset, action) VALUES (?,?,?,?,?)",
                (outcome_id, int(dec["id"]), str(ch.chain_id), asset,
                 (dec["final_action"] or dec["intended_action"] or "")),
            )
            if link.rowcount:
                created_links += 1
        if (i + 1) % 200 == 0:
            conn.commit()
            print("[PROGRESS] %d/%d 配对%d 链接%d (%.0fs)"
                  % (i + 1, len(decisions), matched_pairs, created_links, time.time() - t0))
    conn.commit()

    # ── 收敛：每条决策只保留**判别力最高**的 N 条链 ──
    # 历史遗留里有决策挂着 261 条链（笛卡尔积噪声）。链接数不收敛，
    # 反馈侧拿到的就是噪声而不是证据。
    # v321.3: 收敛的排序键必须与主排序**同源** —— 旧实现按"命中关键词长度",
    # 而主排序已改成"历史方向命中率" → 主排序刚挑出来的最准的链被收敛又按
    # 词数剪掉了 (实测: 新建 56 条链接, 收敛剪掉 56 条, 覆盖决策数纹丝不动)。
    # 用临时表把判别力带进 SQL。
    try:
        conn.execute("CREATE TEMP TABLE IF NOT EXISTS _chain_quality "
                     "(chain_id TEXT PRIMARY KEY, h INTEGER, n INTEGER)")
        conn.execute("DELETE FROM _chain_quality")
        conn.executemany("INSERT OR REPLACE INTO _chain_quality VALUES (?,?,?)",
                         [(cid, h, n) for cid, (h, n) in chain_quality.items()])
    except sqlite3.Error as _tq_exc:
        print(f"[WARN] 判别力临时表建立失败, 收敛退回词数排序: "
              f"{type(_tq_exc).__name__}: {_tq_exc}", flush=True)
        try:
            conn.rollback()
        except sqlite3.Error:
            pass

    pruned = 0
    try:
        cur = conn.execute(
            """DELETE FROM causal_chain_outcome_decisions WHERE id IN (
                   SELECT id FROM (
                     SELECT l.id,
                            ROW_NUMBER() OVER (
                              PARTITION BY l.decision_id
                              ORDER BY COALESCE((SELECT (1.0*q.h/q.n)
                                                 FROM _chain_quality q
                                                 WHERE q.chain_id = CAST(l.chain_id AS TEXT)),
                                                0.5) DESC,
                                       COALESCE((
                                         SELECT length(o.trigger_keywords_matched)
                                         FROM causal_chain_outcomes o WHERE o.id = l.outcome_id
                                       ), 0) DESC,
                                       l.id
                            ) AS rn
                     FROM causal_chain_outcome_decisions l
                   ) WHERE rn > ?
               )""",
            (args.max_chains_per_decision,),
        )
        pruned = cur.rowcount or 0
        if pruned:
            conn.commit()
    except Exception as _prune_exc:
        print("[WARN] 收敛超链接失败: %s: %s" % (type(_prune_exc).__name__, _prune_exc))

    total_links = conn.execute(
        "SELECT COUNT(*) FROM causal_chain_outcome_decisions").fetchone()[0]
    distinct_dec = conn.execute(
        "SELECT COUNT(DISTINCT decision_id) FROM causal_chain_outcome_decisions").fetchone()[0]
    distinct_chain = conn.execute(
        "SELECT COUNT(DISTINCT chain_id) FROM causal_chain_outcome_decisions").fetchone()[0]
    outcomes = conn.execute("SELECT COUNT(*) FROM causal_chain_outcomes").fetchone()[0]
    linked_outcomes = conn.execute(
        """SELECT COUNT(DISTINCT o.id) FROM causal_chain_outcomes o
           JOIN causal_chain_outcome_decisions l ON l.outcome_id=o.id""").fetchone()[0]
    print("[OK] 链×新闻×决策 配对 %d, 新建 outcome %d, 新建链接 %d (耗时 %.0fs)"
          % (matched_pairs, created_outcomes, created_links, time.time() - t0))
    if pruned:
        print("[OK] 收敛超链接 %d 条（每条决策最多保留 %d 条链）"
              % (pruned, args.max_chains_per_decision))
    print("[OK] 链接表总行 %d, 覆盖决策 %d, 覆盖链 %d; outcome 总 %d, 其中挂到决策 %d"
          % (total_links, distinct_dec, distinct_chain, outcomes, linked_outcomes))
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
