"""影子账本 —— 不管闸门放不放行, 每条方向决策都记账、到期结算。

为什么需要它:
  真实链路里, 统一置信度闸门 (当前 20) 把 94% 的方向决策降级成 HOLD,
  一天只剩 0~1 单 —— 统计层拿不到样本, 准确率永远算不出来。
  但"没放行"不等于"没预测": 决策时点的方向、当时的价、4 小时后的价都在库里。
  把三者拼起来, 每一单都是可结算的样本, 一分钱不花。

同时也回答"闸门该设多少": 同一批样本按不同门槛切, 直接对比
样本数 / 命中率 —— 如果高分组和低分组命中率一样, 那道闸门就是筛了个寂寞。

用法:
  python3 shadow_ledger.py --days 30 --dry-run     # 只算不写
  python3 shadow_ledger.py --days 30 --commit      # 写 shadow_ledger 表
  python3 shadow_ledger.py --sweep --days 30       # 只出闸门扫描表
"""
from __future__ import annotations

import argparse
import datetime
import json
import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).resolve().parents[1] / "trident_event_bus.db"

# 只有这几个品种有连续行情, 能算"决策后真实走了多少"
TRADABLE = ("BTC", "ETH", "SOL", "WTI", "XAU")

# 找价容忍窗口: 决策时刻前后 5 分钟内必须有 tick, 否则这一单不记
PRICE_TOL_SEC = 300

DEFAULT_HORIZON_MIN = 240   # 4 小时: 覆盖"新闻落地 → 消化"的常规窗口
GATE_SWEEP = (0.0, 2.0, 5.0, 10.0, 20.0, 30.0)

# 目标样本量：README 定的"能下结论"门槛（≥60 笔已结算）
TARGET_SAMPLES = 60

# 判定闸门有效的最小分组样本数：少于它，命中率就是噪音
MIN_GROUP_N = 5

# 判定"闸门筛出东西"的最小百分点差：±5pt 以内视作没差别
VERDICT_BAND = 5.0


def _connect(readonly: bool) -> sqlite3.Connection:
    if readonly:
        return sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    return sqlite3.connect(DB_PATH)


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS shadow_ledger (
            decision_id     INTEGER PRIMARY KEY,
            decided_ts      INTEGER NOT NULL,
            asset           TEXT    NOT NULL,
            direction       TEXT    NOT NULL,   -- 闸门前的方向 (系统当时怎么想的)
            action_after    TEXT,              -- 闸门之后实际怎么做的 (多半是 HOLD)
            confidence      REAL,
            gate            REAL,
            entry_ts        INTEGER,
            entry_price     REAL,
            horizon_min     INTEGER,
            exit_ts         INTEGER,
            exit_price      REAL,
            move_pct        REAL,
            hit             INTEGER,           -- 方向是否与后续走势一致
            settled_at      INTEGER
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_shadow_unsettled "
                 "ON shadow_ledger(settled_at, decided_ts)")


def nearest_price(conn: sqlite3.Connection, asset: str, ts: int,
                  tol: int = PRICE_TOL_SEC) -> tuple[int, float] | None:
    """取 ts 前后最近的 tick (优先 <= ts 的最后一笔, 没有就取之后的)。"""
    row = conn.execute(
        "SELECT ts, price FROM market_ticks WHERE symbol=? AND ts<=? AND ts>=? "
        "ORDER BY ts DESC LIMIT 1", (asset, ts, ts - tol)).fetchone()
    if row:
        return int(row[0]), float(row[1])
    row = conn.execute(
        "SELECT ts, price FROM market_ticks WHERE symbol=? AND ts>? AND ts<=? "
        "ORDER BY ts ASC LIMIT 1", (asset, ts, ts + tol)).fetchone()
    if row:
        return int(row[0]), float(row[1])
    return None


