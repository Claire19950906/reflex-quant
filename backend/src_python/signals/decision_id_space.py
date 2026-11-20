# -*- coding: utf-8 -*-
"""统一决策编号空间 (decision id space) —— G08/G09/G13/G13b 的根因修复。

背景 (为什么需要这个模块)
------------------------
系统里"决策"有三个编号空间, 历史写入端各写各的:

  1. ai_decisions.id            —— AI 提案 (每条新闻 × 模型 × 资产 一条)
  2. trade_decisions.id         —— 最终执行决策 (guard/policy 之后)
  3. decision_errors.decision_id / decision_evidence.decision_id
                                —— 旧引擎时代的决策号 (与 trade_decisions.id 同空间)

而各表的 decision_id 字段写入口径在"AI 决策纪元"切换时改过:

  · news_events.decision_id     —— 旧纪元写 trade_decisions.id, 新纪元写 ai_decisions.id
  · signal_weight_history.decision_id —— 一直是 ai_decisions.id
  · trade_reflections_v2.decision_id  —— 一直是 trade_decisions.id

结果: 同一条"新闻→决策→反思→权重"链路, 三张表三套编号, join 永远对不上
(实测: 权重表 13321 行 100% 是 ai 空间, 反思表 2135 行 96% 是 trade 空间,
两者直接 join 只有 5 行 —— 权重"算不准"的根因就在这里, 不是算法问题)。

本模块确立**唯一权威口径**并提供解析/迁移函数:

  · 决策(decision) = trade_decisions.id     ← 最终决策, 有结算/反思/盈亏
  · 提案(proposal) = ai_decisions.id         ← AI 分析提案, 不一定成交
  · 新闻(news)     = news_events.news_id     ← 外部新闻号 (与 raw_news/decision_evidence 同空间)

所有跨表 join 一律走这里, 不再各表各猜。**不删任何历史行**, 只补列 + 补解析结果。
"""
from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional, Tuple

SPACE_TRADE = "trade"
SPACE_AI = "ai"
SPACE_NONE = ""

# 各表需要补的列 (幂等, 重复执行安全)
COLUMNS: Dict[str, List[Tuple[str, str]]] = {
    # 新闻 → 决策: decision_id 保留原值, 另存解析出来的两个空间的 id
    "news_events": [
        ("decision_space", "TEXT"),
        ("ai_decision_id", "INTEGER"),
        ("trade_decision_id", "INTEGER"),
    ],
    # 权重表: 标明 decision_id 属于哪个空间, 并给出最终决策 id
    "signal_weight_history": [
        ("decision_space", "TEXT"),
        ("ai_decision_id", "INTEGER"),
        ("trade_decision_id", "INTEGER"),
    ],
    # 反思表: 记录对应的 AI 提案 id (权重表是按提案算的)
    "trade_reflections_v2": [
        ("ai_decision_id", "INTEGER"),
    ],
    # 因果链结果: 记录这条链结果挂到了哪条真实决策 (闭环改链的前提)
    "causal_chain_outcomes": [
        ("decision_id", "INTEGER"),
        ("decision_space", "TEXT"),
    ],
}


def table_columns(conn: sqlite3.Connection, table: str) -> set:
    try:
        return {r[1] for r in conn.execute("PRAGMA table_info(%s)" % table).fetchall()}
    except sqlite3.Error:
        return set()


def ensure_columns(conn: sqlite3.Connection) -> List[str]:
    """补齐本模块需要的列 (幂等)。返回新增的列名列表。"""
    added: List[str] = []
    for table, cols in COLUMNS.items():
        existing = table_columns(conn, table)
        if not existing:
            continue
        for name, decl in cols:
            if name in existing:
                continue
            try:
                conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, name, decl))
                added.append("%s.%s" % (table, name))
            except sqlite3.OperationalError:
                pass
    return added



# ─────────────────────────────────────────────────────────────────────────────
# 解析 (resolution)
# ─────────────────────────────────────────────────────────────────────────────

