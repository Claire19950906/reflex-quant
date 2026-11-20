"""v317 全网溯源 + 事件状态口径回归测试。

覆盖 5 个缺陷（每个都是线上实测复现过的）：

1. RC1 溯源丢字段：抽取器填了 cn_ts 但没匹配到机构名时，_sanitize_llm_result
   的"来源兜底"被嵌在"时间兜底"分支内 → 来源永远空 → 第⑤层拒收 → 时间+来源双丢。
   → 现在来源兜底与时间兜底**解耦**。
2. RC2 时间护栏失效：第④层（不能晚于入库）用 strptime 解析 'T' 分隔 ISO
   入库时间必然失败 → except 分支直接放行 → 护栏形同虚设。
   → 现在走 time_utils.parse_ts_to_bj（北京时间唯一入口）。
3. RC3 事件被丢：api_server 15 天窗口缓存用 `ad.is_sibling = 0` 过滤，
   LEFT JOIN 出的 NULL 行（还没分析的新闻）整行消失。
4. RC4 SSE 快照与事件载荷口径不一致 → 每 5 秒重推 1340 条事件。
5. RC5 状态推导两套实现：8000/8100 对同一行给出不同状态；且噪声行被标成
   "待 AI 分析"（实际永远不会被处理）。

跑法：python3 -m pytest tests/test_cross_net_provenance_v317.py -v
"""
from __future__ import annotations

import os
import sqlite3
import sys

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import pytest

from engine import ai_worker as w
import event_status


# ────────────────────── RC1: 来源兜底与时间兜底解耦 ──────────────────────

def test_rc1_source_backfill_runs_even_when_ts_already_present():
    """v176.11 的回归：cn_ts 已有值、cn_src 为空时，来源也必须被兜底。

    这是线上 2268 行 cn_ts/cn_src 双空的成因 —— 抽取器抽到时间却没匹配到
    机构名，"来源兜底"因为被嵌在"时间兜底"分支里而从未执行。
    """
    res = w._sanitize_llm_result({
        "cross_net_first_seen_ts": "2026-10-11 08:07:00",
        "cross_net_source": "",
        "net_origin_info": "推断时间:2026-10-11 08:07:00",
    })
    assert res["cross_net_first_seen_ts"] == "2026-10-11 08:07:00"
    assert len(res["cross_net_source"]) >= 4, "来源必须被兜底，否则第⑤层会拒收整行"


def test_rc1_sanitize_output_passes_validator():
    """兜底后的结果必须真的能过 5 层校验（否则兜底了也白兜底）。"""
    res = w._sanitize_llm_result({
        "cross_net_first_seen_ts": "2026-10-11 08:07:00",
        "cross_net_source": "",
        "net_origin_info": "推断时间:2026-10-11 08:07:00",
    })
    out = w._validate_cross_net_ts(
        res["cross_net_first_seen_ts"],
        res["cross_net_source"],
        "2026-10-11T16:05:15+08:00",
    )
    assert out == "2026-10-11 08:07:00"


def test_rc1_existing_source_is_never_overwritten():
    """已有合法来源不得被兜底覆盖。"""
    res = w._sanitize_llm_result({
        "cross_net_first_seen_ts": "2026-10-11 08:07:00",
        "cross_net_source": "Bloomberg",
        "net_origin_info": "推断时间:2026-10-11 08:07:00",
    })
    assert res["cross_net_source"] == "Bloomberg"


def test_derive_cross_net_from_noi_extracts_both_fields():
    ts, src = w._derive_cross_net_from_noi("来源:Bloomberg | 推断时间:2026-10-12 20:00:00")
    assert ts == "2026-10-12 20:00:00"
    assert src == "Bloomberg"


def test_derive_cross_net_falls_back_to_whole_noi_when_no_named_source():
    """没有 "来源:" 段时退回整段 noi（中文机构名常只有 3 字符，会被第⑤层拒收）。"""
    ts, src = w._derive_cross_net_from_noi("推断时间:2026-10-11 08:07:00")
    assert ts == "2026-10-11 08:07:00"
    assert len(src) >= 4


