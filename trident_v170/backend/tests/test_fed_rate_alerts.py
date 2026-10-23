# -*- coding: utf-8 -*-
"""fed_rate_expectation 异动推送 + alerts 格式化 单元测试。"""
from __future__ import annotations

import fed_rate_expectation as fre
from engine import alerts as eng_alerts


def test_state_key_normalizes_severity_and_type():
    a = {"severity": "high", "type": "yield_move"}
    b = {"severity": "high", "type": "yield_move", "is_anomaly": True}
    c = {"severity": "med", "type": "yield_move"}
    assert fre._state_key(a) == fre._state_key(b)
    assert fre._state_key(a) != fre._state_key(c)


def test_should_push_requires_anomaly():
    assert fre._should_push({"is_anomaly": False, "severity": "high",
                             "type": "yield_move"}) is False
    assert fre._should_push({"is_anomaly": True, "severity": "high",
                             "type": "yield_move"}) is True


def test_dedup_window_blocks_repeat():
    fre.reset_alert_state()
    anomaly = {"is_anomaly": True, "severity": "high", "type": "yield_move"}
    assert fre._should_push(anomaly) is True
    fre.mark_pushed(anomaly)
    assert fre._should_push(anomaly) is False


def test_dedup_key_changes_with_severity_upgrade():
    """severity 升级（如 low→med）→ state_key 变 → 应允许推送。"""
    fre.reset_alert_state()
    low = {"is_anomaly": True, "severity": "low", "type": "yield_move"}
    med = {"is_anomaly": True, "severity": "med", "type": "yield_move"}
    fre.mark_pushed(low)
    assert fre._should_push(med) is True


def test_reset_alert_state_clears_all():
    fre.reset_alert_state()
    anomaly = {"is_anomaly": True, "severity": "high", "type": "both"}
    fre.mark_pushed(anomaly)
    assert fre._should_push(anomaly) is False
    fre.reset_alert_state()
    assert fre._should_push(anomaly) is True


# ─── format_fed_anomaly_message 单元测试 ────────────────────────────────


def test_format_title_with_high_severity_hawkish():
    anomaly = {"severity": "high", "type": "both",
               "yield_move_bp": 12.0, "expect_move_bp": 80.0}
    exp = {"direction": "hawkish", "expected_24m_change_bp": 80.0,
           "dgs2_pct": 4.5, "current_effr_pct": 5.33}
    title = eng_alerts.format_fed_anomaly_message(anomaly, exp, title_only=True)
    # v283: 标题改成说人话 — 不再用 "[Fed 异动]" jargon,直接说"美联储要加息"
    assert "美联储要加息" in title
    assert "🔴高" in title
    assert "鹰派" in title


def test_format_title_dovish_low_severity():
    anomaly = {"severity": "low", "type": "expectation_shift",
               "yield_move_bp": -2.0, "expect_move_bp": -20.0}
    exp = {"direction": "dovish", "expected_24m_change_bp": -20.0,
           "dgs2_pct": 4.2, "current_effr_pct": 5.33}
    title = eng_alerts.format_fed_anomaly_message(anomaly, exp, title_only=True)
    assert "🟢低" in title
    assert "鸽派" in title


def test_format_body_contains_key_data():
    anomaly = {"severity": "med", "type": "yield_move",
               "yield_move_bp": 10.0, "expect_move_bp": 0.0}
    exp = {"direction": "neutral", "expected_24m_change_bp": 0.0,
           "dgs2_pct": 4.4, "current_effr_pct": 5.33,
           "narrative": "测试叙事",
           "first_principles_chain": ["① 测试链 1", "② 测试链 2"]}
    body = eng_alerts.format_fed_anomaly_message(anomaly, exp, title_only=False)
    # v283: lead with 结论 + 各品种涨跌 + 当前数据 + 白话解释
    assert "美联储基准利率" in body        # 替代 "EFFR" 字段
    assert "美国 2 年期国债收益率" in body   # 替代 "2Y 美债收益率"
    assert "市场对未来" in body             # 利差解释
    assert "白话解释" in body                # 新增板块
    # narrative/chain 已不再是主板块 — 不再断言其直接出现


def test_format_body_handles_empty_chain():
    """LLM 偶尔返回空 chain → 不能崩(空 chain 不应在主板块展示)。"""
    anomaly = {"severity": "low", "type": "none",
               "yield_move_bp": 0.0, "expect_move_bp": 0.0}
    exp = {"direction": "neutral", "expected_24m_change_bp": 0.0,
           "dgs2_pct": 4.5, "current_effr_pct": 5.33,
           "narrative": "", "first_principles_chain": []}
    body = eng_alerts.format_fed_anomaly_message(anomaly, exp, title_only=False)
    # v283: 新版不带第一性原理板块(narrative/chain 不再展示,改为白话解释)
    assert "美联储按兵不动" in body
    assert "白话解释" in body


