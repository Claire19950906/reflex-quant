# -*- coding: utf-8 -*-
"""v389 · 每日成绩单推送到飞书群（可选，未配置就静默跳过）

做法
----
1. 复用 `scorecard_share.collect()` 的口径，不另算一套数；
2. 只在 `FEISHU_SCORECARD_WEBHOOK` 有值时才真的 POST 到群机器人 webhook；
3. 发送失败只写日志，不影响成绩单生成（生成是主任务，推送是附赠）。

用法
----
    FEISHU_SCORECARD_WEBHOOK='https://open.feishu.cn/open-apis/bot/v2/hook/xxx' \
      python3 scripts/push_scorecard_lark.py

安全
----
webhook 只从环境变量读，不落盘、不进代码；仓库里只有 `.env.example` 的占位。
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request


def _build_text(data: dict) -> str:
    settled = data.get("settled") or 0
    winrate = data.get("winrate")
    win_txt = f"{winrate}%" if winrate is not None else "—"
    funnel = data.get("funnel") or []
    funnel_txt = " → ".join(f"{name} {n}" for name, n in funnel)
    return "\n".join([
        f"信号成绩单 · {data.get('generated_at', '')}",
        f"新闻入库 {data.get('news_total', 0)}（24h {data.get('news_24h', 0)}）",
        f"判断 {data.get('decisions', 0)} 条 · 已结算 {settled} 笔 · 胜率 {win_txt}",
        f"平均每笔 {data.get('avg_pnl', 0):.2f}%（合计 {data.get('sum_pnl', 0):.2f}%）",
        f"漏斗：{funnel_txt}",
        f"静默中 {data.get('muted_now', 0)} 个品种 · 已忽略 {data.get('ignored', 0)} 笔",
        "样本不足 10 笔时结论只作方向参考，不构成收益承诺。",
    ])


def main() -> int:
    hook = os.environ.get("FEISHU_SCORECARD_WEBHOOK", "").strip()
    if not hook:
        print("[push] FEISHU_SCORECARD_WEBHOOK 未配置，跳过推送")
        return 0
    try:
        sys.path.insert(0, "/Users/claire/Desktop/金融1.0/backend/src_python")
        import db
        import scorecard_share as sc
        with db.get_connection() as conn:
            data = sc.collect(conn)
        payload = json.dumps(
            {"msg_type": "text", "content": {"text": _build_text(data)}},
            ensure_ascii=False,
        ).encode("utf-8")
        req = urllib.request.Request(
            hook, data=payload, headers={"Content-Type": "application/json; charset=utf-8"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode("utf-8", "ignore")
        print(f"[push] 已推送 {resp.status} {body[:120]}")
        return 0
    except Exception as exc:
        print(f"[push] 失败（不影响生成）：{type(exc).__name__}: {str(exc)[:120]}")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
