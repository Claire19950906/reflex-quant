"""结构化输入层 (v173+) - 旁路模块,不动原推理路径。

职责:
  build_structured_input() - LLM 调用前,拼一份 6 字段 JSON payload。
    这份 payload 不是喂给 LLM 的(原 prompt 不变),而是
    (1) 写到 ai_decisions.structured_input,供前端/CLI 结构化展示;
    (2) 在 LLM 返回 reasoning 后,用作引用校验的事实基准(structured_check.py)。

设计原则:
  - 纯读 (SELECT only) + 纯函数
  - 任何上游失败 -> 对应 block 填 [] 或 {},绝不抛
  - token 预算 <= 1500 (粗估 len(json) // 3)
  - 失败模式:字段缺失填 null,不补造
"""
from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Dict, List, Optional

import sqlite3

# token 预算(粗估)。任何 block 超预算 -> 降级
META_BUDGET = 80
EVENTS_BUDGET = 500
SNAPSHOT_BUDGET = 400
BASELINE_BUDGET = 300
TEXT_SUMMARY_BUDGET = 600
# v200+: microstructure(持仓量/资金费率)预算 — 只读最新样本,很轻
MICROSTRUCTURE_BUDGET = 200

# v200+: 默认 microstructure 取哪几个 symbol（与 coinglass_lsr.get_latest_samples_by_symbols 默认一致）
_MICRO_DEFAULT_SYMBOLS = ("BTC", "ETH", "SOL", "XAU")


def _now() -> int:
    return int(time.time())


def _hash_text(text: str) -> str:
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()[:10]


def _safe_str(value, default: str = "") -> str:
    if value is None:
        return default
    try:
        return str(value)
    except Exception:
        return default


def _parse_assets_csv(s: str) -> List[str]:
    if not s:
        return []
    return [a.strip() for a in s.split(",") if a.strip()]


def _text_summary(news_text: str, budget: int = TEXT_SUMMARY_BUDGET) -> str:
    """PR-6 真实摘要：取首句 + 关键句（数字/资产名/百分比/事件关键词）。

    取代 v173 的纯截断:
      1) 首句 200 字(开头信息密度最高)
      2) 在剩余长度中找含数字/%/政策动词 的句子,补 2~3 句
      3) 超 budget 则再截断
    """
    if not news_text:
        return ""
    text = str(news_text).strip()
    if not text:
        return ""

    # 分句(中英文都支持)
    import re
    sents = re.split(r'(?<=[。！？!?\.])\s*', text)
    sents = [s.strip() for s in sents if s and s.strip()]
    if not sents:
        return text[: budget * 3]

    # 关键句过滤:含数字/%/资产/政策动词
    _KEY_PATTERNS = [
        re.compile(r'\d'),  # 数字
        re.compile(r'%'),   # 百分比
        re.compile(r'BTC|ETH|XAU|WTI|原油|黄金|股市|美元|加息|降息|通胀|CPI|非农|FOMC|OPEC|央行'),
    ]
    out = []
    used = 0
    char_cap = budget * 3

    for i, s in enumerate(sents):
        if used + len(s) > char_cap:
            break
        # 首句一定保留
        if i == 0:
            out.append(s)
            used += len(s)
            continue
        # 关键句过滤
        if any(p.search(s) for p in _KEY_PATTERNS):
            out.append(s)
            used += len(s)
        if len(out) >= 5:
            break

    summary = ' '.join(out).strip()
    if len(summary) > char_cap:
        summary = summary[:char_cap - 3] + '...'
    return summary


