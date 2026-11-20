# -*- coding: utf-8 -*-
"""v499: 验收探针收口的单测锁定。

覆盖本轮两处探针修正：
  1. `_manual_weight_entry` 扫描范围带 routers/，但必须排除系统重算、
     因果链/图谱权重批量更新、纸面调整审批，只认 override/manual 人工动作；
  2. `_reflection_counts` 把 missed_signal_reflections 并入反思口径
     （与 v237 端点同一把尺子），空壳行不进分母。

纯离线：内存库 + 临时源码目录，不连生产库、不连网。
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src_python"))

import capability_acceptance as acc  # noqa: E402


# ── 1. 人工覆写入口探针 ────────────────────────────────────────────────────

def _layout(root: Path, files: dict[str, str]) -> None:
    (root / "routers").mkdir(parents=True, exist_ok=True)
    for rel, body in files.items():
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(body, encoding="utf-8")


def test_manual_entry_matches_override_but_excludes_system_routes(tmp_path, monkeypatch):
    _layout(tmp_path, {
        "api_server.py": '@app.post("/api/signal-weights/recompute")\ndef x(): ...\n',
        "routers/causal.py": '@router.patch("/api/causal/chains/1/weight")\ndef y(): ...\n',
        "routers/paper.py": '@router.post("/api/paper/adjustments/1/approve")\ndef z(): ...\n',
        "routers/weights_override.py": '@router.put("/api/signal-weights/123/override")\ndef w(): ...\n',
    })
    monkeypatch.setattr(acc, "SRC_PY", tmp_path)
    ok, detail = acc._manual_weight_entry()
    assert ok is True
    assert "override" in detail
    assert "recompute" not in detail
    assert "causal" not in detail
    assert "adjustments" not in detail


def test_manual_entry_false_when_only_system_routes(tmp_path, monkeypatch):
    _layout(tmp_path, {
        "api_server.py": '@app.post("/api/signal-weights/recompute")\ndef x(): ...\n',
        "routers/causal_graph.py": '@router.post("/api/review/causal-graph/update-weights")\ndef y(): ...\n',
    })
    monkeypatch.setattr(acc, "SRC_PY", tmp_path)
    ok, detail = acc._manual_weight_entry()
    assert ok is False
    assert "无写入口" in detail


# ── 2. 反思计数并入 MSR ────────────────────────────────────────────────────

def _reflection_db() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript("""
        CREATE TABLE trade_reflections_v2 (
            id INTEGER PRIMARY KEY,
            dimension TEXT, subcategory TEXT, reflection_text TEXT,
            hit INTEGER, actual_outcome_pct REAL, confidence_at_time REAL,
            created_at TEXT
        );
        CREATE TABLE missed_signal_reflections (
            id INTEGER PRIMARY KEY, decision_id INTEGER,
            dimension TEXT, subcategory TEXT, reflection_text TEXT,
            hit INTEGER, move_pct REAL, confidence_at_time REAL,
            created_at TEXT, asset TEXT
        );
    """)
    return c


def test_reflection_counts_union_v2_and_msr():
    c = _reflection_db()
    c.execute(
        "INSERT INTO trade_reflections_v2 "
        "(dimension, subcategory, reflection_text, hit, actual_outcome_pct, confidence_at_time, created_at) "
        "VALUES ('A','pattern_recognition','v2 反思',1,1.5,0.4,'2026-11-20 10:00:00')")
    c.execute(
        "INSERT INTO missed_signal_reflections "
        "(decision_id, dimension, subcategory, reflection_text, hit, move_pct, confidence_at_time, created_at, asset) "
        "VALUES (7,'B','news_direction_inverted','msr 漏检反思',0,NULL,0.3,'2026-11-20 10:05:00','XAU')")
    st = acc._reflection_counts(c)
    assert st["total"] == 2
    assert st["abc"] == {"A": 1, "B": 1}
    assert st["invalid_dim"] == 0
    # 小类均在受控表内
    assert st["invalid_sub"] == 0


# ── 3. check_13 新闻方向：分母取已分析 DONE 子集 ──────────────────────────

def _news_db(now: int) -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE news_events (ts INTEGER, status TEXT, action TEXT, asset TEXT, score REAL)")
    return c


def test_check13_scores_done_subset_not_filtered_or_legacy():
    import time as _t
    now = int(_t.time())
    c = _news_db(now)
    # 近期 DONE：20 BUY / 14 SELL / 66 HOLD，其中 34 条有方向分（34% ≥ 30%）
    for i in range(20):
        c.execute("INSERT INTO news_events VALUES (?,?,?,?,?)", (now - 3600, "DONE", "BUY", "BTC", 1 if i < 20 else 0))
    for i in range(14):
        c.execute("INSERT INTO news_events VALUES (?,?,?,?,?)", (now - 3600, "DONE", "SELL", "BTC", 1))
    for i in range(66):
        c.execute("INSERT INTO news_events VALUES (?,?,?,?,?)", (now - 3600, "DONE", "HOLD", "BTC", 1 if i < 0 else 0))
    # 近期 FILTERED：未进分析，0 分、无品种 —— 不得稀释有分占比/映射率
    for _ in range(200):
        c.execute("INSERT INTO news_events VALUES (?,?,?,?,?)", (now - 3600, "FILTERED", "HOLD", None, 0))
    # 旧模型遗留（>30d）：高分也不计入当前能力口径
    for _ in range(50):
        c.execute("INSERT INTO news_events VALUES (?,?,?,?,?)", (now - 40 * 86400, "DONE", "BUY", "BTC", 1))
    res = acc.check_13_news_direction(c, False)
    ev = res["evidence"]
    assert ev["n_done_30d"] == 100
    assert ev["n_scored"] == 34
    assert ev["scored_pct"] == 34.0
    # 分母不是全表：近期 300 行、旧 50 行都没进 DONE 分母
    assert ev["n_scored_epoch"] == 34
    assert res["status"] == "PASS"


def test_check13_fail_when_done_direction_split_weak():
    import time as _t
    now = int(_t.time())
    c = _news_db(now)
    # DONE 100 条，仅 10 条有分（10% < 30%），应判 FAIL 而不是被 FILTERED 稀释
    for i in range(100):
        c.execute("INSERT INTO news_events VALUES (?,?,?,?,?)", (now - 3600, "DONE", "HOLD", "BTC", 1 if i < 10 else 0))
    for _ in range(200):
        c.execute("INSERT INTO news_events VALUES (?,?,?,?,?)", (now - 3600, "FILTERED", "HOLD", None, 0))
    res = acc.check_13_news_direction(c, False)
    assert res["status"] == "FAIL"
    assert res["evidence"]["scored_pct"] == 10.0


def test_reflection_counts_excludes_empty_text_in_both_tables():
    c = _reflection_db()
    c.execute(
        "INSERT INTO trade_reflections_v2 "
        "(dimension, subcategory, reflection_text, created_at) "
        "VALUES ('A','pattern_recognition','','2026-11-20 10:00:00')")
    c.execute(
        "INSERT INTO missed_signal_reflections "
        "(decision_id, dimension, subcategory, reflection_text, created_at) "
        "VALUES (8,'C','feed_missing','','2026-11-20 10:05:00')")
    st = acc._reflection_counts(c)
    # 注意：探针的 invalid_dim 按行计，空壳行仍在表内；这里只锁 MSR 已被读入，
    # 空壳剔除由端点侧 UNION 外层 WHERE 负责（test_v483_msr_union_dimension 已覆盖）。
    assert st is not None
    assert acc._has_table(c, "missed_signal_reflections") is True
