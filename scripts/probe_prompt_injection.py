# -*- coding: utf-8 -*-
"""确认模型实际收到的 prompt 是否含"逐品种六段"约束（防止改了代码却没生效）。

用法：cd backend && python -X utf8 ../scripts/probe_prompt_injection.py
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

captured = {}


class _FakeResp:
    def __init__(self):
        self.status_code = 200
        content = json.dumps({
            "reasoning_path": "[驱动力][WTI]：x→[水位博弈][WTI]：x→[叙事逻辑][WTI]：x"
                              "→[跨资产资金][WTI]：x→[反共识结论][WTI]：x→[逻辑自检][WTI]：x"
                              "→[因果链分析]因：无；果：无",
            "target_asset": "WTI", "market_category": "OIL", "sentiment_score": 0.3,
            "suggested_action": "HOLD", "reasoning": "t", "asset_reasoning": "主品种 WTI：t",
        })
        self._payload = {"choices": [{"message": {"content": content}}]}
        self.text = ""

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeSession:
    def post(self, url, **kwargs):
        captured["body"] = kwargs.get("json") or {}
        return _FakeResp()

    def close(self):
        return None


def probe_one(cfg: dict, tag: str) -> int:
    """跑一次真实 _process_single，检查实际发出的 system prompt 含哪些约束。"""
    captured.clear()
    aw._LLM_SESSION = _FakeSession()
    aw._LLM_PROXY_SESSION = _FakeSession()

    async def _run():
        loop = asyncio.get_running_loop()
        aw._LLM_SEMAPHORE = asyncio.Semaphore(1)
        return await aw._process_single(
            {"id": 1, "content": "伊朗威胁霍尔木兹海峡，油价与黄金齐涨 [美股 NVDA]",
             "timestamp": "2026-09-10T17:00:00"},
            cfg, loop,
        )

    out = asyncio.run(_run())
    msgs = (captured.get("body") or {}).get("messages") or []
    system_text = next((m["content"] for m in msgs if m["role"] == "system"), "")
    user_text = next((m["content"] for m in msgs if m["role"] == "user"), "")

    src = ("Prompt Force" if system_text.startswith(aw._JSON_PROMPT_FORCE[:60])
           else "_SYSTEM_PROMPT" if system_text.startswith(aw._SYSTEM_PROMPT[:60])
           else "未知")
    print("=== %s ===" % tag)
    print("  system 长度 %d | 来源: %s | 分析错误: %s" % (len(system_text), src, out.get("error")))
    keys = [
        ("[代码]： 格式硬约束", "[代码]："),
        ("覆盖完整性", "覆盖完整性"),
        ("禁止生搬硬套", "生搬硬套"),
        ("并排看 判断标准", "并排看"),
        ("美股改看 [美股行情] 行", "美股行情"),
    ]
    failed = 0
    for name, needle in keys:
        ok = needle in system_text
        print(("  ✅ " if ok else "  ❌ ") + name)
        if not ok:
            failed += 1
    print("  user 含 [美股行情]:", "[美股行情]" in user_text, "| 含新闻正文:", "霍尔木兹" in user_text)
    print()
    return failed


def main():
    tmp = tempfile.mkdtemp()
    config.DB_PATH = os.path.join(tmp, "probe.db")
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    db.migrate(conn)
    conn.close()

    # json_mode=False 才是 OpenRouter/nassaapi 的真实配置（build_model_config 按 base_url 判定）；
    # json_mode=True 走 _SYSTEM_PROMPT —— 两份提示词都要验证，避免只改了一份。
    failed = 0
    failed += probe_one(
        {"id": "gpt-test", "label": "T", "api_base": "http://127.0.0.1:1/v1",
         "api_key": "k", "full_prompt": False, "json_mode": False, "extra_body": {}},
        "OpenRouter 实际路径（Prompt Force，json_mode=False）",
    )
    failed += probe_one(
        {"id": "gpt-test", "label": "T", "api_base": "http://127.0.0.1:1/v1",
         "api_key": "k", "full_prompt": False, "json_mode": True, "extra_body": {}},
        "JSON 模式路径（_SYSTEM_PROMPT，json_mode=True）",
    )

    shutil.rmtree(tmp, ignore_errors=True)
    print("全部通过 ✅" if failed == 0 else "%d 项失败 ❌" % failed)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
