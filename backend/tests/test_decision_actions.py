# -*- coding: utf-8 -*-
"""v383 · decision_actions 离线单测：静默真的拦住写入口，忽略真的退出统计，反手留快照。"""
from __future__ import annotations

from signals import decision_actions as da
from signals import decision_writer


def _seed_decision(conn, asset="XAU", action="BUY") -> int:
    conn.execute("INSERT INTO raw_news (id, source, content, timestamp) "
                 "VALUES (1,'t','x',datetime('now'))")
    cur = conn.execute(
        """INSERT INTO ai_decisions (news_id, target_asset, suggested_action, settled,
                                      sentiment_score, reasoning, created_at)
           VALUES (1, ?, ?, 0, 0.4, 'r', datetime('now'))""",
        (asset, action),
    )
    conn.commit()
    return int(cur.lastrowid)


def test_silence_then_writer_blocks(fresh_db):
    import trade_decisions
    import db as _db
    trade_decisions.init_schema(fresh_db)  # trade_decisions 不在 db.migrate 里，由该模块自建
    _db.migrate(fresh_db)  # 再跑一次幂等迁移，补 status/confidence 等列
    did = _seed_decision(fresh_db)
    res = da.record(fresh_db, did, "silence_asset", duration_min=60, note="新闻源抽风")
    assert res["ok"] is True
    assert res["asset"] == "XAU"
    assert da.is_asset_muted(fresh_db, "XAU")
    assert da.is_asset_muted(fresh_db, "BTC") is None

    wid = decision_writer.write_decision(
        fresh_db, asset="XAU", intended_action="BUY", source="test",
        confidence=80, persist_signal_weight=False,
    )
    row = fresh_db.execute(
        "SELECT final_action, decision_type, deny_reason FROM trade_decisions WHERE id=?",
        (wid,),
    ).fetchone()
    assert row[0] == "HOLD", "静默中的品种不应再放行 BUY"
    assert row[1] == "REJECT"
    assert "用户静默" in (row[2] or "")


def test_ignore_decision_quarantines(fresh_db):
    did = _seed_decision(fresh_db)
    res = da.record(fresh_db, did, "ignore_decision", note="误报")
    assert res["ok"] is True
    row = fresh_db.execute(
        "SELECT quarantined_at, quarantine_reason FROM ai_decisions WHERE id=?", (did,)
    ).fetchone()
    assert row[0], "应标记 quarantined_at"
    assert "user_ignore" in row[1]


def test_reverse_records_shadow_snapshot(fresh_db):
    did = _seed_decision(fresh_db)
    # 造两根 K 线，让反事实至少能跑（跑不了也应 ok，只是快照带 error）
    fresh_db.execute(
        """INSERT INTO kline_bars (symbol, timeframe, ts, open, high, low, close, volume)
           VALUES ('XAU','10m', 1000, 100, 101, 99, 100.5, 1),
                  ('XAU','10m', 1600, 100.5, 102, 100, 101.5, 1)"""
    )
    fresh_db.commit()
    res = da.record(fresh_db, did, "reverse", note="方向反了")
    assert res["ok"] is True
    note = fresh_db.execute(
        "SELECT note FROM decision_actions WHERE decision_id=? AND action='reverse'", (did,)
    ).fetchone()[0]
    assert "shadow_reverse" in note


def test_unknown_action_rejected(fresh_db):
    did = _seed_decision(fresh_db)
    res = da.record(fresh_db, did, "delete_everything")
    assert res["ok"] is False
    assert "未知动作" in res["error"]


def test_silence_on_none_asset_rejected(fresh_db):
    did = _seed_decision(fresh_db, asset="NONE", action="HOLD")
    res = da.record(fresh_db, did, "silence_asset", duration_min=30)
    assert res["ok"] is False


def test_unsilence_by_asset_restores_writes(fresh_db):
    import trade_decisions
    import db as _db
    trade_decisions.init_schema(fresh_db)
    _db.migrate(fresh_db)
    did = _seed_decision(fresh_db, asset="WTI", action="SELL")
    assert da.record(fresh_db, did, "silence_asset", duration_min=0)["ok"] is True
    assert da.is_asset_muted(fresh_db, "WTI")
    assert len(da.active_silences(fresh_db)) == 1

    res = da.unsilence(fresh_db, asset="WTI")
    assert res["ok"] is True and res["revoked"] == 1
    assert da.is_asset_muted(fresh_db, "WTI") is None
    assert da.active_silences(fresh_db) == []

    # 恢复后写入口不再被拦
    wid = decision_writer.write_decision(
        fresh_db, asset="WTI", intended_action="SELL", source="test",
        confidence=80, persist_signal_weight=False,
    )
    row = fresh_db.execute(
        "SELECT final_action, deny_reason FROM trade_decisions WHERE id=?", (wid,)).fetchone()
    assert row[0] != "HOLD"
    assert "用户静默" not in (row[1] or "")


def test_unsilence_by_action_id_and_no_args(fresh_db):
    did = _seed_decision(fresh_db, asset="TSLA", action="BUY")
    da.record(fresh_db, did, "silence_asset", duration_min=30)
    rid = da.active_silences(fresh_db)[0]["id"]
    assert da.unsilence(fresh_db, action_id=rid)["revoked"] == 1
    assert da.is_asset_muted(fresh_db, "TSLA") is None
    assert da.unsilence(fresh_db)["ok"] is False


def test_event_scope_silence_only_blocks_same_event(fresh_db):
    """v390：只静默某个事件类型时，同品种的其它事件仍应放行。"""
    import trade_decisions
    import db as _db
    trade_decisions.init_schema(fresh_db)
    _db.migrate(fresh_db)
    did = _seed_decision(fresh_db, asset="XAU", action="BUY")
    res = da.record(fresh_db, did, "silence_asset", duration_min=0,
                    scope="event", event_type="cpi")
    assert res["ok"] is True and "CPI" in res["effect"]
    assert da.is_asset_muted(fresh_db, "XAU", event_type="CPI")
    assert da.is_asset_muted(fresh_db, "XAU", event_type="NFP") is None
    assert da.is_asset_muted(fresh_db, "XAU") is None  # 不带事件类型时不误伤

    w1 = decision_writer.write_decision(
        fresh_db, asset="XAU", intended_action="BUY", source="t", confidence=80,
        context={"event_type_code": "CPI"}, persist_signal_weight=False)
    row1 = fresh_db.execute(
        "SELECT final_action, deny_reason FROM trade_decisions WHERE id=?", (w1,)).fetchone()
    assert row1[0] == "HOLD" and "仅事件CPI" in (row1[1] or "")

    w2 = decision_writer.write_decision(
        fresh_db, asset="XAU", intended_action="BUY", source="t", confidence=80,
        context={"event_type_code": "NFP"}, persist_signal_weight=False)
    row2 = fresh_db.execute(
        "SELECT final_action, deny_reason FROM trade_decisions WHERE id=?", (w2,)).fetchone()
    assert row2[0] != "HOLD" and "用户静默" not in (row2[1] or "")


def test_event_scope_requires_event_type(fresh_db):
    did = _seed_decision(fresh_db, asset="WTI", action="SELL")
    res = da.record(fresh_db, did, "silence_asset", scope="event", event_type="")
    assert res["ok"] is False and "event_type" in res["error"]
