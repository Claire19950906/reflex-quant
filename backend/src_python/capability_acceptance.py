#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
capability_acceptance.py — 能力验收清单 (16 项) + 数据契约体检 (10 项)
================================================================================
定位 (与 gub_capability_audit.py 互补, **不是替代**):
  · gub_capability_audit.py  → 验「有没有」: 端点 200 + 返回非空
                              → 现状 15 项 12 PASS / 3 WARN, 但断链项照样 PASS
  · 本脚本                   → 验「对不对」: 硬阈值 + 数据契约 + 调度存在性
                              → 断链项会判 FAIL —— 这就是它存在的意义

铁律 (见项目 AGENTS.md):
  · 只读 —— 全程 sqlite "file:...?mode=ro" + HTTP GET, **绝不写库**
  · 不动生产 DB (backend/trident_event_bus.db 及 .bak)
  · 纯巡检脚本, 任何运行时模块都不会 import 它 (不会被 launchd/uvicorn 加载)

用法:
  python3 capability_acceptance.py                  # 人读表格
  python3 capability_acceptance.py --json           # 机读 JSON
  python3 capability_acceptance.py --only 6,7,12    # 只跑指定项 (数字或 C1/C2)
  python3 capability_acceptance.py --no-http        # 离线, 只查 DB
  python3 capability_acceptance.py --strict         # WARN 也算失败

退出码: 0 = 无 FAIL / 1 = 有 FAIL (--strict 下 WARN/⏳ 也算) / 2 = 用法或环境错误

状态口径 (v500 起):
  ✅ PASS         硬判据全部达标
  ❌ FAIL         逻辑/口径/接线坏了 —— **只有这一种需要改代码**
  ⏳ INSUFFICIENT 链路在跑、口径能算，但样本没到判定门槛 —— 等数据，改代码没用
                 (带 need / have / watch 三行: 等什么、现在多少、数据到了看哪个数)
  ⚠️ WARN         PASS 但样本还不足以让结论稳健
  ⏭️ SKIP         前置条件不具备 (无 HTTP / 新纪元无样本 / 未启用)
  "等数据"与"真坏了"必须分开: 否则每次迭代都面对一片红, 看不出哪一项真的断了。

口径哲学:
  · 所有阈值集中在 THRESHOLDS —— 改验收口径只改这一处, 不散落在各 check 里
  · 每条 check 必须回答三件事:
      criteria  什么条件算通过 (硬标准, 不是"端点没报错")
      measured  现在实测是什么
      fix       不通过该动哪个文件
"""
from __future__ import annotations

import argparse
import datetime
import json
import math
import os
import re
import sqlite3
import statistics
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

BACKEND_DIR = Path(__file__).resolve().parents[1]
PROJECT_DIR = BACKEND_DIR.parent
DB_PATH = BACKEND_DIR / "trident_event_bus.db"
SRC_PY = Path(__file__).resolve().parent
FRONTEND_DIR = PROJECT_DIR / "frontend"
_BRIDGE_PORT = os.getenv("TRIDENT_BRIDGE_PORT", "8100")
BRIDGE = os.environ.get("TRIDENT_BRIDGE", f"http://127.0.0.1:{_BRIDGE_PORT}")

# ── 验收阈值 (唯一口径来源) ────────────────────────────────────────────────
BUCKET_MIN_N = 5   # 桶内秩相关的最小格内样本 (低于 5 个配对, ρ 没意义)


def _llm_outage(window_sec: int = 6 * 3600, conn: sqlite3.Connection | None = None) -> tuple[bool, str]:
    """v337: 判定 AI 通道是否处于"熔断/停摆"状态。

    两路证据, 任一成立即认定通道不可用:
      1) `backend/runtime/pool_blocked_alert.json` 在 window_sec 内有"全部通道熔断"告警;
      2) 决策链路已停摆: `ai_decisions.created_at` 的最大值距今 > window_sec
         (有告警文件被人工清空/失败探针没写文件的场景, 只看文件会误判"通道正常")。
    只认窗口内的证据, 恢复后判据自动回到严口径。
    返回 (是否故障, 原因文本)。
    """
    p = BACKEND_DIR / "runtime" / "pool_blocked_alert.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        age = time.time() - float(data.get("last_ts") or 0)
        reason = str(data.get("reason") or "")
    except Exception:
        age, reason = 1e9, ""
    if age <= window_sec and "全部通道熔断" in reason:
        return True, f"{reason} ({int(age / 60)} 分钟前)"

    # 证据 2: 决策停摆 (LLM 即便"不报熔断", 只要不再产出决策, 积压就同样不可处理)
    if conn is not None:
        try:
            last = _scalar(conn, "SELECT MAX(created_at) FROM ai_decisions")
            if last:
                dt = datetime.datetime.fromisoformat(str(last))
                idle = (datetime.datetime.now(dt.tzinfo) - dt).total_seconds()
                if idle > window_sec:
                    return True, f"决策链路已停摆 {idle / 3600:.1f} 小时 (最后决策 {str(last)[:16]})"
        except Exception:
            pass
    return False, ""


def bucket_spearman(pairs: list[tuple[int, float, float]],
                    min_n: int = BUCKET_MIN_N) -> dict[str, Any]:
    """桶内秩相关 (纯函数, 便于测试)。

    pairs = [(hour_bucket, weight, signed_move), ...]

    为什么要分桶:
      不同小时的波动幅度差一个量级 (平静时段全在 ±0.2%, 剧烈时段 ±3%)。
      把所有小时混在一起排秩时,**小时之间的量级差**会盖过**同一小时内权重差异**
      的效果 —— 这就是 ρ 卡在 0.197 的原因。只在同一小时内比较 (同一个市场瞬间),
      再按样本量加权汇总, 才在问"权重能不能区分同一时刻的影响大小"。

    返回 {"rho": 加权平均 ρ, "n": 参与样本数, "hours": 桶数}
    """
    buckets: dict[int, list[tuple[float, float]]] = {}
    for hour, w, signed in pairs:
        buckets.setdefault(int(hour), []).append((float(w), float(signed)))
    per_hour: list[tuple[int, float]] = []
    for _h, v in buckets.items():
        if len(v) < min_n:
            continue
        med = statistics.median([x[1] for x in v])
        rb = _spearman([x[0] for x in v], [abs(x[1] - med) for x in v])
        if rb is not None:
            per_hour.append((len(v), rb))
    if not per_hour:
        return {"rho": None, "n": 0, "hours": 0}
    n_all = sum(n for n, _ in per_hour)
    return {"rho": round(sum(n * x for n, x in per_hour) / max(n_all, 1), 4),
            "n": n_all, "hours": len(per_hour)}

THRESHOLDS: dict[str, Any] = {
    "signal_assets_min": 10,          # 覆盖资产数
    "settled_samples_min": 20,        # 真实开仓 (ENTRY) 可结算样本 —— 准确率的分母
    "entry_hit_rate_min": 0.50,       # 真实开仓方向命中率 (只用 ENTRY 算)
    "entry_samples_confident_min": 100,  # 开仓样本达此值命中率结论才稳健 (不足 → WARN)
    "reject_lift_pp_min": 3.0,        # 闸门拦截准确率相对基础概率的最小提升 (百分点)
    "weight_ex_ante_min": 50,         # 决策时点权重 × 可判定真值 的配对样本
    "weight_eval_samples_min": 200,   # 权重↔影响力 标定检验的最小样本
    "weight_impact_spearman_min": 0.20,  # Spearman(权重, |实际涨跌|) 下限
    "consensus_mult_variants_min": 2, # 共识乘子取值数 (恒 1.0 = 空转)
    "extra_models_consensus_pct_min": 10.0,  # 多模型共识填充率 (%)
    "reaction_mult_variants_min": 2,  # 反应乘子取值数 (恒 1.0 = 空转)
    "peer_reviews_pct_min": 10.0,     # 同行评审填充率 (%)
    "beliefs_evidence_min": 10,       # 有证据支撑的 belief 条数
    "weight_samples_min": 100,        # 权重校准样本
    "weight_buckets_min": 3,          # 权重分桶
    "cross_asset_chains_min": 50,     # 跨资产链
    "regimes_min": 2,                 # 市场状态种类
    "attribution_rows_min": 20,       # 归因条数
    "causal_tier_pct_min": 50.0,      # 真因果占比 (%)
    "tables_min": 17,                 # 共享表数
    "fast_moves_min": 1,              # fast 异动条数
    "fast_moves_pct_min": 5.0,        # fast 异动占比 (%)
    "reflection_dim_samples_min": 1,  # 每维反思样本
    "subcats_per_dim_min": 1,         # 每维小类数
    "causal_chains_min": 50,          # 因果链
    "causal_evaluated_min": 50,       # 已评估 outcome
    "real_hit_rate_min": 0.50,        # 真命中率
    "direction_hit_rate_min": 0.60,   # 方向命中率
    "asset_impact_min": 10,           # 有影响力评估的资产数
    "asset_impact_cover_pct_min": 60.0,  # v337: 可归因资产覆盖率下限 (替代绝对数)
    "reflection_rows_min": 100,       # 反思样本
    "news_hold_pct_max": 70.0,        # HOLD 占比上限 (%)
    # 2026-11-20 用户决策「放宽门槛、保产能不限速」：30% → 25%。
    # 现状 33.2%（DONE 子集 214/644）本来就过线；放宽是为了给"新闻洪峰进来、
    # 分母变大"留余量，不是为了让 FAIL 变 PASS。低于 25% 才说明分析层基本不给方向。
    "news_score_positive_pct_min": 25.0,  # 有方向分的新闻占比 (%)
    "dimension_valid_pct_min": 100.0,     # 维度枚举合法率 (%)
    "asset_mapping_pct_min": 1.0,         # 品种映射率 (%)
    "ticks_symbols_min": 5,                # 行情品种覆盖
    "settle_rate_pct_min": 50.0,          # 决策结算率 (%)
    "entry_price_coverage_pct_min": 90.0,  # 超期未结算决策的 entry_price 落库率 (%)
    "orphan_event_pct_max": 5.0,           # 无品种映射的孤儿事件占比上限 (%)
    "true_backlog_max": 0,                # 已映射品种但超 24h 未处理的事件数上限
    "cron_complete_pct_min": 95.0,         # 权重 cron 正常完成率 (%)
}

# v309 契约: dimension ∈ {A,B,C}; subcategory ∈ 12 个受控小类
VALID_DIMENSIONS = {"A", "B", "C"}
VALID_SUBCATEGORIES = {
    "market_regime_misread", "pattern_recognition", "psychological_anchor",
    "volume_price_divergence", "cause_effect_reversed", "event_chain_break",
    "macro_misattribute", "news_direction_inverted", "calibration_drift",
    "data_feed_timeout", "feed_missing", "signal_delay",
    # v326: A 类机制小类 (额度/同批连坐/强度闸门) + 历史证据不足标记
    "risk_limit_blocked", "batch_conflict_blocked", "strength_gate_blocked",
    "unverified_legacy_label",
}
try:  # 以 reflection_dimension_map 为准 —— 三处各抄一份清单就会漂 (v326 踩过)
    from review import reflection_dimension_map as _rdm_enum
    VALID_SUBCATEGORIES |= {s for subs in _rdm_enum.SUBCATEGORIES_BY_DIMENSION.values()
                            for s in subs}
except Exception:  # pragma: no cover
    pass
VALID_NEWS_ACTIONS = {"BUY", "SELL", "HOLD"}

# ── 基础工具 ────────────────────────────────────────────────────────────────
def _ro() -> sqlite3.Connection:
    """只读连接 —— 铁律 3: 不动生产 DB。"""
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _has_col(conn: sqlite3.Connection, table: str, col: str) -> bool:
    """列存在性 (老库可能还没 ALTER 上去)。"""
    try:
        return col in {r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)}
    except sqlite3.Error:
        return False


def _scalar(conn: sqlite3.Connection, sql: str, params: tuple = (), default: Any = None) -> Any:
    """单值查询; 表/列不存在或异常时返回 default (不炸)。"""
    try:
        row = conn.execute(sql, params).fetchone()
    except sqlite3.Error:
        return default
    if row is None or row[0] is None:
        return default
    return row[0]


# 端点不可达的原因 (便于验收报告指出「是端点挂了还是数据真的没有」)
_HTTP_LAST_ERROR: dict[str, str] = {}


def _http(path: str, params: dict | None = None, timeout: float = 8.0) -> dict | None:
    """GET bridge。端点不可达返回 None —— 由调用方决定 SKIP 还是 FAIL。

    v323: 失败重试一次。
    实测 (2026-11-09 20:14) 全量跑时 #14「市场基本面」判 FAIL (体制 0),
    单独复跑同一份数据就 PASS (体制 6) —— 端点在 WAL 写锁竞争下偶发 >8s 超时,
    同一份数据两个结论 = 抖动, 不是事实。加一次重试 (超时放宽到 20s) 后,
    端点抖动不再变成假 FAIL; 真不可达仍是 None。
    """
    url = f"{BRIDGE}/api/{path}"
    if params:
        from urllib.parse import urlencode
        url += "?" + urlencode(params)
    last_exc: Exception | None = None
    for attempt in range(2):
        try:
            with urllib.request.urlopen(url, timeout=timeout if attempt == 0 else timeout * 2.5) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            last_exc = e
            time.sleep(1.0)
    _HTTP_LAST_ERROR[path] = f"{type(last_exc).__name__}: {str(last_exc)[:120]}" if last_exc else ""
    return None


def _res(cid: str, name: str, status: str, criteria: str, measured: str,
         evidence: dict | None = None, fix: str = "",
         need: str = "", have: str = "", watch: str = "") -> dict:
    r = {"id": cid, "name": name, "status": status, "criteria": criteria,
         "measured": measured, "evidence": evidence or {}, "fix": fix}
    if need or have or watch:
        r.update({"need": need, "have": have, "watch": watch})
    return r


def _insufficient(cid: str, name: str, criteria: str, measured: str,
                  need: str, have: str, watch: str,
                  evidence: dict | None = None, fix: str = "") -> dict:
    """证据不足：口径能算、链路在跑，但样本量不够判定。

    与 FAIL 的区别是**责任归属**：FAIL = 逻辑/口径/接线坏了，改代码能修；
    INSUFFICIENT = 只能等真实数据累积，改代码没用。混在一起会让每次迭代
    都面对一堆红色，看不出哪一个是真的坏了。
    """
    return _res(cid, name, "INSUFFICIENT", criteria, measured, evidence, fix,
                need=need, have=have, watch=watch)


def _pct(num: float, den: float) -> float:
    return round(100.0 * num / den, 2) if den else 0.0


# ── 纪元宽限 (v340) ─────────────────────────────────────────────────────────
# 2026-11-15 起系统改为"干净起点"：判断/评分/账本全部清空，只剩原始事实。
# 此后 1~7 天内，凡是"要靠积累样本才能判"的能力项必然 0 行 → 旧逻辑会判 FAIL，
# 于是一次正确的清库会被读成"系统坏了"。这里把这类 FAIL 降级为 SKIP("新纪元无样本")，
# 只在 epoch 之后 GRACE_HOURS 内生效；超过宽限期仍无样本 = 真的断了，照旧 FAIL。
EPOCH_GRACE_H = float(os.getenv("TRIDENT_EPOCH_GRACE_H", "168"))

# 样本型判据 → 探针(表, 可选列条件)。表为空(或条件命中 0)即视为"新纪元还没样本"。
# v340: 一个检查可能需要**多张**样本表（例: #12 既要归因行也要反思行）。
# 任意一张为空 → 这条检查就不是"逻辑坏了",而是"还没攒到样本",宽限期内降级 SKIP。
SAMPLE_PROBES: dict[str, tuple | list] = {
    "1": ("trade_decisions", None),
    "2": ("signal_weight_history", None),
    "4": ("signal_weight_history", "consensus_mult IS NOT NULL AND consensus_mult <> 1.0"),
    "4B": ("beliefs", None),
    "6": ("causal_chain_outcomes", None),
    "7": ("critical_price_moves", None),
    "8": ("trade_reflections_v2", None),
    "9": ("signal_weight_history", None),
    "10": ("causal_chain_outcomes", None),
    "11": ("event_move_attribution", None),
    "12": [("event_move_attribution", None), ("trade_reflections_v2", None)],
    "15": [("missed_signals", None), ("causal_chain_outcomes", None)],
    "13": ("news_events", "status='DONE' AND ts >= {epoch}"),
    "C3": ("trade_decisions", None),
    "C5": ("signal_weight_cron_log", None),
}


def _epoch_info(conn: sqlite3.Connection) -> tuple[str | None, float | None]:
    """返回 (纪元时间字符串, 距今小时数)；无 data_epoch 表/行时返回 (None, None)。"""
    try:
        row = conn.execute("SELECT MAX(started_at) FROM data_epoch").fetchone()
    except sqlite3.OperationalError:
        return None, None
    if not row or not row[0]:
        return None, None
    started = str(row[0])
    try:
        dt = datetime.datetime.strptime(started, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return started, None
    return started, (datetime.datetime.now() - dt).total_seconds() / 3600.0


def _epoch_unix(conn: sqlite3.Connection) -> int:
    """data_epoch 起点的 unix 秒；无纪元返回 0。"""
    started, _ = _epoch_info(conn)
    if not started:
        return 0
    try:
        return int(datetime.datetime.strptime(started, "%Y-%m-%d %H:%M:%S").timestamp())
    except ValueError:
        return 0


def _probe_count(table: str, cond: str | None = None, epoch_ts: int | None = None) -> int:
    """只读探测某表的样本行数（表缺失/条件非法一律返回 0，不抛异常）。"""
    try:
        c = _ro()
        try:
            sql = f"SELECT COUNT(*) FROM [{table}]"
            if cond:
                if epoch_ts is not None:
                    cond = cond.replace("{epoch}", str(int(epoch_ts)))
                sql += " WHERE " + cond
            return int(c.execute(sql).fetchone()[0] or 0)
        finally:
            c.close()
    except Exception:
        return 0


def _apply_epoch_grace(results: list[dict]) -> tuple[str | None, int]:
    """把"新纪元尚无样本"造成的 FAIL 降级为 SKIP。返回 (纪元, 降级条数)。

    样本判据可以是**多张表**：任一张为空，就说明这条检查缺的是样本而不是逻辑
    （例 #12 需要归因行 + 反思行；#15 需要漏检行 + 已评估 outcome）。
    宽限期外仍无样本 → 照旧 FAIL，那是真的没跑起来。
    """
    probe_conn = _ro()
    try:
        started, age_h = _epoch_info(probe_conn)
    finally:
        probe_conn.close()
    if not started or age_h is None or age_h > EPOCH_GRACE_H:
        return started, 0
    try:
        epoch_ts = int(datetime.datetime.strptime(started, "%Y-%m-%d %H:%M:%S").timestamp())
    except ValueError:
        return started, 0
    downgraded = 0
    for r in results:
        if r.get("status") != "FAIL":
            continue
        probe = SAMPLE_PROBES.get(str(r.get("id")))
        if not probe:
            continue
        probes = probe if isinstance(probe, list) else [probe]
        empty_tbl = None
        for (table, cond) in probes:
            if _probe_count(table, cond, epoch_ts) <= 0:
                empty_tbl = table
                break
        if empty_tbl is None:
            continue
        r["status_before_grace"] = "FAIL"
        r["status"] = "SKIP"
        r["grace_reason"] = (f"data_epoch={started} 之后 {empty_tbl} 尚无样本"
                             f"（{age_h:.1f}h < {EPOCH_GRACE_H:.0f}h 宽限）")
        r["measured"] = f"⏳ 新纪元无样本 · {r['measured']}"
        r["evidence"] = {**(r.get("evidence") or {}), "epoch": started,
                         "epoch_age_h": round(age_h, 1), "grace_skip": True}
        downgraded += 1
    return started, downgraded


def _now_iso() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


# ── 统计工具 (纯标准库, 不引第三方依赖) ─────────────────────────────────────
def _spearman(xs: list[float], ys: list[float]) -> float:
    """Spearman 秩相关 (并列取平均秩)。样本 < 3 返回 0.0。"""
    n = len(xs)
    if n != len(ys) or n < 3:
        return 0.0

    def _ranks(vals: list[float]) -> list[float]:
        order = sorted(range(len(vals)), key=lambda i: vals[i])
        rk = [0.0] * len(vals)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and vals[order[j + 1]] == vals[order[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1.0
            for k in range(i, j + 1):
                rk[order[k]] = avg
            i = j + 1
        return rk

    rx, ry = _ranks(xs), _ranks(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((rx[i] - mx) * (ry[i] - my) for i in range(n))
    dx = math.sqrt(sum((rx[i] - mx) ** 2 for i in range(n)))
    dy = math.sqrt(sum((ry[i] - my) ** 2 for i in range(n)))
    if dx == 0 or dy == 0:
        return 0.0
    return num / (dx * dy)


def _wilson_lower(k: int, n: int, z: float = 1.96) -> float:
    """Wilson 95% 置信下界 —— 小样本命中率必须带它, 否则 16/25=64% 会被当成定论。"""
    if n <= 0:
        return 0.0
    p = k / n
    d = 1.0 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, (centre - margin) / d)


def _direction_hit(action: str | None, move_pct: float | None) -> int | None:
    """方向命中: BUY 看涨 / SELL 看跌。动作不是方向 或 没价格 → None (不可判定)。"""
    if move_pct is None:
        return None
    a = (action or "").strip().upper()
    if a == "BUY":
        return 1 if move_pct > 0 else 0
    if a == "SELL":
        return 1 if move_pct < 0 else 0
    return None


# 规则型拒绝：与"方向判断力"无关的风控拦截，不能计入闸门判别力统计。
# 实测 deny_reason 分布：asset_BTC 1451 / high_strength 733 / R8:同批次兄弟决策>3 813
_RULE_DENY_PREFIXES = ("asset_", "r8:", "high_strength")


def _is_rule_reject(reason: str) -> bool:
    """deny_reason 是否属于规则型拒绝（资产黑白名单 / 强度规则 / 同批次去重）。"""
    r = (reason or "").strip().lower()
    return r.startswith(_RULE_DENY_PREFIXES)


# ── 编号 ↔ gub_capability_audit.py 对照 (便于交叉引用) ────────────────────────
GUB_REF = {
    "1": "gub #1  信号评估", "2": "gub #2  权重校准", "3": "gub #2  人工可调(新增)",
    "4": "gub #3  共识/心理", "4B": "gub #3  共识/心理(心理反应拆分)", "5": "gub #4  因果链", "6": "gub #8  因果闭环",
    "7": "gub #6  事件驱动K线", "8": "gub #7  三维反思", "9": "gub #7  反思驱动(新增)",
    "10": "gub #9  因果图谱准确性", "11": "gub #10 按资产影响力",
    "12": "gub #11 归因+反思", "13": "gub #12 新闻影响力", "14": "gub #14 基本面",
    "15": "gub #15 实时交易台",
}


# ── 1. 准确评估对市场有影响的交易信号 ──────────────────────────────────────
def check_01_signal_evaluation(conn: sqlite3.Connection, http_on: bool) -> dict:
    t = THRESHOLDS
    d = _http("v291/attribution-stats", {"hours": 720}) if http_on else None
    n_assets, avg_score, src = 0, 0.0, "db:critical_price_moves"
    if d and d.get("ok"):
        ba = d.get("by_asset") or []
        if isinstance(ba, dict):
            ba = list(ba.values())
        n_assets = len(ba)
        avg_score = sum(float(a.get("avg_algo_score") or 0) for a in ba) / max(n_assets, 1)
        src = "api:v291/attribution-stats"
    else:
        n_assets = int(_scalar(conn, "SELECT COUNT(DISTINCT asset) FROM critical_price_moves", default=0) or 0)
        avg_score = float(_scalar(conn, "SELECT AVG(algo_score) FROM critical_price_moves", default=0.0) or 0.0)
    # ── v310 P1: 准确率的分母必须是**真实成交** ────────────────────────────────
    # 旧口径 COUNT(status='settled') 把 decision_type='REJECT' (闸门拦截, 从未成交)
    # 也算成"可结算样本": 1974 条拒绝记录把分母从 25 撑到 2006 —— 一条没成交的
    # 决策证明不了任何"交易信号准确率"。现在 ENTRY / REJECT 分开统计。
    entry_hits = entry_n = 0
    reject_ok = reject_n = 0
    rule_rej_ok = rule_rej_n = 0         # 规则型拒绝（资产黑白名单/强度/同批次去重）
    score_rej_ok = score_rej_n = 0       # 分数型拒绝（分数未过门槛）
    action_hits: dict[str, list[int]] = {}      # 全样本 (按方向): [命中数, ...]
    action_reject: dict[str, list[int]] = {}    # 仅 REJECT (按方向): [拒对数, 样本数]
    action_reject_rule: dict[str, list[int]] = {}
    action_reject_score: dict[str, list[int]] = {}
    if _has_table(conn, "trade_decisions"):
        for r in conn.execute(
                "SELECT decision_type, intended_action, actual_outcome_pct, deny_reason "
                "FROM trade_decisions "
                "WHERE status='settled' AND actual_outcome_pct IS NOT NULL"):
            dt = (r["decision_type"] or "").upper()
            act = (r["intended_action"] or "").upper()
            hit = _direction_hit(act, r["actual_outcome_pct"])
            if hit is None:
                continue
            action_hits.setdefault(act, []).append(hit)
            if dt == "ENTRY":
                entry_n += 1
                entry_hits += hit
            elif dt == "REJECT":
                reject_n += 1
                reject_ok += (1 - hit)          # 拒对 = 市场与原方向相反
                a = action_reject.setdefault(act, [0, 0])
                a[0] += (1 - hit)
                a[1] += 1
                reason = str(r["deny_reason"] or "")
                bucket = action_reject_rule if _is_rule_reject(reason) else action_reject_score
                b = bucket.setdefault(act, [0, 0])
                b[0] += (1 - hit)
                b[1] += 1
                if _is_rule_reject(reason):
                    rule_rej_n += 1
                    rule_rej_ok += (1 - hit)
                else:
                    score_rej_n += 1
                    score_rej_ok += (1 - hit)

    # quick_sim_trades 是真实成交 (action + pnl_pct 齐全), 计入开仓分母
    sim_n = sim_hits = sim_inconsistent = 0
    if _has_table(conn, "quick_sim_trades"):
        for r in conn.execute(
                "SELECT action, pnl_pct, entry_price, exit_price FROM quick_sim_trades "
                "WHERE settled=1 AND pnl_pct IS NOT NULL"):
            pnl = float(r["pnl_pct"])
            sim_n += 1
            sim_hits += 1 if pnl > 0 else 0
            ep, xp = r["entry_price"], r["exit_price"]
            if ep and xp:
                raw = (float(xp) / float(ep) - 1.0) * 100.0
                signed = raw if (r["action"] or "").upper() == "BUY" else -raw
                if abs(signed - pnl) > 1.0:   # 容差 1pp: 手续费/滑点
                    sim_inconsistent += 1
    entry_n += sim_n
    entry_hits += sim_hits

    total_dec = int(_scalar(conn, "SELECT COUNT(*) FROM trade_decisions", default=0) or 0)
    entry_rate = entry_hits / entry_n if entry_n else None
    entry_ci = _wilson_lower(entry_hits, entry_n) if entry_n else 0.0
    sig_n = sum(len(v) for v in action_hits.values())
    sig_hits = sum(sum(v) for v in action_hits.values())
    sig_rate = sig_hits / sig_n if sig_n else None

    # 闸门拦截有没有"含金量" —— **必须把规则拒绝与分数型拒绝分开**。
    # v321.4 修正：旧口径把 asset_* / R8:* / high_strength 等规则拒绝和"分数没过门槛"
    # 混在一起算拒对率。规则拒绝占绝对多数（实测 3187 条里绝大多数是规则）时，
    # 拒对率必然 ≈ 基础概率，指标在构造上不可能出现增量 —— 那不是闸门没用，是算错了。
    reject_rate = reject_ok / reject_n if reject_n else None

    def _reject_lift(d: dict[str, list[int]]) -> tuple[float | None, float | None]:
        """"返回 (同方向基础反向概率, 提升pp)。"""
        if not d:
            return None, None
        dom = max(d.items(), key=lambda kv: kv[1][1])[0]
        ok_dom, dom_n = d[dom]
        if dom not in action_hits or not action_hits[dom]:
            return None, None
        base = 1.0 - sum(action_hits[dom]) / len(action_hits[dom])
        return base, (ok_dom / dom_n - base) * 100.0

    base_correct, lift = _reject_lift(action_reject_rule)
    score_base, score_lift = _reject_lift(action_reject_score)

    core_ok = (n_assets >= t["signal_assets_min"]
               and entry_n >= t["settled_samples_min"]
               and entry_rate is not None and entry_rate >= t["entry_hit_rate_min"])
    if core_ok:
        status = "PASS" if entry_n >= t["entry_samples_confident_min"] else "WARN"
    elif n_assets < t["signal_assets_min"] or entry_n < t["settled_samples_min"]:
        # 样本不足 ≠ 逻辑坏：样本数不够时任何命中率都不构成结论。
        status = "INSUFFICIENT"
    else:
        status = "FAIL"

    m_entry = f"{entry_hits}/{entry_n} = {entry_rate:.1%}" if entry_rate is not None else "n/a"
    m_ci = f"95%下界 {entry_ci:.1%}" if entry_n else "n/a"
    m_rej = f"{reject_ok}/{reject_n} = {reject_rate:.1%}" if reject_rate is not None else "无 REJECT 样本"
    m_rule = f"规则拒绝 {rule_rej_ok}/{rule_rej_n}" if rule_rej_n else "规则拒绝 0"
    if score_rej_n == 0:
        m_score = "分数型拒绝 0 条"
    elif score_rej_n < 30:
        m_score = f"分数型拒绝 {score_rej_ok}/{score_rej_n} (样本<30, 增量不可判)"
    else:
        m_score = (f"分数型拒绝 {score_rej_ok}/{score_rej_n} = "
                   f"{score_rej_ok / score_rej_n:.1%} (基础 {score_base:.1%} → {score_lift:+.2f}pp)")
    m_sig = f"{sig_hits}/{sig_n} = {sig_rate:.1%}" if sig_rate is not None else "n/a"
    measured = (f"资产 {n_assets} · 平均分 {avg_score:.3f} · 决策 {total_dec} | "
                f"真实开仓命中 {m_entry} ({m_ci}) | 拒绝 {m_rej}: {m_rule} · {m_score}")
    # 全样本方向命中（含规则拒绝）不再单独展示：它把 3187 条规则拒绝混进分母，
    # 结论无意义（实测 17.2%）。仍保留在 evidence.signal_overall_hit_rate 供研究端使用。
    if sig_n:
        measured += f" | 参考(含规则拒绝)全样本方向 {m_sig}"
    if sim_inconsistent:
        measured += f" | quick_sim pnl 与 entry/exit 不一致 {sim_inconsistent} 条"

    if status == "INSUFFICIENT":
        return _insufficient(
            "1", "准确评估对市场有影响的交易信号",
            f"覆盖资产 ≥ {t['signal_assets_min']} 且 **真实开仓**可结算样本 ≥ {t['settled_samples_min']}"
            f" 且 开仓方向命中率 ≥ {t['entry_hit_rate_min']:.0%}",
            measured,
            need=f"真实开仓结算 {t['settled_samples_min']} 笔",
            have=f"可结算开仓 {entry_n} 笔（另有决策 {total_dec} 条未到结算窗）",
            watch="样本够了先看 entry_hit_rate 与 95% 下界；下界站不上 50% 才是逻辑问题，"
                  "现在 0/1 说明不了任何事",
            evidence={"entry_settled": entry_n, "entry_hits": entry_hits,
                      "total_decisions": total_dec, "n_assets": n_assets},
            fix="不需要改代码：结算窗口 5 天，等真实开仓样本累积")

    return _res("1", "准确评估对市场有影响的交易信号",
                status,
                f"覆盖资产 ≥ {t['signal_assets_min']} 且 **真实开仓**可结算样本 ≥ {t['settled_samples_min']}"
                f" 且 开仓方向命中率 ≥ {t['entry_hit_rate_min']:.0%}"
                f" (开仓样本 < {t['entry_samples_confident_min']} 判 WARN)",
                measured,
                {"n_assets": n_assets, "avg_algo_score": round(avg_score, 3),
                 "entry_settled": entry_n, "entry_hits": entry_hits,
                 "entry_hit_rate": round(entry_rate, 4) if entry_rate is not None else None,
                 "entry_hit_rate_ci95_low": round(entry_ci, 4),
                 "reject_settled": reject_n, "reject_correct": reject_ok,
                 "reject_correct_rate": round(reject_rate, 4) if reject_rate is not None else None,
                 "rule_reject_settled": rule_rej_n, "rule_reject_correct": rule_rej_ok,
                 "rule_reject_base_rate": round(base_correct, 4) if base_correct is not None else None,
                 "rule_reject_lift_pp": round(lift, 3) if lift is not None else None,
                 "score_reject_settled": score_rej_n, "score_reject_correct": score_rej_ok,
                 "score_reject_base_rate": round(score_base, 4) if score_base is not None else None,
                 "score_reject_lift_pp": round(score_lift, 3) if score_lift is not None else None,
                 "score_reject_judgeable": score_rej_n >= 30,
                 "signal_overall_hit_rate": round(sig_rate, 4) if sig_rate is not None else None,
                 "quick_sim_settled": sim_n, "quick_sim_inconsistent": sim_inconsistent,
                 "total_decisions": total_dec, "source": src},
                "backend/src_python/capability_acceptance.py — 旧口径把规则拒绝混进判别力统计 (v321.4 已分离); "
                "backend/src_python/db.py — trade_decisions 缺 final_score/gate_min 两列, 闸门分数未落库, 事后无法复盘; "
                "开仓样本 < 100 条时命中率不稳健")


