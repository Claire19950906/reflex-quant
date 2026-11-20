# -*- coding: utf-8 -*-
"""v482 测试：孤儿清理 / 闭环体检 / 多周期 CLI。

覆盖三条"可测性"改造：
  1. `decision_orphans.purge` 只删真孤儿，不动两个权威编号空间里的行，且大比例删除被拒；
  2. `insight_loop_audit.build` 能在一张最小库上跑通（不依赖真实库），并给出结论；
  3. `multi_horizon_settle.main` 有独立 CLI（--apply 才写库）。
"""
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src_python"))

import decision_orphans as do
import insight_loop_audit as audit


def _mkdb(tmp_path):
    p = tmp_path / "t.db"
    conn = sqlite3.connect(str(p))
    conn.executescript("""
    CREATE TABLE ai_decisions(id INTEGER PRIMARY KEY, created_at TEXT,
        analysis_degraded INTEGER DEFAULT 0, analysis_degraded_reason TEXT,
        suggested_action TEXT, impact_score REAL, market_confirmation TEXT,
        hard_data_status TEXT, insight_hit_ids TEXT, code_fingerprint TEXT);
    CREATE TABLE trade_decisions(id INTEGER PRIMARY KEY, ai_decision_id INTEGER,
        asset TEXT, final_action TEXT, entry_price REAL, decision_ts REAL,
        actual_outcome_pct REAL);
    CREATE TABLE decision_errors(id INTEGER PRIMARY KEY, decision_id INTEGER,
        error_code TEXT);
    CREATE TABLE decision_outcomes_horizon(decision_id INTEGER, horizon TEXT,
        status TEXT, PRIMARY KEY(decision_id, horizon));
    CREATE TABLE structured_reflections(id INTEGER PRIMARY KEY, decision_id INTEGER,
        status TEXT DEFAULT 'pending', verdict TEXT DEFAULT '');
    CREATE TABLE review_insights(id INTEGER PRIMARY KEY, rule TEXT, active INTEGER DEFAULT 1,
        kind INTEGER DEFAULT 1, origin TEXT DEFAULT 'real', source_samples INTEGER DEFAULT 0,
        verification_sample_n INTEGER DEFAULT 0, verification_verdict TEXT DEFAULT '',
        insight_param_key TEXT DEFAULT '', insight_direction INTEGER DEFAULT 0,
        observe_only INTEGER DEFAULT 0, observe_reason TEXT DEFAULT '', asset TEXT DEFAULT '',
        created_at TEXT);
    """)
    conn.execute("INSERT INTO ai_decisions(id) VALUES (1),(2)")
    conn.execute("INSERT INTO trade_decisions(id,ai_decision_id) VALUES (10,1)")
    # 真孤儿：指向不存在的 999；有效：指向 ai=1 / trade=10
    conn.execute("INSERT INTO decision_errors(id,decision_id,error_code) VALUES"
                 "(1,999,'E001a'),(2,1,'E004'),(3,10,'E005')")
    conn.execute("INSERT INTO decision_outcomes_horizon VALUES (10,'15m','settled')")
    conn.execute("INSERT INTO structured_reflections(id,decision_id) VALUES (1,999)")
    conn.commit()
    return conn, str(p)


def test_purge_only_real_orphans(tmp_path):
    conn, _ = _mkdb(tmp_path)
    assert {r["table"] for r in do.audit(conn)} == {"decision_errors",
                                                    "structured_reflections"}
    res = do.purge(conn, dry_run=False, dump_dir=str(tmp_path))
    conn.commit()
    assert res["deleted"] == 2
    left = [r[0] for r in conn.execute("SELECT decision_id FROM decision_errors ORDER BY id")]
    assert left == [1, 10], "两个权威空间里的行必须保留"
    assert conn.execute("SELECT COUNT(*) FROM decision_outcomes_horizon").fetchone()[0] == 1
    assert os.path.exists(res["dump"])
    # 幂等
    assert do.purge(conn, dry_run=False, dump_dir=str(tmp_path))["total_orphans"] == 0


