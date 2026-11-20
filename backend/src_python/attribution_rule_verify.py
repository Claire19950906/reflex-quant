#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""attribution_rule_verify.py — 归因的机器自检（第 6 条「双重验证」第一道，v338）

为什么要它
----------
`event_move_attribution` 里的 algo_score 只由关键词/资产别名/宏观兜底算出来，
实测错配很明显：

    "比特币下跌2.2%…"        → 归因到 SOL / ETH 的异动
    "Callout Rewards 改版"    → 归因到 ETH 的 slow_fall
    "AAVE 近四周下跌30%"      → 归因到 BTC

而 `ai_called=1` 只有 24/254（9.4%）—— LLM 通道全部熔断/额度耗尽，
第二意见实际上跑不起来。于是"双重验证"只剩一道，且这道本身不校验资产。

本模块是**零 LLM 的第二道**（规则自检），三票制：
  A. asset   标题里有没有这个资产？（同板块龙头算 weak，完全无关算 fail）
  B. direction 标题方向词与行情方向是否相反？
  C. timing  新闻是否早于异动，且滞后 ≤ 4h？（晚于异动 = 事后归因）

  0 fail → confirm · 1 fail → partial · ≥2 fail → reject

结果写 rule_verdict / rule_reason / rule_checked_at，**不改动主表行**，
也不删行（AI 复核才做删除，规则复核只打标）——先量出污染面，再决定清理。

用法:
    python3 attribution_rule_verify.py --dry-run
    python3 attribution_rule_verify.py
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

BACKEND_DIR = Path(__file__).resolve().parents[1]
DB_PATH = BACKEND_DIR / "trident_event_bus.db"

_HASH_PREFIX_RE = re.compile(r"^\s*\[hash:[0-9a-fA-F]+\]\s*")

ASSET_ALIASES: Dict[str, Tuple[str, ...]] = {
    "BTC": ("btc", "bitcoin", "比特币"),
    "ETH": ("eth", "ethereum", "ether", "以太坊", "以太币"),
    "SOL": ("sol", "solana", "索拉纳"),
    "XAU": ("xau", "gold", "黄金", "金价"),
    "WTI": ("wti", "oil", "crude", "原油", "布伦特", "brent"),
    "XRP": ("xrp", "ripple", "瑞波"),
    "BNB": ("bnb", "binance coin", "币安币"),
    "DOGE": ("doge", "dogecoin", "狗狗币"),
    "MU": ("mu", "micron", "美光"),
    "AMD": ("amd", "超威"),
    "META": ("meta", "facebook", "脸书"),
    "SKHYNIX": ("sk hynix", "hynix", "海力士"),
}

# 同板块龙头: 龙头的新闻可以解释跟跌/跟涨，但只能算 weak
SECTOR_LEADERS: Dict[str, Tuple[str, ...]] = {
    "ETH": ("BTC",), "SOL": ("BTC", "ETH"), "BNB": ("BTC",),
    "DOGE": ("BTC",), "XRP": ("BTC",), "ADA": ("BTC",),
}

BULL_WORDS = ("上涨", "大涨", "飙升", "走高", "反弹", "利好", "超预期", "增持",
              "买入", "突破", "新高", "上调", "看多", "获批", "rally", "surge",
              "gain", "soar", "beat")
BEAR_WORDS = ("下跌", "大跌", "暴跌", "走低", "回落", "利空", "不及预期", "减持",
              "卖出", "抛售", "跌破", "新低", "下调", "看空", "黑客", "攻击",
              "起诉", "crash", "plunge", "drop", "fall", "miss", "hack")

_MAX_LAG_SEC = 4 * 3600


def _clean(raw: Any) -> str:
    s = str(raw or "").strip()
    s = _HASH_PREFIX_RE.sub("", s)
    return re.sub(r"\s+", " ", s)


