#!/usr/bin/env python3
"""v323: C 维（技术错误）真实沉淀器。

背景
----
漏检面板里 C 维只有 35 条真实样本，而且老的几条是 "REJECTED +2.70%" 这种没有
证据的占位文本。于是"数据缺失、信号慢、接口超时"这三类问题——最容易验证的一类
——反而沉淀最少，面板上永远是"3% 命中"。

做法（每一条都必须有库里的真实证据）
------------------------------------
1. feed_missing（行情断流）
   扫 market_ticks 每个资产的相邻 tick 间隔，间隔 > 120 分钟记为一次断流；
   再看这段断流窗口（+60 分钟缓冲）里有没有 critical_price_moves 的强异动：
     有 → hit=1（断流期间确实发生了该接住的行情）
     无 → hit=0（断流了，但期间没行情，影响有限）
   actual_outcome_pct 填该窗口内真实涨跌幅。

2. signal_delay（信号慢）
   对每笔能关联 raw_news 的 ai_decision，算 决策时间 - 新闻时间 的延迟；
   延迟 > 30 分钟记一次：
     延迟窗口内该资产真实涨跌 ≥ 0.5% → hit=1（慢到已经错过一段行情）
     否则 hit=0

3. data_feed_timeout（抓取失败）
   直接取 feed_health 里 status=failed 的记录，hit 由"当时该资产是否有强异动"决定。

只写有证据的行，不造数；重复运行用 (asset, subcategory, created_at) 去重。

用法：
    python3 c_technical_reflections.py            # 试运行
    python3 c_technical_reflections.py --commit   # 落库
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

try:  # 品种命名唯一入口（v341）；同级目录直接可导入
    from asset_alias import canonical as _canon_asset
except Exception:  # 单文件跑时没有包路径，退化为原样
    def _canon_asset(x):  # type: ignore
        return x

sys.path.insert(0, str(Path(__file__).resolve().parent))

DAYS = 90
GAP_MIN = 120          # 断流判定：相邻 tick 间隔 > 120 分钟
FAIL_GAP_MIN = 20      # 相邻两次抓取失败间隔 <= 20 分钟 -> 视为同一段连续失败
FAIL_WIN_MIN = 60      # 连续失败跨度 >= 60 分钟才算"真断流"（单次抖动不算）
STRONG_MOVE_PCT = 2.0  # "强异动"阈值
DELAY_MIN = 30         # 信号慢：决策比新闻晚 30 分钟以上
DELAY_MISS_PCT = 0.5   # 延迟期间涨跌超过 0.5% 视为已错过行情
MAX_ROWS_PER_KIND = 300


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%dT%H:%M:%S")


def _max_move_in_window(conn, symbol: str, start: int, end: int) -> float | None:
    """用真实行情序列算窗口内最大涨跌幅（相对窗口首根收盘）。

    为什么不再只看 critical_price_moves：
        那张表全库只有 34 行（只有被判成"关键异动"的事件才进）。
        用它来回答"断流期间有没有强异动"，498 次断流必然全是 hit=0 ——
        实测正是如此（C 维 506 条全 0，命中率永远 0%）。
        行情序列本身有 28 万根分钟线，直接算才是真的在测量。
    """
    rows = conn.execute(
        "SELECT close FROM ohlcv_intraday "
        "WHERE asset=? AND ts BETWEEN ? AND ? AND close IS NOT NULL "
        "ORDER BY ts", (symbol, start, end + 3600)).fetchall()
    if len(rows) < 2:
        rows = conn.execute(
            "SELECT close FROM kline_bars "
            "WHERE symbol=? AND timeframe='1m' AND close IS NOT NULL "
            "AND ts BETWEEN ? AND ? ORDER BY ts",
            (symbol, start * 1000, (end + 3600) * 1000)).fetchall()
    if len(rows) < 2:
        return None
    first = float(rows[0][0])
    if first <= 0:
        return None
    return max(abs(float(r[0]) / first - 1.0) * 100.0 for r in rows)


def collect_feed_gaps(conn, cutoff: int) -> list[dict]:
    out = []
    rows = conn.execute(
        "SELECT symbol, ts FROM market_ticks WHERE ts>=? ORDER BY symbol, ts",
        (cutoff,),
    ).fetchall()
    prev_sym, prev_ts = None, None
    for sym, ts in rows:
        if sym == prev_sym and prev_ts is not None and ts - prev_ts > GAP_MIN * 60:
            # 前一根 tick 可能早于 cutoff（纪元），断流"起点"不能落在纪元之前，
            # 否则又往库里写历史行 —— 起点抬到 cutoff。
            start, end = max(int(prev_ts), int(cutoff)), int(ts)
            # v343: 先用真实行情序列算（28 万根分钟线），
            # critical_price_moves 只作为兜底（它只有 34 行，单独用会恒判"无强异动"）
            measured = _max_move_in_window(conn, sym, start, end)
            if measured is not None:
                if measured >= STRONG_MOVE_PCT:
                    hit, pct, note = 1, round(measured, 2), f"断流期间最大波动 {measured:+.2f}%"
                else:
                    hit, pct, note = 0, round(measured, 2), f"断流期间最大波动仅 {measured:.2f}%"
            else:
                mv = conn.execute(
                    "SELECT move_pct, ts FROM critical_price_moves "
                    "WHERE asset=? AND ts BETWEEN ? AND ? "
                    "AND ABS(COALESCE(move_pct,0)) >= ? ORDER BY ABS(move_pct) DESC LIMIT 1",
                    (sym, start, end + 3600, STRONG_MOVE_PCT),
                ).fetchone()
                if mv:
                    hit, pct, note = 1, float(mv[0]), f"断流期间发生 {mv[0]:+.2f}% 异动"
                else:
                    hit, pct, note = 0, 0.0, "断流期间无行情序列，无法判定"
            gap_h = round((end - start) / 3600.0, 1)
            out.append({
                "asset": sym.upper(), "subcategory": "feed_missing",
                "created_at": _iso(start),
                "hit": hit, "actual_outcome_pct": pct,
                "text": f"行情断流 {gap_h}h（{_iso(start)} → {_iso(end)}）：{note}",
            })
        prev_sym, prev_ts = sym, ts
    return out


def _now_iso() -> str:
    from datetime import datetime
    return datetime.now().isoformat(timespec="seconds")


def collect_delays(conn, cutoff: int) -> list[dict]:
    out = []
    decs = conn.execute(
        "SELECT d.id, d.news_id, d.created_at, d.target_asset, r.ts AS news_ts "
        "FROM ai_decisions d JOIN raw_news r ON r.id = d.news_id "
        "WHERE d.created_at >= ? AND d.target_asset NOT IN ('','NONE') "
        "ORDER BY d.id DESC LIMIT 3000",
        (_iso(cutoff),),
    ).fetchall()
    for did, nid, created, asset, nts in decs:
        try:
            t1 = int(datetime.fromisoformat(created).timestamp())
        except Exception:
            continue
        delay = t1 - int(nts or 0)
        if delay < DELAY_MIN * 60 or delay > 6 * 3600:
            continue
        a = (asset or "").upper()
        p0 = conn.execute(
            "SELECT price FROM market_ticks WHERE symbol=? AND ts<=? ORDER BY ts DESC LIMIT 1",
            (a, int(nts)),
        ).fetchone()
        p1 = conn.execute(
            "SELECT price FROM market_ticks WHERE symbol=? AND ts<=? ORDER BY ts DESC LIMIT 1",
            (a, t1),
        ).fetchone()
        if not p0 or not p1 or not p0[0]:
            continue
        chg = (float(p1[0]) - float(p0[0])) / float(p0[0]) * 100.0
        hit = 1 if abs(chg) >= DELAY_MISS_PCT else 0
        out.append({
            "asset": a, "subcategory": "signal_delay",
            "created_at": _iso(t1), "hit": hit, "actual_outcome_pct": round(chg, 3),
            "text": (f"新闻 {_iso(int(nts))} 入库，决策 {_iso(t1)} 才出，延迟 "
                     f"{round(delay/60)} 分钟；延迟期间 {a} 实际 {chg:+.2f}%"
                     f"{'（已错过行情）' if hit else '（未错过）'}"),
            "ai_decision_id": did,
        })
    return out


def collect_feed_failures(conn, cutoff: int) -> list[dict]:
    """从 feed_health 的失败记录里识别"真断流窗口"，并只用真实行情判定 hit。

    v343 重写（原实现有两个致命问题）：
      1) 一次抓取失败就写一条 -> 498 条全是"binance 抓取失败（时间）"这种流水，
         没有"断流窗口"可言，hit 恒为 0，把 C 维命中率永久钉在 0%；
      2) 判 hit 用 critical_price_moves（全库 34 行），几乎必然判"无强异动"。
    现在：
      · 只保留同一资产**连续失败**、且跨度 ≥ FAIL_WIN_MIN 分钟的窗口（一次抖动不算断流）；
      · hit 用 _max_move_in_window 从 ohlcv_intraday / kline_bars 实测；
      · 测不到行情时 hit=0 但文本明说"无法判定"，不伪装成"没出事"。
    """
    out = []
    try:
        rows = conn.execute(
            "SELECT source, asset, ts FROM feed_health "
            "WHERE lower(coalesce(status,'')) IN ('failed','error') "
            "ORDER BY asset, ts DESC LIMIT 3000"
        ).fetchall()
    except sqlite3.OperationalError:
        return out

    def _sec(v):
        try:
            return int(v) if str(v).isdigit() else int(
                datetime.fromisoformat(str(v).replace(" ", "T")).timestamp())
        except Exception:
            return None

    by_asset: dict[str, list[int]] = {}
    for src, asset, ts in rows:
        a = (asset or "").upper()
        t = _sec(ts)
        if not a or not t or t < int(cutoff):
            continue
        by_asset.setdefault(a, []).append(t)

    for a, stamps in by_asset.items():
        stamps.sort()
        win_start = prev = stamps[0]
        for t in stamps[1:] + [None]:
            # 相邻两次失败间隔 <= FAIL_GAP_MIN 视为同一段连续失败
            if t is not None and t - prev <= FAIL_GAP_MIN * 60:
                prev = t
                continue
            span_min = (prev - win_start) / 60.0
            if span_min >= FAIL_WIN_MIN:
                mv = _max_move_in_window(conn, a, win_start, prev)
                if mv is None:
                    hit, pct, note = 0, 0.0, "该时段无行情序列，无法判定是否错过"
                elif mv >= STRONG_MOVE_PCT:
                    hit, pct, note = 1, round(mv, 2), f"断流期间最大波动 {mv:+.2f}%（已错过）"
                else:
                    hit, pct, note = 0, round(mv, 2), f"断流期间最大波动仅 {mv:.2f}%"
                out.append({
                    "asset": a, "subcategory": "data_feed_timeout",
                    "created_at": _iso(win_start), "hit": hit,
                    "actual_outcome_pct": pct,
                    "text": f"行情断流 {span_min / 60.0:.1f}h（{_iso(win_start)} → {_iso(prev)}）：{note}",
                })
            if t is not None:
                win_start = prev = t
    return out


def dedup_and_cap(conn, rows: list[dict]) -> list[dict]:
    existing = {
        (r[0], r[1], r[2])
        for r in conn.execute(
            "SELECT asset, subcategory, created_at FROM trade_reflections_v2 "
            "WHERE dimension='C'"
        )
    }
    fresh = [r for r in rows if (r["asset"], r["subcategory"], r["created_at"]) not in existing]
    out = []
    for sub in ("feed_missing", "signal_delay", "data_feed_timeout"):
        out.extend([r for r in fresh if r["subcategory"] == sub][:MAX_ROWS_PER_KIND])
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()

    import db as _db
    dbp = _db.get_db_path()
    # v341: 这个作业每天跑一次，扫 90 天 market_ticks；原来裸连不设 timeout，
    # 撞上引擎/回填在写就 "database is locked" 直接 exit 1（实测 2026-11-15 就是这样死的）。
    # 只读连接也要给等待窗口 + 失败重试：读锁不该因为写锁短暂持有而放弃。
    conn = None
    for _try in range(3):
        try:
            conn = sqlite3.connect(f"file:{dbp}?mode=ro", uri=True, timeout=30)
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA query_only=ON")
            break
        except sqlite3.OperationalError as e:
            print(f"[WARN] 连接被锁({_try + 1}/3): {e}", file=sys.stderr)
            time.sleep(20 * (_try + 1))
    if conn is None:
        print("[FATAL] 数据库持续被锁，放弃本轮（下次定时再跑）", file=sys.stderr)
        return 2
    cutoff = int(time.time()) - DAYS * 86400
    # v341: 90 天窗口会把**纪元之前**的断流/延迟也算出来并落库 —— 那正是"清空后又被
    # 回填污染"的老路。纪元之后的只算纪元起的部分。
    try:
        import epoch_gate
        ep = epoch_gate.epoch_ts(conn)
        if ep:
            cutoff = max(cutoff, int(epoch_gate.clamp_since(cutoff, ep) or cutoff))
    except Exception as e:
        print(f"[WARN] 纪元闸门不可用({type(e).__name__})，按 90 天全窗口扫", file=sys.stderr)

    raw = None
    rows = None
    # v342: 锁错误不只在 connect 时出现 —— 实测 2026-11-16 07:00 这轮是 connect 成功、
    # 跑到 collect_feed_gaps 扫 market_ticks 时才炸 "database is locked"。
    # WAL 下读通常不阻塞写，但遇到 checkpointer 抢 RESTART / shm 恢复时读会直接 BUSY，
    # 此时 busy_handler 不生效。所以把**整个读阶段**也包进重试。
    for _try in range(3):
        try:
            if conn is None:
                conn = sqlite3.connect(f"file:{dbp}?mode=ro", uri=True, timeout=30)
                conn.execute("PRAGMA busy_timeout=30000")
                conn.execute("PRAGMA query_only=ON")
            raw = collect_feed_gaps(conn, cutoff) + collect_delays(conn, cutoff) + collect_feed_failures(conn, cutoff)
            rows = dedup_and_cap(conn, raw)
            break
        except sqlite3.OperationalError as e:
            if "locked" not in str(e).lower() or _try == 2:
                raise
            print(f"[WARN] 读阶段被锁({_try + 1}/3): {e}", file=sys.stderr)
            try:
                conn.close()
            except Exception:
                pass
            conn = None
            time.sleep(20 * (_try + 1))
    if conn is not None:
        conn.close()
    if rows is None:
        print("[FATAL] 读阶段持续被锁，放弃本轮（下次定时再跑）", file=sys.stderr)
        return 2

    from collections import Counter
    by_sub = Counter(r["subcategory"] for r in rows)
    by_hit = Counter(f"{r['subcategory']}:hit={r['hit']}" for r in rows)
    print("=" * 60)
    print(f"扫描 90 天，候选 {len(raw)} 条，去重后 {len(rows)} 条")
    for k, v in by_sub.items():
        print(f"  {k:<20} {v}")
    for k, v in sorted(by_hit.items()):
        print(f"     {k}  {v}")

    if not args.commit:
        print("\n(试运行，未落库 — 加 --commit 生效)")
        return

    # v342: 补写本体三元组 (layer/sub_layer/signal_name)。
    # 原来只写 dimension='C' + subcategory，layer 一列全空 —— 验收 C7
    # "回填就绪度" 按权威映射表去解析时解析不出来（实测可解析 39/345 = 11.3%），
    # 直接判 FAIL。映射取自 reflection_dimension_map.SUB_LAYER_TO_DIMENSION，
    # 口径一致，不新造概念。
    SUBCAT_TO_ONTOLOGY = {
        "data_feed_timeout": ("data", "latency", "feed_stale"),
        "signal_delay": ("timing", "timelag_to_event", "entered_too_late"),
        "feed_missing": ("data", "coverage", "feed_missing"),
        "calibration_drift": ("strategy", "regime_filter", "calibration_drift"),
    }

    w = sqlite3.connect(dbp, timeout=20)
    try:
        cur = w.cursor()
        for r in rows:
            layer, sub_layer, signal_name = SUBCAT_TO_ONTOLOGY.get(
                r["subcategory"], ("", "", ""))
            # v412-G: 这类"决策延迟/行情断流"是**流程体检记录**，不是交易反思。
            # 写进 trade_reflections_v2 会连锁污染所有反思统计（C 类虚高、hit 恒 0、
            # 三层验证恒判"技术错误"、富化因无 decision_id 而永远跳过），
            # 已迁移到独立表 technical_audit（见 migrate_technical_audit.py）。
            cur.execute(
                """INSERT INTO technical_audit
                   (asset, action, dimension, subcategory, audit_text,
                    created_at, hit, actual_outcome_pct, ai_decision_id,
                    layer, sub_layer, signal_name, ontology_signal,
                    migrated_from_id, migrated_at)
                   VALUES (?, 'HOLD', 'C', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)""",
                (_canon_asset(r["asset"]), r["subcategory"], r["text"], r["created_at"],
                 r["hit"], r["actual_outcome_pct"], r.get("ai_decision_id"),
                 layer, sub_layer, signal_name,
                 f"{layer}/{sub_layer}/{signal_name}" if layer else "",
                 _now_iso()),
            )
        w.commit()
        print(f"\n✅ 已写入 {len(rows)} 条技术审计 → technical_audit（不再混入交易反思表）")
    finally:
        w.close()


if __name__ == "__main__":
    main()