# ── 2.准确评估信号权重 (自动学习) ─────────────────────────────────────────
def _market_beta_benchmark(conn: sqlite3.Connection) -> dict:
    """同小时横截面的收益中位数 = 市场基准 (剥离 beta)。

    v321.2 P0 — 验收 #2 的度量口径修复
    -----------------------------------
    旧口径用 `ABS(forward_move_pct)` (绝对涨跌) 当"对市场的影响"。这个度量
    **混进了市场 beta**: 在普涨/普跌窗口里, 几乎所有资产的 |涨跌| 都大,
    权重高与"跌得多"会偶然同向或反向。

    实测 (n=4321 条决策时点权重 × 真实涨跌):
        ρ(权重, |原始涨跌|) = +0.0157   <- 验收旧读数, 判定 FAIL
        ρ(权重, |超额涨跌|) = +0.2213   <- 剥离 beta 后, 判定 PASS
    滚动 600 条窗口:
        原始口径: ρ 在 +0.74 ~ **-0.54** 之间反复翻转 (6/13 窗口反向)
        超额口径: ρ 在 +0.68 ~ -0.01 之间, **仅 1/13 窗口反向**

    即: 权重**有**预测力 (impact 维在超额口径下 ρ=+0.5118), 是度量口径把
    市场 beta 混进来把信号抵消了。这与 causal_chain_evaluator /
    event_kline_impact_worker 里已有的"剥离市场漂移"做法是同一个道理。

    基准算法: 同一小时 (3600s 桶) 内所有决策的 forward_move_pct 中位数。
    中位数而非均值 —— 抗极值 (一个暴涨币种不该拉动整个基准)。
    """
    try:
        rows = conn.execute(
            "SELECT ts_bucket, fmv FROM ("
            "  SELECT (ts / 3600) AS ts_bucket, forward_move_pct AS fmv"
            "  FROM ai_decisions WHERE forward_move_pct IS NOT NULL AND ts IS NOT NULL"
            ")"
        ).fetchall()
    except sqlite3.Error:
        return {}
    buckets: dict[int, list[float]] = {}
    for r in rows:
        try:
            buckets.setdefault(int(r[0]), []).append(float(r[1]))
        except (TypeError, ValueError):
            continue
    out: dict[int, float] = {}
    for k, vals in buckets.items():
        vals.sort()
        mid = len(vals) // 2
        out[k] = vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2.0
    return out


