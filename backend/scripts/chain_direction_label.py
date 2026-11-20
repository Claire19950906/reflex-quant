#!/usr/bin/env python3
"""v401 P0-3 · 因果链方向标注（LLM 语义，按资产判断）

为什么不用规则法：
    `causal_chain_evaluator._infer_direction` 的 docstring 记着 v322 的实测结论 ——
    用机制词表（推升/买盘/利空）补方向，628 条链里补了 58 条，回测方向命中率从
    50.0% 掉到 **41.9%（低于随机）**。原因是那些"利多/利空"是**相对别的资产**说的：
    "地缘冲突（利空原油）→ 避险资金流入黄金（利多）" 里"利空"指的不是黄金。
    → 方向必须"对**这个资产**"判断，只有语义模型能做；再叠加 v302 的教训，
      **effect 里的已实现收益/命中率一律不许当证据**（那是事后结论，是泄漏）。

本脚本：把一条链的 (asset, name, cause, effect) 交给模型，只问
"事件发生后，**该资产**价格的**预期**方向是 UP / DOWN / BOTH"，要求给置信度。
置信度 < 阈值、或模型判 BOTH、或模型说是流程缺陷类链 → 保持 BOTH（不硬猜）。

用法：
    python3 scripts/chain_direction_label.py --limit 20          # 小样本 dry-run
    python3 scripts/chain_direction_label.py --apply             # 全量落库
    python3 scripts/chain_direction_label.py --db /tmp/x.db --apply
    python3 scripts/chain_direction_label.py --verify-sample 30  # 标注后自一致性抽查
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(os.path.dirname(_HERE), "src_python")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

AUDIT_DIR = os.path.join(os.path.dirname(_HERE), "runtime", "chain_cleanup")
OVERLAY = os.path.join(os.path.dirname(_HERE), "runtime", "chain_direction", "llm_labels.json")

SYSTEM = (
    "你是宏观/商品/加密市场的因果链标注器。任务只有一个：判断某条因果关系"
    "对**指定资产**的价格**预期**方向。\n"
    "铁律：\n"
    "1. 只对 prompt 里给出的那个资产判断，不要考虑链文本提到的其他资产；\n"
    "2. effect 里若出现已实现收益、WIN/LOSS、命中率、% 数字，一律忽略（那是事后结论）；\n"
    "3. 若这条链其实描述的是流程缺陷（过度交易、数据缺失、执行失败、信号冗余、"
    "覆盖度告警、重复入场），方向必须是 BOTH；\n"
    "4. 看不出来就 BOTH，置信度给低；不确定时**不许**编方向；\n"
    "5. 只输出 JSON 数组，不要解释、不要代码块标记。"
)

TEMPLATE = """对下列每条链，判断事件发生并沿链传导后，**该资产**价格的预期方向。

链列表（JSON）：
{payload}

