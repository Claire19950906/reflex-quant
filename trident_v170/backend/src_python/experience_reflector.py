#!/usr/bin/env python3
"""经验系统 v186 — 加载 / 注入 / 应用 / 自适应。

三件套（process 经验规定）：
  ROOT_CAUSE — 具体到 layer + sub_layer + signal_name
  LESSON     — 一句话可执行经验
  VERIFICATION — 如何验证这条经验真的有效（simulation / replay / A/B）
"""
from __future__ import annotations

import json, time, sqlite3
from typing import Any, Dict, List, Optional
import db


# ============================================================
# 加载
# ============================================================
def load_experiences(asset=None, gate_kind=None, actor=None,
                     kind=None, top_k=6):
    """按上下文匹配 + 优先级 + weight 排序。"""
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    out = []
    try:
        rows = conn.execute("""
            SELECT id, kind, title, content, applicable_assets,
                   applicable_kinds, applicable_actors,
                   weight, priority, source, created_at
              FROM experience_entries
             WHERE status='active'
             ORDER BY priority DESC, weight DESC, id DESC LIMIT 200
        """).fetchall()
        for r in rows:
            d = dict(r)
            try: aa = json.loads(d["applicable_assets"] or "[]")
            except: aa = []
            try: ak = json.loads(d["applicable_kinds"] or "[]")
            except: ak = []
            try: ar = json.loads(d["applicable_actors"] or "[]")
            except: ar = []
            score = 0
            if asset and (not aa or asset.upper() in [a.upper() for a in aa]):
                score += 2
            if gate_kind and (not ak or any(k.lower() in gate_kind.lower() for k in ak)):
                score += 3
            if actor and (not ar or any(a.lower() in actor.lower() for a in ar)):
                score += 2
            d["_match_score"] = score
            out.append(d)
        out.sort(key=lambda x: (-x["_match_score"], -x["priority"]))
        return out[:top_k]
    except Exception as exc:
        print(f"[EXP-REFL] load 失败: {exc}", flush=True)
        return []
    finally:
        conn.close()


def compute_persona_weight(asset, actor):
    """根据 persona/government 经验计算 actor 对 asset 的影响力权重。"""
    if not actor:
        return 1.0
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("""
            SELECT weight, applicable_assets, applicable_actors
              FROM experience_entries
             WHERE status='active' AND kind IN ('persona','government')
        """).fetchall()
    except Exception:
        return 1.0
    finally:
        conn.close()
    actor_low = actor.lower()
    asset_up = (asset or "").upper()
    # 分两个池:specific(有具体 actors/assets)+ generic(空 ar/aa 作为兜底)
    specific_w = []   # list[float]
    generic_w = []    # list[float]
    for r in rows:
        try:
            aa = json.loads(r["applicable_assets"] or "[]")
            ar = json.loads(r["applicable_actors"] or "[]")
        except Exception:
            aa, ar = [], []
        # actor_match:有具体 ar 且 actor 命中 OR 无 ar(generic 兜底)
        actor_specific = bool(ar) and any(a.lower() in actor_low for a in ar)
        asset_specific = bool(aa) and asset_up in [a.upper() for a in aa]
        actor_generic = not ar
        asset_generic = not aa
        if actor_specific and asset_specific:
            specific_w.append(float(r["weight"]))
        elif (actor_specific and asset_generic) or (asset_specific and actor_generic):
            specific_w.append(float(r["weight"]))
        elif actor_generic and asset_generic:
            generic_w.append(float(r["weight"]))
    if specific_w:
        return round(max(specific_w), 3)
    if generic_w:
        return round(max(generic_w), 3)
    return 1.0


