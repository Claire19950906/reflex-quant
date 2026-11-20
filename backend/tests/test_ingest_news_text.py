"""锁住 engine/ingest.py 的两个真 bug（2026-11-18 发现并修复）。

两个 bug 长得很像：都是"函数体被切断/调用口径不一致"，都**不报错**，只是安静地少数据。

1. `_extract_news_text` 缺了尾巴
   函数的 docstring、`parts` 初始化、嵌套 `_try_parse_json` / `_dig` 都在，
   但真正干活的 `_dig(msg)` + 去重 + `return ". ".join(unique)` 被切到了
   下一个函数 `_extract_pub_time` 的 `return ""` 之后（永远不执行）。
   → `_extract_news_text` 走完嵌套定义就直接到底，**隐式返回 None**。
   → 调用点 `if text:` 全部为假 → 新闻正文一条都进不了库，只剩后续兜底。
   断言: 拿到正文、多条标题按出现顺序去重、拼成 ". "。

2. `_extract_pub_time` 调用口径不一致
   函数头按 `msg["pub"]["time"]` 取值，但 `ingest` 里 480 行传的是 pub 本体，
   于是 `msg.get("pub")` 为 None → 整段判断跳过 → `published_at` 恒为空""。
   同一处却能从 `result["time"]` 拿到 ts_epoch，说明数据是有的，只是没被读到。
   断言: 传 pub 本体也能拿到发布时间。
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                'src_python'))

from engine.ingest import _extract_news_text, _extract_pub_time


def _fj_pub(titles_and_desc):
    """按 FinancialJuice 的真实结构造一条 Centrifugo publication。"""
    items = [{'Title': t, 'Description': d} for t, d in titles_and_desc]
    return {'data': {'msg': json.dumps(items, ensure_ascii=False), 'ev': 'sendUpdates'},
            'time': 1763500000}


def test_news_text_is_extracted():
    pub = _fj_pub([('Gold hits record high', 'Spot gold up 2% on ETF inflows')])
    text = _extract_news_text(pub)
    assert text is not None, '正文提取不该返回 None（尾巴丢失就会这样）'
    assert 'Gold hits record high' in text
    assert 'Spot gold up 2% on ETF inflows' in text


def test_news_text_dedupes_repeated_titles():
    pub = _fj_pub([('Gold hits record high', 'Spot gold up 2% on ETF inflows'),
                   ('Gold hits record high', '')])
    text = _extract_news_text(pub)
    assert text is not None
    assert text.count('Gold hits record high') == 1, '重复标题必须去重'
    assert ' · ' not in text


def test_news_text_empty_message_returns_none():
    assert _extract_news_text({}) is None


def test_pub_time_accepts_pub_body():
    """调用点传的是 pub 本体（含 time），不是一个 {"pub": {...}} 外壳。"""
    got = _extract_pub_time({'time': 1763500000, 'data': {'msg': 'x'}})
    assert got and got.startswith('20'), got


def test_pub_time_accepts_outer_push():
    """同时也保住老口径：外层 {'pub': {...}}。"""
    got = _extract_pub_time({'pub': {'time': 1763500000}})
    assert got and got.startswith('20'), got


def test_pub_time_fj_short_time():
    got = _extract_pub_time({'data': {'PostedShort': '2h ago'}})
    assert got == '2h ago'


def test_pub_time_missing_returns_empty_string():
    """拿不到就返回空串（写库时留空，让下游看到"采集失真"），不能抛。"""
    assert _extract_pub_time({}) == ''
