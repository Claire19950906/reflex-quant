# -*- coding: utf-8 -*-
"""eval_set_freeze.py — 把"这一批判定"钉死在固定样本上（v405 / P1）

为什么要有这个文件
-----------------
`causal_chain_outcomes` 的历史告诉我们真正的问题不是"信号差"，而是**样本会被抹掉**：
11-15 09:10 之前那一纪元有 804 条已评估（真命中率 0.4786），一次"清空重开纪元"之后
只剩 9 条 PENDING —— 指标不是变差了，是**分母没了**，于是同一条链"昨天 47.9%、
今天 0%"两种说法都能成立，谁也证不了谁。

`frozen_ruler.py` 冻结的是**尺子**（怎么算对、指纹化、改了就作废）。这个脚本冻结的是
**样本**：哪一批 chain / outcome 属于"这一版能力的考卷"。两者缺一不可 ——
尺子固定而考卷每次重抽，分数就不可比。

产物是**只读快照**（JSON + 指纹），不写业务库：
    backend/runtime/eval_sets/<name>.json

用法
----
    python3 scripts/eval_set_freeze.py --name causal_v1 --days 60          # 建/更新冻结集
    python3 scripts/eval_set_freeze.py --name causal_v1 --report           # 按冻结成员重算（看漂移）
    python3 scripts/eval_set_freeze.py --name causal_v1 --report --json

口径（与 causal_chain_evaluator.evaluate_one 一致，记录在指纹里）
----------------------------------------------------------------
  · 命中 = direction_hit=1 且 magnitude_hit=1 → HIT
  · 方向对但幅度出界 → HIT_WEAK（**也计入方向命中率**，但不计入"完美命中"）
  · 方向中性链（BOTH）没到幅度门槛 → NEUTRAL（不进分母）
  · 方向命中率 = (HIT+HIT_WEAK)/(HIT+HIT_WEAK+MISS)，Wilson 95% 区间
  · 基准 = 该窗口全部已评估行的"无条件方向命中率"，不默认 50%
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND / "src_python") not in sys.path:
    sys.path.insert(0, str(BACKEND / "src_python"))
OUT_DIR = BACKEND / "runtime" / "eval_sets"

# 口径指纹：改任何一项 = 换考卷的评分标准，旧对比作废
METRIC = {
    "version": "E1",
    "hit": "direction_hit AND magnitude_hit",
    "hit_weak": "direction_hit only",
    "neutral_excluded": True,
    "denominator": "HIT+HIT_WEAK+MISS",
    "direction_excess_based": True,
    "interval": "wilson95",
}


def _db_path() -> str:
    try:
        import db
        return db.get_db_path()
    except Exception:
        return str(BACKEND / "trident_event_bus.db")


def _connect_ro(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def wilson(hits: int, n: int, z: float = 1.96):
    if n <= 0:
        return (0.0, 0.0)
    p = hits / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    s = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (round((c - s) / d, 4), round((c + s) / d, 4))


def collect(conn, days: int) -> dict:
    """挑出"这一版能力的考卷"：启用链、已评估、触发在窗口内。"""
    since = int(time.time()) - days * 86400
    rows = conn.execute(
        """SELECT o.id, o.chain_id, o.trigger_ts, o.status, o.direction_hit,
                  c.asset, c.expected_direction, c.created_at
           FROM causal_chain_outcomes o
           JOIN causal_chains c ON c.id = o.chain_id
           WHERE o.status IN ('HIT','HIT_WEAK','MISS','NEUTRAL','EXPIRED')
             AND o.trigger_ts >= ?
           ORDER BY o.id""",
        (since,),
    ).fetchall()
    members = [
        {"outcome_id": r["id"], "chain_id": str(r["chain_id"]), "asset": r["asset"] or "UNKNOWN",
         "direction": (r["expected_direction"] or "BOTH").upper(), "status": r["status"]}
        for r in rows
    ]
    fp_src = json.dumps([[m["outcome_id"], m["chain_id"], m["direction"]] for m in members],
                        ensure_ascii=False).encode()
    return {
        "days": days,
        "members": members,
        "fingerprint": hashlib.sha256(fp_src).hexdigest()[:16],
        "n_chains": len({m["chain_id"] for m in members}),
        "n_outcomes": len(members),
    }


def metrics(members: list) -> dict:
    d = {"HIT": 0, "HIT_WEAK": 0, "MISS": 0, "NEUTRAL": 0, "EXPIRED": 0}
    for m in members:
        d[m["status"]] = d.get(m["status"], 0) + 1
    denom = d["HIT"] + d["HIT_WEAK"] + d["MISS"]
    hits = d["HIT"] + d["HIT_WEAK"]
    # v405 补丁：BOTH 链的"命中"是 trivial —— 它只主张"会动"，幅度过门槛就算 HIT，
    # 实测 428/428 = 100%。把 BOTH 混进方向命中率，等于给 20% 的样本白送命中。
    # 方向能力只能看**有方向主张**的那部分（UP/DOWN）。
    by_dir: dict = {}
    for m in members:
        if m["status"] not in ("HIT", "HIT_WEAK", "MISS"):
            continue
        dv = m["direction"] or "BOTH"
        b = by_dir.setdefault(dv, {"n": 0, "hit": 0})
        b["n"] += 1
        b["hit"] += 1 if m["status"] in ("HIT", "HIT_WEAK") else 0
    dir_stats = {}
    for k, v in by_dir.items():
        dir_stats[k] = {"n": v["n"], "hit": v["hit"],
                        "rate": round(v["hit"] / v["n"], 4) if v["n"] else None,
                        "wilson95": list(wilson(v["hit"], v["n"]))}
    dn = sum(v["n"] for k, v in by_dir.items() if k in ("UP", "DOWN"))
    dh = sum(v["hit"] for k, v in by_dir.items() if k in ("UP", "DOWN"))
    ci = wilson(hits, denom)
    per_asset: dict = {}
    for m in members:
        if m["status"] not in ("HIT", "HIT_WEAK", "MISS"):
            continue
        a = per_asset.setdefault(m["asset"], {"n": 0, "hit": 0})
        a["n"] += 1
        a["hit"] += 1 if m["status"] in ("HIT", "HIT_WEAK") else 0
    assets = {}
    for a, v in sorted(per_asset.items(), key=lambda x: -x[1]["n"]):
        assets[a] = {"n": v["n"],
                     "direction_hit_rate": round(v["hit"] / v["n"], 3) if v["n"] else None,
                     "wilson95": list(wilson(v["hit"], v["n"]))}
    return {"by_status": d, "excluded_neutral": d["NEUTRAL"],
            "denominator": denom,
            "direction_hit_rate": round(hits / denom, 4) if denom else None,
            "wilson95": list(ci), "per_asset": assets,
            "perfect_hit_rate": round(d["HIT"] / denom, 4) if denom else None,
            # 只看有方向主张的链 —— 这才是"方向能力"，BOTH 的命中不算
            "directional": {"n": dn, "hit": dh,
                            "rate": round(dh / dn, 4) if dn else None,
                            "wilson95": list(wilson(dh, dn))},
            "by_direction": dir_stats}


def baseline(conn, days: int) -> dict:
    """同一窗口的"全押 UP"命中率 —— 基准必须实测，不许默认 50%。

    v405 修正：旧实现拿"这批行的命中率"当基准，等于自己跟自己比（0.5928 vs 0.5928）。
    真正的无条件基准是**不做判断、一律喊 UP** 能拿多少：超额收益 > 0 的比例
    （超额 = 本资产 5 日涨跌 − 同期等权市场组合，与 evaluate_one 同口径，
      存在 notes.excess_pct 里）。
    """
    since = int(time.time()) - days * 86400
    rows = conn.execute(
        """SELECT notes FROM causal_chain_outcomes
           WHERE trigger_ts>=? AND status IN ('HIT','HIT_WEAK','MISS')""", (since,))
    n = up = 0
    for (notes,) in rows:
        try:
            ex = json.loads(notes or "{}").get("excess_pct")
        except json.JSONDecodeError:
            ex = None
        if ex is None:
            continue
        n += 1
        up += 1 if float(ex) > 0 else 0
    up_rate = up / n if n else None
    down_rate = (n - up) / n if n else None
    return {"n": n, "up": up, "up_rate": up_rate,
            "unconditional_rate": round(up_rate, 4) if up_rate is not None else None,
            # 公平基准：这窗口里"一律喊 UP / 一律喊 DOWN"哪个更高 —— 单边行情下
            # 恒喊一边就能拿到 60%+，方向命中率必须跟这个比，而不是跟 50% 比。
            "best_constant_rate": round(max(up_rate, down_rate), 4) if n else None,
            "best_constant_side": ("UP" if up_rate >= down_rate else "DOWN") if n else None,
            "wilson95": list(wilson(up, n))}


def freeze(name: str, days: int, db_path: str) -> dict:
    conn = _connect_ro(db_path)
    c = collect(conn, days)
    m = metrics(c["members"])
    b = baseline(conn, days)
    doc = {
        "name": name, "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "metric": METRIC, "fingerprint": c["fingerprint"],
        "lookback_days": days, "n_chains": c["n_chains"], "n_outcomes": c["n_outcomes"],
        "metrics": m, "baseline": b,
        # 只存 id 级成员，值本身不存 —— 这样"链被改坏"能在 --report 里暴露成漂移
        "members": c["members"],
        "db_path": db_path,
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / ("%s.json" % name)
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    conn.close()
    doc["path"] = str(path)
    return doc


def report(name: str) -> dict:
    path = OUT_DIR / ("%s.json" % name)
    if not path.exists():
        raise SystemExit("冻结集不存在: %s（先不带 --report 跑一次）" % path)
    doc = json.loads(path.read_text(encoding="utf-8"))
    frozen = {m["outcome_id"]: m for m in doc["members"]}
    conn = _connect_ro(doc["db_path"])
    live = {}
    for r in conn.execute(
            """SELECT o.id, o.status, c.asset, c.expected_direction
               FROM causal_chain_outcomes o JOIN causal_chains c ON c.id=o.chain_id
               WHERE o.id IN (%s)""" % ",".join("?" * len(frozen)), list(frozen)):
        live[r["id"]] = {"status": r["status"], "asset": r["asset"],
                         "direction": (r["expected_direction"] or "BOTH").upper()}
    conn.close()
    drift = {"status_changed": 0, "direction_changed": 0, "asset_changed": 0, "missing": 0}
    merged = []
    for oid, fm in frozen.items():
        lv = live.get(oid)
        if not lv:
            drift["missing"] += 1
            continue
        if lv["status"] != fm["status"]:
            drift["status_changed"] += 1
        if lv["direction"] != fm["direction"]:
            drift["direction_changed"] += 1
        if (lv["asset"] or "UNKNOWN") != fm["asset"]:
            drift["asset_changed"] += 1
        merged.append({**fm, **{k: lv[k] for k in ("status", "asset", "direction")}})
    cur = metrics(merged)
    return {"name": name, "fingerprint_frozen": doc["fingerprint"],
            "frozen_at": doc["created_at"], "n_frozen": len(frozen),
            "drift": drift,
            "metrics_frozen": doc["metrics"], "metrics_now": cur,
            "delta_direction_hit_rate": (
                None if doc["metrics"]["direction_hit_rate"] is None
                or cur["direction_hit_rate"] is None
                else round(cur["direction_hit_rate"] - doc["metrics"]["direction_hit_rate"], 4)),
            "baseline_frozen": doc["baseline"]}


def main(argv=None):
    ap = argparse.ArgumentParser(description="固定评测集（v405）")
    ap.add_argument("--name", default="causal_v1")
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--db", default="")
    ap.add_argument("--report", action="store_true", help="按冻结成员重算，暴露漂移")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if a.report:
        r = report(a.name)
        if a.json:
            print(json.dumps(r, ensure_ascii=False, indent=1))
        else:
            fm, nm = r["metrics_frozen"], r["metrics_now"]
            print("冻结集 %s（%s，%d 条 outcome）" % (r["name"], r["frozen_at"], r["n_frozen"]))
            print("  漂移: 状态变 %d · 方向变 %d · 资产变 %d · 缺失 %d"
                  % (r["drift"]["status_changed"], r["drift"]["direction_changed"],
                     r["drift"]["asset_changed"], r["drift"]["missing"]))
            print("  冻结时方向命中率 %s %s（n=%d）"
                  % (fm["direction_hit_rate"], fm["wilson95"], fm["denominator"]))
            print("  现在  方向命中率 %s %s（n=%d）"
                  % (nm["direction_hit_rate"], nm["wilson95"], nm["denominator"]))
            print("  Δ = %s" % r["delta_direction_hit_rate"])
        return 0
    d = freeze(a.name, a.days, a.db or _db_path())
    if a.json:
        print(json.dumps({k: v for k, v in d.items() if k != "members"},
                         ensure_ascii=False, indent=1))
    else:
        m = d["metrics"]
        print("冻结集 %s → %s" % (d["name"], d["path"]))
        print("  指纹 %s · 链 %d 条 · outcome %d 条 · 窗口 %d 天"
              % (d["fingerprint"], d["n_chains"], d["n_outcomes"], d["lookback_days"]))
        print("  %s" % m["by_status"])
        print("  含BOTH命中率 %s（Wilson95 %s，分母 %d，NEUTRAL 剔除 %d）"
              % (m["direction_hit_rate"], m["wilson95"], m["denominator"], m["excluded_neutral"]))
        dr = m["directional"]
        print("  仅UP/DOWN方向命中率 %s（Wilson95 %s，n=%d）← 这才是方向能力"
              % (dr["rate"], dr["wilson95"], dr["n"]))
        for k, v in sorted(m["by_direction"].items(), key=lambda x: -x[1]["n"]):
            print("    %-5s n=%-5d hit_rate=%s" % (k, v["n"], v["rate"]))
        b = d["baseline"]
        print("  完美命中率 %s" % m["perfect_hit_rate"])
        print("  基准(实测) 全押UP %s / 全押DOWN %s → 最优常数策略 %s(%s) n=%d"
              % (b["unconditional_rate"], round(1 - b["unconditional_rate"], 4) if b["n"] else None,
                 b["best_constant_rate"], b["best_constant_side"], b["n"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