def test_format_handles_missing_fields_gracefully():
    """anomaly/expectation 字段不全 → 不崩，使用默认值。"""
    body = eng_alerts.format_fed_anomaly_message({}, {}, title_only=False)
    assert "中性" in body  # direction 缺省 → neutral → "中性"


def test_format_title_short_when_no_movement():
    """yield_move=0 / expect_move=0 → 标题不显示数字尾巴。"""
    anomaly = {"severity": "low", "type": "none",
               "yield_move_bp": 0.0, "expect_move_bp": 0.0}
    exp = {"direction": "neutral", "expected_24m_change_bp": 0.0}
    title = eng_alerts.format_fed_anomaly_message(anomaly, exp, title_only=True)
    # v283: 中性场景标题固定 "美联储按兵不动",不带数字尾巴
    assert "美联储按兵不动" in title
    # 不应有 bp jargon
    assert "bp" not in title.lower()


# ─── v284: 判断依据板块 ────────────────────────────────────────────────

def test_format_body_has_evidence_section_with_fred_source():
    """v284: data_source=fred → 邮件正文应出现「判断依据」板块,并标注数据源是 FRED。"""
    anomaly = {"severity": "high", "type": "both",
               "yield_move_bp": -30.0, "expect_move_bp": -50.0,
               "trigger_reason": "2Y 美债收益率 24h 下降 30 个点（阈值 5 个点）",
               "threshold_regime": "static", "threshold_vix": None,
               "news_refs": []}
    exp = {"direction": "dovish", "expected_24m_change_bp": -50.0,
           "dgs2_pct": 4.30, "current_effr_pct": 5.33,
           "narrative": "", "first_principles_chain": []}
    body = eng_alerts.format_fed_anomaly_message(anomaly, exp,
                                                 title_only=False,
                                                 data_source="fred")
    assert "【判断依据】" in body
    assert "美联储官方 FRED 数据" in body
    assert "触发原因" in body
    # 无新闻时应诚实标注
    assert "未关联" in body or "未采集到" in body


def test_format_body_shows_proxy_data_source_warning():
    """v284: data_source=yfinance 代理 → 邮件应注明"代理估算"而非冒充官方。"""
    anomaly = {"severity": "med", "type": "yield_move",
               "yield_move_bp": 8.0, "expect_move_bp": 5.0,
               "trigger_reason": "2Y 美债收益率 24h 上升 8 个点",
               "threshold_regime": "static", "threshold_vix": None,
               "news_refs": []}
    exp = {"direction": "hawkish", "expected_24m_change_bp": 5.0,
           "dgs2_pct": 4.5, "current_effr_pct": 5.33,
           "narrative": "", "first_principles_chain": []}
    body = eng_alerts.format_fed_anomaly_message(anomaly, exp,
                                                 title_only=False,
                                                 data_source="yfinance_5y_irx_proxy")
    assert "yfinance 代理" in body
    # 必须有误差标注,不能冒充官方精度
    assert "误差" in body


def test_format_body_correlates_news_when_available():
    """v284: anomaly.news_refs 非空 → 邮件应展示新闻标题 + 来源 + 重要度。"""
    anomaly = {"severity": "high", "type": "both",
               "yield_move_bp": -30.0, "expect_move_bp": -50.0,
               "trigger_reason": "2Y 美债收益率 24h 下降 30 个点",
               "threshold_regime": "static", "threshold_vix": None,
               "news_refs": [
                   {"ts": 1762500000, "title": "美联储主席鲍威尔暗示降息",
                    "source": "reuters", "impact_1to100": 85},
               ]}
    exp = {"direction": "dovish", "expected_24m_change_bp": -50.0,
           "dgs2_pct": 4.30, "current_effr_pct": 5.33,
           "narrative": "", "first_principles_chain": []}
    body = eng_alerts.format_fed_anomaly_message(anomaly, exp,
                                                 title_only=False,
                                                 data_source="fred")
    assert "关联新闻" in body
    assert "美联储主席鲍威尔暗示降息" in body
    assert "reuters" in body
    assert "重要度 85" in body
    # 必须有免责说明
    assert "仅供参考" in body


