"""决策证据链 + carry-over 信号残留的用例。

两块都是"事后追溯"的基础设施，坏了不阻断主流程，所以最容易烂在无人看的地方：
- `decision_evidence` 存的是**当时**的新闻快照。如果它跟着 `raw_news` 一起被改，
  复盘时看到的就不是"决策那一刻依据了什么"，而是"后来被改成什么样"——溯源失去意义。
- `signal_state` 是跨轮次的残留强度。TTL 与衰减算错，旧事件的余温会一直挂在信号台上，
  或者反过来，同一事件续报时被当成新事件重复计数。

`raw_news` 与 `signal_state` 的读法都依赖 `row_factory = sqlite3.Row`（`db.get_connection`
会设，自建连接必须自己设），这里照生产口径设置。
"""

import sqlite3

import pytest

import db

BASE = 1_700_000_000


@pytest.fixture(scope="module")
def _schema_conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    db.migrate(c)
    return c


@pytest.fixture()
def conn(_schema_conn):
    for t in ("decision_evidence", "signal_state", "raw_news"):
        _schema_conn.execute(f"DELETE FROM {t}")
    return _schema_conn


def _news(conn, content="新闻正文", ts=BASE):
    return db.insert_raw_news(conn, source="s", content=content, timestamp=str(ts), ts=ts)


# ── 证据链：存的是快照，不是引用 ──────────────────────────────────────────

def test_record_evidence_snapshots_the_news_at_decision_time(conn):
    nid = _news(conn, "美联储降息 25bp", BASE)
    assert db.record_evidence(conn, decision_id=7, news_id=nid, used_for="trigger") is True
    row = conn.execute(
        "SELECT published_at_snapshot, fetched_at_snapshot, content_hash_snapshot, used_for "
        "FROM decision_evidence WHERE decision_id=7").fetchone()
    assert row["fetched_at_snapshot"] == BASE
    assert row["content_hash_snapshot"] == db.content_fingerprint("美联储降息 25bp")
    assert row["used_for"] == "trigger"


def test_snapshot_survives_source_news_being_edited(conn):
    nid = _news(conn, "原始措辞：降息 25bp", BASE)
    db.record_evidence(conn, decision_id=8, news_id=nid)
    conn.execute("UPDATE raw_news SET content='改过的措辞：降息 50bp' WHERE id=?", (nid,))
    ev = db.fetch_evidence(conn, 8)[0]
    # 快照字段钉在决策那一刻，正文后来被改也改不动它
    assert ev["content_hash_snapshot"] == db.content_fingerprint("原始措辞：降息 25bp")
    # 已知口径：content_fingerprint 是入库时算的，手改正文**不会**重算这一列。
    # 所以"当前指纹"仍是最初那条 → 要判断正文是否被改过，只能拿 content 自己再算一遍比对。
    assert ev["current_hash"] == db.content_fingerprint("原始措辞：降息 25bp")
    assert ev["content"] == "改过的措辞：降息 50bp"
    assert db.content_fingerprint(ev["content"]) != ev["current_hash"]


def test_record_evidence_for_missing_news_still_writes_blank_snapshot(conn):
    # 新闻已被清掉/尚未入库也不能丢证据行，只是快照为空
    assert db.record_evidence(conn, decision_id=9, news_id=99999) is True
    row = conn.execute(
        "SELECT published_at_snapshot, fetched_at_snapshot FROM decision_evidence "
        "WHERE decision_id=9").fetchone()
    assert row["published_at_snapshot"] == "" and row["fetched_at_snapshot"] == 0


def test_same_evidence_slot_is_replaced_but_different_usage_kept(conn):
    # 主键是 (decision_id, news_id, used_for)：同一用途重写会覆盖，不重复堆行；
    # 但一条新闻同时当 trigger 和 cross_check 用，是两条独立证据，要各留一行。
    nid = _news(conn, "同一条新闻")
    db.record_evidence(conn, decision_id=10, news_id=nid, used_for="context")
    db.record_evidence(conn, decision_id=10, news_id=nid, used_for="context")
    db.record_evidence(conn, decision_id=10, news_id=nid, used_for="cross_check")
    rows = conn.execute(
        "SELECT used_for FROM decision_evidence WHERE decision_id=10 ORDER BY used_for").fetchall()
    assert [r["used_for"] for r in rows] == ["context", "cross_check"]
    assert db.fetch_evidence(conn, 10) != []


def test_record_evidence_degrades_when_table_missing():
    bare = sqlite3.connect(":memory:")
    bare.row_factory = sqlite3.Row
    assert db.record_evidence(bare, decision_id=1, news_id=1) is False
    assert db.fetch_evidence(bare, 1) == []


