#!/usr/bin/env python3
"""contract_diff.py — 对拍 api_server(8000) 与 bridge(8100) 的同名端点

route_merge_audit.py 只看源码相似度，回答不了「删掉一侧，返回会变成什么样」。
本脚本对同一条路径同时请求两个端口，比较：
  * HTTP 状态码
  * 顶层字段集合（多了什么、少了什么）
  * 每个字段的类型（int/str/list/null 是否一致）
  * 列表元素字段集合（取第一条）
  * 响应体大小、耗时

只读：只发 GET，不写任何数据、不改服务状态。产物不落库，直接打印。

用法：
  python3 backend/scripts/contract_diff.py                       # 只跑给定默认集
  python3 backend/scripts/contract_diff.py --all                 # 跑全部重复端点
  python3 backend/scripts/contract_diff.py --path /api/market/movers
  python3 backend/scripts/contract_diff.py --json

注意：端口上跑的是**当前内存里的代码**，不是磁盘上的文件。若某个服务重启过而另一个没有，
差异可能来自这个不对称，而不是设计分歧。
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Tuple

API_BASE = "http://127.0.0.1:8000"
BRIDGE_BASE = "http://127.0.0.1:8100"
# 重型查询（因果图/影响面/日历）20s 量不出来，用 --timeout 抬高再对拍
TIMEOUT = 20.0


def _set_timeout(seconds: float) -> None:
    """抬高单次请求超时 —— 因果图/影响面/日历这类重查询 20s 量不出来。"""
    global TIMEOUT
    TIMEOUT = float(seconds)

# 默认挑几条代表不同"分歧类型"的，避免一上来就把 SSE / 重计算端点全打一遍
DEFAULT_PATHS = [
    "/api/market/movers",
    "/api/market/prices",
    "/api/health",
    "/api/events/today",
    "/api/review/snapshot",
    "/api/crowd/ranking",
    "/api/trading/signals",
]


def fetch(base: str, path: str) -> Dict[str, Any]:
    url = base + path
    t0 = time.monotonic()
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read()
            elapsed = time.monotonic() - t0
            body = json.loads(raw.decode("utf-8", "replace")) if raw else None
            return {"ok": True, "status": resp.status, "ms": round(elapsed * 1000),
                    "bytes": len(raw), "body": body}
    except urllib.error.HTTPError as e:
        return {"ok": False, "status": e.code, "ms": round((time.monotonic() - t0) * 1000),
                "bytes": 0, "body": None, "error": f"HTTP {e.code}"}
    except Exception as e:  # 连接拒绝、超时、非 JSON
        return {"ok": False, "status": None, "ms": round((time.monotonic() - t0) * 1000),
                "bytes": 0, "body": None, "error": f"{type(e).__name__}: {str(e)[:60]}"}


def _shape(v: Any, depth: int = 0) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, (int, float)):
        return "num"
    if isinstance(v, str):
        return "str"
    if isinstance(v, list):
        if not v or depth >= 2:
            return "list"
        return "list[" + _shape(v[0], depth + 1) + "]"
    if isinstance(v, dict):
        return "dict"
    return type(v).__name__


def diff_body(a: Any, b: Any) -> Dict[str, List[str]]:
    out = {"only_api": [], "only_bridge": [], "type_mismatch": []}
    if not isinstance(a, dict) or not isinstance(b, dict):
        if _shape(a) != _shape(b):
            out["type_mismatch"].append(f"顶层类型 {_shape(a)} vs {_shape(b)}")
        return out
    ka, kb = set(a), set(b)
    out["only_api"] = sorted(ka - kb)
    out["only_bridge"] = sorted(kb - ka)
    for k in sorted(ka & kb):
        sa, sb = _shape(a[k]), _shape(b[k])
        if sa != sb:
            out["type_mismatch"].append(f"{k}: {sa} vs {sb}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="8000/8100 同名端点对拍")
    ap.add_argument("--all", action="store_true", help="跑 route_merge_audit 列出的全部重复端点")
    ap.add_argument("--path", action="append", default=[], help="指定路径，可多次")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--timeout", type=float, default=TIMEOUT,
                    help=f"单次请求超时秒数（默认 {TIMEOUT:g}；重查询用 60+）")
    args = ap.parse_args()
    _set_timeout(float(args.timeout))

    paths = list(args.path)
    if args.all:
        from route_merge_audit import audit  # 同目录，复用同一份清单
        paths += [r["path"] for r in audit()]
    paths = paths or DEFAULT_PATHS

    results = []
    for p in paths:
        a, b = fetch(API_BASE, p), fetch(BRIDGE_BASE, p)
        row = {
            "path": p,
            "status": [a.get("status"), b.get("status")],
            "ms": [a.get("ms"), b.get("ms")],
            "bytes": [a.get("bytes"), b.get("bytes")],
            "diff": diff_body(a.get("body"), b.get("body")),
            "api_error": a.get("error"),
            "bridge_error": b.get("error"),
        }
        results.append(row)

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return 0

    for r in results:
        print(f"\n{'=' * 96}\n{r['path']}")
        print(f"  8000: {r['status'][0]}  {r['ms'][0]}ms  {r['bytes'][0]}B"
              + (f"  !{r['api_error']}" if r['api_error'] else ""))
        print(f"  8100: {r['status'][1]}  {r['ms'][1]}ms  {r['bytes'][1]}B"
              + (f"  !{r['bridge_error']}" if r['bridge_error'] else ""))
        d = r["diff"]
        if d["only_api"]:
            print(f"  只有 8000 有字段: {', '.join(d['only_api'])}")
        if d["only_bridge"]:
            print(f"  只有 8100 有字段: {', '.join(d['only_bridge'])}")
        if d["type_mismatch"]:
            print(f"  类型不同: {'; '.join(d['type_mismatch'])}")
        if not any(d.values()):
            print("  字段集合与类型一致（值可能不同，不做逐值比较）")

    print("\n说明：只对齐「结构」，不比较具体数值。数值差异（如 movers 的品种口径）"
          "要靠抓同一时刻两份响应单独判。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
