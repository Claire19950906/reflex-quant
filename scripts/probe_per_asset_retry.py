# -*- coding: utf-8 -*-
"""端到端验证"副品种逐品种覆盖"重试：首次输出缺副品种行 → 是否真的重试并采纳修正结果。

场景（对应真实数据现象）：
  第 1 次调用：模型输出无品种前缀的六段 + sibling_decisions=[XAU] → 应判定缺失
  第 2 次调用（带 _PER_ASSET_RETRY_HINT）：补上 [XAU]：行 → 应采纳修正后的结果

用法：cd backend && python -X utf8 ../scripts/probe_per_asset_retry.py
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

calls = []


def _payload(path: str, sibs):
    return json.dumps({
        "reasoning_path": path,
        "target_asset": "BTC",
        "market_category": "CRYPTO",
        "sentiment_score": 0.3,
        "suggested_action": "HOLD",
        "reasoning": "测试",
        "asset_reasoning": "主品种 BTC：测试",
        "sibling_decisions": sibs,
    })


# 首次：六段无品种前缀；重试：补上 [BTC]/[XAU] 逐段行
FIRST_PATH = (
    "[主体]对象：测试 ｜ 国别/市场：美国\n"
    "[驱动力]整体资金面偏紧→[水位博弈]存量博弈→[叙事逻辑]主线未变"
    "→[跨资产资金]资金观望→[反共识结论]中性→[逻辑自检]自洽"
    "→[因果链分析]因：暂无匹配；果：暂无匹配结果"
)
FIXED_PATH = (
    "[主体]对象：测试 ｜ 国别/市场：美国\n"
    "[驱动力][BTC]：资金面偏紧；[XAU]：避险需求平稳"
    "→[水位博弈][BTC]：区间中位；[XAU]：未超买"
    "→[叙事逻辑][BTC]：主线未变；[XAU]：与本次新闻无关，本段不适用"
    "→[跨资产资金]资金观望→[反共识结论][BTC]：中性；[XAU]：中性"
    "→[逻辑自检]自洽→[因果链分析]因：暂无匹配；果：暂无匹配结果"
)


class _FakeResp:
    def __init__(self, content):
        self.status_code = 200
        self._payload = {"choices": [{"message": {"content": content}}]}
        self.text = ""

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeSession:
    def post(self, url, **kwargs):
        body = kwargs.get("json") or {}
        msgs = body.get("messages") or []
        texts = [str(m.get("content", "")) for m in msgs]
        # 区分三种调用：首次 / 六段格式纠正 / 副品种逐品种纠正
        kind = "first"
        if any("没有按六段格式" in t for t in texts):
            kind = "retry:六段格式"
        elif any("缺少某些副品种的逐品种分析" in t for t in texts):
            kind = "retry:副品种覆盖"
        elif len(msgs) > 2:
            kind = "retry:其它"
        calls.append(kind)
        # 六段格式未通过时先返回带六段但无品种前缀的版本；
        # 只有收到"副品种覆盖"提示才返回补齐逐品种行的版本。
        if kind == "retry:副品种覆盖":
            content = _payload(FIXED_PATH, [{"target_asset": "XAU", "action": "HOLD"}])
        else:
            content = _payload(FIRST_PATH, [{"target_asset": "XAU", "action": "HOLD"}])
        return _FakeResp(content)

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

    # 用 OpenRouter 的真实配置（json_mode=False）——这正是此前重试永不触发的场景
    cfg = {"id": "deepseek/deepseek-chat", "label": "deepseek/deepseek-chat (OpenRouter)",
           "api_base": "https://openrouter.ai/api/v1", "api_key": "k",
           "full_prompt": False, "json_mode": False, "use_proxy": True, "extra_body": {}}

    async def run():
        loop = asyncio.get_running_loop()
        aw._LLM_SEMAPHORE = asyncio.Semaphore(1)
        return await aw._process_single(
            {"id": 1, "content": "测试新闻内容", "timestamp": "2026-09-10T17:10:00"},
            cfg, loop,
        )

    out = asyncio.run(run())
    result = out.get("result") or {}
    path = str(result.get("reasoning_path") or "")
    missing = aw._siblings_missing_in_stages(result)

    checks = [
        ("发生了副品种覆盖重试", "retry:副品种覆盖" in calls, str(calls)),
        ("最终结果采纳了补齐逐品种行的版本", "[XAU]" in path and "[BTC]" in path, ""),
        ("最终无缺失副品种", missing == [], str(missing)),
        ("无数据段写了原因（未编造）", "本段不适用" in path, ""),
    ]
    print("=== 端到端校验 ===")
    failed = 0
    for name, ok, extra in checks:
        print(("  ✅ " if ok else "  ❌ ") + name + (f"  → {extra}" if extra else ""))
        if not ok:
            failed += 1
    print()
    print("调用序列:", calls)

    shutil.rmtree(tmp, ignore_errors=True)
    print("全部通过 ✅" if failed == 0 else "%d 项失败 ❌" % failed)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
