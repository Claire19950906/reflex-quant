# -*- coding: utf-8 -*-
"""v322:「待分析」永不下降 —— 队头阻塞死循环的三条不变量回归单测。

事故背景（2026-11-05 生产实测）
--------------------------------
症状：前端「待分析」3320 条且 12 小时零下降；引擎日志显示 LLM 调用一直在跑
（main 33 / verify 78 / failover 16 次），但没有一条 news 变成 DONE。

根因链（逐环已验证）
--------------------
1. ``ai_decisions`` 的 INSERT **列与参数错位**：列第 9 位是 ``child_count``、
   第 10 位是 ``aggregation_key``，而 VALUES 把 ``agg_key``（'OIL|WTI|BUY'）
   传给第 9 位、把 ``0`` 传给第 10 位 → child_count 列被写成聚合键（373 行脏）。
2. 落库时 ``int(p_row["child_count"])`` 抛 ValueError → **整批 16 条**事务
   rollback → 全部退回 PENDING（实测 "Batch persist 失败" 累计 175 次）。
3. 认领 SQL 是 ``ORDER BY id ASC LIMIT 16`` → 同一批每轮被重新认领
   → **队头阻塞**，新新闻永远排不上。
4. 看门狗 ``except: pass`` 静默吞掉 DB 锁 → PROCESSING 僵尸永不回收
   → ``retry_count`` 永远 0 → 队头永不让位。

本文件锁死的不变量
------------------
A. INSERT 的列/参数不再错位：child_count 传 0，aggregation_key 传 agg_key
B. 脏 child_count 不得炸掉整批（_safe_child_count 兜底）
C. 队头阻塞兜底：落库失败的行进入冷却期，认领时跳过冷却中的行
"""

from __future__ import annotations

import os
import re
import sqlite3
import sys

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import pytest

from engine import ai_worker

AI_WORKER_PY = os.path.join(SRC, "engine", "ai_worker.py")

RAW_NEWS_DDL = """
CREATE TABLE raw_news (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    -- ⚠️ 必须带上**真实**的 CHECK 约束（与线上 raw_news 一致）。
    -- v322 事故：这张表原来没有 CHECK → 单测全绿 → 生产上每条
    -- status='FILTERED' 的 UPDATE 都被数据库拒绝 → 「待分析」永不下降。
    -- 测试用假表而生产用真表，是这类 bug 能溜过去的根本原因。
    -- 注意写法：CHECK 是 status 的**列级**约束（中间没有逗号）——
    -- SQLite 的语法要求表级约束必须放在所有列定义之后。
    status TEXT DEFAULT 'PENDING'
        CHECK (status IN ('PENDING', 'PROCESSING', 'DONE', 'FAILED', 'FILTERED')),
    is_noise INTEGER DEFAULT 0,
    gate_unavailable INTEGER DEFAULT 0,
    -- v413: 线上 raw_news 有这列（门卫结论），认领优先级与归档"可交易不丢"都要用它。
    -- 之前测试表缺这列 → v408 的归档 SQL 与 v413 的排序一上线就整条报
    -- "no such column: gate_verdict"。测试表必须与线上同构。
    gate_verdict TEXT DEFAULT '',
    retry_count INTEGER DEFAULT 0,
    processing_at TEXT,
    relevance_score REAL DEFAULT 0.0,
    content TEXT DEFAULT '',
    -- v322: 认领时效门槛要按新闻时间过滤 → 必须有 timestamp 列
    timestamp TEXT DEFAULT (datetime('now','localtime')),
    -- v322: 门槛实际用 ts（epoch 整数，无时区歧义）
    ts INTEGER
);
"""