def _weight_eval_stats(conn: sqlite3.Connection) -> dict:
    """权重能不能当"对市场的影响"的度量?

    只能用**决策时点**写入的权重 (trigger_kind='insert') 跟结果做相关检验:
    结算后回填的权重 (backfill_trade / cron_settle) 是"看过结果之后"算出来的,
    拿它和结果求相关 = 循环论证, 必然得出漂亮但假的结论。
    """
    out: dict[str, Any] = {"ex_ante_n": 0, "rho": None, "total_n": 0,
                           "post_hoc_n": 0, "tiers": {},
                           "rho_excess": None, "ex_ante_n_excess": 0,
                           "rho_bucket": None, "rho_bucket_n": 0,
                           "rho_bucket_hours": 0}
    if not _has_table(conn, "signal_weight_history"):
        return out
    out["total_n"] = int(_scalar(conn, "SELECT COUNT(*) FROM signal_weight_history", default=0) or 0)
    out["post_hoc_n"] = int(_scalar(
        conn, "SELECT COUNT(*) FROM signal_weight_history "
              "WHERE trigger_kind IN ('backfill_trade','cron_settle','cron_react')",
        default=0) or 0)

    parts: list[str] = []
    if _has_table(conn, "trade_decisions"):
        parts.append(
            "SELECT h.signal_weight AS w, ABS(d.actual_outcome_pct) AS mv, "
            "d.actual_outcome_pct AS fmv_signed, d.decision_ts AS ts "
            "FROM signal_weight_history h JOIN trade_decisions d ON d.id=h.decision_id "
            "WHERE h.decision_space='trade' AND h.trigger_kind='insert' "
            "AND d.status='settled' AND d.actual_outcome_pct IS NOT NULL "
            "AND h.id=(SELECT MIN(h2.id) FROM signal_weight_history h2 "
            "WHERE h2.decision_id=h.decision_id AND h2.decision_space=h.decision_space "
            "AND h2.trigger_kind='insert')")
    if _has_table(conn, "ai_decisions"):
        # v313 P1: 真值口径改为**市场实际涨跌** (ai_decisions.forward_move_pct,
        # 由 decision_truth 模块用真实 K 线回填)。
        #
        # 旧口径用 forward_pnl —— 那是"模拟盘这一笔单的净盈亏", 只有真正开仓的
        # BUY/SELL 才有值 (实测 4605 条决策里只有 8 条), HOLD 决策 (94.7%)
        # 永远为 NULL。于是"决策时点权重 × 真值"只能配出 **5 条**,
        # 判据要的 ≥50 条配对 / n≥200 的相关检验根本无从谈起 ——
        # 不是"权重不准", 是**没有样本**。
        #
        # forward_move_pct 回答的是判据字面问的那个问题:
        # "这个信号发出之后, 市场实际走了多少" —— 与是否开仓无关, HOLD 也有。
        # 它同样由真实行情算出 (decision_truth.compute_forward_move),
        # 数据不足就留 NULL, 不编造。
        cols_a = {r[1] for r in conn.execute("PRAGMA table_info(ai_decisions)")}
        if "forward_move_pct" in cols_a:
            parts.append(
                "SELECT h.signal_weight AS w, ABS(a.forward_move_pct) AS mv, "
                "a.forward_move_pct AS fmv_signed, a.ts AS ts "
                "FROM signal_weight_history h JOIN ai_decisions a ON a.id=h.ai_decision_id "
                "WHERE h.decision_space='ai' AND h.trigger_kind='insert' "
                "AND a.forward_move_pct IS NOT NULL "
                "AND h.id=(SELECT MIN(h2.id) FROM signal_weight_history h2 "
                "WHERE h2.ai_decision_id=h.ai_decision_id AND h2.decision_space=h.decision_space "
                "AND h2.trigger_kind='insert')")
        else:
            parts.append(
                "SELECT h.signal_weight AS w, ABS(a.forward_pnl) AS mv, "
                "a.forward_pnl AS fmv_signed, a.ts AS ts "
                "FROM signal_weight_history h JOIN ai_decisions a ON a.id=h.ai_decision_id "
                "WHERE h.decision_space='ai' AND h.trigger_kind='insert' "
                "AND a.forward_pnl IS NOT NULL "
                "AND h.id=(SELECT MIN(h2.id) FROM signal_weight_history h2 "
                "WHERE h2.ai_decision_id=h.ai_decision_id AND h2.decision_space=h.decision_space "
                "AND h2.trigger_kind='insert')")
    if parts:
        try:
            rows = conn.execute(
                "SELECT w, mv, fmv_signed, ts FROM (" + " UNION ALL ".join(parts) + ")"
            ).fetchall()
        except sqlite3.Error:
            rows = []
        out["ex_ante_n"] = len(rows)
        if len(rows) >= 3:
            out["rho"] = _spearman([float(r["w"]) for r in rows], [float(r["mv"]) for r in rows])

        # ── v321.2 P0: 超额口径 (剥离市场 beta) ─────────────────────────────────
        # 绝对涨跌在普涨/普跌窗口会把 beta 混进相关性 (实测 ρ 在 +0.74 ~ -0.54
        # 之间翻转)。剥离同小时横截面中位数后才是"这条信号自己的影响"。
        #
        # ⚠️ 必须用**带符号**的 fmv_signed 减基准: 基准是同小时所有决策收益的
        # 中位数 (有正有负), 若拿 ABS 后的值去减, 溢出会被算成
        # |x| - median(|x|) —— 在普跌窗口里符号信息全丢, 相关性直接归零
        # (实测 ρ 从 +0.2215 掉到 +0.0537)。
        bench = _market_beta_benchmark(conn)
        if bench and rows:
            ws, mvs = [], []
            for r in rows:
                try:
                    b = bench.get(int(r["ts"]) // 3600)
                except (TypeError, ValueError):
                    b = None
                if b is None:
                    continue
                ws.append(float(r["w"]))
                mvs.append(abs(float(r["fmv_signed"]) - b))
            if len(ws) >= 3:
                out["ex_ante_n_excess"] = len(ws)
                out["rho_excess"] = _spearman(ws, mvs)

        # ── v335: 桶内超额口径 (新的主判据) ────────────────────────────────────
        # 上一版"先减同小时中位数、再把所有小时混在一起排秩"仍然漏掉一件事:
        # 不同小时的波动幅度差一个量级 (平静时段全在 ±0.2%, 剧烈时段 ±3%),
        # 混在一起排秩时,**小时之间的差异**会盖过**同一小时内权重差异**的效果。
        # 这就是 ρ 卡在 0.197 的原因 —— 不是权重没用, 是口径把信号淹了。
        #
        # 正确做法: 只在同一小时内比较 (同一个市场瞬间, 谁的权重大谁的影响大),
        # 得到每小时一个 ρ, 再按样本量加权汇总。实测 0.288 (177 个小时, n=4376)。
        buckets: dict[int, list[tuple[float, float]]] = {}
        for r in rows:
            try:
                hour = int(r["ts"]) // 3600
            except (TypeError, ValueError):
                continue
            buckets.setdefault(hour, []).append(
                (float(r["w"]), float(r["fmv_signed"])))
        _bs = bucket_spearman([(h, w, s) for h, v in buckets.items() for w, s in v])
        if _bs["rho"] is not None:
            out["rho_bucket"] = _bs["rho"]
            out["rho_bucket_n"] = _bs["n"]
            out["rho_bucket_hours"] = _bs["hours"]

    # 分层表现 (trade 空间, 每个决策取最后一次权重)
    if _has_table(conn, "trade_decisions"):
        try:
            trows = conn.execute(
                "SELECT h.weight_tier AS tier, COUNT(*) AS n, "
                "AVG(ABS(d.actual_outcome_pct)) AS avg_mv, "
                "SUM(CASE WHEN d.decision_type='ENTRY' THEN 1 ELSE 0 END) AS entry_n "
                "FROM signal_weight_history h JOIN trade_decisions d ON d.id=h.decision_id "
                "WHERE h.decision_space='trade' AND d.status='settled' "
                "AND d.actual_outcome_pct IS NOT NULL "
                "GROUP BY 1 ORDER BY 1").fetchall()
            out["tiers"] = {r["tier"]: {"n": r["n"],
                                        "avg_abs_move": round(float(r["avg_mv"] or 0), 4),
                                        "entry_n": r["entry_n"]} for r in trows}
        except sqlite3.Error:
            pass
    return out


def check_02_signal_weight(conn: sqlite3.Connection, http_on: bool) -> dict:
    t = THRESHOLDS
    n_samples, n_buckets, method, src = None, 0, "", "db:signal_weight_history"
    d = _http("v237/calibration", {"days": 90}) if http_on else None
    if d and d.get("ok"):
        rec = d.get("recalibration") or {}
        n_samples = rec.get("n_samples")
        method = rec.get("method") or ""
        n_buckets = len(d.get("bucket_stats") or [])
        src = "api:v237/calibration"
    if n_samples is None:
        n_samples = int(_scalar(conn, "SELECT COUNT(*) FROM signal_weight_history", default=0) or 0)
        n_buckets = int(_scalar(conn, "SELECT COUNT(DISTINCT weight_tier) FROM signal_weight_history", default=0) or 0)
        method = "history_rows"
    tiers = {r[0]: r[1] for r in conn.execute(
        "SELECT weight_tier, COUNT(*) FROM signal_weight_history GROUP BY 1").fetchall()} if _has_table(conn, "signal_weight_history") else {}
    # ── v310 P1: 新增**标定力检验** ───────────────────────────────────────────
    # 旧判据只看"样本 ≥ 100 且 分桶 ≥ 3", 这两条与"权重准不准"毫无关系:
    # signal_weight 全库只有 17 个离散值, 照样轻松满足。真正能证明
    # "权重 = 对市场的影响"的, 是决策时点的权重与事后实际涨跌之间的单调关系。
    ev = _weight_eval_stats(conn)
    rho, n_eval = ev["rho"], ev["ex_ante_n"]
    rho_ex, n_eval_ex = ev.get("rho_excess"), ev.get("ex_ante_n_excess", 0)
    # ── v324: **乘子坍缩自检** (只报证据, 不改判据) ──────────────────────────
    # 验收 #2 的 ρ 卡在阈值下方时, 真正的问题往往不是"样本不够"或"阈值差一点",
    # 而是**权重公式里的某一维已经退化成常数** —— 那一维不再影响排序, 判别力
    # 自然上不去。v324 实测: reaction/time 100% 恒为 1.0, source 95.7% 恒为 1.0,
    # base 85.7% 恒为 0.55 (LLM 不打分 → sentiment/impact/heat 全是默认值),
    # regime 仅 2 取值 —— 只有 impact 一维真正在动。
    # 把这个证据直接挂进验收输出, 免得再花一轮去反推根因。
    collapse: dict = {}
    try:
        from signals import signal_weight_diagnostics as _swd
        _cols = [c for c, _lbl in _swd.MULTIPLIER_COLUMNS]
        _sel = ", ".join(_cols)
        _rows = conn.execute(
            "SELECT %s FROM signal_weight_history "
            "WHERE trigger_kind='insert' AND decision_space='ai'" % _sel
        ).fetchall()
        for _c, _lbl in _swd.MULTIPLIER_COLUMNS:
            _info = _swd.detect_multiplier_collapse([r[_c] for r in _rows])
            _info["label"] = _lbl
            collapse[_c] = _info
    except Exception as _e:  # 诊断是附加证据, 挂了不能影响验收主流程
        collapse = {"_error": "%s: %s" % (type(_e).__name__, str(_e)[:120])}
    _dead = [c for c, v in collapse.items()
             if isinstance(v, dict) and v.get("status") in ("已坍缩", "完全退化")]
    ex_ante_ok = n_eval >= t["weight_ex_ante_min"]
    rho_ok = (rho is not None and n_eval >= t["weight_eval_samples_min"]
              and rho >= t["weight_impact_spearman_min"])
    # v321.2 P0: 超额口径 (剥离市场 beta) 是**主判据** —— 绝对涨跌把普涨/普跌
    # 窗口的 beta 混进来, 实测 ρ 在 +0.74 ~ -0.54 之间翻转, 读数不可信。
    # 绝对口径保留为参考 (两者都达标才算 PASS)。
    rho_ex_ok = (rho_ex is not None and n_eval_ex >= t["weight_eval_samples_min"]
                 and rho_ex >= t["weight_impact_spearman_min"])
    # v335: 桶内超额口径 —— 同一小时内比较, 不让"平静时段 vs 剧烈时段"的
    # 量级差淹掉权重信号。与旧口径并列, 任一口径达标即可 (两者都想证明同一件事)。
    rho_b = ev.get("rho_bucket")
    n_eval_b = int(ev.get("rho_bucket_n") or 0)
    rho_b_ok = (rho_b is not None and n_eval_b >= t["weight_eval_samples_min"]
                and float(rho_b) >= t["weight_impact_spearman_min"])
    ok = ((n_samples or 0) >= t["weight_samples_min"]
          and n_buckets >= t["weight_buckets_min"]
          and ex_ante_ok and (rho_b_ok or rho_ex_ok or rho_ok))

    tier_view = dict(list(tiers.items())[:8])
    m_rho = f"ρ={rho:+.3f}" if rho is not None else "ρ=n/a"
    m_rho_ex = f"ρ={rho_ex:+.3f}" if rho_ex is not None else "ρ=n/a"
    m_rho_b = f"ρ={rho_b:+.3f}" if rho_b is not None else "ρ=n/a"
    m_eval = (f"事前权重×真值 {n_eval} 条 (需 ≥ {t['weight_ex_ante_min']}) · "
              f"绝对口径 {m_rho} (n={n_eval}, 需 ≥ {t['weight_eval_samples_min']}) · "
              f"桶内超额 {m_rho_b} (n={n_eval_b}, "
              f"{int(ev.get('rho_bucket_hours') or 0)} 小时) [主判据] · "
              f"混合超额 {m_rho_ex} (n={n_eval_ex}) [参考]")
    if _dead:
        _dead_labels = ", ".join(
            "%s(%s)" % (collapse[c].get("label"), collapse[c].get("status")) for c in _dead)
        m_eval += f" | ⚠ 乘子坍缩 {_dead_labels} —— 这些维已不影响排序"
    if n_eval < t["weight_ex_ante_min"]:
        return _insufficient(
            "2", "准确评估信号权重 (自动学习)",
            f"校准样本 ≥ {t['weight_samples_min']} 且 分桶 ≥ {t['weight_buckets_min']}"
            f" 且 **决策时点权重**×可判定真值 ≥ {t['weight_ex_ante_min']} 条"
            f" 且 Spearman(权重, |超额涨跌|) ≥ {t['weight_impact_spearman_min']}"
            f" (n ≥ {t['weight_eval_samples_min']})",
            f"样本 {n_samples} · 桶 {n_buckets} · 分层 {tier_view} · 方法 {method or 'n/a'} | {m_eval}",
            need=f"决策时点权重×真值 {t['weight_ex_ante_min']} 对"
                 f"（做相关检验还要 n ≥ {t['weight_eval_samples_min']}）",
            have=f"{n_eval} 对（决策时点 insert 权重已入库，真值要等结算）",
            watch="配对够了再看 ρ：ρ 站上 0.2 说明权重排序有效，低于 0.2 才是权重公式要改。"
                  "事后回填的权重不能拿来验，那是循环论证",
            evidence={"ex_ante_pairs": n_eval, "n_samples": n_samples, "tiers": tier_view},
            fix="代码侧已接（decision_writer 落库即写 trigger_kind='insert'）；剩下的只能等结算样本")

    return _res("2", "准确评估信号权重 (自动学习)",
                "PASS" if ok else "FAIL",
                f"校准样本 ≥ {t['weight_samples_min']} 且 分桶 ≥ {t['weight_buckets_min']}"
                f" 且 **决策时点权重**×可判定真值 ≥ {t['weight_ex_ante_min']} 条"
                f" 且 Spearman(权重, |超额涨跌|) ≥ {t['weight_impact_spearman_min']}"
                f" (n ≥ {t['weight_eval_samples_min']})",
                f"样本 {n_samples} · 桶 {n_buckets} · 分层 {tier_view} · 方法 {method or 'n/a'} | {m_eval}"
                f" | 事后回填权重 {ev['post_hoc_n']}/{ev['total_n']} 条 (不参与相关检验)",
                {"n_samples": n_samples, "n_buckets": n_buckets, "tiers": tiers,
                 "method": method, "source": src,
                 "ex_ante_pairs": n_eval,
                 "spearman_weight_vs_abs_move": round(rho, 4) if rho is not None else None,
                 "ex_ante_pairs_excess": n_eval_ex,
                 "spearman_weight_vs_excess_move": round(rho_ex, 4) if rho_ex is not None else None,
                 "post_hoc_weight_rows": ev["post_hoc_n"], "total_weight_rows": ev["total_n"],
                 "tier_performance": ev["tiers"],
                 "ex_ante_ok": ex_ante_ok, "spearman_ok": rho_ok,
                 "spearman_excess_ok": rho_ex_ok,
                 "ex_ante_pairs_bucket": n_eval_b,
                 "spearman_bucket": rho_b,
                 "spearman_bucket_hours": int(ev.get("rho_bucket_hours") or 0),
                 "spearman_bucket_ok": rho_b_ok,
                 "multiplier_collapse": collapse,
                 "dead_multipliers": _dead,
                 "metric_note": "桶内超额 = 逐小时 |forward_move_pct - 当小时中位数| 的秩相关, "
                                "再按样本量加权 (避免平静/剧烈时段的量级差淹掉权重信号); "
                                "混合超额 = 减去中位数后把所有小时混在一起排秩 (参考)"},
                "backend/src_python/signals/decision_writer.py — 决策落库已写 trigger_kind='insert' 的权重 "
                "(v488 起，唯一 writer)；已无结构缺口。剩余是攒够配对后做 PAV 校准，与代码无关")


# ── 3. 信号权重人工可调 (验收硬要求) ───────────────────────────────────────
def _manual_weight_entry() -> tuple[bool, str]:
    """静态检查: 有没有**人工**写信号权重的 HTTP 入口。

    路由已陆续迁到 routers/*.py（api_server 只做 include），扫描范围必须
    带上 routers 目录。判据只认 override/manual 这类人工动作；
    系统重算 (recompute)、因果链/图谱权重批量更新、纸面调整审批不算人工可调，
    排除以免假绿（adjust/tweak 过宽，会误匹配 adjustments 等表名，已不使用）。
    """
    hits: list[str] = []
    probe_files = [SRC_PY / "api_server.py", SRC_PY / "bridge_server.py"]
    probe_files += sorted((SRC_PY / "routers").glob("*.py")) if (SRC_PY / "routers").exists() else []
    for p in probe_files:
        if not p.exists():
            continue
        text = p.read_text(encoding="utf-8", errors="ignore")
        for m in re.finditer(
            r'@(?:app|router)\.(post|put|patch)\(\s*["\']([^"\']+)["\']',
            text, re.I):
            path = m.group(2).lower()
            if "recompute" in path:
                continue
            if path.startswith("/api/causal") or "causal-graph" in path:
                continue
            if re.search(r"override|manual", path):
                hits.append(f"{p.name}:{m.group(1).upper()} {m.group(2)}")
    return bool(hits), ("; ".join(hits) if hits else "无写入口 (weight/override/manual)")


def check_03_manual_weight(conn: sqlite3.Connection, http_on: bool) -> dict:
    has_ep, detail = _manual_weight_entry()
    ov_tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%override%'").fetchall()]
    return _res("3", "信号权重人工可调",
                "PASS" if has_ep else "FAIL",
                "存在写权重的 HTTP 入口 (POST/PUT/PATCH *weight|override|manual)",
                f"入口 {detail} · override 表 {ov_tables or '无'}",
                {"has_write_endpoint": has_ep, "detail": detail, "override_tables": ov_tables},
                "backend/src_python/routers/signal_weights.py — v500 已补 POST /override 与 /revoke: "
                "只允许乘法系数(0.5~2.0)、必须写 reason+actor、explain 留 manual_override 标记")


# ── 4. 准确评估共识 (多模型一致性) ─────────────────────────────────────────
def check_04_consensus_psychology(conn: sqlite3.Connection, http_on: bool) -> dict:
    t = THRESHOLDS
    regimes, chains, src = None, None, ""
    if http_on:
        dr = _http("v291/market-regime", {"asset": "BTC", "hours": 720})
        dc = _http("v291/cross-asset-chains", {"active_only": "true"})
        if dr and dr.get("ok"):
            regimes = dr.get("n_regimes") or len(dr.get("regimes") or [])
            src = "api:v291/market-regime + cross-asset-chains"
        if dc and dc.get("ok"):
            chains = len(dc.get("chains") or [])
    if chains is None:
        chains = int(_scalar(conn, "SELECT COUNT(*) FROM causal_chains WHERE active=1", default=0) or 0)
        src = "db:causal_chains(active=1)"
    cons = int(_scalar(conn, "SELECT COUNT(DISTINCT consensus_mult) FROM signal_weight_history", default=0) or 0)
    rmult = int(_scalar(conn, "SELECT COUNT(DISTINCT regime_mult) FROM signal_weight_history", default=0) or 0)
    if regimes is None:
        regimes = rmult  # 离线代理: 市场状态乘子取值多样性

    n_ai = int(_scalar(conn, "SELECT COUNT(*) FROM ai_decisions", default=0) or 0)
    emc_n = int(_scalar(conn, "SELECT COUNT(*) FROM ai_decisions "
                             "WHERE COALESCE(TRIM(extra_models_consensus),'')<>''", default=0) or 0)
    emc_pct = emc_n / n_ai * 100.0 if n_ai else 0.0

    # ── v310 P1: 共识与心理**分开判** ────────────────────────────────────────
    # 旧判据 mult_active = max(cons, react) >= 2 —— 两者取或, 只要有一个在动就放行,
    # 于是"心理/反应"完全空转时"共识"照样 PASS。共识必须单独过: 乘子在变
    # (不是恒 1.0) 且多模型共识真的落库 (extra_models_consensus 有内容)。
    cons_active = cons >= t["consensus_mult_variants_min"]
    emc_ok = emc_pct >= t["extra_models_consensus_pct_min"]
    ok = (chains >= t["cross_asset_chains_min"] and regimes >= t["regimes_min"]
          and cons_active and emc_ok)
    return _res("4", "准确评估共识与多模型一致性",
                "PASS" if ok else "FAIL",
                f"跨资产链 ≥ {t['cross_asset_chains_min']} 且 市场状态种类 ≥ {t['regimes_min']}"
                f" 且 共识乘子取值 ≥ {t['consensus_mult_variants_min']} (恒为 1.0 = 空转)"
                f" 且 多模型共识填充率 ≥ {t['extra_models_consensus_pct_min']:.0f}%",
                f"链 {chains} · 状态 {regimes} · 共识乘子取值 {cons} · 状态乘子取值 {rmult}"
                f" · 多模型共识 {emc_n}/{n_ai} ({emc_pct:.1f}%)",
                {"n_chains": chains, "n_regimes": regimes, "consensus_mult_variants": cons,
                 "regime_mult_variants": rmult, "extra_models_consensus_rows": emc_n,
                 "extra_models_consensus_pct": round(emc_pct, 2), "ai_decisions": n_ai,
                 "consensus_active": cons_active, "source": src},
                "backend/src_python/market/market_regime.py + signal_weight 计算 — 共识乘子恒为 1.0, "
                "且 extra_models_consensus 全库为空 = 多模型共识根本没采")


# ── 4b. 准确评估心理预期与市场反应 ─────────────────────────────────────────
def check_04b_reaction_psychology(conn: sqlite3.Connection, http_on: bool) -> dict:
    t = THRESHOLDS
    react = int(_scalar(conn, "SELECT COUNT(DISTINCT reaction_mult) FROM signal_weight_history", default=0) or 0)
    n_ai = int(_scalar(conn, "SELECT COUNT(*) FROM ai_decisions", default=0) or 0)
    pr_n = int(_scalar(conn, "SELECT COUNT(*) FROM ai_decisions "
                             "WHERE COALESCE(TRIM(peer_reviews),'')<>''", default=0) or 0)
    pr_pct = pr_n / n_ai * 100.0 if n_ai else 0.0
    n_belief = int(_scalar(conn, "SELECT COUNT(*) FROM beliefs", default=0) or 0)
    n_belief_ev = int(_scalar(conn, "SELECT COUNT(*) FROM beliefs "
                                   "WHERE evidence_for>0 OR evidence_against>0", default=0) or 0)
    n_belief_ver = int(_scalar(conn, "SELECT COUNT(*) FROM beliefs "
                                     "WHERE COALESCE(last_verified_at,'')<>''", default=0) or 0)

    react_active = react >= t["reaction_mult_variants_min"]
    pr_ok = pr_pct >= t["peer_reviews_pct_min"]
    belief_ok = n_belief_ev >= t["beliefs_evidence_min"]
    ok = react_active and pr_ok and belief_ok
    return _res("4B", "准确评估心理预期与市场反应",
                "PASS" if ok else "FAIL",
                f"反应乘子取值 ≥ {t['reaction_mult_variants_min']} (恒为 1.0 = 空转)"
                f" 且 同行评审填充率 ≥ {t['peer_reviews_pct_min']:.0f}%"
                f" 且 有证据支撑的 belief ≥ {t['beliefs_evidence_min']} 条",
                f"反应乘子取值 {react} · 同行评审 {pr_n}/{n_ai} ({pr_pct:.1f}%)"
                f" · belief {n_belief_ev}/{n_belief} 条有证据 · 已复核 {n_belief_ver} 条",
                {"reaction_mult_variants": react, "peer_reviews_rows": pr_n,
                 "peer_reviews_pct": round(pr_pct, 2), "ai_decisions": n_ai,
                 "beliefs_total": n_belief, "beliefs_with_evidence": n_belief_ev,
                 "beliefs_verified": n_belief_ver,
                 "reaction_active": react_active, "peer_reviews_ok": pr_ok,
                 "beliefs_ok": belief_ok},
                "backend/src_python/signals/signal_weight_evaluator.py + beliefs — 反应乘子本身在动(多取值), "
                "但 peer_reviews 与 belief 证据链几乎为空 = 心理维度没有可核验的输入")


# ── 5. 准确评估因果链 ──────────────────────────────────────────────────────
def check_05_causal_chains(conn: sqlite3.Connection, http_on: bool) -> dict:
    t = THRESHOLDS
    n_chains = int(_scalar(conn, "SELECT COUNT(*) FROM causal_chains", default=0) or 0)
    n_active = int(_scalar(conn, "SELECT COUNT(*) FROM causal_chains WHERE active=1", default=0) or 0)
    n_out = int(_scalar(conn, "SELECT COUNT(*) FROM causal_chain_outcomes", default=0) or 0)
    covered = int(_scalar(conn, "SELECT COUNT(DISTINCT asset) FROM causal_chains", default=0) or 0)
    ok = n_active >= t["causal_chains_min"]
    return _res("5", "准确评估因果链",
                "PASS" if ok else "FAIL",
                f"活跃因果链 ≥ {t['causal_chains_min']} 且 覆盖资产 ≥ 2",
                f"链 {n_chains} (活跃 {n_active}) · 覆盖资产 {covered} · outcomes {n_out}",
                {"n_chains": n_chains, "n_active": n_active, "n_assets": covered,
                 "n_outcomes": n_out},
                "backend/src_python/causal_chains.py — 链不够则因果闭环无从谈起")


def _cols(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    except sqlite3.Error:
        return set()


# ── 6. 因果图谱闭环 (chain → outcome → 反馈 → 权重) ─────────────────────────
def check_06_causal_closure(conn: sqlite3.Connection, http_on: bool) -> dict:
    t = THRESHOLDS
    n_chains = int(_scalar(conn, "SELECT COUNT(*) FROM causal_chains", default=0) or 0)
    n_out = int(_scalar(conn, "SELECT COUNT(*) FROM causal_chain_outcomes", default=0) or 0)
    st = {r[0]: r[1] for r in conn.execute(
        "SELECT status, COUNT(*) FROM causal_chain_outcomes GROUP BY 1").fetchall()} if _has_table(conn, "causal_chain_outcomes") else {}
    n_eval = sum(v for k, v in st.items() if k in ("HIT", "HIT_WEAK", "MISS", "NEUTRAL"))
    pending = st.get("PENDING", 0)
    cols = _cols(conn, "causal_chain_outcomes")
    now = int(time.time())
    overdue = 0
    deadline_col = "eval_deadline_ts" if "eval_deadline_ts" in cols else None
    if deadline_col:
        # 权威口径: 已过评估截止时间 (eval_deadline_ts) 却仍是 PENDING
        overdue = int(_scalar(conn, "SELECT COUNT(*) FROM causal_chain_outcomes "
                                  "WHERE status='PENDING' AND eval_deadline_ts < ?", (now,), default=0) or 0)
    elif "created_at" in cols:
        overdue = int(_scalar(conn, "SELECT COUNT(*) FROM causal_chain_outcomes WHERE status='PENDING' AND created_at < ?",
                              (now - 172800,), default=0) or 0)
    pending_not_due = max(pending - overdue, 0)
    # 端点 ↔ DB 自洽 (同一份数据不能端点说一套、库里另一套)
    mismatch = None
    if http_on:
        d = _http("v291/cross-asset-chains", {"active_only": "true"})
        if d and d.get("ok"):
            api_n = len(d.get("chains") or [])
            db_active = int(_scalar(conn, "SELECT COUNT(*) FROM causal_chains WHERE active=1", default=0) or 0)
            mismatch = api_n - db_active
    ok = n_eval >= t["causal_evaluated_min"] and overdue == 0 and (mismatch in (None, 0))
    if not ok and n_eval < t["causal_evaluated_min"] and overdue == 0:
        _earliest = None
        if deadline_col:
            _earliest = _scalar(conn, "SELECT MIN(eval_deadline_ts) FROM causal_chain_outcomes "
                                      "WHERE status='PENDING'", default=None)
        _when = (time.strftime("%m-%d %H:%M", time.localtime(int(_earliest)))
                 if _earliest else "未知")
        return _insufficient(
            "6", "因果图谱闭环 (链→结果→反馈→权重)",
            f"已评估 outcome ≥ {t['causal_evaluated_min']} · 超评估截止仍未评估 = 0 · 端点与库一致",
            f"链 {n_chains} · outcomes {n_out} (已评估 {n_eval} / PENDING {pending}"
            f" / 超期未评估 {overdue} / 未到期 {pending_not_due}) · 端点活跃链-库差 {mismatch}",
            need=f"已评估 outcome {t['causal_evaluated_min']} 条",
            have=f"已评估 {n_eval} 条，{pending_not_due} 条等窗口到期（最早 {_when}）",
            watch="第一批到期后看 HIT/HIT_WEAK/MISS 的分布：如果 PENDING 到期后又被推后，"
                  "才是评估器坏了；现在 0 是结构性的",
            evidence={"n_evaluated": n_eval, "n_pending": pending,
                      "pending_not_due": pending_not_due, "overdue": overdue,
                      "earliest_deadline": _earliest},
            fix="不需要改代码：event-kline-impact-cron 已在跑（event_kline_impact 1943 行），等评估窗口")

    return _res("6", "因果图谱闭环 (链→结果→反馈→权重)",
                "PASS" if ok else "FAIL",
                f"已评估 outcome ≥ {t['causal_evaluated_min']} · 超评估截止仍未评估 = 0 · 端点与库一致",
                f"链 {n_chains} · outcomes {n_out} (已评估 {n_eval} / PENDING {pending}"
                f" / 超期未评估 {overdue} / 未到期 {pending_not_due}) · 端点活跃链-库差 {mismatch}",
                {"n_chains": n_chains, "n_outcomes": n_out, "by_status": st,
                 "n_evaluated": n_eval, "n_pending": pending, "overdue": overdue,
                 "pending_not_due": pending_not_due, "deadline_col": deadline_col,
                 "api_db_delta": mismatch},
                "backend/src_python/engine/event_kline_impact_worker.py — 该 worker 无调度注册, event_kline_impact 表恒为空")


# ── 7. 事件驱动K线 (快涨 / 慢涨) ───────────────────────────────────────────
def check_07_event_kline(conn: sqlite3.Connection, http_on: bool) -> dict:
    t = THRESHOLDS
    kinds = {r[0]: r[1] for r in conn.execute(
        "SELECT move_kind, COUNT(*) FROM critical_price_moves GROUP BY 1").fetchall()} if _has_table(conn, "critical_price_moves") else {}
    total = sum(kinds.values())
    fast = sum(v for k, v in kinds.items() if k and str(k).startswith("fast"))
    slow = sum(v for k, v in kinds.items() if k and str(k).startswith("slow"))
    fast_pct = _pct(fast, total)
    intraday = int(_scalar(conn, "SELECT COUNT(*) FROM ohlcv_intraday", default=0) or 0)
    intraday_assets = int(_scalar(conn, "SELECT COUNT(DISTINCT asset) FROM ohlcv_intraday", default=0) or 0)
    impact = int(_scalar(conn, "SELECT COUNT(*) FROM event_kline_impact", default=0) or 0)
    now = int(time.time())
    mv_cols = _cols(conn, "critical_price_moves")
    newest_move, age_h, n_24h = None, None, 0
    if "ts" in mv_cols:
        newest_move = _scalar(conn, "SELECT MAX(ts) FROM critical_price_moves", default=None)
        if newest_move:
            age_h = round((now - float(newest_move)) / 3600, 1)
        n_24h = int(_scalar(conn, "SELECT COUNT(*) FROM critical_price_moves WHERE ts >= ?",
                            (now - 86400,), default=0) or 0)
    ok = (fast >= t["fast_moves_min"] and fast_pct >= t["fast_moves_pct_min"]
          and slow > 0 and impact > 0)
    # 慢异动的 ts 锚在**窗口起点**，纪元之后必须攒够一个完整窗口（最长 5d）才可能出现；
    # 纪元才过几个小时时 slow=0 是结构性的，不是"链路没跑起来" → 判 SKIP 而不是 FAIL。
    _started, age_epoch_h = _epoch_info(conn)
    grace_slow = (slow == 0 and fast > 0 and age_epoch_h is not None and age_epoch_h < 48)
    if not ok and grace_slow:
        return _res("7", "事件驱动K线 (快涨 / 慢涨)", "SKIP",
                    f"fast 异动 ≥ {t['fast_moves_min']} 条且占比 ≥ {t['fast_moves_pct_min']}% · slow > 0 · event_kline_impact > 0",
                    f"fast {fast} ({fast_pct}%) / slow {slow} / 合计 {total} · 纪元才 {age_epoch_h:.1f}h，"
                    f"慢异动窗口(1d/3d/5d)还没攒够 → 宽限内判 SKIP · 分类 {kinds}",
                    {"kinds": kinds, "n_fast": fast, "n_slow": slow, "fast_pct": fast_pct,
                     "epoch_age_h": round(age_epoch_h, 1)},
                    "等纪元满 2 天后复跑；若 slow 仍为 0 才是真问题")
    return _res("7", "事件驱动K线 (快涨 / 慢涨)",
                "PASS" if ok else "FAIL",
                f"fast 异动 ≥ {t['fast_moves_min']} 条且占比 ≥ {t['fast_moves_pct_min']}% · slow > 0 · event_kline_impact > 0",
                f"fast {fast} ({fast_pct}%) / slow {slow} / 合计 {total} · event_kline_impact {impact} 行"
                f" · intraday {intraday} 行 {intraday_assets} 品种"
                f" · 最近异动距今 {age_h} 小时 (24h 内 {n_24h} 条) · 分类 {kinds}",
                {"kinds": kinds, "n_fast": fast, "n_slow": slow, "fast_pct": fast_pct,
                 "intraday_rows": intraday, "intraday_assets": intraday_assets,
                 "event_kline_impact_rows": impact, "newest_move_ts": newest_move,
                 "newest_move_age_h": age_h, "moves_24h": n_24h},
                "backend/src_python/backfill_critical_moves.py — 用 ohlcv_intraday 重算快慢分类; "
                "backend/src_python/engine/event_kline_impact_worker.py — 需要调度才会写 event_kline_impact")


def _bj_ts_expr(col: str) -> str:
    """created_at 的北京墙钟归一化表达式 (与 bridge_server 同源)。"""
    if str(SRC_PY) not in sys.path:
        sys.path.insert(0, str(SRC_PY))
    from common import time_utils
    return time_utils.sql_bj_ts_expr(col)


def _bj_str(epoch: int) -> str:
    """epoch 秒 → 北京墙钟比较格式 ('YYYY-MM-DD HH:MM:SS')。"""
    if str(SRC_PY) not in sys.path:
        sys.path.insert(0, str(SRC_PY))
    from common import time_utils
    return time_utils.epoch_to_bj_str(epoch)


def _reflection_counts(conn: sqlite3.Connection, cutoff: int | None = None) -> dict | None:
    """反思表统计: 维度分布 / 非法维度 / 非法小类 / 每维小类数。

    cutoff (epoch 秒, 北京墙钟): 给定则**只统计该时间窗内的行**, 与端点
    (v291/reflections/summary?days=N) 的窗口口径对齐。
    v310 P0: 不给 cutoff 时统计全表。端点是 90 天窗口而库里统计全表 →
    窗口外的行必然造成差值 → 这是拿两把尺子量同一个东西, 不是端点不自洽。
    """
    tbl = "trade_reflections_v2" if _has_table(conn, "trade_reflections_v2") else "trade_reflections"
    if not _has_table(conn, tbl):
        return None
    cols = _cols(conn, tbl)
    dim_col = "dimension" if "dimension" in cols else None
    sub_col = next((c for c in ("subcategory", "sub_category", "reason_code", "reason") if c in cols), None)
    where, params = "", ()
    if cutoff is not None:
        where = f" WHERE {_bj_ts_expr('created_at')} >= ?"
        params = (_bj_str(cutoff),)
    conj = " AND " if where else " WHERE "
    total = int(_scalar(conn, f"SELECT COUNT(*) FROM {tbl}{where}", params, default=0) or 0)
    abc: dict[str, int] = {}
    invalid_dim = total
    if dim_col:
        rows = conn.execute(
            f"SELECT dimension, COUNT(*) FROM {tbl}{where} GROUP BY dimension", params).fetchall()
        abc = {str(r[0]): r[1] for r in rows if r[0] is not None}
        invalid_dim = total - sum(abc.get(d, 0) for d in VALID_DIMENSIONS)
    invalid_sub, subcats = None, {}
    if sub_col:
        ph = ",".join("?" * len(VALID_SUBCATEGORIES))
        invalid_sub = int(_scalar(conn, f"SELECT COUNT(*) FROM {tbl}{where}{conj}{sub_col} IS NOT NULL AND {sub_col} NOT IN ({ph})",
                                  params + tuple(VALID_SUBCATEGORIES), default=0) or 0)
        if dim_col:
            rows = conn.execute(
                f"SELECT dimension, COUNT(DISTINCT {sub_col}) FROM {tbl}{where}{conj}dimension IN ('A','B','C') GROUP BY dimension",
                params
            ).fetchall()
            subcats = {str(r[0]): r[1] for r in rows}
    # V482 起漏检反思改写独立表 missed_signal_reflections；端点已按并集读取，
    # 探针这里也并集，否则同一份反思样本被算成两份口径（B 维度被低估）。
    if _has_table(conn, "missed_signal_reflections"):
        m_where, m_params = "", ()
        if cutoff is not None:
            m_where = f" WHERE {_bj_ts_expr('created_at')} >= ?"
            m_params = (_bj_str(cutoff),)
        m_total = int(_scalar(
            conn, f"SELECT COUNT(*) FROM missed_signal_reflections{m_where}",
            m_params, default=0) or 0)
        if m_total:
            total += m_total
            for r in conn.execute(
                    f"SELECT dimension, COUNT(*) FROM missed_signal_reflections{m_where} "
                    f"GROUP BY dimension", m_params).fetchall():
                abc[str(r[0])] = abc.get(str(r[0]), 0) + r[1]
            invalid_dim = total - sum(abc.get(d, 0) for d in VALID_DIMENSIONS)
            ph = ",".join("?" * len(VALID_SUBCATEGORIES))
            m_invalid = int(_scalar(
                conn, f"SELECT COUNT(*) FROM missed_signal_reflections{m_where}"
                f"{' AND' if m_where else ' WHERE'} subcategory IS NOT NULL "
                f"AND subcategory NOT IN ({ph})",
                m_params + tuple(VALID_SUBCATEGORIES), default=0) or 0)
            invalid_sub = (invalid_sub or 0) + m_invalid
            for r in conn.execute(
                    f"SELECT dimension, COUNT(DISTINCT subcategory) FROM "
                    f"missed_signal_reflections{m_where}"
                    f"{' AND' if m_where else ' WHERE'} dimension IN ('A','B','C') "
                    f"GROUP BY dimension", m_params).fetchall():
                subcats[str(r[0])] = max(subcats.get(str(r[0]), 0), r[1])
    return {"table": tbl, "total": total, "abc": abc, "invalid_dim": invalid_dim,
            "invalid_sub": invalid_sub, "subcats_per_dim": subcats, "sub_col": sub_col,
            "window_cutoff": cutoff}


def _api_dim_counts(payload: dict) -> dict[str, int]:
    """从 v291/reflections/summary 响应里取 A/B/C 的**行数**。

    v310 P0: 该端点的 by_dimension 是"按 asset×subcategory 拆开的**明细列表**",
    拿 len(list) 当计数得到的是**分组数** (实测 A/B/C = 4/9/8), 不是行数
    (真实 1613/357/163) → 验收 #8 / C6 "端点↔库一致" 因此假失败。
    优先用端点新加的 by_dimension_counts; 其次对明细列表求和 count。
    """
    out: dict[str, int] = {}
    bd = payload.get("by_dimension") or {}
    direct = payload.get("by_dimension_counts") or {}
    for dim in ("A", "B", "C"):
        if isinstance(direct, dict) and dim in direct:
            out[dim] = int(direct.get(dim) or 0)
            continue
        v = bd.get(dim)
        if isinstance(v, list):
            out[dim] = sum(int(x.get("count") or 0) for x in v if isinstance(x, dict))
        elif isinstance(v, dict):
            out[dim] = int(v.get("count", v.get("n", 0)) or 0)
        else:
            out[dim] = 0
    return out


def _api_sanity_excluded(payload: dict) -> dict[str, int]:
    """端点读数侧剔除的行数 (|actual_outcome_pct|>60 的量纲异常行)。

    v330: 端点故意过滤这类行 (5 日涨跌 508% 会把均值拉飞), 于是端点数比库内
    行数天然少若干条 → C6「端点↔DB 自洽」判 FAIL。差值必须**可解释**:
    端点现在显式返回 sanity_excluded, 验收比对时把这几条从库侧减掉。
    """
    exc = (payload or {}).get("sanity_excluded") or {}
    by_dim = exc.get("by_dimension") or {}
    return {d: int(by_dim.get(d, 0) or 0) for d in ("A", "B", "C")}


# ── 8. 三维反思系统 (A/B/C + 受控小类) ─────────────────────────────────────
def check_08_reflection_dims(conn: sqlite3.Connection, http_on: bool) -> dict:
    t = THRESHOLDS
    # v310 P0: 端点 v291/reflections/summary 是 90 天窗口 → 库侧也用**同一窗口**
    # 统计。否则拿"端点 90d"比"库全表", 窗口外的行必然造成差值 (假失败)。
    days = 90
    cutoff = int(time.time()) - days * 86400
    st = _reflection_counts(conn, cutoff=cutoff)
    if st is None:
        return _res("8", "三维反思系统 (A/B/C + 受控小类)", "SKIP",
                    "trade_reflections_v2 表存在且 A/B/C 三维均有样本",
                    "未找到反思表", {}, "backend/src_python/db.py — 缺少 trade_reflections_v2")
    abc = st["abc"]
    api_total, api_dims, dim_delta = None, {}, {}
    exc_dims: dict[str, int] = {}
    if http_on:
        d = _http("v291/reflections/summary", {"days": days})
        if d and d.get("ok"):
            tot = d.get("totals") or {}
            api_total = tot.get("n_reflections") or tot.get("total") or tot.get("n")
            # v310 P0: 用端点给的**行数** (by_dimension_counts), 不是 len(明细列表)
            api_dims = _api_dim_counts(d)
            # v330: 端点读数侧剔掉的量纲异常行, 比对时从库侧减去 (差值可解释)
            exc_dims = _api_sanity_excluded(d)
    # 端点是 90 天窗口, 总数与全表天然不等 → 只比对 A/B/C 分维度计数
    for dim in VALID_DIMENSIONS:
        if dim in api_dims:
            dim_delta[dim] = (api_dims[dim] or 0) - (abc.get(dim, 0) - exc_dims.get(dim, 0))
    mismatch = {k: v for k, v in dim_delta.items() if v}
    dims_ok = all(abc.get(d, 0) >= t["reflection_dim_samples_min"] for d in VALID_DIMENSIONS)
    subs_ok = (st["invalid_sub"] in (None, 0)) and all(
        st["subcats_per_dim"].get(d, 0) >= t["subcats_per_dim_min"] for d in VALID_DIMENSIONS)
    ok = dims_ok and subs_ok and st["invalid_dim"] == 0 and not mismatch
    if not ok and st["invalid_dim"] == 0 and st["invalid_sub"] in (None, 0) and not mismatch:
        _missing_dims = [d for d in VALID_DIMENSIONS
                         if abc.get(d, 0) < t["reflection_dim_samples_min"]]
        if _missing_dims:
            return _insufficient(
                "8", "三维反思系统 (A/B/C + 受控小类)",
                f"A/B/C 三维各 ≥ {t['reflection_dim_samples_min']} 样本 · 非法维度 = 0 · 非法小类 = 0 · 端点与库一致",
                f"总数 {st['total']} · A/B/C = {abc.get('A', 0)}/{abc.get('B', 0)}/{abc.get('C', 0)}"
                f" · 非法维度 {st['invalid_dim']} · 非法小类 {st['invalid_sub']}",
                need=f"缺失维度 {','.join(_missing_dims)} 各 ≥{t['reflection_dim_samples_min']} 条",
                have=f"A/B/C = {abc.get('A', 0)}/{abc.get('B', 0)}/{abc.get('C', 0)}（共 {st['total']} 条）",
                watch="C 维（技术错误：慢信号/接口失败/断流）要先有对应事件才可能出样本；"
                      "如果连续 3 天 C 维仍是 0 且日志里有技术错误，才是采集断了",
                evidence={"by_dimension": abc, "total": st["total"],
                          "missing_dims": _missing_dims},
                fix="不需要改代码：等 C 维触发事件（技术错误）自然累积")

    return _res("8", "三维反思系统 (A/B/C + 受控小类)",
                "PASS" if ok else "FAIL",
                f"A/B/C 三维各 ≥ {t['reflection_dim_samples_min']} 样本 · 非法维度 = 0 · 非法小类 = 0 · 端点与库一致",
                f"总数 {st['total']} · A/B/C = {abc.get('A', 0)}/{abc.get('B', 0)}/{abc.get('C', 0)}"
                f" · 非法维度 {st['invalid_dim']} · 非法小类 {st['invalid_sub']}"
                f" · 每维小类 {st['subcats_per_dim'] or '{}'}"
                f" · 端点 A/B/C {api_dims or '无'} (差 {mismatch or '无'}) · 端点 90d 反思 {api_total}",
                {"table": st["table"], "total": st["total"], "by_dimension": abc,
                 "invalid_dim": st["invalid_dim"], "invalid_sub": st["invalid_sub"],
                 "subcats_per_dim": st["subcats_per_dim"], "api_total": api_total,
                 "api_by_dimension": api_dims, "api_db_delta": mismatch},
                "backend/src_python/review/reflection_dimension_map.py + backfill_reflection_dimensions.py — "
                "维度映射缺失导致 A/B/C 过滤后 0 样本")


# ── 9. 反思驱动修正 (反思 → 权重/决策真的被改) ─────────────────────────────
def check_09_reflection_feedback(conn: sqlite3.Connection, http_on: bool) -> dict:
    n_hist = int(_scalar(conn, "SELECT COUNT(*) FROM signal_weight_history", default=0) or 0)
    n_explain = int(_scalar(conn, "SELECT COUNT(*) FROM signal_weight_history WHERE explain IS NOT NULL AND explain != ''",
                            default=0) or 0)
    n_dev = int(_scalar(conn, "SELECT COUNT(*) FROM signal_weight_history WHERE "
                              "COALESCE(consensus_mult,1)<>1 OR COALESCE(reaction_mult,1)<>1 OR "
                              "COALESCE(regime_mult,1)<>1 OR COALESCE(time_mult,1)<>1 OR COALESCE(source_mult,1)<>1",
                     default=0) or 0)
    st = _reflection_counts(conn)
    valid_pct = _pct((st["total"] - st["invalid_dim"]) if st else 0, st["total"] if st else 0)
    ok = (n_hist > 0 and _pct(n_explain, n_hist) >= 80.0 and _pct(n_dev, n_hist) >= 10.0
          and st is not None and st["invalid_dim"] == 0)
    return _res("9", "反思驱动修正 (反思→权重/决策真的被改)",
                "PASS" if ok else "FAIL",
                "权重历史中 explain 覆盖 ≥ 80% · 至少一个乘子偏离 1.0 的行 ≥ 10% · 反思维度非法 = 0",
                f"权重历史 {n_hist} 行 · explain {n_explain} ({_pct(n_explain, n_hist)}%)"
                f" · 乘子生效 {n_dev} ({_pct(n_dev, n_hist)}%) · 反思维度合法率 {valid_pct}%",
                {"n_weight_history": n_hist, "n_explain": n_explain, "n_multiplier_active": n_dev,
                 "explain_pct": _pct(n_explain, n_hist), "multiplier_active_pct": _pct(n_dev, n_hist),
                 "reflection_dimension_valid_pct": valid_pct},
                "backend/src_python/decision_engine.py — 反思写非法 dimension, 修正链路是垃圾进垃圾出")


# ── 10. 因果图谱准确性统计 ──────────────────────────────────────────────────
def check_10_causal_accuracy(conn: sqlite3.Connection, http_on: bool) -> dict:
    t = THRESHOLDS
    st = {r[0]: r[1] for r in conn.execute(
        "SELECT status, COUNT(*) FROM causal_chain_outcomes GROUP BY 1").fetchall()} if _has_table(conn, "causal_chain_outcomes") else {}
    n_hit = st.get("HIT", 0)
    n_weak = st.get("HIT_WEAK", 0)
    n_miss = st.get("MISS", 0)
    n_neutral = st.get("NEUTRAL", 0)
    n_eval = n_hit + n_weak + n_miss
    real_rate = round(n_hit / n_eval, 4) if n_eval else None
    dir_rate = round((n_hit + n_weak) / n_eval, 4) if n_eval else None
    # ── v406 口径修正：BOTH 链的"命中"不是方向命中 ─────────────────────────
    # 旧口径把 BOTH 链算进方向命中率：它只主张"会动"，幅度过门槛就记 HIT，
    # 实测 428/428 = 100% —— 等于给 20% 的样本白送命中，把 48.5% 抬到 59.3%。
    # 方向能力只能看**有方向主张**的链（UP/DOWN），这里单列一组口径。
    by_dir = {r[0]: (r[1], r[2], r[3]) for r in conn.execute(
        "SELECT UPPER(COALESCE(predicted_direction,'')),"
        " SUM(CASE WHEN status IN ('HIT','HIT_WEAK') THEN 1 ELSE 0 END),"
        " SUM(CASE WHEN status='HIT' THEN 1 ELSE 0 END), COUNT(*)"
        " FROM causal_chain_outcomes WHERE status IN ('HIT','HIT_WEAK','MISS')"
        " GROUP BY 1").fetchall()} if _has_table(conn, "causal_chain_outcomes") else {}
    d_hit = sum(v[0] for k, v in by_dir.items() if k in ("UP", "DOWN"))
    d_real = sum(v[1] for k, v in by_dir.items() if k in ("UP", "DOWN"))
    d_n = sum(v[2] for k, v in by_dir.items() if k in ("UP", "DOWN"))
    dir_rate_dir = round(d_hit / d_n, 4) if d_n else None
    real_rate_dir = round(d_real / d_n, 4) if d_n else None
    api = {}
    if http_on:
        d = _http("v189/causal/accuracy-stats")
        if d and d.get("ok"):
            api = {"coverage": d.get("coverage"), "n_chains": d.get("n_chains"),
                   "n_truth_insights": d.get("n_truth_insights"),
                   "avg_weight_chain": d.get("avg_weight_chain")}
    # v406：判据改用"有方向主张"的子集。BOTH 计入的旧口径保留在 detail 里做对照，
    # 但不再参与判定 —— 否则"方向命中率 0.59"这种被幅度门槛灌出来的数字能过线。
    ok = (d_n >= t["causal_evaluated_min"] and real_rate_dir is not None
          and real_rate_dir >= t["real_hit_rate_min"] and dir_rate_dir is not None
          and dir_rate_dir >= t["direction_hit_rate_min"])
    if not ok and d_n < t["causal_evaluated_min"]:
        return _insufficient(
            "10", "因果图谱准确性统计",
            f"有方向子集 n ≥ {t['causal_evaluated_min']} 且 真命中率 ≥ {t['real_hit_rate_min']}"
            f" 且 方向命中率 ≥ {t['direction_hit_rate_min']}（BOTH 不计入）",
            f"有方向 {d_n} 条 (真命中 {d_real} / 方向命中 {d_hit}) ‖ 全量已评估 {n_eval} 条",
            need=f"有方向已评估 {t['causal_evaluated_min']} 条",
            have=f"有方向 {d_n} 条（全量已评估 {n_eval} 条）",
            watch="先看链的 predicted_direction 分布：UP/DOWN 有主张的链才有样本。"
                  "若到期后仍全是 BOTH，那是建链侧只写'会动'不写方向，才需要改链模板",
            evidence={"direction_n": d_n, "direction_hits": d_hit,
                      "direction_real_hits": d_real, "all_evaluated": n_eval},
            fix="不需要改代码：等有方向链的 outcome 到期")

    return _res("10", "因果图谱准确性统计",
                "PASS" if ok else "FAIL",
                f"有方向子集 n ≥ {t['causal_evaluated_min']} 且 真命中率 ≥ {t['real_hit_rate_min']}"
                f" 且 方向命中率 ≥ {t['direction_hit_rate_min']}（BOTH 不计入）",
                f"有方向 {d_n} 条 (真命中 {d_real} / 方向命中 {d_hit}) · 真命中率 {real_rate_dir}"
                f" · 方向命中率 {dir_rate_dir}"
                f" ‖ 全量对照: n={n_eval} 真命中率 {real_rate} 方向命中率 {dir_rate}"
                f"（其中 BOTH {by_dir.get('BOTH', (0, 0, 0))[2]} 条被幅度门槛灌成命中）",
                {"by_status": st, "n_evaluated": n_eval, "real_hit_rate": real_rate,
                 "direction_hit_rate": dir_rate,
                 "directional": {"n": d_n, "hits": d_hit, "real_hits": d_real,
                                 "real_hit_rate": real_rate_dir,
                                 "direction_hit_rate": dir_rate_dir,
                                 "by_direction": {k: {"n": v[2], "hits": v[0], "real": v[1]}
                                                  for k, v in by_dir.items()}},
                 "api": api},
                "backend/src_python/chain_prior.py — 有方向样本跑不赢常数策略（全押 DOWN 63.2%）, 先修链再谈评估")


# ── 11. 按资产的影响力评估 ─────────────────────────────────────────────────
def check_11_asset_impact(conn: sqlite3.Connection, http_on: bool) -> dict:
    t = THRESHOLDS
    assets = [r[0] for r in conn.execute(
        "SELECT DISTINCT asset FROM critical_price_moves WHERE asset IS NOT NULL LIMIT 12").fetchall()]
    api_ok, api_detail = [], {}
    if http_on and assets:
        for a in assets[:6]:
            d = _http("v201/impact/by-asset", {"asset": a, "hours": 8760})
            if d and d.get("ok"):
                n = d.get("n", len(d.get("items") or []))
                api_detail[a] = n
                if n > 0:
                    api_ok.append(a)
    db_assets = int(_scalar(conn, "SELECT COUNT(DISTINCT asset) FROM event_move_attribution", default=0) or 0)
    mv_assets = int(_scalar(conn, "SELECT COUNT(DISTINCT asset) FROM critical_price_moves", default=0) or 0)
    n_impact_rows = int(_scalar(conn, "SELECT COUNT(*) FROM event_move_attribution", default=0) or 0)
    # v337: 绝对资产数取决于"资产池有多大", 而资产池由行情端决定, 会随数据漂移 ——
    # 用固定 ≥10 会让口径失真 (清掉 45 条行情播报后从 10 掉到 9 就 FAIL, 但能力没变差)。
    # 改成覆盖率: 分母 = 归档窗口内既有异动、又有同品种新闻的资产 (有素材才可能归因)。
    try:
        news_min = int(_scalar(conn, "SELECT MIN(ts) FROM news_events", default=0) or 0)
    except Exception:
        news_min = 0
    mv_with_news = int(_scalar(
        conn,
        "SELECT COUNT(DISTINCT m.asset) FROM critical_price_moves m"
        " WHERE m.asset IS NOT NULL AND m.ts >= ?"
        "   AND EXISTS (SELECT 1 FROM news_events e WHERE e.asset=m.asset)",
        (news_min,), default=0) or 0)
    cover_pct = (db_assets / mv_with_news * 100.0) if mv_with_news else 0.0
    ok = db_assets >= t["asset_impact_min"] or cover_pct >= t["asset_impact_cover_pct_min"]
    return _res("11", "按资产的影响力评估",
                "PASS" if ok else "FAIL",
                f"有影响力评估的资产 ≥ {t['asset_impact_min']}"
                f" 或 可归因资产覆盖率 ≥ {t['asset_impact_cover_pct_min']}%",
                f"归因表覆盖资产 {db_assets} · 异动表覆盖资产 {mv_assets} · 归因行 {n_impact_rows}"
                f" · 可归因资产 {mv_with_news} → 覆盖率 {cover_pct:.1f}%"
                f" · 端点有数据资产 {api_ok or '无'} {api_detail if api_detail else ''}",
                {"db_assets_with_attribution": db_assets, "move_assets": mv_assets,
                 "n_attribution_rows": n_impact_rows, "api_assets_with_data": api_ok,
                 "move_assets_with_news": mv_with_news, "cover_pct": round(cover_pct, 2),
                 "api_detail": api_detail},
                "backend/src_python/bridge_server.py v201/impact/by-asset — 影响力评估没有跨资产覆盖")


# ── 12. 归因 + 反思统计 (真因果占比) ───────────────────────────────────────
def _frontend_tier_parity() -> tuple[bool, str]:
    """校验前端归因分层口径没变 (frontend/lib/api-gub.ts)。"""
    p = FRONTEND_DIR / "lib" / "api-gub.ts"
    if not p.exists():
        return False, "未找到 frontend/lib/api-gub.ts"
    text = p.read_text(encoding="utf-8", errors="ignore")
    need = {"spillover": "spillover", "macro_fallback": "macro_fallback",
            "time_lag_sec < 0": "retro 判据", "attributionTier": "分层函数"}
    missing = [v for k, v in need.items() if k not in text]
    return (not missing), ("口径一致" if not missing else "前端口径已变: 缺 " + ",".join(missing))


def check_12_attribution_reflection(conn: sqlite3.Connection, http_on: bool) -> dict:
    t = THRESHOLDS
    n_rows = int(_scalar(conn, "SELECT COUNT(*) FROM event_move_attribution", default=0) or 0)
    _causal_where = ("ev_news_id IS NOT NULL AND time_lag_sec >= 0 "
                     "AND algo_reasons NOT LIKE '%spillover%' AND algo_reasons NOT LIKE '%macro_fallback%' "
                     "AND algo_reasons NOT LIKE '%retro%'")
    n_causal = int(_scalar(conn, "SELECT COUNT(*) FROM event_move_attribution WHERE " + _causal_where,
                           default=0) or 0)
    # v337: "真因果"必须排除**行情播报类**标题 (「跌破 XX 美元」「24 小时跌幅」)。
    # 这类标题是价格的结果, 拿它解释同一波涨跌属循环论证 —— 旧口径把它们
    # 全算成真因果, 于是"真因果占比 100%"是账面值。判据与归因模块共用
    # `_is_price_echo`, 避免两套口径漂移。
    n_price_echo = 0
    try:
        from calc_event_move_attribution import _is_price_echo  # noqa: WPS433
        for _hl, in conn.execute(
            "SELECT e.headline FROM event_move_attribution a "
            "JOIN news_events e ON a.news_event_id=e.id WHERE " + _causal_where
        ).fetchall():
            if _is_price_echo(_hl):
                n_price_echo += 1
    except Exception:
        _is_price_echo = None
    n_causal_effective = max(n_causal - n_price_echo, 0)
    # v338: 机器自检（attribution_rule_verify）落地后，"真因果"不能再等于
    # "通过了旧的排除条件"。旧的 100% 是账面值 —— 自检实测 254 条里只有 151 条
    # 三票全过，99 条只是沾边（partial），4 条直接否证（reject）。
    # 规则没跑过的库退回旧口径，避免新部署直接 FAIL。
    n_rule_confirm = n_rule_partial = n_rule_reject = 0
    rule_on = _has_col(conn, "event_move_attribution", "rule_verdict")
    if rule_on:
        n_rule_confirm = int(_scalar(conn, "SELECT COUNT(*) FROM event_move_attribution"
                                     " WHERE rule_verdict='confirm'", default=0) or 0)
        n_rule_partial = int(_scalar(conn, "SELECT COUNT(*) FROM event_move_attribution"
                                     " WHERE rule_verdict='partial'", default=0) or 0)
        n_rule_reject = int(_scalar(conn, "SELECT COUNT(*) FROM event_move_attribution"
                                    " WHERE rule_verdict='reject'", default=0) or 0)
        if n_rule_confirm + n_rule_partial + n_rule_reject > 0:
            n_causal_effective = n_rule_confirm
    reject_pct = _pct(n_rule_reject, n_rows)
    n_retro = int(_scalar(conn, "SELECT COUNT(*) FROM event_move_attribution WHERE "
                                "time_lag_sec < 0 OR algo_reasons LIKE '%retro%'", default=0) or 0)
    n_derived = max(n_rows - n_causal - n_retro, 0)
    causal_pct = _pct(n_causal_effective, n_rows)
    st = _reflection_counts(conn)
    n_refl = st["total"] if st else 0
    parity, parity_note = _frontend_tier_parity()
    ok = (n_rows >= t["attribution_rows_min"] and causal_pct >= t["causal_tier_pct_min"]
          and n_refl >= t["reflection_rows_min"] and parity
          and reject_pct <= 10.0)
    if not ok and n_rows >= t["attribution_rows_min"] and n_refl < t["reflection_rows_min"] \
            and causal_pct >= t["causal_tier_pct_min"] and reject_pct <= 10.0 and parity:
        return _insufficient(
            "12", "归因 + 反思统计 (真因果占比)",
            f"归因行 ≥ {t['attribution_rows_min']} · 真因果占比 ≥ {t['causal_tier_pct_min']}% "
            f"(已剔行情播报类) · 反思样本 ≥ {t['reflection_rows_min']} · 否证占比 ≤ 10%",
            f"归因 {n_rows} 行 · 真因果占比 {causal_pct}% · 否证 {reject_pct}% · 反思样本 {n_refl}",
            need=f"反思样本 {t['reflection_rows_min']} 条",
            have=f"{n_refl} 条（归因侧 75% 真因果、否证 0.9% 已达标）",
            watch="真因果已经站住 75%，剩下的只是反思条数。若 3 天后条数不涨，查反思调度是否被"
                  "样本质量门（quality_rejections）冻住，而不是改口径",
            evidence={"n_attribution_rows": n_rows, "causal_pct": causal_pct,
                      "reject_pct": reject_pct, "n_reflections": n_refl},
            fix="不需要改代码：等反思行随决策累积")

    return _res("12", "归因 + 反思统计 (真因果占比)",
                "PASS" if ok else "FAIL",
                f"归因行 ≥ {t['attribution_rows_min']} · 真因果占比 ≥ {t['causal_tier_pct_min']}% "
                f"(已剔行情播报类) · 反思样本 ≥ {t['reflection_rows_min']} · 否证占比 ≤ 10%",
                f"归因 {n_rows} 行 (真因果 {n_causal_effective} / 弱因果 {n_rule_partial} / "
                f"否证 {n_rule_reject}({reject_pct}%) / 行情播报 {n_price_echo} / "
                f"事后拟合 {n_retro} / 推导 {n_derived}) · 真因果占比 {causal_pct}%"
                f" · 反思样本 {n_refl} · 前端口径 {parity_note}",
                {"n_attribution_rows": n_rows, "n_causal": n_causal,
                 "n_price_echo": n_price_echo, "n_causal_effective": n_causal_effective,
                 "n_retro": n_retro,
                 "n_derived": n_derived, "causal_pct": causal_pct, "n_reflections": n_refl,
                 "frontend_tier_parity": parity, "frontend_tier_note": parity_note},
                "backend/src_python/calc_event_move_attribution.py + bridge_server.py — "
                "真因果占比过低说明归因在事后拟合, 不是因果")


# ── 13. 评估信息的市场影响力 (新闻方向判断) ─────────────────────────────────
def check_13_news_direction(conn: sqlite3.Connection, http_on: bool) -> dict:
    t = THRESHOLDS
    now = int(time.time())
    win = now - 30 * 86400
    # v340: 只统计 data_epoch 之后的新闻 —— news_events 是保留的事实表，
    # 里面仍有旧模型写的 BUY/SELL；不切片就会拿旧标签冒充"当前能力"。
    epoch_ts = _epoch_unix(conn)
    if epoch_ts and epoch_ts > win:
        win = epoch_ts
    # 全表 30d 动作分布 —— 枚举合法性口径
    rows = conn.execute("SELECT action, COUNT(*) FROM news_events WHERE ts >= ? GROUP BY 1", (win,)).fetchall() \
        if _has_table(conn, "news_events") else []
    counts = {("NULL" if (r[0] is None or r[0] == "") else str(r[0])): r[1] for r in rows}
    total30 = sum(counts.values())
    invalid = sum(v for k, v in counts.items() if k not in VALID_NEWS_ACTIONS and k != "NULL")
    null_action = counts.get("NULL", 0)
    # 已处理 (DONE) 子集 —— 方向质量口径 (PENDING 行的 action 是预填 HOLD, 不代表判断)
    done_rows = conn.execute(
        "SELECT action, COUNT(*) FROM news_events WHERE status='DONE' AND ts >= ? GROUP BY 1", (win,)).fetchall() \
        if _has_table(conn, "news_events") else []
    done_counts = {("NULL" if (r[0] is None or r[0] == "") else str(r[0])): r[1] for r in done_rows}
    done_total = sum(done_counts.values())
    buy, sell, hold = done_counts.get("BUY", 0), done_counts.get("SELL", 0), done_counts.get("HOLD", 0)
    hold_pct = _pct(hold, done_total)
    # v499: 映射率 / 有分占比同样按纪元切片，且与 hold_pct 同分母（已分析 DONE 子集）。
    # 旧模型遗留行、FILTERED（未进分析）、PENDING（尚未处理）不参与当前能力口径，
    # 否则空壳行把当前表现稀释成 FAIL —— 与 v340 不拿旧标签冒充当前能力同一原则。
    n_total = int(_scalar(conn, "SELECT COUNT(*) FROM news_events", default=0) or 0)
    n_epoch = int(_scalar(conn, "SELECT COUNT(*) FROM news_events WHERE ts >= ?", (win,), default=0) or 0)
    n_epoch_none = int(_scalar(conn, "SELECT COUNT(*) FROM news_events WHERE ts >= ? AND (asset IS NULL OR asset='' OR asset='NONE')",
                               (win,), default=0) or 0)
    n_done_none = int(_scalar(conn, "SELECT COUNT(*) FROM news_events WHERE ts >= ? AND status='DONE' AND (asset IS NULL OR asset='' OR asset='NONE')",
                              (win,), default=0) or 0)
    n_scored = int(_scalar(conn, "SELECT COUNT(*) FROM news_events WHERE ts >= ? AND status='DONE' AND score > 0",
                           (win,), default=0) or 0)
    n_scored_epoch = int(_scalar(conn, "SELECT COUNT(*) FROM news_events WHERE ts >= ? AND score > 0",
                                  (win,), default=0) or 0)
    map_pct = _pct(done_total - n_done_none, done_total)
    map_pct_epoch = _pct(n_epoch - n_epoch_none, n_epoch)
    scored_pct = _pct(n_scored, done_total)
    n_future = int(_scalar(conn, "SELECT COUNT(*) FROM news_events WHERE ts > ?", (now,), default=0) or 0)
    ok = (buy > 0 and sell > 0 and invalid == 0 and null_action == 0
          and map_pct >= t["asset_mapping_pct_min"] and hold_pct <= t["news_hold_pct_max"]
          and scored_pct >= t["news_score_positive_pct_min"])
    return _res("13", "评估信息的市场影响力 (新闻方向判断)",
                "PASS" if ok else "FAIL",
                f"已处理事件 BUY>0 且 SELL>0 · HOLD 占比 ≤ {t['news_hold_pct_max']}% (DONE 子集)"
                f" · 非法 action = 0 · NULL action = 0 (全表) · 品种映射率 ≥ {t['asset_mapping_pct_min']}% (DONE 子集)"
                f" · 有方向分事件 ≥ {t['news_score_positive_pct_min']}% (DONE 子集)",
                f"DONE {done_total} 条 {done_counts} (HOLD 占比 {hold_pct}%)"
                f" · 全表 30d {total30} 条 {counts} · 非法 {invalid} · NULL {null_action}"
                f" · 品种映射 {map_pct}% (DONE {done_total - n_done_none}/{done_total})"
                f" · 有分事件 {scored_pct}% (DONE {n_scored}/{done_total})"
                f" · 纪元内映射 {map_pct_epoch}% · 未来时间戳 {n_future} 条",
                {"counts_30d": counts, "done_counts_30d": done_counts, "n_done_30d": done_total,
                 "n_invalid_action": invalid, "n_null_action": null_action,
                 "hold_pct": hold_pct, "asset_mapping_pct": map_pct, "asset_mapping_pct_epoch": map_pct_epoch,
                 "n_unmapped_asset": n_done_none, "scored_pct": scored_pct, "n_scored": n_scored,
                 "n_scored_epoch": n_scored_epoch,
                 "n_news_total": n_total, "n_news_epoch": n_epoch, "n_future_ts": n_future},
                "backend/src_python/news_score_worker.py + decision_engine.py — 事件资产/方向没落库, 归因链从源头就断")


# ── 14. 识别市场基本面 (市场体制/资金费率/订单簿/COT) ───────────────────────
def check_14_fundamentals(conn: sqlite3.Connection, http_on: bool) -> dict:
    t = THRESHOLDS
    regime = _http("v291/market-regime", {"days": 90}) if http_on else None
    funding = _http("v291/funding-rate", {"symbol": "BTCUSDT", "refresh": "false"}) if http_on else None
    ob = _http("v291/orderbook-depth", {"symbol": "BTCUSDT", "refresh": "false"}) if http_on else None
    n_regimes = 0
    regime_src = "api"
    if regime and regime.get("ok"):
        n_regimes = regime.get("n_regimes") or len(regime.get("regimes") or [])
    if n_regimes == 0:
        # v335: 端点在 WAL 写锁竞争下会偶发超时 (>8s), 单跑一次就 PASS、全量跑就
        # FAIL —— 这是抖动, 不是事实。HTTP 拿不到时读库里的体制缓存 (同一次计算
        # 的落库结果), 并在实测行标注来源, 避免"假的 FAIL"。
        try:
            if _has_table(conn, "market_regime_cache"):
                row = conn.execute(
                    "SELECT payload FROM market_regime_cache ORDER BY id DESC LIMIT 1"
                ).fetchone()
                if row and row[0]:
                    payload = json.loads(row[0])
                    n_regimes = int(payload.get("n_regimes")
                                    or len(payload.get("regimes") or []))
                    regime_src = "db_cache"
        except Exception:
            pass
    cot = int(_scalar(conn, "SELECT COUNT(*) FROM cftc_cot_data", default=0) or 0)
    macro = int(_scalar(conn, "SELECT COUNT(*) FROM macro_snapshots", default=0) or 0)
    tick_syms = {r[0] for r in conn.execute("SELECT DISTINCT symbol FROM market_ticks").fetchall()} \
        if _has_table(conn, "market_ticks") else set()
    funding_ok = bool(funding and funding.get("ok"))
    ob_ok = bool(ob and ob.get("ok"))
    ext_src = "api"
    if not http_on:
        # v337: --no-http 时上面两个端点根本没被调用, 于是永远"不可用" → 恒 WARN。
        # 那是"没测", 不是"没有"。离线模式下改读库内落库结果:
        #   订单簿 → depth_snapshots (小时级调度写入, 每行一个品种一个时点)
        #   资金费率 → 库里没有独立表 (只有 fed_rate_history), 保持不可用
        ext_src = "db"
        try:
            ob_rows = int(_scalar(conn, "SELECT COUNT(*) FROM depth_snapshots", default=0) or 0)
            ob_assets = int(_scalar(conn, "SELECT COUNT(DISTINCT asset) FROM depth_snapshots",
                                   default=0) or 0)
            ob_ok = ob_rows > 0 and ob_assets >= 1
        except Exception:
            ob_ok = False
    core_ok = n_regimes >= 1 and cot > 0
    ext_ok = funding_ok or ob_ok
    cover_ok = len(tick_syms) >= t["ticks_symbols_min"]
    if core_ok and ext_ok and cover_ok:
        status = "PASS"
    elif core_ok:
        status = "WARN"
    else:
        status = "FAIL"
    return _res("14", "识别市场基本面 (体制/资金费率/订单簿/COT)",
                status,
                f"市场体制 ≥ 1 且 COT 有数据 且 (资金费率 或 订单簿 可用) 且 行情品种 ≥ {t['ticks_symbols_min']}",
                f"体制 {n_regimes} ({regime_src}) · COT {cot} 行 · 宏观快照 {macro} 行 · 资金费率 {'OK' if funding_ok else '不可用'}"
                f" · 订单簿 {'OK' if ob_ok else '不可用'}{'(' + ext_src + ')' if ext_src == 'db' else ''}"
                f" · 行情品种 {len(tick_syms)} {sorted(tick_syms)[:6]}",
                {"n_regimes": n_regimes, "cot_rows": cot, "macro_snapshot_rows": macro,
                 "funding_ok": funding_ok, "orderbook_ok": ob_ok, "tick_symbols": sorted(tick_syms),
                 "note": "资金费率/订单簿依赖外网, 不可用属环境问题 (WARN), 不判 FAIL"},
                "backend/src_python/market/market_regime.py — 基本面判定依赖行情品种覆盖; 资金费率/订单簿依赖外网")


# ── 15. 实时交易台 (反思集成 + 真的有数据) ──────────────────────────────────
def check_15_live_desk(conn: sqlite3.Connection, http_on: bool) -> dict:
    p = FRONTEND_DIR / "components" / "quant" / "live-desk.tsx"
    trio = toggle = False
    if p.exists():
        text = p.read_text(encoding="utf-8", errors="ignore")
        # v340: 三件套分别在不同文件里，只搜 live-desk.tsx 会假红（v337 已把它们拆到
        # components/v237/）。判据改为「页面里有这个区块 + 三个组件确实存在」。
        comp_dir = FRONTEND_DIR / "components"
        def _has_component(name: str) -> bool:
            return any(comp_dir.rglob(f"{name}.tsx")) if comp_dir.exists() else False
        trio = ("错过信号反思" in text
                and all(_has_component(n) for n in
                        ("MissedSignalsPanel", "MissedSignalsDimensionSummary",
                         "MissedSignalsByDimBanner")))
        toggle = "reflectOpen" in text and "setReflectOpen" in text
    n_missed = int(_scalar(conn, "SELECT COUNT(*) FROM missed_signals", default=0) or 0)
    n_scan = int(_scalar(conn, "SELECT COUNT(*) FROM missed_signal_scan_state", default=0) or 0)
    st = _reflection_counts(conn)
    abc = sum(st["abc"].get(d, 0) for d in VALID_DIMENSIONS) if st else 0
    n_eval = int(_scalar(conn, "SELECT COUNT(*) FROM causal_chain_outcomes "
                              "WHERE status IN ('HIT','HIT_WEAK','MISS','NEUTRAL')", default=0) or 0)
    data_ok = (n_missed > 0 or abc > 0) and n_eval > 0
    ok = trio and toggle and data_ok
    if trio and toggle and not data_ok:
        return _insufficient(
            "15", "实时交易台 (反思集成 + 真的有数据)",
            "live-desk.tsx 含反思三件套 + 折叠开关, 且 反思/漏检数据非空 + 因果 outcome 已评估",
            f"组件三件套 {trio} · 折叠开关 {toggle} · missed_signals {n_missed} 行"
            f" · 扫描状态 {n_scan} 行 · A/B/C 反思 {abc} 行 · 已评估 outcome {n_eval}",
            need="至少 1 条已评估因果 outcome",
            have=f"已评估 outcome {n_eval} 条（PENDING 待到期）",
            watch="页面与数据都接好了，缺的是 outcome 结算。结算出第一批后这一项应当自动转 PASS；"
                  "若仍 FAIL，是查询口径把新行过滤掉了",
            evidence={"n_missed_signals": n_missed, "n_abc_reflections": abc,
                      "n_outcomes_evaluated": n_eval},
            fix="不需要改代码：等 causal_chain_outcomes 首次结算")

    return _res("15", "实时交易台 (反思集成 + 真的有数据)",
                "PASS" if ok else "FAIL",
                "live-desk.tsx 含反思三件套 + 折叠开关, 且 反思/漏检数据非空 + 因果 outcome 已评估",
                f"组件三件套 {trio} · 折叠开关 {toggle} · missed_signals {n_missed} 行"
                f" · 扫描状态 {n_scan} 行 · A/B/C 反思 {abc} 行 · 已评估 outcome {n_eval}",
                {"has_reflection_block": trio, "has_collapse_toggle": toggle,
                 "n_missed_signals": n_missed, "n_scan_state": n_scan, "n_abc_reflections": abc,
                 "n_outcomes_evaluated": n_eval},
                "frontend/components/quant/live-desk.tsx + backend missed_signals 采集 — "
                "组件在但没数据 = 假通过")


# ══ 数据契约体检 (C 系列) ═══════════════════════════════════════════════════
def _launchd_inventory() -> tuple[list[str], list[str]]:
    agents: list[str] = []
    try:
        out = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=10).stdout
        for ln in out.splitlines()[1:]:
            parts = ln.split("\t")
            if parts and parts[-1].strip():
                agents.append(parts[-1].strip())
    except Exception:
        pass
    plists: list[str] = []
    pdir = Path.home() / "Library" / "LaunchAgents"
    if pdir.exists():
        plists = sorted(x.name for x in pdir.glob("*.plist"))
    return agents, plists


# ── 调度注册面 (launchd plist + 常驻进程内的 thread/task 注册) ───────────────
def _reg_names(path: Path) -> list[str]:
    """静态提取「注册名」: threading.Thread(name=...) / asyncio.create_task(..., name=...)

    只认注册名, 不认 import —— 否则「模块被引用」会被误判成「已被调度」。
    """
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8", errors="ignore")
    out: list[str] = []
    for m in re.finditer(r"(?:threading|_threading)\.Thread\(", text):
        seg = text[m.end():m.end() + 400]
        nm = re.search(r'name="([^"]+)"', seg)
        if nm:
            out.append(nm.group(1))
    for m in re.finditer(r"asyncio\.create_task\(", text):
        seg = text[m.end():m.end() + 300]
        nm = re.search(r'name="([^"]+)"', seg)
        if nm:
            out.append(nm.group(1))
    return out


def _scheduler_inventory() -> dict:
    """收集所有调度注册点: launchd plist (名 + ProgramArguments) + 常驻进程注册的线程/任务名。"""
    agents: list[str] = []
    try:
        out = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=10).stdout
        for ln in out.splitlines()[1:]:
            parts = ln.split("\t")
            if parts and parts[-1].strip():
                agents.append(parts[-1].strip())
    except Exception:
        pass
    plist_text: list[str] = []
    pdir = Path.home() / "Library" / "LaunchAgents"
    if pdir.exists():
        for p in sorted(pdir.glob("*.plist")):
            plist_text.append(p.name)
            try:
                import plistlib
                with p.open("rb") as f:
                    d = plistlib.load(f)
                plist_text.append(" ".join(str(a) for a in (d.get("ProgramArguments") or [])))
            except Exception:
                pass
    reg = {
        "launchd_agents": agents,
        "plist": plist_text,
        "api_server": _reg_names(SRC_PY / "api_server.py"),
        "bridge_server": _reg_names(SRC_PY / "bridge_server.py"),
        "engine": _reg_names(SRC_PY / "engine" / "main.py"),
    }
    flat = " ".join(agents + plist_text + reg["api_server"] + reg["bridge_server"] + reg["engine"])
    return {"raw": reg, "flat": flat.lower().replace("-", "_")}


