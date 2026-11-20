# -*- coding: utf-8 -*-
"""端到端验证「副品种逐段覆盖重试」（方向1）在真实链路里生效。

场景：
  第 1 次调用：副品种只在 1 段有行（其余段是主品种独有）→ 应判定"逐段覆盖不足"
  第 2 次调用（带缺口提示）：补齐所有段 → 应采纳修正结果

用法：cd backend && python -X utf8 ../scripts/probe_sibling_coverage_retry.py
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

PARTIAL = (  # 首答：WTI 只在「驱动力」有行，其余段只有主品种
    "[主体]对象：霍尔木兹 ｜ 国别/市场：中东\n"
    "[驱动力][XAU]：避险买盘；[WTI]：供给中断风险\n"
    "[水位博弈][XAU]：超卖\n[叙事逻辑][XAU]：主线未变\n"
    "[跨资产资金]资金观望\n[反共识结论][XAU]：中性\n[逻辑自检][XAU]：自洽"
    "\n[因果链分析]因：暂无匹配；果：暂无匹配结果"
)
FULL = (  # 修正后：六段每段都有 WTI 行
    "[主体]对象：霍尔木兹 ｜ 国别/市场：中东\n"
    "[驱动力][XAU]：避险买盘；[WTI]：供给中断风险\n"
    "[水位博弈][XAU]：超卖；[WTI]：接近超买\n"
    "[叙事逻辑][XAU]：主线未变；[WTI]：供给叙事\n"
    "[跨资产资金][XAU]：资金流入；[WTI]：资金流入\n"
    "[反共识结论][XAU]：中性；[WTI]：偏多\n"
    "[逻辑自检][XAU]：自洽；[WTI]：自洽"
    "\n[因果链分析]因：暂无匹配；果：暂无匹配结果"
)


def _payload(path):
    return json.dumps({
        "reasoning_path": path, "target_asset": "XAU", "market_category": "GOLD",
        "sentiment_score": 0.5, "suggested_action": "BUY", "reasoning": "测试",
        "asset_reasoning": "主品种 XAU(黄金)：避险；副品种 WTI(原油)：供给中断",
        "sibling_decisions": [{"target_asset": "WTI", "market_category": "OIL",
                               "sentiment_score": 0.3, "suggested_action": "BUY",
                               "reasoning": "供给中断"}],
    })


class _FakeResp:
    def __init__(self, content):
        self.status_code = 200
        self._p = {"choices": [{"message": {"content": content}}]}
        self.text = ""

    def raise_for_status(self):
        return None

    def json(self):
        return self._p


class _FakeSession:
    def post(self, url, **kwargs):
        body = kwargs.get("json") or {}
        texts = " ".join(str(m.get("content", "")) for m in (body.get("messages") or []))
        if "本次具体缺口" in texts:
            calls.append("retry:逐段覆盖")
            return _FakeResp(_payload(FULL))
        calls.append("first")
        return _FakeResp(_payload(PARTIAL))

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
    cfg = {"id": "deepseek/deepseek-chat", "label": "deepseek/deepseek-chat (OpenRouter)",
           "api_base": "https://openrouter.ai/api/v1", "api_key": "k",
           "full_prompt": False, "json_mode": False, "use_proxy": True, "extra_body": {}}

    async def run():
        loop = asyncio.get_running_loop()
        aw._LLM_SEMAPHORE = asyncio.Semaphore(1)
        return await aw._process_single(
            {"id": 1, "content": "伊朗威胁封锁霍尔木兹海峡", "timestamp": "2026-09-11T12:40:00"},
            cfg, loop,
        )

    out = asyncio.run(run())
    res = out.get("result") or {}
    still = aw._siblings_missing_stages(res)
    sibs = [str(s.get("target_asset")) for s in (res.get("sibling_decisions") or [])]

    checks = [
        ("触发了逐段覆盖重试", "retry:逐段覆盖" in calls, str(calls)),
        ("重试后副品种仍在列表里", "WTI" in sibs, str(sibs)),
        ("重试后逐段缺口清空", not still, str(still)),
        ("六段内容含 WTI 行", "[WTI]" in str(res.get("reasoning_path")), ""),
        ("未触发 SIBDROP（未被误剔除）", "WTI" in sibs, ""),
    ]
    print("=== 端到端校验 ===")
    failed = 0
    for name, ok, extra in checks:
        print(("  ✅ " if ok else "  ❌ ") + name + (f"  → {extra}" if extra else ""))
        if not ok:
            failed += 1
    print("\n调用序列:", calls)
    shutil.rmtree(tmp, ignore_errors=True)
    print("全部通过 ✅" if failed == 0 else "%d 项失败 ❌" % failed)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
