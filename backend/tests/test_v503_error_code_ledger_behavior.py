#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v503：错误码台账的行为测试（error_code_ledger_build 从 18% 起步）。

这块回答的是一个很尖锐的问题：33 个错误码里，到底哪几个真的改善过结果？
台账最怕两件事：① 用"没触发"当成"没用"；② 用三种 tier 混算胜率。
两条都在这里钉死。
"""
import sqlite3
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "src_python"))

import error_code_ledger_build as ec


# ── 注册表本身：33 个码、分层固定 ──────────────────────────────
def test_registry_is_43_codes():
    # 注意：README 里"33 个错因码"是叙事口径（33 类错因），
    # 注册表实际登记 43 个码（E001a–E001j 拆细后变多）。两个数都要能对上。
    assert len(ec.REGISTRY) == 43


def test_registry_tiers_are_only_three_kinds():
    tiers = {t for t, _l, _n in ec.REGISTRY.values()}
    assert tiers == {"hard", "soft", "research"}


def test_tier_split_counts():
    counts = {}
    for t, _l, _n in ec.REGISTRY.values():
        counts[t] = counts.get(t, 0) + 1
    # 19 hard / 8 soft / 16 research —— 数字变了说明有人动了分层，必须一起改文档
    assert counts == {"hard": 19, "soft": 8, "research": 16}


def test_research_codes_never_count_as_hard():
    # 语义没定的码（E019/E020）不许被当成 hard 参与闸门
    for code in ("E019", "E020", "E004", "E009"):
        assert ec.REGISTRY[code][0] == "research"


def test_hard_codes_include_mechanical_checks():
    for code in ("E003", "E008", "E016", "E027"):
        assert ec.REGISTRY[code][0] == "hard"


# ── 统计采集 ──────────────────────────────────────────────────

DDL = """
    CREATE TABLE decision_errors(decision_id INTEGER, error_code TEXT, created_at TEXT);
    CREATE TABLE quick_sim_trades(decision_id INTEGER PRIMARY KEY, settled INTEGER, pnl_pct REAL);
"""

FILE_DDL = DDL + """
    CREATE TABLE error_code_ledger(error_code TEXT PRIMARY KEY, tier TEXT, layer TEXT,
        first_seen_at TEXT, last_seen_at TEXT, triggers_total INTEGER, triggers_30d INTEGER,
        confirmed_bad INTEGER, rejected_good INTEGER, pnl_delta_pct REAL,
        last_evaluated_at TEXT, note TEXT);
