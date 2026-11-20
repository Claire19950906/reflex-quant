#!/usr/bin/env python3
"""决策的代码指纹 —— 让"这条决策是哪一代算法产出的"变成可查字段。

背景（2026-11-19）
-----------------
算法换代后，旧样本与新样本不可比；当时的做法是**清空全库**重新起算。
删库能拿到干净基线，代价是历史全部作废、只能靠 backups 里的快照留档。
根因不是"没有基线"，而是**决策行上没有任何标记说明它属于哪一代代码**：
`ai_decisions.strategy_version_id` 跟的是策略参数版本（重置前 1,274 条里
1,234 条都是 `1`），代码改了它不变。

指纹口径
--------
`<git短哈希>/<参数哈希>`，例如 `a4304e8/p3f2a1b`：
  · git 短哈希 —— 代码代次（工作区有未提交改动时追加 `+`，避免把脏工作区
    当成干净提交）
  · 参数哈希   —— 影响决策行为的关键环境变量的 8 位摘要（口径见 _PARAM_KEYS）

同一个指纹 = 同一代代码 + 同一套参数 → 指标可以直接合并统计；
指纹不同 → 应当分层看，不能混算。

设计约束
--------
· 只读、幂等、带缓存：一条进程只算一次，不因为取指纹而变慢。
· 任何一步失败都返回 `unknown`，绝不让它影响决策落库。
"""
from __future__ import annotations

import hashlib
import os
import subprocess
from functools import lru_cache
from pathlib import Path

# 只收"会改变决策结果"的开关。加新开关时必须同步加进这里，
# 否则换了参数还显示同一个指纹 —— 那比没有指纹更危险。
_PARAM_KEYS: tuple = (
    "REVIEW_INSIGHTS_RANK",
    "REVIEW_INSIGHTS_LIMIT",
    "REVIEW_INSIGHTS_MIN_SAMPLES",
    "INSIGHT_ATTR_HORIZON_H",
    "TRIDENT_TWO_STEP_LEANPROMPT",
    "TRIDENT_TWO_STEP_RESCUE_LIMIT",
    "NEWS_POOL_ROUTER",
    "AUDIT_GATE",
    "AUDIT_GATE_ENFORCE",
)

_ROOT = Path(__file__).resolve().parents[2]


@lru_cache(maxsize=1)
def _git_hash() -> str:
    """项目根的 git 短哈希；脏工作区追加 `+`，取不到返回空串。"""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(_ROOT), capture_output=True, text=True, timeout=5,
        )
        if out.returncode != 0:
            return ""
        h = (out.stdout or "").strip()
        if not h:
            return ""
        try:
            dirty = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=str(_ROOT), capture_output=True, text=True, timeout=8,
            )
            if dirty.returncode == 0 and (dirty.stdout or "").strip():
                h += "+"
        except Exception:
            pass
        return h
    except Exception:
        return ""


@lru_cache(maxsize=1)
def _env_file_values() -> dict:
    """backend/.env 里与决策相关的键值（只读解析，不污染进程环境）。

    为什么需要：`_param_hash` 原来只读 os.getenv —— 同一份代码，engine（
    launchd 里加载了 .env）和脚本（没加载）会算出两个不同的参数哈希，
    等于把同一代样本劈成两代。实测 2026-11-19：一次 -c 调用得 p1bff9471，
    导入 config 后得 p32890b4f。这里把 .env 作为缺省值补进来，进程环境
    仍然优先（launchd 覆盖 > .env > 空）。
    """
    out: dict = {}
    try:
        path = _ROOT / "backend" / ".env"
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    except Exception:
        pass
    return out


@lru_cache(maxsize=1)
def _param_hash() -> str:
    """关键参数的 8 位摘要；全为默认时也是稳定值。"""
    file_vals = _env_file_values()
    raw = "|".join(
        f"{k}={os.environ[k] if k in os.environ else file_vals.get(k, '')}"
        for k in _PARAM_KEYS
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]


@lru_cache(maxsize=1)
def fingerprint() -> str:
    """当前进程所属的代码代次标识，形如 `a4304e8/p3f2a1b`。

    取不到 git 时退化为 `nogit/p<hash>` —— 仍有参数维度，不返回空串。
    """
    g = _git_hash() or "nogit"
    return f"{g}/p{_param_hash()}"


def stamp(conn, decision_id) -> str:
    """把当前指纹写到指定决策行；返回写入值（失败返回空串）。

    为什么要做成函数：ai_decisions 有**两条** INSERT 路径（主分析链、
    spool 补写链），11-19 实测 4 条新决策全部来自 spool 链、指纹全空 ——
    只挂在主链上等于没做。统一从这里写，避免以后再漏第三条。
    """
    if not decision_id:
        return ""
    try:
        fp = fingerprint()
        conn.execute(
            "UPDATE ai_decisions SET code_fingerprint=? WHERE id=?",
            (fp, int(decision_id)),
        )
        return fp
    except Exception as exc:  # 绝不因为旁注信息拖垮落库
        print(f"[FINGERPRINT] 写入失败 id={decision_id}: "
              f"{type(exc).__name__}: {str(exc)[:80]}", flush=True)
        return ""


def reload_fingerprint() -> str:
    """清缓存后重算（测试/长驻进程热更新用）。"""
    _git_hash.cache_clear()
    _param_hash.cache_clear()
    fingerprint.cache_clear()
    return fingerprint()
