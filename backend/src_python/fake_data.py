"""禁止假数据 —— 全库审计、清理与守卫（v401）

原则（用户原话："以后在这个系统里不能出现任何假的数据"）：

    任何入库的数值或文本，必须能追溯到**真实观测**——真实 tick、真实新闻原文、
    真实 LLM 原文。占位值、默认值、模板拼出来的"判断"，一律不许作为有效值存在：
    要么置 NULL（表示"不知道"），要么删行（表示"这条本来就不该存在"）。
    绝不允许第三种处理方式：编一个看起来合理的数把它填上。

本项目真实发生过的形态，本模块逐条对应：

    F1  evidence_confidence=0.0 占位 —— 降级路径没算过置信度，却按"低分"参与统计
    F2  impact_score=5.0 默认值 —— 门卫没打分，却按"影响力 5"参与权重
    F3  trade_reflections_v2.confidence_at_time=0 占位
    F4  quick_sim_trades 引用降级决策 —— R11 明令禁止建仓，历史却建了 2 笔
    F5  shadow_ledger 引用降级决策
    F6  market_ticks / news_events 出现 demo/mock/synthetic 源（造出来的行情/新闻）
    F7  决策指向不存在的新闻（孤儿行，事件本身不存在）
    F8  历史回放混进真实绩效表（quick_sim_trades 出现 replay 标识）—— 回放可反复重跑，
        混进胜率/Sharpe 就等于用能刷的样本池自证
    F9  回放行没有来源标签（t2_sim_trades.source 为空/未知）—— 分不清哪些是回放，
        隔离就失效
    F11 自述"没做分析"却没置降级位 —— reasoning 里写着"未做全文分析/关键词推断"，
        但 analysis_degraded=0，于是这种"半个判断"照样进统计、还能过 R11 建仓
    F12 推理链为空/极短却没置降级位（<20 字）—— 与 F11 同性质，只是没自述

两种用法：
    python3 fake_data.py                 # 审计（只读，打印每类的条数与样本）
    python3 fake_data.py --commit        # 清理（先备份库！CLI 不会自动备份）
    python3 fake_data.py --guard         # 守卫：有任何一类 > 0 就退出码 1
"""
from __future__ import annotations

import argparse
import datetime
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

DB_PATH = Path(__file__).resolve().parents[1] / "trident_event_bus.db"

# 造数据的源标记。真实源（sina / median / jin10 / reuters…）不在此列。
# v453: 补 test / probe —— 实测 market_ticks 里进过一条 source='test' price=100 的
# BTC tick（真价 62,414），既让事件影响算出 99.8%，又和一条假成交的入场价 100 对得上。
# 用 \btest\b 而不是裸 test，避免误伤 "latest" 这类正常词。
SYNTHETIC_SOURCE_RE = re.compile(r"(demo|mock|synth|fake|placeholder|\btest\b|probe)", re.I)


def _connect(readonly: bool) -> sqlite3.Connection:
    if readonly:
        return sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    # 生产库有引擎/桥接在并发写：默认 5s 锁等待必然 "database is locked"，
    # 给足 120s，并显式设 busy_timeout（sqlite3 timeout 参数只管连接建立期）
    conn = sqlite3.connect(DB_PATH, timeout=120)
    conn.execute("PRAGMA busy_timeout=120000")
    return conn


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _has_col(conn: sqlite3.Connection, table: str, col: str) -> bool:
    if not _has_table(conn, table):
        return False
    return col in {r[1] for r in conn.execute(f"PRAGMA table_info('{table}')")}