def _meta_block(news_row):
    '''meta block: news metadata + freshness/hash.'''
    try:
        keys = news_row.keys() if hasattr(news_row, 'keys') else []
        news_id = int(news_row['id']) if 'id' in keys else 0
        source = _safe_str(news_row['source'] if 'source' in keys else '')
        text = _safe_str(news_row['text'] if 'text' in keys else news_row['content'] if 'content' in keys else '')
        published_at = 0
        if 'published_at' in keys and news_row['published_at'] is not None:
            try:
                published_at = int(news_row['published_at'])
            except (TypeError, ValueError):
                published_at = 0
        if not published_at and 'ts' in keys:
            try:
                published_at = int(news_row['ts'])
            except (TypeError, ValueError):
                published_at = 0
    except Exception:
        news_id, source, text, published_at = 0, '', '', 0

    freshness_min = 0
    if published_at > 0:
        freshness_min = max(0, (_now() - published_at) // 60)

    return {
        'news_id': news_id,
        'source': source,
        'published_at': published_at,
        'freshness_min': freshness_min,
        'text_hash': _hash_text(text),
        'text_len': len(text or ''),
    }


def _events_block(event_struct_events, event_atom_rows, budget=EVENTS_BUDGET):
    """Build events array. Priority: event_atom rows, fallback event_struct."""
    events = []

    if event_atom_rows:
        for row in event_atom_rows:
            try:
                keys = row.keys() if hasattr(row, 'keys') else []
                events.append({
                    'atom_id': int(row['id']) if 'id' in keys else 0,
                    'type_code': _safe_str(row['event_type_code'] if 'event_type_code' in keys else row['type_code'] if 'type_code' in keys else ''),
                    'type_name_zh': _safe_str(row['type_name_zh'] if 'type_name_zh' in keys else ''),
                    'actor': _safe_str(row['actor'] if 'actor' in keys else ''),
                    'event_time': int(row['event_time']) if row['event_time'] is not None else 0,
                    'magnitude': {
                        'value': float(row['magnitude_value']) if row['magnitude_value'] is not None else None,
                        'unit': _safe_str(row['magnitude_unit'] if 'magnitude_unit' in keys else ''),
                    },
                    'assets': _parse_assets_csv(_safe_str(row['asset_code'] if 'asset_code' in keys else '')),
                    'direction': _safe_str(row['direction'] if 'direction' in keys else 'unknown'),
                    'direction_confidence': float(row['direction_confidence']) if row['direction_confidence'] is not None else 0.0,
                    'direction_source': _safe_str(row['direction_source'] if 'direction_source' in keys else 'rule_bias'),
                    'evidence_count': int(row['evidence_count']) if row['evidence_count'] is not None else 1,
                    'source': 'event_atom',
                })
            except Exception:
                continue

    if not events and event_struct_events:
        for e in event_struct_events:
            try:
                events.append({
                    'type_code': _safe_str(e.get('type_code', '')),
                    'type_name_zh': _safe_str(e.get('event_type', e.get('type_name_zh', ''))),
                    'actor': _safe_str(e.get('actor', '')),
                    'event_time': int(e.get('event_time', 0)) if e.get('event_time') is not None else 0,
                    'magnitude': {
                        'value': float(e['magnitude_value']) if e.get('magnitude_value') is not None else None,
                        'unit': _safe_str(e.get('magnitude_unit', '')),
                    },
                    'assets': list(e.get('target_assets') or e.get('assets') or []),
                    'direction': _safe_str(e.get('direction', 'unknown')),
                    'direction_confidence': float(e.get('direction_confidence', 0.0)) if e.get('direction_confidence') is not None else 0.0,
                    'direction_source': _safe_str(e.get('direction_source', 'rule_bias')),
                    'evidence_count': 1,
                    'source': 'event_struct',
                })
            except Exception:
                continue

    char_cap = budget * 3
    out, used = [], 0
    for e in events:
        cost = len(json.dumps(e, ensure_ascii=False))
        if used + cost > char_cap and out:
            break
        out.append(e)
        used += cost
    return out


def _snapshot_block(market_snapshot_dict, budget=SNAPSHOT_BUDGET, news_ts=None):
    """Flatten market_snapshot_dict['assets'] into structured array.

    Keep core fields only, budget 400 tokens; overflow cuts trailing assets.

    v174: news_ts added — when provided, enrich entry with:
      - news_ts (事件时间, s, int)
      - snap_ts (快照生成时间, s, int)
      - price_at_news_ts (发布时刻价, 从 market_snapshot_dict.assets[i].klines_at 找最近一根<=news_ts)
      - delta_pct_news_to_now (发布到现在的涨跌幅 %)
    这些字段是 SERVER VERDICT 同源时间结构化输出,保证 LLM reasoning 有"事件时间锚"可引用。
    """
    snap = market_snapshot_dict or {}
    assets_dict = snap.get('assets') or {}
    out = []
    char_cap = budget * 3
    used = 0

    def _f(d, key):
        v = d.get(key)
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    def _ts_to_int(v):
        try:
            return int(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    # 时间锚: snap["ts"] 是 server 端抓快照时打的 ts(server_verdict._SERVER_SNAPSHOT_CACHE 同步)
    snap_ts_int = _ts_to_int(snap.get('ts')) or _now()
    news_ts_int = _ts_to_int(news_ts)

    for asset_id in ('BTC', 'XAU', 'WTI', 'ETH'):
        a = assets_dict.get(asset_id) or {}
        if not a:
            continue
        if a.get('status') == 'unavailable':
            entry = {'asset': asset_id, 'status': 'unavailable'}
        else:
            stats = a.get('stats_7d') or {}
            mtf = a.get('mtf') or {}
            s1 = (mtf.get('1h') or {}).get('indicators') or {}
            current_price = _f(a, 'price')
            entry = {
                'asset': asset_id,
                'price': current_price,
                'price_str': a.get('price_str', ''),
                'trend_1d': stats.get('trend', ''),
                'rsi_1h': _f(s1, 'rsi'),
                'vwap_pos': _f(s1, 'vwap_pos'),
                'vol_ratio_1h': _f(s1, 'volume_ratio'),
                'funding': _f(s1, 'funding_rate'),
                'oi': _f(s1, 'oi'),
                'freshness': (a.get('payload') or {}).get('freshness', 'realtime'),
            }
            # ── v174: 时间结构化字段 ──
            # snap_ts: 快照生成时间(我方 server 拉快照那一刻)
            entry['snap_ts'] = snap_ts_int
            if news_ts_int:
                entry['news_ts'] = news_ts_int
                entry['snap_news_delta_s'] = snap_ts_int - news_ts_int
                # price_at_news_ts: 从 a.klines_at (15m K 线尾部) 找最近 <= news_ts 的 close
                # 兼容两种来源:snap['assets'][id].klines_at['15m'] 或 snap['assets'][id].payload.klines_at
                klines_at = (
                    a.get('klines_at')
                    or (a.get('payload') or {}).get('klines_at')
                    or {}
                )
                k15 = klines_at.get('15m') or []
                price_at_news_ts = None
                for kline in reversed(k15):  # 由旧到新,取最后一个 <= news_ts
                    t_k = _ts_to_int(kline.get('t') or kline.get('open_time'))
                    if t_k and t_k <= news_ts_int:
                        price_at_news_ts = _f(kline, 'c') or _f(kline, 'close')
                        break
                if price_at_news_ts is not None and current_price not in (None, 0):
                    entry['price_at_news_ts'] = price_at_news_ts
                    entry['delta_pct_news_to_now'] = round(
                        (current_price - price_at_news_ts) / price_at_news_ts * 100.0, 3
                    )
        cost = len(json.dumps(entry, ensure_ascii=False))
        if used + cost > char_cap and out:
            break
        out.append(entry)
        used += cost

    return out


def _baseline_block(connection, budget=BASELINE_BUDGET, min_sample=5, days=30):
    """PR-5 真实数据:读 backtest_event_outcomes 缓存表。

    每条形如 {type_code: {sample_n, win_n, loss_n, win_rate_4h, avg_pnl_4h}}
    sample_n < min_sample -> win_rate_4h = null, avg_pnl_4h = 0.0(不补造)。

    注意:这是离线缓存表,由 db.refresh_backtest_event_outcomes() 周期刷新。
         表为空时返回空 dict,不报。

    同时保留 by_type 的 sample_n 统计(查 event_atom),便于快速判断数据稀缺度。
    """
    if connection is None:
        return {
            'by_type': {},
            'by_type_atom_n': {},
            'min_sample': min_sample,
            'days': days,
            'n_total': 0,
            'source': 'none',
        }

    # 1) 真实胜率/平均收益 - 读缓存表
    by_type = {}
    n_total = 0
    try:
        rows = connection.execute(
            """
            SELECT type_code, sample_n, win_n, loss_n, avg_pnl_4h
            FROM backtest_event_outcomes
            WHERE last_updated >= ?
            """,
            (_now() - days * 86400,),
        ).fetchall()
        for r in rows:
            sample_n = int(r['sample_n'] or 0)
            win_n = int(r['win_n'] or 0)
            loss_n = int(r['loss_n'] or 0)
            n_total += sample_n
            entry = {
                'sample_n': sample_n,
                'win_n': win_n,
                'loss_n': loss_n,
            }
            if sample_n < min_sample:
                entry['win_rate_4h'] = None
                entry['avg_pnl_4h'] = 0.0
            else:
                entry['win_rate_4h'] = round(win_n / sample_n, 4) if sample_n else None
                entry['avg_pnl_4h'] = round(float(r['avg_pnl_4h'] or 0.0), 6)
            by_type[_safe_str(r['type_code'], 'unknown')] = entry
    except Exception:
        by_type = {}

    # 2) event_atom 总样本数(数据稀疏度指标)
    by_type_atom_n = {}
    try:
        rows2 = connection.execute(
            """
            SELECT det.code AS type_code, COUNT(ea.id) AS n
            FROM event_atom ea
            JOIN dim_event_type det ON det.id = ea.event_type_id
            WHERE ea.created_at > ?
            GROUP BY det.code
            """,
            (_now() - days * 86400,),
        ).fetchall()
        for r in rows2:
            by_type_atom_n[_safe_str(r['type_code'], 'unknown')] = int(r['n'] or 0)
    except Exception:
        by_type_atom_n = {}

    char_cap = budget * 3
    cost = len(json.dumps({'bt': by_type, 'an': by_type_atom_n}, ensure_ascii=False))
    if cost > char_cap and by_type:
        # 按 sample_n 降序截断,保留头部高频 type_code
        sorted_keys = sorted(by_type.keys(), key=lambda k: by_type[k].get('sample_n', 0), reverse=True)
        by_type = {k: by_type[k] for k in sorted_keys[:6]}

    return {
        'by_type': by_type,
        'by_type_atom_n': by_type_atom_n,
        'min_sample': min_sample,
        'days': days,
        'n_total': n_total,
        'source': 'backtest_event_outcomes+event_atom' if by_type else 'empty',
    }


def _extract_target_assets(snapshot: List[dict], relations: List[dict]) -> List[str]:
    """v200+: 从 snapshot / relations 推断本次要查 microstructure 的主品种。

    优先级:snapshot[0].asset > relations 中资产(出现频次最多) > 默认四件套。
    snapshot 是 _snapshot_block 输出 [{asset, funding, ...}], 长度通常 1~6。
    """
    out: List[str] = []
    if isinstance(snapshot, list):
        for s in snapshot:
            if not isinstance(s, dict):
                continue
            a = str(s.get("asset") or s.get("symbol") or "").upper().strip()
            if a and a not in out and a not in ("", "N/A"):
                out.append(a)
    if not out and isinstance(relations, list):
        from collections import Counter
        cnt: Counter = Counter()
        for r in relations:
            if isinstance(r, dict):
                a = str(r.get("asset") or "").upper().strip()
                if a and a not in ("", "N/A"):
                    cnt[a] += 1
        for a, _ in cnt.most_common(3):
            if a not in out:
                out.append(a)
    if not out:
        out = list(_MICRO_DEFAULT_SYMBOLS)
    return out[:5]


def _microstructure_block(target_assets: List[str], budget=MICROSTRUCTURE_BUDGET) -> Dict[str, Any]:
    """v200+: 拼 microstructure JSON 块,供 ai_prompts + 前端结构化展示。

    输出 schema:
      {
        'lsr': {symbol: {ratio, delta_4h_pct, ov_long_qty, ov_short_qty,
                          whale_long_qty, whale_short_qty, oi_mcap, cache_age_s, stale}},
        'funding': {symbol: {funding_rate_pct, next_funding_time, is_extreme}},
        'stale_symbols': [sym, ...],   # cache_age_s > 3600 → 标 stale
        'fresh_ts': int,                # 拉数据的最新时间戳
        'source': 'coinglass_lsr+funding_rate_feed' | 'partial' | 'empty'
      }

    失败/无样本 → 返回 source='empty',不抛。
    """
    syms = tuple(a.upper() for a in target_assets if a)
    # 补默认:始终把 BTC/ETH/SOL/XAU 也带回来作为参照系（覆盖范围广,事件逻辑链常用）
    for d in _MICRO_DEFAULT_SYMBOLS:
        if d not in syms:
            syms = syms + (d,)
    syms = syms[:6]  # 截断

    out: Dict[str, Any] = {
        "lsr": {}, "funding": {}, "stale_symbols": [],
        "fresh_ts": _now(), "source": "empty",
    }

    # ── 1) LSR(持仓量/多空比/大户持仓) from coinglass_lsr ──
    try:
        import coinglass_lsr
        lsr_map = coinglass_lsr.get_latest_samples_by_symbols(symbols=syms, mode="trader")
        for sym, sample in lsr_map.items():
            if not isinstance(sample, dict):
                continue
            out["lsr"][sym] = {
                "ratio": sample.get("ratio", 0.0),
                "ratio_pct": sample.get("ratio_pct", ""),
                "delta_4h_pct": sample.get("delta_4h_pct", ""),
                "delta_30m_pct": sample.get("delta_30m_pct", ""),
                "ov_long_qty": sample.get("ov_long_qty", 0.0),
                "ov_short_qty": sample.get("ov_short_qty", 0.0),
                "whale_long_qty": sample.get("whale_long_qty", 0.0),
                "whale_short_qty": sample.get("whale_short_qty", 0.0),
                "oi_mcap": sample.get("oi_mcap", 0.0),
                "cache_age_s": sample.get("cache_age_s", 0),
            }
            if sample.get("stale"):
                out["stale_symbols"].append(sym)
            if sample.get("ts"):
                out["fresh_ts"] = max(out["fresh_ts"], int(sample["ts"]))
    except Exception:
        pass

    # ── 2) Funding Rate(永续费率) from funding_rate_feed ──
    try:
        import funding_rate_feed
        # funding_rate_feed 内部用 inst_id,需把 BTC → BTC-USDT-SWAP / XAU → XAU-USDT-SWAP
        inst_map = {
            "BTC": "BTC-USDT-SWAP", "ETH": "ETH-USDT-SWAP",
            "SOL": "SOL-USDT-SWAP", "XAU": "XAU-USDT-SWAP",
        }
        fr_insts = [inst_map[s] for s in syms if s in inst_map]
        if fr_insts:
            fr = funding_rate_feed.fetch_funding_rates(instruments=fr_insts)
            if isinstance(fr, dict) and fr.get("ok") and isinstance(fr.get("items"), list):
                for it in fr["items"]:
                    if not isinstance(it, dict):
                        continue
                    inst = str(it.get("instrument") or "")
                    sym = inst.split("-")[0] if inst else ""
                    rate = it.get("funding_rate")
                    if sym and rate is not None:
                        out["funding"][sym] = {
                            "funding_rate_pct": round(float(rate) * 100, 4),
                            "next_funding_time": it.get("next_funding_time"),
                            "is_extreme": abs(float(rate)) >= 0.0005,
                        }
                # extreme 字段（注入提醒用）
                if isinstance(fr.get("extreme"), list) and fr["extreme"]:
                    out["funding_extreme"] = fr["extreme"]
    except Exception:
        pass

    # ── 3) source 判定 ──
    has_lsr = bool(out["lsr"])
    has_fund = bool(out["funding"])
    if has_lsr and has_fund:
        out["source"] = "coinglass_lsr+funding_rate_feed"
    elif has_lsr or has_fund:
        out["source"] = "partial"
    return out


def _relations_block(news_id, connection, budget=200):
    """PR-4 真实数据:同条新闻的 event_atom 之间的「资产共现」关系。

    输入:news_id (raw_news.id) + connection。
    逻辑:取该新闻下的所有 event_atom,按 asset 维度构造二元组
         [{atom_a_id, atom_b_id, type_codes, asset, n_shared_assets}]。
    截断:保留前 budget token(粗估 char_cap = budget*3)。

    返回 [] if news_id == 0 or connection is None or 无 event_atom。
    """
    if connection is None or not news_id or news_id <= 0:
        return []

    try:
        atom_rows = connection.execute(
            """
            SELECT ea.id AS atom_id,
                   det.code AS type_code,
                   COALESCE(da.code, '') AS asset
            FROM event_atom ea
            JOIN dim_event_type det ON det.id = ea.event_type_id
            LEFT JOIN dim_asset da ON da.id = ea.asset_id
            WHERE ea.raw_news_id = ?
            ORDER BY ea.id
            """,
            (int(news_id),),
        ).fetchall()
    except Exception:
        return []

    if not atom_rows or len(atom_rows) < 2:
        # 只有 0 或 1 个 event_atom,没有「关系」可言
        return []

    # 二元共现:同一资产被两个事件影响 → relation 候选
    by_asset = {}
    for r in atom_rows:
        a = _safe_str(r['asset'], '')
        if not a:
            continue
        by_asset.setdefault(a, []).append({
            'atom_id': int(r['atom_id']),
            'type_code': _safe_str(r['type_code']),
        })

    rels = []
    for asset, atoms in by_asset.items():
        if len(atoms) < 2:
            continue
        # 同资产 N 个事件 → C(N, 2) 个 relation 对
        for i in range(len(atoms)):
            for j in range(i + 1, len(atoms)):
                rels.append({
                    'atom_a_id': atoms[i]['atom_id'],
                    'atom_b_id': atoms[j]['atom_id'],
                    'shared_asset': asset,
                    'type_codes': sorted({atoms[i]['type_code'], atoms[j]['type_code']}),
                })

    char_cap = budget * 3
    out, used = [], 0
    for r in rels:
        cost = len(json.dumps(r, ensure_ascii=False))
        if used + cost > char_cap and out:
            break
        out.append(r)
        used += cost
    return out


def build_structured_input(
    news_row,
    market_snapshot_dict=None,
    event_struct_events=None,
    event_atom_rows=None,
    connection=None,
    news_ts=None,
):
    """Main entry: assemble 6-field structured payload.

    Each block fails -> fills [] or {}, never raises.

    v174: news_ts — 显式事件时间(秒)。来自 news_row['timestamp'] 或 raw_news.published_at。
          用于 _snapshot_block 注入 price_at_news_ts / snap_news_delta_s。
    """
    try:
        meta = _meta_block(news_row)
    except Exception:
        meta = {'news_id': 0, 'source': '', 'published_at': 0,
                'freshness_min': 0, 'text_hash': '', 'text_len': 0}

    try:
        events = _events_block(event_struct_events, event_atom_rows)
    except Exception:
        events = []

    # v174: 若未显式传 news_ts,优先用 meta.published_at(meta 块已经解析过 raw_news.timestamp)
    if news_ts is None:
        news_ts = meta.get('published_at') if isinstance(meta, dict) else None

    try:
        snapshot = _snapshot_block(market_snapshot_dict, news_ts=news_ts)
    except Exception:
        snapshot = []

    try:
        baseline = _baseline_block(connection)
    except Exception:
        baseline = {'by_type': {}, 'by_type_atom_n': {}, 'min_sample': 5,
                    'days': 30, 'n_total': 0, 'source': 'fallback'}

    try:
        relations = _relations_block(int(meta.get('news_id', 0) or 0), connection)
    except Exception:
        relations = []

    # v200+: microstructure(持仓/资金费率) — 注入 LLM 决策前的最新微观结构
    try:
        microstructure = _microstructure_block(
            target_assets=_extract_target_assets(snapshot, relations),
        )
    except Exception:
        microstructure = {'lsr': {}, 'funding': {}, 'stale_symbols': [], 'source': 'fallback'}

    try:
        keys = news_row.keys() if hasattr(news_row, 'keys') else []
        text = _safe_str(news_row['text'] if 'text' in keys else news_row['content'] if 'content' in keys else '')
    except Exception:
        text = ''
    text_summary = _text_summary(text)

    return {
        'meta': meta,
        'events': events,
        'snapshot': snapshot,
        'relations': relations,
        'baseline': baseline,
        'microstructure': microstructure,
        'text_summary': text_summary,
    }


def serialize(payload):
    """Convert to JSON string, truncate if > 6000 chars (DB size guard)."""
    try:
        s = json.dumps(payload, ensure_ascii=False, default=str)
    except Exception:
        s = json.dumps({'meta': payload.get('meta', {}), 'events': [],
                        'snapshot': [], 'relations': [],
                        'baseline': {}, 'text_summary': ''},
                       ensure_ascii=False)
    if len(s) > 6000:
        payload2 = dict(payload)
        if 'text_summary' in payload2:
            payload2['text_summary'] = str(payload2['text_summary'])[:300]
        s = json.dumps(payload2, ensure_ascii=False, default=str)
        if len(s) > 6000:
            s = s[:5997] + '...'
    return s


def estimate_tokens(payload):
    """Rough token estimate (len(json) // 3)."""
    try:
        s = json.dumps(payload, ensure_ascii=False, default=str)
        return len(s) // 3
    except Exception:
        return 0
