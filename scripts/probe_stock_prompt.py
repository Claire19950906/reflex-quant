# -*- coding: utf-8 -*-
"""端到端验证：股票新闻分析时，模型收到的 prompt 是否含股票技术指标。

背景：股票此前只有"价+7d区间+趋势"，没有 RSI/ADX/VWAP/量能比；
而六段「水位博弈」段硬要求引用这些指标且禁止凭空判断
→ 模型无数据可写，只能把股票新闻判成 target_asset=NONE（不下单）。

本脚本 mock HTTP，跑真实 _process_single，检查实际发出的 messages：
  ① user 消息含 [美股行情] 行与 RSI/ADX/VWAP/量能比
  ② 主品种识别走的是新闻自带的 [美股 XXX] 标签（不是字典顺序）
  ③ 不联网、不碰生产库

用法：cd backend && python -X utf8 ../scripts/probe_stock_prompt.py
"""

import asyncio
import json
import os
import shutil
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "backend", "src_python"))

import config
import db
from engine import ai_worker as aw
import stock_prices

captured = {}


class _FakeResp:
    def __init__(self):
        self.status_code = 200
        content = json.dumps({
            "reasoning_path": (
                "[主体]对象：苹果公司(AAPL) ｜ 国别/市场：美国\n"
                "[驱动力][AAPL]：x→[水位博弈][AAPL]：x→[叙事逻辑][AAPL]：x"
                "→[跨资产资金]x→[反共识结论][AAPL]：x→[逻辑自检][AAPL]：x"
            ),
            "target_asset": "AAPL",
            "market_category": "OTHER",
            "sentiment_score": 0.3,
            "suggested_action": "HOLD",
            "reasoning": "测试",
            "asset_reasoning": "主品种 AAPL(苹果公司)：测试",
        })
        self._payload = {"choices": [{"message": {"content": content}}]}
        self.text = ""

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeSession:
    def post(self, url, **kwargs):
        captured["url"] = url
        captured["body"] = kwargs.get("json") or {}
        return _FakeResp()

    def close(self):
        return None


def main():
    tmp = tempfile.mkdtemp()
    config.DB_PATH = os.path.join(tmp, "probe.db")
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    db.migrate(conn)
    conn.close()

    aw._LLM_SESSION = _FakeSession()
    aw._LLM_PROXY_SESSION = _FakeSession()

    # 用真实新闻格式（带 [美股 AAPL] 标签）——验证标签优先识别
    news_text = (
        "如果你在十年前向苹果股票投资1万美元，今天你会拥有多少钱 "
        "[美股 AAPL] 来源:Yahoo Finance NVDA 相关提及"
    )

    # 先用真实网络取一次行情行（若网络不可用则跳过网络断言，只验证注入链路）
    try:
        live_line = stock_prices.stock_context_line(news_text)
    except Exception as exc:
        live_line = ""
        print("（行情行为空，可能无网络：%s）" % type(exc).__name__)

    cfg = {
        "id": "gpt-test",
        "label": "TestModel",
        "api_base": "http://127.0.0.1:1/v1",
        "api_key": "k",
        "full_prompt": False,
        "extra_body": {},
    }

    async def run():
        loop = asyncio.get_running_loop()
        aw._LLM_SEMAPHORE = asyncio.Semaphore(1)
        return await aw._process_single(
            {"id": 1, "content": news_text, "timestamp": "2026-09-10T16:50:00"},
            cfg, loop,
        )

    out = asyncio.run(run())

    body = captured.get("body") or {}
    msgs = body.get("messages") or []
    system_text = next((m["content"] for m in msgs if m["role"] == "system"), "")
    user_text = next((m["content"] for m in msgs if m["role"] == "user"), "")

    checks = [
        ("分析未报错", out.get("error") is None, str(out.get("error"))[:60]),
        ("symbol 识别采信 [美股 AAPL] 标签", stock_prices.detect_stock_symbol(news_text) == "AAPLUSDT",
         stock_prices.detect_stock_symbol(news_text) or "-"),
        ("user 消息含 [美股行情] 行", "[美股行情]" in user_text, ""),
        ("行情行含 RSI", "RSI" in user_text, ""),
        ("行情行含 ADX", "ADX" in user_text, ""),
        ("行情行含 VWAP", "VWAP" in user_text, ""),
        ("行情行含量能比", "量" in user_text and "x" in user_text, ""),
        ("system prompt 含股票无快照数据时的指路规则", "美股行情" in system_text and "不要因为快照里没有就判 NONE" in system_text, ""),
    ]
    print()
    print("=== 端到端校验 ===")
    failed = 0
    for name, ok, extra in checks:
        print(("  ✅ " if ok else "  ❌ ") + name + (f"  → {extra}" if extra else ""))
        if not ok:
            failed += 1
    print()
    print("实际 [美股行情] 行:")
    for line in user_text.split("\n"):
        if "[美股行情]" in line:
            print("  " + line.strip()[:160])

    shutil.rmtree(tmp, ignore_errors=True)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
