#!/usr/bin/env python3
"""
第7条：错因三层分类 + 验证执行器

背景：trade_reflections_v2 有 verification / verified_at 字段，但
      verified_at 全为 0（一条都没真跑过），error_layer 只有 4% 被标。
      所以「错因判得对不对」一直无法回答。

做两件事：
  A 强制三层分类（对全部反思，不留空）
     技术错误   ：无法结算/缺价（pnl 为空）→ 数据链路或行情缺失
     信息方向错误：REJECTED/HOLD 但事后该资产出现 ≥1% 的有效移动（漏掉了）
     买卖判断失误：真下了单(BUY/SELL/CLOSE)但 hit=0
  B 给每条错因挂「可验证后果」，独立测量后判 验证/否证/不可分离
     技术错误   → 该日该资产在库里有行情吗？有=链路问题(验证)；无=数据源缺(验证，但类型不同)
     买卖判断   → 当时置信度 ≥0.6 却亏损=过度自信(验证)；低置信亏损=否证
     信息方向   → ±1天内该资产新闻方向 与 事后实际方向 相反=验证

产出表 reflection_3layer，供尺子直接读。

用法：python3 reflection_3layer_verifier.py [--show]
"""
from __future__ import annotations
import os as _os, sys as _sys  # 脚本在 review/ 下，父目录才是 src_python
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), )
from common._paths import SRC_DIR, BACKEND_DIR  # P2: 路径锚点

import argparse
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

BACKEND = BACKEND_DIR
DB = BACKEND / "trident_event_bus.db"
# v412-G 修正：outcome 的单位是**百分点**（0.86 = 0.86%），原值 0.01 等于 0.01%，
# 比"有效移动"小两个数量级，导致几乎所有观望决策都被判"漏掉有效移动"
# （实测 56 条里有 54 条实际涨跌不到 1%，纯属阈值写错）。注释原意就是 1%。
BIG_MOVE = 1.0         # 有效移动阈值 1 个百分点
CONF_GATE = 0.6        # 高置信门槛
# v326: 方向判错 vs 时机偏差的分界 —— 结算幅度 0.3% 以下视为噪声区
DIRECTION_WRONG_MIN_PCT = 0.3


def _news_direction(conn, asset: str, anchor: int):
    """返回同期（±1 天）该资产最近的一条方向性新闻动作 BUY/SELL，查不到返回 None。

    只认 is_noise=0 且有明确 BUY/SELL 的新闻；查不到就返回 None，
    由调用方按"宁缺不猜"处理（不因为查不到就给出有利结论）。
    """
    row = conn.execute(
        "SELECT action FROM news_events WHERE ts BETWEEN ? AND ? AND is_noise=0 "
        "AND (asset=? OR assets LIKE ?) AND action IN ('BUY','SELL') "
        "ORDER BY abs(ts-?) LIMIT 1",
        (anchor - 86400, anchor + 86400, asset, f"%{asset}%", anchor)).fetchone()
    return row[0] if row else None


