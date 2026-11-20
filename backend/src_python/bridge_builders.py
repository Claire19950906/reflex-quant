#!/usr/bin/env python3
"""bridge_builders.py — 桥服务里"纯数据加工"的部分（2026-11-18）。

从 bridge_server.py（10,000+ 行）搬出来的第一个闭包：
`_v237_by_dimension_build` 是 543 行的巨无霸，它带 4 个私有助手（时间窗/取数/格式化），
加上通用的 `db_cursor`，合计 5 个函数 + 2 个常量。

搬之前先把它压到"能搬"的条件：DB_PATH 从两个服务各写一遍收成 `db_location`（唯一来源），
于是这里只依赖 db_location / time_utils / stdlib，不回引 bridge_server（避免循环）。

**装饰器要跟 def 一起搬**（`@contextmanager` 在 decorator_list 里，`node.lineno` 是 def 那行，
只按 lineno 切会把装饰器落在原文件里、错挂到下一个函数上——第一次就这么翻车了）。

bridge_server 用 `from bridge_builders import *` 接回，名字不变。
"""

from __future__ import annotations

from common import time_utils
from contextlib import contextmanager
from datetime import datetime, timezone, time as datetime_time, date, timedelta
import sqlite3
import time

from db_location import DB_PATH

import re
from typing import Any

__all__ = [
    "MAX_PLAUSIBLE_MOVE_PCT",
    "_strip_hash_prefix",
    "_v237_by_dimension_build",
    "_fmt_range",
    "_decision_ts_column",
    "_load_decision_ts_by_asset",
    "db_cursor",
]


MAX_PLAUSIBLE_MOVE_PCT = 90.0


