# -*- coding: utf-8 -*-
"""端到端验证：复盘经验分区是否真的进入 system prompt、且不再塞进 user 消息。

做法：隔离临时库铺 12 条启用经验（超过旧的 8 条上限）→ mock HTTP →
跑真实的 _process_single → 检查实际发出的 messages。
不联网、不碰生产库。

用法：cd backend && python -X utf8 ../scripts/probe_insights_prompt.py
"""

import asyncio
import json
import os
import shutil
import sqlite3
import sys
import tempfile

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "backend", "src_python"))

import config
import db
from engine import ai_worker as aw

captured = {}


class _FakeResp:
    """伪造 requests 响应：捕获请求体，返回一段合法的六段式 JSON。"""

    def __init__(self):
        self.status_code = 200
        self.text = ""
        content = json.dumps({
            "reasoning_path": (
                "[主体]对象：测试 ｜ 国别/市场：美国\n"
                "[驱动力]x→[水位博弈]x→[叙事逻辑]x→[跨资产资金]x→[反共识结论]x→[逻辑自检]x"
            ),
            "target_asset": "BTC",
            "market_category": "CRYPTO",
            "sentiment_score": 0.3,
            "suggested_action": "HOLD",
            "reasoning": "测试",
        })
        self._payload = {"choices": [{"message": {"content": content}}]}

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeSession:
    """伪造 requests.Session（ai_worker 用模块级 _LLM_SESSION / _LLM_PROXY_SESSION）。"""

    def post(self, url, **kwargs):
        captured["url"] = url
        captured["body"] = kwargs.get("json") or {}
        return _FakeResp()

    def close(self):
        return None


def fake_post(url, **kwargs):
    captured["url"] = url
    captured["body"] = kwargs.get("json") or {}
    return _FakeResp()


def main():
    tmp = tempfile.mkdtemp()
    config.DB_PATH = os.path.join(tmp, "probe.db")
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    db.migrate(conn)
    for i in range(12):
        conn.execute(
            "INSERT INTO review_insights (rule, scope, active, source_samples, origin)"
            " VALUES (?, 'review', 1, ?, ?)",
            (f"测试经验第{i + 1}条", 100 - i, "real"),
        )
    conn.commit()
    conn.close()

    # patch 模块级 Session（ai_worker 通过 session.post 发请求）
    aw._LLM_SESSION = _FakeSession()
    aw._LLM_PROXY_SESSION = _FakeSession()
    requests.post = fake_post

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
        section = aw._load_insights_section()
        print("分区生成: %d 字" % len(section))
        print("  含标题 ═══ 复盘经验:", "═══ 复盘经验" in section)
        print("  条数:", section.count(". [历史交易"))
        aw._LLM_SEMAPHORE = asyncio.Semaphore(1)
        out = await aw._process_single(
            {"id": 1, "content": "测试新闻内容", "timestamp": "2026-09-10T12:00:00"},
            cfg, loop, insights_section=section,
        )
        print("  分析返回 error:", out.get("error"))

    asyncio.run(run())

    body = captured.get("body") or {}
    msgs = body.get("messages") or []
    system_text = next((m["content"] for m in msgs if m["role"] == "system"), "")
    user_text = next((m["content"] for m in msgs if m["role"] == "user"), "")
    print()
    print("=== 实际发出的 prompt 校验 ===")
    checks = [
        ("system 含经验分区标题", "═══ 复盘经验" in system_text),
        ("system 含第 12 条（未被 8 条截断）", "测试经验第12条" in system_text),
        ("system 含样本数与来源标注", "[历史交易 · 100 样本]" in system_text),
        ("user 里已无经验残留（挪出 user 消息）", "复盘经验" not in user_text),
        ("user 仍含新闻正文", "测试新闻内容" in user_text),
    ]
    for name, ok in checks:
        print(("  ✅ " if ok else "  ❌ ") + name)
    print("  system 长度: %d 字" % len(system_text))

    shutil.rmtree(tmp, ignore_errors=True)
    return 0 if all(ok for _, ok in checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
