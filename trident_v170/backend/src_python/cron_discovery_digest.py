#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v276: 每日发现汇总 cron — 早 7:00 推一份"昨日发现汇总"。

调度方: launchd (com.trident.discovery-digest-cron.plist)
入口: run_discovery_digest_cron.sh 通过 python3 cron_discovery_digest.py 调起。

汇总范围(过去 24h):
  ① Fed 路径异动(fed_rate_expectation 跨日快照对比)
  ② 跨资产同步异常(从 abnormal_attributions 查 severity_hint>=2 的 multi_factor)
  ③ 周期反转信号(从 cycle_reversal_signals 落库,v276 起所有检测到信号都入库)
  ④ 重要 event_atom(direction_confidence * magnitude_value 高的)
  ⑤ 漏检信号(missed_signal=1 的 ai_decisions)
  ⑥ KG / 因果链 自动审批(cron_kg_review + cron_causal_expand 结果)

输出: Markdown 摘要 → send_important_news() 推到 Server酱/邮件/PushPlus

设计原则:
  - 不依赖 api daemon(直接调模块函数)
  - 失败不 panic:任一子任务失败 → 其它继续 + 错误写入摘要
  - 不修改任何主表(只读 db)
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from typing import Any, Dict

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import db  # noqa: E402


LOG_DIR = "/Users/claire/.trident/log"
LOG_FILE = os.path.join(LOG_DIR, "cron_discovery_digest.log")


def _digest_fed() -> Dict[str, Any]:
    """过去 24h Fed 路径异动摘要。"""
    out: Dict[str, Any] = {"ok": False, "anomaly_count": 0}
    try:
        cutoff = int(time.time()) - 86400
        conn = sqlite3.connect(db.get_db_path())
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                SELECT ts, dgs2_pct, effr_pct, spread_bp, source
                  FROM fed_rate_history
                 WHERE ts >= ?
                 ORDER BY ts ASC
                """,
                (cutoff,),
            ).fetchall()
            if len(rows) < 2:
                out["note"] = "fed_rate_history 样本不足(<2 条)"
                return out
            first = dict(rows[0])
            last = dict(rows[-1])
            dgs2_change_bp = (last["dgs2_pct"] - first["dgs2_pct"]) * 100
            spread_change = last["spread_bp"] - first["spread_bp"]
            out["ok"] = True
            out["anomaly_count"] = (
                1 if abs(dgs2_change_bp) >= 10 or abs(spread_change) >= 15 else 0
            )
            out["dgs2_change_bp"] = round(dgs2_change_bp, 2)
            out["spread_change_bp"] = round(spread_change, 2)
            out["dgs2_first"] = round(first["dgs2_pct"], 3)
            out["dgs2_last"] = round(last["dgs2_pct"], 3)
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


def _digest_cross_asset() -> Dict[str, Any]:
    """过去 24h 跨资产同步异常摘要。

    v276: 查 abnormal_attributions(已有的跨资产 attribution 表),按 severity_hint 分组统计。
    高 8+ 视为重要(≥2 资产同步异常对齐)。
    """
    out: Dict[str, Any] = {"ok": False, "anomaly_count": 0}
    try:
        cutoff = int(time.time()) - 86400
        conn = sqlite3.connect(db.get_db_path())
        conn.row_factory = sqlite3.Row
        try:
            # 高严重度(severity_hint >= 2)且多因子(multi_factor)
            rows = conn.execute(
                """
                SELECT id, asset, signal_kind, anchor_ts, summary, severity_hint
                  FROM abnormal_attributions
                 WHERE anchor_ts >= ?
                   AND severity_hint >= 2
                   AND signal_kind IN ('multi_factor', 'cross_asset', 'multi_asset')
                 ORDER BY severity_hint DESC, anchor_ts DESC LIMIT 10
                """,
                (cutoff,),
            ).fetchall()
            total_rows = conn.execute(
                """
                SELECT COUNT(*) FROM abnormal_attributions
                 WHERE anchor_ts >= ? AND severity_hint >= 1
                """,
                (cutoff,),
            ).fetchone()[0]
            out["anomaly_count"] = len(rows)
            out["total_signals_24h"] = int(total_rows or 0)
            if rows:
                out["top5"] = [
                    {
                        "asset": r["asset"],
                        "kind": r["signal_kind"],
                        "severity_hint": int(r["severity_hint"] or 0),
                        "summary": (r["summary"] or "")[:60],
                        "ts": int(r["anchor_ts"]),
                    }
                    for r in rows[:5]
                ]
                out["last_ts"] = int(rows[0]["anchor_ts"])
            else:
                out["note"] = "近 24h 无高严重度跨资产异常"
            out["ok"] = True
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


def _digest_cycle() -> Dict[str, Any]:
    """过去 24h 周期反转信号摘要。

    查 cycle_reversal_signals 表(由 cycle_reversal_watcher.py 实时写入,
    v276 起所有 ≥MIN_VOTES 的信号都落库;sent=1 是已推送,sent=0 是已检测到但未推送)。
    """
    out: Dict[str, Any] = {"ok": False, "signal_count": 0}
    try:
        cutoff = int(time.time()) - 86400
        conn = sqlite3.connect(db.get_db_path())
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                SELECT asset, direction, prob, votes, days_to, sent, phase, ts
                  FROM cycle_reversal_signals
                 WHERE ts >= ?
                 ORDER BY prob DESC LIMIT 10
                """,
                (cutoff,),
            ).fetchall()
            sent_n = conn.execute(
                "SELECT COUNT(*) FROM cycle_reversal_signals WHERE ts >= ? AND sent = 1",
                (cutoff,),
            ).fetchone()[0]
            out["signal_count"] = len(rows)
            out["sent_count"] = int(sent_n or 0)
            out["top5"] = [
                {
                    "asset": r["asset"],
                    "direction": r["direction"],
                    "prob": round(float(r["prob"] or 0), 2),
                    "votes": int(r["votes"] or 0),
                    "days_to": r["days_to"],
                    "sent": bool(r["sent"]),
                }
                for r in rows[:5]
            ]
            if not rows:
                out["note"] = "近 24h 无周期反转信号(cycle_reversal_watcher 暂未跑)"
            out["ok"] = True
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


