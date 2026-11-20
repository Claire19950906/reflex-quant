#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v188 LLM 真深度反思 worker。

旧 v186 反思 worker (paper_reflection_worker) 输出的只是模板填充的 (root_cause, lesson)。
本模块新增 LLM 二次反思:对 settled=1 的决策,调一次 LLM 做 5 句话级别的深度归因,
结果写入 trade_reflections_v2.llm_depth_reflection。

触发条件(只对「失败」反思深度调用, 节省 token):
  - is_correct = LOSS
  - depth_score >= 3 (有足够上下文)
  - forward_pnl < -0.005 (真实亏损)

LLM prompt 模板见 _DEPTH_PROMPT_TEMPLATE。

数据流:
  1. paper_reflection_worker._insert_one() INSERT 后,可选调用 enrich_with_llm()
  2. /api/v188/trade-reflections/{id}/reproduce 手动触发
  3. 后台 daemon: enrich_pending_loop() 每 5 分钟扫一次
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional

import db


_DEPTH_PROMPT_TEMPLATE = """\
你是资深交易复盘员。下面这条决策已经结算并触发了 v186 模板化反思(已写出 layer/sub_layer/signal_name),
请基于提供的事实(含决策后才发生的客观结果),用 5 句话给出更深入的具体归因。

【v232.5 硬约束 — 数字落地】用户反馈「反思系统反思不够深入」,核心原因就是
LLM 写出的"分析文"没有引用真实数字,看起来合理实则空话。下列规则不再可选:

HARD RULE 1: 每句话都必须至少 1 个量化锚点(数字/百分比/比率/相关性)。
            锚点示例:pnl=+0.0123 / mfe=0.8% mae=-0.5% / 近30条命中率63% /
                    机械反演净值≈-0.0199(已扣往返成本) / 与BTC相关r=+0.4 / 同layer盈亏比0.85 / ATR=1.2%
HARD RULE 2: 5 句话总共必须包含 ≥ 3 个不同类别的数字锚点(只引一个 pnl 不算)。
            类别:pnl量级 / 价格-百分比 / MFE-MAE / 历史统计 / 反事实-跨资产。
HARD RULE 3: 历史比较必须给出"样本量 N"和"本条 pnl 在历史分布中的位置
            (上四分位/中位/下四分位)",不可只说"表现偏差"。
HARD RULE 4: counterfactual_pnl_estimate 必须**直接引用**下面给的
            「机械反演净值」(已扣两次往返成本), 带符号、不许给区间。
            【禁止】自己把 forward_pnl 取负 —— forward_pnl 已经是扣费净值,
            取负等于把成本加回去一次, 会写出"反向就能赚"的假收益。
HARD RULE 5: prevention 必须包含 ≥ 1 个"可量化的下次判断标准",如
            「下次同 layer 信号若 event_strength < 0.5 → 跳过」。

要求:
1. 第 1 句:指出最关键的失败环节(2-3 个事实组合),必须引用「事后真实方向」判断决策时预期是否被证伪。
2. 第 2 句:对比同 layer 下的近 30 条历史决策,本条的特殊之处是什么(给样本量)。
3. 第 3 句:如果按反向 (BUY→SELL 或 SELL→BUY) 操作, 直接引用「机械反演净值」并说明"反向同样亏(成本吃掉)", 不得把它写成可赚的收益。
4. 第 4 句:此条是否值得升级为新经验? 如果是, 给出 1 句话 LESSON(可含量化标准)。
5. 第 5 句:下次如何避免(具体到参数/规则/prompt 修改 + 可量化阈值)。

# 输出格式(JSON)
{{
  "key_failure_chain": "...",
  "historical_comparison": "...",
  "counterfactual_pnl_estimate": "...",
  "lesson_candidate": "...",
  "prevention": "...",
  "evidence_score_self_assessment": <0-5>,
  "evidence_categories_used": ["pnl", "mfe_mae", "historical", "counterfactual", "cross_asset"],
  "mode": "LOSS"
}}

# 决策上下文
- decision_id: {decision_id}
- asset: {asset}
- action: {action}
- event_strength: {event_strength}
- event_type: {event_type}
- market_confirmation: {market_confirmation}
- forward_pnl: {forward_pnl}
- mfe_pct: {mfe_pct} mae_pct: {mae_pct}
- reasoning: {reasoning}

# 事后已验证的客观事实（决策后才发生，非决策时预期）
- 本品种决策后 24h 真实 K 线方向: {realized_move}
- 关联品种决策后 24h 真实方向: {posthoc_cross_asset}
- 往返成本: {cost_bps}bp (已从正/反向收益中扣除)
- 机械反演净值: {counterfactual_net} (反向操作扣两次往返成本后的净值, 只能引用不可自算)
- 同类历史反例(verified/failed/未验证): {similar_past_lessons}

# v186 模板化反思结果
- layer: {layer}
- sub_layer: {sub_layer}
- signal_name: {signal_name}
- root_cause: {root_cause}
- lesson: {lesson}
"""


