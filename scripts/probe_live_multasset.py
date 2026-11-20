# -*- coding: utf-8 -*-
"""真实调用实测：多品种新闻分析时，主品种与副品种各由哪个模型产出。

静态分析容易绕晕（副品种条目继承主条目 model_label，但库里却出现两个不同模型），
这里直接跑真实 LLM（OpenRouter），观察实际用的模型与六段格式。

用法：cd backend && python -X utf8 ../scripts/probe_live_multasset.py
注意：会真实调用 LLM（约 1-2 次请求），消耗 OpenRouter 额度。
"""

import asyncio
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

NEWS = (
    "伊朗威胁封锁霍尔木兹海峡，美国航母战斗群驶入波斯湾；"
    "同时沙特阿美设施遭无人机袭击，国际油价与黄金同步跳涨。"
)


def main():
    tmp = tempfile.mkdtemp()
    config.DB_PATH = os.path.join(tmp, "probe.db")
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    db.migrate(conn)
    conn.close()

    print("=== 模型池 ===")
    for m in config.AI_MODEL_ROSTER:
        print("  %-28s weight=%s" % (m["id"], m.get("weight")))
    print("  OpenRouter 健康:", aw._openrouter_healthy(), "| MiniMax key 存在:",
          bool(config.MINIMAX_API_KEY))
    print()

    cfg = aw.build_model_config(config.DEFAULT_AI_MODEL_ID)
    print("本次使用模型:", cfg["id"], "| label:", cfg["label"],
          "| json_mode:", cfg.get("json_mode"))
    print()

    async def run():
        loop = asyncio.get_running_loop()
        aw._LLM_SEMAPHORE = asyncio.Semaphore(1)
        return await aw._process_single(
            {"id": 1, "content": NEWS, "timestamp": "2026-09-10T17:30:00"},
            cfg, loop,
        )

    out = asyncio.run(run())
    res = out.get("result") or {}
    print("=== 分析结果 ===")
    print("  error:", out.get("error"))
    print("  model_label:", out.get("model_label"), "| result.model_id:", res.get("model_id"))
    print("  target_asset:", res.get("target_asset"), "| action:", res.get("suggested_action"))
    sibs = res.get("sibling_decisions") or []
    print("  sibling_decisions:", [(s.get("target_asset"), s.get("suggested_action")) for s in sibs])
    print()
    path = str(res.get("reasoning_path") or "")
    print("=== 六段逐品种标签检查 ===")
    present = aw._path_assets_in_stages(path)
    print("  六段中出现的品种:", sorted(present) or "（无品种前缀）")
    print("  缺失（sibling 有但六段无）:", aw._siblings_missing_in_stages(res) or "无")
    print()
    print("=== reasoning_path（前 800 字）===")
    print(path[:800])

    shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