class DecisionIndex:
    """一次性把各表读进内存, 供批量解析 (11k 新闻 + 13k 权重行, 逐行 SQL 太慢)。"""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.ai_by_id: Dict[int, Dict[str, Any]] = {}
        self.ai_by_news: Dict[int, List[int]] = {}
        self.td_by_id: Dict[int, Dict[str, Any]] = {}
        self.td_by_ai: Dict[int, List[int]] = {}
        self.td_by_news: Dict[int, List[int]] = {}
        self.de_td_by_news: Dict[int, List[int]] = {}
        self._load()

    def _load(self) -> None:
        c = self.conn
        for r in c.execute(
            "SELECT id, news_id, target_asset, suggested_action, ts, created_at FROM ai_decisions"
        ).fetchall():
            aid = int(r["id"] or 0)
            if not aid:
                continue
            self.ai_by_id[aid] = dict(r)
            if r["news_id"] is not None:
                self.ai_by_news.setdefault(int(r["news_id"]), []).append(aid)
        for r in c.execute(
            "SELECT id, news_id, ai_decision_id, decision_ts, asset, status FROM trade_decisions"
        ).fetchall():
            tid = int(r["id"] or 0)
            if not tid:
                continue
            self.td_by_id[tid] = dict(r)
            if r["ai_decision_id"] is not None:
                self.td_by_ai.setdefault(int(r["ai_decision_id"]), []).append(tid)
            if r["news_id"] is not None:
                self.td_by_news.setdefault(int(r["news_id"]), []).append(tid)
        # decision_evidence 里 decision_id 落在 trade 空间的行 = 系统自己记的
        # "这条决策用了这条新闻" (最权威的新闻→决策证据)
        try:
            for r in c.execute(
                "SELECT de.news_id, de.decision_id FROM decision_evidence de "
                "JOIN trade_decisions td ON td.id = de.decision_id"
            ).fetchall():
                if r["news_id"] is None:
                    continue
                self.de_td_by_news.setdefault(int(r["news_id"]), []).append(int(r["decision_id"]))
        except sqlite3.OperationalError:
            pass

    # ── 单条解析 ────────────────────────────────────────────────────────────
    def resolve_ai(self, news_id: Optional[int], asset: str = "", ts: int = 0) -> Optional[int]:
        if news_id is None:
            return None
        cands = self.ai_by_news.get(int(news_id)) or []
        if not cands:
            return None
        if asset:
            same = [a for a in cands
                    if str((self.ai_by_id[a].get("target_asset") or "")).upper() == asset.upper()]
            if same:
                cands = same
        if ts and len(cands) > 1:
            def _dist(a: int) -> Tuple[int, int]:
                raw = self.ai_by_id[a].get("ts") or self.ai_by_id[a].get("created_at") or 0
                try:
                    t = int(float(raw))
                except (TypeError, ValueError):
                    t = 0
                if t > 1_000_000_000_000:  # 毫秒
                    t //= 1000
                return (abs(t - int(ts)), t)
            cands = sorted(cands, key=_dist)
        return cands[0] if cands else None

    def resolve_trade(self, news_id: Optional[int], ts: int = 0,
                      ai_id: Optional[int] = None) -> Optional[int]:
        """解析新闻对应的最终决策 (trade_decisions.id)。"""
        if ai_id is not None:
            cands = self.td_by_ai.get(int(ai_id)) or []
            if cands:
                return self._closest(cands, ts)
        if news_id is not None:
            cands = self.de_td_by_news.get(int(news_id)) or []
            if cands:
                return self._closest(cands, ts)
            cands = self.td_by_news.get(int(news_id)) or []
            if cands:
                return self._closest(cands, ts)
        return None

    def _closest(self, cands: List[int], ts: int) -> int:
        if len(cands) == 1 or not ts:
            return cands[0]
        def _dist(tid: int) -> Tuple[int, float]:
            raw = self.td_by_id.get(tid, {}).get("decision_ts") or 0
            try:
                v = float(raw)
            except (TypeError, ValueError):
                v = 0.0
            return (abs(v - float(ts)), v)
        return sorted(cands, key=_dist)[0]

    def resolve_space(self, decision_id: Optional[int]) -> str:
        """判断一个裸 decision_id 属于哪个编号空间。"""
        if decision_id is None:
            return SPACE_NONE
        did = int(decision_id)
        if did in self.td_by_id:
            return SPACE_TRADE
        if did in self.ai_by_id:
            return SPACE_AI
        return SPACE_NONE


# ─────────────────────────────────────────────────────────────────────────────
# 迁移
# ─────────────────────────────────────────────────────────────────────────────

