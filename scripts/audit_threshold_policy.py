#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""体检：品种级阈值政策是否真的在生效。

背景：政策来自 `runtime/backtest/threshold_map_*.json`，是一份文件而不是表。
换了矩阵、关掉开关、或矩阵过期，系统会静默回落到自适应阈值 —— 没人会发现。
本脚本把这件事变成一条可检查的结论。

判据：
  · 开关开着（TRIDENT_THRESH_POLICY 不是 off）
  · 最新矩阵存在、且是最近 7 天内生成的
  · 矩阵里有 use/weak 品种（否则等于没有任何品种被执行）
  · 抽查核心品种确实拿到政策（BTC/ETH/XAU/AAPL... 命中率）
退出码：0 正常；1 有品种拿不到政策（仅提示）；2 开关关闭或矩阵缺失/过期
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "backend", "src_python"))
import asset_threshold_policy as atp

CORE = ["BTC", "ETH", "XAU", "WTI", "AAPL", "NVDA", "TSLA", "SPX", "NDX"]
MAX_AGE_DAYS = 7


def main() -> int:
    atp.clear_cache()
    print("[POLICY] 开关:", "ON" if atp.enabled() else "OFF(TRIDENT_THRESH_POLICY=off)")
    data = atp.load_map()
    if not data:
        print("[POLICY] 没有阈值矩阵 → 全部品种回落到波动率自适应")
        return 2
    path = atp._cache.get("path") or "?"
    age_d = (time.time() - os.path.getmtime(path)) / 86400.0
    assets = data.get("assets") or {}
    verdicts: dict[str, int] = {}
    for v in assets.values():
        verdicts[(v.get("pick") or {}).get("verdict", "?")] = \
            verdicts.get((v.get("pick") or {}).get("verdict", "?"), 0) + 1
    print(f"[POLICY] 矩阵: {os.path.basename(path)} · {age_d:.1f} 天前 · {len(assets)} 品种")
    print("[POLICY] 判定分布:", ", ".join(f"{k}={v}" for k, v in sorted(verdicts.items())))

    # 只有 use/weak 会真正改变检测阈值；blocked 的 2% 比自适应更松，
    # 按"只收紧"设计不会生效，这里分开报，避免把 9/9 当成"全部收紧"。
    eff = [a for a in CORE if (atp.policy(a) or {}).get("verdict") in ("use", "weak")]
    blk = [a for a in CORE if (atp.policy(a) or {}).get("verdict") == "blocked"]
    other = [a for a in CORE if a not in eff and a not in blk]
    print(f"[POLICY] 真正收紧检测阈值 {len(eff)}/{len(CORE)}: "
          + (", ".join(f"{a}={atp.policy(a)['thresh']:g}%" for a in eff) or "无"))
    print(f"[POLICY] 判定 blocked（结论是别追涨，检测阈值不动）{len(blk)}: "
          + (", ".join(blk) or "无"))
    if other:
        print(f"[POLICY] 无政策/样本不足（走自适应）: {', '.join(other)}")

    rc = 0
    if not atp.enabled():
        rc = 2
    elif age_d > MAX_AGE_DAYS:
        print(f"[POLICY] 矩阵已 {age_d:.1f} 天未更新（阈值 {MAX_AGE_DAYS} 天）→ 建议重跑 sweep")
        rc = 2
    elif not any(v in verdicts for v in ("use", "weak")):
        print("[POLICY] 没有任何 use/weak 品种 → 政策实际不起作用")
        rc = 2
    return rc


if __name__ == "__main__":
    sys.exit(main())