输出格式（严格 JSON 数组，逐条给结论，id 必须原样返回）：
[{{"id": 12, "direction": "UP|DOWN|BOTH", "confidence": 0.0-1.0, "why": "不超过15字"}}]
"""

# effect 里这些词是事后结论的痕迹，喂给模型前先抹掉，避免它顺着结果编方向
_LEAK_PAT = re.compile(
    r"(WIN|LOSS|HIT|MISS)\s*[+-]?\d+(\.\d+)?%?"
    r"|[+-]\d+(\.\d+)?%"
    r"|#\d+(?:-\d+)?"
    r"|命中率\s*\d+(\.\d+)?%?"
    r"|已实现[^，。；,;]*"
    r"|实际(涨|跌)[^，。；,;]*",
    re.I,
)


def _strip_leak(text: str) -> str:
    return _LEAK_PAT.sub("", text or "").strip()


def _db_path(explicit: str | None) -> str:
    if explicit:
        return explicit
    import db
    return db.get_db_path()


def _targets(conn: sqlite3.Connection, limit: int | None) -> list[dict]:
    rows = conn.execute(
        "SELECT id, name, cause, effect, asset, expected_direction"
        " FROM causal_chains WHERE enabled=1 AND asset<>'UNKNOWN'"
        " AND asset IS NOT NULL"
        " ORDER BY id"
    ).fetchall()
    out = [
        {
            "id": r["id"],
            "asset": r["asset"],
            "name": _strip_leak(r["name"] or "")[:120],
            "cause": _strip_leak(r["cause"] or "")[:220],
            "effect": _strip_leak(r["effect"] or "")[:220],
            "direction_old": (r["expected_direction"] or "BOTH").upper(),
        }
        for r in rows
    ]
    if limit:
        out = out[: int(limit)]
    return out


def _parse_json_array(text: str) -> list[dict]:
    if not text:
        return []
    s = text.strip()
    s = re.sub(r"^```(?:json)?|```$", "", s, flags=re.I | re.M).strip()
    start = s.find("[")
    end = s.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        data = json.loads(s[start:end + 1])
    except (ValueError, TypeError):
        return []
    return data if isinstance(data, list) else []


def label_batch(chat, items: list[dict]) -> dict[int, dict]:
    payload = json.dumps(
        [{"id": i["id"], "asset": i["asset"], "name": i["name"],
          "cause": i["cause"], "effect": i["effect"]} for i in items],
        ensure_ascii=False,
    )
    raw = chat(TEMPLATE.format(payload=payload), purpose="sql", system=SYSTEM,
               max_tokens=900, temperature=0.0, call_site="chain_direction_label")
    out: dict[int, dict] = {}
    for d in _parse_json_array(raw):
        try:
            cid = int(d.get("id"))
        except (TypeError, ValueError):
            continue
        d_ = str(d.get("direction") or "").upper()
        if d_ not in ("UP", "DOWN", "BOTH"):
            continue
        try:
            conf = float(d.get("confidence") or 0.0)
        except (TypeError, ValueError):
            conf = 0.0
        out[cid] = {"direction": d_, "confidence": conf,
                    "why": str(d.get("why") or "")[:40]}
    return out


def save_overlay(decisions: list[dict]) -> str:
    """把标注写成覆盖层 —— derive_strategy_columns 每次重派生都会重算规则方向，
    没有这层，每小时一次的 causal_weight_feedback 会把标注静默抹掉。"""
    os.makedirs(os.path.dirname(OVERLAY), exist_ok=True)
    labels: dict = {}
    try:
        with open(OVERLAY, encoding="utf-8") as fh:
            labels = dict((json.load(fh) or {}).get("labels") or {})
    except (OSError, ValueError):
        labels = {}
    for d in decisions:
        labels[str(d["id"])] = d["direction_new"]
    with open(OVERLAY, "w", encoding="utf-8") as fh:
        json.dump({"updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "count": len(labels), "labels": labels}, fh,
                  ensure_ascii=False, indent=1, sort_keys=True)
    # v406 P5: 同时写 DB 镜像 —— 文件只放在 runtime 目录，换机/清目录就丢，
    # 而且"按方向查链"没法在 SQL 侧审计。写失败不影响 JSON（JSON 仍是写入主路径）。
    try:
        import causal_chain_evaluator as _ev
        import db as _db
        _c = sqlite3.connect(str(_db.get_db_path()), timeout=120)
        _c.execute("PRAGMA busy_timeout=120000")
        try:
            _n = _ev.sync_overlay_to_db(_c, labels, source="llm-label",
                                        note="chain_direction_label.py")
            print("[OVERLAY] DB 镜像写入 %d 条" % _n, flush=True)
        finally:
            _c.close()
    except Exception as _exc:
        print("[OVERLAY] DB 镜像写入失败(保留 JSON): %s: %s"
              % (type(_exc).__name__, str(_exc)[:80]), flush=True)
    return OVERLAY


def run(args) -> int:
    from engine.ai_worker import chat

    path = _db_path(args.db)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    items = _targets(conn, args.limit)
    print(f"库: {path} · 待标注 {len(items)} 条（enabled=1 且有 asset）")

    os.makedirs(AUDIT_DIR, exist_ok=True)
    audit_path = args.audit_file or os.path.join(
        AUDIT_DIR, f"direction-{time.strftime('%Y%m%d-%H%M%S')}.jsonl")
    done_ids: set[int] = set()
    if args.audit_file and os.path.exists(audit_path):
        for line in open(audit_path, encoding="utf-8"):
            try:
                done_ids.add(int(json.loads(line)["id"]))
            except (ValueError, KeyError, TypeError):
                continue
    if done_ids:
        items = [i for i in items if i["id"] not in done_ids]
        print(f"续跑：已有 {len(done_ids)} 条结论，本次待标注 {len(items)} 条")

    batch = max(1, int(args.batch))
    decisions: list[dict] = []

    # 单次调用实测 ~55s（MiniMax 池），串行 47 批要 40 分钟 —— 并发跑。
    from concurrent.futures import ThreadPoolExecutor, as_completed
    chunks = [items[i:i + batch] for i in range(0, len(items), batch)]
    results: dict[int, dict] = {}

    def _work(idx_chunk):
        idx, chunk = idx_chunk
        return idx, label_batch(chat, chunk)

    done = 0
    fh = open(audit_path, "a", encoding="utf-8")
    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        futs = {ex.submit(_work, (i, c)): i for i, c in enumerate(chunks)}
        for fut in as_completed(futs):
            try:
                idx, res = fut.result()
            except Exception as e:                      # 单批失败不拖垮全局
                print(f"  批 {futs[fut]} 失败: {type(e).__name__}: {e}", flush=True)
                continue
            results[idx] = res
            for it in chunks[idx]:
                got = res.get(it["id"])
                if not got:
                    continue
                conf = got["confidence"]
                new = got["direction"] if (got["direction"] != "BOTH"
                                           and got["confidence"] >= args.min_conf
                                           ) else "BOTH"
                rec = {**it, "direction_new": new, "confidence": conf,
                       "why": got["why"]}
                decisions.append(rec)
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            fh.flush()
            done += 1
            print(f"  完成 {done}/{len(chunks)} 批（累计 {len(decisions)} 条）", flush=True)

    fh.close()

    conn.close()

    up = sum(1 for d in decisions if d["direction_new"] == "UP")
    down = sum(1 for d in decisions if d["direction_new"] == "DOWN")
    both = len(decisions) - up - down
    flips = [d for d in decisions if d["direction_old"] in ("UP", "DOWN")
             and d["direction_new"] != d["direction_old"]]
    print(f"标注结果: UP {up} · DOWN {down} · BOTH {both}"
          f" · 共 {len(decisions)}/{len(items)} 条有结论")
    print(f"与旧标注冲突 {len(flips)} 条（旧 UP/DOWN 被改成别的）")
    print(f"审计: {audit_path}")

    if not args.apply:
        print("dry-run：未写库（加 --apply 落库）")
        return 0

    from causal_chain_evaluator import resnapshot_predictions
    conn = sqlite3.connect(path, timeout=120)
    conn.execute("PRAGMA busy_timeout=120000")
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    from causal_chain_evaluator import has_column
    has_src = has_column(conn, "causal_chains", "direction_source")
    sql = ("UPDATE causal_chains SET expected_direction=?, direction_source='llm',"
           " updated_at=? WHERE id=?") if has_src else (
           "UPDATE causal_chains SET expected_direction=?, updated_at=? WHERE id=?")
    changed = 0
    for d in decisions:
        if d["direction_new"] == d["direction_old"]:
            continue
        if has_src:
            conn.execute(sql, (d["direction_new"], now, d["id"]))
        else:
            conn.execute(sql, (d["direction_new"], now, d["id"]))
        changed += 1
    conn.commit()
    snap = resnapshot_predictions(conn)
    conn.commit()
    conn.close()
    overlay = save_overlay(decisions)
    print(f"已落库: expected_direction 更新 {changed} 条 · 预测快照 {snap}")
    print(f"覆盖层: {overlay}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--batch", type=int, default=8)
    ap.add_argument("--min-conf", type=float, default=0.7)
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--audit-file", default=None, help="指定审计文件（续跑用）")
    ap.add_argument("--retries", type=int, default=2, help="失败批次的串行重试轮数")
    args = ap.parse_args(argv)
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