def collect(conn: sqlite3.Connection, days: int, horizon: int) -> list[dict]:
    """把窗口内的方向决策 + 入场价 + 到期价拼成可结算样本。

    v400: 剔除 analysis_degraded=1 的方向结论。
    降级路径（target_none / llm_empty / summary_only / server_fallback）产出的
    BUY/SELL 不是推演结果，实测 3 笔成交里 2 笔来自降级。把它们留在账本里，
    等于用模板拼出来的方向给"判断能力"打分 —— R11 只拦建仓，拦不住记账。
    """
    since = int(datetime.datetime.now().timestamp()) - days * 86400
    rows = conn.execute(
        """SELECT id, ts, target_asset, ungated_action, suggested_action,
                  evidence_confidence
           FROM ai_decisions
           WHERE ungated_action IN ('BUY','SELL')
             AND target_asset IN ({})
             AND ts >= ?
             AND COALESCE(analysis_degraded, 0) = 0
           ORDER BY ts""".format(",".join("?" * len(TRADABLE)),
                                 ), (*TRADABLE, since)).fetchall()

    out = []
    for did, ts, asset, direction, action, conf in rows:
        entry = nearest_price(conn, asset, int(ts))
        if not entry:
            continue
        exit_ = nearest_price(conn, asset, int(ts) + horizon * 60)
        item = {
            "decision_id": did, "decided_ts": int(ts), "asset": asset,
            "direction": direction, "action_after": action,
            "confidence": float(conf or 0.0),
            "entry_ts": entry[0], "entry_price": entry[1],
            "horizon_min": horizon,
        }
        if exit_:
            move = (exit_[1] - entry[1]) / entry[1] * 100.0
            hit = 1 if ((direction == "BUY" and move > 0)
                        or (direction == "SELL" and move < 0)) else 0
            item.update({"exit_ts": exit_[0], "exit_price": exit_[1],
                         "move_pct": round(move, 4), "hit": hit})
        out.append(item)
    return out


def write(conn: sqlite3.Connection, items: list[dict]) -> int:
    ensure_table(conn)
    now = int(datetime.datetime.now().timestamp())
    n = 0
    for it in items:
        conn.execute(
            """INSERT INTO shadow_ledger
               (decision_id, decided_ts, asset, direction, action_after, confidence,
                entry_ts, entry_price, horizon_min, exit_ts, exit_price, move_pct,
                hit, settled_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(decision_id) DO UPDATE SET
                 action_after=excluded.action_after,
                 exit_ts=excluded.exit_ts, exit_price=excluded.exit_price,
                 move_pct=excluded.move_pct, hit=excluded.hit,
                 settled_at=excluded.settled_at""",
            (it["decision_id"], it["decided_ts"], it["asset"], it["direction"],
             it["action_after"], it["confidence"], it["entry_ts"], it["entry_price"],
             it["horizon_min"], it.get("exit_ts"), it.get("exit_price"),
             it.get("move_pct"), it.get("hit"),
             now if it.get("hit") is not None else None))
        n += 1
    conn.commit()
    return n


def prune_invalid(conn: sqlite3.Connection) -> int:
    """清掉语义上不该留在账本里的行，返回删除条数。

    背景：v400 才在 collect() 入口加了 analysis_degraded=0 过滤，但账本在此之前
    已经写进去了降级路径拼出来的方向（实测 35 条里 25 条是降级）。留着它们，等于
    用模板拼的方向给"判断能力"打分 —— R11 只拦建仓，拦不住记账。

    只删"这条决策本来就不该记"的行（降级 / 无方向 / 池外 / 决策已被删），
    **不按时间窗口删**：老样本也是样本，只要判断是真的就留着。
    """
    ensure_table(conn)
    q = """DELETE FROM shadow_ledger WHERE decision_id IN (
             SELECT s.decision_id FROM shadow_ledger s
             LEFT JOIN ai_decisions d ON d.id = s.decision_id
             WHERE d.id IS NULL
                OR COALESCE(d.analysis_degraded, 0) = 1
                OR d.ungated_action NOT IN ('BUY','SELL')
                OR d.target_asset NOT IN ({})
           )""".format(",".join("?" * len(TRADABLE)))
    cur = conn.execute(q, TRADABLE)
    conn.commit()
    return int(cur.rowcount or 0)


def _cn_date(ts: int) -> str:
    """按北京时间切日期，与前端/端点 `+8 hours` 口径一致。"""
    tz = datetime.timezone(datetime.timedelta(hours=8))
    return datetime.datetime.fromtimestamp(int(ts), tz).date().isoformat()


