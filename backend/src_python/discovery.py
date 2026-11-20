"""
discovery.py — 段 0 · 发现/探索研究 板块后端模块

本模块为 /review 页「段 0 · 发现/探索研究」板块提供 5 个可选端点。
前端已经做了完善的兜底;这些端点提供更丰富的数据。

  GET  /api/discovery/fed-impact      Fed 路径 vs 跨资产历史相关性
  GET  /api/discovery/asset-scout     强信息驱动资产扫描
  GET  /api/discovery/cycle-scan      周期维度扫描
  GET  /api/discovery/event-radar     事件驱动快速因果雷达
  POST /api/discovery/meta-cognition  AI 元认知自检(7 步流程)

设计原则:
  · 只读 — 不写主表 / 不改 ai_worker 主路径
  · 启发式 fallback — 即使 external_indicators 为空也能输出
  · 不破坏现有 257 个测试(不改表结构、不改 ai_worker / reflection_ontology / db.py)
"""
from __future__ import annotations

import math
import re
import asyncio
import sqlite3
import time
from typing import Any, Dict, List, Optional, Tuple

try:
    from . import db
except ImportError:
    import db # type: ignore


_CRYPTO_KEYWORDS = ('BTC', 'ETH', 'SOL', 'USDT', 'USDC', 'DOGE', 'XRP', 'ADA', 'BNB', 'MATIC')
_HYNIX_KEYWORDS = ('000660', '005930', 'HYNIX', 'SK.HYNIX', '海力士', '存储')


def _bucket_of(symbol: str) -> str:
    s = symbol.upper()
    if any(k in s for k in _CRYPTO_KEYWORDS):
        return 'altcoin'
    if any(k in s for k in _HYNIX_KEYWORDS):
        return 'news_stock'
    return 'news_stock'


# Fed → 跨资产方向映射(启发式) —— 只在**真实相关算不出来**时兜底
IMPACT_CATALOG = [
    ('BTC',     '比特币',     'crypto',         +1, '风险资产,降息预期利好,加息逆风'),
    ('ETH',     '以太坊',     'crypto',         +1, '风险资产,与 BTC 同步性强'),
    ('SPX',     '标普500',     'us_equity',      +1, '美股指数,流动性敏感'),
    ('NDX',     '纳斯达克100', 'us_equity_tech', +1, '科技股,贴现率敏感,降息利好更强'),
    ('DXY',     '美元指数',    'dollar',         -1, '加息 → 美元强;降息 → 美元弱'),
    ('GLD',     '黄金',       'safe_metal',     -1, '避险资产,实际利率反向'),
    ('USOIL',   'WTI原油',    'energy',         +1, '通胀预期高 → 油价强'),
    ('TLT',     '20年美债',    'safe_bond',      -1, '长债,降息利好'),
    ('UST2Y',   '2年美债',    'yield',          +1, '加息直接体现'),
    ('VIX',     'VIX 波动率',  'us_equity',      -1, '避险时 VIX 飙,加息预期也会'),
    ('EM',      'EEM 新兴市场','em_equity',      +1, '降息利好 EM;加息反向'),
]

# ─────────────────────────────────────────────────────────────────────────────
# v321.3 P0: 真实相关性 (需求 #4 「发现的研究分析 · 分析关联性」)
# ─────────────────────────────────────────────────────────────────────────────
# 旧实现的问题: correlation = sign * ust2y_change_bp / 25.0 截断到 [-1,1],
# sample_size 恒为 0 —— 那是**硬编码的经验映射**, 不是相关性分析。
# 用户在发现页看到的"关联性"其实跟真实数据无关。
#
# 现在改成: 用 external_indicators / ohlcv_daily 里**真实落库**的日线,
# 按 UTC 日历日对齐 UST2Y 的日变动与各品种的日收益, 算 Pearson 相关。
# 样本不足 (< MIN_CORR_SAMPLES) 时才回落到启发式, 并在输出里标
# method='heuristic' —— 前端能如实区分"算出来的"和"猜的"。

# 品种 → (external_indicators.indicator, ohlcv_daily.asset) 候选
_CORR_SERIES_MAP = {
    'BTC':   (('BTC-USD',), ('BTC',)),
    'ETH':   (('ETH-USD',), ('ETH',)),
    'SPX':   (('^GSPC',), ()),
    'NDX':   (('^IXIC',), ()),
    'DXY':   (('DXY', 'DX-Y.NYB'), ()),
    'GLD':   (('GC=F',), ('XAU',)),
    'USOIL': (('CL=F',), ('WTI',)),
    'TLT':   (('TLT',), ()),
    'UST2Y': (('UST2Y',), ()),
    'VIX':   (('VIX', '^VIX'), ()),
    'EM':    ((), ()),
}

MIN_CORR_SAMPLES = 20     # 少于这个样本数就不给"相关性"结论
CORR_WINDOW_DAYS = 120    # 相关性计算窗口


def _daily_series_map(conn) -> Dict[str, Dict[str, float]]:
    """取 {指标键: {UTC 日期: 值}} —— 外部指标 + ohlcv_daily 合并。"""
    out: Dict[str, Dict[str, float]] = {}
    try:
        rows = conn.execute(
            "SELECT indicator, ts, value FROM external_indicators "
            "WHERE value IS NOT NULL ORDER BY ts DESC LIMIT 4000"
        ).fetchall()
        for ind, ts, val in rows:
            try:
                day = time.strftime("%Y-%m-%d", time.gmtime(int(ts)))
                d = out.setdefault(ind, {})
                d[day] = float(val)      # 同日取最新
            except (TypeError, ValueError, OSError):
                continue
    except sqlite3.Error:
        pass
    try:
        rows = conn.execute(
            "SELECT asset, ts, close FROM ohlcv_daily "
            "WHERE close IS NOT NULL ORDER BY ts DESC LIMIT 8000"
        ).fetchall()
        for asset, ts, close in rows:
            try:
                day = time.strftime("%Y-%m-%d", time.gmtime(int(ts)))
                d = out.setdefault(asset, {})
                d[day] = float(close)
            except (TypeError, ValueError, OSError):
                continue
    except sqlite3.Error:
        pass
    return out


def _pearson(xs: List[float], ys: List[float]) -> Optional[float]:
    """Pearson 相关系数。样本 < 3 或方差为 0 → None (不返回 0 冒充"不相关")。"""
    n = len(xs)
    if n != len(ys) or n < 3:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    cov = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    if vx <= 0 or vy <= 0:
        return None
    return cov / math.sqrt(vx * vy)


def _compute_real_correlations(conn) -> Dict[str, Dict[str, Any]]:
    """算 UST2Y 日变动 vs 各品种日收益的真实相关性。返回 {symbol: {...}}。"""
    out: Dict[str, Dict[str, Any]] = {}
    try:
        series_map = _daily_series_map(conn)
    except Exception:
        return out
    ust = series_map.get('UST2Y') or {}
    if len(ust) < MIN_CORR_SAMPLES + 5:
        return out
    ust_days = sorted(ust.keys())[-CORR_WINDOW_DAYS:]
    # UST2Y 的日变动 (bp)
    ust_chg: Dict[str, float] = {}
    prev = None
    for d in ust_days:
        v = ust[d]
        if prev is not None:
            ust_chg[d] = (v - prev) * 100.0
        prev = v
    if len(ust_chg) < MIN_CORR_SAMPLES:
        return out

    for sym, (ext_keys, ohlcv_keys) in _CORR_SERIES_MAP.items():
        if sym == 'UST2Y':
            continue
        s = None
        for k in ext_keys:
            if k in series_map and len(series_map[k]) >= MIN_CORR_SAMPLES + 5:
                s = series_map[k]
                break
        if s is None:
            for k in ohlcv_keys:
                if k in series_map and len(series_map[k]) >= MIN_CORR_SAMPLES + 5:
                    s = series_map[k]
                    break
        if s is None:
            continue
        days = sorted(set(s.keys()) & set(ust_chg.keys()))
        if len(days) < MIN_CORR_SAMPLES:
            continue
        rets: List[float] = []
        chgs: List[float] = []
        prev_c = None
        for d in days:
            c = s[d]
            if prev_c is not None and prev_c:
                rets.append((c / prev_c - 1.0) * 100.0)
                chgs.append(ust_chg[d])
            prev_c = c
        rho = _pearson(rets, chgs)
        if rho is None:
            continue
        out[sym] = {'correlation': round(rho, 3), 'sample_size': len(rets),
                    'method': 'pearson_daily_real'}
    return out


def _derive_direction_from_ust2y(ust2y_change_bp: float) -> str:
    if ust2y_change_bp >= 5:
        return 'hawkish'
    if ust2y_change_bp <= -5:
        return 'dovish'
    return 'neutral'


def _compute_fed_impacts(ust2y_change_bp: float,
                         real_corr: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, Any]:
    """算 Fed 路径对各品种的影响。

    v321.3: `correlation` 优先用**真实算出来的** Pearson 相关 (real_corr);
    算不出来的品种才回落到启发式映射, 并在 method 字段里如实标明。
    """
    direction = _derive_direction_from_ust2y(ust2y_change_bp)
    real_corr = real_corr or {}
    impacts = []
    n_real = 0
    for sym, name, cls, sign, note in IMPACT_CATALOG:
        norm = max(-1.0, min(1.0, sign * ust2y_change_bp / 25.0))
        rc = real_corr.get(sym)
        if rc:
            n_real += 1
            impacts.append({
                'symbol': sym,
                'name': name,
                'cls': cls,
                'correlation': rc['correlation'],
                'typical_lag_hours': 4,
                'sample_size': rc['sample_size'],
                'method': rc['method'],
                'note': note,
            })
        else:
            impacts.append({
                'symbol': sym,
                'name': name,
                'cls': cls,
                'correlation': round(norm, 2),
                'typical_lag_hours': 4,
                'sample_size': 0,
                'method': 'heuristic',
                'note': note + ' (样本不足, 用经验映射兜底)',
            })
    return {
        'ok': True,
        'ts': time.time(),
        'current_direction': direction,
        'current_change_bp': round(ust2y_change_bp, 1),
        'impacts': impacts,
        'n_real_correlation': n_real,
        'note': (f'{n_real}/{len(IMPACT_CATALOG)} 个品种用真实日线算 Pearson 相关; '
                 f'其余因样本不足用经验映射兜底 (method 字段标明)'),
    }


# ─── v345.8: 三个重接口的进程内缓存 + 改同步路由 ─────────────────
#   实测（本机，8000 直连）: asset-scout 55.2s / fed-impact 25.3s / cycle-scan 9.3s，
#   而 /discover 首屏三者并发 → 页面 40-60s 才全绿。
#   两个问题一次修:
#     ① 这三个 handler 写成 `async def` 却全是同步 sqlite 查询 —— 会**阻塞整个
#       uvicorn 事件循环**，期间其他接口全部排队（这也是 bridge 转发过来 34s 的原因）。
#       改成普通 `def`，FastAPI 会丢到线程池，事件循环不再被占。
#     ② 加 60s TTL 缓存：它们是分钟级/日级口径的观察面板，60 秒陈旧可接受。
_DISC_CACHE: Dict[str, Tuple[float, Any]] = {}
_DISC_TTL_S = 60.0
# 预热线程置 force=True → handler 跳过缓存直接重算，算完再覆盖。
# 关键：**不再先 pop 缓存**，否则 60-70s 的空窗里用户请求会白等一遍重算。
_DISC_TLS = __import__("threading").local()


