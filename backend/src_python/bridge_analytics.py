#!/usr/bin/env python3
"""bridge_analytics.py — 桥服务的"分析类"端点实现（2026-11-18）。

这两个端点的共同点：**纯读库聚合**，不带缓存、不起线程，但各自都很长
（`v291_reflections_summary` 229 行、`analysis_error_taxonomy` 150 行），
夹在 bridge_server 里就把"服务装配"和"SQL 聚合逻辑"搅在一起。

搬法：实现整体下沉（连 docstring / 参数默认值一起），`bridge_server` 里留同名薄端点
——签名、Query 校验、路由注册位置全部不变，前端契约与路由数都不受影响。

依赖只到 db_cursor（bridge_builders）/ time_utils / stdlib，不回引 bridge_server。
"""

from __future__ import annotations

import time
from typing import Annotated, Any

from fastapi import Query

from bridge_builders import MAX_PLAUSIBLE_MOVE_PCT, db_cursor
from bridge_news import _parse_asset_list
from common import time_utils

def v291_reflections_summary(
    asset: Annotated[str | None, Query(description="过滤资产 (None=全部)")] = None,
    days: Annotated[int, Query(ge=1, le=3650)] = 90,
) -> dict:
    """v291+ P1: 反思数据摘要聚合 (trade_reflections_v2 表 519 行)

    按 dimension + subcategory 聚合:
    - count: 数量
    - hit_rate: 命中概率 (hit=1 / count)
    - avg_outcome_pct: 平均实际收益 (%)
    - avg_confidence: 平均预测置信度
    - 暴露 top 3 worst subcategory (hit_rate 最低)
    """
    cutoff = int(time.time()) - days * 86400
    # v309 P1: created_at 是 TEXT, 历史上有两种落库格式 (T 分隔=本机本地时间 /
    # 空格分隔=UTC), 直接字典序比较会在同一日期内错乱 → 统一用
    # time_utils.sql_bj_ts_expr() 归一化成"北京墙钟 + 空格分隔"再比。
    # cutoff 用北京时区换算 (项目约定: 裸时间串 = 北京时间)。
    _cutoff_str = time_utils.epoch_to_bj_str(cutoff)
    _ts_expr = time_utils.sql_bj_ts_expr("created_at")

    with db_cursor() as conn:
        params: list[Any] = [_cutoff_str]
        asset_clause = ""
        if asset:
            asset_clause = " AND asset=?"
            params.append(asset.upper())
        rows = conn.execute(
            f"SELECT asset, dimension, subcategory, "
            f"  COUNT(*) as cnt, "
            f"  SUM(hit) as hits, "
            f"  AVG(actual_outcome_pct) as avg_out, "
            f"  AVG(confidence_at_time) as avg_conf, "
            f"  AVG(CAST(settled AS REAL)) as settled_rate "
            f"FROM trade_reflections_v2 "
            # v323：表里有 15 条量纲异常的 actual_outcome_pct（最大 508%，
            # 5 日涨跌不可能到这个量级，属落库时单位错了），它们会把
            # 分组均值直接拉飞（面板上 calibration_drift 平均 89.78% 就是它）。
            # 这里只做**读数侧**的可信区间过滤，不改原始行、不造数。
            f"WHERE {_ts_expr} >= ? AND ABS(COALESCE(actual_outcome_pct,0)) <= 60 {asset_clause} "
            f"GROUP BY asset, dimension, subcategory "
            f"ORDER BY asset, dimension, cnt DESC",
            params,
        ).fetchall()

    # v343: A 维存在"同一时点同一资产写多行"的重复 —— 实测 55 行只有 22 个唯一
    # (asset, created_at)，最重的一组 7 行；而且组内 hit 还会自相矛盾
    # （0.14% 那行判 miss、1.46% 那行判 hit，因为各自取了不同的结果窗口）。
    # 这里不改写库、不动命中率算法，只把重复程度如实暴露，避免把"行数"当"事件数"读。
    with db_cursor() as _dq_conn:
        _dq_rows = _dq_conn.execute(
            f"SELECT COUNT(*) AS n_rows, "
            f"  COUNT(DISTINCT asset || '|' || created_at) AS n_pts "
            f"FROM trade_reflections_v2 WHERE {_ts_expr} >= ? "
            f"AND ABS(COALESCE(actual_outcome_pct,0)) <= 60 {asset_clause}",
            params,
        ).fetchone()
        _conflict = _dq_conn.execute(
            f"SELECT COUNT(*) FROM (SELECT asset, created_at "
            f"FROM trade_reflections_v2 WHERE {_ts_expr} >= ? "
            f"AND ABS(COALESCE(actual_outcome_pct,0)) <= 60 {asset_clause} "
            f"GROUP BY asset, created_at HAVING COUNT(DISTINCT hit) > 1)",
            params,
        ).fetchone()[0]

    # v343: 事件点口径 —— 同一 asset+created_at 只算 1 个事件。
    # 组内 hit 全一致才计入命中率；互相矛盾的组判为"不可判"、排除出分母。
    # 这样"BTC 13 条全错"就不会再被重复行放大成 13 个错误。
    with db_cursor() as _pt_conn:
        _pt = _pt_conn.execute(
            f"SELECT dimension, COUNT(*) AS pts, "
            f"  SUM(CASE WHEN agree=1 THEN hit ELSE 0 END) AS hits, "
            f"  SUM(CASE WHEN agree=0 THEN 1 ELSE 0 END) AS conflicts "
            # 同一事件常被写成差 1~3 秒的多行（实测 07:17:43 与 07:17:44 就是一回事），
            # 所以按"分钟"归并，而不是精确到秒。
            f"FROM (SELECT asset, dimension, "
            f"  CAST(strftime('%s', replace(substr({_ts_expr},1,19), 'T', ' ')) AS INTEGER)/60 AS bucket, "
            f"  COUNT(DISTINCT hit) AS d, "
            f"  CASE WHEN COUNT(DISTINCT hit)=1 THEN 1 ELSE 0 END AS agree, "
            f"  MAX(hit) AS hit "
            f"  FROM trade_reflections_v2 WHERE {_ts_expr} >= ? "
            f"  AND ABS(COALESCE(actual_outcome_pct,0)) <= 60 {asset_clause} "
            f"  GROUP BY asset, bucket, dimension) "
            f"GROUP BY dimension",
            params,
        ).fetchall()
    _pt_by_dim = {r["dimension"]: r for r in _pt}
    _pt_pred = [r for k, r in _pt_by_dim.items() if k in ("A", "B")]
    _pt_total = sum(int(r["pts"] or 0) for r in _pt_pred)
    _pt_hits = sum(int(r["hits"] or 0) for r in _pt_pred)
    _pt_conf = sum(int(r["conflicts"] or 0) for r in _pt_pred)
    _pt_meas = _pt_total - _pt_conf

    by_dim: dict[str, list[dict]] = {"A": [], "B": [], "C": []}
    all_subs: list[dict] = []
    for r in rows:
        cnt = r["cnt"]
        hits = r["hits"] or 0
        hit_rate = (hits / cnt) if cnt > 0 else 0
        rec = {
            "asset": r["asset"],
            "dimension": r["dimension"],
            "subcategory": r["subcategory"],
            "count": cnt,
            "hits": int(hits),
            "misses": int(cnt - hits),
            "hit_rate": round(hit_rate, 3),
            "avg_outcome_pct": round(r["avg_out"] or 0, 2),
            "avg_confidence": round(r["avg_conf"] or 0, 3),
            "settled_rate": round(r["settled_rate"] or 0, 3),
        }
        if r["dimension"] in by_dim:
            by_dim[r["dimension"]].append(rec)
        all_subs.append(rec)

    # v343: 原来 overall_hit_rate = 所有维度 hits / 所有维度 count，但两个维度的 hit
    # 语义是**反的**：
    #   A/B —— hit=1 表示"判断被市场证实"（越高越好）
    #   C   —— hit=1 表示"错因被证实"（断流期间确实错过行情 / 迟到真错过，越高越糟）
    # 混在一起算出 3.2% 这种数字，看着像"系统命中率崩了"，实际只是口径污染。
    # 现在 overall 只统计预测类维度（A/B），C 单独给"错因成立率"。
    PREDICTION_DIMS = {"A", "B"}
    total = sum(r["count"] for r in all_subs)
    total_hits = sum(r["hits"] for r in all_subs)
    pred_subs = [r for r in all_subs if (r.get("dimension") or "") in PREDICTION_DIMS]
    pred_total = sum(r["count"] for r in pred_subs)
    pred_hits = sum(r["hits"] for r in pred_subs)
    overall_hit_rate = pred_hits / pred_total if pred_total > 0 else 0
    # v343: C 维里 `data_feed_timeout` 是"单次抓取失败"的流水（一次失败一条，
    # 没有"断流窗口"可言），它的 hit 恒为 0 —— 把它算进命中率分母，会让
    # "C 维错因成立率"永远显示 0%，而真实可判的只有 feed_missing / signal_delay。
    # 这里把"不可判"单独计数并排除出分母，避免继续误导。
    UNMEASURABLE_SUBS = {"data_feed_timeout"}
    unmeasurable = sum(r["count"] for r in all_subs
                       if r.get("subcategory") in UNMEASURABLE_SUBS)
    dim_hit_rate: dict[str, float] = {}
    for dim, recs in by_dim.items():
        d_total = sum(x["count"] for x in recs
                      if x.get("subcategory") not in UNMEASURABLE_SUBS)
        d_hits = sum(x["hits"] for x in recs
                     if x.get("subcategory") not in UNMEASURABLE_SUBS)
        dim_hit_rate[dim] = round(d_hits / d_total, 3) if d_total > 0 else 0
    overall_avg_out = (
        sum(r["avg_outcome_pct"] * r["count"] for r in all_subs) / total
        if total > 0
        else 0
    )

    qualified = [r for r in all_subs if r["count"] >= 3]
    worst_3 = sorted(qualified, key=lambda x: x["hit_rate"])[:3]
    best_3 = sorted(qualified, key=lambda x: -x["hit_rate"])[:3]

    # v330: 上面 WHERE 里有一条"读数侧可信区间"过滤 |actual_outcome_pct|<=60。
    # 这条过滤**故意**剔掉量纲错误的行 (5 日涨跌不可能 508%), 但它让端点数
    # 与库内行数天然差 10 条 → 验收 C6「端点↔DB 自洽」判 FAIL、三维卡片
    # 与库对不上。差异必须**显式说明**, 不能让人以为是端点漏数据。
    with db_cursor() as conn:
        _params2: list[Any] = [_cutoff_str]
        _asset2 = ""
        if asset:
            _asset2 = " AND asset=?"
            _params2.append(asset.upper())
        exc_rows = conn.execute(
            f"SELECT dimension, COUNT(*) AS cnt FROM trade_reflections_v2 "
            f"WHERE {_ts_expr} >= ? AND ABS(COALESCE(actual_outcome_pct,0)) > 60 {_asset2} "
            f"GROUP BY dimension",
            _params2,
        ).fetchall()
    sanity_excluded = {"total": 0, "by_dimension": {}}
    for r in exc_rows:
        sanity_excluded["total"] += int(r["cnt"])
        sanity_excluded["by_dimension"][str(r["dimension"])] = int(r["cnt"])

    shadow_4h = None
    try:
        _s_asset = asset.upper() if asset else ""
        _s_where = "WHERE verdict IN ('direction_ok','direction_wrong')"
        _s_args: list[Any] = []
        if _s_asset:
            _s_where += " AND asset=?"
            _s_args.append(_s_asset)
        with db_cursor() as _s_conn:
            _s_tot = _s_conn.execute(
                f"SELECT COUNT(*) AS n, COALESCE(SUM(reflection_effective),0) AS ok "
                f"FROM shadow_reflections {_s_where}", _s_args).fetchone()
            _s_asset_rows = _s_conn.execute(
                f"SELECT asset, COUNT(*) AS n, COALESCE(SUM(reflection_effective),0) AS ok "
                f"FROM shadow_reflections {_s_where} GROUP BY asset ORDER BY n DESC",
                _s_args).fetchall()
            _s_24h = _s_conn.execute(
                "SELECT COUNT(*) FROM shadow_reflections"
                " WHERE created_at >= datetime('now','-1 day')").fetchone()[0]
            _s_v24 = _s_conn.execute(
                "SELECT COALESCE(NULLIF(verdict_24h,''),'pending') AS v, COUNT(*) AS n"
                "  FROM shadow_reflections GROUP BY 1").fetchall()
            _s_flip = _s_conn.execute(
                "SELECT COUNT(*) FROM shadow_reflections"
                " WHERE verdict='direction_wrong' AND verdict_24h='direction_ok'").fetchone()[0]
        _n = int(_s_tot["n"] or 0)
        _ok = int(_s_tot["ok"] or 0)
        _v24 = {str(r["v"]): int(r["n"]) for r in _s_v24}
        _n24 = _v24.get("direction_ok", 0) + _v24.get("direction_wrong", 0)
        shadow_4h = {
            "rows": _n,
            "ok": _ok,
            "wrong": _n - _ok,
            "hit_rate": round(_ok / _n, 3) if _n else None,
            "rows_24h": int(_s_24h or 0),
            "conclusive": _n >= 60,
            "note": "4h 真值结算的方向命中；n<60 只记录不结论" if _n < 60 else "样本达标",
            # v416: 24h 二次判决 —— 4h 判错但 24h 判对 = 窗口太短，不是判断错
            "horizon_24h": {
                "settled": _n24,
                "ok": _v24.get("direction_ok", 0),
                "wrong": _v24.get("direction_wrong", 0),
                "insufficient": _v24.get("insufficient", 0) + _v24.get("pending", 0),
                "hit_rate": round(_v24.get("direction_ok", 0) / _n24, 3) if _n24 else None,
                "flipped_from_4h_wrong": int(_s_flip or 0),
            },
            "by_asset": [
                {"asset": str(r["asset"]), "n": int(r["n"] or 0), "ok": int(r["ok"] or 0),
                 "hit_rate": round(int(r["ok"] or 0) / int(r["n"]), 3) if int(r["n"] or 0) else None}
                for r in _s_asset_rows
            ],
        }
    except Exception as _s_exc:
        shadow_4h = {"error": f"{type(_s_exc).__name__}: {_s_exc}"}

    return {
        "ok": True,
        "days": days,
        "asset_filter": asset.upper() if asset else None,
        "sanity_excluded": sanity_excluded,
        "totals": {
            "n_reflections": total,
            "n_settled": sum(int(r["count"] * r["settled_rate"]) for r in all_subs),
            # overall 只含 A/B（预测类）；C 维的 hit 是"错因成立"，语义相反，
            # 混进来会把命中率算成 3% 这种无意义数字。
            "overall_hit_rate": round(overall_hit_rate, 3),
            "overall_hit_scope": "A,B（预测命中口径）",
            "overall_avg_outcome_pct": round(overall_avg_out, 2),
        },
        # v343: 分维度命中率 + 语义说明，避免再被误读
        "hit_rate_by_dimension": dim_hit_rate,
        # v343: 不可判的记录（单次抓取失败流水，没有断流窗口）单独暴露出来
        # v343: 行数 ≠ 事件数。同一时点被写多行会把"条数"放大 2~2.5 倍，
        # 组内 hit 还可能矛盾，这里一并报出。
        "data_quality": {
            "n_rows": int(_dq_rows["n_rows"] or 0),
            "n_unique_points": int(_dq_rows["n_pts"] or 0),
            "duplicate_ratio": round((_dq_rows["n_rows"] - _dq_rows["n_pts"]) / _dq_rows["n_rows"], 3)
            if _dq_rows["n_rows"] else 0,
            "conflicting_points": int(_conflict or 0),
            "note": "同一 asset+created_at 多行视为同一事件的重复写入；行数不等于事件数",
        },
        # v343: 事件点口径（同一 asset+时间只算 1 个事件、组内矛盾判不可判）
        "point_level": {
            "n_points": _pt_total,
            "measurable_points": _pt_meas,
            "conflict_points": _pt_conf,
            "hit_rate": round(_pt_hits / _pt_meas, 3) if _pt_meas else 0,
            "scope": "A,B；同一 asset+created_at 视为一个事件，组内 hit 矛盾则不判",
        },
        "unmeasurable_count": unmeasurable,
        "unmeasurable_note": "data_feed_timeout 是单次抓取失败流水，无断流窗口、hit 恒为 0，已排除出命中率分母",
        "hit_semantics": {
            "A": "hit=1 表示判断被市场证实（越高越好）",
            "B": "hit=1 表示判断被市场证实（越高越好）",
            "C": "hit=1 表示错因被证实（断流/迟到真的造成错过，越高越糟）",
        },
        "by_dimension": {k: v for k, v in by_dim.items()},
        # v310 P0: by_dimension 的值是**按 asset×subcategory 拆开的明细列表**,
        # 不是"该维度的行数"。调用方若拿 len(list) 当计数, 会得到"分组数"
        # (实测 A/B/C = 4/9/8), 与库里的真实行数 (1613/357/163) 差两个数量级,
        # 验收 C6 "端点↔库自洽" 因此假失败。这里给出**无歧义的聚合计数**。
        "by_dimension_counts": {
            dim: sum(int(r["count"]) for r in by_dim.get(dim, []))
            for dim in ("A", "B", "C")
        },
        "top_3_worst": worst_3,
        "top_3_best": best_3,
        "shadow_4h": shadow_4h,   # v414: 影子层真值（n<60 不下结论）
        "data_source": "trade_reflections_v2",
        "ts": int(time.time()),
    }


