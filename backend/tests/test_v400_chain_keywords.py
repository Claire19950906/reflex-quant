#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_v400_chain_keywords.py — 因果链触发词净化 + 品种绑定的回归（全离线）

背景（2026-11-16 实测，752 条链）
  · 722/751（96%）的 trigger_words 混进了 LLM 输出 JSON 的键名/枚举
    （verdict / prompt / Asset / BUY / SELL / ai_worker / custom checklist …），
    任何一段文本都能命中任意链。
  · 旧品种规则 = "词表里第一个有行情的 ticker" → 一条讲 XAU 的链只要词表里
    WTI 排在前面就被绑成 WTI；319/752（42%）落到 UNKNOWN，匹配恒为 0。

本测试锁住三件事：
  1. 净化只删不造，且删得分类正确（system / ticker / 超长 / 重复 / 截断）；
  2. 品种判定按语义权重（effect > name > cause > trigger_words），判不准留 UNKNOWN；
  3. 入库→派生这条真实路径（derive_strategy_columns）写出的 asset/keywords 与
     纯函数同口径 —— 否则"修了函数没修入库"是最常见的假修复。
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_PY = os.path.join(SRC, "src_python")
if SRC_PY not in sys.path:
    sys.path.insert(0, SRC_PY)

import chain_keywords as ck


# ---------------------------------------------------------------------------
# 1. 触发词净化
# ---------------------------------------------------------------------------

def test_clean_removes_system_tokens():
    raw = ("爆炸,中东冲突,加息,verdict,prompt,ETH,BUY,XAU,SELL,"
           "ai_worker,checklist,market_event,partial,胡塞武装,输油管道")
    res = ck.clean_keywords(raw)
    kept = set(res.kept)
    assert "verdict" not in kept and "prompt" not in kept
    assert "ai_worker" not in kept and "checklist" not in kept
    assert "market_event" not in kept and "partial" not in kept
    assert "BUY" not in kept and "SELL" not in kept
    # 真正的英文事件词要保留（不能一刀切删英文）
    assert {"爆炸", "中东冲突", "加息", "胡塞武装", "输油管道"} <= kept
    assert res.removed


def test_clean_drops_tickers_and_dedupes():
    res = ck.clean_keywords("XAU,黄金,WTI,原油,黄金,沙特,沙特")
    assert res.kept == ("沙特",), res.kept


def test_clean_truncates_over_limit():
    tokens = [f"事件词{i}" for i in range(20)]
    res = ck.clean_keywords(",".join(tokens), max_keywords=12)
    assert len(res.kept) == 12
    # 截断部分必须记进 removed，否则"污染率"会被算低
    assert len(res.removed) == 8


def test_tickers_do_not_consume_keyword_cap():
    """品种代码不占名额 —— 否则真事件词会被挤掉（实测链 #1 丢了 SEC/ETP）。"""
    raw = "爆炸,中东冲突,通胀,加息,战争,停火谈判,霍尔木兹,袭击,地缘冲突,ETH,XAU,加密资产,SEC,ETP"
    res = ck.clean_keywords(raw, drop_tickers=False)
    assert "ETH" in res.kept and "XAU" in res.kept
    assert "SEC" in res.kept and "ETP" in res.kept, res.kept
    assert len([k for k in res.kept if not ck._is_ticker_token(k)]) == 12


def test_suspect_flag_reflects_pollution():
    polluted = ("verdict,prompt,Asset,BUY,SELL,ai_worker,checklist,partial,"
                "market_event,geopolitical_conflict,加息,沙特")
    assert ck.clean_keywords(polluted).suspect is True

    clean = "加息,沙特,胡塞武装,输油管道,库存"
    assert ck.clean_keywords(clean).suspect is False


def test_clean_keeps_json_fragment_out():
    res = ck.clean_keywords('{"verdict": "BUY"},加息,  ,—')
    assert res.kept == ("加息",), res.kept


# ---------------------------------------------------------------------------
# 2. 品种绑定
# ---------------------------------------------------------------------------

