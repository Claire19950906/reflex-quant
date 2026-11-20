#!/usr/bin/env python3
"""过夜体检：第二天一早一条命令回答「昨晚跑得怎么样」。

为什么单独写一个：
    `daily_funnel_report.py` 回答的是「漏斗卡在哪一段」，`audit_jobs.py` 回答
    「哪些定时任务在报错」，`audit_indicator_freshness.py` 回答「哪些指标停更」。
    过夜复盘需要的是**一次把四件事拼起来**：样本够不够、成本多少、稳不稳、
    指纹有没有混代。分散在四个脚本里，早上要跑四遍再自己对齐时间窗口。

只读：不写业务表、不改配置；唯一写的是 reports/overnight/ 下的报告文件。

用法：
    python3 scripts/overnight_report.py                # 今天
    python3 scripts/overnight_report.py --date 2026-11-20
    python3 scripts/overnight_report.py --no-write     # 只打印
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import os
import re
import sqlite3
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "backend", "src_python"))
import db
import shadow_score


def _q(conn, sql, params=()):
    try:
        return conn.execute(sql, params).fetchone()[0]
    except sqlite3.Error as e:
        print(f"[OVERNIGHT] 查询失败: {e} | SQL={sql.strip()[:70]}", file=sys.stderr)
        return -1


def _fmt(n) -> str:
    if n is None or n < 0:
        return "—"
    return f"{int(n):,}"


def _pct(a, b) -> str:
    if not b or b <= 0 or a is None or a < 0:
        return "—"
    return f"{a / b * 100:.1f}%"


def _jobs_audit() -> tuple[int, int, list[str]]:
    """调 audit_jobs.py 拿「作业总数 / 有问题数 / 问题作业名」。"""
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        out = subprocess.run(
            [sys.executable, os.path.join(repo, "scripts", "audit_jobs.py")],
            capture_output=True, text=True, timeout=120,
        ).stdout
    except Exception as e:
        return -1, -1, [f"audit_jobs 执行失败: {e}"]
    m = re.search(r"作业 (\d+) 个，有问题 (\d+) 个", out)
    total = int(m.group(1)) if m else -1
    bad = int(m.group(2)) if m else -1
    names = re.findall(r"\[(com\.trident\.[^\]]+)\]", out)
    return total, bad, sorted(set(names))


def _lock_counts() -> dict:
    """逐日志文件统计 `database is locked` 出现次数（用于做增量基线）。"""
    log_dir = os.path.expanduser("~/.trident/log")
    out = {}
    for path in glob.glob(os.path.join(log_dir, "*.log")):
        try:
            with open(path, encoding="utf-8", errors="ignore") as f:
                n = f.read().count("database is locked")
        except Exception:
            continue
        if n:
            out[os.path.basename(path)] = n
    return out


def _baseline_path() -> str:
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(repo, "backend", "reports", "overnight", ".lock_baseline.json")


def _lock_hits() -> tuple[int, int, bool]:
    """相对基线的锁冲突增量（没基线就只能报涉及作业数，不能报次数）。"""
    counts = _lock_counts()
    base, has_base = {}, False
    try:
        with open(_baseline_path(), encoding="utf-8") as f:
            data = json.load(f)
        base = data.get("counts", {})
        has_base = True
    except Exception:
        pass
    delta = sum(max(0, n - base.get(k, 0)) for k, n in counts.items()) if has_base else -1
    jobs = len(counts)
    return delta, jobs, has_base


def _snapshot_locks() -> str:
    counts = _lock_counts()
    os.makedirs(os.path.dirname(_baseline_path()), exist_ok=True)
    with open(_baseline_path(), "w", encoding="utf-8") as f:
        json.dump({"ts": dt.datetime.now().isoformat(timespec="seconds"),
                   "counts": counts}, f, ensure_ascii=False, indent=2)
    return _baseline_path()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=dt.date.today().isoformat())
    ap.add_argument("--no-write", action="store_true")
    ap.add_argument("--snapshot", action="store_true",
                    help="只记录当前锁日志计数作为基线，然后退出")
    args = ap.parse_args()
    if args.snapshot:
        print(f"锁基线已记录: {_snapshot_locks()}")
        return 0
    day = args.date

    conn = sqlite3.connect(f"file:{db.get_db_path()}?mode=ro", uri=True, timeout=30)
    start = int(dt.datetime.combine(dt.date.fromisoformat(day), dt.time.min).timestamp())
    end = start + 86400

    dec = _q(conn, "SELECT COUNT(*) FROM ai_decisions WHERE ts >= ? AND ts < ?", (start, end))
    dec_all = _q(conn, "SELECT COUNT(*) FROM ai_decisions")
    hold = _q(conn, "SELECT COUNT(*) FROM ai_decisions WHERE ts >= ? AND ts < ? "
                    "AND UPPER(suggested_action)='HOLD'", (start, end))
    eff = _q(conn, "SELECT COUNT(*) FROM ai_decisions WHERE ts >= ? AND ts < ? "
                   "AND COALESCE(analysis_degraded,0)=0 "
                   "AND UPPER(suggested_action)<>'HOLD'", (start, end))
    none_asset = _q(conn, "SELECT COUNT(*) FROM ai_decisions WHERE ts >= ? AND ts < ? "
                          "AND UPPER(COALESCE(target_asset,'')) IN ('','NONE')", (start, end))
    trades = _q(conn, "SELECT COUNT(*) FROM quick_sim_trades")
    settled = _q(conn, "SELECT COUNT(*) FROM quick_sim_trades WHERE COALESCE(settled,0)=1 "
                       "AND COALESCE(verdict,'')<>'ABANDONED'")
    win = _q(conn, "SELECT COUNT(*) FROM quick_sim_trades WHERE UPPER(COALESCE(verdict,''))='WIN'")
    pnl = _q(conn, "SELECT COALESCE(SUM(pnl_usdt),0) FROM quick_sim_trades "
                   "WHERE COALESCE(settled,0)=1")
    no_fp = _q(conn, "SELECT COUNT(*) FROM quick_sim_trades "
                     "WHERE COALESCE(code_fingerprint,'')=''")

    calls = _q(conn, "SELECT COUNT(*) FROM ai_call_stats WHERE ts >= ? AND ts < ?", (start, end))
    fails = _q(conn, "SELECT COUNT(*) FROM ai_call_stats WHERE ts >= ? AND ts < ? "
                     "AND status<>'ok'", (start, end))
    tokens = _q(conn, "SELECT COALESCE(SUM(tokens_in+tokens_out),0) FROM ai_call_stats "
                      "WHERE ts >= ? AND ts < ?", (start, end))
    cost_rows = []
    try:
        cost_rows = conn.execute(
            "SELECT purpose, COUNT(*), COALESCE(SUM(tokens_in+tokens_out),0) "
            "FROM ai_call_stats WHERE ts >= ? AND ts < ? GROUP BY purpose "
            "ORDER BY 3 DESC", (start, end)
        ).fetchall()
    except sqlite3.Error:
        pass
    fp_rows = []
    try:
        fp_rows = conn.execute(
            "SELECT COALESCE(NULLIF(code_fingerprint,''),'(未标注)') fp, COUNT(*), "
            "SUM(COALESCE(settled,0)), "
            "SUM(CASE WHEN UPPER(COALESCE(verdict,''))='WIN' THEN 1 ELSE 0 END), "
            "COALESCE(SUM(pnl_usdt),0) FROM quick_sim_trades GROUP BY 1 ORDER BY 2 DESC"
        ).fetchall()
    except sqlite3.Error:
        pass

    degr_rows = []
    try:
        degr_rows = conn.execute(
            "SELECT COALESCE(NULLIF(analysis_degraded_reason,''),'(未注明)') r, COUNT(*) "
            "FROM ai_decisions WHERE ts >= ? AND COALESCE(analysis_degraded,0)=1 "
            "GROUP BY 1 ORDER BY 2 DESC LIMIT 8", (start,)
        ).fetchall()
    except sqlite3.Error:
        pass

    shadow_rows, shadow_err = [], ""
    try:
        shadow_rows = shadow_score.compute_scores(conn, days_back=10)
    except Exception as e:  # K 线缺失/表结构变动都不该让整份报告挂掉
        shadow_err = f"{type(e).__name__}: {e}"
    shadow_stats = shadow_score.summarize(shadow_rows) if shadow_rows else []

    # 双台账一致性：quick_sim_trades（成交）与 trade_decisions（打点）各记一套，
    # 关联键是 decision_id ↔ ai_decision_id。对不上就意味着"成交了没留痕"
    # 或"留痕了没成交"，两种都会让按指纹分层失真。
    orphan_trade = _q(conn,
        "SELECT COUNT(*) FROM quick_sim_trades t WHERE t.decision_id IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM trade_decisions d "
        "WHERE d.ai_decision_id = t.decision_id)")
    orphan_mark = _q(conn,
        "SELECT COUNT(*) FROM trade_decisions d WHERE d.quick_sim_trade_id IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM quick_sim_trades t WHERE t.id = d.quick_sim_trade_id)")
    fp_mismatch = _q(conn,
        "SELECT COUNT(*) FROM quick_sim_trades t JOIN trade_decisions d "
        "ON d.ai_decision_id = t.decision_id "
        "WHERE COALESCE(t.code_fingerprint,'') <> COALESCE(d.code_fingerprint,'')")

    restarts = _q(conn, "SELECT COUNT(*) FROM daily_reset_log WHERE reset_date = ?", (day,))
    destructive = _q(conn, "SELECT COUNT(*) FROM daily_reset_log WHERE reset_date = ? "
                           "AND COALESCE(cleared_decisions,0) > 0", (day,))
    refl = _q(conn, "SELECT COUNT(*) FROM trade_reflections WHERE created_at >= ?", (day,))
    conn.close()

    paused = os.path.exists(os.path.expanduser("~/.trident/state/DATA_PAUSED"))
    job_total, job_bad, job_names = _jobs_audit()
    lock_hits, lock_jobs, has_base = _lock_hits()

    verdict = []
    if paused and not dec:
        verdict.append("**当天数据处于暂停状态**（DATA_PAUSED 存在）：0 条是暂停，不是故障")
    if settled >= 60:
        verdict.append(f"已结算 {settled} 笔 ≥ 60：数字**可以下结论**")
    else:
        verdict.append(f"已结算 {settled} 笔 < 60：**任何胜率/均值都不构成结论**")
    if no_fp:
        verdict.append(f"{no_fp} 笔成交没有代码指纹 —— 代次不可追溯")
    if destructive:
        verdict.append(f"当天有 {destructive} 次**破坏性重置**（cleared_decisions>0）"
                       " —— 样本被清过，跨日不可比")

    lines = [
        f"# 过夜体检 {day}",
        "",
        "## 一句话结论",
        *[f"- {v}" for v in verdict],
        "",
        "| 指标 | 数值 | 口径 |",
        "|---|---|---|",
        f"| 当日决策 | {_fmt(dec)} | HOLD {_fmt(hold)}（{_pct(hold, dec)}） |",
        f"| **有效判断**（未降级且非 HOLD） | **{_fmt(eff)}** | 真实产能 |",
        f"| 标的为空（NONE） | {_fmt(none_asset)} | {_pct(none_asset, dec)}，即使过闸也会被拦 |",
        f"| 累计成交 | {_fmt(trades)} | 台账 quick_sim_trades |",
        f"| 已结算 | {_fmt(settled)} | 门槛 60；胜 {_fmt(win)} |",
        f"| 累计净盈亏 | {float(pnl or 0):+.2f} USDT | 仅已结算 |",
        "",
        "## 0b) 降级原因（今日）",
        "| 原因 | 条数 |",
        "|---|---|",
    ]
    if degr_rows:
        lines += [f"| {r} | {n} |" for r, n in degr_rows]
    else:
        lines += ["| — | 0 |"]
    lines += [
        "",
        "## 1) 成交按代码指纹分层",
        "| 指纹（git/参数） | 成交 | 已结算 | 胜 | 净PnL(USDT) |",
        "|---|---|---|---|---|",
    ]
    if fp_rows:
        for fp, n, s, w, p in fp_rows:
            lines.append(f"| `{fp}` | {n} | {int(s or 0)} | {int(w or 0)} | {float(p or 0):+.2f} |")
        if len(fp_rows) > 1:
            lines.append("- 多指纹并存：**不要合并算胜率**，先按代次看样本量")
    else:
        lines.append("| — | — | — | — | — |")

    lines += [
        "",
        "## 1b) 决策级影子打分（不等开仓，1h/4h/24h）",
    ]
    if shadow_err:
        lines.append(f"- 计算失败：{shadow_err}")
    elif not shadow_stats:
        lines.append("- 暂无到期的 BUY/SELL 样本（1h 窗口最快，决策后一小时才有数）")
    else:
        lines += [
            "| 指纹 | 窗口 | 样本 | 胜 | 负 | 胜率 | 均值净收益% |",
            "|---|---|---|---|---|---|---|",
        ]
        for st in shadow_stats:
            label = next(lb for sec, lb in shadow_score.HORIZONS
                         if sec == st["horizon_sec"])
            wr = f"{st['win_rate'] * 100:.1f}%" if st["win_rate"] is not None else "—"
            lines.append(f"| `{st['fingerprint']}` | {label} | {st['n']} | {st['win']} | "
                         f"{st['loss']} | {wr} | {st['avg_net_pct']:+.3f} |")
        lines.append("- 净收益已扣 8bps 往返成本；样本 < 30 只看方向与成本，不看胜率")
    lines += [
        "",
        "## 2) AI 成本",
        "| 用途 | 调用 | token | 占当日 |",
        "|---|---|---|---|",
    ]
    for purpose, n, tok in cost_rows:
        lines.append(f"| {purpose or '—'} | {_fmt(n)} | {_fmt(tok)} | {_pct(tok, tokens)} |")
    lines += [
        "",
        f"- 合计 **{_fmt(calls)} 次调用 / {_fmt(tokens)} token**，"
        f"失败 {_fmt(fails)}（{_pct(fails, calls)}）",
        f"- 每决策成本：{(tokens / dec):,.0f} token/条" if dec and dec > 0 and tokens >= 0
        else "- 每决策成本：—",
        f"- 每有效判断成本：{(tokens / eff):,.0f} token/条" if eff and eff > 0 and tokens >= 0
        else "- 每有效判断成本：—",
        "",
        "## 2b) 双台账一致性（成交 ↔ 打点）",
        f"- 成交没有打点：**{_fmt(orphan_trade)}** 笔",
        f"- 打点指向了不存在的成交：**{_fmt(orphan_mark)}** 条",
        f"- 同一决策两表指纹不一致：**{_fmt(fp_mismatch)}** 笔",
    ]
    lines += [
        "",
        "## 3) 稳定性",
        f"- 数据暂停标记：**{'存在' if paused else '无'}**",
        f"- 引擎启动次数（daily_reset_log, trigger=startup）：**{_fmt(restarts)}**",
        f"- 破坏性重置（cleared_decisions>0）：**{_fmt(destructive)}**",
        f"- launchd 作业：{_fmt(job_total)} 个，**有问题 {_fmt(job_bad)} 个**",
        ("- 锁冲突（相对昨晚基线）：**" + _fmt(lock_hits) + " 次**，涉及 "
         + _fmt(lock_jobs) + " 个作业"
         if has_base else
         "- 锁冲突：**无基线**，只能报涉及 " + _fmt(lock_jobs)
         + " 个作业（次数不可比，下次先跑 --snapshot）"),
        f"- 反思落库（trade_reflections）：{_fmt(refl)} 条",
    ]
    if job_names:
        lines.append(f"- 问题作业：{', '.join(job_names[:12])}"
                     + (" …" if len(job_names) > 12 else ""))

    lines += [
        "",
        "## 4) 明早优先看什么",
    ]
    tips = []
    if destructive:
        tips.append("先看是谁在重置：`SELECT reset_at,notes FROM daily_reset_log "
                    "WHERE cleared_decisions>0`")
    if eff and eff < 10:
        tips.append(f"有效判断只有 {eff} 条：先查 NONE {none_asset} 条与降级原因，不要先调参")
    if lock_hits > 50:
        tips.append("锁冲突仍高：优先合并高频作业，而不是继续加 busy_timeout")
    if not tips:
        tips.append("指标无异常，按指纹分层继续攒样本")
    lines += [f"{i + 1}. {t}" for i, t in enumerate(tips)]

    text = "\n".join(lines)
    print(text)
    if not args.no_write:
        out_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "backend", "reports", "overnight")
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, f"overnight_{day}.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        print(f"\n已写入 {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
