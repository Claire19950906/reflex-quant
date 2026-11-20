#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_v3213_p3_cn_etf.py — v321.3 P3 A股 ETF 资金流 (国家基金真实观测) 的回归

关键性质:
  1. **不编数据**: 拉不到就**不落库**, 绝不填 0 或猜值
  2. 落库键是 macro_snapshots 的 metric_key (cn_etf.<标的>.<字段>)
  3. flow_state 不依赖 row_factory (sqlite3.connect 默认不设它)
  4. regime 判定: 主力净流入为正 → 进场; 为负 → 撤离; 中性 → 不产出
"""
from __future__ import annotations

import os
import sqlite3
import sys

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_PY = os.path.join(SRC, "src_python")
if SRC_PY not in sys.path:
    sys.path.insert(0, SRC_PY)


def _mk_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE macro_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, category TEXT,
        source TEXT, metric_key TEXT, value REAL, unit TEXT, status TEXT,
        payload TEXT)""")
    conn.commit()
    return conn


def test_parse_klines_fields():
    import cn_etf_flow as cef
    rows = cef._parse_klines([
        "2026-11-06,-731839920.0,330997474.0,400842464.0,309046032.0,"
        "-1040885952.0,-14.04,6.35,7.69,5.93,-19.97"])
    assert len(rows) == 1
    r = rows[0]
    assert r["date"] == "2026-11-06"
    assert r["main_net"] == -731839920.0      # f52 主力净流入
    assert r["xl_net"] == -1040885952.0      # f56 超大单净流入
    assert r["xl_pct"] == -19.97


def test_parse_klines_skips_garbage():
    import cn_etf_flow as cef
    assert cef._parse_klines(["bad,line"]) == []
    assert cef._parse_klines([]) == []


def test_snapshot_flow_writes_and_sums():
    import cn_etf_flow as cef
    conn = _mk_db()
    bundles = [{
        'key': 'hs300', 'name': '沪深300ETF', 'secid': '1.510300',
        'rows': [{'date': '2026-11-06', 'main_net': -100.0, 'xl_net': -200.0,
                  'small_net': 0, 'mid_net': 0, 'big_net': 0,
                  'main_pct': 0, 'xl_pct': 0}],
    }, {
        'key': 'cyb', 'name': '创业板ETF', 'secid': '0.159915',
        'rows': [{'date': '2026-11-06', 'main_net': -50.0, 'xl_net': -25.0,
                  'small_net': 0, 'mid_net': 0, 'big_net': 0,
                  'main_pct': 0, 'xl_pct': 0}],
    }]
    st = cef.snapshot_flow(conn, bundles)
    assert st["written"] == 6          # 2 标的 × 2 字段 + 2 合计
    assert st["main_total"] == -150.0
    assert st["xl_total"] == -225.0
    n = conn.execute("SELECT COUNT(*) FROM macro_snapshots").fetchone()[0]
    assert n == 6


def test_flow_state_without_row_factory():
    """flow_state 不能依赖 row_factory (sqlite3.connect 默认不设它)。"""
    import cn_etf_flow as cef
    conn = sqlite3.connect(":memory:")   # 默认 tuple 行
    conn.execute("""CREATE TABLE macro_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, category TEXT,
        source TEXT, metric_key TEXT, value REAL, unit TEXT, status TEXT,
        payload TEXT)""")
    now = 1_700_000_000
    conn.execute("INSERT INTO macro_snapshots "
                 "(ts,category,source,metric_key,value,unit,status,payload) "
                 "VALUES (?,?,?,?,?,?,?,?)",
                 (now, 'cn_etf_flow', 'eastmoney', 'cn_etf.hs300.main_net',
                  -100.0, 'cny', 'ok', '{}'))
    conn.execute("INSERT INTO macro_snapshots "
                 "(ts,category,source,metric_key,value,unit,status,payload) "
                 "VALUES (?,?,?,?,?,?,?,?)",
                 (now, 'cn_etf_flow', 'eastmoney', 'cn_etf.hs300.xl_net',
                  -200.0, 'cny', 'ok', '{}'))
    conn.commit()
    st = cef.flow_state(conn)
    assert st["n"] == 1
    assert st["main_total"] == -100.0
    assert st["xl_total"] == -200.0
    assert st["targets"]["hs300"]["main_net"] == -100.0


