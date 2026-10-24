"""
v302+ P0: 因果图谱反馈评估器 (causal_chain_evaluator)
==================================================
需求 8 核心: "排除干扰 → 判断哪些因果链对, 哪些错, 再纠正"

backfill: 扫过去 N 天 news_events, 用 chain.trigger_keywords 匹配,
           写入 causal_chain_outcomes (PENDING).

evaluate: 扫 PENDING 且 eval_deadline 已到, 查 ohlcv T+5 真实涨跌,
          判定 HIT/MISS/NEUTRAL, 同步 causal_chains.hit/miss.

判定规则:
  pred_dir=UP   → price[T+5]>price[T+0] 为方向对
  pred_dir=DOWN → price[T+5]<price[T+0] 为方向对
  pred_dir=BOTH → |pct_5d|>=mag_lo 为方向对
  HIT = dir 对 AND mag 落在 [lo,hi]
  MISS = dir 反
  NEUTRAL = dir 对但 mag 不在 [lo,hi]
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from typing import Iterable, Optional

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "trident_event_bus.db")
EVAL_HORIZON_DAYS = 5
TIME_HORIZON_LOOKBACK = 90
TRIGGER_COOLDOWN_HOURS = 48

# --- schema 适配常量 (causal_chains 已重设计, 见 migrate_causal_chains.py) ---
# 新表无 expected_direction/magnitude_lo/hi, 评估所需的预测必须在 backfill 时从
# 链的文本(name/cause/effect)推断并快照进 causal_chain_outcomes.predicted_*。
DEFAULT_MAG_LO = 1.0
DEFAULT_MAG_HI = 8.0

# trigger_words 里的结构性 token, 不是真正的触发关键词
_CHAIN_STOPWORDS = {"asset", "prompt", "verdict", "event", "eventtype"}

# 方向推断词典见下方 _ACTION_UP/_ACTION_DOWN/_UP_WORDS/_DOWN_WORDS/_NEUTRAL_WORDS

# v302+ P2: 资产别名映射 — 让 chain.asset 能在 headline/body 文本里命中同义/中文写法,
# 避免 XAU/WTI/SOL/ADA/SKHYNIX 等资产因为 news.assets 字段没标而漏匹配.
ASSET_ALIASES = {
    "XAU": ["xau", "gold", "黄金", "gc1", "spot_gold", "金价"],
    "WTI": ["wti", "oil", "原油", "crude", "petroleum", "brent"],
    "SOL": ["sol", "solana"],
    "ADA": ["ada", "cardano", "艾达"],
    "SKHYNIX": ["skhynix", "hynix", "000660", "海力士", "sk_hynix", "hbm", "存储", "内存"],
    "BTC": ["btc", "bitcoin", "比特币"],
    "ETH": ["eth", "ethereum", "以太", "以太坊"],
    "NVDA": ["nvda", "nvidia", "英伟达"],
    "TSLA": ["tsla", "tesla", "特斯拉"],
    "MU": ["mu", "micron", "美光"],
    "BNB": ["bnb", "binance", "币安"],
    "XRP": ["xrp", "ripple"],
    "DOGE": ["doge", "dogecoin", "狗狗币"],
}


@dataclass
class ChainRow:
    chain_id: str
    asset: str
    trigger_keywords: list[str]
    expected_direction: str
    magnitude_lo: float
    magnitude_hi: float
    confidence: float


def _split_keywords(s):
    if not s:
        return []
    return [k.strip() for k in s.split(",") if k.strip()]


def _load_priced_assets(conn):
    """返回有行情数据的资产集合 —— 只有这些资产才能做 T+5 评估."""
    try:
        return {r[0] for r in conn.execute("SELECT DISTINCT asset FROM ohlcv_daily")}
    except sqlite3.Error:
        return set()


# 明确的交易操作词 —— 优先级最高, 直接说明作者想要什么方向.
# 注意: 这些必须先于"利好/利空"判断, 因为 "美俄石油交易利空→WTI传导" 里
# "利空" 描述的是*触发原因*, 而链条结论是 WTI 上行.
_ACTION_UP = ("做多", "买入", "buy", "long", "加仓", "看涨")
_ACTION_DOWN = ("做空", "卖出", "sell", "short", "减仓", "看跌")
# 结论性方向词 (次优先级)
_UP_WORDS = ("上涨", "走强", "上行", "拉涨", "提振", "偏多", "利多", "看多",
             "多头", "牛市", "涨", "bull", "正向冲击")
_DOWN_WORDS = ("下跌", "走弱", "下行", "回落", "杀跌", "承压", "偏空",
               "看空", "空头", "熊市", "跌", "bear", "负向冲击")
# 双向/无方向 —— 出现即判 BOTH, 不做倾向
_NEUTRAL_WORDS = ("双向", "分化", "震荡", "有限", "模糊", "不确定", "不涨反跌")


def _infer_direction(name):
    """从链的 name 推断预期方向.

    v302+ 只用 name, 不用 cause/effect —— effect 字段 60% 含事后复盘结论
    (WIN/LOSS/方向正确/兑现), 用它推导"预测"会造成数据泄漏, 让 hit rate 虚高.

    优先级: 双向词 > 操作词(做多/做空) > 结论词 > BOTH
    """
    if not name:
        return "BOTH"
    low = name.lower()
    if any(w in low for w in _NEUTRAL_WORDS):
        return "BOTH"
    # 取最后一个操作词: "中东地缘冲突→原油做空 SELL" 里后者是最终结论
    up_pos = max([low.rfind(w) for w in _ACTION_UP] + [-1])
    dn_pos = max([low.rfind(w) for w in _ACTION_DOWN] + [-1])
    if up_pos >= 0 or dn_pos >= 0:
        return "UP" if up_pos > dn_pos else "DOWN"
    up = sum(1 for w in _UP_WORDS if w in low)
    down = sum(1 for w in _DOWN_WORDS if w in low)
    if up > down:
        return "UP"
    if down > up:
        return "DOWN"
    return "BOTH"


def load_active_chains(conn, priced_assets=None):
    """加载可用于评估的因果链.

    v302 兼容层: causal_chains 表已被 migrate_causal_chains.py 重设计, 旧列
    (chain_id/asset/expected_direction/magnitude_lo/hi/confidence/trigger_keywords/active)
    全部不存在. 新结构把资产编码在 trigger_words 的 "Asset,<TICKER>" 前缀里,
    方向则需从 name/cause/effect 文本推断.
    """
    if priced_assets is None:
        priced_assets = _load_priced_assets(conn)
    rows = conn.execute(
        """SELECT id, name, cause, effect, trigger_words, weight
           FROM causal_chains WHERE enabled=1"""
    ).fetchall()
    out = []
    for r in rows:
        parts = [p.strip() for p in (r[4] or "").split(",") if p.strip()]
        # 资产: 取 trigger_words 里第一个能匹配行情的 ticker
        asset = None
        for p in parts:
            pu = p.upper()
            if pu in priced_assets:
                asset = pu
                break
        if not asset:
            continue  # 无行情 → 无法评估, 跳过
        # 剔除所有 ticker (不只选中的那个): 一条链常同时提到多个资产
        # (如 WTI/XAU/BTC), 残留 ticker 会被当成触发词造成跨资产串味.
        kws = [p for p in parts
               if p.lower() not in _CHAIN_STOPWORDS and p.upper() not in priced_assets]
        if not kws:
            continue
        out.append(ChainRow(
            chain_id=r[0],
            asset=asset,
            trigger_keywords=kws,
            expected_direction=_infer_direction(r[1]),
            magnitude_lo=DEFAULT_MAG_LO,
            magnitude_hi=DEFAULT_MAG_HI,
            confidence=float(r[5] or 0.5),
        ))
    return out


def derive_strategy_columns(conn, chain_ids=None) -> dict:
    """把"策略链路"列从知识库列派生出来 —— 与 load_active_chains 完全同源.

    背景: causal_chains 一表两用。
      知识列 (name/cause/effect/trigger_words/enabled/origin/weight) —— 真实数据源
      策略列 (chain_id/asset/trigger_keywords/expected_direction/magnitude_*) ——
        bridge_server 与反馈闭环按这些列查询, 缺失时整条链路静默失效。

    派生规则 (与 load_active_chains 逐条一致, 避免两套语义漂移):
      chain_id            = CAST(id AS TEXT)   —— 必须与 outcomes.chain_id 同键
      asset               = trigger_words 里第一个有行情的 ticker, 无则 'UNKNOWN'
      trigger_keywords    = trigger_words 去掉全部 ticker 与结构性 stopword 后的剩余词
      expected_direction  = _infer_direction(name)
      magnitude_lo / hi   = DEFAULT_MAG_LO / DEFAULT_MAG_HI
      confidence          = weight (夹到 [0, 1])

    asset 必须是真实 ticker: bridge 的 _match_chains_for_outcome 用
    `WHERE active=1 AND asset=?` 匹配, asset 为 'UNKNOWN' 时命中恒为 0,
    hit/miss 反馈闭环会完全空转。

    chain_ids=None → 全表; 否则只处理指定 id (新建链入库后立即调用)。
    返回统计 dict: {"updated", "with_asset", "priced_assets"}。
    """
    priced_assets = _load_priced_assets(conn)

    if chain_ids is None:
        rows = conn.execute(
            "SELECT id, name, trigger_words, weight FROM causal_chains"
        ).fetchall()
    else:
        ids = [int(i) for i in chain_ids]
        if not ids:
            return {"updated": 0, "with_asset": 0, "priced_assets": len(priced_assets)}
        qmarks = ",".join("?" * len(ids))
        rows = conn.execute(
            f"SELECT id, name, trigger_words, weight FROM causal_chains "
            f"WHERE id IN ({qmarks})", ids
        ).fetchall()

    updated = with_asset = 0
    for cid, name, trigger_words, weight in rows:
        parts = [p.strip() for p in (trigger_words or "").split(",") if p.strip()]

        # 资产: trigger_words 里第一个能匹配行情的 ticker
        asset = None
        for p in parts:
            if p.upper() in priced_assets:
                asset = p.upper()
                break

        # 关键词: 剔除所有 ticker (不只选中的那个) 与结构性 stopword,
        # 否则残留 ticker 会被当成触发词造成跨资产串味。
        kws = [p for p in parts
               if p.lower() not in _CHAIN_STOPWORDS and p.upper() not in priced_assets]

        try:
            conf = float(weight)
        except (TypeError, ValueError):
            conf = 0.5
        conf = max(0.0, min(1.0, conf))

        if asset is None:
            # 无行情资产 → bridge 无法按 asset 命中, 保持 UNKNOWN 且不给关键词
            new_asset, new_kws = "UNKNOWN", ""
        else:
            new_asset, new_kws = asset, ",".join(kws)
            with_asset += 1

        conn.execute(
            "UPDATE causal_chains SET chain_id=CAST(id AS TEXT), asset=?, trigger_keywords=?,"
            " expected_direction=?, magnitude_lo=?, magnitude_hi=?, confidence=?,"
            " updated_at=strftime('%s','now') WHERE id=?",
            (new_asset, new_kws, _infer_direction(name),
             DEFAULT_MAG_LO, DEFAULT_MAG_HI, conf, cid),
        )
        updated += 1

    return {"updated": updated, "with_asset": with_asset,
            "priced_assets": len(priced_assets)}


_COMMON_TICKERS = {"btc", "eth", "sol", "solana", "xrp", "bnb", "ada", "doge", "mu",
                   "tsla", "nvda", "wti", "xau", "gold", "oil", "skhynix", "micron",
                   "cardano", "fed", "fomc", "sec", "opec"}
# 常见 ticker/通用词 —— 单独出现不足以判定链路相关, 需额外 keyword 确认


def meaningful_keyword_hit(text: str, kws: Iterable[str], asset: str = "") -> list[str]:
    """返回命中的关键词, 但要求至少命中一个"有意义"的关键词。

    规则 (与 _kw_match 一致, 供 bridge 等其它消费者复用):
      - 纯 ASCII 关键词: 需 len>=4 且不在常见 ticker/通用词集合里
      - 含非 ASCII 的关键词: 需 len>=2

    只有 ticker 或过短词命中时视为未命中 —— 否则 'buy'/'sell' 这类
    高频词会让所有链同时命中, 命中量被严重放大。
    asset: 该链自身的资产代码, 同样不算有意义命中 (避免资产名自证)。
    """
    text = text or ""
    text_lower = text.lower()
    tickers = set(_COMMON_TICKERS)
    if asset:
        tickers.add(asset.lower())
    hit: list[str] = []
    meaningful: list[str] = []
    for k in kws or []:
        if not k:
            continue
        if re.match(r"^[A-Za-z0-9_]+$", k):
            if k.lower() in text_lower:
                hit.append(k)
                if k.lower() not in tickers and len(k) >= 4:
                    meaningful.append(k)
        else:
            if k in text:
                hit.append(k)
                if len(k) >= 2:
                    meaningful.append(k)
    return hit if meaningful else []


def _kw_match(headline, body, kws, asset):
    """返回匹配上的 keyword 列表.
    规则: 至少 1 个非资产代码/常见词关键词命中 (避免单纯 ticker 误匹配)."""
    return meaningful_keyword_hit((headline or "") + " " + (body or ""), kws, asset)


def _load_news_with_content(conn, days):
    """一次性载入窗口内新闻+正文.

    backfill 会对每条链调用 find_news_triggers, 若每次都全表扫描 5966 行 × 270 条链
    会退化. 这里做进程内缓存 (同一 conn 下结果不变).
    """
    key = "_news_cache_%s" % days
    cached = getattr(conn, key, None)
    if cached is not None:
        return cached
    start_ts = int(time.time()) - days * 86400
    rows = conn.execute(
        """SELECT ne.id, rn.content, ne.asset, ne.ts, ne.action, ne.news_id
           FROM news_events ne
           LEFT JOIN raw_news rn ON rn.id = ne.news_id
           WHERE ne.ts>=? ORDER BY ne.ts DESC""",
        (start_ts,),
    ).fetchall()
    try:
        setattr(conn, key, rows)
    except AttributeError:
        pass  # sqlite3.Connection 不允许任意属性时忽略缓存
    return rows


def find_news_triggers(conn, chain, days):
    """扫描新闻, 找触发 chain 的事件.

    v302 兼容层: news_events 表没有 headline/body/assets 列, 实际列为
    (id, news_id, decision_id, ts, source, asset, action, score, is_noise, status).
    标题正文在 raw_news.content, 通过 news_id = raw_news.id 关联 (约 27% 可关联).
    """
    rows = _load_news_with_content(conn, days)
    seen_ts = []
    # v302+ P2: 资产别名集合 — 用 chain.asset 的别名在正文里命中,
    # 避免 news.asset 字段不完整导致漏匹配 (XAU→黄金/WTI→原油 等).
    aliases = ASSET_ALIASES.get(chain.asset.upper(), [chain.asset.lower()])
    alias_lower = [a.lower() for a in aliases]
    for r in rows:
        content = r[1] or ""
        news_asset = (r[2] or "")
        text_lower = content.lower()
        # 双通道匹配: (a) news_events.asset 等于 chain.asset
        #           (b) 正文含 chain.asset 别名
        asset_match = (news_asset or "").upper() == chain.asset.upper()
        alias_match = bool(content) and any(a in text_lower for a in alias_lower)
        if not (asset_match or alias_match):
            continue
        hit = _kw_match(content, "", chain.trigger_keywords, chain.asset)
        if not hit:
            continue
        if any(abs(r[3] - t) < TRIGGER_COOLDOWN_HOURS * 3600 for t in seen_ts):
            continue
        seen_ts.append(r[3])
        yield {
            "id": r[0], "news_id": r[5], "headline": content,
            "asset": news_asset, "ts": r[3], "action": r[4],
            "matched_kws": hit,
        }


def get_close_at_or_before(conn, asset, ts):
    row = conn.execute(
        "SELECT close FROM ohlcv_daily WHERE asset=? AND ts<=? ORDER BY ts DESC LIMIT 1",
        (asset, ts),
    ).fetchone()
    return float(row[0]) if row else None


def get_close_t_plus_n(conn, asset, start_ts, n):
    """start_ts 当日或之后第 n 个交易日的 close (0=T+0, 4=T+5 第 5 个).
    注意: ohlcv_daily 的 ts 是当日 12:00 (yfinance 写入时区), 触发事件 ts 可能晚于 12:00,
    所以用 14 天回溯窗口确保有 5 个连续交易日."""
    row = conn.execute(
        """SELECT close, ts FROM ohlcv_daily
           WHERE asset=? AND ts>=? ORDER BY ts ASC LIMIT 1 OFFSET ?""",
        (asset, start_ts - 14 * 86400, n),
    ).fetchone()
    if not row:
        return None, None
    return float(row[0]), int(row[1])


def insert_pending_outcome(conn, chain, ev, deadline_ts):
    """插入 PENDING outcome. 返回 True 表示真正新增了行.

    注意: 不能靠 IntegrityError 判断去重 —— 本表的 UNIQUE 约束会让
    INSERT OR IGNORE **静默跳过**重复行而不抛异常, 导致调用方把"已存在"
    误报成"新增", 计数虚高. 这里改用 cursor.rowcount 判定实际插入.
    """
    try:
        cur = conn.execute(
            """INSERT OR IGNORE INTO causal_chain_outcomes
                 (chain_id, trigger_ts, trigger_source, trigger_ref_id,
                  trigger_headline, trigger_keywords_matched,
                  predicted_direction, predicted_mag_lo, predicted_mag_hi,
                  eval_deadline_ts, status)
               VALUES (?, ?, 'news_events', ?, ?, ?, ?, ?, ?, ?, 'PENDING')""",
            (chain.chain_id, ev["ts"], ev["id"],
             (ev["headline"] or "")[:200], ",".join(ev["matched_kws"]),
             chain.expected_direction, chain.magnitude_lo, chain.magnitude_hi,
             deadline_ts),
        )
        conn.commit()
        return cur.rowcount > 0
    except sqlite3.IntegrityError:
        return False


def _resolve_chain_asset(conn, chain_id, priced_assets=None):
    """按 chain_id(= causal_chains.id) 解析资产.

    v302 兼容层: 新表没有 asset 列, 资产编码在 trigger_words 里, 需按
    有行情的 ticker 反查. 找不到返回 None.
    """
    row = conn.execute(
        "SELECT trigger_words FROM causal_chains WHERE id=?", (chain_id,)
    ).fetchone()
    if not row:
        return None
    if priced_assets is None:
        priced_assets = _load_priced_assets(conn)
    for p in (row[0] or "").split(","):
        pu = p.strip().upper()
        if pu in priced_assets:
            return pu
    return None


def evaluate_one(conn, row, force=False):
    """row 是 outcomes 表的一行. 列序见 SELECT.

    v302+ 新增 force: 跳过 deadline 检查 (用于 force-evaluate).
    """
    outcome_id, chain_id, trigger_ts = row[0], row[1], row[2]
    pred_dir = row[10]
    mag_lo = float(row[11] or 0.0)
    mag_hi = float(row[12] or 999.0)
    deadline_ts = row[13] if len(row) > 13 else None
    now = int(time.time())
    if not force and deadline_ts and now < deadline_ts:
        return {"status": "PENDING", "reason": "not_due"}
    asset = _resolve_chain_asset(conn, chain_id)
    if not asset:
        return {"status": "EXPIRED", "reason": "chain_removed_or_no_asset"}
    close_t0 = get_close_at_or_before(conn, asset, trigger_ts)
    if close_t0 is None:
        return {"status": "EXPIRED", "reason": "no_ohlcv_at_trigger"}
    close_t5, ts_t5 = get_close_t_plus_n(conn, asset, trigger_ts, 4)
    if close_t5 is None:
        return {"status": "EXPIRED", "reason": "no_ohlcv_at_5d"}
    pct_5d = (close_t5 - close_t0) / close_t0 * 100.0
    abs_pct = abs(pct_5d)
    if pred_dir == "UP":
        direction_hit = 1 if pct_5d > 0 else 0
    elif pred_dir == "DOWN":
        direction_hit = 1 if pct_5d < 0 else 0
    else:
        direction_hit = 1 if abs_pct >= mag_lo else 0
    magnitude_hit = 1 if (mag_lo <= abs_pct <= mag_hi) else 0
    # v302+ P2 修复: HIT 判定改为"方向对"为 HIT, 幅度匹配记录到 magnitude_hit 字段,
    # 解决 5d 实际波动 < mag_lo 导致 NEUTRAL 误判 (此前 BTC mag_lo=2% 太严).
    # - HIT:    direction_hit=1 AND magnitude_hit=1  (方向对 + 幅度合理 = 完美)
    # - HIT_WEAK: direction_hit=1 AND magnitude_hit=0 (方向对但幅度偏 = 弱命中)
    # - MISS:   direction_hit=0 (方向反 = 完全错)
    if direction_hit == 1 and magnitude_hit == 1:
        status = "HIT"
    elif direction_hit == 1:
        status = "HIT_WEAK"
    else:
        status = "MISS"
    conn.execute(
        """UPDATE causal_chain_outcomes
           SET actual_close_at_trigger=?, actual_close_at_5d=?, actual_pct_5d=?,
               status=?, direction_hit=?, magnitude_hit=?, notes=?, evaluated_at=?
           WHERE id=?""",
        (close_t0, close_t5, pct_5d, status, direction_hit, magnitude_hit,
         json.dumps({"ts_t5": ts_t5, "mag_window": [mag_lo, mag_hi], "pred_dir": pred_dir},
                    ensure_ascii=False), now, outcome_id),
    )
    # v302 兼容: causal_chains 新表没有 hit_count/miss_count/last_verified_ts 列,
    # 命中统计改由 causal_chain_outcomes 聚合得出 (见 _chain_stats), 此处不再反写.
    conn.commit()
    return {"status": status, "pct_5d": pct_5d,
            "close_t0": close_t0, "close_t5": close_t5,
            "direction_hit": direction_hit, "magnitude_hit": magnitude_hit}


def backfill(conn, days=TIME_HORIZON_LOOKBACK):
    chains = load_active_chains(conn)
    inserted = 0
    per_chain = {}
    for ch in chains:
        n = 0
        for ev in find_news_triggers(conn, ch, days):
            deadline_ts = ev["ts"] + 7 * 86400
            if insert_pending_outcome(conn, ch, ev, deadline_ts):
                inserted += 1
                n += 1
        per_chain[ch.chain_id] = n
    return {"chains_scanned": len(chains),
            "outcomes_inserted": inserted,
            "lookback_days": days,
            "per_chain": per_chain}


def evaluate_pending(conn, force=False, max_age_days=None):
    """Evaluate PENDING outcomes whose eval_deadline has passed.

    v302+ 新增:
      force=True        → 忽略 deadline,评估所有 PENDING (用于测试 / 短期窗口信号)
      max_age_days=int  → 仅评估 trigger 在 N 天以内的 PENDING (防止误评远古噪音)
    """
    now = int(time.time())
    if force:
        if max_age_days is not None:
            min_ts = now - int(max_age_days * 86400)
            rows = conn.execute(
                """SELECT id, chain_id, trigger_ts, trigger_source, trigger_ref_id,
                          trigger_headline, trigger_keywords_matched, predicted_direction,
                          NULL, NULL, predicted_direction, predicted_mag_lo, predicted_mag_hi,
                          eval_deadline_ts
                   FROM causal_chain_outcomes
                   WHERE status='PENDING' AND trigger_ts>=?
                   ORDER BY trigger_ts ASC""",
                (min_ts,),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT id, chain_id, trigger_ts, trigger_source, trigger_ref_id,
                          trigger_headline, trigger_keywords_matched, predicted_direction,
                          NULL, NULL, predicted_direction, predicted_mag_lo, predicted_mag_hi,
                          eval_deadline_ts
                   FROM causal_chain_outcomes
                   WHERE status='PENDING'
                   ORDER BY trigger_ts ASC""",
            ).fetchall()
    else:
        rows = conn.execute(
            """SELECT id, chain_id, trigger_ts, trigger_source, trigger_ref_id,
                      trigger_headline, trigger_keywords_matched, predicted_direction,
                      NULL, NULL, predicted_direction, predicted_mag_lo, predicted_mag_hi,
                      eval_deadline_ts
               FROM causal_chain_outcomes
               WHERE status='PENDING' AND eval_deadline_ts<=?
               ORDER BY trigger_ts ASC""",
            (now,),
        ).fetchall()
    out = {"evaluated": 0, "hit": 0, "hit_weak": 0, "miss": 0, "neutral": 0, "expired": 0, "samples": [], "force_mode": force, "max_age_days": max_age_days}
    for r in rows:
        result = evaluate_one(conn, r, force=force)
        out["evaluated"] += 1
        st = result["status"].lower()
        if st in out:
            out[st] += 1
        if len(out["samples"]) < 5:
            out["samples"].append({
                "chain_id": r[1], "headline": (r[5] or "")[:60], "result": result,
            })
    return out