def migrate_news_decisions(conn: sqlite3.Connection, idx: Optional[DecisionIndex] = None) -> Dict[str, int]:
    """把 news_events 的决策链接补全。

    · decision_space: 原来的 decision_id 属于哪个空间 (trade / ai / '')
    · ai_decision_id: 解析出的 AI 提案 id
    · trade_decision_id: 解析出的最终决策 id
    · 悬空 decision_id (两个空间都找不到) → 置 NULL (事实: 那条决策已不存在)

    保留原 decision_id 值不变, 只是把它"归类 + 补链"。
    """
    idx = idx or DecisionIndex(conn)
    stats = {"rows": 0, "linked_trade": 0, "linked_ai": 0, "dangling_cleared": 0,
             "space_trade": 0, "space_ai": 0, "space_none": 0}
    rows = conn.execute(
        "SELECT id, news_id, decision_id, ts, asset FROM news_events ORDER BY id"
    ).fetchall()
    for r in rows:
        nid = r["id"]
        raw_did = r["decision_id"]
        space = idx.resolve_space(raw_did)
        ai_id: Optional[int] = None
        td_id: Optional[int] = None
        if space == SPACE_TRADE:
            td_id = int(raw_did)
        elif space == SPACE_AI:
            ai_id = int(raw_did)
        # 没有裸 id 可用时, 也要按新闻号反查 (系统自己记的证据链)
        if td_id is None:
            td_id = idx.resolve_trade(r["news_id"], int(r["ts"] or 0), ai_id)
        if ai_id is None:
            ai_id = idx.resolve_ai(r["news_id"], r["asset"] or "", int(r["ts"] or 0))
        new_raw = raw_did
        if space == SPACE_NONE and raw_did is not None:
            new_raw = None  # 悬空 → 清掉 (该决策已不存在于任何表)
            stats["dangling_cleared"] += 1
        conn.execute(
            "UPDATE news_events SET decision_id=?, decision_space=?, ai_decision_id=?, "
            "trade_decision_id=? WHERE id=?",
            (new_raw, space or SPACE_NONE, ai_id, td_id, nid),
        )
        stats["rows"] += 1
        if space == SPACE_TRADE:
            stats["space_trade"] += 1
        elif space == SPACE_AI:
            stats["space_ai"] += 1
        else:
            stats["space_none"] += 1
        if td_id:
            stats["linked_trade"] += 1
        if ai_id:
            stats["linked_ai"] += 1
    conn.commit()
    return stats




def migrate_signal_weight_space(conn: sqlite3.Connection,
                                idx: Optional[DecisionIndex] = None) -> Dict[str, int]:
    """给权重表补 decision_space / ai_decision_id / trade_decision_id。

    权重行本身的 decision_id 保持不变 (ai 空间, 100% 可解析), 另外把
    "这条权重最终落到哪条成交决策" 显式写出来 —— 这是权重能跟真实盈亏
    对账的前提 (G13/G13b 的根因)。
    """
    idx = idx or DecisionIndex(conn)
    stats = {"rows": 0, "space_ai": 0, "space_trade": 0, "space_none": 0, "linked_trade": 0}
    rows = conn.execute(
        "SELECT id, decision_id FROM signal_weight_history ORDER BY id"
    ).fetchall()
    for r in rows:
        did = int(r["decision_id"] or 0)
        space = idx.resolve_space(did)
        ai_id = did if space == SPACE_AI else None
        td_id = did if space == SPACE_TRADE else None
        if td_id is None and ai_id is not None:
            cands = idx.td_by_ai.get(ai_id) or []
            td_id = cands[0] if cands else None
        if ai_id is None and space == SPACE_NONE:
            ai_id = did  # 悬空权重行: 决策已不存在 → 保留原值供追溯
        conn.execute(
            "UPDATE signal_weight_history SET decision_space=?, ai_decision_id=?, "
            "trade_decision_id=? WHERE id=?",
            (space or SPACE_NONE, ai_id, td_id, int(r["id"])),
        )
        stats["rows"] += 1
        key = "space_%s" % (space or "none")
        stats[key] = stats.get(key, 0) + 1
        if td_id:
            stats["linked_trade"] += 1
    conn.commit()
    return stats