def test_effect_and_name_beat_trigger_order():
    """讲 XAU 的链，词表里 WTI 排第一，也必须绑 XAU。"""
    d = ck.decide_asset(
        name="美俄乌克兰谈判涉及石油交易→黄金与风险资产反应",
        cause="地缘政治与能源政策双重事件",
        effect="黄金 XAU 短周期双向小幅震荡；ETH 同期仍收跌",
        trigger_words="WTI,XAU,BUY,SELL,verdict,加密资产",
        priced_assets={"XAU", "WTI", "ETH"},
    )
    assert d.asset == "XAU", d.as_dict()
    # XAU: name 2 + effect 3 + trigger 1 = 6；ETH: effect 3 + trigger 1 = 4
    assert d.strength == "name" or d.strength == "effect"


def test_name_wins_when_only_name_mentions():
    d = ck.decide_asset(
        name="中东地缘冲突→原油做空",
        cause="霍尔木兹海峡局势升级",
        effect="价格方向见 name",
        trigger_words="袭击,油轮,BUY,SELL",
        priced_assets={"WTI", "XAU"},
    )
    assert d.asset == "WTI", d.as_dict()


def test_trigger_only_tie_falls_back_to_unknown():
    """只在词表里出现、且两个品种并列 → 不硬猜（旧实现会绑第一个）。"""
    d = ck.decide_asset(
        name="某事件冲击风险资产",
        cause="无明确标的",
        effect="方向不明",
        trigger_words="XAU,WTI",
        priced_assets={"XAU", "WTI"},
    )
    assert d.asset == "UNKNOWN", d.as_dict()
    assert d.strength == "ambiguous"


def test_priced_filter_removes_unpriceable_asset():
    d = ck.decide_asset(
        name="黄金创新高",
        effect="XAU 上涨",
        trigger_words="加息",
        priced_assets={"WTI"},   # 黄金没有行情 → 不能被选
    )
    assert d.asset == "UNKNOWN", d.as_dict()


def test_no_asset_at_all_is_unknown():
    d = ck.decide_asset(name="供应宽松", cause="库存增加", effect="价格回落")
    assert d.asset == "UNKNOWN" and d.strength == "none"


def test_alias_uses_word_boundary():
    """oil 不能命中 boil（旧实现的子串匹配会误命中）。"""
    d = ck.decide_asset(name="boiling water in the reactor", effect="supply rising",
                        priced_assets={"WTI"})
    assert d.asset == "UNKNOWN", d.as_dict()


# ---------------------------------------------------------------------------
# 3. 真实路径：入库派生
# ---------------------------------------------------------------------------

def _mk_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE causal_chains ("
        " id INTEGER PRIMARY KEY, name TEXT, cause TEXT, effect TEXT,"
        " trigger_words TEXT, weight REAL, mechanism TEXT, precondition TEXT,"
        " enabled INTEGER, chain_id TEXT, asset TEXT, trigger_keywords TEXT,"
        " expected_direction TEXT, magnitude_lo REAL, magnitude_hi REAL,"
        " confidence REAL, updated_at TEXT)"
    )
    conn.execute("CREATE TABLE ohlcv_daily (asset TEXT, ts INTEGER, close REAL)")
    conn.commit()
    return conn


def test_derive_strategy_columns_applies_new_rules():
    import causal_chain_evaluator as cce

    conn = _mk_db()
    conn.execute(
        "INSERT INTO causal_chains (id, name, cause, effect, trigger_words, weight,"
        " mechanism, precondition, enabled) VALUES (1, ?, ?, ?, ?, 1.0, '', '', 1)",
        ("美俄乌克蘭谈判→黄金与风险资产反应",
         "地缘政治与能源政策双重事件",
         "黄金 XAU 短周期双向小幅震荡",
         "WTI,XAU,BUY,SELL,verdict,胡塞武装,输油管道"),
    )
    conn.commit()

    cce.derive_strategy_columns(conn, chain_ids=[1])

    row = conn.execute(
        "SELECT asset, trigger_keywords, expected_direction FROM causal_chains WHERE id=1"
    ).fetchone()
    assert row["asset"] == "XAU", dict(row)
    kws = row["trigger_keywords"]
    assert "BUY" not in kws and "SELL" not in kws and "verdict" not in kws
    assert "胡塞武装" in kws
    assert row["expected_direction"] in ("UP", "DOWN", "BOTH")