def get_matched_experience_id(asset: str, actor: str) -> int:
    """返回当前 actor/asset 命中的最高优先级经验 id(0=无命中, 中性)。

    用于把命中经验写进 ai_decisions.persona_match_id，方便归因页分析
    「某 actor 的发言历史命中率/胜率」。
    """
    if not actor:
        return 0
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("""
            SELECT id, weight, applicable_assets, applicable_actors
              FROM experience_entries
             WHERE status='active' AND kind IN ('persona','government')
        """).fetchall()
    except Exception:
        return 0
    finally:
        conn.close()
    actor_low = actor.lower()
    asset_up = (asset or "").upper()
    best = (0, 0.0)  # (id, weight)
    for r in rows:
        try:
            aa = json.loads(r["applicable_assets"] or "[]")
            ar = json.loads(r["applicable_actors"] or "[]")
        except Exception:
            aa, ar = [], []
        actor_specific = bool(ar) and any(a.lower() in actor_low for a in ar)
        asset_specific = bool(aa) and asset_up in [a.upper() for a in aa]
        actor_generic = not ar
        asset_generic = not aa
        # 仅在 specific(actor 或 asset 至少一边)命中时记录 id(generic 没意义)
        if (actor_specific and (asset_specific or asset_generic)) or \
           (asset_specific and (actor_specific or actor_generic)):
            w = float(r["weight"] or 1.0)
            if w > best[1]:
                best = (int(r["id"]), w)
    return best[0]


# ============================================================
# 提示词注入
# ============================================================
def build_prompt_block(asset="", gate_kind="", actor="", max_chars=1800,
                       decision_id: int = 0) -> "str | tuple[str, List[int]]":
    """生成要注入 LLM prompt 的经验块(中文)。

    v188 P0-2:
      - 加 decision_id 参数 — 当经验被真正注入到 prompt 时,
        调 apply_experience() 写入 experience_applications 并标 llm_invoked=1。
      - 返回值改为 (block_text, injected_ids) tuple (兼容旧 str 调用方)。
        若调用方传 decision_id=0(纯查询),仍返回 str block。
    """
    exps = load_experiences(asset=asset, gate_kind=gate_kind, actor=actor, top_k=5)
    if not exps:
        if decision_id > 0:
            return ("", [])
        return ""
    lines = ["# 经验系统 v186 自动注入(按相关性排序)"]
    persona_w = compute_persona_weight(asset, actor)
    if persona_w != 1.0:
        lines.append(f"  → 人物影响权重 {actor} 对 {asset} = {persona_w} (基于经验库自动校准)")
    lines.append("")
    injected_ids: List[int] = []
    for i, e in enumerate(exps, 1):
        title = e["title"]; kind = e["kind"]; weight = e.get("weight", 1.0)
        content = (e["content"] or "").strip()
        if len(content) > 320:
            content = content[:317] + "..."
        lines.append(f"[EXP{i}] [{kind}] (w={weight}) {title}")
        lines.append(f"  {content}")
        lines.append("")
        try:
            injected_ids.append(int(e.get("id") or 0))
        except Exception:
            pass
    block = "\n".join(lines)
    if len(block) > max_chars:
        block = block[:max_chars - 50] + "\n...(经验块过长已截断)"
    # v188 P0-2: 标记 llm_invoked (只有在真正被 prompt 用到的经验才算)
    if decision_id > 0 and block.strip():
        for eid in injected_ids:
            if eid > 0:
                try:
                    apply_experience(eid, int(decision_id),
                                     applied_action="prompt_inject", llm_invoked=1)
                except Exception:
                    pass
    if decision_id > 0:
        return (block, injected_ids)
    return block


