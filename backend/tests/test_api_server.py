# -*- coding: utf-8 -*-
"""
test_api_server.py — api_server.py (8000 端口) 窗口过滤口径回归测试
=====================================================================

为什么要有这个文件:
  v309 P1 修的是 bridge_server.py (8100 端口)。**api_server.py (8000 端口)
  有一处同类的、但 P1 完全没碰到的窗口过滤 bug**:

    review_insights.created_at 的落库格式是 **空格分隔** 的 UTC
    (db.py 的 DEFAULT 是 datetime('now') → 'YYYY-MM-DD HH:MM:SS'),
    而 api_server._utc_ts_to_iso() 产出的 cutoff 是 **'T' 分隔** 的
    ('YYYY-MM-DDTHH:%M:%S')。

    SQLite 字符串比较按字节序: ' '(0x20) < 'T'(0x54)。
    → `created_at >= '2026-10-25T08:00:00'` 对一行 '2026-10-25 08:00:00'
      求值结果是 **False** —— 同一天的行被整片漏掉。

  这个 bug 跟 ai_decisions.created_at 那个不一样: ai_decisions 存的是 'T' 分隔
  (v302+ 那次修复就是把它统一成 'T' 的), 所以 _utc_ts_to_iso 对它是**对的**;
  只有 review_insights 这张表存的是空格分隔, 用同一个 cutoff 去比就错。

  修法: 给 review_insights 单独用**空格分隔**的 cutoff (即 UTC 语义 + 空格),
  不复用 _utc_ts_to_iso 的 'T' 分隔串。

纯离线: 只用内存 SQLite + 纯函数, 不碰生产 DB / 不需要网络。
"""
from __future__ import annotations

import os
import sqlite3
import sys

_SRC_PYTHON = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src_python")
if _SRC_PYTHON not in sys.path:
    sys.path.insert(0, _SRC_PYTHON)


# ─────────────────────────────────────────────────────────────────────────────
# 口径常量 (与 api_server.py 保持一致, 改动时两边同步)
# ─────────────────────────────────────────────────────────────────────────────

# review_insights.created_at 的落库格式: UTC + **空格** 分隔
RI_STORE_FMT = "%Y-%m-%d %H:%M:%S"
# 该表专用的 cutoff 格式: 同样 UTC + **空格** 分隔 (与落库格式一致 → 字典序可靠)
RI_CMP_FMT = "%Y-%m-%d %H:%M:%S"


def _utc_space_str(epoch: int) -> str:
    """epoch → UTC 空格分隔串 (review_insights 专用 cutoff)。"""
    from datetime import datetime, timezone
    return datetime.fromtimestamp(int(epoch), tz=timezone.utc).strftime(RI_CMP_FMT)


def _epoch(text: str) -> int:
    from datetime import datetime, timezone
    return int(datetime.strptime(text, RI_STORE_FMT)
               .replace(tzinfo=timezone.utc).timestamp())


def _make_table(rows):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE review_insights (id INTEGER PRIMARY KEY, "
                 "created_at TEXT NOT NULL)")
    conn.executemany("INSERT INTO review_insights VALUES (?,?)", rows)
    return conn


# ─────────────────────────────────────────────────────────────────────────────
# 1. 复现 bug: 'T' 分隔 cutoff 对空格分隔存储 → 同一天整片漏掉
# ─────────────────────────────────────────────────────────────────────────────
def test_bug_repro_t_separated_cutoff_drops_same_day_rows():
    """这是 bug 的最小复现 —— 必须能证明旧逻辑是错的。

    行 '2026-10-25 08:00:00' (空格, UTC) 与 cutoff '2026-10-25T08:00:00' (T)
    在 SQLite 里按字节序比较 → ' ' < 'T' → >= 不成立 → 该行被漏掉。
    """
    conn = _make_table([(1, "2026-10-25 08:00:00")])
    # 旧逻辑 (错的): T 分隔 cutoff
    got_old = {r[0] for r in conn.execute(
        "SELECT id FROM review_insights WHERE created_at >= ?",
        ("2026-10-25T08:00:00",))}
    assert got_old == set(), (
        "本测试的前提是旧逻辑会漏行; 若这里过了说明 SQLite 比较语义变了, "
        "需要重新评估修复")

    # 新逻辑 (对的): 空格分隔 cutoff, 与落库格式一致
    got_new = {r[0] for r in conn.execute(
        "SELECT id FROM review_insights WHERE created_at >= ?",
        (_utc_space_str(_epoch("2026-10-25 08:00:00")),))}
    assert got_new == {1}, "空格分隔 cutoff 应该命中同一天的行"
    conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# 2. 新口径的行为测试: 窗口过滤必须正确