def _extract_insert_block():
    """从源码里抽出 INSERT 的列清单与参数元组（按源码文本，不执行代码）。"""
    src = open(AI_WORKER_PY, encoding="utf-8").read()
    # 定位列清单最完整的那条主插入（含 aggregation_key 的落库路径），
    # 避免命中别处只有 7 列的简版回填插入。
    i = src.index("aggregation_key")
    i = src.rindex("INSERT INTO ai_decisions", 0, i)
    seg = src[i:i + 12000]
    cols_part = seg[seg.index("(") + 1:re.search(r"\)\s*VALUES", seg).start()]
    cols = [c.strip() for c in cols_part.replace("\n", " ").split(",")]
    cols = [c for c in cols if c]
    vstart = seg.index("VALUES (")
    # 先跳过 SQL 里的 "VALUES (?, ?, ...)" 占位符段
    depth = 1
    i2 = vstart + len("VALUES (")
    while depth > 0:
        if seg[i2] == "(":
            depth += 1
        elif seg[i2] == ")":
            depth -= 1
        i2 += 1
    # 再往后找 Python 侧的参数元组 "("（SQL 串结束、逗号之后）
    # v408: 参数改成 tuple(_db_safe(_px) for _px in ( ... )) 的生成器包装，
    # 直接取第一个 "(" 会拿到 tuple( 本身、把整个生成器当成 1 个参数 → 误报"列数≠参数数"。
    # 这里先剥掉外层包裹，定位到 for ... in ( 之后那个真正的参数元组。
    k = seg.find("(", i2)
    head = seg[i2:k]
    if "tuple" in head or head.strip() == "tuple":
        gen = seg.find("in (", k)
        if gen != -1:
            k = seg.find("(", gen)
    pstart = k
    depth = 1
    j = pstart + 1
    while depth > 0:
        if seg[j] == "(":
            depth += 1
        elif seg[j] == ")":
            depth -= 1
        j += 1
    params_txt = seg[pstart + 1:j - 1]
    # 剥离注释（注意跳过字符串字面量里的 '#'，避免误伤 URL）
    cleaned = []
    in_s = None
    i3 = 0
    while i3 < len(params_txt):
        ch = params_txt[i3]
        if in_s:
            cleaned.append(ch)
            if ch == in_s:
                in_s = None
            i3 += 1
            continue
        if ch in ("'", '"'):
            in_s = ch
            cleaned.append(ch)
            i3 += 1
            continue
        if ch == "#":
            while i3 < len(params_txt) and params_txt[i3] != "\n":
                i3 += 1
            continue
        cleaned.append(ch)
        i3 += 1
    params_txt = "".join(cleaned)
    depth = 0
    parts = []
    buf = ""
    for ch in params_txt:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(buf.strip())
            buf = ""
        else:
            buf += ch
    if buf.strip():
        parts.append(buf.strip())
    return cols, parts



# ───────────────── 不变量 B：脏 child_count 不炸整批 ─────────────────

@pytest.mark.parametrize("dirty", [
    "OIL|WTI|BUY",
    "GOLD|XAU|BUY",
    "CRYPTO|ETH|SELL",
    "",
    None,
    "not-a-number",
    [],
])
def test_safe_child_count_never_raises(dirty):
    """脏值一律当 0，绝不抛异常（抛异常 = 整批 16 条作废）。"""
    assert ai_worker._safe_child_count(dirty) == 0


def test_safe_child_count_reads_valid_ints():
    assert ai_worker._safe_child_count(0) == 0
    assert ai_worker._safe_child_count(3) == 3
    assert ai_worker._safe_child_count("7") == 7
    assert ai_worker._safe_child_count(None) == 0
    assert ai_worker._safe_child_count("") == 0


def test_safe_child_count_is_used_in_persist_path():
    """落库路径必须走 _safe_child_count，不得再有裸 int()（防止回退）。"""
    src = open(AI_WORKER_PY, encoding="utf-8").read()
    # 只看代码行：注释里提到 int(child_count) 是允许的（那是事故说明）
    code_only = "\n".join(
        ln for ln in src.splitlines() if not ln.strip().startswith("#"))
    bare = re.findall(r'int\(\s*\w+\[["\']child_count["\']', code_only)
    assert not bare, (
        "落库路径仍有裸 int(child_count)：%r —— 脏数据会再次炸掉整批" % (bare,))


# ─────────────── 不变量 C：队头阻塞兜底（冷却期跳过） ───────────────

_MIN_AI_DECISIONS_DDL = """
CREATE TABLE IF NOT EXISTS ai_decisions (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    news_id INTEGER NOT NULL
);
"""


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "t.db"))
    c.row_factory = sqlite3.Row
    c.executescript(RAW_NEWS_DDL)
    # v339: _CLAIM_DEDUP 里有 "NOT EXISTS (SELECT 1 FROM ai_decisions ...)"，
    # 夹具只建了 raw_news → 认领 SQL 直接 "no such table: ai_decisions"。
    # 本文件不写 ai_decisions，建最小空表足够（去重语义 = 空表全部可认领）。
    c.executescript(_MIN_AI_DECISIONS_DDL)
    yield c
    c.close()


