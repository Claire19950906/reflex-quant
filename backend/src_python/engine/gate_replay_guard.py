#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gate_replay_guard — 自动回放「门卫未判定」新闻（v321 自愈）。

背景（2026-11-03）
-----------------
v320 引入了 gate_unavailable=1：门卫(LLM)通道故障时，新闻照常入库但标记
「门卫未判定」，主分析不认领、不丢弃，等 replay_deferred_gate.py 回放补判。

但该回放**从未被调度**：没有 launchd 任务、没有 cron、ai_worker 主循环也不调。
→ 通道故障期间入库的新闻永久冻结 → 主分析永不认领 → 前端永远显示「待 AI 分析」。
实测冻结 759 条，而通道早已恢复（探测 HTTP 200 / 1.2s）。

本模块提供两条接入路径（都复用同一份回放逻辑）：

1. 常驻守护（可单独起）：
       python3 engine/gate_replay_guard.py            # 常驻，每 5 分钟一轮
       python3 engine/gate_replay_guard.py --once     # 跑一轮就退出（cron 用）

2. 被 ai_worker 主循环调用（engine/ai_worker.py 里 run_gate_replay_if_due()）：
   引擎本来就在跑，复用它的进程，不新增常驻进程。

设计约束
--------
- **通道健康预检**：回放每条要消耗一次 LLM 额度。通道不健康时直接跳过，
  绝不盲目消耗、绝不把好数据写成噪音。
- **幂等**：只取 gate_unavailable=1 AND status='PENDING' 的行，回放成功后
  该字段清零 → 不会被重复回放。
- **失败不崩**：任何单条异常都跳过并保留 gate_unavailable=1（下轮再试），
  绝不丢数据。
- **批量有界**：每轮最多回放 N 条（默认 50），避免一次回放几千条把
  LLM 额度打爆、也避免长时间持写锁饿死后台 ingest/ai_worker。
- **独立短事务**：每条回放单独 commit（与 replay_deferred_gate.py 一致），
  不攒批 —— 攒批会让写锁跨越 LLM 调用被长期持有 → "database is locked"。
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

SRC_DIR = Path(__file__).resolve().parent.parent
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

DEFAULT_PROBE_TIMEOUT = 20.0     # 通道健康预检超时（秒）
DEFAULT_BATCH_LIMIT = 50         # 每轮最多回放条数
MAX_CONSECUTIVE_FAILS = 3        # 连续失败多少次才判定"通道真挂了"并停本轮
DEFAULT_INTERVAL_S = 300.0       # 两轮之间间隔（秒）


import os as _os

# 2026-11-16：除 gate_unavailable=1 的"冻结"条，还要覆盖"从未被门卫判定"的 PENDING。
# 事故背景：引擎停摆期间入库的新闻带着空 gate_verdict 落进 PENDING，
# 而 AI 批处理只认领 gate_verdict='可交易'、回放只认领 gate_unavailable=1，
# 这批条两边都不认 —— 没人处理，只能等超过 1 小时被"归档超龄"丢掉。
# 实测某时刻 259 条 PENDING 里 248 条属此类。
_INCLUDE_UNJUDGED = _os.getenv("GATE_REPLAY_INCLUDE_UNJUDGED", "1") not in ("0", "false", "False")

# "从未判定"的定义：门卫标签为空 / 未判定 / NULL
_UNJUDGED_SQL = "(gate_verdict IS NULL OR gate_verdict IN ('', '未判定'))"


def _now_str() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _db_path() -> Path:
    """DB 路径：优先 TRIDENT_DB_PATH 环境变量，其次项目默认位置。"""
    env = os.getenv("TRIDENT_DB_PATH")
    if env:
        return Path(env)
    return SRC_DIR.parent / "trident_event_bus.db"


def _connect(db_path: Optional[Path] = None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path or _db_path()), timeout=30.0)
    conn.row_factory = sqlite3.Row
    # 30s busy_timeout：回放进程与 ingest/ai_worker 抢同一张表
    conn.execute("PRAGMA busy_timeout=30000;")
    return conn


