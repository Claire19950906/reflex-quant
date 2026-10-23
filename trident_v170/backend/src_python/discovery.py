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
import sqlite3
import time
from typing import Any, Dict, List

try:
    from . import db
except ImportError:
    import db  # type: ignore


_CRYPTO_KEYWORDS = ('BTC', 'ETH', 'SOL', 'USDT', 'USDC', 'DOGE', 'XRP', 'ADA', 'BNB', 'MATIC')
_HYNIX_KEYWORDS = ('000660', '005930', 'HYNIX', 'SK.HYNIX', '海力士', '存储')


def _bucket_of(symbol: str) -> str:
    s = symbol.upper()
    if any(k in s for k in _CRYPTO_KEYWORDS):
        return 'altcoin'
    if any(k in s for k in _HYNIX_KEYWORDS):
        return 'news_stock'
    return 'news_stock'


# Fed → 跨资产方向映射(启发式)
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


def _derive_direction_from_ust2y(ust2y_change_bp: float) -> str:
    if ust2y_change_bp >= 5:
        return 'hawkish'
    if ust2y_change_bp <= -5:
        return 'dovish'
    return 'neutral'


def _compute_fed_impacts(ust2y_change_bp: float) -> Dict[str, Any]:
    direction = _derive_direction_from_ust2y(ust2y_change_bp)
    impacts = []
    for sym, name, cls, sign, note in IMPACT_CATALOG:
        norm = max(-1.0, min(1.0, sign * ust2y_change_bp / 25.0))
        impacts.append({
            'symbol': sym,
            'name': name,
            'cls': cls,
            'correlation': round(norm, 2),
            'typical_lag_hours': 4,
            'sample_size': 0,
            'note': note,
        })
    return {
        'ok': True,
        'ts': time.time(),
        'current_direction': direction,
        'current_change_bp': round(ust2y_change_bp, 1),
        'impacts': impacts,
        'note': '启发式输出,基于 Fed 路径 → 品种方向 的经验映射',
    }


async def fed_impact_handler() -> Dict[str, Any]:
    """GET /api/discovery/fed-impact"""
    ust2y_change = 0.0
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
    except Exception:
        pass
    return _compute_fed_impacts(ust2y_change)


async def asset_scout_handler() -> Dict[str, Any]:
    """GET /api/discovery/asset-scout"""
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
    return {"ok": True, "ts": time.time(), "items": items[:30], "news_density": []}


async def cycle_scan_handler() -> Dict[str, Any]:
    """GET /api/discovery/cycle-scan — v226+ 接 cycle_analyst.

    数据源：kline_bars（symbol + timeframe + ts + close）。
    取最近 60 根 1d K 线 → 调 detect_phase / detect_cycle_length /
    predict_reversal / multi_timeframe_signal。

    启发式 fallback：kline_bars < 10 根时退回 v225 的 pct_7d 启发式
    （用 factor_snapshots.final_score 序列）— 保持响应不空。
    """
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

        if not rows and not used_fallback:
            used_fallback = True
            rows = _cycle_scan_factor_fallback()
    except Exception:
        pass
    return {"ok": True, "ts": time.time(), "rows": rows, "fallback": used_fallback}


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


async def event_radar_handler() -> Dict[str, Any]:
    """GET /api/discovery/event-radar"""
    items: List[Dict[str, Any]] = []
    try:
        conn = sqlite3.connect(db.get_db_path())
        conn.row_factory = sqlite3.Row
        try:
            try:
                rows_db = conn.execute(
                    "SELECT id, headline, ts, impact_score FROM event_atom "
                    "WHERE ts >= ? ORDER BY ts DESC LIMIT 20",
                    (int(time.time()) - 4 * 3600,),
                ).fetchall()
                # v226+ Q5 闭环：批量取所有 event_atom_id → ai_decisions 映射（避免 N+1）
                atom_ids = [int(r['id']) for r in rows_db]
                decisions_map = _fetch_decisions_by_atom_ids(conn, atom_ids)
                for r in rows_db:
                    atom_id = int(r['id'])
                    affected = decisions_map.get(atom_id, [])
                    items.append({
                        'event_id': str(atom_id),
                        'title': r['headline'],
                        'ts': r['ts'],
                        'age_minutes': int((time.time() - r['ts']) / 60),
                        'urgency': 'high' if r['impact_score'] > 0.7 else 'medium' if r['impact_score'] > 0.4 else 'low',
                        'likely_affected': affected,
                        'likely_affected_count': len(affected),
                        'confidence': r['impact_score'],
                        'hint': _format_affected_hint(affected),
                    })
            except Exception:
                pass
        finally:
            conn.close()
    except Exception:
        pass
    return {"ok": True, "ts": time.time(), "items": items}


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


# ── Undervalued Detector 启发式 ─────────────────────────────────────────
# 启发式定义：满足以下条件越多 → "被严重低估/刚被严重砸"分越高：
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