def _count(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> int:
    try:
        return int(conn.execute(sql, args).fetchone()[0] or 0)
    except sqlite3.OperationalError:
        return 0


def _sample_ids(conn: sqlite3.Connection, sql: str, args: tuple = (), n: int = 5) -> list:
    try:
        return [r[0] for r in conn.execute(sql + f" LIMIT {n}", args)]
    except sqlite3.OperationalError:
        return []


# ── 七类假数据：审计 ────────────────────────────────────────────────────
# 每条规则返回 {code, name, count, action, samples}；action 说明清理方式。

_DEGRADED = "COALESCE(d.analysis_degraded, 0) = 1"


def scan(conn: sqlite3.Connection) -> list[dict]:
    out: list[dict] = []

    # F1 置信度占位：降级行里 confidence=0 —— 0 是"没算过"，不是"算出来很低"
    f1 = _count(conn, f"""SELECT COUNT(*) FROM ai_decisions d
                          WHERE COALESCE(d.evidence_confidence, -1) = 0 AND {_DEGRADED}""")
    out.append({"code": "F1", "name": "置信度 0 占位（降级行）", "count": f1,
                "action": "置 NULL",
                "samples": _sample_ids(conn, f"""SELECT d.id FROM ai_decisions d
                          WHERE COALESCE(d.evidence_confidence, -1) = 0 AND {_DEGRADED}""")})

    # F2 影响力默认值：impact_score=5.0 且没有任何门卫证据
    f2 = _count(conn, """SELECT COUNT(*) FROM ai_decisions d
                         LEFT JOIN raw_news r ON r.id = d.news_id
                         WHERE d.impact_score = 5.0
                           AND COALESCE(r.gate_impact_1to100, 0) = 0
                           AND COALESCE(r.gate_strength, 0) = 0""")
    out.append({"code": "F2", "name": "影响力 5.0 默认值（无门卫证据）", "count": f2,
                "action": "置 NULL",
                "samples": _sample_ids(conn, """SELECT d.id FROM ai_decisions d
                         LEFT JOIN raw_news r ON r.id = d.news_id
                         WHERE d.impact_score = 5.0
                           AND COALESCE(r.gate_impact_1to100, 0) = 0
                           AND COALESCE(r.gate_strength, 0) = 0""")})

    # F3 反思置信度占位
    f3 = 0
    if _has_col(conn, "trade_reflections_v2", "confidence_at_time"):
        f3 = _count(conn, """SELECT COUNT(*) FROM trade_reflections_v2
                             WHERE COALESCE(confidence_at_time, -1) = 0""")
    out.append({"code": "F3", "name": "反思 confidence_at_time=0 占位", "count": f3,
                "action": "置 NULL",
                "samples": _sample_ids(conn, """SELECT id FROM trade_reflections_v2
                             WHERE COALESCE(confidence_at_time, -1) = 0""") if f3 else []})

    # F4 假成交：引用降级决策的模拟成交
    f4 = _count(conn, f"""SELECT COUNT(*) FROM quick_sim_trades t
                          JOIN ai_decisions d ON d.id = t.decision_id
                          WHERE {_DEGRADED}""")
    out.append({"code": "F4", "name": "模拟成交引用降级决策（假成交）", "count": f4,
                "action": "删行",
                "samples": _sample_ids(conn, f"""SELECT t.id FROM quick_sim_trades t
                          JOIN ai_decisions d ON d.id = t.decision_id
                          WHERE {_DEGRADED}""")})

    # F5 影子样本引用降级决策
    f5 = _count(conn, f"""SELECT COUNT(*) FROM shadow_ledger s
                          JOIN ai_decisions d ON d.id = s.decision_id
                          WHERE {_DEGRADED}""")
    out.append({"code": "F5", "name": "影子样本引用降级决策", "count": f5,
                "action": "删行",
                "samples": _sample_ids(conn, f"""SELECT s.decision_id FROM shadow_ledger s
                          JOIN ai_decisions d ON d.id = s.decision_id
                          WHERE {_DEGRADED}""")})

    # F6 造出来的行情/新闻（demo/mock/synthetic 源）
    f6 = 0
    for tbl in ("market_ticks", "news_events", "raw_news"):
        if _has_col(conn, tbl, "source"):
            for r in conn.execute(f"SELECT DISTINCT source FROM {tbl} WHERE source IS NOT NULL"):
                if SYNTHETIC_SOURCE_RE.search(str(r[0])):
                    f6 += _count(conn, f"SELECT COUNT(*) FROM {tbl} WHERE source = ?", (r[0],))
    out.append({"code": "F6", "name": "demo/mock/合成 源的行情或新闻", "count": f6,
                "action": "删行", "samples": []})

    # F7 孤儿决策：指向不存在的新闻
    f7 = _count(conn, """SELECT COUNT(*) FROM ai_decisions d
                         LEFT JOIN raw_news r ON r.id = d.news_id
                         WHERE d.news_id IS NOT NULL AND r.id IS NULL""")
    out.append({"code": "F7", "name": "决策指向不存在的新闻（孤儿）", "count": f7,
                "action": "删行",
                "samples": _sample_ids(conn, """SELECT d.id FROM ai_decisions d
                         LEFT JOIN raw_news r ON r.id = d.news_id
                         WHERE d.news_id IS NOT NULL AND r.id IS NULL""") if f7 else []})

    # F10 孤儿成交：quick_sim_trades 的 decision_id 在 ai_decisions 里不存在
    #   典型来源：重置删了决策但没删成交、或手工/脚本塞进来的探针行。
    #   这类行没有任何决策依据可复核，按 "不许假数据" 直接清。
    f10 = _count(conn, """SELECT COUNT(*) FROM quick_sim_trades t
                          LEFT JOIN ai_decisions d ON d.id = t.decision_id
                          WHERE d.id IS NULL""")
    out.append({"code": "F10", "name": "成交引用了不存在的决策（孤儿成交）", "count": f10,
                "action": "删行",
                "samples": _sample_ids(conn, """SELECT t.id FROM quick_sim_trades t
                          LEFT JOIN ai_decisions d ON d.id = t.decision_id
                          WHERE d.id IS NULL""") if f10 else []})

    # F8 回放泄漏进真实绩效表
    f8 = 0
    if _has_table(conn, "quick_sim_trades"):
        f8 = _count(conn, """SELECT COUNT(*) FROM quick_sim_trades
                             WHERE LOWER(COALESCE(trade_uuid, '') || ' ' ||
                                         COALESCE(strategy_id, '') || ' ' ||
                                         COALESCE(paper_trading_run_id, '')) LIKE '%replay%'""")
    out.append({"code": "F8", "name": "回放混进真实绩效表（quick_sim_trades 带 replay 标识）",
                "count": f8, "action": "删行（回放不属于真实成交）",
                "samples": _sample_ids(conn, """SELECT id FROM quick_sim_trades
                             WHERE LOWER(COALESCE(trade_uuid, '') || ' ' ||
                                         COALESCE(strategy_id, '') || ' ' ||
                                         COALESCE(paper_trading_run_id, '')) LIKE '%replay%'""") if f8 else []})

    # F9 回放行无来源标签
    f9 = 0
    if _has_table(conn, "t2_sim_trades"):
        f9 = _count(conn, """SELECT COUNT(*) FROM t2_sim_trades
                             WHERE source IS NULL OR TRIM(source) = ''
                                OR source NOT IN ('news_replay', 'decision_replay')""")
    out.append({"code": "F9", "name": "回放行缺少来源标签（无法与真实样本区分）", "count": f9,
                "action": "人工核查（补 source 或删行，不自动处理）", "samples": []})

    # F11 自述降级未置位：文本自己承认"没做全文分析/只按关键词推断"，字段却是 0。
    #     这类行不是"假数值"，而是"状态标签假"—— 它会被当成完整判断参与胜率、
    #     过 R11 建仓，实际没有任何全文依据。按原文自述置位，不编造。
    f11 = 0
    if _has_table(conn, "ai_decisions"):
        f11 = _count(conn, """SELECT COUNT(*) FROM ai_decisions d
                              WHERE COALESCE(d.analysis_degraded, 0) = 0
                                AND NOT EXISTS (SELECT 1 FROM quick_sim_trades qt
                                                WHERE qt.decision_id = d.id)
                                AND (COALESCE(d.reasoning, '') LIKE '%未做全文分析%'
                                  OR COALESCE(d.reasoning, '') LIKE '%关键词推断%'
                                  OR COALESCE(d.reasoning, '') LIKE '%异常降级%'
                                  OR COALESCE(d.reasoning_path, '') LIKE '%未做全文分析%'
                                  OR COALESCE(d.reasoning_path, '') LIKE '%关键词推断%'
                                  OR COALESCE(d.reasoning_path, '') LIKE '%异常降级%')""")
    out.append({"code": "F11", "name": "自述未做分析却未置降级位", "count": f11,
                "action": "置 analysis_degraded=1（按原文自述，不编造）",
                "samples": _sample_ids(conn, """SELECT d.id FROM ai_decisions d
                              WHERE COALESCE(d.analysis_degraded, 0) = 0
                                AND NOT EXISTS (SELECT 1 FROM quick_sim_trades qt
                                                WHERE qt.decision_id = d.id)
                                AND (COALESCE(d.reasoning, '') LIKE '%未做全文分析%'
                                  OR COALESCE(d.reasoning, '') LIKE '%关键词推断%'
                                  OR COALESCE(d.reasoning, '') LIKE '%异常降级%'
                                  OR COALESCE(d.reasoning_path, '') LIKE '%未做全文分析%'
                                  OR COALESCE(d.reasoning_path, '') LIKE '%关键词推断%'
                                  OR COALESCE(d.reasoning_path, '') LIKE '%异常降级%')""") if f11 else []})

    # F12 推理链为空/极短却未置降级位
    f12 = 0
    if _has_table(conn, "ai_decisions"):
        f12 = _count(conn, """SELECT COUNT(*) FROM ai_decisions d
                              WHERE COALESCE(d.analysis_degraded, 0) = 0
                                AND NOT EXISTS (SELECT 1 FROM quick_sim_trades qt
                                                WHERE qt.decision_id = d.id)
                                AND LENGTH(TRIM(COALESCE(d.reasoning, ''))) < 20""")
    out.append({"code": "F12", "name": "推理链为空/极短却未置降级位（<20 字）", "count": f12,
                "action": "置 analysis_degraded=1（无推理链即降级）",
                "samples": _sample_ids(conn, """SELECT d.id FROM ai_decisions d
                              WHERE COALESCE(d.analysis_degraded, 0) = 0
                                AND NOT EXISTS (SELECT 1 FROM quick_sim_trades qt
                                                WHERE qt.decision_id = d.id)
                                AND LENGTH(TRIM(COALESCE(d.reasoning, ''))) < 20""") if f12 else []})

    return out


def total_fake(rows: list[dict]) -> int:
    return sum(r["count"] for r in rows)


# ── 清理 ─────────────────────────────────────────────────────────────

def _exec_retry(conn: sqlite3.Connection, sql: str, args: tuple = (),
                attempts: int = 10, wait: float = 6.0) -> int:
    """写锁被引擎占住时不要硬顶：退避重试。生产库 24h 都有人在写。"""
    import time as _t
    last: Exception | None = None
    for i in range(attempts):
        try:
            return int(conn.execute(sql, args).rowcount)
        except sqlite3.OperationalError as e:
            last = e
            if "locked" not in str(e).lower():
                raise
            _t.sleep(wait * (1 + i // 3))
    raise last  # type: ignore[misc]


def _bulk_by_ids(conn: sqlite3.Connection, table: str, set_sql: str,
                 ids: list[int], chunk: int = 80, key: str = "id") -> int:
    """按主键分批 UPDATE/DELETE，每批单独提交 —— 长事务会把写锁攥住，分批即用即放。"""
    n = 0
    for i in range(0, len(ids), chunk):
        part = ids[i:i + chunk]
        ph = ",".join("?" * len(part))
        n += _exec_retry(conn, f"{set_sql} WHERE {key} IN ({ph})", tuple(part))
        conn.commit()
    return n


def rebuild_equity_snapshots(conn: sqlite3.Connection, initial: float = 10_000.0) -> int:
    """删掉假成交后，净值快照必须重算——否则 S5「账实相符」立刻对不上。

    快照里原来的 realized_pnl 是把假成交算进去的，留着就是"无法被一条 SQL 复现的数字"。
    这里按剩余的真实成交重算 equity / realized_pnl / n_settled / peak / drawdown，
    并在 note 上标明"已剔除假成交后重算"，不掩盖改过。
    """
    if not _has_table(conn, "sim_equity_snapshots"):
        return 0
    trades = conn.execute(
        "SELECT COALESCE(pnl_usdt, 0), COALESCE(exit_ts, 0) FROM quick_sim_trades WHERE settled = 1"
    ).fetchall()
    snaps = conn.execute("SELECT rowid, ts, note FROM sim_equity_snapshots ORDER BY ts").fetchall()
    n = 0
    peak = initial
    for rowid, ts, note in snaps:
        realized = sum(p for p, ets in trades if ets <= ts)
        equity = initial + realized
        peak = max(peak, equity)
        dd = (equity - peak) / peak * 100 if peak else 0.0
        cuma = sum(1 for _, ets in trades if ets <= ts)
        _exec_retry(conn,
                    """UPDATE sim_equity_snapshots
                       SET equity=?, realized_pnl=?, n_settled=?, peak_equity=?, drawdown_pct=?,
                           note=?
                       WHERE rowid=?""",
                    (round(equity, 4), round(realized, 4), cuma, round(peak, 4),
                     round(dd, 4), ("[已剔除假成交后重算] " + (note or "")).strip(), rowid))
        conn.commit()
        n += 1
    return n


def _ids(conn: sqlite3.Connection, sql: str) -> list[int]:
    return [int(r[0]) for r in conn.execute(sql).fetchall()]


def purge(conn: sqlite3.Connection, rows: list[dict] | None = None) -> dict:
    """按 scan() 的结论清理，返回每类实际影响行数。

    全部按主键分批 + 每批 commit + 写锁退避重试：生产库里有引擎在持续写，
    单条长事务会把锁攥住几十秒，分批能把冲突窗口压到毫秒级。
    """
    rows = rows or scan(conn)
    done: dict[str, int] = {}

    f1 = _ids(conn, """SELECT id FROM ai_decisions
                       WHERE COALESCE(evidence_confidence, -1) = 0
                         AND COALESCE(analysis_degraded, 0) = 1""")
    done["F1"] = _bulk_by_ids(conn, "ai_decisions",
                              "UPDATE ai_decisions SET evidence_confidence = NULL", f1)

    # F2 影响力默认值：有门卫证据的行先交给 backfill_impact_score.py 回填（与引擎同一份 ladder），
    #    回填不了的（门卫压根没跑）才置 NULL —— 这里只负责后者，不自己编映射。
    f2 = _ids(conn, """SELECT d.id FROM ai_decisions d
                       LEFT JOIN raw_news r ON r.id = d.news_id
                       WHERE d.impact_score = 5.0
                         AND COALESCE(r.gate_impact_1to100, 0) = 0
                         AND COALESCE(r.gate_strength, 0) = 0""")
    done["F2"] = _bulk_by_ids(conn, "ai_decisions",
                              "UPDATE ai_decisions SET impact_score = NULL", f2)

    done["F3"] = 0
    if _has_col(conn, "trade_reflections_v2", "confidence_at_time"):
        f3 = _ids(conn, """SELECT id FROM trade_reflections_v2
                           WHERE COALESCE(confidence_at_time, -1) = 0""")
        done["F3"] = _bulk_by_ids(conn, "trade_reflections_v2",
                                  "UPDATE trade_reflections_v2 SET confidence_at_time = NULL", f3)

    f4 = _ids(conn, f"""SELECT t.id FROM quick_sim_trades t
                        JOIN ai_decisions d ON d.id = t.decision_id
                        WHERE {_DEGRADED}""")
    done["F4"] = _bulk_by_ids(conn, "quick_sim_trades",
                              "DELETE FROM quick_sim_trades", f4)

    f5 = _ids(conn, f"""SELECT s.decision_id FROM shadow_ledger s
                        JOIN ai_decisions d ON d.id = s.decision_id
                        WHERE {_DEGRADED}""")
    done["F5"] = _bulk_by_ids(conn, "shadow_ledger",
                              "DELETE FROM shadow_ledger", f5, key="decision_id")

    done["F6"] = 0
    for tbl in ("market_ticks", "news_events", "raw_news"):
        if not _has_col(conn, tbl, "source"):
            continue
        for r in conn.execute(f"SELECT DISTINCT source FROM {tbl} WHERE source IS NOT NULL").fetchall():
            if SYNTHETIC_SOURCE_RE.search(str(r[0])):
                done["F6"] += _exec_retry(conn, f"DELETE FROM {tbl} WHERE source = ?", (r[0],))
                conn.commit()

    done["F8"] = 0
    if _has_table(conn, "quick_sim_trades"):
        f8 = _ids(conn, """SELECT id FROM quick_sim_trades
                           WHERE LOWER(COALESCE(trade_uuid, '') || ' ' ||
                                       COALESCE(strategy_id, '') || ' ' ||
                                       COALESCE(paper_trading_run_id, '')) LIKE '%replay%'""")
        done["F8"] = _bulk_by_ids(conn, "quick_sim_trades", "DELETE FROM quick_sim_trades", f8)

    f10 = _ids(conn, """SELECT t.id FROM quick_sim_trades t
                        LEFT JOIN ai_decisions d ON d.id = t.decision_id
                        WHERE d.id IS NULL""")
    done["F10"] = _bulk_by_ids(conn, "quick_sim_trades", "DELETE FROM quick_sim_trades", f10)

    done["F9"] = 0   # 来源标签缺失需人工判断，不自动改

    f7 = _ids(conn, """SELECT d.id FROM ai_decisions d
                       LEFT JOIN raw_news r ON r.id = d.news_id
                       WHERE d.news_id IS NOT NULL AND r.id IS NULL""")
    # v477: 删之前先归档。F7 的判据是"源新闻已不存在"（重置会清 raw_news），
    # 于是**重置→孤儿判定→删决策**这条链会把已经结算的胜负样本一起抹掉，
    # 而胜率/赔率恰恰要靠这些行累积。归档是真实数据的搬迁，不是补造。
    _f7_archived = 0
    if f7 and _has_table(conn, "ai_decisions"):
        try:
            from api_maintenance import _ensure_decision_history as _edh
            _edh(conn)
            _cols = [r[1] for r in conn.execute("PRAGMA table_info(ai_decisions)")]
            _keep = [c for c in (
                "id", "news_id", "created_at", "ts", "suggested_action",
                "sentiment_score", "target_asset", "market_category", "reasoning",
                "settled", "is_correct", "forward_pnl", "entry_price", "entry_time",
                "truth_verdict", "strategy_id", "original_action", "gate_chain",
            ) if c in _cols]
            _ids_txt = ",".join(str(int(x)) for x in f7)
            conn.execute(
                f"INSERT OR IGNORE INTO ai_decisions_history "
                f"({', '.join(_keep)}, reset_at, reset_batch) "
                f"SELECT {', '.join(_keep)}, "
                f"'{time.strftime('%Y-%m-%d %H:%M:%S')}', 'f7_orphan_purge' "
                f"FROM ai_decisions WHERE id IN ({_ids_txt})")
            _f7_archived = conn.execute(
                f"SELECT COUNT(*) FROM ai_decisions_history "
                f"WHERE reset_batch='f7_orphan_purge'").fetchone()[0]
        except Exception:
            _f7_archived = 0
    done["F7"] = _bulk_by_ids(conn, "ai_decisions", "DELETE FROM ai_decisions", f7)
    if done["F7"] or _f7_archived:
        print(f"[FAKE] F7 清理孤儿决策 {done['F7']} 条，"
              f"归档保留 {_f7_archived} 条（累计 history）")

    # 删了成交就必须重算净值快照，否则「账实相符」当场对不上
    done["F4b_snapshots"] = rebuild_equity_snapshots(conn) if done["F4"] else 0

    conn.commit()
    # ── F11/F12 放在最后：只补状态标签，不参与任何"删行/删成交"的级联判断。
    #   若放在前面，刚被标记成降级的决策会立刻让引用它的成交被 F4/F5 删掉 ——
    #   这等于用"标签修正"顺手销毁真实成交记录，方向错了。
    # F11/F12：不改内容，只把"自述降级但字段没置位"的状态标签补上。
    #   两类共用同一列 analysis_degraded=1，所以一次 UPDATE 覆盖，分开计数。
    _self11 = """COALESCE(analysis_degraded,0)=0
                AND NOT EXISTS (SELECT 1 FROM quick_sim_trades qt WHERE qt.decision_id = ai_decisions.id)
                AND (
                   COALESCE(reasoning,'') LIKE '%未做全文分析%'
                OR COALESCE(reasoning,'') LIKE '%关键词推断%'
                OR COALESCE(reasoning,'') LIKE '%异常降级%'
                OR COALESCE(reasoning_path,'') LIKE '%未做全文分析%'
                OR COALESCE(reasoning_path,'') LIKE '%关键词推断%'
                OR COALESCE(reasoning_path,'') LIKE '%异常降级%')"""
    _self12 = ("COALESCE(analysis_degraded,0)=0 "
                 "AND NOT EXISTS (SELECT 1 FROM quick_sim_trades qt "
                 "WHERE qt.decision_id = ai_decisions.id) "
                 "AND LENGTH(TRIM(COALESCE(reasoning,'')))<20")
    f11 = _ids(conn, f"SELECT id FROM ai_decisions WHERE {_self11}")
    f12 = _ids(conn, f"SELECT id FROM ai_decisions WHERE {_self12}")
    _mark = sorted(set(f11) | set(f12))
    done["F11"] = _bulk_by_ids(conn, "ai_decisions",
                               "UPDATE ai_decisions SET analysis_degraded = 1", _mark)
    done["F12"] = 0  # 与 F11 共用同一条 UPDATE，避免同一行被统计两次

    return done


def main() -> int:
    ap = argparse.ArgumentParser(description="禁止假数据：审计 / 清理 / 守卫")
    ap.add_argument("--commit", action="store_true", help="执行清理（务必先备份库）")
    ap.add_argument("--guard", action="store_true", help="有任何假数据则退出码 1")
    ap.add_argument("--json-out", type=str, default="", help="把审计结果写成 JSON")
    a = ap.parse_args()

    conn = _connect(readonly=not a.commit)
    try:
        rows = scan(conn)
        total = total_fake(rows)
        print(f"{'类':<4}{'条数':>7}  {'处置':<8} 名称")
        for r in rows:
            mark = "  " if r["count"] == 0 else "❗"
            print(f"{mark}{r['code']:<4}{r['count']:>7}  {r['action']:<8} {r['name']}"
                  + (f"  样本 {r['samples']}" if r["samples"] else ""))
        print(f"\n合计假数据行: {total}")

        if a.json_out:
            p = Path(a.json_out)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps({
                "generated_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
                "total": total, "categories": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"审计结果已写: {p}")

        if a.commit:
            done = purge(conn, rows)
            print("\n已清理:", json.dumps(done, ensure_ascii=False))
            rows = scan(conn)   # 清理后复扫，必须为 0
            left = total_fake(rows)
            print(f"复扫剩余假数据: {left}")
            return 0 if left == 0 else 2

        if a.guard:
            if total:
                print("\nGUARD FAIL: 系统中存在假数据，见上表。")
                return 1
            print("\nGUARD OK: 未发现假数据。")
            return 0
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