def _digest_events() -> Dict[str, Any]:
    """过去 24h 重要 event_atom(direction_confidence * magnitude_value 高的)摘要。

    兼容 v176+ event_atom 表 schema:
      - 没有 headline 列 → 用 actor + reasoning 拼接
      - 没有 impact_score 列 → 用 direction_confidence * magnitude_value 替代
    """
    out: Dict[str, Any] = {"ok": False, "event_count": 0}
    try:
        cutoff = int(time.time()) - 86400
        conn = sqlite3.connect(db.get_db_path())
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                SELECT id, event_time, actor, direction, direction_confidence,
                       magnitude_value, magnitude_unit, reasoning, asset_id
                  FROM event_atom
                 WHERE event_time >= ?
                 ORDER BY direction_confidence DESC, magnitude_value DESC LIMIT 10
                """,
                (cutoff,),
            ).fetchall()
            out["event_count"] = len(rows)
            out["top5"] = [
                {
                    "id": int(r["id"]),
                    "actor": (r["actor"] or "")[:40] or (r["reasoning"] or "?")[:30],
                    "direction": r["direction"],
                    "score": round(float(r["direction_confidence"] or 0) *
                                   abs(float(r["magnitude_value"] or 0)), 3),
                    "magnitude": f"{r['magnitude_value'] or 0:.2f}{r['magnitude_unit'] or ''}",
                }
                for r in rows[:5]
            ]
            out["ok"] = True
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


def _digest_missed_signals() -> Dict[str, Any]:
    """过去 24h ai_decisions.missed_signal=1 的漏检数。"""
    out: Dict[str, Any] = {"ok": False, "missed_count": 0}
    try:
        cutoff = int(time.time()) - 86400
        conn = sqlite3.connect(db.get_db_path())
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                """
                SELECT COUNT(*) AS c
                  FROM ai_decisions
                 WHERE missed_signal = 1
                   AND created_at >= datetime(?, 'unixepoch')
                """,
                (cutoff,),
            ).fetchone()
            out["missed_count"] = int(row["c"] or 0)
            out["ok"] = True
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


def _digest_kg_review() -> Dict[str, Any]:
    """最近一次 cron_kg_review 的输出行(若存在)。

    v276: 适配实际 schema — finished_at 是 string 而非 epoch ts;数据在 steps.r2/r3 下。
    """
    out: Dict[str, Any] = {"ok": False, "ran_today": False}
    try:
        from datetime import datetime
        today_start_str = datetime.now().strftime("%Y-%m-%d")
        log_path = os.path.join(LOG_DIR, "kg_review_cron.out.log")
        if not os.path.exists(log_path):
            out["note"] = "kg_review 日志不存在"
            return out
        last_line = None
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or not line.startswith("{"):
                    continue
                try:
                    obj = json.loads(line)
                    if obj.get("finished_at", "").startswith(today_start_str):
                        last_line = obj
                except Exception:
                    pass
        if last_line:
            out["ok"] = True
            out["ran_today"] = True
            steps = last_line.get("steps", {}) or {}
            r2 = steps.get("r2_kg_approve", {}) or {}
            r3 = steps.get("r3_attr_review", {}) or {}
            out["approved"] = r2.get("approved_count", 0)
            out["reviewed"] = r3.get("reviewed_count", 0)
            out["skipped"] = r2.get("skipped", 0)
            out["finished_at"] = last_line.get("finished_at", "")
        else:
            # ok=true + ran_today=false 是有效状态(今日 KG 还未到 04:00 调度时间)
            out["ok"] = True
            out["note"] = "今日 KG 审批尚未运行(04:00 调度执行)"
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


def _format_markdown(sections: Dict[str, Any]) -> str:
    """把 6 个子任务结果格式化成 Markdown 摘要。"""
    lines = ["## 📊 发现页 · 昨日规律汇总", ""]
    today = time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))
    lines.append(f"**日期**:{today}(过去 24h)")
    lines.append("")

    fed = sections.get("fed", {})
    if fed.get("ok"):
        delta = fed.get("dgs2_change_bp", 0)
        spread = fed.get("spread_change_bp", 0)
        anomaly_flag = " ⚠️ **异动**" if fed.get("anomaly_count") else ""
        lines.append(f"### 🏛️ Fed 路径{anomaly_flag}")
        lines.append(
            f"- 2Y UST 24h 变化:**{delta:+.1f}bp** "
            f"({fed.get('dgs2_first')}% → {fed.get('dgs2_last')}%)"
        )
        lines.append(f"- 价差 24h 变化:**{spread:+.1f}bp**")
    else:
        lines.append("### 🏛️ Fed 路径")
        lines.append(f"- ⚠️ {fed.get('error') or fed.get('note') or '数据不足'}")

    cross = sections.get("cross_asset", {})
    lines.append("")
    if cross.get("ok"):
        anomaly_count = cross.get("anomaly_count", 0)
        total = cross.get("total_signals_24h", 0)
        lines.append(
            f"### 🌐 跨资产异常 (高严重度 {anomaly_count} 条 / 全 {total} 条)"
        )
        if anomaly_count:
            for r in cross.get("top5", []):
                lines.append(
                    f"- **{r['asset']}** [{r['kind']}/{r['severity_hint']}] "
                    f"{r.get('summary', '')}"
                )
        else:
            lines.append("- 近 24h 无严重度 ≥2 的跨资产异常")
    else:
        lines.append("### 🌐 跨资产异常")
        lines.append(f"- ⚠️ {cross.get('error') or '查询失败'}")

    cyc = sections.get("cycle", {})
    lines.append("")
    if cyc.get("ok"):
        sent_n = cyc.get("sent_count", 0)
        total = cyc.get("signal_count", 0)
        lines.append(
            f"### ☯️ 周期反转信号 ({total} 条检测 / {sent_n} 条已推送)"
        )
        if total:
            for r in cyc.get("top5", []):
                push_mark = "📨" if r.get("sent") else "·"
                lines.append(
                    f"- {push_mark} **{r['asset']}** {r['direction']} "
                    f"prob={r['prob']:.0%} "
                    f"votes={r.get('votes', '?')} "
                    f"days_to={r.get('days_to', '?')}"
                )
        else:
            lines.append("- 近 24h 无周期反转信号(cycle_reversal_watcher 暂未跑)")
    else:
        lines.append("### ☯️ 周期反转信号")
        lines.append(f"- ⚠️ {cyc.get('error') or '查询失败'}")

    ev = sections.get("events", {})
    lines.append("")
    if ev.get("ok"):
        if ev.get("event_count"):
            lines.append(f"### ⚡ 重要事件 ({ev['event_count']} 条 高置信)")
            for r in ev.get("top5", []):
                lines.append(
                    f"- [{r.get('score', 0):.2f} {r.get('direction', 'unknown')}] "
                    f"{r.get('actor', '?')} (mag {r.get('magnitude', '?')})"
                )
        else:
            lines.append("### ⚡ 重要事件")
            lines.append("- 近 24h 无高置信度 event_atom")
    else:
        lines.append("### ⚡ 重要事件")
        lines.append(f"- ⚠️ {ev.get('error') or '查询失败'}")

    missed = sections.get("missed", {})
    lines.append("")
    if missed.get("ok"):
        n = missed.get("missed_count", 0)
        lines.append("### 🪞 漏检信号")
        lines.append(
            f"- AI HOLD 但行情有显著异动:**{n} 条**"
            + (" ⚠️ 多漏检需要回看" if n >= 3 else "")
        )
    else:
        lines.append("### 🪞 漏检信号")
        lines.append(f"- ⚠️ {missed.get('error') or '查询失败'}")

    kg = sections.get("kg_review", {})
    lines.append("")
    if kg.get("ran_today"):
        lines.append("### 🤖 KG 自动审批")
        lines.append(
            f"- approved: **{kg.get('approved', 0)}** · "
            f"reviewed: {kg.get('reviewed', 0)} · "
            f"skipped: {kg.get('skipped', 0)}"
        )
    elif kg.get("ok"):
        lines.append("### 🤖 KG 自动审批")
        lines.append(f"- ⏳ {kg.get('note', '今日尚未运行')}")
    else:
        lines.append("### 🤖 KG 自动审批")
        lines.append(f"- ⚠️ {kg.get('error') or '查询失败'}")

    lines.append("")
    lines.append("---")
    lines.append("💡 完整数据见 /discover → 📡 Fed 全景监控 + 各子段")
    return "\n".join(lines)


def _send(title: str, body: str) -> None:
    """走 send_important_news 三通道推送(失败不抛)。"""
    try:
        from engine.alerts import send_important_news
        send_important_news(title, body)
        return
    except ImportError:
        pass
    try:
        from engine import alerts as _alerts  # type: ignore
        _alerts.send_important_news(title, body)
    except Exception as exc:
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] "
                        f"推送失败: {type(exc).__name__}: {exc}\n")
        except Exception:
            pass


def main() -> int:
    """主入口:聚合 6 个子任务 + 推送。"""
    started = int(time.time())
    sections = {
        "fed": _digest_fed(),
        "cross_asset": _digest_cross_asset(),
        "cycle": _digest_cycle(),
        "events": _digest_events(),
        "missed": _digest_missed_signals(),
        "kg_review": _digest_kg_review(),
    }

    md = _format_markdown(sections)
    has_anomaly = (
        sections.get("fed", {}).get("anomaly_count", 0)
        or sections.get("cross_asset", {}).get("anomaly_count", 0)
        or sections.get("missed", {}).get("missed_count", 0) >= 3
    )
    prefix = "🚨" if has_anomaly else "📊"
    title = f"{prefix} 发现页 · 昨日规律汇总"
    _send(title, md)

    summary = {
        "ok": True,
        "ts": started,
        "duration_s": int(time.time()) - started,
        "sections": {
            k: {
                "ok": v.get("ok", False),
                "count": v.get("anomaly_count", v.get("signal_count",
                       v.get("event_count", v.get("missed_count", 0)))),
            } for k, v in sections.items()
        },
        "title": title,
    }
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(
                f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] "
                + json.dumps(summary, ensure_ascii=False)
                + "\n"
            )
    except Exception:
        pass
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as exc:
        import traceback
        err = {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "trace": traceback.format_exc(),
        }
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] "
                        + json.dumps(err, ensure_ascii=False) + "\n")
        except Exception:
            pass
        print(json.dumps(err, ensure_ascii=False))
        sys.exit(1)