# ─────────────────────────────────────────────────────────────────────────────
def test_space_cutoff_window_filter_is_correct():
    """空格分隔 cutoff 的窗口过滤: 边界与区间都正确。"""
    conn = _make_table([
        (1, "2026-10-25 07:00:00"),   # 窗口外 (早)
        (2, "2026-10-25 08:00:00"),   # 窗口内 (起点, 含)
        (3, "2026-10-25 12:00:00"),   # 窗口内
        (4, "2026-10-25 18:00:00"),   # 窗口内 (终点, 含)
        (5, "2026-10-25 19:00:00"),   # 窗口外 (晚)
    ])
    lo = _utc_space_str(_epoch("2026-10-25 08:00:00"))
    hi = _utc_space_str(_epoch("2026-10-25 18:00:00"))
    got = {r[0] for r in conn.execute(
        "SELECT id FROM review_insights WHERE created_at >= ? AND created_at <= ?",
        (lo, hi))}
    assert got == {2, 3, 4}, f"窗口过滤结果不对: {got}"
    conn.close()


def test_cutoff_and_storage_format_agree_byte_for_byte():
    """cutoff 与落库格式必须**完全同形** —— 这是字典序比较成立的前提。

    这是防回归的关键断言: 一旦有人把 cutoff 改回 'T' 分隔 (因为别的表
    用的是 'T'), 这个测试立刻炸。
    """
    epoch = _epoch("2026-10-25 08:00:00")
    cutoff = _utc_space_str(epoch)
    stored = "2026-10-25 08:00:00"
    assert len(cutoff) == len(stored) == 19
    assert cutoff[10] == " " and stored[10] == " ", "分隔符必须都是空格"
    assert cutoff == stored, "同一时刻的 cutoff 与落库串必须逐字节相同"


def test_boundary_day_is_not_dropped():
    """跨天边界的经典错法: 同一天的行不能因为分隔符差异被整片漏掉。

    这是 dashboard "今日" 显示 0 条 / 全期数据 的根因。
    """
    conn = _make_table([
        (1, "2026-10-25 09:00:00"),   # 今天 09:00
        (2, "2026-10-25 23:59:59"),   # 今天 23:59:59
    ])
    lo = _utc_space_str(_epoch("2026-10-25 00:00:00"))
    hi = _utc_space_str(_epoch("2026-10-25 23:59:59"))
    got = {r[0] for r in conn.execute(
        "SELECT id FROM review_insights WHERE created_at >= ? AND created_at <= ?",
        (lo, hi))}
    assert got == {1, 2}, f"今天的行被漏掉了: {got}"
    conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# 3. 防回归: api_server.py 必须用**空格** cutoff 给 review_insights
# ─────────────────────────────────────────────────────────────────────────────
def test_review_insights_cutoff_uses_space_format():
    """静态钉死: 对 review_insights.created_at 做窗口过滤的函数, 必须用空格分隔 cutoff。

    v504 修正：这条防线原先只扫 api_server.py，而真正执行该查询的代码
    （routers/review.py）在早前的切片里已经搬走了；更糟的是 api_server 里
    唯一"命中"的其实是 _utc_ts_to_space_iso 这个 helper **自己的 docstring**
    （里面提到 review_insights.created_at）—— 也就是说这条测试一直在
    靠误报通过。helper 一搬走，误报消失，测试就挂了。

    现在的判据只认**真的写着查询**的函数：函数源码里同时出现
    `FROM review_insights` 与 `created_at >=`，才算被检查对象。
    """
    import ast

    files = [os.path.join(_SRC_PYTHON, "api_server.py"),
             os.path.join(_SRC_PYTHON, "routers", "review.py")]

    offenders: list[str] = []
    checked: list[str] = []
    for path in files:
        with open(path, encoding="utf-8") as f:
            src = f.read()
        tree = ast.parse(src, filename=path)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            seg = ast.get_source_segment(src, node) or ""
            if "FROM review_insights" not in seg:
                continue            # 只认真查询, 不认注释/docstring
            if "created_at >=" not in seg:
                continue
            checked.append(f"{os.path.basename(path)}::{node.name}")
            for sub in ast.walk(node):
                if not isinstance(sub, ast.Call):
                    continue
                fn = sub.func
                name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", "")
                if name == "_utc_ts_to_iso":
                    offenders.append(f"{os.path.basename(path)}::{node.name}")
                    break

    assert checked, (
        "没找到任何**真的查询** review_insights.created_at 的函数 —— "
        "代码结构又变了，本测试需要重新评估")
    assert not offenders, (
        f"这些函数仍用 _utc_ts_to_iso('T' 分隔) 过滤 review_insights "
        f"(空格分隔存储) → 同一天的行会被漏掉: {offenders}")


