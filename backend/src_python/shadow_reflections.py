#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""shadow_reflections.py — v413 影子结算反思层

为什么需要
----------
`trade_reflections_v2` 的 156 条里，143 条是模板（HOLD、无入场价、无出场价），
系统的三道验收（verifier）对它们只能判"不可分离"——147/156 的
`reflection_effective` 是 NULL。这不是反思写得差，是**反思对象选错了**：
HOLD 决策本来就不成交，永远没有 pnl，永远无法验收。

真正能被判对错的样本在影子账本里：`shadow_ledger`（闸门前方向 + 入场价 +
4h 出场价 + hit）和 `shadow_degraded`（被降级路径丢掉、但记了账的方向）。
这里每天约 12 条，且**天然带结算真值**。

本模块把反思对象从"决策"换成"结算行"：
    系统当时声称的方向  ← claimed_direction
    价格实际走出来的方向 ← realized_direction
    两者一比就是结论（direction_ok / direction_wrong / flat）

刻意不做的事
------------
1. **不做 LLM 归因**。归因（"为什么错"）可以后补，但"对不对"必须先有；
   现在连"对不对"都判不了，再加 LLM 只是又一层文本。
2. **不回填历史积压**。按当前口径：数据采集中断后恢复，以恢复时刻为新起点，
   不处理停机期堆积的历史数据（首次运行只写入 watermark，旧行不追溯；
   需要时用 --since 显式指定起点）。
3. **不写进 trade_reflections_v2**。那张表已经因为"不同来源混写"付出过代价
   （526 条流程指标被当成交易反思），换一张表，语义干净、可整表回滚。

用法
    python3 shadow_reflections.py --report          # 看 watermark 与可处理量
    python3 shadow_reflections.py --dry-run --since 2026-11-17
    python3 shadow_reflections.py --commit          # 正常增量（每日一次）
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import db

STATE_NAME = "shadow_reflections_watermark"
FLOOR_NAME = "shadow_reflections_floor"

# 与 shadow_degraded_collect 的 FLAT_PCT 对齐：|move| < 0.01% 视为没动
# （数据源存在"同一个价反复写进不同 ts"的假 tick，这类样本不能用来判对错）
FLAT_PCT = 0.01

# v414: 水位按**结算时刻(exit_ts)**推进，并保留回看窗口。
#
# 旧实现有两个结构性问题（2026-11-18 实测产线 0 行、dry-run 45 行）：
#   1. 取数按 decided_ts，而水位每跑一次写成 now —— 结算要等 4h、跑批也是 4h，
#      水位永远跑在结算前面，样本被永久跳过；
#   2. 迟到结算（wait_min 超过 240）即使写进来，exit_ts 也落在水位之前，同样被跳过。
#
# 现在：取 exit_ts >= (watermark - LOOKBACK)，处理完把水位推到本次最大 exit_ts；
# 回看窗口 + UNIQUE(source, source_id) 幂等 upsert 一起兜住迟到结算。
# 回看不处理"停机期历史积压"：首次运行仍以此刻为起点，要追溯必须显式 --since。
LOOKBACK_HOURS = 48

_CREATE_SHADOW_REFLECTIONS = """
    CREATE TABLE IF NOT EXISTS shadow_reflections (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source TEXT NOT NULL,                 -- shadow_ledger / shadow_degraded
        source_id INTEGER NOT NULL,           -- 源表的 decision_id
        decision_id INTEGER NOT NULL DEFAULT 0,
        asset TEXT NOT NULL DEFAULT '',
        claimed_direction TEXT NOT NULL DEFAULT '',   -- 系统当时声称的方向
        claimed_confidence REAL,
        realized_direction TEXT NOT NULL DEFAULT '',  -- 价格实际走出来的方向
        realized_move_pct REAL,
        entry_ts INTEGER,
        exit_ts INTEGER,
        entry_price REAL,
        exit_price REAL,
        window_min REAL,
        followed INTEGER NOT NULL DEFAULT 0,  -- 系统最终是否执行了这个方向
        verdict TEXT NOT NULL DEFAULT '',     -- direction_ok/wrong/flat/insufficient
        verdict_basis TEXT NOT NULL DEFAULT '',
        attribution TEXT NOT NULL DEFAULT '', -- 归因来源；'none' = 只有结论没有归因
        evidence TEXT NOT NULL DEFAULT '',
        reflection_effective INTEGER,         -- 1 一致 / 0 否证 / NULL 判不了
        created_at TEXT NOT NULL DEFAULT '',
        UNIQUE(source, source_id)
    );
"""