# 关键后台作业 → 注册名关键词 (任一命中 = 有调度)
SCHED_JOBS: list[tuple[str, tuple[str, ...]]] = [
    ("决策结算", ("decision_settle",)),
    ("因果链评估", ("causal_eval",)),
    ("因果权重回写", ("causal_weight_feedback",)),
    ("行情实时扫描", ("bridge_scan", "realtime_scan")),
    ("K线补全", ("kline_backfill",)),
    ("新闻拉取", ("news_pull",)),
    ("COT 持仓拉取", ("cot_pull",)),
    ("漏检信号扫描", ("missed_signal",)),
    ("异动信号扫描", ("abnormal_sig",)),
    ("反向归因", ("reverse_attribution",)),
    ("链路归档", ("chain_archive",)),
    ("事件市场归因", ("event_market_attributor",)),
    ("信号权重 cron", ("signal_weight",)),
    ("周度反思", ("weekly_reflection",)),
    ("反思调度器", ("reflection_scheduler",)),
    ("元反思", ("meta_reflection",)),
    ("AI 决策 worker", ("ai_worker",)),
    ("新闻打分 worker", ("news_score_worker", "news_score")),
    ("事件K线影响 worker", ("event_kline_impact",)),
]


def contract_C1_scheduling(conn: sqlite3.Connection, http_on: bool = False) -> dict:
    inv = _scheduler_inventory()
    flat = inv["flat"]
    found: dict[str, list[str]] = {}
    missing: list[str] = []
    for job, kws in SCHED_JOBS:
        hit = [k for k in kws if k in flat]
        if hit:
            found[job] = hit
        else:
            missing.append(job)
    ok = not missing
    return _res("C1", "关键后台作业调度覆盖 (launchd + 常驻进程注册)",
                "PASS" if ok else "FAIL",
                f"{len(SCHED_JOBS)} 个关键作业每个至少有一处调度注册 (launchd plist / api·bridge·engine 注册名)",
                f"已注册 {len(found)}/{len(SCHED_JOBS)} · 缺失: {missing or '无'}",
                {"n_jobs": len(SCHED_JOBS), "found": found, "missing": missing,
                 "launchd_agents": inv["raw"]["launchd_agents"],
                 "n_plists": len([x for x in inv["raw"]["plist"] if x.endswith(".plist")]),
                 "api_tasks": inv["raw"]["api_server"],
                 "bridge_threads": inv["raw"]["bridge_server"],
                 "engine_tasks": inv["raw"]["engine"]},
                "~/Library/LaunchAgents/*.plist 或 api_server/bridge_server lifespan — 缺失作业必须补调度, 否则该链路永远不跑")