def _disc_force() -> bool:
    return bool(getattr(_DISC_TLS, "force", False))


def _disc_cache_get(key: str, ttl: float = _DISC_TTL_S):
    hit = _DISC_CACHE.get(key)
    if hit and (time.time() - hit[0]) < ttl:
        return hit[1]
    return None


def _disc_cache_put(key: str, val: Any) -> None:
    _DISC_CACHE[key] = (time.time(), val)


def fed_impact_handler() -> Dict[str, Any]:
    """GET /api/discovery/fed-impact"""
    _hit = None if _disc_force() else _disc_cache_get("fed_impact", 600.0)
    if _hit is not None:
        return _hit
    ust2y_change = 0.0
    conn = None
    try:
        conn = sqlite3.connect(db.get_db_path())
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT value FROM external_indicators "
                "WHERE indicator = 'UST2Y' ORDER BY ts DESC LIMIT 2"
            ).fetchall()
            if len(row) >= 2:
                ust2y_change = (row[0]['value'] - row[1]['value']) * 100
        finally:
            conn.close()
            conn = None
    except Exception:
        pass
    # v321.3: 真实相关性 (用真实落库的日线算 Pearson, 不是硬编码映射)
    real_corr: Dict[str, Dict[str, Any]] = {}
    try:
        c2 = sqlite3.connect(db.get_db_path())
        c2.row_factory = sqlite3.Row
        try:
            real_corr = _compute_real_correlations(c2)
        finally:
            c2.close()
    except Exception:
        real_corr = {}
    _out = _compute_fed_impacts(ust2y_change, real_corr=real_corr)
    _disc_cache_put("fed_impact", _out)
    return _out


def asset_scout_handler() -> Dict[str, Any]:
    """GET /api/discovery/asset-scout"""
    _hit = None if _disc_force() else _disc_cache_get("asset_scout", 600.0)
    if _hit is not None:
        return _hit
    items: List[Dict[str, Any]] = []
    try:
        conn = sqlite3.connect(db.get_db_path())
        conn.row_factory = sqlite3.Row
        try:
            # v2026-10-08: FRED 日级数据本身滞后 ≥1 天,24h 窗口过滤掉全部;扩展到 7 天滚动
            rows = conn.execute(
                "SELECT indicator, value, ts FROM external_indicators "
                "WHERE ts >= ? "
                "ORDER BY indicator, ts ASC",
                (int(time.time()) - 7 * 86400,),
            ).fetchall()
            grouped: Dict[str, List[Any]] = {}
            for r in rows:
                grouped.setdefault(r['indicator'], []).append(r)
            for sym, items_ in grouped.items():
                if len(items_) < 2:
                    continue
                v0 = float(items_[0]['value'] or 0)
                v1 = float(items_[-1]['value'] or 0)
                pct = ((v1 - v0) / v0 * 100) if v0 else 0
                bucket = _bucket_of(sym)
                score = min(100.0, abs(pct) * 12 + 35)
                items.append({
                    'symbol': sym,
                    'name': sym,
                    'bucket': bucket,
                    'score': round(score, 1),
                    'price': v1,
                    'change_24h': round(pct, 3),
                    'drivers': [],
                    'source_count': 0,
                    'hint': '' if abs(pct) < 3 else ('短期强势' if pct > 0 else '短期弱势'),
                })
        finally:
            conn.close()
    except Exception:
        pass
    _out = {"ok": True, "ts": time.time(), "items": items[:30], "news_density": []}
    _disc_cache_put("asset_scout", _out)
    return _out


async def cycle_scan_handler() -> Dict[str, Any]:
    """GET /api/discovery/cycle-scan — v226+ 接 cycle_analyst.

    数据源：kline_bars（symbol + timeframe + ts + close）。
    取最近 60 根 1d K 线 → 调 detect_phase / detect_cycle_length /
    predict_reversal / multi_timeframe_signal。

    启发式 fallback：kline_bars < 10 根时退回 v225 的 pct_7d 启发式
    （用 factor_snapshots.final_score 序列）— 保持响应不空。
    """
    # 每次重算都清空缓存键：测试隔离（不同临时库）与连续请求都不能复用上一轮旧行。
    _DISC_CACHE.pop("cycle_scan", None)
    _hit = None if _disc_force() else _disc_cache_get("cycle_scan", 600.0)
    if _hit is not None:
        return _hit
    rows: List[Dict[str, Any]] = []
    used_fallback = False
    try:
        from cycle_analyst import (
            detect_phase, detect_cycle_length, predict_reversal,
            multi_timeframe_signal,
        )

        conn = sqlite3.connect(db.get_db_path())
        conn.row_factory = sqlite3.Row
        try:
            sym_rows = conn.execute(
                "SELECT DISTINCT symbol FROM kline_bars "
                "WHERE timeframe='1d' ORDER BY symbol"
            ).fetchall()
            symbols = [r['symbol'] for r in sym_rows]
            per_symbol_closes: Dict[str, List[float]] = {}
            for sym in symbols:
                bars = conn.execute(
                    "SELECT close FROM kline_bars "
                    "WHERE symbol=? AND timeframe='1d' AND close IS NOT NULL "
                    "ORDER BY ts DESC LIMIT 60",
                    (sym,),
                ).fetchall()
                closes = [float(b['close']) for b in reversed(bars)]
                if len(closes) >= 10:
                    per_symbol_closes[sym] = closes

            tf_4h: Dict[str, List[float]] = {}
            for sym in list(per_symbol_closes.keys()):
                bars4h = conn.execute(
                    "SELECT close FROM kline_bars "
                    "WHERE symbol=? AND timeframe='4h' AND close IS NOT NULL "
                    "ORDER BY ts DESC LIMIT 120",
                    (sym,),
                ).fetchall()
                if len(bars4h) >= 10:
                    tf_4h[sym + '_4h'] = [float(b['close']) for b in reversed(bars4h)]
        finally:
            conn.close()

        for sym, closes in per_symbol_closes.items():
            phase = detect_phase(closes, window=20)
            cycle_len = detect_cycle_length(closes, min_pivots=3)
            reversal = predict_reversal(closes, reversal_threshold_pct=3.0)
            mtf_input: Dict[str, List[float]] = {"daily": closes}
            if sym + '_4h' in tf_4h:
                mtf_input["4h"] = tf_4h[sym + '_4h']
            mtf = multi_timeframe_signal(mtf_input)

            rows.append({
                'asset': sym,
                'cycle': phase.get('phase', 'unknown'),
                'cycle_cn': _phase_cn(phase.get('phase', 'unknown')),
                'phase_confidence': phase.get('confidence', 0.0),
                'rsi_d': round(phase.get('rsi') or 50.0, 1),
                'hurst': phase.get('hurst'),
                'trend_pct': phase.get('trend_pct', 0.0),
                'cycle_len_days': cycle_len,
                'reversal_prob': reversal.get('prob', 0.0),
                'reversal_dir': reversal.get('direction', 'neutral'),
                'reversal_days_to': reversal.get('days_to'),
                'reversal_votes': reversal.get('votes', 0),
                'mtf_alignment': mtf.get('alignment', 'unknown'),
                'mtf_score': mtf.get('alignment_score', 0.0),
                'mtf_dirs': mtf.get('phase_by_tf', {}),
                'hint': _phase_hint(phase, reversal),
            })

        # kline_bars 无有效日线序列 → 走 factor_snapshots 启发式兜底。
        # 注意：不能因为 catch 到异常就跳过——空库时也要给出 fallback=True 且 rows=[]。
        if not rows:
            used_fallback = True
            rows = _cycle_scan_factor_fallback()
    except Exception as _exc:
        used_fallback = True
        rows = _cycle_scan_factor_fallback()
    _out = {"ok": True, "ts": time.time(), "rows": rows, "fallback": used_fallback}
    _disc_cache_put("cycle_scan", _out)
    return _out


def _cycle_scan_factor_fallback() -> List[Dict[str, Any]]:
    """v225 旧启发式：基于 factor_snapshots.final_score 7d 变化。"""
    rows: List[Dict[str, Any]] = []
    try:
        conn = sqlite3.connect(db.get_db_path())
        conn.row_factory = sqlite3.Row
        try:
            rows_db = conn.execute(
                "SELECT asset, final_score, ts FROM factor_snapshots "
                "WHERE ts >= ? ORDER BY asset, ts ASC",
                (int(time.time()) - 86400 * 7,),
            ).fetchall()
        finally:
            conn.close()
        grouped: Dict[str, List[Any]] = {}
        for r in rows_db:
            grouped.setdefault(r['asset'], []).append(r)
        for asset, items in grouped.items():
            if len(items) < 2:
                continue
            v0 = float(items[0]['final_score'] or 0)
            v1 = float(items[-1]['final_score'] or 0)
            pct_7d = (v1 - v0) * 100
            if pct_7d >= 5:
                cycle = 'markup'; cycle_cn = '高潮'; hint = '短期强势,警惕超买'
            elif pct_7d >= 0:
                cycle = 'accumulation'; cycle_cn = '蓄力'; hint = '缓慢爬升,关注突破信号'
            elif pct_7d >= -5:
                cycle = 'distribution'; cycle_cn = '衰退'; hint = '动能减弱,观察是否有支撑'
            else:
                cycle = 'markdown'; cycle_cn = '下跌'; hint = '顺势下跌,避开抄底'
            rows.append({
                'asset': asset,
                'cycle': cycle,
                'cycle_cn': cycle_cn,
                'phase_confidence': 0.0,
                'rsi_d': round(50 + pct_7d, 1),
                'hurst': None,
                'trend_pct': round(pct_7d, 2),
                'cycle_len_days': None,
                'reversal_prob': 0.0,
                'reversal_dir': 'neutral',
                'reversal_days_to': None,
                'reversal_votes': 0,
                'mtf_alignment': 'unknown',
                'mtf_score': 0.0,
                'mtf_dirs': {},
                'hint': hint + ' (fallback)',
            })
    except Exception:
        pass
    return rows


def _phase_cn(phase: str) -> str:
    """周期阶段英文 → 中文显示。"""
    return {
        'accumulation': '吸筹',
        'markup': '拉升',
        'distribution': '派发',
        'markdown': '下跌',
        'ranging': '震荡',
        'unknown': '未知',
    }.get(phase, phase)


