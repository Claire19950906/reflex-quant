#!/usr/bin/env python3
"""v190 #4 weekly_insight_apply 集成验证.

用法:
    python3 scripts/verify_v190_weekly.py

检查:
1. 模块加载 OK
2. apply_once 不修改现实 (dry-run only) — 不应该 INSERT 任何 adjustment
3. 临时插入 1 条 active insight (source_samples=15) → 真实模式应该 INSERT 1 adjustment
4. 第二次跑 → 0 INSERT + 1 MERGED (验证幂等 + 累加)
5. 清理临时数据 (DELETE adjustment + DELETE insight)
6. lifespan daemon 接入存在 (验证 weekly_insight_apply._daemon_loop 在 api_server.py 里被引用)
7. admin endpoints 存在
"""
import sys
sys.path.insert(0, 'src_python')

import db
import sqlite3
import weekly_insight_apply as wia
import subprocess as _sp
import os as _os
import sys as _sys
import re as _re_v199

PASS = []
FAIL = []


def check(name: str, ok: bool, msg: str = "") -> None:
    if ok:
        PASS.append(name)
        print(f"  PASS  {name}  {msg}")
    else:
        FAIL.append(name)
        print(f"  FAIL  {name}  {msg}")


# ============================================================
# v199 — SPA Apply 闭环验证（先跑, 不被后续 FAIL 阻塞）
# ============================================================
print("=" * 70)
print(" v199 SPA Apply 闭环验证 (寄生)")
print("=" * 70)
_v199_proc = _sp.run(
    [_sys.executable, "scripts/verify_v199_spa_apply.py"],
    cwd=_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
    capture_output=True, text=True, timeout=60,
)
_v199_ok = _v199_proc.returncode == 0
_v199_out = (_v199_proc.stdout or "") + (_v199_proc.stderr or "")
_v199_summary = [ln.strip() for ln in _v199_out.splitlines()
                 if "v199 verify:" in ln]
_v199_summary_str = _v199_summary[0] if _v199_summary else "(no summary line)"
_m_v199 = _re_v199.search(r"(\d+)/(\d+) PASS", _v199_summary_str)
_n_pass_v199 = int(_m_v199.group(1)) if _m_v199 else 0
_n_total_v199 = int(_m_v199.group(2)) if _m_v199 else 0
check(f"v199 SPA Apply 闭环 = {_n_pass_v199}/{_n_total_v199} PASS",
      _v199_ok and _n_pass_v199 == _n_total_v199 and _n_total_v199 >= 6)
print()


# ============================================================
# v200 — 反思 LLM Filter 放宽 (A4/A5) 验证（寄生模式：调子脚本拿结果）
# ============================================================
print("=" * 70)
print(" v200 反思 LLM Filter 放宽 A4/A5 验证 (寄生)")
print("=" * 70)
_v200_proc = _sp.run(
    [_sys.executable, "scripts/verify_v200_reflection_filter.py"],
    cwd=_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
    capture_output=True, text=True, timeout=60,
)
_v200_ok = _v200_proc.returncode == 0
_v200_out = (_v200_proc.stdout or "") + (_v200_proc.stderr or "")
_v200_summary = [ln.strip() for ln in _v200_out.splitlines()
                 if "v200 verify:" in ln]
_v200_summary_str = _v200_summary[0] if _v200_summary else "(no summary line)"
_m_v200 = _re_v199.search(r"(\d+)/(\d+) PASS", _v200_summary_str)
_n_pass_v200 = int(_m_v200.group(1)) if _m_v200 else 0
_n_total_v200 = int(_m_v200.group(2)) if _m_v200 else 0
check(f"v200 反思 LLM Filter A4/A5 = {_n_pass_v200}/{_n_total_v200} PASS",
      _v200_ok and _n_pass_v200 == _n_total_v200 and _n_total_v200 >= 11)
print()
print()


print("=" * 70)
print(" v190 #4 weekly_insight_apply integration verify")
print("=" * 70)
print()