def analysis_error_taxonomy(hours: Annotated[int, Query(ge=1, le=8760)] = 168):
    import time as _t
    since_epoch = int(_t.time()) - hours * 3600
    out = {
        "ok": True,
        "window_hours": hours,
        "classes": [],
        "data_source": "trade_reflections_v2 / event_move_attribution(_rejected) / feed_health",
    }

    # ── 1. 买卖信号判断 ─────────────────────────────
    cls1 = {"key": "signal", "label": "买卖信号判断", "subs": [], "samples": []}
    with db_cursor() as conn:
        tot = conn.execute(
            "SELECT COUNT(*) FROM trade_reflections_v2 "
            "WHERE hit IS NOT NULL AND UPPER(action) IN ('BUY','SELL')").fetchone()[0]
        bad = conn.execute(
            "SELECT COUNT(*) FROM trade_reflections_v2 "
            "WHERE hit=0 AND UPPER(action) IN ('BUY','SELL')").fetchone()[0]
        pairs = conn.execute(
            "SELECT UPPER(action), hit, COUNT(*) FROM trade_reflections_v2 "
            "WHERE hit IS NOT NULL AND UPPER(action) IN ('BUY','SELL') "
            "GROUP BY 1,2").fetchall()
        small = conn.execute(
            "SELECT COUNT(*) FROM trade_reflections_v2 "
            "WHERE hit=0 AND UPPER(action) IN ('BUY','SELL') "
            "AND ABS(COALESCE(pnl_pct,0)) < 1.0").fetchone()[0]
        big = conn.execute(
            "SELECT COUNT(*) FROM trade_reflections_v2 "
            "WHERE hit=0 AND UPPER(action) IN ('BUY','SELL') "
            "AND ABS(COALESCE(pnl_pct,0)) >= 1.0").fetchone()[0]
        samples = conn.execute(
            "SELECT asset, UPPER(action), ROUND(COALESCE(pnl_pct,0),2), "
            "       COALESCE(settled_at,''), COALESCE(error_layer,'') "
            "FROM trade_reflections_v2 "
            "WHERE hit=0 AND UPPER(action) IN ('BUY','SELL') "
            "ORDER BY id DESC LIMIT 5").fetchall()

    buy_bad = sum(n for a, h, n in pairs if a == "BUY" and h == 0)
    sell_bad = sum(n for a, h, n in pairs if a == "SELL" and h == 0)
    cls1["total"] = int(tot)
    cls1["bad"] = int(bad)
    cls1["rate"] = round(bad / tot * 100, 1) if tot else 0.0
    cls1["subs"] = [
        {"label": "追多被打回（BUY 后下跌）", "n": int(buy_bad)},
        {"label": "追空被打回（SELL 后上涨）", "n": int(sell_bad)},
        {"label": "幅度不足（错但没走远 <1%）", "n": int(small)},
        {"label": "明显反向（错且跌/涨 ≥1%）", "n": int(big)},
    ]
    cls1["samples"] = [
        {"asset": a, "action": ac, "pnl_pct": p, "settled_at": st, "error_layer": el}
        for a, ac, p, st, el in samples
    ]
    out["classes"].append(cls1)

    # ── 2. 方向框架（事件→方向 推理链不成立）───────
    cls2 = {"key": "direction", "label": "信息分析方向错误", "subs": [], "samples": []}
    with db_cursor() as conn:
        confirmed = conn.execute("SELECT COUNT(*) FROM event_move_attribution").fetchone()[0]
        rejected = conn.execute(
            "SELECT COUNT(*) FROM event_move_attribution_rejected "
            "WHERE COALESCE(reject_reason,'') LIKE '%不成立%'").fetchone()[0]
        orphan = conn.execute(
            "SELECT COUNT(*) FROM event_move_attribution_rejected "
            "WHERE reject_reason='orphan_move'").fetchone()[0]
        echo = conn.execute(
            "SELECT COUNT(*) FROM event_move_attribution_rejected "
            "WHERE reject_reason='price_echo'").fetchone()[0]
        weak = conn.execute(
            "SELECT COUNT(*) FROM event_move_attribution WHERE algo_score < 0.5").fetchone()[0]
        strong = conn.execute(
            "SELECT COUNT(*) FROM event_move_attribution WHERE algo_score >= 0.8").fetchone()[0]
        rej_samples = conn.execute(
            "SELECT asset, ROUND(COALESCE(move_pct,0),2), COALESCE(ev_action,''), "
            "       SUBSTR(COALESCE(reject_reason,''),1,80), COALESCE(ev_source,'') "
            "FROM event_move_attribution_rejected "
            "WHERE COALESCE(reject_reason,'') LIKE '%不成立%' "
            "ORDER BY id DESC LIMIT 5").fetchall()

    base2 = confirmed + rejected
    cls2["total"] = int(base2)
    cls2["bad"] = int(rejected)
    cls2["rate"] = round(rejected / base2 * 100, 1) if base2 else 0.0
    cls2["subs"] = [
        {"label": "判定“事件与异动无因果关系”", "n": int(rejected)},
        {"label": "弱因果（评分 <0.5，方向勉强）", "n": int(weak)},
        {"label": "强因果（评分 ≥0.8，可进链）", "n": int(strong)},
        {"label": "异动找不到对应新闻（孤儿）", "n": int(orphan)},
        {"label": "自指行情播报（把行情当新闻）", "n": int(echo)},
    ]
    cls2["samples"] = [
        {"asset": a, "move_pct": p, "ev_action": act, "reason": rs, "ev_source": src}
        for a, p, act, rs, src in rej_samples
    ]
    out["classes"].append(cls2)

    # ── 3. 技术故障（数据缺失 / 信号慢）─────────────
    cls3 = {"key": "technical", "label": "技术错误", "subs": [], "samples": []}
    since_str = time_utils.utc_str(since_epoch)
    _fh_ts = time_utils.sql_utc_ts_expr("ts")
    OOS_FILTER = (" AND COALESCE(asset,'') NOT IN ('','NONE')"
                      " AND asset NOT LIKE '%.KS' AND asset NOT LIKE '%.KQ'"
                      " AND asset NOT LIKE '%.SS' AND asset NOT LIKE '%.SZ'"
                  " AND asset NOT LIKE '%HYNIX%'"
                      # yahoo/yfinance 在本机网络恒不可达(SSL 必断), 已改用腾讯/新浪取数,
                      # 历史失败行属"源不可用"而非"当前故障", 不计入技术错误
                      # asia/equity/tiingo/coinglass_lsr 已从取价链删除(不在交易范围或需 key),
                      # 只剩历史行; 同样不计入"当前故障", 否则看板永远清不干净
                      " AND lower(COALESCE(source,'')) NOT IN "
                      "('yahoo','yfinance','asia','equity','tiingo','coinglass_lsr')")
    with db_cursor() as conn:
        failed = conn.execute(
            "SELECT COUNT(*) FROM feed_health "
            f"WHERE {_fh_ts} >= ? AND status='failed'" + OOS_FILTER, (since_str,)).fetchone()[0]
        slow = conn.execute(
            "SELECT COUNT(*) FROM feed_health "
            f"WHERE {_fh_ts} >= ? AND COALESCE(latency_ms,0) >= 3000" + OOS_FILTER, (since_str,)).fetchone()[0]
        by_src = conn.execute(
            "SELECT source, COUNT(*) FROM feed_health "
            f"WHERE {_fh_ts} >= ? AND status='failed'" + OOS_FILTER +
            " GROUP BY 1 ORDER BY 2 DESC LIMIT 5", (since_str,)).fetchall()
        stale_bars = conn.execute(
            "SELECT asset, MAX(ts) FROM ohlcv_daily WHERE COALESCE(asset,'') NOT IN ('','NONE') "
            "AND asset NOT LIKE '%.KS' AND asset NOT LIKE '%.KQ' AND asset NOT LIKE '%.SS' "
            "AND asset NOT LIKE '%.SZ' AND asset NOT LIKE '%HYNIX%' "
            "GROUP BY asset HAVING MAX(ts) < ? ORDER BY 2 ASC LIMIT 5",
            (int(_t.time()) - 3 * 86400,)).fetchall()
        tech_samples = conn.execute(
            "SELECT source, asset, feed_kind, COALESCE(latency_ms,0), SUBSTR(COALESCE(error,''),1,60) "
            "FROM feed_health "
            f"WHERE {_fh_ts} >= ? AND status='failed'" + OOS_FILTER +
            " ORDER BY id DESC LIMIT 5", (since_str,)).fetchall()

    cls3["total"] = int(failed) + int(slow)
    cls3["bad"] = int(failed)
    cls3["rate"] = None
    cls3["subs"] = [
        {"label": f"{src} 抓取失败", "n": int(n)} for src, n in by_src
    ]
    cls3["subs"].append({"label": "接口慢（响应 ≥3s，是“信号慢”的来源）", "n": int(slow)})
    cls3["subs"].append({"label": "yahoo/yfinance：本机网络不可达，已改用腾讯/新浪（不计故障）", "n": 0})
    for a, ts in stale_bars:
        days = round((_t.time() - ts) / 86400, 1)
        cls3["subs"].append({"label": f"{a} 日线已 {days} 天没更新", "n": 1})
    cls3["samples"] = [
        {"source": s_, "asset": a_, "feed_kind": fk, "latency_ms": lat, "error": err}
        for s_, a_, fk, lat, err in tech_samples
    ]
    out["classes"].append(cls3)
    return out


