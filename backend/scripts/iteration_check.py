#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""iteration_check.py — 一次迭代的"先跑这个"入口 (2026-11-20, v500)

为什么有它：改完代码想确认"闭环没断 + 能力口径没坏 + 老功能没回归"，
过去要记三条命令、各自解读输出。这里合成一条，并把结论压成三行。

    python3 scripts/iteration_check.py              # 自测 + 能力验收 (~40s)
    python3 scripts/iteration_check.py --with-pytest # 再加回归 (数分钟)
    python3 scripts/iteration_check.py --json        # 机读

三条尺子各自负责什么：
  · offline_selftest   —— 交易闭环 (合成行情, 临时库): 27 项
  · capability_acceptance —— 26 项能力/契约, 分 PASS / FAIL / ⏳等数据
  · pytest (可选)      —— 404 个测试文件的回归

铁律：只跑只读脚本与测试；不启引擎、不写生产库。
退出码: 0 = 无硬失败 / 1 = 有失败 / 2 = 环境错误
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
PY = sys.executable or "python3"


def _run(cmd: list[str], timeout: int = 900) -> tuple[int, str, str]:
    env = dict(os.environ)
    env.setdefault("TRIDENT_BLOCK_PROD_WRITE", "1")   # 测试不得写生产库
    p = subprocess.run(cmd, cwd=str(BACKEND), capture_output=True, text=True,
                       timeout=timeout, env=env)
    return p.returncode, (p.stdout or ""), (p.stderr or "")


