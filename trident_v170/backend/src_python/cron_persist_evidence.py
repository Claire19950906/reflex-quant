#!/usr/bin/env python3
"""v276+ cron: 反例持久化 — 把 verify_all_pending 结果落成 supports / contradicts 边。

调度方: launchd (com.trident.causal-graph-persist-cron.plist) — 默认每 6h 跑一次,
错峰 (与 causal-graph-refresh-cron 错开 1h, 让 WAL 不拥塞)。

职责 (顺序):
  对每个 known_asset:
    1. verify_all_pending(asset)        — 规则核验, 0 LLM, ~50ms/节点
    2. persist_evidence_edges(asset, results) — 写 supports/contradicts 边

防御 confirmation bias:
  支持和反例都落成边 (绿色 supports / 红对抗日 contradicts),
  前端 stats bar 实时显示 "X 因素:5 支持 2 反例" → 用户立即识别非单边信念。

设计:
  - 不依赖 api daemon (直接调模块函数)
  - 单 asset 失败不阻塞其他 (try/except 隔离)
  - 0 个 active 节点的资产自动跳过
  - 异常时 JSON 行写到 stderr, launchd 记 err log
  - _with_retry 应对 WAL 锁竞争

为什么必须 cron 自动跑 (vs 手动):
  之前反例只写到 evidence JSON 字段, 前端看不到。
  现在 supports/contradicts 落成边 — 但 verify 必须定期跑才能刷新。
  API 端点 persist-evidence 是手动触发, 用户不点就永远不更新。
  cron 每 6h 自动跑一次, 保证反例持久化永远新鲜。
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback
from typing import Any, Dict, List

# 允许脚本独立跑
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)


LOG_DIR = "/Users/claire/.trident/log"
LOG_FILE = os.path.join(LOG_DIR, "cron_persist_evidence.log")


def _with_retry(fn, *, retries: int = 8, base_delay: float = 1.0):
    """重试封装 — 应对 api_server 持锁场景。

    策略: 指数退避 (1s, 2s, 4s, 8s, ... cap 8s) 总重试 8 次 ≈ 30s 兜底。
    """
    import sqlite3 as _sq
    delay = base_delay
    last_exc = None
    for attempt in range(retries):
        try:
            return fn()
        except _sq.OperationalError as e:
            last_exc = e
            err = str(e).lower()
            if "locked" not in err and "busy" not in err:
                raise
            if attempt == retries - 1:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 8.0)
    raise last_exc  # type: ignore[misc]


def _log(msg: str) -> None:
    import datetime as _dt
    ts = _dt.datetime.now().isoformat(sep=" ", timespec="seconds")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _persist_one(causal_graph, asset: str) -> Dict[str, Any]:
    """单资产: verify + persist。失败抛出, 由外层捕获。"""
    r = _with_retry(lambda: causal_graph.verify_all_pending(asset))
    results = (r or {}).get("results") or []
    if not results:
        return {
            "asset": asset,
            "verified": 0,
            "evidence_added": 0,
            "skipped": "no_results",
        }
    ev_r = _with_retry(lambda: causal_graph.persist_evidence_edges(asset, results))
    return {
        "asset": asset,
        "verified": len(results),
        "supports_added": ev_r.get("supports_added", 0),
        "contradicts_added": ev_r.get("contradicts_added", 0),
        "evidence_nodes_created": ev_r.get("evidence_nodes_created", 0),
    }


def main() -> int:
    t_start = time.monotonic()
    _log("=== persist-evidence cron start ===")
    steps: Dict[str, Any] = {}
    try:
        import causal_graph
        assets: List[str] = causal_graph.known_assets()
        _log(f"assets to process: {len(assets)} ({assets})")

        per_asset: List[Dict[str, Any]] = []
        for a in assets:
            t0 = time.monotonic()
            try:
                r = _persist_one(causal_graph, a)
                r["_elapsed_sec"] = round(time.monotonic() - t0, 2)
                r["_ok"] = True
                per_asset.append(r)
                _log(f"  {a}: ok ({r['_elapsed_sec']}s) sup={r.get('supports_added',0)} con={r.get('contradicts_added',0)}")
            except Exception as exc:
                per_asset.append({
                    "asset": a,
                    "_ok": False,
                    "_elapsed_sec": round(time.monotonic() - t0, 2),
                    "error": f"{type(exc).__name__}: {str(exc)[:200]}",
                    "trace": traceback.format_exc(limit=2),
                })
                _log(f"  {a}: FAIL {type(exc).__name__}: {str(exc)[:80]}")

        elapsed = round(time.monotonic() - t_start, 2)
        ok = sum(1 for x in per_asset if x.get("_ok"))
        out = {
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
            "elapsed_sec": elapsed,
            "ok": ok == len(per_asset),
            "asset_total": len(per_asset),
            "asset_ok": ok,
            "asset_fail": len(per_asset) - ok,
            "total_supports": sum(x.get("supports_added", 0) for x in per_asset if x.get("_ok")),
            "total_contradicts": sum(x.get("contradicts_added", 0) for x in per_asset if x.get("_ok")),
            "per_asset": per_asset,
        }
        _log(f"RESULT: {json.dumps(out, ensure_ascii=False)}")
        _log(f"=== persist-evidence cron done ({elapsed}s, {ok}/{len(per_asset)} ok) ===")
        return 0
    except Exception as exc:
        out = {
            "ok": False,
            "error": f"{type(exc).__name__}: {str(exc)[:200]}",
            "trace": traceback.format_exc(limit=4),
            "elapsed_sec": round(time.monotonic() - t_start, 2),
        }
        _log(f"ERROR: {json.dumps(out, ensure_ascii=False)}")
        return 0


if __name__ == "__main__":
    sys.exit(main())