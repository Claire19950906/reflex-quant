# -*- coding: utf-8 -*-
"""crowd 域路由（结构切片第 2 刀，2026-11-16）。

从 api_server.py 整体搬来 9 条 crowd-pulse 端点：v316 月榜/人群画像/源权威度/原始条目/
话题观点/主体层/采集触发，v317 注意力领先性 + 先行漏检。逻辑逐字未改，只把 `@app.`
换成 `@router.`，注册顺序与搬走前一致。

切片规矩（同 routers/meta.py）：
    1. 端点整体搬走，**不重写逻辑**（连注释一起搬，行为零变化）；
    2. 只把 `@app.` 换成 `@router.`，其余字面不动；
    3. 需要的依赖显式 import，**不要**去 import api_server（否则又变成循环依赖）；
    4. api_server 里改成 include_router，并在原位留一行"已迁移"注释。
"""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter

import config
import db

router = APIRouter()


# ─────────────────────────────────────────────────────────────────────────────
# v316 Crowd Pulse —— 二级市场人群画像 / 权威度 / 月榜
#
# 数据全部来自真实抓取（crowd_sources.py：百度热搜·实时/财经榜、同花顺热门股榜、
# CoinGecko 趋势、Google News RSS），每条原始条目都带 url 可回溯到原页面。
# 这里只做只读输出；采集由 crowd_worker.py 定时触发。
# ─────────────────────────────────────────────────────────────────────────────
@router.get("/api/crowd/ranking", tags=["crowd-pulse"])
def crowd_ranking(month: str = "", limit: int = 50, kind: str = "",
                        sort: str = "authority"):
    """月度榜单。month 省略 = 最新有数据的月份。

    sort: authority(默认，跨平台交叉验证优先) / heat(各源最热) / momentum(正在升温)

    每条带 explain 字段（人可读的权威度拆解：热度/跨平台共现/持续天数/动能/媒体权重），
    保证任何一条排名都能被追问「凭什么」。
    """
    import crowd_pulse
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    try:
        rows = crowd_pulse.monthly_ranking(
            conn, month=month, limit=min(max(1, limit), 500),
            topic_kind=kind, sort=sort)
        return {"month": month or (rows[0]["month"] if rows else ""),
                "sort": sort, "count": len(rows), "ranking": rows}
    finally:
        conn.close()


@router.get("/api/crowd/audience", tags=["crowd-pulse"])
def crowd_audience(month: str = "", dimension: str = ""):
    """人群画像分群（9 个维度：platform/market/asset_class/theme/sentiment/
    time_window/influence_tier/media_tier/media_channel）。

    每个分群给出 item_count（真实条目数）、heat_total、share（热度占比）、
    sample_sources（该分群由哪些源贡献）—— 全部来自真实采集计数。
    """
    import crowd_pulse
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    try:
        return crowd_pulse.audience_profile(conn, month=month, dimension=dimension)
    finally:
        conn.close()


@router.get("/api/crowd/sources", tags=["crowd-pulse"])
def crowd_source_authority(month: str = ""):
    """各数据源/媒体的权威度（含跨源复现次数）。"""
    import crowd_pulse
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    try:
        rows = crowd_pulse.source_authority(conn, month=month)
        return {"month": month or (rows[0]["month"] if rows else ""),
                "count": len(rows), "sources": rows}
    finally:
        conn.close()


@router.get("/api/crowd/items", tags=["crowd-pulse"])
def crowd_items(limit: int = 100, source: str = ""):
    """最近采集到的原始条目（可逐条回溯 url）。"""
    import crowd_pulse
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    try:
        rows = crowd_pulse.latest_items(conn, limit=min(max(1, limit), 500),
                                        source=source)
        return {"count": len(rows), "items": rows}
    finally:
        conn.close()


@router.get("/api/crowd/topics", tags=["crowd-pulse"])
def crowd_topics(month: str = "", days: int = 0, limit: int = 40,
                       sort: str = "heat", item_limit: int = 5, kind: str = "",
                       layer: str = "topic", min_mentions: int = 3):
    """话题 × 观点 —— 「现在市场上的人在关注什么、持什么观点」。

    与 /crowd/ranking 的分工：
      · ranking = 指标表（谁的热度/权威度最高）
      · topics  = 人话（话题是什么 + 多空观点 + 具体说了什么 + 原话 url）

    days > 0 → 最近 N 天（贴近"现在"）；days = 0 → 整月（与月榜同口径）。
    sort: heat(默认) / authority / momentum / latest / bullish
    kind: ""(全部) / topic(题材+新闻，即"在讨论什么") / asset(个股/币种，即"在买什么")
    layer: topic(默认，题材大类) / subject(具体主体，见 /api/crowd/subjects)
    """
    import crowd_pulse
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    try:
        if str(layer or "topic").lower() == "subject":
            import crowd_subjects
            return crowd_subjects.subject_overview_cached(
                conn, month=month, days=max(0, min(int(days or 0), 90)),
                limit=min(max(1, limit), 200), sort=sort,
                item_limit=min(max(0, int(item_limit or 0)), 12), kind=kind,
                min_mentions=max(1, min(int(min_mentions or 3), 100)))
        return crowd_pulse.topic_overview_cached(
            conn, month=month, days=max(0, min(int(days or 0), 90)),
            limit=min(max(1, limit), 200), sort=sort,
            item_limit=min(max(0, int(item_limit or 0)), 12), kind=kind)
    finally:
        conn.close()


