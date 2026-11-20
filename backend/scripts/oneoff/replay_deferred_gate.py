#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""replay_deferred_gate.py — v320: 回放"门卫未判定"新闻，补判后放行主分析

背景（2026-11-01 事故）：
  MiniMax 两个账户 Token Plan 全部耗尽 → gate 池全池 429 持续约 3h15m（2651 次失败）
  → 期间每条新闻的 L3 门卫都拿不到结论。
  v176.9 的 fail-closed 策略把"门卫不可用"直接当作"是噪音"（is_noise=1），
  于是这 3 小时的新闻 100% 被永久销毁 —— LLM 恢复后也回放不了，系统出现
  "零信号 + 数据空洞"的静默故障。

v320 改造后：
  · 门卫不可用 → 落库时标记 gate_unavailable=1（不当噪音、保留待回放）
  · ai_worker 认领时排除 gate_unavailable=1（不产出伪造的"AI 观望"假信号）
  · 本脚本在 LLM 恢复后重跑门卫补判 → 回填 gate_* → 清 gate_unavailable
    → 状态仍是 PENDING → 被 ai_worker 正常认领分析

用法:
  python3 replay_deferred_gate.py                 # dry-run: 统计待回放条数
  python3 replay_deferred_gate.py --limit 50      # dry-run: 只看前 50 条
  python3 replay_deferred_gate.py --commit        # 实际回放（逐条调门卫 LLM）
  python3 replay_deferred_gate.py --commit --max-age-hours 6
                                                  # 只回放 6 小时内的新闻（更早的
                                                  # 市场已消化，回放意义不大 → 直接判噪音）

注意:
  · --commit 会真实消耗 LLM 额度（每条一次门卫调用）。先跑 dry-run 看量级。
  · 门卫仍不可用时，该条保持 gate_unavailable=1，下次再试（不丢数据）。
  · 回放是**串行**的：每条一次 LLM 调用，数百条会跑较久。

v321 变更（2026-11-03）:
  · --limit 默认 0 = **不限量，全量回放**（SQLite 的 "LIMIT 0" 会返回 0 行，
    故代码里特判成"无 LIMIT 子句"；原默认 200 会把 backlog 截断）。
  · --max-age-hours 默认 0 = **不按时间过滤**，避免回放时静默丢弃旧新闻。
  · 回放成功后补写 status/is_noise/gate_* 字段，否则回填的 gate_* 永远不会被
    ai_worker 读到（它只认领 status='PENDING' AND is_noise=0 的行），
    这批新闻就成了"回放了但既不分析也不释放"的死数据。

  ⚠️ 两个已踩过的坑（别再踩第二次）:
  (1) status **不能**写 'FILTERED' —— raw_news 的 CHECK 约束是
      CHECK (status IN ('PENDING','PROCESSING','DONE','FAILED'))，
      写 'FILTERED' 会 IntegrityError 让**整批回放崩掉**（首次执行即命中）。
      realtime_filter._attach_gate 返回的 status="FILTERED" 只是 filter 结果里的
      逻辑标签，入库时被丢弃，从未进过 DB。
      权威语义见 engine/ingest.py:378-390 —— status 硬编码 "PENDING"，
      区分"排除/可交易"靠的是 is_noise：
        门卫判"排除"   → status='PENDING' + is_noise=1
        门卫判"可交易" → status='PENDING' + is_noise=0
      ai_worker 认领条件含 is_noise=0 → 噪音行天然不被认领，不需要特殊 status。
  (2) 每条回放必须**独立短事务**（UPDATE 后立即 commit），不能攒批。
      攒批会让写锁跨越 LLM 调用被长期持有（单次调用 5-8s），
      后台 ingest/ai_worker 抢不到锁 → "database is locked"。
      同时单条写库失败要 try/except 跳过（该条保持 gate_unavailable=1，不丢数据）。
