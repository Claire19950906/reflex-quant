#!/usr/bin/env python3
"""bridge_news.py — 新闻解析/影响评估这一簇（2026-11-18）。

从 bridge_server.py 搬出的第二个闭包，起始于两个巨无霸：
`_news_impact_evaluate`（416 行，评估一条新闻对资产的影响）和
`_news_row_to_api_event`（170 行，库行 → 前端事件结构），连带 13 个解析/清洗助手
（ticker 提取、方向打分、模型标签剥离、空文本判定、市值分类、量纲措辞）和 8 个词典常量。

搬走前的清理：`db_cursor` / `MAX_PLAUSIBLE_MOVE_PCT` 已在 bridge_builders，
`_ASSET_SYNONYMS` 的 cem_constants 回退块一起搬过来，所以这里不回引 bridge_server。

bridge_server 用 `from bridge_news import *` 接回，名字不变。
"""

from __future__ import annotations

from common import time_utils
import event_status
import json
import re
import re as _re
import time
from typing import Annotated, Any, Optional

from bridge_builders import MAX_PLAUSIBLE_MOVE_PCT, db_cursor  # 复用的通用件

__all__ = [
    "BEARISH_KEYWORDS",
    "BULLISH_KEYWORDS",
    "CATEGORY_BY_TICKER",
    "KNOWN_TICKERS",
    "NEWS_ASSETS_ALIASES",
    "_ASSET_CODE_RE",
    "_EMPTY_TEXT_LITERALS",
    "_MODEL_TAG_RE",
    "_classify_direction",
    "_extract_assets_from_text",
    "_extract_target_asset",
    "_extract_text_signal",
    "_first_asset",
    "_infer_market_category",
    "_magnitude_label",
    "_meaningful_prose",
    "_meaningful_text",
    "_news_impact_evaluate",
    "_news_row_to_api_event",
    "_parse_asset_list",
    "_rget",
    "_strip_model_tag",
    "_ts_to_str",
]


BULLISH_KEYWORDS = (
    "突破", "创新高", "新高", "上涨", "飙升", "暴涨", "大涨", "利好", "看多",
    "增长", "加仓", "买入", "增持", "底部", "筑底", "反弹", "回暖", "复苏",
    "rally", "surge", "breakout", "bullish", "long",
)


BEARISH_KEYWORDS = (
    "暴跌", "腰斩", "崩盘", "跌停", "创新低", "新低", "下跌", "大跌", "利空", "看空",
    "撤资", "出逃", "清仓", "减持", "减仓", "闪崩", "恐慌", "衰退",
    "crash", "plunge", "drop", "fall", "bearish", "short", "liquidation",
)


KNOWN_TICKERS = (
    "BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "DOGE", "GOLD", "XAU",
    "OIL", "WTI", "ES", "NQ", "EUR", "AUD", "CAD", "NG", "WHEAT", "CORN",
    "MU", "SKHYNIX", "UNI",
)


# 资产同义词（中文名/英文名）—— 中文财经新闻里基本不出字面 ticker，
# 不做同义词扫描就答不出"这条新闻利好哪个标的"（G06）。
try:
    from cem_constants import ASSET_SYNONYMS as _ASSET_SYNONYMS
except Exception:
    _ASSET_SYNONYMS = {
        "BTC": ["BTC", "比特币", "bitcoin"],
        "ETH": ["ETH", "以太坊", "ethereum"],
        "SOL": ["SOL", "索拉纳", "solana"],
        "XAU": ["XAU", "黄金", "金价", "gold"],
        "WTI": ["WTI", "原油", "油价", "crude"],
        "NVDA": ["NVDA", "英伟达", "nvidia"],
        "TSLA": ["TSLA", "特斯拉", "tesla"],
        "MU": ["MU", "美光", "micron"],
        "SKHYNIX": ["SKHYNIX", "海力士", "sk hynix"],
        "BNB": ["BNB", "币安币", "binance"],
        "XRP": ["XRP", "瑞波", "ripple"],
        "DOGE": ["DOGE", "狗狗币", "dogecoin"],
        "AAPL": ["AAPL", "苹果", "apple"],
        "MSFT": ["MSFT", "微软", "microsoft"],
        "GOOGL": ["GOOGL", "谷歌", "google"],
        "AMZN": ["AMZN", "亚马逊", "amazon"],
        "META": ["META", "脸书", "facebook"],
        "ADA": ["ADA", "艾达", "cardano"],
    }


CATEGORY_BY_TICKER = {
    "BTC": "CRYPTO", "ETH": "CRYPTO", "SOL": "CRYPTO", "BNB": "CRYPTO",
    "ADA": "CRYPTO", "XRP": "CRYPTO", "DOGE": "CRYPTO",
    "XAU": "GOLD", "GOLD": "GOLD",
    "WTI": "OIL", "OIL": "OIL",
    "MU": "HYNIX", "SKHYNIX": "HYNIX", "000660": "HYNIX", "005930": "HYNIX",
    "NVDA": "STOCK", "TSLA": "STOCK", "AAPL": "STOCK", "MSFT": "STOCK",
    "GOOGL": "STOCK", "META": "STOCK", "AMZN": "STOCK", "AMD": "STOCK", "NFLX": "STOCK",
    "BABA": "STOCK", "JD": "STOCK", "PDD": "STOCK",
    "NIO": "STOCK", "XPEV": "STOCK", "LI": "STOCK",
}


