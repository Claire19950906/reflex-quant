#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v514：异常异动量纲过滤的行为测试。

背景：历史库里出现过 move_pct = -9867% 这种脏值（量纲算错），
一旦进决策队列就会产出"跌 9867%"这种不可能的单，直接污染胜率与归因。
所以这里钉三件事：
  ① 决策队列必须只收 |move_pct| ≤ 90，边界是闭区间，NULL 按 0 处理；
  ② 所有"读 move_pct 做判断"的地方都必须带同样的上界 —— 少一处就是漏一条脏值；
  ③ 编排层（回填 / 实时）该计的数必须计对，不能被脏值带偏。
"""
import ast
import re
import sqlite3
import sys
import time
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "src_python"))
SRC = BACKEND / "src_python"

import decision_engine as de

DDL = """
CREATE TABLE critical_price_moves(
    id INTEGER PRIMARY KEY, asset TEXT, ts INTEGER, start_price REAL, peak_price REAL,
    end_price REAL, move_pct REAL, move_kind TEXT, duration_min INTEGER,
    algo_score REAL, context_json TEXT
);
CREATE TABLE trade_decisions(
    id INTEGER PRIMARY KEY, asset TEXT, final_action TEXT, entry_price REAL,
    decision_ts INTEGER, event_strength TEXT, context_json TEXT, ai_decision_id INTEGER,
    status TEXT, settled_at INTEGER, actual_outcome_pct REAL
);
CREATE TABLE trade_reflections_v2(
    id INTEGER PRIMARY KEY AUTOINCREMENT, decision_id INTEGER UNIQUE, paper_run_id INTEGER,
    asset TEXT, action TEXT, entry_price REAL, pnl_pct REAL,
    dimension TEXT DEFAULT '', subcategory TEXT DEFAULT '', hit INTEGER DEFAULT 0,
    actual_outcome_pct REAL, confidence_at_time REAL, settled INTEGER DEFAULT 0,
    created_at TEXT
);
CREATE TABLE ohlcv_daily(asset TEXT, ts INTEGER, close REAL);
"""


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(DDL)
    return c


def _mv(conn, sid, move_pct, ts=None, algo=0.9, asset="BTC"):
    conn.execute("INSERT INTO critical_price_moves VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 (sid, asset, ts if ts is not None else int(time.time()) - 3600,
                  100.0, 100.0, 100.0, move_pct, "spike", 30, algo, "{}"))
    conn.commit()


# ── 纪律①：量纲过滤 ────────────────────────────────────────
def test_dirty_move_pct_is_excluded(conn):
    _mv(conn, 1, -9867.0)      # 历史真实出现过的脏值
    _mv(conn, 2, 5.0)
    rows = de.list_critical_moves(conn, days=1, threshold=0.7)
    assert [r["id"] for r in rows] == [2], "脏值必须挡在决策队列之外"


def test_boundary_90_is_included_and_over_is_not(conn):
    _mv(conn, 1, 90.0)
    _mv(conn, 2, -90.0)
    _mv(conn, 3, 90.5)
    _mv(conn, 4, -91.0)
    rows = de.list_critical_moves(conn, days=1, threshold=0.7)
    assert sorted(r["id"] for r in rows) == [1, 2], "边界闭区间：±90 收，90.5 不收"


def test_null_move_pct_counts_as_zero(conn):
    _mv(conn, 1, None)
    rows = de.list_critical_moves(conn, days=1, threshold=0.7)
    assert [r["id"] for r in rows] == [1]


def test_threshold_and_window_filters(conn):
    now = int(time.time())
    _mv(conn, 1, 3.0, ts=now - 3600, algo=0.9)
    _mv(conn, 2, 3.0, ts=now - 3600, algo=0.5)      # 分数不够
    _mv(conn, 3, 3.0, ts=now - 10 * 86400)          # 超出天数窗口
    _mv(conn, 4, 1.0, ts=now - 2 * 3600)
    rows = de.list_critical_moves(conn, days=1, threshold=0.7)
    assert [r["id"] for r in rows] == [4, 1], "按 ts 升序"


def test_rows_carry_fields_needed_by_producer(conn):
    _mv(conn, 1, 4.0, algo=0.85)
    r = de.list_critical_moves(conn, 1, 0.7)[0]
    for col in ("id", "asset", "ts", "start_price", "peak_price", "end_price",
                "move_pct", "move_kind", "duration_min", "algo_score", "context_json"):
        assert col in r.keys(), f"生产者需要 {col}"


# ── 纪律②：所有读 move_pct 的地方都带上界 ──────────────────
BOUNDED_SQL = re.compile(r"ABS\(.{0,40}move_pct.{0,40}\)\s*(<=|<|>=|>)", re.I)
BY_KEY_SQL = re.compile(r"\b(id|signal_id)\s*=\s*\?", re.I)

# 已知的"没有量纲上界"读法 —— 全部是研究/展示口径，不参与下单与绩效统计。
# 新增项必须显式登记，否则用例失败：防止以后有人悄悄扩大暴露面。
KNOWN_UNBOUNDED = {
    ("bridge_builders.py", "build_"): "面板列表：只按分数与时间筛，脏值会出现在只读列表",
    ("opt_move_shape.py", "main"): "离线参数搜索脚本，不在线上路径",
    ("opt_sector_beta.py", "main"): "离线参数搜索脚本，不在线上路径",
    ("opt_weight_shadow.py", "main"): "离线影子权重搜索，不在线上路径",
    ("seed_trade_reflections.py", "main"): "造种子数据的一次性脚本",
    ("nlq.py", ""): "自然语言查询聚合，只做展示",
    ("evaluate_missed_signals.py", "_move_pct_from_signal"): "纯算术：从信号价算涨跌幅，不读库",
    ("event_reverse_lookup.py", ""): "按时间就近反查，不参与计算",
}


def _strings_of(node):
    """把一个函数（或整模块）里的所有字符串片段拼起来，动态拼 SQL 也能看见。"""
    out = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            out.append(sub.value)
    return "\n".join(out)


def _scan():
    offenders = []
    for path in sorted(SRC.glob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError:
            continue
        fns = [n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        if not fns:
            fns = [tree]
        seen = []
        for fn in fns:
            blob = _strings_of(fn)
            if "critical_price_moves" not in blob:
                continue
            if not re.search(r"move_pct", blob, re.I):
                continue
            if BOUNDED_SQL.search(blob) or BY_KEY_SQL.search(blob):
                continue
            if re.search(r"(INSERT\s+INTO|UPDATE)\s+critical_price_moves", blob, re.I):
                continue  # 生产者：往表里写数，不需要读时判量纲
            name = getattr(fn, "name", "")
            if (path.name, name) not in seen:
                seen.append((path.name, name))
        offenders.extend(seen)
    return offenders


def test_every_move_pct_reader_has_upper_bound():
    """静态兜底：读 move_pct 却不判量纲的函数必须显式登记。"""
    offenders = _scan()
    def _registered(fn):
        for (f, scope), why in KNOWN_UNBOUNDED.items():
            if f == fn:
                return why
        return None
    unexpected = [o for o in offenders if _registered(o[0]) is None]
    assert not unexpected, "新的无上界读法必须显式登记：\n" + "\n".join(map(str, unexpected))


def test_decision_path_readers_are_all_bounded():
    """下单与绩效统计走的文件，一个无上界读法都不许有。"""
    critical = {"decision_engine.py", "calc_event_move_attribution.py"}
    bad = [o for o in _scan() if o[0] in critical]
    assert not bad, "决策/归因路径出现无上界读法：" + str(bad)


# ── 纪律③：编排层计数 ─────────────────────────────────────
@pytest.fixture()
def wired(conn, monkeypatch):
    monkeypatch.setattr(de, "_db", lambda: conn, raising=False)

    def _propose(c, mv):
        if mv["move_pct"] and mv["move_pct"] > 0:
            return {"action": "inserted", "decision_id": int(mv["id"]), "reason": "BUY"}
        return {"action": "skipped", "decision_id": None, "reason": "dup"}

    monkeypatch.setattr(de, "propose_one", _propose, raising=False)
    monkeypatch.setattr(de, "settle_one",
                        lambda c, d: {"action": "skipped", "decision_id": d["id"]}, raising=False)
    return conn


def test_run_live_once_counts_and_action_dist(wired):
    now = int(time.time())
    _mv(wired, 1, 5.0, ts=now - 600)
    _mv(wired, 2, -5.0, ts=now - 700)
    _mv(wired, 3, 0.0, ts=now - 800)                 # 映射成 skipped
    _mv(wired, 4, 400.0, ts=now - 900)              # 脏值，不该进
    _mv(wired, 5, 5.0, ts=now - 7200)               # 2 小时前，超出 1 小时窗口
    summary = de.run_live_once(threshold=0.7)
    assert summary["inserted"] == 1, "窗口内只有一条正向异动（脏值、负向、过期都不算）"
    assert summary["skipped_dup"] == 2, "负向和平盘走 skipped 路径，但要计数"
    assert summary["action_dist"]["BUY"] == 1
    assert summary["phase"] == "live_once"


def test_run_backfill_reports_moves_seen(wired):
    _mv(wired, 1, 5.0)
    _mv(wired, 2, -4.0)
    _mv(wired, 3, -9867.0)          # 脏值
    summary = de.run_backfill(days=30, threshold=0.7)
    assert summary["moves_seen"] == 2, "脏值不进候选，计数里也不该出现"
    assert summary["inserted"] == 1
    assert summary["phase"] == "backfill"
    assert summary["days"] == 30