async def undervalued_detector_handler() -> Dict[str, Any]:
    """GET /api/discovery/undervalued-detector — 类似海力士被严重低估/砸过的标的"""
    items: List[Dict[str, Any]] = []
    method = "empty"
    try:
        import cycle_analyst as ca

        conn = sqlite3.connect(db.get_db_path())
        conn.row_factory = sqlite3.Row
        try:
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
                d["undervalued_score"] = round(min(score, 100.0), 1)
                d["reasons"] = reasons

                if d["undervalued_score"] >= 60:
                    d["hint"] = ("严重低估/超跌 — 关注反转确认信号后再入场" if d["phase"] == "accumulation"
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

    return {
        "ok": True,
        "ts": int(time.time()),
        "items": items,
        "method": method,
        "note": ("海力士启发式：7d 跌幅≥10% + 30d 跌幅≥15% + cycle=accumulation + "
                 "reversal_prob≥0.5 + 情绪转暖 → 综合分 0-100"),
    }


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
    window_s = _RANK_TIMEFRAMES.get(timeframe, 86400)
    items: List[Dict[str, Any]] = []
    gainers: List[Dict[str, Any]] = []
    losers: List[Dict[str, Any]] = []
    crashes: List[Dict[str, Any]] = []
    category_breakdown: Dict[str, Any] = {}
    method = "empty"

    try:
        conn = sqlite3.connect(db.get_db_path())
        conn.row_factory = sqlite3.Row
        try:
            cutoff = int(time.time()) - window_s
            sym_window = conn.execute(
                "SELECT asset, value, ts FROM external_indicators "
                "WHERE ts >= ? AND indicator NOT IN ('UST2Y','DXY','VIX') "
                "ORDER BY asset, ts DESC",
                (cutoff,),
            ).fetchall()

            by_asset: Dict[str, List[Any]] = {}
            for r in sym_window:
                by_asset.setdefault(r["asset"], []).append(r)

            for asset, rows in by_asset.items():
                if len(rows) < 2:
                    continue
                latest = float(rows[0]["value"])
                earliest = float(rows[-1]["value"])
                if earliest <= 0 or latest <= 0:
                    continue
                change_pct = (latest - earliest) / earliest * 100
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

    return {
        "ok": True,
        "ts": int(time.time()),
        "timeframe": timeframe,
        "window_s": window_s,
        "total_count": len(items),
        "gainers": gainers,
        "losers": losers,
        "crashes": crashes,
        "crash_threshold_pct": crash_threshold_pct,
        "category_breakdown": category_breakdown,
        "method": method,
        "note": ("crashes = timeframe 内跌幅 ≤ crash_threshold_pct 的标的，"
                 "可与 undervalued_detector 联动"),
    }


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
            try:
                ar = conn.execute(
                    "SELECT main_asset, COUNT(*) AS c FROM raw_news "
                    "WHERE timestamp >= datetime('now','localtime','-24 hours') "
                    "AND gate_verdict='material' "
                    "GROUP BY main_asset ORDER BY c DESC LIMIT 5"
                ).fetchall()
                atotal = conn.execute(
                    "SELECT COUNT(*) FROM raw_news "
                    "WHERE timestamp >= datetime('now','localtime','-24 hours') "
                    "AND gate_verdict='material'"
                ).fetchone()[0]
            except sqlite3.OperationalError:
                ar, atotal = [], 0
            top_assets = [{"asset": str(r["main_asset"] or ""), "count": int(r["c"])} for r in ar]
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
                    "ORDER BY score DESC LIMIT 5"
                ).fetchall()
                etotal = conn.execute(
                    "SELECT COUNT(*) FROM news_events "
                    "WHERE ts >= strftime('%s','now','-24 hours')"
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
            top_cycle = [
                {
                    "asset": str(r["asset"] or ""),
                    "phase": str(r["phase"] or ""),
                    "direction": str(r["direction"] or ""),
                    "prob": float(r["prob"] or 0),
                }
                for r in crows[:3]
            ]
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
            pillars = [
                {"key": "intraday", "label": "日内流动性", "last_update": None, "count_24h": 0, "status": "empty"},
                {"key": "smart_money", "label": "聪明钱追踪", "last_update": None, "count_24h": 0, "status": "empty"},
                {"key": "fed_watch", "label": "Fed 全景", "last_update": None, "count_24h": 0, "status": "empty"},
                {"key": "asset_scout", "label": "资产猎手", "last_update": None, "count_24h": int(atotal), "status": ("fresh" if atotal else "empty")},
                {"key": "event_radar", "label": "事件雷达", "last_update": None, "count_24h": int(etotal), "status": ("fresh" if etotal else "empty")},
                {"key": "cycle_radar", "label": "周期维度", "last_update": None, "count_24h": int(ctotal), "status": ("fresh" if ctotal else "empty")},
                {"key": "weekly", "label": "周度观察", "last_update": None, "count_24h": 0, "status": "empty"},
                {"key": "macro_calendar", "label": "宏观日历", "last_update": None, "count_24h": 0, "status": "empty"},
            ]
            out["pillars"] = pillars

            # ── loop_stats: 闭环 24h ──
            try:
                into_chains = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM causal_chains "
                        "WHERE updated_at >= datetime('now','localtime','-24 hours')"
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
                "scanned_24h": int(atotal) + int(ctotal),
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