def test_purge_refuses_mass_deletion(tmp_path):
    conn, _ = _mkdb(tmp_path)
    conn.execute("DELETE FROM ai_decisions")
    conn.execute("DELETE FROM trade_decisions")
    for i in range(1500):
        conn.execute("INSERT INTO decision_errors(id,decision_id) VALUES (?,?)", (100 + i, 5))
    conn.commit()
    res = do.purge(conn, dry_run=False, dump_dir=str(tmp_path))
    assert any(r["table"] == "decision_errors" for r in res["refused"])
    assert conn.execute("SELECT COUNT(*) FROM decision_errors").fetchone()[0] == 1503


def test_audit_build_on_minimal_db(tmp_path, monkeypatch):
    conn, path = _mkdb(tmp_path)
    conn.execute("INSERT INTO review_insights(id,rule,created_at) VALUES"
                 "(1,'[score.buy_min_score] 提高门槛至 0.6，24h 胜率需 >45%','2026-11-19 10:00:00')")
    conn.commit()
    monkeypatch.setattr(audit, "DB_PATH", path)
    d = audit.build()
    assert d["decisions"]["total"] == 2
    assert d["insights"]["levels"]["actionable"] == 1
    assert d["insights"]["with_key"] == 1
    text = audit.render_text(d)
    assert "洞察闭环体检" in text
    assert "验证窗口未开" in text


def test_multi_horizon_has_cli():
    import multi_horizon_settle as mh
    assert callable(mh.main)
    # 演练模式不应写库：无 --apply 时 commit=False
    src = open(mh.__file__, encoding="utf-8").read()
    assert "commit=args.apply" in src


def test_common_llm_budget_shim():
    """额度闸门曾因 `from common import llm_budget` ImportError 被静默放行。"""
    from common import llm_budget as lb
    assert callable(lb.check)
    assert lb.request_budget() >= 0


def test_jin10_quota_persists_across_processes(tmp_path, monkeypatch):
    """配额冷却落盘：cron 每起一个新进程都要重试 7 个 token 才认账（实测 327 次/小时）。"""
    from feeds import jin10_mcp as m
    monkeypatch.setattr(m, "_QUOTA_STATE_FILE", str(tmp_path / "q.json"))
    monkeypatch.setattr(m, "_quota_disk_cache", {"mtime": 0.0, "data": {}})
    m._quota_exhausted_until.clear()
    m._mark_token_exhausted(0, "get_kline")
    assert m._read_quota_disk().get("0:get_kline", 0) > 0
    # 模拟"新进程"：清掉内存，只靠磁盘
    m._quota_exhausted_until.clear()
    assert m._token_is_exhausted(0, "get_kline") is True


def test_stock_price_negative_cache(monkeypatch):
    """行情源连续失败时，同一票不应每分钟重打一次（实测 1657 次 SSL 失败/天）。"""
    import time
    import stock_prices as sp
    monkeypatch.setitem(sp._price_cache, "V482ZZ",
                        {"price": None, "ts": time.time(), "source": "", "ttl": 300})
    assert sp.fetch_stock_price("V482ZZ") is None


def test_run_once_backfills_long_horizons(monkeypatch):
    """第一遍只扫最近 limit 条，1d/5d 成熟时决策可能已掉出窗口 → 必须补扫。"""
    import sqlite3
    import time
    import multi_horizon_settle as mh

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE trade_decisions(id INTEGER, asset TEXT,"
                 " final_action TEXT, entry_price REAL, decision_ts REAL)")
    now = int(time.time())
    conn.execute("INSERT INTO trade_decisions VALUES (1,'BTC','BUY',100,?)",
                 (now - 8 * 86400,))
    conn.execute("INSERT INTO trade_decisions VALUES (2,'BTC','BUY',100,?)",
                 (now - 1800,))
    mh.init_schema(conn)
    seen = []
    monkeypatch.setattr(mh, "settle_horizons",
                        lambda c, **kw: (seen.append(kw["decision_id"])
                                         or {"written": 0, "horizons": {}}))
    out = mh.run_once(conn, now_ts=now, limit=1, commit=False)
    assert sorted(seen) == [1, 2]          # 老决策被补扫
    assert out["backfill"] >= 1