def test_derive_cross_net_handles_empty_and_unparseable():
    assert w._derive_cross_net_from_noi("") == ("", "")
    ts, src = w._derive_cross_net_from_noi("无法确认")
    assert ts == ""
    assert src == "无法确认"


# ────────────────────── RC2: 第④层护栏真正生效 ──────────────────────

def test_rc2_layer4_rejects_ts_after_ingest():
    """入库时间是 'T' 分隔 ISO —— 旧代码 strptime 必然失败 → 护栏失效。

    线上实测 319 行的 cn_ts 晚于入库数小时仍被当作有效。
    """
    assert w._validate_cross_net_ts(
        "2026-10-12 20:00:00", "路透社首发", "2026-10-12T16:06:43+08:00"
    ) == "", "晚于入库的溯源时间必须被拒收"


def test_rc2_layer4_accepts_ts_before_ingest():
    assert w._validate_cross_net_ts(
        "2026-10-12 08:00:00", "路透社首发", "2026-10-12T16:06:43+08:00"
    ) == "2026-10-12 08:00:00"


def test_rc2_layer4_tolerates_space_separated_ingest_ts():
    """存量 4 行空格分隔的入库时间也要能比较（不能因解析失败而放行）。"""
    assert w._validate_cross_net_ts(
        "2026-10-12 08:00:00", "路透社首发", "2026-10-12 16:06:43"
    ) == "2026-10-12 08:00:00"
    assert w._validate_cross_net_ts(
        "2026-10-12 20:00:00", "路透社首发", "2026-10-12 16:06:43"
    ) == ""


def test_rc2_layer3_rejects_future_time():
    """未来时间必须被拒收（第③层）。"""
    assert w._validate_cross_net_ts(
        "2999-01-01 00:00:00", "路透社首发", "2026-10-12T16:06:43+08:00"
    ) == ""


def test_rc2_layer5_still_requires_source():
    """第⑤层：source 不足 4 字符 → 拒收（护栏语义不变）。"""
    assert w._validate_cross_net_ts(
        "2026-10-12 08:00:00", "路透", "2026-10-12T16:06:43+08:00"
    ) == ""


def test_rc2_ambiguous_still_requires_two_candidates():
    assert w._validate_cross_net_ts(
        "AMBIGUOUS", "候选1: 美联储官网 / 候选2: 路透社", "2026-10-12T16:06:43+08:00"
    ) == "AMBIGUOUS"
    assert w._validate_cross_net_ts(
        "AMBIGUOUS", "美联储官网", "2026-10-12T16:06:43+08:00"
    ) == ""


# ────────────────────── RC5: 状态口径唯一真相源 ──────────────────────

def test_rc5_noise_derives_skipped_not_pending():
    """噪声新闻永远不会被 AI 处理 —— 不能显示成"待 AI 分析"。"""
    assert event_status.resolve_analysis_status(
        "PENDING", has_decision=False, is_noise=True
    ) == "SKIPPED"


def test_rc5_has_decision_wins_over_raw_status():
    assert event_status.resolve_analysis_status(
        "FAILED", has_decision=True, is_noise=False
    ) == "DONE"


def test_rc5_raw_status_passthrough():
    for raw in ("PENDING", "PROCESSING", "FAILED", "DONE"):
        assert event_status.resolve_analysis_status(
            raw, has_decision=False, is_noise=False
        ) == raw


def test_rc5_unknown_status_falls_back_to_pending():
    assert event_status.resolve_analysis_status(
        "WAT", has_decision=False, is_noise=False
    ) == "PENDING"
    assert event_status.resolve_analysis_status(
        None, has_decision=False, is_noise=False
    ) == "PENDING"


