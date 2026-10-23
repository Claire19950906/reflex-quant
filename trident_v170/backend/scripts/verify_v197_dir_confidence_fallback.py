#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v197: 验证 event_atom.extract_and_persist 区分 news 明确 (0.7) vs fallback (0.4)。

策略：用 monkeypatch 替换 event_struct.extract_events，构造两种 target_assets：
  - 空 list → 触发 fallback (asset_code 由 DEFAULT_ASSET_FOR_EVENT_TYPE 兜底)
  - 非空 → news 明确路径

这样可以避开 LLM/正则词典不可控的现实约束，直接验证 d_conf 分支逻辑。
"""
from __future__ import annotations
import sys
import os
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src_python"))

import db
import event_atom
import event_struct
import sqlite3


def main():
    results = []

    print("=" * 70)
    print("1. event_atom.FALLBACK_DIR_CONFIDENCE 常量")
    print("=" * 70)
    fc = event_atom.FALLBACK_DIR_CONFIDENCE
    print(f"  FALLBACK_DIR_CONFIDENCE = {fc}")
    ok1 = fc == 0.4
    results.append(("FALLBACK_DIR_CONFIDENCE == 0.4", ok1, f"got={fc}"))
    print()

    print("=" * 70)
    print("2. DEFAULT_ASSET_FOR_EVENT_TYPE 字典存在关键项")
    print("=" * 70)
    d = event_atom.DEFAULT_ASSET_FOR_EVENT_TYPE
    need = {"geopolitical_conflict", "central_bank_gold", "natural_disaster"}
    missing = need - set(d.keys())
    print(f"  DEFAULT_ASSET_FOR_EVENT_TYPE keys={list(d.keys())}")
    print(f"  missing={missing}")
    ok2 = not missing
    results.append(("DEFAULT_ASSET_FOR_EVENT_TYPE 含关键 fallback key",
                    ok2, f"missing={missing}"))
    print()

    print("=" * 70)
    print("3. fallback 路径 — target_assets=[] → d_conf=0.4 source=rule_bias_default_asset")
    print("=" * 70)
    raw_id = _insert_raw_news("[v197-test] fallback 路径")
    print(f"  插入 raw_news id={raw_id}")

    # monkeypatch：让 extract_events 返回 [{'event_type': '地缘冲突', 'target_assets': []}]
    orig_extract = event_struct.extract_events
    event_struct.extract_events = lambda text: [
        {"event_type": "地缘冲突", "actor": "", "action": "地缘冲突",
         "magnitude_value": None, "magnitude_unit": None,
         "target_assets": [], "raw": text[:120]}
    ]
    try:
        ids_fb = event_atom.extract_and_persist(
            "[v197-test] fallback 路径",
            raw_news_id=raw_id,
            default_event_time=int(time.time()),
            fallback_used=1,
        )
    finally:
        event_struct.extract_events = orig_extract

    print(f"  extract_and_persist 返回 ids: {ids_fb}")
    if ids_fb:
        _dump_atoms(ids_fb)
    ok3 = _check_confidence(ids_fb, 0.4, "rule_bias_default_asset")
    results.append(("fallback 路径 d_conf=0.4 source=rule_bias_default_asset",
                    ok3, f"ok={ok3}"))
    print()

    print("=" * 70)
    print("4. news 明确路径 — target_assets=['BTC'] → d_conf=0.7 source=rule_bias")
    print("=" * 70)
    raw_id2 = _insert_raw_news("[v197-test] news 明确路径")
    print(f"  插入 raw_news id={raw_id2}")

    event_struct.extract_events = lambda text: [
        {"event_type": "央行购金", "actor": "央行", "action": "央行购金",
         "magnitude_value": None, "magnitude_unit": None,
         "target_assets": ["XAU"], "raw": text[:120]}
    ]
    try:
        ids_ex = event_atom.extract_and_persist(
            "[v197-test] news 明确路径",
            raw_news_id=raw_id2,
            default_event_time=int(time.time()),
            fallback_used=0,
        )
    finally:
        event_struct.extract_events = orig_extract

    print(f"  extract_and_persist 返回 ids: {ids_ex}")
    if ids_ex:
        _dump_atoms(ids_ex)
    # XAU 在 dim_event_type.direction_bias["央行购金"]["XAU"] 应该非空 → direction 被 set
    ok4 = _check_confidence(ids_ex, 0.7, "rule_bias")
    results.append(("news 明确路径 d_conf=0.7 source=rule_bias",
                    ok4, f"ok={ok4}"))
    print()

    print("=" * 70)
    print("5. 清理测试数据")
    print("=" * 70)
    n_clean = _cleanup(raw_id, ids_fb, ids_ex)
    _cleanup_raw(raw_id2)
    print(f"  清理 atom 数={n_clean}")
    results.append(("清理测试 atom+raw", n_clean > 0, f"del={n_clean}"))
    print()

    print("=" * 70)
    print("总结")
    print("=" * 70)
    n_pass = sum(1 for _, ok, _ in results if ok)
    n_total = len(results)
    for label, ok, val in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {label}: {val}")
    print(f"\n  总计: {n_pass}/{n_total} 通过")
    return n_pass == n_total


def _insert_raw_news(text: str) -> int:
    conn = db.get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO raw_news (source, content, ts) VALUES (?, ?, ?)"
            " RETURNING id",
            ("v197_test", text, int(time.time())),
        )
        row = cur.fetchone()
        conn.commit()
        return row[0]
    finally:
        conn.close()


def _dump_atoms(ids):
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    placeholders = ",".join("?" * len(ids))
    for r in conn.execute(
        f"""SELECT a.id, e.code AS etype, ast.code AS asset, a.direction,
                   a.direction_confidence, a.direction_source, a.fallback_used
            FROM event_atom a
            LEFT JOIN dim_event_type e ON a.event_type_id=e.id
            LEFT JOIN dim_asset ast ON a.asset_id=ast.id
            WHERE a.id IN ({placeholders})
            ORDER BY a.id""",
        ids,
    ).fetchall():
        print(f"    id={r['id']:>4} type={str(r['etype']):<25} "
              f"asset={str(r['asset']):<6} dir={r['direction']:<8} "
              f"d_conf={r['direction_confidence']} "
              f"source={r['direction_source']:<25} "
              f"fb_used={r['fallback_used']}")
    conn.close()


def _check_confidence(ids, expected_conf: float, expected_source: str) -> bool:
    if not ids:
        print("    [WARN] 没有 atom 落地（可能新闻未被识别为 fallback）")
        return False
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT id, direction_confidence, direction_source "
        f"FROM event_atom WHERE id IN ({placeholders})",
        ids,
    ).fetchall()
    conn.close()
    ok = all(
        r["direction_confidence"] == expected_conf
        and r["direction_source"] == expected_source
        for r in rows
    )
    print(f"    期望 d_conf={expected_conf} source={expected_source}")
    print(f"    实际: {[(r['direction_confidence'], r['direction_source']) for r in rows]}")
    return ok


def _check_source(ids, expected_source: str) -> bool:
    if not ids:
        print("    [WARN] 没有 atom 落地")
        return False
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT id, direction_confidence, direction_source "
        f"FROM event_atom WHERE id IN ({placeholders})",
        ids,
    ).fetchall()
    conn.close()
    ok = all(r["direction_source"] == expected_source for r in rows)
    print(f"    期望 source={expected_source}（d_conf 不强制）")
    print(f"    实际: {[(r['direction_confidence'], r['direction_source']) for r in rows]}")
    return ok


def _cleanup(raw_id, ids_fb, ids_ex):
    conn = db.get_connection()
    all_ids = list(ids_fb or []) + list(ids_ex or [])
    if all_ids:
        placeholders = ",".join("?" * len(all_ids))
        n = conn.execute(
            f"DELETE FROM event_atom WHERE id IN ({placeholders})", all_ids
        ).rowcount
    else:
        n = 0
    if raw_id:
        conn.execute("DELETE FROM raw_news WHERE id=?", (raw_id,))
    conn.commit()
    conn.close()
    return n


def _cleanup_raw(raw_id):
    conn = db.get_connection()
    conn.execute("DELETE FROM raw_news WHERE id=?", (raw_id,))
    conn.commit()
    conn.close()


if __name__ == "__main__":
    ok = main()
    sys.exit(0 if ok else 1)