# 4. v309: raw_news.timestamp 的两种落库格式 (engine/ingest.py 与 api_server.py)
# ─────────────────────────────────────────────────────────────────────────────
# 落库格式 (v309 审计确认):
#   · engine/ingest.py 写 engine.utils._ts() → 'YYYY-MM-DDTHH:MM:SS.mmm+08:00'
#     ('T' 分隔, **北京**时间, 带 +08:00 偏移后缀)
#   · api_server.py 写 api_server._now_iso() → 'YYYY-MM-DDTHH:MM:SS+08:00'
#     (同为 'T' 分隔北京时间)
#   · db.py DEFAULT 是 datetime('now') → 'YYYY-MM-DD HH:MM:SS' (UTC, 空格分隔)
#     —— 仅当某条 INSERT 没显式给 timestamp 时才会用到
#
# 旧 bug: cutoff 用空格分隔串 → 字节序 ' '(0x20) < 'T'(0x54) → cutoff 当天的
# 行被整片漏掉。现在统一用 time_utils.sql_bj_ts_expr() 归一化。
def _make_raw_news_table(rows):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE raw_news (id INTEGER PRIMARY KEY, "
                 "timestamp TEXT NOT NULL, is_noise INTEGER DEFAULT 0)")
    conn.executemany("INSERT INTO raw_news (id, timestamp, is_noise) VALUES (?,?,?)",
                     rows)
    return conn


def test_raw_news_window_filter_handles_both_storage_formats():
    """'T' 分隔(北京) 与 ' ' 分隔(UTC) 两种写法必须归一化到同一北京墙钟。"""
    from common import time_utils
    conn = _make_raw_news_table([
        (1, "2026-10-25T16:00:00.000+08:00", 0),   # engine/ingest.py 写法
        (2, "2026-10-25T16:00:00+08:00", 0),        # api_server 写法
        (3, "2026-10-25 08:00:00", 0),              # DB DEFAULT (UTC) → 北京 16:00
    ])
    expr = time_utils.sql_bj_ts_expr("timestamp")
    rows = dict(conn.execute(f"SELECT id, {expr} FROM raw_news"))
    assert rows[1] == rows[2] == rows[3] == "2026-10-25 16:00:00", (
        f"同一时刻的多种写法归一化后不一致: {rows}")
    conn.close()


def test_raw_news_space_cutoff_misorders_boundary_day_rows():
    """反证: 旧逻辑 (空格 cutoff) 对 'T' 分隔的行在**边界日**判错方向。

    字节序: ' '(0x20) < 'T'(0x54) → 同一日期下, 'T' 串恒大于 ' ' 串。于是:
      · 下界 `>= lo`: 边界日**早于** lo 的行 → 判为 TRUE → 被**多算进来**
      · 上界 `<= hi`: 边界日**晚于** hi 的行 → 判为 FALSE → 被**漏掉**
    两种都是错的 —— 字典序 != 时间序。
    """
    conn = _make_raw_news_table([
        (1, "2026-10-25T08:00:00.000+08:00", 0),   # 边界日 08:00 → 窗口外, 但旧逻辑判 IN
        (2, "2026-10-26T10:00:00.000+08:00", 0),   # 边界日 10:00 → 窗口内, 但旧逻辑判 OUT
    ])
    lo = "2026-10-25 12:00:00"   # 空格分隔 cutoff
    hi = "2026-10-26 18:00:00"
    # 旧逻辑: 直接字典序比
    got_old = {r[0] for r in conn.execute(
        "SELECT id FROM raw_news WHERE timestamp >= ? AND timestamp <= ?",
        (lo, hi))}
    assert got_old == {1}, (
        f"本测试的前提是旧逻辑在边界日判错方向; 若这里过了说明 SQLite 比较语义变了, "
        f"需要重新评估修复。got={got_old}")

    # 新逻辑: 归一化成北京墙钟 + 空格分隔后比较 → 语义正确
    from common import time_utils
    expr = time_utils.sql_bj_ts_expr("timestamp")
    got_new = {r[0] for r in conn.execute(
        f"SELECT id FROM raw_news WHERE {expr} >= ? AND {expr} <= ?",
        (lo, hi))}
    assert got_new == {2}, f"归一化后窗口过滤结果不对: {got_new}"
    conn.close()