def repair_orphan_trade_decision_refs(conn: sqlite3.Connection) -> Dict[str, int]:
    """清理 trade_decisions 里指向**已删除行**的上游引用 (v321.3 P1, 防复发)。

    为什么必须有这个: 「重置交易历史」(_clear_trading_traces) 会删掉
    ai_decisions / raw_news / news_events, 但 trade_decisions 的两个指针
    (ai_decision_id / news_id) 还指着它们 → 变成**死指针**。
    实测那次重置留下 1998 条孤儿 ai_decision_id + 1783 条孤儿 news_id,
    后果是: 对账永远有悬空 / 按 id 的关联全部错位 / 新旧数据混在一个空间。

    这里把它们**置 NULL** (不是删决策 —— 决策的 asset/action/结算结果还要用于
    复盘与校准), 并在 context_json 留一笔「上游已随重置清除」的痕迹。
    跑在 reconcile 里 → 每次重置后自动收敛, 不会再攒出几千条孤儿。

    ⚠️ news_id 的孤儿判定必须用 `news_events.news_id` 的**值相等**, 不能用
    `raw_news.id` 的存在性 —— 追溯靠的是值相等, raw_news 行被删不代表指针失效。
    (第一版用 raw_news 判定, 误清 1780 条本来可追溯的决策: traceable 3233→1453)
    """
    stats = {"orphan_ai_decision_id": 0, "orphan_news_id": 0}
    try:
        cur = conn.execute(
            "UPDATE trade_decisions SET ai_decision_id=NULL "
            "WHERE ai_decision_id IS NOT NULL "
            "  AND NOT EXISTS (SELECT 1 FROM ai_decisions a "
            "                  WHERE a.id = trade_decisions.ai_decision_id)"
        )
        stats["orphan_ai_decision_id"] = cur.rowcount or 0
        cur = conn.execute(
            "UPDATE trade_decisions SET news_id=NULL "
            "WHERE news_id IS NOT NULL "
            "  AND NOT EXISTS (SELECT 1 FROM news_events n WHERE n.news_id = trade_decisions.news_id) "
            "  AND NOT EXISTS (SELECT 1 FROM news_events n WHERE n.id = trade_decisions.news_id)"
        )
        stats["orphan_news_id"] = cur.rowcount or 0
        conn.commit()
    except sqlite3.Error as exc:
        print(f"[ID-SPACE] trade_decisions 孤儿清理失败: {type(exc).__name__}: {exc}",
              flush=True)
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
    return stats


def migrate_reflection_ai_link(conn: sqlite3.Connection,
                               idx: Optional[DecisionIndex] = None) -> Dict[str, int]:
    """给反思表补 ai_decision_id: 权重是按 AI 提案算的, 反思是按成交决策记的,
    不把这条链接补上, 就没法拿"权重"去对账"真实盈亏"。
    """
    idx = idx or DecisionIndex(conn)
    stats = {"rows": 0, "linked_ai": 0}
    rows = conn.execute("SELECT id, decision_id FROM trade_reflections_v2").fetchall()
    for r in rows:
        did = int(r["decision_id"] or 0)
        ai_id = None
        if did in idx.ai_by_id:
            ai_id = did
        else:
            td = idx.td_by_id.get(did) or {}
            if td.get("ai_decision_id"):
                ai_id = int(td["ai_decision_id"])
            elif td.get("news_id") is not None:
                ai_id = idx.resolve_ai(int(td["news_id"]), td.get("asset") or "", 0)
        conn.execute("UPDATE trade_reflections_v2 SET ai_decision_id=? WHERE id=?",
                     (ai_id, int(r["id"])))
        stats["rows"] += 1
        if ai_id:
            stats["linked_ai"] += 1
    conn.commit()
    return stats


def repair_missing_weight_space(conn: sqlite3.Connection,
                                idx: Optional[DecisionIndex] = None) -> Dict[str, int]:
    """**定向**补齐权重表里 decision_space IS NULL 的行 —— 不碰其他行。

    v313 P1 (数据修复): 历史上有 4218 行权重 (538 insert + 3680 cron_react)
    因为写入端的降级分支 (把 "database is locked" 误判成"老库缺列", 见
    signal_weight_evaluator.evaluate_and_persist) 落库时没带空间字段。
    这些行的 decision_id **100% 对应 ai_decisions.id** (实测 538/538 与
    3680/3680 全部命中 ai_decisions, 且 ai_decisions.id ∈ [2554,7195] 与
    trade_decisions.id ∈ [5,2111] **无交集**, 所以编号空间判定无歧义)。

    不修的后果: 这些权重在验收 #2 的 join
        h.decision_space='ai' AND h.ai_decision_id=a.id
    里永远配不上真值 —— 决策时点权重写了, 但对账时"查无此权重"。

    为什么单独写一个定向函数, 而不直接跑 migrate_signal_weight_space:
    全量迁移会对 20079 行都重算 trade_decision_id, 那是另一个语义的变更;
    本函数只补 NULL 的, 改动面最小、可反复执行 (幂等)。
    """
    idx = idx or DecisionIndex(conn)
    stats: Dict[str, int] = {"scanned": 0, "repaired": 0,
                             "space_ai": 0, "space_trade": 0, "space_none": 0}
    rows = conn.execute(
        "SELECT id, decision_id FROM signal_weight_history "
        "WHERE decision_space IS NULL ORDER BY id"
    ).fetchall()
    for r in rows:
        stats["scanned"] += 1
        # 下标取值, 不依赖 row_factory (db.get_connection() 返回 tuple 行)
        did = int(r[1] or 0)
        space = idx.resolve_space(did)
        ai_id = did if space == SPACE_AI else None
        td_id = did if space == SPACE_TRADE else None
        if td_id is None and ai_id is not None:
            cands = idx.td_by_ai.get(ai_id) or []
            td_id = cands[0] if cands else None
        conn.execute(
            "UPDATE signal_weight_history SET decision_space=?, ai_decision_id=?, "
            "trade_decision_id=? WHERE id=?",
            (space or SPACE_NONE, ai_id, td_id, int(r["id"])),
        )
        stats["repaired"] += 1
        key = "space_%s" % (space or "none")
        stats[key] = stats.get(key, 0) + 1
    conn.commit()
    return stats


