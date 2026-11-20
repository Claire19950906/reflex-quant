"""v401 禁止假数据：guard / purge 回归测试（全离线，内存库）"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src_python"))

import fake_data
import db as _db_mod


def _db() -> sqlite3.Connection:
    """内存库：直接用产品的 db.migrate() 建全 schema。

    以前这里手写了 6 张"薄表"（quick_sim_trades 6 列、ai_decisions 5 列…），
    生产代码查 trade_uuid / decision_id / entry_ts 就 no such column ——
    那是夹具比实现"薄"，不是应用 bug（真实库这些列都在）。
    v406 改成跟生产同源：migrate 建 110 张表，测试断言才有意义。
    """
    con = sqlite3.connect(":memory:")
    _db_mod.migrate(con)
    return con


def _seed(con: sqlite3.Connection) -> None:
    # v406: schema 换成真实表后，必须显式写列名 —— 真实表有 100+ 列，
    # 位置式 INSERT 会把值塞进错误的列。
    con.execute("INSERT INTO raw_news (id, source, content, timestamp, gate_impact_1to100) "
                "VALUES (1,'sina','真新闻正文','2026-11-17 08:00:00',0)")
    con.execute("INSERT INTO raw_news (id, source, content, timestamp, gate_impact_1to100) "
                "VALUES (2,'demo','演示源新闻','2026-11-17 08:00:00',0)")
    d = ("INSERT INTO ai_decisions "
         "(id, news_id, sentiment_score, suggested_action, reasoning, created_at, "
         " evidence_confidence, impact_score, analysis_degraded) "
         "VALUES (?,?,?,?,?,?,?,?,?)")
    stamp = "2026-11-17 08:00:00"

    def _dec(i, news_id, conf, impact, degraded):
        # sentiment_score/suggested_action/reasoning/created_at 在真实表里是 NOT NULL
        con.execute(d, (i, news_id, 0.5, "HOLD", "真实分析结论", stamp, conf, impact, degraded))

    # 真决策：有门卫证据
    _dec(10, 1, 0.72, 7.0, 0)
    # F1 + F2：降级 + 置信度 0 + 影响力默认 5.0
    _dec(11, 1, 0.0, 5.0, 1)
    # F1 以外的降级行：置信度 NULL 但影响力仍默认
    _dec(12, 1, None, 5.0, 1)
    # F7 孤儿：新闻不存在
    _dec(13, 999, 0.6, 6.0, 0)
    # F3
    con.execute("INSERT INTO trade_reflections_v2 (id, confidence_at_time) VALUES (100,0.0)")
    con.execute("INSERT INTO trade_reflections_v2 (id, confidence_at_time) VALUES (101,0.55)")
    # F4：引用降级决策的假成交 + 一笔真成交
    t = ("INSERT INTO quick_sim_trades "
         "(id, decision_id, asset, action, entry_price, entry_ts, horizon_minutes, "
         " pnl_usdt, pnl_pct, settled, exit_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?)")

    def _trade(i, dec_id, pnl, pct, exit_ts):
        # asset/action/entry_price/entry_ts/horizon_minutes 在真实表里是 NOT NULL
        con.execute(t, (i, dec_id, "BTC", "BUY", 100000.0, 0, 60, pnl, pct, 1, exit_ts))

    _trade(200, 11, -1.215885, -1.2, 1800)
    _trade(201, 10, -1.210317, -1.2, 1500)
    # F5
    # decided_ts / asset / direction 在真实表里是 NOT NULL
    con.execute("INSERT INTO shadow_ledger (decision_id, decided_ts, asset, direction) "
                "VALUES (11, 1700000000, 'BTC', 'BUY')")
    con.execute("""INSERT INTO sim_equity_snapshots
                   (ts, equity, realized_pnl, n_settled, n_open, peak_equity, drawdown_pct, note)
                   VALUES (1500,9989.68,-1.2103,2,0,10000.0,-0.1,'old')""")
    con.commit()


def test_scan_finds_all_but_clean_rows():
    con = _db()
    _seed(con)
    rows = {r["code"]: r["count"] for r in fake_data.scan(con)}
    assert rows["F1"] == 1          # 只有 11（12 的置信度已经是 NULL）
    assert rows["F2"] == 2          # 11、12 默认 5.0 且门卫 0 分
    assert rows["F3"] == 1
    assert rows["F4"] == 1
    assert rows["F5"] == 1
    assert rows["F7"] == 1
    assert rows["F12"] == 1          # v483 新增：13 号决策推理链极短且未置降级（未成交，可安全补标）
    # v483：F11/F12 加入后基准从 8 → 9。两条新规则只管"状态标签假"，不删行、不删成交，
    #        且排除已成交决策，避免用改标签的手去销毁真实成交记录。
    assert fake_data.total_fake(fake_data.scan(con)) == sum(rows.values()) == 9


def test_purge_nulls_placeholders_instead_of_guessing():
    con = _db()
    _seed(con)
    fake_data.purge(con)
    assert con.execute("SELECT evidence_confidence FROM ai_decisions WHERE id=11").fetchone()[0] is None
    assert con.execute("SELECT impact_score FROM ai_decisions WHERE id=11").fetchone()[0] is None
    # 有门卫证据的真决策不许动
    assert con.execute("SELECT impact_score FROM ai_decisions WHERE id=10").fetchone()[0] == 7.0
    assert con.execute("SELECT confidence_at_time FROM trade_reflections_v2 WHERE id=100").fetchone()[0] is None


def test_purge_deletes_fake_trades_and_rebuilds_equity():
    con = _db()
    _seed(con)
    fake_data.purge(con)
    left = con.execute("SELECT id, pnl_usdt FROM quick_sim_trades").fetchall()
    assert left == [(201, -1.210317)]
    snap = con.execute("SELECT equity, realized_pnl, n_settled, note FROM sim_equity_snapshots").fetchone()
    assert round(snap[1], 4) == -1.2103 and snap[2] == 1
    assert snap[0] == 10000 + snap[1]
    assert "已剔除假成交" in snap[3]


def test_scan_after_purge_is_zero_and_guard_would_pass():
    con = _db()
    _seed(con)
    fake_data.purge(con)
    assert fake_data.total_fake(fake_data.scan(con)) == 0


def test_orphan_and_demo_source_removed():
    con = _db()
    _seed(con)
    fake_data.purge(con)
    assert con.execute("SELECT COUNT(*) FROM ai_decisions WHERE id=13").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM raw_news WHERE source='demo'").fetchone()[0] == 0


def test_guard_cli_exits_nonzero_when_fake_exists(monkeypatch, tmp_path):
    con = _db()
    _seed(con)
    con.commit()
    db = tmp_path / "fake.db"
    # 单文件库：把内存表结构复刻进去
    disk = sqlite3.connect(db)
    con.backup(disk)
    disk.close()
    monkeypatch.setattr(fake_data, "DB_PATH", db)
    monkeypatch.setattr(sys, "argv", ["fake_data.py", "--guard"])
    assert fake_data.main() == 1

    monkeypatch.setattr(fake_data, "DB_PATH", db)
    # 清理后守卫必须放行
    c2 = sqlite3.connect(db)
    fake_data.purge(c2, fake_data.scan(c2))
    c2.close()
    monkeypatch.setattr(sys, "argv", ["fake_data.py", "--guard"])
    assert fake_data.main() == 0
