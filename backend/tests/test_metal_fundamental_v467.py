# -*- coding: utf-8 -*-
"""v467 白银 / 铜 非公司基本面分量测试。

这一版接的两条核心是"跨品种相对比价"，符号方向最容易搞反，必须钉住：
  - 金银比（黄金/白银）：比值越高 = 白银相对黄金越便宜 → **对白银偏多**；
  - 铜金比（铜/黄金）：比值越高 = 增长预期越强 → **对铜偏多**。
另外"没接到的库存"必须写成缺口，不能悄悄当 0 分。
"""
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "src_python"))

import alt_fundamental as af


def _synth_db(series):
    """series: {metric_key: [值...]}，按天递增写入 90 天。"""
    conn = sqlite3.connect(":memory:")
    conn.execute("""CREATE TABLE macro_snapshots(
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, category TEXT NOT NULL,
        source TEXT NOT NULL DEFAULT '', metric_key TEXT NOT NULL, value REAL,
        unit TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'ok',
        payload TEXT NOT NULL DEFAULT '{}')""")
    base = datetime(2026, 1, 1)
    rows = []
    for key, vals in series.items():
        for i, v in enumerate(vals):
            ts = int((base + timedelta(days=i)).timestamp())
            rows.append((ts, "test", "synth", key, v, "USD", "ok", "{}"))
    conn.executemany(
        "INSERT INTO macro_snapshots(ts,category,source,metric_key,value,unit,status,payload)"
        " VALUES(?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    return conn


def _synth_market():
    return _synth_db({
        "xau.usd": [3000.0 + i for i in range(90)],
        "xag.usd": [100.0 + i * 0.2 for i in range(90)],
        "xcu.usd": [6.0 + i * 0.01 for i in range(90)],
        "dxy": [100.0 + i * 0.05 for i in range(90)],
    })


def test_silver_and_copper_available_with_two_families():
    """只要"美元 + 比价"两族到位就必须出分（cot/实际利率缺了写缺口，不算不发分）。"""
    conn = _synth_market()
    for symbol in ("XAG", "SILVER", "白银", "CU", "COPPER", "铜"):
        out = af.build(symbol, conn=conn, use_cache=False)
        assert out["available"] is True, symbol
        assert len(out["components"]) >= 2, symbol
        fams = {af._COMP_SOURCE.get(k, k) for k in out["components"]}
        assert len(fams) >= 2, (symbol, fams)


def test_gold_silver_ratio_high_is_bullish_silver():
    """金银比冲到历史最高 → 白银相对黄金最便宜 → ratio 分量必须为正（偏多白银）。"""
    conn = _synth_db({
        "xau.usd": [3000.0] * 90,
        "xag.usd": [100.0 - i for i in range(90)],     # 白银一路跌 → 金银比一路升
        "dxy": [100.0] * 90,                          # 美元不动 → usd 分量为 0
    })
    out = af.build("XAG", conn=conn, use_cache=False)
    ratio = out["components"].get("ratio")
    assert ratio is not None
    assert ratio["score"] > 0.8, ratio       # 分位≈100% → (1-0.5)*2 = +1
    detail = " ".join(out["evidence"])
    assert "金银比" in detail


def test_copper_gold_ratio_low_is_bearish_copper():
    """铜金比跌到历史最低 → 增长预期最弱 → ratio 分量必须为负（偏空铜）。"""
    conn = _synth_db({
        "xau.usd": [3000.0] * 90,
        "xcu.usd": [10.0 - i * 0.05 for i in range(90)],
        "dxy": [100.0] * 90,
    })
    out = af.build("CU", conn=conn, use_cache=False)
    ratio = out["components"].get("ratio")
    assert ratio is not None
    assert ratio["score"] < -0.8, ratio


def test_metal_missing_inventory_is_a_gap_not_zero():
    """库存/需求这类没接到的数据必须出现在 gaps 里，不能被当成中性 0 分。"""
    silver = af.build("XAG", use_cache=False)
    copper = af.build("CU", use_cache=False)
    assert any("库存" in g for g in silver["gaps"]), silver["gaps"]
    assert any("库存" in g for g in copper["gaps"]), copper["gaps"]
    assert any("需求" in g for g in copper["gaps"]), copper["gaps"]


def test_ratio_series_needs_both_legs():
    """缺一条腿就当天不算 —— 不能拿单边价格假装成比价。"""
    conn = _synth_db({"xau.usd": [3000.0] * 90,
                      "xag.usd": [100.0] * 40})       # 只有 40 天白银
    pairs = af._ratio_series(conn, "xau.usd", "xag.usd")
    assert len(pairs) == 40, len(pairs)


def test_metal_alias_routing():
    for alias, code in (("SILVER", "XAG"), ("白银", "XAG"), ("XAGUSD", "XAG"),
                        ("COPPER", "CU"), ("铜", "CU"), ("HG", "CU")):
        assert af.canonical(alias) == code, alias


def test_unsupported_metal_stays_unsupported():
    """铂金没接（没价格也没需求分项）就必须明说不支持。"""
    out = af.build("PLATINUM", use_cache=False)
    assert out["available"] is False
    assert out["reason"] == "unsupported"
    assert "N/A" in out["detail"] or "不支持" in out["detail"]


# ── v467 补：沪深300 与 TIC ────────────────────────────────────────────────────
def _synth_cn(pe_vals, close_vals):
    return _synth_db({"csi300.pe": pe_vals, "csi300.close": close_vals})


def test_csi300_available_with_two_families():
    """沪深300：官方 PE + 指数盈利(EPS) 两族到位就必须出分。"""
    pe = [13.0 + (i % 7) * 0.05 for i in range(400)]
    close = [3900.0 + i * 3 for i in range(400)]
    out = af.build("CSI300", conn=_synth_cn(pe, close), use_cache=False)
    assert out["available"] is True
    assert set(out["components"]) == {"pe", "eps"}
    fams = {af._COMP_SOURCE.get(k, k) for k in out["components"]}
    assert len(fams) >= 2, fams


def test_csi300_expensive_pe_is_bearish():
    """PE 冲到样本最高 = 贵 = 负分（估值反向）。"""
    pe = [10.0 + i * 0.05 for i in range(400)]      # 一路抬升，最新在最高位
    close = [3000.0] * 400
    out = af.build("CSI300", conn=_synth_cn(pe, close), use_cache=False)
    assert out["components"]["pe"]["score"] < -0.8, out["components"]["pe"]


def test_csi300_eps_improving_is_bullish():
    """点位不动、PE 一路降 → EPS 在改善 → 盈利分量必须为正。"""
    pe = [20.0 - i * 0.02 for i in range(400)]      # PE 下降 → EPS=点位/PE 上升
    close = [4000.0] * 400
    out = af.build("CSI300", conn=_synth_cn(pe, close), use_cache=False)
    assert out["components"]["eps"]["score"] > 0.8, out["components"]["eps"]


def test_csi300_alias_routing():
    for alias in ("000300.SS", "000300.SH", "HS300", "沪深300", "CSI300"):
        assert af.canonical(alias) == "CSI300", alias


def _tic_db(months, start_year=2010):
    rows = []
    base = datetime(start_year, 1, 15)
    for i, v in enumerate(months):
        y, m = divmod(base.month - 1 + i, 12)
        rows.append((int(datetime(base.year + y, m + 1, 15).timestamp()),
                     "tic", "synth", "ust.tic_foreign_holdings", v, "USD bn", "ok", "{}"))
    conn = sqlite3.connect(":memory:")
    conn.execute("""CREATE TABLE macro_snapshots(
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, category TEXT NOT NULL,
        source TEXT NOT NULL DEFAULT '', metric_key TEXT NOT NULL, value REAL,
        unit TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'ok',
        payload TEXT NOT NULL DEFAULT '{}')""")
    conn.executemany(
        "INSERT INTO macro_snapshots(ts,category,source,metric_key,value,unit,status,payload)"
        " VALUES(?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    return conn


def test_tic_stale_never_scores():
    """TIC 太旧（一年前）就不许发分 —— 缺口写清楚，不能拿旧数据充当下。"""
    conn = _tic_db([5000.0 + i * 10 for i in range(120)])   # 2010→2019 结束，早已过期
    gaps = []
    comp = af._tic_score(conn, gaps, 0.06)
    assert comp is None                  # 不发分 = 不进加权，而不是发一个 0 分充数
    assert any("滞后" in g for g in gaps), gaps


def test_tic_fresh_increasing_is_bullish():
    """数据够新 + 外资持续增持 → 正分（买盘在）。"""
    from datetime import datetime as _dt
    now = _dt.now()
    months = []
    total = 4000.0
    for i in range(48):
        y, m = divmod(now.month - 1 - (47 - i), 12)
        months.append((_dt(now.year + y, m + 1, 15), total))
        total += 20.0
    conn = sqlite3.connect(":memory:")
    conn.execute("""CREATE TABLE macro_snapshots(
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, category TEXT NOT NULL,
        source TEXT NOT NULL DEFAULT '', metric_key TEXT NOT NULL, value REAL,
        unit TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'ok',
        payload TEXT NOT NULL DEFAULT '{}')""")
    conn.executemany(
        "INSERT INTO macro_snapshots(ts,category,source,metric_key,value,unit,status,payload)"
        " VALUES(?,?,?,?,?,?,?,?)",
        [(int(d.timestamp()), "tic", "synth", "ust.tic_foreign_holdings", v, "USD bn", "ok", "{}")
         for d, v in months])
    conn.commit()
    gaps = []
    comp = af._tic_score(conn, gaps, 0.06)
    assert comp is not None
    assert comp["score"] is not None
    assert "12 个月" in comp["detail"], comp