def test_format_body_honest_when_no_news_correlation():
    """v284: 没找到新闻时,必须诚实标注"未关联具体新闻事件",不能伪造。"""
    anomaly = {"severity": "high", "type": "yield_move",
               "yield_move_bp": -15.0, "expect_move_bp": -10.0,
               "trigger_reason": "2Y 利率 24h 下降 15 个点",
               "threshold_regime": "static", "threshold_vix": None,
               "news_refs": []}
    exp = {"direction": "dovish", "expected_24m_change_bp": -10.0,
           "dgs2_pct": 4.5, "current_effr_pct": 5.33,
           "narrative": "", "first_principles_chain": []}
    body = eng_alerts.format_fed_anomaly_message(anomaly, exp,
                                                 title_only=False,
                                                 data_source="fred")
    assert "未采集到" in body or "未关联" in body
    # 必须强调是基于利率数据自身变动,不能暗示是新闻驱动
    assert "利率数据自身变动" in body


def test_correlate_recent_fed_news_returns_list():
    """v284: _correlate_recent_fed_news 返回 list(可能空) — 不抛异常。"""
    from fed_rate_expectation import _correlate_recent_fed_news
    result = _correlate_recent_fed_news(window_hours=6, limit=3)
    assert isinstance(result, list)
    if result:
        for r in result:
            assert "title" in r
            assert "source" in r
            assert "ts" in r


def test_build_trigger_reason_humanizes_atype():
    """v284: _build_trigger_reason 把机器 atype 翻译成人话。"""
    from fed_rate_expectation import _build_trigger_reason
    thr = {"yield_bp": 5.0, "expect_bp": 25.0, "regime": "static", "vix": None}
    r1 = _build_trigger_reason("both", -30.0, 5.0, -50.0, 25.0, thr)
    assert "2Y 美债收益率" in r1 and "降息" in r1 and "阈值" in r1
    r2 = _build_trigger_reason("expectation_shift", 1.0, 5.0, 30.0, 25.0, thr)
    assert "市场预期 24 月累计加息" in r2
    r3 = _build_trigger_reason("none", 0.0, 5.0, 0.0, 25.0, thr)
    assert "无显著异动" in r3


def test_daemon_loop_calls_push_and_sleeps(monkeypatch):
    """daemon_loop: 第一次 push → sleep 60s（最小间隔），用 monkeypatch 把 sleep
    短路成 record-only，第一次 push 后即退出循环。"""
    calls = {"push": 0, "sleep_durations": []}

    def fake_push():
        calls["push"] += 1
        # 返回 no_anomaly 以避免 mark_pushed 副作用
        return {"pushed": False, "reason": "no_anomaly"}

    def fake_sleep(s):
        calls["sleep_durations"].append(s)
        raise StopIteration  # 跳出 while True

    monkeypatch.setattr(fre, "push_fed_anomaly_if_new", fake_push)
    monkeypatch.setattr(fre.time, "sleep", fake_sleep)
    # 缩短间隔：env 默认 300 → 这里用 monkeypatch 设小一点
    monkeypatch.setenv("FED_PUSH_INTERVAL_S", "60")
    try:
        fre.daemon_loop()
    except StopIteration:
        pass
    assert calls["push"] == 1
    assert calls["sleep_durations"] == [60]


def test_daemon_loop_interval_min_60(monkeypatch):
    """daemon_loop: 任何 < 60s 的间隔被强制抬到 60s。"""
    sleep_durations = []

    def fake_push():
        return {"pushed": False, "reason": "no_anomaly"}

    def fake_sleep(s):
        sleep_durations.append(s)
        raise StopIteration

    monkeypatch.setattr(fre, "push_fed_anomaly_if_new", fake_push)
    monkeypatch.setattr(fre.time, "sleep", fake_sleep)
    monkeypatch.setenv("FED_PUSH_INTERVAL_S", "5")
    try:
        fre.daemon_loop()
    except StopIteration:
        pass
    assert sleep_durations[0] == 60


def test_daemon_loop_logs_pushed(monkeypatch, capsys):
    """daemon_loop: pushed=True → 打印 PUSHED 行。"""
    monkeypatch.setattr(fre, "push_fed_anomaly_if_new",
                        lambda: {"pushed": True,
                                 "anomaly": {"direction": "hawkish",
                                             "severity": "high",
                                             "change_bp": 18.5}})
    monkeypatch.setenv("FED_PUSH_INTERVAL_S", "60")

    def fake_sleep(s):
        raise StopIteration
    monkeypatch.setattr(fre.time, "sleep", fake_sleep)
    try:
        fre.daemon_loop()
    except StopIteration:
        pass
    captured = capsys.readouterr()
    assert "PUSHED" in captured.out
    assert "hawkish" in captured.out
    assert "18.5bp" in captured.out