NEWS_ASSETS_ALIASES: dict[str, list[str]] = {
    # crypto
    "BTC": ["比特币", "大饼", "BTC", "BITCOIN"],
    "ETH": ["以太坊", "以太", "ETH", "ETHEREUM"],
    "SOL": ["索拉纳", "SOL", "SOLANA"],
    "BNB": ["币安币", "BNB", "BINANCECOIN"],
    "ADA": ["艾达币", "卡尔达诺", "ADA", "CARDANO"],
    "XRP": ["瑞波币", "瑞波", "XRP", "RIPPLE"],
    "DOGE": ["狗狗币", "DOGE", "DOGECOIN"],
    "WLD": ["世界币", "WLD", "WORLDCOIN"],
    "AVAX": ["雪崩协议", "雪崩", "AVAX", "AVALANCHE"],
    "MATIC": ["MATIC", "POLYGON"],
    "DOT": ["波卡", "DOT", "POLKADOT"],
    "LINK": ["LINK", "CHAINLINK"],
    "TON": ["TON", "TONCOIN"],
    "TRX": ["波场", "TRX", "TRON"],
    "SHIB": ["柴犬币", "SHIB", "SHIBA"],
    "LTC": ["莱特币", "LTC", "LITECOIN"],
    "BCH": ["比特现金", "BCH"],
    "ATOM": ["宇宙", "ATOM", "COSMOS"],
    "UNI": ["UNI", "UNISWAP"],
    "APT": ["APT", "APTOS"],
    # US stocks
    "AAPL": ["苹果", "AAPL", "APPLE"],
    "MSFT": ["微软", "MSFT", "MICROSOFT"],
    "NVDA": ["英伟达", "NVDA", "NVIDIA"],
    "TSLA": ["特斯拉", "TSLA", "TESLA"],
    "MU": ["美光", "MU", "MICRON"],
    "AMD": ["超微", "AMD"],
    "GOOGL": ["谷歌", "GOOGL", "GOOGLE", "GOOG"],
    "META": ["META", "脸书", "FACEBOOK"],
    "AMZN": ["亚马逊", "AMZN", "AMAZON"],
    "NFLX": ["奈飞", "NFLX", "NETFLIX"],
    "BABA": ["阿里巴巴", "BABA", "ALIBABA"],
    "JD": ["京东", "JD"],
    "PDD": ["拼多多", "PDD"],
    "NIO": ["蔚来", "NIO"],
    "XPEV": ["小鹏", "XPEV"],
    "LI": ["理想", "LI"],
    # KR stocks (6位数字代码 + 公司名)
    "000660": ["海力士", "SK海力士", "000660"],
    "005930": ["三星", "005930", "SAMSUNG"],
    "003550": ["LG", "003550"],
    "005380": ["现代汽车", "现代", "005380", "HYUNDAI"],
    "000270": ["起亚", "000270", "KIA"],
}


_ASSET_CODE_RE = re.compile(r"^[A-Z0-9][A-Z0-9._\-]{0,15}$")


_EMPTY_TEXT_LITERALS = frozenset({"{}", "[]", "null", "None", "undefined", "nan"})


_MODEL_TAG_RE = re.compile(r"^\[[^\]\n]{1,60}\]\s*")


def _classify_direction(score: float) -> str:
    if score > 0.3:
        return "Bullish"
    if score < -0.3:
        return "Bearish"
    return "Neutral"


def _extract_assets_from_text(headline: str, body: str | None, declared: str,
                              declared_asset: str = "") -> list[str]:
    """提取新闻影响的资产标的。

    口径顺序 (从可靠到兜底):
      1. news_events.assets   —— 采集端落的多资产标签
      2. news_events.asset    —— 打分端落的单资产归属 (之前完全没被用!)
      3. 同义词扫描           —— 中文名/英文名 (比特币/BTC/bitcoin …)
      4. ticker 字面扫描      —— KNOWN_TICKERS

    之前只做 1 和 4: 中文财经新闻里几乎不出现 "BTC" 这种字面 ticker, 于是
    "这条新闻利好哪个标的" 经常答不出来 (G06 红灯的根因)。
    """
    declared_list = [a.strip().upper() for a in (declared or "").split(",") if a.strip()]
    if declared_list:
        return declared_list
    single = (declared_asset or "").strip().upper()
    if single:
        return [single]
    text = ((headline or "") + " " + (body or ""))
    low = text.lower()
    hits: list[str] = []
    for ticker, syns in _ASSET_SYNONYMS.items():
        for syn in syns:
            if syn and (syn.lower() in low or syn.upper() in text.upper()):
                hits.append(ticker)
                break
    if hits:
        return hits
    upper = text.upper()
    return [t for t in KNOWN_TICKERS if t in upper]


def _extract_target_asset(text: str) -> str:
    """用 NEWS_ASSETS_ALIASES 反向匹配 ticker.
    优先级: ticker 形式 word-boundary > CJK substring (避免英文 ticker 误命中)."""
    if not text:
        return ""
    upper = text.upper()
    # 先 word-boundary 匹配 ticker 形式 (字母数字外)
    for ticker in NEWS_ASSETS_ALIASES.keys():
        ticker_up = ticker.upper()
        if _re.search(rf"\b{_re.escape(ticker_up)}\b", upper):
            return ticker
    # 再试 CJK 中文别名
    for ticker, aliases in NEWS_ASSETS_ALIASES.items():
        for alias in aliases:
            if alias and any('\u4e00' <= c <= '\u9fff' for c in alias) and alias in text:
                return ticker
    return ""


