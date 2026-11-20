# -*- coding: utf-8 -*-
"""分析前范围前置过滤：把「注定 out_of_scope」的新闻挡在主分析之前。

为什么要前置
------------
2026-11-17 06:37 实测：gate 调了 1,006 次、main 只成功 71 次（14:1），
92 条 PENDING 压着，今天走到 DONE 的是 0 条。而 482 条历史决策里有 176 条
（36.5%）target_asset 落在 NONE/个股，全部走完了「7.5k 字符 system prompt +
七个注入块 + 约 25 秒调用」，只为得出「不在核心清单，HOLD 兜底」。

也就是说：**队列里排的队，有三分之一是不必排的**。

保守到什么程度
--------------
只用**确定性判据**丢新闻，判不准一律进分析：

  · `bucket`        —— 门卫只给了桶标签（个股/板块/ETF），没有核心品种
  · `stock_hints`   —— 正文只有财报/涨停/回购这类个股特征，且无核心别名
  · `category_stock`—— market_category 明确是股票

`no_evidence`（门卫没给任何信息，实测占 49.8%）**绝不丢** —— 那才是最容易误杀的桶。
前置过滤只影响「要不要花 25 秒和 5.4k tokens」，不影响任何交易判断：
被丢的行落 `raw_news.status='FILTERED' + filter_reason='prefilter_scope:*'`，
随时可查、可回捞（把 filter_reason 清掉改回 PENDING 即可重跑）。

开关与默认值（重要）
-------------------
**默认关闭**（`ANALYSIS_PREFILTER` 不设 = 关）。原因不是保守，是实测：

2026-11-17 07:10 拿当时 PENDING 的 101 条试判，会丢 12 条（11.9%），人工抽查
其中 **4 条不该丢**：
  · 32033「Endeavour Silver 截获 1.42 米银当量矿段」—— 是白银(XAG)供给新闻，被桶标签挡住
  · 32034「美国商务部限制措施、美股收低」—— 宏观邻近，被当个股
  · 32028 / 32031 两次财经日历（澳洲联储、欧洲央行）—— 只因出现"业绩报告"命中个股特征

也就是说 `stock_hints` 这一档的判据还不够硬（"业绩报告"这种词在日历里也出现）。
**先不默认开**：要么收紧判据，要么只对 `gate_main_asset` 明确是桶标签且正文无任何
核心/邻近别名的行生效。开关打开前请先跑 `summarize()` 抽查。

`ANALYSIS_PREFILTER=1` 打开；默认关。
"""
from __future__ import annotations

import os
import sqlite3
from typing import Any, Iterable, List, Sequence, Tuple

from .route import route_news

# 只丢这几类「确定性越界」；no_evidence / unknown 一律保留
DROP_REASONS = ("bucket", "stock_hints", "category_stock")

PREFILTER_ENV = "ANALYSIS_PREFILTER"


def enabled() -> bool:
    """v412-G：默认开启。

    原注释写"收紧判据前不许默认开"，因为当时 stock_hints 会误杀白银/宏观日历。
    那次抽查之后有两个判据 bug 被修掉（都在 `analysis/route.py`）：
      1) 「桶标签判定」排在「正文别名检查」之前 → 正文明写比特币/白银也被判越界；
      2) 别名表不认宏观/地缘词（美联储、战争、制裁、OPEC）→ 这类传导源被丢掉。
    修完用 14 天全量 18,851 条新闻重抽：判越界 3,451 条（18.3%），
    其中正文含核心/宏观关键词的**只剩 2 条**（0.01%，此前 126 条）。

    仍保留开关：`ANALYSIS_PREFILTER=0` 可随时关闭（关掉只是不做前置过滤，行为与旧版本一致）。
    """
    return str(os.getenv(PREFILTER_ENV, "1")).strip() in ("1", "true", "True")


def judge(row: Any) -> Tuple[bool, str]:
    """单行判定：返回 (是否丢弃, 判据)。不抛异常 —— 判不了就当保留。"""
    try:
        text = str(_get(row, "content") or "")[:2000]
        title = str(_get(row, "title") or "")[:200]
        res = route_news(
            gate_main_asset=str(_get(row, "gate_main_asset") or ""),
            gate_assets=str(_get(row, "gate_assets") or ""),
            news_text=(title + " " + text).strip(),
            category=str(_get(row, "market_category") or _get(row, "category") or ""),
        )
    except Exception:
        return (False, "judge_error")
    if res.scope == "out_of_scope" and res.reason in DROP_REASONS:
        return (True, res.reason)
    return (False, f"keep:{res.scope}/{res.reason}")


def _get(row: Any, key: Any) -> Any:
    """兼容 sqlite3.Row / dict / **tuple**。

    v412-G：引擎的连接没有设 row_factory，`UPDATE ... RETURNING *` 拿回来的是**元组**，
    `row["content"]` 会抛 TypeError；原来只捕获 IndexError/KeyError → judge 抛异常 →
    被兜成 `judge_error` → 全部 keep。prefilter 从上线起一条都没过滤过，就是卡在这。
    """
    if isinstance(row, dict):
        return row.get(key)
    try:
        return row[key]
    except Exception:
        return None


def _row_id(row: Any) -> int:
    """取行 id。`RETURNING *` 的第一列就是 raw_news.id，元组形态按位置取。"""
    if isinstance(row, dict):
        try:
            return int(row.get("id") or 0)
        except Exception:
            return 0
    try:
        return int(row["id"])
    except Exception:
        try:
            return int(row[0])
        except Exception:
            return 0


def apply(conn: sqlite3.Connection, rows: Sequence[Any]) -> List[Any]:
    """把该丢的行标 FILTERED，返回保留的行。

    调用点必须处在「已把行 UPDATE 成 PROCESSING 的同一事务」里 ——
    这样过滤和被过滤是原子的，不会出现「标了 PROCESSING 又变 FILTERED」的中间态。

    v412-G：不再从 row 里读字段（row 可能是元组），改为按 id 回查 raw_news。
    批大小只有几十条，多几十个 SELECT 可以接受，换来「判据真的被执行」。
    """
    if not rows:
        return []
    if not enabled():
        return list(rows)
    kept: List[Any] = []
    dropped: List[Tuple[int, str]] = []
    for row in rows:
        nid = _row_id(row)
        if not nid:
            kept.append(row)
            continue
        try:
            info = conn.execute(
                "SELECT content, gate_main_asset, gate_assets, gate_kind "
                "FROM raw_news WHERE id=?", (nid,)).fetchone()
        except Exception:
            info = None
        if not info:
            kept.append(row)
            continue
        drop, why = judge({
            "id": nid,
            "content": _get(info, 0),
            "gate_main_asset": _get(info, 1),
            "gate_assets": _get(info, 2),
            "gate_kind": _get(info, 3),
        })
        if drop:
            dropped.append((nid, why))
        else:
            kept.append(row)
    for nid, why in dropped:
        conn.execute(
            "UPDATE raw_news SET status='FILTERED', processing_at=NULL,"
            " filter_reason=? WHERE id=?",
            (f"prefilter_scope:{why}"[:40], nid),
        )
    if dropped:
        print(f"  [PREFILTER] 范围前置过滤 {len(dropped)} 条"
              f"（{', '.join(f'{n}:{w}' for n, w in dropped[:4])}）", flush=True)
    return kept


def summarize(rows: Iterable[Any]) -> dict:
    """只统计不动作（给审计脚本用）。"""
    stat: dict = {}
    for row in rows:
        drop, why = judge(row)
        key = ("drop:" + why) if drop else why
        stat[key] = stat.get(key, 0) + 1
    return stat