def run(conn) -> int:
    conn.execute("""CREATE TABLE IF NOT EXISTS reflection_3layer(
        reflection_id INTEGER PRIMARY KEY, asset TEXT, action TEXT, hit INTEGER,
        layer TEXT, sub_layer TEXT, claim TEXT, verdict TEXT, evidence TEXT,
        evaluated_at TEXT, realized_pct REAL)""")
    # v412-G: 原来这里先 `DELETE FROM reflection_3layer` 再逐条计算 —— DELETE 一旦执行
    # 就持有写锁，直到末尾 commit 才释放；而中间要对 650 条反思逐条查 news_events，
    # 计算期间写锁被占住几十秒，两头受害：
    #   · 抢锁时撞上 bridge/engine 正在写 → 直接 `database is locked` 整个任务退出；
    #   · 抢到锁时反过来把 bridge 的写入堵住。
    # 改成"先在内存算完，末尾一个极短事务里 DELETE + INSERT"，写锁窗口从几十秒压到毫秒。

    price_index = {}
    for asset, d in conn.execute(
            "SELECT asset, date(ts,'unixepoch') FROM ohlcv_daily GROUP BY asset, 2"):
        price_index.setdefault((asset or '').strip().upper(), set()).add(d)

    out = []
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows = conn.execute(
        "SELECT r.id, r.asset, r.action, r.hit, r.pnl_pct, r.actual_outcome_pct, "
        "       r.confidence_at_time, r.created_at, r.kline_at_decision, "
        "       (SELECT d.ts FROM ai_decisions d WHERE d.id = r.ai_decision_id) AS dec_ts "
        "FROM trade_reflections_v2 r").fetchall()
    for rid, asset, action, hit, pnl, outcome, conf, created, kline, dec_ts in rows:
        a = (asset or '').strip().upper()
        # created_at 有两种格式：epoch 整数 或 'YYYY-MM-DD HH:MM:SS' 文本
        if isinstance(created, str) and created.strip():
            try:
                created = int(datetime.strptime(created.strip(), "%Y-%m-%d %H:%M:%S")
                              .replace(tzinfo=timezone.utc).timestamp())
            except ValueError:
                created = 0
        else:
            created = int(created or 0)
        d = datetime.fromtimestamp(created, tz=timezone.utc).strftime("%Y-%m-%d") if created else None

        # ---- A 三层分类
        force_verdict = None
        _anchor = int(dec_ts) if dec_ts else created
        # v412-G 修正：只有**该成交却结算不出来**才算技术错误。
        # HOLD/REJECTED 是"观望/被过滤"，本来就没有成交、没有 pnl 是正常的，
        # 把它们判成"技术错误"会造成"98% 的错都是技术故障"这种假象
        # （实测 137/148 条 HOLD 全被冤枉）。
        if pnl is None and action not in ("HOLD", "REJECTED"):
            layer, sub = "技术错误", "无法结算/缺价"
            claim = "该笔因缺价或缺行情未结算"
        elif action in ("BUY", "SELL", "CLOSE"):
            if hit == 0:
                # v326: 把原来的"方向或时机错误"一个桶拆成两个 —— 用户要能一眼分清
                # "方向判错 (认知问题)" 与 "时机/止损没摆好 (执行参数问题)"。
                # 判据: 结算幅度是否越过噪声区。真被打反 = 方向被证伪; 幅度在噪声内
                # = 方向大体成立, 亏损来自入场点/止损位置。
                mag = abs(float(pnl)) if pnl is not None else 0.0
                if mag >= DIRECTION_WRONG_MIN_PCT:
                    layer, sub = "买卖判断失误", "方向判错"
                    claim = ("下单方向与结果相反且幅度 %.2f%% (超出噪声区)，说明方向判断被证伪"
                             % mag)
                else:
                    layer, sub = "买卖判断失误", "时机/止损偏差"
                    claim = ("方向大体成立但幅度仅 %.2f%% (噪声区)，问题在入场点或止损位置"
                             % mag)
            else:
                layer, sub = "无明显错误", "—"
                claim = "方向正确"
        elif action in ("REJECTED", "HOLD"):
            if outcome is None:
                # 观望没成交 → 没有盈亏是对的，不能判"错"；
                # 但如果当日行情也取不到，就无法判断"是否错失移动" → 判不了，保持诚实。
                layer, sub = "无明显错误", "观望未成交"
                claim = "观望未成交，无盈亏；当日行情缺失，无法判断是否错失"
                force_verdict = ("不可分离", "缺当日行情，无法判定是否错失移动")
            elif abs(outcome) >= BIG_MOVE:
                # v412-G: 先查同期新闻方向。若新闻是反方向、价格却朝正方向走，
                # 说明"没跟这一波"是对的（消息与价格背离，跟进反而危险），
                # 不能记成"漏掉有效移动"。查不到新闻就不下这个有利结论（宁缺不猜）。
                _nd = _news_direction(conn, a, _anchor) if _anchor else None
                if _nd and ((_nd == "BUY" and outcome < 0) or (_nd == "SELL" and outcome > 0)):
                    layer, sub = "无明显错误", "避开反向消息"
                    claim = (f"观望期间涨跌 {outcome:+.2f}%，但同期新闻方向与之相反，"
                             f"未跟进是对的")
                else:
                    layer, sub = "信息方向错误", "漏掉有效移动"
                    claim = "过滤/观望期间该资产出现有效移动，说明方向或框架判错"
            else:
                layer, sub = "无明显错误", "—"
                claim = "观望期间无有效移动"
        else:
            layer, sub = "信息方向错误", "其他动作"
            claim = "动作类型异常"

        # ---- B 可验证后果
        if force_verdict is not None:
            verdict, ev = force_verdict
        elif layer == "技术错误":
            has_bar = d in price_index.get(a, set()) if d else False
            # 必须可证伪：库里当日有行情 → 是结算链路漏算（验证）；
            # 当日真的没行情 → 客观缺失，判不了（不可分离），不能一律记验证
            if has_bar:
                verdict, ev = "验证", "当日库内有行情，仍没结算出来→结算链路漏算"
            else:
                verdict, ev = "不可分离", "当日库内无行情，可能客观缺数据，无法证伪"
        elif layer == "买卖判断失误":
            if conf is None:
                verdict, ev = "不可分离", "无当时置信度，无法判定是否过度自信"
            elif conf >= CONF_GATE:
                verdict, ev = "验证", f"当时置信度 {conf:.2f} ≥ {CONF_GATE} 仍亏损→过度自信成立"
            else:
                verdict, ev = "否证", f"当时置信度 {conf:.2f} < {CONF_GATE}，低置信亏损不算判断失误"
        elif layer == "信息方向错误":
            anchor = int(dec_ts) if dec_ts else created
            news = conn.execute(
                "SELECT action FROM news_events WHERE ts BETWEEN ? AND ? AND is_noise=0 "
                "AND (asset=? OR assets LIKE ?) AND action IN ('BUY','SELL') "
                "ORDER BY abs(ts-?) LIMIT 1",
                (anchor - 86400, anchor + 86400, a, f"%{a}%", anchor)).fetchone()
            if not news:
                verdict, ev = "不可分离", "±1天内无同资产方向性新闻，无法判定方向对错"
            elif outcome is not None:
                news_up = news[0] == "BUY"
                move_up = outcome > 0
                if news_up == move_up:
                    verdict, ev = "验证", "新闻方向与事后移动同向，说明漏检成立"
                else:
                    verdict, ev = "否证", "新闻方向与事后移动相反，方向本身判错"
            else:
                verdict, ev = "不可分离", "缺事后收益，无法比对"
        else:
            verdict, ev = "不适用", "无错误，不做错因验证"

        out.append((rid, asset, action, hit, layer, sub, claim, verdict, ev, now, outcome))

    # 极短事务：DELETE 与 INSERT 在同一次提交内完成，失败则整体回滚，
    # 不会出现"清空了但没写进新数据"的空表状态。
    for attempt in range(3):
        try:
            with conn:
                conn.execute("DELETE FROM reflection_3layer")
                conn.executemany(
                    "INSERT OR REPLACE INTO reflection_3layer VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    out)
            break
        except sqlite3.OperationalError as e:  # 锁/忙
            if "locked" not in str(e).lower() and "busy" not in str(e).lower():
                raise
            if attempt == 2:
                raise
            print(f"[VERIFY] 写锁冲突，{1.5 * (attempt + 1):.1f}s 后重试 "
                  f"({attempt + 1}/3): {e}", file=sys.stderr)
            time.sleep(1.5 * (attempt + 1))
    return len(out)


def show(conn) -> None:
    total = conn.execute("SELECT count(*) FROM reflection_3layer").fetchone()[0]
    if not total:
        print("还没有数据，先跑一次")
        return
    print(f"反思总数 {total}（三层分类覆盖率 100%）\n")
    print(f"{'层级':<14}{'条数':>7}{'占比':>8}{'验证':>7}{'否证':>7}{'不可分离':>9}")
    for layer, n, v, f, x in conn.execute(
            "SELECT layer, count(*), "
            "sum(CASE WHEN verdict='验证' THEN 1 ELSE 0 END), "
            "sum(CASE WHEN verdict='否证' THEN 1 ELSE 0 END), "
            "sum(CASE WHEN verdict='不可分离' THEN 1 ELSE 0 END) "
            "FROM reflection_3layer GROUP BY layer ORDER BY 2 DESC").fetchall():
        print(f"{layer:<14}{n:>7}{n / total * 100:>7.1f}%{v or 0:>7}{f or 0:>7}{x or 0:>9}")
    n, v = conn.execute(
        "SELECT count(*), sum(CASE WHEN verdict='验证' THEN 1 ELSE 0 END) "
        "FROM reflection_3layer WHERE verdict IN ('验证','否证')").fetchone()
    if n:
        print(f"\n错因验证一致率 = {v / n * 100:.1f}%  (n={n})")
        print("（一致率 = 被判的错因里，独立测量后确实成立的占比）")
    print(f"错因标注覆盖率：100%（改造前 error_layer 仅 4%）")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", action="store_true")
    args = ap.parse_args()
    if not DB.exists():
        print(f"[ERR] 库不存在: {DB}", file=sys.stderr)
        return 2
    conn = sqlite3.connect(str(DB), timeout=90)
    try:
        conn.execute("PRAGMA busy_timeout=90000")
        if args.show:
            show(conn)
        else:
            n = run(conn)
            print(f"已写入 {n} 条 → reflection_3layer")
            show(conn)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