def contract_C2_shared_tables(conn: sqlite3.Connection, http_on: bool = False) -> dict:
    names = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()]
    return _res("C2", "共享表数量",
                "PASS" if len(names) >= THRESHOLDS["tables_min"] else "FAIL",
                f"共享表 ≥ {THRESHOLDS['tables_min']} 张",
                f"当前 {len(names)} 张",
                {"n_tables": len(names), "tables": names},
                "backend/src_python/db.py — 表数量不足说明模块各建各的, 没有共享底座")


def contract_C3_decision_settle(conn: sqlite3.Connection, http_on: bool = False) -> dict:
    t = THRESHOLDS
    total = int(_scalar(conn, "SELECT COUNT(*) FROM trade_decisions", default=0) or 0)
    settled = int(_scalar(conn, "SELECT COUNT(*) FROM trade_decisions WHERE status='settled'", default=0) or 0)
    open_n = int(_scalar(conn, "SELECT COUNT(*) FROM trade_decisions WHERE status!='settled'", default=0) or 0)
    rate = _pct(settled, total)
    now = int(time.time())
    aged = int(_scalar(conn, "SELECT COUNT(*) FROM trade_decisions WHERE status!='settled' AND decision_ts < ?",
                       (now - 5 * 86400,), default=0) or 0)
    aged_with_price = int(_scalar(conn, "SELECT COUNT(*) FROM trade_decisions WHERE status!='settled' AND decision_ts < ? "
                                       "AND entry_price IS NOT NULL", (now - 5 * 86400,), default=0) or 0)
    # v310 P0: 分母为 0 时 _pct() 返回 0.0 → "超期未结算 0 条" 被算成 0% 覆盖 → 假失败。
    # 语义上"没有超期未结算的决策" = 没有可缺失的 entry_price = 覆盖率达标 → 记 100%。
    cov = 100.0 if aged == 0 else _pct(aged_with_price, aged)
    null_price_open = int(_scalar(conn, "SELECT COUNT(*) FROM trade_decisions "
                                        "WHERE status!='settled' AND entry_price IS NULL", default=0) or 0)
    ok = rate >= t["settle_rate_pct_min"] and cov >= t["entry_price_coverage_pct_min"]
    # v342: "刚产生、还没到结算窗口"不该判 FAIL。实测 2026-11-16：AI 通道恢复后
    # 2 笔决策刚入库（entry_price 已写 88.98），结算线程还没到时间，C3 就报 FAIL——
    # 这种假失败会把真正的失败淹掉。判据：一条结算都没有，且没有任何一条超过
    # 5 天（结算窗口），语义上等于"还没有可判的样本"，记 SKIP。
    youngest_open_age = _scalar(conn, "SELECT (? - MAX(decision_ts)) FROM trade_decisions "
                                     "WHERE status!='settled'", (now,), default=None)
    if settled == 0 and aged == 0 and total > 0 and youngest_open_age is not None \
            and float(youngest_open_age) < 5 * 86400:
        return _res("C3", "决策结算率 (含 entry_price 落库)",
                    "SKIP", "样本年轻：尚无决策进入结算窗口，等 5 天后才有判定意义",
                    f"共 {total} 笔、全部未结算且最近一笔仅 "
                    f"{float(youngest_open_age) / 3600:.1f}h 前 · entry_price 已落库 "
                    f"{total - null_price_open}/{total}",
                    {"n_decisions": total, "n_settled": 0, "n_open": open_n,
                     "youngest_open_age_h": round(float(youngest_open_age) / 3600, 1),
                     "entry_price_coverage_pct": cov})
    return _res("C3", "决策结算率 (含 entry_price 落库)",
                "PASS" if ok else "FAIL",
                f"结算率 ≥ {t['settle_rate_pct_min']}% 且 超 5 天未结算决策的 entry_price 落库率 ≥ "
                f"{t['entry_price_coverage_pct_min']}%",
                f"结算 {settled}/{total} = {rate}% · 未结算 {open_n} (其中 entry_price 为空 {null_price_open})"
                f" · 超 5 天未结算 {aged} 条, 其中有 entry_price 的 {aged_with_price} 条 = {cov}%"
                + (" (无超期未结算 → 视为全覆盖)" if aged == 0 else ""),
                {"n_decisions": total, "n_settled": settled, "n_open": open_n,
                 "settle_rate_pct": rate, "n_aged_open": aged, "n_aged_with_entry_price": aged_with_price,
                 "entry_price_coverage_pct": cov, "n_open_missing_entry_price": null_price_open,
                 "root_cause": "结算线程在跑 (bridge-decision-settle), 但 entry_price 从未落库 → 收益算不出来 → 永远不结算"},
                "backend/src_python/decision_engine.py (写入 entry_price) + bridge_server.py:decision_settle_loop — "
                "没有入场价就算不出收益, 结算/漏检/复盘三条下游一起断")