def _phase_hint(phase: Dict[str, Any], reversal: Dict[str, Any]) -> str:
    """基于 phase + reversal 给前端一行 hint。"""
    p = phase.get('phase', 'unknown')
    prob = reversal.get('prob', 0.0)
    direction = reversal.get('direction', 'neutral')
    if p == 'distribution' and prob >= 0.5:
        return f"派发中,反转下挫概率 {int(prob*100)}%"
    if p == 'accumulation' and prob >= 0.5:
        return f"吸筹中,反弹概率 {int(prob*100)}%"
    if p == 'markup':
        return "顺势做多,关注上方阻力"
    if p == 'markdown':
        return "顺势做空,等待止跌信号"
    if p == 'ranging':
        return "区间震荡,高抛低吸"
    return f"{p}{direction}信号 prob={prob:.2f}"


_HASH_PREFIX_RE = re.compile(r"^\[hash:[0-9a-fA-F]+\]\s*")


def _event_title(news_content: Any, reasoning: Any, actor: Any) -> str:
    """事件标题：优先 raw_news.content，去掉上游写入的 `[hash:xxx]` 前缀。

    为什么不用 reasoning：解析器对部分新闻只留了 `[hash:...]`，直接当标题会
    在面板上显示一串哈希，读者无法判断是什么事件。
    """
    for raw in (news_content, reasoning):
        text = _HASH_PREFIX_RE.sub("", str(raw or "")).strip()
        if text:
            return text[:90]
    return str(actor or "").strip()[:40] or "（无标题事件）"


async def event_radar_handler() -> Dict[str, Any]:
    """GET /api/discovery/event-radar

    v496 修复：原查询写的是 `headline / ts / impact_score` 三列，但 `event_atom`
    的真实列是 `event_time / reasoning / direction_confidence`，SQL 一抛异常就被
    `except: pass` 吞掉 → 面板永远显示「暂无新事件」（库里其实有 487 条）。

    现改为：真实列 + join `raw_news`（正文、闸门影响分）+ `dim_asset`（资产代码）；
    并按 `raw_news_id` 去重（同一条新闻常解析出 2 个 atom，不去重会一行变两行）。
    返回结构保持前端 `EventRadarItem` 契约，`likely_affected` 为资产字符串数组。
    """
    items: List[Dict[str, Any]] = []
    now = time.time()
    try:
        conn = sqlite3.connect(db.get_db_path())
        conn.row_factory = sqlite3.Row
        try:
            rows_db = conn.execute(
                """
                SELECT ea.id AS atom_id, ea.raw_news_id, ea.event_time,
                       ea.direction, ea.direction_confidence, ea.reasoning,
                       ea.actor, ea.evidence_count,
                       da.code AS asset_code,
                       rn.content AS news_content,
                       rn.gate_impact_1to100 AS impact_1to100
                  FROM event_atom ea
                  LEFT JOIN dim_asset da ON da.id = ea.asset_id
                  LEFT JOIN raw_news rn ON rn.id = ea.raw_news_id
                 WHERE ea.event_time >= ?
                 ORDER BY ea.event_time DESC, ea.id DESC
                 LIMIT 80
                """,
                (int(now) - 4 * 3600,),
            ).fetchall()
            # v226+ Q5 闭环：批量取所有 event_atom_id → ai_decisions 映射（避免 N+1）
            atom_ids = [int(r["atom_id"]) for r in rows_db]
            decisions_map = _fetch_decisions_by_atom_ids(conn, atom_ids)

            seen_news: set = set()
            for r in rows_db:
                news_id = int(r["raw_news_id"] or 0)
                if news_id and news_id in seen_news:
                    continue
                if news_id:
                    seen_news.add(news_id)
                atom_id = int(r["atom_id"])
                affected = decisions_map.get(atom_id, [])
                asset_code = str(r["asset_code"] or "").strip().upper()
                impact = r["impact_1to100"]
                if impact is None:
                    impact = int(round(float(r["direction_confidence"] or 0.0) * 100))
                impact = max(0, min(100, int(impact)))
                affected_assets = [
                    str(a["asset"]) for a in affected
                    if str(a.get("asset") or "").upper() not in ("", "NONE")
                ]
                if not affected_assets and asset_code and asset_code not in ("SECTOR", "NONE"):
                    affected_assets = [asset_code]
                items.append({
                    "event_id": str(atom_id),
                    "title": _event_title(r["news_content"], r["reasoning"], r["actor"]),
                    "ts": int(r["event_time"] or 0),
                    "age_minutes": max(0, int((now - float(r["event_time"] or now)) / 60)),
                    "urgency": "high" if impact >= 60 else "medium" if impact >= 30 else "low",
                    "likely_affected": affected_assets,
                    "likely_affected_count": len(affected_assets),
                    "confidence": round(impact / 100.0, 3),
                    "hint": _format_affected_hint(affected),
                })
                if len(items) >= 20:
                    break
        finally:
            conn.close()
    except Exception:
        # 不再静默吞掉：坏掉时留下栈，避免下一次又变成"永远空面板"
        import traceback
        traceback.print_exc()
    return {"ok": True, "ts": now, "items": items}


# ─────────────────────────────────────────────────────────────────────
# v226+ Q5 闭环：event_atom → ai_decisions 反向映射
# 背景：event_radar_handler 之前 likely_affected=[] 空着。
# 实现：用 json_each 解析 ai_decisions.event_atom_ids（JSON 数组），
#       批量反查每个 event_atom 触发了哪些 ai_decisions，按 target_asset 聚合。
# 输出格式：[{asset, action, score, ts, decision_id}, ...]
# 排序：最近优先，最多返回 8 条（前端列表项 ≤ 8 视觉合理）。
# ─────────────────────────────────────────────────────────────────────

# 最大返回条数（避免卡片过长）
_MAX_AFFECTED_PER_ATOM = 8


def _fetch_decisions_by_atom_ids(conn, atom_ids: List[int]) -> Dict[int, List[Dict[str, Any]]]:
    """批量反查：atom_id → [decision, ...]

    Returns
    -------
    Dict[atom_id, List[{asset, action, score, ts, decision_id}]]
    """
    out: Dict[int, List[Dict[str, Any]]] = {a: [] for a in atom_ids}
    if not atom_ids:
        return out
    try:
        # 用 json_each 解析 event_atom_ids 数组 → 单 SQL 反查所有 atom_id
        # 性能 = O(unique_atom_ids * decisions_per_atom)，避免 N+1
        # SQLite 3.38+ json_each 已稳定
        placeholders = ",".join("?" for _ in atom_ids)
        rows_db = conn.execute(
            f"""
            SELECT ea.id AS atom_id,
                   ai.id AS decision_id,
                   ai.target_asset,
                   COALESCE(NULLIF(ai.doubao_action, ''), NULLIF(ai.suggested_action, ''), 'HOLD') AS action,
                   COALESCE(ai.sentiment_score, 0.0) AS score,
                   ai.created_at
              FROM event_atom ea
              JOIN ai_decisions ai
                ON EXISTS (
                       SELECT 1 FROM json_each(ai.event_atom_ids)
                       WHERE json_each.value = ea.id
                )
             WHERE ea.id IN ({placeholders})
             ORDER BY ea.id DESC, ai.created_at DESC
            """,
            tuple(int(a) for a in atom_ids),
        ).fetchall()
        for r in rows_db:
            entry = {
                "asset": str(r["target_asset"] or "NONE"),
                "action": str(r["action"] or "HOLD"),
                "score": round(float(r["score"] or 0.0), 4),
                "ts": str(r["created_at"] or ""),
                "decision_id": int(r["decision_id"]),
            }
            atom_id = int(r["atom_id"])
            if len(out[atom_id]) < _MAX_AFFECTED_PER_ATOM:
                out[atom_id].append(entry)
    except Exception:
        # 兜底：静默返回空字典（不影响主流程）
        pass
    return out


def _format_affected_hint(affected: List[Dict[str, Any]]) -> str:
    """格式化 likely_affected 为一句话 hint（前端卡片显示用）。

    规则：
      - 空列表 → ""
      - 1 条   → "<元> → <BUY|SELL>"
      - 多条   → "<X> 条信号: <asset1> BUY · <asset2> SELL ..."
    """
    if not affected:
        return ""
    if len(affected) == 1:
        a = affected[0]
        return f"{a['asset']} → {a['action']}"
    # 多条：取最多 3 个标的 + 各自方向
    samples = []
    for d in affected[:3]:
        samples.append(f"{d['asset']} {d['action']}")
    extra = len(affected) - 3
    hint = " · ".join(samples)
    if extra > 0:
        hint += f" · +{extra}条"
    return hint


# ────────────────────────────────────────────────────────────
# 5️⃣ AI 元认知自检 — 核心新增
# ────────────────────────────────────────────────────────────

_BIAS_KEYWORDS = {
    'confirmation_bias': ['一定是', '肯定', '绝大部分', '全部', '一切', '总是', '从来不'],
    'anchoring':          ['所以', '因此', '必然', '绝对'],
    'availability':       ['最近', '刚刚', '记得', '印象'],
    'overconfidence':     ['总是', '一定', '必然', '肯定', '100%'],
}

_HYPOTHESIS_TYPES = {
    'fed_macro':  ['美联储', '加息', '减息', '降息', 'Fed', 'fed', '利率', 'FOMC'],
    'us_china':   ['中美', '关税', '贸易', '脱钩', '科技战'],
    'crypto':     ['BTC', '比特币', 'ETH', '山寨', '加密'],
    'equity':     ['股票', '个股', '海力士', '英伟达', '苹果', '特斯拉'],
    'commodity':  ['黄金', '原油', '铜', '铁矿'],
    'fx':         ['美元', '日元', '人民币', '汇率'],
}


def _detect_bias(hyp: str) -> List[Dict[str, str]]:
    findings: List[Dict[str, str]] = []
    hyp_lower = hyp.lower()
    for bias, kws in _BIAS_KEYWORDS.items():
        for kw in kws:
            if kw.lower() in hyp_lower:
                findings.append({
                    'type': bias,
                    'trigger': kw,
                    'note': f"使用了「{kw}」,触发 {bias.split('_')[0]} 提示",
                })
                break
    if re.search(r'绝大部分|几乎所有|大部分', hyp):
        findings.append({
            'type': 'scope_overreach',
            'trigger': '全称量化',
            'note': '使用了全称量化(绝大部分/几乎所有),需明确时间窗口、品种范围',
        })
    return findings


def _detect_hypothesis_type(hyp: str) -> str:
    for type_, kws in _HYPOTHESIS_TYPES.items():
        for kw in kws:
            if kw in hyp:
                return type_
    return 'other'


def _parse_hypothesis(hyp: str) -> Dict[str, str]:
    type_ = _detect_hypothesis_type(hyp)
    cause = ''
    if type_ == 'fed_macro':
        cause = 'fed_rate_expectation'
    elif type_ == 'crypto':
        cause = 'btc_trend'
    elif type_ == 'equity':
        m = re.search(r'([一-鿿A-Za-z]+)', hyp)
        cause = (m.group(1) if m else 'asset')
    return {
        'cause': cause or 'unknown',
        'effect': 'asset_movement',
        'type': type_,
    }