def count_backlog(conn: sqlite3.Connection) -> Tuple[int, int]:
    """返回 (待回放总数, 其中 PENDING 且未判定的条数)。

    v422（2026-11-17）：修 SQL 优先级 bug —— 这是今天配额被烧光的真凶。

    ## 修的是什么

    原写法把 `extra` 直接拼在 WHERE 末尾，**没有括号**：

    ```sql
    WHERE COALESCE(gate_unavailable,0)=1 OR (gate_verdict IS NULL OR gate_verdict IN ('','未判定'))
    ```

    SQL 里 `AND` 优先级高于 `OR`，所以这个 WHERE 实际等于：

    ```sql
    gw_unavailable=1  OR  全库 gate_verdict 为空的新闻
    ```

    → **status 条件（`AND status='PENDING'`）被整个 OR 掉了**，
    统计结果变成"全库所有 gate_verdict 未判定的新闻"，包含早已
    FILTERED / DONE 的历史行。

    ## 实测差多少（2026-11-17 10:4x）

    | 口径 | 条数 |
    |---|---|
    | 回放脚本报的 `remaining`（带 bug） | **11,141** |
    | 真实待回放（`(...) AND status='PENDING'`） | **3** |

    虚高 **3,714 倍**。

    ## 为什么这个 bug 这么致命

    `run_gate_replay_background` 每 300 秒跑一轮，入口是
    `total, pending = count_backlog(conn); if not pending: return stat`。
    因为 pending 永远报 11,141（那批历史行不会变），**这个"if 空就停"的短路
    永远不会触发** —— 引擎于是每 5 分钟无意义地烧一轮门卫调用（50 条/轮，
    每条 6–13s），直到配额熔断。今日 `ai_call_stats` 记录 gate 调用 **2,688 次**
    而主分析只有 **312 次**（8.6:1），根子就在这里。

    回放本身几乎全是白干：今日回放 145 条，判定分布是
    **排除 134 / 可交易 11**（命中率 7.6%）。
    """
    extra = f" OR {_UNJUDGED_SQL}" if _INCLUDE_UNJUDGED else ""
    # ⚠️ 括号必须只包住 OR 那一组，`AND status='PENDING'` 要留在组**外面**：
    #    写成 `(A AND status='PENDING' OR B)` 依然会按 AND>OR 解析成
    #    `((A AND status) OR B)` —— status 又被 OR 掉了，等于没修。
    _where = "(COALESCE(gate_unavailable,0)=1" + extra + ")"
    # v444（2026-11-18，用户定策）：断档水位之下的行**不回放**。
    # 回放是"补历史"的另一条通道（每 900s 一轮、每轮 50 条），如果只堵认领不堵这里，
    # 通道一恢复额度照样会被历史旧闻烧光 —— 用户要的是"只分析现在进来的"。
    try:
        import news_cutoff as _nc  # noqa: WPS433
        _floor = int(_nc.get_floor())
    except Exception:
        _floor = 0
    total = conn.execute(
        "SELECT COUNT(*) FROM raw_news WHERE " + _where
        + " AND (? <= 0 OR (ts IS NOT NULL AND ts >= ?))",
        (_floor, _floor),
    ).fetchone()[0]
    pending = conn.execute(
        "SELECT COUNT(*) FROM raw_news WHERE " + _where + " AND status='PENDING'"
        + " AND (? <= 0 OR (ts IS NOT NULL AND ts >= ?))",
        (_floor, _floor),
    ).fetchone()[0]
    return int(total or 0), int(pending or 0)