# ============================================================
# 反思细分写入(三件套)
# ============================================================
def record_root_cause(*, reflection_id, layer, sub_layer, signal_name,
                      root_cause, lesson, verification,
                      experience_ids=None, depth_score=0):
    """写入反思的三件套(细粒度)。
    layer/sub_layer/signal_name 替代 23 类线性码。
    layer: data / timing / signal / judgment / execution / strategy / gate
    sub_layer 举例:
      analysis: llm_output / sentiment_score / event_struct / macro_context / factor_chain
      filter:   confidence_gate / contradiction_check / strength_threshold / asset_match
      data:     feed_status / latency / coverage
      gate:     gate_verdict / gate_strength / gate_core_impact
    """
    layer_breakdown = json.dumps({
        "layer": layer, "sub_layer": sub_layer, "signal_name": signal_name,
        "root_cause": root_cause, "lesson": lesson, "verification": verification,
    }, ensure_ascii=False)
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            UPDATE decision_reflection
               SET layer_breakdown = ?, error_root_cause = ?,
                   experience_applied = ?, depth_score = ?,
                   actionable_suggestion = ?
             WHERE id = ?
        """, (
            layer_breakdown, root_cause,
            json.dumps(experience_ids or [], ensure_ascii=False),
            max(0, min(100, depth_score)),
            f"LESSON: {lesson} | VERIFY: {verification}",
            reflection_id,
        ))
        conn.commit()
        return cur.rowcount
    except Exception as exc:
        print(f"[EXP-REFL] record_root_cause 失败: {exc}", flush=True)
        return 0
    finally:
        conn.close()


def apply_experience(experience_id, decision_id, applied_action="referenced",
                      llm_invoked: int = 0):
    """记录某次决策应用了哪条经验。

    v188 P0-2: 加 llm_invoked 标记:
      - llm_invoked=0: 仅"命中"经验(如 ai_decisions.persona_match_id 自动选择)
      - llm_invoked=1: 经验被真正注入到 LLM prompt(build_prompt_block 调用点)
    这样 adaptive_review_and_apply 可以区分"误应用"vs"被 LLM 实际参考"。
    """
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS experience_applications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                experience_id INTEGER, decision_id INTEGER,
                applied_action TEXT, applied_at TEXT DEFAULT (datetime('now')),
                llm_invoked INTEGER DEFAULT 0,
                UNIQUE(experience_id, decision_id, applied_action)
            )
        """)
        conn.commit()
        cur.execute("""
            INSERT OR IGNORE INTO experience_applications
              (experience_id, decision_id, applied_action, llm_invoked)
            VALUES (?, ?, ?, ?)
        """, (experience_id, decision_id, applied_action, int(llm_invoked)))
        conn.commit()
        # 注意: IGNORE 时不更新已有行(避免覆盖早期 llm_invoked=1 的标记)
        # 如果需要从 0 → 1, 用 mark_llm_invoked()
        return cur.rowcount > 0
    except Exception as exc:
        print(f"[EXP-REFL] apply 失败: {exc}", flush=True)
        return False
    finally:
        conn.close()


