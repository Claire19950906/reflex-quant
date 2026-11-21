"""chain_prior.py — 把 causal_chains 接进实时决策 (需求 #8)

背景
-----
724 条因果链此前只在两条旁路上存在:
  · `cron_causal_outcome_eval` 事后评估 5d 结果
  · `causal_weight_feedback` 每小时回写 posterior
而**决策链路完全不读它** —— `decision_engine` 与 `ai_worker` 里没有任何
`causal_chains` 引用。结果就是"出现新闻先按因果链决策"这件事从未发生,
链学得再准也影响不到下单。

本模块提供唯一入口, 供两条决策链调用:
  · `match_by_headline(conn, asset, headline)` —— 新闻路径 (ai_worker)
  · `match_by_time(conn, asset, ts, window)`     —— 异动路径 (decision_engine)

返回值是"先验证据", 不做二值判断; 由调用方决定 annotate / veto。
"""

from __future__ import annotations

import math
import re
import sqlite3
from typing import Any, Dict, Iterable, List, Optional, Tuple

# 链的资产字段若为这些值, 表示"跨资产/未指定"—— 不能拿来解释某个具体品种。
_NON_ASSET_VALUES = {"", "UNKNOWN", "NONE", "ALL", "ANY"}

# 关键词分隔符: 历史数据里 , / 、 / 空格 / | 混用
_SPLIT_RE = re.compile(r"[,，、;；|/\s]+")

# 单条链最多贡献的关键词命中数 (避免长词表把分数刷高)
_MAX_KEYWORD_HITS = 3

# 通用/结构词 —— 出现在 trigger_words 里但没有区分度。
# 实测启用链的触发词混进了 "BUY/SELL/verdict/prompt/Asset/market_event" 以及
# 品种代码本身 (ETH/XAU/WTI); 拿这些做匹配 = 任意一条 ETH 新闻都能命中任何
# 一条 ETH 链, 先验被稀释成噪声。品种已由 asset 列强制过滤, 无需重复。
_GENERIC_TOKENS = {
    "buy", "sell", "hold", "verdict", "prompt", "asset", "market_event", "event",
    "news", "message", "true", "false", "null",
    "btc", "eth", "sol", "xau", "wti", "nvda", "mu", "ada", "doge", "xrp", "bnb",
    "skhynix", "tsla", "crude", "gold", "silver", "bitcoin", "ethereum", "solana",
    "加密资产", "美股", "个股", "原油", "黄金", "消息", "市场", "行情", "事件",
}
# v400 P0-1: 与入库侧共用同一份系统 token 黑名单 —— 旧库里的污染词在这里
# 也要被过滤（回填之前的历史行同样会进 match_by_headline，不能只靠源头修）。
try:  # 纯增强：导入失败不影响本模块的 stdlib 独立性
    from chain_keywords import SYSTEM_TOKENS as _KW_SYSTEM_TOKENS
    _GENERIC_TOKENS |= {t.lower() for t in _KW_SYSTEM_TOKENS}
except Exception:
    pass


def _keyword_set(chain: Dict[str, Any]) -> set[str]:
    """抽关键词。

    v423: 清洗过的 trigger_keywords 优先，且不再与 trigger_words 取并集。
    原因：trigger_keywords 是按链主题做过净化/门控的派生列（见
    scripts/clean_chain_keywords.py），而 trigger_words 是知识列、保留着
    "BUY/SELL/美联储/品种名"等原始污染词；两者并集会把门控结果重新污染，
    实测任何带"美联储"的新闻都能点燃地缘链。仅在 trigger_keywords 为空时
    才回退到 trigger_words（老链兼容）。
    """
    src = str(chain.get("trigger_keywords") or "").strip()
    fields = ("trigger_keywords",) if src else ("trigger_words",)
    out: set[str] = set()
    for field in fields:
        raw = chain.get(field) or ""
        for part in _SPLIT_RE.split(str(raw)):
            p = part.strip()
            if not p:
                continue
            # 单字关键词噪声太大 (如 "涨"/"跌"), 要求 ≥2 字
            if len(p) < 2:
                continue
            if p.lower() in _GENERIC_TOKENS:
                continue
            out.add(p.lower())
    return out


