#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""门卫反思持久化 worker —— v186 第二层反思基础设施。

职责:
  - 定期调用 gate_reflection.gate_reflection() 计算各品种门卫精度/召回
  - 把结果写入 gate_reflections 表（asset / cycle_ts / precision / recall / forward_cnt / reverse_cnt / details_json）
  - 失败时静默（不阻断主流程）

执行模式:
  - main() 一次性跑全品种(默认 BTC/ETH/SOL/XAU/WTI)
  - run_once(asset=None) → 跑某个品种
  - daemon_loop() → 每 N 分钟轮询(N 由 env GATE_REFLECT_INTERVAL_S 控制, 默认 1800s=30min)

设计:
  - 反向推的 abnormals 列表通过 api_server._detect_5m_abnormals 拿(避免本模块直接依赖 quote_sources)
  - 反向推窗口: 1800s(同 gate_reflection.REVERSE_WINDOW_S)
  - 单次失败 → print + continue,不抛异常
"""
from __future__ import annotations

import json, time, os
from typing import Any, Dict, List, Optional

import db
import gate_reflection as _gr

ASSETS_DEFAULT: List[str] = ["BTC", "ETH", "SOL", "XAU", "WTI"]
CYCLE_DAYS_DEFAULT: int = 3

_INSERT_SQL = """
    INSERT OR REPLACE INTO gate_reflections_cycle
        (asset, cycle_ts, cycle_iso, days, precision, pass_winrate,
         n_judged, n_decided, n_win, n_loss,
         n_abnormal, n_covered, n_miss, recall,
         forward_json, reverse_json, summary_json)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _detect_abnormals_safe(asset: str) -> Optional[List[Dict[str, Any]]]:
    """尝试从 api_server 拿某品种 5m 异动点;失败 → None(纯 forward 模式)。"""
    try:
        from api_server import _detect_5m_abnormals
        return _detect_5m_abnormals(asset) or None
    except Exception as exc:
        print(f"[GATE-REFLECT] 拿 abnormals 失败: {type(exc).__name__}: {exc}", flush=True)
        return None


def _persist(asset: str, result: Dict[str, Any], cycle_ts: int) -> bool:
    """把一次反思结果落库;失败静默。"""
    try:
        s = result.get("summary") or {}
        conn = db.get_connection()
        try:
            conn.execute(
                _INSERT_SQL,
                (
                    asset, cycle_ts,
                    time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(cycle_ts)),
                    result.get("days"),
                    s.get("precision"), s.get("pass_winrate"),
                    s.get("n_judged"), s.get("n_decided"),
                    s.get("n_win"), s.get("n_loss"),
                    s.get("n_abnormal"), s.get("n_covered"),
                    s.get("n_miss"), s.get("recall"),
                    json.dumps(result.get("forward") or [], ensure_ascii=False),
                    json.dumps(result.get("reverse") or [], ensure_ascii=False),
                    json.dumps(s, ensure_ascii=False),
                ),
            )
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception as exc:
        print(f"[GATE-REFLECT] persist failed asset={asset}: {type(exc).__name__}: {exc}",
              flush=True)
        return False


def run_once(asset: str, days: int = CYCLE_DAYS_DEFAULT,
             abnormals: Optional[List[Dict[str, Any]]] = None) -> Optional[Dict[str, Any]]:
    """跑一次某品种门卫反思 → 落库 → 返回 summary(失败 → None)。"""
    try:
        result = _gr.gate_reflection(asset=asset, days=days, abnormals=abnormals)
    except Exception as exc:
        print(f"[GATE-REFLECT] gate_reflection() failed asset={asset}: "
              f"{type(exc).__name__}: {exc}", flush=True)
        return None
    cycle_ts = int(time.time())
    ok = _persist(asset, result, cycle_ts)
    s = result.get("summary") or {}
    print(f"[GATE-REFLECT] {asset:5s} | persist={'OK' if ok else 'FAIL':4s} | "
          f"precision={s.get('precision')!s:6s} recall={s.get('recall')!s:6s} | "
          f"decided={s.get('n_decided', 0)}/{s.get('n_judged', 0)} "
          f"miss={s.get('n_miss', 0)}/{s.get('n_abnormal', 0)}", flush=True)
    return s if ok else None


def main(assets: Optional[List[str]] = None,
         days: int = CYCLE_DAYS_DEFAULT) -> Dict[str, Any]:
    """跑一轮所有品种 → 返回汇总。"""
    assets = list(assets or ASSETS_DEFAULT)
    summary: Dict[str, Any] = {}
    for a in assets:
        abnormals = _detect_abnormals_safe(a)
        s = run_once(a, days=days, abnormals=abnormals)
        if s:
            summary[a] = s
    return summary


def daemon_loop(interval_s: int = 1800,
                assets: Optional[List[str]] = None,
                days: int = CYCLE_DAYS_DEFAULT) -> None:
    """daemon 模式:每 interval_s 秒跑一轮;Ctrl+C 退出。"""
    print(f"[GATE-REFLECT] daemon started | interval={interval_s}s | assets={assets or ASSETS_DEFAULT}",
          flush=True)
    try:
        while True:
            try:
                main(assets=assets, days=days)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[GATE-REFLECT] daemon loop error: {type(exc).__name__}: {exc}",
                      flush=True)
            time.sleep(int(interval_s))
    except KeyboardInterrupt:
        print("\n[GATE-REFLECT] daemon stopped (KeyboardInterrupt)", flush=True)


if __name__ == "__main__":
    # CLI: python3 -m gate_reflection_worker [interval_s]
    interval = int(os.environ.get("GATE_REFLECT_INTERVAL_S", "1800"))
    daemon_loop(interval_s=interval)