def mark_llm_invoked(experience_id: int, decision_id: int, applied_action: str = "referenced") -> bool:
    """把已有的 experience_applications 行标记为 llm_invoked=1。

    v188 P0-2: 用于 build_prompt_block 注入经验到 prompt 后调用,标记"经验被 LLM 实际参考"。
    """
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            UPDATE experience_applications
               SET llm_invoked = 1
             WHERE experience_id = ? AND decision_id = ? AND applied_action = ?
        """, (int(experience_id), int(decision_id), applied_action))
        conn.commit()
        return cur.rowcount > 0
    except Exception as exc:
        print(f"[EXP-REFL] mark_llm_invoked 失败: {exc}", flush=True)
        return False
    finally:
        conn.close()


def adaptive_review_and_apply(window_hours=24):
    """v188 自适应:每条经验被应用的胜率自动调整权重 + 优先级, 同时算 lift。

    增强(v188):
      - 胜率 >= 60% (n>=3) → weight +0.05(上限 1.0)
      - 胜率 <= 35% (n>=3) → weight -0.1(下限 0.1)
      - 30 天未用 → priority -10
      - 计算 applied_win_rate vs control_win_rate → 写入 lift 列
      - control group: 同 actor+asset 同窗口内, 但没匹配上该经验的决策

    返回: stats 字典, 含 lift 数据。
    """
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    stats = {
        "n_scanned": 0, "n_weight_up": 0, "n_weight_down": 0, "n_priority_decay": 0,
        "n_lift_computed": 0, "lift_distribution": {"positive": 0, "negative": 0, "neutral": 0},
    }
    try:
        exps = conn.execute("""
            SELECT id, weight, priority, applicable_assets, applicable_actors, applicable_kinds
              FROM experience_entries WHERE status='active'
        """).fetchall()
        for e in exps:
            stats["n_scanned"] += 1
            eid = e["id"]
            # 1. 应用过该经验的决策胜率
            row = conn.execute("""
                SELECT COUNT(*) AS n,
                       AVG(CASE WHEN qs.pnl_pct > 0 THEN 1.0 ELSE 0.0 END) AS win_rate
                  FROM experience_applications ea
                  JOIN ai_decisions ad ON ad.id = ea.decision_id
                  JOIN quick_sim_trades qs ON qs.decision_id = ad.id
                 WHERE ea.experience_id = ? AND qs.settled=1
                   AND ea.applied_at >= datetime('now', ?)
            """, (eid, f"-{window_hours} hours")).fetchone()
            n = int(row["n"] or 0) if row else 0
            wr = float(row["win_rate"] or 0) if row else 0
            if n >= 3:
                if wr >= 0.6:
                    new_w = min(1.0, float(e["weight"]) + 0.05)
                    if new_w > float(e["weight"]):
                        cur.execute("UPDATE experience_entries SET weight=?, updated_at=datetime('now') WHERE id=?", (new_w, eid))
                        stats["n_weight_up"] += 1
                elif wr <= 0.35:
                    new_w = max(0.1, float(e["weight"]) - 0.1)
                    if new_w < float(e["weight"]):
                        cur.execute("UPDATE experience_entries SET weight=?, updated_at=datetime('now') WHERE id=?", (new_w, eid))
                        stats["n_weight_down"] += 1

            # 2. v188: control group + lift
            try:
                aa = json.loads(e["applicable_assets"] or "[]")
                ar = json.loads(e["applicable_actors"] or "[]")
                ak = json.loads(e["applicable_kinds"] or "[]")
                ctrl_row = conn.execute("""
                    SELECT COUNT(*) AS n,
                           AVG(CASE WHEN qs.pnl_pct > 0 THEN 1.0 ELSE 0.0 END) AS win_rate
                      FROM ai_decisions ad
                      JOIN quick_sim_trades qs ON qs.decision_id = ad.id
                     WHERE qs.settled=1
                       AND ad.ts >= strftime('%s', datetime('now', ?))
                       AND NOT EXISTS (
                           SELECT 1 FROM experience_applications ea
                            WHERE ea.decision_id = ad.id AND ea.experience_id = ?
                       )
                """, (f"-{window_hours} hours", eid)).fetchone()
                ctrl_n = int(ctrl_row["n"] or 0) if ctrl_row else 0
                ctrl_wr = float(ctrl_row["win_rate"] or 0) if ctrl_row else 0
                lift = wr - ctrl_wr
                if ctrl_n >= 3:
                    cur.execute("""
                        UPDATE experience_entries
                           SET applied_win_rate = ?,
                               control_apply_count = ?,
                               control_win_rate = ?,
                               lift = ?
                         WHERE id = ?
                    """, (wr, ctrl_n, ctrl_wr, lift, eid))
                    stats["n_lift_computed"] += 1
                    if lift > 0.05:
                        stats["lift_distribution"]["positive"] += 1
                    elif lift < -0.05:
                        stats["lift_distribution"]["negative"] += 1
                    else:
                        stats["lift_distribution"]["neutral"] += 1
            except Exception as exc:
                print(f"[EXP-REFL] lift calc for exp {eid} failed: "
                      f"{type(exc).__name__}: {exc}", flush=True)

            # 3. 30 天未用 → priority decay
            # 旧版 priority-10/次 过激,一个未用的 exp 跑 5 轮就 priority=0,
            # 后续即便命中 LLM 抽出的高频模式也捞不回来。改 -3/次,加 ≤5 时停止,
            # 避免把"老但有效"的规则埋没掉。
            row2 = conn.execute("SELECT MAX(applied_at) AS last_used FROM experience_applications WHERE experience_id=?", (eid,)).fetchone()
            if not row2 or not row2["last_used"]:
                cur.execute("SELECT priority FROM experience_entries WHERE id=?", (eid,))
                cur_row = cur.fetchone()
                cur_prio = int(cur_row[0]) if cur_row else 5
                if cur_prio > 5:
                    stats["n_priority_decay"] += 1
                    cur.execute(
                        "UPDATE experience_entries SET priority=?, updated_at=datetime('now') WHERE id=?",
                        (max(5, cur_prio - 3), eid),
                    )
            # v189-7: per-experience commit, 避免 N 个 experience 同一事务持锁 ~N×5ms.
            try:
                conn.commit()
            except sqlite3.OperationalError as exc:
                # 单条 exp 自适应失败 (lock) 不影响其他 exp.
                print(f"[EXP-REFL] skip adaptive commit for exp {eid}: {type(exc).__name__}: {str(exc)[:80]}")
    except Exception as exc:
        print(f"[EXP-REFL] adaptive 失败: {exc}", flush=True)
    finally:
        conn.close()
    return stats


# ============================================================
# 反思 prompt 模板(给 LLM 用)
# ============================================================
REFLECTION_PROMPT_TEMPLATE = """\
你是一个深度交易复盘员。请基于以下三件套要求,反思失败决策。