# ──────────────────────────────────────────────────────────────────────
# 影响评估 / 宏观日历 / 数据源健康（2026-11-18 第二批下沉）
# ──────────────────────────────────────────────────────────────────────

_IMPACT_SEV_RANK = {"low": 1, "medium": 2, "high": 3, "critical": 4}


_IMPACT_SOURCE_NOTE = {
    "event_kline_impact": (
        "event_kline_impact: 事件→K线影响 (worker 已算好 event_title/severity/direction); "
        "impact_pct 已统一成有符号百分数"
    ),
    "event_move_attribution": (
        "该资产 event_kline_impact 无数据, 已降级到 event_move_attribution "
        "(事件→涨跌, 含 ev_action/ev_source/news_event_id 事件链)"
    ),
    "critical_price_moves (fallback)": (
        "该资产没有事件归因数据, 已降级到 critical_price_moves (纯价格异动, 无新闻事件链)"
    ),
}


_MOVE_KIND_CN = {
    "surge": "急拉", "spike": "急拉", "spike_up": "急拉", "surge_up": "急拉",
    "plunge": "急跌", "crash": "急跌", "spike_down": "急跌", "plunge_down": "急跌",
    "slow_rise": "缓涨", "gradual_rise": "缓涨", "drift_up": "缓涨",
    "slow_drop": "缓跌", "gradual_drop": "缓跌", "drift_down": "缓跌",
}