"""


def _open_file_db(path):
    c = sqlite3.connect(str(path))
    c.executescript(FILE_DDL)
    return c


def _opened(path):
    return sqlite3.connect(str(path))


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.executescript("""
        CREATE TABLE decision_errors(decision_id INTEGER, error_code TEXT, created_at TEXT);
        CREATE TABLE quick_sim_trades(decision_id INTEGER PRIMARY KEY, settled INTEGER, pnl_pct REAL);
        CREATE TABLE error_code_ledger(error_code TEXT PRIMARY KEY, tier TEXT, layer TEXT,
            first_seen_at TEXT, last_seen_at TEXT, triggers_total INTEGER, triggers_30d INTEGER,
            confirmed_bad INTEGER, rejected_good INTEGER, pnl_delta_pct REAL,
            last_evaluated_at TEXT, note TEXT);
    """)
    yield c
    c.close()


def _trade(c, did, pnl, settled=1):
    c.execute("INSERT INTO quick_sim_trades VALUES(?,?,?)", (did, settled, pnl))


def _err(c, did, code, ts="2026-11-20 10:00:00"):
    c.execute("INSERT INTO decision_errors VALUES(?,?,?)", (did, code, ts))


def _by_code(items):
    return {i["error_code"]: i for i in items}


def test_collect_counts_and_confirms_bad(conn):
    _trade(conn, 1, -3.0)
    _trade(conn, 2, 2.0)
    _err(conn, 1, "E001")
    _err(conn, 2, "E001")
    out = _by_code(ec.collect(conn))
    e001 = out["E001"]
    assert e001["triggers_total"] == 2
    assert e001["confirmed_bad"] == 1        # 触发且亏
    assert e001["rejected_good"] == 1        # 触发但赚（可能拦错了）


def test_collect_registry_codes_with_zero_triggers_are_included(conn):
    out = _by_code(ec.collect(conn))
    assert len(out) == 43                     # 注册表全量都在台账里
    assert out["E005"]["triggers_total"] == 0
    assert "无样本 ≠ 无效" in out["E005"]["note"]


def test_collect_unknown_code_defaults_to_research(conn):
    _trade(conn, 1, 1.0)
    _err(conn, 1, "E999")
    out = _by_code(ec.collect(conn))
    assert out["E999"]["tier"] == "research"
    assert "未在注册表中登记" in out["E999"]["note"]


def test_pnl_delta_compares_against_untouched_trades(conn):
    _trade(conn, 1, -4.0)     # 触发组
    _trade(conn, 2, 4.0)      # 未触发组
    _err(conn, 1, "E003")
    out = _by_code(ec.collect(conn))
    # 全局均值 = 0；触发组均值 = -4 → delta = -4
    assert out["E003"]["pnl_delta_pct"] == pytest.approx(-4.0)


def test_pnl_delta_none_when_no_settled_global(conn):
    out = _by_code(ec.collect(conn))
    assert out["E001"]["pnl_delta_pct"] is None


def test_unsettled_trades_do_not_count(conn):
    _trade(conn, 1, -5.0, settled=0)
    _err(conn, 1, "E001")
    out = _by_code(ec.collect(conn))
    assert out["E001"]["confirmed_bad"] == 0
    assert out["E001"]["pnl_delta_pct"] is None


def test_collect_keeps_first_and_last_seen(conn):
    _trade(conn, 1, 1.0)
    _err(conn, 1, "E001", "2026-10-01 00:00:00")
    _err(conn, 2, "E001", "2026-11-20 00:00:00")
    out = _by_code(ec.collect(conn))
    assert out["E001"]["first_seen_at"] == "2026-10-01 00:00:00"
    assert out["E001"]["last_seen_at"] == "2026-11-20 00:00:00"


# ── 落库：dry-run 不写，正式跑幂等 ────────────────────────────
UPSERT_EXEC = ec.UPSERT


def _write(conn, items):
    with conn:
        for it in items:
            conn.execute(UPSERT_EXEC, (
                it["error_code"], it["tier"], it["layer"],
                it["first_seen_at"], it["last_seen_at"],
                it["triggers_total"], it["triggers_30d"],
                it["confirmed_bad"], it["rejected_good"],
                it["pnl_delta_pct"], it["evaluated_at"], it["note"]))


def test_upsert_is_idempotent(conn):
    _trade(conn, 1, 1.0)
    _err(conn, 1, "E001")
    items = ec.collect(conn)
    _write(conn, items)
    _write(conn, items)
    assert conn.execute("SELECT COUNT(*) FROM error_code_ledger").fetchone()[0] == 43


def test_upsert_updates_counters_not_duplicates(conn):
    _trade(conn, 1, 1.0)
    _err(conn, 1, "E001")
    _write(conn, ec.collect(conn))
    _err(conn, 1, "E001")
    _write(conn, ec.collect(conn))
    row = conn.execute(
        "SELECT triggers_total FROM error_code_ledger WHERE error_code='E001'").fetchone()
    assert row[0] == 2


def test_main_dry_run_does_not_write(tmp_path, monkeypatch):
    # main() 会在 finally 里 close 连接，所以用文件库，事后重开一次核对
    path = tmp_path / "ledger.db"
    c = _open_file_db(path)
    _trade(c, 1, 1.0)
    _err(c, 1, "E001")
    c.commit()
    c.close()
    monkeypatch.setattr(ec, "_connect", lambda: _opened(path))
    assert ec.main(["--dry-run"]) == 0
    c2 = _opened(path)
    try:
        assert c2.execute("SELECT COUNT(*) FROM error_code_ledger").fetchone()[0] == 0
    finally:
        c2.close()


def test_main_commit_path_writes_all_codes(tmp_path, monkeypatch, capsys):
    path = tmp_path / "ledger.db"
    c = _open_file_db(path)
    _trade(c, 1, -2.0)
    _err(c, 1, "E001")
    c.commit()
    c.close()
    monkeypatch.setattr(ec, "_connect", lambda: _opened(path))
    assert ec.main([]) == 0
    out = capsys.readouterr().out
    assert "已写入 43 行" in out
    assert "hard" in out and "research" in out
    c2 = _opened(path)
    try:
        assert c2.execute("SELECT COUNT(*) FROM error_code_ledger").fetchone()[0] == 43
    finally:
        c2.close()