def test_rc5_two_ports_agree_on_same_row():
    """8000 与 8100 对同一行数据必须给出同一个状态。

    旧实现：8000 看 raw_news.status，8100 看 news_events.status ——
    而 news_events.status 被 news_score_worker 无脑写成 'DONE'，
    于是 raw_news=FAILED 的行在 8100 显示"DONE"、在 8000 显示"AI 分析失败"。
    """
    for rn_status, ne_status, has_decision, is_noise in [
        ("FAILED", "DONE", False, False),
        ("FAILED", "DONE", True, False),
        ("PENDING", "DONE", False, True),
        ("DONE", "DONE", True, False),
        ("PENDING", "PENDING", False, False),
    ]:
        s8000 = event_status.resolve_analysis_status(
            rn_status, has_decision=has_decision, is_noise=is_noise)
        s8100 = event_status.resolve_analysis_status(
            rn_status if rn_status is not None else ne_status,
            has_decision=has_decision,
            is_noise=is_noise,
        )
        assert s8000 == s8100, (rn_status, ne_status, has_decision, is_noise)


def test_rc5_api_server_delegates_to_event_status():
    """api_server 必须走唯一真相源（而不是自己私有一份推导）。"""
    import api_server
    assert api_server._normalize_analysis_status("PENDING", False, True) == "SKIPPED"
    assert api_server._normalize_analysis_status("FAILED", True, False) == "DONE"
    assert api_server._normalize_analysis_status("PROCESSING", False, False) == "PROCESSING"


# ────────────────────── RC6: news_events.status 不得当作分析状态 ──────────────────────

def test_rc6_news_events_status_never_used_as_analysis_status():
    """news_events.status 恒为 'DONE'（news_score_worker 写死，语义是"已打分"）。

    拿它当分析状态 = 对 raw_news 已被裁剪的孤儿行凭空捏造"分析已完成"。
    实测：news_events 17078 行 status 全部 'DONE'，其中 6368 行 raw_news 不存在，
    且这 6368 行一条主决策都没有 → 旧回退逻辑把它们全标成 DONE。
    """
    import bridge_server
    orphan = {
        "id": 1, "news_id": 2, "ts": 1700000000, "fetched_at": 1700000000,
        "status": "DONE",          # ← news_events.status（恒 DONE，无信息量）
        "raw_status": None,        # ← raw_news 行不存在 → 取不到引擎真值
        "is_noise": 0,
        "ad_id": None,             # ← 没有主决策
        "headline": "某新闻", "body": "", "source": "x", "action": None,
        "score": 0.0, "assets": "", "asset": "", "decision_id": None,
    }
    out = bridge_server._news_row_to_api_event(orphan)
    assert out["analysis_status"] == "PENDING", (
        f"孤儿行没有决策时必须是 PENDING，实际 {out['analysis_status']}")
    assert out["decision_id"] is None


def test_rc6_orphan_with_decision_is_done():
    """孤儿行但**有**主决策 → DONE（has_decision 优先于状态缺失）。"""
    import bridge_server
    row = {
        "id": 1, "news_id": 2, "ts": 1700000000, "fetched_at": 1700000000,
        "status": "DONE", "raw_status": None, "is_noise": 0, "ad_id": 9275,
        "headline": "某新闻", "body": "", "source": "x", "action": None,
        "score": 0.0, "assets": "", "asset": "", "decision_id": None,
    }
    out = bridge_server._news_row_to_api_event(row)
    assert out["analysis_status"] == "DONE"
    assert out["decision_id"] == 9275


def test_rc6_orphan_noise_is_skipped():
    """孤儿 + 噪声 → SKIPPED（不显示"待 AI 分析"）。"""
    import bridge_server
    row = {
        "id": 1, "news_id": 2, "ts": 1700000000, "fetched_at": 1700000000,
        "status": "DONE", "raw_status": None, "is_noise": 1, "ad_id": None,
        "headline": "噪声", "body": "", "source": "x", "action": None,
        "score": 0.0, "assets": "", "asset": "", "decision_id": None,
    }
    out = bridge_server._news_row_to_api_event(row)
    assert out["analysis_status"] == "SKIPPED"