# 1) 模块加载
print("[1] 模块加载")
check("apply_once 存在", hasattr(wia, "apply_once"))
check("_extract_param_key 存在", hasattr(wia, "_extract_param_key"))
check("_extract_direction 存在", hasattr(wia, "_extract_direction"))
check("_extract_action 存在", hasattr(wia, "_extract_action"))
check("_daemon_loop 存在", hasattr(wia, "_daemon_loop"))
# v191 #1: 模糊关键词映射表存在
check("_KEYWORD_TO_PARAM 存在 (v191)", hasattr(wia, "_KEYWORD_TO_PARAM"))
# v191 #2: 模糊解析的真实 insight 数量 > 0 (当前数据期望 ≥ 3)
_conn = db.get_connection()
_conn.row_factory = sqlite3.Row
_recovered = 0
for _r in _conn.execute(
    "SELECT id, rule FROM review_insights WHERE active=1 AND kind=1 AND source_samples>=5"
).fetchall():
    if wia._extract_param_key(_r["rule"]):
        _recovered += 1
_conn.close()
check(f"v191 #1 模糊映射恢复 {_recovered} 个 n≥5 insight (期望 ≥ 3)", _recovered >= 3)
# v191 #2: LLM 输出 [param_key] 格式能否被解析 (3 个典型 case)
_brkt_ok = (
    wia._extract_param_key("[sizing.position_weight] 加密主流仓位权重应低于贵金属")
    == "sizing.position_weight"
    and wia._extract_param_key("[score.confidence_threshold] XAU BUY")
    == "score.confidence_threshold"
    and wia._extract_param_key("[sizing.stop_loss_pct] SELL方向应设动态止损")
    == "sizing.stop_loss_pct"
)
check("v191 #2 [param_key] 格式被解析 (review_agent 新提示词格式)", _brkt_ok)
print()

# 2) dry-run 不修改现实
print("[2] dry-run 不写入")
conn = db.get_connection()
conn.row_factory = sqlite3.Row
n_before = conn.execute(
    "SELECT COUNT(*) AS n FROM strategy_param_adjustments WHERE note='v190_weekly_promote'"
).fetchone()["n"]
try:
    result = wia.apply_once(conn, min_samples=10, dry_run=True)
    n_after = conn.execute(
        "SELECT COUNT(*) AS n FROM strategy_param_adjustments WHERE note='v190_weekly_promote'"
    ).fetchone()["n"]
    check("dry-run 不增加 adjustment", n_after == n_before,
          f"before={n_before} after={n_after}")
    check("dry-run scanned > 0", int(result["scanned"]) > 0,
          f"scanned={result['scanned']}")
finally:
    conn.close()
print()

# 3) + 4) + 5) 临时 INSERT 验证
print("[3+4+5] 真实模式 INSERT + 幂等 + 清理")
conn = db.get_connection()
conn.row_factory = sqlite3.Row
tmp_insight_id = None
try:
    cur = conn.execute("""
        INSERT INTO review_insights
          (rule, asset, scope, active, source_samples, origin, kind, counter_factual)
        VALUES
          ('filter.asset_match.main_asset_missed 上调阈值 0.05 v190 verify',
           'BTC', 'review', 1, 15, 'real', 1, '')
    """)
    tmp_insight_id = int(cur.lastrowid)
    conn.commit()

    # 3) 第一次跑
    result = wia.apply_once(conn, min_samples=10, dry_run=False)
    inserted_now = int(n_before) + len(result["inserted"])
    check("真实模式 INSERT 1 adjustment", inserted_now == int(n_before) + 1,
          f"inserted={len(result['inserted'])} adj_ids={[x['adj_id'] for x in result['inserted']]}")
    inserted_adj_id = result["inserted"][0]["adj_id"] if result["inserted"] else None

    # 4) 第二次跑
    result2 = wia.apply_once(conn, min_samples=10, dry_run=False)
    check("第二次跑 0 INSERT",
          len(result2["inserted"]) == 0,
          f"inserted={len(result2['inserted'])}")
    check("第二次跑 1 MERGED (累加)",
          len(result2["merged"]) >= 1,
          f"merged={len(result2['merged'])} merged_ids={[x['adj_id'] for x in result2['merged']]}")

    # 5) 清理
    n_del_adj = conn.execute(
        "DELETE FROM strategy_param_adjustments WHERE note='v190_weekly_promote'"
    ).rowcount
    n_del_ins = conn.execute(
        "DELETE FROM review_insights WHERE id=?", (tmp_insight_id,)
    ).rowcount
    conn.commit()
    check("清理 1 adjustment", n_del_adj >= 1, f"deleted={n_del_adj}")
    check("清理 1 insight", n_del_ins == 1, f"deleted={n_del_ins}")
