#!/usr/bin/env python3
"""v441 回归：归档理由补全 + 漏单捞回（scripts/backfill_filter_reason.py）。

背景：raw_news 里有 15,986 行 status='FILTERED' 但 filter_reason 为空 ——
归档了没记理由，漏斗报表只能说"未知"，等于看不见为什么没进分析。
脚本按旁证字段（gate_verdict / gate_unavailable）还原理由，并把
「门卫判可交易却仍在时效内」的行恢复成 PENDING。
"""
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(HERE)
SRC = os.path.join(BACKEND, "src_python")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import db as _db
import time


def _load_script():
    path = os.path.join(BACKEND, "scripts", "backfill_filter_reason.py")
    spec = importlib.util.spec_from_file_location("bfr", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _seed(conn):
    now = int(time.time())
    rows = [
        # 门卫判可交易 + 新鲜 → 应捞回 PENDING
        ("t_fresh", "可交易", 0, now - 600),
        # 门卫判可交易 + 已过时效 → 应标 tradeable_but_expired
        ("t_old", "可交易", 0, now - 30 * 3600),
        # 门卫判噪音
        ("t_noise", "排除", 0, now - 3600),
        # 门卫判无信号
        ("t_nosig", "无信号", 0, now - 3600),
        # 门卫当时不可用
        ("t_down", "未判定", 2, now - 3600),
        # 无任何旁证的老行
        ("t_legacy", "", 0, now - 3600),
        # 老名字 degraded_stale
        ("t_degraded", "无信号", 0, now - 3600),
    ]
    for i, (key, verdict, unavailable, ts) in enumerate(rows):
        conn.execute(
            "INSERT INTO raw_news (id, source, content, timestamp, ts, status, is_noise,"
            " gate_verdict, gate_unavailable, filter_reason) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (1000 + i, "test", f"content {key}", str(ts), str(ts), "FILTERED", 0,
             verdict, unavailable, ""))
    conn.execute(
        "UPDATE raw_news SET filter_reason='degraded_stale' WHERE id=?", (1000 + 6,))
    conn.commit()


def test_backfill_assigns_reasons_and_restores_fresh_tradeable(isolated_db):
    conn = isolated_db
    _seed(conn)

    mod = _load_script()
    mod.DB = _db.DB_PATH if hasattr(_db, "DB_PATH") else mod.DB
    groups = mod.plan(conn, restore=True)
    mod.apply_plan(conn, groups)

    got = dict(conn.execute(
        "SELECT id, status || '|' || filter_reason FROM raw_news WHERE id BETWEEN 1000 AND 1006"
    ).fetchall())
    assert got[1000] == "PENDING|", got[1000]                     # 新鲜可交易 → 捞回
    assert got[1001] == "FILTERED|tradeable_but_expired", got[1001]
    assert got[1002] == "FILTERED|gate_noise", got[1002]
    assert got[1003] == "FILTERED|gate_no_signal", got[1003]
    assert got[1004] == "FILTERED|gate_undetermined", got[1004]
    assert got[1005] == "FILTERED|legacy_unknown", got[1005]
    assert got[1006] == "FILTERED|gate_down", got[1006]           # 老名字归一

    # 二次执行不应再产生改动（幂等）
    groups2 = mod.plan(conn, restore=True)
    assert sum(len(v) for v in groups2.values()) == 0, groups2


def test_insert_writes_filter_reason_when_archived_at_ingest(isolated_db):
    """入库当场就归档的行必须自带理由（v441 修的就是这条静默路径）。"""
    conn = isolated_db

    cases = [
        ("排除", 0, 1, "FILTERED", "gate_noise"),
        ("未判定", 2, 0, "FILTERED", "gate_undetermined"),
        ("", 0, 1, "FILTERED", "noise_rule"),
    ]
    now = str(int(time.time()))
    ids = []
    for verdict, gu, noise, status, _why in cases:
        ids.append(_db.insert_raw_news(
            conn, source="ut", content=f"v441 reason {verdict or 'rule'} {now}",
            timestamp=now, status=status, is_noise=noise,
            gate={"verdict": verdict} if verdict else None, gate_unavailable=gu))
    conn.commit()

    got = dict(conn.execute(
        "SELECT gate_verdict || '|' || COALESCE(gate_unavailable,0), filter_reason"
        " FROM raw_news WHERE id IN (%s)" % ",".join(str(i) for i in ids)).fetchall())
    assert got["排除|0"] == "gate_noise", got
    assert got["未判定|2"] == "gate_undetermined", got
    assert got["|0"] == "noise_rule", got