def _v237_by_dimension_build(days: int) -> dict:
    """
    推导 A/B/C 三维度漏检分类(完全基于真实数据),每个维度下细分小类 (v291+):

    A 维度(买卖信号 - signal):
      A1_strong_buy_missed     强买入信号(algo_score>=0.7 且 move>0),无 trade_decision
      A2_strong_sell_missed    强卖出信号(algo_score>=0.7 且 move<0),无 trade_decision
      A3_continuation_missed   持续性信号(>3d 持续),无 trade_decision

    B 维度(信息方向 - direction):
      B1_rate_conflict         行情方向 vs 利率方向冲突
      B2_macro_news_conflict   同期宏观新闻情绪 vs 行情方向反向
      B3_thin_market           周末/节假日 流动性陷阱 (move 幅度异常大, 但属"无主"时段)

    C 维度(技术错误 - technical):
      C1_feed_latency          feed 延迟异常 (latency_ms > 2000)
      C2_feed_failure          feed 抓取失败 (status=failed)
      C3_data_gap              数据缺口 (ohlcv_daily 缺失 >3d)

    数据为空时返回 honest empty + status
    """
    cutoff = int(time.time()) - days * 86400
    # v291+ 细分子类, 每个 item 都有 subcategory 字段
    A_items: list[dict] = []
    B_items: list[dict] = []
    C_items: list[dict] = []

    with db_cursor() as conn:
        has_decisions = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='trade_decisions'"
        ).fetchone()
        has_signals = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='signals'"
        ).fetchone()

        # ── v308 P1-fix3: A/B/C 维度的三条数据诚实性修复 ──────────────────
        #
        # ① 计数源: 旧代码从 "LIMIT 200 的明细" 派生 n, 于是 days>=90 时
        #    A 恒等于 200 (撞上限), 与天数无关 —— 界面显示的是"被截断的条数"
        #    而不是真实漏检数。现在取**全集**, 明细再单独截断。
        #
        # ② 量纲异常: external_indicators 里个别资产的价格序列混进了不同量纲
        #    的数据 (BTC 曾出现 start=77.48 → end=7722.72, move_pct=9867%)。
        #    这类行不是市场异动, 是数据污染, 计入 A 维度会让"漏检数"虚高。
        #    |move_pct| > 90% 对任何资产的一日涨跌都不可能成立 → 剔除并单独计数。
        #
        # ③ 决策匹配: trade_decisions 现行结构的时间列是 decision_ts (REAL epoch)。
        #    旧代码查 created_at —— 该列在现行表里是 TEXT(或不存在),
        #    ABS(created_at - ts) 恒不命中 → **所有**强信号都被判成"漏检",
        #    A 维度等于"强信号总数", 完全没有做漏检判定。
        _MAX_PLAUSIBLE_MOVE_PCT = MAX_PLAUSIBLE_MOVE_PCT
        cpm_rows_all = conn.execute(
            "SELECT id, asset, ts, move_pct, move_kind, algo_score, duration_min "
            "FROM critical_price_moves "
            "WHERE algo_score >= 0.7 AND ts >= ? "
            "ORDER BY ts DESC",
            (cutoff,),
        ).fetchall()
        cpm_bad_rows = sum(
            1 for r in cpm_rows_all
            if abs(r["move_pct"] or 0) > _MAX_PLAUSIBLE_MOVE_PCT
        )
        cpm_rows = [
            r for r in cpm_rows_all
            if abs(r["move_pct"] or 0) <= _MAX_PLAUSIBLE_MOVE_PCT
        ]

        # 决策时间列 —— 统一走 _load_decision_ts_by_asset (decision_ts REAL epoch;
        # 老库回退 created_at 时按 UTC 墙钟解析, 不做裸数值减法)。
        # v309 P1: 这里原来内联了一份同样的逻辑, 与 /api/v237/missed-signals 端点
        # 的实现各写各的 —— 后者还在用 `ABS(created_at - ?)` (恒不命中), 导致两个
        # 端点对"同期有无决策"的判定口径不一致。现在合并到同一个 helper。
        _td_ts_col = _decision_ts_column(conn)
        _dec_by_asset: dict[str, list[float]] = {}
        if has_decisions is not None:
            _dec_by_asset = _load_decision_ts_by_asset(conn, cutoff - 86400)
        fed_rows = conn.execute(
            "SELECT ts, dgs2_pct, effr_pct, spread_bp FROM fed_rate_history ORDER BY ts ASC"
        ).fetchall()
        # 真实时间范围 (用于 data_alignment 动态展示)
        # v308 P1-fix3: COUNT 也只算量纲合理的行 —— 否则界面上 "(n rows)" 会把
        # 污染行算进去, 和下面 A 维度"参与统计的强信号总数"对不上。
        cpm_range = conn.execute(
            "SELECT MIN(ts), MAX(ts), COUNT(*) FROM critical_price_moves "
            "WHERE ts >= ? AND ABS(COALESCE(move_pct,0)) <= ?",
            (cutoff, MAX_PLAUSIBLE_MOVE_PCT),
        ).fetchone()
        fed_range = conn.execute(
            "SELECT MIN(ts), MAX(ts), COUNT(*) FROM fed_rate_history"
        ).fetchone()
        fh_range = conn.execute(
            "SELECT MIN(ts), MAX(ts), COUNT(*) FROM feed_health"
        ).fetchone()

        def fed_direction_at(ts: int) -> int:
            if len(fed_rows) < 2:
                return 0
            prev = next((row for row in fed_rows if row["ts"] <= ts), None)
            nxt = next((row for row in fed_rows if row["ts"] > ts), None)
            if prev is None or nxt is None:
                return 0
            delta = (nxt["effr_pct"] or 0) - (prev["effr_pct"] or 0)
            if abs(delta) < 0.01:
                return 0
            return 1 if delta > 0 else -1

        for cpm in cpm_rows:
            fed_dir = fed_direction_at(cpm["ts"])
            move_dir = 1 if cpm["move_pct"] > 0 else -1

            # A 维度: 强信号但同期无任何决策记录
            # (修复: 原逻辑要求 trade_decisions/signals 表"不存在"才输出, 反了.
            #  正确语义 = critical_move 出现时, 同期 ±24h 内没有对应的 trade_decisions
            #  条目 — 这才是"应该下决策但没下"的漏检.
            #  若 trade_decisions 表根本不存在, 则全量高信号都视为 A 维度.)
            if has_decisions is None:
                # 表不存在 → 把所有强信号列作"应下而未下"
                a_reason = "DB 中无 trade_decisions 表 — 上游决策源未接入, 所有强信号视为漏检"
            else:
                # v308 P1-fix3: 用预载的决策时间做内存匹配 (±24h)。
                # 旧代码每条信号发一次 SQL, 且查的是错误列名 created_at
                # (现行表是 decision_ts) → ABS(created_at - ts) 恒为巨大值,
                # 永远匹配不到 → 所有强信号都被误判为"漏检"。
                _dec_ts = _dec_by_asset.get((cpm["asset"] or "").upper()) or []
                td_hit = None
                for _dt in _dec_ts:
                    if abs(_dt - cpm["ts"]) <= 86400:
                        td_hit = _dt
                        break
                if td_hit is None:
                    a_reason = (
                        f"强信号出现时, 同期 (±24h) 无 trade_decision 记录覆盖 — "
                        f"决策系统未识别该信号 (asset={cpm['asset']})"
                    )
                else:
                    a_reason = None
            if a_reason is not None:
                # v291+ 细分子类
                if cpm["move_pct"] > 0:
                    subcategory = "A1_strong_buy_missed"
                else:
                    subcategory = "A2_strong_sell_missed"
                # 持续性判断 (duration_min > 3*1440 = 4320 分钟 ≈ 3d)
                if cpm["duration_min"] >= 4320:
                    subcategory = "A3_continuation_missed"
                A_items.append({
                    "asset": cpm["asset"],
                    "ts": cpm["ts"],
                    "subcategory": subcategory,  # v291+ 新增
                    "pattern": "strong_signal_no_decision",
                    "signal": f"algo_score={cpm['algo_score']:.2f} move_pct={cpm['move_pct']:.2f}",
                    "ignored_reason": a_reason,
                    "confidence_at_time": cpm["algo_score"],
                    "actual_outcome_5d": None,
                    "source": "critical_price_moves+trade_decisions",
                })

            # B 维度（v323 重写）：方向错必须基于「真的给过方向」的决策。
        # 旧实现是「利率变动 ±7 天内有任意反向行情」→ 笛卡尔展开：
        # 实测产出 40,508 条、全部同一个罪名 B1_rate_conflict，数量只取决于行情密度，
        # 与判断质量无关 —— 等于把「逻辑框架错」这一大类彻底糊掉了。
        # 新口径：days 窗口内所有 BUY/SELL 决策 → 决策时刻价 vs +60 分钟价，
        # 反向且幅度超阈值才算方向错，并按「错在哪一层」细分小类：
        #   B1_macro_conflict     宏观锚（利率/央行/通胀）解读反了
        #   B2_event_misdirection 事件方向解读反了（该涨判跌 / 该跌判涨）
        #   B3_chain_misuse       因果链选错（有 event_struct 链但行情反向）
        _B_HORIZON_SEC = 3600
        _B_WRONG_PCT = 0.5   # 反向 0.5% 以上算「方向错」，以下算噪声

        def _px_at(sym: str, ts: int):
            try:
                r = conn.execute(
                    "SELECT price FROM market_ticks WHERE symbol=? AND ts<=?"
                    " ORDER BY ts DESC LIMIT 1", (sym, int(ts)),
                ).fetchone()
                return float(r["price"]) if r and r["price"] else None
            except Exception:
                return None

        try:
            _b_rows = conn.execute(
                "SELECT id, target_asset, suggested_action, sentiment_score,"
                "       created_at, COALESCE(market_category,'') AS mc,"
                "       COALESCE(event_struct,'') AS es,"
                "       COALESCE(reasoning,'') AS rs"
                " FROM ai_decisions"
                " WHERE suggested_action IN ('BUY','SELL')"
                " ORDER BY id DESC LIMIT 4000"
            ).fetchall()
        except Exception as _e:
            print(f"[v237_by_dimension] B 维度取决策失败: {type(_e).__name__}: {_e}", flush=True)
            _b_rows = []

        _b_checked = 0
        for dr in _b_rows:
            sym = str(dr["target_asset"] or "").upper()
            if sym not in ("BTC", "ETH", "SOL", "WTI", "XAU"):
                continue
            ts0 = time_utils.utc_str_to_epoch(str(dr["created_at"] or ""))
            if ts0 is None or float(ts0) < cutoff:
                continue
            t0 = int(float(ts0))
            p0 = _px_at(sym, t0)
            p1 = _px_at(sym, t0 + _B_HORIZON_SEC)
            if not p0 or not p1:
                continue
            _b_checked += 1
            pct = (p1 - p0) / p0 * 100.0
            act = str(dr["suggested_action"]).upper()
            wrong = (act == "BUY" and pct <= -_B_WRONG_PCT) or (act == "SELL" and pct >= _B_WRONG_PCT)
            if not wrong:
                continue
            mc = str(dr["mc"] or "").upper()
            es = str(dr["es"] or "")
            rs = str(dr["rs"] or "")
            # 方向决策里实测没有 MACRO 类别（全是 GOLD/CRYPTO/OIL/STOCK），
            # 所以宏观错必须靠推理文本判，别指望 category 字段。
            _macro_kw = ("利率", "美联储", "FOMC", "CPI", "非农", "央行", "加息", "降息", "通胀", "国债收益率")
            if "MACRO" in mc or "宏观" in mc or any(k in rs for k in _macro_kw):
                sub, why = "B1_macro_conflict", "宏观锚（利率/央行/通胀）解读方向反了"
            elif es and ("chain" in es.lower() or "causal" in es.lower() or "链" in es):
                sub, why = "B3_chain_misuse", "因果链选错了链条，行情反向"
            else:
                sub, why = "B2_event_misdirection", "事件方向解读反了（该涨判跌/该跌判涨）"
            B_items.append({
                "asset": sym,
                "ts": t0,
                "subcategory": sub,
                "pattern": "direction_wrong_vs_price",
                "signal": f"{act} @ {p0:.2f} → 60分钟后 {p1:.2f} ({pct:+.2f}%)",
                "ignored_reason": f"{why}（决策#{dr['id']}，分数 {dr['sentiment_score']}）",
                "confidence_at_time": dr["sentiment_score"],
                "actual_outcome_5d": round(pct, 2),
                "source": "ai_decisions+market_ticks(60m)",
            })
        print(f"[v237_by_dimension] B 维重算: 参与 {_b_checked} 笔方向决策 → 方向错 {len(B_items)} 条", flush=True)

        # C 维度: 独立派生自 feed_health 异常 (不依赖 critical_moves 时间锚)
        # v291+ 修复: feed_health (Oct 17-18 ISO text) 与 critical_moves (Oct 9-12 unix ts)
        # 时间错开. 改为: 直接遍历 feed_health 找 status='failed' 或 latency > 2000ms
        # 的条目, 按 (asset, source, day) 去重, 转成 unix ts, 生成 C1/C2 项.
        feed_rows_raw = conn.execute(
            "SELECT ts, asset, source, latency_ms, status, error, feed_kind "
            "FROM feed_health "
            "WHERE (status='failed' OR latency_ms > 2000) "
            "ORDER BY ts DESC LIMIT 200"
        ).fetchall()
        seen_keys: set[tuple] = set()
        for fr in feed_rows_raw:
            ts_text = fr["ts"]
            try:
                if "T" in ts_text:
                    dt = datetime.fromisoformat(ts_text.replace("Z", "+00:00"))
                else:
                    dt = datetime.strptime(ts_text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                unix_ts = int(dt.timestamp())
            except Exception:
                continue
            day_key = ts_text[:10]
            dedup_key = (fr["asset"], fr["source"], day_key)
            if dedup_key in seen_keys:
                continue
            seen_keys.add(dedup_key)
            if fr["status"] == "failed":
                subcategory = "C2_feed_failure"
                ignored_reason = (
                    f"{fr['source']} feed 抓取失败 (status={fr['status']}, "
                    f"err={fr['error']}) — 数据盲区"
                )
            else:
                subcategory = "C1_feed_latency"
                ignored_reason = (
                    f"{fr['source']} feed 延迟 {fr['latency_ms']}ms 超过 2s 阈值"
                )
            C_items.append({
                "asset": fr["asset"] or "GLOBAL",
                "ts": unix_ts,
                "subcategory": subcategory,
                "pattern": "feed_health_degraded",
                "signal": f"latency={fr['latency_ms']}ms status={fr['status']} source={fr['source']}",
                "ignored_reason": ignored_reason,
                "confidence_at_time": min(0.95, (fr["latency_ms"] or 0) / 5000),
                "actual_outcome_5d": None,
                "source": "feed_health(独立)",
            })

    # ─── v302+ P1: trade_reflections_v2 三维度细分 (A1/A2/A3, B1/B2/B3, C1/C2/C3 真实样本)
    # 这是反思系统的"判定历史" — 每个样本都是实际发生的反思记录, 按 dimension+subcategory
    # 聚合后, 让前端"三维度错因"卡片能展示真实的"细分小类",而不是后端推导的空模板.
    #
    # shape 严格匹配 frontend components/v237/MissedSignalsDimensionSummary.tsx
    # 期望的 dimensions.{A,B,C}.{total, hit, hit_rate, by_subcategory} 形状
    #
    # 注意: trade_reflections_v2.subcategory 命名约定 (2026-10-15 后)
    #   A: market_regime_misread / pattern_recognition / psychological_anchor / volume_price_divergence
    #   B: cause_effect_reversed / event_chain_break / macro_misattribute / news_direction_inverted
    #   C: calibration_drift / data_feed_timeout / feed_missing / signal_delay
    # 这与 critical_price_moves 推导出的 A1/A2/A3 (stron_buy/sell/continuation) 是不同维度,
    # 前者是"系统反思时的归因" (meta), 后者是"信号发生时是否漏检" (signal)
    reflections_dimensions: dict[str, dict] = {
        "A": {"label": "买卖信号判断", "total": 0, "hit": 0, "hit_rate": 0.0,
              "by_subcategory": {}, "items": []},
        "B": {"label": "信息分析方向", "total": 0, "hit": 0, "hit_rate": 0.0,
              "by_subcategory": {}, "items": []},
        "C": {"label": "技术错误",     "total": 0, "hit": 0, "hit_rate": 0.0,
              "by_subcategory": {}, "items": []},
    }
    reflections_total = 0
    try:
        # v309 P1: created_at 是 TEXT, 且历史上有**两种**落库格式:
        #   · 'YYYY-MM-DDTHH:MM:SS' (T 分隔) ← paper_reflection_worker 写的是
        #     time.strftime(..., time.localtime()), 即本机本地时间 (= 北京)
        #   · 'YYYY-MM-DD HH:MM:SS' (空格分隔) ← bridge_server / DB DEFAULT
        #     写的是 datetime('now'), 即 **UTC**
        # v309 P0 只把 epoch 整数改成了空格格式字符串, 但没有处理 T 分隔那批 →
        # 两种格式混在一列里, 直接字典序比较会在**同一日期内**错乱
        # ('T' 的 ASCII 比 ' ' 大, 于是 '2026-10-25T01:00:00' > '2026-10-25 08:00:00')。
        # 现在统一走 time_utils.sql_bj_ts_expr(): 两种格式都归一化成
        # "北京墙钟 + 空格分隔" 再比较, 字典序才可靠。
        # cutoff 用北京时区换算 (项目约定: 裸时间串 = 北京时间)。
        _ref_cutoff_str = time_utils.epoch_to_bj_str(cutoff)
        _ref_ts_expr = time_utils.sql_bj_ts_expr("created_at")
        with db_cursor() as ref_conn:
            # v482 起 HOLD 漏检行改写独立记账表 missed_signal_reflections，
            # trade_reflections_v2 不再新增漏检模板行。读侧必须把两表并起来，
            # 否则新漏检反思在三维度聚合里不可见（迁移漏接的读侧收口）。
            has_msr = ref_conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='missed_signal_reflections'"
            ).fetchone() is not None

            # 归一化子查询：两表列名对齐（MSR 的 move_pct ⇔ v2 的 actual_outcome_pct）
            _ref_union = (
                "SELECT dimension, subcategory, hit, actual_outcome_pct AS outcome, "
                "       confidence_at_time, reflection_text, asset, created_at, id "
                "FROM trade_reflections_v2 "
                f"WHERE {_ref_ts_expr} >= ? AND dimension IN ('A','B','C') "
            )
            if has_msr:
                _ref_union += (
                    " UNION ALL "
                    "SELECT dimension, subcategory, hit, move_pct AS outcome, "
                    "       confidence_at_time, reflection_text, asset, created_at, decision_id AS id "
                    "FROM missed_signal_reflections "
                    f"WHERE {_ref_ts_expr} >= ? AND dimension IN ('A','B','C') "
                )

            ref_rows = ref_conn.execute(
                "SELECT dimension, subcategory, "
                "       COUNT(*) as n, "
                # v323：实测 527 条 calibration_drift 的 hit=1 但 actual_outcome_pct 是 NULL
                # —— 结果压根没算出来，却被记成"命中"，于是"命中率 99%"是凭空造的。
                # 命中只对**已结算**（有真实未来涨跌）的行统计，未结算的单独计数。
                "       SUM(CASE WHEN hit=1 AND outcome IS NOT NULL "
                "                AND ABS(outcome) <= 60 "
                "                THEN 1 ELSE 0 END) as n_hit, "
                # v323：546 条 calibration_drift 里 527 条 actual_outcome_pct 是 NULL
                # （真实结果从没算过），剩下 19 条又被 3 条量纲异常值（1058%/350%）
                # 主导，均值被拉成 89.78% —— 一个 5 日涨跌不可能出现的数。
                # 所以：均值只统计"有值且在可信区间内"的行，同时把覆盖率单独暴露，
                # 免得把"没算出来"伪装成"平均涨了 89%"。
                "       AVG(CASE WHEN outcome IS NOT NULL "
                "                AND ABS(outcome) <= 60 "
                "                THEN outcome END) as avg_outcome, "
                "       SUM(CASE WHEN outcome IS NOT NULL "
                "                AND ABS(outcome) <= 60 "
                "                THEN 1 ELSE 0 END) as n_outcome, "
                "       AVG(confidence_at_time) as avg_conf "
                "FROM (" + _ref_union + ") "
                # v323：A 维 2,626 条里 2,276 条是**空壳**（reflection_text 为空、
                # action=REJECTED 的批量占位行），全表 4,959 条里 3,026 条如此。
                # 把它们算进分母，等于用没写过反思的行去点评"反思准不准"。
                # 这里只统计真正有反思内容的行，被排除的条数单独返回（不隐藏）。
                "WHERE TRIM(COALESCE(reflection_text,'')) <> '' "
                "GROUP BY dimension, subcategory "
                "ORDER BY dimension, n DESC",
                (_ref_cutoff_str, _ref_cutoff_str) if has_msr else (_ref_cutoff_str,),
            ).fetchall()
            # 同时拉明细 (前 100 条, 给"三维度明细"面板用)
            # 空壳行计数同样并两表（MSR 也可能落空 reflection_text 的占位行）
            _empty_union = (
                "SELECT reflection_text FROM trade_reflections_v2 "
                f"WHERE {_ref_ts_expr} >= ? AND dimension IN ('A','B','C') "
            )
            if has_msr:
                _empty_union += (
                    " UNION ALL SELECT reflection_text FROM missed_signal_reflections "
                    f"WHERE {_ref_ts_expr} >= ? AND dimension IN ('A','B','C') "
                )
            _empty_rows = ref_conn.execute(
                "SELECT COUNT(*) FROM (" + _empty_union + ") "
                "WHERE TRIM(COALESCE(reflection_text,'')) = ''",
                (_ref_cutoff_str, _ref_cutoff_str) if has_msr else (_ref_cutoff_str,),
            ).fetchone()[0]
            ref_items = ref_conn.execute(
                "SELECT id, asset, dimension, subcategory, confidence_at_time, "
                "       outcome AS actual_outcome_pct, hit, created_at, reflection_text "
                "FROM (" + _ref_union + ") "
                "ORDER BY created_at DESC LIMIT 100",
                (_ref_cutoff_str, _ref_cutoff_str) if has_msr else (_ref_cutoff_str,),
            ).fetchall()
        ref_items_json = [dict(r) for r in ref_items]
        # 按维度聚合
        for k in ("A", "B", "C"):
            reflections_dimensions[k]["items"] = [
                ri for ri in ref_items_json if ri.get("dimension") == k
            ][:30]
        grand_total = 0
        for r in ref_rows:
            k = r["dimension"]
            if k not in reflections_dimensions:
                continue
            sub = r["subcategory"] or "(unknown)"
            n = r["n"] or 0
            n_hit = r["n_hit"] or 0
            # 命中率分母 = 已结算条数（有真实结果）；没有结算样本时命中率记 0，
            # 并在 outcome_coverage 里暴露"这批样本其实没验证过"。
            _n_settled_for_rate = r["n_outcome"] or 0
            hit_rate = round(n_hit / _n_settled_for_rate, 4) if _n_settled_for_rate > 0 else 0.0
            # v323：risk_limit_blocked / strength_gate_blocked / batch_conflict_blocked
            # 是**系统主动拦下信号**（额度、闸门、连坐），不是"判断错了"。
            # 之前混在 A 维"判断失误"里，"A 维有多差"会被这些规则拦截污染。
            _RULE_BLOCK_SUBS = {
                "risk_limit_blocked", "strength_gate_blocked",
                "batch_conflict_blocked",
            }
            _n_out = r["n_outcome"] or 0
            reflections_dimensions[k]["by_subcategory"][sub] = {
                "n": n,
                "hit": n_hit,
                "hit_rate": hit_rate,
                "avg_outcome_pct": round(r["avg_outcome"] or 0.0, 3),
                "avg_confidence": round(r["avg_conf"] or 0.0, 3),
                # 有多少条真正算出了未来涨跌（n_outcome/n），低于 100% 就别把均值当真
                "outcome_coverage": round(_n_out / n, 3) if n > 0 else 0.0,
                # 三分类：规则拦截（系统主动拦）/ 未验证（历史标签但无证据）/ 判断失误
                # C 维本身就是"技术错误"（数据缺失/信号慢/接口失败），
                # 性质上不属于"判断失误"，不能混进判断力统计。
                "kind": (
                    "technical" if k == "C"
                    else "rule_block" if sub in _RULE_BLOCK_SUBS
                    else "unverified" if sub == "unverified_legacy_label"
                    else "judgment"
                ),
            }
            reflections_dimensions[k]["total"] += n
            reflections_dimensions[k]["hit"] += n_hit
            if sub in _RULE_BLOCK_SUBS:
                reflections_dimensions[k]["rule_block_total"] = (
                    reflections_dimensions[k].get("rule_block_total", 0) + n
                )
            elif k == "C":
                reflections_dimensions[k]["technical_total"] = (
                    reflections_dimensions[k].get("technical_total", 0) + n
                )
            elif sub == "unverified_legacy_label":
                reflections_dimensions[k]["unverified_total"] = (
                    reflections_dimensions[k].get("unverified_total", 0) + n
                )
            else:
                reflections_dimensions[k]["judgment_total"] = (
                    reflections_dimensions[k].get("judgment_total", 0) + n
                )
            reflections_dimensions[k]["hit_rate"] = round(
                reflections_dimensions[k]["hit"] / max(1, reflections_dimensions[k]["total"]), 4
            )
            grand_total += n
        reflections_total = grand_total
    except Exception as e:
        # 不影响主流程, 仅 trade_reflections 维度细分留空
        print(f"[v237_by_dimension] reflection aggregation query failed: {type(e).__name__}: {e}", flush=True)
        reflections_dimensions = {k: {"label": v["label"], "total": 0, "hit": 0,
                                       "hit_rate": 0.0, "by_subcategory": {}, "items": [],
                                       "judgment_total": 0, "rule_block_total": 0,
                                       "unverified_total": 0, "technical_total": 0}
                                  for k, v in reflections_dimensions.items()}

    _a_total = len(A_items)
    _b_total = len(B_items)
    _c_total = len(C_items)
    _sum_total = _a_total + _b_total + _c_total

    # v323 口径统一：这个接口同时给出两个**不同含义**的数，之前没标清楚，
    # 页面上看起来像"同一维度两个数字对不上"。
    #   totals[k]                = 漏检信号数（critical_price_moves 实时算：
    #                              强信号出现了，但同期没有决策去接）
    #   dimensions[k].total      = 反思样本数（trade_reflections_v2 落库：
    #                              系统事后真的反思了多少条、对错如何）
    # 二者本来就不该相等（不是每条漏检都会被反思）。这里把涇渭分明的语义
    # 直接写进 payload，并给每个维度补上 missed_total，前端一处显示两个口径。
    _missed_map = {"A": _a_total, "B": _b_total, "C": _c_total}
    for _k, _v in (reflections_dimensions or {}).items():
        if isinstance(_v, dict):
            _v["missed_total"] = _missed_map.get(_k, 0)
    # v308 P1-fix3: grand_total 之前恒为 0 (reflections_total, 因为
    # trade_reflections_v2 里 dimension IN ('A','B','C') 实际为 0 行),
    # 与同层的 sum=1156 自相矛盾 —— 前端注释里还写着"grand_total 516"。
    # 现在 grand_total = 三维度真实总数 (与 sum 一致), 反思样本数单列。
    return {
        "ok": True,
        "days": days,
        "semantics": {
            "missed_total": "漏检信号数 = 强信号出现但同期无决策接住（critical_price_moves 实时计算）",
            "reflection_total": "反思样本数 = 系统事后实际写下的反思条数（trade_reflections_v2 + missed_signal_reflections，已排除空壳行）",
            "note": "两个数字口径不同，不应相等：漏检≠被反思。命中率与平均涨跌只对已结算的反思样本计算。",
        },
        "by_dimension": {
            "A_signal": {
                "label": "A · 买卖信号",
                "n": _a_total,
                # 明细截断到 50 条 (控响应体积), 但 n 是**真实总数**, 不受截断影响
                "items": A_items[:50],
                "items_returned": min(len(A_items), 50),
                "items_truncated": len(A_items) > 50,
                "note": "A 维度需要 trade_decisions 上游;DB 暂无" if not has_decisions and not has_signals else None,
            },
            "B_direction": {
                "label": "B · 信息分析方向",
                "n": _b_total,
                "items": B_items[:50],
                "items_returned": min(len(B_items), 50),
                "items_truncated": len(B_items) > 50,
                "note": None,
            },
            "C_technical": {
                "label": "C · 技术错误",
                "n": _c_total,
                "items": C_items[:50],
                "items_returned": min(len(C_items), 50),
                "items_truncated": len(C_items) > 50,
                "note": None,
            },
        },
        "totals": {
            "A": _a_total,
            "B": _b_total,
            "C": _c_total,
            "sum": _sum_total,
            # 三维度真实总数 (与 sum 一致) —— 不再是恒 0 的 reflections_total
            "grand_total": _sum_total,
            # 反思系统 (trade_reflections_v2) 样本数, 单独给出以免与三维混淆
            "reflections_total": reflections_total,
        },
        # v302+ P1: trade_reflections_v2 三维度细分 (反思系统真实样本)
        # shape 匹配 frontend components/v237/MissedSignalsDimensionSummary.tsx
        # 和 components/v237/MissedSignalsPanel.tsx (quality tab)
        "dimensions": reflections_dimensions,
        "grand_total": _sum_total,
        "data_quality": {
            # 决策匹配用的真实时间列 (decision_ts / created_at)
            "decision_ts_column": _td_ts_col,
            # v323：被排除在反思统计外的空壳行（reflection_text 为空的批量占位行）
            "reflections_empty_text_rows": locals().get("_empty_rows", 0),
            # 决策表缺失时无法做漏检判定 (A 维度 = 全部强信号)
            "decisions_available": has_decisions is not None,
            # 被剔除的量纲异常行数 (价格序列混档, 非市场异动)
            "implausible_move_rows_excluded": cpm_bad_rows,
            # 参与统计的强信号总数 (剔除异常后)
            "strong_signals_scanned": len(cpm_rows),
        },
        "data_sources": ["critical_price_moves", "fed_rate_history", "feed_health",
                         "trade_reflections_v2", "missed_signal_reflections"],
        "data_alignment": {
            "critical_moves_range": _fmt_range(cpm_range),
            "feed_health_range": _fmt_range(fh_range),
            "fed_rate_range": _fmt_range(fed_range),
            "warning": (
                "B/C 维度需要 critical_moves 与 fed_rate/feed_health 时间窗口重叠,"
                "若数据时间轴错开,B/C 自动为 0。A 维度基于 critical_moves 自身"
                " (反查 trade_decisions 表判断是否漏检) 仍可输出。"
                "trade_reflections_v2 维度细分是独立数据源,不受时间窗口错开影响."
            ),
        },
    }