def get_feedback_summary(conn, days=TIME_HORIZON_LOOKBACK):
    start_ts = int(time.time()) - days * 86400
    chains = load_active_chains(conn)
    out_chains = []
    for ch in chains:
        outcomes = conn.execute(
            """SELECT trigger_ts, trigger_headline, status, actual_pct_5d,
                      direction_hit, magnitude_hit
               FROM causal_chain_outcomes
               WHERE chain_id=? AND trigger_ts>=?
               ORDER BY trigger_ts DESC""",
            (ch.chain_id, start_ts),
        ).fetchall()
        n = len(outcomes)
        # v302: evaluate_one 会产出 HIT_WEAK (方向对但幅度偏), 计入有效评估,
        # 否则这类样本会被当成"未评估"而虚高 MISS 比例.
        n_hit = sum(1 for o in outcomes if o[2] in ("HIT", "HIT_WEAK"))
        n_miss = sum(1 for o in outcomes if o[2] == "MISS")
        n_neu = sum(1 for o in outcomes if o[2] == "NEUTRAL")
        n_pend = sum(1 for o in outcomes if o[2] == "PENDING")
        n_eval = n_hit + n_miss + n_neu
        real_hit_rate = (n_hit / n_eval) if n_eval > 0 else None
        calib_gap = ((ch.confidence - real_hit_rate) if real_hit_rate is not None else None)
        out_chains.append({
            "chain_id": ch.chain_id, "asset": ch.asset,
            "expected_direction": ch.expected_direction,
            "magnitude_lo": ch.magnitude_lo, "magnitude_hi": ch.magnitude_hi,
            "stated_confidence": ch.confidence,
            "lifetime_hit": n_hit, "lifetime_miss": n_miss,
            "window_days": days,
            "outcomes_n": n, "outcomes_hit": n_hit, "outcomes_miss": n_miss,
            "outcomes_neutral": n_neu, "outcomes_pending": n_pend,
            "real_hit_rate": real_hit_rate,
            "calibration_gap": calib_gap,
            "needs_recalibration": (
                real_hit_rate is not None and abs(calib_gap) > 0.15 and n_eval >= 5
            ),
            "recent_outcomes": [
                {"ts": o[0],
                 "headline": (o[1] or "")[:80],
                 "status": o[2],
                 "pct_5d": o[3],
                 "direction_hit": (bool(o[4]) if o[4] is not None else None),
                 "magnitude_hit": (bool(o[5]) if o[5] is not None else None)}
                for o in outcomes[:8]
            ],
        })
    total_o = sum(c["outcomes_n"] for c in out_chains)
    total_hit = sum(c["outcomes_hit"] for c in out_chains)
    total_miss = sum(c["outcomes_miss"] for c in out_chains)
    total_neu = sum(c["outcomes_neutral"] for c in out_chains)
    total_pend = sum(c["outcomes_pending"] for c in out_chains)
    total_eval = total_hit + total_miss + total_neu
    return {
        "window_days": days,
        "chains": out_chains,
        "summary": {
            "n_chains": len(out_chains),
            "n_chains_with_outcomes": sum(1 for c in out_chains if c["outcomes_n"] > 0),
            "n_chains_needs_recalibration": sum(1 for c in out_chains if c["needs_recalibration"]),
            "total_outcomes": total_o,
            "total_hit": total_hit,
            "total_miss": total_miss,
            "total_neutral": total_neu,
            "total_pending": total_pend,
            "overall_hit_rate": (total_hit / total_eval) if total_eval > 0 else None,
        },
    }


def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("cmd", choices=["backfill", "evaluate", "summary", "all"])
    p.add_argument("--days", type=int, default=TIME_HORIZON_LOOKBACK)
    args = p.parse_args()
    conn = sqlite3.connect(DB_PATH)
    if args.cmd == "backfill":
        print(json.dumps(backfill(conn, args.days), indent=2, ensure_ascii=False))
    elif args.cmd == "evaluate":
        print(json.dumps(evaluate_pending(conn), indent=2, ensure_ascii=False))
    elif args.cmd == "summary":
        print(json.dumps(get_feedback_summary(conn, args.days), indent=2, ensure_ascii=False))
    else:
        print(">>> backfill")
        print(json.dumps(backfill(conn, args.days), indent=2, ensure_ascii=False))
        print(">>> evaluate")
        print(json.dumps(evaluate_pending(conn), indent=2, ensure_ascii=False))
        print(">>> summary (first 1500 chars)")
        s = json.dumps(get_feedback_summary(conn, args.days), indent=2, ensure_ascii=False)
        print(s[:1500] + ("..." if len(s) > 1500 else ""))
    conn.close()


if __name__ == "__main__":
    main()