_MOVE_SEV_RANK_SQL = (
    "CASE WHEN ABS(COALESCE(move_pct,0)) >= 7 THEN 4"
    " WHEN ABS(COALESCE(move_pct,0)) >= 5 THEN 3"
    " WHEN ABS(COALESCE(move_pct,0)) >= 3 THEN 2 ELSE 1 END"
)


def _impact_rows_eki(
    conn,
    asset: str,
    frm: int,
    to: int,
    severity_min: str,
    min_abs: float,
    limit: int,
    order: str = "time",
) -> list:
    """event_kline_impact → 前端 ImpactRow 契约 (第一优先数据源)。

    · impact_pct: 该表是**小数**且 ≥ 0 (max(上冲, 下探) 的幅度), 方向另存 direction 列
      → 这里按 direction 补符号: up → +mag, down → −mag, neutral → 0 (幅度存 impact_abs_pct)。
    · severity: 直接用 worker 写好的 impact_severity (low/medium/high)。
    """
    out: list = []
    # ⚠️ severity 门槛必须下推到 SQL: 如果先 LIMIT 再在 Python 里过滤 severity,
    #    最新但全是 low 的行会把 limit 吃掉, 更早的高严重度行永远轮不到 (v319 修复)。
    sev_min_rank = _IMPACT_SEV_RANK.get((severity_min or "low").lower(), 1)
    sev_rank_sql = (
        "CASE LOWER(COALESCE(impact_severity,'low'))"
        " WHEN 'critical' THEN 4 WHEN 'high' THEN 3 WHEN 'medium' THEN 2 ELSE 1 END"
    )
    sql = (
        "SELECT id, event_source, event_id, event_title, event_time, asset, "
        "       impact_pct, impact_severity, direction, importance "
        "FROM event_kline_impact "
        "WHERE UPPER(asset)=? AND event_time>=? AND event_time<=? "
    )
    params: list = [asset, frm, to]
    if min_abs > 0:
        # impact_pct 是小数 → 乘 100 换成百分数再比
        sql += "AND ABS(COALESCE(impact_pct,0))*100 >= ? "
        params.append(min_abs)
    if sev_min_rank > 1:
        sql += f"AND {sev_rank_sql} >= ? "
        params.append(sev_min_rank)
    # order=magnitude → 幅度最大的优先 (归因页要"这段时间里最大的几次冲击";
    # 只按时间排序 + LIMIT 会把窗口另一端挤掉 → 30d 窗口只剩最近 1~2 天有 marker)
    if (order or "time").strip().lower() == "magnitude":
        sql += "ORDER BY ABS(COALESCE(impact_pct,0)) DESC, event_time DESC LIMIT ?"
    else:
        sql += "ORDER BY event_time DESC LIMIT ?"
    params.append(limit)
    try:
        rows = conn.execute(sql, params).fetchall()
    except Exception as exc:  # 老库没有这张表 → 交给下一级兜底
        print(f"[bridge] event_kline_impact 查询失败: {exc}")
        return out
    for r in rows:
        (rid, ev_src, ev_id, title, ts, _asset, pct_frac, sev, direction, importance) = r
        mag = abs(float(pct_frac or 0.0)) * 100.0
        d = (direction or "neutral").lower()
        if d == "up":
            signed = mag
        elif d == "down":
            signed = -mag
        else:
            signed = 0.0
            d = "flat"
        sev = (sev or "low").lower()
        if not _impact_sev_ok(sev, severity_min):
            continue
        ttl = (title or "").strip()
        if not ttl:
            ttl = f"事件异动 {signed:+.2f}%" if signed else f"事件异动 ±{mag:.2f}%"
        out.append({
            "event_id": f"eki-{rid}",
            "event_source": ev_src or "event_kline_impact",
            "event_action": "UNKNOWN",
            "event_news_id": ev_id,
            "event_score": None,
            "event_time": int(ts or 0),
            "asset": asset,
            "move_pct": signed,
            "move_kind": "event_impact",
            "algo_score": None,
            "duration_min": None,
            "severity": sev,
            # ── 前端 ImpactRow 契约字段 ──
            "event_title": ttl,
            "impact_severity": sev,
            "impact_pct": signed,        # 有符号百分数
            "impact_abs_pct": mag,       # 幅度 (百分数)
            "impact_pct_raw": float(pct_frac or 0.0),  # worker 原始小数 (未 ×100)
            "direction": "UNKNOWN",      # 事件方向(利好/利空)未知, 真实方向看 move_direction
            # ── v318 降噪字段 ──
            "move_direction": d,         # 真实涨跌方向 (涨绿跌红)
            "event_backed": True,        # 来自 news_events → 有真实新闻事件链
            "importance": importance,
        })
    return out


