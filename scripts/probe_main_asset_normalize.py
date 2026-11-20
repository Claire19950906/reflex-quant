# -*- coding: utf-8 -*-
"""验证主品种归一化在真实链路里是否生效（NONE + 副品种 → 应提升第一个副品种）。

用 mock LLM 返回"target_asset=NONE 但给了 sibling_decisions"的典型矛盾结果，
跑真实 _process_single，检查最终 result 的 target_asset。

用法：cd backend && python -X utf8 ../scripts/probe_main_asset_normalize.py
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
            "reasoning_path": (
                "[主体]对象：霍尔木兹海峡 ｜ 国别/市场：中东\n"
                "[驱动力][WTI]：供给中断风险\n[水位博弈][WTI]：强势\n"
                "[叙事逻辑][WTI]：供给\n[跨资产资金]资金流入\n"
                "[反共识结论][WTI]：偏多\n[逻辑自检]自洽\n"
                "[因果链分析]因：暂无匹配；果：暂无匹配结果"
            ),
            # 矛盾点：主品种写 NONE，却把真实受影响的品种放进 sibling
            "target_asset": "NONE",
            "market_category": "OTHER",
            "sentiment_score": 0.05,
            "suggested_action": "HOLD",
            "reasoning": "测试",
            "asset_reasoning": "主品种 XAU(黄金)：避险需求上升；副品种 WTI：供给中断",
            "sibling_decisions": [
                {"target_asset": "WTI", "market_category": "OIL", "sentiment_score": 0.6,
                 "suggested_action": "BUY", "reasoning": "供给中断推升油价"},
                {"target_asset": "XAU", "market_category": "GOLD", "sentiment_score": 0.4,
                 "suggested_action": "BUY", "reasoning": "避险买盘"},
            ],
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


def main():
    tmp = tempfile.mkdtemp()
    config.DB_PATH = os.path.join(tmp, "probe.db")
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    db.migrate(conn)
    conn.close()

    aw._LLM_SESSION = _FakeSession()
    aw._LLM_PROXY_SESSION = _FakeSession()

    cfg = {"id": "openai/gpt-4o-mini", "label": "gpt-4o-mini (OpenRouter)",
           "api_base": "https://openrouter.ai/api/v1", "api_key": "k",
           "full_prompt": False, "json_mode": False, "use_proxy": True, "extra_body": {}}

    async def run():
        loop = asyncio.get_running_loop()
        aw._LLM_SEMAPHORE = asyncio.Semaphore(1)
        return await aw._process_single(
            {"id": 1, "content": "伊朗威胁封锁霍尔木兹海峡，油价与黄金跳涨",
             "timestamp": "2026-09-11T12:10:00"},
            cfg, loop,
        )

    out = asyncio.run(run())
    res = out.get("result") or {}
    ta = str(res.get("target_asset") or "")
    sibs = res.get("sibling_decisions") or []

    checks = [
        ("归一化生效：target_asset 不再是 NONE", ta not in ("", "NONE", "OTHER"), ta),
        ("提升为第一个副品种 WTI", ta == "WTI", ta),
        ("market_category 跟随为 OIL", str(res.get("market_category")) == "OIL", str(res.get("market_category"))),
        ("suggested_action 取被提升那条", str(res.get("suggested_action")).upper() == "BUY", str(res.get("suggested_action"))),
        ("WTI 不再重复出现在 sibling", "WTI" not in [str(s.get("target_asset")).upper() for s in sibs],
         str([s.get("target_asset") for s in sibs])),
        ("asset_reasoning 主品种行对齐 WTI", str(res.get("asset_reasoning", "")).startswith("主品种 WTI"),
         str(res.get("asset_reasoning"))[:40]),
    ]
    print("=== 归一化端到端校验 ===")
    failed = 0
    for name, ok, extra in checks:
        print(("  ✅ " if ok else "  ❌ ") + name + (f"  → {extra}" if extra else ""))
        if not ok:
            failed += 1
    print()
    print("最终 target_asset:", ta, "| sibling:", [s.get("target_asset") for s in sibs])
    print("asset_reasoning:", str(res.get("asset_reasoning"))[:100])

    shutil.rmtree(tmp, ignore_errors=True)
    print("全部通过 ✅" if failed == 0 else "%d 项失败 ❌" % failed)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