def _evidence_score(chain: Dict[str, Any]) -> Tuple[float, int]:
    """链的历史证据强度 → (命中率, 样本量)。

    hit_count / miss_count 由 `migrate_causal_chain_outcomes` 与
    `causal_weight_feedback` 维护; 缺失时按 0 样本处理 (不否决)。
    """
    try:
        hit = int(chain.get("hit_count") or 0)
        miss = int(chain.get("miss_count") or 0)
    except (TypeError, ValueError):
        return 0.0, 0
    n = hit + miss
    if n <= 0:
        return 0.0, 0
    return hit / n, n


def _wilson_lower(hits: int, n: int, z: float = 1.96) -> float:
    """命中率的 Wilson 95% 下界 —— 用它而不是点估计当门槛。

    2 次全中的点估计是 100%，但下界只有 34%：拿 2 个样本去否决一笔真实交易，
    等于拿噪声当纪律。v406 把门槛从"n>=2 且 hit>=0.6"改成"下界>0.5"。
    """
    if n <= 0:
        return 0.0
    p = hits / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    s = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5)
    return (c - s) / d


def _wilson_upper(hits: int, n: int, z: float = 1.96) -> float:
    """Wilson 95% 上界 —— 与下界一起给出"这个命中率能信到哪一步"的区间。

    只报点估计的界面（58%）会让人以为精度是 ±1%，实际 11/19 的区间是 36%~77%。
    """
    if n <= 0:
        return 0.0
    p = hits / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    s = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5)
    return min(1.0, (c + s) / d)


# ── v417 基准率：把"行情自己在漂"从"链会预测"里扣掉 ──────────────────────
# 问题（2026-11-18 实测）：全库 UP 链方向命中 50.5%、DOWN 链 62.7%，看着"看跌有效"。
# 但同期 XAU 的 5 日窗口本身就是 71% 下跌（p_up=0.29），DOWN 链的 62.7% **低于**
# 基准 —— 它不是在预测，是在骑漂移；反过来 XAU 的 UP 链 58% 却是实打实的超额。
# 结论：绝对命中率不可用作闸门，必须跟"闭眼猜这个方向"的基准率比。
BASE_RATE_DAYS = 60       # 基准率回看窗口（自然日）
BASE_RATE_HORIZON = 5     # 与链评估同口径：5 个交易日
_BASE_RATE_CACHE: Dict[Tuple[str, int, int], Optional[float]] = {}


def _base_rate_up(conn: sqlite3.Connection, asset: str,
                  days: int = BASE_RATE_DAYS,
                  horizon: int = BASE_RATE_HORIZON) -> Optional[float]:
    """该品种近 days 天里，任意一天往后 horizon 个交易日收涨的比例。

    与 `causal_graph._direction_base_rate` 同一口径（同一份 ohlcv_daily、
    同样的去重方式），但**本模块自带实现**：chain_prior 被决策链路高频调用，
    不能再挂一个 176KB 的 causal_graph 进来（会拖慢每次下单前的先验计算）。
    取不到行情时返回 None，调用方退回旧 Wilson 口径，不阻断决策。
    """
    key = (str(asset or "").upper(), int(days), int(horizon))
    if key in _BASE_RATE_CACHE:
        return _BASE_RATE_CACHE[key]
    p_up: Optional[float] = None
    try:
        rows = conn.execute(
            "SELECT ts, close FROM ohlcv_daily WHERE asset=? ORDER BY ts DESC LIMIT ?",
            (key[0], max(days * 2, 30)),
        ).fetchall()
    except sqlite3.Error:
        rows = []
    if rows and len(rows) > horizon:
        series = sorted(((int(r[0]), float(r[1])) for r in rows if r[1] is not None))
        lo_ts = series[-1][0] - days * 86400
        n = ups = 0
        for i in range(len(series) - horizon):
            if series[i][0] < lo_ts:
                continue
            n += 1
            ups += 1 if series[i + horizon][1] > series[i][1] else 0
        if n:
            p_up = round(ups / n, 4)
    _BASE_RATE_CACHE[key] = p_up
    return p_up


def _binom_p_greater(hits: int, n: int, p0: float) -> float:
    """P(X >= hits | n, p0) 的单侧 p 值（正态近似 + 连续性修正）。

    不引 scipy 的代价是近似，但 n>=12 时误差在 1e-3 量级，用来做 0.05 的闸门足够。
    p0 缺失/越界时返回 1.0（= 不显著，宁可不开闸）。
    """
    if n <= 0 or not (0.0 < float(p0) < 1.0):
        return 1.0
    p0 = float(p0)
    z = (hits - 0.5 - n * p0) / math.sqrt(n * p0 * (1 - p0))
    return max(0.0, min(1.0, 1.0 - 0.5 * (1.0 + math.erf(z / math.sqrt(2)))))


