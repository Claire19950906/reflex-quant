#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Judge Worker — 异步消费未校验决策,调 Judge LLM 写 structured_check.

与主 ai_worker 完全解耦:
- 不阻塞决策写入
- 不阻塞用户查询
- 失败仅留空,不传播

调用方式:
    python3 judge_worker.py              # 前台循环
    或 launchctl 单独管理
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import db
import sqlite3

JUDGE_PROMPT = """你是交易决策质量审核员。判断下面 AI 决策的内部一致性。

【新闻摘要】
{news}

【AI 决策】
- 标的: {target_asset}
- 行动: {action}
- 信心: {confidence}
- reasoning: {reasoning}

【任务】
请用 50-150 字评估这个决策的内部一致性,按下面 JSON 输出:
{{
  "consistency": "ok" | "minor" | "major",
  "consistency_reason": "...",
  "asset_nature_correct": true | false,
  "key_logic_chain_broken": false,
  "self_contradiction": "无" | "矛盾1: ..." | "矛盾2: ...",
  "skip_trade_rationale": "...",
  "severity": 0 | 1 | 2 | 3,
  "needs_human_review": true | false
}}

severity:
- 0: 完全自洽,放心
- 1: 小瑕疵,不必复核
- 2: 中度矛盾,值得人工看一眼
- 3: 严重自相矛盾,必须复核

只输出 JSON,不要解释。"""


def _call_judge_llm(news: str, target_asset: str, action: str,
                    confidence: float, reasoning: str) -> dict | None:
    """调 Judge LLM. 优先 Ollama 本地(免费), 失败 fallback MiniMax/DeepSeek.

    Returns parsed dict on success, None on failure (caller records nothing).
    """
    import config
    import requests

    user_msg = JUDGE_PROMPT.format(
        news=news[:1500],
        target_asset=target_asset or "N/A",
        action=action or "N/A",
        confidence=confidence if confidence is not None else 0.0,
        reasoning=(reasoning or "")[:3500],
    )

    # ── 路径 1: 本地 Ollama（首选 - 完全免费 / 无限 token / 离线可工作）────────
    ollama_url = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
    ollama_model = os.environ.get("OLLAMA_JUDGE_MODEL", "qwen2.5-coder:1.5b")  # 1.5b 在 M1 Air 上推理 <20s,7b 经常 60s+ OOM
    try:
        r = requests.post(
            f"{ollama_url}/api/generate",
            json={
                "model": ollama_model,
                "prompt": user_msg,
                "stream": False,
                "keep_alive": "24h",  # 模型驻留 24 小时（默认 5m 太短，每次都要 30s 冷加载）
                "options": {
                    "temperature": 0.1,
                    "num_predict": 600,         # 600 tokens 足够
                    "top_p": 0.9,
                },
            },
            timeout=300,  # 含冷加载（首次可能 60-90s, 后续每条 30s+）
        )
        r.raise_for_status()
        text = r.json().get("response", "")
        duration_ms = r.json().get("total_duration", 0) / 1_000_000
        eval_ms = r.json().get("eval_duration", 0) / 1_000_000
        load_ms = r.json().get("load_duration", 0) / 1_000_000
        print(
            f"[JUDGE-OLLAMA] model={ollama_model} "
            f"total={duration_ms:.0f}ms eval={eval_ms:.0f}ms load={load_ms:.0f}ms "
            f"out_len={len(text)}",
            flush=True,
        )
        return _parse_judge_json(text, source="ollama")
    except Exception as e:
        print(f"[JUDGE-OLLAMA] FAIL: {type(e).__name__}: {e}, fallback to remote", flush=True)

    # ── 路径 2: 远程 LLM fallback（MiniMax 或 OpenRouter）────────────────────────
    api_key = getattr(config, "MINIMAX2_API_KEY", None) or os.environ.get("MINIMAX2_API_KEY")
    api_base = (getattr(config, "MINIMAX_BASE_URL", None) or "https://api.minimax.chat/v1").rstrip("/")
    use_proxy = False
    if not api_key:
        api_key = getattr(config, "OPENROUTER_API_KEY", None) or os.environ.get("OPENROUTER_API_KEY")
        api_base = getattr(config, "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
        use_proxy = True
    if not api_key:
        print("[JUDGE] No remote fallback LLM key available", flush=True)
        return None

    model = getattr(config, "MINIMAX2_MODEL", None) or "MiniMax-Text-01"
    print(f"[JUDGE-FALLBACK] api_base={api_base} model={model}", flush=True)
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    is_minimax = "minimax" in api_base.lower()
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": "你是交易决策质量审核员。只输出 JSON,不要解释。"},
            {"role": "user", "content": user_msg},
        ],
        "temperature": 0.1,
        "max_tokens": 1500,
    }
    if is_minimax and model.upper() in {"MINIMAX-M3", "MINIMAX-M2.7", "MINIMAX-M2.7-HIGHSPEED"}:
        body["thinking"] = {"type": "disabled"}
    elif not is_minimax:
        body["response_format"] = {"type": "json_object"}
    try:
        kwargs = {"headers": headers, "json": body, "timeout": 30}
        if use_proxy:
            kwargs["proxies"] = {
                "http": os.environ.get("HTTP_PROXY", ""),
                "https": os.environ.get("HTTPS_PROXY", ""),
            }
        r = requests.post(f"{api_base}/chat/completions", **kwargs)
        r.raise_for_status()
        text = r.json()["choices"][0]["message"]["content"]
        print(f"[JUDGE-FALLBACK] raw len={len(text)} first200={text[:200]!r}", flush=True)
        return _parse_judge_json(text, source="remote")
    except Exception as e:
        print(f"[JUDGE-FALLBACK] FAIL: {type(e).__name__}: {e}", flush=True)
        return None