def test_record_evidence_batch_counts_and_skips_none(conn):
    ids = [_news(conn, f"新闻{i}", BASE + i) for i in range(3)]
    n = db.record_evidence_batch(conn, decision_id=11, news_ids=[ids[0], None, ids[2], "垃圾"])
    assert n == 2                       # None 与不可转 int 的都跳过，不抛
    assert len(db.fetch_evidence(conn, 11)) == 2


def test_fetch_evidence_orders_by_capture_time(conn):
    ids = [_news(conn, f"证据{i}", BASE + i) for i in range(2)]
    for nid in ids:
        db.record_evidence(conn, decision_id=12, news_id=nid)
    ev = db.fetch_evidence(conn, 12)
    assert [e["news_id"] for e in ev] == ids


# ── carry-over 信号残留 ──────────────────────────────────────────────────

def test_record_signal_inserts_new_row(conn):
    assert db.record_signal(conn, signal_id="sig-1", asset="BTC", impact=0.6,
                            direction="BULLISH", reason="降息", ttl_seconds=3600,
                            now=BASE) is True
    row = conn.execute("SELECT * FROM signal_state WHERE signal_id='sig-1'").fetchone()
    assert row["impact"] == pytest.approx(0.6)
    assert row["created_at"] == BASE and row["last_seen_at"] == BASE
    assert row["direction"] == "BULLISH" and row["ttl_seconds"] == 3600


def test_repeat_signal_keeps_max_impact_and_resets_ttl_window(conn):
    db.record_signal(conn, signal_id="sig-2", asset="BTC", impact=0.6, ttl_seconds=3600, now=BASE)
    db.record_signal(conn, signal_id="sig-2", asset="BTC", impact=0.4, ttl_seconds=3600, now=BASE + 3000)
    row = conn.execute("SELECT * FROM signal_state WHERE signal_id='sig-2'").fetchone()
    assert row["impact"] == pytest.approx(0.6)          # 续报不叠加，取最大值
    assert row["created_at"] == BASE + 3000             # TTL 窗口从续报时刻重新计
    assert row["last_seen_at"] == BASE + 3000


def test_get_active_signals_filters_by_ttl_and_asset(conn):
    db.record_signal(conn, signal_id="live", asset="BTC", impact=1.0, ttl_seconds=3600, now=BASE)
    db.record_signal(conn, signal_id="dead", asset="BTC", impact=1.0, ttl_seconds=60, now=BASE)
    db.record_signal(conn, signal_id="eth", asset="ETH", impact=1.0, ttl_seconds=3600, now=BASE)

    active = db.get_active_signals(conn, now=BASE + 120)
    assert {s["signal_id"] for s in active} == {"live", "eth"}        # dead 已过 TTL

    only_btc = db.get_active_signals(conn, asset="BTC", now=BASE + 120)
    assert {s["signal_id"] for s in only_btc} == {"live"}


def test_get_active_signals_decays_impact_by_age(conn):
    db.record_signal(conn, signal_id="d", asset="BTC", impact=1.0,
                     decay_lambda=1e-5, ttl_seconds=86400, now=BASE)
    fresh = db.get_active_signals(conn, now=BASE)[0]
    old = db.get_active_signals(conn, now=BASE + 3600)[0]
    assert fresh["age_seconds"] == 0
    assert fresh["current_impact"] == pytest.approx(1.0)
    assert old["age_seconds"] == 3600
    assert old["current_impact"] < 1.0 and old["current_impact"] > 0.9


def test_purge_expired_signals_only_removes_expired(conn):
    db.record_signal(conn, signal_id="old", asset="BTC", impact=1.0, ttl_seconds=60, now=BASE)
    db.record_signal(conn, signal_id="new", asset="BTC", impact=1.0, ttl_seconds=86400, now=BASE)
    assert db.purge_expired_signals(conn, now=BASE + 300) == 1
    left = conn.execute("SELECT signal_id FROM signal_state").fetchall()
    assert [r["signal_id"] for r in left] == ["new"]
    assert db.purge_expired_signals(conn, now=BASE + 300) == 0


def test_signal_helpers_degrade_when_table_missing():
    bare = sqlite3.connect(":memory:")
    bare.row_factory = sqlite3.Row
    assert db.record_signal(bare, signal_id="x", asset="BTC", impact=1.0) is False
    assert db.get_active_signals(bare) == []
    assert db.purge_expired_signals(bare) == 0


def test_signal_and_asset_strings_are_truncated_at_write(conn):
    db.record_signal(conn, signal_id="S" * 300, asset="BTCUSDT-PERPETUAL" * 5,
                     impact=1.0, reason="r" * 500, direction="BULLISH" * 5, now=BASE)
    row = conn.execute("SELECT signal_id, asset, reason, direction FROM signal_state").fetchone()
    assert len(row["signal_id"]) == 128 and len(row["asset"]) == 20
    assert len(row["reason"]) == 200 and len(row["direction"]) == 10