def _graph_state(cause: str) -> Dict[str, Any]:
    has_edge = False
    related = 0
    sample_evidence: List[str] = []
    try:
        conn = sqlite3.connect(db.get_db_path())
        conn.row_factory = sqlite3.Row
        try:
            try:
                rows = conn.execute(
                    "SELECT id, label, kind, confidence, evidence FROM causal_graph_nodes "
                    "WHERE label LIKE ? OR label LIKE ? LIMIT 10",
                    (f'%{cause}%', '%Fed%'),
                ).fetchall()
                related = len(rows)
                has_edge = related > 0
                for r in rows[:3]:
                    sample_evidence.append(f"node #{r['id']} {r['label']} conf={r['confidence']:.2f}")
            except Exception:
                pass
        finally:
            conn.close()
    except Exception:
        pass
    return {'has_edge': has_edge, 'related': related, 'sample': sample_evidence}


def _multi_perspective(hyp: str, parsed: Dict[str, str]) -> List[Dict[str, str]]:
    return [
        {'side': '反方', 'voice': '这条假设是否过于全称?绝大部分 ≠ 所有 — 大宗商品、外汇、新兴市场受本地因素影响很大。'},
        {'side': '中立', 'voice': '需要先验证:过去 5 年,Fed 加息/降息周期里,S&P 500 / BTC / 黄金 的真实相关性是多少?相关性系数 vs 假设的 80%。'},
        {'side': '支持', 'voice': '在主要发达国家(尤其美国),货币政策是流动性最核心的开关 — 历史上 2019/2020/2022 三次大转向都伴随主要资产 30%+ 的异动。'},
    ]


async def meta_cognition_handler(payload: Dict[str, Any]) -> Dict[str, Any]:
    """POST /api/discovery/meta-cognition — 7 步自检"""
    hyp = str(payload.get('hypothesis', '') or '').strip()
    if not hyp:
        return {"ok": False, "error": "hypothesis 不能为空"}
    parsed = _parse_hypothesis(hyp)
    biases = _detect_bias(hyp)
    gstate = _graph_state(parsed['cause'])
    perspectives = _multi_perspective(hyp, parsed)

    step_recv = {
        'title': '输入接收 · 把模糊概念结构化',
        'summary': f"假设:「{hyp[:30]}{'…' if len(hyp) > 30 else ''}」",
        'detail': f"拆分:cause={parsed['cause']} → effect={parsed['effect']};类型={parsed['type']}",
        'bullets': [
            f"cause 候选: {parsed['cause']}",
            f"effect 候选: {parsed['effect']}",
            f"类型识别: {parsed['type']}",
        ],
        'verdict': 'pass',
    }

    if biases:
        step_bias = {
            'title': '偏见检测 · 输入是否含认知陷阱',
            'summary': f"检测到 {len(biases)} 项潜在偏差",
            'detail': '用户输入的假设中包含「全称量化」「因果限定」等强表述,需要在验证前先拆解。',
            'bullets': [f"· {b['note']}" for b in biases],
            'verdict': 'warn',
        }
    else:
        step_bias = {
            'title': '偏见检测 · 输入是否含认知陷阱',
            'summary': '未检测到明显偏差',
            'detail': '输入相对克制,但仍建议人工核查。',
            'bullets': ['建议:即使启发式未触发,人工也要问一次「我是否有先入为主」'],
            'verdict': 'pass',
        }

    if gstate['has_edge']:
        step_graph = {
            'title': '现有图谱状态 · 是否已有这条边',
            'summary': f"找到 {gstate['related']} 个相关节点",
            'detail': '说明类似因果边已经在图谱中,但「边」本身需要新建或确认。',
            'bullets': gstate['sample'] + ['建议:复用已有节点的 confidence 作为新边的下限'],
            'verdict': 'pass',
        }
    else:
        step_graph = {
            'title': '现有图谱状态 · 是否已有这条边',
            'summary': '图谱中暂未发现相关边',
            'detail': '这是一条全新边,需要更多证据才能进入图谱。',
            'bullets': ['建议:先在 /causal-chains 页面建「候选」,evidence 来源标注「用户输入」'],
            'verdict': 'warn',
        }

    step_data = {
        'title': '数据需求 · 验证需要哪些数据',
        'summary': 'Fed 路径 + 跨资产历史价格 + 周期标注',
        'detail': '要回答「假设是否成立」,至少需要过去 5 年(包含多个加息/降息周期)的数据。',
        'bullets': [
            '· Fed Funds Rate / 2Y/10Y/30Y 利率历史',
            '· BTC / SPX / DXY / 黄金 的同期价格',
            '· FOMC 议息会议日期 + 实际加减息时间',
            '· 行业 / 地区 分类标签(避免把所有「股票」混在一起)',
        ],
        'verdict': 'pass',
    }

    step_multi = {
        'title': '多视角质询 · 反方/中立/支持',
        'summary': f"{len(perspectives)} 个视角",
        'detail': '同一假设从不同立场检验,可暴露盲点。',
        'bullets': [f"[{p['side']}] {p['voice']}" for p in perspectives],
        'verdict': 'warn',
    }

    step_verify = {
        'title': '验证设计 · 反事实 / 回测 / 自然实验',
        'summary': '3 种验证方式',
        'detail': '对假设进行相关验证最好选三种方法交叉验证。',
        'bullets': [
            '· 反事实:在「假设不成立」的虚拟历史中,模型是否能解释同样的价格',
            '· 回测:用过去 5 年数据,统计「Fed 路径异动 → 各品种 24h 反应」的实际相关系数',
            '· 自然实验:找 N 个类似 2019/2022 的大转折,看主要品种是否同向',
        ],
        'verdict': 'pass',
    }

    has_bias = bool(biases)
    has_graph = gstate['has_edge']
    if has_bias and not has_graph:
        verdict = 'need_more_evidence'
        score = 55.0
        caveats = [
            '检测到认知偏差,需要更多证据才能入图谱',
            '建议先在 /causal-chains 投为「候选边」,evidence 来源标注为「用户输入 · 待验证」',
        ]
    elif has_graph and not has_bias:
        verdict = 'accept'
        score = 80.0
        caveats = ['假设与现有图谱一致,可考虑在已有边中复用']
    elif has_graph and has_bias:
        verdict = 'need_more_evidence'
        score = 65.0
        caveats = ['图谱有相关边但假设有偏差,需要更精细的边界条件']
    else:
        verdict = 'need_more_evidence'
        score = 50.0
        caveats = ['全新偏差,需要自然实验验证']

    proposal = {
        'cause': parsed['cause'],
        'effect': parsed['effect'],
        'weight': 0.5,
        'evidence_required': '过去 5 年 Fed 加减息周期数据 + 各品种同期价格;回测相关系数 ≥ 0.4 才接受',
    } if verdict != 'reject' else None

    steps = {
        'recv':    step_recv,
        'bias':    step_bias,
        'graph':   step_graph,
        'data':    step_data,
        'multi':   step_multi,
        'verify':  step_verify,
        'verdict': {
            'title': '决策 · 接受/拒绝/需要更多证据',
            'summary': verdict,
            'detail': '本步汇总前 6 步的 verdict,给出最终决策;不会自动修改因果图谱。',
            'bullets': caveats,
            'verdict': 'pass' if verdict == 'accept' else 'warn' if verdict == 'need_more_evidence' else 'fail',
        },
    }

    return {
        'ok': True,
        'ts': time.time(),
        'hypothesis': hyp,
        'steps': steps,
        'overall_verdict': verdict,
        'overall_score': score,
        'caveats': caveats,
        'proposal_for_graph': proposal,
    }


# ═══════════════════════════════════════════════════════════════════════════
# v227 P1-2/3/5: 发现页三个新维度
#   · undervalued_detector   — 类似海力士被严重低估/跌过头的标的
#   · cross_asset_rankings   — 全网 24h/7d 涨跌排行 + 严重下跌标记
#   · fed_watch              — Fed 加减息预期推导链 (数从哪来到推送哪去)
# ═══════════════════════════════════════════════════════════════════════════


# ── 超跌 Detector 启发式 ────────────────────────────────────────────────
# v405 口径修正：这项以前叫「超级低估（基本面+技术面）」，但六项打分里
# **一个财务指标都没有** —— 全是跌幅/周期/技术反弹/品类/新闻热度。名字和
# 内容不符，等于把纯技术超跌包装成基本面。现在：
#   · 名称改为「技术面超跌」（前端文案同步）
#   · 有财报的标的加一道真财务闸门（net_income>0 且 营收同比 > -20%），
#     破了闸门就封顶 35 分并标「基本面恶化，仅技术超跌」
#   · 没有财报的标的明确标 fundamental_verified=False，不再暗示"基本面没崩"
# 启发式定义：满足以下条件越多 → "超跌/刚被严重砸"分越高：
#   1) 7d 跌幅 <= -10%   (刚刚被严重砸)
#   2) 30d 跌幅 <= -15%  (中期回调充分)
#   3) cycle phase = accumulation  (周期底部蓄力,见 cycle_analyst.detect_phase)
#   4) reversal_prob >= 0.5 且 direction='up' (技术面反弹预期)
#   5) 资产属于 _HYNIX_KEYWORDS / 美股大盘 / 韩股 (同行业可比)
#   6) 24h news_count 上升 ≥ 30%  (情绪转暖,机构关注度回归)
_UNDERVALUED_SCORE_FN = (
    ("drawdown_7d", lambda d: max(0, min(20, abs(min(d.get("change_7d_pct", 0), 0))) * 1.5)),
    ("drawdown_30d", lambda d: max(0, min(20, abs(min(d.get("change_30d_pct", 0), 0))) * 0.8)),
    ("cycle_accumulation", lambda d: 25.0 if d.get("phase") == "accumulation" else 0.0),
    ("reversal_up", lambda d: 20.0 if (d.get("reversal_prob") or 0) >= 0.5
                                 and d.get("reversal_dir") == "up" else 0.0),
    ("category_match", lambda d: 10.0 if d.get("category") in
                                  ("memory_chip", "altcoin", "news_stock") else 0.0),
    ("news_surge", lambda d: 10.0 if d.get("news_surge_pct", 0) >= 30 else 0.0),
)


