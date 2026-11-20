# -*- coding: utf-8 -*-
"""v310 P2 —— 「真实持仓战绩」口径回归测试。

背景 (2026-10-29):
  8100 (bridge_server) 的 /api/review/snapshot 是一份**自己的简化实现**, 与 8000
  (api_server) 口径完全不同:
    - 数据源取 trade_reflections_v2, 而不是真实持仓 ai_decisions
    - 忽略时间窗口
    - tracking 恒 0、settled = total
    - avg_pnl = AVG(actual_outcome_pct) —— 该列被污染 → 界面显示"平均 +117.71%"
  结果前端"真实持仓战绩"显示 2208 笔 / 胜率 13.35%, 与模拟盘 (15 笔 / 50%) 对不上。

修复: 新增 src_python/review_snapshot.py 作为**唯一实现**, 8000 与 8100 共用。

全部离线: 只用内存库, 不连网络、不碰生产 DB。
"""

from __future__ import annotations

import inspect
import os
import sqlite3
import sys
import time
from pathlib import Path

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from review import review_snapshot as rs


def _mk_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE ai_decisions (
            id INTEGER PRIMARY KEY,
            target_asset TEXT,
            suggested_action TEXT,
            ts INTEGER,
            created_at TEXT,
            entry_price REAL,
            settled INTEGER,
            forward_pnl REAL,
            mfe_pct REAL,
            mae_pct REAL
        );
        -- 旧实现读的就是这张表 (actual_outcome_pct 已被污染)
        CREATE TABLE trade_reflections_v2 (
            id INTEGER PRIMARY KEY,
            hit INTEGER,
            actual_outcome_pct REAL
        );
        """
    )
    return conn


def _add_decision(conn, did, ts, pnl, settled=1, action="BUY", price=100.0):
    conn.execute(
        "INSERT INTO ai_decisions (id, target_asset, suggested_action, ts, created_at,"
        " entry_price, settled, forward_pnl, mfe_pct, mae_pct)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (did, "XAU", action, ts, "2026-10-29T00:00:00", price, settled, pnl, 0.1, -0.1),
    )


def test_paper_stats_come_from_real_positions_not_reflections():
    """paper 必须来自 ai_decisions, 绝不能被 trade_reflections_v2 的污染列带偏。"""
    conn = _mk_conn()
    now = int(time.time())
    _add_decision(conn, 1, now - 3600, 0.5)          # 真实持仓: 1 胜
    _add_decision(conn, 2, now - 3600, -0.25)         # 真实持仓: 1 负
    # 反思表: 污染的巨大"收益" —— 旧实现会把它当成平均收益 (+399.5%)
    conn.execute(
        "INSERT INTO trade_reflections_v2 (id, hit, actual_outcome_pct) VALUES (1,1,399.5)"
    )

    snap = rs.build_snapshot(conn)
    paper = snap["paper"]
    assert paper["total"] == 2
    assert paper["wins"] == 1
    assert paper["losses"] == 1
    assert paper["winrate_pct"] == 50.0
    assert abs(paper["avg_pnl"] - 0.125) < 1e-6
    assert paper["avg_pnl"] < 1.0, "平均收益不能是反思表污染列的 399.5"
    conn.close()


def test_tracking_and_settled_are_separated():
    """跟踪中的仓位 (settled=0) 不算已结算, 胜率分母只取有明确胜负的单。"""
    conn = _mk_conn()
    now = int(time.time())
    _add_decision(conn, 1, now - 3600, 0.5, settled=1)
    _add_decision(conn, 2, now - 3600, -0.25, settled=1)
    _add_decision(conn, 3, now - 3600, None, settled=0)   # 跟踪中
    _add_decision(conn, 4, now - 3600, 0.0, settled=1)    # 结算但持平 → holds

    paper = rs.build_snapshot(conn)["paper"]
    assert paper["total"] == 4
    assert paper["tracking"] == 1
    assert paper["settled"] == 3
    assert paper["wins"] == 1
    assert paper["losses"] == 1
    assert paper["holds"] == 1
    assert paper["winrate_pct"] == 50.0
    conn.close()


def test_hours_window_filters_old_decisions():
    """时段过滤必须生效 —— 旧 bridge 实现完全忽略窗口, "今日"显示全期累计。"""
    conn = _mk_conn()
    now = int(time.time())
    _add_decision(conn, 1, now - 3600, 0.5)             # 1 小时内
    _add_decision(conn, 2, now - 10 * 86400, 0.5)       # 10 天前
    conn.commit()

    assert rs.build_snapshot(conn, hours=24)["paper"]["total"] == 1
    assert rs.build_snapshot(conn)["paper"]["total"] == 2
    conn.close()


def test_from_to_window_filters_old_decisions():
    conn = _mk_conn()
    now = int(time.time())
    _add_decision(conn, 1, now - 3600, 0.5)
    _add_decision(conn, 2, now - 10 * 86400, 0.5)
    conn.commit()

    snap = rs.build_snapshot(conn, from_ts=now - 86400, to_ts=now)
    assert snap["paper"]["total"] == 1
    assert snap["window"]["from_ts"] == now - 86400
    assert snap["window"]["to_ts"] == now
    conn.close()


def test_snapshot_module_never_queries_reflection_table():
    """守卫: 快照实现里不允许再出现 FROM trade_reflections_v2。"""
    src = Path(rs.__file__).read_text(encoding="utf-8")
    assert "FROM trade_reflections_v2" not in src
    assert "trade_reflections_v2" not in src.split('"""')[2]  # 只允许出现在文件头说明里


def test_bridge_and_api_share_the_same_implementation():
    """8000 与 8100 必须走同一份实现, 否则两边数字还会对不上。

    v398: 8100 侧改成薄转发 —— 正常路径问 8000 要(它有自己的缓存),
    拿不到才用共享模块 review_snapshot.build_snapshot() 本地兜底。
    所以断言: 8100 必须转发 8000 **且** 兜底也走同一份 build_snapshot。
    (直接挂载 8000 的实现会更"同一份", 但实测 20~30s —— 那份实现用 8000 的
     sqlite 连接参数, 在 8100 进程里跟自己写库抢锁。)
    """
    import bridge_server
    import routers.review_run as review_run

    # 2026-11-17: /api/review/snapshot 已从 api_server 迁到 routers/review_run.py，
    # 断言目标跟着端点走（不然一搬家测试就 AttributeError）。
    bridge_src = inspect.getsource(bridge_server.review_snapshot)
    api_src = inspect.getsource(review_run.get_review_snapshot)
    # v455 起 8100 改成**进程内直调**权威实现（自转发 8100 会自循环超时），
    # 两条路径都算「同一份实现」：直调 routers.review_run.get_review_snapshot，
    # 失败才走共享 build_snapshot 兜底。
    assert ("8000/api/review/snapshot" in bridge_src
            or "get_review_snapshot" in bridge_src), \
        "8100 正常路径必须复用 8000 的权威实现"
    assert "build_snapshot" in bridge_src, "8100 本地兜底必须用共享实现"
    assert "build_snapshot" in api_src