def test_clean_path_matches_derive_path():
    """同一行数据：纯函数判定 == 派生列写入（防两套口径漂移）。"""
    import causal_chain_evaluator as cce

    name = "胡塞袭击沙特输油泵站→WTI 供应扰动"
    cause = "无人机袭击利雅得输油管道"
    effect = "WTI 短周期上行"
    tw = "袭击,胡塞武装,WTI,BUY,verdict,供应中断"

    d = ck.decide_asset(name, cause, effect, tw, priced_assets={"WTI"})
    conn = _mk_db()
    conn.execute(
        "INSERT INTO causal_chains (id, name, cause, effect, trigger_words, weight,"
        " mechanism, precondition, enabled) VALUES (1, ?, ?, ?, ?, 1.0, '', '', 1)",
        (name, cause, effect, tw),
    )
    conn.commit()
    cce.derive_strategy_columns(conn, chain_ids=[1])
    got = conn.execute("SELECT asset FROM causal_chains WHERE id=1").fetchone()["asset"]
    assert got == d.asset == "WTI"


def test_derive_keeps_llm_direction_overlay(tmp_path, monkeypatch):
    """P0-3 的 LLM 方向标注必须活过 derive_strategy_columns。

    这是最容易静默失效的一处：derive 每次都重算 `_infer_direction(name)`，
    而 `causal_weight_feedback` 每小时对 applied 链跑一次 —— 没有覆盖层，
    标注会在下一个整点被规则法（v322 实测 41.9%，低于随机）悄悄换掉。
    """
    import causal_chain_evaluator as cce

    labels = tmp_path / "llm_labels.json"
    labels.write_text(json.dumps({"labels": {"1": "DOWN"}}), encoding="utf-8")
    monkeypatch.setattr(cce, "LLM_LABELS_PATH", str(labels))
    # 全量跑时库里可能已有 chain_direction_overlay 行（DB 镜像优先于 JSON），
    # 这里要验的是 JSON 标注这条路径本身，所以把镜像置空。
    monkeypatch.setattr(cce, "_overlay_from_db", lambda: {})
    cce._LLM_LABEL_CACHE.update({"mtime": None, "map": {}})

    conn = _mk_db()
    conn.execute(
        "INSERT INTO causal_chains (id, name, cause, effect, trigger_words, weight,"
        " mechanism, precondition, enabled) VALUES (1, ?, ?, ?, ?, 1.0, '', '', 1)",
        ("地缘冲突→黄金避险买盘", "中东冲突升级", "XAU 短周期上行", "胡塞武装,袭击,XAU"),
    )
    conn.commit()
    cce.derive_strategy_columns(conn, chain_ids=[1])
    got = conn.execute(
        "SELECT expected_direction FROM causal_chains WHERE id=1").fetchone()[0]
    assert got == "DOWN", got            # 规则法会判 UP，覆盖层必须赢


def test_add_chain_writes_clean_keywords(tmp_path, monkeypatch):
    """入库路径（add_chain）也必须净化 —— 否则新链继续污染库。"""
    import causal_chains

    db_path = tmp_path / "t.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE causal_chains ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, cause TEXT, effect TEXT,"
        " trigger_words TEXT, note TEXT, mechanism TEXT, precondition TEXT,"
        " invalidation TEXT, enabled INTEGER, origin TEXT, weight REAL,"
        " chain_id TEXT, asset TEXT, trigger_keywords TEXT, expected_direction TEXT,"
        " magnitude_lo REAL, magnitude_hi REAL, confidence REAL, active INTEGER,"
        " created_at TEXT, updated_at TEXT, hit_count INTEGER, miss_count INTEGER,"
        " source TEXT, last_verified_ts TEXT)"
    )
    conn.execute("CREATE TABLE ohlcv_daily (asset TEXT, ts INTEGER, close REAL)")
    conn.commit()

    res = causal_chains.add_chain(
        "地缘升级导致原油供应中断", "WTI 上行", name="霍尔木兹→WTI",
        trigger_words="袭击,霍尔木兹,WTI,BUY,he said,verdict,ai_worker",
        origin="auto_real", conn=conn,
    )
    assert res["ok"] is True

    row = conn.execute("SELECT trigger_words FROM causal_chains WHERE id=?", (res["id"],)).fetchone()
    written = row["trigger_words"]
    for bad in ("BUY", "verdict", "ai_worker"):
        assert bad not in written, written
    # 品种代码**保留**在 trigger_words（判 asset 的弱证据），匹配时才剔除
    assert "WTI" in written, written
    assert "霍尔木兹" in written
    conn.close()