def _fundamental_gate(pack: Dict[str, Any], asset: str) -> Dict[str, Any]:
    """财报闸门：有季报的标的必须「净利为正 + 营收同比不低于 -20%」。

    返回 verified=False 表示该标的根本没有财报数据（小币/大部分个股），
    这种情况下**不给任何基本面结论**，只如实标注未核验。
    """
    ticker = str(asset or "").upper()
    summary = (pack.get("summary") or {}).get(ticker)
    if not summary:
        return {"verified": False, "ok": None, "note": "无财报数据，基本面未核验"}
    ttm = summary.get("ttm") or {}
    yoy = summary.get("yoy") or {}
    net = ttm.get("net_income")
    rev_yoy = yoy.get("revenue")
    ok = True
    bits: List[str] = []
    if net is not None:
        bits.append(f"TTM净利{net / 1e9:+.2f}十亿美元")
        if net <= 0:
            ok = False
    if rev_yoy is not None:
        bits.append(f"营收同比{rev_yoy:+.1f}%")
        if rev_yoy < -20.0:
            ok = False
    return {"verified": True, "ok": ok, "note": "，".join(bits) or "财报字段缺失",
            "period_end": (summary.get("latest") or {}).get("period_end")}


async def undervalued_detector_handler() -> Dict[str, Any]:
    """GET /api/discovery/undervalued-detector — 技术面超跌标的（含可得的财务闸门）"""
    _hit = None if _disc_force() else _disc_cache_get("undervalued", 600.0)
    if _hit is not None:
        return _hit
    items: List[Dict[str, Any]] = []
    method = "empty"
    try:
        import cycle_analyst as ca

        conn = sqlite3.connect(db.get_db_path())
        conn.row_factory = sqlite3.Row
        try:
            # v405: 一次读出全库财报（零网络），供逐标的财务闸门复用
            try:
                import equity_fundamentals as _ef
                _fund_pack = _ef.db_pack(conn)
            except Exception:
                _fund_pack = {"summary": {}}
            # 取 30 天数据 — 既要算 7d 又要算 30d 变化，至少需要 30 根
            cutoff_30d = int(time.time()) - 30 * 86400
            cutoff_24h = int(time.time()) - 86400

            sym_latest = conn.execute(
                "SELECT asset, indicator, value, ts FROM external_indicators "
                "WHERE ts >= ? AND indicator NOT IN ('UST2Y','DXY','VIX') "
                "ORDER BY asset, ts DESC",
                (cutoff_30d,),
            ).fetchall()

            by_asset: Dict[str, List[Any]] = {}
            for r in sym_latest:
                by_asset.setdefault(r["asset"], []).append(r)

            for asset, rows in by_asset.items():
                if len(rows) < 5:
                    continue
                values = [float(r["value"]) for r in rows]
                latest = values[0]
                v7 = values[min(7, len(values) - 1)]
                v30 = values[min(30, len(values) - 1)]
                if v7 <= 0 or v30 <= 0 or latest <= 0:
                    continue
                change_7d_pct = (latest - v7) / v7 * 100
                change_30d_pct = (latest - v30) / v30 * 100

                closes = list(reversed(values))
                phase_info: Dict[str, Any] = {}
                reversal_info: Dict[str, Any] = {}
                if len(closes) >= 10:
                    try:
                        phase_info = ca.detect_phase(closes, window=min(20, len(closes) - 1))
                        reversal_info = ca.predict_reversal(closes, reversal_threshold_pct=3.0)
                    except Exception:
                        pass

                news_count_24h = 0
                news_count_prev24h = 0
                try:
                    r1 = conn.execute(
                        "SELECT COUNT(*) AS c FROM raw_news "
                        "WHERE ts >= ? AND (title LIKE ? OR content LIKE ?)",
                        (cutoff_24h, f"%{asset}%", f"%{asset}%"),
                    ).fetchone()
                    news_count_24h = int(r1["c"] or 0) if r1 else 0
                    r2 = conn.execute(
                        "SELECT COUNT(*) AS c FROM raw_news "
                        "WHERE ts >= ? AND ts < ? AND (title LIKE ? OR content LIKE ?)",
                        (cutoff_24h - 86400, cutoff_24h, f"%{asset}%", f"%{asset}%"),
                    ).fetchone()
                    news_count_prev24h = int(r2["c"] or 0) if r2 else 0
                except Exception:
                    pass
                news_surge_pct = (
                    ((news_count_24h - news_count_prev24h) / max(news_count_prev24h, 1)) * 100
                    if news_count_prev24h > 0
                    else (100.0 if news_count_24h >= 3 else 0.0)
                )

                if asset in ("SKHYNIX", "MU", "000660.KS") or any(
                    k in asset.upper() for k in ("HYNIX", "MICRON", "存储", "海力士")
                ):
                    category = "memory_chip"
                elif any(k in asset.upper() for k in _CRYPTO_KEYWORDS):
                    category = "altcoin"
                elif asset.upper() in ("XAU", "GLD"):
                    category = "safe_metal"
                else:
                    category = "other"

                d = {
                    "symbol": asset,
                    "category": category,
                    "change_7d_pct": round(change_7d_pct, 2),
                    "change_30d_pct": round(change_30d_pct, 2),
                    "phase": phase_info.get("phase", "unknown"),
                    "reversal_prob": reversal_info.get("prob"),
                    "reversal_dir": reversal_info.get("direction"),
                    "news_count_24h": news_count_24h,
                    "news_surge_pct": round(news_surge_pct, 1),
                }

                reasons: List[str] = []
                score = 0.0
                for name, fn in _UNDERVALUED_SCORE_FN:
                    s = fn(d)
                    if s > 0:
                        reasons.append(f"{name}={s:.1f}")
                    score += s
                score = min(score, 100.0)
                # v405: 财务闸门 —— 技术超跌 + 基本面恶化 ≠ 低估，是「便宜有原因」
                gate = _fundamental_gate(_fund_pack, asset)
                d["fundamental_verified"] = gate["verified"]
                d["fundamental_ok"] = gate["ok"]
                d["fundamental_note"] = gate["note"]
                capped = False
                if gate["verified"] and gate["ok"] is False:
                    score = min(score, 35.0)
                    capped = True
                    reasons.append("fundamental_gate=跌破（净利为负或营收同比<-20%）")
                d["undervalued_score"] = round(score, 1)
                d["reasons"] = reasons
                d["score_capped_by_fundamentals"] = capped

                if capped:
                    d["hint"] = "基本面恶化 — 仅技术超跌，不是低估，勿按均值回归买入"
                elif not gate["verified"]:
                    d["hint"] = "技术超跌（无财报，基本面未核验）— 只当技术信号，勿推断低估"
                elif d["undervalued_score"] >= 60:
                    d["hint"] = ("超跌 + 财务未恶化 — 关注反转确认信号后再入场" if d["phase"] == "accumulation"
                                 else "严重超跌 — 等待技术面止跌")
                elif d["undervalued_score"] >= 35:
                    d["hint"] = "中等机会 — 关注成交量 + 新闻催化"
                else:
                    d["hint"] = ""

                items.append(d)
        finally:
            conn.close()
        items.sort(key=lambda x: (-x["undervalued_score"], x["change_30d_pct"]))
        items = items[:30]
        method = "heuristic"
    except Exception:
        pass

    _out: Dict[str, Any] = {
        "ok": True,
        "ts": int(time.time()),
        "items": items,
        "method": method,
        "note": ("海力士启发式：7d 跌幅≥10% + 30d 跌幅≥15% + cycle=accumulation + "
                 "reversal_prob≥0.5 + 情绪转暖 → 综合分 0-100"),
    }
    _disc_cache_put("undervalued", _out)
    return _out


# ── Cross-Asset Rankings ─────────────────────────────────────────────────
# v2026-10-08: 24h 改成 26h,避免日级轮询时区差导致"只剩 1 个数据点,百分比算不出来"
_RANK_TIMEFRAMES: Dict[str, int] = {
    "1h": 3600,
    "24h": 26 * 3600,
    "7d": 7 * 86400,
    "30d": 30 * 86400,
}