def test_regime_detects_exit_on_negative_flow():
    """主力净流入为负 → 判定 cn_state_fund_exit (国家基金在撤)。"""
    import time as _t
    from market import market_regime as mr
    conn = _mk_db()
    now = int(_t.time())      # 必须用**新鲜**时间戳: 陈旧数据会被新鲜度门槛拦掉
    conn.execute("INSERT INTO macro_snapshots "
                 "(ts,category,source,metric_key,value,unit,status,payload) "
                 "VALUES (?,?,?,?,?,?,?,?)",
                 (now, 'cn_etf_flow', 'eastmoney', 'cn_etf.hs300.main_net',
                  -731839920.0, 'cny', 'ok', '{}'))
    conn.execute("INSERT INTO macro_snapshots "
                 "(ts,category,source,metric_key,value,unit,status,payload) "
                 "VALUES (?,?,?,?,?,?,?,?)",
                 (now, 'cn_etf_flow', 'eastmoney', 'cn_etf.hs300.xl_net',
                  -1040885952.0, 'cny', 'ok', '{}'))
    conn.commit()
    out = mr._detect_cn_state_fund_flow(conn)
    assert out, "净流出应判定为撤离"
    r = out[0]
    assert r["regime"] == "cn_state_fund_exit"
    assert r["proxy"] is False, "这是真实成交数据, 不是推断"
    assert r["main_net_yi"] < 0


def test_regime_detects_inflow_on_positive_flow():
    import time as _t
    from market import market_regime as mr
    conn = _mk_db()
    now = int(_t.time())      # 新鲜时间戳
    conn.execute("INSERT INTO macro_snapshots "
                 "(ts,category,source,metric_key,value,unit,status,payload) "
                 "VALUES (?,?,?,?,?,?,?,?)",
                 (now, 'cn_etf_flow', 'eastmoney', 'cn_etf.hs300.main_net',
                  900000000.0, 'cny', 'ok', '{}'))
    conn.execute("INSERT INTO macro_snapshots "
                 "(ts,category,source,metric_key,value,unit,status,payload) "
                 "VALUES (?,?,?,?,?,?,?,?)",
                 (now, 'cn_etf_flow', 'eastmoney', 'cn_etf.hs300.xl_net',
                  800000000.0, 'cny', 'ok', '{}'))
    conn.commit()
    out = mr._detect_cn_state_fund_flow(conn)
    assert out and out[0]["regime"] == "cn_state_fund_inflow"


def test_regime_no_output_without_data():
    """没有数据 → **不产出** (宁可没有, 不编)。"""
    from market import market_regime as mr
    conn = _mk_db()
    assert mr._detect_cn_state_fund_flow(conn) == []


def test_regime_skips_stale_data():
    """v321.3 P2: 数据太旧 (外网/采集挂了) → **不给结论**。

    否则会把几天前的旧值讲成"今天国家队在流出", 直接误导决策。
    """
    import time as _t
    from market import market_regime as mr
    conn = _mk_db()
    old_ts = int(_t.time()) - 3 * 24 * 3600      # 3 天前
    for mk, val in (('cn_etf.hs300.main_net', -731839920.0),
                    ('cn_etf.hs300.xl_net', -1040885952.0)):
        conn.execute("INSERT INTO macro_snapshots "
                     "(ts,category,source,metric_key,value,unit,status,payload) "
                     "VALUES (?,?,?,?,?,?,?,?)",
                     (old_ts, 'cn_etf_flow', 'eastmoney', mk,
                      val, 'cny', 'ok', '{}'))
    conn.commit()
    assert mr._detect_cn_state_fund_flow(conn) == [], "陈旧数据不应产出 regime"


def test_snapshot_flow_all_failed_writes_nothing():
    """v321.3 P2: 全部 target 拉取失败 → **不能写合计**。

    之前会写 main_total=0 / xl_total=0 当成"真实观测",
    regime 读到全 0 会误判成资金流出 (假数据污染真实信号)。
    """
    import cn_etf_flow as cef
    conn = _mk_db()
    bundles = [
        {'key': 'hs300', 'name': '沪深300ETF', 'secid': '1.510300', 'rows': []},
        {'key': 'cyb', 'name': '创业板ETF', 'secid': '0.159915', 'rows': []},
    ]
    st = cef.snapshot_flow(conn, bundles)
    assert st["ok"] is False
    assert st["written"] == 0
    n = conn.execute("SELECT COUNT(*) FROM macro_snapshots").fetchone()[0]
    assert n == 0, "全失败时不应有任何写入"
    tot = conn.execute(
        "SELECT COUNT(*) FROM macro_snapshots WHERE metric_key LIKE 'cn_etf_total.%'"
    ).fetchone()[0]
    assert tot == 0, "全失败时不能写合计 (会变成全 0 假数据)"


