#!/usr/bin/env python3
"""v421 · 注入日志 ↔ 决策 的对账（离线配对，默认 dry-run）

问题：`causal_injection_log` 1782 条，`decision_id` **0 条**有值 —— 这张表只记了
"某品种某秒注入了哪些因子"，但没记"注进哪一次决策"，于是无法回答
"注入到底有没有进到决策里、进了以后命中率如何"。

engine 四个埋点现场都拿不到 decision id（L2 兜底 / 两处 attribute / batch），
要贯穿多层改造。这里走另一条路：**离线按 (品种, 时间窗) 配对**，
只填"同品种且时间近似唯一"的（高置信），其余保持 NULL 并计入 ambiguous/unmatched。

规则：
  · 品种归一：中文标签映射到 ticker（原油→WTI、黄金→XAU、美股→US_STOCK …）；
  · 时间：注入 ts 是 `YYYY-MM-DD HH:MM:SS` 文本，决策 ts 是 epoch 秒；
  · 窗口内候选决策 = 同品种、|Δ| ≤ --window 秒；
  · 恰好 1 个候选 → 认定为该决策；0 个 → unmatched；≥2 个 → ambiguous（不猜）。

用法：
  python3 scripts/link_injection_decisions.py                  # 只看
  python3 scripts/link_injection_decisions.py --apply          # 写库（只填 NULL）
"""
from __future__ import annotations

import argparse
import csv
import os
import sqlite3
import sys
import time

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(BACKEND, "trident_event_bus.db")

# 注入侧资产名常是中文标签（上游传的是"品种中文名"而不是 ticker）
_ALIAS = {
    "原油": "WTI", "黄金": "XAU", "白银": "XAG", "美股": "US_STOCK",
    "个股": "INDIVIDUAL_STOCK", "加密主流": "CRYPTO", "山寨": "CRYPTO",
    "宏观": "MACRO", "a股": "CN_STOCK", "港股": "HK_STOCK",
}


def _norm(asset: str) -> str:
    a = str(asset or "").strip()
    return _ALIAS.get(a, a.upper())


def _to_epoch(ts: str) -> int | None:
    try:
        return int(time.mktime(time.strptime(str(ts).strip(), "%Y-%m-%d %H:%M:%S")))
    except Exception:
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", type=int, default=90, help="配对时间窗（秒）")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--out", default="runtime/injection_decision_links.csv")
    args = ap.parse_args()
    out_path = args.out if os.path.isabs(args.out) else os.path.join(BACKEND, args.out)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    conn = sqlite3.connect(f"file:{DB}", uri=True, timeout=20)
    conn.row_factory = sqlite3.Row
    try:
        inj = [dict(r) for r in conn.execute(
            "SELECT id, ts, asset, mode FROM causal_injection_log"
            " WHERE decision_id IS NULL ORDER BY id"
        ).fetchall()]
        dec = [dict(r) for r in conn.execute(
            "SELECT id, target_asset, ts FROM ai_decisions"
            " WHERE ts IS NOT NULL ORDER BY ts"
        ).fetchall()]
        by_asset: dict[str, list[tuple[int, int]]] = {}
        for d in dec:
            by_asset.setdefault(_norm(d["target_asset"]), []).append((int(d["ts"]), int(d["id"])))
        for k in by_asset:
            by_asset[k].sort()

        import bisect
        rows, matched, ambiguous, unmatched = [], 0, 0, 0
        for r in inj:
            a = _norm(r["asset"])
            t = _to_epoch(r["ts"])
            status, hit_id, delta = "unmatched", "", ""
            if t is not None and a in by_asset:
                lst = by_asset[a]
                lo = bisect.bisect_left(lst, (t - args.window, -1))
                hi = bisect.bisect_right(lst, (t + args.window, 10 ** 12))
                cands = lst[lo:hi]
                if len(cands) == 1:
                    hit_id, status = cands[0][1], "matched"
                    delta = cands[0][0] - t
                    matched += 1
                elif len(cands) > 1:
                    # 取最近的一个，但标记 ambiguous 供人工复核（不自动写）
                    nearest = min(cands, key=lambda c: abs(c[0] - t))
                    hit_id, delta, status = nearest[1], nearest[0] - t, "ambiguous"
                    ambiguous += 1
                else:
                    unmatched += 1
            else:
                unmatched += 1
            rows.append({"injection_id": r["id"], "ts": r["ts"], "asset_raw": r["asset"],
                         "asset": a, "mode": r["mode"], "decision_id": hit_id,
                         "delta_s": delta, "status": status})

        with open(out_path, "w", encoding="utf-8-sig", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=["injection_id", "ts", "asset_raw", "asset",
                                               "mode", "decision_id", "delta_s", "status"])
            w.writeheader()
            w.writerows(rows)

        n = len(inj)
        if n == 0:                      # 空表时 matched / n 会 ZeroDivisionError
            print(f"待配对 0 条；明细 {out_path}")
            return 0
        print(f"待配对 {n} 条 → 唯一命中 {matched}（{matched / n:.1%}）、"
              f"歧义 {ambiguous}、配不上 {unmatched}；明细 {out_path}")
        if not args.apply:
            print("[dry-run] 未写库。确认后加 --apply（只填 decision_id 为 NULL 的行）。")
            return 0
        # 只写唯一命中的，歧义的一律不猜
        updates = [(int(r["decision_id"]), int(r["injection_id"])) for r in rows
                   if r["status"] == "matched"]
        conn.executemany(
            "UPDATE causal_injection_log SET decision_id=? WHERE id=? AND decision_id IS NULL",
            updates,
        )
        conn.commit()
        filled = conn.execute(
            "SELECT COUNT(*) FROM causal_injection_log WHERE decision_id IS NOT NULL"
        ).fetchone()[0]
        print(f"已写入 {len(updates)} 条；表内现有 decision_id {filled} / {n + filled} 条有主。")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
