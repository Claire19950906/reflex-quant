# -*- coding: utf-8 -*-
"""v399 P2: 把「洞察」变成**可测假设**，承认测不了的单独标 observe_only。

背景（`docs/archive/细节洞察能力评估-2026-11-16.html` P2 第一条）
-------------------------------------------------------
59 条洞察里能落到参数的只有个位数，其余是"注意样本少""消息冲击时别追"这类
**无法被证伪的经验**。它们和可执行洞察混在一个池子里，于是"洞察落地率"这个
数字永远说不清：分母里一大半是根本不该被算作"待落地"的东西。

本模块只做三件事（纯函数 + 一条幂等写库，零外部依赖）：
  1. `classify(rule)`：从洞察文案里解出 (参数键, 预期方向, 观测窗口)，
     判据全部复用 `param_key_aliases`（审批链路的同一把尺子，不另立标准）；
  2. 解不出参数键 / 解出单位不等价 / 没说方向的 → `observe_only=1`，
     写明原因，**不进晋升队列**（`weekly_insight_apply` 本来也会拒，这里把
     理由提前到入库时，让占比可见）；
  3. `ensure_classified(conn)`：给历史行补分类（幂等，只处理未分类的行）。

铁律：不确定就标 observe_only，不许编一个"看起来可测"的假设。
"""
from __future__ import annotations

import re
import sqlite3
from typing import Dict, Optional

# 洞察文案里参数键的既有约定：行首方括号，如 "[sizing.stop_loss_pct] 消息冲击…"
_PARAM_KEY_RE = re.compile(r"\s*\[([A-Za-z0-9_.]+)\]")

# 预期方向词表（先判方向，再判"是否可执行"）
_UP_WORDS = ("提高", "上调", "增加", "加大", "放宽", "提升", "加仓", "延长")
_DOWN_WORDS = ("降低", "下调", "收紧", "减少", "降权", "缩短", "暂停", "避免", "削弱")

# v409: 百分比数值（"止损放到 1.5%"）—— sizing.* 换算要用
_PCT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")

# v409.1: 百分比只有紧跟"幅度词"时才是止损/止盈幅度；「账户0.3%」「仓位0.5%」
# 这类是风险占比，不能除以 ATR%（会把 sl_mult 算成个荒谬的小数）。
_MAGNITUDE_PRE_WORDS = ("止损", "止盈", "目标", "回撤", "亏损", "盈利", "波动")

# 观测窗口：文案里显式写了就解析，没写用默认 72h（与 2h 前向结算 × 3 个周期对齐）
_WINDOW_RE = re.compile(r"(\d+)\s*(小时|h|天|日|周)")
_WINDOW_UNIT_H = {"小时": 1, "h": 1, "天": 24, "日": 24, "周": 168}
DEFAULT_WINDOW_H = 72

# 可执行洞察的验证通过线：验证 verdict=passed 即算过
_PASS_VERDICTS = ("passed", "pass", "param_rule")


def _extract_param_key(rule: str) -> str:
    m = _PARAM_KEY_RE.match(rule or "")
    return m.group(1) if m else ""


def _extract_window_h(rule: str) -> int:
    m = _WINDOW_RE.search(rule or "")
    if not m:
        return DEFAULT_WINDOW_H
    try:
        n = int(m.group(1))
    except ValueError:
        return DEFAULT_WINDOW_H
    h = n * _WINDOW_UNIT_H.get(m.group(2), 1)
    return max(1, min(h, 24 * 30))


def _extract_direction(rule: str) -> int:
    text = rule or ""
    up = any(w in text for w in _UP_WORDS)
    down = any(w in text for w in _DOWN_WORDS)
    if up and not down:
        return 1
    if down and not up:
        return -1
    return 0                       # 都没说 / 同时说 → 不可判方向