def test_snapshot_flow_partial_marks_status_partial():
    """v321.3 P2: 部分 target 成功 → 合计标 partial (口径与全量不可比)。"""
    import cn_etf_flow as cef
    conn = _mk_db()
    bundles = [
        {'key': 'hs300', 'name': '沪深300ETF', 'secid': '1.510300',
         'rows': [{'date': '2026-11-06', 'main_net': -100.0, 'xl_net': -200.0}]},
        {'key': 'cyb', 'name': '创业板ETF', 'secid': '0.159915', 'rows': []},
    ]
    st = cef.snapshot_flow(conn, bundles)
    assert st["ok"] is True
    assert st["n_ok"] == 1 and st["n_expected"] == 2
    row = conn.execute(
        "SELECT status FROM macro_snapshots WHERE metric_key='cn_etf_total.main_net'"
    ).fetchone()
    assert row[0] == 'partial', "部分成功的合计不能标 ok"


def test_flow_state_takes_latest_not_oldest():
    """v321.3 P2: flow_state 必须取**最新**的一条, 不是最旧的。

    rows 是 ts DESC, 之前无条件覆盖 → 最终留下的是**最旧**的值。
    """
    import cn_etf_flow as cef
    conn = sqlite3.connect(":memory:")
    conn.execute("""CREATE TABLE macro_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, category TEXT,
        source TEXT, metric_key TEXT, value REAL, unit TEXT, status TEXT,
        payload TEXT)""")
    for ts, val in ((1_700_000_000, -111.0), (1_700_009_999, -222.0)):
        conn.execute("INSERT INTO macro_snapshots "
                     "(ts,category,source,metric_key,value,unit,status,payload) "
                     "VALUES (?,?,?,?,?,?,?,?)",
                     (ts, 'cn_etf_flow', 'eastmoney', 'cn_etf.hs300.main_net',
                      val, 'cny', 'ok', '{}'))
    conn.commit()
    st = cef.flow_state(conn)
    assert st["targets"]["hs300"]["main_net"] == -222.0, "应取最新(-222)不是最旧(-111)"


def test_flow_state_reports_age_and_latest_ts():
    """v321.3 P2: flow_state 要报新鲜度 (age_s), 供 regime 判陈旧。"""
    import time as _t
    import cn_etf_flow as cef
    conn = _mk_db()
    now = int(_t.time()) - 3600          # 1 小时前
    conn.execute("INSERT INTO macro_snapshots "
                 "(ts,category,source,metric_key,value,unit,status,payload) "
                 "VALUES (?,?,?,?,?,?,?,?)",
                 (now, 'cn_etf_flow', 'eastmoney', 'cn_etf.hs300.main_net',
                  -100.0, 'cny', 'ok', '{}'))
    conn.commit()
    st = cef.flow_state(conn)
    assert st["latest_ts"] == now
    assert st["age_s"] is not None and 3500 <= st["age_s"] <= 3700


# ── v321.3 P2: 历史回填 (backfill) ───────────────────────────────────────────

def _all_targets_bundles(dates):
    """构造 8 个标的、每个日期都有数据的 bundles (模拟完整回填)。"""
    import cn_etf_flow as cef
    out = []
    for i, (secid, key, name) in enumerate(cef.ETF_TARGETS):
        rows = [{'date': d, 'main_net': -100.0 * (i + 1), 'xl_net': -50.0 * (i + 1)}
                for d in dates]
        out.append({'key': key, 'name': name, 'secid': secid, 'rows': rows})
    return out