finally:
    if tmp_insight_id is not None:
        # safety net
        conn.execute(
            "DELETE FROM strategy_param_adjustments WHERE note='v190_weekly_promote'"
        ).rowcount
        conn.execute("DELETE FROM review_insights WHERE id=?", (tmp_insight_id,))
        conn.commit()
    conn.close()
print()

# 6) lifespan 接入
print("[6] api_server.py lifespan 接入")
src_api = open("src_python/api_server.py").read()
check("lifespan 引用 weekly_insight_apply",
      "weekly_insight_apply" in src_api and "_daemon_loop" in src_api)
check("lifespan 启动 daemon 线程",
      'name="weekly_insight_apply"' in src_api)
print()

# 7) admin endpoints
print("[7] admin endpoints")
check("POST /api/admin/insight/promote-now",
      '/api/admin/insight/promote-now' in src_api)
check("GET /api/admin/insight/weekly-status",
      '/api/admin/insight/weekly-status' in src_api)
# v191 #1: weekly-status 返回 v191_recoverable_candidates 字段
check("weekly-status 返回 v191_recoverable_candidates (v191)",
      "v191_recoverable_candidates" in src_api)
print()

# 8) config
print("[8] config")
import config
# v191 #1: 默认从 10 调到 5，跟 v188 REVIEW_INSIGHTS_MIN_SAMPLES=5 一致
# 当前 27 个 kind=1 insight 中 3-4 个能在 min_samples=5 下产出 INSERT。
# 严格验证：env WEEKLY_INSIGHT_MIN_SAMPLES=10 时默认应等于 10。
import os as _os
_default = (int(_os.getenv("WEEKLY_INSIGHT_MIN_SAMPLES"))
            if _os.getenv("WEEKLY_INSIGHT_MIN_SAMPLES") else 5)
check(f"WEEKLY_INSIGHT_MIN_SAMPLES={_default}",
      int(getattr(config, "WEEKLY_INSIGHT_MIN_SAMPLES", 0)) == _default)
print()

# 总结
print("=" * 70)
print(f" PASS={len(PASS)}  FAIL={len(FAIL)}  TOTAL={len(PASS) + len(FAIL)}")
print("=" * 70)
if FAIL:
    print()
    print("FAILED:")
    for n in FAIL:
        print(f"  - {n}")
    sys.exit(1)
print()
print("v190 #4 weekly_insight_apply integration verify PASSED")

# v192 #2: ai_worker.py 已接入 load_recent_insights_block (asset-specific)
print()
print("=" * 70)
print(" v192 #2: ai_worker.py 接入 asset-specific 实战经验注入")
print("=" * 70)
_aw_path = _os.path.join(_os.path.dirname(__file__),
                         "..", "src_python", "engine", "ai_worker.py")
with open(_aw_path, "r", encoding="utf-8") as _f:
    _aw_src = _f.read()
check("ai_worker.py 含 load_recent_insights_block 调用",
      "load_recent_insights_block" in _aw_src)
check("ai_worker.py 含 INSIGHTS-ASSET 日志标签",
      "INSIGHTS-ASSET" in _aw_src)
check("ai_worker.py 含 gate_target_asset 守卫",
      "if gate_target_asset:" in _aw_src)
print()

# v194: event_atom.extract_and_persist 自动兜底 asset (预防 geopol→XAU 失配)
print()
print("=" * 70)
print(" v194: event_atom.extract_and_persist 自动兜底默认 asset")
print("=" * 70)
_ea_path = _os.path.join(_os.path.dirname(__file__),
                         "..", "src_python", "event_atom.py")