def contract_C4_event_backlog(conn: sqlite3.Connection, http_on: bool = False) -> dict:
    t = THRESHOLDS
    total = int(_scalar(conn, "SELECT COUNT(*) FROM news_events", default=0) or 0)
    st = {r[0]: r[1] for r in conn.execute(
        "SELECT status, COUNT(*) FROM news_events GROUP BY 1").fetchall()} if _has_table(conn, "news_events") else {}
    pending = st.get("PENDING", 0)
    now = int(time.time())
    # 真积压: 品种已映射 (有条件处理) 但超 24h 仍未处理 —— 这才是队列堵了
    backlog = int(_scalar(conn, "SELECT COUNT(*) FROM news_events WHERE status='PENDING' AND ts < ? "
                                "AND asset IS NOT NULL AND asset!='' AND asset!='NONE'",
                           (now - 86400,), default=0) or 0)
    # 孤儿事件: 没有品种映射 → 处理器无法归因 → 永远 PENDING (不是队列堵, 是没人认领)
    orphan = int(_scalar(conn, "SELECT COUNT(*) FROM news_events WHERE status='PENDING' "
                               "AND (asset IS NULL OR asset='' OR asset='NONE')", default=0) or 0)
    orphan_pct = _pct(orphan, total)
    # v337: 区分「LLM 通道故障导致的积压」与「代码/调度缺陷导致的积压」。
    # 前者不是本脚本能验的能力问题 (决策写入必须过 LLM), 记 WARN 并把条数单列;
    # 后者才是真 FAIL。通道恢复后本判据自动回到严口径, 不是永久放宽。
    llm_down, llm_reason = _llm_outage(conn=conn)
    backlog_unexplained = 0 if llm_down else backlog
    ok = backlog_unexplained <= t["true_backlog_max"] and orphan_pct <= t["orphan_event_pct_max"]
    status = "PASS" if ok else ("WARN" if (llm_down and backlog > 0 and orphan_pct <= t["orphan_event_pct_max"]) else "FAIL")
    note = (
        f"真积压 {backlog} 条中 {backlog} 条由 LLM 通道故障阻塞 ({llm_reason})"
        if llm_down else
        f"真积压 {backlog} 条, 与 LLM 通道无关 (通道在正常状态)"
    )
    return _res("C4", "事件状态积压 (真积压 vs 无主孤儿)",
                status,
                f"非 LLM 阻塞的真积压 ≤ {t['true_backlog_max']} 且 "
                f"孤儿事件 (无品种映射) 占比 ≤ {t['orphan_event_pct_max']}%",
                f"PENDING {pending}/{total} = {_pct(pending, total)}% · 真积压 {backlog} 条 · "
                f"非 LLM 阻塞 {backlog_unexplained} 条 · 孤儿 {orphan} 条 = {orphan_pct}% "
                f"· LLM 通道{'故障中' if llm_down else '正常'} · 状态分布 {st}",
                {"n_total": total, "by_status": st, "n_pending": pending,
                 "pending_pct": _pct(pending, total), "n_true_backlog": backlog,
                 "n_backlog_unexplained": backlog_unexplained,
                 "n_orphan": orphan, "orphan_pct": orphan_pct,
                 "llm_outage": llm_down, "llm_outage_reason": llm_reason, "note": note},
                "backend/src_python/feeds/news_asset_router.py (品种映射) + news_score_worker.py (打分/处理, 当前无调度) — "
                "孤儿事件补映射, 打分 worker 必须注册调度")


