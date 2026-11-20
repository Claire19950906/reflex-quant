# -*- coding: utf-8 -*-
"""reasoning_quality_report.py - P3 LLM 第一人称自述推理质量报告

每次对一条 ai_decision 让 LLM 自己口述：
  我当时推理...
  我假设了...
  我反思...
  下次我会...

样本与触发：默认仅错误样本 (is_correct=0 或 forward_pnl<0) — 避免无成本生成的正确样本
成本控制：每个 decision_id 一旦生成后写入 decision_narrative 表 — 不重复
          用户每次手动点「生成自述」才调用 LLM（避免 cron 批量成本）
"""
from __future__ import annotations
import json
import sqlite3
from typing import Any, Dict, List, Optional


def _ensure_narrative_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS decision_narrative (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            decision_id INTEGER NOT NULL,
            review_dict TEXT NOT NULL,
            narrative TEXT NOT NULL,
            model TEXT DEFAULT '',
            prompt_tokens INTEGER DEFAULT 0,
            completion_tokens INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now')),
            UNIQUE(decision_id)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_decision_narrative_ts
            ON decision_narrative(created_at)
    """)
    conn.commit()


def build_prompt(decision: Dict[str, Any], review: Dict[str, Any]) -> str:
    """构造 LLM 第一人称自述的 prompt。

    v275+ 改造：把"反思哲学"放在 prompt 头部作为【原则】而非【固定模板】。
    强调「一事一议」「第一性原理」「追问几层由你判断」「不写固定公式」。
    4 段标题保留作为组织提示，但不强制填全 / 不强制字数。
    """
    notes = review.get("notes") or []
    steps_present = (review.get("steps") or {}).get("present") or []
    steps_missing = (review.get("steps") or {}).get("missing") or []
    factors = (review.get("factor_concepts") or {}).get("concepts") or []
    cad = (review.get("cross_asset_direction") or {})

    # ─── 元指令：放在 prompt 头部，作为「反思原则」而非「必须遵循的格式」───
    # 这是为了体现"反思深度由错误性质决定 + 第一性原理 + 不写死公式"
    # 所有原 4 段提示保留，但下面明确告知 LLM：觉得不够就多写；觉得简单就一句话
    META_PRINCIPLES = (
        "【反思原则 — 不强制，按需调用】\n"
        "• 一事一议：反思深度由你自己判断。\n"
        "  - 简单错误（数据缺失 / 字段漏读 / 单点遗漏）一句就够，别为了凑数硬写。\n"
        "  - 复杂错误（推理链断裂 / 概念错位 / 跨资产方向错）需要追问到核心原因 —— 但「追几层」由你定，不是 5 层、不是 3 层，是「追问到没答案为止」。\n"
        "• 第一性原理：把当时推理当作「结论 / 前提 / 推理 / 数据」四件套拆开看。\n"
        "  反复追问「我凭什么相信这个前提」—— 追问到没答案为止，那就是核心原因。\n"
        "• 不回避系统的可改进点：你认为某条不重要，就明确写「这条我觉得不重要，因为……」。\n"
        "• 改进点必须可执行：不要写「应该更小心」「下次注意」这种空话。\n"
        "  每个改进点要指向具体环节 —— 改数据（采什么）、改 prompt 某段（哪一段）、改参数（哪个参数）、改假设校验（怎么校验）。\n"
        "\n"
    )

    return (
        "你是当时做这笔决策的 LLM。请用『我』的口吻，对自己的推理过程做一份第一人称反思。\n"
        "用户已经记录了系统层面检查出来的可改进点（如 8 段推理链缺哪几段、是否出现反共识、"
        "是否有因果机制关键词、因素量化、跨资产传导方向、是否明说假设、是否引用历史样本、校准等）。\n"
        "\n"
        + META_PRINCIPLES +
        "下面 4 段是「组织提示」，不是「填空模板」—— 觉得不够可以补充，觉得简单可以只写一段或两句话。\n"
        "  1) 我当时推理：……（简要复述当时思考路径）\n"
        "  2) 我假设了：……（明示推理的前提假设 —— 可多条，也可不写）\n"
        "  3) 我反思：……（系统列出的可改进点中你认为最关键的，以及你自己的追问 —— 多少由你判断）\n"
        "  4) 下次我会：……（具体可执行的修正 —— 可多条）\n"
        "\n"
        "其它约束：\n"
        "  - 必须用『我』自称，不要用『模型/LLM/AI』\n"
        "  - 不要写礼貌话或免责声明\n"
        "  - 不要回避系统列出的可改进点\n"
        "  - 总长度 300-800 中文字符（复杂错误可以超 800，不要为了凑字数也不要因简单错误硬撑）\n"
        "\n"
        "———— 元数据 ————\n"
        "decision_id: {decision_id}\n"
        "asset: {asset}\n"
        "action: {action}\n"
        "is_correct: {is_correct}\n"
        "forward_pnl: {forward_pnl}\n"
        "reasoning_length: {reasoning_length}\n"
        "8 段已覆盖 ({steps_n}/8): {steps_present}\n"
        "8 段缺: {steps_missing}\n"
        "因素概念: {factors}\n"
        "跨资产: {cad}\n"
        "———— 系统列出的可改进点 ————\n"
        "{notes_block}\n"
        "———— 当时的 reasoning 文本 ————\n"
        "{reasoning}\n"
    ).format(
        decision_id=decision.get("id") or "?",
        asset=(decision.get("target_asset") or "?").upper(),
        action=(decision.get("suggested_action") or "?").upper(),
        is_correct=decision.get("is_correct"),
        forward_pnl=decision.get("forward_pnl"),
        reasoning_length=len(decision.get("reasoning_path") or decision.get("reasoning") or ""),
        steps_n=len(steps_present),
        steps_present=", ".join(steps_present) or "(空)",
        steps_missing=", ".join(steps_missing) or "(无)",
        factors=", ".join(factors) or "(空)",
        cad=cad.get("detail") or "(空)",
        notes_block="\n".join("- " + n for n in notes) or "(无)",
        reasoning=(decision.get("reasoning_path") or decision.get("reasoning") or "(空)")[:3000],
    )


def get_narrative_for_decision(decision_id: int, conn: sqlite3.Connection) -> Optional[Dict[str, Any]]:
    _ensure_narrative_table(conn)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM decision_narrative WHERE decision_id = ?",
        (int(decision_id),),
    ).fetchone()
    if not row:
        return None
    d = dict(row)
    try:
        d["review_dict"] = json.loads(d.get("review_dict") or "{}")
    except (json.JSONDecodeError, TypeError):
        d["review_dict"] = {}
    return d


def list_narratives(limit: int = 50, days: int = 7,
                    only_failed: bool = True, conn: sqlite3.Connection = None) -> List[Dict[str, Any]]:
    if conn is None:
        return []
    _ensure_narrative_table(conn)
    conn.row_factory = sqlite3.Row
    limit = min(max(int(limit), 1), 200)
    days = min(max(int(days), 1), 90)
    if only_failed:
        sql_q = ("SELECT n.* FROM decision_narrative n "
                  "JOIN ai_decisions d ON d.id = n.decision_id "
                  "WHERE n.created_at >= datetime('now', ?) "
                  "AND (d.is_correct = 0 OR (d.forward_pnl IS NOT NULL AND d.forward_pnl < 0)) "
                  "ORDER BY n.created_at DESC LIMIT ?")
        params = ["-{} days".format(days), limit]
    else:
        sql_q = ("SELECT * FROM decision_narrative "
                 "WHERE created_at >= datetime('now', ?) "
                 "ORDER BY created_at DESC LIMIT ?")
        params = ["-{} days".format(days), limit]
    rows = conn.execute(sql_q, params).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["review_dict"] = json.loads(d.get("review_dict") or "{}")
        except (json.JSONDecodeError, TypeError):
            d["review_dict"] = {}
        out.append(d)
    return out


def save_narrative(decision_id: int, review_dict: Dict[str, Any],
                   narrative: str, model: str = "",
                   prompt_tokens: int = 0, completion_tokens: int = 0,
                   conn: sqlite3.Connection = None) -> int:
    if conn is None:
        return -1
    _ensure_narrative_table(conn)
    cur = conn.execute(
        """INSERT OR REPLACE INTO decision_narrative
              (decision_id, review_dict, narrative, model,
               prompt_tokens, completion_tokens, created_at)
           VALUES (?, ?, ?, ?, ?, ?, datetime('now'))""",
        (int(decision_id), json.dumps(review_dict, ensure_ascii=False)[:8000],
         str(narrative), str(model or "")[:64],
         int(prompt_tokens or 0), int(completion_tokens or 0)),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def call_llm_for_narrative(prompt: str, model_label: str = None) -> Dict[str, Any]:
    """调用 LLM 生成自述 — 通过 ai_worker.chat (走 reflection purpose 池)。"""
    try:
        from engine import ai_worker as _aiw
        text = _aiw.chat(prompt, purpose="reflection", call_site="reasoning_quality_report",
                         max_tokens=900, temperature=0.5)
        return {"text": text or "", "error": "" if text else "empty", "model": model_label or "reflection-pool"}
    except Exception as e:
        return {"text": "", "error": str(e), "model": model_label or "reflection-pool"}