# v417 证据闸门（v406 的绝对口径 → 超额口径）：
#   · n >= 12（保留，样本不足一切免谈）；
#   · 超额 lift = 命中率 − 基准率 ≥ 0.10（至少比"闭眼猜"好 10 个百分点）；
#   · 单侧二项检验 p <= 0.05（这个超额不是运气）。
# 为什么必须换：绝对 Wilson 下界>0.5 在当前样本量下**数学上不可达** ——
# 14/20=70% 的下界只有 0.481，要过 0.5 得 n≈35~40，按现有产出永远等不到，
# strong 档恒空。而"超额 25pp + p=0.018"的链是真实有效的证据。
MIN_LIFT_FOR_STRONG = 0.10
MAX_P_VALUE_FOR_STRONG = 0.05


def _evidence_from_outcomes(conn: sqlite3.Connection, chain_ids: List[str]) -> Dict[str, Tuple[int, int]]:
    """从 causal_chain_outcomes 直接算每条链的 (方向命中, 方向样本数)。

    为什么要有这个：`causal_chains.hit_count/miss_count` 由
    `causal_weight_feedback` 维护，但实测**全库 752 条链这两列都是 0**
    （weight 也全是 1.0）。也就是说决策侧读到的"证据强度"永远是 (0, 0) →
    `aggregate_direction` 恒返回 UNKNOWN、`conflict_veto` 永不触发 ——
    闭环写了 3,349 条 outcome，决策侧一条都没看见。

    只统计**有方向主张**的链（UP/DOWN）：BOTH 链的 direction_hit 是"幅度过没过线"，
    不是方向对错，混进来会把命中率抬到 100%（实测 428/428）。
    """
    if not chain_ids:
        return {}
    out: Dict[str, Tuple[int, int]] = {}
    marks = ",".join("?" * len(chain_ids))
    try:
        rows = conn.execute(
            "SELECT o.chain_id,"
            " SUM(CASE WHEN o.status IN ('HIT','HIT_WEAK') THEN 1 ELSE 0 END) h,"
            " COUNT(*) n"
            " FROM causal_chain_outcomes o JOIN causal_chains c ON c.id=o.chain_id"
            " WHERE o.status IN ('HIT','HIT_WEAK','MISS')"
            "   AND UPPER(COALESCE(c.expected_direction,'')) IN ('UP','DOWN')"
            "   AND o.chain_id IN (%s) GROUP BY o.chain_id" % marks,
            [str(x) for x in chain_ids],
        ).fetchall()
    except sqlite3.Error:
        return {}
    for r in rows:
        out[str(r[0])] = (int(r[1] or 0), int(r[2] or 0))
    return out