def _impact_sev_ok(sev: str | None, severity_min: str | None) -> bool:
    """severity 门槛比较 (low < medium < high < critical)。"""
    return _IMPACT_SEV_RANK.get((sev or "low").lower(), 1) >= _IMPACT_SEV_RANK.get(
        (severity_min or "low").lower(), 1
    )


def _impact_title(kind: str | None, pct: float | None, action: str | None) -> str:
    """v303 R10: 前端 MoversPanel L1814 `titleText(it.event_title)`, 空则 continue。
    之前 bridge 只给 move_kind/event_action, 没有 event_title
    → 151 条 impact marker 全被跳过。这里按 kind+涨跌幅合成可读标题。"""
    base = _MOVE_KIND_CN.get((kind or "").lower())
    if not base:
        base = {
            "BUY": "利好拉升", "SELL": "利空下跌",
            "UNKNOWN": "价格异动", "HOLD": "价格异动",
        }.get((action or "UNKNOWN").upper(), "价格异动")
    if pct is None:
        return base
    return f"{base} {pct:+.1f}%"


def _move_direction(pct: float | None) -> str:
    """按**实际涨跌幅**给出方向 (up/down/flat)。

    v318: 之前前端把 impact marker 一律涂成红色 (#ef4444), 涨的跌的都红 →
    用户看到"满屏红点"。方向必须由 move_pct 的符号决定, 不能由 ev_action 决定
    (ev_action 是"事件利好/利空", 与价格方向常常相反 —— 利空消息也可能被
    资金买在低点)。
    """
    if pct is None:
        return "flat"
    if pct > 0:
        return "up"
    if pct < 0:
        return "down"
    return "flat"


def _row_matches_asset(r, asset_u: str) -> bool:
    """news_events 行是否属于该品种。

    v310: 复数 `assets` 列只是历史列（且格式可能是门卫 JSON），
    权威品种在**单数** `asset` 列 —— 两个都认，否则只标了单数列的
    新闻会被整条丢掉。
    """
    if asset_u in _parse_asset_list(r["assets"]):
        return True
    try:
        return str(r["asset"] or "").strip().upper() == asset_u
    except (IndexError, KeyError):
        return False