def _fmt_range(rng) -> str:
    """把 (min_ts, max_ts, count) 序列化成 'YYYY-MM-DD ~ YYYY-MM-DD (n rows)'.
    容忍混合类型: int/float epoch 或 ISO text (YYYY-MM-DD HH:MM:SS)."""
    if not rng or rng[0] is None or rng[1] is None:
        return "(empty)"

    def _to_iso(v) -> str:
        if v is None:
            return "?"
        s = str(v)
        # ISO text already
        if isinstance(v, str) and "-" in s and ":" in s:
            return s[:10]
        # numeric epoch
        try:
            return time.strftime("%Y-%m-%d", time.gmtime(int(float(v))))
        except (ValueError, TypeError, OSError):
            return s[:10] if len(s) >= 10 else s

    lo = _to_iso(rng[0])
    hi = _to_iso(rng[1])
    n = int(rng[2]) if len(rng) > 2 and rng[2] is not None else 0
    return f"{lo} ~ {hi} ({n} rows)"


def _decision_ts_column(conn) -> str:
    """返回 trade_decisions 里实际用于"决策时间"的时间列名。

    现行结构是 decision_ts (REAL epoch); 老库回退 created_at。
    空串 = 该表没有可用时间列 (无法做漏检判定)。
    """
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(trade_decisions)")}
    except Exception:
        return ""
    if "decision_ts" in cols:
        return "decision_ts"
    if "created_at" in cols:
        return "created_at"
    return ""


