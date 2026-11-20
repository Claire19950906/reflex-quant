# -*- coding: utf-8 -*-
"""v322: 看门狗不得回收「在办批次」+ 行情源连接失败快速放弃 —— 回归单测。

事故背景（2026-11-05 生产实测）
--------------------------------
「待分析」永不下降的**最后一环**（前几环已修：child_count 参数错位 / 看门狗静默吞锁 /
队头阻塞无冷却）：

1) 行情源整体不通
   `curl https://query1.finance.yahoo.com` 与 `https://www.okx.com/api/v5/market/candles`
   均连接超时（5s），只有 MiniMax 正常。而 `_yahoo_get_json()` 把**连接类失败**
   （timeout/DNS/ConnectionError）与 HTTP 429 混在同一个重试循环里：
       total_attempts = _YAHOO_RETRY_MAX(4) × len(_YAHOO_BASES)(2) = 8
       8 × _YAHOO_TIMEOUT(10s) + 退避(0.6/1.2/2.4…) ≈ **89 秒/次**
   OKX 侧同理（3 次 × timeout ≈ 31 秒/次）。
   → 每条新闻落库多耗 1-2 分钟 → 单批 16 条跑 **42 分钟**。

2) 看门狗把在办批次当僵尸回收
   看门狗规则是「认领后 15 分钟未完成即僵尸 → retry_count+1 → 改回 PENDING」。
   42 分钟 > 15 分钟 → **批还在跑，行已被改回 PENDING**。

3) 批事务白干
   批事务末尾 `UPDATE raw_news SET status='DONE' ... WHERE id=? AND status='PROCESSING'`
   此时匹配 0 行 → **这一批一条都进不了 DONE**；下一轮又按 id ASC 认领同一批
   → 无限循环 → 「待分析」只增不减。
   （现场证据：raw_news.processing_at 全表 6913 行**全是 NULL** —— 只有看门狗的
   `processing_at = NULL` 会这样；说明回收者确实是看门狗而不是引擎自己的冷却逻辑。）

本文件锁死的三条不变量：
  A. 看门狗回收 SQL 必须排除本进程「在办批次」(_ACTIVE_BATCH_IDS)
  B. 认领后登记在办批次、批处理结束后注销（否则看门狗会误杀新批次）
  C. 行情源连接类失败必须快速放弃（不再跑满 8 次重试 ≈ 89 秒）
"""

from __future__ import annotations

import os
import sqlite3
import sys
import time

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import pytest
import requests

from feeds import quote_sources
from engine import ai_worker


AI_WORKER_PATH = os.path.join(SRC, "engine", "ai_worker.py")
# P2 领域分包后实现搬到了 feeds/quote_sources.py；用模块自身的 __file__ 定位真身，
# 否则读到的是原位兼容垫片（只有 sys.modules 别名，没有实现源码）。
from feeds import quote_sources as _qs
QUOTE_SOURCES_PATH = _qs.__file__


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


# ─────────────────────────────────────────────────────────────────────────────
# A. 看门狗回收 SQL 必须排除「在办批次」
# ─────────────────────────────────────────────────────────────────────────────

def test_reclaim_sql_without_active_ids_has_no_exclusion():
    sql, params = ai_worker._build_reclaim_sql([])
    assert "NOT IN" not in sql
    # v322：参数顺序 = [冷却秒数, 重试阈值]（冷却参数是 v322 新增的）
    assert params == [ai_worker._BATCH_RETRY_COOLDOWN_SEC,
                      ai_worker._MAX_PENDING_RETRIES]
    # 占位符数必须与参数数一致（否则 execute 直接抛 "Incorrect number of bindings"）
    assert sql.count("?") == len(params)


def test_reclaim_sql_excludes_active_batch_ids():
    sql, params = ai_worker._build_reclaim_sql([11883, 11832, 11894])
    assert "AND id NOT IN (?,?,?)" in sql
    # 参数顺序：冷却秒数、重试阈值在前，排除的 id 在后（必须与占位符顺序一致）
    assert params[0] == ai_worker._BATCH_RETRY_COOLDOWN_SEC
    assert params[1] == ai_worker._MAX_PENDING_RETRIES
    assert params[2:] == [11832, 11883, 11894]
    assert sql.count("?") == len(params)


def test_reclaim_sql_dedupes_and_sorts_active_ids():
    sql, params = ai_worker._build_reclaim_sql([7, 3, 7])
    assert "AND id NOT IN (?,?)" in sql
    assert params[2:] == [3, 7]
    assert sql.count("?") == len(params)


