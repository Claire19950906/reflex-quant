#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A/B 实验：prompt 是否逼着模型把方向分写成 0（只跑不落库）。

背景
----
`engine/ai_worker.py` 主分析提示词里有一句「拿不准方向时给 HOLD + sentiment_score=0」。
实测 2026-11-16/17 的 516 条决策里 474 条（91.9%）sentiment_score=0，
而闸门要求 `|score|*100*乘子 >= 15` —— 也就是 92% 的样本在进闸门之前就被判死。

本脚本用**同一批新闻、同一套精简输出契约**，只改「不确定怎么表达」这一句，对比：

  臂 A（现状）：拿不准方向时给 HOLD + sentiment_score=0
  臂 B（候选）：HOLD 也必须给方向倾向，不确定用 |score| 小表示，不要用 0

然后用 1 分钟 K 线做 60 分钟事后结算，看两件事：
  1. 非零方向分占比（能不能把可排序样本从 42 拉起来）
  2. 方向符号的事后正确率（非零分到底有没有预测力）

约束
----
* 全程 `sqlite mode=ro`，**不写任何决策/统计表**（连 ai_call_stats 都不写，避免污染线上指标）
* 只做实验，不改 prompt、不改闸门、不重启服务

用法
----
    python3 backend/scripts/analysis_prompt_ab.py --n 16
    python3 backend/scripts/analysis_prompt_ab.py --n 16 --horizon 60 --json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DB_PATH = os.path.join(ROOT, "backend", "trident_event_bus.db")
SRC = os.path.join(ROOT, "backend", "src_python")

# 只有这些品种有 1 分钟线，才能做结算；WTI 只有日线，排除
SETTLE_ASSETS = ("BTC", "ETH", "SOL", "XAU")

ARMS = {
    "A_现状": (
        "拿不准方向时给 HOLD + sentiment_score=0，宁可观望不要瞎猜。"
    ),
    "B_候选": (
        "即使结论是 HOLD，也必须给出方向倾向：sentiment_score 表示方向强度，"
        "多头为正、空头为负；不确定时用「很小的非零值」表示（例如 ±0.05 ~ ±0.15），"
        "绝对不要用 0 表示不确定。同时填写 direction 与 direction_strength。"
    ),
}

OUT_SCHEMA = """只输出一个 JSON 对象，不要任何解释文字：
{"action":"BUY|SELL|HOLD","sentiment_score":<浮点数，-1~1，负=利空 正=利多>,
 "direction":"up|down|neutral","direction_strength":<0~1 浮点>,
 "reason":"<不超过 80 字，说明你凭什么这么判断>"}"""


# ────────────────────────────────────────────────────────── 数据读取（只读）
def _ro_conn() -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def pick_samples(conn: sqlite3.Connection, n: int) -> List[sqlite3.Row]:
    """取「有品种、有 1m 线、有新闻正文」的决策行，按时间倒序。

    用 ai_decisions 而不是 raw_news 作为入口：这样每一行都能对回系统真实判断，
    便于把实验结果和线上那条记录并排看（但实验不覆盖线上记录）。
    """
    rows = conn.execute(
        """
        SELECT d.id, d.news_id, d.target_asset, d.created_at, d.suggested_action,
               d.sentiment_score, d.analysis_degraded, d.analysis_degraded_reason,
               n.content
        FROM ai_decisions d JOIN raw_news n ON n.id = d.news_id
        WHERE d.target_asset IN ('BTC','ETH','SOL','XAU')
          AND n.content IS NOT NULL AND length(n.content) > 40
        ORDER BY d.id DESC
        LIMIT 400
        """
    ).fetchall()
    return rows[:n]


