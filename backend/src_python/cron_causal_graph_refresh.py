#!/usr/bin/env python3
"""v276+ cron: 因果图谱每 6h 自动生长 — 防御 confirmation bias, 让图谱像神经元一样活络。

调度方: launchd (com.trident.causal-graph-refresh-cron.plist) — 默认每 6h 跑一次。
入口: run_causal_graph_refresh.sh 通过 python3 cron_causal_graph_refresh.py 调起。

职责 (顺序):
  1. link_cross_asset_neurons()       — 建/强化 16 对宏观传导神经
  2. seed_from_causal_chains()        — 把新因果链种进图谱(免 LLM)
  3. verify_all_pending + persist     — 每个品种 verify 后写 supports/contradicts 边

设计:
  - 不依赖 api daemon (直接调模块函数)
  - 失败不 panic (写日志后退出 0)
  - 异常时 JSON 行写到 stderr, launchd 记 err log

为什么必须 cron 自动跑:
  之前图谱靠人手点 POST /api/review/causal-graph/reason, 5 天才更新一次。
  XAU/XAG/WTI 等非 BTC/ETH/SOL 品类从来没成功入图, 因为 LLM 慢且失败率高。
  现在 cron 每 6h 跑一次, 且先走 seed_from_causal_chains(免 LLM),
  保证图谱任何时候都至少有「资产节点 + 宏观驱动节点 + 跨资产神经」三层结构。
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback
from typing import Any, Dict

# 允许脚本独立跑
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)


LOG_DIR = "/Users/claire/.trident/log"
LOG_FILE = os.path.join(LOG_DIR, "cron_causal_graph_refresh.log")


def _with_retry(fn, *, retries: int = 8, base_delay: float = 1.0):
    """重试封装 — 应对 api_server 持锁场景。

    策略: 指数退避 (1s, 2s, 4s, 8s, ... cap 8s) 总重试 8 次 ≈ 30s 兜底。
    WAL 下单 writer; api_server 长事务可能持锁 5-10s, 需要等。
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
    # 不应到这里
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


def _safe_step(name: str, fn) -> Dict[str, Any]:
    """每个步骤单独 try — 一个挂了不阻塞其他。"""
    t0 = time.monotonic()
    try:
        r = fn()
        r["_step"] = name
        r["_elapsed_sec"] = round(time.monotonic() - t0, 2)
        r["_ok"] = True
        return r
    except Exception as exc:
        return {
            "_step": name,
            "_ok": False,
            "_elapsed_sec": round(time.monotonic() - t0, 2),
            "error": f"{type(exc).__name__}: {str(exc)[:200]}",
            "trace": traceback.format_exc(limit=3),
        }


def main() -> int:
    t_start = time.monotonic()
    _log("=== causal-graph refresh cron start ===")
    steps: Dict[str, Any] = {}

    try:
        # 延迟导入 (避免循环 + 失败快速定位)
        import causal_graph

        # Step 1: 跨资产神经 — 始终跑, 幂等 (cheap, ~10s)
        steps["link_cross_asset"] = _safe_step(
            "link_cross_asset",
            lambda: _with_retry(causal_graph.link_cross_asset_neurons),
        )

        # Step 2: 因果链种子 — 把 causal_chains 种入图谱 (免 LLM, 幂等)
        # 幂等设计: 重跑只会 update 已有节点,不会重复加边
        steps["seed_chains"] = _safe_step(
            "seed_chains",
            lambda: _with_retry(causal_graph.seed_from_causal_chains),
        )

        # Step 2.5: 跨资产神经权重滚动更新 — v276+ 用 abnormal_attributions 7d
        # 共现数据, 把硬编码 base_weight 替换成「数据驱动」值。
        # cheap (~3s, 纯 SQL UPDATE), 幂等 (UPDATE SET weight)。
        steps["update_weights"] = _safe_step(
            "update_weights",
            lambda: _with_retry(
                lambda: causal_graph.update_cross_asset_weights(
                    since_days=7, min_evidence=50,
                )
            ),
        )

        # 注意: verify_all_pending + persist_evidence 不在 cron 自动跑,因:
        #   1. 跑全 13 品种 verify 太慢 (~2 分钟), 易被 API 抢锁导致 30s retry 链
        #   2. 这两个操作改 evidence 字段, 与 history_review 路径有竞争
        # 改由 /api/review/causal-graph/persist-evidence 端点手动或定期 API 触发。
        # stats 端点已经能看到 verify 状态, 无需 cron 强推。
        steps["note"] = {
            "_ok": True,
            "skip_verify_in_cron": True,
            "why": "verify+persist 由 API 端点触发,避免与 api_server 写锁竞争",
        }

        assets = causal_graph.known_assets()
        elapsed = round(time.monotonic() - t_start, 2)
        out = {
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
            "elapsed_sec": elapsed,
            "ok": True,
            "asset_count": len(assets),
            "steps": steps,
        }
        _log(f"RESULT: {json.dumps(out, ensure_ascii=False)}")
        _log(f"=== causal-graph refresh cron done ({elapsed}s) ===")
        return 0
    except Exception as exc:
        out = {
            "ok": False,
            "error": f"{type(exc).__name__}: {str(exc)[:200]}",
            "trace": traceback.format_exc(limit=4),
            "elapsed_sec": round(time.monotonic() - t_start, 2),
        }
        _log(f"ERROR: {json.dumps(out, ensure_ascii=False)}")
        return 0  # 不 panic, 让 launchd 不重复拉起


if __name__ == "__main__":
    sys.exit(main())