def classify(rule: str, asset: str = "", source_samples: int = 0,
             context: Optional[Dict[str, float]] = None) -> Dict[str, object]:
    """把一条洞察判成「可测假设」或「只能观察」。

    返回 dict 字段：
      param_key / param_bucket / direction / window_h / hypothesis /
      observe_only / observe_reason
    """
    rule = str(rule or "")
    window_h = _extract_window_h(rule)
    direction = _extract_direction(rule)
    raw_key = _extract_param_key(rule)

    def _observe(reason: str) -> Dict[str, object]:
        return {
            "param_key": raw_key,
            "param_bucket": "",
            "direction": direction,
            "window_h": window_h,
            "hypothesis": "",
            "observe_only": 1,
            "observe_reason": reason,
        }

    if not raw_key:
        return _observe("没有参数键，无法映射到任何可调参数")

    # v409: sizing.* 这类"单位不等价"的键，有上下文才能落地（见 param_key_aliases.convert_value）。
    # 没有上下文就照旧 observe_only —— 宁可不动，也不拿假权益/假 ATR 算出一个错参数。
    from param_key_aliases import SIZING_REQUIRED_CONTEXT as _SRC
    if raw_key in _SRC:
        try:
            from param_key_aliases import convert_value, missing_context, UNMAPPABLE
            need = missing_context(raw_key, context)
            if need:
                return _observe(
                    f"{raw_key} 需要上下文 {', '.join(need)} 才能换算，当前缺失"
                    f"；{UNMAPPABLE.get(raw_key, '')}")
            m_val = _PCT_RE.search(rule)
            if not m_val:
                return _observe(f"{raw_key} 有上下文，但文案没给具体数值（如 1.5%），无法换算")
            # v409.1: 百分比必须是"止损/止盈幅度"，不能是"账户风险占比"。
            # 实测 1259「WTI单笔风险控制在账户0.3%—0.5%，按止损距离反推仓位」：
            # 0.3% 是**账户风险占比**，不是价格止损幅度，换算成 sl_mult 会得出个
            # 荒谬值（0.3/0.455≈0.66 倍 ATR 的止损），等于把风控写坏。
            # 判据：百分比前面 14 字内必须出现 止损/止盈/目标/回撤 这类幅度词。
            _pre = rule[max(0, m_val.start() - 14):m_val.start()]
            if not any(w in _pre for w in _MAGNITUDE_PRE_WORDS):
                return _observe(
                    f"{raw_key} 命中的百分比（{m_val.group(0)}）前面没有"
                    f"「止损/止盈幅度」这类词，疑似账户风险占比或仓位比例，"
                    f"拒绝换算（避免把风控参数写坏）")
            canon, conv, bucket, why = convert_value(raw_key, float(m_val.group(1)), context)
            if not canon or bucket not in ("settings", "strategy"):
                return _observe(why or f"{raw_key} 换算失败")
            # v409.1: 换算结果要落在人话可解释的区间里，超界就是"数值语义认错了"。
            # 实测 1244「单笔止损须预先设定，按约3.2%亏损反推并执行」→ 3.2%/0.452≈7.08 倍 ATR，
            # 而"3.2%亏损"说的是**账户回撤预算**，不是价格止损距离。7 倍 ATR 的止损等于没止损，
            # 写进 params 比不改更坏。这里给 sl_mult/tp*_mult 设 0.2~4 倍的可解释区间。
            if canon in ("sl_mult", "tp1_mult", "tp2_mult") and not (0.2 <= float(conv) <= 4.0):
                return _observe(
                    f"{raw_key} {m_val.group(1)}% 换算成 {canon}={conv:g}，"
                    f"超出可解释区间（0.2~4 倍 ATR）→ 疑似把账户亏损/仓位比例当成了价格幅度，"
                    f"拒绝换算")
            hyp = (f"{canon} = {conv:g}（由 {raw_key} {m_val.group(1)}% 换算）→ "
                   f"{window_h}h 内 {asset or '同资产'} 同模式决策的胜率/命中率不低于当前基线"
                   f"；判据 = 前后同模式样本胜率差（n≥30 才算可读）")
            return {
                "param_key": raw_key, "param_bucket": bucket, "direction": direction,
                "window_h": window_h, "hypothesis": hyp, "observe_only": 0,
                "observe_reason": ("" if int(source_samples or 0) >= 30
                                   else "可执行，但样本 n<30，验证结论不可读"),
            }
        except Exception:
            pass
    try:
        from param_key_aliases import resolve_key
        canon, bucket, why = resolve_key(raw_key)
    except Exception:
        return _observe("param_key_aliases 不可用，无法判定可落地性")
    if not canon:
        return _observe(why or "参数键无法映射到可调项")
    if bucket not in ("settings", "strategy"):
        return _observe(why or f"参数桶 {bucket} 不支持自动落地")
    if direction == 0:
        return _observe(f"{canon} 可映射，但文案没有声明预期方向（提高/降低）")

    dir_word = "提高" if direction > 0 else "降低"
    hyp = (
        f"{canon} {dir_word} → {window_h}h 内 {asset or '同资产'} 同模式决策的胜率/命中率"
        f"应不低于当前基线；判据 = 前后同模式样本胜率差（n≥30 才算可读）"
    )
    return {
        "param_key": raw_key,
        "param_bucket": bucket,
        "direction": direction,
        "window_h": window_h,
        "hypothesis": hyp,
        "observe_only": 0,
        "observe_reason": ("" if int(source_samples or 0) >= 30
                           else "可执行，但样本 n<30，验证结论不可读"),
    }


