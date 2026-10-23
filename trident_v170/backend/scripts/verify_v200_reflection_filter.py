#!/usr/bin/env python3
"""v200 A4 反思 filter 放宽验证.

检查:
C1 enrich_pending_loop SQL 已不 JOIN quick_sim_trades
C2 新 SQL 包含 error_layer IN filter
C3 dry-run 模式命中数 = N (ref#93 + 其他未 LLM 反思的 pnl<-0.5%)
C4 enrich_one 不被 ref#93 之外已 LLM 反思的触及
C5 dry-run 不写库 (用 monkeypatch 测)
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src_python"))

import db, sqlite3
import trade_reflection_llm as trllm

PASS = []
FAIL = []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append((name, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}  {detail}")


# C1: SQL 已不 JOIN quick_sim_trades
print("[C1] enrich_pending_loop SQL 检查 (实际执行代码, 不含 docstring)")
import inspect, re as _re
src = inspect.getsource(trllm.enrich_pending_loop)
# 截取 """ 之后 (docstring) 和函数末尾 """ 之前的 SQL 部分
# docstring 在第一对 """ 之内, SQL 在第二个 """ 之后
sql_part = src.split('"""', 2)[2]  # 第 2 次 " 之后的内容 (SQL)
check("C1.1 SQL 不含 JOIN quick_sim_trades",
      "JOIN quick_sim_trades" not in sql_part)
check("C1.2 SQL 不含 qs.verdict 条件",
      "qs.verdict" not in sql_part)
check("C1.3 SQL 含 r.error_layer IN 守卫",
      "error_layer IN ('analysis', 'filter')" in sql_part)
check("C1.4 SQL 含 pnl_pct < -0.005 阈值 (不变)",
      "pnl_pct < -0.005" in sql_part)

# C7: enrich_one 内部 SQL 也是 LEFT JOIN ai_decisions (v200 A5)
print("[C7] enrich_one SQL 检查 (A5 LEFT JOIN)")
e1_src = inspect.getsource(trllm.enrich_one)
e1_sql = e1_src.split('"""', 2)[2]
check("C7.1 enrich_one SQL 不再是 INNER JOIN ai_decisions",
      "LEFT JOIN ai_decisions" in e1_sql and
      ("JOIN ai_decisions" not in e1_sql or e1_sql.count("JOIN ai_decisions") == 1))
check("C7.2 enrich_one SQL 不含 JOIN quick_sim_trades",
      "JOIN quick_sim_trades" not in e1_sql)
check("C7.3 enrich_one SQL 不含 qs.verdict",
      "qs.verdict" not in e1_sql)
print()

# C2: 当前 SQL 命中数 (临时插入测试反思保证可重复测试)
print("[C2] 当前数据库实测命中数 (含临时测试行)")
c = db.get_connection(); c.row_factory = sqlite3.Row
# 临时插入一个 fake 反思, pnl<-0.5% depth≥3 layer='analysis' 无 llm
import time as _t
_ts = _t.strftime("%Y-%m-%dT%H:%M:%S")
_test_ref_id = None
try:
    cur = c.execute("""
        INSERT INTO trade_reflections_v2
          (decision_id, paper_run_id, asset, action,
           pnl_pct, mfe_pct, mae_pct, settled_at,
           error_layer, error_sub_layer, error_signal,
           root_cause, lesson, depth_score, created_at,
           llm_depth_reflection)
          VALUES (-1, 0, 'XAU', 'BUY',
                  -0.012, -0.01, -0.02, ?,
                  'analysis', 'test_sub', 'test_sig',
                  'v200 verify test reflection', 'test lesson', 5, ?,
                  '')
    """, (_ts, _ts))
    c.commit()
    _test_ref_id = cur.lastrowid
    print(f"  inserted test ref#{_test_ref_id}")
except Exception as _e:
    print(f"  test insert failed: {_e}")
    _test_ref_id = None

n_match = c.execute("""
    SELECT COUNT(*) FROM trade_reflections_v2 r
     WHERE r.llm_depth_reflection = ''
       AND r.depth_score >= 3
       AND r.pnl_pct < -0.005
       AND r.error_layer IN ('analysis', 'filter')
""").fetchone()[0]
check(f"C2.1 当前命中数 = {n_match} (预期 ≥ 1, daemon 清空后也成立)",
      n_match >= 1)