def _insert(conn, **kw):
    cols = ", ".join(kw)
    marks = ", ".join("?" for _ in kw)
    conn.execute("INSERT INTO raw_news (%s) VALUES (%s)" % (cols, marks),
                 tuple(kw.values()))
    conn.commit()


def test_claim_skips_rows_in_cooldown(conn):
    """落库失败的行处于冷却期（processing_at = 未来时间）→ 不得被立刻重新认领。"""
    _insert(conn, id=1, status="PENDING", is_noise=0, gate_unavailable=0,
            retry_count=1, processing_at="2099-01-01 00:00:00")
    _insert(conn, id=2, status="PENDING", is_noise=0, gate_unavailable=0,
            retry_count=0, processing_at=None)

    rows = conn.execute(ai_worker._BATCH_CLAIM_SQL, (10,)).fetchall()
    conn.commit()

    assert [r["id"] for r in rows] == [2], (
        "冷却中的行不得被认领 —— 否则队头被同一批反复霸占，新新闻永远排队")


def test_claim_still_works_for_normal_rows(conn):
    """正常 PENDING 行照常被认领（冷却逻辑不能误伤正常吞吐）。"""
    _insert(conn, id=10, status="PENDING", is_noise=0, gate_unavailable=0,
            retry_count=0, processing_at=None)
    rows = conn.execute(ai_worker._BATCH_CLAIM_SQL, (10,)).fetchall()
    conn.commit()
    assert [r["id"] for r in rows] == [10]
    got = conn.execute("SELECT status FROM raw_news WHERE id=10").fetchone()
    assert got["status"] == "PROCESSING"


def test_claim_respects_order_and_limit(conn):
    """仍然是最老的优先 + 限批量（队头推进的基本语义不能被改坏）。"""
    for i in range(1, 6):
        _insert(conn, id=i, status="PENDING", is_noise=0, gate_unavailable=0,
                retry_count=0, processing_at=None)
    rows = conn.execute(ai_worker._BATCH_CLAIM_SQL, (3,)).fetchall()
    conn.commit()
    assert [r["id"] for r in rows] == [1, 2, 3]


def test_claim_does_not_reset_retry_count(conn):
    """v321.1 不变量保持：认领不得清零 retry_count。"""
    _insert(conn, id=20, status="PENDING", is_noise=0, gate_unavailable=0,
            retry_count=2, processing_at=None)
    conn.execute(ai_worker._BATCH_CLAIM_SQL, (10,)).fetchall()
    conn.commit()
    got = conn.execute("SELECT retry_count FROM raw_news WHERE id=20").fetchone()
    assert got["retry_count"] == 2


# ───────── 不变量 D：冷却时长必须 > 单批最坏耗时（否则等于没冷却） ─────────

# 实测单批 wall（2026-11-05 生产日志）：Batch done | 16/16 ok | wall=390.2s
# 冷却若短于它 → 冷却一过同批立刻被重新认领 → 无限循环。
_OBSERVED_MAX_BATCH_WALL_SEC = 390.2


def test_retry_cooldown_exceeds_observed_batch_wall():
    """冷却必须长于实测单批最坏耗时 —— 否则冷却形同虚设。

    事故（2026-11-05 下午）：冷却 120 秒 < 单批 390 秒
    → `claimed 16 rows (ids=[11974, 11975, ...])` 反复出现同一批 id
    → 队头永久霸占 → 新新闻永远排不上 → 「待分析」只增不减。
    """
    assert ai_worker._BATCH_RETRY_COOLDOWN_SEC > _OBSERVED_MAX_BATCH_WALL_SEC, (
        "冷却 %ds 必须 > 单批最坏耗时 %.0fs —— 否则同一批会被立刻重新认领"
        % (ai_worker._BATCH_RETRY_COOLDOWN_SEC, _OBSERVED_MAX_BATCH_WALL_SEC)
    )