def sweep_verdict(sweep: list[dict], min_n: int = MIN_GROUP_N,
                  band: float = VERDICT_BAND) -> dict:
    """把闸门扫描表翻译成一句人话。

    这是整套影子账本真正要回答的问题：置信度闸门（20）到底是在筛好坏，
    还是只是把样本筛掉了。用同一批决策按门槛切，比较"不设门槛"与"门槛 20"：
      · 高分组更准（差 ≥ band）→ effective
      · 高分组更不准（差 ≤ -band）→ inverted
      · 差在 band 以内 → flat（筛了个寂寞）
      · 门槛 20 剩不到 min_n 条 → insufficient（样本不够，先不评）
    """
    base = next((r for r in sweep if float(r["gate"]) == 0.0), None)
    high = next((r for r in sweep if float(r["gate"]) == 20.0), None)
    if not base or not high:
        return {"code": "no_base", "text": "缺少门槛 0 或 20 的分组，无法判定"}
    if base.get("hit_rate") is None:
        return {"code": "no_base", "text": "门槛 0 分组还没有已结算样本，无法判定"}
    if int(high.get("n") or 0) < min_n:
        return {"code": "insufficient", "delta": None,
                "text": (f"门槛 20 只剩 {int(high.get('n') or 0)} 条"
                         f"（<{min_n} 条），样本不足：先攒样本，别急着评闸门")}
    delta = float(high["hit_rate"]) - float(base["hit_rate"])
    if delta >= band:
        code, text = "effective", f"门槛 20 比不设门槛准 {delta:+.1f}pt，闸门有效"
    elif delta <= -band:
        code, text = "inverted", f"门槛 20 比不设门槛差 {delta:+.1f}pt，闸门反向（高置信反而更不准）"
    else:
        code, text = "flat", f"高低分组命中率只差 {delta:+.1f}pt，闸门基本没筛出东西"
    return {"code": code, "delta": round(delta, 1), "text": text}


def summarize(items: list[dict], days: int, horizon: int) -> dict:
    """把一批样本压成一份可直接落盘/上屏的快照（与 /api/v291 端点同口径）。"""
    settled = [i for i in items if i.get("hit") is not None]
    dates = {_cn_date(i["decided_ts"]) for i in settled}
    n, hit = len(settled), sum(i["hit"] for i in settled)

    def _rate(h: int, m: int) -> float | None:
        return round(100 * h / m, 1) if m else None

    assets: dict[str, dict] = {}
    for i in settled:
        a = assets.setdefault(i["asset"], {"asset": i["asset"], "n": 0, "hit": 0})
        a["n"] += 1
        a["hit"] += int(i["hit"])
    dirs: dict[str, dict] = {}
    for i in settled:
        d = dirs.setdefault(i["direction"], {"direction": i["direction"], "n": 0, "hit": 0})
        d["n"] += 1
        d["hit"] += int(i["hit"])

    sweep_rows = []
    for g in GATE_SWEEP:
        sub = [i for i in settled if float(i.get("confidence") or 0.0) >= g]
        sweep_rows.append({"gate": g, "n": len(sub),
                           "hit_rate": _rate(sum(i["hit"] for i in sub), len(sub))})

    return {
        "days": days,
        "horizon_min": horizon,
        "samples": len(items),
        "settled": n,
        "hit_rate": _rate(hit, n),
        "avg_move_pct": round(sum(i["move_pct"] for i in settled) / n, 3) if n else None,
        "avg_abs_move_pct": round(sum(abs(i["move_pct"]) for i in settled) / n, 3) if n else None,
        "days_covered": len(dates),
        "samples_per_day": round(n / max(len(dates), 1), 1),
        "target_samples": TARGET_SAMPLES,
        "remaining_to_target": max(TARGET_SAMPLES - n, 0),
        "by_asset": [{"asset": v["asset"], "n": v["n"],
                      "hit_rate": _rate(v["hit"], v["n"])} for v in assets.values()],
        "by_direction": [{"direction": v["direction"], "n": v["n"],
                          "hit_rate": _rate(v["hit"], v["n"])} for v in dirs.values()],
        "gate_sweep": sweep_rows,
        "sweep_verdict": sweep_verdict(sweep_rows),
    }


def report(items: list[dict]) -> None:
    settled = [i for i in items if i.get("hit") is not None]
    if not settled:
        print("没有可结算样本")
        return
    days = len({datetime.datetime.fromtimestamp(i["decided_ts"]).date()
                for i in settled})
    hit = sum(i["hit"] for i in settled)
    print(f"\n可结算样本 {len(settled)} 条 / 跨 {days} 天 "
          f"→ 平均 {len(settled) / max(days,1):.1f} 条/天")
    print(f"整体方向命中率 {100 * hit / len(settled):.1f}%  "
          f"(50% = 抛硬币; 低于 50% 说明方向判断还不如随机)")
    print(f"平均 |涨跌| {sum(abs(i['move_pct']) for i in settled) / len(settled):.2f}%"
          f"  · 平均 {sum(i['move_pct'] for i in settled) / len(settled):+.3f}%")