def _star_by_percentile(scores: list[float], val: float) -> int:
    """v303 R10: 按分位数排名给 1-5 星。

    之前用 `int(score*5)+1` 绝对阈值,但 news_events.score 实际分布是
    min=0.0 / avg=0.183 / max=1.0 —— 绝大多数 score < 0.4 → 恒等于 1 星,
    前端 star_min=3 直接筛空 (0 条)。

    改成"在窗口内的相对排名": 分数越靠近顶部星级越高。
    ★5 = top 5%, ★4 = top 20%, ★3 = top 40%, ★2 = top 70%, 其余 ★1。
    这样 star_min=3 稳定取到 top 40%, 与 score 的绝对量纲无关。
    """
    if not scores:
        return 1
    v = abs(val)
    below = sum(1 for s in scores if abs(s) < v)
    pct = below / len(scores)          # 0=最高分, 1=最低分
    if pct < 0.05:
        return 5
    if pct < 0.20:
        return 4
    if pct < 0.40:
        return 3
    if pct < 0.70:
        return 2
    return 1


def _utc_text_to_epoch(value: Any) -> int:
    """UTC 语义的时间文本 → epoch 秒 (解析失败返 0)。

    v309 P1: 用于 feed_health.ts —— 该列由 feed_probe 写的是 UTC 墙钟。
    旧代码用 datetime.strptime(...).timestamp(), strptime 结果无时区,
    .timestamp() 按**本机本地时区**解释 → 北京机器上整体偏 8 小时。
    """
    try:
        return time_utils.utc_str_to_epoch(value) or 0
    except Exception:
        return 0


def impact_by_asset(
    asset: str,
    from_ts: int | None = None,
    to_ts: int | None = None,
    severity_min: str = "low",
    min_abs_pct: float = 0.0,
    order: str = "time",   # time (最新优先) | magnitude (幅度最大优先)
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
):
    """真实数据 (v319): event_kline_impact → event_move_attribution → critical_price_moves。

    v302+ P2 优先 event_move_attribution (事件→涨跌, 带 ev_action/ev_source/ev_news_id 完整事件链),
    critical_moves 仅 K 线。v319 起把 **event_kline_impact 提到第一优先** ——
    主资产 (XAU/BTC/WTI) 的真实事件影响只存在这张表里, 之前端点对 XAU 恒返回空。

    v318: 新增 min_abs_pct (最小幅度阈值, 默认 0 = 不过滤, 保持向后兼容)
    与 move_direction (按价格涨跌幅给的真实涨跌方向) / event_backed
    (该条是否真有事件来源) —— 前端据此降噪, 不再"满屏红点"。

    出口单位约定: impact_pct = **有符号百分数** (正=涨), impact_abs_pct = 幅度百分数。
    """
    frm = from_ts or (int(time.time()) - 30 * 86400)
    to = to_ts or int(time.time())
    try:
        min_abs = abs(float(min_abs_pct or 0.0))
    except (TypeError, ValueError):
        min_abs = 0.0
    asset_up = (asset or "").strip().upper()
    out: list = []
    real_source_used = "none"
    # severity 门槛下推到 SQL (别让 LIMIT 先吃满再在 Python 里过滤)
    sev_min_rank = _IMPACT_SEV_RANK.get((severity_min or "low").lower(), 1)
    with db_cursor() as conn:
        # 第一优先: event_kline_impact (主资产真实事件影响 + 已算好的标题/严重度/方向)
        out = _impact_rows_eki(conn, asset_up, frm, to, severity_min, min_abs, limit, order)
        if out:
            real_source_used = "event_kline_impact"
        else:
            # 第二优先: event_move_attribution (事件→资产影响,带 ev_action/ev_source/news_event_id)
            # v308 P1-fix3: 该表的 move_pct 与 critical_price_moves 同源同量纲, 同样要
            # 剔除量纲污染行 —— 否则 9867% 的假异动会以"critical" severity 出现在影响面板。
            rows = conn.execute(
                "SELECT id, move_ts, move_pct, move_kind, algo_score, duration_min, "
                "       ev_news_id, ev_source, ev_action, ev_score "
                "FROM event_move_attribution "
                "WHERE asset=? AND move_ts>=? AND move_ts<=? "
                "AND ABS(COALESCE(move_pct,0)) <= ? "
                + ("AND ABS(COALESCE(move_pct,0)) >= ? " if min_abs > 0 else "")
                + (f"AND {_MOVE_SEV_RANK_SQL} >= ? " if sev_min_rank > 1 else "")
                + ("ORDER BY ABS(COALESCE(move_pct,0)) DESC, move_ts DESC LIMIT ?"
                   if (order or "time").strip().lower() == "magnitude"
                   else "ORDER BY move_ts DESC LIMIT ?"),
                ((asset, frm, to, MAX_PLAUSIBLE_MOVE_PCT, min_abs, sev_min_rank, limit)
                 if min_abs > 0 and sev_min_rank > 1 else
                 (asset, frm, to, MAX_PLAUSIBLE_MOVE_PCT, sev_min_rank, limit)
                 if sev_min_rank > 1 else
                 (asset, frm, to, MAX_PLAUSIBLE_MOVE_PCT, min_abs, limit)
                 if min_abs > 0 else
                 (asset, frm, to, MAX_PLAUSIBLE_MOVE_PCT, limit)),
            ).fetchall()
            for r in rows:
                (rid, ts, pct, kind, score, dur,
                 ev_news_id, ev_source, ev_action, ev_score) = r
                a = abs(pct)
                if a >= 7: sev = "critical"
                elif a >= 5: sev = "high"
                elif a >= 3: sev = "medium"
                else: sev = "low"
                if not _impact_sev_ok(sev, severity_min): continue
                out.append({
                    "event_id": f"ema-{rid}", "event_source": ev_source or "unknown",
                    "event_action": ev_action or "UNKNOWN",
                    "event_news_id": ev_news_id,
                    "event_score": ev_score,
                    "event_time": ts, "asset": asset,
                    "move_pct": pct, "move_kind": kind, "algo_score": score,
                    "duration_min": dur, "severity": sev,
                    # ── 前端 ImpactRow 契约字段 (v303 R10) ──
                    # MoversPanel 读 it.event_title / it.impact_severity
                    "event_title": _impact_title(kind, pct, ev_action),
                    "impact_severity": sev,
                    "impact_pct": pct,            # move_pct 本身就是有符号百分数
                    "impact_abs_pct": a,          # 幅度 (百分数)
                    "direction": ev_action or "UNKNOWN",
                    # ── v318 降噪字段 ──
                    "move_direction": _move_direction(pct),   # 真实涨跌方向 (涨绿跌红)
                    "event_backed": bool(ev_news_id),           # 是否真有新闻事件来源
                })
            if out:
                real_source_used = "event_move_attribution"
        # 第三兜底: critical_price_moves (纯价格异动, 前两级都没数据时)
        if not out:
            real_source_used = "critical_price_moves (fallback)"
            rows2 = conn.execute(
                "SELECT id, ts, move_pct, move_kind, algo_score, duration_min "
                "FROM critical_price_moves "
                "WHERE asset=? AND ts>=? AND ts<=? "
                "AND ABS(COALESCE(move_pct,0)) <= ? "
                + ("AND ABS(COALESCE(move_pct,0)) >= ? " if min_abs > 0 else "")
                + (f"AND {_MOVE_SEV_RANK_SQL} >= ? " if sev_min_rank > 1 else "")
                + ("ORDER BY ABS(COALESCE(move_pct,0)) DESC, ts DESC LIMIT ?"
                   if (order or "time").strip().lower() == "magnitude"
                   else "ORDER BY ts DESC LIMIT ?"),
                ((asset, frm, to, MAX_PLAUSIBLE_MOVE_PCT, min_abs, sev_min_rank, limit)
                 if min_abs > 0 and sev_min_rank > 1 else
                 (asset, frm, to, MAX_PLAUSIBLE_MOVE_PCT, sev_min_rank, limit)
                 if sev_min_rank > 1 else
                 (asset, frm, to, MAX_PLAUSIBLE_MOVE_PCT, min_abs, limit)
                 if min_abs > 0 else
                 (asset, frm, to, MAX_PLAUSIBLE_MOVE_PCT, limit)),
            ).fetchall()
            for r in rows2:
                rid, ts, pct, kind, score, dur = r
                a = abs(pct)
                if a >= 7: sev = "critical"
                elif a >= 5: sev = "high"
                elif a >= 3: sev = "medium"
                else: sev = "low"
                if not _impact_sev_ok(sev, severity_min): continue
                out.append({
                    "event_id": f"cpm-{rid}", "event_source": "critical_price_moves",
                    "event_time": ts, "asset": asset,
                    "move_pct": pct, "move_kind": kind, "algo_score": score,
                    "duration_min": dur, "severity": sev,
                    # 前端 ImpactRow 契约字段, 同上
                    "event_title": _impact_title(kind, pct, None),
                    "impact_severity": sev,
                    "impact_pct": pct,            # move_pct 本身就是有符号百分数
                    "impact_abs_pct": a,          # 幅度 (百分数)
                    "direction": "UNKNOWN",
                    # ── v318 降噪字段 ──
                    "move_direction": _move_direction(pct),
                    # critical_price_moves **没有**新闻事件来源 → 纯价格异动,
                    # 前端可据此降级(不标成"消息面驱动")
                    "event_backed": False,
                })
            if not out:
                # 三张表都没数据 → 诚实报空 (不伪造 marker, 也不谎报数据源)
                real_source_used = "none"
    return {
        "ok": True,
        "items": out,
        "n": len(out),
        "asset": asset_up,
        "source": real_source_used,
        "honest_note": _IMPACT_SOURCE_NOTE.get(
            real_source_used,
            "该资产在时间窗内没有任何影响记录 (三张表都没数据)",
        ),
    }