def test_backfill_writes_all_days_with_trade_day_ts():
    """回填: 每个交易日都写, 且 ts = 该交易日 15:00 (固定、可复现)。"""
    import cn_etf_flow as cef
    conn = _mk_db()
    dates = ['2026-10-01', '2026-10-02', '2026-10-03']
    st = cef.backfill_flow(conn, _all_targets_bundles(dates))
    assert st["ok"] is True
    assert st["days_full"] == 3
    # 3 天 × 8 标的 × 2 字段 = 48 明细 + 3 天 × 2 合计 = 54
    assert st["written"] == 54
    n = conn.execute("SELECT COUNT(*) FROM macro_snapshots").fetchone()[0]
    assert n == 54
    # ts 必须等于交易日 15:00 (而不是采集时刻)
    expect_ts = cef._trade_day_ts('2026-10-02')
    rows = conn.execute(
        "SELECT ts FROM macro_snapshots WHERE metric_key='cn_etf.hs300.main_net'"
    ).fetchall()
    assert len(rows) == 3
    assert expect_ts in [r[0] for r in rows]


def test_backfill_is_idempotent():
    """幂等: 重复回填同一份历史 → 行数不变 (不产生重复行)。"""
    import cn_etf_flow as cef
    conn = _mk_db()
    bundles = _all_targets_bundles(['2026-10-01', '2026-10-02'])
    cef.backfill_flow(conn, bundles)
    n1 = conn.execute("SELECT COUNT(*) FROM macro_snapshots").fetchone()[0]
    cef.backfill_flow(conn, bundles)
    n2 = conn.execute("SELECT COUNT(*) FROM macro_snapshots").fetchone()[0]
    assert n1 == n2, "回填必须幂等"


def test_backfill_skips_total_when_targets_incomplete():
    """某交易日标的不全 → **不写合计** (口径不一致, 写了会污染分位)。"""
    import cn_etf_flow as cef
    conn = _mk_db()
    bundles = _all_targets_bundles(['2026-10-01'])
    bundles = bundles[:-1]              # 少一个标的 → 7/8
    st = cef.backfill_flow(conn, bundles)
    assert st["days_full"] == 0
    n_total = conn.execute(
        "SELECT COUNT(*) FROM macro_snapshots WHERE metric_key LIKE 'cn_etf_total.%'"
    ).fetchone()[0]
    assert n_total == 0, "标的不全时不应写合计"
    # 但明细行仍要写 (每个标的自己那天的值是真实的)
    n_detail = conn.execute(
        "SELECT COUNT(*) FROM macro_snapshots WHERE metric_key LIKE 'cn_etf.%' "
        "AND metric_key NOT LIKE 'cn_etf_total.%'"
    ).fetchone()[0]
    assert n_detail == 7 * 2


def test_backfill_total_payload_has_date_for_percentile_dedup():
    """合计行 payload 必须带 date —— regime 分位按交易日去重靠它。"""
    import cn_etf_flow as cef
    conn = _mk_db()
    cef.backfill_flow(conn, _all_targets_bundles(['2026-10-01']))
    payload = conn.execute(
        "SELECT payload FROM macro_snapshots WHERE metric_key='cn_etf_total.main_net'"
    ).fetchone()[0]
    import json as _json
    data = _json.loads(payload)
    assert data["date"] == "2026-10-01"
    assert data["n_ok"] == 8 and data["n_expected"] == 8


def test_fetch_etf_flow_timeout_is_actually_forwarded():
    """P0-2: timeout 形参必须**真的**传到 HTTP 层 (之前是死参数)。"""
    import cn_etf_flow as cef
    seen = {}

    def _fake_http_json(url, timeout=None):
        seen["timeout"] = timeout
        return {"data": {"klines": ["2026-10-01,1,2,3,4,5,6,7,8,9,10"]}}

    orig = cef._http_json
    cef._http_json = _fake_http_json
    try:
        cef.fetch_etf_flow(limit=5, timeout=3.5)
    finally:
        cef._http_json = orig
    assert seen.get("timeout") == 3.5, "timeout 没有透传到 _http_json"


def test_trade_day_ts_is_stable_and_after_market_close():
    import cn_etf_flow as cef
    ts1 = cef._trade_day_ts('2026-10-01')
    ts2 = cef._trade_day_ts('2026-10-01')
    assert ts1 == ts2, "同一交易日 ts 必须可复现"
    assert cef._trade_day_ts('bad-date') == 0


# ── v321.3 P2: 失败退避 (state) ──────────────────────────────────────────────

