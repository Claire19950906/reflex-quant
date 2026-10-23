#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v226: Fed 利率预期异动推送后台循环。

调用 fed_rate_expectation.fetch_expectation() → 检查 anomaly.is_anomaly
→ 通过 engine.alerts.send_important_news() 推送到手机 / 邮箱 / 微信。

幂等：UNIQUE (ts // cooldown) → 同一异动冷却期内不重复推。
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from typing import Any, Dict

import config
import db
# fed_rate_expectation.py 实际位于 src_python/ 根目录（与 engine/ 同级），
# 不是 engine/ 子目录。源码其它文件（api_server / fed_watch / discovery）都用
# `import fed_rate_expectation` 绝对导入；这里之前误写 `from . import` 相对导入
# 导致 engine.py 启动 ImportError → com.trident.engine 连续 91 次崩溃重启
# → 没有任何 AI 分析跑 → 首页「今日信号/今日已分析」恒为 0。
# engine.py 启动时把 src_python/ 加进了 sys.path，所以这里用绝对导入即可。
import fed_rate_expectation as _fed_exp
from .alerts import send_important_news

logger = logging.getLogger("fed_rate_notifier")

_COOLDOWN_S = 1800  # 30 分钟内同 severity 不重复推


def _record_and_dedup(anomaly: Dict[str, Any], severity: str) -> bool:
    """返回 True 表示"应推送"（去重后）。"""
    bucket_ts = int(time.time()) // _COOLDOWN_S
    key = f"fed_anomaly::{severity}::{bucket_ts}"
    conn = sqlite3.connect(db.get_db_path())
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS fed_notify_dedup (
              key TEXT PRIMARY KEY,
              ts INTEGER
            )
            """
        )
        exists = conn.execute(
            "SELECT 1 FROM fed_notify_dedup WHERE key = ?", (key,)
        ).fetchone()
        if exists:
            return False
        conn.execute(
            "INSERT INTO fed_notify_dedup(key, ts) VALUES (?, ?)",
            (key, int(time.time())),
        )
        cutoff = int(time.time()) - 86400
        conn.execute("DELETE FROM fed_notify_dedup WHERE ts < ?", (cutoff,))
        conn.commit()
        return True
    finally:
        try:
            conn.close()
        except Exception:
            return True  # 兜底：表失败 → 当成"该推"


def _format_title(anomaly: Dict[str, Any], exp: Dict[str, Any]) -> str:
    direction_emoji = "🔼" if anomaly["yield_move_bp"] > 0 else "🔽"
    severity_emoji = {"high": "🚨", "med": "⚠️", "low": "ℹ️"}.get(anomaly["severity"], "ℹ️")
    return (
        f"{severity_emoji} Fed 利率预期异动 {direction_emoji} "
        f"{exp['direction']} {abs(exp['expected_24m_change_bp']):.0f}bp"
    )


def _format_body(anomaly: Dict[str, Any], out: Dict[str, Any]) -> str:
    exp = out["expectation"]
    cur = out["current"]
    lines = [
        anomaly["message"],
        "",
        "── 第一性原理推导 ──",
        f"2Y UST = {cur['dgs2_pct']:.3f}%, EFFR = {cur['effr_pct']:.3f}%",
        f"Spread = {exp['spread_bp']:+.1f}bp",
        f"推导预期 24 月累计变动 = {exp['expected_24m_change_bp']:+.0f}bp",
        f"方向：{exp['direction']}",
        exp["narrative"],
        "",
        "── 推导链 ──",
    ]
    lines.extend(exp["first_principles_chain"])
    lines.extend([
        "",
        f"── 数据源：{out['data_source']} ──",
        f"观测时间：{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(out['ts']))}",
    ])
    return "\n".join(lines)


async def fed_rate_notifier_loop() -> None:
    """主循环：每 config.FED_RATE_NOTIFY_POLL_SECONDS 秒检查一次。"""
    if not config.FED_RATE_NOTIFY_ENABLED:
        logger.info("[FED-NOTIFY] disabled by config")
        return

    poll = max(60, int(config.FED_RATE_NOTIFY_POLL_SECONDS))
    logger.info(
        "[FED-NOTIFY] started: yield_move_th=%.0fbp expect_move_th=%.0fbp poll=%ds",
        config.FED_RATE_YIELD_MOVE_BP,
        config.FED_RATE_EXPECT_MOVE_BP,
        poll,
    )

    while True:
        try:
            out = await asyncio.to_thread(_fed_exp.fetch_expectation)
            anomaly = out.get("anomaly") or {}
            if anomaly.get("is_anomaly"):
                severity = anomaly.get("severity", "med")
                if _record_and_dedup(anomaly, severity):
                    title = _format_title(anomaly, out["expectation"])
                    body = _format_body(anomaly, out)
                    try:
                        await asyncio.to_thread(send_important_news, title, body)
                        logger.info("[FED-NOTIFY] pushed: %s", title)
                    except Exception as exc:
                        logger.warning("[FED-NOTIFY] push failed: %s", exc)
                else:
                    logger.debug("[FED-NOTIFY] deduped, skipping")
            else:
                logger.debug("[FED-NOTIFY] no anomaly")
        except Exception as exc:
            logger.warning("[FED-NOTIFY] loop error: %s", exc)
        await asyncio.sleep(poll)


if __name__ == "__main__":
    out = _fed_exp.fetch_expectation()
    print(_format_title(out["anomaly"], out["expectation"]))
    print(_format_body(out["anomaly"], out))