_ASSET_WORD_MAP = {
    "XAU": "XAU", "GOLD": "XAU", "黄金": "XAU", "金价": "XAU",
    "BTC": "BTC", "比特币": "BTC",
    "ETH": "ETH", "以太": "ETH", "以太坊": "ETH",
    "SOL": "SOL",
    "WTI": "WTI", "原油": "WTI", "油价": "WTI", "布伦特": "WTI",
}


def asset_from_text(text: str) -> str:
    """v409.1: 从洞察正文里认出品种（只在 asset 字段为空时兜底用）。

    为什么需要：`save_insights` 只解析 `[WTI]` 这种**前缀**才会写 asset；
    实测 1267 这条「WTI 波动大，止损收窄至 0.6%~0.8%」没有前缀，asset 写成了空串，
    于是 market_context 查不到 atr_pct，本来能换算的 sizing.* 退化成 observe_only。
    这里只认明确出现的品种词，认不出就返回空串（继续 observe_only，不猜）。
    """
    s = str(text or "").upper()
    if not s:
        return ""
    for word, sym in _ASSET_WORD_MAP.items():
        if word.isascii():
            # 拉丁词按整词匹配，避免 "SOL" 命中 "SOLUTION" 这类误报
            if re.search(r"(?<![A-Z0-9])" + re.escape(word) + r"(?![A-Z0-9])", s):
                return sym
        elif word in str(text or ""):
            return sym
    return ""