_WIN_PROMPT_TEMPLATE = """\
你是资深交易复盘员。下面这条决策**已经赚了钱**并结算完毕, 也触发了 v186 模板化反思。
旧系统只反思亏损单, 导致经验池里全是"别做什么", 没有"该继续做什么"。
这次请你对**赢单**做 5 句归因, 目标是把"这次为什么对"变成可复用的正向经验。

【硬约束 — 数字落地 + 不许编】
HARD RULE 1: 每句至少 1 个量化锚点(pnl / mfe-mae / 历史胜率 / 跨资产相关性)。
HARD RULE 2: 必须区分「信号对」与「运气好」—— 用 luck 口径(如果给了)和历史同 layer 命中率说明。
HARD RULE 3: 不许把赢单写成"下次该反向做"; 也不许建议下调权重。
HARD RULE 4: 反事实只引用给出的「机械反演净值」, 自行取负会把成本算错。
HARD RULE 5: keep_rule 必须是可量化、可回放的判断标准(如「event_strength ≥ 0.7 且
            market_confirmation=bull → 保留 BUY 路径」)。

要求:
1. 第 1 句:这次盈利主要来自哪个环节(2-3 个事实组合), 引用事后真实方向证明不是偶然。
2. 第 2 句:同 layer 近 30 条的对比 —— 本条是"典型成功"还是"离群的好运"(给样本量)。
3. 第 3 句:如果当时反向做会怎样 —— 直接引用「机械反演净值」, 说明成本吃掉多少。
4. 第 4 句:这条经验是否值得进经验池(正向)? 给出 1 句 LESSON。
5. 第 5 句:什么条件下应当**继续**这样做(keep_rule, 带量化阈值), 什么条件下要警惕(失效条件)。

# 输出格式(JSON)
{{
  "key_success_chain": "...",
  "historical_comparison": "...",
  "counterfactual_pnl_estimate": "...",
  "lesson_candidate": "...",
  "keep_rule": "...",
  "invalidation_condition": "...",
  "evidence_score_self_assessment": <0-5>,
  "evidence_categories_used": ["pnl", "mfe_mae", "historical", "counterfactual", "cross_asset"],
  "mode": "WIN"
}}

# 决策上下文
- decision_id: {decision_id}
- asset: {asset}
- action: {action}
- event_strength: {event_strength}
- event_type: {event_type}
- market_confirmation: {market_confirmation}
- forward_pnl(净): {forward_pnl}
- mfe_pct: {mfe_pct} mae_pct: {mae_pct}
- reasoning: {reasoning}

# 事后已验证的客观事实
- 本品种决策后 24h 真实 K 线方向: {realized_move}
- 关联品种决策后 24h 真实方向: {posthoc_cross_asset}
- 往返成本: {cost_bps}bp (已从收益中扣除)
- 机械反演净值: {counterfactual_net} (反向操作扣两次往返成本后的净值)
- 同类历史对照: {similar_past_lessons}

# v186 模板化反思结果
- layer: {layer}
- sub_layer: {sub_layer}
- signal_name: {signal_name}
- root_cause: {root_cause}
- lesson: {lesson}
"""