def sweep(items: list[dict]) -> None:
    """同一批样本, 按不同门槛切 —— 看闸门到底有没有筛出东西。"""
    settled = [i for i in items if i.get("hit") is not None]
    print("\n=== 闸门扫描 (同一批决策, 只看门槛 ≤ 这个值的) ===")
    print(f"{'门槛':>6} {'样本':>6} {'占比':>7} {'命中率':>8} {'条/天':>7}")
    days = {_cn_date(i["decided_ts"]) for i in settled}
    rows = []
    for g in GATE_SWEEP:
        sub = [i for i in settled if float(i.get("confidence") or 0.0) >= g]
        rows.append({"gate": g, "n": len(sub),
                     "hit_rate": (round(100 * sum(i["hit"] for i in sub) / len(sub), 1)
                                  if sub else None)})
        if not sub:
            print(f"{g:>6.0f} {0:>6} {'0.0%':>7} {'-':>8} {'0.0':>7}")
            continue
        hit = sum(i["hit"] for i in sub)
        pct = 100 * len(sub) / len(settled) if settled else 0.0
        print(f"{g:>6.0f} {len(sub):>6} {pct:>6.1f}% "
              f"{100*hit/len(sub):>7.1f}% {len(sub)/max(len(days),1):>7.1f}")
    v = sweep_verdict(rows)
    print(f"\n结论: {v['text']}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--horizon", type=int, default=DEFAULT_HORIZON_MIN)
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--sweep", action="store_true")
    ap.add_argument("--json-out", type=str, default="",
                    help="把本批快照写成 JSON（每日 cron 用来留档）")
    ap.add_argument("--snapshot-dir", type=str, default="",
                    help="把 latest.json + YYYY-MM-DD.json 写进该目录，并清理 90 天前的快照。"
                         "落盘放在 Python 里做，是因为 launchd 下的 /bin/bash 拿不到"
                         "桌面目录权限（cp/find 都会 EPERM），python3 才有。")
    ap.add_argument("--keep-days", type=int, default=90)
    ap.add_argument("--prune-only", action="store_true",
                    help="只清账本里的降级/无效行，不采集不写")
    a = ap.parse_args()

    if a.prune_only:
        conn = _connect(readonly=False)
        try:
            n = prune_invalid(conn)
        finally:
            conn.close()
        print(f"已删除无效行: {n} 条")
        return 0

    ro = not a.commit
    conn = _connect(ro)
    try:
        items = collect(conn, a.days, a.horizon)
        pruned = prune_invalid(conn) if a.commit else 0
    finally:
        if ro:
            conn.close()

    report(items)
    sweep(items)

    snap = summarize(items, a.days, a.horizon)
    snap["pruned"] = pruned
    snap["generated_at"] = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    print(f"\n样本 {snap['settled']} 条 / 目标 {TARGET_SAMPLES} 条"
          f"（还差 {snap['remaining_to_target']}）"
          f"，{snap['days_covered']} 天均 {snap['samples_per_day']} 条/天")

    if a.json_out:
        out = Path(a.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"快照已写: {out}")

    if a.snapshot_dir:
        d = Path(a.snapshot_dir)
        d.mkdir(parents=True, exist_ok=True)
        blob = json.dumps(snap, ensure_ascii=False, indent=2)
        (d / "latest.json").write_text(blob, encoding="utf-8")
        dated = d / f"{_cn_date(int(datetime.datetime.now().timestamp()))}.json"
        dated.write_text(blob, encoding="utf-8")
        cutoff = datetime.datetime.now().timestamp() - a.keep_days * 86400
        for f in d.glob("20*.json"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
            except OSError:
                pass
        print(f"快照已归档: {d}/latest.json + {dated.name}")

    if a.commit:
        n = write(conn, items)
        conn.close()
        print(f"\n已写入 shadow_ledger: {n} 条（清理无效行 {pruned} 条）")
    else:
        print("\n[dry-run] 未写库。加 --commit 落库。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
