# -*- coding: utf-8 -*-
"""kg_improvements_apply.py — 给"已批准但永远没落地"的改进候选一个了断（v405 / P1）

背景（实测）
-----------
`kg_improvements` 里有 6 条 `approved` + 3 条 `pending`，`applied_at` **全空**。
看上去像"审核通过了但执行器没跑"；逐条读完 payload 之后结论不同：

  · 6 条 `add_edge` 的 reason 全是 **"Binance 行情快照缺失"/"WTI 实时快照缺失"/
    "WTI 决策快照缺失"** —— 这是**数据基础设施故障**，不是市场因果；
    把它做成图谱里的边，等于把"我们没取到价"写进"原油和谁有关系"。
  · `target` 形如 `trade:10573 asset:XAU`，而 `trade_id` 在库里**不是唯一命名空间**：
    256/258/259/260 只在 `trade_audit_log` 命中，11483/11555/11816 只在 `ai_decisions` 命中，
    10573 一张表都不命中。换句话说，这批候选的行号映射本身就是坏的。
  · 1 条 `add_rule`（E014 推理链断裂）是**唯一像"知识"的一条**，但它要求改
    `reflection_checkers` 的代码，不是运行时能自动应用的。
  · 1 条 `reweight`（trade 260）指向的成交已被 v403 当假数据删除。

所以"补一个 apply 执行器"的正确形态不是"批量把 approved 写成 applied"（那等于给
账本刷绿漆），而是：**逐条校验 payload 在今天是否还成立，成立才应用，不成立就
明确拒收并写清原因**。本脚本就是这道闸门。

判定结果三档
------------
  applied  有可执行语义、payload 自洽、目标已解析 → 真正执行（目前只有 add_rule 的
           规则登记支持"登记"，其余需人工改码，一律 blocked）
  rejected 证据与 payload 冲突（成交已删 / 原因本就是数据故障）→ 调 decide_proposal(reject)
  blocked  语义成立但需要人工改代码 / payload 缺字段 → mark_apply_failed(原因)，
           这样 `list_approved_unapplied` 不会再把它当"待执行"反复取出来

用法
----
    python3 scripts/kg_improvements_apply.py                 # 只读体检（默认）
    python3 scripts/kg_improvements_apply.py --apply         # 落判定（写 kg_improvements）
    python3 scripts/kg_improvements_apply.py --json
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND / "src_python") not in sys.path:
    sys.path.insert(0, str(BACKEND / "src_python"))
AUDIT_DIR = BACKEND / "runtime" / "kg_apply"

# 数据基础设施故障 —— 不是因果知识，不允许进图谱
INFRA_MARKERS = ("快照缺失", "快照不可用", "行情不可用", "行情缺失", "取价失败",
                 "数据缺失", "接口超时", "无法验证")
# 出现这些词说明它想描述"关系"，才有资格做边
EDGE_HINTS = ("cause", "effect", "驱动", "利多", "利空", "传导")


def _db_path() -> str:
    try:
        import db
        return db.get_db_path()
    except Exception:
        return str(BACKEND / "trident_event_bus.db")


def _conn(path: str, write: bool) -> sqlite3.Connection:
    if write:
        c = sqlite3.connect(path, timeout=60)
        c.execute("PRAGMA busy_timeout=60000")
        c.row_factory = sqlite3.Row
        return c
    c = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def resolve_trade(conn, trade_id: int) -> dict:
    """trade_id 在库里到底指谁 —— 命中多张表就是命名空间冲突，不能当证据。"""
    hits = {}
    for table in ("trade_reflections_v2", "trade_audit_log", "ai_decisions", "trade_decisions"):
        try:
            n = conn.execute("SELECT COUNT(*) FROM %s WHERE id=?" % table, (trade_id,)).fetchone()[0]
        except sqlite3.Error:
            continue
        if n:
            hits[table] = n
    return {"trade_id": trade_id, "matches": hits,
            "ambiguous": len(hits) > 1, "missing": not hits}


def judge(row: dict, conn) -> dict:
    try:
        payload = json.loads(row["payload"] or "{}")
    except json.JSONDecodeError:
        payload = {}
    kind, target, reason = row["kind"], row["target"] or "", row["reason"] or ""
    out = {"id": row["id"], "kind": kind, "target": target, "status": row["status"]}

    trade_id = payload.get("trade_id")
    if trade_id is None and target.startswith("trade:"):
        try:
            trade_id = int(target.split("trade:")[1].split()[0])
        except (ValueError, IndexError):
            trade_id = None

    # 1) 原因本身就是数据故障 → 不可执行（这条最容易被误当成"因果"）
    if any(m in reason for m in INFRA_MARKERS) or any(m in (payload.get("reason_text") or "") for m in INFRA_MARKERS):
        out.update(verdict="rejected", code="infra_not_causal",
                   why="原因是行情/快照缺失（数据基础设施故障），不是市场因果，"
                       "做成边等于把'没取到价'写进图谱")
        return out

    # 2) 目标交易在库里解析不了 / 解析出多张表 → 证据无效
    if trade_id is not None:
        res = resolve_trade(conn, int(trade_id))
        out["trade_resolution"] = res
        if res["missing"]:
            out.update(verdict="rejected", code="trade_missing",
                       why="trade_id=%s 在任何成交/决策表里都不存在（可能已被 v403 当假数据删除）" % trade_id)
            return out
        if res["ambiguous"]:
            out.update(verdict="rejected", code="trade_ambiguous",
                       why="trade_id=%s 同时命中 %s —— id 命名空间冲突，无法确定它指的是哪笔"
                           % (trade_id, "/".join(res["matches"])))
            return out

    # 3) 按 kind 判断"今天能不能真的执行"
    if kind == "add_edge":
        has_edge = any(k in payload for k in ("cause", "effect")) or any(h in reason for h in EDGE_HINTS)
        if not has_edge:
            out.update(verdict="blocked", code="payload_not_edge",
                       why="payload 只有 trade/asset/action/reason_code，没有 cause→effect，"
                           "无法构造成图谱边；需要重新提取关系才能应用")
            return out
        out.update(verdict="blocked", code="edge_apply_needs_graph_api",
                   why="边构造存在，但 asset_driver_edges 的写入需要 cause 节点 id，payload 未提供")
        return out
    if kind == "reweight":
        if "chain_id" not in payload and "edge_id" not in payload:
            out.update(verdict="blocked", code="payload_not_weight",
                       why="payload 没有 chain_id/edge_id，无法确定要调谁的权重")
            return out
        out.update(verdict="blocked", code="weight_apply_needs_review",
                   why="可执行，但会改线上权重，需人工确认后再跑")
        return out
    if kind == "add_rule":
        if not payload.get("rule_code"):
            out.update(verdict="blocked", code="payload_no_rule_code", why="payload 缺 rule_code")
            return out
        out.update(verdict="blocked", code="rule_needs_code_change",
                   why="规则 %s 要落进 reflection_checkers 的代码路径，运行时无法自证有效；"
                       "登记为'待人工改码'而不是假装已应用" % payload.get("rule_code"))
        return out

    out.update(verdict="blocked", code="unknown_kind", why="未知 kind=%s" % kind)
    return out


def run(apply: bool, db_path: str) -> dict:
    conn = _conn(db_path, write=False)
    rows = [dict(r) for r in conn.execute(
        "SELECT id,kind,target,reason,status,payload,applied_at,apply_error "
        "FROM kg_improvements WHERE status IN ('approved','pending') ORDER BY id")]
    results = [judge(r, conn) for r in rows]
    conn.close()

    summary = {"total": len(results),
               "rejected": sum(1 for r in results if r["verdict"] == "rejected"),
               "blocked": sum(1 for r in results if r["verdict"] == "blocked"),
               "applied": sum(1 for r in results if r["verdict"] == "applied")}

    if apply and (summary["rejected"] or summary["blocked"]):
        import kg_improvements as kgi
        done, failed = [], []
        for r in results:
            # decide_proposal 只接受 status='pending' 的行（approved 行会返回
            # "已被处理" 且**不改任何东西** —— 第一版就是这么静默丢了 6 条判定的）。
            # 所以：pending → 真正 reject；approved → mark_apply_failed（写 apply_error，
            # list_approved_unapplied 会自动跳过，不会再把它们当待办反复取出来）。
            last_err = None
            for _ in range(3):
                try:
                    if r["verdict"] == "rejected" and r["status"] == "pending":
                        res = kgi.decide_proposal(r["id"], "reject",
                                                  note="[v406-apply] %s" % r["why"])
                    else:
                        res = kgi.mark_apply_failed(
                            r["id"], "[v406-apply/%s] %s" % (r["verdict"], r["why"]))
                    if res.get("ok") is False:
                        raise RuntimeError(res.get("error") or "未知失败")
                    last_err = None
                    done.append(r["id"])
                    break
                except Exception as e:
                    last_err = str(e)
                    time.sleep(2)
            if last_err:
                r["apply_error"] = last_err
                failed.append(r["id"])
        summary["written"] = done
        summary["failed"] = failed
        summary["written"] = done
        AUDIT_DIR.mkdir(parents=True, exist_ok=True)
        fp = AUDIT_DIR / ("apply-%s.jsonl" % time.strftime("%Y%m%d-%H%M%S"))
        with fp.open("a", encoding="utf-8") as f:
            for r in results:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        summary["audit"] = str(fp)
    return {"summary": summary, "results": results}


def main(argv=None):
    ap = argparse.ArgumentParser(description="kg_improvements 落地闸门（v405）")
    ap.add_argument("--apply", action="store_true", help="写回判定（默认只读体检）")
    ap.add_argument("--db", default="")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    out = run(a.apply, a.db or _db_path())
    if a.json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return 0
    s = out["summary"]
    print("体检 %d 条：可应用 %d · 拒收 %d · 待人工 %d%s"
          % (s["total"], s["applied"], s["rejected"], s["blocked"],
             ("（已写回）" if s.get("written") else "（只读，未写回）")))
    for r in out["results"]:
        print("  #%s %-9s %-24s %-8s %s" % (r["id"], r["kind"], (r["target"] or "")[:24],
                                            r["verdict"], r["code"]))
        print("      %s" % r["why"])
    if s.get("audit"):
        print("审计: %s" % s["audit"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
