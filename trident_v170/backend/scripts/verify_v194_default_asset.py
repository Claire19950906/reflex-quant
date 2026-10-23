#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v194: 验证 event_atom.py::DEFAULT_ASSET_FOR_EVENT_TYPE 自动兜底。

验收目标：
1. event_atom.py 含 DEFAULT_ASSET_FOR_EVENT_TYPE 字典
2. 含 geopol_conflict → XAU 映射
3. extract_and_persist 含 fallback 逻辑 + 动态 source
4. v193 #2 backfill 数据 bias 一致率 >= 95%
"""
from __future__ import annotations
import sys
import os

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src_python"))

import event_atom
import db
import sqlite3

CN_TO_DB = {"利多": "bullish", "利空": "bearish", "中性": "neutral",
            "bullish": "bullish", "bearish": "bearish", "neutral": "neutral"}


def main():
    results = []
    print("=" * 70)
    print("1. DEFAULT_ASSET_FOR_EVENT_TYPE 字典存在")
    print("=" * 70)
    defaults = getattr(event_atom, "DEFAULT_ASSET_FOR_EVENT_TYPE", {})
    print(f"  字典大小: {len(defaults)}")
    for et, asset in defaults.items():
        print(f"    {et:<25} → {asset}")
    has_geopol = ("geopolitical_conflict" in defaults
                  and defaults["geopolitical_conflict"] == "XAU")
    has_opec = "opec_cut" in defaults and defaults["opec_cut"] == "WTI"
    has_central = ("central_bank_gold" in defaults
                   and defaults["central_bank_gold"] == "XAU")
    ok1 = has_geopol and has_opec and has_central and len(defaults) >= 5
    results.append(("DEFAULT_ASSET_FOR_EVENT_TYPE 含 5+ 关键映射",
                    ok1, f"n={len(defaults)}"))

    print()
    print("=" * 70)
    print("2. extract_and_persist 源码含 fallback 逻辑")
    print("=" * 70)
    ea_path = os.path.join(HERE, "..", "src_python", "event_atom.py")
    with open(ea_path, "r", encoding="utf-8") as f:
        src = f.read()
    has_dict_ref = "DEFAULT_ASSET_FOR_EVENT_TYPE" in src
    has_branch = "if not asset_code and code in DEFAULT_ASSET_FOR_EVENT_TYPE" in src
    has_src_var = "asset_source = " in src
    has_src_use = "direction_source=asset_source" in src
    print(f"  引用 DEFAULT_ASSET_FOR_EVENT_TYPE: {has_dict_ref}")
    print(f"  含 fallback 分支: {has_branch}")
    print(f"  含 asset_source 变量: {has_src_var}")
    print(f"  direction_source 用 asset_source: {has_src_use}")
    ok2 = has_dict_ref and has_branch and has_src_var and has_src_use
    results.append(("extract_and_persist 含 fallback 逻辑 + 动态 source",
                    ok2, f"branch={has_branch} use={has_src_use}"))

    print()
    print("=" * 70)
    print("3. v193 #2 backfill 数据 (rule_bias_default_asset)")
    print("=" * 70)
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    n_rule = conn.execute(
        "SELECT COUNT(*) AS n FROM event_atom"
        " WHERE direction_source = 'rule_bias'"
    ).fetchone()["n"]
    n_default = conn.execute(
        "SELECT COUNT(*) AS n FROM event_atom"
        " WHERE direction_source = 'rule_bias_default_asset'"
    ).fetchone()["n"]
    print(f"  source='rule_bias': {n_rule}")
    print(f"  source='rule_bias_default_asset': {n_default}")
    ok3 = n_default >= 5
    results.append(("v193 #2 backfill 已写入 5+ rule_bias_default_asset",
                    ok3, f"n={n_default}"))

    print()
    print("=" * 70)
    print("4. fallback 数据 bias 一致性")
    print("=" * 70)
    rows = conn.execute("""
        SELECT det.code AS et_code, a.code AS asset_code,
               ea.direction,
               json_extract(det.direction_bias, '$."' || a.code || '"') AS bias_value,
               COUNT(*) AS n
        FROM event_atom ea
        JOIN dim_event_type det ON det.id = ea.event_type_id
        JOIN dim_asset a ON a.id = ea.asset_id
        WHERE ea.direction_source = 'rule_bias_default_asset'
        GROUP BY et_code, asset_code
    """).fetchall()
    n_match = 0
    n_total = 0
    for r in rows:
        bias_to_db = CN_TO_DB.get(r["bias_value"] or "", "unknown")
        consistent = (r["direction"] == bias_to_db) if bias_to_db != "unknown" else True
        n_total += 1
        if consistent:
            n_match += 1
        tag = "[OK]" if consistent else "[XX]"
        print(f"  {tag} {r['et_code']:<25} asset={r['asset_code']:<10} "
              f"event={r['direction']:<8} bias={r['bias_value']:<8} n={r['n']}")
    pct = 100 * n_match / max(n_total, 1)
    ok4 = pct >= 95
    results.append(("fallback 数据 bias 一致率 >= 95%", ok4,
                    f"{n_match}/{n_total} = {pct:.1f}%"))

    print()
    print("=" * 70)
    print("5. 未来路径：_EVENT_RULES 改空时 fallback 才触发")
    print("=" * 70)
    import event_struct
    geopol_rule = None
    for r in event_struct._EVENT_RULES:
        if r.get("event_type") in ("地缘冲突", "geopolitical_conflict"):
            geopol_rule = r
            break
    if geopol_rule:
        ta = geopol_rule["target_assets"]
        print(f"  _EVENT_RULES geopol_conflict 实际 target_assets: {ta}")
        print(f"  当前规则已配 target_assets (fallback 不会被命中): {bool(ta)}")
        # 预期：当前规则已配 → fallback 是预防性代码
        ok5 = bool(ta)
    else:
        print(f"  geopol_conflict 规则未找到")
        ok5 = False
    results.append(("v194 fallback 是预防性代码 (当前规则未触发)", ok5,
                    f"target_assets 已配={ok5}"))

    print()
    print("=" * 70)
    print("总结")
    print("=" * 70)
    n_pass = sum(1 for _, ok, _ in results if ok)
    n_total_all = len(results)
    for label, ok, val in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {label}: {val}")
    print(f"\n  总计: {n_pass}/{n_total_all} 通过")
    conn.close()
    return n_pass == n_total_all


if __name__ == "__main__":
    ok = main()
    sys.exit(0 if ok else 1)