def probe_channels() -> Dict[str, Any]:
    """通道健康预检 —— 复用 config 的池 + 轻量请求。

    返回：{"ok": bool, "channels": [...], "reason": str}
    不抛异常：探测失败本身也是一种「不可用」。

    v322 二次加固（事故：2026-11-04 额度被打穿）：
      探针本身也是**真实计费请求**（每个通道一次，max_tokens=5）。
      本模块每 5 分钟一轮、一轮 N 个通道 → 一天几百次白烧的请求，
      而且探针完全绕过了主分析的额度闸门与熔断器。
      现在：额度用光 → 一个探针都不发（探针毫无意义，额度不够干正事）；
            通道已在熔断中 → 不探（探了也只是再确认一次已知事实）。
    """
    out: Dict[str, Any] = {"ok": False, "channels": [], "reason": ""}
    try:
        import config # noqa: WPS433
        import requests # noqa: WPS433
    except Exception as exc:  # pragma: no cover
        out["reason"] = "import failed: %s: %s" % (type(exc).__name__, exc)
        return out

    # ── v443：回放要给"新新闻"留额度，不能把预算烧到零 ────────────────
    # 事故（2026-11-17 23:40）：回放 50 条/300s ≈ 14,400 次/天，而门卫日预算 6,000、
    # 全站 9,000。回放一路跑到额度见底 → 新新闻的门卫请求被闸门直接拒 → 全部
    # 「未判定」降级放行 → 门卫等于不存在（今天因此积了 1,611 条 gate_down）。
    # 现在：回放占用量超过门卫预算的 60% 就跳过本轮，把额度留给实时新闻。
    try:
        import llm_budget as _lb_gate # noqa: WPS433
        _reserve_pct = float(os.getenv("GATE_REPLAY_RESERVE_PCT", "60") or 60)
        _u = _lb_gate.usage_by_purpose("gate", refresh=True)
        _used = int(_u.get("requests") or 0)
        _cap = int(os.getenv("REALTIME_GATE_DAILY_BUDGET", "6000") or 6000)
        if _cap > 0 and _used >= _cap * _reserve_pct / 100.0:
            out["reason"] = ("回放让位：门卫今日已用 %d/%d 次（≥%g%%），"
                            "额度优先留给实时新闻" % (_used, _cap, _reserve_pct))
            return out
    except Exception:
        pass

    # ── v322: 额度闸门 —— 额度用光时探针一律不发 ──────────────────────
    try:
        import llm_budget # noqa: WPS433
        _ok, _reason = llm_budget.check(0, purpose="gate")
        if not _ok:
            out["reason"] = "额度闸门拦截：%s" % _reason
            return out
    except Exception as exc:
        out["reason"] = "budget check failed: %s: %s" % (type(exc).__name__, exc)
        return out

    try:
        pool = config.get_pool_for_purpose("gate")
    except Exception as exc:
        out["reason"] = "pool unavailable: %s: %s" % (type(exc).__name__, exc)
        return out

    if not pool:
        out["reason"] = "gate pool empty"
        return out

    healthy = 0
    for m in pool:
        name = str(m.get("label") or m.get("id") or "?")
        base = str(m.get("api_base") or "").rstrip("/")
        key = str(m.get("api_key") or "")
        model = str(m.get("request_model") or m.get("id") or "")
        entry: Dict[str, Any] = {"name": name, "model": model}
        if not base or not key:
            entry["ok"] = False
            entry["error"] = "missing base/key"
            out["channels"].append(entry)
            continue

        # ── v322: 熔断中的通道不探（探了也只是再确认一次已知事实）────
        _alias = str(m.get("key_alias") or m.get("label") or m.get("id") or "unknown")
        try:
            from circuit_breaker import get as _cb_get  # noqa: WPS433
            _bq = _cb_get(f"llm_main_quota:{_alias}", fail_threshold=1, open_seconds=1800.0)
            _ba = _cb_get(f"llm_main_auth:{_alias}", fail_threshold=1, open_seconds=86400.0)
            if not (_bq.allow() and (_ba is None or _ba.allow())):
                entry["ok"] = False
                entry["skipped"] = "breaker open"
                entry["error"] = "通道熔断中（跳过探测，不消耗额度）"
                out["channels"].append(entry)
                continue
        except Exception:
            pass  # 熔断器不可用 → 照常探测（不因保护机制故障停摆）

        t0 = time.monotonic()
        # ── v322: 速率闸门（探针也是真实计费请求）────────────────────────
        try:
            import llm_budget as _lb_rpm
            _ok_rpm, _why_rpm = _lb_rpm.consume_rate_slot(purpose="probe")
            if not _ok_rpm:
                entry["ok"] = False
                entry["skipped"] = "rate_limited"
                entry["error"] = _why_rpm
                out["channels"].append(entry)
                continue
        except Exception:
            pass  # 闸门自身异常 → 照常探
        try:
            sess = requests.Session()
            sess.trust_env = bool(m.get("use_proxy"))
            resp = sess.post(
                base + "/chat/completions",
                json={"model": model,
                      "messages": [{"role": "user", "content": "ok"}],
                      "max_tokens": 5, "temperature": 0},
                headers={"Authorization": "Bearer %s" % key},
                timeout=(6, DEFAULT_PROBE_TIMEOUT),
            )
            entry["status_code"] = resp.status_code
            entry["latency_ms"] = round((time.monotonic() - t0) * 1000, 1)
            entry["ok"] = resp.status_code == 200
            if not entry["ok"]:
                entry["error"] = resp.text[:160]
        except Exception as exc:
            entry["ok"] = False
            entry["latency_ms"] = round((time.monotonic() - t0) * 1000, 1)
            entry["error"] = "%s: %s" % (type(exc).__name__, str(exc)[:160])
        if entry.get("ok"):
            healthy += 1
        out["channels"].append(entry)

    out["ok"] = healthy > 0
    out["healthy"] = healthy
    out["total"] = len(pool)
    if not out["ok"]:
        out["reason"] = "all %d gate channels unavailable" % len(pool)
    return out