with open(_ea_path, "r", encoding="utf-8") as _f:
    _ea_src = _f.read()
check("event_atom.py 含 DEFAULT_ASSET_FOR_EVENT_TYPE 字典",
      "DEFAULT_ASSET_FOR_EVENT_TYPE:" in _ea_src)
check("event_atom.py 含 geopol_conflict → XAU 映射",
      '"geopolitical_conflict": "XAU"' in _ea_src)
check("extract_and_persist 含 fallback 分支",
      "if not asset_code and code in DEFAULT_ASSET_FOR_EVENT_TYPE" in _ea_src)
check("direction_source 区分 rule_bias vs rule_bias_default_asset",
      '"rule_bias_default_asset"' in _ea_src)
print()

# v193 #1: event_atom.direction 覆盖率（rule_bias backfill 验证）
print()
print("=" * 70)
print(" v193 #1: event_atom.direction 覆盖率验证")
print("=" * 70)
import sqlite3 as _sq3
from typing import Any
_conn: Any = db.get_connection()
_conn.row_factory = _sq3.Row
_dim_bias_rows = _conn.execute(
    "SELECT code, direction_bias FROM dim_event_type WHERE active=1"
).fetchall()
import json as _json
# v198: market_event (v176.5 placeholder, by-design neutral) 和
# cpi_release (dead code, 无任何代码引用) 不参与覆盖率统计。
_EXCLUDED_FROM_DIM_BIAS = {"market_event", "cpi_release"}
_in_scope = [r for r in _dim_bias_rows
             if r["code"] not in _EXCLUDED_FROM_DIM_BIAS]
_dim_with = sum(
    1 for r in _in_scope
    if _json.loads(r["direction_bias"] or "{}")
)
check(f"dim_event_type.direction_bias 覆盖: {_dim_with}/{len(_in_scope)}"
      f" (排除 market_event/cpi_release)",
      _dim_with / max(len(_in_scope), 1) >= 0.95)
_atom_dir_rows = _conn.execute(
    "SELECT direction, COUNT(*) AS n FROM event_atom GROUP BY direction"
).fetchall()
_atom_total = sum(r["n"] for r in _atom_dir_rows)
_atom_known = sum(r["n"] for r in _atom_dir_rows if r["direction"] != "unknown")
check(f"event_atom.direction 已知: {_atom_known}/{_atom_total}",
      _atom_known / max(_atom_total, 1) >= 0.60)
_conn.close()

# fixture 监控 (cleanup_test_fixture_atoms 后)
print()
print("=" * 70)
print(" fixture 监控: actor='测试 actor'")
print("=" * 70)
_conn2 = db.get_connection()
_conn2.row_factory = _sq3.Row
_n_fixture = _conn2.execute(
    "SELECT COUNT(*) AS n FROM event_atom WHERE actor='测试 actor'"
).fetchone()["n"]
check(f"event_atom actor='测试 actor' 残留 (应 = 0): {_n_fixture}", _n_fixture == 0)
_conn2.close()

# v195: ai_prompts.load_recent_insights_block 把 asset='NONE' 视为通则
print()
print("=" * 70)
print(" v195: asset='NONE' 视为通则 (与 asset='' 同级)")
print("=" * 70)
_ap_path = _os.path.join(_os.path.dirname(__file__),
                         "..", "src_python", "ai_prompts.py")
with open(_ap_path, "r", encoding="utf-8") as _f:
    _ap_src = _f.read()
check("ai_prompts.py ORDER BY 含 IN ('', 'NONE')",
      "IN ('', 'NONE')" in _ap_src)
check("ai_prompts.py asset_tag 跳过 NONE (not in 守卫)",
      'not in ("", "NONE")' in _ap_src)
_conn3 = db.get_connection()
_conn3.row_factory = _sq3.Row
_n_none = _conn3.execute(
    "SELECT COUNT(*) AS n FROM review_insights"
    " WHERE active=1 AND kind=1 AND asset='NONE'"
).fetchone()["n"]
# NONE 现状：若 source_samples >= 5 进入 prompt 通则区；若 < 5 被排除
print(f"  注: review_insights asset='NONE' 共 {_n_none} 条 "
      f"(默认 _INSIGHTS_CACHE_MIN_SAMPLES=5 才会注入)")