def test_reclaim_sql_keeps_zombie_thresholds():
    """回收语义不能被改坏：retry_count+1、达阈值标 FAILED、15 分钟僵尸线。"""
    sql = ai_worker._WATCHDOG_RECLAIM_SQL_BASE
    assert "retry_count = retry_count + 1" in sql
    assert "'FAILED' ELSE 'PENDING'" in sql
    assert "status='PROCESSING'" in sql
    assert "-15 minutes" in sql
    # v322：回收后必须写冷却时刻（不是 NULL）—— 否则立刻可被重新认领 → 队头霸占
    assert "processing_at = NULL" not in sql
    assert "datetime('now', 'localtime'" in sql
    # v434：判死必须看**实际认领时间**，不是「认领时间非空」——
    # 原来那条 `processing_at IS NOT NULL` 让 NULL 僵尸永远回收不到
    # （实测 11-17 有 6 条从 18:00 挂到 19:14，只有重启才清掉）。
    assert "COALESCE(processing_at" in sql
    assert "processing_at IS NOT NULL" not in sql


def test_reclaim_sql_is_actually_applied_against_real_rows():
    """行为验证：在办批次**不能**被回收，真僵尸**必须**被回收。"""
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    # v434：补 ts 列 —— 回收 SQL 现在用「新闻入库 ts」兜底给 processing_at 为空的行算年龄
    con.execute(
        "CREATE TABLE raw_news ("
        " id INTEGER PRIMARY KEY,"
        " status TEXT,"
        " retry_count INTEGER DEFAULT 0,"
        " processing_at TEXT,"
        " ts INTEGER)"
    )
    con.execute(
        "INSERT INTO raw_news VALUES (1,'PROCESSING',0,'2020-01-01 00:00:00',0)"  # 真僵尸
    )
    con.execute(
        "INSERT INTO raw_news VALUES (2,'PROCESSING',0,'2020-01-01 00:00:00',0)"  # 在办批次
    )
    con.execute(
        "INSERT INTO raw_news VALUES (3,'DONE',0,NULL,0)"  # 已完成，不该动
    )

    sql, params = ai_worker._build_reclaim_sql([2])
    con.execute(sql, params)
    con.commit()

    rows = {r["id"]: (r["status"], r["retry_count"]) for r in
            con.execute("SELECT id, status, retry_count FROM raw_news").fetchall()}
    assert rows[1] == ("PENDING", 1), "真僵尸必须被回收"
    assert rows[2] == ("PROCESSING", 0), "在办批次绝不能被回收"
    assert rows[3] == ("DONE", 0), "已完成的行不该被回收"
    con.close()


# ─────────────────────────────────────────────────────────────────────────────
# B. 认领后登记 / 批结束后注销
# ─────────────────────────────────────────────────────────────────────────────

def test_active_batch_registry_exists():
    assert isinstance(ai_worker._ACTIVE_BATCH_IDS, dict)
    assert hasattr(ai_worker._ACTIVE_BATCH_LOCK, "acquire")


def test_claim_registers_and_persist_releases_active_batch():
    """源码级锁死：登记在认领之后、注销在批处理落库之后。"""
    src = _read(AI_WORKER_PATH)

    reg = src.index("_hb_stop = _active_batch_register(batch_ids)")
    claim = src.index("batch_ids = [r[\"id\"] for r in batch]")
    assert claim < reg, "必须先拿到 batch_ids 再登记"

    rel = src.index("_active_batch_release(batch_ids, _hb_stop)")
    persist = src.index("decision_infos = _batch_persist()")
    assert persist < rel, "必须在落库结束后才注销"

    # 看门狗必须走带排除项的构造器 + 租约过滤
    assert "_build_reclaim_sql(_skip)" in src
    assert "_active_batch_live_ids()" in src


def test_register_and_release_roundtrip():
    """登记 → 排除名单含它；注销 → 排除名单不含它，心跳线程也被停掉。"""
    ai_worker._ACTIVE_BATCH_IDS.clear()
    try:
        stop = ai_worker._active_batch_register([7, 8])
        assert ai_worker._active_batch_live_ids() == [7, 8]
        ai_worker._active_batch_release([7, 8], stop)
        assert ai_worker._active_batch_live_ids() == []
        assert stop.is_set(), "注销必须停掉心跳线程"
    finally:
        ai_worker._ACTIVE_BATCH_IDS.clear()


def test_lease_expiry_lets_watchdog_reclaim_zombie():
    """租约过期（心跳停了 = 批次确实卡死/已死）→ 不再排除 → 看门狗能回收。

    没有这条，批处理中途抛异常时「注销」不执行，那批 PROCESSING 行会永久卡住。
    """
    ai_worker._ACTIVE_BATCH_IDS.clear()
    try:
        stale = time.monotonic() - (ai_worker._ACTIVE_BATCH_LEASE_S + 60)
        ai_worker._ACTIVE_BATCH_IDS[42] = stale
        assert ai_worker._active_batch_live_ids() == [], "过期登记必须被排除出保护名单"

        ai_worker._ACTIVE_BATCH_IDS[43] = time.monotonic()
        assert ai_worker._active_batch_live_ids() == [43], "新鲜登记必须继续受保护"
    finally:
        ai_worker._ACTIVE_BATCH_IDS.clear()