def _call_llm_depth(prompt: str) -> Optional[Dict[str, Any]]:
    """调一次 LLM 返回 dict。失败 → None。

    优先 engine.ai_worker.chat (项目标准通道), 失败兜底 api_server.call_llm。
    """
    text = ""
    try:
        from engine.ai_worker import chat as _chat  # type: ignore
        text = _chat(prompt, purpose="reflection", call_site="trade_reflection_llm",
                     max_tokens=400, temperature=0.3) or ""
    except Exception as exc:
        print(f"[LLM-REFLECT] engine.ai_worker.chat 失败: {type(exc).__name__}: {exc}", flush=True)
    if not text:
        try:
            from api_server import call_llm as _call
            text = _call(prompt) or ""
        except Exception:
            pass
    if not text:
        return None
    try:
        import re
        m = re.search(r'\{.*\}', text, re.DOTALL)
        if m:
            return json.loads(m.group(0))
    except Exception:
        pass
    return {"raw_response": text[:1000]}


def _count_evidence_anchors(text: str) -> int:
    """与 paper_reflection_worker._count_evidence_anchors 同口径的数字锚点计数。

    本模块不直接 import worker(会循环依赖), 只复制这条最小口径:
    文本里出现多少个"带单位的数字/百分比"字段。
    """
    import re as _re
    if not text:
        return 0
    pats = (
        r"pnl\s*[=:]\s*[+-]?\d", r"mfe\s*[=:]", r"mae\s*[=:]",
        r"[+-]?\d+\.?\d*\s*%", r"hit_rate|命中率", r"样本量\s*n\s*=",
        r"r\s*=\s*[+-]?\d", r"ATR", r"反演|counterfactual",
    )
    return sum(1 for p in pats if _re.search(p, text, _re.I))


_CROSS_ASSET_MAP = {
    "XAU": ["WTI", "BTC"],
    "WTI": ["XAU", "BTC"],
    "BTC": ["ETH", "SOL"],
    "ETH": ["BTC", "SOL"],
    "SOL": ["BTC", "ETH"],
}
_POSTHOC_WINDOW_MS = 24 * 3600 * 1000  # 决策后 24h


def _fmt_direction(dir_info) -> str:
    if not dir_info or not dir_info.get("n"):
        return "(无数据)"
    return f"{dir_info.get('direction')} {dir_info.get('pct', 0):+.2f}% (n={dir_info.get('n')})"


def _collect_posthoc_evidence(conn, asset, decision_ts_ms):
    """收集决策后才发生的客观事实（非决策时预期），喂给深度反思。

    1. 本品种决策后 24h 真实 K 线方向（复用 insight_verifier，扁平/占位 K 线会被过滤）
    2. 关联品种（跨资产）决策后 24h 真实方向
    3. 同类历史反例（review_insights.counter_factual / verification_verdict）

    全部失败时返回诚实占位，绝不编造方向。asset/decision_ts 缺失 → 直接无数据。
    """
    import insight_verifier as _ivr
    out = {
        "realized_move": "(无数据)",
        "posthoc_cross_asset": "(无数据)",
        "similar_past_lessons": "(无)",
    }
    if not decision_ts_ms or not asset:
        return out
    asset = asset.upper()
    end_ts = decision_ts_ms + _POSTHOC_WINDOW_MS
    try:
        rows = _ivr._fetch_kline_window(conn, asset, decision_ts_ms, end_ts, "1h")
        out["realized_move"] = _fmt_direction(_ivr.compute_kline_direction(rows, asset))
    except Exception:
        pass
    cross_lines = []
    for c in _CROSS_ASSET_MAP.get(asset, []):
        try:
            rows = _ivr._fetch_kline_window(conn, c, decision_ts_ms, end_ts, "1h")
            d = _ivr.compute_kline_direction(rows, c)
            if d.get("n"):
                cross_lines.append(f"{c}: {_fmt_direction(d)}")
        except Exception:
            continue
    if cross_lines:
        out["posthoc_cross_asset"] = "; ".join(cross_lines)
    try:
        rows = conn.execute(
            "SELECT rule, asset, counter_factual, verification_verdict "
            "FROM review_insights "
            "WHERE (asset = ? OR scope = 'all') AND counter_factual != '' "
            "ORDER BY id DESC LIMIT 3",
            (asset,),
        ).fetchall()
        lessons = [f"[{ (r[3] or '未验证') }] {(r[2] or '')[:120]}" for r in rows]
        if lessons:
            out["similar_past_lessons"] = " | ".join(lessons)
    except Exception:
        pass
    return out