def _hit_assets(text: str, names: Tuple[str, ...]) -> bool:
    low = text.lower()
    for n in names:
        if re.search(r"(?<![a-z0-9])%s(?![a-z0-9])" % re.escape(n.lower()), low):
            return True
    return False


def check_asset(asset: str, headline: str, ev_assets: str, ev_action: str) -> Tuple[str, str]:
    a = str(asset or "").upper()
    if ev_action in ("CROSS", "MACRO") or not a:
        return "na", "跨资产/宏观事件，资产票不适用"
    if _hit_assets(headline, ASSET_ALIASES.get(a, (a.lower(),))):
        return "pass", "标题直接提及该资产"
    for leader in SECTOR_LEADERS.get(a, ()):
        if _hit_assets(headline, ASSET_ALIASES.get(leader, (leader.lower(),))):
            return "weak", "标题只提及同板块龙头 %s，属传导解释" % leader
    # 新闻自带的 assets 字段里如果列了该资产，也算弱证据
    if _hit_assets(str(ev_assets or ""), ASSET_ALIASES.get(a, (a.lower(),))):
        return "weak", "事件资产列表含该资产，但标题未提及"
    return "fail", "标题与该资产无关"


def check_direction(headline: str, move_pct: float) -> Tuple[str, str]:
    low = str(headline or "").lower()
    bull = sum(1 for w in BULL_WORDS if w.lower() in low)
    bear = sum(1 for w in BEAR_WORDS if w.lower() in low)
    if bull == 0 and bear == 0:
        return "na", "标题无明确方向词"
    if move_pct == 0:
        return "na", "异动幅度为 0"
    news_dir = 1 if bull > bear else -1 if bear > bull else 0
    if news_dir == 0:
        return "na", "方向词互相抵消"
    price_dir = 1 if move_pct > 0 else -1
    if news_dir == price_dir:
        return "pass", "方向一致"
    return "fail", "标题方向(%s)与行情(%s)相反" % (
        "多" if news_dir > 0 else "空", "涨" if price_dir > 0 else "跌")


def check_timing(ev_ts: Any, move_ts: Any, lag: Any) -> Tuple[str, str]:
    try:
        ev, mv = int(ev_ts or 0), int(move_ts or 0)
    except (TypeError, ValueError):
        return "na", "时间戳缺失"
    if ev <= 0 or mv <= 0:
        return "na", "时间戳缺失"
    if ev > mv:
        return "fail", "新闻晚于异动 %.0f 分钟，属事后归因" % ((ev - mv) / 60.0)
    lag_s = int(lag or 0) or (mv - ev)
    if lag_s > _MAX_LAG_SEC:
        return "fail", "滞后 %.1f 小时，超出 4h 窗口" % (lag_s / 3600.0)
    return "pass", "新闻先于异动 %.0f 分钟" % (lag_s / 60.0)