_CREATE_SHADOW_STATE = """
    CREATE TABLE IF NOT EXISTS shadow_reflection_state (
        name TEXT PRIMARY KEY,
        watermark_ts INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL DEFAULT ''
    );
"""

_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_srefl_verdict ON shadow_reflections(verdict)",
    "CREATE INDEX IF NOT EXISTS idx_srefl_source ON shadow_reflections(source, exit_ts)",
    "CREATE INDEX IF NOT EXISTS idx_srefl_asset ON shadow_reflections(asset, created_at)",
]


def _ensure_schema(conn: sqlite3.Connection, commit: bool = True) -> None:
    """DDL 需要写锁；引擎在跑时会撞锁，这里做短重试而不是直接失败。

    参考 v412 的教训：写锁冲突本身不是错误，"失败一次就放弃"才是。
    """
    for sql in (_CREATE_SHADOW_REFLECTIONS, _CREATE_SHADOW_STATE, *_INDEXES):
        for attempt in range(1, 6):
            try:
                conn.execute(sql)
                break
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt == 5:
                    raise
                time.sleep(1.5 * attempt)
    if commit:
        conn.commit()


def _get_watermark(conn: sqlite3.Connection) -> Optional[int]:
    row = conn.execute(
        "SELECT watermark_ts FROM shadow_reflection_state WHERE name=?", (STATE_NAME,)
    ).fetchone()
    return int(row[0]) if row else None


def _get_state_ts(conn: sqlite3.Connection, name: str) -> Optional[int]:
    row = conn.execute(
        "SELECT watermark_ts FROM shadow_reflection_state WHERE name=?", (name,)
    ).fetchone()
    return int(row[0]) if row else None


def _set_state_ts(conn: sqlite3.Connection, name: str, ts: int) -> None:
    conn.execute(
        "INSERT INTO shadow_reflection_state(name, watermark_ts, updated_at)"
        " VALUES(?,?,?)"
        " ON CONFLICT(name) DO UPDATE SET watermark_ts=excluded.watermark_ts,"
        " updated_at=excluded.updated_at",
        (name, int(ts), time.strftime("%Y-%m-%dT%H:%M:%S")),
    )
    conn.commit()


def _set_watermark(conn: sqlite3.Connection, ts: int) -> None:
    conn.execute(
        "INSERT INTO shadow_reflection_state(name, watermark_ts, updated_at)"
        " VALUES(?,?,?)"
        " ON CONFLICT(name) DO UPDATE SET watermark_ts=excluded.watermark_ts,"
        " updated_at=excluded.updated_at",
        (STATE_NAME, int(ts), time.strftime("%Y-%m-%dT%H:%M:%S")),
    )
    conn.commit()


def _direction_sign(direction: str) -> int:
    d = (direction or "").upper().strip()
    if d in ("BUY", "LONG", "UP", "看多", "做多"):
        return 1
    if d in ("SELL", "SHORT", "DOWN", "看空", "做空"):
        return -1
    return 0


def _judge(claimed: str, move_pct: Optional[float]) -> Dict[str, Any]:
    """只用价格判对错，不看系统自己怎么解释——这是本表存在的意义。"""
    claim = _direction_sign(claimed)
    if move_pct is None:
        return {"verdict": "insufficient", "realized": "",
                "basis": "无结算价，判不了（缺席不等于正确）",
                "effective": None}
    move = float(move_pct)
    if abs(move) < FLAT_PCT:
        return {"verdict": "flat", "realized": "FLAT",
                "basis": "4 小时窗口内价格几乎没动（可能假 tick），不能用来判方向",
                "effective": None}
    realized = "UP" if move > 0 else "DOWN"
    if not claim:
        return {"verdict": "insufficient", "realized": realized,
                "basis": "系统没给方向，只有价格结果", "effective": None}
    if claim > 0 and move > 0 or claim < 0 and move < 0:
        return {"verdict": "direction_ok", "realized": realized,
                "basis": "声称方向 %s，价格实际 %+.4f%% → 一致" % (claimed, move),
                "effective": 1}
    return {"verdict": "direction_wrong", "realized": realized,
            "basis": "声称方向 %s，价格实际 %+.4f%% → 相反" % (claimed, move),
            "effective": 0}