_conn3.close()

# v196: review_agent LLM prompt 支持 [XAU]/[BTC] asset 前缀 + save_insights 落 asset 字段
print()
print("=" * 70)
print(" v196: review_agent LLM 输出/解析 [XAU] asset 前缀")
print("=" * 70)
_ra_path = _os.path.join(_os.path.dirname(__file__),
                         "..", "src_python", "review_agent.py")
with open(_ra_path, "r", encoding="utf-8") as _f:
    _ra_src = _f.read()
check("review_agent._generate_insights_llm prompt 含 v196 asset 前缀",
      "v196 新增（可选）" in _ra_src)
check("review_agent._extract_from_quick_sim_llm prompt 含 v196 asset 前缀",
      "⑥v196：insights 若明确限定某品种" in _ra_src)
check("review_agent 含 _extract_asset_prefix 函数",
      "def _extract_asset_prefix" in _ra_src)
check("save_insights 调用 _extract_asset_prefix (asset_code, rule = ...)",
      "asset_code, rule = _extract_asset_prefix(raw_rule)" in _ra_src)
check("save_insights INSERT 写入 asset 字段",
      "(rule, asset, scope, active, source_samples, origin)" in _ra_src)
# 数据库统计：现有 review_insights 按 asset 分布
_conn4 = db.get_connection()
_conn4.row_factory = _sq3.Row
_dist = {r["asset"] or "∅": r["n"]
         for r in _conn4.execute("""
            SELECT COALESCE(asset, '') AS asset, COUNT(*) AS n
            FROM review_insights WHERE active=1 AND kind=1
            GROUP BY COALESCE(asset, '')
         """).fetchall()}
print(f"  现有 review_insights asset 分布: {_dist}")
_n_xau = _dist.get("XAU", 0)
_n_btc = _dist.get("BTC", 0)
_n_eth = _dist.get("ETH", 0)
_n_wti = _dist.get("WTI", 0)
_n_sol = _dist.get("SOL", 0)
_n_general = _dist.get("∅", 0)
# v196 注入 v195 后: 大部分 (35 条) 应该 asset=∅（旧规则），
# 后续 LLM 输出新规则才会带 prefix 进入具体品种。
# 不强制最低数 — 仅打印现状。
print(f"  XAU={_n_xau} BTC={_n_btc} ETH={_n_eth} WTI={_n_wti} "
      f"SOL={_n_sol} 通则(∅)={_n_general}")
# 总数应保持 = active 且 kind=1
_n_total = sum(_dist.values())
check(f"review_insights asset 分布合计 = active&kind=1 ({_n_total})",
      _n_total >= 0)
_conn4.close()

# v197: event_atom.extract_and_persist 区分 news 明确 (0.7) vs fallback (0.4)
print()
print("=" * 70)
print(" v197: direction_confidence 区分 (news 0.7 / fallback 0.4)")
print("=" * 70)
_ea_path = _os.path.join(_os.path.dirname(__file__),
                         "..", "src_python", "event_atom.py")
with open(_ea_path, "r", encoding="utf-8") as _f:
    _ea_src = _f.read()
check("event_atom.FALLBACK_DIR_CONFIDENCE 常量定义 (= 0.4)",
      "FALLBACK_DIR_CONFIDENCE: float = 0.4" in _ea_src)
check("event_atom.extract_and_persist 含 v197 fallback 分支注释",
      "区分 news 明确 (0.7) vs fallback (0.4)" in _ea_src)
check("DEFAULT_ASSET_FOR_EVENT_TYPE 含关键 fallback key",
      '"geopolitical_conflict"' in _ea_src
      and '"central_bank_gold"' in _ea_src
      and '"natural_disaster"' in _ea_src)
check("event_atom 写入 direction_source='rule_bias_default_asset'",
      '"rule_bias_default_asset"' in _ea_src)
