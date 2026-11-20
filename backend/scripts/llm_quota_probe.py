#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""额度恢复探针 —— 解决「有额度了还一直挂着不动」这一半。

背景（2026-11-20）：402 欠费后按 scope 熔断 30 分钟。到期前引擎不会重试，
所以用户充值后最长要**干等 30 分钟**才恢复出货；期间队列不动、页面像死的。
反过来，探针也绝对不能变成新的烧钱点：所以

  · 只有在 LLM_QUOTA_DEAD.json 里存在**未到期**的 dead scope 时才发请求；
  · 每次只打 dedup 后的极轻量探测（max_tokens=5），3 个 key 组 ≈ 15 token；
  · 没有熔断 → 直接退出，0 请求 0 token；
  · 探测成功才清 scope，并（仅当引擎停在 pool-blocked 时）kickstart 续跑。

用法：
  python3 scripts/llm_quota_probe.py            # 正常巡检
  python3 scripts/llm_quota_probe.py --simulate-ok  # 演练：假装探测成功，验证解除链路
"""
import argparse
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src_python")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

STATE = os.path.expanduser("~/.trident/state/LLM_QUOTA_DEAD.json")
PROGRESS = os.path.join(os.path.dirname(HERE), "runtime", "ai_worker_progress.json")
LOG = os.path.expanduser("~/.trident/log/quota_probe.log")

# 主分析可用的通道别名（用于决定要不要解 'main' 这个 scope）
MAIN_ALIASES = {"minimax", "minimax1", "minimax2", "engine_openrouter", "openrouter"}


def _say(msg: str) -> None:
    print(msg, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(time.strftime("%F %T") + " " + msg + "\n")
    except Exception:
        pass


def _active_dead() -> dict:
    try:
        with open(STATE, "r", encoding="utf-8") as f:
            d = json.load(f) or {}
    except Exception:
        return {}
    now = time.time()
    return {k: v for k, v in d.items() if isinstance(v, dict) and float(v.get("until", 0)) > now}


def _engine_step() -> str:
    try:
        with open(PROGRESS, "r", encoding="utf-8") as f:
            return str((json.load(f) or {}).get("step") or "")
    except Exception:
        return ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--simulate-ok", action="store_true",
                    help="演练用：不真发请求，按「全部通道已恢复」处理")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()

    dead = _active_dead()
    if not dead:
        if not a.quiet:
            _say("[QUOTA-PROBE] 无未到期熔断 → 0 请求 0 token")
        return 0

    _say(f"[QUOTA-PROBE] 存在熔断：{sorted(dead)}，开始轻量探测（max_tokens=5）")
    if a.simulate_ok:
        ok_aliases = set(MAIN_ALIASES) | {"minimax-gate", "minimax-peer", "minimax-peer2"}
        detail = "simulate-ok"
    else:
        try:
            import model_probe
            d = model_probe._probe_model_channels_now()
            chans = d.get("channels") or []
            ok_aliases = {str(c.get("runtime_alias") or "") for c in chans if c.get("available")}
            detail = "probe n=%d ok=%d" % (len(chans), len(ok_aliases))
        except Exception as exc:
            _say(f"[QUOTA-PROBE] 探测异常，保持熔断：{type(exc).__name__}: {exc}")
            return 0

    try:
        import llm_budget
    except Exception as exc:
        _say(f"[QUOTA-PROBE] 载入 llm_budget 失败：{exc}")
        return 0

    cleared = []
    for scope in list(dead):
        need = scope.split(":", 1)[1] if scope.startswith("key:") else scope
        if need == "main":
            hit = bool(ok_aliases & MAIN_ALIASES)
        elif need == "gate":
            hit = any("gate" in x for x in ok_aliases)
        else:
            hit = need in ok_aliases
        if hit:
            try:
                llm_budget.clear_quota_dead(scope)
                cleared.append(scope)
            except Exception as exc:
                _say(f"[QUOTA-PROBE] 清 {scope} 失败：{exc}")

    if not cleared:
        _say(f"[QUOTA-PROBE] 仍未恢复（{detail}）→ 保持熔断，零产出但零空烧")
        return 0

    _say(f"[QUOTA-PROBE] ✅ 已解除熔断 {cleared}（{detail}）")
    # 只有引擎正停在 pool-blocked（没有在办批次）时才 kickstart，避免打断正常批次
    if _engine_step() == "pool-blocked":
        uid = os.getuid()
        try:
            subprocess.run(["launchctl", "kickstart", "-k", f"gui/{uid}/com.trident.engine"],
                           timeout=30, check=False)
            _say("[QUOTA-PROBE] 引擎原在 pool-blocked → 已重启续跑")
        except Exception as exc:
            _say(f"[QUOTA-PROBE] 重启引擎失败：{exc}")
    else:
        _say("[QUOTA-PROBE] 引擎不在 pool-blocked → 下一轮自动续跑，不动进程")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