def audit(conn: sqlite3.Connection) -> Dict[str, Any]:
    """当前编号空间健康度 (供门禁与审计脚本读取)。"""
    def q(sql: str) -> int:
        try:
            return int(conn.execute(sql).fetchone()[0] or 0)
        except sqlite3.OperationalError:
            return 0
    return {
        "news_total": q("SELECT COUNT(*) FROM news_events"),
        "news_with_decision": q("SELECT COUNT(*) FROM news_events WHERE decision_id IS NOT NULL"),
        "news_dangling": q("""SELECT COUNT(*) FROM news_events n
            WHERE n.decision_id IS NOT NULL
              AND NOT EXISTS(SELECT 1 FROM trade_decisions d WHERE d.id=n.decision_id)
              AND NOT EXISTS(SELECT 1 FROM ai_decisions a WHERE a.id=n.decision_id)"""),
        # v321.3 P1: 决策侧的孤儿引用 (指向已删的 ai_decisions / raw_news)
        "td_orphan_ai_decision_id": q("""SELECT COUNT(*) FROM trade_decisions d
            WHERE d.ai_decision_id IS NOT NULL
              AND NOT EXISTS(SELECT 1 FROM ai_decisions a WHERE a.id=d.ai_decision_id)"""),
        "td_orphan_news_id": q("""SELECT COUNT(*) FROM trade_decisions d
            WHERE d.news_id IS NOT NULL
              AND NOT EXISTS(SELECT 1 FROM raw_news r WHERE r.id=d.news_id)
              AND NOT EXISTS(SELECT 1 FROM news_events n WHERE n.id=d.news_id)"""),
        "td_total": q("SELECT COUNT(*) FROM trade_decisions"),
        # 决策 → 新闻 的可追溯率 (需求 #5「串联互通」的真实度量)
        "td_traceable": q("""SELECT COUNT(DISTINCT d.id) FROM trade_decisions d
            WHERE EXISTS(SELECT 1 FROM ai_decisions a WHERE a.id=d.ai_decision_id
                        AND EXISTS(SELECT 1 FROM news_events n WHERE n.ai_decision_id=a.id))
               OR EXISTS(SELECT 1 FROM news_events n WHERE n.news_id=d.news_id)
               OR EXISTS(SELECT 1 FROM raw_news r WHERE r.id=d.news_id
                        AND EXISTS(SELECT 1 FROM news_events n WHERE n.news_id=r.id))"""),
        "news_trade_linked": q("SELECT COUNT(*) FROM news_events WHERE trade_decision_id IS NOT NULL"),
        "news_ai_linked": q("SELECT COUNT(*) FROM news_events WHERE ai_decision_id IS NOT NULL"),
        "swh_total": q("SELECT COUNT(*) FROM signal_weight_history"),
        "swh_trade_space": q("SELECT COUNT(*) FROM signal_weight_history WHERE decision_space='trade'"),
        "swh_ai_space": q("SELECT COUNT(*) FROM signal_weight_history WHERE decision_space='ai'"),
        "trv2_total": q("SELECT COUNT(*) FROM trade_reflections_v2"),
        "trv2_ai_linked": q("SELECT COUNT(*) FROM trade_reflections_v2 WHERE ai_decision_id IS NOT NULL"),
    }


__all__ = [
    "SPACE_TRADE", "SPACE_AI", "SPACE_NONE", "COLUMNS",
    "ensure_columns", "table_columns", "DecisionIndex",
    "migrate_news_decisions", "migrate_signal_weight_space",
    "migrate_reflection_ai_link", "repair_missing_weight_space",
    "repair_orphan_trade_decision_refs", "audit",
]
