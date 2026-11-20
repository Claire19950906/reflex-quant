#!/usr/bin/env python3
"""链库触发词体检 / 元信息链下线（v427）。

为什么需要：扩链库触发覆盖时发现，真正卡住样本量的不是"缺数据源"，而是
① 冷却太长（48h→24h 实测样本 +81%）；② 一部分链的 trigger_words 根本不是
"世界上发生的事"，而是规则自己的术语（"样本不足""跨品种同亏""BUY追多"）
——这类链永远不可能被触发，却占着 enabled 名额、拉低链库命中率的整体分母。

本脚本做两件事：
  1) 体检：按当前触发口径逐链统计触发量，列出 0 触发链、元信息词链；
  2) --apply：把"元信息词链"标记为 origin='deprecated_meta'、enabled=0（可回滚，
     只改这两列，不动文本）。

用法：
    python3 scripts/causal_trigger_audit.py            # 只看
    python3 scripts/causal_trigger_audit.py --apply    # 写库（批量 + 退避重试）
"""
import argparse
import csv
import os
import re
import sqlite3
import sys
import time

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BACKEND, "src_python"))

import causal_chain_evaluator as cce

# 触发词里出现这些词，说明写的是"我们自己的规则/数据状态"，而不是外部事件
_META_PAT = re.compile(
    r"(样本|数据缺失|数据质量|覆盖度|None|回测|策略|仓位|开仓|平仓|追多|追空|"
    r"止损|止盈|胜率|亏损|重复|跨品种|因果倒置|叙事催化|决策频次|新闻窗口|"
    r"信息不对称|无法触发|自证)"
)
# 元信息词占触发词一半以上 → 判定为元信息链
_META_MAX_SHARE = 0.5


def audit(conn, days=30):
    chains = cce.load_active_chains(conn)
    report = []
    for ch in chains:
        kws = list(ch.trigger_keywords or [])
        meta = [k for k in kws if _META_PAT.search(k or "")]
        n = 0
        for ev in cce.find_news_triggers(conn, ch, days):
            if ch.created_ts and int(ev["ts"]) < ch.created_ts:
                continue
            n += 1
        report.append({
            "chain_id": ch.chain_id,
            "asset": ch.asset,
            "keywords": " / ".join(kws),
            "keyword_count": len(kws),
            "meta_word_count": len(meta),
            "meta_words": " / ".join(meta),
            "triggers_%dd" % days: n,
            "age_hours": int((time.time() - (ch.created_ts or time.time())) / 3600),
            "verdict": (
                "元信息链-应下线"
                if kws and len(meta) / len(kws) >= _META_MAX_SHARE
                else ("零触发-可观察" if n == 0 else "")
            ),
        })
    return report


def apply_disable(conn, chain_ids, note):
    """把元信息链标记下线（只改 origin/enabled，可回滚）。"""
    done = 0
    for cid in chain_ids:
        for attempt in range(30):
            try:
                cur = conn.execute(
                    "UPDATE causal_chains SET origin='deprecated_meta', enabled=0, "
                    "note=COALESCE(note,'')||? WHERE id=?",
                    (note, cid),
                )
                conn.commit()
                done += cur.rowcount
                break
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc):
                    raise
                time.sleep(0.5 * (attempt + 1))
        else:
            print("  [warn] 链 %s 写入仍被锁，跳过" % cid)
    return done


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="把元信息链标记下线")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--db", default=os.environ.get("TRIDENT_DB_PATH")
                    or os.path.join(BACKEND, "trident_event_bus.db"))
    args = ap.parse_args()

    conn = sqlite3.connect(args.db, timeout=20)
    conn.row_factory = sqlite3.Row
    rep = audit(conn, days=args.days)

    meta_chains = [r["chain_id"] for r in rep if r["verdict"] == "元信息链-应下线"]
    zero_chains = [r["chain_id"] for r in rep if r["verdict"] == "零触发-可观察"]
    tot = sum(r["triggers_%dd" % args.days] for r in rep)
    print("[audit] 启用链 %d | 冷却 %sh | %d 天合计触发 %d | n>=5 链 %d"
          % (len(rep), cce.TRIGGER_COOLDOWN_HOURS, args.days, tot,
             sum(1 for r in rep if r["triggers_%dd" % args.days] >= 5)))
    print("[audit] 元信息链 %d：%s" % (len(meta_chains), meta_chains))
    print("[audit] 零触发链 %d：%s" % (len(zero_chains), zero_chains))
    for r in rep:
        if r["verdict"]:
            print("  链%-4d %-5s 词数%-2d 元信息%-2d 链龄%5dh 触发%3d ← %s"
                  % (r["chain_id"], r["asset"], r["keyword_count"],
                     r["meta_word_count"], r["age_hours"],
                     r["triggers_%dd" % args.days], r["verdict"]))

    out_dir = os.path.join(BACKEND, "runtime", "trigger_audit")
    os.makedirs(out_dir, exist_ok=True)
    csv_path = os.path.join(out_dir, "trigger_audit_%s.csv"
                            % time.strftime("%Y%m%d_%H%M%S"))
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rep[0].keys()))
        writer.writeheader()
        writer.writerows(rep)
    print("[audit] 明细: %s" % csv_path)

    if args.apply and meta_chains:
        n = apply_disable(conn, meta_chains, "[deprecated_meta:v427 元信息触发词]")
        print("[apply] 已下线 %d 条元信息链" % n)
    elif args.apply:
        print("[apply] 无需下线")
    conn.close()


if __name__ == "__main__":
    main()
