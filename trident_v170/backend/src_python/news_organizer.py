#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""新闻数据整理 — 按平台分文档导出（供去各新闻平台对比时效）。

背景：不同平台推送的新闻，入库时只记了"我们采集的时刻"（raw_news.timestamp/ts），
平台声称的发布时间在 2026-09 前被丢弃。本模块从 raw_news 读取，
把"平台发布时间(published_at，存量无则标注) + 我们采集时间(timestamp)"按平台
分文件导出为 CSV（Excel 可直接打开），每批生成一个带时间戳的文件夹：

    backend/news_exports/
        batch_20260903_173000/
            总览.txt                    ← 本批各平台条数 / 时间跨度 / 说明
            TechFlow深潮.csv
            东方财富.csv
            金十.csv
            美股新闻.csv
            ...（按实际入库 source 动态分文件）

每条记录含：
    - published_at   平台声称的发布时间（存量新闻未存 → 空，标注"存量未记录"）
    - ingest_at      我们采集入库时刻（raw_news.timestamp，北京时区 ISO）
    - delay_seconds  平台发布时间 → 我们入库的滞后秒数（正=平台先发我们后收）
                     无 published_at 时为空（无法计算）
    - content        新闻标题+正文（去 [hash:] 前缀）

用法：
    from news_organizer import export_news_batch, list_batches
    export_news_batch()          # 立即生成一批
    list_batches()               # 列出已有批次