def _load_decision_ts_by_asset(conn, since_epoch: int) -> dict[str, list[float]]:
    """按资产装载 trade_decisions 的决策时间 (epoch 秒), 供"同期是否已有决策"判定。

    v309 P1 (修遗留 bug): 判定必须用 **decision_ts (REAL epoch)**。
    旧代码查 created_at —— 现行表里 created_at 是 TEXT ('2026-10-25 00:36:12',
    语义为 UTC 墙钟), 拿它做 ABS(created_at - ts) 数值比较时 SQLite 只取前导数字
    ('2026') → 差值恒为巨大值 → **永远匹配不到** → 所有强信号都被判成"漏检",
    A 维度退化成"强信号总数", 完全没有做漏检判定。
    老库没有 decision_ts 时回退 created_at, 按 UTC 墙钟解析成 epoch 再比。

    since_epoch: 只装载该时间点之后的决策 (±24h 窗口只需要这个下界)。
    """
    out: dict[str, list[float]] = {}
    col = _decision_ts_column(conn)
    if not col:
        return out
    try:
        if col == "decision_ts":
            rows = conn.execute(
                "SELECT asset, decision_ts FROM trade_decisions "
                "WHERE decision_ts IS NOT NULL AND decision_ts >= ?",
                (since_epoch,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT asset, created_at FROM trade_decisions WHERE created_at IS NOT NULL"
            ).fetchall()
    except Exception as e:
        print(f"[bridge] 决策时间预载失败({col}): {e}", flush=True)
        return out
    for r in rows:
        if col == "decision_ts":
            try:
                ts = float(r["decision_ts"])
            except (TypeError, ValueError):
                continue
        else:
            ts = time_utils.utc_str_to_epoch(r["created_at"])
            if ts is None:
                continue
            ts = float(ts)
        if ts < since_epoch:
            continue
        out.setdefault((r["asset"] or "").upper(), []).append(ts)
    return out


@contextmanager
def db_cursor():
    """只读连接 (默认), 用于 GET 端点"""
    conn = sqlite3.connect(str(DB_PATH), timeout=5)
    conn.row_factory = sqlite3.Row
    # v311: 读侧也设 busy_timeout —— 撞写锁时等一下, 而不是立刻抛 "database is locked"
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA query_only=ON")
    try:
        yield conn
    finally:
        conn.close()


_HASH_PREFIX = re.compile(r"^\[hash:[0-9a-f]{4,}\]\s*", re.I)


def _strip_hash_prefix(text: Any) -> str:
    """去掉 raw_news.content 前面的内部指纹标记 [hash:xxxxxxxx]。"""
    return _HASH_PREFIX.sub("", str(text or "").strip())
