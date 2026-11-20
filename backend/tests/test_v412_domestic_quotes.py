"""v412：国内行情源解析与"说明跟来源走"的离线单测（不联网）。

为什么要单测解析：这些字段位置是从真实报文里定的（新浪/腾讯外盘 7 号位是昨收、
美股 3/4 号位是现价/昨收），写错一位就会把"跌 4%"算成"涨 4%"，
而这类错误在快照里看不出来，只会悄悄污染决策。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src_python"))

from feeds import domestic_quotes as dq


SINA_HF_CL = ('var hq_str_hf_CL="85.28,-0.51,85.29,85.31,85.59,85.06,07:34:59,'
              '85.72,85.16,0,2,2,2026-11-17,纽约原油";')
SINA_HF_GC = ('var hq_str_hf_GC="3855.60,0.07,3855.50,3856.00,3862.60,3854.40,07:36:02,'
              '3853.00,3859.50,0,2,3,2026-11-17,纽约黄金";')
TX_US_NVDA = ('v_usNVDA="200~英伟达~NVDA.OQ~156.14~162.85~159.02~331761847~0~0~0~0~0~0~'
              '0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~~2026-11-16 16:00:01~-6.71~-4.12~163.20";')
TX_KR_660 = 'v_kr000660="352~SK hynix Inc.~000660.KS~1120000~1216000~1216000~4809580~~~";'


def test_sina_hf_parse_price_and_change():
    d = dq._parse_sina_hf(SINA_HF_CL)
    assert d is not None
    assert abs(d["price"] - 85.28) < 1e-6
    # 昨收在 7 号位（85.72）→ 跌幅应为 -0.51% 量级，不能跟 8 号位（开盘 85.16）算成本
    assert d["prev_close"] == 85.72
    assert abs(d["change_pct"] - (-0.51)) < 0.05


def test_tencent_us_stock_parse():
    d = dq._parse_tencent(TX_US_NVDA)
    assert d is not None
    assert abs(d["price"] - 156.14) < 1e-6
    assert d["prev_close"] == 162.85
    assert abs(d["change_pct"] - (-4.12)) < 0.05


def test_tencent_hf_line_uses_sina_parser():
    """腾讯 hf_ 行是逗号格式（和美股波浪号格式不同），必须走新浪解析器。"""
    d = dq._parse_sina_hf(SINA_HF_GC.replace("hq_str_hf_GC", "v_hf_GC").replace("var ", ""))
    assert d is not None
    assert abs(d["price"] - 3855.60) < 1e-6


def test_note_follows_source_not_asset():
    """同一个资产走不同来源，说明必须不同 —— 防止"新浪备份顶着 Gate 的说法"。"""
    assert dq._note_for("BTC", "gate_BTC_USDT") == "Gate.io 现货成交价"
    assert "新浪" in dq._note_for("BTC", "sina_hf_BTC")
    assert "期货代理" in dq._note_for("XAU", "tencent_hf_GC")
    assert "期货代理" in dq._note_for("XAU", "sina_hf_GC")
    assert "收盘价" in dq._note_for("NVDA", "tencent_usNVDA")


def test_forbidden_etf_never_used_for_spot():
    """腾讯 usBTC 是灰度迷你 ETF，不得当 BTC 现货价。"""
    assert "usBTC" in dq._FORBIDDEN
    assert "BTC" not in dq._TENCENT_CODES           # BTC 只走 Gate / 新浪 hf_BTC
    assert dq._GATE_PAIRS["BTC"] == "BTC_USDT"


def _mk(rows):
    """[ts, open, high, low, close, vol] —— 与 gate_klines 输出同序（项目内部 K 线结构）。"""
    return [list(r) for r in rows]


def test_agg_1h_to_4h_open_high_low_close():
    """4 根 1h → 1 根 4h：开取第一根、收取最后一根、高低取极值、量求和。"""
    t0 = 1794816000            # 4h 对齐（4h 边界）
    rows = _mk([
        # [ts, open, high, low, close, vol]
        [t0,         100.0, 101.0, 99.0,  100.0, 1.0],
        [t0 + 3600,  101.0, 103.0, 101.0, 102.0, 2.0],
        [t0 + 7200,  102.0, 105.0, 98.0,  101.0, 3.0],
        [t0 + 10800, 101.0, 104.0, 100.0, 104.0, 4.0],
    ])
    out = dq._agg(rows, 14400, 42)
    assert len(out) == 1
    ts, o, h, l, c, v = out[0]
    assert ts == t0
    assert o == 100.0 and c == 104.0          # 开=首根开，收=末根收
    assert h == 105.0 and l == 98.0           # 高低取极值，不是首末
    assert v == 10.0                          # 量求和


def test_agg_respects_limit_and_order():
    """聚合结果按时间升序、只保留最后 limit 根（超限不能把老的当近的）。"""
    rows = _mk([[1794816000 + i * 3600, 100.0 + i, 101.0 + i, 99.0 + i, 100.0 + i, 1.0]
                for i in range(12)])
    out = dq._agg(rows, 14400, 2)
    assert len(out) == 2
    assert out[0][0] < out[1][0]
    assert out[-1][4] == 111.0                # 末根收盘 = 最新


SINA_DAILY_GC = ("/*<script>location.href='//sina.com';</script>*/\n"
                 'var _GC([{"date":"2026-11-13","open":"3881.700","high":"3927.200",'
                 '"low":"3871.100","close":"3887.600","volume":"0"},'
                 '{"date":"2026-11-16","open":"3887.600","high":"3904.300",'
                 '"low":"3835.200","close":"3858.900","volume":"0"},'
                 '{"date":"2026-11-17","open":"3859.500","high":"3867.300",'
                 '"low":"3854.200","close":"3857.800","volume":"0"}]);')


def test_sina_daily_klines_parse(monkeypatch):
    """新浪外盘日线：JSONP 要剥壳，字段是 date/open/high/low/close（不是 7 号位那套）。"""
    monkeypatch.setattr(dq, "_http_get", lambda *a, **k: SINA_DAILY_GC)
    rows = dq.sina_daily_klines("GC", bars=12)
    assert len(rows) == 3
    # [ts, o, h, l, c, v]，升序，末根=当天
    assert rows[-1][1] == 3859.5 and rows[-1][2] == 3867.3
    assert rows[-1][3] == 3854.2 and rows[-1][4] == 3857.8
    assert rows[0][0] < rows[-1][0]
    # 空串/盗链页不能当数据
    monkeypatch.setattr(dq, "_http_get", lambda *a, **k: "<html>blocked</html>")
    try:
        dq.sina_daily_klines("GC")
    except ValueError:
        pass
    else:
        raise AssertionError("结构不符时应抛 ValueError，不能返回空当成功")


def test_sina_daily_only_feeds_1d(monkeypatch):
    """商品只有新浪日线可用时，只补 1d，绝不拿日线硬凑小时线。"""
    # JSONP 变量名跟品种走（GC/CL），所以按 URL 里的 sym 换壳，否则 WTI 会解析失败
    monkeypatch.setattr(
        dq, "_http_get",
        lambda url, *a, **k: SINA_DAILY_GC.replace("_GC(", "_CL(")
        if url.rstrip("/").endswith("symbol=CL") else SINA_DAILY_GC)
    monkeypatch.setattr(dq, "gate_klines", lambda *a, **k: [])
    monkeypatch.setattr(dq, "em_klines", lambda *a, **k: [])
    dq._SRC_DOWN_UNTIL.clear()   # 熔断状态会跨用例残留，必须清
    out = dq.fetch_mtf(["XAU", "WTI"])
    assert set(out) == {"XAU", "WTI"}
    for asset in out:
        assert out[asset]["_source_1d"] == "sina_daily_k"
        # 没拿到 1h/4h 就不允许预设 intraday 来源（否则上层会把"没补上"读成"已补"）
        assert "_source_intraday" not in out[asset]
        assert set(out[asset]) == {"1d", "_source_1d"}


EM_GC_JSON = ('{"rc":0,"data":{"code":"GC00Y","name":"COMEX黄金","klines":['
              '"2026-11-17 08:00,3862.9,3866.1,3869.8,3855.8,3589",'
              '"2026-11-17 09:00,3866.1,3854.5,3867.8,3854.0,4494"]}}')


def test_em_klines_column_order(monkeypatch):
    """东财 fields2 是 f52=开 f53=**收** f54=高 f55=低——"收"在高前面。

    照抄 Gate 的顺序（开高低收）会把最高价和收盘价写反，趋势和 ATR 全错，
    而快照里看不出异常。这例就是钉死这个顺序。
    """
    class _Resp:
        status_code = 200
        text = EM_GC_JSON

    class _Sess:
        trust_env = False
        def get(self, *a, **k):
            return _Resp()
        def close(self):
            pass

    monkeypatch.setattr(dq, "requests", None, raising=False)
    import requests as real_rq
    monkeypatch.setattr(real_rq, "Session", lambda: _Sess())
    rows = dq.em_klines("101.GC00Y", 60, 200, retries=1)
    assert len(rows) == 2
    # [ts, o, h, l, c, v]
    assert rows[0][1] == 3862.9          # open
    assert rows[0][2] == 3869.8          # high
    assert rows[0][3] == 3855.8          # low
    assert rows[0][4] == 3866.1          # close
    assert rows[1][0] > rows[0][0]       # 升序


def test_fetch_mtf_commodity_three_sources(monkeypatch):
    """商品：1d 来自新浪日线、4h/1h 来自东财——两段来源不同，不能混成同一个 source。"""
    monkeypatch.setattr(dq, "_http_get", lambda url, *a, **k: SINA_DAILY_GC)
    emh = []
    for i in range(80):
        emh.append([1600000000 + i * 3600, 3800.0 + i, 3810.0 + i, 3790.0 + i, 3805.0 + i, 100.0])
    monkeypatch.setattr(dq, "em_klines", lambda *a, **k: emh)
    monkeypatch.setattr(dq, "gate_klines", lambda *a, **k: [])
    dq._SRC_DOWN_UNTIL.clear()   # 前面用例可能把 mtf_em:XAU 熔断掉
    out = dq.fetch_mtf(["XAU"], workers=2)
    per = out["XAU"]
    assert per["_source_1d"] == "sina_daily_k"
    assert per["_source_intraday"] == "em_klines"
    assert len(per["1d"]) == 3 and len(per["1h"]) == 48
    assert len(per["4h"]) >= 2
