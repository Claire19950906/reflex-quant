#!/usr/bin/env python3
"""v446（2026-11-18）—— 入库前的零成本筛子：重复 / 垃圾 / 统计·分析类（非新闻）。

用户指令（2026-11-18 原话）：

> "筛选吧重复的、垃圾信息、统计类分析类（非新闻类信息）筛出去"

背景（实测）：近 30 分钟入库 **16,432 条**，但：
  · 门卫只判了 140 次（其余靠 6h 缓存/降级）；
  · 主分析只跑了 12 次 → 落库决策 1 条；
  · `filter_reason` 里 `gate_undetermined` 4,800 条。
也就是说：**新闻洪水里绝大多数是同一条消息被多个源转发 + 统计/榜单/复盘类噪音**，
它们既烧门卫额度、又把真正的快讯挤在后面。这一层在**入库前**用纯字符串规则筛掉，
不花任何 LLM 额度、微秒级。

三个筛子（都可独立开关）：

1. `is_duplicate()`  —— 归一化指纹（`db.content_fingerprint`）在窗口内已存在 → 重复。
   同一条快讯被 jin10 / 东财 / 深潮 / feishu 各转一次，指纹完全相同（剥 hash 前缀、
   去标点空白、小写），所以精确去重就能吃掉跨源转载。
2. `nonnews_reason()` —— 统计 / 榜单 / 数据一览 / 复盘 / 研报 / 行情播报类
   （用户要的"统计类分析类非新闻"）。
3. 已有 `realtime_filter` 的 JUNK_WORDS 继续管营销/空投/教程类。

设计原则：**宁可漏放，不可错杀突发**。规则只收"结构性模板"（榜单、一览、复盘、
同比环比汇总），不碰"某公司宣布…""美联储暗示…"这类事件句。
"""
from __future__ import annotations

import re
from typing import Optional

# ── 配置（延迟读 config，避开 import 期依赖）──────────────────────────────
def _cfg(name: str, default):
    try:
        import config  # noqa: WPS433
        return getattr(config, name, default)
    except Exception:
        return default


def enabled() -> bool:
    try:
        return bool(int(_cfg("NEWS_PREFILTER_ENABLED", 1)))
    except (TypeError, ValueError):
        return True


def dedup_enabled() -> bool:
    try:
        return bool(int(_cfg("NEWS_DEDUP_ON_INGEST", 1)))
    except (TypeError, ValueError):
        return True


def dedup_window_h() -> float:
    try:
        return max(0.0, float(_cfg("NEWS_DEDUP_WINDOW_H", 6) or 0))
    except (TypeError, ValueError):
        return 6.0


def nonnews_enabled() -> bool:
    try:
        return bool(int(_cfg("NEWS_NONNEWS_FILTER", 1)))
    except (TypeError, ValueError):
        return True


# ── 1) 重复 ────────────────────────────────────────────────────────────────
def is_duplicate(conn, fingerprint: str, ts_int: int) -> bool:
    """窗口内已存在同指纹的行 → 判重复（保留最早那条，后到的标 FILTERED）。

    窗口默认 6 小时：超过 6 小时的同文重复（例如同一份月报每天被转）才算新事件。
    """
    if not fingerprint or not dedup_enabled():
        return False
    _win = dedup_window_h()
    if _win <= 0:
        return False
    try:
        lo = int(ts_int or 0) - int(_win * 3600)
        row = conn.execute(
            "SELECT 1 FROM raw_news"
            " WHERE content_fingerprint = ? AND ts >= ?"
            "   AND status IN ('PENDING','PROCESSING','DONE')"
            " LIMIT 1",
            (fingerprint, lo),
        ).fetchone()
        return bool(row)
    except Exception:
        return False