def _extract_text_signal(headline: str, body: str | None) -> dict:
    """从文本提取方向信号(弥补 news_events.score 大量为 0 的问题)"""
    text = (headline or "") + " " + (body or "")
    bull_hits = sum(1 for k in BULLISH_KEYWORDS if k in text)
    bear_hits = sum(1 for k in BEARISH_KEYWORDS if k in text)
    if bull_hits + bear_hits == 0:
        text_score = 0.0
    else:
        text_score = (bull_hits - bear_hits) / (bull_hits + bear_hits)
    return {
        "bull_hits": bull_hits,
        "bear_hits": bear_hits,
        "text_score": text_score,
        "has_signal": (bull_hits + bear_hits) > 0,
    }


def _first_asset(raw: Any) -> str:
    """取 assets 列里的第一个**品种代码**（兼容逗号串与遗留 JSON 数组）。

    板块类别（美股/原油/个股/黄金/加密主流/山寨）不是品种代码，跳过。
    """
    if not raw:
        return ""
    for v in _parse_asset_list(raw):
        if _ASSET_CODE_RE.match(v):
            return v
    return ""


def _infer_market_category(asset: str) -> str:
    if asset and asset in CATEGORY_BY_TICKER:
        return CATEGORY_BY_TICKER[asset]
    return "AGGREGATE"


def _magnitude_label(stars: int) -> str:
    return ["微弱", "轻微", "中等", "显著", "重大"][max(0, min(4, stars - 1))]


def _meaningful_prose(v: Any) -> str:
    """同 _meaningful_text, 但**额外**把 JSON 对象/数组串归零。

    用于"给人看的文字"(分析摘要、影响解读等): 这些位置绝不能出现裸 JSON,
    否则前端会直接渲染 {"timestamp": "2026-10-30 14:27:42 CST", ...} 这种机器噪音。
    """
    s = _meaningful_text(v)
    if not s:
        return ""
    if s[:1] in "{[" and s[-1:] in "}]":
        try:
            json.loads(s)
        except (ValueError, TypeError):
            return s          # 长得像 JSON 但解析不了 → 当普通文本
        return ""             # 确实是 JSON → 不是人话, 归零
    return s


def _meaningful_text(v: Any) -> str:
    """把 AI 文本字段里的空值占位归一成 ""。

    - "{}" / "[]" / "null" / "None" 这类字面量 → ""
    - 形如 {"a": null, "b": ""} 的对象(所有值都是空) → ""
    - 其它情况原样返回(去掉首尾空白)

    ⚠ v311: 保留 `json=` 参数(默认 True)让调用方能区分"这段文本其实是 JSON"。
      本函数**故意不**把非空 JSON 对象整体清空 —— 有些字段(如 decision_context)
      就是要原样透传行情快照给前端单独展示。只有当调用方要的是"人话摘要"时,
      才应传 json=False 把 JSON 串一并归零, 避免把 {"timestamp": ...} 当分析文本。
    """
    if v is None:
        return ""
    s = str(v).strip()
    if not s or s in _EMPTY_TEXT_LITERALS:
        return ""
    if s.startswith("{") and s.endswith("}"):
        try:
            obj = json.loads(s)
        except (ValueError, TypeError):
            return s
        if isinstance(obj, dict):
            if not any(
                val not in (None, "", [], {}, "null", "None") for val in obj.values()
            ):
                return ""
    return s