"""
import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parents[2] / "src_python"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

DB_PATH = Path(os.getenv("TRIDENT_DB_PATH") or (
    SRC_DIR.parent / "trident_event_bus.db"
))


def _ts() -> str:
    return time.strftime("%H:%M:%S")


def _parse_ts(value):
    """把 raw_news.ts(epoch 秒) / timestamp('YYYY-MM-DD HH:MM:SS') 转 epoch 秒。"""
    if value in (None, ""):
        return None
    try:
        iv = int(value)
        if iv > 1_000_000_000:
            return iv
    except (TypeError, ValueError):
        pass
    try:
        return int(time.mktime(time.strptime(str(value)[:19], "%Y-%m-%d %H:%M:%S")))
    except Exception:
        return None


def _ensure_column(cur, conn):
    cols = {r[1] for r in cur.execute("PRAGMA table_info(raw_news)").fetchall()}
    if "gate_unavailable" not in cols:
        cur.execute("ALTER TABLE raw_news ADD COLUMN gate_unavailable "
                    "INTEGER NOT NULL DEFAULT 0")
        conn.commit()
        print("[replay] 已补列 raw_news.gate_unavailable")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true",
                    help="实际回放并写回 DB（默认 dry-run）")
    ap.add_argument("--limit", type=int, default=0,
                    help="本次最多回放多少条（默认 0 = 不限，全量回放）")
    ap.add_argument("--max-age-hours", type=float, default=0.0,
                    help="只回放这么多个小时内的新闻；0 = 不按时间过滤，"
                         "全部回放（更早的也让门卫判一次，避免静默丢数据）；"
                         "指定正数时更早的判为过期噪音")
    args = ap.parse_args()

    if not DB_PATH.exists():
        print(f"DB not found: {DB_PATH}", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    # v321: 回放进程与后台 ingest/ai_worker/reflection_loop 抢同一张表。
    # 默认 timeout=5s 太短 → "database is locked"（实测每次 LLM 调用后埋点写库失败）。
    # 这里给 30s，并且每条回放用**独立短事务**（下面每条 UPDATE 后立即 commit），
    # 绝不做长事务 —— 长事务会一直占写锁，把后台进程全饿死。
    conn.execute("PRAGMA busy_timeout=30000;")
    cur = conn.cursor()
    _ensure_column(cur, conn)

    total_deferred = cur.execute(
        "SELECT COUNT(*) FROM raw_news WHERE COALESCE(gate_unavailable,0)=1"
    ).fetchone()[0]
    pending = cur.execute(
        "SELECT COUNT(*) FROM raw_news "
        "WHERE COALESCE(gate_unavailable,0)=1 AND status='PENDING'"
    ).fetchone()[0]
    print(f"[{_ts()}] replay_deferred_gate: 待回放总数={total_deferred} "
          f"(其中 PENDING={pending})")

    if not pending:
        print("[replay] 无需回放")
        return

    # --limit 0 = 不限：SQLite 的 "LIMIT 0" 会返回 0 行，必须特判成"无 LIMIT"。
    select_sql = (
        "SELECT id, source, content, ts, timestamp FROM raw_news "
        "WHERE COALESCE(gate_unavailable,0)=1 AND status='PENDING' "
        "ORDER BY id ASC"
    )
    if args.limit > 0:
        select_sql += " LIMIT ?"
        rows = cur.execute(select_sql, (args.limit,)).fetchall()
    else:
        rows = cur.execute(select_sql).fetchall()
    if not args.commit:
        print(f"[replay] dry-run：将回放前 {len(rows)} 条（加 --commit 实际执行）")
        for r in rows[:10]:
            print(f"  #{r['id']} {r['source']} | {(r['content'] or '')[:70]}")
        if len(rows) > 10:
            print(f"  ... 其余 {len(rows) - 10} 条省略")
        return

    # 真实回放：逐条重跑 L3 门卫
    from realtime_filter import _l3_semantic_gate

    now = time.time()
    ok_cnt = 0
    fail_cnt = 0
    expired_cnt = 0
    excluded_cnt = 0

    for r in rows:
        content = r["content"] or ""
        title = content
        if title.startswith("[hash:") and "]" in title:
            title = title.split("]", 1)[1].strip()
        title = title[:300]

        # 时效：太旧的新闻市场早已消化，回放无意义 → 直接判噪音
        # --max-age-hours 0 = 不按时间过滤（默认，全量回放不静默丢数据）
        epoch = _parse_ts(r["ts"]) or _parse_ts(r["timestamp"])
        age_h = ((now - epoch) / 3600.0) if epoch else 999.0
        if args.max_age_hours > 0 and age_h > args.max_age_hours:
            # 过期 → 判为噪音（不入库路径分析），语义与入库一致：status='PENDING' + is_noise=1。
            # 'DONE' 虽然合法，但会让"已完成"的统计口径混入"从未分析"的噪音行，故不用。
            try:
                cur.execute(
                    "UPDATE raw_news SET gate_unavailable=0, is_noise=1, "
                    # v321.1: 解冻 = 重新入队 → 重置重试计数，给满 3 次机会。
                    "retry_count=0, "
                    "relevance_score=0.0, status='PENDING', gate_verdict='排除', "
                    "gate_core_impact='回放时已过期' WHERE id=?",
                    (r["id"],),
                )
                conn.commit()
            except sqlite3.Error as e:
                conn.rollback()
                print(f"[{_ts()}] 过期行写库失败（跳过）: id={r['id']} "
                      f"{type(e).__name__}: {e}", flush=True)
                continue
            expired_cnt += 1
            continue

        gate = _l3_semantic_gate(title, content[:500])
        if gate is None:
            # 门卫仍不可用 → 保持 gate_unavailable=1，等下次
            fail_cnt += 1
            continue

        verdict = str(gate.get("verdict") or "")
        is_noise = 1 if verdict == "排除" else 0
        # v321 修正（第二次）：必须与**入库路径**的语义完全一致，否则撞 CHECK 约束。
        #
        # 权威依据 —— engine/ingest.py:378-390 的入库调用：
        #     db.insert_raw_news(..., status="PENDING",            # ← 硬编码 PENDING
        #                        is_noise=int(f_result["is_noise"]))
        # 而 raw_news 的 CHECK 约束是：
        #     CHECK (status IN ('PENDING','PROCESSING','DONE','FAILED'))
        # → 'FILTERED' **根本不合法**（v321 初版写 'FILTERED' → IntegrityError 回放全崩）。
        # realtime_filter._attach_gate 返回的 status="FILTERED" 只是 filter 结果里的
        # 逻辑标签，入库时被丢弃，从未进过 DB。
        #
        # 真实语义：门卫判"排除" → status='PENDING' + is_noise=1；
        #           判"可交易" → status='PENDING' + is_noise=0。
        # ai_worker 认领条件含 is_noise=0 → 噪音行天然不会被认领，无需特殊 status。
        next_status = "PENDING"
        # v321: 单条写库失败（锁冲突/约束）不能中断整批回放 —— 跳过该条继续，
        # 该条仍保持 gate_unavailable=1（下次回放会再取到），不丢数据。
        try:
            cur.execute(
                "UPDATE raw_news SET gate_unavailable=0, is_noise=?, status=?, "
                # v321.1: 解冻 = 重新入队 → 重置重试计数，给满 3 次机会。
                "retry_count=0, "
                "relevance_score=CASE WHEN ?=1 THEN 0.0 ELSE relevance_score END, "
                "gate_core_impact=?, gate_is_material=?, gate_assets=?, "
                "gate_main_asset=?, gate_kind=?, gate_timing=?, gate_strength=?, "
                "gate_verdict=?, gate_impact_1to100=? WHERE id=?",
                (
                    is_noise,
                    next_status,
                    is_noise,
                    str(gate.get("core_impact") or "")[:300],
                    1 if gate.get("is_material") else 0,
                    json.dumps(gate.get("assets") or [], ensure_ascii=False)[:120],
                    str(gate.get("main_asset") or "")[:20],
                    str(gate.get("kind") or "")[:10],
                    str(gate.get("timing") or "")[:10],
                    int(gate.get("strength") or 0),
                    verdict[:10],
                    int(gate.get("impact_1to100") or 0),
                    r["id"],
                ),
            )
            # v321: 每条**立即 commit**（独立短事务）—— 不攒批。
            # 攒批（每 10 条 commit）会让写锁跨越 LLM 调用被长期持有（单次 LLM 5-8s），
            # 后台 ingest/ai_worker 抢不到锁 → "database is locked"。
            # 每条一提交：持锁窗口仅几毫秒，且中断时已完成的条目不会丢。
            conn.commit()
        except sqlite3.Error as e:
            conn.rollback()
            fail_cnt += 1
            print(f"[{_ts()}] 写库失败（跳过该条，仍待回放）: id={r['id']} "
                  f"{type(e).__name__}: {e}", flush=True)
            continue
        ok_cnt += 1
        if is_noise:
            excluded_cnt += 1
        if ok_cnt % 10 == 0:
            print(f"[{_ts()}] 已回放 {ok_cnt} 条（失败 {fail_cnt} / 过期 {expired_cnt}）",
                  flush=True)

    conn.commit()
    remain = cur.execute(
        "SELECT COUNT(*) FROM raw_news WHERE COALESCE(gate_unavailable,0)=1"
    ).fetchone()[0]
    print(f"[{_ts()}] 回放完成：成功={ok_cnt}（其中判排除={excluded_cnt}） "
          f"失败={fail_cnt} 过期丢弃={expired_cnt}；仍待回放={remain}")
    conn.close()


if __name__ == "__main__":
    main()