def enrich_one(reflection_id: int) -> bool:
    """对单条 trade_reflections_v2 反思做 LLM 深度反思,写入 llm_depth_reflection。

    返回 True 表示成功, False 表示失败/跳过。
    v200 A5: LEFT JOIN ai_decisions, 历史 decision_id 不存在时也不失败 (prompt 残缺但仍跑)。
    v200 A4: 取消 quick_sim JOIN, 直接读 r.pnl_pct 判定 LOSS。
    """
    import sqlite3 as _sq3
    conn = db.get_connection()
    conn.row_factory = _sq3.Row
    try:
        row = conn.execute("""
            SELECT r.id, r.decision_id, r.asset, r.action, r.error_layer,
                   r.error_sub_layer, r.error_signal, r.root_cause, r.lesson,
                   r.pnl_pct, r.mfe_pct, r.mae_pct, r.depth_score,
                   r.llm_depth_reflection,
                   a.event_strength, a.event_type_code, a.market_confirmation,
                   a.reasoning,
                   a.created_at
                   -- v200 A4: 去掉 quick_sim_trades JOIN (命中不全)
                   -- v200 A5: 改 LEFT JOIN 让历史 decision_id 不存在的反思也能跑
              FROM trade_reflections_v2 r
              LEFT JOIN ai_decisions a ON a.id = r.decision_id
             WHERE r.id = ?
        """, (int(reflection_id),)).fetchone()
        if not row:
            return False
        d = dict(row)
        if d.get("llm_depth_reflection"):
            return True
        # v231+ (H5): reflection 缓存失效 - 如果该反思的 decision 现在被 quality_rejections
        # 标记为脏 (H4 拦了), 跳过 LLM enrichment, 不浪费 token 反思脏数据.
        # off 路径: is_decision_dirty 返 False, 不影响.
        try:
            import sample_quality_filter as _sqf
            if _sqf.is_decision_dirty(conn, d.get("decision_id")):
                print(f"[H5] enrich_one reflection_id={reflection_id} 跳过 LLM: "
                      f"decision_id={d.get('decision_id')} 已被 quality_rejections 标记 (脏数据)",
                      flush=True)
                return False
        except Exception as _exc_h5:
            # H5 检查自身失败 → 兜底继续 (宁做 LLM 不漏学)
            print(f"[H5] is_decision_dirty 检查失败, 兜底继续 enrich_one: "
                  f"{type(_exc_h5).__name__}: {_exc_h5}", flush=True)
        # v482: 亏损单照旧(-0.5% 阈值), 盈利单也进(≥ +0.5%) —— 只反思亏损会让
        # 经验池全是"别做什么", 赢单的正向条件永远进不了池子。
        try:
            pnl = float(d.get("pnl_pct") or 0)
        except (ValueError, TypeError):
            return False
        if pnl < -0.005:
            mode = "LOSS"
        elif pnl >= 0.005:
            mode = "WIN"
        else:
            return False            # 噪声区(|pnl|<0.5%) 不值得花 token
        # v482: 不再只拿 depth_score(字段齐全度的公式分)当质量门 ——
        # 字段全但内容空话的行不该花 token, 字段少但已带数字锚点的行不该被拦。
        # 判据: depth_score>=3 或有 >=2 个数字锚点, 满足其一即放行。
        _ds = int(d.get("depth_score") or 0)
        _anchors = 0
        try:
            _anchors = int(_count_evidence_anchors(
                (d.get("root_cause") or "") + " " + (d.get("lesson") or "")))
        except Exception:
            _anchors = 0
        if _ds < 3 and _anchors < 2:
            return False
        # v482: 成本与机械反演净值在这里一次算清, 喂给模型引用 ——
        # 不让模型自己对净 pnl 取负(会重复计成本, 写出"反向就能赚")。
        cost_pct = 0.0
        try:
            import cost_model as _cm
            cost_pct = float(_cm.round_trip_cost_bps(d.get("asset") or "")) / 100.0
        except Exception:
            cost_pct = 0.0
        cost_bps_i = int(round(cost_pct * 100))
        cf_net = -(pnl + 2.0 * cost_pct)
        decision_ts_ms = None
        try:
            import time as _t
            _s = (d.get("created_at") or "").strip()
            if _s:
                decision_ts_ms = int(_t.mktime(_t.strptime(_s, "%Y-%m-%d %H:%M:%S")) * 1000)
        except Exception:
            decision_ts_ms = None
        posthoc = _collect_posthoc_evidence(conn, d.get("asset") or "", decision_ts_ms)
        # v482: 按 pnl 符号选模板(亏损→归因失败, 盈利→归因成功), 并把成本/反事实
        # 净值喂给模型, 不让它自己对净值取负。
        try:
            pnl = float(d.get("pnl_pct") or 0)
        except (ValueError, TypeError):
            pnl = 0.0
        mode = "WIN" if pnl >= 0.005 else "LOSS"
        cost_pct = 0.0
        try:
            import cost_model as _cm
            cost_pct = float(_cm.round_trip_cost_bps(d.get("asset") or "")) / 100.0
        except Exception:
            cost_pct = 0.0
        cost_bps_i = int(round(cost_pct * 100))
        cf_net = -(pnl + 2.0 * cost_pct)
        tmpl = _DEPTH_PROMPT_TEMPLATE if mode == "LOSS" else _WIN_PROMPT_TEMPLATE
        prompt = tmpl.format(
            decision_id=d["decision_id"], asset=d.get("asset") or "",
            action=d.get("action") or "",
            event_strength=d.get("event_strength") or "",
            event_type=d.get("event_type_code") or "",
            market_confirmation=d.get("market_confirmation") or "",
            forward_pnl=d.get("pnl_pct"),
            mfe_pct=d.get("mfe_pct"), mae_pct=d.get("mae_pct"),
            reasoning=(d.get("reasoning") or "")[:800],
            realized_move=posthoc["realized_move"],
            posthoc_cross_asset=posthoc["posthoc_cross_asset"],
            similar_past_lessons=posthoc["similar_past_lessons"],
            cost_bps=cost_bps_i,
            counterfactual_net=f"{cf_net:+.4f}",
            layer=d.get("error_layer") or "",
            sub_layer=d.get("error_sub_layer") or "",
            signal_name=d.get("error_signal") or "",
            root_cause=(d.get("root_cause") or "")[:300],
            lesson=(d.get("lesson") or "")[:200],
        )
        result = _call_llm_depth(prompt)
        if not result:
            return False
        text = json.dumps(result, ensure_ascii=False)
        # v190: 同步把 lesson_candidate 落到 lesson 字段,前端读 lesson 直接是真反思。
        # LLM 输出有时是 ```json\n{...}\n``` markdown 围栏, json.loads 失败
        # → result == {"raw_response": "...嵌套JSON..."}, 需要二次解析。
        new_lesson = ""
        parsed = result
        if "raw_response" in result and len(result) == 1:
            try:
                inner = json.loads(result["raw_response"]) if result["raw_response"].strip().startswith("{") else None
            except Exception:
                # 提取 markdown 围栏里的 JSON
                import re as _re
                m = _re.search(r"```(?:json)?\s*(\{.*?\})\s*```", result["raw_response"], _re.DOTALL)
                if m:
                    try:
                        inner = json.loads(m.group(1))
                    except Exception:
                        inner = None
                else:
                    inner = None
            if isinstance(inner, dict):
                parsed = inner
        if isinstance(parsed, dict):
            for key in ("lesson_candidate", "prevention", "key_failure_chain"):
                v = parsed.get(key)
                if isinstance(v, str) and v.strip():
                    new_lesson = v.strip()[:300]
                    break
        if new_lesson:
            conn.execute(
                "UPDATE trade_reflections_v2 "
                "   SET llm_depth_reflection=?, lesson=? "
                " WHERE id=?",
                (text[:3000], new_lesson, int(reflection_id)),
            )
        else:
            conn.execute(
                "UPDATE trade_reflections_v2 SET llm_depth_reflection=? WHERE id=?",
                (text[:3000], int(reflection_id)),
            )
        conn.commit()
        return True
    except Exception as exc:
        print(f"[LLM-REFLECT] enrich_one {reflection_id} 失败: "
              f"{type(exc).__name__}: {exc}", flush=True)
        return False
    finally:
        conn.close()