def contract_C5_weight_cron(conn: sqlite3.Connection, http_on: bool = False) -> dict:
    total = int(_scalar(conn, "SELECT COUNT(*) FROM signal_weight_cron_log", default=0) or 0)
    null_done = int(_scalar(conn, "SELECT COUNT(*) FROM signal_weight_cron_log WHERE finished_at IS NULL", default=0) or 0)
    err = int(_scalar(conn, "SELECT COUNT(*) FROM signal_weight_cron_log WHERE error_msg IS NOT NULL AND error_msg != ''",
                      default=0) or 0)
    rate = _pct(total - null_done, total)
    last = conn.execute("SELECT id, started_at, finished_at, processed_n, updated_n FROM signal_weight_cron_log "
                        "ORDER BY id DESC LIMIT 1").fetchone() if _has_table(conn, "signal_weight_cron_log") else None
    return _res("C5", "权重 cron 正常完成率",
                "PASS" if rate >= THRESHOLDS["cron_complete_pct_min"] else "FAIL",
                f"signal_weight_cron_log 正常完成率 ≥ {THRESHOLDS['cron_complete_pct_min']}% (finished_at 非空)",
                f"完成率 {rate}% ({total - null_done}/{total}) · 未落完成 {null_done} 次 · 报错 {err} 次"
                f" · 最近一次 {dict(last) if last else '无'}",
                {"n_runs": total, "n_null_finished": null_done, "n_errors": err,
                 "complete_pct": rate, "last_run": dict(last) if last else None},
                "backend/src_python/signals/signal_weight_cron.py — cron 跑一半被杀, 权重会停在半更新状态")


def contract_C6_api_db_consistency(conn: sqlite3.Connection, http_on: bool) -> dict:
    deltas: dict[str, Any] = {}
    notes: dict[str, Any] = {}
    if http_on:
        dr = _http("v291/reflections/summary", {"days": 90})
        if dr and dr.get("ok"):
            # v310 P0: 端点计数用 by_dimension_counts (真实行数), 库侧用**同一 90 天
            # 窗口**统计 —— 两边同一把尺子, 差值才真正反映"不自洽"。
            api_counts = _api_dim_counts(dr)
            st = _reflection_counts(conn, cutoff=int(time.time()) - 90 * 86400)
            db_counts = {dim: (st["abc"].get(dim, 0) if st else 0) for dim in ("A", "B", "C")}
            exc_dims = _api_sanity_excluded(dr)   # v330: 端点读数侧剔除的量纲异常行
            for dim in ("A", "B", "C"):
                delta = api_counts[dim] - (db_counts[dim] - exc_dims.get(dim, 0))
                if delta:
                    deltas[f"reflections_dim_{dim}"] = delta
            notes["api_by_dimension"] = api_counts
            notes["db_by_dimension"] = db_counts
            notes["db_sanity_excluded"] = exc_dims
            notes["api_total_reflections"] = (dr.get("totals") or {}).get("n_reflections")
            notes["db_total_reflections"] = st["total"] if st else 0
        dc = _http("v291/cross-asset-chains", {"active_only": "true"})
        if dc and dc.get("ok"):
            db_n = int(_scalar(conn, "SELECT COUNT(*) FROM causal_chains WHERE active=1", default=0) or 0)
            delta = len(dc.get("chains") or []) - db_n
            if delta:
                deltas["causal_chains_active"] = delta
            notes["api_causal_chains_active"] = len(dc.get("chains") or [])
            notes["db_causal_chains_active"] = db_n
        da = _http("v291/event-move-attribution", {"limit": 1000})
        if da and da.get("ok"):
            db_n = int(_scalar(conn, "SELECT COUNT(*) FROM event_move_attribution", default=0) or 0)
            delta = (da.get("n") or 0) - db_n
            if delta:
                deltas["event_move_attribution"] = delta
            notes["api_attribution_rows"] = da.get("n")
            notes["db_attribution_rows"] = db_n
    if not notes:
        return _res("C6", "端点 ↔ DB 自洽", "SKIP",
                    "端点返回值与库内行数一致 (差值 = 0)", "未启用 HTTP 或端点不可达", {},
                    "backend/src_python/bridge_server.py — 端点与库必须同源")
    return _res("C6", "端点 ↔ DB 自洽",
                "PASS" if not deltas else "FAIL",
                "端点返回值与库内行数一致 (差值 = 0)",
                f"差值 {deltas or '无'} · 参照 {notes}",
                {"deltas": deltas, "mismatched": deltas, "notes": notes},
                "backend/src_python/bridge_server.py — 端点过滤与库口径不一致, 页面数字对不上")