def test_superseded_batch_loses_protection():
    """上一批没走正常注销（continue 跳出 / 异常）→ 新批登记后旧批不再受保护。

    否则旧批的行会一直挂在 PROCESSING 上 —— 正是「待分析」永不下降的形态。
    """
    ai_worker._ACTIVE_BATCH_IDS.clear()
    old_interval = ai_worker._ACTIVE_BATCH_HEARTBEAT_S
    ai_worker._ACTIVE_BATCH_HEARTBEAT_S = 0.05
    stop_old = stop_new = None
    try:
        stop_old = ai_worker._active_batch_register([11])
        assert ai_worker._active_batch_live_ids() == [11]

        stop_new = ai_worker._active_batch_register([12])  # 代次 +1 → 旧心跳作废
        time.sleep(0.4)
        assert 11 not in ai_worker._active_batch_live_ids(), "被取代的批次必须失去保护"
        assert ai_worker._active_batch_live_ids() == [12]
    finally:
        if stop_old is not None:
            stop_old.set()
        if stop_new is not None:
            stop_new.set()
        ai_worker._ACTIVE_BATCH_HEARTBEAT_S = old_interval
        ai_worker._ACTIVE_BATCH_IDS.clear()


# ─────────────────────────────────────────────────────────────────────────────
# C. 行情源连接类失败快速放弃
# ─────────────────────────────────────────────────────────────────────────────

# 注意：requests.exceptions 里**没有** ConnectError（那是 urllib3 的名字）
# 也没有 ConnectionResetError（那是 Python 内置异常）。
# 但 requests 会把 urllib3 的 NewConnectionError 包成 requests.ConnectionError，
# 而 socket 的 ECONNRESET 会直接冒成内置 ConnectionResetError —— 两者都要能识别。
# 所以这里用「构造函数」而不是「类名字符串」来造异常。
_CONN_ERROR_FACTORIES = [
    lambda: requests.exceptions.ConnectTimeout("boom"),
    lambda: requests.exceptions.ReadTimeout("boom"),
    lambda: requests.exceptions.ConnectionError("boom"),
    lambda: requests.exceptions.SSLError("boom"),
    lambda: requests.exceptions.ProxyError("boom"),
    lambda: requests.exceptions.ChunkedEncodingError("boom"),
    lambda: requests.exceptions.Timeout("boom"),
    lambda: ConnectionResetError("boom"),
    lambda: ConnectionRefusedError("boom"),
    lambda: OSError("connection reset by peer"),
]


@pytest.mark.parametrize("factory", _CONN_ERROR_FACTORIES)
def test_conn_error_classification(factory):
    import requests
    exc = factory()
    assert quote_sources._is_conn_error(exc) is True


def test_conn_error_names_cover_observed_production_exceptions():
    """锁死「按异常类名识别」这条规则覆盖生产上真实出现过的类名。

    事故现场日志里出现过：
      · requests.exceptions.ConnectionError（urllib3 连接失败被包装）
      · SSLError（TLS 握手被重置）
      · 内置 ConnectionResetError（socket ECONNRESET）
    quote_sources._is_conn_error 按 `type(exc).__name__` 匹配白名单，
    所以这些名字必须在名单里 —— 否则又会把 8 次重试跑满（89 秒/次）。
    """
    import requests
    src = _read(QUOTE_SOURCES_PATH)
    for name in ("ConnectionError", "SSLError", "ConnectionResetError",
                 "ConnectTimeout", "ReadTimeout", "ProxyError"):
        assert '"%s"' % name in src, (
            "连接类异常名 %s 必须出现在 _is_conn_error 的白名单里" % name
        )
    assert quote_sources._is_conn_error(requests.exceptions.SSLError("x")) is True
    assert quote_sources._is_conn_error(ConnectionResetError("x")) is True


def test_business_error_is_not_conn_error():
    """HTTP 业务错误（已拿到响应）不该被当成连接层失败 —— 否则会误伤正常重试。"""
    import requests
    assert quote_sources._is_conn_error(requests.exceptions.HTTPError("500")) is False
    assert quote_sources._is_conn_error(ValueError("bad json")) is False


def test_quote_sources_fail_fast_on_conn_error():
    """源码级锁死：Yahoo 与 OKX 都要在连接类失败时立即 break。"""
    src = _read(QUOTE_SOURCES_PATH)
    assert "_YAHOO_CONN_FAIL_BUDGET" in src
    assert "YAHOO-CONN-FAIL" in src
    assert "OKX-CONN-FAIL" in src
    # Yahoo 与 OKX 两处都要有「连接不通 → break」
    assert src.count("_is_conn_error(_e)") >= 2