def enrich_pending_loop(batch_size: int = 10) -> int:
    """v200 A4: 取消 quick_sim JOIN, 让所有 pnl<-0.5% 反思进 LLM 反思.

    v199 原 filter (失败):
      SELECT r.id FROM trade_reflections_v2 r
      JOIN ai_decisions a ON a.id = r.decision_id
      JOIN quick_sim_trades qs ON qs.decision_id = a.id
      WHERE r.llm_depth_reflection = ''
        AND qs.verdict = 'LOSS'
        AND r.depth_score >= 3
        AND r.pnl_pct < -0.005

    v199 失败根因 (audit 实测 73 条):
      - quick_sim_trades 只有 15 条, 仅 13 个不同 decision_id
      - trade_reflections_v2 有 73 条, decision_id 范围 [1243, 1000247]
      - JOIN 命中: 13/73 (17.8%)
      - 即使 8 条 pnl<-0.5% 的反思也大多已写 LLM (7/8), 剩下 1 条 (ref#93) qs_verdict=None 被排除
      - 所以 daemon 永远 processed=0, 不是 filter 太严, 是 JOIN 命中率太低

    v200 A4 新 filter (直接用 r.pnl_pct 不用 quick_sim JOIN):
      - depth_score >= 3 (有上下文)
      - pnl_pct < -0.005 (LOSS 阈值不变)
      - llm_depth_reflection = '' (未写过)
      - error_layer IN ('analysis', 'filter') (新: 排除 schema 默认占位)

    73 条反思期望命中:
      pnl<-0.5% AND depth≥3 AND llm='' AND layer IN:  8 条 - 7 条 (已写) = 1 条 (ref#93)
      所以 v200 A4 一次性只能补 1 条历史, 之后等待新 trade 写入
    """
    import sqlite3 as _sq3
    conn = db.get_connection()
    conn.row_factory = _sq3.Row
    processed = 0
    try:
        rows = conn.execute("""
            SELECT r.id
              FROM trade_reflections_v2 r
             WHERE r.llm_depth_reflection = ''
               AND r.depth_score >= 3
               -- v482: 亏损单(< -0.5%) 与盈利单(>= +0.5%) 都要深度归因;
               -- 只学亏损会让经验池只剩"别做什么"。
               AND (r.pnl_pct < -0.005 OR r.pnl_pct >= 0.005)
               AND r.error_layer IN ('analysis', 'filter')  -- v200 A4: 排除 schema 默认占位
             ORDER BY r.id DESC LIMIT ?
        """, (max(1, min(batch_size, 50)),)).fetchall()
    finally:
        conn.close()
    for r in rows:
        if enrich_one(int(r["id"])):
            processed += 1
    return processed