def kline_close(conn: sqlite3.Connection, symbol: str, ts_ms: int,
                tol_min: int = 10) -> Optional[float]:
    """取 ts 之后最近一根 1m 收盘价；超过 tol_min 分钟视为缺口。"""
    row = conn.execute(
        """SELECT close FROM kline_bars
           WHERE symbol=? AND timeframe='1m' AND ts>=?
           ORDER BY ts LIMIT 1""",
        (symbol, ts_ms),
    ).fetchone()
    if not row:
        return None
    got = conn.execute(
        "SELECT ts FROM kline_bars WHERE symbol=? AND timeframe='1m' AND ts>=? ORDER BY ts LIMIT 1",
        (symbol, ts_ms),
    ).fetchone()
    if got and (got["ts"] - ts_ms) > tol_min * 60_000:
        return None
    return float(row["close"])


def forward_move(conn: sqlite3.Connection, symbol: str, ts_ms: int,
                 horizon_min: int) -> Optional[float]:
    """ts → ts+horizon 的涨跌幅（%）。"""
    p0 = kline_close(conn, symbol, ts_ms)
    if p0 is None or p0 <= 0:
        return None
    p1 = kline_close(conn, symbol, ts_ms + horizon_min * 60_000)
    if p1 is None:
        return None
    return (p1 - p0) / p0 * 100.0


def parse_created_at(s: str) -> Optional[int]:
    """created_at 形如 2026-11-17T03:05:47.247+08:00 → epoch ms。"""
    if not s:
        return None
    txt = str(s).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(txt)
    except ValueError:
        return None
    return int(dt.timestamp() * 1000)


# ─────────────────────────────────────────────────────── 模型调用（不埋点）
def load_model() -> Dict[str, Any]:
    sys.path.insert(0, SRC)
    import config # noqa: WPS433  (延迟导入：只有真正发请求时才需要)

    m = config.pick_model_for_purpose("main")
    return {
        "id": m.get("id"),
        "model": m.get("request_model") or m.get("id"),
        "api_base": m.get("api_base"),
        "api_key": m.get("api_key"),
        "extra_body": dict(m.get("extra_body") or {}),
    }


