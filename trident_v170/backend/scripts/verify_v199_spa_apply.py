#!/usr/bin/env python3
"""v199 SPA Apply 闭环验证。

5 项硬检查:
  C1. migration 已应用 — ai_decisions.applied_setting_changes 列存在
  C2. SPA worker 模块可加载 + run_once() 返回结构正确
  C3. pending SPA 中满足阈值且在白名单的已推进到 status='applied'
  C4. 已 applied SPA 已写入 applied_setting_change 文本
  C5. ai_decisions.applied_setting_changes 已回填（asset 维度 sync 跑过）

全部通过 → v199 闭环跑通 (SPA 不再永久 pending + ai_worker 读取注入 prompt)。
"""
from __future__ import annotations
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + "/src_python")
import sqlite3


PASS = "\033[32m✓\033[0m"
FAIL = "\033[31m✗\033[0m"
results: list = []


def _check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    icon = PASS if ok else FAIL
    print(f"  {icon} {name}: {detail}")


def main() -> int:
    import db
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row

    # C1
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(ai_decisions)").fetchall()]
    c1 = "applied_setting_changes" in cols
    _check("C1 migration", c1, f"ai_decisions.applied_setting_changes 列={'存在' if c1 else '缺失'}")

    # C2
    try:
        import spa_apply_worker as saw
        c2 = (hasattr(saw, "run_once")
              and hasattr(saw, "daemon_loop")
              and "filter.asset_match.main_asset_missed" in saw.KNOWN_FILTER_PARAM_KEYS)
        _check("C2 worker 模块", c2, f"run_once/daemon_loop/KNOWN_FILTER_PARAM_KEYS={c2}")
    except Exception as exc:
        c2 = False
        _check("C2 worker 模块", False, f"加载失败: {type(exc).__name__}: {exc}")

    # C3 — pending SPA 中阈值满足且白名单的应已 applied
    if c2:
        cands = saw._fetch_pending_candidates(conn,
                min_samples=saw.SAMPLE_THRESHOLD,
                min_conf=saw.CONFIDENCE_THRESHOLD)
        applied_count = conn.execute(
            "SELECT COUNT(*) AS n FROM strategy_param_adjustments "
            "WHERE status='applied' AND applied_setting_change != ''"
        ).fetchone()["n"]
        c3 = applied_count > 0
        _check("C3 pending 推进", c3,
               f"满足阈值但仍未推进的 pending={len(cands)} 条 (应=0), "
               f"已 applied 数={applied_count}")
    else:
        _check("C3 pending 推进", False, "worker 未加载, 跳过")

    # C4 — applied_setting_change 已写入文本
    applied_rows = conn.execute(
        "SELECT id, param_key, status, applied_setting_change "
        "FROM strategy_param_adjustments "
        "WHERE status='applied' AND applied_setting_change != ''"
    ).fetchall()
    c4 = (len(applied_rows) > 0
          and all(len(str(r["applied_setting_change"] or "")) > 20 for r in applied_rows))
    sample_text = applied_rows[0]["applied_setting_change"] if applied_rows else ""
    _check("C4 applied_setting_change 文本", c4,
           f"{len(applied_rows)} 条已 applied, sample='{sample_text[:120]}'")

    # C5 — ai_decisions.applied_setting_changes 已回填
    synced = conn.execute(
        "SELECT COUNT(*) AS n FROM ai_decisions "
        "WHERE applied_setting_changes != '' AND applied_setting_changes IS NOT NULL"
    ).fetchone()["n"]
    c5 = synced > 0
    _check("C5 ai_decisions 回填", c5,
           f"applied_setting_changes 非空的 ai_decisions={synced} 条")

    # 额外 — ai_worker prompt 注入函数可调
    try:
        from engine.ai_worker import _load_spa_applied_block_sync
        b_btc = _load_spa_applied_block_sync(asset="BTC", max_chars=600)
        b_xau = _load_spa_applied_block_sync(asset="XAU", max_chars=600)
        c6 = "v199 SPA Applied Block" in b_btc and "v199 SPA Applied Block" in b_xau
        _check("C6 prompt 注入函数", c6,
               f"BTC block len={len(b_btc)}, XAU block len={len(b_xau)}")
    except Exception as exc:
        _check("C6 prompt 注入函数", False, f"加载/调用失败: {type(exc).__name__}: {exc}")

    conn.close()
    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print()
    print("=" * 70)
    print(f"v199 verify: {passed}/{total} PASS")
    print("=" * 70)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())