def _flow_vote(conn: Optional[sqlite3.Connection], asset: str,
               move_ts: int, move_pct: float) -> Tuple[str, str]:
    """v490 P1-2: 资金面佐证票（OI / 多空比 / 费率 / 量能）。

    与前三票不同，这一票**不直接判成败**：资金数据缺失记 na，方向一致记
    align，背离记 diverge，仅作佐证写进 rule_reason，供归因可信度参考。
    只取异动时点之前已存在的快照（无未来函数）。
    """
    if conn is None or not asset:
        return ("na", "无资金面连接")
    try:
        rows = conn.execute(
            "SELECT kind, ts, value FROM flow_snapshots "
            "WHERE asset=? AND ts <= ? ORDER BY ts DESC LIMIT 60",
            ((asset or "").upper(), int(move_ts)),
        ).fetchall()
    except sqlite3.OperationalError:
        return ("na", "无资金面表")
    if not rows:
        return ("na", "异动前无资金快照")
    by_kind: Dict[str, list] = {}
    for kind, ts, val in rows:
        try:
            by_kind.setdefault(str(kind), []).append((int(ts), float(val)))
        except (TypeError, ValueError):
            continue
    move_dir = 1 if (move_pct or 0) > 0 else -1
    notes: List[str] = []
    diverge = 0
    align = 0
    # 多空比：最近一条相对窗口内基准的方向
    for kind, label in (("lsratio", "多空比"), ("funding", "费率"), ("oi", "持仓")):
        seq = by_kind.get(kind) or []
        if len(seq) < 2:
            continue
        seq.sort(key=lambda x: x[0])
        base = seq[0][1]
        near = seq[-1][1]
        if not base or abs(base) < 1e-9:
            continue
        delta = (near - base) / abs(base)
        if abs(delta) < (0.08 if kind == "lsratio" else 0.0005):
            continue
        k_dir = 1 if delta > 0 else -1
        if k_dir == move_dir:
            align += 1
            notes.append(f"{label}同向({delta:+.0%})")
        else:
            diverge += 1
            notes.append(f"{label}背离({delta:+.0%})")
    if by_kind.get("volume"):
        notes.append("有量能")
    if not notes:
        return ("na", "资金面平淡")
    if diverge > align:
        return ("diverge", "/".join(notes))
    if align:
        return ("align", "/".join(notes))
    return ("weak", "/".join(notes))


