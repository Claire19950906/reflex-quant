# -*- coding: utf-8 -*-
"""复现 news=4661：主品种 XAU 六段仅 1 段提及、却下了 SELL 建仓 —— 查 FOCUS-GATE 为何未生效。

做法：mock LLM 原样返回该条的真实输出，跑真实 _process_single，观察最终 action。

用法：cd backend && python -X utf8 ../scripts/probe_focus_gate_miss.py
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

# news=4661 的真实 reasoning_path（主品种 XAU，六段仅 1 段提及 XAU）
REAL_PATH = (
    "[主体]对象：美债收益率 ｜ 国别/市场：美国\n"
    "[驱动力]美债收益率的上涨反映了市场对未来利率上升的预期，尤其是10年期收益率接近5.00%关口。\n"
    "[水位博弈]当前10年期美债收益率为4.979%，接近多年来的高位，RSI指标显示超卖状态，可能引发反弹。\n"
    "[叙事逻辑]当前市场对美债收益率的关注度高，利率上升可能影响风险资产的表现。\n"
    "[跨资产资金]美债收益率上升可能导致资金从风险资产（如BTC、ETH）流出，流入更安全的美债。\n"
    "[反共识结论]尽管美债收益率上升，但市场尚未完全消化这一信息，建议关注市场动态。\n"
    "[因果链分析]因：美债收益率上升；果：风险资产承压，黄金可能因避险需求受到支撑。"
)


class _Resp:
    def __init__(self, content):
        self.status_code = 200
        self._p = {"choices": [{"message": {"content": content}}]}

    def raise_for_status(self):
        return None

    def json(self):
        return self._p


class _Sess:
    def post(self, url, **kwargs):
        content = json.dumps({
            "reasoning_path": REAL_PATH,
            "target_asset": "XAU",
            "market_category": "GOLD",
            "sentiment_score": -0.3,
            "suggested_action": "SELL",
            "reasoning": "美债收益率上升，黄金承压",
            "asset_reasoning": "主品种 XAU(黄金)：美债收益率上升可能导致避险需求上升",
            "sibling_decisions": [],
        })
        return _Resp(content)

    def close(self):
        return None


def main():
    tmp = tempfile.mkdtemp()
    config.DB_PATH = os.path.join(tmp, "p.db")
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    db.migrate(conn)
    conn.close()

    aw._LLM_SESSION = _Sess()
    aw._LLM_PROXY_SESSION = _Sess()
    cfg = {"id": "openai/gpt-4o-mini", "label": "gpt-4o-mini (OpenRouter)",
           "api_base": "https://openrouter.ai/api/v1", "api_key": "k",
           "full_prompt": False, "json_mode": False, "use_proxy": True, "extra_body": {}}

    async def go():
        loop = asyncio.get_running_loop()
        aw._LLM_SEMAPHORE = asyncio.Semaphore(1)
        return await aw._process_single(
            {"id": 1, "content": "【美债收益率在亚洲交易时段触及多年高位】",
             "timestamp": "2026-09-11T13:44:00"},
            cfg, loop)

    out = asyncio.run(go())
    res = out.get("result") or {}
    hits = aw._asset_mentioned_stage_count(str(res.get("reasoning_path") or ""), "XAU")

    print()
    print("=== 复现结果 ===")
    print("  最终 action:", res.get("suggested_action"), "| score:", res.get("sentiment_score"))
    print("  XAU 提及段数:", hits)
    print("  reasoning:", str(res.get("reasoning"))[:70])
    ok = str(res.get("suggested_action")).upper() == "HOLD"
    print()
    print("  " + ("✅ FOCUS-GATE 生效（降级 HOLD）" if ok else "❌ 未降级 —— 复现了 bug"))

    shutil.rmtree(tmp, ignore_errors=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