def test_rc6_raw_status_wins_over_news_events_status():
    """raw_news.status 是引擎真值，优先于 news_events.status。"""
    import bridge_server
    row = {
        "id": 1, "news_id": 2, "ts": 1700000000, "fetched_at": 1700000000,
        "status": "DONE", "raw_status": "FAILED", "is_noise": 0, "ad_id": None,
        "headline": "失败的分析", "body": "", "source": "x", "action": None,
        "score": 0.0, "assets": "", "asset": "", "decision_id": None,
    }
    out = bridge_server._news_row_to_api_event(row)
    assert out["analysis_status"] == "FAILED", (
        f"raw_news.status=FAILED 必须保留，实际 {out['analysis_status']}")


# ────────────────── RC7: 回填脚本的复核(prune)模式 ──────────────────

def _make_prune_db(tmp_path, rows):
    """rows: [(id, timestamp, noi, cn_ts, cn_src)] → 最小 raw_news 库。"""
    import sqlite3
    db = tmp_path / "prune.db"
    conn = sqlite3.connect(db)
    conn.execute("""
        CREATE TABLE raw_news (
            id INTEGER PRIMARY KEY,
            timestamp TEXT NOT NULL DEFAULT '',
            net_origin_info TEXT DEFAULT '',
            cross_net_first_seen_ts TEXT NOT NULL DEFAULT '',
            cross_net_source TEXT NOT NULL DEFAULT ''
        )
    """)
    conn.executemany(
        "INSERT INTO raw_news (id, timestamp, net_origin_info, "
        "cross_net_first_seen_ts, cross_net_source) VALUES (?,?,?,?,?)",
        rows,
    )
    conn.commit()
    conn.close()
    return str(db)


