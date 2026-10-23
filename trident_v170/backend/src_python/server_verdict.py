"""Server-side verdict — 在 LLM 调用前替 AI 算好"水位/价格是否走完/预期反应"等大结论。

设计动机：当前 prompt 让 AI 自己看 RSI/ADX/VWAP/量能这些原始数自己写结论，但模型经常用错
（特别是 M3 fallback）→ 输出含混/编造/过度保守。本模块在 server 端把这些指标直接转成一句
可读的「水位 / 价格状态 / 预期」结论块，塞到 prompt 头部让 AI 引用即可，省 token 也省判断。

输出四个块：
  [SERVER MARKET VERDICT] — 每个品种一句话：水位/价格/趋势
  [SERVER PRICE-DONE?]   — 当前价位相对新闻事件是否已走完（避免追单）
  [SERVER RECENT NEWS]   — 同 target_asset 最近 24h 同类决策时间点（最多 5 条）
  [SERVER EXPECTED]      — 预期短期方向（基于趋势 + 量能 + VWAP 关系）

整个模块：纯函数 + db 查询，IO 错误全部吞掉返回降级块（绝不抛异常阻塞 AI 主循环）。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

BJ = timezone(timedelta(hours=8))

# ── 中文→英文资产标签映射（raw_news.gate_main_asset 是中文：「美股/原油/加密主流/黄金/个股/A股」）
# 设计动机：门卫层 gate 阶段对中文新闻产出的是「美股/原油」这类中文标签，但 server verdict 需要
# 拉取 ai_decisions 历史（按 target_asset 匹配）。统一归一为英文：
# - 美股/港股/中概 → US_STOCK（最常见的股市类目）
# - 个股 → INDIVIDUAL_STOCK
# - A股/沪深/上证/深证 → CN_STOCK
# - 黄金/地缘避险 → XAU
# - 原油/能源 → WTI
# - 加密主流/加密/数字货币 → BTC（兜底取主流币）
# - 汇率/外汇/美元 → US_STOCK（宏观落点）
_CN_ASSET_MAP: Dict[str, str] = {
    "美股": "US_STOCK",
    "港股": "US_STOCK",
    "中概": "US_STOCK",
    "中概互联": "US_STOCK",
    "A股": "CN_STOCK",
    "沪深": "CN_STOCK",
    "上证": "CN_STOCK",
    "深证": "CN_STOCK",
    "创业板": "CN_STOCK",
    "科创板": "CN_STOCK",
    "海力士": "SKHYNIX",
    "sk海力士": "SKHYNIX",
    "韩股": "SKHYNIX",
    "个股": "INDIVIDUAL_STOCK",
    "单股": "INDIVIDUAL_STOCK",
    "黄金": "XAU",
    "金价": "XAU",
    "贵金属": "XAU",
    "避险资产": "XAU",
    # v176.5: 白银/银/铂金/钯金 走 XAU (贵金属类共用代码)
    "白银": "XAU",
    "银价": "XAU",
    "铂金": "XAU",
    "钯金": "XAU",
    "XAG": "XAU",
    "原油": "WTI",
    "油价": "WTI",
    "石油": "WTI",
    "能源": "WTI",
    "OPEC": "WTI",
    "加密主流": "BTC",
    "加密": "BTC",
    "数字货币": "BTC",
    "币圈": "BTC",
    "虚拟货币": "BTC",
    "区块链": "BTC",
    "外汇": "US_STOCK",
    "美元": "US_STOCK",
    "汇率": "US_STOCK",
    "美债": "US_STOCK",
}


def _normalize_target_asset(raw: str) -> str:
    """中文 → 英文 target_asset 归一化。无匹配或空 → 返回原值。"""
    if not raw:
        return ""
    s = str(raw).strip()
    if not s:
        return ""
    up = s.upper()
    # 已是英文目标之一 → 直接返回
    if up in {"XAU", "WTI", "BTC", "ETH", "SOL", "CN_STOCK", "US_STOCK", "INDIVIDUAL_STOCK", "SKHYNIX", "NONE", "OTHER"}:
        return up
    # 中文映射
    if s in _CN_ASSET_MAP:
        return _CN_ASSET_MAP[s]
    # 模糊匹配：包含子串
    for k, v in _CN_ASSET_MAP.items():
        if k in s:
            return v
    # 上层已是 NONE/OTHER/空 → 原样返回
    return up


def _verdict_for_asset(asset_id: str, asset: Dict[str, Any]) -> str:
    """One-line verdict per asset: 水位 + 价格 + 趋势 + 量能.
    v200 P4: 加入 7d 涨跌 + OI 增减字段.
    """
    if not asset or asset.get("status") == "unavailable":
        return f"{asset_id}: 数据不可用"
    price = asset.get("price_str") or "?"
    chg = asset.get("change_24h_str") or "?"
    fr = asset.get("funding_rate_str") or "N/A"
    stats = asset.get("stats_7d") or {}
    trend = stats.get("trend") or "?"
    trend_str = stats.get("trend_strength") or "?"
    chg_7d_str = stats.get("return_7d_str") or ""  # v200 P4: 7d 涨跌
    oi_chg_pct = asset.get("oi_change_pct")  # v200 P4: OI 环比 %
    rsi_str = ""
    vwap_str = ""
    vol_str = ""
    mtf = asset.get("mtf") or {}
    s1 = (mtf.get("1h") or {}).get("indicators") or {}
    if s1.get("rsi") is not None:
        rsi = s1["rsi"]
        if rsi >= 70:
            rsi_str = "超买"
        elif rsi <= 30:
            rsi_str = "超卖"
        else:
            rsi_str = f"中性{rsi:.0f}"
    if s1.get("vwap_pos"):
        vwap_str = "VWAP上" if s1["vwap_pos"] == "above" else "VWAP下"
    if s1.get("volume_ratio") is not None:
        vr = s1["volume_ratio"]
        if vr >= 1.5:
            vol_str = f"放量{vr:.1f}x"
        elif vr <= 0.7:
            vol_str = f"缩量{vr:.1f}x"
        else:
            vol_str = f"存量{vr:.1f}x"
    parts = [f"{price}", f"24h{chg}"]
    # v200 P4: 7d 涨跌（数据缺失时跳过，不造数）
    if chg_7d_str:
        parts.append(f"7d{chg_7d_str}")
    parts.append(f"日线{trend}({trend_str})")
    if rsi_str:
        parts.append(f"1H RSI {rsi_str}")
    if vwap_str:
        parts.append(vwap_str)
    if vol_str:
        parts.append(vol_str)
    parts.append(f"费率{fr}")
    # v200 P4: OI 增减（≥±0.5% 才显示，避免 0.0% 噪音）
    if oi_chg_pct is not None and abs(oi_chg_pct) >= 0.5:
        if oi_chg_pct > 0.5:
            parts.append(f"OI↑{oi_chg_pct:+.1f}%")
        else:
            parts.append(f"OI↓{oi_chg_pct:+.1f}%")
    return f"{asset_id} " + " · ".join(parts)


def _parse_news_ts(news_ts: str) -> Optional[int]:
    """把 news_ts(ISO string)解析成 epoch 毫秒;解析失败返回 None。"""
    if not news_ts:
        return None
    try:
        s = news_ts.strip()
        # 兼容 '2026-09-21 02:07:43' 与 '2026-09-21T02:07:43+08:00' 两种格式
        if "T" not in s and " " in s:
            s = s.replace(" ", "T", 1)
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=BJ)
        return int(dt.timestamp() * 1000)
    except Exception:
        return None


def _price_at_ts(asset_id: str, ts_ms: int, now_ms: int) -> Optional[Tuple[float, str]]:
    """查 market_ticks / kline_bars 拿指定时刻(ts_ms)前后 5 分钟内的最接近价格。

    返回 (price, source);source = 'tick' | 'kline' | None
    优先级：market_ticks(分钟级 tick) → kline_bars 15m(快照兜底)。

    重要保护:
      - market_ticks.ts 是「秒」,kline_bars.ts 是「毫秒」,所以两个分支分别处理。
      - 距 now_ms 超过 48h 的价视为过期,绝不返回（避免 WTI/XAU 无 tick 时错用 7 天前 K 线）。
      - 距 now_ms 超过 6h 的 K 线返回时附带 `stale` 标记,让 AI 知道对比不可靠。
    """
    if ts_ms is None:
        return None
    # ── 距现在太久 → 视为过期,直接拒 ──
    # news 已发布 > 48h:不应还在算 price-in,这条 AI 决策早就该 HOLD
    age_h = (now_ms - ts_ms) / (1000.0 * 3600.0) if ts_ms <= now_ms else 0.0
    if age_h > 48.0:
        return None
    try:
        from db import get_connection
        conn = get_connection()
    except Exception:
        return None
    try:
        # 1) market_ticks：ts 是「秒」,±5min = ±300s
        rows = conn.execute(
            """
            SELECT price, ts FROM market_ticks
            WHERE symbol = ? AND ts BETWEEN ? AND ?
            ORDER BY ABS(ts - ?) LIMIT 1
            """,
            (asset_id, int(ts_ms / 1000) - 300, int(ts_ms / 1000) + 300, int(ts_ms / 1000)),
        ).fetchone()
        if rows and rows[0] is not None:
            return (float(rows[0]), "tick")
        # 2) kline_bars：按 1h → 4h → 15m 顺序找 ts ≤ 目标最近的 K 线
        # （Yahoo `60m` 只支持 1h,不支持 15m,所以 WTI 15m 经常断流；多 timeframe fallback 兜住）
        for tf, max_stale_h in (("1h", 6.0), ("4h", 24.0), ("15m", 6.0)):
            rows = conn.execute(
                """
                SELECT close, ts FROM kline_bars
                WHERE symbol = ? AND timeframe = ? AND ts <= ?
                ORDER BY ts DESC LIMIT 1
                """,
                (asset_id, tf, ts_ms),
            ).fetchone()
            if not rows or rows[0] is None:
                continue
            kline_age_h = (now_ms - rows[1]) / (1000.0 * 3600.0)
            # 超过该 timeframe 容忍度（1h/15m 用 6h,4h 用 24h）才标 stale
            if kline_age_h > max_stale_h:
                # 继续尝试下一 timeframe，看有没有更近的
                continue
            tag = f"kline-{tf}"
            return (float(rows[0]), tag)
        # 全部 timeframe 都没找到或都太老 → 返回 None（让外层说"price-in 不可评估"）
        return None
    except Exception:
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _price_done_for_asset(asset_id: str, asset: Dict[str, Any], news_ts: str) -> str:
    """判断「价格是否走完」：基于 (a) 新闻发布时刻 vs 当前快照价的对比 + (b) 多周期趋势。

    输出格式：优先返回「[新闻发布价 ± 涨跌%] + [现在价 ± 区间位置] + [结论]」一句话。
    让 AI 直接读出 price-in 程度（发布前是否已涨/发布后跟了多少）。

    判定逻辑（无 AI，纯阈值）：
      - news_ts 缺失 → 退回到原 7d 区间 + 趋势判断（保持兼容）
      - news_ts 时刻价 P_news 不可查 → 输出'price-in 不可评估（无发布时刻价）'
      - 当前价 P_now 与 P_news 对比：涨跌 < ±0.3% → '即时性低,price-in 未确认';
        ±0.3~1.5% → '部分 price-in';> ±1.5% + 多周期趋势一致 → 'price-in 完成';
        > ±1.5% + 1h 反向 → 'price-in 已超量（短周期反向）'
      - 多周期趋势支撑：1h/4h/1d 一致 + 量能 ≥1.5x → 叠加'异动进行中'
    """
    if not asset or asset.get("status") == "unavailable":
        return f"{asset_id}: 数据缺失（N/A），无法判断是否走完"

    news_ms = _parse_news_ts(news_ts)
    price_now = asset.get("price", 0)
    stats = asset.get("stats_7d") or {}
    mtf = asset.get("mtf") or {}
    trend_1d = stats.get("trend", "Ranging")
    s_1h = mtf.get("1h") or {}
    s_4h = mtf.get("4h") or {}
    trend_1h = s_1h.get("trend", "Ranging")
    trend_4h = s_4h.get("trend", "Ranging")
    vol_1h = (s_1h.get("indicators") or {}).get("volume_ratio")
    hi_7d = stats.get("highest_7d", 0)
    lo_7d = stats.get("lowest_7d", 0)

    # ── 区间位置（无论 news_ts 是否可用，都算） ──
    if hi_7d and lo_7d and hi_7d > lo_7d:
        if price_now >= hi_7d - (hi_7d - lo_7d) * 0.2:
            range_pos = "区间顶部"
        elif price_now <= lo_7d + (hi_7d - lo_7d) * 0.2:
            range_pos = "区间底部"
        else:
            range_pos = "区间中部"
    else:
        range_pos = "?"

    aligned = (trend_1d == trend_1h == trend_4h) and trend_1d not in ("Ranging", "?")
    vol_high = vol_1h is not None and vol_1h >= 1.5
    divergent = trend_1d != trend_1h and trend_1h not in ("Ranging", "?")

    # ── 主路径：news_ts 可解析 → 查发布时刻价做对比 ──
    if news_ms is not None:
        now_ms = int(datetime.now(BJ).timestamp() * 1000)
        snap = _price_at_ts(asset_id, news_ms, now_ms)
        if snap is not None:
            p_news, src = snap
            # stale K 线（>6h 前）→ 标"数据陈旧"，但仍告知 AI「这就是库内最近的参考」
            is_stale = src.startswith("kline-stale")
            src_tag = f"({src})"
            if p_news > 0:
                chg_pct = (price_now - p_news) / p_news * 100.0
                abs_chg = abs(chg_pct)
                # stale 时不给具体"已跟 X%"，改为"对比不可靠"
                if is_stale:
                    timing = f"发布价陈旧（{src}），price-in 不可靠"
                else:
                    # 1.5% 阈值与"显著异动"对齐（crypto/黄金/原油日内 1.5% 已算大波动）
                    if abs_chg < 0.3:
                        timing = "即时性低（发布后 <0.3% 跟单）"
                    elif abs_chg < 1.5:
                        timing = "部分 price-in（已跟 0.3~1.5%）"
                    else:
                        # 大幅 price-in 时叠加多周期判断
                        if divergent:
                            timing = "price-in 已超量（短周期反向）"
                        else:
                            timing = "price-in 完成（已跟 >1.5%）"
                # 趋势 / 量能叠加
                extra = ""
                if aligned and vol_high:
                    extra = f" + 多周期一致+1H 量能{vol_1h:.1f}x"
                elif divergent:
                    extra = f" + 1H 反向于 {trend_1d}"
                return (
                    f"{asset_id}: 发布{src_tag}${p_news:.4g} → "
                    f"现${price_now:.4g}({chg_pct:+.2f}%, {range_pos}); "
                    f"{timing}{extra}"
                )
        # news_ms 可解析但查不到价 → 明确告知,不偷偷退回
        return (
            f"{asset_id}: price-in 不可评估（无发布时刻价，现 ${price_now:.4g}, {range_pos}）"
        )

    # ── 退路：news_ts 缺失 → 用原逻辑（兼容历史 AI 调用） ──
    if aligned and vol_high:
        return f"{asset_id}: 异动进行中（日/4h/1h 趋势一致 + 1H 量能{vol_1h:.1f}x）"
    if divergent:
        return f"{asset_id}: 走势可能见顶/底（1H 反向于 {trend_1d}）"
    if range_pos in ("区间顶部", "区间底部") and not vol_high:
        return f"{asset_id}: 价格已走完（7d {range_pos} + 无新增放量）"
    return f"{asset_id}: 价格尚未充分反应（{range_pos}，1H 趋势 {trend_1h}）"


def _recent_news(conn: sqlite3.Connection, target_asset: str, current_id: int,
                 hours: int = 24, limit: int = 5) -> List[Tuple[str, str]]:
    """同 target_asset 最近 24h 已分析决策（target_asset 匹配 + 排除当前）。

    ai_decisions 表实际时间列是 created_at（DB 自动写入），event_ts 不存在。
    """
    if not target_asset or target_asset.upper() in ("NONE", "OTHER", ""):
        return []
    try:
        cutoff = (datetime.now(BJ) - timedelta(hours=hours)).isoformat(timespec="seconds")
        rows = conn.execute(
            """
            SELECT created_at, target_asset FROM ai_decisions
            WHERE target_asset = ? AND id < ?
              AND created_at >= ?
              AND reasoning IS NOT NULL AND length(reasoning) > 0
            ORDER BY created_at DESC LIMIT ?
            """,
            (target_asset.upper(), int(current_id), cutoff, int(limit)),
        ).fetchall()
        out: List[Tuple[str, str]] = []
        for r in rows:
            ts = r[0] if len(r) >= 1 else None
            ta = r[1] if len(r) >= 2 else ""
            if not ts:
                continue
            out.append((str(ts), str(ta or "")))
        return out
    except Exception:
        return []


def _format_recent(news: List[Tuple[str, str]], target_asset: str) -> str:
    if not news:
        return f"（{target_asset or 'N/A'} 最近 24h 无相关历史决策）"
    lines = []
    for row in news[:5]:
        # 兼容 sqlite3.Row / tuple / list — 全部按 [0],[1] 取值
        try:
            ts = row[0] if row is not None and len(row) >= 1 else ""
            ta = row[1] if row is not None and len(row) >= 2 else ""
        except Exception:
            continue
        ts = str(ts) if ts is not None else ""
        ta = str(ta) if ta is not None else ""
        ts_short = ts[:16].replace("T", " ") if ts else "?"
        same_or_diff = "（同标）" if (ta or "").upper() == (target_asset or "").upper() else f"（副标 {ta}）"
        lines.append(f"  · {ts_short} {same_or_diff}")
    return "\n".join(lines)


def build_verdict(snap: Optional[Dict[str, Any]],
                  news_ts: str = "",
                  target_asset: str = "",
                  current_id: int = 0,
                  connection: Optional[sqlite3.Connection] = None) -> str:
    """主入口：拼出 [SERVER VERDICT] 块（直接拼到 prompt 头部）。

    snap: market_snapshot.get_snapshot() 的返回（含 assets / macro / summary / status）
          None 或 status=down 时输出降级块。
    news_ts: 当前新闻入库时间（北京时间 ISO），用于相关性显示
    target_asset: 当前新闻的主品种（用于拉同标的近期历史决策）
    """
    # 中文→英文归一（raw_news.gate_main_asset 是中文「美股/原油」类标签）
    target_asset = _normalize_target_asset(target_asset)
    lines: List[str] = []

    # ── 1. SERVER MARKET VERDICT ────────────────────────────────────────────
    lines.append("═══ SERVER MARKET VERDICT（server 已算好，AI 直接引用，不要重算）═══")

    if not snap or snap.get("status") == "down":
        lines.append("⚠️ 行情快照不可用 — 所有品种 price/RSI/ADX/VWAP N/A。")
        lines.append("  → AI 必须按 N/A 处理，禁止凭印象/常识填充价位或水位。")
        lines.append("  → 宁可 HOLD 并在 reasoning 写 '数据缺失无法判断'，不得编造价格。")
    else:
        assets = snap.get("assets") or {}
        for asset_id in ("BTC", "XAU", "WTI", "ETH"):
            a = assets.get(asset_id)
            lines.append("  " + _verdict_for_asset(asset_id, a if a else {}))

        # ── 2. SERVER PRICE-DONE? ───────────────────────────────────────────
        lines.append("")
        lines.append("═══ SERVER PRICE-DONE?（新闻发布后该品种价格是否已走完）═══")
        for asset_id in ("BTC", "XAU", "WTI", "ETH"):
            a = (snap.get("assets") or {}).get(asset_id) or {}
            lines.append("  " + _price_done_for_asset(asset_id, a, news_ts))

    # ── 3. SERVER RECENT NEWS（同标最近 24h） ───────────────────────────────
    lines.append("")
    lines.append(f"═══ SERVER RECENT NEWS（{target_asset or 'N/A'} 最近 24h 已分析决策，最多 5 条）═══")
    owned = connection is None
    conn = connection
    try:
        if conn is None:
            from db import get_connection
            conn = get_connection()
        news = _recent_news(conn, target_asset, current_id, hours=24, limit=5)
        lines.append(_format_recent(news, target_asset))
    except Exception as e:
        lines.append(f"（数据库查询失败：{type(e).__name__}，无相关历史）")
    finally:
        if owned and conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    # ── 4. SERVER EXPECTED（基于趋势 + 量能 + VWAP 派生方向预期） ─────────
    lines.append("")
    lines.append("═══ SERVER EXPECTED（基于水位/趋势/量能派生，AI 可采纳或反对）═══")
    if snap and snap.get("status") != "down":
        for asset_id in ("BTC", "XAU", "WTI", "ETH"):
            a = (snap.get("assets") or {}).get(asset_id) or {}
            if a.get("status") == "unavailable":
                lines.append(f"  {asset_id}: N/A（数据缺失）")
                continue
            stats = a.get("stats_7d") or {}
            mtf = a.get("mtf") or {}
            t_1d = stats.get("trend", "Ranging")
            t_1h = (mtf.get("1h") or {}).get("trend", "Ranging")
            s1 = (mtf.get("1h") or {}).get("indicators") or {}
            rsi = s1.get("rsi")
            vwap_pos = s1.get("vwap_pos")
            vol = s1.get("volume_ratio")
            fr = a.get("funding_rate_pct", 0.0)
            exp: List[str] = []
            if t_1d in ("Strong Bull", "Bull") and t_1h in ("Strong Bull", "Bull", "Mild Bull"):
                exp.append("短期顺势偏多")
            elif t_1d in ("Strong Bear", "Bear") and t_1h in ("Strong Bear", "Bear", "Mild Bear"):
                exp.append("短期顺势偏空")
            else:
                exp.append("短期震荡/分歧")
            if rsi is not None and rsi >= 70:
                exp.append("RSI超买警惕回调")
            elif rsi is not None and rsi <= 30:
                exp.append("RSI超卖警惕反弹")
            if vwap_pos == "above":
                exp.append("价在VWAP上偏强")
            elif vwap_pos == "below":
                exp.append("价在VWAP下偏弱")
            if vol is not None and vol >= 1.5:
                exp.append(f"放量{vol:.1f}x")
            if fr is not None and fr > 0.01:
                exp.append(f"费率{fr:+.3f}%偏拥挤")
            elif fr is not None and fr < -0.01:
                exp.append(f"费率{fr:+.3f}%偏空拥挤")
            if exp:
                lines.append(f"  {asset_id}: " + " · ".join(exp))
            else:
                lines.append(f"  {asset_id}: 无明显预期信号")
    else:
        lines.append("  ⚠️ 数据不可用，预期方向 N/A。")

    # ════════════════════════════════════════════════════════════════
    # v200 P4: 跨品种全维度对比表（4品种×5指标）
    # 让 AI 在 [反共识结论] 优先引用此表做"单品种 vs 多品种"对比
    # ════════════════════════════════════════════════════════════════
    lines.append("")
    lines.append("═══ SERVER CROSS-ASSET COMPARE（4品种×5指标 对比表，AI 优先用于[反共识结论]）═══")
    if snap and snap.get("status") != "down":
        lines.append("  品种     24h      7d         费率         OI变动    1H RSI")
        for asset_id in ("BTC", "ETH", "XAU", "WTI"):
            a = (snap.get("assets") or {}).get(asset_id) or {}
            if a.get("status") == "unavailable":
                lines.append(f"  {asset_id:<8} 数据不可用")
                continue
            chg_24h = a.get("change_24h_str") or "?"
            chg_7d = (a.get("stats_7d") or {}).get("return_7d_str") or "?"
            fr = a.get("funding_rate_str") or "N/A"
            oi_chg = a.get("oi_change_pct")
            oi_str = f"{oi_chg:+.1f}%" if oi_chg is not None else "N/A"
            rsi_indicators = ((a.get("mtf") or {}).get("1h") or {}).get("indicators") or {}
            rsi_v = rsi_indicators.get("rsi")
            if rsi_v is not None:
                if rsi_v >= 70:
                    rsi_str = f"{rsi_v:.0f}超买"
                elif rsi_v <= 30:
                    rsi_str = f"{rsi_v:.0f}超卖"
                else:
                    rsi_str = f"{rsi_v:.0f}"
            else:
                rsi_str = "N/A"
            lines.append(
                f"  {asset_id:<8} {chg_24h:>8} {chg_7d:>8} {fr:>10} {oi_str:>8} {rsi_str:>8}"
            )
    else:
        lines.append("  ⚠️ 数据不可用")

    return "\n".join(lines)

    if rsi_str:
        parts.append(f"1H RSI {rsi_str}")
    if vwap_str:
        parts.append(vwap_str)
    if vol_str:
        parts.append(vol_str)
    parts.append(f"费率{fr}")
    return f"{asset_id} " + " · ".join(parts)


# ════════════════════════════════════════════════════════════════
# v200 P4: 公共 API —— 任意模块可调（system-wide MarketDataService）
# ════════════════════════════════════════════════════════════════


def get_snapshot_fresh_or_cached(max_age_s: float = 60.0) -> Dict[str, Any]:
    """获取最新可用 snap dict（优先从 ai_worker 模块级缓存读，过期则新拉）。
    返回的 dict 是 market_snapshot.get_snapshot() 的标准输出（assets/macro/summary/status）。
    """
    import time as _time
    import json as _json
    now_ts = _time.time()
    # 1) 尝试从 ai_worker 模块级缓存读
    try:
        import engine.ai_worker as _aw
        cache = getattr(_aw, "_SERVER_SNAPSHOT_CACHE", None) or {}
        cache_ts = float(cache.get("ts", 0.0))
        cache_json = cache.get("json")
        if cache_json and (now_ts - cache_ts) <= max_age_s:
            if isinstance(cache_json, str):
                try:
                    return _json.loads(cache_json)
                except Exception:
                    pass
            elif isinstance(cache_json, dict):
                return cache_json
    except Exception:
        # engine 模块未加载（独立进程/单测场景）→ fallback 走同步快照
        pass
    # 2) 缓存不可用 / 过期 → 调同步快照
    try:
        from market_snapshot import get_snapshot_sync
        return get_snapshot_sync()
    except Exception:
        return {"status": "down", "assets": {}, "macro": {}, "summary": "", "ts": now_ts}


def get_verdict_for_asset(asset_id: str, snap: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """返回某品种的完整 verdict 字典（结构化字段，便于其他模块直接读取）。

    示例返回值:
        {
            "asset_id": "BTC", "status": "ok", "price": 111234.5,
            "price_str": "$111234.50", "change_24h_pct": 1.23, "change_24h_str": "+1.23%",
            "change_7d_str": "+3.45%", "rsi_1h": 55.0, "rsi_1h_status": "中性",
            "vwap_pos": "above", "volume_ratio": 1.6, "funding_rate_pct": 0.0053,
            "oi_change_pct": 2.1, "trend_1d": "Bull", "trend_1h": "Mild Bull",
            "range_position": "区间中部",
        }
    """
    if snap is None:
        snap = get_snapshot_fresh_or_cached()
    assets = (snap or {}).get("assets") or {}
    asset = assets.get(asset_id) or {}
    result: Dict[str, Any] = {
        "asset_id": asset_id,
        "status": asset.get("status") or ("down" if (snap or {}).get("status") == "down" else "unknown"),
        "price": asset.get("price", 0.0),
        "price_str": asset.get("price_str") or "N/A",
        "change_24h_pct": asset.get("change_24h_pct", 0.0),
        "change_24h_str": asset.get("change_24h_str") or "N/A",
        "change_7d_str": (asset.get("stats_7d") or {}).get("return_7d_str") or "",
        "rsi_1h": ((asset.get("mtf") or {}).get("1h") or {}).get("indicators", {}).get("rsi"),
        "vwap_pos": ((asset.get("mtf") or {}).get("1h") or {}).get("indicators", {}).get("vwap_pos"),
        "volume_ratio": ((asset.get("mtf") or {}).get("1h") or {}).get("indicators", {}).get("volume_ratio"),
        "funding_rate_pct": asset.get("funding_rate_pct", 0.0),
        "oi_change_pct": asset.get("oi_change_pct"),
        "trend_1d": (asset.get("stats_7d") or {}).get("trend"),
        "trend_1h": ((asset.get("mtf") or {}).get("1h") or {}).get("trend"),
    }
    rsi = result["rsi_1h"]
    if rsi is None:
        result["rsi_1h_status"] = "N/A"
    elif rsi >= 70:
        result["rsi_1h_status"] = "超买"
    elif rsi <= 30:
        result["rsi_1h_status"] = "超卖"
    else:
        result["rsi_1h_status"] = "中性"
    stats = asset.get("stats_7d") or {}
    hi = stats.get("highest_7d", 0)
    lo = stats.get("lowest_7d", 0)
    price = result["price"] or 0
    if hi and lo and hi > lo:
        if price >= hi - (hi - lo) * 0.2:
            result["range_position"] = "区间顶部"
        elif price <= lo + (hi - lo) * 0.2:
            result["range_position"] = "区间底部"
        else:
            result["range_position"] = "区间中部"
    else:
        result["range_position"] = "N/A"
    return result


def get_cross_asset_table(snap: Optional[Dict[str, Any]] = None) -> str:
    """返回跨品种全维度对比表（markdown 字符串，4品种×5指标）。
    等价于 build_verdict() 输出中 "═══ SERVER CROSS-ASSET COMPARE ═══" 块。
    """
    if snap is None:
        snap = get_snapshot_fresh_or_cached()
    lines: List[str] = []
    lines.append("═══ SERVER CROSS-ASSET COMPARE（4品种×5指标 对比表）═══")
    if snap and snap.get("status") != "down":
        lines.append("  品种     24h      7d         费率         OI变动    1H RSI")
        for asset_id in ("BTC", "ETH", "XAU", "WTI"):
            v = get_verdict_for_asset(asset_id, snap)
            if v["status"] == "unavailable":
                lines.append(f"  {asset_id:<8} 数据不可用")
                continue
            rsi_v = v["rsi_1h"]
            if rsi_v is None:
                rsi_str = "N/A"
            elif rsi_v >= 70:
                rsi_str = f"{rsi_v:.0f}超买"
            elif rsi_v <= 30:
                rsi_str = f"{rsi_v:.0f}超卖"
            else:
                rsi_str = f"{rsi_v:.0f}"
            oi_chg = v["oi_change_pct"]
            if oi_chg is None:
                oi_str = "N/A"
            elif abs(oi_chg) < 0.5:
                oi_str = "N/A"  # <0.5% 视为噪音
            else:
                oi_str = f"{oi_chg:+.1f}%"
            chg_7d = v["change_7d_str"] or "?"
            fr = f"{v['funding_rate_pct']:+.4f}%" if v["funding_rate_pct"] else "N/A"
            lines.append(
                f"  {asset_id:<8} {v['change_24h_str']:>8} {chg_7d:>8} {fr:>10} {oi_str:>8} {rsi_str:>8}"
            )
    else:
        lines.append("  ⚠️ 数据不可用")
    return "\n".join(lines)


def get_full_verdict_block(news_ts: str = "",
 target_asset: str = "",
                           current_id: int = 0,
                           connection: Optional[sqlite3.Connection] = None,
                           snap: Optional[Dict[str, Any]] = None) -> str:
    """返回完整 SERVER VERDICT 文本块（5 段：MARKET VERDICT + PRICE-DONE + RECENT NEWS +
    EXPECTED + CROSS-ASSET COMPARE）。等价于 build_verdict() 但加了 P4 跨品种对比段。
    任意模块调用此函数即可获得完整 verdict 文本。
    """
    if snap is None:
        snap = get_snapshot_fresh_or_cached()
    main_block = build_verdict(snap, news_ts=news_ts, target_asset=target_asset,
                               current_id=current_id, connection=connection)
    compare_block = get_cross_asset_table(snap)
    return main_block + "\n\n" + compare_block