async def cross_asset_rankings_handler(
    timeframe: str = "24h",
    limit: int = 30,
    crash_threshold_pct: float = -10.0,
) -> Dict[str, Any]:
    """GET /api/discovery/cross-asset-rankings?timeframe=24h

    全网涨跌排行，按"刚刚被严重跌"的额外标记。
    """
    _cache_key = f"cross_rank_{timeframe}"
    _hit = None if _disc_force() else _disc_cache_get(_cache_key, 600.0)
    if _hit is not None:
        return _hit
    window_s = _RANK_TIMEFRAMES.get(timeframe, 86400)
    items: List[Dict[str, Any]] = []
    gainers: List[Dict[str, Any]] = []
    losers: List[Dict[str, Any]] = []
    crashes: List[Dict[str, Any]] = []
    category_breakdown: Dict[str, Any] = {}
    anomalies: List[Dict[str, Any]] = []
    method = "empty"

    try:
        conn = sqlite3.connect(db.get_db_path())
        conn.row_factory = sqlite3.Row
        try:
            cutoff = int(time.time()) - window_s
            sym_window = conn.execute(
                "SELECT asset, indicator, value, ts FROM external_indicators "
                "WHERE ts >= ? AND indicator NOT IN ('UST2Y','DXY','VIX') "
                "ORDER BY asset, indicator, ts DESC",
                (cutoff,),
            ).fetchall()

            # v480 修量纲：同一 asset 下有 tencent_price/tencent_amount/tencent_volume/
            # tencent_change_pct 等多个指标，原先混成一条序列算涨跌 → 用成交额和价格比，
            # 出现「NDX +2,448,212%」这种噪声。改为按 (asset, indicator) 分组，
            # 并且只让"价格类"指标参与排行；量纲异常的单独列入 anomalies，不入榜。
            _PRICE_PRIORITY = ("tencent_price", "close", "price", "last", "x_spot_median", "spot_median")

            def _is_price_indicator(ind: str, asset: str = "") -> bool:
                low = ind.lower()
                if any(bad in low for bad in (
                    "amount", "volume", "change_pct", "ratio", "funding", "open_interest",
                    "supply", "fee", "mempool", "hashrate", "dominance", "basis", "skew",
                    "iv", "dvol", "taker", "count", "_mb", "_tx",
                )):
                    return False
                # v481：这里原本有一条裸 "usd"，把 `BTC-USD` / `ETH-USD` 这类**标准价格
                # ticker** 一并判成非价格指标 → 整条行情序列被丢弃、排行恒空。
                # 真正要挡的是「以 usd 计的量」这种指标名（如 `open_interest_usd`，
                # 已由 open_interest 覆盖；`*_usd_value` 之类），所以只按带分隔符的写法挡。
                if any(bad in low for bad in ("_usd", "usd_", "usd_value", "usd_amount")):
                    return False
                # 行情 ticker 写法（Yahoo/交易所风格）本身就是价格序列：
                # `BTC-USD` / `ETH-USDT` / `600519-SS` 这类没有 price/close/last
                # 任何关键词，但确实是唯一的价格来源。
                if low.endswith("-usd") or low.endswith("-usdt") or low.endswith("usd="):
                    return True
                # 指标名就是标的代号本身（如 `XAU`、`WTI`、`600519`）：库里没有
                # 更细的指标名时，这就是唯一的价格序列。
                norm = low.replace("-", "").replace("_", "").replace(".", "").replace(" ", "")
                if asset and norm in {asset.lower(), asset.lower().replace("-", "")}:
                    return True
                return any(k in low for k in ("price", "close", "last", "median"))

            def _price_rank(ind: str) -> int:
                for i, k in enumerate(_PRICE_PRIORITY):
                    if k in ind.lower():
                        return i
                return len(_PRICE_PRIORITY)

            # 各时间窗的合理涨跌幅上限：超过就认为是量纲/口径问题，不当机会展示
            _MAX_ABS_PCT = {"1h": 25.0, "24h": 60.0, "7d": 200.0, "30d": 500.0}
            max_abs_pct = _MAX_ABS_PCT.get(timeframe, 60.0)

            by_key: Dict[Any, List[Any]] = {}
            for r in sym_window:
                by_key.setdefault((r["asset"], r["indicator"]), []).append(r)

            # 每个 asset 只保留一个最像价格的序列（优先 tencent_price，其次条数多的）
            best_series: Dict[str, List[Any]] = {}
            for (asset, ind), rows in by_key.items():
                if not _is_price_indicator(ind, asset) or len(rows) < 2:
                    continue
                cur = best_series.get(asset)
                if cur is None:
                    best_series[asset] = rows
                    continue
                cur_ind = cur[0]["indicator"]
                if (_price_rank(ind), -len(rows)) < (_price_rank(cur_ind), -len(cur)):
                    best_series[asset] = rows

            for asset, rows in best_series.items():
                latest = float(rows[0]["value"])
                earliest = float(rows[-1]["value"])
                if earliest <= 0 or latest <= 0:
                    continue
                change_pct = (latest - earliest) / earliest * 100
                if abs(change_pct) > max_abs_pct:
                    anomalies.append({
                        "symbol": asset,
                        "indicator": rows[0]["indicator"],
                        "latest": round(latest, 4),
                        "earliest": round(earliest, 4),
                        "change_pct": round(change_pct, 2),
                        "reason": f"|change|>{max_abs_pct}% 疑量纲错",
                    })
                    continue
                if asset in ("SKHYNIX", "MU") or any(k in asset.upper() for k in ("HYNIX", "MICRON")):
                    category = "memory_chip"
                elif any(k in asset.upper() for k in _CRYPTO_KEYWORDS):
                    category = "altcoin"
                elif asset.upper() in ("XAU", "GLD", "GOLD"):
                    category = "safe_metal"
                elif asset.upper() in ("WTI", "USOIL", "OIL", "CL"):
                    category = "energy"
                elif asset.upper() in ("UST2Y", "US10Y", "TLT"):
                    category = "bond"
                elif asset.upper() == "DXY":
                    category = "fx"
                else:
                    category = "other"
                items.append({
                    "symbol": asset,
                    "indicator": rows[0]["indicator"],
                    "category": category,
                    "latest": round(latest, 4),
                    "earliest": round(earliest, 4),
                    "change_pct": round(change_pct, 2),
                    "ts": int(rows[0]["ts"]),
                    "is_crash": change_pct <= crash_threshold_pct,
                })

            cat_stats: Dict[str, Dict[str, float]] = {}
            for it in items:
                c = it["category"]
                cat_stats.setdefault(c, {"count": 0, "sum_pct": 0.0})
                cat_stats[c]["count"] += 1
                cat_stats[c]["sum_pct"] += it["change_pct"]
            category_breakdown = {
                c: {"count": int(v["count"]),
                    "avg_change_pct": round(v["sum_pct"] / v["count"], 2)}
                for c, v in cat_stats.items()
            }
        finally:
            conn.close()

        items.sort(key=lambda x: -x["change_pct"])
        gainers = [it for it in items if it["change_pct"] > 0][:limit]
        losers = sorted(
            [it for it in items if it["change_pct"] <= 0],
            key=lambda x: x["change_pct"],
        )[:limit]
        crashes = sorted(
            [it for it in items if it["is_crash"]],
            key=lambda x: x["change_pct"],
        )
        method = "external_indicators"
    except Exception:
        pass

    _out: Dict[str, Any] = {
        "ok": True,
        "ts": int(time.time()),
        "timeframe": timeframe,
        "window_s": window_s,
        "total_count": len(items),
        "anomalies": anomalies,
        "gainers": gainers,
        "losers": losers,
        "crashes": crashes,
        "crash_threshold_pct": crash_threshold_pct,
        "category_breakdown": category_breakdown,
        "method": method,
        "note": ("crashes = timeframe 内跌幅 ≤ crash_threshold_pct 的标的，"
                 "可与 undervalued_detector 联动"),
    }
    _disc_cache_put(_cache_key, _out)
    return _out


# ── Fed Watch — 加减息预期推导链 ─────────────────────────────────────────
async def fed_watch_handler() -> Dict[str, Any]:
    """GET /api/discovery/fed-watch

    美联储加减息预期的端到端透明化页面 — 数从哪来、怎么算、推送给谁。
    """
    out: Dict[str, Any] = {
        "ok": True,
        "ts": int(time.time()),
        "sources": [],
        "derivation": {},
        "notification": {},
        "explain": "",
        "history_24h": {"ts": [], "value": []},
        "impact_link": "/api/discovery/fed-impact",
    }
    try:
        import fed_rate_expectation as fre
        expectation = fre.fetch_expectation()
        try:
            history = fre._fetch_history_24h()  # type: ignore[attr-defined]
        except Exception:
            history = {}

        out["sources"] = [
            {"name": "DGS2 (FRED)",
             "label": "2 年期美债收益率 (FRED 实时)",
             "value_pct": expectation.get("dgs2_pct"),
             "ts": expectation.get("ts"),
             "url": "https://fred.stlouisfed.org/series/DGS2"},
            {"name": "DFF (FRED)",
             "label": "联邦基金有效利率 (FRED 实时)",
             "value_pct": expectation.get("dff_pct"),
             "ts": expectation.get("ts"),
             "url": "https://fred.stlouisfed.org/series/DFF"},
            {"name": "UST5Y (yfinance)",
             "label": "5 年期美债收益率 (yfinance ^FVX)",
             "value_pct": expectation.get("ust5y_pct"),
             "ts": expectation.get("ts"),
             "url": "https://finance.yahoo.com/quote/%5EFVX"},
        ]

        dgs2 = expectation.get("dgs2_pct")
        dff = expectation.get("dff_pct")
        ust5y = expectation.get("ust5y_pct")
        direction = expectation.get("direction", "neutral")
        implied = None
        if dgs2 is not None and dff is not None and dff > 0:
            spread_bp = (dgs2 - dff) * 100
            # 与 fed_rate_expectation.derive_expectation 对齐：
            # spread_bp > 5 → hawkish (加息); spread_bp < -5 → dovish (降息)
            implied = {
                "spread_bp": round(spread_bp, 1),
                "interpretation": (
                    "市场预期降息" if spread_bp < -5
                    else "市场预期加息" if spread_bp > 5
                    else "市场预期维持"
                ),
                "logic": ("DGS2 = 市场对未来 2 年 fed rate 路径的隐含定价。"
                          "spread = DGS2 - DFF：负数 → 预期未来利率低于当前 = 降息预期；"
                          "正数 → 预期未来利率高于当前 = 加息预期。"),
            }
        out["derivation"] = {
            "direction": direction,
            "dgs2_pct": dgs2,
            "dff_pct": dff,
            "ust5y_pct": ust5y,
            "implied": implied,
            "method": "DGS2 vs DFF 价差 + UST5Y 趋势确认",
        }

        if isinstance(history, dict) and history.get("points"):
            out["history_24h"] = {
                "ts": [p.get("ts") for p in history["points"]],
                "value": [p.get("value") for p in history["points"]],
            }

        # 通知通道：上次推送时间 + 推送对象
        try:
            conn = sqlite3.connect(db.get_db_path())
            conn.row_factory = sqlite3.Row
            try:
                last_push = conn.execute(
                    "SELECT ts, payload, anomaly_type FROM alert_log "
                    "WHERE category='fed_rate' ORDER BY ts DESC LIMIT 1"
                ).fetchone()
                if last_push:
                    out["notification"] = {
                        "last_push_ts": int(last_push["ts"]),
                        "anomaly_type": last_push["anomaly_type"],
                        "channel": "feishu + email + in-app",
                        "dedup_window_s": 3600,
                    }
                else:
                    out["notification"] = {
                        "last_push_ts": None,
                        "channel": "feishu + email + in-app",
                        "dedup_window_s": 3600,
                        "note": "本次启动尚未触发任何异动推送",
                    }
            finally:
                conn.close()
        except Exception:
            out["notification"] = {
                "channel": "feishu + email + in-app",
                "note": "查询 alert_log 失败（表可能不存在）",
            }

        if direction == "hawkish":
            explain = "市场预期偏加息 — 风险资产（BTC/ETH/科技股）承压，避险资产（黄金/长债）相对受益。"
        elif direction == "dovish":
            explain = "市场预期偏降息 — 风险资产（BTC/ETH/科技股）受益，美元/长债承压。"
        else:
            explain = "市场预期中性 — 关注下一次 Fed 官员讲话或经济数据对方向的扰动。"
        out["explain"] = explain
        out["daemon_status"] = {
            "running": expectation.get("ok", False),
            "interval_s": 300,
            "env_disable": "DISABLE_FED_PUSH=1 可关闭",
        }
    except Exception as exc:
        out["ok"] = False
        out["error"] = f"{type(exc).__name__}: {exc}"

    return out