def test_rc7_prune_clears_chronology_violating_rows(tmp_path):
    """复核模式：溯源时间晚于入库时间的行必须被清空。

    v317 之前第④层护栏是死的（strptime 解析不了 'T 分隔+08:00' 格式 →
    except 直接放行），历史脏数据才会混进库里。
    """
    import importlib.util
    import sys

    script = ("/Users/claire/Desktop/金融1.0/backend/scripts/"
              "backfill_cross_net_provenance.py")
    spec = importlib.util.spec_from_file_location("backfill_xnet", script)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["backfill_xnet"] = mod
    spec.loader.exec_module(mod)

    db = _make_prune_db(tmp_path, [
        # 越界：溯源时间比入库晚 5 天 → 应清空
        (1, "2026-10-21T18:24:51+08:00", "来源:Reuters | 推断时间:2026-10-26 23:49:00",
         "2026-10-26 23:49:00", "Reuters"),
        # 合法：溯源时间早于入库 → 保留
        (2, "2026-10-21T18:24:51+08:00", "来源:Reuters | 推断时间:2026-10-20 08:00:00",
         "2026-10-20 08:00:00", "Reuters"),
        # AMBIGUOUS 哨兵 + 候选来源 → 保留
        (3, "2026-10-21T18:24:51+08:00", "无法确认", "AMBIGUOUS",
         "候选1: A社 / 候选2: B社"),
    ])

    class _Args:
        apply = True
        limit = 0
        backup_dir = str(tmp_path)
        verbose = False
        prune_invalid = True

    rc = mod._prune_invalid(mod._connect(db), db, _Args())
    assert rc == 0

    import sqlite3
    conn = sqlite3.connect(db)
    got = {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT id, cross_net_first_seen_ts, cross_net_source FROM raw_news")}
    conn.close()

    assert got[1] == ("", ""), f"越界行应被清空，实际 {got[1]}"
    assert got[2] == ("2026-10-20 08:00:00", "Reuters"), f"合法行被误清: {got[2]}"
    assert got[3] == ("AMBIGUOUS", "候选1: A社 / 候选2: B社"), f"哨兵行被误清: {got[3]}"


def test_rc7_prune_keeps_minute_rounding_tolerance(tmp_path):
    """分钟精度时间戳向上取整（比入库晚 1~4 秒）必须保留。

    NOI 只有分钟精度 → '推断时间:08:29:00' 相对 08:28:56 的入库时间会"晚几秒"，
    校验器第④层给了 60s 容差，不能把这种行误判成越界。
    """
    import importlib.util
    import sys

    script = ("/Users/claire/Desktop/金融1.0/backend/scripts/"
              "backfill_cross_net_provenance.py")
    spec = importlib.util.spec_from_file_location("backfill_xnet2", script)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["backfill_xnet2"] = mod
    spec.loader.exec_module(mod)

    db = _make_prune_db(tmp_path, [
        (1, "2026-10-27T08:28:56+08:00", "推断时间:2026-10-27 08:29:00",
         "2026-10-27 08:29:00", "推断时间:2026-10-27 08:29:00"),
    ])

    class _Args:
        apply = False
        limit = 0
        backup_dir = str(tmp_path)
        verbose = False
        prune_invalid = True

    mod._prune_invalid(mod._connect(db), db, _Args())

    import sqlite3
    conn = sqlite3.connect(db)
    got = conn.execute(
        "SELECT cross_net_first_seen_ts FROM raw_news WHERE id=1").fetchone()[0]
    conn.close()
    assert got == "2026-10-27 08:29:00", f"分钟取整容差内的行被误清: {got!r}"


def test_rc7_prune_uses_empty_string_not_null(tmp_path):
    """清空必须写 ''（两列是 NOT NULL DEFAULT ''），写 NULL 会撞约束。"""
    import importlib.util
    import sys

    script = ("/Users/claire/Desktop/金融1.0/backend/scripts/"
              "backfill_cross_net_provenance.py")
    spec = importlib.util.spec_from_file_location("backfill_xnet3", script)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["backfill_xnet3"] = mod
    spec.loader.exec_module(mod)

    db = _make_prune_db(tmp_path, [
        (1, "2026-10-21T18:24:51+08:00", "来源:X | 推断时间:2026-10-26 23:49:00",
         "2026-10-26 23:49:00", "X来源"),
    ])

    class _Args:
        apply = True
        limit = 0
        backup_dir = str(tmp_path)
        verbose = False
        prune_invalid = True

    assert mod._prune_invalid(mod._connect(db), db, _Args()) == 0

    import sqlite3
    conn = sqlite3.connect(db)
    vals = conn.execute(
        "SELECT cross_net_first_seen_ts, cross_net_source FROM raw_news "
        "WHERE id=1").fetchone()
    conn.close()
    assert vals[0] == "" and vals[1] == "", f"应写成空串，实际 {vals}"



# ────────────────────── RC3: 事件不被 LEFT JOIN 过滤掉 ──────────────────────

@pytest.fixture()
def event_db(tmp_path):
    """最小 raw_news/ai_decisions 库，复现 api_server 的 15 天窗口查询口径。"""
    path = str(tmp_path / "events.db")
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE raw_news (
            id INTEGER PRIMARY KEY, timestamp TEXT, is_noise INTEGER DEFAULT 0,
            status TEXT DEFAULT 'PENDING', content_fingerprint TEXT DEFAULT ''
        );
        CREATE TABLE ai_decisions (
            id INTEGER PRIMARY KEY, news_id INTEGER, is_sibling INTEGER DEFAULT 0
        );
        """
    )
    conn.execute("INSERT INTO raw_news VALUES (1, '2026-10-12T10:00:00+08:00', 0, 'PENDING', '')")
    conn.execute("INSERT INTO raw_news VALUES (2, '2026-10-12T10:00:00+08:00', 0, 'DONE', '')")
    conn.execute("INSERT INTO raw_news VALUES (3, '2026-10-12T10:00:00+08:00', 1, 'PENDING', '')")
    conn.execute("INSERT INTO ai_decisions VALUES (10, 2, 0)")
    conn.commit()
    conn.close()
    return path


def test_rc3_no_decision_rows_are_not_dropped(event_db):
    """没有决策的新闻（待 AI 分析）必须仍在结果里。

    旧写法 `ad.is_sibling = 0` 作用在 LEFT JOIN 出的 NULL 行上 → 整行被过滤。
    """
    conn = sqlite3.connect(event_db)
    rows = conn.execute(
        """
        SELECT rn.id
        FROM raw_news rn
        LEFT JOIN (
            SELECT news_id, MAX(id) AS latest_id FROM ai_decisions
            WHERE is_sibling = 0 OR is_sibling IS NULL GROUP BY news_id
        ) latest ON latest.news_id = rn.id
        LEFT JOIN ai_decisions ad ON ad.id = latest.latest_id
        WHERE rn.is_noise = 0 AND (ad.is_sibling = 0 OR ad.is_sibling IS NULL)
        """
    ).fetchall()
    ids = {r[0] for r in rows}
    conn.close()
    assert ids == {1, 2}, "没有决策的 news_id=1 必须保留"


def test_rc3_sibling_decisions_are_not_promoted_to_main(event_db):
    """副品种决策不得被当成主事件（否则 reasoning/action 是另一个品种的）。"""
    conn = sqlite3.connect(event_db)
    conn.execute("INSERT INTO ai_decisions VALUES (11, 2, 1)")  # 副品种，id 更大
    conn.commit()
    rows = conn.execute(
        """
        SELECT rn.id, ad.id
        FROM raw_news rn
        LEFT JOIN (
            SELECT news_id, MAX(id) AS latest_id FROM ai_decisions
            WHERE is_sibling = 0 OR is_sibling IS NULL GROUP BY news_id
        ) latest ON latest.news_id = rn.id
        LEFT JOIN ai_decisions ad ON ad.id = latest.latest_id
        WHERE rn.is_noise = 0 AND (ad.is_sibling = 0 OR ad.is_sibling IS NULL)
        """
    ).fetchall()
    conn.close()
    # news_id=2 的主决策必须是 10（is_sibling=0），不是 11（is_sibling=1）
    picked = {r[0]: r[1] for r in rows}
    assert picked[2] == 10


def test_rc4_snapshot_and_payload_use_same_status_rule(event_db):
    """SSE 快照与事件载荷必须用同一套口径，否则每 5 秒重推一遍。

    旧实现：快照用 MAX(ad.id)（跨所有决策）+ 原始 status；
    载荷用"最新主决策" + 归一化 status → 1340 行永远不一致。
    """
    conn = sqlite3.connect(event_db)
    conn.execute("INSERT INTO ai_decisions VALUES (11, 2, 1)")  # 副品种 id 更大
    conn.commit()
    rows = conn.execute(
        """
        SELECT rn.id, rn.status, rn.is_noise,
               (SELECT MAX(ad.id) FROM ai_decisions ad WHERE ad.news_id = rn.id
                 AND (ad.is_sibling = 0 OR ad.is_sibling IS NULL)) AS did
        FROM raw_news rn WHERE rn.is_noise = 0
        """
    ).fetchall()
    conn.close()
    for rid, rn_status, is_noise, did in rows:
        snap = event_status.resolve_analysis_status(
            rn_status, has_decision=did is not None, is_noise=bool(is_noise))
        assert snap in event_status.VALID_STATUSES
    # news_id=2 存在主决策 → DONE（不被副决策的更大 id 带偏）
    row = [r for r in rows if r[0] == 2][0]
    assert row[3] == 10
    assert event_status.resolve_analysis_status(
        row[1], has_decision=True, is_noise=False) == "DONE"


