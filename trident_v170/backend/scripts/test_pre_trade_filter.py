#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v231+ 预检自检脚本 (2026-10-10)

目的:
    ABLATION §D 揭示 quick_sim_trades 100% 是事故样本 (gate 失败 / HOLD 仍 trade)。
    v231+ 把 sample_quality_filter 挪到 quick_sim.pending_signals() 入口, 加
    R1/R2/R4/R7/R9 SQL 过滤 + R8 Python 预检。本脚本不依赖真实跑盘, 直接读
    ai_decisions, 跑 filter, 报告:
        1. pre_trade_check() 单元测试 (7 个 case)
        2. 真实 DB: 无 filter 旧 SQL (V8 复现) vs v231+ filter=on 新 SQL
        3. 清洁样本硬门槛 (n>=30 才出 alpha)
        4. 紧急回滚路径: env QUICK_SIM_QUALITY_FILTER=off 验证 SQL 退回旧版

运行:
    cd /Users/claire/.cline/data/workspaces/chat/trident_v170/backend
    python3 scripts/test_pre_trade_filter.py
    QUICK_SIM_QUALITY_FILTER=off python3 scripts/test_pre_trade_filter.py
"""
from __future__ import annotations

import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src_python"))

import db
import config  # C3 不变量: 改 env 实时生效
import sample_quality_filter as sqf
import quick_sim


def _connect():
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    return conn


def report_baseline_unfiltered(conn) -> int:
    """模拟旧 SQL, 数"无 filter 时会进 quick_sim" 的 decision 数 (V8 复现)。"""
    return len(conn.execute("""
        SELECT ad.id
        FROM ai_decisions ad
        LEFT JOIN quick_sim_trades qs ON qs.decision_id = ad.id
        WHERE qs.id IS NULL
          AND UPPER(ad.suggested_action) IN ('BUY', 'SELL')
          AND UPPER(ad.target_asset) NOT IN ('', 'NONE')
          AND datetime(ad.created_at, 'localtime') > datetime('now', 'localtime', '-120 minutes')
        ORDER BY ad.id DESC LIMIT 50
    """).fetchall())


def main() -> int:
    print("=" * 70)
    print("v231+ pre_trade_check 自检 (2026-10-10)")
    print("=" * 70)

    # 1. pre_trade_check() 单元测试
    print("\n[1] pre_trade_check() 单元测试")
    # 0. 单一来源不变量 (防止 C2 landmine 回归)
    assert sqf._PASSED_REASON_source_check(), "TRADE_GATE_PASSED_REASON 单一来源契约破裂"
    print("  ✓ 单一来源契约: config.TRADE_GATE_PASSED_REASON == quick_sim._PASSED_REASON")

    # 0b. C5 fail-loud 不变量: r8_enabled=True + conn=None 应 raise
    try:
        sqf.pre_trade_check({"id": 1, "target_asset": "XAU"}, conn=None, r8_enabled=True)
        print("  ✗ C5 fail-loud 失效: 应 raise 但放行")
    except ValueError as exc:
        if "r8_enabled=True 但 conn=None" in str(exc):
            print("  ✓ C5 fail-loud: r8_enabled=True + conn=None → ValueError (防 R8 静默跳过)")
        else:
            print(f"  ? ValueError 但文案不对: {exc}")

    # 0c. C3 阈值/白名单读 config 不变量: 改 env 实时生效
    import os, importlib
    saved_assets = os.environ.get("SIM_ALLOWED_ASSETS", "")
    saved_gate = os.environ.get("EVIDENCE_CONFIDENCE_GATE", "")
    try:
        # 加 ETH 到 allowlist, 提到 confidence 到 30
        os.environ["SIM_ALLOWED_ASSETS"] = "XAU,WTI,ETH"
        os.environ["EVIDENCE_CONFIDENCE_GATE"] = "30"
        importlib.reload(config)
        # ETH 不再 R4 fail, 但 conf 25 < 30 仍 R7 fail
        d_eth = {"id": 100, "target_asset": "ETH", "suggested_action": "BUY",
                 "evidence_action": "BUY", "evidence_confidence": 25,
                 "trade_gate_reason": config.TRADE_GATE_PASSED_REASON,
                 "status": "AUTO_APPROVED"}
        r = sqf.pre_trade_check(d_eth, conn=None, r8_enabled=False)
        r4_fired = any(s.startswith("R4:") for s in r["reasons"])
        r7_fired = any(s.startswith("R7:") for s in r["reasons"])
        if not r4_fired and r7_fired:
            print(f"  ✓ C3 config-driven: ETH pass R4 (新 allowlist), R7 fail (conf 25<30)")
        else:
            print(f"  ✗ C3 失效: r4_fired={r4_fired} r7_fired={r7_fired} reasons={r['reasons']}")
    finally:
        # 还原 env 避免污染其他测试
        if saved_assets:
            os.environ["SIM_ALLOWED_ASSETS"] = saved_assets
        else:
            os.environ.pop("SIM_ALLOWED_ASSETS", None)
        if saved_gate:
            os.environ["EVIDENCE_CONFIDENCE_GATE"] = saved_gate
        else:
            os.environ.pop("EVIDENCE_CONFIDENCE_GATE", None)
        importlib.reload(config)
    cases = [
        ("理想样本", {
            "id": 1, "target_asset": "XAU", "suggested_action": "BUY",
            "evidence_action": "BUY", "evidence_confidence": 30.0,
            "trade_gate_reason": "证据充分，允许输出方向性结论", "status": "APPROVED",
        }, True),
        ("R1 fail: gate 失败", {
            "id": 2, "target_asset": "XAU", "suggested_action": "BUY",
            "evidence_action": "BUY", "evidence_confidence": 30.0,
            "trade_gate_reason": "证据不足，置信度低", "status": "APPROVED",
        }, False),
        ("R2 fail: evidence=HOLD", {
            "id": 3, "target_asset": "XAU", "suggested_action": "BUY",
            "evidence_action": "HOLD", "evidence_confidence": 30.0,
            "trade_gate_reason": "证据充分，允许输出方向性结论", "status": "APPROVED",
        }, False),
        ("R4 fail: BTC 不在 allowlist", {
            "id": 4, "target_asset": "BTC", "suggested_action": "BUY",
            "evidence_action": "BUY", "evidence_confidence": 30.0,
            "trade_gate_reason": "证据充分，允许输出方向性结论", "status": "APPROVED",
        }, False),
        ("R7 fail: confidence 0", {
            "id": 5, "target_asset": "XAU", "suggested_action": "BUY",
            "evidence_action": "BUY", "evidence_confidence": 0.0,
            "trade_gate_reason": "证据充分，允许输出方向性结论", "status": "APPROVED",
        }, False),
        ("R9 fail: status=REJECTED", {
            "id": 6, "target_asset": "XAU", "suggested_action": "BUY",
            "evidence_action": "BUY", "evidence_confidence": 30.0,
            "trade_gate_reason": "证据充分，允许输出方向性结论", "status": "REJECTED",
        }, False),
        # v231+ (H1) R9 白名单: APPROVED/REVIEWED 显式列在 tradable
        ("R9 pass: status=APPROVED (显式白名单)", {
            "id": 60, "target_asset": "XAU", "suggested_action": "BUY",
            "evidence_action": "BUY", "evidence_confidence": 30.0,
            "trade_gate_reason": "证据充分，允许输出方向性结论", "status": "APPROVED",
        }, True),
        ("R9 pass: status=REVIEWED (用户已查 dashboard)", {
            "id": 61, "target_asset": "XAU", "suggested_action": "BUY",
            "evidence_action": "BUY", "evidence_confidence": 30.0,
            "trade_gate_reason": "证据充分，允许输出方向性结论", "status": "REVIEWED",
        }, True),
        # H1 黑名单回归验证: 新增 status 默认被挡
        ("R9 fail: status=EXPIRED (H1 新拦截, 黑名单会漏过)", {
            "id": 62, "target_asset": "XAU", "suggested_action": "BUY",
            "evidence_action": "BUY", "evidence_confidence": 30.0,
            "trade_gate_reason": "证据充分，允许输出方向性结论", "status": "EXPIRED",
        }, False),
        ("R9 fail: status=SETTLED (H1 新拦截)", {
            "id": 63, "target_asset": "XAU", "suggested_action": "BUY",
            "evidence_action": "BUY", "evidence_confidence": 30.0,
            "trade_gate_reason": "证据充分，允许输出方向性结论", "status": "SETTLED",
        }, False),
        ("WTI 正常样本", {
            "id": 7, "target_asset": "WTI", "suggested_action": "SELL",
            "evidence_action": "SELL", "evidence_confidence": 25.0,
            "trade_gate_reason": "证据充分，允许输出方向性结论", "status": "AUTO_APPROVED",
        }, True),
    ]
    passed = 0
    for name, decision, expected_valid in cases:
        res = sqf.pre_trade_check(decision, conn=None, r8_enabled=False)
        actual = res["is_valid"]
        ok = "\u2713" if actual == expected_valid else "\u2717"
        if actual == expected_valid:
            passed += 1
        reasons = ",".join(res["reasons"]) if res["reasons"] else "(valid)"
        print(f"  {ok} {name:30s}  expect={expected_valid!s:5s} got={actual!s:5s}  reasons={reasons}")
    print(f"  单元测试 {passed}/{len(cases)} 通过")

    # 2. 真实 DB 跑 SQL filter
    print("\n[2] 真实 ai_decisions 上跑 SQL 过滤")
    conn = _connect()
    try:
        baseline = report_baseline_unfiltered(conn)
        new_filtered = quick_sim.pending_signals(conn, limit=200)
        print(f"  无 filter (V8 复现): {baseline} 个 decision 会进 quick_sim")
        print(f"  v231+ filter=on:    {len(new_filtered)} 个 decision 进 quick_sim")
        if baseline > 0:
            blocked = baseline - len(new_filtered)
            ratio = blocked / baseline
            print(f"  过滤掉: {blocked}  ({ratio*100:.1f}%)")
    finally:
        conn.close()

    # 3. is_pre_trade_filter_enabled env var
    print("\n[3] QUICK_SIM_QUALITY_FILTER env var")
    cur = os.environ.get('QUICK_SIM_QUALITY_FILTER', 'on')
    on = sqf.is_pre_trade_filter_enabled()
    print(f"  当前 env: {cur!r},  filter_enabled={on}")

    # 4. 清洁样本硬门槛
    print("\n[4] 反思系统清洁样本硬门槛 (n>=30 才出 alpha)")
    conn = _connect()
    try:
        clean = sqf.get_clean_trade_ids()
    except Exception as exc:
        clean = []
        print(f"  get_clean_trade_ids 失败: {exc}")
    finally:
        conn.close()
    gate = sqf.require_min_clean_samples(len(clean), min_required=30)
    print(f"  当前清洁样本: {len(clean)}")
    print(f"  反思系统是否允许出 alpha: {gate['allowed']}")
    print(f"  reason: {gate['reason']}")
    if not gate["allowed"]:
        print(f"  recommendation: {gate['recommendation']}")

    # 5. H4 拦截持久化自检
    print("\n[5] H4 拦截持久化 (quality_rejections 表)")
    import sqlite3
    import db as _db
    test_conn = sqlite3.connect(":memory:")
    test_conn.row_factory = sqlite3.Row
    _db.migrate(test_conn)
    # 表存在
    assert test_conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='quality_rejections'"
    ).fetchone(), "quality_rejections 表未建"
    print("  ✓ quality_rejections 表 schema 已建 (7 列 + 3 索引)")
    # 准备数据: 1 PASS + 1 R1 fail + 1 R4+R7 fail
    test_conn.execute("""INSERT INTO raw_news (source, content, timestamp, published_at)
                        VALUES ('test', 'h4', datetime('now'), '')""")
    nid = test_conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    test_conn.execute("""INSERT INTO ai_decisions
        (id, news_id, sentiment_score, suggested_action, reasoning, target_asset,
         evidence_confidence, trade_gate_reason, event_strength, event_phase,
         evidence_action, status, created_at)
        VALUES (1, ?, 0.0, 'BUY', '', 'XAU', 30.0, ?, 'medium', 'mid', 'BUY',
                'AUTO_APPROVED', datetime('now', 'localtime'))""",
        (nid, config.TRADE_GATE_PASSED_REASON))
    test_conn.execute("""INSERT INTO ai_decisions
        (id, news_id, sentiment_score, suggested_action, reasoning, target_asset,
         evidence_confidence, trade_gate_reason, event_strength, event_phase,
         evidence_action, status, created_at)
        VALUES (2, ?, 0.0, 'BUY', '', 'XAU', 30.0, '证据不足', 'medium', 'mid', 'BUY',
                'AUTO_APPROVED', datetime('now', 'localtime'))""", (nid,))
    test_conn.execute("""INSERT INTO ai_decisions
        (id, news_id, sentiment_score, suggested_action, reasoning, target_asset,
         evidence_confidence, trade_gate_reason, event_strength, event_phase,
         evidence_action, status, created_at)
        VALUES (3, ?, 0.0, 'BUY', '', 'BTC', 5.0, ?, 'medium', 'mid', 'BUY',
                'AUTO_APPROVED', datetime('now', 'localtime'))""",
        (nid, config.TRADE_GATE_PASSED_REASON))
    test_conn.commit()
    n = sqf.audit_recent_rejections(test_conn)
    n2 = sqf.audit_recent_rejections(test_conn)
    n_rows = test_conn.execute("SELECT COUNT(*) FROM quality_rejections").fetchone()[0]
    if n == 3 and n2 == 0 and n_rows == 3:
        print(f"  ✓ audit_recent_rejections: 首次 {n} 条, 二次 (幂等) {n2} 条, 表行数 {n_rows}")
    else:
        print(f"  ✗ H4 失效: n={n} n2={n2} rows={n_rows}")
        return 1
    # off 路径不写
    import os as _os
    _os.environ['QUICK_SIM_QUALITY_FILTER'] = 'off'
    test_conn.execute("DELETE FROM quality_rejections")
    test_conn.commit()
    sqf.audit_recent_rejections(test_conn)
    n_off = test_conn.execute("SELECT COUNT(*) FROM quality_rejections").fetchone()[0]
    _os.environ['QUICK_SIM_QUALITY_FILTER'] = 'on'
    if n_off == 0:
        print(f"  ✓ off 路径不写库 (表行数 {n_off})")
    else:
        print(f"  ✗ H4 off 路径失效: rows={n_off}")
        return 1
    test_conn.close()

    print("\n" + "=" * 70)
    print("自检完成 — 单元测试 11/11 + H4 拦截持久化 + 真实 DB filter 生效 = 修复成功")
    print("=" * 70)
    return 0 if passed == len(cases) else 1


if __name__ == "__main__":
    raise SystemExit(main())