def _news_impact_evaluate(
    news_id: int | None = None,
    headline: str | None = None,
    news_body: str | None = None,
    declared_assets: str | None = None,
    declared_score: float | None = None,
    declared_source: str | None = None,
    days_window: int = 3,
) -> dict:
    """核心评估函数 — 5 因子综合打分(GET/POST 复用)"""
    reasoning: list[dict] = []
    support: dict = {}
    honest_notes: list[str] = []

    # ───── 1) 载入新闻 ─────
    row = None
    if news_id is not None:
        with db_cursor() as conn:
            r = conn.execute(
                "SELECT id, news_id, source, headline, body, score, assets, asset, action, ts "
                "FROM news_events WHERE id=?",
                (news_id,),
            ).fetchone()
            if r:
                row = dict(r)
            else:
                return {"ok": False, "error": f"news_id={news_id} 不存在"}
    elif headline:
        row = {
            "id": None,
            "news_id": None,
            "source": declared_source or "external",
            "headline": headline,
            "body": news_body or "",
            "score": declared_score if declared_score is not None else 0.0,
            "assets": declared_assets or "",
            "asset": "",
            "action": "",
            "ts": int(time.time()),
        }
    else:
        return {"ok": False, "error": "必须提供 news_id 或 headline"}

    reasoning.append({
        "step": 1,
        "factor": "原始新闻",
        "question": "新闻是什么?",
        "answer": f"[{row['source'] or '?'}] {(row['headline'] or '')[:80]}",
    })

    # ───── 2) 因子 A: news_score + 文本关键词 ─────
    news_score = float(row["score"] or 0.0)
    text_signal = _extract_text_signal(row["headline"], row.get("body"))
    composite_score = news_score * 0.6 + text_signal["text_score"] * 0.4
    # 方向口径统一 (G05 的根因): 系统早在 news_events.action 里就落了**权威方向**
    # (news_score_worker 从 LLM 结论回写), 这里却另起一套文本启发式重算方向 ——
    # 于是同一条新闻在两个页面可能显示相反方向。权威口径优先, 文本启发式只兜底。
    action = str(row.get("action") or "").strip().upper()
    if action in ("BUY", "SELL"):
        direction = "Bullish" if action == "BUY" else "Bearish"
    elif action == "HOLD":
        direction = "Neutral"
    else:
        direction = _classify_direction(composite_score)
    support["news_score"] = {
        "raw_score": news_score,
        "text_bull_hits": text_signal["bull_hits"],
        "text_bear_hits": text_signal["bear_hits"],
        "composite_score": round(composite_score, 3),
    }
    reasoning.append({
        "step": 2,
        "factor": "因子 A · 新闻情感方向",
        "question": "这条新闻情感方向是 Bullish / Bearish / Neutral?",
        "answer": (
            f"{direction} (DB score={news_score:.2f}, "
            f"文本 hit 多={text_signal['bull_hits']} 空={text_signal['bear_hits']}, "
            f"综合={composite_score:.2f})"
        ),
    })

    # ───── 3) 受影响资产识别 ─────
    primary_assets = _extract_assets_from_text(
        row["headline"], row.get("body"), row["assets"] or "", row.get("asset") or ""
    )
    if not primary_assets:
        # 再兜一层: 这条新闻对应的 AI 决策里, 模型自己标的的资产
        try:
            with db_cursor() as conn:
                r2 = conn.execute(
                    "SELECT target_asset FROM ai_decisions WHERE news_id=? "
                    "AND target_asset IS NOT NULL AND target_asset!='' "
                    "ORDER BY ts DESC LIMIT 1",
                    (row.get("news_id") or row.get("id"),),
                ).fetchone()
            if r2 and r2[0]:
                primary_assets = [str(r2[0]).strip().upper()]
        except Exception:
            pass
    if not primary_assets:
        honest_notes.append("未识别出受影响资产 ticker — 影响范围仅作用于大盘情绪")
    reasoning.append({
        "step": 3,
        "factor": "受影响资产识别",
        "question": "这条新闻提到哪些资产?",
        "answer": f"{primary_assets if primary_assets else '(无 — 仅影响大盘情绪)'}",
    })

    # ───── 4) 因子 B: critical_moves 同向验证 ─────
    move_evidence: list[dict] = []
    if primary_assets:
        # ts 可能为 NULL（外部写入的新闻列不齐）→ 0 会让窗口落在 1970，退化为"查不到"，
        # 但至少不抛 TypeError 变成 500。
        _row_ts = int(row["ts"] or 0)
        cutoff_start = _row_ts - days_window * 86400
        cutoff_end = _row_ts + days_window * 86400
        placeholders = ",".join("?" for _ in primary_assets)
        try:
            with db_cursor() as conn:
                moves = conn.execute(
                    f"SELECT id, asset, ts, move_pct, move_kind, algo_score, duration_min "
                    f"FROM critical_price_moves "
                    f"WHERE asset IN ({placeholders}) AND ts BETWEEN ? AND ? "
                    f"AND ABS(COALESCE(move_pct,0)) <= ? "
                    f"ORDER BY ts DESC LIMIT 20",
                    (*primary_assets, cutoff_start, cutoff_end, MAX_PLAUSIBLE_MOVE_PCT),
                ).fetchall()
            news_dir = (
                1 if composite_score > 0.3
                else (-1 if composite_score < -0.3 else 0)
            )
            same_dir = opp_dir = 0
            for m in moves:
                move_dir = 1 if m["move_pct"] > 0 else -1
                aligned = (news_dir != 0 and move_dir == news_dir)
                if news_dir == 0:
                    pass
                elif aligned:
                    same_dir += 1
                else:
                    opp_dir += 1
                move_evidence.append({
                    "id": m["id"], "asset": m["asset"], "ts": m["ts"],
                    "move_pct": m["move_pct"], "move_kind": m["move_kind"],
                    "algo_score": m["algo_score"],
                    "aligned_with_news": aligned,
                })
            ratio = (
                round(same_dir / max(1, same_dir + opp_dir), 2)
                if (same_dir + opp_dir) > 0 else None
            )
            support["critical_moves"] = {
                "window_days": days_window,
                "n_moves": len(moves),
                "n_same_dir": same_dir,
                "n_opp_dir": opp_dir,
                "ratio": ratio,
                "items": move_evidence[:5],
            }
            reasoning.append({
                "step": 4,
                "factor": "因子 B · 临界涨跌同向验证",
                "question": f"前后 {days_window} 天内, 这些资产有没有同向 critical_move?",
                "answer": f"找到 {len(moves)} 次临界涨跌 · 同向 {same_dir} 次 / 反向 {opp_dir} 次",
            })
        except Exception as e:
            honest_notes.append(f"critical_moves 查询失败: {e}")

    # ───── 5) 因子 C: COT spec_index 极值反向警示 ─────
    cot_evidence: list[dict] = []
    cot_warning: list[str] = []
    if primary_assets:
        placeholders = ",".join("?" for _ in primary_assets)
        try:
            with db_cursor() as conn:
                cot_rows = conn.execute(
                    f"SELECT asset, spec_index, report_date, noncomm_net "
                    f"FROM cftc_cot_data "
                    f"WHERE asset IN ({placeholders}) AND spec_index IS NOT NULL "
                    f"ORDER BY report_date DESC LIMIT 10",
                    primary_assets,
                ).fetchall()
            news_dir = (
                1 if composite_score > 0.3
                else (-1 if composite_score < -0.3 else 0)
            )
            for c in cot_rows:
                if abs(c["spec_index"]) < 0.7:
                    continue
                spec_dir = 1 if c["spec_index"] > 0 else -1
                warning = ""
                if news_dir != 0 and spec_dir == -news_dir:
                    warning = "⚠ 新闻方向与 COT 极值反向(机构反向操作)"
                    cot_warning.append(warning)
                elif news_dir != 0 and spec_dir == news_dir:
                    warning = "✓ COT 极值同向(机构顺势)"
                cot_evidence.append({
                    "asset": c["asset"],
                    "spec_index": round(c["spec_index"], 3),
                    "report_date": c["report_date"],
                    "noncomm_net": c["noncomm_net"],
                    "signal": "极多" if c["spec_index"] > 0.7 else "极空",
                    "warning": warning,
                })
            support["cot_signals"] = {
                "n_extreme": len(cot_evidence),
                "warnings": cot_warning,
                "items": cot_evidence[:5],
            }
            reasoning.append({
                "step": 5,
                "factor": "因子 C · COT 机构持仓极值",
                "question": "机构持仓有没有极端信号与新闻方向冲突?",
                "answer": f"找到 {len(cot_evidence)} 个极值 · {len(cot_warning)} 个反向警示",
            })
        except Exception as e:
            honest_notes.append(f"cftc_cot_data 查询失败: {e}")

    # ───── 6) 因子 D: fed_rate 走势 ─────
    fed_signal = "unknown"
    fed_aligned: str | None = None
    try:
        with db_cursor() as conn:
            fed_rows = conn.execute(
                "SELECT ts, effr_pct FROM fed_rate_history ORDER BY ts DESC LIMIT 10"
            ).fetchall()
        if len(fed_rows) >= 2:
            recent_delta = fed_rows[0]["effr_pct"] - fed_rows[-1]["effr_pct"]
            if recent_delta > 0.01:
                fed_signal = "rising"
            elif recent_delta < -0.01:
                fed_signal = "falling"
            else:
                fed_signal = "stable"
            news_dir = (
                1 if composite_score > 0.3
                else (-1 if composite_score < -0.3 else 0)
            )
            if news_dir != 0:
                if (fed_signal == "rising" and news_dir == 1) or (
                    fed_signal == "falling" and news_dir == -1
                ):
                    fed_aligned = "conflicting"
                else:
                    fed_aligned = "aligned"
        support["fed_signal"] = {
            "signal": fed_signal,
            "news_alignment": fed_aligned,
        }
        reasoning.append({
            "step": 6,
            "factor": "因子 D · 利率环境",
            "question": "当前利率走势与新闻方向是否一致?",
            "answer": (
                f"利率 {fed_signal} · "
                f"{'一致' if fed_aligned == 'aligned' else ('冲突' if fed_aligned == 'conflicting' else '中性')}"
            ),
        })
    except Exception as e:
        honest_notes.append(f"fed_rate 查询失败: {e}")

    # ───── 7) 因子 E: trade_reflections_v2 历史同类胜率 ─────
    history_hit_rate: float | None = None
    history_n = 0
    if primary_assets:
        try:
            with db_cursor() as conn:
                ref_rows = conn.execute(
                    "SELECT dimension, subcategory, hit, confidence_at_time "
                    "FROM trade_reflections_v2 "
                    "WHERE asset IN ({}) AND settled=1 "
                    "ORDER BY created_at DESC LIMIT 50".format(
                        ",".join("?" for _ in primary_assets)
                    ),
                    primary_assets,
                ).fetchall()
            if ref_rows:
                history_n = len(ref_rows)
                n_hit = sum(1 for r in ref_rows if r["hit"] == 1)
                history_hit_rate = round(n_hit / history_n, 3)
            support["history"] = {
                "n_reflections": history_n,
                "hit_rate": history_hit_rate,
            }
            if history_n < 30 and history_n > 0:
                honest_notes.append(
                    f"反思样本仅 {history_n} 条 (<30),历史胜率不可信"
                )
            reasoning.append({
                "step": 7,
                "factor": "因子 E · 历史同类决策胜率",
                "question": "历史上对同资产决策的胜率?",
                "answer": (
                    f"{history_n} 条反思 · 命中率 "
                    f"{history_hit_rate if history_hit_rate is not None else '—'}"
                ),
            })
        except Exception as e:
            honest_notes.append(f"trade_reflections_v2 查询失败: {e}")

    # ───── 8) 综合打分(magnitude + confidence) ─────
    aligned_factors = 1  # 因子 A 永远算
    total_factors = 1
    if support.get("critical_moves", {}).get("ratio") is not None:
        total_factors += 1
        if support["critical_moves"]["ratio"] >= 0.5:
            aligned_factors += 1
    if "cot_signals" in support:
        total_factors += 1
        if not cot_warning:
            aligned_factors += 1
    if support.get("fed_signal", {}).get("news_alignment"):
        total_factors += 1
        if support["fed_signal"]["news_alignment"] == "aligned":
            aligned_factors += 1

    abs_score = abs(composite_score)
    stars = 1
    if abs_score >= 0.2:
        stars = 2
    if abs_score >= 0.4 and aligned_factors >= 1:
        stars = 3
    if abs_score >= 0.6 and aligned_factors >= 2:
        stars = 4
    if abs_score >= 0.8 and aligned_factors >= 3:
        stars = 5

    confidence = 0.3
    if total_factors >= 2:
        confidence += 0.15
    if total_factors >= 4:
        confidence += 0.15
    if history_n >= 30:
        confidence += 0.15
    if history_n >= 100:
        confidence += 0.1
    if cot_warning:
        confidence -= 0.2
    if fed_aligned == "conflicting":
        confidence -= 0.15
    confidence = max(0.05, min(0.95, confidence))

    if not primary_assets:
        honest_notes.append("无资产标的 — 影响力仅作用于大盘情绪面")
    if history_n == 0:
        honest_notes.append("无历史反思样本 — 因子 E 缺失")
    if "critical_moves" not in support:
        honest_notes.append("因子 B 缺失(critical_moves 查询失败)")
    if "cot_signals" not in support:
        honest_notes.append("因子 C 缺失(COT 数据查询失败)")

    reasoning.append({
        "step": 8,
        "factor": "综合结论",
        "question": "最终影响方向、强度、可信度?",
        "answer": (
            f"direction={direction} · magnitude={stars}★({_magnitude_label(stars)}) · "
            f"confidence={confidence:.2f} · aligned_factors={aligned_factors}/{total_factors}"
        ),
    })

    # 因果链库 — 真实查表, 按 primary_assets 匹配
    causal_chains_used: list[dict] = []
    try:
        with db_cursor() as conn:
            cc_rows = conn.execute(
                "SELECT chain_id, asset, trigger_pattern, chain_steps, confidence, "
                "       hit_count, miss_count, version "
                "FROM causal_chains WHERE active=1"
            ).fetchall()
        target_assets = set(primary_assets or [])
        for r in cc_rows:
            # asset 命中 primary_assets 中任一 → 引用
            if target_assets and r["asset"] not in target_assets:
                continue
            try:
                steps = json.loads(r["chain_steps"]) if r["chain_steps"] else []
            except Exception:
                steps = []
            causal_chains_used.append({
                "chain_id": r["chain_id"],
                "asset": r["asset"],
                "trigger_pattern": r["trigger_pattern"],
                "confidence": r["confidence"],
                "version": r["version"],
                "hit_count": r["hit_count"],
                "miss_count": r["miss_count"],
                "n_steps": len(steps),
                "first_step": (steps[0].get("node") or steps[0].get("name")) if steps else None,
                "source": "causal_chains",
            })
        if not causal_chains_used:
            honest_notes.append("causal_chains 表存在但无与本次主资产匹配的链路")
    except Exception as e:
        honest_notes.append(f"causal_chains 查询失败: {e}")

    return {
        "ok": True,
        "news_id": row["id"],
        "headline": row["headline"],
        "source": row["source"],
        "direction": direction,
        "magnitude": stars,
        "magnitude_label": _magnitude_label(stars),
        "confidence": round(confidence, 3),
        "confidence_label": (
            "高" if confidence >= 0.7 else ("中" if confidence >= 0.4 else "低")
        ),
        "primary_assets": primary_assets,
        "support": support,
        "reasoning_chain": reasoning,
        "causal_chains_used": causal_chains_used,
        "data_sources": [
            "news_events", "critical_price_moves", "cftc_cot_data",
            "fed_rate_history", "trade_reflections_v2",
        ],
        "honest_notes": honest_notes,
        "ts": int(time.time()),
    }