def reproduce(reflection_id: int) -> Dict[str, Any]:
    """端点用: 对单条 trade_reflections_v2 重新调 LLM 反思。"""
    import sqlite3 as _sq3
    conn = db.get_connection()
    conn.row_factory = _sq3.Row
    try:
        row = conn.execute("""
            SELECT r.id, r.decision_id, r.asset, r.action, r.error_layer,
                   r.error_sub_layer, r.error_signal, r.root_cause, r.lesson,
                   r.pnl_pct, r.mfe_pct, r.mae_pct, r.depth_score,
                   r.llm_depth_reflection,
                   a.event_strength, a.event_type_code, a.market_confirmation,
                   a.reasoning,
                   a.created_at,
                   qs.verdict AS is_correct   -- P1: 结算正确性改读 quick_sim_trades
              FROM trade_reflections_v2 r
              JOIN ai_decisions a ON a.id = r.decision_id
              JOIN quick_sim_trades qs ON qs.decision_id = a.id
             WHERE r.id = ?
        """, (int(reflection_id),)).fetchone()
        if not row:
            return {"ok": False, "error": f"reflection {reflection_id} not found"}
        d = dict(row)
        # v231+ (H5): 用户手动 reprocess 也检查 - decision 脏的话拒跑, 告诉用户
        # 而不是默默生成一份基于脏数据的 lesson.
        try:
            import sample_quality_filter as _sqf
            if _sqf.is_decision_dirty(conn, d.get("decision_id")):
                return {
                    "ok": False,
                    "reflection_id": int(reflection_id),
                    "skipped_reason": "decision_dirty",
                    "error": (f"decision_id={d.get('decision_id')} 已被 quality_rejections 标记 "
                              f"(quality filter 拒过), 不生成 LLM 反思 (基于脏数据). "
                              f"如确需 reprocess, 先从 quality_rejections 删该 decision_id."),
                }
        except Exception as _exc_h5r:
            # 兜底: 检查失败 → 继续 (避免堵死 reproduce 路径)
            print(f"[H5] reproduce is_decision_dirty 检查失败, 兜底继续: "
                  f"{type(_exc_h5r).__name__}: {_exc_h5r}", flush=True)
        decision_ts_ms = None
        try:
            import time as _t
            _s = (d.get("created_at") or "").strip()
            if _s:
                decision_ts_ms = int(_t.mktime(_t.strptime(_s, "%Y-%m-%d %H:%M:%S")) * 1000)
        except Exception:
            decision_ts_ms = None
        posthoc = _collect_posthoc_evidence(conn, d.get("asset") or "", decision_ts_ms)
        # v482: 按 pnl 符号选模板(亏损→归因失败, 盈利→归因成功), 并把成本/反事实
        # 净值喂给模型, 不让它自己对净值取负。
        try:
            pnl = float(d.get("pnl_pct") or 0)
        except (ValueError, TypeError):
            pnl = 0.0
        mode = "WIN" if pnl >= 0.005 else "LOSS"
        cost_pct = 0.0
        try:
            import cost_model as _cm
            cost_pct = float(_cm.round_trip_cost_bps(d.get("asset") or "")) / 100.0
        except Exception:
            cost_pct = 0.0
        cost_bps_i = int(round(cost_pct * 100))
        cf_net = -(pnl + 2.0 * cost_pct)
        tmpl = _DEPTH_PROMPT_TEMPLATE if mode == "LOSS" else _WIN_PROMPT_TEMPLATE
        prompt = tmpl.format(
            decision_id=d["decision_id"], asset=d.get("asset") or "",
            action=d.get("action") or "",
            event_strength=d.get("event_strength") or "",
            event_type=d.get("event_type_code") or "",
            market_confirmation=d.get("market_confirmation") or "",
            forward_pnl=d.get("pnl_pct"),
            mfe_pct=d.get("mfe_pct"), mae_pct=d.get("mae_pct"),
            reasoning=(d.get("reasoning") or "")[:800],
            realized_move=posthoc["realized_move"],
            posthoc_cross_asset=posthoc["posthoc_cross_asset"],
            similar_past_lessons=posthoc["similar_past_lessons"],
            cost_bps=cost_bps_i,
            counterfactual_net=f"{cf_net:+.4f}",
            layer=d.get("error_layer") or "",
            sub_layer=d.get("error_sub_layer") or "",
            signal_name=d.get("error_signal") or "",
            root_cause=(d.get("root_cause") or "")[:300],
            lesson=(d.get("lesson") or "")[:200],
        )
        result = _call_llm_depth(prompt)
        if result:
            text = json.dumps(result, ensure_ascii=False)
            # v190: 同步把 lesson_candidate 落到 lesson 字段(与 enrich_one 一致)
            # markdown 围栏二次解析逻辑同 enrich_one
            new_lesson = ""
            parsed = result
            if isinstance(result, dict) and "raw_response" in result and len(result) == 1:
                import re as _re
                m = _re.search(r"```(?:json)?\s*(\{.*?\})\s*```", result["raw_response"], _re.DOTALL)
                if m:
                    try:
                        inner = json.loads(m.group(1))
                        if isinstance(inner, dict):
                            parsed = inner
                    except Exception:
                        pass
            if isinstance(parsed, dict):
                for key in ("lesson_candidate", "prevention", "key_failure_chain"):
                    v = parsed.get(key)
                    if isinstance(v, str) and v.strip():
                        new_lesson = v.strip()[:300]
                        break
            if new_lesson:
                conn.execute(
                    "UPDATE trade_reflections_v2 "
                    "   SET llm_depth_reflection=?, lesson=? "
                    " WHERE id=?",
                    (text[:3000], new_lesson, int(reflection_id)),
                )
            else:
                conn.execute(
                    "UPDATE trade_reflections_v2 SET llm_depth_reflection=? WHERE id=?",
                    (text[:3000], int(reflection_id)),
                )
            conn.commit()
            return {"ok": True, "reflection_id": int(reflection_id),
                    "llm_depth_reflection": result,
                    "prompt_used": prompt[:500]}
        return {"ok": False, "reflection_id": int(reflection_id),
                "error": "LLM call failed (返回 None)"}
    except Exception as exc:
        return {"ok": False, "reflection_id": int(reflection_id),
                "error": f"{type(exc).__name__}: {exc}"}
    finally:
        conn.close()