def test_watchdog_reclaim_applies_cooldown_not_null(conn):
    """看门狗回收后必须写「冷却到什么时候」，不能置 NULL。

    置 NULL → 这行立刻可被认领 → 引擎重启后第一批又认领同一批 id
    → 又跑 390 秒 → 又失败 → 无限循环。
    """
    _insert(conn, id=30, status="PROCESSING", is_noise=0, gate_unavailable=0,
            retry_count=0,
            processing_at="2000-01-01 00:00:00")   # 远超 15 分钟 → 视为僵尸
    sql, params = ai_worker._build_reclaim_sql([])
    conn.execute(sql, params)
    conn.commit()
    row = conn.execute(
        "SELECT status, processing_at FROM raw_news WHERE id=30").fetchone()
    assert row["status"] == "PENDING"
    assert row["processing_at"] is not None, (
        "回收后 processing_at 必须写成冷却时刻（未来时间），"
        "置 NULL 等于立刻可被重新认领 → 队头永久霸占"
    )
    # 且冷却必须真的在未来（此刻还认领不到它）
    cooldown_rows = conn.execute(
        ai_worker._BATCH_CLAIM_SQL, (10,)).fetchall()
    assert cooldown_rows == [], "冷却中的行不得被立即认领"


def test_reclaim_sql_params_match_placeholders():
    """回收 SQL 的占位符个数必须与参数个数一致（改 SQL 忘了改参数 = 直接抛错）。"""
    sql, params = ai_worker._build_reclaim_sql([])
    assert sql.count("?") == len(params), (
        "回收 SQL 占位符 %d ≠ 参数 %d" % (sql.count("?"), len(params)))
    sql2, params2 = ai_worker._build_reclaim_sql([1, 2, 3])
    assert sql2.count("?") == len(params2), (
        "带排除子句时占位符 %d ≠ 参数 %d" % (sql2.count("?"), len(params2)))
    assert "id NOT IN" in sql2


# ───────── 不变量 E：认领时效门槛（陈旧新闻不送去分析） ─────────

def test_claim_sql_has_freshness_gate():
    """生产认领 SQL 必须带时效门槛（陈旧新闻不浪费额度）。"""
    sql, params = ai_worker._build_claim_sql(10)
    assert sql.count("?") == len(params), (
        "认领 SQL 占位符 %d ≠ 参数 %d —— 会抛 Incorrect number of bindings"
        % (sql.count("?"), len(params)))
    assert "? <= 0" in sql, "认领 SQL 必须带时效门槛开关（0 = 不限制）"
    # 参数顺序：[max_age_hours, cutoff, deg_min, deg_cutoff, trade_cutoff, limit]
    # （v418 加了第三档建仓窗口边界，v432 加了门卫未判定行的降级时效边界）
    assert params[0] == int(ai_worker.config.NEWS_ANALYZE_MAX_AGE_HOURS)
    assert params[-1] == 10, "最后一个参数必须是 limit"


def test_claim_skips_stale_news(conn):
    """超过时效门槛的新闻**不得被认领**（否则额度烧在用不上的老新闻上）。"""
    import time as _t
    now_ts = int(_t.time())
    _insert(conn, id=101, status="PENDING", is_noise=0, gate_unavailable=0,
            retry_count=0, processing_at=None, ts=now_ts - 14 * 86400)
    _insert(conn, id=102, status="PENDING", is_noise=0, gate_unavailable=0,
            retry_count=0, processing_at=None, ts=now_ts)

    sql, params = ai_worker._build_claim_sql(10)
    rows = conn.execute(sql, params).fetchall()
    conn.commit()
    ids = [r["id"] for r in rows]
    assert 101 not in ids, "14 天前的新闻不得被认领（额度照烧但信号用不上）"
    assert 102 in ids, "新鲜新闻必须照常被认领"


def test_claim_freshness_gate_disabled_keeps_old_behavior(conn):
    """门槛设为 0（不限制）时，行为与旧版一致（老新闻也能被认领）。"""
    import time as _t
    now_ts = int(_t.time())
    _insert(conn, id=111, status="PENDING", is_noise=0, gate_unavailable=0,
            retry_count=0, processing_at=None, ts=now_ts - 14 * 86400)

    _old = ai_worker.config.NEWS_ANALYZE_MAX_AGE_HOURS
    try:
        ai_worker.config.NEWS_ANALYZE_MAX_AGE_HOURS = 0
        sql, params = ai_worker._build_claim_sql(10)
        rows = conn.execute(sql, params).fetchall()
        conn.commit()
        assert [r["id"] for r in rows] == [111], "门槛关闭时应保持旧行为"
    finally:
        ai_worker.config.NEWS_ANALYZE_MAX_AGE_HOURS = _old