def call_model(model: Dict[str, Any], prompt: str, *, max_tokens: int = 900,
               timeout: float = 90.0) -> Tuple[str, str]:
    """直接打 HTTP，不走 chat() 包装 —— 因为 chat() 会写 ai_call_stats。"""
    import urllib.request

    body: Dict[str, Any] = {
        "model": model["model"],
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
        "max_tokens": max_tokens,
    }
    for k, v in (model.get("extra_body") or {}).items():
        if k in ("max_tokens", "temperature", "messages", "model", "stream"):
            continue
        body[k] = v
    req = urllib.request.Request(
        f"{model['api_base']}/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {model['api_key']}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as e:  # 网络/超时/配额：返回空串，由上层标记
        return "", f"{type(e).__name__}: {str(e)[:80]}"
    try:
        text = payload["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        return "", f"bad_payload: {json.dumps(payload, ensure_ascii=False)[:80]}"
    elapsed = time.time() - t0
    _ = elapsed
    return text, ""


def extract_json(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def build_prompt(content: str, asset: str, rule: str) -> str:
    body = str(content or "").strip()[:900]
    return f"""你是一名加密货币/贵金属量化交易员的新闻分析助手。
目标品种：{asset}
新闻正文：
{body}

判断要求：
{rule}

{OUT_SCHEMA}"""


# ─────────────────────────────────────────────────────────────── 主流程
def run(n: int, horizon: int, out_json: bool) -> int:
    conn = _ro_conn()
    rows = pick_samples(conn, n)
    if not rows:
        print("没有可用样本（需要 target_asset ∈ BTC/ETH/SOL/XAU 且有正文）")
        return 1
    model = load_model()

    jobs: List[Tuple[str, sqlite3.Row]] = [(k, r) for r in rows for k in ARMS]

    def work(job: Tuple[str, sqlite3.Row]) -> Dict[str, Any]:
        arm, row = job
        text, err = call_model(model, build_prompt(row["content"], row["target_asset"], ARMS[arm]))
        parsed = extract_json(text)
        return {"arm": arm, "row": row, "parsed": parsed, "err": err, "raw": text[:200]}

    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(work, jobs))

    # ── 结算：60 分钟事后位移 + 判定
    table: List[Dict[str, Any]] = []
    for r in results:
        row = r["row"]
        ts_ms = parse_created_at(row["created_at"])
        mv = forward_move(conn, row["target_asset"], ts_ms, horizon) if ts_ms else None
        p = r["parsed"] or {}
        score = p.get("sentiment_score")
        try:
            score = float(score)
        except (TypeError, ValueError):
            score = None
        table.append({
            "arm": r["arm"],
            "id": row["id"],
            "asset": row["target_asset"],
            "action": (p.get("action") or "").upper(),
            "score": score,
            "direction": p.get("direction"),
            "strength": p.get("direction_strength"),
            "live_action": row["suggested_action"],
            "live_score": row["sentiment_score"],
            "fwd_pct": None if mv is None else round(mv, 4),
            "err": r["err"] or ("" if r["parsed"] else "unparsable"),
        })

    def summarize(arm: str) -> Dict[str, Any]:
        picked = [t for t in table if t["arm"] == arm]
        ok_rows = [t for t in picked if t["score"] is not None]
        nonzero = [t for t in ok_rows if abs(t["score"]) > 1e-9]
        settled = [t for t in nonzero if t["fwd_pct"] is not None and abs(t["fwd_pct"]) >= 0.05]
        hits = [t for t in settled if (t["score"] > 0) == (t["fwd_pct"] > 0)]
        mean_abs = (sum(abs(t["score"]) for t in ok_rows) / len(ok_rows)) if ok_rows else 0.0
        return {
            "arm": arm,
            "n": len(picked),
            "parsed": len(ok_rows),
            "nonzero": len(nonzero),
            "nonzero_rate": (len(nonzero) / len(ok_rows) * 100) if ok_rows else 0.0,
            "mean_abs_score": round(mean_abs, 3),
            "settleable": len(settled),
            "sign_hit_rate": (len(hits) / len(settled) * 100) if settled else 0.0,
            "errors": sum(1 for t in picked if t["err"]),
        }

    summary = [summarize(a) for a in ARMS]
    result = {
        "horizon_min": horizon,
        "model": model["model"],
        "summary": summary,
        "rows": table,
    }

    if out_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    print(f"\n== prompt A/B 实验 == 模型={model['model']}  事后窗口={horizon} 分钟  样本={len(rows)} 条×2 臂")
    print(f"\n{'臂':<10}{'解析成功':>8}{'非零分':>8}{'非零率':>9}{'平均|score|':>12}{'可结算':>7}{'符号命中':>9}{'失败':>6}")
    for s in summary:
        print(f"{s['arm']:<10}{s['parsed']:>8}{s['nonzero']:>8}{s['nonzero_rate']:>8.1f}%"
              f"{s['mean_abs_score']:>12.3f}{s['settleable']:>7}{s['sign_hit_rate']:>8.1f}%{s['errors']:>6}")

    print("\n逐条对照（live = 线上真实记录，A/B = 同一条新闻的两次实验输出）")
    print(f"{'id':>6}{'asset':>7}{'臂':<10}{'act':>5}{'score':>8}{'fwd%':>9}{'live_act':>9}{'live_score':>11}")
    for t in table:
        score = "" if t["score"] is None else f"{t['score']:+.3f}"
        fwd = "" if t["fwd_pct"] is None else f"{t['fwd_pct']:+.4f}"
        print(f"{t['id']:>6}{t['asset']:>7}{t['arm']:<10}{t['action']:>5}{score:>8}{fwd:>9}"
              f"{t['live_action']:>9}{t['live_score']:>11}")

    errs = [t for t in table if t["err"]]
    if errs:
        print(f"\n失败 {len(errs)} 条：", sorted({t["err"] for t in errs})[:5])
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="prompt A/B：不确定是否必须写成 0")
    ap.add_argument("--n", type=int, default=16, help="样本条数（每条跑两臂）")
    ap.add_argument("--horizon", type=int, default=60, help="事后结算窗口（分钟）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    a = ap.parse_args()
    return run(a.n, a.horizon, a.json)


if __name__ == "__main__":
    sys.exit(main())