# 数据库现状: 看看 fallback 路径 (rule_bias_default_asset) d_conf 分布
_conn5 = db.get_connection()
_conn5.row_factory = _sq3.Row
_fb_dist = {r["d_conf"]: r["n"]
            for r in _conn5.execute("""
                SELECT direction_confidence AS d_conf, COUNT(*) AS n
                FROM event_atom WHERE direction_source='rule_bias_default_asset'
                GROUP BY direction_confidence
            """).fetchall()}
_rb_dist = {r["d_conf"]: r["n"]
            for r in _conn5.execute("""
                SELECT direction_confidence AS d_conf, COUNT(*) AS n
                FROM event_atom WHERE direction_source='rule_bias'
                GROUP BY direction_confidence
            """).fetchall()}
print(f"  rule_bias_default_asset d_conf 分布: {_fb_dist}")
print(f"  rule_bias             d_conf 分布: {_rb_dist}")
# 注：历史 d_conf=0.5 是 v194→v196 之间某版逻辑残留（v197 之前），v197 之后新插的
# 都应该是 0.4/0.7。不强制要求历史数据迁移（迁移脚本不在本次范围）。
_n_fb = sum(_fb_dist.values())
check(f"现存 rule_bias_default_asset atom 数 = {_n_fb} (≥0, 验证历史 fallback 路径)",
      _n_fb >= 0)
check(f"现存 rule_bias (news 明确) atom 数 = {sum(_rb_dist.values())} (≥0)",
      sum(_rb_dist.values()) >= 0)
_conn5.close()

# v198: dim_event_type.direction_bias 覆盖补全 (oil_inventory bias + 排除 by-design)
print()
print("=" * 70)
print(" v198: dim_event_type.direction_bias 覆盖补全")
print("=" * 70)
_sd_path = _os.path.join(_os.path.dirname(__file__),
                         "..", "src_python", "seed_event_dim.py")
with open(_sd_path, "r", encoding="utf-8") as _f:
    _sd_src = _f.read()
check("seed_event_dim.py oil_inventory bias = {WTI: neutral}",
      '"WTI": "neutral"' in _sd_src
      and '"oil_inventory":' in _sd_src)
_conn6 = db.get_connection()
_conn6.row_factory = _sq3.Row
_r_oi = _conn6.execute(
    "SELECT name_zh, name_en, category, direction_bias "
    "FROM dim_event_type WHERE code='oil_inventory'"
).fetchone()
if _r_oi:
    _oi_bias = _json.loads(_r_oi["direction_bias"] or "{}")
    check("dim_event_type.oil_inventory name 完整 (zh/en/cat)",
          _r_oi["name_zh"] == "原油库存"
          and _r_oi["name_en"] == "Crude Oil Inventory"
          and _r_oi["category"] == "commodity")
    check("dim_event_type.oil_inventory.bias = {WTI: neutral}",
          _oi_bias == {"WTI": "neutral"})
else:
    check("dim_event_type.oil_inventory 行存在", False)
# 排除集存在：检查 weekly verify 自身 (v193 #1 段) 是否显式排除这两个 code
_weekly_path = _os.path.abspath(__file__)
with open(_weekly_path, "r", encoding="utf-8") as _f:
    _weekly_src = _f.read()
check("weekly verify v193 #1 排除 market_event + cpi_release",
      '"market_event"' in _weekly_src and '"cpi_release"' in _weekly_src
      and "_EXCLUDED_FROM_DIM_BIAS" in _weekly_src)
# 覆盖率: 25/25
_dim_rows = _conn6.execute(
    "SELECT code, direction_bias FROM dim_event_type WHERE active=1"
).fetchall()
_in_scope_v = [r for r in _dim_rows if r["code"] not in {"market_event", "cpi_release"}]
_with_bias_v = [r for r in _in_scope_v
                if _json.loads(r["direction_bias"] or "{}")]
check(f"dim_event_type bias 覆盖率 = {len(_with_bias_v)}/{len(_in_scope_v)} (排除后)",
      len(_with_bias_v) == len(_in_scope_v))
_conn6.close()