def _collect(conn: sqlite3.Connection, since_exit_ts: int) -> List[Dict[str, Any]]:
    """按**结算时刻(exit_ts)**取增量样本。

    为什么不用 decided_ts：结算相对决策有固定 4h（还可能迟到）延迟，而水位是按跑批时刻
    推进的。用 decided_ts 做水位，等于要求"决策后立刻结算"，结果是一条也取不到。
    exit_ts 是"这条样本什么时候算出了结果"，用它做水位才是增量语义。
    """
    rows: List[Dict[str, Any]] = []
    for r in conn.execute(
        """SELECT decision_id, decided_ts, asset, direction, action_after, confidence,
                  entry_ts, entry_price, exit_ts, exit_price, move_pct, hit, gate_score
             FROM shadow_ledger
            WHERE exit_ts IS NOT NULL AND exit_ts >= ?
            ORDER BY exit_ts ASC""",
        (int(since_exit_ts),),
    ):
        rows.append({"source": "shadow_ledger", "source_id": int(r[0]),
                     "decision_id": int(r[0]), "decided_ts": int(r[1] or 0),
                     "asset": str(r[2] or ""), "claimed": str(r[3] or ""),
                     "claim_conf": r[5],
                     "followed": 0 if str(r[4] or "").upper() == "HOLD" else 1,
                     "entry_ts": r[6], "entry_price": r[7],
                     "exit_ts": r[8], "exit_price": r[9], "move_pct": r[10],
                     "hit": r[11], "gate_score": r[12]})
    for r in conn.execute(
        """SELECT decision_id, decided_ts, asset, direction, degrade_reason, confidence,
                  entry_ts, entry_price, exit_ts, exit_price, wait_min, move_pct, hit
             FROM shadow_degraded
            WHERE exit_ts IS NOT NULL AND exit_ts >= ?
            ORDER BY exit_ts ASC""",
        (int(since_exit_ts),),
    ):
        rows.append({"source": "shadow_degraded", "source_id": int(r[0]),
                     "decision_id": int(r[0]), "decided_ts": int(r[1] or 0),
                     "asset": str(r[2] or ""), "claimed": str(r[3] or ""),
                     "claim_conf": r[5],
                     "followed": 0,
                     "entry_ts": r[6], "entry_price": r[7],
                     "exit_ts": r[8], "exit_price": r[9], "move_pct": r[11],
                     "hit": r[12], "gate_score": None,
                     "degrade_reason": str(r[4] or "")})
    return rows


def _evidence(row: Dict[str, Any]) -> str:
    return json.dumps(
        {
            "asset": row["asset"],
            "entry_ts": row["entry_ts"],
            "exit_ts": row["exit_ts"],
            "entry_price": row["entry_price"],
            "exit_price": row["exit_price"],
            "move_pct": row["move_pct"],
            "price_source": "%s.entry_price/exit_price" % row["source"],
            "source_row": "%s.decision_id=%s" % (row["source"], row["source_id"]),
        },
        ensure_ascii=False,
    )


_INSERT_REFLECTION_SQL = """
    INSERT INTO shadow_reflections
       (source, source_id, decision_id, asset, claimed_direction,
        claimed_confidence, realized_direction, realized_move_pct,
        entry_ts, exit_ts, entry_price, exit_price, window_min,
        followed, verdict, verdict_basis, attribution, evidence,
        reflection_effective, created_at)
    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    ON CONFLICT(source, source_id) DO UPDATE SET
      realized_direction=excluded.realized_direction,
      realized_move_pct=excluded.realized_move_pct,
      exit_price=excluded.exit_price, exit_ts=excluded.exit_ts,
      window_min=excluded.window_min, verdict=excluded.verdict,
      verdict_basis=excluded.verdict_basis,
      evidence=excluded.evidence,
      reflection_effective=excluded.reflection_effective
"""


