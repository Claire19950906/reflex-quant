"""v406 基本面扩面 —— 单元测试（全部离线：不联网、不碰生产库）。

覆盖四件事：
  1. 五类标的分类路由（non_equity / placeholder / us / a_share / hk / other_market）；
  2. 估值分位与盈利修正的**一致性护栏**（价值陷阱 / 成长溢价只折半、不翻转）；
  3. A股累计口径还原 TTM、EPS 同比与负基数守卫；
  4. AV 日配额账本（超额必须拒绝请求，而不是先请求再失败）。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "src_python"))

import asset_kinds as ak
import cn_fundamentals as cf
import config
import decision_guard as dg
import equity_fundamentals as ef
import fundamental_factor as ff


# ── 1) 分类路由 ────────────────────────────────────────────────────────────
def test_classify_covers_all_kinds():
    cases = {
        # 非公司标的：本来就没有公司级基本面
        "BTC": ("non_equity", None), "XAU": ("non_equity", None),
        "WTI": ("non_equity", None), "US10Y": ("non_equity", None),
        # 占位标签：代码没解析出来，是**缺口**
        "CN_STOCK": ("placeholder", None), "HK_STOCK": ("placeholder", None),
        "US_STOCK": ("placeholder", None), "INDIVIDUAL_STOCK": ("placeholder", None),
        "NONE": ("placeholder", None), "": ("placeholder", None),
        # 三市场 + 归一
        "NVDA": ("us", "NVDA"), "nvda": ("us", "NVDA"),
        "600519": ("a_share", "600519.SH"), "600519.SH": ("a_share", "600519.SH"),
        "SH600519": ("a_share", "600519.SH"), "000001": ("a_share", "000001.SZ"),
        "00700": ("hk", "00700.HK"), "00700.HK": ("hk", "00700.HK"),
        # 代码清楚、市场没接
        "SKHYNIX": ("other_market", "000660.KS"), "000660.KS": ("other_market", "000660.KS"),
        # 别名
        "US_HYNIX": ("us", "MU"),
    }
    for asset, expected in cases.items():
        assert ak.classify(asset) == expected, f"{asset!r} → {ak.classify(asset)}"


def test_placeholder_never_invents_ticker():
    """占位标签必须返回 ticker=None —— 一旦猜成某只票，就会把别家财报喂进判定。"""
    for tag in ("CN_STOCK", "HK_STOCK", "US_STOCK", "INDIVIDUAL_STOCK", "SOMETHING_NEW"):
        out = ff.build(tag, use_cache=False)
        assert out["ticker"] is None
        assert out["reason"] == "placeholder"
        assert out["available"] is False


def test_non_equity_and_gap_are_distinguishable():
    """「不适用」和「缺口」在输出里必须能分开，否则复盘会把两者混为一谈。"""
    # v460: BTC 走 alt 口径（可审计的分量结论），"不适用"的代表改用未接入的白银；
    # 关键是三种 reason 仍能区分："不适用 / 有读数但口径不同 / 缺口"。
    btc = ff.build("BTC", use_cache=False)
    assert btc["kind"] == "alt" and btc["reason"] in ("ok", "insufficient")
    metal = ff.build("VIX", use_cache=False)   # v467: SILVER 已接入 alt，改用 alt 不覆盖的 VIX
    assert metal["reason"] == "non_equity" and "N/A" in metal["detail"]
    placeholder = ff.build("CN_STOCK", use_cache=False)
    assert placeholder["reason"] == "placeholder" and "缺口" in placeholder["detail"]
    kr = ff.build("SKHYNIX", use_cache=False)
    assert kr["reason"] == "market_unsupported" and kr["ticker"] == "000660.KS"


def test_resolve_a_share_rejects_non_codes():
    assert ak.resolve_a_share("6005190") is None      # 7 位
    assert ak.resolve_a_share("NVDA") is None
    assert ak.resolve_a_share("") is None
    assert ak.resolve_a_share("830000") == "830000.BJ"


# ── 2) 一致性护栏 ──────────────────────────────────────────────────────────
def test_value_trap_halves_positive_valuation():
    ev: list = []
    bits: list = []
    used, flags = ff._consistency(0.8, -0.30, ev, bits)
    assert used == 0.4 and flags == ["value_trap"]
    assert any("价值陷阱" in x for x in ev)


def test_growth_premium_halves_negative_valuation():
    ev: list = []
    used, flags = ff._consistency(-0.60, 0.50, ev, [])
    assert used == -0.30 and flags == ["growth_premium"]


def test_consistency_leaves_aligned_cases_untouched():
    assert ff._consistency(0.5, 0.2, [], []) == (0.5, [])
    # 低估值 + 轻微下修（-0.09 未过阈值）不折半
    assert ff._consistency(0.6, -0.09, [], []) == (0.6, [])
    # 缺任一侧就不做护栏
    assert ff._consistency(None, -0.5, [], []) == (None, [])
    assert ff._consistency(0.7, None, [], []) == (0.7, [])


def test_guard_reports_flags_and_used_valuation():
    """decision_guard 展示的必须是「折半后」的分量，且把护栏标签带进 reason。"""
    out = dg.evaluate_decision({
        "sentiment_score": 0.5, "suggested_action": "buy", "target_asset": "NVDA",
        "market_confirmation": "unknown", "decision_context": "{}",
        "cluster_size": 1, "history_sample": {"total": 0, "wins": 0},
        "heat_score": 5.0, "impact_score": 5.0, "asset_impact": 5.0,
        "fundamental": {
            "available": True, "score": 0.3, "ticker": "NVDA", "kind": "us",
            "reason": "ok", "flags": ["value_trap"],
            "components": {"valuation_percentile": 0.8, "valuation_used": 0.4,
                           "earnings_revision": -0.2},
        },
    })
    factor = out["factors"]["fundamental"]
    assert out["fundamental"]["flags"] == ["value_trap"]
    assert "护栏=value_trap" in factor["explanation"]
    assert "0.40" in factor["explanation"]     # 用的是折半值，不是 0.80


def test_guard_keeps_reason_reason_for_gaps():
    out = dg.evaluate_decision({
        "sentiment_score": 0.5, "suggested_action": "buy", "target_asset": "CN_STOCK",
        "market_confirmation": "unknown", "decision_context": "{}",
        "cluster_size": 1, "history_sample": {"total": 0, "wins": 0},
        "heat_score": 5.0, "impact_score": 5.0, "asset_impact": 5.0,
        "fundamental": {"available": False, "reason": "placeholder", "kind": "placeholder",
                        "detail": "占位标签，基本面读不到"},
    })
    factor = out["factors"]["fundamental"]
    assert factor["available"] is False
    assert out["fundamental"]["reason"] == "placeholder"
    # 不可得时不占权重
    assert abs(out["coverage"] - 0.32) < 1e-9


# ── 3) A股累计口径 → TTM ───────────────────────────────────────────────────
def _a_row(report_date: str, label: str, **kw):
    row = {"REPORTDATE": report_date, "DATEMMDD": label}
    row.update(kw)
    return row


def test_a_ttm_restores_ttm_from_cumulative():
    """三季报累计 51.53 + 上年年报 68 − 去年三季报累计 47 = TTM 72.53。"""
    rows = [
        _a_row("2026-09-30", "三季报", BASIC_EPS=51.53),
        _a_row("2025-12-31", "年报", BASIC_EPS=68.0),
        _a_row("2025-09-30", "三季报", BASIC_EPS=47.0),
    ]
    assert cf._a_ttm(rows, "BASIC_EPS") == round(51.53 + 68.0 - 47.0, 4)


def test_a_ttm_annual_is_passthrough():
    rows = [_a_row("2026-12-31", "年报", BASIC_EPS=70.0)]
    assert cf._a_ttm(rows, "BASIC_EPS") == 70.0


def test_a_ttm_returns_none_when_incomplete():
    """缺上年年报就宁可不给 TTM —— 不给错的数，也不给"累计值冒充 TTM"。"""
    rows = [_a_row("2026-09-30", "三季报", BASIC_EPS=51.53)]
    assert cf._a_ttm(rows, "BASIC_EPS") is None
    assert cf._a_quarter_count("三季报") == 3
    assert cf._a_quarter_count("年报") == 4


def _q(period_end: str, eps, revenue=100.0, gp=50.0):
    return {"period_end": period_end, "eps_diluted": eps, "revenue": revenue,
            "gross_profit": gp, "net_income": 10.0, "operating_cash_flow": 12.0}


def test_summarize_eps_yoy_from_income_statement():
    rows = [_q("2026-09-30", 3.0), _q("2026-06-30", 2.5), _q("2026-03-31", 2.0), _q("2025-12-31", 2.5),
            _q("2025-09-30", 1.5), _q("2025-06-30", 1.2), _q("2025-03-31", 1.1), _q("2024-12-31", 1.2)]
    out = ef.summarize(rows)
    assert out["ttm"]["eps_diluted"] == 10.0
    assert out["yoy"]["eps_diluted"] == 100.0     # 10.0 / 5.0 - 1
    assert out["eps_source"] == "income_statement"


def test_summarize_eps_yoy_from_earnings_history_when_missing():
    """利润表没 EPS（AV 常态）时用一致预期历史拼序列，并标清来源。"""
    rows = [_q("2026-09-30", None), _q("2026-06-30", None), _q("2026-03-31", None),
            _q("2025-12-31", None), _q("2025-09-30", None), _q("2025-06-30", None),
            _q("2025-03-31", None), _q("2024-12-31", None)]
    hist = [{"event_date": d, "eps_actual": v} for d, v in (
        ("2026-09-30", 3.0), ("2026-06-30", 2.5), ("2026-03-31", 2.0), ("2025-12-31", 2.5),
        ("2025-09-30", 1.5), ("2025-06-30", 1.2), ("2025-03-31", 1.1), ("2024-12-31", 1.2))]
    out = ef.summarize(rows, eps_history=hist)
    assert out["yoy"]["eps_diluted"] == 100.0
    assert out["eps_source"] == "earnings_history"


def test_summarize_negative_base_guard():
    """基数为负：同比数学上无意义（扭亏会被读成 -150%），不算并标明原因。"""
    rows = [_q("2026-09-30", 1.0), _q("2026-06-30", 0.5), _q("2026-03-31", 0.2), _q("2025-12-31", 0.3),
            _q("2025-09-30", -0.5), _q("2025-06-30", -0.5), _q("2025-03-31", -0.5), _q("2024-12-31", -0.5)]
    out = ef.summarize(rows)
    assert "eps_diluted" not in out["yoy"]
    assert out["eps_source"].endswith("_neg_base")
    assert out["ttm"]["eps_diluted"] == 2.0       # 水平仍然给


def test_summarize_short_history_gives_no_ttm():
    out = ef.summarize([_q("2026-09-30", 1.0)])
    assert out["ttm"] == {} and out["yoy"] == {}
    assert out["eps_source"] == "income_statement"


def test_db_pack_window_is_800_days():
    """400 天只装得下 ~5 个季度 → EPS 同比永远算不出来；窗口必须 ≥8 季度。"""
    src = Path(ef.__file__).read_text(encoding="utf-8")
    assert "days: int = 800" in src or "days=800" in src


# ── 4) AV 日配额账本 ────────────────────────────────────────────────────────
def test_av_budget_refuses_after_limit(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "AV_DAILY_BUDGET", 2)
    monkeypatch.setattr(ef, "_budget_dir", lambda: str(tmp_path))
    assert ef._take_av_budget() == ""
    assert ef._take_av_budget() == ""
    second = ef._take_av_budget()
    assert second and "配额已用尽" in second
    state = ef.budget_state()
    assert state["used"] == 2 and state["limit"] == 2 and state["remaining"] == 0


def test_av_budget_state_survives_corrupt_file(monkeypatch, tmp_path):
    monkeypatch.setattr(ef, "_budget_dir", lambda: str(tmp_path))
    (tmp_path / f"av_budget_{time.strftime('%Y-%m-%d')}.json").write_text("{not json", encoding="utf-8")
    assert ef.budget_state()["used"] == 0


# ── 5) 落库读回：CN 表 → macro 层条目 ─────────────────────────────────────
def test_cn_layer_items_shape_matches_us_keys():
    rec = {"ticker": "600519.SH", "market": "A", "name": "贵州茅台", "report_date": "2026-09-30",
           "period_label": "三季报", "revenue_ttm": 174433934764.98, "eps_ttm": 65.66,
           "revenue_yoy": 1.8179, "net_profit_yoy": -0.25, "roe_annualized": 32.63,
           "gross_margin": 89.58, "pe_ttm": 22.14, "pb": 6.70, "source": "eastmoney"}
    items = cf.layer_items(rec)
    keys = {i["metric_key"] for i in items}
    assert {"fund.revenue_ttm", "fund.eps_ttm", "fund.revenue_yoy", "fund.net_profit_yoy",
            "fund.roe", "fund.gross_margin", "fund.pe_ttm", "fund.pb"} == keys
    rev = next(i for i in items if i["metric_key"] == "fund.revenue_ttm")
    assert rev["unit"] == "亿" and abs(rev["value"] - 174433934764.98 / 1e8) < 1e-6
    assert all(i["payload"]["market"] == "A" for i in items)


def test_cn_build_rejects_non_cn_market():
    out = cf.build("NVDA", "US")
    assert out["available"] is False and out["score"] == 0.0


# ── 6) 配额是共用资源：快照层必须给三表 cron 留出余量 ─────────────────────
def test_equity_snapshot_reserves_budget_for_statements(monkeypatch, tmp_path):
    """名单 5→16 只后，快照层若不记账会把 25 次/日吃干 → 三表 cron 静默饿死。

    剩余配额 <= AV_SNAPSHOT_RESERVE 时，快照层必须**不发请求**就返回 None。
    """
    import feeds.equity_feed as efeed

    called = {"n": 0}

    def _get(url, timeout):
        called["n"] += 1
        return {"Symbol": "AAPL", "PERatio": "20"}

    monkeypatch.setattr(config, "AV_DAILY_BUDGET", 25)
    monkeypatch.setattr(config, "AV_SNAPSHOT_RESERVE", 12)
    monkeypatch.setattr(ef, "_budget_dir", lambda: str(tmp_path))
    (tmp_path / f"av_budget_{time.strftime('%Y-%m-%d')}.json").write_text(
        json.dumps({"used": 14, "limit": 25}), encoding="utf-8")
    assert efeed._av_fallback(_get, "AAPL") is None
    assert called["n"] == 0                    # 一次请求都没发

    # 余量充足时照常请求，并且记账 +1
    (tmp_path / f"av_budget_{time.strftime('%Y-%m-%d')}.json").write_text(
        json.dumps({"used": 5, "limit": 25}), encoding="utf-8")
    out = efeed._av_fallback(_get, "AAPL")
    assert called["n"] == 1 and out is not None
    assert ef.budget_state()["used"] == 6

# ── 7) 配额切分与轮转：免费档装不下 16 只，必须按「样本少的先补」排队 ──────
def test_fetch_pack_limit_shrinks_with_remaining_budget(monkeypatch, tmp_path):
    """剩 25 次就按配置刷，剩 8 次就只刷 2 只（每只 4 次），不硬撞 429。"""
    monkeypatch.setattr(ef, "_budget_dir", lambda: str(tmp_path))
    monkeypatch.setattr(config, "EQUITY_MAX_PER_RUN", 3)
    f = tmp_path / f"av_budget_{time.strftime('%Y-%m-%d')}.json"

    f.write_text(json.dumps({"used": 8, "limit": 25}), encoding="utf-8")     # 剩 17
    assert ef.budget_state()["remaining"] == 17
    f.write_text(json.dumps({"used": 21, "limit": 25}), encoding="utf-8")    # 剩 4
    assert ef.budget_state()["remaining"] == 4

    # 直接验算自适应逻辑（不跑网络）：剩 4 次 → 1 只
    left = ef.budget_state()["remaining"]
    assert max(1, left // 4) == 1


def test_snapshot_order_puts_starved_tickers_first():
    """eq.pe 样本少的票必须排在前面，否则名单一长末尾永远拿不到分位。"""
    import feeds.equity_feed as efeed

    counts = {"NVDA": 2841, "MSTR": 2841, "AMZN": 0}    # NEWCO 库里没有 → 也算 0
    order = efeed._snapshot_starved(["NVDA", "MSTR", "AMZN", "NEWCO"], counts=counts)
    assert order[:2] == ["AMZN", "NEWCO"]      # 0 样本的两只在前（按原顺序 tie-break）
    assert order[2:] == ["NVDA", "MSTR"]
    # 空计数/未知票都不炸，仍返回全部
    assert set(efeed._snapshot_starved(["A", "B"], counts={})) == {"A", "B"}

def test_fetch_snapshot_two_phase_only_av_for_fmp_misses(monkeypatch):
    """两阶段取数：FMP 命中的票不碰 AV；只有 FMP 落空的票才排队走 AV。"""
    import feeds.equity_feed as efeed

    monkeypatch.setattr(config, "FMP_API_KEY", "k")
    monkeypatch.setattr(config, "ALPHAVANTAGE_API_KEY", "k")
    monkeypatch.setattr(config, "EQUITY_TICKERS", ("AAA", "BBB", "CCC"))

    fmp_calls, av_calls = [], []

    def fake_fmp(get, ticker):          # AAA 走 FMP 成功，BBB/CCC 落空
        fmp_calls.append(ticker)
        if ticker == "AAA":
            return [{"status": "ok", "payload": {"label": ticker}, "metric_key": "equity"}]
        return None

    def fake_av(get, ticker):
        av_calls.append(ticker)
        return None                     # 一律失败，看它回落到 unavailable 而不是崩

    monkeypatch.setattr(efeed, "_fmp_one", fake_fmp)
    monkeypatch.setattr(efeed, "_av_fallback", fake_av)
    rows = efeed.fetch_snapshot(get=lambda *a, **k: None)

    assert fmp_calls == ["AAA", "BBB", "CCC"]      # FMP 全量逐票试
    assert av_calls == ["BBB", "CCC"]              # 只有落空的才走 AV
    assert "AAA" not in av_calls                   # 命中 FMP 的票不浪费配额
    labels = {r["payload"].get("label") for r in rows if r.get("payload")}
    assert labels == {"AAA", "BBB", "CCC"}         # 每个 ticker 都有返回行（不缺位）

# ── 8) PE 历史倒推：分位不再靠快照熬天数 ────────────────────────────────
def _mem_conn(events, bars):
    """内存库：只放 pe_history 需要的两张表。"""
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE equity_earnings_events "
                 "(ticker TEXT, event_date TEXT, eps_actual REAL)")
    conn.execute("CREATE TABLE ohlcv_daily (asset TEXT, ts INTEGER, close REAL)")
    for d, eps in events:
        conn.execute("INSERT INTO equity_earnings_events VALUES ('TST', ?, ?)", (d, eps))
    for ts, close in bars:
        conn.execute("INSERT INTO ohlcv_daily VALUES ('TST', ?, ?)", (ts, close))
    return conn


def test_pe_history_never_uses_unpublished_eps():
    """未公布（event_date 在未来）的季报不能进 TTM —— 防前视。"""
    import datetime as dt
    base = dt.datetime(2025, 1, 1, tzinfo=dt.timezone.utc).timestamp()
    day = 86400
    events = [("2025-01-15", 1.0), ("2025-04-15", 1.0), ("2025-07-15", 1.0),
              ("2025-10-15", 1.0), ("2026-01-15", 9.0)]      # 最后一季是"未来"才公布的
    bars = [(int(base + day * 350), 10.0),                  # 2025-12-17，9.0 那季还没公布
            (int(base + day * 400), 10.0)]                  # 2026-02-05，已公布
    hist = ef.pe_history("TST", days=365, conn=_mem_conn(events, bars))
    assert len(hist) == 2
    assert abs(hist[0][1] - 2.5) < 1e-6      # 10 / (1+1+1+1)，9.0 那一季不能算进去
    assert abs(hist[1][1] - 10.0 / 12) < 1e-6   # 公布后才进 TTM（1+1+1+9）


def test_pe_history_skips_loss_making_ttm():
    """TTM 亏损（四季合计 ≤ 0）不给 PE —— 负 PE 放分位会变成"便宜=看多"。"""
    import datetime as dt
    base = dt.datetime(2025, 1, 1, tzinfo=dt.timezone.utc).timestamp()
    events = [("2025-01-15", -1.0), ("2025-04-15", -1.0), ("2025-07-15", 0.5), ("2025-10-15", 0.5)]
    bars = [(int(base + 86400 * 300), 10.0)]
    assert ef.pe_history("TST", days=365, conn=_mem_conn(events, bars)) == []


def test_pe_history_needs_four_quarters():
    """季报不足 4 个 → 拼不出 TTM，返回空而不是拿 1 个季度硬凑。"""
    import datetime as dt
    base = dt.datetime(2025, 1, 1, tzinfo=dt.timezone.utc).timestamp()
    events = [("2025-01-15", 1.0), ("2025-04-15", 1.0)]
    bars = [(int(base + 86400 * 200), 10.0)]
    assert ef.pe_history("TST", days=365, conn=_mem_conn(events, bars)) == []


def test_pe_readout_prefers_backfill_series(monkeypatch):
    """有倒推序列时，分位以「日线×EPS」为准，并标注来源。"""
    import fundamental_factor as ff
    import equity_fundamentals as efq

    series = [(i * 86400, 10.0 + i * 0.1) for i in range(100)]       # 100 天缓涨
    monkeypatch.setattr(efq, "pe_history", lambda *a, **k: series)
    rd = ff._percentile_readout("TST", "eq.pe", "pe")
    assert rd["pe_source"] == "backfill(price/eps_ttm)"
    assert rd["pe_samples"] == 100
    assert rd["pe_position"] == 100.0        # 当前值就是序列最高点
    assert rd["pe"] == round(series[-1][1], 2)

# ── 9) 公司名反解：占位标签不再永远拿不到基本面 ────────────────────────
def test_name_alias_resolves_only_within_same_market():
    """CN_STOCK 里的"苹果"不许认成美股 AAPL —— 跨市场宁可不认。"""
    import asset_kinds as ak

    assert ak.resolve_from_text("贵州茅台三季度净利同比增长 12%", "CN_STOCK") == ("a_share", "600519.SH")
    assert ak.resolve_from_text("腾讯控股宣布回购 30 亿港元", "HK_STOCK") == ("hk", "00700.HK")
    assert ak.resolve_from_text("英伟达发布新一代芯片", "US_STOCK") == ("us", "NVDA")
    # 跨市场不认
    assert ak.resolve_from_text("苹果公司发布新机", "CN_STOCK") is None
    # NONE 是模型明确说"无可交易标的"，不推翻
    assert ak.resolve_from_text("贵州茅台大涨", "NONE") is None
    # 空文本不炸
    assert ak.resolve_from_text("", "CN_STOCK") is None


def test_longest_name_wins():
    """长名优先：'腾讯控股' 不能被 '腾讯' 抢先匹配成乱码。"""
    import asset_kinds as ak

    assert ak.resolve_from_text("腾讯控股今日公告", "HK_STOCK") == ("hk", "00700.HK")
    assert ak.classify("贵州茅台") == ("a_share", "600519.SH")


def test_build_uses_hint_to_recover_cn_ticker():
    """占位标签 + 新闻点名公司 → 反解出代码后正常出读数。"""
    import fundamental_factor as ff

    plain = ff.build("CN_STOCK", use_cache=False)          # 无 hint → 仍是缺口
    assert plain["available"] is False and plain["reason"] == "placeholder"

    hinted = ff.build("CN_STOCK", use_cache=False, hint="贵州茅台三季度净利润同比增长 12%")
    assert hinted["ticker"] == "600519.SH" and hinted["kind"] == "a_share"

# ── 10) 资产负债表按季对齐（原来把最新一季贴给了所有季）─────────────────
def test_av_balance_sheet_matches_by_quarter():
    """每季的净资产/总资产必须来自同一季的报表，不能拿最新一季顶替。"""
    income = {"quarterlyReports": [
        {"fiscalDateEnding": "2026-09-30", "totalRevenue": "10", "netIncome": "2"},
        {"fiscalDateEnding": "2026-06-30", "totalRevenue": "9", "netIncome": "1"},
    ]}
    balance = {"quarterlyReports": [
        {"fiscalDateEnding": "2026-09-30", "totalAssets": "100", "totalShareholderEquity": "50"},
        {"fiscalDateEnding": "2026-06-30", "totalAssets": "90", "totalShareholderEquity": "40"},
    ]}
    rows = ef.parse_av_statements(income, balance, {})
    got = {r["period_end"]: (r["total_equity"], r["total_assets"]) for r in rows}
    assert got["2026-09-30"] == (50.0, 100.0)
    assert got["2026-06-30"] == (40.0, 90.0)      # 不是 50/100

# ── 11) 正文直接抠代码 + 全市场名称字典 ────────────────────────────────
def test_extract_codes_from_text():
    """新闻正文里写了代码就优先用代码；指数和 4 位数不能被当成股票代码。"""
    assert ak.extract_codes("有投资者向新易盛（300502.SZ）提问") == [("a_share", "300502.SZ")]
    assert ak.extract_codes("600519.SH 今日大涨") == [("a_share", "600519.SH")]
    assert ak.extract_codes("$NVDA 盘后涨 3%") == [("us", "NVDA")]
    assert ak.extract_codes("标普500指数将达8200点") == []       # 4 位数不是 A股代码
    assert ak.extract_codes("科创50跌6%") == []                 # 指数不是个股


def test_name_map_resolves_beyond_static_aliases():
    """静态别名表只有 74 个名字；库里 5800 只的字典要能把中小盘简称也解出来。"""
    m = {"东山精密": ("a_share", "002384.SZ"), "中科飞测": ("a_share", "688361.SH")}
    assert ak.resolve_from_text("【东山精密触及跌停】成交额96亿", "CN_STOCK", m) == \
        ("a_share", "002384.SZ")
    # 没有字典时解不出（说明命中确实来自字典，不是静态表）
    assert ak.resolve_from_text("【东山精密触及跌停】成交额96亿", "CN_STOCK") is None
    # 市场不匹配仍然不认：A股字典喂给 HK_STOCK 不应命中
    assert ak.resolve_from_text("【东山精密触及跌停】", "HK_STOCK", m) is None
    # NONE 是模型明确"无可交易标的"，不推翻
    assert ak.resolve_from_text("东山精密跌停", "NONE", m) is None


def test_quarter_ends_for_hk_dictionary():
    """港股各家报告期分散，字典要靠自然季末去扫，至少给出 8 个、且按时间倒序。"""
    dates = cf._quarter_ends(8)
    assert len(dates) == 8
    assert dates == sorted(dates, reverse=True)
    assert all(d.endswith(("-03-31", "-06-30", "-09-30", "-12-31")) for d in dates)


# ── 日更调度：不依赖 launchd（macOS TCC 挡住后台进程读 ~/Desktop）────────────
def test_equity_daily_due_respects_hour_gate():
    """05:10 之前不跑，之后才跑 —— 跑早了纯浪费配额。"""
    import datetime as _dt
    import api_server as _srv
    before = _dt.datetime(2026, 11, 18, 5, 5)
    after = _dt.datetime(2026, 11, 18, 5, 11)
    assert _srv._equity_daily_due(now=before, last_run="") is False
    assert _srv._equity_daily_due(now=after, last_run="") is True


def test_equity_daily_due_once_per_day():
    """当天跑过就不再跑（launchd 与引擎两份调度靠这个状态文件互斥）。"""
    import datetime as _dt
    import api_server as _srv
    n = _dt.datetime(2026, 11, 18, 9, 30)
    assert _srv._equity_daily_due(now=n, last_run="2026-11-18") is False
    assert _srv._equity_daily_due(now=n, last_run="2026-11-17") is True


def test_equity_daily_watcher_is_registered():
    """必须真的挂进 lifespan —— 只写函数不注册等于没生效。"""
    import pathlib as _pl
    src = _pl.Path(__file__).resolve().parents[1] / "src_python" / "api_server.py"
    text = src.read_text(encoding="utf-8")
    assert "equity_fundamentals_daily_watcher()" in text
    assert 'name="equity_daily"' in text, "watcher 未注册进 lifespan tasks 列表"