def run_selftest() -> dict:
    rc, stdout, stderr = _run([PY, "scripts/offline_selftest.py"], timeout=300)
    out = stdout + stderr
    m = re.search(r"共\s*(\d+)\s*项，通过\s*(\d+)，失败\s*(\d+)", out)
    total, passed, failed = (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else (0, 0, 0)
    fails = [ln.strip() for ln in out.splitlines() if ln.startswith("FAIL ")]
    return {"ok": rc == 0 and failed == 0, "total": total, "passed": passed,
            "failed": failed, "fail_lines": fails[:8], "raw_tail": out.splitlines()[-3:]}


def run_acceptance() -> dict:
    rc, stdout, stderr = _run(
        [PY, "src_python/capability_acceptance.py", "--no-http", "--json"], timeout=300)
    try:
        # 只解析 stdout —— stderr 里的 "[epoch] ..." 尾注会让整段 JSON 解析失败
        payload = json.loads(stdout[stdout.index("{"):])
    except Exception:
        return {"ok": False, "error": (stdout + stderr)[-300:]}
    s = payload.get("summary", {})
    hard = [r for r in payload.get("results", []) if r.get("status") in ("FAIL", "ERROR")]
    waiting = [r for r in payload.get("results", []) if r.get("status") == "INSUFFICIENT"]
    return {"ok": rc == 0 and not hard, "summary": s,
            "hard_fails": [{"id": r["id"], "name": r["name"],
                            "measure": r.get("measured", "")[:120]} for r in hard],
            "waiting": [{"id": r["id"], "need": r.get("need", ""), "have": r.get("have", "")}
                        for r in waiting]}


def run_capacity(days: int = 7) -> dict | None:
    """分析产能体检（只读聚合）：方向产出、完整率、队列、门卫漏斗、主通道短输出。

    "保产能、不限速、严禁零产出空烧 token" 需要一个每次迭代都能看的数字：
    方向性 0.71 条/日、主通道短输出 42%，这些比"通过了几项"更接近产能本身。
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = "src_python"
    env.setdefault("TRIDENT_BLOCK_PROD_WRITE", "1")
    p = subprocess.run([PY, "src_python/analysis_yield.py", "--days", str(days), "--json"],
                       cwd=str(BACKEND), capture_output=True, text=True,
                       timeout=180, env=env)
    out = p.stdout or ""
    if "{" not in out:
        return None
    try:
        d = json.loads(out[out.index("{"):])
    except Exception:
        return None
    rates = d.get("rates") or {}
    queue = d.get("queue") or {}
    funnel = d.get("funnel") or {}
    chans = d.get("channels") or []
    worst = None
    for c in chans:
        if (c.get("calls") or 0) >= 200 and (c.get("short_rate") or 0) >= 0.30:
            if worst is None or (c.get("short_rate") or 0) > (worst.get("short_rate") or 0):
                worst = c
    return {
        "directional_per_day": rates.get("directional_per_day"),
        "complete_rate": rates.get("complete_over_in_scope"),
        "queue_pending": queue.get("pending_actionable"),
        "queue_eta_h": queue.get("eta_drain_h"),
        "gate_filter_rate": funnel.get("filter_rate"),
        "worst_channel": None if not worst else {
            "model_id": worst.get("model_id"), "calls": worst.get("calls"),
            "short_rate": worst.get("short_rate"), "verdict": worst.get("verdict"),
        },
    }


def run_burn(hours: int = 1) -> dict | None:
    """空烧体检：近 N 小时"调了模型但零输出"的调用占比（按 purpose 拆）。

    "严禁零产出空烧 token" 需要一个能天天看的数：402/429 打满时，调用照发、
    tokens_out=0，日志里全是失败 —— 这就是空烧。这里只读 ai_call_stats。
    """
    import sqlite3
    db = BACKEND / "trident_event_bus.db"
    if not db.exists():
        return None
    now = int(time.time())
    try:
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT purpose, COUNT(*) n, SUM(tokens_out=0) z FROM ai_call_stats "
            "WHERE ts>=? GROUP BY 1 ORDER BY 2 DESC", (now - hours * 3600,)
        ).fetchall()
        tot = sum(r["n"] for r in rows)
        zero = sum((r["z"] or 0) for r in rows)
        hot = [{"purpose": r["purpose"], "n": r["n"], "zero": r["z"] or 0,
                "rate": round((r["z"] or 0) / r["n"], 4)} for r in rows
               if (r["n"] or 0) >= 20 and (r["z"] or 0) / max(1, r["n"]) >= 0.10]
        c.close()
        return {"hours": hours, "calls": tot, "zero": zero,
                "rate": round(zero / tot, 4) if tot else None, "hot": hot}
    except sqlite3.Error:
        return None


def print_burn(b: dict | None) -> None:
    if not b or not b.get("calls"):
        print("   （空烧体检无数据）")
        return
    icon = "🔥" if (b.get("rate") or 0) >= 0.10 or b.get("hot") else "✅"
    rtxt = f"{b['rate']*100:.1f}%" if b.get("rate") is not None else "n/a"
    print(f"{icon} 空烧({b['hours']}h)  {b['zero']}/{b['calls']} 次零输出（{rtxt}）")
    for h in (b.get("hot") or [])[:3]:
        print(f"     ⚠ {h['purpose']} 零输出 {h['zero']}/{h['n']}（{h['rate']*100:.0f}%）"
              f" — 调了模型没产出，先查配额/超时/think-only")


def _pct(v: object) -> str:
    """百分比容错：合成库/空库上 complete_rate、filter_rate 都是 None，
    原实现直接 v*100 会 TypeError，把整个体检脚本打崩（README 首屏就让人跑这条）。"""
    return f"{v*100:.1f}%" if isinstance(v, (int, float)) else "n/a"


def print_capacity(cap: dict | None) -> None:
    if not cap:
        print("   （产能体检不可用：analysis_yield 读取失败）")
        return
    d = cap.get("directional_per_day")
    cr = cap.get("complete_rate")
    q = cap.get("queue_pending")
    eta = cap.get("queue_eta_h")
    fr = cap.get("gate_filter_rate")
    worst = cap.get("worst_channel")
    dtxt = f"{d:.2f}" if isinstance(d, (int, float)) else "n/a"
    qtxt = (f"{q} 条/{eta}h" if isinstance(q, (int, float)) and isinstance(eta, (int, float))
            else "n/a")
    txt = (f"📈 产能   方向 {dtxt} 条/日 · "
           f"完整率 {_pct(cr)} · 队列 {qtxt} · 门卫否 {_pct(fr)}")
    print(txt)
    if worst:
        print(f"     ⚠ 主通道短输出 {_pct(worst.get('short_rate'))}（{worst.get('model_id')}，"
              f"{worst.get('calls')} 次） — {worst.get('verdict') or ''}；"
              f"这是空烧 token 的主要来源")


def run_pytest() -> dict:
    rc, stdout, stderr = _run([PY, "-m", "pytest", "tests/", "-q"], timeout=3600)
    out = stdout + stderr
    m = re.search(r"(\d+)\s+passed", out)
    f = re.search(r"(\d+)\s+failed", out)
    errs = re.search(r"(\d+)\s+error", out)
    return {"ok": rc == 0, "passed": int(m.group(1)) if m else 0,
            "failed": int(f.group(1)) if f else 0,
            "errors": int(errs.group(1)) if errs else 0,
            "raw_tail": out.splitlines()[-2:]}


def _demo_db_hint() -> None:
    """演示库上跑出来的 ❌ 大多是“没数据”，不是代码坏了。

    陌生人第一次按 README 跑这条时连的就是 backend/demo.db，
    如果不说明，他会看到 PASS 8 · FAIL 10 就以为项目是坏的。
    """
    db = os.environ.get("TRIDENT_DB_PATH", "")
    if "demo" in Path(db).name.lower():
        print("ℹ️  当前连的是合成演示库 demo.db：依赖真实样本的能力项会显示 ❌ / ⏳，")
        print("    那是「没数据」，不是代码坏了。要真结论请把 TRIDENT_DB_PATH 指向真实库。")
        print()


def main() -> int:
    _demo_db_hint()
    ap = argparse.ArgumentParser(description="一次迭代的统一自检入口")
    ap.add_argument("--with-pytest", action="store_true", help="加跑全量回归")
    ap.add_argument("--json", action="store_true", help="只输出 JSON")
    args = ap.parse_args()

    st = run_selftest()
    acc = run_acceptance()
    cap = run_capacity()
    burn = run_burn()
    pt = run_pytest() if args.with_pytest else None

    payload = {"selftest": st, "acceptance": acc, "capacity": cap, "burn": burn, "pytest": pt}
    hard_fail = (not st["ok"]) or (not acc.get("ok", False)) or (pt is not None and not pt["ok"])
    payload["verdict"] = "FAIL" if hard_fail else "OK"

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 1 if hard_fail else 0

    print("=" * 64)
    print("迭代自检 · 2026-11-20 口径")
    print("=" * 64)
    icon = "✅" if st["ok"] else "❌"
    print(f"{icon} 交易闭环   {st['passed']}/{st['total']} 项通过"
          + (f"（失败 {st['failed']}）" if st["failed"] else ""))
    for ln in st["fail_lines"]:
        print(f"     {ln}")

    s = acc.get("summary", {})
    icon = "✅" if acc.get("ok") else "❌"
    print(f"{icon} 能力验收   PASS {s.get('PASS',0)} · FAIL {s.get('FAIL',0)}"
          f" · ⏳等数据 {s.get('INSUFFICIENT',0)} · WARN {s.get('WARN',0)}"
          f" · SKIP {s.get('SKIP',0)}")
    for r in acc.get("hard_fails", []):
        print(f"     ❌ #{r['id']} {r['name']} — {r['measure']}")
    for r in acc.get("waiting", []):
        print(f"     ⏳ #{r['id']} 等 {r['need']}｜现有 {r['have']}")

    print_capacity(cap)
    print_burn(burn)

    if pt is not None:
        icon = "✅" if pt["ok"] else "❌"
        print(f"{icon} 回归测试   {pt['passed']} passed · {pt['failed']} failed · {pt['errors']} error")
    else:
        print("   （回归未跑：加 --with-pytest）")

    print("-" * 64)
    if hard_fail:
        print("结论: 有硬失败 —— 先修 ❌，⏳ 不影响。")
    else:
        print("结论: 闭环与口径都正常；⭐ 剩下的 ⏳ 是等真实数据，不要改代码。")
        if acc.get("waiting"):
            print("      数据进来后，照每一项的『等什么 / 现有』念，够门槛了再看『数据到了看』那句。")
    print("=" * 64)
    return 1 if hard_fail else 0


if __name__ == "__main__":
    sys.exit(main())