def sources_alerts(
    hours: Annotated[int, Query(ge=1, le=8760)] = 24,
    severity_min: str = "low",
):
    """真实数据: 从 feed_health 提告警
    schema: id, asset, feed_kind, source, status, latency_ms, value, error, ts(text)
    status: 'failed' / 'success'  (只有这两种值,5651 failed)
    """
    # feed_health.ts 是 **UTC** 墙钟 (feed_probe._now_iso 用 datetime.now(timezone.utc)),
    # 格式 'YYYY-MM-DD HH:MM:SS' —— 与 created_at 不同, 别混用口径。
    # v309 P1: 该列历史上有 'T' 分隔 (ISO) 和 ' ' 分隔两种格式混存 (见本文件
    # v237_by_dimension 里的解析分支), 直接字典序比会漏掉一部分行 →
    # 统一用 sql_utc_ts_expr() 归一化 (仍是 UTC 墙钟, cutoff 也用 utc_str)。
    since_str = time_utils.utc_str(int(time.time()) - hours * 3600)
    _fh_ts_expr = time_utils.sql_utc_ts_expr("ts")
    with db_cursor() as conn:
        rows = conn.execute(
            "SELECT id, source, asset, feed_kind, status, latency_ms, error, ts "
            "FROM feed_health "
            f"WHERE {_fh_ts_expr} >= ? AND status='failed' "
            "ORDER BY ts DESC LIMIT 200",
            (since_str,),
        ).fetchall()
    sev_rank = {"low": 0, "medium": 1, "high": 2, "critical": 3}
    threshold = sev_rank.get(severity_min, 0)
    out = []
    # v356: 账户只做 美元 / 黄金 / 加密 / 美股 —— 韩股(KOSPI/KOSDAQ, 如 SKHYNIX、
    #   000660.KS)与 A 股的取数失败不属于"信息源异常"，不该占顶部告警横幅。
    _OUT_OF_SCOPE = ('SKHYNIX', 'US_HYNIX', 'HYNIX')

    def _out_of_scope(asset: str) -> bool:
        a = (asset or '').upper()
        if not a:
            return False
        if a.endswith('.KS') or a.endswith('.KQ'):
            return True
        if a.endswith('.SS') or a.endswith('.SZ'):
            return True
        return any(k in a for k in _OUT_OF_SCOPE)
    for r in rows:
        fid, src, asset, fk, st, lat, err, ts_text = r
        if _out_of_scope(asset):
            continue
        # severity: failed + 高延迟 → high, failed + 普通 → medium
        if lat and lat >= 3000:
            sev = "high"
        else:
            sev = "medium"
        if sev_rank.get(sev, 0) < threshold:
            continue
        # 把 text datetime 转成 ts (epoch) 给前端
        # v309 P1: 原代码用 datetime.strptime(...).timestamp() —— strptime 得到的是
        # **无时区** datetime, .timestamp() 按**本机本地时区**解释, 而 feed_health.ts
        # 存的是 UTC → 告警时间整体偏 8 小时 (北京机器上)。
        # 现在显式按 UTC 解析。
        ts_epoch = _utc_text_to_epoch(ts_text)
        out.append({
            "id": fid, "source": src, "asset": asset, "feed_kind": fk,
            "status": st, "severity": sev,
            "latency_ms": lat, "ts": ts_epoch, "note": err or "",
        })
    # v342: 原来一条「源 × 品种」失败就返回一行 —— yfinance 一个源在 73 个品种上失败
    #   会变成 73 张一模一样的告警卡，前端就只能显示「197 个信息源异常」这种噪音。
    #   这里按 (source, feed_kind) 收敛成一条，带上失败品种数。
    grouped: dict[tuple[str, str], dict] = {}
    for it in out:
        key = (str(it["source"]), str(it["feed_kind"]))
        g = grouped.get(key)
        if g is None:
            grouped[key] = {
                "id": it["id"],
                "source": it["source"],
                "asset": it["asset"],
                "feed_kind": it["feed_kind"],
                "status": it["status"],
                "severity": it["severity"],
                "latency_ms": it["latency_ms"],
                "ts": it["ts"],
                "note": it["note"],
                "_assets": {it["asset"]},
                "_samples": [it["asset"]] if it["asset"] else [],
            }
        else:
            g["_assets"].add(it["asset"])
            if len(g["_samples"]) < 3 and it["asset"] not in g["_samples"]:
                g["_samples"].append(it["asset"])
            if (it["ts"] or 0) > (g["ts"] or 0):
                g["ts"] = it["ts"]
                g["note"] = it["note"] or g["note"]
                g["id"] = it["id"]
                g["latency_ms"] = it["latency_ms"]
    agg = []
    for g in grouped.values():
        n_assets = len(g.pop("_assets"))
        samples = g.pop("_samples")
        if n_assets > 1:
            g["asset"] = f"{n_assets} 个品种（{'/'.join(str(x) for x in samples[:3])}…）"
        g["assets_failed"] = n_assets
        agg.append(g)
    agg.sort(key=lambda x: x["ts"] or 0, reverse=True)
    return {
        "ok": True,
        "alerts": agg,
        "n": len(agg),
        "n_raw_failures": len(out),
        "data_source": "feed_health (real probe data, status='failed'; 按 源+通道 收敛)",
        "n_total_scanned": len(rows),
    }