def verify(row: Dict[str, Any], conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    headline = _clean(row.get("headline"))
    votes = [
        ("asset", *check_asset(row.get("asset"), headline, row.get("ev_assets"),
                               row.get("ev_action"))),
        ("direction", *check_direction(headline, float(row.get("move_pct") or 0.0))),
        ("timing", *check_timing(row.get("ev_ts"), row.get("move_ts"), row.get("time_lag_sec"))),
    ]
    # v490 P1-2: 第四票资金面佐证（不参与 fail 计数，只写入理由）
    flow_status, flow_detail = _flow_vote(
        conn, row.get("asset") or "", int(row.get("move_ts") or 0),
        float(row.get("move_pct") or 0.0))
    votes.append(("flow", flow_status, flow_detail))
    fails = [v for v in votes if v[1] == "fail"]
    weaks = [v for v in votes if v[1] == "weak"]
    if not fails and not weaks:
        verdict = "confirm"
    elif len(fails) >= 2:
        verdict = "reject"
    else:
        verdict = "partial"
    reason = " | ".join("%s:%s(%s)" % (n, s, d) for n, s, d in votes)
    return {"verdict": verdict, "reason": reason[:400],
            "fail_n": len(fails), "weak_n": len(weaks)}


def ensure_columns(conn: sqlite3.Connection) -> None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(event_move_attribution)")}
    for col, ddl in (("rule_verdict", "TEXT"), ("rule_reason", "TEXT"),
                     ("rule_checked_at", "INTEGER")):
        if col not in cols:
            conn.execute("ALTER TABLE event_move_attribution ADD COLUMN %s %s" % (col, ddl))
            print("[migrate] +%s" % col)


def verify_all(conn: sqlite3.Connection, limit: int = 0,
               dry_run: bool = False) -> Dict[str, Any]:
    """对 event_move_attribution 现有行做三票自检并写回。

    被两组调用方共用:
      · 命令行 `python3 attribution_rule_verify.py`
      · calc_event_move_attribution.main() —— 归因表是 DELETE+INSERT **整表重建**,
        手动打标下一轮就被清空 (实测验收 #12 从 59.4% 掉到 34.6%)。
        必须接在重建之后, 否则 rule_verdict 只是第一轮的快照。
    """
    ensure_columns(conn)
    # v338: 必须拿写锁再读。归因表由 calc_event_move_attribution 以
    # DELETE+INSERT 方式重建, 读与写之间只要发生一次重建, 行 id 就失效 ——
    # 上一版实测: SELECT 到 254 行后表被重建, UPDATE 全部打空 (0 命中),
    # 表里留下 91 行 rule_verdict=NULL, 验收 #12 从 59% 掉到 34%。
    # SQLite 默认 busy_timeout=0 —— 拿不到锁立刻报 "database is locked"。
    # 实测背景: calc 重建 commit 释放写锁的瞬间, 其他 worker 抢锁,
    # 这里 BEGIN IMMEDIATE 立即失败, 异常被 calc 的 try/except 吞掉,
    # 整轮 0 标注; 窗口外的旧标注行留下来, 验收 #12 于是从 59.4% 掉到 34.65%
    # (88 条 confirm 全是窗口外残留)。加 busy_timeout + 退避重试。
    try:
        conn.execute("PRAGMA busy_timeout=60000")
    except sqlite3.Error:
        pass
    for _attempt in range(3):
        try:
            conn.execute("BEGIN IMMEDIATE")
            break
        except sqlite3.OperationalError as _exc:
            if "within a transaction" in str(_exc):
                break  # 调用方自己已开事务, 沿用该事务
            if _attempt == 2:
                raise
            time.sleep(1.5)
    sql = """SELECT a.id, a.asset, a.move_pct, a.move_kind, a.move_ts, a.ev_ts,
                    a.time_lag_sec, a.ev_action, a.algo_score,
                    COALESCE(NULLIF(n.headline, ''), r.content) AS headline,
                    n.assets AS ev_assets
               FROM event_move_attribution a
               LEFT JOIN news_events n ON n.id = a.ev_news_id
               LEFT JOIN raw_news r ON r.id = n.news_id"""
    if limit > 0:
        sql += " LIMIT %d" % limit
    _cursor = conn.execute(sql)
    _cols = [d[0] for d in _cursor.description]
    rows = _cursor.fetchall()
    # 不能假设调用方的行工厂。calc_event_move_attribution 的连接是 tuple 行，
    # `dict(tuple)` 直接 `TypeError: object is not iterable` —— v338 把本函数接进
    # 重建后一直是这个异常被 try/except 吞掉，表现为「标注随机丢失」：
    # 窗口外旧标注留着、窗口内新行永远 NULL，验收 #12 卡在 34.65%。
    dict_rows = [dict(zip(_cols, rr)) for rr in rows]

    results = []
    dist: Counter = Counter()
    now = int(time.time())
    for r in dict_rows:
        v = verify(r, conn)
        dist[v["verdict"]] += 1
        results.append((v["verdict"], v["reason"], now, int(r["id"])))

    if not dry_run and results:
        conn.executemany(
            """UPDATE event_move_attribution
                  SET rule_verdict = ?, rule_reason = ?, rule_checked_at = ?
                WHERE id = ?""", results)
    # BEGIN IMMEDIATE 必须显式收尾: 不 commit 就退出 = 全部回滚,
    # dry-run 则 rollback 释放写锁。
    if dry_run:
        conn.rollback()
    else:
        conn.commit()
    return {"total": len(results), "dist": dict(dist), "rows": rows,
            "dry_run": dry_run}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DB_PATH))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    out = verify_all(conn, limit=args.limit, dry_run=args.dry_run)

    total = out["total"] or 1
    dist = out["dist"]
    print("归因 %d 条 → rule_verdict: %s" % (out["total"], dist))
    print("  reject 占比 %.1f%% · partial 占比 %.1f%%" % (
        dist.get("reject", 0) / total * 100, dist.get("partial", 0) / total * 100))
    for r in out["rows"][:5]:
        v = verify(dict(r))
        print("  [%s] %s %s %.2f%% ← %s" % (
            v["verdict"], r["asset"], r["move_kind"], float(r["move_pct"] or 0),
            _clean(r["headline"])[:60]))

    if args.dry_run:
        print("dry-run: 未写入")
    else:
        print("已写入 %d 条 rule_verdict" % out["total"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