def test_expire_stale_news_marks_filtered(conn):
    """超龄新闻必须被归档（标 FILTERED），不再占「待分析」计数。

    加了认领时效门槛后，积压的老新闻永远不会被认领 —— 但仍留在 PENDING
    → 前端「待分析」显示 2500 条，实际一条也处理不了。
    """
    import time as _t
    now_ts = int(_t.time())
    _insert(conn, id=121, status="PENDING", is_noise=0, gate_unavailable=0,
            retry_count=0, processing_at=None, ts=now_ts - 14 * 86400)
    _insert(conn, id=122, status="PENDING", is_noise=0, gate_unavailable=0,
            retry_count=0, processing_at=None, ts=now_ts)

    n = ai_worker._expire_stale_news(conn)
    conn.commit()
    assert n == 1, "应只归档超龄的那 1 条（新鲜的不动）"
    row = conn.execute(
        "SELECT status FROM raw_news WHERE id=121").fetchone()
    assert row["status"] == "FILTERED", "超龄新闻必须标 FILTERED（不再占待分析计数）"
    row2 = conn.execute(
        "SELECT status FROM raw_news WHERE id=122").fetchone()
    assert row2["status"] == "PENDING", "新鲜新闻不得被归档"


def test_expire_stale_also_archives_gate_misjudged_noise(conn):
    """归档**不过滤 is_noise** —— 被门卫误杀的正常新闻也是僵尸。

    实测（2026-11-05）：2340 条 PENDING 里 is_noise=1 占 61%，
    抽样全是正常新闻（波兰央行/IIF 数据/美联储讲话…），gate_verdict 全是「排除」
    —— 那是门卫把「模型没回答」当成「不是噪音」的误杀（见 10.18）。
    它们同样永远不会被分析 → 必须一并归档，否则「待分析」计数降不下来。
    """
    import time as _t
    now_ts = int(_t.time())
    _insert(conn, id=141, status="PENDING", is_noise=1, gate_unavailable=0,
            retry_count=0, processing_at=None, ts=now_ts - 14 * 86400)

    n = ai_worker._expire_stale_news(conn)
    conn.commit()
    assert n == 1, "被门卫误杀的僵尸行也必须归档"
    row = conn.execute(
        "SELECT status FROM raw_news WHERE id=141").fetchone()
    assert row["status"] == "FILTERED", (
        "is_noise=1 的僵尸行必须归档 —— 否则「待分析」计数永远降不下来"
    )


def test_expire_stale_respects_gate_frozen_rows(conn):
    """门卫冻结行（gate_unavailable=1）不得被归档 —— 它们还要等回放。"""
    import time as _t
    now_ts = int(_t.time())
    _insert(conn, id=131, status="PENDING", is_noise=0, gate_unavailable=1,
            retry_count=0, processing_at=None, ts=now_ts - 14 * 86400)

    ai_worker._expire_stale_news(conn)
    conn.commit()
    row = conn.execute(
        "SELECT status FROM raw_news WHERE id=131").fetchone()
    assert row["status"] == "PENDING", "门卫冻结行要留给回放，不能直接归档"


# ───────────────────── 不变量 A：INSERT 参数不再错位 ─────────────────────

def test_insert_columns_and_params_are_aligned():
    cols, params = _extract_insert_block()
    assert len(cols) == len(params), (
        "ai_decisions INSERT 列数 %d ≠ 参数数 %d —— 错位会静默写坏列"
        % (len(cols), len(params)))


def test_child_count_gets_zero_not_agg_key():
    cols, params = _extract_insert_block()
    idx = cols.index("child_count")
    assert params[idx].strip() == "0", (
        "child_count 第 %d 位参数应为 0，实际 %r —— 会把聚合键写进整数列"
        % (idx + 1, params[idx]))


def test_aggregation_key_gets_agg_key():
    cols, params = _extract_insert_block()
    idx = cols.index("aggregation_key")
    assert "agg_key" in params[idx], (
        "aggregation_key 第 %d 位参数应含 agg_key，实际 %r"
        % (idx + 1, params[idx]))


def test_child_count_and_aggregation_key_not_swapped():
    cols, params = _extract_insert_block()
    cc = params[cols.index("child_count")]
    ak = params[cols.index("aggregation_key")]
    assert "agg_key" not in cc, "child_count 收到了 agg_key —— 事故根因"
    assert ak.strip() != "0", "aggregation_key 收到了 0 —— 两列参数互换"
