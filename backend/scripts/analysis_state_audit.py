#!/usr/bin/env python3
"""analysis_state_audit.py — 用现有账本复算「六终态 + HOLD 原因」的分布。

只读：不写库、不改口径。用途是在接入任何运行时改动之前，先把「现在的 89.2% 降级」
拆成真实的原因分布，回答两个问题：

  1. 有多少条其实只是「不在服务范围」（不该算分析失败）？
  2. 剩下真正失败的，是数据没到、模型没答，还是输出结构不达标？

用法：
  python3 backend/scripts/analysis_state_audit.py                       # 生产库，全部日期
  python3 backend/scripts/analysis_state_audit.py --date 2026-11-16
  python3 backend/scripts/analysis_state_audit.py --db /path/to/copy.db
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from collections import Counter, defaultdict

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_BACKEND, "src_python")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from analysis import contract as ac
from signals import decision_provenance as dp


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn


def _load(conn, date: str | None, since: str | None):
    sql = ("SELECT id, created_at, suggested_action, target_asset, "
           "analysis_degraded, analysis_degraded_reason, hard_data_status, "
           "asset_route, trade_gate_reason, reasoning_path "
           "FROM ai_decisions WHERE 1=1")
    params = []
    if date:
        sql += " AND created_at >= ? AND created_at < ?"
        params += [f"{date}T00:00:00", f"{date}T23:59:59.999"]
    elif since:
        sql += " AND created_at >= ?"
        params += [since]
    return conn.execute(sql, params).fetchall()


def _hard_status(row) -> str:
    hs = str(row["hard_data_status"] or "").strip()
    if hs:
        return hs
    # 老行没写 hard_data_status → 按同一套规则现算（只描述，不改事实）
    return dp.hard_status(row["target_asset"], "")


def audit(rows) -> dict:
    states = Counter()
    reasons = Counter()
    by_state_action = defaultdict(Counter)
    cross = Counter()

    for row in rows:
        asset = str(row["target_asset"] or "")
        hard = _hard_status(row)
        state = ac.derive_state(
            degraded=int(row["analysis_degraded"] or 0),
            reason=str(row["analysis_degraded_reason"] or ""),
            asset=asset,
            hard_status=hard,
        )
        reason = ac.derive_no_trade_reason(
            action=str(row["suggested_action"] or ""),
            state=state,
            gate_reason=str(row["trade_gate_reason"] or ""),
            news_age_min=None,      # 无法从 created_at 反推新闻年龄，跳过 stale 判定
        )
        states[state] += 1
        reasons[reason or "(已下单)"] += 1
        by_state_action[state][str(row["suggested_action"] or "").upper()] += 1
        if str(row["analysis_degraded_reason"] or "") == "target_none":
            cross[("target_none", state)] += 1
    return {
        "total": len(rows),
        "states": states,
        "reasons": reasons,
        "by_state_action": by_state_action,
        "target_none_cross": cross,
    }


def _pct(n, total):
    return f"{(n / total * 100 if total else 0):5.1f}%"


def _window_ms(date: str | None, since: str | None) -> tuple[int, int]:
    """把 --date / --since 换成 epoch ms 区间（本地日界，Beijing）。"""
    import datetime as _dt
    if date:
        d0 = _dt.datetime.strptime(date, "%Y-%m-%d")
        tz = _dt.timezone(_dt.timedelta(hours=8))
        lo = int(d0.replace(tzinfo=tz).timestamp() * 1000)
        return lo, lo + 86_400_000
    if since:
        try:
            lo = int(_dt.datetime.fromisoformat(since).timestamp() * 1000)
        except ValueError:
            lo = 0
        return lo, lo + 86_400_000
    return 0, 2 ** 62


def failure_attribution(conn, date: str | None, since: str | None,
                        llm_failed: int, parse_failed: int) -> list[tuple[str, int, str]]:
    """把「模型失败」拆成可归因的四类（只用 ai_call_stats 的既有埋点，不改库）。

    为什么要拆：昨天 96 条 llm_failed 里既有「调用超时」，也有「调用成功但输出空」，
    混成一个数字就只能瞎治。这里按调用侧证据分开：
      ① 网络/超时/配额（status != ok）
      ② 调用成功但输出过短（tokens_out < 600，八段根本写不完）
      ③ 调用成功、输出正常，但入库后仍被判 llm_failed（多为解析后空字段）
      ④ 结构不达标（parse_failed）
    """
    lo, hi = _window_ms(date, since)
    out: list[tuple[str, int, str]] = []
    try:
        bad = conn.execute(
            """SELECT status, count(*) FROM ai_call_stats
               WHERE purpose='main' AND status<>'ok' AND ts*1000>=? AND ts*1000<?"""
            " GROUP BY 1", (lo, hi)).fetchall()
        ok = conn.execute(
            """SELECT count(*), sum(tokens_out<600), avg(tokens_out)
               FROM ai_call_stats
               WHERE purpose='main' AND status='ok' AND ts*1000>=? AND ts*1000<?""",
            (lo, hi)).fetchone()
    except Exception as e:
        return [("（调用统计不可用）", 0, f"{type(e).__name__}: {e}")[0:3]]

    bad_total = sum(n for _, n in bad)
    for st, n in bad:
        out.append((f"主分析调用失败：{st}", n, "网络/超时/配额"))
    ok_n = int(ok[0] or 0)
    short = int(ok[1] or 0)
    out.append(("调用成功但输出 <600 tokens", short,
                f"占成功调用 {short}/{ok_n}（{(short/ok_n*100 if ok_n else 0):.0f}%），八段写不完 → 多是 compressed/parse_failed 的来源"))
    out.append(("调用失败合计", bad_total, "对应不到决策行的部分不会落 llm_failed"))
    out.append(("决策侧 llm_failed", llm_failed, "含「调用成功但返回空」的兜底"))
    out.append(("决策侧 parse_failed", parse_failed, "结构不达标，不是模型没答"))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(_BACKEND, "trident_event_bus.db"))
    ap.add_argument("--date", default=None, help="YYYY-MM-DD（本地日界，created_at 带 +08:00）")
    ap.add_argument("--since", default=None, help="created_at >= 该字符串")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"[FATAL] 库不存在：{args.db}")
        return 2

    conn = _connect(args.db)
    try:
        rows = _load(conn, args.date, args.since)
    finally:
        pass  # 连接留到失败归因统计后再关

    res = audit(rows)
    total = res["total"]
    print(f"\n== 分析终态审计 ==  库={args.db}  范围={args.date or args.since or '全部'}  行数={total}\n")
    if not total:
        print("（该范围内没有决策行）")
        return 0

    print("终态分布（derive_state 复算）")
    for st in ac.ANALYSIS_STATES:
        n = res["states"].get(st, 0)
        acts = res["by_state_action"].get(st, {})
        act_txt = " ".join(f"{k}={v}" for k, v in sorted(acts.items()))
        print(f"  {st:<16} {n:>5}  {_pct(n, total)}   {ac.STATE_LABELS[st]:<8}  {act_txt}")

    real_fail = sum(res["states"].get(s, 0) for s in
                    ("data_unavailable", "llm_failed", "parse_failed"))
    print(f"\n其中「不在服务范围」 {res['states'].get('out_of_scope', 0)} 条"
          f"（{_pct(res['states'].get('out_of_scope', 0), total)}）—— 不该计入分析失败")
    print(f"真实分析失败        {real_fail} 条（{_pct(real_fail, total)}）"
          f"，其中 数据 {res['states'].get('data_unavailable', 0)} / "
          f"模型 {res['states'].get('llm_failed', 0)} / "
          f"结构 {res['states'].get('parse_failed', 0)}")

    print("\nHOLD 原因分布（derive_no_trade_reason 复算）")
    for r, n in res["reasons"].most_common():
        print(f"  {r:<18} {n:>5}  {_pct(n, total)}")

    if res["target_none_cross"]:
        print("\ntarget_none 的去向（证明越界/失败被混记）")
        for (reason, st), n in res["target_none_cross"].most_common():
            print(f"  reason={reason:<12} → {st:<16} {n:>5}")

    print("\n失败归因拆分（模型失败到底失败在哪一环）")
    for name, n, note in failure_attribution(
            conn, args.date, args.since,
            res["states"].get("llm_failed", 0), res["states"].get("parse_failed", 0)):
        print(f"  {name:<26} {n:>5}   {note}")
    conn.close()

    print("\n口径说明：本次审计不把它写回库；接入运行时后，"
          "六终态将落 analysis_state，HOLD 原因将落 no_trade_reason。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