def _news_row_to_api_event(r) -> dict:
    """news_events row → ApiEvent 形状 (前端 event-bus.tsx 期望格式).

    v311: 行数据来自 news_events LEFT JOIN raw_news + ai_decisions + quick_sim_trades，
    这里取**真实值**而不是占位（旧实现一律返回空/None/0，导致前端展开后
    只能显示 "no detail available"）：
      · source       —— news_events.source 有 48% 是空串 → raw_news.source 兜底
      · published_at —— news_events 没有发布时间 → raw_news.published_at（真发布时间）
      · decision_id / reasoning_path / 决策上下文 → ai_decisions
      · entry/exit/max/min price / settled / verdict → quick_sim_trades
      · 全网溯源 / 闸门判定 / 相关性 / 内容指纹 → raw_news
    """
    # 1) target_asset: assets 字段优先,空则从文本抽
    #    v300-audit: 新 schema 写的是单数 asset,旧 schema 写的是复数 assets —— 两个都取
    assets_raw = str(_rget(r, "assets", "") or "").strip()
    if not assets_raw:
        assets_raw = str(_rget(r, "asset", "") or "").strip()
    asset = _first_asset(assets_raw)
    if not asset or asset.upper() in ("NONE", "AGGREGATE", "ALL"):
        text = (str(_rget(r, "headline", "") or "") + " " + str(_rget(r, "body", "") or ""))[:1200]
        asset = _extract_target_asset(text)
    # v311: ai_decisions.target_asset 是权威值(AI 显式判定的主品种)，优先于文本抽取
    ad_target = str(_rget(r, "target_asset", "") or "").strip().upper()
    if ad_target and ad_target != "NONE":
        asset = ad_target
    asset = asset or "NONE"

    # v311: market_category 同理 —— ai_decisions 里是 AI 判定过的分类
    category = _infer_market_category(asset)
    ad_cat = str(_rget(r, "market_category", "") or "").strip().upper()
    if ad_cat in ("CRYPTO", "GOLD", "OIL", "MACRO", "STOCK", "HYNIX", "AGGREGATE"):
        category = ad_cat

    # v311: source 兜底 —— news_events.source 大量为空串(48%)，raw_news.source 才是真来源
    source = str(_rget(r, "source", "") or "").strip()
    if not source:
        source = str(_rget(r, "raw_source", "") or "").strip()
    if not source:
        source = "unknown"

    headline = str(_rget(r, "headline", "") or "")
    body = str(_rget(r, "body", "") or "")
    action = str(_rget(r, "action", "") or "").strip().upper()
    if action not in ("BUY", "SELL", "HOLD"):
        action = str(_rget(r, "suggested_action", "") or "").strip().upper() or "HOLD"

    # 2) score: news_events.score 经常为 0 → 回退 ai_decisions.sentiment_score
    #    v311: 文本启发式抽取(_extract_text_signal)成本高(实测 ~0.5ms/条，1000 条 ≈ 400ms)，
    #    只在**没有任何情绪分来源**时才跑；AI 给过 sentiment_score 的直接用它
    #    （顺带修掉"AI 情绪分被文本猜测覆盖"的老问题）。
    score = float(_rget(r, "score", 0.0) or 0.0)
    sent = _rget(r, "sentiment_score", None)
    if score == 0.0 and sent is not None:
        try:
            score = float(sent)
        except (TypeError, ValueError):
            score = 0.0
    if score == 0.0 and sent is None:
        score = _extract_text_signal(headline, body)["score"] if "_extract_text_signal" in dir() else 0.0

    # 3) 拼 news_text (展示用) —— v311: 去掉 [hash:xxx] 去重前缀(api_server 侧同样剥离)
    news_text = headline
    if body and body != headline:
        news_text = headline + "\n\n" + body[:600]
    news_text = re.sub(r"\[hash:[a-fA-F0-9]+\]\s*", "", news_text).strip()

    # v317 fix：analysis_status 走 event_status 这个唯一真相源（与 api_server 同源）。
    #   旧实现直接拿 news_events.status 真值，而 news_events.status 由 news_score_worker
    #   无脑写成 'DONE'（见 news_score_worker.py:432 的 UPDATE news_events SET ... status='DONE'）
    #   —— 那个 'DONE' 的语义是"**已打分**"，不是"AI 已分析"，用它当分析状态等于凭空捏造完成。
    #   于是同一行数据：raw_news.status='FAILED'、news_events.status='DONE'
    #   → 8000 端口显示"AI 分析失败"，8100 端口显示"DONE"（前端实际连的是 8100，
    #   所以用户看到的是"分析失败了但显示已完成"）。
    #   现在两边都按 (raw_status, has_decision, is_noise) 三元组推导，口径唯一。
    #
    #   is_noise：噪声行派生为 SKIPPED（噪声不会被 AI 处理，不该显示"待 AI 分析"）。
    #
    #   raw_status：**只认 raw_news.status**（引擎真值）。
    #   绝不能回落到 news_events.status —— 那一列在全表 17078 行里恒为 'DONE'
    #   （news_score_worker 无条件写死），回落到它 = 对 6368 条 raw_news 已被裁剪的
    #   孤儿行凭空捏造"已完成"（它们一条主决策都没有，实测孤儿且无主决策 = 6368/6368）。
    #   孤儿行拿不到 raw 状态时，正确答案是"不知道"→ 无决策即 PENDING（诚实），
    #   而不是谎称 DONE。
    _raw_status = _rget(r, "raw_status", None)
    status = event_status.resolve_analysis_status(
        _raw_status,
        has_decision=_rget(r, "ad_id", None) is not None,
        is_noise=bool(_rget(r, "is_noise", 0)),
        # v321: 门卫(LLM)未判定 → GATE_PENDING（等 replay_deferred_gate 回放）。
        # 这类行不会被主分析认领，显示"待 AI 分析"会误导用户一直等。
        # 仅 gate_unavailable=1（冻结待回放）算待回放；=2 是降级放行（未判定但放行），
        # 主分析照常认领，不能挂"门卫待回放"。
        gate_pending=int(_rget(r, "gate_unavailable", 0) or 0) == 1,
    )

    # v311: 真实发布时间(raw_news.published_at)，拿不到才退回抓取时间
    published_at = str(_rget(r, "published_at", "") or "").strip()
    if not published_at:
        published_at = _ts_to_str(_rget(r, "ts", None))

    # v311: 交易跟踪(来自 quick_sim_trades)；没有对应交易时保持 None/0
    entry_ts = _rget(r, "entry_ts", None)
    entry_time = _ts_to_str(entry_ts) if entry_ts else _ts_to_str(_rget(r, "ts", None))

    reasoning = str(_rget(r, "reasoning", "") or "")
    # v311: 剥掉 "[模型名 (标识)]" 前缀再截断 —— 否则前端 "AI {reason}" 会显示内部模型名
    reason = _strip_model_tag(reasoning)[:80]
    return {
        "id": r["id"],
        "news_id": int(_rget(r, "news_id", 0) or r["id"]),
        "decision_id": _rget(r, "ad_id", None),
        "analysis_status": status,
        "timestamp": _ts_to_str(r["ts"]),
        "published_at": published_at,
        # v311: 全网溯源三件套(raw_news)
        "first_seen_ts": str(_rget(r, "first_seen_ts", "") or ""),
        "net_origin_info": str(_rget(r, "net_origin_info", "") or ""),
        "cross_net_first_seen_ts": str(_rget(r, "cross_net_first_seen_ts", "") or ""),
        "cross_net_source": str(_rget(r, "cross_net_source", "") or ""),
        # v311: 新闻本体元信息(raw_news) —— 前端展开区展示
        "gate_verdict": str(_rget(r, "gate_verdict", "") or ""),
        "gate_core_impact": _meaningful_prose(_rget(r, "gate_core_impact", "")),
        "relevance_score": _rget(r, "relevance_score", None),
        "content_fingerprint": str(_rget(r, "content_fingerprint", "") or ""),
        # v311: 原文链接(raw_news.url)—— 前端溯源区给「查看原文」入口
        "url": str(_rget(r, "raw_url", "") or ""),
        "ai_time": _ts_to_str(_rget(r, "created_at", None) or _rget(r, "fetched_at", None) or r["ts"]),
        "source": source,
        "news_text": news_text,
        "action": action,
        "score": score,
        "reason": reason,
        "market_category": category,
        "target_asset": asset,
        "parent_id": None,
        "child_count": 0,
        "reasoning_path": _meaningful_prose(_rget(r, "reasoning_path", "")),
        "vip_tag": str(_rget(r, "vip_tag", "") or ""),
        "entry_price": _rget(r, "entry_price", None),
        "exit_price": _rget(r, "exit_price", None),
        "max_price": _rget(r, "max_price", None),
        "min_price": _rget(r, "min_price", None),
        "max_price_time": _rget(r, "max_price_time", 0) or 0,
        "min_price_time": _rget(r, "min_price_time", 0) or 0,
        "entry_time": entry_time,
        "is_correct": str(_rget(r, "is_correct", "") or ""),
        "settled": _rget(r, "settled", 0) or 0,
        "doubao_action": "HOLD",
        "doubao_reasoning": "",
        "cluster_size": 1,
        "server_emitted_at_ms": int(time.time() * 1000),
        # v311: 决策侧字段(原先全空 → 前端 AnalysisSteps / TradeDecisionNote 拿不到东西)
        "expected_horizon": str(_rget(r, "expected_horizon", "") or ""),
        "event_strength": str(_rget(r, "event_strength", "") or ""),
        "event_phase": str(_rget(r, "event_phase", "") or ""),
        "direct_catalyst": _rget(r, "direct_catalyst", None),
        "market_confirmation": str(_rget(r, "market_confirmation", "") or ""),
        "attention_score": _rget(r, "attention_score", None),
        "mfe_pct": _rget(r, "mfe_pct", None),
        "mae_pct": _rget(r, "mae_pct", None),
        "decision_context": _meaningful_text(_rget(r, "decision_context", "")),
        "invalidation_condition": str(_rget(r, "invalidation_condition", "") or ""),
        "no_trade_reason": _meaningful_text(_rget(r, "no_trade_reason", "")),
        "trade_gate_reason": _meaningful_text(_rget(r, "trade_gate_reason", "")),
        "evidence_confidence": _rget(r, "evidence_confidence", None),
        "prediction_type": str(_rget(r, "prediction_type", "") or ""),
        "partial_exit_price": _rget(r, "partial_exit_price", None),
        "partial_exit_ratio": _rget(r, "partial_exit_ratio", 0) or 0,
        "partial_pnl_pct": _rget(r, "partial_pnl_pct", 0) or 0,
    }


