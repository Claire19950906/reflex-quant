#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v226: 跨资产异常同步推送后台循环。

调用 cross_asset_context.detect_anomaly() → 检查 is_anomaly
→ 通过 engine.alerts.send_important_news() 推送到手机 / 邮箱 / 微信。

幂等：UNIQUE (severity // cooldown) → 同一异动冷却期内不重复推。
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from typing import Any, Dict

import config
import db
# cross_asset_context.py 实际位于 src_python/ 根目录（与 engine/ 同级），
# api_server 等其它模块用 `import cross_asset_context` 绝对导入；
# 这里之前误写 `from . import` 相对导入，导致 engine.py 启动 ImportError。
# 改成绝对导入，与 fed_rate_notifier / api_server 风格一致。
from .alerts import send_important_news
import cross_asset_context as _cac

logger = logging.getLogger("cross_asset_anomaly_notifier")

_COOLDOWN_S = 3600  # 1 小时内同 severity 不重复推（异常信号刷新慢）


def _record_and_dedup(severity: str, direction: str) -> bool:
    bucket_ts = int(time.time()) // _COOLDOWN_S
    key = f"cross_anomaly::{severity}::{direction}::{bucket_ts}"
    conn = sqlite3.connect(db.get_db_path())
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cross_anomaly_dedup (
              key TEXT PRIMARY KEY,
              ts INTEGER
            )
            """
        )
        exists = conn.execute(
            "SELECT 1 FROM cross_anomaly_dedup WHERE key = ?", (key,)
        ).fetchone()
        if exists:
            return False
        conn.execute(
            "INSERT INTO cross_anomaly_dedup(key, ts) VALUES (?, ?)",
            (key, int(time.time())),
        )
        cutoff = int(time.time()) - 86400
        conn.execute("DELETE FROM cross_anomaly_dedup WHERE ts < ?", (cutoff,))
        conn.commit()
        return True
    finally:
        try:
            conn.close()
        except Exception:
            return True


def _format_title(anomaly: Dict[str, Any]) -> str:
    severity_emoji = {"high": "🚨", "med": "⚠️", "low": "ℹ️"}.get(anomaly["severity"], "ℹ️")
    direction_emoji = "📈" if anomaly["direction"] == "up" else "📉"
    return (
        f"{severity_emoji} 跨资产同步异常 {direction_emoji} "
        f"{anomaly['classes_aligned']} 行业同向"
    )


def _format_body(anomaly: Dict[str, Any]) -> str:
    lines = [
        anomaly["message"],
        "",
        f"── 异常概要 ──",
        f"同向行业：{', '.join(anomaly['classes_aligned_names'])}",
        f"代表品种：{', '.join(anomaly['symbols_aligned'][:8])}",
        f"形态一致：{'是（系统性传导信号）' if anomaly['shape_consistent'] else '否（行业差异较大）'}",
        f"宏观 regime：{anomaly['macro_regime']}",
        "",
        "── 可能根因（按概率从高到低） ──",
    ]
    direction = anomaly["direction"]
    if direction == "down":
        lines.extend([
            "  · Fed 鹰派 pivot / 流动性紧缩 → 跨资产同时去杠杆",
            "  · 地缘风险升级 → 风险偏好收缩（股、币、商品齐跌）",
            "  · 重大信用事件 / 金融机构暴雷 → 系统性抛售",
        ])
    elif direction == "up":
        lines.extend([
            "  · Fed 鸽派 pivot / 流动性宽松 → 跨资产同步 risk-on",
            "  · 重大风险事件解除（贸易战缓和 / 战争结束）",
            "  · 美元走弱 → EM/大宗商品/风险资产齐涨",
        ])
    lines.extend([
        "",
        f"── 触发阈值 ──",
        f"≥ {config.CROSS_ASSET_ANOMALY_MIN_CLASSES} 个不同行业｜幅度 ≥ {config.CROSS_ASSET_ANOMALY_MIN_PCT}%",
        f"观测时间：{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())}",
    ])
    return "\n".join(lines)


async def cross_asset_anomaly_loop() -> None:
    """主循环：每 config.CROSS_ASSET_ANOMALY_POLL_SECONDS 秒检查一次。"""
    if not config.CROSS_ASSET_ANOMALY_ENABLED:
        logger.info("[CROSS-ANOMALY] disabled by config")
        return

    poll = max(60, int(config.CROSS_ASSET_ANOMALY_POLL_SECONDS))
    min_classes = int(config.CROSS_ASSET_ANOMALY_MIN_CLASSES)
    min_pct = float(config.CROSS_ASSET_ANOMALY_MIN_PCT)
    logger.info(
        "[CROSS-ANOMALY] started: min_classes=%d min_pct=%.2f%% poll=%ds",
        min_classes, min_pct, poll,
    )

    while True:
        try:
            def _run():
                snap = _cac.cross_asset_snapshot(asset="*", ts_iso=None)
                return _cac.detect_anomaly(snap, min_classes=min_classes, min_pct=min_pct)
            anomaly = await asyncio.to_thread(_run)
            if anomaly.get("is_anomaly"):
                if _record_and_dedup(anomaly["severity"], anomaly["direction"]):
                    title = _format_title(anomaly)
                    body = _format_body(anomaly)
                    try:
                        await asyncio.to_thread(send_important_news, title, body)
                        logger.info("[CROSS-ANOMALY] pushed: %s", title)
                    except Exception as exc:
                        logger.warning("[CROSS-ANOMALY] push failed: %s", exc)
                else:
                    logger.debug("[CROSS-ANOMALY] deduped")
            else:
                logger.debug("[CROSS-ANOMALY] no anomaly")
        except Exception as exc:
            logger.warning("[CROSS-ANOMALY] loop error: %s", exc)
        await asyncio.sleep(poll)


if __name__ == "__main__":
    snap = _cac.cross_asset_snapshot(asset="*", ts_iso=None)
    a = _cac.detect_anomaly(snap)
    print(_format_title(a))
    print(_format_body(a))