@router.get("/api/crowd/subjects", tags=["crowd-pulse"])
def crowd_subjects_view(month: str = "", days: int = 0, limit: int = 40,
                               sort: str = "heat", item_limit: int = 5,
                               kind: str = "", min_mentions: int = 3):
    """具体主体 × 观点 —— 「市场上的人具体在聊什么」。

    与 /api/crowd/topics 的分工：
      · topics   = 题材大类（科技/AI、金融、消费…）—— 分类清楚但看不出聊什么
      · subjects = 具体主体（英特尔 / 黄金 / 美国中期选举 / 人形机器人）

    主体来源（按可靠性）：
      1. 核心词典（公司/商品/宏观/事件/指数，中英别名归一）
      2. 结构化信号（同花顺概念与个股名、gnews 话题标签、百度财经公司名前缀）
      3. 窗口内跨源挖掘的高频短语（长尾新话题）

    sort: heat(默认) / authority / mentions / latest / bullish
    min_mentions: 主体至少被提到几次才进列表（权威主体豁免该门槛）
    """
    import crowd_subjects
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    try:
        return crowd_subjects.subject_overview_cached(
            conn, month=month, days=max(0, min(int(days or 0), 90)),
            limit=min(max(1, limit), 200), sort=sort,
            item_limit=min(max(0, int(item_limit or 0)), 12), kind=kind,
            min_mentions=max(1, min(int(min_mentions or 3), 100)))
    finally:
        conn.close()


@router.post("/api/crowd/collect", tags=["crowd-pulse"])
def crowd_collect():
    """手动触发一次真实采集（立即建榜 + 建画像）。"""
    import config
    import crowd_pulse
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    try:
        return crowd_pulse.run_once(conn, list(config.CROWD_PULSE_SOURCES))
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# v317 Crowd Attention Lead —— 注意力是否先于价格移动（可证伪）
#
# 这个接口回答的是「注意力有没有增量预测力」，而不是「注意力之后价格涨没涨」。
# 两者极易混淆：
#   · 表面命中率（hit_rate）会因为「媒体追价格」而虚高；
#   · 所以每个事件都同时算**反向对照**（注意力之前价格是否已动），
#     并给出 clean subset（剔除价格已先动的样本）后的命中率；
#   · summary.verdict 是最终判定，不接受"看起来很好"的表面数字。
# ─────────────────────────────────────────────────────────────────────────────
@router.get("/api/crowd/attention-lead", tags=["crowd-pulse"])
def crowd_attention_lead(lookback_hours: int = 0, min_platforms: int = 0,
                               move_pct: float = 0.0, horizon_minutes: int = 0,
                               timeframe: str = "", limit: int = 50):
    """注意力 → 价格移动 的领先性检验（带反向对照的证伪装置）。

    返回：
      params   实际使用的阈值
      summary  命中率 / 中位领先分钟 / 反向对照 / 干净子集 / verdict 判定
      events   逐事件明细（含 explain 人话解释、原始条目 url 可回溯）
      note     结论说明（含样本不足时的诚实提示）

    样本不足时 events 为空、命中率为 null —— 不用占位数据冒充结论。
    """
    import crowd_attention_lead as cal
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    try:
        res = cal.evaluate_lead(
            conn,
            lookback_hours=lookback_hours or None,
            min_platforms=min_platforms or None,
            move_pct=move_pct or None,
            horizon_minutes=horizon_minutes or None,
            timeframe=timeframe or None,
        )
        res["events"] = res["events"][:max(1, min(int(limit or 50), 200))]
        return res
    finally:
        conn.close()


@router.get("/api/crowd/pending-attention", tags=["crowd-pulse"])
def crowd_pending_attention(lookback_hours: int = 0, min_platforms: int = 0,
                                  move_pct: float = 0.0,
                                  horizon_minutes: int = 0, timeframe: str = ""):
    """先行漏检候选：注意力已经起来，但价格还没动、系统也还没出手。

    与传统漏检检测的区别：传统检测要等价格异动**之后**才回看；
    这里在异动**之前**就能给出候选，附带系统最近决策（HOLD/BUY/SELL）。
    """
    import crowd_attention_lead as cal
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    try:
        res = cal.pending_attention(
            conn,
            lookback_hours=lookback_hours or None,
            min_platforms=min_platforms or None,
            move_pct=move_pct or None,
            horizon_minutes=horizon_minutes or None,
            timeframe=timeframe or None,
        )
        return res
    finally:
        conn.close()