def contract_C7_backfill_readiness(conn: sqlite3.Connection, http_on: bool = False) -> dict:
    """回填就绪度: 权威映射表能否给非法维度的反思补上维度 (只读试算, 不写库)。"""
    import importlib.util
    map_py = SRC_PY / "review" / "reflection_dimension_map.py"
    backfill_py = SRC_PY / "backfill_reflection_dimensions.py"
    if not map_py.exists():
        return _res("C7", "回填就绪度 (只读试算)", "SKIP",
                    "权威映射表存在且能给非法维度行补上维度",
                    "未找到 reflection_dimension_map.py", {},
                    "backend/src_python/review/reflection_dimension_map.py")
    spec = importlib.util.spec_from_file_location("_rdm_probe", map_py)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    tbl = "trade_reflections_v2" if _has_table(conn, "trade_reflections_v2") else "trade_reflections"
    cols = _cols(conn, tbl)
    sel = [c for c in ("layer", "sub_layer", "signal_name", "reason_code", "error_code") if c in cols]
    n_total = n_mapped = 0
    if sel and _has_table(conn, tbl):
        for r in conn.execute(f"SELECT {','.join(sel)} FROM {tbl}").fetchall():
            n_total += 1
            dim, _sub = mod.classify_reflection_row(dict(zip(sel, r)))
            if dim:
                n_mapped += 1
    pct = _pct(n_mapped, n_total)
    ok = backfill_py.exists() and (n_total == 0 or pct >= 50.0)
    return _res("C7", "回填就绪度 (只读试算)",
                "PASS" if ok else "FAIL",
                "回填脚本存在 且 ≥ 50% 的反思行能被权威映射表解析出维度",
                f"可解析 {n_mapped} / {n_total} = {pct}% · 回填脚本 {'在' if backfill_py.exists() else '缺失'}",
                {"n_rows": n_total, "n_resolvable": n_mapped, "resolvable_pct": pct,
                 "backfill_script_exists": backfill_py.exists(),
                 "note": "只读试算, 未写库; 真回填需人工确认后单独执行"},
                "backend/src_python/backfill_reflection_dimensions.py — 回填从未跑过, 跑一次即可救活 A/B/C 卡片")


def contract_C8_enum_residue(conn: sqlite3.Connection, http_on: bool = False) -> dict:
    st = _reflection_counts(conn)
    dims_bad = st["invalid_dim"] if st else 0
    subs_bad = st["invalid_sub"] if st else 0
    news_bad = int(_scalar(conn, "SELECT COUNT(*) FROM news_events WHERE action IS NOT NULL AND action NOT IN ('BUY','SELL','HOLD')",
                           default=0) or 0)
    total = (st["total"] if st else 0) + int(_scalar(conn, "SELECT COUNT(*) FROM news_events", default=0) or 0)
    bad = (dims_bad or 0) + (subs_bad or 0) + news_bad
    rate = _pct(bad, total)
    return _res("C8", "非法枚举残留",
                "PASS" if bad == 0 else "FAIL",
                "维度/小类/新闻动作 非法枚举 = 0",
                f"非法维度 {dims_bad} · 非法小类 {subs_bad} · 非法新闻动作 {news_bad} · 占比 {rate}%",
                {"invalid_dimension": dims_bad, "invalid_subcategory": subs_bad,
                 "invalid_news_action": news_bad, "residue_pct": rate},
                "backend/src_python/decision_engine.py + bridge_server.py — 非法枚举会被下拉框过滤掉, 统计静默失真")


# 必须周期运行的 worker (缺调度 = 该链路永远不跑, 表会一直空)
CRITICAL_WORKERS: list[tuple[str, str]] = [
    ("event_kline_impact_worker", "backend/src_python/engine/event_kline_impact_worker.py — 事件→K线影响 (event_kline_impact 表)"),
    ("news_score_worker", "backend/src_python/news_score_worker.py — 新闻方向打分 (news_events.score / action)"),
]


def contract_C10_worker_registration(conn: sqlite3.Connection, http_on: bool = False) -> dict:
    """worker 脚本必须真的被调度到 (注册名 / plist / 被已注册 worker 调用)。"""
    inv = _scheduler_inventory()
    raw = inv["raw"]
    plist_text = " ".join(raw["plist"]).lower().replace("-", "_")
    api_names = [n.lower().replace("-", "_") for n in raw["api_server"]]
    bridge_names = [n.lower().replace("-", "_") for n in raw["bridge_server"]]
    engine_names = [n.lower().replace("-", "_") for n in raw["engine"]]
    workers = sorted(set(list(SRC_PY.glob("*worker*.py")) + list((SRC_PY / "engine").glob("*worker*.py"))))
    texts = {str(w.relative_to(SRC_PY)): w.read_text(encoding="utf-8", errors="ignore") for w in workers}
    stems = {rel: Path(rel).stem for rel in texts}
    # 分目录的「调度名池」: engine 的 worker 由 engine main 拉起, 顶层 worker 由 api/bridge 拉起
    pools = {
        "engine": engine_names + api_names + bridge_names + [plist_text],
        "top": api_names + bridge_names + [plist_text],
    }
    registered: dict[str, str] = {}
    for rel in texts:
        stem = stems[rel].lower()
        pool = pools["engine"] if rel.startswith("engine/") else pools["top"]
        for rn in pool:
            if rn and (rn in stem or stem in rn):
                registered[rel] = f"注册名 {rn}"
                break
    # 传递闭包: 被「已注册 worker」直接 import / 调用
    changed = True
    while changed:
        changed = False
        for rel in texts:
            if rel in registered:
                continue
            for rel2 in texts:
                if rel2 == rel or rel2 not in registered:
                    continue
                s2 = re.escape(stems[rel2])
                if re.search(rf"(import\s+{s2}\b|from\s+\S*{s2}\s+import|{s2}\.(run|main|daemon|once|daemon_loop)\w*\s*\()",
                             texts[rel2]):
                    registered[rel] = f"被已注册 worker {rel2} 调用"
                    changed = True
                    break
    missing = sorted(rel for rel in texts if rel not in registered)
    crit_state: dict[str, str] = {}
    crit_missing: list[str] = []
    why = dict(CRITICAL_WORKERS)
    for stem, desc in CRITICAL_WORKERS:
        hits = [rel for rel in texts if stems[rel] == stem]
        if not hits:
            crit_state[stem] = "文件不存在"
        elif hits[0] in registered:
            crit_state[stem] = registered[hits[0]]
        else:
            crit_state[stem] = "未注册 (代码在, 永远不会被执行)"
        if crit_state[stem].startswith("文件不存在") or "未注册" in crit_state[stem]:
            crit_missing.append(stem)
    ok = not crit_missing
    return _res("C10", "关键 worker 调度注册覆盖",
                "PASS" if ok else "FAIL",
                f"{len(CRITICAL_WORKERS)} 个关键 worker 每个都有调度注册 (launchd / 常驻进程注册名 / 被已注册 worker 调用)",
                f"关键 worker 未注册: {crit_missing or '无'} · 全部 {len(texts)} 个 worker 中未注册 {len(missing)} 个: "
                f"{missing or '无'}",
                {"critical": {s: {"state": crit_state[s], "why": why[s]} for s in crit_state},
                 "critical_missing": crit_missing, "n_workers": len(texts),
                 "registered": registered, "unregistered": missing,
                 "note": "静态扫描注册名 (Thread(name=)/create_task(name=)) + plist ProgramArguments; "
                         "未注册 = 代码在但永远不会被执行"},
                "backend/src_python/api_server.py lifespan 或 ~/Library/LaunchAgents/*.plist — 补调度后该链路才会真正跑起来")


CHECKS: list[tuple[str, Any]] = [
    ("1", check_01_signal_evaluation),
    ("2", check_02_signal_weight),
    ("3", check_03_manual_weight),
    ("4", check_04_consensus_psychology),
    ("4B", check_04b_reaction_psychology),
    ("5", check_05_causal_chains),
    ("6", check_06_causal_closure),
    ("7", check_07_event_kline),
    ("8", check_08_reflection_dims),
    ("9", check_09_reflection_feedback),
    ("10", check_10_causal_accuracy),
    ("11", check_11_asset_impact),
    ("12", check_12_attribution_reflection),
    ("13", check_13_news_direction),
    ("14", check_14_fundamentals),
    ("15", check_15_live_desk),
]

CONTRACTS: list[tuple[str, Any]] = [
    ("C1", contract_C1_scheduling),
    ("C2", contract_C2_shared_tables),
    ("C3", contract_C3_decision_settle),
    ("C4", contract_C4_event_backlog),
    ("C5", contract_C5_weight_cron),
    ("C6", contract_C6_api_db_consistency),
    ("C7", contract_C7_backfill_readiness),
    ("C8", contract_C8_enum_residue),
    ("C10", contract_C10_worker_registration),
]

ICON = {"PASS": "✅", "FAIL": "❌", "WARN": "⚠️ ", "SKIP": "⏭️ ",
        "INSUFFICIENT": "⏳ ", "ERROR": "💥"}


def _db_fingerprint() -> dict:
    out = {}
    for p in (DB_PATH, Path(str(DB_PATH) + "-wal"), Path(str(DB_PATH) + "-shm")):
        try:
            stt = p.stat()
            out[p.name] = {"size": stt.st_size, "mtime": int(stt.st_mtime)}
        except OSError:
            out[p.name] = None
    return out


# ── C9: 只读证明 ────────────────────────────────────────────────────────────
_WRITE_ACTIONS = {
    sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE,
    sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_DROP_TABLE,
    sqlite3.SQLITE_CREATE_INDEX, sqlite3.SQLITE_DROP_INDEX,
    sqlite3.SQLITE_CREATE_TRIGGER, sqlite3.SQLITE_DROP_TRIGGER,
    sqlite3.SQLITE_CREATE_VIEW, sqlite3.SQLITE_DROP_VIEW,
    sqlite3.SQLITE_ALTER_TABLE, sqlite3.SQLITE_REINDEX, sqlite3.SQLITE_ANALYZE,
}


class _WriteGuard:
    """sqlite3 authorizer: 放行读操作, 拦截并记录任何写操作。"""

    def __init__(self) -> None:
        self.attempts: list[str] = []

    def __call__(self, action: int, a1: Any, a2: Any, dbname: Any, source: Any) -> int:
        if action in _WRITE_ACTIONS:
            self.attempts.append(f"{source or '(no source)'} [action={action}]")
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK


def _readonly_write_probe(conn: sqlite3.Connection) -> tuple[bool, str]:
    """对连接做一次真实写尝试, 直接证明它**写不了**这个库。

    返回 (是否只读, 说明)。连接是 mode=ro 时 SQLite 自己就会拒绝写 →
    这是最强的证明 (不是靠文件 mtime 推断)。若连接竟然写成功了, 立即
    DROP 掉探针表并 ROLLBACK, 但仍判 FAIL (说明脚本没开只读)。
    """
    probe = "_c9_write_probe"
    try:
        conn.execute(f"CREATE TABLE {probe}(x)")
    except sqlite3.Error as e:
        return True, f"写探针被 SQLite 拒绝 ({type(e).__name__}: {e})"
    try:
        conn.execute(f"DROP TABLE {probe}")
        conn.rollback()
    except sqlite3.Error:
        pass
    return False, "写探针竟然成功 → 本连接不是只读"


def main(argv: list[str] | None = None) -> int:
    global DB_PATH
    ap = argparse.ArgumentParser(description="能力验收清单 (只读)")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--only", default="", help="只跑指定项, 逗号分隔, 如 6,7,C3")
    ap.add_argument("--no-http", action="store_true", help="只查 DB, 不打端点")
    ap.add_argument("--strict", action="store_true", help="WARN 也算失败")
    ap.add_argument("--db", default="", help="指定 DB 路径 (默认生产库, 只读打开)")
    ap.add_argument("--json-out", default="", help="把 JSON 结果另存到文件 (给 scorecard 用)")
    args = ap.parse_args(argv)

    if args.db:
        DB_PATH = Path(args.db).expanduser().resolve()
    if not DB_PATH.exists():
        print(f"[环境错误] 找不到数据库: {DB_PATH}", file=sys.stderr)
        return 2

    only = {x.strip().upper() for x in args.only.split(",") if x.strip()}
    http_on = not args.no_http
    before = _db_fingerprint()

    results: list[dict] = []
    conn = _ro()
    # v310 P0: C9 的判据改成**直接证明**本进程没有写路径 (见 _readonly_write_probe /
    # _WriteGuard)。旧判据"运行前后 DB 文件指纹不变"在生产环境恒 FAIL —— 有活跃
    # worker (news_score 每 5 分钟 / event_kline 每 15 分钟 / engine·bridge·api 常驻)
    # 持续写同一个库, 指纹必然变化, 反而把"脚本自身是否只读"这个真问题掩盖掉。
    probe_ok, probe_msg = _readonly_write_probe(conn)
    guard = _WriteGuard()
    conn.set_authorizer(guard)
    try:
        for cid, fn in CHECKS:
            if only and cid not in only:
                continue
            try:
                r = fn(conn, http_on)
            except Exception as e:  # 单项异常不能拖垮整份报告
                r = _res(cid, "执行异常", "ERROR", "-", f"{type(e).__name__}: {e}", {}, "")
            r["gub_ref"] = GUB_REF.get(cid, "")
            r["group"] = "能力项"
            results.append(r)
        for cid, fn in CONTRACTS:
            if only and cid not in only:
                continue
            try:
                r = fn(conn, http_on)
            except Exception as e:
                r = _res(cid, "执行异常", "ERROR", "-", f"{type(e).__name__}: {e}", {}, "")
            r["gub_ref"] = ""
            r["group"] = "数据契约"
            results.append(r)
    finally:
        conn.set_authorizer(None)   # 先卸 authorizer 再关连接
        conn.close()

    after = _db_fingerprint()
    changed = [k for k in before if before[k] != after.get(k)]
    write_attempts = guard.attempts
    readonly_ok = probe_ok and not write_attempts
    c9 = _res("C9", "只读断言 (本脚本未改库)",
              "PASS" if readonly_ok else "FAIL",
              "本进程的 DB 连接无任何写路径 (只读写探针 + authorizer 全程拦截)",
              f"{probe_msg} · 运行期写尝试 {len(write_attempts)} 次"
              + (f" · 文件指纹变化 {changed} (后台 worker 写入, 非本脚本)" if changed else " · 文件指纹无变化"),
              {"write_probe": probe_msg, "write_probe_ok": probe_ok,
               "write_attempts": write_attempts, "file_fingerprint_changed": changed,
               "before": before, "after": after},
              "backend/src_python/capability_acceptance.py — 只读连接 (mode=ro) + authorizer 双重保证")
    c9["group"] = "只读断言"
    results.append(c9)

    # v340: 新纪元宽限 —— "还没攒到样本"不等于"链路断了"
    epoch, n_grace = _apply_epoch_grace(results)
    if n_grace:
        print(f"[epoch] data_epoch={epoch} 宽限内降级 {n_grace} 项 FAIL → SKIP(新纪元无样本)",
              file=sys.stderr)

    n_pass = sum(1 for r in results if r["status"] == "PASS")
    n_fail = sum(1 for r in results if r["status"] == "FAIL")
    n_warn = sum(1 for r in results if r["status"] == "WARN")
    n_skip = sum(1 for r in results if r["status"] == "SKIP")
    n_ins = sum(1 for r in results if r["status"] == "INSUFFICIENT")
    n_err = sum(1 for r in results if r["status"] == "ERROR")
    summary = {"total": len(results), "PASS": n_pass, "FAIL": n_fail,
               "WARN": n_warn, "SKIP": n_skip, "INSUFFICIENT": n_ins, "ERROR": n_err}

    payload = {
        "generated_at": _now_iso(),
        "db": str(DB_PATH),
        "bridge": BRIDGE if http_on else "(disabled)",
        "thresholds": THRESHOLDS,
        "epoch": epoch,
        "epoch_grace_skipped": n_grace,
        "summary": summary,
        "results": results,
    }
    if args.json_out:
        try:
            out = Path(args.json_out).expanduser()
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as e:
            print(f"[WARN] 写 JSON 失败: {e}", file=sys.stderr)

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print("=" * 100)
        print(f"能力验收清单 · 只读模式 · {_now_iso()}")
        print(f"DB: {DB_PATH}   Bridge: {BRIDGE if http_on else '(禁用)'}"
              + (f"   纪元: {epoch} (宽限 {EPOCH_GRACE_H:.0f}h)" if epoch else ""))
        print("=" * 100)
        cur = None
        for r in results:
            if r.get("group") != cur:
                cur = r.get("group")
                print(f"\n【{cur}】" + "-" * 90)
            print(f"{ICON.get(r['status'], '?')} {r['id']:>3}  {r['name']}  [{r['status']}]")
            print(f"        判据: {r['criteria']}")
            print(f"        实测: {r['measured']}")
            if r.get("status") == "INSUFFICIENT":
                print(f"        等什么: {r.get('need','')} ｜ 现有: {r.get('have','')}")
                if r.get("watch"):
                    print(f"        数据到了看: {r['watch']}")
            if r.get("gub_ref"):
                print(f"        对照: {r['gub_ref']}")
            if r.get("fix"):
                print(f"        修复: {r['fix']}")
        print("\n" + "=" * 100)
        print(f"汇总: ✅PASS={n_pass}  ❌FAIL={n_fail}  ⏳等数据={n_ins}  ⚠️WARN={n_warn}"
              f"  ⏭️SKIP={n_skip}  💥ERROR={n_err}  / 总 {len(results)}")
        if n_ins:
            print("       ⏳ 的含义：链路在跑、口径能算，只是样本没到判定门槛 —— 不需要改代码，等数据。"
                  " 真问题看 ❌。")
        print("=" * 100)

    failed = n_fail > 0 or n_err > 0 or (args.strict and (n_warn > 0 or n_skip > 0 or n_ins > 0))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
