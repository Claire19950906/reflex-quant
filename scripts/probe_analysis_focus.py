# -*- coding: utf-8 -*-
"""端到端验证「主品种分析针对性」链路：A 重试 + B 风控降级。

两个场景：
  场景1（A 生效）：首答跑偏（主品种 XAU 但六段不讲黄金）→ 触发重试 →
                   重试给针对性六段 → 采纳，保留原方向单
  场景2（B 生效）：首答跑偏、重试仍跑偏 → 方向单降级 HOLD（不给无依据的信号）

用法：cd backend && python -X utf8 ../scripts/probe_analysis_focus.py
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

OFFTOPIC = (  # 跑偏：六段讲峰会/区域合作，几乎不提黄金
    "[主体]对象：中亚峰会 ｜ 国别/市场：韩国/宏观\n"
    "[驱动力]峰会可能影响区域经济合作与政治稳定。\n"
    "[水位博弈]当前市场关注度较低，RSI 显示震荡。\n"
    "[叙事逻辑]峰会对区域合作带来积极信号，市场反应有限。\n"
    "[跨资产资金]资金流向显示 WTI 上涨，BTC 与 XAU 平稳。\n"
    "[反共识结论]无显著反共识信号。\n"
    "[逻辑自检]影响尚不明确，建议观望。\n"
    "[因果链分析]因：暂无匹配；果：暂无匹配结果"
)
FOCUSED = (  # 修正后：六段围绕黄金
    "[主体]对象：中亚峰会 ｜ 国别/市场：韩国/宏观\n"
    "[驱动力]峰会未涉及能源或货币政策，对黄金无直接传导，但避险情绪小幅升温。\n"
    "[水位博弈]黄金 4H RSI 45 中性，VWAP 上方，量能 0.8x 存量博弈。\n"
    "[叙事逻辑]黄金主线仍是实际利率与央行购金，本次峰会不改主线。\n"
    "[跨资产资金]黄金资金费率为正但增幅有限，未见明显避险买盘。\n"
    "[反共识结论]黄金无显著反共识信号，倾向观望。\n"
    "[逻辑自检]黄金与技术面、资金面自洽，方向中性。\n"
    "[因果链分析]因：暂无匹配；果：暂无匹配结果"
)
STILL_OFFTOPIC = OFFTOPIC  # 重试仍跑偏（场景2）


class _Resp:
    def __init__(self, content):
        self.status_code = 200
        self._p = {"choices": [{"message": {"content": content}}]}

    def raise_for_status(self):
        return None

    def json(self):
        return self._p


def _payload(path, action, score):
    return json.dumps({
        "reasoning_path": path, "target_asset": "XAU", "market_category": "GOLD",
        "sentiment_score": score, "suggested_action": action, "reasoning": "t",
        "asset_reasoning": "主品种 XAU(黄金)：t",
        "sibling_decisions": [],
    })


def run_case(name, retry_path, retry_action="BUY", retry_score=0.3):
    calls = []

    class _Sess:
        def post(self, url, **kwargs):
            body = kwargs.get("json") or {}
            texts = " ".join(str(m.get("content", "")) for m in (body.get("messages") or []))
            if "六段里几乎没有针对你判定的主品种" in texts:
                calls.append("retry:focus")
                return _Resp(_payload(retry_path, retry_action, retry_score))
            calls.append("first")
            return _Resp(_payload(OFFTOPIC, "BUY", 0.3))

        def close(self):
            return None

    tmp = tempfile.mkdtemp()
    config.DB_PATH = os.path.join(tmp, "p.db")
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    db.migrate(conn)
    conn.close()

    aw._LLM_SESSION = _Sess()
    aw._LLM_PROXY_SESSION = _Sess()
    cfg = {"id": "deepseek/deepseek-chat", "label": "deepseek/deepseek-chat (OpenRouter)",
           "api_base": "https://openrouter.ai/api/v1", "api_key": "k",
           "full_prompt": False, "json_mode": False, "use_proxy": True, "extra_body": {}}

    async def go():
        loop = asyncio.get_running_loop()
        aw._LLM_SEMAPHORE = asyncio.Semaphore(1)
        return await aw._process_single(
            {"id": 1, "content": "中亚峰会召开，区域经济合作", "timestamp": "2026-09-11T13:00:00"},
            cfg, loop)

    out = asyncio.run(go())
    res = out.get("result") or {}
    shutil.rmtree(tmp, ignore_errors=True)
    return calls, res


def main():
    failed = 0
    print("=== 场景1：重试后给出针对性分析 → 采纳（A 生效）===")
    calls, res = run_case("A", FOCUSED, "BUY", 0.3)
    hits = aw._asset_mentioned_stage_count(str(res.get("reasoning_path") or ""), "XAU")
    checks = [
        ("触发了 focus 重试", "retry:focus" in calls, str(calls)),
        ("采纳了针对性的六段", hits >= 3, f"提及 {hits} 段"),
        ("方向单保留（有依据）", str(res.get("suggested_action")) == "BUY", str(res.get("suggested_action"))),
    ]
    for n, ok, ex in checks:
        print(("  ✅ " if ok else "  ❌ ") + n + (f"  → {ex}" if ex else ""))
        if not ok:
            failed += 1

    print("\n=== 场景2：重试仍跑偏 → 降级 HOLD（B 生效）===")
    calls2, res2 = run_case("B", STILL_OFFTOPIC, "BUY", 0.3)
    hits2 = aw._asset_mentioned_stage_count(str(res2.get("reasoning_path") or ""), "XAU")
    checks2 = [
        ("触发了 focus 重试", "retry:focus" in calls2, str(calls2)),
        ("仍跑偏（提及段数低）", hits2 < 2, f"提及 {hits2} 段"),
        ("方向单已降级 HOLD", str(res2.get("suggested_action")) == "HOLD", str(res2.get("suggested_action"))),
        ("score 归中性区", abs(float(res2.get("sentiment_score") or 0)) <= 0.10, str(res2.get("sentiment_score"))),
        ("reasoning 说明降级原因", "依据不足" in str(res2.get("reasoning")), str(res2.get("reasoning"))[:40]),
    ]
    for n, ok, ex in checks2:
        print(("  ✅ " if ok else "  ❌ ") + n + (f"  → {ex}" if ex else ""))
        if not ok:
            failed += 1

    print("\n全部通过 ✅" if failed == 0 else f"\n{failed} 项失败 ❌")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