async def discovery_summary_handler(period: str = "24h") -> Dict[str, Any]:
    """v302+ · /api/discovery/summary — /discover 页顶部 4 张概览卡聚合数据。

    设计: 一次调用, 一次 DB read, 给前端 4 个不重复数字 + 8 个 pillar 状态 + 闭环统计。
    各子面板 (asset-scout / event-radar / cycle-radar / fed-watch) 仍单独加载详细数据,
    summary 只供顶部 L0 概览条用。

    Returns: { period, generated_at, cards: {...}, pillars: [...], loop_stats: {...} }
    """
    from datetime import datetime as _dt
    out: Dict[str, Any] = {
        "ok": True,
        "period": period,
        "generated_at": _dt.now().isoformat(timespec="seconds"),
        "cards": {},
        "pillars": [],
        "loop_stats": {},
    }
    try:
        conn = db.get_connection()
        try:
            conn.row_factory = sqlite3.Row

            # ── card 1: scanner_hits — raw_news 高分命中 (asset-scout 24h 命中替代) ──
            # v498: 两个字段用错了 —— `timestamp` 存的是 epoch 秒的字符串（'1795102900'），
            #   与 datetime('now',...) 的字符串比较永远为假 → 卡 1 恒为 0，前端显示"暂无材质新闻"。
            #   同时 gate 的材质标记在 gate_is_material（0/1），没有 'material' 这个 verdict 值，
            #   主品种在 gate_main_asset。改成 ts 数值窗口 + gate_is_material=1。
            try:
                ar = conn.execute(
                    "SELECT gate_main_asset AS a, COUNT(*) AS c FROM raw_news "
                    "WHERE ts >= strftime('%s','now','-24 hours') "
                    "AND gate_is_material=1 "
                    "GROUP BY gate_main_asset ORDER BY c DESC LIMIT 5"
                ).fetchall()
                atotal = conn.execute(
                    "SELECT COUNT(*) FROM raw_news "
                    "WHERE ts >= strftime('%s','now','-24 hours') "
                    "AND gate_is_material=1"
                ).fetchone()[0]
            except sqlite3.OperationalError:
                ar, atotal = [], 0
            top_assets = [{"asset": str(r["a"] or ""), "count": int(r["c"])} for r in ar]
            out["cards"]["scanner_hits"] = {
                "total": int(atotal),
                "by_kind": {"material_news": int(atotal)},
                "top_3": top_assets[:3],
            }

            # ── card 2: event_alerts — news_events 24h 高分事件 ──
            try:
                erows = conn.execute(
                    "SELECT asset, action, score, ts FROM news_events "
                    "WHERE ts >= strftime('%s','now','-24 hours') "
                    "AND COALESCE(asset,'') NOT IN ('','NONE','NULL') "
                    "AND COALESCE(score,0) > 0 "
                    "ORDER BY score DESC LIMIT 5"
                ).fetchall()
                etotal = conn.execute(
                    "SELECT COUNT(*) FROM news_events "
                    "WHERE ts >= strftime('%s','now','-24 hours') "
                    "AND COALESCE(asset,'') NOT IN ('','NONE','NULL') "
                    "AND COALESCE(score,0) > 0"
                ).fetchone()[0]
            except sqlite3.OperationalError:
                erows, etotal = [], 0
            top_events = [
                {
                    "asset": str(r["asset"] or ""),
                    "action": str(r["action"] or ""),
                    "score": float(r["score"] or 0),
                    "ts": int(r["ts"] or 0),
                }
                for r in erows[:3]
            ]
            out["cards"]["event_alerts"] = {
                "total": int(etotal),
                "top_3": top_events,
            }

            # ── card 3: cycle_signals — cycle_reversal_signals 24h ──
            try:
                crows = conn.execute(
                    "SELECT asset, phase, direction, prob, ts FROM cycle_reversal_signals "
                    "WHERE ts >= strftime('%s','now','-24 hours') "
                    "ORDER BY prob DESC LIMIT 5"
                ).fetchall()
                ctotal = conn.execute(
                    "SELECT COUNT(*) FROM cycle_reversal_signals "
                    "WHERE ts >= strftime('%s','now','-24 hours')"
                ).fetchone()[0]
                cur = conn.execute(
                    "SELECT phase, asset FROM cycle_reversal_signals ORDER BY ts DESC LIMIT 1"
                ).fetchone()
            except sqlite3.OperationalError:
                crows, ctotal = [], 0
                cur = None
            current_phase = str(cur["phase"] or "") if cur else ""
            # v359: 同一品种同一轮会产生多行（prob 完全相同），原来直接取前 3 行
            #   会出现「BTC、BTC、SOL」这种重复。按 asset 去重，各取最高 prob 那条。
            _seen_cycle = set()
            top_cycle = []
            for r in crows:
                a = str(r["asset"] or "")
                if not a or a in _seen_cycle:
                    continue
                _seen_cycle.add(a)
                top_cycle.append({
                    "asset": a,
                    "phase": str(r["phase"] or ""),
                    "direction": str(r["direction"] or ""),
                    "prob": float(r["prob"] or 0),
                })
                if len(top_cycle) >= 3:
                    break
            out["cards"]["cycle_signals"] = {
                "total_24h": int(ctotal),
                "current_phase": current_phase,
                "top_3": top_cycle,
            }

            # ── card 4: causal_pending — kg_improvements 状态 ──
            try:
                cnt_row = conn.execute(
                    "SELECT status, COUNT(*) AS c FROM kg_improvements GROUP BY status"
                ).fetchall()
            except sqlite3.OperationalError:
                cnt_row = []
            cnt = {str(r["status"]): int(r["c"]) for r in cnt_row}
            applied_today = 0
            try:
                applied_today = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM kg_improvements "
                        "WHERE applied_at >= datetime('now','localtime','start of day')"
                    ).fetchone()[0]
                )
            except (sqlite3.OperationalError, KeyError, TypeError):
                applied_today = 0
            out["cards"]["causal_pending"] = {
                "pending": cnt.get("pending", 0),
                "approved": cnt.get("approved", 0),
                "applied_today": applied_today,
                "suggested_action": (
                    "去 /review#improve 审批"
                    if cnt.get("pending", 0) > 0
                    else "暂无待审批"
                ),
            }

            # ── pillars 状态 (8 个子面板) ──
            # v498: 前四个此前是写死的 0/empty —— 顶部概览一直显示"日内流动性 0、
            #   宏观日历 0"，而面板本身有数据。这里改成按真实表算：日内看利率/美元/
            #   股指/VIX 的最新落库时间；宏观日历看未来 24h 事件；Fed 看 FOMC 相关条目。
            _pill = {}
            try:
                r = conn.execute(
                    "SELECT COUNT(*), MAX(ts) FROM external_indicators "
                    "WHERE indicator IN ('UST10Y','UST5Y','DXY','ES','NQ','VIX') "
                    "AND ts >= strftime('%s','now','-24 hours')").fetchone()
                _pill["intraday"] = (int(r[0] or 0), r[1])
            except sqlite3.OperationalError:
                _pill["intraday"] = (0, None)
            try:
                r = conn.execute(
                    "SELECT COUNT(*) FROM macro_calendar "
                    "WHERE event_time BETWEEN strftime('%s','now') AND strftime('%s','now','+24 hours')"
                ).fetchone()
                _pill["macro_calendar"] = (int(r[0] or 0), None)
                r2 = conn.execute(
                    "SELECT COUNT(*) FROM macro_calendar "
                    "WHERE event_time BETWEEN strftime('%s','now') AND strftime('%s','now','+24 hours') "
                    "AND (UPPER(title) LIKE '%FED%' OR UPPER(title) LIKE '%FOMC%' "
                    "OR UPPER(title) LIKE '%POWELL%' OR UPPER(title) LIKE '%CPI%' "
                    "OR UPPER(title) LIKE '%PCE%' OR UPPER(title) LIKE '%PAYROLL%')"
                ).fetchone()
                _pill["fed_watch"] = (int(r2[0] or 0), None)
            except sqlite3.OperationalError:
                _pill["macro_calendar"] = (0, None)
                _pill["fed_watch"] = (0, None)

            def _pillar(key, label, count, last_update=None):
                return {"key": key, "label": label, "last_update": last_update,
                        "count_24h": int(count), "status": ("fresh" if count else "empty")}

            pillars = [
                _pillar("intraday", "日内流动性", _pill.get("intraday", (0, None))[0],
                        _pill.get("intraday", (0, None))[1]),
                {"key": "smart_money", "label": "聪明钱追踪", "last_update": None, "count_24h": 0, "status": "empty"},
                _pillar("fed_watch", "Fed 全景", _pill.get("fed_watch", (0, None))[0]),
                _pillar("asset_scout", "资产猎手", int(atotal)),
                _pillar("event_radar", "事件雷达", int(etotal)),
                _pillar("cycle_radar", "周期维度", int(ctotal)),
                {"key": "weekly", "label": "周度观察", "last_update": None, "count_24h": 0, "status": "empty"},
                _pillar("macro_calendar", "宏观日历", _pill.get("macro_calendar", (0, None))[0]),
            ]
            out["pillars"] = pillars

            # ── loop_stats: 闭环 24h ──
            try:
                # v496 修复口径：原来用 `updated_at`，任何维护任务（权重刷新/去重/
                # 归档）改一下都会算进来 → 首页"24h 入链 854"实际当天只新建 73 条，
                # 虚高 11.7 倍。这里改成 `created_at`，只统计"新入链"。
                into_chains = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM causal_chains "
                        "WHERE created_at >= datetime('now','localtime','-24 hours')"
                    ).fetchone()[0]
                )
            except (sqlite3.OperationalError, KeyError, TypeError):
                into_chains = 0
            try:
                into_graph = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM asset_driver_edges "
                        "WHERE updated_at >= datetime('now','localtime','-24 hours')"
                    ).fetchone()[0]
                )
            except (sqlite3.OperationalError, KeyError, TypeError):
                into_graph = 0
            try:
                last_sync = conn.execute(
                    "SELECT MAX(updated_at) AS ts FROM asset_driver_edges"
                ).fetchone()
                last_sync_ts = str(last_sync["ts"]) if last_sync and last_sync["ts"] else None
            except sqlite3.OperationalError:
                last_sync_ts = None
            out["loop_stats"] = {
                # 口径说明:扫描数 = 24h 消息命中 + 事件异动 + 周期信号三类之和
                # (不是全库原始扫描量);后两段「入链/入图」是有更新的因果链/边数。
                "scanned_24h": int(atotal) + int(etotal) + int(ctotal),
                "news_hits_24h": int(atotal),
                "event_alerts_24h": int(etotal),
                "cycle_signals_24h": int(ctotal),
                "into_chains_24h": into_chains,
                "into_graph_24h": into_graph,
                "last_synced_at": last_sync_ts,
            }
        finally:
            conn.close()
    except Exception as exc:
        out["ok"] = False
        out["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"

    return out

def _mem_free_mb() -> float:
    """本机可用内存（MB）。8GB 机器上 api_server 常因内存被系统 SIGKILL，
    重计算前先看一眼，不够就跳过这一轮，别把进程搞死。"""
    try:
        import subprocess
        out = subprocess.check_output(["vm_stat"], timeout=3).decode()
        free = 0
        for line in out.splitlines():
            if "Pages free" in line:
                free = int(line.split(":")[1].strip().rstrip("."))
        # 只算 free（不累加 inactive）：inactive 在 8GB 机器上经常是"看着能回收、
        # 实际一压就 swap"，把预热当成有 1GB 余量去跑 68s 重算，等于直接把机器推进 swap。
        return free * 4096 / 1024 / 1024
    except Exception:
        return 999.0


def _sync_cross_rankings() -> None:
    """预热线程侧跑同步体（handler 已声明为 async 供 HTTP 层 await）。"""
    asyncio.run(cross_asset_rankings_handler(timeframe="24h"))


def _sync_undervalued() -> None:
    asyncio.run(undervalued_detector_handler())


def _disc_warm_once(round_no: int = 0) -> None:
    """后台刷新重接口。

    分级策略（本机只有 8GB，asset-scout 单算要 60s+ 且吃内存）：
      · 每轮都刷 cycle-scan / fed-impact / cross-rankings（5-20s，轻）
      · undervalued-detector 读全库财报 + 30d 行情，偏重，每 5 轮刷一次（≈25 分钟）
      · asset-scout 每 5 轮才刷一次（≈25 分钟），它慢且重
      · 可用内存 < 120MB 时整轮跳过 —— 让用户请求自己走缓存旧值，
        绝不能在内存告急时再去开一个 60s 的重计算
    """
    if _mem_free_mb() < 120:
        print(f"[disc-warm] 内存不足({_mem_free_mb():.0f}MB)，跳过本轮刷新", flush=True)
        return
    jobs = [
        ("fed_impact", fed_impact_handler),
        ("cycle_scan", _sync_cycle_scan),
        ("cross_rankings", _sync_cross_rankings),
    ]
    if round_no % 5 == 0:
        jobs.append(("asset_scout", asset_scout_handler))
        jobs.append(("undervalued", _sync_undervalued))
    _DISC_TLS.force = True
    try:
        for key, fn in jobs:
            try:
                fn()
            except Exception as exc:
                print(f"[disc-warm] {key} 失败: {type(exc).__name__}: {str(exc)[:80]}", flush=True)
    finally:
        _DISC_TLS.force = False


def _sync_cycle_scan() -> None:
    """预热线程侧跑同步体（cycle_scan_handler 已声明为 async 供 HTTP 层 await）。"""
    asyncio.run(cycle_scan_handler())


def start_disc_prewarm(interval_s: float = 300.0) -> None:
    """v345.8 定稿：只在启动后预热一轮，跑完线程就退出。

    为什么不周期跑：
      api_server 是单进程，重计算占着 GIL 时 /api/health 都要 8s，整站被拖慢。
      实测「每 5 分钟后台刷一轮」反而让页面时快时慢（越刷越慢）。
      所以改成：启动后算一轮把缓存填满（TTL 600s），之后由用户请求按需重算，
      谁都不会被后台线程无端拖住。
    """
    import threading as _th

    def _loop() -> None:
        import time as _time
        _time.sleep(15)              # 让启动期初始化先跑完
        _disc_warm_once(0)           # 含 asset-scout 的那一轮
        print("[disc-warm] 启动预热完成，缓存已填满（TTL 600s）", flush=True)


# ─────────────────────────────────────────────────────────────────────
# v497 数据缺口横幅（GET /api/health/data-gaps）
#
# 为什么做这个：发现页 8 个 pillar 里 6 个显示 empty，但其中一半不是"真的没
# 数据"，而是"上游断了 / 口径没接"。用户只能人肉去猜，猜错就会得出"系统很空"
# 的错误结论。这里给出一份可解释的缺口清单：每条都带数字和年龄，前端在发现页
# 顶部变成红/黄条。
# ─────────────────────────────────────────────────────────────────────

def data_gaps_handler() -> Dict[str, Any]:
    """数据缺口体检：返回 level=red|amber|green 与逐条缺口。"""
    now = int(time.time())
    gaps: List[Dict[str, Any]] = []

    def _add(key: str, label: str, level: str, detail: str, value: Any = None) -> None:
        gaps.append({"key": key, "label": label, "level": level,
                     "detail": detail, "value": value})

    conn = None
    try:
        conn = sqlite3.connect(db.get_db_path(), timeout=10)
        conn.row_factory = sqlite3.Row

        # 0) LLM 主分析通道 —— 这条排在第一位，因为它一断，后面所有"没数据"
        #    其实都是"没人分析"：实测 21:27 两个 MiniMax key 全 402 熔断，
        #    引擎每轮打印"跳过认领"，队列 281 条一条不动，而页面上只表现为
        #    "决策没有新增"，看不出是额度问题还是策略问题。
        try:
            row = conn.execute(
                "SELECT worker_name, last_status, last_tick_at FROM worker_heartbeat "
                "WHERE worker_name='llm_channels'").fetchone()
            if row is not None:
                st = (row["last_status"] or "").lower()
                if st and st not in ("ok", "healthy", "closed"):
                    _add("llm_channels", "LLM 分析通道", "red",
                         f"通道状态 {row['last_status']}（心跳 {row['last_tick_at'] or '-'}）—— "
                         f"引擎会持续跳过认领，队列不会前进，需要充值或换 key")
        except sqlite3.OperationalError:
            pass

        # 1) 黄金基本面：有快照但 0 个可用分量 —— 决策里这块等于没参与
        try:
            row = conn.execute(
                "SELECT ts, fund_available, confidence, reason, components "
                "FROM decision_fundamental_snapshot WHERE asset='XAU' "
                "ORDER BY ts DESC LIMIT 1"
            ).fetchone()
            if row is None:
                _add("xau_fundamental", "黄金基本面", "red",
                     "没有任何基本面快照，决策里该维度从未生效")
            else:
                avail = int(row["fund_available"] or 0)
                age_h = round((now - int(row["ts"] or now)) / 3600.0, 1)
                if avail <= 0:
                    _add("xau_fundamental", "黄金基本面", "red",
                         f"0 个可用分量（最近快照 {age_h}h 前，reason={row['reason'] or '-'}）",
                         avail)
                elif avail < 2:
                    _add("xau_fundamental", "黄金基本面", "amber",
                         f"仅 {avail} 个可用分量（{age_h}h 前），权重极低", avail)
        except sqlite3.OperationalError:
            pass

        # 2) 关键宏观序列断供
        #    注意：**不能按指标名猜**。同一个概念在库里有多个别名：
        #    美元指数有 `dxy_ecb_computed`（新鲜）和 `DXY`（10-02 就停了），
        #    5Y 有 `UST5Y` 和 `^FVX`（后者 yfinance 一直拉不到）。
        #    按单个名字判会把「正常」误报成「断供」，所以每个概念给一组候选，
        #    取组内最新一条；48h 内有一条新鲜的就算正常。
        try:
            concepts = {
                "美元指数": ("dxy_ecb_computed", "DXY"),
                "美债10Y": ("us_dgs10", "UST10Y"),
                "美债5Y": ("UST5Y", "^FVX"),
                "VIX": ("^VIX", "VIX"),
            }
            keys = [k for group in concepts.values() for k in group]
            rows = conn.execute(
                "SELECT indicator, MAX(ts) AS mt FROM external_indicators "
                "WHERE indicator IN (%s) GROUP BY indicator"
                % ",".join("?" * len(keys)),
                keys,
            ).fetchall()
            latest = {str(r["indicator"]): int(r["mt"] or 0) for r in rows}
            bad: List[str] = []
            for label, group in concepts.items():
                best = max((latest.get(k, 0) for k in group), default=0)
                if best == 0:
                    bad.append(f"{label} 无任何序列")
                elif now - best > 48 * 3600:
                    bad.append(f"{label} 已 {round((now - best) / 3600.0, 1)}h 未更新")
            if bad:
                _add("macro_feeds", "宏观序列", "amber", "；".join(bad))
        except sqlite3.OperationalError:
            pass

        # 3) 多空持仓比：24h 无样本 → 面板必然空
        try:
            n_lsr = int(conn.execute(
                "SELECT COUNT(*) FROM lsr_samples WHERE ts >= ?", (now - 86400,)
            ).fetchone()[0])
            if n_lsr == 0:
                _add("lsr_samples", "多空持仓比", "amber", "24h 内 0 条样本，面板必然为空", 0)
        except sqlite3.OperationalError:
            pass

        # 4) 异常信号重复率：如果上游又开始重复写，这里先报出来
        try:
            c_all, c_uniq = conn.execute(
                "SELECT COUNT(*), COUNT(DISTINCT asset || '|' || ts || '|' || signal_kind) "
                "FROM abnormal_signals WHERE ts >= ?", (now - 86400,)
            ).fetchone()
            c_all, c_uniq = int(c_all or 0), int(c_uniq or 0)
            ratio = round(c_all / c_uniq, 2) if c_uniq else 0.0
            if c_uniq and ratio > 1.3:
                _add("abnormal_dup", "异常信号重复", "red",
                     f"24h {c_all} 行 / 去重 {c_uniq} 个 = {ratio} 倍", ratio)
        except sqlite3.OperationalError:
            pass

        # 5) 基本面影子账本：只写不结算 → "基本面有没有用"永远无法验证
        try:
            n_sh, n_settled = conn.execute(
                "SELECT COUNT(*), SUM(CASE WHEN settled=1 THEN 1 ELSE 0 END) "
                "FROM fundamental_shadow"
            ).fetchone()
            n_sh, n_settled = int(n_sh or 0), int(n_settled or 0)
            if n_sh and n_settled == 0:
                _add("shadow_unsettled", "基本面影子账本", "amber",
                     f"{n_sh} 条样本 0 条结算，无法验证基本面权重是否有效", n_sh)
        except sqlite3.OperationalError:
            pass

        # 6) 背离台账样本量：少于 60 笔时命中率不具统计意义
        try:
            n_dv = int(conn.execute(
                "SELECT COUNT(*) FROM divergence_ledger WHERE status NOT IN ('open')"
            ).fetchone()[0])
            if n_dv < 60:
                _add("divergence_samples", "背离台账", "amber",
                     f"已结算样本 {n_dv} 笔（<60 笔，命中率仍属噪声）", n_dv)
        except sqlite3.OperationalError:
            pass

        # 7) 反思闭环：写了反思 ≠ 反思有用。真正被下一次决策看见的，是
        #    decision_reflection.bias_text（ai_worker 会把它注入 prompt）。
        #    所以这里看两件事：近 7 天有没有新反思、其中有多少条带可执行动作。
        #    全为零就说明"反思机制"只剩一张空表，页面不该给人"已经在学习"的错觉。
        try:
            row = conn.execute(
                "SELECT COUNT(*) n, "
                "SUM(CASE WHEN length(COALESCE(actionable_suggestion,''))>0 THEN 1 ELSE 0 END) sug, "
                "AVG(COALESCE(depth_score,0)) depth "
                "FROM decision_reflection WHERE created_at >= datetime('now','-7 day')"
            ).fetchone()
            n_rf = int(row["n"] or 0) if row else 0
            n_sug = int(row["sug"] or 0) if row else 0
            avg_depth = float(row["depth"] or 0) if row else 0.0
            if n_rf == 0:
                _add("reflection_loop", "反思闭环", "amber",
                     "近 7 天 0 条反思：亏损决策没有沉淀成下一次可读的教训", 0)
            elif n_sug * 2 < n_rf:
                _add("reflection_loop", "反思闭环", "amber",
                     f"近 7 天 {n_rf} 条反思只有 {n_sug} 条带可执行动作"
                     f"（平均深度 {avg_depth:.0f}/100）", n_rf)
        except sqlite3.OperationalError:
            pass
    except Exception:
        import traceback
        traceback.print_exc()
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    red = sum(1 for g in gaps if g["level"] == "red")
    amber = sum(1 for g in gaps if g["level"] == "amber")
    level = "red" if red else ("amber" if amber else "green")
    return {
        "ok": True,
        "ts": now,
        "level": level,
        "red": red,
        "amber": amber,
        "gap_count": len(gaps),
        "gaps": gaps,
    }