def test_raw_news_new_logic_hits_same_day_rows():
    """新逻辑: 归一化后用北京墙钟 cutoff → 当天的行必须命中。"""
    from common import time_utils
    conn = _make_raw_news_table([
        (1, "2026-10-25T16:00:00.000+08:00", 0),
        (2, "2026-10-25T08:00:00.000+08:00", 0),   # 更早
    ])
    expr = time_utils.sql_bj_ts_expr("timestamp")
    cutoff = time_utils.epoch_to_bj_str(time_utils.bj_str_to_epoch(
        "2026-10-25 12:00:00"))
    got = {r[0] for r in conn.execute(
        f"SELECT id FROM raw_news WHERE {expr} >= ?", (cutoff,))}
    assert got == {1}, f"当天的行被漏掉了: {got}"
    conn.close()


def test_api_server_events_loader_uses_bj_normalized_expr():
    """静态钉死: _events_15d_load_sync 必须用 sql_bj_ts_expr 归一化 raw_news.timestamp。"""
    import ast

    src_path = os.path.join(_SRC_PYTHON, "api_server.py")
    with open(src_path, encoding="utf-8") as f:
        src = f.read()
    tree = ast.parse(src, filename=src_path)

    found = False
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name != "_events_15d_load_sync":
            continue
        seg = ast.get_source_segment(src, node) or ""
        assert "sql_bj_ts_expr" in seg, (
            "_events_15d_load_sync 未使用 time_utils.sql_bj_ts_expr 归一化 "
            "raw_news.timestamp → cutoff 当天的行会被漏掉")
        assert "epoch_to_bj_str" in seg, (
            "_events_15d_load_sync 的 cutoff 必须用 time_utils.epoch_to_bj_str() "
            "(北京墙钟 + 空格分隔), 与归一化表达式同形")
        found = True

    assert found, "没找到 _events_15d_load_sync —— 代码结构变了, 需重新评估本测试"


# ─────────────────────────────────────────────────────────────────────────────
# 5. v309: causal_accuracy_stats 里两张表的 created_at 口径不同
# ─────────────────────────────────────────────────────────────────────────────
#   · review_insights.created_at → db.py DEFAULT datetime('now') = UTC, 空格分隔
#   · causal_chains.created_at   → db.py DEFAULT datetime('now','localtime')
#                                  = 本机 localtime, 空格分隔
# 旧代码用同一个**北京** cutoff 去比两张表 → 两边都错。
def test_causal_accuracy_stats_uses_distinct_cutoffs_per_table():
    """静态钉死: 两张表必须用各自的归一化表达式 + 各自的 cutoff。

    2026-11-17：端点已迁到 routers/causal_graph.py（结构整改 P2），扫新文件。
    """
    import ast

    src_path = os.path.join(_SRC_PYTHON, "routers", "causal_graph.py")
    with open(src_path, encoding="utf-8") as f:
        src = f.read()
    tree = ast.parse(src, filename=src_path)

    target = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == "causal_accuracy_stats":
                target = ast.get_source_segment(src, node) or ""
                break

    assert target, "没找到 causal_accuracy_stats —— 代码结构变了, 需重新评估本测试"
    assert "sql_utc_ts_expr" in target, (
        "review_insights (UTC 语义) 必须用 time_utils.sql_utc_ts_expr() 归一化")
    assert "sql_local_ts_expr" in target, (
        "causal_chains (本机 localtime 语义) 必须用 time_utils.sql_local_ts_expr() 归一化")
    assert "utc_str" in target, "review_insights 的 cutoff 必须用 utc_str() (UTC 墙钟)"
    assert "local_str" in target, "causal_chains 的 cutoff 必须用 local_str() (本机墙钟)"


def test_causal_accuracy_stats_no_longer_uses_single_shared_beijing_cutoff():
    """反证: 不得再用单一 since_iso (北京墙钟) 去比两张语义不同的表。

    2026-11-17：同上，扫 routers/causal_graph.py。
    """
    import ast

    src_path = os.path.join(_SRC_PYTHON, "routers", "causal_graph.py")
    with open(src_path, encoding="utf-8") as f:
        src = f.read()
    tree = ast.parse(src, filename=src_path)

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name != "causal_accuracy_stats":
            continue
        seg = ast.get_source_segment(src, node) or ""
        assert "since_iso" not in seg, (
            "causal_accuracy_stats 仍在用共享的 since_iso (北京墙钟) 比较 "
            "review_insights(UTC) 与 causal_chains(localtime) → 两边都错")


