"""v400：影子账本日更 —— 清污染、按真实天数算样本、把闸门扫描翻成结论。

背景：v335 的影子账本只有 35 行，其中 25 行是降级路径拼出来的方向（账本先于
v400 的 analysis_degraded 过滤落库），首页面板拿它显示"命中率 30%"等于用模板
给判断力打分。本测试锁住三件事：
  1. prune_invalid 只删语义无效的行，不误伤真样本；
  2. samples_per_day 按"真有样本的天数"算，不按查询窗口算；
  3. sweep_verdict 的四种结论（样本不足 / 有效 / 反向 / 筛了个寂寞）。
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src_python"))

import shadow_ledger as sl


def _db(tmp_path) -> sqlite3.Connection:
    conn = sqlite3.connect(tmp_path / "t.db")
    conn.execute("""CREATE TABLE ai_decisions (
        id INTEGER, ts INTEGER, target_asset TEXT, ungated_action TEXT,
        suggested_action TEXT, evidence_confidence REAL,
        analysis_degraded INTEGER DEFAULT 0)""")
    return conn


def _add(conn, did: int, asset: str = "BTC", action: str = "BUY",
         degraded: int = 0, conf: float = 0.0, ts: int = 1_700_000_000) -> None:
    conn.execute("INSERT INTO ai_decisions VALUES (?,?,?,?,?,?,?)",
                 (did, ts, asset, action, "HOLD", conf, degraded))


def _settled(did: int, hit: int, conf: float, day_ts: int,
             direction: str = "BUY") -> dict:
    return {"decision_id": did, "decided_ts": day_ts, "asset": "BTC",
            "direction": direction, "action_after": "HOLD", "confidence": conf,
            "move_pct": 1.0 if hit else -1.0, "hit": hit}


# ── 清理 ──────────────────────────────────────────────────────────────

def test_prune_removes_degraded_but_keeps_real_samples(tmp_path):
    conn = _db(tmp_path)
    _add(conn, 1, degraded=0)
    _add(conn, 2, degraded=1)              # 降级方向：不该记账
    _add(conn, 3, asset="SKHYNIX")         # 池外品种
    _add(conn, 4, action="HOLD")           # 本来就没方向
    sl.ensure_table(conn)
    for did in (1, 2, 3, 4):
        conn.execute("INSERT INTO shadow_ledger (decision_id, decided_ts, asset, "
                     "direction, confidence, hit) VALUES (?,?,?,?,?,?)",
                     (did, 1_700_000_000, "BTC", "BUY", 10.0, 0))
    conn.commit()

    assert sl.prune_invalid(conn) == 3
    left = conn.execute("SELECT decision_id FROM shadow_ledger").fetchall()
    assert left == [(1,)]


def test_prune_drops_rows_whose_decision_is_gone(tmp_path):
    """决策被删了（库重置/回滚）也不能留在账本里假装有样本。"""
    conn = _db(tmp_path)
    sl.ensure_table(conn)
    conn.execute("INSERT INTO shadow_ledger (decision_id, decided_ts, asset, "
                 "direction, confidence, hit) VALUES (99,?,?,?,?,?)",
                 (1_700_000_000, "BTC", "BUY", 10.0, 1))
    conn.commit()
    assert sl.prune_invalid(conn) == 1
    assert conn.execute("SELECT COUNT(*) FROM shadow_ledger").fetchone()[0] == 0


def test_prune_is_idempotent(tmp_path):
    conn = _db(tmp_path)
    _add(conn, 1)
    sl.ensure_table(conn)
    conn.execute("INSERT INTO shadow_ledger (decision_id, decided_ts, asset, "
                 "direction, confidence, hit) VALUES (1,?,?,?,?,?)",
                 (1_700_000_000, "BTC", "BUY", 10.0, 1))
    conn.commit()
    assert sl.prune_invalid(conn) == 0


# ── 汇总口径 ──────────────────────────────────────────────────────────

def test_samples_per_day_uses_covered_days_not_window():
    """40 天窗口只攒了 2 天样本时，应该是 5 条/天，而不是 10/40=0.25。"""
    day1, day2 = 1_700_000_000, 1_700_000_000 + 86400
    items = [_settled(i, i % 2, 10.0, day1) for i in range(1, 6)]
    items += [_settled(10 + i, i % 2, 10.0, day2) for i in range(1, 6)]
    snap = sl.summarize(items, days=40, horizon=240)
    assert snap["settled"] == 10 and snap["days_covered"] == 2
    assert snap["samples_per_day"] == 5.0
    assert snap["remaining_to_target"] == sl.TARGET_SAMPLES - 10


def test_summarize_splits_by_asset_and_direction():
    ts = 1_700_000_000
    items = [_settled(1, 1, 10.0, ts, "BUY"), _settled(2, 0, 10.0, ts, "SELL")]
    snap = sl.summarize(items, days=30, horizon=240)
    assert {d["direction"] for d in snap["by_direction"]} == {"BUY", "SELL"}
    assert snap["by_asset"][0]["asset"] == "BTC"
    assert snap["hit_rate"] == 50.0                       # 1 中 1 不中


# ── 闸门结论 ──────────────────────────────────────────────────────────

def _sweep(base_n, base_rate, high_n, high_rate):
    return [{"gate": 0.0, "n": base_n, "hit_rate": base_rate},
            {"gate": 5.0, "n": base_n, "hit_rate": base_rate},
            {"gate": 20.0, "n": high_n, "hit_rate": high_rate},
            {"gate": 30.0, "n": 0, "hit_rate": None}]


def test_verdict_insufficient_when_gate20_has_too_few_samples():
    v = sl.sweep_verdict(_sweep(40, 40.0, 1, 100.0))
    assert v["code"] == "insufficient" and "样本不足" in v["text"]


def test_verdict_effective_when_high_group_is_clearly_better():
    v = sl.sweep_verdict(_sweep(40, 40.0, 8, 62.5))
    assert v["code"] == "effective" and v["delta"] == 22.5


def test_verdict_inverted_when_high_group_is_worse():
    v = sl.sweep_verdict(_sweep(40, 60.0, 8, 40.0))
    assert v["code"] == "inverted" and v["delta"] == -20.0


def test_verdict_flat_when_gate_screens_nothing():
    """门槛前后命中率差 3pt（< band）：闸门筛了个寂寞。"""
    v = sl.sweep_verdict(_sweep(40, 40.0, 8, 43.0))
    assert v["code"] == "flat"


def test_verdict_no_base_without_gate_zero():
    assert sl.sweep_verdict([{"gate": 20.0, "n": 9, "hit_rate": 50.0}])["code"] == "no_base"


def test_target_samples_matches_readme_gate():
    """README 写"≥60 笔已结算才算样本"，这条别被随手改小。"""
    assert sl.TARGET_SAMPLES == 60