def _enrich_evidence(conn: sqlite3.Connection, priors: List[Dict[str, Any]]) -> None:
    """把 priors 的 hit_rate / n_outcomes / wilson_lower 用 outcomes 表覆盖。"""
    ev = _evidence_from_outcomes(conn, [str(p.get("chain_id")) for p in priors])
    for p in priors:
        h, n = ev.get(str(p.get("chain_id")), (0, 0))
        p["n_outcomes"] = n
        p["hit_rate"] = round(h / n, 4) if n else 0.0
        p["wilson_lower"] = round(_wilson_lower(h, n), 4)
        p["evidence_source"] = "causal_chain_outcomes"
        # v417: 命中率必须带区间 —— 只报 58% 会让人以为精度 ±1%，
        # 实际 11/19 的 95% 区间是 36%~77%（下面是上界，下界即 wilson_lower）。
        p["ci_low"] = p["wilson_lower"]
        p["ci_high"] = round(_wilson_upper(h, n), 4)
        # v417: 基准率与超额 —— 闸门改用它（见 MIN_LIFT_FOR_STRONG）
        p["base_rate"] = None
        p["lift"] = None
        p["p_value"] = None
        direction = str(p.get("direction") or "").upper()
        if n and direction in ("UP", "DOWN"):
            p_up = _base_rate_up(conn, p.get("asset") or "")
            if p_up is not None:
                p0 = p_up if direction == "UP" else 1.0 - p_up
                p["base_rate"] = round(p0, 4)
                p["lift"] = round(h / n - p0, 4)
                p["p_value"] = round(_binom_p_greater(h, n, p0), 4)
        # v414: BOTH(幅度) 链的方向证据恒为 0（口径上就该如此），但它们的**幅度证据**
        # 一直被丢掉 —— 实测 22 条 BOTH 链 n>=8 且下界>0.5，是全库最有统计支撑的一批。
        # 这里单独统计，写进 mag_n/mag_wilson，**不并入方向口径**。
        if str(p.get("direction") or "").upper() == "BOTH":
            try:
                row = conn.execute(
                    "SELECT SUM(CASE WHEN status IN ('HIT','HIT_WEAK') THEN 1 ELSE 0 END) h,"
                    " COUNT(*) n FROM causal_chain_outcomes"
                    " WHERE chain_id=? AND status IN ('HIT','HIT_WEAK','MISS')",
                    (str(p.get("chain_id")),),
                ).fetchone()
            except Exception:
                row = None
            if row and (row[1] or 0) > 0:
                p["mag_n"] = int(row[1])
                p["mag_hit_rate"] = round((row[0] or 0) / int(row[1]), 4)
                p["mag_wilson"] = round(_wilson_lower(int(row[0] or 0), int(row[1])), 4)
        # v407: 档位随证据一起算, 让调用方一眼看出"能不能否决"
        p["tier"] = tier_of(p)


def _direction_of(chain: Dict[str, Any]) -> str:
    d = str(chain.get("expected_direction") or "BOTH").upper()
    return d if d in ("UP", "DOWN") else "BOTH"


def _shape(row: sqlite3.Row | Dict[str, Any], hits: List[str],
           match_type: str) -> Dict[str, Any]:
    chain = dict(row)
    rate, n = _evidence_score(chain)
    try:
        weight = float(chain.get("weight") or 0.0)
    except (TypeError, ValueError):
        weight = 0.0
    # v407: 展示用的"后验"取 confidence 而不是 weight —— weight 由每小时反馈写入,
    # 但该反馈要求 n≥12(全库 0 条达标), 于是 weight 恒 1.0, prompt 里会出现
    # "后验=1.00 n=4"这种自相矛盾的假高。confidence 是 chain_steps_verify 按
    # Beta(2,2) 后验均值算的, 有真实梯度。
    try:
        posterior = float(chain.get("confidence") if chain.get("confidence") is not None
                          else weight)
    except (TypeError, ValueError):
        posterior = weight
    return {
        "chain_id": chain.get("chain_id") or chain.get("id"),
        "name": (chain.get("name") or "")[:80],
        "asset": chain.get("asset"),
        "direction": _direction_of(chain),
        "magnitude_lo": chain.get("magnitude_lo"),
        "magnitude_hi": chain.get("magnitude_hi"),
        "posterior": round(posterior, 4),
        "hit_rate": round(rate, 4),
        "n_outcomes": n,
        "matched_keywords": hits[:5],
        "match_type": match_type,
        "mechanism": (chain.get("mechanism") or "")[:200],
    }