def market_context(conn: sqlite3.Connection, asset: str,
                   rule: str = "") -> Dict[str, float]:
    """v409: 给 sizing.* 换算用的最小上下文（只取库里真有的，缺就缺着）。

    · atr_pct  —— `kline_snapshots` 最近一条 1h 读数（XAU/BTC/ETH/SOL/WTI 有）
    · equity_usdt —— v409.5: 优先 `sim_equity_snapshots` 最新一条（实时权益，
      sim_equity.py 写的，S5 账实相符校验过）；没有快照才退回
      config.PAPER_INITIAL_EQUITY_USDT，且不把初始值冒充当前权益。

    v409.1: asset 为空时允许从正文 rule 里兜底认品种（见 asset_from_text），
    认不出就不放 atr_pct —— 宁可退回 observe_only，也不拿错品种的 ATR 去算。
    """
    ctx: Dict[str, float] = {}
    # v409.5: 权益优先取 `sim_equity_snapshots` 的**实时快照**（sim_equity.py 每次结算后写，
    # 与 quick_sim_trades 的 pnl_usdt 由 S5 账实相符校验）；拿不到才退回纸盘初始权益。
    # 之前用初始 10000 当权益是"借用" —— 实际已亏到 9997.57，长期跑下去会算错 notional。
    try:
        _row = conn.execute(
            "SELECT equity FROM sim_equity_snapshots WHERE equity IS NOT NULL "
            "AND equity > 0 ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        if _row and _row[0]:
            ctx["equity_usdt"] = float(_row[0])
    except sqlite3.Error:
        pass
    if "equity_usdt" not in ctx:
        try:
            import config as _cfg
            _eq = float(getattr(_cfg, "PAPER_INITIAL_EQUITY_USDT", 0.0) or 0.0)
            if _eq > 0:
                ctx["equity_usdt"] = _eq
        except Exception:
            pass
    a = str(asset or "").strip().upper()
    if not a:
        a = asset_from_text(rule)
    if not a:
        return ctx
    try:
        row = conn.execute(
            "SELECT atr_pct FROM kline_snapshots WHERE symbol=? AND timeframe='1h' "
            "AND atr_pct IS NOT NULL AND atr_pct > 0 ORDER BY ts DESC LIMIT 1", (a,)
        ).fetchone()
        if row and row[0]:
            ctx["atr_pct"] = float(row[0])
    except sqlite3.Error:
        pass
    return ctx


def classify_one(conn: sqlite3.Connection, insight_id: int) -> int:
    """v409: 洞察**落库当刻**就判定（不等 5 分钟后的巡检）。

    v399 的分类放在反思 worker 每轮开头，属于"事后筛"；这里把同一把尺子
    移到写入口，让 `observe_only` 与参数键从一开始就成立 —— 晋升链路
    （weekly_insight_apply）读到的永远是已判定的行，不会短暂看到"未分类"。

    幂等：已判定过的行不动。返回 1 表示本次判了，0 表示跳过（已判/无此 id）。
    """
    try:
        row = conn.execute(
            "SELECT id, rule, asset, COALESCE(source_samples,0) AS n, "
            "COALESCE(insight_classified_at,'') AS done "
            "FROM review_insights WHERE id=?", (int(insight_id),)).fetchone()
    except sqlite3.Error:
        return 0
    if not row or row[4]:
        return 0
    c = classify(row[1], row[2] or "", int(row[3] or 0),
                 context=market_context(conn, row[2] or "", row[1] or ""))
    try:
        conn.execute(
            "UPDATE review_insights SET insight_hypothesis=?, insight_param_key=?,"
            " insight_param_bucket=?, insight_direction=?, insight_window_h=?,"
            " observe_only=?, observe_reason=?, insight_classified_at=datetime('now')"
            " WHERE id=?",
            (c["hypothesis"], c["param_key"], c["param_bucket"], c["direction"],
             c["window_h"], c["observe_only"], c["observe_reason"], int(insight_id)),
        )
        conn.commit()
    except sqlite3.Error:
        return 0
    return 1


def ensure_classified(conn: sqlite3.Connection, limit: int = 200) -> int:
    """给未分类的洞察补写分类（幂等）。返回本次处理条数。"""
    try:
        rows = conn.execute(
            "SELECT id, rule, asset, COALESCE(source_samples,0) AS n "
            "FROM review_insights WHERE COALESCE(insight_classified_at,'')='' "
            "ORDER BY id LIMIT ?",
            (int(limit),),
        ).fetchall()
    except sqlite3.Error:
        return 0
    if not rows:
        return 0
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.Error:
        pass
    done = 0
    for r in rows:
        try:
            iid, rule, asset, n = r[0], r[1], r[2], r[3]
            c = classify(rule, asset or "", int(n or 0),
                         context=market_context(conn, asset or "", rule or ""))
            conn.execute(
                "UPDATE review_insights SET insight_hypothesis=?, insight_param_key=?,"
                " insight_param_bucket=?, insight_direction=?, insight_window_h=?,"
                " observe_only=?, observe_reason=?, insight_classified_at=datetime('now')"
                " WHERE id=?",
                (c["hypothesis"], c["param_key"], c["param_bucket"], c["direction"],
                 c["window_h"], c["observe_only"], c["observe_reason"], iid),
            )
            done += 1
        except sqlite3.Error:
            continue
    try:
        conn.commit()
    except sqlite3.Error:
        pass
    return done


def pool_stats(conn: sqlite3.Connection) -> Dict[str, int]:
    """观察型 / 可执行 两个池的条数 + 可执行池的验证通过情况。"""
    out = {"active": 0, "observe_only": 0, "actionable": 0,
           "actionable_verified": 0, "actionable_passed": 0, "unclassified": 0}
    try:
        out["active"] = int(conn.execute(
            "SELECT COUNT(*) FROM review_insights WHERE active=1").fetchone()[0] or 0)
        out["observe_only"] = int(conn.execute(
            "SELECT COUNT(*) FROM review_insights WHERE active=1 AND COALESCE(observe_only,0)=1"
        ).fetchone()[0] or 0)
        out["actionable"] = out["active"] - out["observe_only"]
        out["unclassified"] = int(conn.execute(
            "SELECT COUNT(*) FROM review_insights WHERE active=1"
            " AND COALESCE(insight_classified_at,'')=''").fetchone()[0] or 0)
        for verdict, in conn.execute(
            "SELECT COALESCE(verification_verdict,'') FROM review_insights"
            " WHERE active=1 AND COALESCE(observe_only,0)=0"
        ).fetchall():
            if verdict:
                out["actionable_verified"] += 1
                if str(verdict).lower() in _PASS_VERDICTS:
                    out["actionable_passed"] += 1
    except sqlite3.Error:
        return out
    return out


def observe_detail(stats: Optional[Dict[str, int]] = None) -> str:
    s = stats or {}
    act = int(s.get("actionable") or 0)
    ver = int(s.get("actionable_verified") or 0)
    passed = int(s.get("actionable_passed") or 0)
    pct = f"{passed * 100.0 / ver:.1f}%" if ver else "—"
    return (f"可执行 {act} 条 / 只能观察 {int(s.get('observe_only') or 0)} 条；"
            f"可执行池已验证 {ver} 条、通过 {passed} 条（{pct}）")