# 反思三件套要求(强制)
1. **ROOT_CAUSE** — 具体到 layer + sub_layer + signal_name
   - layer 取值: data / timing / signal / judgment / execution / strategy / gate
   - sub_layer 举例:
     · analysis 子层: llm_output / sentiment_score / event_struct / macro_context / factor_chain / cross_asset_transmission
     · filter 子层: confidence_gate / contradiction_check / strength_threshold / asset_match / temporal_alignment
     · data 子层: feed_status / latency / coverage / cleanliness
     · gate 子层: gate_verdict / gate_strength / gate_core_impact
   - signal_name 必须精确到失败的具体信号 (例: "sector_rotation_strength", "fed_pivot_score", "btc_etf_flow")
2. **LESSON** — 一句话可执行经验(可直接转化为参数/规则/prompt)
3. **VERIFICATION** — 如何验证这条经验真的有效
   - 选项: A/B (24h)/ replay (历史重放)/ simulation (用现有 past_decisions)/ pilot (限 BTC 24h 试)

# 输出格式(JSON)
{{
  "root_cause": {{
    "layer": "<layer>",
    "sub_layer": "<sub_layer>",
    "signal_name": "<signal_name>",
    "detail": "具体描述为什么错了"
  }},
  "lesson": "一句话经验",
  "verification": "A/B|pilot|replay|simulation + 步骤",
  "confidence": 0.0~1.0,
  "reusable_to_other_assets": ["BTC","ETH",...]
}}

# 输入数据
- 决策 ID: {decision_id}
- 资产: {asset}
- 动作: {action}
- 触发事件: {event_summary}
- 入场价: {entry_price} 出场价: {exit_price}
- PnL%: {pnl_pct}
- MFE%: {mfe_pct} MAE%: {mae_pct}
- 当前经验库相关条目:
{experience_block}
"""


def build_reflection_prompt(decision_row):
    """给 LLM 用的反思 prompt。"""
    asset = decision_row.get("target_asset") or ""
    actor = decision_row.get("event_actor") or ""
    gate_kind = decision_row.get("gate_kind") or ""
    exps = load_experiences(asset=asset, gate_kind=gate_kind, actor=actor, top_k=5)
    block_lines = []
    for i, e in enumerate(exps, 1):
        block_lines.append(f"[EXP{i}] [{e['kind']}] {e['title']}: {(e['content'] or '')[:200]}")
    if not block_lines:
        block_lines.append("(暂无相关经验)")
    return REFLECTION_PROMPT_TEMPLATE.format(
        decision_id=decision_row.get("id") or "",
        asset=asset,
        action=decision_row.get("suggested_action") or "",
        event_summary=(decision_row.get("news_summary") or decision_row.get("title") or "")[:300],
        entry_price=decision_row.get("entry_price"),
        exit_price=decision_row.get("exit_price"),
        pnl_pct=decision_row.get("forward_pnl"),
        mfe_pct=decision_row.get("mfe_pct"),
        mae_pct=decision_row.get("mae_pct"),
        experience_block="\n".join(block_lines),
    )