def _replay_one(cur, conn, row: sqlite3.Row) -> Tuple[str, str]:
    """回放单条。

    返回 (结果, 详情)：结果 ∈ {"ok", "gate_unavailable", "error"}
      · ok               —— 门卫判完并写回成功（is_noise / gate_* 已补全）
      · gate_unavailable —— 门卫仍不可用，保持冻结等下轮
      · error            —— 写库失败，保持冻结等下轮（不丢数据）
    """
    from realtime_filter import _l3_semantic_gate  # 延迟导入，避免循环依赖

    content = row["content"] or ""
    title = content
    if title.startswith("[hash:") and "]" in title:
        title = title.split("]", 1)[1].strip()
    title = title[:300]

    try:
        gate = _l3_semantic_gate(title, content[:500])
    except Exception as exc:
        return "error", "%s: %s" % (type(exc).__name__, str(exc)[:120])

    if gate is None:
        # 门卫仍不可用 → 保持冻结（不消耗后续决策额度，不丢数据）
        return "gate_unavailable", "gate LLM unavailable"

    verdict = str(gate.get("verdict") or "")
    is_noise = 1 if verdict == "排除" else 0
    # status 必须是 PENDING —— raw_news 的 CHECK 约束只允许
    # PENDING/PROCESSING/DONE/FAILED，写 'FILTERED' 会 IntegrityError。
    # 语义与 engine/ingest.py 入库路径一致：区分「排除/可交易」靠 is_noise。
    try:
        cur.execute(
            "UPDATE raw_news SET gate_unavailable=0, is_noise=?, status='PENDING', "
            # v321.1: 解冻 = 重新入队 → 重置重试计数，给满 3 次机会。
            # （原先这个重置错放在 ai_worker 的认领语句里 → 每次认领都清零，
            #   retry_count 永远到不了阈值 → 失败行无限重试。见 ai_worker v321.1。）
            "retry_count=0, "
            "relevance_score=CASE WHEN ?=1 THEN 0.0 ELSE relevance_score END, "
            "gate_core_impact=?, gate_is_material=?, gate_assets=?, "
            "gate_main_asset=?, gate_kind=?, gate_timing=?, gate_strength=?, "
            "gate_verdict=?, gate_impact_1to100=? WHERE id=?",
            (
                is_noise,
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
                row["id"],
            ),
        )
        conn.commit()   # 独立短事务：不攒批（见模块 docstring）
        return "ok", "verdict=%s is_noise=%d" % (verdict, is_noise)
    except sqlite3.Error as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        return "error", "%s: %s" % (type(exc).__name__, str(exc)[:120])