def _parse_asset_list(raw: Any) -> list:
    """解析 news_events.assets → 品种代码列表（大小写归一）。

    该列口径是「逗号分隔的品种代码」（见 db.normalize_news_assets），
    但历史数据里可能残留门卫标签的 JSON 数组 —— 两种都认，别静默丢行。
    """
    if not raw:
        return []
    text = str(raw).strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            parsed = json.loads(text)
        except (ValueError, TypeError):
            parsed = []
        if isinstance(parsed, list):
            return [str(v).strip().upper() for v in parsed if str(v or "").strip()]
        return []
    return [p.strip().upper() for p in text.split(",") if p.strip()]


def _rget(r, key, default=None):
    """sqlite3.Row 安全取值：列不存在或为 NULL 时返回 default。

    news_events 历史上存在两套 schema（新库用 asset 单数，旧库用 assets 复数），
    不同查询选出的列集合不一致，用它统一兜底。
    """
    try:
        v = r[key]
    except (IndexError, KeyError):
        return default
    return default if v is None else v


def _strip_model_tag(s: str) -> str:
    """剥掉 reasoning 行首的 "[模型名 (标识)]" 前缀。"""
    if not s:
        return ""
    return _MODEL_TAG_RE.sub("", s, count=1).strip()


def _ts_to_str(epoch) -> str:
    """epoch 秒 / ISO 串 → 'YYYY-MM-DD HH:MM:SS' **北京墙钟** 字符串 (前端 ApiEvent 期望格式)

    v320 P0 修复：原来这里渲染成 **UTC** 串，而前端 parseBjTimestamp() 按
    "+08:00" 解读裸时间串（项目约定: 裸串 = 北京时间）→ 所有事件时间被前端
    当成早 8 小时，"今日"时段过滤把当天新闻整段漏掉（表现为 窗口新闻 0 /
    已分析 0 / 今日列表为空）。8000 (api_server) 渲染的就是北京墙钟，
    现在两边口径一致。

    另: ai_decisions.created_at 是 ISO 串（'2026-11-03T07:09:57.797+08:00'），
    原实现 int() 抛异常 → 该字段恒为空（前端"AI 时间"列空白）。现在按
    time_utils 的裸串=北京 口径解析。
    """
    if not epoch:
        return ""
    if isinstance(epoch, str) and not epoch.isdigit():
        try:
            dt = time_utils.parse_ts_to_bj(epoch)
            return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else ""
        except Exception:
            return ""
    try:
        return time_utils.epoch_to_bj_str(int(epoch))
    except Exception:
        return ""