def run(since_ts: Optional[int] = None, commit: bool = False) -> Dict[str, Any]:
    conn = db.get_connection()
    conn.execute("PRAGMA busy_timeout=30000")
    out: Dict[str, Any] = {"at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    try:
        _ensure_schema(conn)
        now_ts = int(time.time())
        wm = _get_watermark(conn)
        out["watermark_before"] = wm

        floor = _get_state_ts(conn, FLOOR_NAME)
        out["floor_before"] = floor
        if since_ts is not None:
            start = int(since_ts)
            out["mode"] = "explicit_since"
            if floor is None:
                floor = start
        elif wm is None:
            # 首次运行：以"此刻"为新起点，不处理停机期堆积的历史数据（用户的恢复口径）
            start = now_ts
            floor = floor if floor is not None else now_ts
            out["mode"] = "first_run_from_now"
        else:
            # v414: 水位存的是"上次处理到的最大 exit_ts"，回看 48h 兜迟到结算；
            # 但回看不得越过 recovery floor —— 否则会把恢复时刻之前的积压重新捞进来。
            # 幂等 upsert 保证重扫不会产生重复行。
            start = int(wm) - LOOKBACK_HOURS * 3600
            if floor is not None:
                start = max(start, int(floor))
            out["mode"] = "incremental_lookback"

        rows = _collect(conn, start)
        out["start_ts"] = start
        out["candidates"] = len(rows)

        buckets: Dict[str, int] = {}
        written = 0
        max_exit = 0
        for row in rows:
            if row.get("exit_ts"):
                max_exit = max(max_exit, int(row["exit_ts"]))
            j = _judge(row["claimed"], row["move_pct"])
            buckets[j["verdict"]] = buckets.get(j["verdict"], 0) + 1
            if not commit:
                continue
            window_min = None
            if row["entry_ts"] and row["exit_ts"]:
                window_min = round((int(row["exit_ts"]) - int(row["entry_ts"])) / 60.0, 1)
            conn.execute(
                _INSERT_REFLECTION_SQL,
                (row["source"], row["source_id"], row["decision_id"], row["asset"],
                 row["claimed"], row["claim_conf"], j["realized"], row["move_pct"],
                 row["entry_ts"], row["exit_ts"], row["entry_price"], row["exit_price"],
                 window_min, row["followed"], j["verdict"], j["basis"],
                 "none", _evidence(row), j["effective"],
                 time.strftime("%Y-%m-%dT%H:%M:%S")),
            )
            written += 1
        if commit:
            conn.commit()
            # 水位只按"已处理到的最大 exit_ts"推进；本次没有样本就保持原水位不动
            # （旧的 now_ts 写法会把还没结算的样本永久甩在身后）
            new_wm = max(int(max_exit), int(wm or 0), int(start))
            if floor is None:
                floor = int(start)
            _set_state_ts(conn, FLOOR_NAME, int(floor))
            _set_watermark(conn, new_wm)
            # v482: 新样本落库后顺手给反思行盖 4h/24h 真值章。
            # 盖章失败不能拖垮主账本（影子层自身的健康比反思标签更重要）。
            try:
                import reflection_horizon_truth as _rht
                out["horizon_truth"] = _rht.stamp(conn, commit=True)
            except Exception as _rht_exc:
                print(f"[shadow] horizon truth stamp failed: {_rht_exc}", flush=True)
        out["watermark_new"] = max(int(max_exit), int(wm or 0), int(start)) if commit else None
        out["written"] = written
        out["verdicts"] = buckets
        after = conn.execute(
            "SELECT COALESCE(NULLIF(verdict,''),'(空)') v, COUNT(*) n"
            "  FROM shadow_reflections GROUP BY v ORDER BY n DESC"
        ).fetchall()
        out["table"] = {str(a[0]): int(a[1]) for a in after}
        out["watermark_after"] = _get_watermark(conn)
        out["committed"] = bool(commit)
        return out
    finally:
        conn.close()


def _parse_since(raw: str) -> Optional[int]:
    if not raw:
        return None
    raw = raw.strip()
    if raw in ("now", ""):
        return int(time.time())
    if raw.isdigit():
        return int(raw)
    # 'YYYY-MM-DD' 或 'YYYY-MM-DD HH:MM:SS' → 本机时区 epoch
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return int(time.mktime(time.strptime(raw, fmt)))
        except ValueError:
            continue
    raise SystemExit("--since 无法解析: %s" % raw)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--since", default="",
                    help="显式起点（epoch / 'YYYY-MM-DD' / 'now'）；不给则用 watermark 增量")
    args = ap.parse_args()

    since_ts = _parse_since(args.since) if args.since else None
    if args.report:
        conn = db.get_connection()
        try:
            _ensure_schema(conn)
            print(json.dumps({
                "watermark": _get_watermark(conn),
                "table": dict(conn.execute(
                    "SELECT COALESCE(NULLIF(verdict,''),'(空)'), COUNT(*)"
                    "  FROM shadow_reflections GROUP BY 1").fetchall()),
            }, ensure_ascii=False, indent=2))
        finally:
            conn.close()
        return 0

    res = run(since_ts=since_ts, commit=bool(args.commit and not args.dry_run))
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