def run_once(limit: int = DEFAULT_BATCH_LIMIT,
             db_path: Optional[Path] = None,
             skip_probe: bool = False) -> Dict[str, Any]:
    """跑一轮回放。返回统计结果（不抛异常）。"""
    stat: Dict[str, Any] = {
        "ts": time.time(),
        "time_str": _now_str(),
        "skipped": False,
        "reason": "",
        "probe_ok": None,
        "ok": 0,
        "gate_unavailable": 0,
        "error": 0,
        "remaining": 0,
    }

    path = db_path or _db_path()
    if not path.exists():
        stat["reason"] = "db not found: %s" % path
        return stat

    conn = _connect(path)
    try:
        total, pending = count_backlog(conn)
        stat["backlog_total"] = total
        stat["backlog_pending"] = pending
        if not pending:
            stat["reason"] = "no backlog"
            return stat

        # 通道健康预检 —— 通道不健康就别消耗额度
        if not skip_probe:
            probe = probe_channels()
            stat["probe_ok"] = probe.get("ok")
            stat["probe_channels"] = probe.get("channels")
            if not probe.get("ok"):
                stat["skipped"] = True
                stat["reason"] = probe.get("reason") or "channels unavailable"
                stat["remaining"] = pending
                return stat

        extra = f" OR {_UNJUDGED_SQL}" if _INCLUDE_UNJUDGED else ""
        # v444：取候选时同样按断档水位过滤（与 count_backlog 同口径）。
        try:
            import news_cutoff as _nc  # noqa: WPS433
            _floor = int(_nc.get_floor())
        except Exception:
            _floor = 0
        rows = conn.execute(
            "SELECT id, source, content, ts, timestamp FROM raw_news "
            "WHERE (COALESCE(gate_unavailable,0)=1" + extra + ") AND status='PENDING' "
            "AND (? <= 0 OR (ts IS NOT NULL AND ts >= ?)) "
            "ORDER BY id ASC LIMIT ?",
            (_floor, _floor, max(1, int(limit))),
        ).fetchall()
        if not rows:
            stat["reason"] = "no claimable rows"
            stat["remaining"] = pending
            return stat

        cur = conn.cursor()
        # v321: 连续失败计数 —— 单条判不了只跳过该条，继续处理后面的；
        # 连续多条都判不了才判定"门卫通道真挂了"，停本轮（预检已拦过一次，
        # 这里防的是运行中通道挂掉的情况）。
        consecutive_fail = 0
        for r in rows:
            res, detail = _replay_one(cur, conn, r)
            if res == "ok":
                stat["ok"] += 1
                consecutive_fail = 0
                print("[gate-replay] #%d ok  %s" % (r["id"], detail), flush=True)
                continue
            consecutive_fail += 1
            if res == "gate_unavailable":
                stat["gate_unavailable"] += 1
                print("[gate-replay] #%d 门卫判不了此条，保持冻结（继续下一条）"
                      % r["id"], flush=True)
            else:
                stat["error"] += 1
                print("[gate-replay] #%d 写库失败: %s" % (r["id"], detail), flush=True)
            if consecutive_fail >= MAX_CONSECUTIVE_FAILS:
                print("[gate-replay] 连续 %d 条失败，本轮停止（保留剩余冻结）"
                      % consecutive_fail, flush=True)
                break

        _, pending_after = count_backlog(conn)
        stat["remaining"] = pending_after
        stat["reason"] = "ok=%d frozen=%d err=%d remaining=%d" % (
            stat["ok"], stat["gate_unavailable"], stat["error"], pending_after)
        return stat
    except Exception as exc:
        stat["reason"] = "run_once failed: %s: %s" % (type(exc).__name__, str(exc)[:200])
        return stat
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _write_progress(stat: Dict[str, Any]) -> None:
    """落盘进度，供运维/前端查看。"""
    try:
        runtime = SRC_DIR.parent / "runtime"
        runtime.mkdir(parents=True, exist_ok=True)
        with open(runtime / "gate_replay_progress.json", "w", encoding="utf-8") as f:
            json.dump(stat, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="门卫回放守护（自愈 gate_unavailable 积压）")
    ap.add_argument("--once", action="store_true", help="跑一轮就退出（cron 用）")
    ap.add_argument("--limit", type=int, default=DEFAULT_BATCH_LIMIT,
                    help="每轮最多回放条数（默认 %d）" % DEFAULT_BATCH_LIMIT)
    ap.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_S,
                    help="两轮间隔秒数（默认 %d）" % DEFAULT_INTERVAL_S)
    ap.add_argument("--skip-probe", action="store_true",
                    help="跳过通道健康预检（不推荐，会盲消耗额度）")
    args = ap.parse_args()

    if args.once:
        stat = run_once(limit=args.limit, skip_probe=args.skip_probe)
        _write_progress(stat)
        print("[gate-replay] %s" % stat.get("reason"), flush=True)
        return 0

    print("[gate-replay] 守护启动，每 %.0fs 一轮，每轮最多 %d 条"
          % (args.interval, args.limit), flush=True)
    while True:
        try:
            stat = run_once(limit=args.limit, skip_probe=args.skip_probe)
            _write_progress(stat)
            if stat.get("ok"):
                print("[gate-replay] %s" % stat.get("reason"), flush=True)
        except Exception as exc:
            print("[gate-replay] 轮次异常: %s: %s" % (type(exc).__name__, exc), flush=True)
        time.sleep(max(30.0, args.interval))
    return 0


if __name__ == "__main__":
    sys.exit(main())