# ── 2) 统计 / 分析 / 榜单 / 复盘（非新闻）────────────────────────────────
# 全部是"结构性模板"，出现在标题或开头 200 字内就足够判死。
# 刻意避开："宣布/签署/批准/发布/暂停/起诉/违约/暴雷/降息/加息"等事件动词。
_NONNEWS_PATTERNS = [
    # 榜单 / 排名 / 盘点
    r"涨幅榜", r"跌幅榜", r"排行榜", r"榜单", r"排名前[0-9一二三四五六七八九十]",
    r"盘点", r"一览表?", r"数据一览", r"全览",
    # 汇总 / 回顾 / 前瞻 / 复盘（纯归纳性文本）
    r"要闻汇总", r"要闻回顾", r"早间要闻", r"午间要闻", r"晚间要闻",
    r"复盘", r"周复盘", r"月度复盘", r"年度复盘",
    r"市场回顾", r"行情回顾", r"本周(回顾|总结)", r"本月(回顾|总结)",
    r"前瞻", r"本周前瞻", r"下周前瞻", r"数据前瞻", r"事件前瞻",
    # 行情播报 / 分时综述
    r"行情播报", r"行情速递", r"市场速览", r"盘面速览", r"收盘播报", r"开盘播报",
    r"早盘(速览|综述|播报)", r"午盘(速览|综述|播报)", r"尾盘(速览|综述|播报)",
    r"(收涨|收跌|收报|报收)\s*[0-9]", r"指数.{0,6}(涨|跌).{0,4}[0-9]%",
    # 统计口径（同比/环比/统计/数据整理）
    r"统计显示", r"数据显示", r"据统计", r"数据整理", r"数据汇总",
    r"同比(增长|下降|上升|下滑)", r"环比(增长|下降|上升|下滑)",
    # 研报 / 评级 / 目标价 / 观点归纳
    r"研报", r"评级", r"目标价", r"投资建议", r"荐股", r"机构调研",
    r"分析师(预计|预测|观点|认为)", r"专家(解读|观点|表示|认为)",
    r"市场解读", r"深度解读", r"独家解读", r"后市(展望|研判|分析)",
    # 资金/持仓统计
    r"资金流向", r"主力资金", r"净流入", r"净流出", r"持仓变化", r"仓位统计",
    # 教程/科普/榜单类（与 realtime_filter 的 JUNK 互补）
    r"教程", r"入门指南", r"新手必读", r"一文读懂", r"图解",
]
_RE_NONNEWS = [re.compile(p) for p in _NONNEWS_PATTERNS]

# 负向词：出现这些说明是**真事件**，即使命中上面的模板也不筛（防错杀）
_EVENT_WORDS = (
    "宣布", "签署", "批准", "否决", "通过", "发布", "暂停", "恢复", "叫停",
    "起诉", "诉讼", "违约", "破产", "暴雷", "清算", "被罚", "罚款", "调查",
    "降息", "加息", "降准", "上调", "下调", "抛售", "增持", "回购",
    "拿下", "中标", "收购", "并购", "裁员", "停产", "罢工", "制裁", "禁令",
    "上市", "退市", "冻结", "查封", "逮捕", "死亡", "坠毁", "爆炸", "袭击",
)


def nonnews_reason(text: str) -> Optional[str]:
    """命中统计/分析/榜单类模板 → 返回原因串；否则 None。

    `reason` 会写进 `raw_news.filter_reason`，便于事后统计"筛掉了什么"。
    """
    if not text or not nonnews_enabled():
        return None
    _t = str(text)[:200]
    # 先看是否含事件动词：含则视为新闻，不做模板筛
    if any(w in _t for w in _EVENT_WORDS):
        return None
    for rx in _RE_NONNEWS:
        if rx.search(_t):
            return "non_news:" + rx.pattern[:24]
    return None


def classify(content: str, conn=None, fingerprint: str = "", ts_int: int = 0) -> Optional[str]:
    """统一入口：返回 None 表示保留；否则返回 filter_reason（调用方标 FILTERED）。"""
    if not enabled():
        return None
    _r = nonnews_reason(content)
    if _r:
        return _r
    if conn is not None and is_duplicate(conn, fingerprint, ts_int):
        return "dup_fingerprint"
    return None