def test_state_roundtrip(tmp_path, monkeypatch):
    """状态文件: 记录上次尝试/成功时刻, 读写一致。"""
    import cn_etf_flow as cef
    monkeypatch.setenv("TRIDENT_STATE_DIR", str(tmp_path))
    cef._write_state(111, 222)
    st = cef._read_state()
    assert st["last_ok"] == 111
    assert st["last_attempt"] == 222


def test_state_defaults_when_missing(tmp_path, monkeypatch):
    import cn_etf_flow as cef
    monkeypatch.setenv("TRIDENT_STATE_DIR", str(tmp_path))
    st = cef._read_state()
    assert st["last_ok"] == 0 and st["last_attempt"] == 0


def test_backoff_uses_last_attempt_not_last_ok(tmp_path, monkeypatch):
    """退避必须基于 **last_attempt**。

    若基于 last_ok: "一次都没成功过"时 last_ok=0 → 退避永不触发
    → 外网持续不通时每轮都去撞墙, 只会让 IP 风控更严。
    """
    import cn_etf_flow as cef
    monkeypatch.setenv("TRIDENT_STATE_DIR", str(tmp_path))
    import time as _t
    cef._write_state(0, int(_t.time()))     # 从未成功, 但刚刚尝试过
    st = cef._read_state()
    assert st["last_ok"] == 0
    assert st["last_attempt"] > 0, "失败也必须更新 last_attempt, 否则退避不生效"


def test_backoff_state_survives_legacy_plain_timestamp(tmp_path, monkeypatch):
    """兼容旧格式 (纯数字时间戳) —— 升级不能把老 state 读崩。"""
    import cn_etf_flow as cef
    monkeypatch.setenv("TRIDENT_STATE_DIR", str(tmp_path))
    p = cef._state_path()
    with open(p, "w") as f:
        f.write("1794214731")
    st = cef._read_state()
    assert st["last_ok"] == 1794214731
    assert st["last_attempt"] == 1794214731


# ── v321.3 P2: kg-review cron 写锁退避 + 步骤独立 ────────────────────────────

def test_kg_review_retry_on_lock_then_success(monkeypatch):
    """写锁冲突 → 指数退避重试 → 最终成功。"""
    import sqlite3
    import cron_kg_review as ckr

    calls = {"n": 0}

    def _flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise sqlite3.OperationalError("database is locked")
        return {"ok": True}

    monkeypatch.setattr(ckr.time, "sleep", lambda s: None)
    out = ckr._retry_on_lock(_flaky, "t", attempts=5, base_sleep=0.01)
    assert out["ok"] is True
    assert calls["n"] == 3


def test_kg_review_retry_does_not_swallow_other_errors(monkeypatch):
    """非锁异常 → 立即抛出 (不掩盖真 bug)。"""
    import sqlite3
    import cron_kg_review as ckr

    def _boom():
        raise sqlite3.OperationalError("no such table: xyz")

    monkeypatch.setattr(ckr.time, "sleep", lambda s: None)
    try:
        ckr._retry_on_lock(_boom, "t", attempts=3, base_sleep=0.01)
        raise AssertionError("应当抛出")
    except sqlite3.OperationalError as exc:
        assert "no such table" in str(exc)


def test_kg_review_steps_are_independent(monkeypatch):
    """R2 失败 → **R3 仍要执行** (原来一次锁冲突让两个闭环全废)。"""
    import cron_kg_review as ckr

    ran = {"r2": False, "r3": False}

    def _r2_fail():
        raise RuntimeError("database is locked")

    def _r2_ok():
        ran["r2"] = True
        return {"scanned": 1, "approved": [1], "skipped": 0}

    def _r3_ok():
        ran["r3"] = True
        return {"scanned": 2, "approved": [2, 3], "skipped": [4]}

    monkeypatch.setattr(ckr, "_run_kg_auto_approve", _r2_fail)
    monkeypatch.setattr(ckr, "_run_attribution_auto_review", _r3_ok)
    monkeypatch.setattr(ckr.time, "sleep", lambda s: None)
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = ckr.main()
    assert ran["r3"] is True, "R2 失败不应阻止 R3 执行"
    assert rc in (0, 1)      # 有失败 → 退出码非 0, 但 R3 已跑