def _rank(priors: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """排序: 证据强度优先, 其次 posterior, 再次关键词命中数。

    设计意图: 一条"命中率 80% / n=12"的链应该排在"posterior 1.0 / n=0"的链前面,
    否则新链会靠先验权重压过被验证过的老链。
    """
    def _key(p):
        # v414: BOTH 链的方向证据恒 0，但幅度证据可能很硬（实测 n>=8 有 22 条）。
        # 排序若只看方向 n，这批最有支撑的链会被 limit 直接截掉 —— 等于白算。
        if str(p.get("direction") or "").upper() == "BOTH":
            n = int(p.get("mag_n") or 0)
            rate = float(p.get("mag_hit_rate") or 0.0)
        else:
            n = int(p.get("n_outcomes") or 0)
            rate = float(p.get("hit_rate") or 0.0)
        return (n >= 2, rate if n >= 2 else 0.0, p["posterior"], len(p["matched_keywords"]))

    return sorted(priors, key=_key, reverse=True)


def _hit(kw: str, lower_headline: str) -> bool:
    """关键词命中判定。

    纯英文关键词必须做**词边界**匹配 —— 否则 "rate" 会命中 "corporate"、
    "oil" 会命中 "boil", 实测造成 14.8%→ 大量假命中。中文按子串匹配。
    """
    if kw.isascii():
        return re.search(r"(?<![a-z0-9])" + re.escape(kw) + r"(?![a-z0-9])",
                         lower_headline) is not None
    return kw in lower_headline


def match_by_headline(conn: sqlite3.Connection, asset: str,
                      headline: str, limit: int = 3) -> List[Dict[str, Any]]:
    """按 (品种, 新闻标题) 匹配因果链。

    规则:
      1. 只取 enabled=1;
      2. 链的 asset 必须等于本品种 (跨资产链 asset 为 UNKNOWN/ALL 时不参与);
      3. 标题须命中 ≥1 个 trigger 关键词;
      4. 按证据强度排序, 取前 limit 条。
    """
    if not asset or not headline:
        return []
    cur = conn.execute(
        "SELECT id, chain_id, name, asset, trigger_words, trigger_keywords,"
        " expected_direction, weight, confidence, hit_count, miss_count,"
        " magnitude_lo, magnitude_hi, mechanism"
        " FROM causal_chains WHERE enabled=1 AND asset=?"
        " AND COALESCE(active,1)=1"
        " ORDER BY weight DESC",
        (str(asset),),
    )
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    h = str(headline).lower()
    priors: List[Dict[str, Any]] = []
    for row in rows:
        kws = _keyword_set(dict(row))
        hits = sorted(k for k in kws if _hit(k, h))
        if not hits:
            continue
        priors.append(_shape(row, hits[: _MAX_KEYWORD_HITS], "headline"))
    _enrich_evidence(conn, priors)
    ranked = _rank(priors)
    # v414: 名额分开留 —— 幅度链证据硬、排序会把它顶到前面，全占满后方向候选就看不见了。
    # 方向证据才是否决的依据，不能被幅度参考挤掉。
    lim = max(1, int(limit))
    mag = [p for p in ranked if str(p.get("direction") or "").upper() == "BOTH"]
    dirn = [p for p in ranked if str(p.get("direction") or "").upper() != "BOTH"]
    if not mag or not dirn:
        return ranked[:lim]
    k = lim // 2
    return dirn[:lim - k] + mag[:k]


def match_by_time(conn: sqlite3.Connection, asset: str, ts: int,
                  window_sec: int = 3600, limit: int = 3,
                  news_table: str = "news_events",
                  allow_future: bool = False) -> List[Dict[str, Any]]:
    """按 (品种, 时间) 找最近的同品种新闻, 再用它匹配因果链。

    用于 `decision_engine` 的异动路径 —— 那条路径只有 (asset, ts), 没有标题。
    找不到窗口内新闻时返回空列表 (不猜)。

    v337: 默认**不吃未来新闻** (`allow_future=False`)。旧实现用 `ts±window`
    取最近一条, 会把"异动之后"才发布的新闻当成事前先验 —— 那是前视偏差,
    回测里会凭空变准, 实盘里根本不成立。要做事后解释才传 True。
    """
    if not asset or not ts:
        return []
    ts_i = int(ts)
    hi = ts_i + (int(window_sec) if allow_future else 0)
    try:
        row = conn.execute(
            "SELECT headline FROM %s"
            " WHERE asset=? AND ts>=? AND ts<=?"
            " AND headline IS NOT NULL AND headline<>''"
            " ORDER BY ABS(ts-?) ASC LIMIT 1" % news_table,
            (str(asset), ts_i - int(window_sec), hi, ts_i),
        ).fetchone()
    except sqlite3.Error:
        return []
    if not row:
        return []
    headline = row[0] if not isinstance(row, sqlite3.Row) else row["headline"]
    priors = match_by_headline(conn, asset, headline, limit=limit)
    for p in priors:
        p["matched_headline"] = str(headline)[:120]
        p["match_type"] = "time_window"
    return priors


# v406 证据闸门：一条链要拿来说方向，必须先"跑赢硬币"。
#   · n >= 12：中位样本只有 6～11 条，低于 12 的命中率无法区分 50% 与 60%；
#   · Wilson 下界 > 0.5：点估计被小样本放大过（2/2 的点估计是 100%，下界只有 34%）；
#   · 实测现状：0 条链达标（命中率最好的链也只有 n=11）。
# 这不是把功能关掉，是把"什么时候能开"写成了可验证条件 —— 样本够了会自动开。
MIN_CHAIN_SAMPLES_FOR_DIRECTION = 12
MIN_WILSON_LOWER_FOR_DIRECTION = 0.5

# v414 实测（2026-11-17）：方向链里 Wilson 下界>0.5 的**最好也只有 4/4（下界 0.51）**，
# 门槛一放到 8，达标方向链 = 0 条。所以「能否决」的门槛不能靠降数字来"开闸"——
# 那是拿 4 个样本去否决真实交易。方向判定保持 12，等样本自己长上来。
# 幅度/胜率链（BOTH）是另一回事：它们讲的是"这时候追不追"，不是"往哪走"，
# 8 条样本 + 下界>0.5（实测 22 条可达标）就够用，且**永不允许转成方向**。
import os as _os
MIN_CHAIN_SAMPLES_FOR_STRONG = MIN_CHAIN_SAMPLES_FOR_DIRECTION
MIN_CHAIN_SAMPLES_FOR_MAGNITUDE = int(_os.getenv("CHAIN_MAG_MIN_N", "8"))

# v407 三级先验：实测全库没有一条链达到 12 样本 → 决策侧此前"一条都没看见"
# (chain_prior 恒空, 否决恒不触发)。拆三档, 让证据不足的链也能被**标注**:
#   strong(n≥12 且 Wilson 下界>0.5) → 给方向 + 可否决
#   weak(3≤n<12)                    → 只出 UP_WEAK/DOWN_WEAK 标注, 否决不采信
#   none(n<3)                       → 不表态
MIN_CHAIN_SAMPLES_FOR_WEAK = 3


def _eligible(p: Dict[str, Any]) -> bool:
    """一条链能不能被当成"已验证方向证据"（= strong 档 / 可否决）。

    v417 两种口径并存（按证据里有没有基准率自动选）：
      · 有 base_rate + p_value → **超额口径**：lift ≥ 0.10 且 p ≤ 0.05。
        这是现在的默认路径（`_enrich_evidence` 会算基准率）。
      · 没有基准率（老调用方 / 该品种没有行情）→ 退回 v406 的 Wilson 下界>0.5，
        宁可不达标，也不在没有参照物时开闸。
    """
    n = int(p.get("n_outcomes") or 0)
    if n < MIN_CHAIN_SAMPLES_FOR_STRONG:
        return False
    lift = p.get("lift")
    p_value = p.get("p_value")
    if lift is not None and p_value is not None:
        return (float(lift) >= MIN_LIFT_FOR_STRONG
                and float(p_value) <= MAX_P_VALUE_FOR_STRONG)
    lower = p.get("wilson_lower")
    if lower is None:                      # 兼容老调用方（只有 hit_rate/n）
        lower = _wilson_lower(int(round(float(p.get("hit_rate") or 0) * n)), n)
    return float(lower) > MIN_WILSON_LOWER_FOR_DIRECTION


def tier_of(p: Dict[str, Any]) -> str:
    """strong / weak / none —— 供调用方决定"能不能否决"。

    v414: 分方向看待 —— BOTH(幅度) 链达标只代表"这种局面里追不追有统计支撑",
    **永远不转成方向**；UP/DOWN 仍要 12 样本 + Wilson 下界>0.5 才算 strong。
    """
    if _eligible(p):
        return "strong"
    if str(p.get("direction") or "").upper() == "BOTH":
        n = int(p.get("mag_n") or 0)
        if (n >= MIN_CHAIN_SAMPLES_FOR_MAGNITUDE
                and float(p.get("mag_wilson") or 0) > MIN_WILSON_LOWER_FOR_DIRECTION):
            return "strong"
    if int(p.get("n_outcomes") or 0) >= MIN_CHAIN_SAMPLES_FOR_WEAK:
        return "weak"
    return "none"


def aggregate_direction(priors: List[Dict[str, Any]]) -> str:
    """多条链合成方向: UP / DOWN / CONFLICT / UP_WEAK / DOWN_WEAK / UNKNOWN。

    strong 档才给正式方向; 只有 weak 证据时出 *_WEAK, 明确表示"不确定、别照做";
    两者都没有 → UNKNOWN。CONFLICT 只可能出现在 strong 之间。
    """
    if not priors:
        return "UNKNOWN"
    ups = [p for p in priors if p["direction"] == "UP" and _eligible(p)]
    dns = [p for p in priors if p["direction"] == "DOWN" and _eligible(p)]
    if ups and dns:
        return "CONFLICT"
    if ups:
        return "UP"
    if dns:
        return "DOWN"
    # v407: strong 一个都没有 → 再看 weak 档, 只出 *_WEAK 标注
    wups = [p for p in priors if p["direction"] == "UP"
            and int(p.get("n_outcomes") or 0) >= MIN_CHAIN_SAMPLES_FOR_WEAK]
    wdns = [p for p in priors if p["direction"] == "DOWN"
            and int(p.get("n_outcomes") or 0) >= MIN_CHAIN_SAMPLES_FOR_WEAK]
    if wups and wdns:
        return "WEAK_MIXED"
    if wups:
        return "UP_WEAK"
    if wdns:
        return "DOWN_WEAK"
    return "UNKNOWN"


def conflict_veto(priors: List[Dict[str, Any]], action: str,
                  min_hit_rate: Optional[float] = None,
                  min_posterior: float = 0.60) -> Optional[Dict[str, Any]]:
    """判断链先验是否足以**否决**这次下单。

    否决条件 (全部满足才否决, 宁可不否决):
      1. 该链处于 strong 档 —— 唯一的统计闸门（v417: 超额≥10pp 且 p≤0.05；
         拿不到基准率时退回 v406 的 Wilson 下界>0.5）;
      2. hit_rate ≥ min_hit_rate（默认不设绝对下限：闸门已经是"相对基准的超额"，
         再加一道 60% 的绝对线会把"58% 命中但基准只有 29%"的有效链挡在门外）;
      3. posterior ≥ min_posterior —— 但**已过超额闸门的链豁免**这一条：
         Beta(2,2) 后验会把 11/19 压到 0.565，低于 0.60 就永远否不了，
         等于用展示口径把统计显著的证据废掉。posterior 的本意是挡"weight 恒 1.0"
         的假高，统计闸门已经更强，不需要它再挡一次;
      4. 链方向与本次 action 相反。
    weak 档永远不否决 —— 3~11 个样本的猜测不足以挡下一笔真实交易。
    返回被否决的链, 否则 None。
    """
    if not action or action.upper() not in ("BUY", "SELL"):
        return None
    want = "UP" if action.upper() == "BUY" else "DOWN"
    for p in priors:
        if p["direction"] != ("DOWN" if want == "UP" else "UP"):
            continue
        if not _eligible(p):
            continue
        hit = float(p.get("hit_rate") or 0.0)
        if min_hit_rate is not None and hit < float(min_hit_rate):
            continue
        post = float(p.get("posterior") or 0.0)
        lift = p.get("lift")
        post_ok = post >= min_posterior or (lift is not None
                                            and float(lift) >= MIN_LIFT_FOR_STRONG)
        if not post_ok:
            continue
        return p
    return None


def build_prompt_block(priors: List[Dict[str, Any]]) -> str:
    """给 LLM prompt 用的"命中因果链"文本块 (无命中返回空串)。

    v407: 按证据档位分层 —— strong(可当先验)、weak(只作候选解释)。
    旧实现把所有命中链都写成"优先级高于临场判断", 而实测全库 0 条达标,
    等于让模型把 5 个样本的猜测当成验证过的规律。
    """
    if not priors:
        return ""
    tiers = {str(p.get("tier") or "none") for p in priors}
    lines = []
    mag = [p for p in priors if str(p.get("direction") or "").upper() == "BOTH"]
    dirn = [p for p in priors if str(p.get("direction") or "").upper() != "BOTH"]
    if mag:
        lines.append("【命中因果链·幅度/胜率参考（只说明这种局面下追不追、空间大不大，"
                     "**不可据此定向**）】")
        for i, p in enumerate(mag, 1):
            tag = {"strong": "有样本支撑", "weak": "样本不足", "none": "无样本"}.get(
                str(p.get("tier") or "none"), "无样本")
            lines.append(
                f"{i}. [{p['asset']}|幅度|{tag}] {p['name']}"
                f" | 幅度命中={float(p.get('mag_hit_rate') or 0):.0%}(n={int(p.get('mag_n') or 0)})"
                f" | 关键词={','.join(p['matched_keywords'])}")
        lines.append("")
    priors = dirn
    if not priors:
        return "\n".join(lines).rstrip()
    if "strong" in tiers:
        lines.append("【命中因果链·已验证档（样本≥12 且跑赢同品种同方向基准≥10pp, p≤0.05）"
                     "】")
    if tiers & {"weak", "none"}:
        lines.append("【命中因果链·候选档（样本不足，只能当解释假设，不得据此否决或加仓）】")
    for i, p in enumerate(priors, 1):
        tag = {"strong": "已验证", "weak": "样本不足", "none": "无样本"}.get(
            str(p.get("tier") or "none"), "无样本")
        ev_txt = (f"命中率={float(p.get('hit_rate') or 0):.0%}"
                  f"({int(p.get('n_outcomes') or 0)}次, "
                  f"区间{float(p.get('ci_low') or 0):.0%}-{float(p.get('ci_high') or 0):.0%})")
        if p.get("base_rate") is not None and p.get("lift") is not None:
            ev_txt += (f" 基准={float(p['base_rate']):.0%} "
                       # lift 是比例（0.2 = 20pp），直接 :+.0f 会打印成 +0pp
                       f"超额={float(p['lift']) * 100:+.0f}pp "
                       f"p={float(p.get('p_value') or 1):.2f}")
        lines.append(
            f"{i}. [{p['asset']}|{p['direction']}|{tag}] {p['name']}"
            f" | {ev_txt}"
            f" | 关键词={','.join(p['matched_keywords'])}"
        )
        if p.get("mechanism"):
            lines.append(f"   机理：{p['mechanism']}")
    if "strong" in tiers:
        lines.append("已验证档若与本次判断方向相反，必须在 summary 里说明反例依据。")
    else:
        lines.append("以上均未达到验证门槛：可以引用作为候选解释，但方向仍以本次证据为准。")
    return "\n".join(lines)


# ── v417 列表/导出用：给每一行补"证据"字段 ────────────────────────────────
# 问题（2026-11-18 实测）：`/api/causal/chains` 的 lite/full 字段集里**根本没有**
# confidence / hit_count / miss_count / asset / expected_direction —— 因果链管理页
# 只能显示"触发词 + 权重"，一条链准不准在界面上是隐形的。这个函数把派生证据
# 一次性算好（outcomes 批量聚合 + 基准率按品种缓存），供路由层平铺给前端。
_EVIDENCE_FIELDS = ("n_outcomes", "hit_rate", "ci_low", "ci_high",
                    "base_rate", "lift", "p_value", "tier", "mag_n",
                    "mag_hit_rate", "mag_wilson")


def annotate_chains(conn: sqlite3.Connection,
                    rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """就地给链列表行补 evidence 字段（n / 命中率 / 区间 / 基准 / 超额 / 档位）。

    不改行里原有的任何字段语义，只新增 `evidence` 字典和若干平铺键，
    这样前端列、CSV 导出、质量接口可以共用同一份口径。
    """
    if not rows:
        return rows
    priors: List[Dict[str, Any]] = []
    for r in rows:
        priors.append({
            "chain_id": r.get("id"),
            "asset": r.get("asset"),
            "direction": _direction_of(r),
        })
    try:
        _enrich_evidence(conn, priors)
    except sqlite3.Error:
        return rows
    by_id = {str(p.get("chain_id")): p for p in priors}
    for r in rows:
        p = by_id.get(str(r.get("id")))
        if not p:
            continue
        ev = {k: p.get(k) for k in _EVIDENCE_FIELDS}
        # BOTH（幅度）链达标只说明"这种局面追不追有统计支撑"，**永远不转成方向**。
        # 档位必须显式区分，否则界面上会和"可定方向"的强证据混为一谈。
        if str(r.get("expected_direction") or "").upper() == "BOTH" and ev.get("tier") == "strong":
            ev["tier"] = "strong_mag"
        r["evidence"] = ev
        for k, v in ev.items():
            if k not in r:
                r[k] = v
        r.setdefault("p_hit", ev.get("hit_rate"))
        r.setdefault("n", ev.get("n_outcomes"))
        try:
            r.setdefault("posterior", round(float(r.get("confidence") or 0.0), 4))
        except (TypeError, ValueError):
            r.setdefault("posterior", 0.0)
    return rows