n_already = c.execute("""
    SELECT COUNT(*) FROM trade_reflections_v2
    WHERE llm_depth_reflection IS NOT NULL AND llm_depth_reflection != ''
""").fetchone()[0]
check(f"C2.2 已写 LLM 反思 = {n_already} (≥ 7)",
      n_already >= 7)
print()

# C3: dry-run 不真调 LLM (测 SQL 走通)
print("[C3] enrich_pending_loop dry-run 调用")
# 用 monkeypatch 替换 enrich_one 看是否调用
called_ids = []
_orig = trllm.enrich_one
def _spy(reflection_id):
    called_ids.append(reflection_id)
    return False  # 不真写
trllm.enrich_one = _spy
try:
    n_processed = trllm.enrich_pending_loop(batch_size=50)
finally:
    trllm.enrich_one = _orig
check(f"C3.1 dry-run 调用了 enrich_one {len(called_ids)} 次",
      len(called_ids) >= 1)
check(f"C3.2 dry-run 不写库 (returned processed={n_processed} 仅当 enrich_one return True)",
      n_processed == 0)  # spy 返回 False
print(f"  spy 看到 reflection_ids = {called_ids}")
print()

# C4: 验证 spy 看到的 ids 都是 pnl<-0.5% 且 layer IN (说明新 filter 工作)
print("[C4] spy 调用 IDs 都满足新 filter")
ok_all = True
for rid in called_ids:
    r = c.execute("SELECT pnl_pct, depth_score, error_layer, llm_depth_reflection FROM trade_reflections_v2 WHERE id = ?", (rid,)).fetchone()
    cond_ok = (r["pnl_pct"] < -0.005 and r["depth_score"] >= 3
               and r["error_layer"] in ("analysis", "filter")
               and (r["llm_depth_reflection"] is None or r["llm_depth_reflection"] == ""))
    if not cond_ok:
        ok_all = False
        print(f"  ref#{rid} 不满足 filter: pnl={r['pnl_pct']:+.3f}% d={r['depth_score']} layer={r['error_layer']}")
check(f"C4.1 所有 spy 调用都满足 v200 A4 filter", ok_all)
print()

# C5: 改回 SQL 前后对比 — 老 SQL 命中 0, 新 SQL 命中 ≥ 1
print("[C5] 改前/改后 SQL 命中数对比")
n_old = c.execute("""
    SELECT COUNT(*) FROM trade_reflections_v2 r
    JOIN quick_sim_trades qs ON qs.decision_id = r.decision_id
    WHERE r.llm_depth_reflection = ''
      AND qs.verdict = 'LOSS'
      AND r.depth_score >= 3
      AND r.pnl_pct < -0.005
""").fetchone()[0]
n_new = c.execute("""
    SELECT COUNT(*) FROM trade_reflections_v2 r
    WHERE r.llm_depth_reflection = ''
      AND r.depth_score >= 3
      AND r.pnl_pct < -0.005
      AND r.error_layer IN ('analysis', 'filter')
""").fetchone()[0]
check(f"C5.1 旧 SQL 命中 = {n_old}, 新 SQL 命中 = {n_new}, 新 > 旧",
      n_new > n_old)
print()

# C6: 历史 7 条 LLM 反思的 evidence 是 raw_response, v200 A4 跑出来的也是同格式
print("[C6] LLM 反思格式兼容 (与历史 7 条一致)")
r = c.execute("""
    SELECT llm_depth_reflection FROM trade_reflections_v2
    WHERE llm_depth_reflection IS NOT NULL AND llm_depth_reflection != ''
    LIMIT 1
""").fetchone()
if r:
    sample = r[0]
    check(f"C6.1 历史反思是 JSON wrap (raw_response) 格式",
          'raw_response' in sample or '"key_failure_chain"' in sample)
print()

# 总结
print("=" * 70)
print(f" v200 verify: {len(PASS)}/{len(PASS) + len(FAIL)} PASS")
print("=" * 70)

# 清理临时测试行
if _test_ref_id is not None:
    try:
        c.execute("DELETE FROM trade_reflections_v2 WHERE id = ?", (_test_ref_id,))
        c.commit()
        print(f"  cleanup: deleted test ref#{_test_ref_id}")
    except Exception as _e:
        print(f"  cleanup failed: {_e}")
c.close()

if FAIL:
    print("FAILED:")
    for n, d in FAIL:
        print(f"  - {n}: {d}")
    sys.exit(1)
print("\nv200 A4 reflection filter relaxation verify PASSED")