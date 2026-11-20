#!/usr/bin/env python3
"""v408 回归：归档语义拆分 + 别国本地统计的市场归属。

守的三件事（都来自 2026-11-16 的实测事故）：
1. 超龄归档必须留痕（filter_reason='stale_expired'），不能再和"判噪音"共用 FILTERED；
2. 门卫判「可交易」的行不能被静默归档（11-16 一天 52 条真信号就这么没了）；
3. 「以色列CPI / 埃及最低工资 / 马来西亚棕榈油出口」这类别国本地数据判池外，
   但带强驱动锚点（美联储/黄金/原油/中东）的必须照常分析。
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src_python"
sys.path.insert(0, str(SRC))

from feeds import news_router
import engine.ai_worker as aiw


# ─────────────────── ① 市场归属：别国本地统计 ───────────────────


@pytest.mark.parametrize("text", [
    "以色列统计局：以色列10月CPI同比上涨1.1%，9月为上涨1.6%",
    "埃及公共部门最低工资标准自1月起上调",
    "马来西亚11月1日至15日棕榈油出口量为794717吨",
    "沙特阿拉伯统计局：9月CPI环比持平",
])
def test_foreign_local_stat_is_pool_out(text):
    assert news_router.pool_out_reason(text, "").startswith("池外宏观")


@pytest.mark.parametrize("text", [
    # 美国/欧元区数据：池内品种的直接驱动，必须分析
    "美国10月CPI同比上涨3.2%",
    "欧元区第三季度GDP环比增长0.3%",
    # 别国数据 + 强驱动锚点：伊朗/中东/黄金/原油/美联储
    "以色列CPI回落，但伊朗局势推升黄金避险买盘",
    "埃及上调最低工资，布伦特原油走高",
    "以色列10月CPI，美联储官员称不急于降息",
])
def test_foreign_stat_with_strong_anchor_stays(text):
    assert news_router.pool_out_reason(text, "") == ""


def test_foreign_country_without_stat_is_not_filtered():
    # 只有地名、没有统计口径 → 不判池外（可能是地缘/突发事件）
    text = "以色列北部遭无人机袭击，暂无伤亡报告"
    assert news_router.pool_out_reason(text, "") == ""


def test_scope_switch_off_restores_old_behavior(monkeypatch):
    text = "以色列统计局：10月CPI同比上涨1.1%"
    monkeypatch.setenv("NEWS_POOL_SCOPE", "0")
    assert news_router.pool_out_reason(text, "") == ""


def test_stock_announcement_still_caught():
    # v396 的老口径不能被新规则挤掉
    text = "南卫股份发布公告：实控人涉嫌内幕交易被刑拘"
    reason = news_router.pool_out_reason(text, "")
    assert reason, "个股公告必须被拦下（v396 口径或 v406 非交易类口径都算）"


# ─────────────────── ② 归档留痕 ───────────────────

_SCHEMA = """
CREATE TABLE raw_news (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    status TEXT NOT NULL DEFAULT 'PENDING',
    is_noise INTEGER NOT NULL DEFAULT 0,
    ts INTEGER,
    processing_at TEXT DEFAULT NULL,
    gate_unavailable INTEGER DEFAULT 0,
    gate_verdict TEXT DEFAULT '',
    filter_reason TEXT NOT NULL DEFAULT ''
);
"""

_HOUR = 3600


def _conn(with_reason_col: bool = True) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    if with_reason_col:
        conn.executescript(_SCHEMA)
    else:
        conn.executescript(_SCHEMA.replace(
            ",\n    filter_reason TEXT NOT NULL DEFAULT ''", ""))
    now = int(time.time())
    rows = [
        # id, status, ts, gate_unavailable, gate_verdict
        ("PENDING", now - 10 * _HOUR, 0, "可交易"),   # 真信号：不许归档
        ("PENDING", now - 10 * _HOUR, 2, "未判定"),   # 降级放行：可归档
        ("PENDING", now, 0, "可交易"),                # 新鲜：不动
        ("DONE", now - 10 * _HOUR, 0, "可交易"),      # 终态：不动
    ]
    for status, ts, gu, gv in rows:
        conn.execute("INSERT INTO raw_news(status, ts, gate_unavailable, gate_verdict) "
                     "VALUES (?,?,?,?)", (status, ts, gu, gv))
    conn.commit()
    return conn


def _rows(conn):
    cols = [r[1] for r in conn.execute("PRAGMA table_info(raw_news)")]
    reason = "filter_reason" if "filter_reason" in cols else "'' AS filter_reason"
    return conn.execute(f"SELECT id, status, gate_verdict, {reason} FROM raw_news "
                        "ORDER BY id").fetchall()


def test_expire_keeps_tradeable_and_leaves_reason(monkeypatch):
    monkeypatch.delenv("NEWS_EXPIRE_KEEP_TRADEABLE", raising=False)
    # v419 起门卫未判定行有自己的宽限档；本用例守的是 v408 语义，先把新档关掉
    monkeypatch.setenv("NEWS_EXPIRE_KEEP_GATE_DOWN", "0")
    conn = _conn()
    n = aiw._expire_stale_news(conn)
    assert n == 1
    got = _rows(conn)
    assert got[0][1] == "PENDING", "门卫判可交易的行不许被归档"
    assert got[1][1] == "FILTERED"
    assert got[1][3] == "stale_expired", "归档必须留痕"
    assert got[2][1] == "PENDING" and got[3][1] == "DONE"
    conn.close()


def test_expire_can_still_archive_everything(monkeypatch):
    # 开关关掉 → 退回旧行为（可交易也归档），用于压积压
    monkeypatch.setenv("NEWS_EXPIRE_KEEP_TRADEABLE", "0")
    monkeypatch.setenv("NEWS_EXPIRE_KEEP_GATE_DOWN", "0")
    conn = _conn()
    assert aiw._expire_stale_news(conn) == 2
    assert _rows(conn)[0][1] == "FILTERED"
    conn.close()


def test_expire_works_on_legacy_schema_without_reason_col(monkeypatch):
    monkeypatch.delenv("NEWS_EXPIRE_KEEP_TRADEABLE", raising=False)
    monkeypatch.setenv("NEWS_EXPIRE_KEEP_GATE_DOWN", "0")
    conn = _conn(with_reason_col=False)
    assert aiw._expire_stale_news(conn) == 1  # 不因缺列整条 UPDATE 被拒
    assert _rows(conn)[1][1] == "FILTERED"
    conn.close()


def test_expire_zero_disables(monkeypatch):
    import config
    monkeypatch.setattr(config, "NEWS_ANALYZE_MAX_AGE_HOURS", 0)
    conn = _conn()
    assert aiw._expire_stale_news(conn) == 0
    conn.close()


def test_expire_sql_shape():
    sql = aiw._expire_sql(True, True)
    assert "filter_reason='stale_expired'" in sql
    assert "!= '可交易'" in sql
    legacy = aiw._expire_sql(True, False)
    assert "filter_reason" not in legacy


# ─────────────────── ③ v419：门卫未判定行的宽限与认领优先级 ───────────────────

def test_gate_down_row_survives_normal_cutoff(monkeypatch):
    """门卫未判定（=2）的行超了 6h 常规门槛也不许直接蒸发 —— 4683 条就是这么没的。"""
    monkeypatch.delenv("NEWS_EXPIRE_KEEP_GATE_DOWN", raising=False)
    monkeypatch.setenv("NEWS_EXPIRE_GATE_DOWN_GRACE_H", "24")
    # v432 另有一条「门卫未判定超 2h → 退出建仓候选」的强归档（按 45 分钟窗口算），
    # 这里只验证 v419 的宽限语义，所以先把 v432 那层关掉。
    monkeypatch.setenv("NEWS_EXPIRE_GATE_DOWN_H", "0")
    conn = _conn()
    aiw._expire_stale_news(conn)
    got = _rows(conn)
    assert got[1][1] == "PENDING", "10 小时的门卫未判定行应还在宽限内"
    conn.close()


def test_gate_down_row_archived_after_grace_with_own_reason(monkeypatch):
    """过宽限才归档，且必须留**不同**原因，事后才能精确回放。"""
    monkeypatch.delenv("NEWS_EXPIRE_KEEP_GATE_DOWN", raising=False)
    monkeypatch.setenv("NEWS_EXPIRE_GATE_DOWN_GRACE_H", "24")
    monkeypatch.setenv("NEWS_EXPIRE_GATE_DOWN_H", "0")  # 关掉 v432 的 2h 快速归档
    conn = _conn()
    conn.execute("INSERT INTO raw_news(status, ts, gate_unavailable, gate_verdict) "
                 "VALUES ('PENDING', ?, 2, '')", (int(time.time()) - 30 * _HOUR,))
    conn.commit()
    n = aiw._expire_stale_news(conn)
    assert n >= 1
    archived = conn.execute(
        "SELECT status, filter_reason FROM raw_news WHERE gate_unavailable=2 "
        "AND ts < ?", (int(time.time()) - 20 * _HOUR,)).fetchone()
    assert archived[0] == "FILTERED"
    assert archived[1] == "expired_gate_down", "门卫未判定要留自己的原因"
    conn.close()


def test_expire_sql_keeps_gate_down_out_of_normal_pass():
    sql = aiw._expire_sql(True, True, keep_gate_down=True)
    assert "gate_unavailable, 0) != 2" in sql
    gd = aiw._expire_gate_down_sql(True)
    assert "expired_gate_down" in gd and "= 2" in gd


def test_claim_sql_prioritizes_gate_down_rows():
    """认领顺序：窗口内可交易 → 过期可交易 → 门卫未判定 → 其余。"""
    sql, params = aiw._build_claim_sql(8)
    assert "gate_unavailable, 0) = 2 THEN 2" in sql
    assert "ELSE 3 END" in sql
    # v444 起参数变成 8 个（加了断档水位 floor ×2、降级时效 deg_* ×2），
    # 不再硬编码条数：用占位符个数做动态校验，避免每次加档位都红一次。
    assert len(params) == sql.count("?"), (
        f"参数个数必须与占位符一致：params={len(params)} placeholders={sql.count('?')}")
    # 顺序契约：[max_age, cutoff, floor, floor, deg_min, deg_cutoff, trade_cutoff, limit]
    assert params[0] == int(getattr(aiw.config, "NEWS_ANALYZE_MAX_AGE_HOURS", 0) or 0)
    assert params[-1] == 8


def test_fresh_sweep_archives_stale_non_tradeable_gate0(monkeypatch):
    """v436：gate=0 非可交易行超建仓窗口即退出候选；可交易/新鲜行保留。"""
    monkeypatch.delenv("NEWS_EXPIRE_KEEP_TRADEABLE", raising=False)
    monkeypatch.setenv("NEWS_EXPIRE_KEEP_GATE_DOWN", "0")
    now = int(time.time())
    old = now - 2 * _HOUR  # 远超 45min 建仓窗口，但未到 6h 常规归档档
    conn = sqlite3.connect(":memory:")
    conn.executescript(_SCHEMA)
    rows = [
        # id, status, ts, gate_unavailable, gate_verdict
        (1, "PENDING", old, 0, "排除"),       # 超窗非可交易 → 归档
        (2, "PENDING", old, 0, "可交易"),     # 超窗真信号 → 保留
        (3, "PENDING", now, 0, "排除"),       # 新鲜 → 保留
        (4, "PENDING", old, 2, "未判定"),     # 降级行 → 不归本清扫管
    ]
    for _, status, ts, gu, gv in rows:
        conn.execute("INSERT INTO raw_news(status, ts, gate_unavailable, gate_verdict) "
                     "VALUES (?,?,?,?)", (status, ts, gu, gv))
    conn.commit()

    aiw._expire_stale_news(conn)
    by_id = {r[0]: r for r in conn.execute(
        "SELECT id, status, gate_verdict, filter_reason FROM raw_news ORDER BY id")}
    assert by_id[1][1] == "FILTERED", "超窗非可交易 gate=0 应退出候选"
    assert by_id[1][3] == "stale_expired"
    assert by_id[2][1] == "PENDING", "可交易行不许被归档"
    assert by_id[3][1] == "PENDING", "新鲜非可交易行应保留"
    conn.close()
