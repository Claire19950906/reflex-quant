# -*- coding: utf-8 -*-
"""v446 —— 入库前筛子（重复 / 统计·分析类非新闻）的回归用例。

用户指令（2026-11-18）："筛选吧重复的、垃圾信息、统计类分析类（非新闻类信息）筛出去"。

覆盖：
  1. 统计/榜单/复盘/研报类被筛（含 reason 前缀非空）
  2. 真事件句不被误杀（含"宣布/加息/起诉"等事件动词）
  3. 同指纹窗口内重复 → dup_fingerprint
  4. 窗口外的同文不算重复
  5. 开关关闭时全部放行
"""
from __future__ import annotations

import os
import sqlite3
import sys

import pytest

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import news_keepfilter as kf


def _set_cfg(monkeypatch, **kw):
    for k, v in kw.items():
        monkeypatch.setenv(k, str(v))
        try:
            import config
            monkeypatch.setattr(config, k, v, raising=False)
        except Exception:
            pass


@pytest.fixture()
def db():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE raw_news (id INTEGER PRIMARY KEY, content TEXT,"
                 " ts INTEGER, status TEXT, content_fingerprint TEXT)")
    yield conn
    conn.close()


@pytest.mark.parametrize("text", [
    "BTC 24小时涨幅榜：SOL 领涨 12%",
    "今日加密货币市场复盘：主流币种普遍回调",
    "上周 ETF 资金流向一览：净流入 3.2 亿美元",
    "本周前瞻：关注美联储议息与 CPI 数据",
    "机构观点：比特币年底目标价 15 万美元",
    "据统计，过去 7 天链上活跃地址同比增长 18%",
    "收盘播报：标普收涨 1.2%，纳指收跌 0.3%",
])
def test_nonnews_filtered(monkeypatch, text):
    _set_cfg(monkeypatch, NEWS_PREFILTER_ENABLED=1, NEWS_NONNEWS_FILTER=1)
    r = kf.nonnews_reason(text)
    assert r and r.startswith("non_news:"), (text, r)


@pytest.mark.parametrize("text", [
    "美联储宣布降息 25 个基点，为年内第三次",
    "SEC 批准首只以太坊现货 ETF",
    "某交易所被罚 500 万美元，涉嫌违反反洗钱规定",
    "挪威国家石油公司遭网络攻击，生产未受影响",
    "Binance 宣布暂停部分提现，称正在排查",
])
def test_real_news_kept(monkeypatch, text):
    _set_cfg(monkeypatch, NEWS_PREFILTER_ENABLED=1, NEWS_NONNEWS_FILTER=1)
    assert kf.nonnews_reason(text) is None, text


def test_duplicate_detected(monkeypatch, db):
    _set_cfg(monkeypatch, NEWS_PREFILTER_ENABLED=1, NEWS_DEDUP_ON_INGEST=1, NEWS_DEDUP_WINDOW_H=6)
    now = 1_799_500_000
    db.execute("INSERT INTO raw_news (content, ts, status, content_fingerprint)"
               " VALUES ('x', ?, 'DONE', 'fp-abc')", (now - 3600,))
    assert kf.is_duplicate(db, "fp-abc", now) is True
    assert kf.is_duplicate(db, "fp-other", now) is False


def test_duplicate_outside_window(monkeypatch, db):
    _set_cfg(monkeypatch, NEWS_DEDUP_ON_INGEST=1, NEWS_DEDUP_WINDOW_H=6)
    now = 1_799_500_000
    db.execute("INSERT INTO raw_news (content, ts, status, content_fingerprint)"
               " VALUES ('x', ?, 'DONE', 'fp-old')", (now - 8 * 3600,))
    assert kf.is_duplicate(db, "fp-old", now) is False


def test_duplicate_ignores_filtered_original(monkeypatch, db):
    """原行已是 FILTERED（被判噪音）时，后来的同文仍要重新走流程。"""
    _set_cfg(monkeypatch, NEWS_DEDUP_ON_INGEST=1)
    now = 1_799_500_000
    db.execute("INSERT INTO raw_news (content, ts, status, content_fingerprint)"
               " VALUES ('x', ?, 'FILTERED', 'fp-f')", (now - 60,))
    assert kf.is_duplicate(db, "fp-f", now) is False


def test_switch_off_passes_everything(monkeypatch, db):
    _set_cfg(monkeypatch, NEWS_PREFILTER_ENABLED=0)
    assert kf.classify("BTC 涨幅榜", conn=db, fingerprint="fp", ts_int=0) is None