def daemon_loop(interval_s: int = 300, batch_size: int = 5) -> None:
    """v189-fix: 周期循环 enrich_pending_loop（按 5min 间隔）。

    设计：
      - 默认 5min/次，每次最多 batch_size=5 条（LLM 深度反思单次成本高，限速）
      - env 覆盖：
          TRADE_REFLECT_INTERVAL_S  (默认 300)
          TRADE_REFLECT_BATCH       (默认 5)
      - 单次失败不退出，只 print 日志；KeyboardInterrupt 优雅退出
      - 由 api_server lifespan 在独立线程中启动
    """
    import time as _t
    interval_s = max(60, int(os.environ.get("TRADE_REFLECT_INTERVAL_S", interval_s)))
    batch_size = max(1, min(20, int(os.environ.get("TRADE_REFLECT_BATCH", batch_size))))
    print(f"[LLM-REFLECT] daemon started | interval={interval_s}s batch={batch_size}",
          flush=True)
    try:
        while True:
            try:
                n = enrich_pending_loop(batch_size=batch_size)
                if n > 0:
                    print(f"[LLM-REFLECT] daemon processed={n}", flush=True)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[LLM-REFLECT] daemon err: {type(exc).__name__}: {exc}",
                      flush=True)
            _t.sleep(interval_s)
    except KeyboardInterrupt:
        print("\n[LLM-REFLECT] daemon stopped (KeyboardInterrupt)", flush=True)


__all__ = ["enrich_one", "enrich_pending_loop", "reproduce", "daemon_loop"]