def v189_calendar_events_for_asset(
    asset: str,
    hours: Annotated[int, Query(ge=1, le=8760)] = 720,
    star_min: int = 1,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
):
    """v189 资产日历事件 (前端 MoversPanel markers 消费).
    v302+ 实现: 从 news_events 查. 匹配策略 (按顺序):
      1. assets JSON 包含 asset (上游已解析)
      2. headline 文本含 asset 名 / 中文名 / 常见 ticker
         兜底: news_events.assets 字段经常空, 用 headline 模糊匹配保底
    """
    cutoff = int(time.time()) - hours * 3600
    asset_u = asset.upper()
    # 资产中英文别名 (前端选 BTC 时也能匹配 "比特币"/"bitcoin")
    alias_map = {
        "BTC": ["BTC", "BITCOIN", "比特币", "XBT"],
        "ETH": ["ETH", "ETHEREUM", "以太坊"],
        "SOL": ["SOL", "SOLANA"],
        "BNB": ["BNB", "BINANCE", "币安"],
        "ADA": ["ADA", "CARDANO", "艾达"],
        "DOGE": ["DOGE", "DOGECOIN", "狗狗币"],
        "XRP": ["XRP", "RIPPLE", "瑞波"],
        "MU": ["MU", "MICRON", "美光"],
        "NVDA": ["NVDA", "NVIDIA", "英伟达"],
        "TSLA": ["TSLA", "TESLA", "特斯拉"],
        "AAPL": ["AAPL", "APPLE", "苹果"],
        "SKHYNIX": ["SKHYNIX", "SK HYNIX", "海力士"],
        "WTI": ["WTI", "原油", "OIL"],
        "XAU": ["XAU", "GOLD", "黄金"],
    }
    aliases = alias_map.get(asset_u, [asset_u])
    # 1) assets 列匹配（复数 assets 列表 或 单数 asset 权威列）
    with db_cursor() as conn:
        rows = conn.execute(
            "SELECT id, source, headline, action, score, assets, asset, ts, fetched_at, news_id "
            "FROM news_events WHERE ts >= ? AND (UPPER(assets) LIKE ? OR UPPER(assets) LIKE ? "
            "                           OR UPPER(COALESCE(asset,'')) = ?) "
            "ORDER BY ts DESC LIMIT ?",
            (cutoff, f'%"{asset_u}"%', f"%,{asset_u}%", asset_u, limit * 4),
        ).fetchall()
    items = []
    seen_ids = set()
    # v303 R10: 先收集全窗口分数池, 再按分位数排名定星级
    score_pool: list[float] = []
    for r in rows:
        if not _row_matches_asset(r, asset_u):
            continue
        score_pool.append(abs(r["score"] or 0))
    for r in rows:
        if not _row_matches_asset(r, asset_u):
            continue
        score = abs(r["score"] or 0)
        star = _star_by_percentile(score_pool, score)
        if star < star_min:
            continue
        items.append({
            "id": r["id"], "news_id": r["news_id"], "asset": asset_u,
            "source": r["source"], "headline": r["headline"],
            "action": r["action"], "score": r["score"],
            "ts": r["ts"], "fetched_at": r["fetched_at"],
            "star": star, "match": "assets_json",
            # ── 前端 CalEvent 契约字段 (v303 R10) ──
            # MoversPanel L1775 读 ce.ts_bj / L1778 读 ce.title,
            # 之前只给 ts/headline → ts_bj=undefined → `ts_bj < t0-3600` 恒真 → 全被 continue
            "ts_bj": r["ts"],
            "title": r["headline"] or "",
            "actual": None, "consensus": None,
            "is_released": False, "surprise": "",
            "age_hours": round((int(time.time()) - r["ts"]) / 3600, 1) if r["ts"] else None,
        })
        seen_ids.add(r["id"])
        if len(items) >= limit:
            break
    # 2) headline 文本兜底
    if len(items) < limit:
        with db_cursor() as conn:
            sub = " OR ".join(["UPPER(headline) LIKE ?"] * len(aliases))
            params: list[Any] = [cutoff] + [f"%{a}%" for a in aliases] + [limit * 2]
            rows2 = conn.execute(
                f"SELECT id, source, headline, action, score, assets, ts, fetched_at, news_id "
                f"FROM news_events WHERE ts >= ? AND ({sub}) "
                f"ORDER BY ts DESC LIMIT ?",
                params,
            ).fetchall()
        for r in rows2:
            if r["id"] in seen_ids:
                continue
            score = abs(r["score"] or 0)
            score_pool.append(score)
        for r in rows2:
            if r["id"] in seen_ids:
                continue
            score = abs(r["score"] or 0)
            star = _star_by_percentile(score_pool, score)
            if star < star_min:
                continue
            items.append({
                "id": r["id"], "news_id": r["news_id"], "asset": asset_u,
                "source": r["source"], "headline": r["headline"],
                "action": r["action"], "score": r["score"],
                "ts": r["ts"], "fetched_at": r["fetched_at"],
                "star": star, "match": "headline_fallback",
                # 前端 CalEvent 契约字段 (v303 R10), 同上
                "ts_bj": r["ts"],
                "title": r["headline"] or "",
                "actual": None, "consensus": None,
                "is_released": False, "surprise": "",
                "age_hours": round((int(time.time()) - r["ts"]) / 3600, 1) if r["ts"] else None,
            })
            seen_ids.add(r["id"])
            if len(items) >= limit:
                break
    return {
        "ok": True,
        "asset": asset_u,
        "hours": hours,
        "star_min": star_min,
        "items": items,
        # v303 Q1 cont: 前端 MoversPanel 读 `events` (CalEvent[]), 不是 `items`
        # (之前只给 items → 前端 `calRes?.events ?? []` 恒空 → 日历 marker 永远 0)
        "events": items,
        "n": len(items),
        "count": len(items),
        "data_source": "news_events (v302+ assets_json + headline_fallback 双匹配)",
    }

