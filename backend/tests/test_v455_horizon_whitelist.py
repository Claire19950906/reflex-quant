# -*- coding: utf-8 -*-
"""v453：白名单按周期分档 + 反向黑名单 + 影子账本按各自周期结算。

背景：旧的 `tech_actionable.json` 只有一套 5 日口径的扁平 bull/bear。台账里
bb_upper_break 3 日超额 +1.2 判「有效」、5 日只剩噪声 —— 一套通吃等于把它的
3 日结论扔掉，同时也让 5 日噪声搭上顺风车。本测试锁四件事：
  1. `resolve_horizon` 从短到长取第一个放行周期；
  2. 黑名单因子整批出现时不出方向，混在有效因子里只作背景；
  3. 扁平旧配置仍可被识别（不因为缺 by_horizon 就全盘失效）；
  4. 影子账本按每行记账时的 horizon 结算，而不是统一 5 日。
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src_python"))

import cron_tech_shadow as cts
import tech_signal_bridge as tsb


ALLOW = {
    "by_horizon": {
        "1": {"bull": [], "bear": []},
        "3": {"bull": ["bb_lower_break", "bb_upper_break"], "bear": ["bb_upper_break"]},
        "5": {"bull": ["rsi_oversold"], "bear": ["kdj_overbought"]},
        "10": {"bull": [], "bear": []},
    },
    "blacklist": ["vol_bear_break"],
}


def test_resolve_horizon_takes_shortest_allowed():
    items = [{"k": "bb_upper_break", "w": 1.0}]
    assert tsb.resolve_horizon(items, ALLOW) == 3


def test_resolve_horizon_none_when_nothing_allowed():
    assert tsb.resolve_horizon([{"k": "div_bottom", "w": 1.0}], ALLOW) is None


def test_blacklist_blocks_alone_but_not_mixed():
    only_black = [{"k": "vol_bear_break", "w": 1.2}]
    assert tsb.resolve_horizon(only_black, ALLOW) is None

    mixed = [{"k": "vol_bear_break", "w": 1.2}, {"k": "rsi_oversold", "w": 1.0}]
    assert tsb.resolve_horizon(mixed, ALLOW) == 5


def test_blacklist_is_background_only_in_decide():
    action, _why, _st = tsb.decide_direction(
        [{"k": "vol_bear_break", "w": 1.2}], -2.0, None, ALLOW)
    assert action == "HOLD"


def test_flat_config_still_works():
    flat = {"bull": ["rsi_oversold"], "bear": []}
    assert tsb.resolve_horizon([{"k": "rsi_oversold", "w": 1.0}], flat) == 1


def _settle_db() -> tuple[sqlite3.Connection, int]:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE ohlcv_daily (asset TEXT, ts INTEGER, close REAL)")
    t0 = 1_700_000_000
    for asset in ("BTC", "ETH"):
        for i, close in enumerate([100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 106.0]):
            conn.execute("INSERT INTO ohlcv_daily VALUES (?,?,?)",
                         (asset, t0 + i * 86400, close))
    cts._ensure_schema(conn)
    return conn, t0


def test_settle_uses_each_row_horizon():
    conn, t0 = _settle_db()
    conn.execute(
        "INSERT INTO tech_shadow_signals (asset, ts, action, horizon, close, settled)"
        " VALUES ('BTC', ?, 'BUY', 1, 100.0, 0)", (t0,))
    conn.execute(
        "INSERT INTO tech_shadow_signals (asset, ts, action, horizon, close, settled)"
        " VALUES ('ETH', ?, 'BUY', 5, 100.0, 0)", (t0,))
    conn.commit()

    assert cts.settle(conn) == 2
    rows = dict(conn.execute(
        "SELECT asset, fwd_ret_5d FROM tech_shadow_signals").fetchall())
    assert rows["BTC"] == 1.0      # 1 根后 101 vs 100
    assert rows["ETH"] == 5.0      # 5 根后 105 vs 100