"""

from __future__ import annotations

import csv
import json
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import config
import db

# 平台源名 → 文件名友好名（Windows 非法字符替换）
_SOURCE_FILE_SAFE = {
    "TechFlow 深潮": "TechFlow深潮",
    "东方财富": "东方财富",
    "律动 BlockBeats": "律动BlockBeats",
    "yahoo_stock": "美股新闻",
    "finnhub": "美股新闻",
    "jin10": "金十快讯",
    "jin10_calendar": "金十日历",
    "fred": "FRED宏观",
    "financialjuice": "FinancialJuice",
    "feishu_jin10": "飞书转金十数据",
    "reuters": "Reuters路透",
    "bls": "BLS劳工统计",
}

# ── 动态参数（可页面改、立即生效）：存 paper_trading_settings.news_export_settings JSON ──
_SETTING_KEYS = ("interval_minutes", "window_hours", "keep_batches")
_SETTING_DEFAULTS = {
    "interval_minutes": 30,   # 自动整理检查节奏（分钟）
    "window_hours": 24,       # 每批回溯最近 N 小时新闻
    "keep_batches": 30,       # 最多保留最近 N 批（旧的自动清理）
}


def get_export_settings(connection=None) -> Dict[str, Any]:
    """读新闻整理参数（DB 优先，缺省回退 config/.env 默认）。"""
    own = connection is None
    conn = connection or db.get_connection()
    try:
        stored: Dict[str, Any] = {}
        try:
            row = conn.execute(
                "SELECT news_export_settings FROM paper_trading_settings WHERE id=1"
            ).fetchone()
            if row and row[0]:
                parsed = json.loads(row[0])
                if isinstance(parsed, dict):
                    stored = parsed
        except (sqlite3.OperationalError, TypeError, json.JSONDecodeError):
            stored = {}
        result = {}
        for key in _SETTING_KEYS:
            cfg_default = getattr(config, "NEWS_EXPORT_" + key.upper(), _SETTING_DEFAULTS[key])
            result[key] = max(1, int(stored.get(key) or cfg_default))
        result["interval_minutes"] = max(5, result["interval_minutes"])
        return result
    finally:
        if own:
            conn.close()


def set_export_settings(*, interval_minutes=None, window_hours=None,
                        keep_batches=None, connection=None) -> Dict[str, Any]:
    """保存新闻整理参数（页面输入框保存后立即生效，无需重启）。"""
    own = connection is None
    conn = connection or db.get_connection()
    try:
        current = get_export_settings(conn)
        merged = {
            "interval_minutes": current["interval_minutes"] if interval_minutes is None else max(5, int(interval_minutes)),
            "window_hours": current["window_hours"] if window_hours is None else max(1, int(window_hours)),
            "keep_batches": current["keep_batches"] if keep_batches is None else max(3, int(keep_batches)),
        }
        conn.execute(
            """UPDATE paper_trading_settings
               SET news_export_settings=?
               WHERE id=1""",
            (json.dumps(merged, ensure_ascii=False),),
        )
        if own:
            conn.commit()
        return get_export_settings(conn)
    finally:
        if own:
            conn.close()


def _file_safe(source: str) -> str:
    s = _SOURCE_FILE_SAFE.get(source, source)
    return re.sub(r'[\\/:*?"<>|\s]+', "_", s)[:60] or "未知平台"


def _strip_hash(content: str) -> str:
    """去掉入库前缀 [hash:xxxxxxxx] → 还原纯新闻文本。"""
    return re.sub(r"^\[hash:[a-fA-F0-9]+\]\s*", "", content or "").strip()


def _parse_dt(value: str) -> Optional[datetime]:
    """宽松解析入库/发布时间串 → 北京时间 datetime（与全项目统一时区口径）。"""
    return config.parse_time_to_bj(value)


def _fmt_local(value: Any) -> str:
    """入库时间串 → 'YYYY-MM-DD HH:MM:SS'（北京时间）；解析失败原样截断。"""
    return config.fmt_bj(value) or str(value or "")[:19]


def export_news_batch(
    window_hours: Optional[float] = None,
    export_dir: Optional[str] = None,
    keep_batches: Optional[int] = None,
) -> Dict[str, Any]:
    """生成一批"按平台分文件"的新闻导出文档，返回批次信息。

    - 每批新建 news_exports/batch_YYYYmmdd_HHMMSS/ 文件夹
    - 按 raw_news.source 分组，每平台一个 CSV（utf-8-sig，Excel 直接打开）
    - 文件名带平台名（_SOURCE_FILE_SAFE 映射 + 非法字符清理）
    - 同时清理超过 keep_batches 的最老批次
    """
    window_hours = window_hours if window_hours is not None else float(get_export_settings()["window_hours"])
    base_dir = export_dir or config.NEWS_EXPORT_DIR
    os.makedirs(base_dir, exist_ok=True)
    # 批次目录唯一名：同秒内多次导出自动加 _2/_3 后缀（手动连点/测试都安全）
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    batch_name = f"batch_{stamp}"
    suffix = 2
    while os.path.exists(os.path.join(base_dir, batch_name)):
        batch_name = f"batch_{stamp}_{suffix}"
        suffix += 1
    batch_dir = os.path.join(base_dir, batch_name)

    since = datetime.now(config.TZ_SHANGHAI) - timedelta(hours=window_hours)
    since_epoch = int(since.timestamp())
    conn = db.get_connection()
    try:
        # 容忍旧库：published_at 列缺失时（尚未 migrate）按空处理，不影响导出
        cols = {row[1] for row in conn.execute("PRAGMA table_info(raw_news)")}
        has_pub = "published_at" in cols
        sel_pub = "published_at" if has_pub else "'' AS published_at"
        rows = conn.execute(
            f"""SELECT id, source, content, timestamp, {sel_pub}
                FROM raw_news
                WHERE ts >= ? OR (ts IS NULL AND timestamp >= ?)
                ORDER BY timestamp""",
            (since_epoch, since.isoformat(timespec="seconds")),
        ).fetchall()
    finally:
        conn.close()

    # 按平台分组（位置索引：0=id 1=source 2=content 3=timestamp 4=published_at）
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        source = str(row[1] or "未知")
        item = {
            "id": row[0],
            "source": source,
            "content": _strip_hash(row[2]),
            "ingest_at": _fmt_local(row[3]),     # 我们采集入库时刻（统一 YYYY-MM-DD HH:MM:SS）
            "published_at": row[4] or "",        # 平台发布时间（存量空）
        }
        # 滞后秒数（平台先发 → 我们后收为正）
        pub_dt = _parse_dt(item["published_at"])
        ing_dt = _parse_dt(item["ingest_at"])
        if pub_dt is not None and ing_dt is not None:
            item["delay_seconds"] = int((ing_dt - pub_dt).total_seconds())
        else:
            item["delay_seconds"] = None
        groups.setdefault(source, []).append(item)

    os.makedirs(batch_dir, exist_ok=True)
    summary_lines: List[str] = []
    files: List[Dict[str, Any]] = []
    for source in sorted(groups):
        items = groups[source]
        fname = f"{_file_safe(source)}.csv"
        path = os.path.join(batch_dir, fname)
        _write_platform_csv(path, source, items)
        files.append({"file": fname, "source": source, "count": len(items)})
        times = sorted(it["ingest_at"] for it in items)
        summary_lines.append(
            f"{source}\t{len(items)} 条\t{times[0][:19]} ~ {times[-1][:19]}"
            f"\t滞后中位 {_median_delay_sec(items)}s"
        )

    # 总览说明
    overview = os.path.join(batch_dir, "总览.txt")
    with open(overview, "w", encoding="utf-8") as f:
        f.write(f"新闻数据整理 — {datetime.now():%Y-%m-%d %H:%M:%S}\n")
        f.write(f"回溯窗口: 最近 {window_hours:.0f} 小时  新闻总数: {len(rows)}\n")
        f.write("=" * 60 + "\n")
        f.write("字段说明:\n")
        f.write("  published_at = 平台声称的发布时间（2026-09 前入库的存量新闻当时未存该字段 → 空白）\n")
        f.write("  ingest_at     = 我们采集入库的时刻（系统自己记录的，任何新闻都有）\n")
        f.write("  delay_seconds = 平台发布 → 我们入库的滞后秒数（正=平台先发我们后收）\n")
        f.write("拿 CSV 去对应新闻平台搜标题，比对平台公布时间与 ingest_at 即可判断平台是否滞后。\n")
        f.write("=" * 60 + "\n\n")
        f.write("各平台汇总:\n")
        f.write("\n".join(summary_lines) if summary_lines else "(窗口内无新闻)")

    # 清理老批次（保留最近 keep 个）
    keep = keep_batches if keep_batches is not None else int(get_export_settings()["keep_batches"])
    cleaned = _prune_old_batches(base_dir, keep)

    return {
        "batch": os.path.basename(batch_dir),
        "dir": batch_dir,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "window_hours": window_hours,
        "total_news": len(rows),
        "files": files,
        "pruned": cleaned,
    }


def _write_platform_csv(path: str, source: str, items: List[Dict[str, Any]]) -> None:
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "序号", "平台", "平台发布时间(published_at)", "我们采集入库时刻(ingest_at)",
            "滞后秒数", "新闻标题/内容",
        ])
        for idx, it in enumerate(items, 1):
            delay = "" if it["delay_seconds"] is None else it["delay_seconds"]
            writer.writerow([
                idx,
                source,
                it["published_at"] or "（存量未记录）",
                it["ingest_at"][:19] if it["ingest_at"] else "",
                delay,
                it["content"],
            ])


def _median_delay_sec(items: List[Dict[str, Any]]) -> str:
    vals = sorted(it["delay_seconds"] for it in items if it["delay_seconds"] is not None)
    if not vals:
        return "—"
    mid = len(vals) // 2
    med = vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2
    return f"{med:.0f}"


def _prune_old_batches(base_dir: str, keep: int) -> int:
    """保留最近 keep 个 batch_* 文件夹，删除更老的；返回删除数。"""
    try:
        dirs = sorted(
            (d for d in os.listdir(base_dir) if d.startswith("batch_")),
            key=lambda d: d,
        )
    except OSError:
        return 0
    removed = 0
    for d in dirs[:-keep] if keep > 0 else dirs:
        try:
            import shutil
            shutil.rmtree(os.path.join(base_dir, d))
            removed += 1
        except OSError:
            pass
    return removed


def list_batches(export_dir: Optional[str] = None) -> List[Dict[str, Any]]:
    """列出已有批次（含平台文件清单与各文件新闻数），新的在前。"""
    base_dir = export_dir or config.NEWS_EXPORT_DIR
    batches: List[Dict[str, Any]] = []
    try:
        dirs = [d for d in os.listdir(base_dir) if d.startswith("batch_")]
    except OSError:
        return batches
    for d in sorted(dirs, reverse=True):
        bdir = os.path.join(base_dir, d)
        files = []
        total = 0
        overview_text = ""
        try:
            names = sorted(os.listdir(bdir))
        except OSError:
            continue
        for name in names:
            if name == "总览.txt":
                try:
                    with open(os.path.join(bdir, name), encoding="utf-8") as f:
                        overview_text = f.read()[:2000]
                except OSError:
                    pass
                continue
            if name.endswith(".csv"):
                count = _count_csv_rows(os.path.join(bdir, name))
                total += count
                files.append({"file": name, "count": count})
        batches.append({
            "batch": d,
            "dir": bdir,
            "files": files,
            "total_news": total,
            "overview": overview_text,
        })
    return batches


def _count_csv_rows(path: str) -> int:
    try:
        with open(path, encoding="utf-8-sig") as f:
            return max(0, sum(1 for _ in f) - 1)
    except OSError:
        return 0


# ── 采集时效统计（页面可视化：每源轮询间隔 / 平均滞后 / 样本数）─────────
# 轮询间隔（秒）来自 config 各源配置；实时推送类源（WS/webhook）为 0 = 到达即发布
# key 用规范小写平台键（与 bucket() 归一一致）
_POLL_SECONDS_BY_SOURCE = {
    "techflow": "NEWS_SOURCE_POLL_SECONDS",
    "eastmoney": "NEWS_SOURCE_POLL_SECONDS",
    "blockbeats": "NEWS_SOURCE_POLL_SECONDS",
    "jin10": "JIN10_MCP_POLL_SECONDS",
    "jin10_calendar": "JIN10_CALENDAR_POLL_SECONDS",
    "yahoo_stock": "STOCK_NEWS_POLL_SECONDS",
    "reuters": "REUTERS_NEWS_POLL_SECONDS",
    "bls": "BLS_POLL_SECONDS",
    "fred": "FRED_POLL_SECONDS",
}

_SOURCE_DISPLAY = {
    "techflow": "TechFlow 深潮",
    "eastmoney": "东方财富",
    "blockbeats": "律动 BlockBeats",
    "yahoo_stock": "美股新闻 (Finnhub/Yahoo)",
    "reuters": "Reuters 路透",
    "bls": "BLS 劳工统计局",
    "jin10": "金十快讯",
    "jin10_calendar": "金十日历",
    "fred": "FRED 宏观",
    "financialjuice": "FinancialJuice / TreeNews (WS 实时推)",
    "feishu_jin10": "飞书转金十数据 (实时推)",
}


def _poll_seconds(source: str) -> int:
    """该源当前配置的轮询间隔（秒）。WS 实时推送源 → 0（平台主动推，无需轮询）。"""
    s = (source or "").lower()
    # 中英文显示名 → 规范键
    if "深潮" in s or "techflow" in s:
        s = "techflow"
    elif "东财" in s or "eastmoney" in s:
        s = "eastmoney"
    elif "律动" in s or "blockbeats" in s:
        s = "blockbeats"
    elif "金十" in s and "calendar" in s:
        s = "jin10_calendar"
    elif "金十" in s or "jin10" in s:
        s = "jin10"
    elif "fred" in s or "stlouis" in s:
        s = "fred"
    key = _POLL_SECONDS_BY_SOURCE.get(s)
    if key:
        try:
            return max(0, int(getattr(config, key, 0)))
        except Exception:
            return 0
    return 0


def source_latency_stats(hours: float = 24) -> List[Dict[str, Any]]:
    """统计各源采集时效（对比平台发布时间 vs 我们入库时刻）。

    返回按源聚合（含 WS 变体归一）：
      [{source, poll_seconds, samples, median_delay_sec, p90_delay_sec,
        stale_count(>3h), fresh_count(<10min), latest_ingest_at}]
    仅统计带 published_at 的样本；无时间样本单独计数。
    """
    since = datetime.now(config.TZ_SHANGHAI) - timedelta(hours=hours)
    since_epoch = int(since.timestamp())
    conn = db.get_connection()
    try:
        rows = conn.execute(
            "SELECT source, published_at, timestamp, ts FROM raw_news"
            " WHERE ts >= ? OR (ts IS NULL AND timestamp >= ?)",
            (since_epoch, since.isoformat(timespec="seconds")),
        ).fetchall()
    finally:
        conn.close()

    agg: Dict[str, Dict[str, Any]] = {}

    def bucket(source: str) -> Dict[str, Any]:
        """源名 → 规范分组键：WS/tree→financialjuice；yahoo_stock/jin10/fred 原样；
        深潮/东财/律动中文名 → techflow/eastmoney/blockbeats；兼容英文源名。"""
        s = (source or "").lower()
        if s.startswith("ws:") or "financialjuice" in s or "tree" in s or "telegram" in s:
            key = "financialjuice"
        elif "feishu" in s or "飞书" in (source or ""):
            key = "feishu_jin10"
        elif "yahoo" in s or "finnhub" in s or "美股" in (source or ""):
            key = "yahoo_stock"
        elif "深潮" in s or "techflow" in s:
            key = "techflow"
        elif "东财" in s or "eastmoney" in s or "东方财富" in (source or ""):
            key = "eastmoney"
        elif "律动" in s or "blockbeats" in s:
            key = "blockbeats"
        elif "金十" in s or "jin10" in s:
            key = "jin10_calendar" if "calendar" in s else "jin10"
        elif "fred" in s or "stlouis" in s:
            key = "fred"
        else:
            key = (source or "未知").strip()
        return agg.setdefault(key, {
            "source": key,
            "poll_seconds": _poll_seconds(key),
            "delays": [],
            "no_time_samples": 0,
            "latest_ingest": "",
        })

    for row in rows:
        b = bucket(row[0])
        ts_str = str(row[3] or "")
        ingest_iso = str(row[2] or "")
        if ts_str and ts_str.isdigit():
            try:
                dt = datetime.fromtimestamp(float(ts_str), tz=config.TZ_SHANGHAI)
                ingest_iso = dt.isoformat(timespec="seconds")
            except Exception:
                pass
        if ingest_iso and ingest_iso > b["latest_ingest"]:
            b["latest_ingest"] = ingest_iso[:19]
        pub_raw = str(row[1] or "").strip()
        if not pub_raw:
            b["no_time_samples"] += 1
            continue
        pub_dt = config.parse_time_to_bj(pub_raw)
        ing_dt = config.parse_time_to_bj(ingest_iso)
        if pub_dt is not None and ing_dt is not None:
            delay = (ing_dt - pub_dt).total_seconds()
            if delay >= -3600:  # 容忍 ±1h 时钟偏差，更大的负值视为异常样本剔除
                b["delays"].append(delay)

    out: List[Dict[str, Any]] = []
    for key, b in agg.items():
        delays = sorted(b["delays"])
        n = len(delays)
        median = delays[n // 2] if n else None
        p90 = delays[min(n - 1, int(n * 0.9))] if n else None
        stale = sum(1 for d in delays if d > 3 * 3600)
        fresh = sum(1 for d in delays if d < 10 * 60)
        out.append({
            "source": b["source"],
            "display": _SOURCE_DISPLAY.get(b["source"], b["source"]),
            "poll_seconds": b["poll_seconds"],
            "samples": n,
            "no_time_samples": b["no_time_samples"],
            "median_delay_sec": int(median) if median is not None else None,
            "p90_delay_sec": int(p90) if p90 is not None else None,
            "stale_count": stale,          # 滞后 > 3h 的样本数（平台严重滞后/补发）
            "fresh_count": fresh,          # < 10min 的样本数（即时推送）
            "latest_ingest_at": b["latest_ingest"],
        })
    out.sort(key=lambda x: -x["samples"])
    return out


if __name__ == "__main__":
    print(export_news_batch())