def _parse_judge_json(text: str, source: str = "") -> dict | None:
    """从 LLM 输出中稳健提取 JSON 对象. 返回 None 表示无法解析."""
    import re
    if not text:
        return None
    # 1) 剥 <think>...</think> 块
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    text = text.strip()
    # 2) 剥 markdown ``` 包裹
    if text.startswith("```"):
        parts = text.split("```", 2)
        if len(parts) >= 2:
            text = parts[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip().rstrip("`")
    # 3) 找第一个 {...} 块
    if not text.startswith("{"):
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            text = text[start:end+1]
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            # 兜底字段：确保 8 字段都有
            return {
                "consistency": parsed.get("consistency", "ok"),
                "consistency_reason": parsed.get("consistency_reason", ""),
                "asset_nature_correct": parsed.get("asset_nature_correct", True),
                "key_logic_chain_broken": parsed.get("key_logic_chain_broken", False),
                "self_contradiction": parsed.get("self_contradiction", "无"),
                "skip_trade_rationale": parsed.get("skip_trade_rationale", ""),
                "severity": int(parsed.get("severity", 0) or 0),
                "needs_human_review": bool(parsed.get("needs_human_review", False)),
                "_source": source,
            }
        return None
    except Exception as e:
        print(f"[JUDGE] JSON parse FAIL ({source}): {e}", flush=True)
        return None
        # 2) 剥 markdown ```json 包裹
        if text.startswith("```"):
            text = text.split("```", 2)[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip().rstrip("`")
        # 3) 强健 JSON 解析：找第一个 {...} 块
        if not text.startswith("{"):
            start = text.find("{")
            end = text.rfind("}")
            if start >= 0 and end > start:
                text = text[start:end+1]
        return json.loads(text)


def process_one(decision_id: int, conn: sqlite3.Connection) -> bool:
    """校验一条决策. 成功返回 True."""
    row = conn.execute(
        """SELECT ad.id, ad.target_asset, ad.suggested_action, ad.evidence_confidence,
                  ad.reasoning, ad.reasoning_path, ad.news_id, ad.structured_check,
                  rn.content as news_content
           FROM ai_decisions ad
           LEFT JOIN raw_news rn ON rn.id = ad.news_id
           WHERE ad.id = ?""",
        (decision_id,),
    ).fetchone()
    if not row:
        return False

    # 已校验过则跳过(避免重复消耗 LLM)
    existing = row["structured_check"] or ""
    if existing.startswith('{"consistency"'):
        print(f"[JUDGE] #{decision_id} already checked, skip", flush=True)
        return False

    # 拼接 reasoning + reasoning_path(reasoning 是摘要, reasoning_path 是完整八段)
    full_reasoning = row["reasoning"] or ""
    if row["reasoning_path"]:
        full_reasoning = (full_reasoning + "\n\n【完整八段分析】\n" + row["reasoning_path"]) if full_reasoning else row["reasoning_path"]

    result = _call_judge_llm(
        news=row["news_content"] or "",
        target_asset=row["target_asset"] or "",
        action=row["suggested_action"] or "",
        confidence=row["evidence_confidence"] or 0.0,
        reasoning=full_reasoning,
    )
    if result is None:
        return False

    # 写回 structured_check 列(JSON)
    payload = json.dumps(result, ensure_ascii=False)
    conn.execute(
        "UPDATE ai_decisions SET structured_check = ? WHERE id = ?",
        (payload[:3000], decision_id),
    )
    conn.commit()
    severity = result.get("severity", 0)
    print(
        f"[JUDGE] #{decision_id} {row['target_asset']} {row['suggested_action']} "
        f"-> severity={severity} consistency={result.get('consistency')}",
        flush=True,
    )
    return True


def main_loop(batch_size: int = 5, sleep_sec: int = 5, max_iter: int = -1) -> None:
    """主循环: 扫未校验决策, 调 Judge."""
    print(f"[JUDGE] Worker started. batch={batch_size} sleep={sleep_sec}s", flush=True)
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    try:
        i = 0
        while max_iter < 0 or i < max_iter:
            i += 1
            # 找未校验的决策(structured_check 是旧版 PR-4 schema, 不含 consistency 字段)
            # reasoning 短(60-130 字)没关系,因为 reasoning_path 是完整八段
            rows = conn.execute(
                """SELECT id FROM ai_decisions
                   WHERE structured_check IS NOT NULL
                     AND length(structured_check) > 10
                     AND structured_check NOT LIKE '{"consistency":%'
                     AND (length(reasoning) > 30 OR length(reasoning_path) > 200)
                     AND created_at >= datetime('now', '-24 hours')
                   ORDER BY id DESC LIMIT ?""",
                (batch_size,),
            ).fetchall()
            if not rows:
                time.sleep(sleep_sec)
                continue
            for row in rows:
                try:
                    process_one(row["id"], conn)
                except Exception as e:
                    print(f"[JUDGE] #{row['id']} FAIL: {e}", flush=True)
                    traceback.print_exc()
                time.sleep(0.5)
    finally:
        conn.close()


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--batch", type=int, default=5)
    p.add_argument("--sleep", type=int, default=5)
    p.add_argument("--once", action="store_true", help="process one batch then exit")
    args = p.parse_args()
    main_loop(batch_size=args.batch, sleep_sec=args.sleep,
              max_iter=1 if args.once else -1)