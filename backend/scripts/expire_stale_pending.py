#!/usr/bin/env python3
"""过期队列清扫：把积压超过 N 小时的 PENDING 新闻作废。

事故（2026-11-20）：暂停 5 小时后队列积压 390 条，最老 21 小时。引擎按 FIFO 消费，
旧新闻既没分析价值（价格已走完），又把新新闻顶在后面 —— 队列看着"有货"，实际产能停摆。
本脚本每 10 分钟把超龄 PENDING 标成 EXPIRED（filter_reason=stale_hours），
只动状态列，不改内容、不删行。
"""
import argparse
import os
import sqlite3
import time

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  "trident_event_bus.db")
PAUSE = os.path.expanduser("~/.trident/state/DATA_PAUSED")


def _exec(con, sql, args, tries=4):
    """写语句带重试：引擎批次落库时会短时独占写锁，单次 15s 超时就抛
    database is locked（实测 launchd 里退出码 1，队列清扫静默失效）。
    这里按「锁」类错误退避重试，最多 4 次，其它错误照旧抛出。"""
    for i in range(tries):
        try:
            return con.execute(sql, args)
        except sqlite3.OperationalError as e:
            if "locked" not in str(e).lower() or i == tries - 1:
                raise
            time.sleep(1.5 * (i + 1))



def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=2.0)
    ap.add_argument("--limit", type=int, default=2000)
    ap.add_argument("--trade-min", type=float, default=45.0,
                    help="可交易 PENDING 超过多少分钟作废（默认 45，与建仓窗口一致）")
    ap.add_argument("--stuck-min", type=float, default=6.0,
                    help="PROCESSING 超过多少分钟算卡死（默认 6）")
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()
    if os.path.exists(PAUSE):
        print("[EXPIRE] 数据暂停中，跳过（暂停期积压要保留，恢复后再清）")
        return 0
    cutoff = int(time.time()) - int(a.hours * 3600)
    _now_str = lambda: time.strftime("%Y-%m-%d %H:%M:%S")
    con = sqlite3.connect(DB, timeout=30)
    try:
        con.execute("PRAGMA busy_timeout=30000")
        n = con.execute(
            "SELECT COUNT(*) FROM raw_news WHERE status='PENDING' AND ts IS NOT NULL AND ts < ?",
            (cutoff,)).fetchone()[0]
        if a.dry or not n:
            print(f"[EXPIRE] 待作废 {n} 条（>{a.hours}h）")
        else:
            con.execute(
                "UPDATE raw_news SET status='FILTERED', filter_reason=?, "
                "processing_at=COALESCE(processing_at, ?) "
                "WHERE id IN (SELECT id FROM raw_news WHERE status='PENDING' "
                "AND ts IS NOT NULL AND ts < ? ORDER BY ts LIMIT ?)",
                (f"stale>{int(a.hours)}h", int(time.time()), cutoff, a.limit))
            con.commit()
            print(f"[EXPIRE] 已作废 {n} 条超龄 PENDING（>{a.hours}h）")
        if a.dry:
            return 0

        # 建仓窗口外的「可交易」也作废：45 分钟后已不能建仓，再分析只会白花 token。
        # 与引擎 news_trade_max_age_seconds=2700 同口径。
        try:
            win = int(a.trade_min * 60)
            cur5 = con.execute(
                "UPDATE raw_news SET status='FILTERED', filter_reason='tradable_expired', "
                "processing_at=NULL WHERE status='PENDING' AND gate_verdict='可交易' "
                "AND ts IS NOT NULL AND ts < ?", (int(time.time()) - win,))
            tn = int(cur5.rowcount or 0)
            if tn:
                print(f"[EXPIRE] 建仓窗口外可交易作废 {tn} 条（>{a.trade_min:g} 分钟）")
        except Exception as e:
            print("[EXPIRE] 可交易作废失败:", type(e).__name__, e)

        # ── 自愈 1：PROCESSING 但决策已落库 ─────────────────────────────
        # 事故（2026-11-20 17:19 批）：4 条分析成功、token 已计，第一条决策
        # #13591 写完后就卡住，后面 3 条连状态都没回写 → 行永远 PROCESSING。
        # 若不修：6 分钟后"卡死回收"会把它放回 PENDING **重新分析**（再烧一次
        # token），而决策其实已经有了。有决策 = 已产出，直接补 DONE 才是对的。
        try:
            rep_ids = [r[0] for r in con.execute(
                "SELECT DISTINCT r.id FROM raw_news r JOIN ai_decisions d "
                "ON d.news_id = r.id WHERE r.status='PROCESSING'").fetchall()]
            if rep_ids and not a.dry:
                _exec(con, "UPDATE raw_news SET status='DONE', processing_at=NULL "
                      "WHERE id IN (%s)" % ",".join(str(i) for i in rep_ids), ())
                con.commit()
                print(f"[EXPIRE] 决策已落库但状态卡住 → 补 DONE {len(rep_ids)} 条"
                      f"（放回 PENDING 会重复烧 token）：{rep_ids[:8]}")
        except Exception as e:
            print("[EXPIRE] 自愈 DONE 失败:", type(e).__name__, e)

        # 卡死的 PROCESSING：认领后 10 分钟没结果 → 放回队列重试；超 3 次判 FAILED。
        # 事故（2026-11-20）：两条 16:13 认领后 11 分钟无任何 LLM 记录 —— 线程卡在网络等待，
        # 队列既不出结果也不释放，看起来"有货"其实停摆。
        stuck = int(a.stuck_min * 60)
        now_s = int(time.time())
        # 注意：engine 认领时会重置 retry_count，所以卡死次数不能用它算，
        # 改用 filter_reason 里的 stuck:N 自己计数（engine 不清这个字段）。
        rows = con.execute(
            "SELECT id, COALESCE(filter_reason,'') FROM raw_news "
            "WHERE status='PROCESSING' AND processing_at IS NOT NULL "
            "AND (julianday(?)-julianday(processing_at))*86400 > ?",
            (_now_str(), stuck)).fetchall()
        back = dead = 0
        for _id, _fr in rows:
            try:
                n = int(str(_fr).split("stuck:")[1].split("|")[0]) if "stuck:" in str(_fr) else 0
            except Exception:
                n = 0
            n += 1
            if n >= 3:
                _exec(con, "UPDATE raw_news SET status='FAILED', processing_at=NULL, "
                      "filter_reason=? WHERE id=?", ("stuck:%d|giveup" % n, _id))
                dead += 1
            else:
                _exec(con, "UPDATE raw_news SET status='PENDING', processing_at=NULL, "
                      "filter_reason=? WHERE id=?", ("stuck:%d" % n, _id))
                back += 1
        if back or dead:
            print(f"[EXPIRE] 卡死 PROCESSING 放回 {back} 条 / 判死 {dead} 条")
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
