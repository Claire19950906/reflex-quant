#!/usr/bin/env python3
"""美股/A股行情补齐（v451, 2026-11-19）。

背景：执行器只在「market_ticks 里有该标的的最新价」时才肯下单（paper 模式靠
_make_db_price_lookup 从库里取价）。原先只有 BTC/ETH/SOL/XAU/WTI 五个品种有行情，
于是主分析对 NVDA / MSTR / 510300 / 002241 这些标的给出 BUY/SELL 后，执行器一律
`asset_unsupported` 拒掉 —— 信号是真的，只是没法定成交价。

数据源：腾讯行情（qt.gtimg.cn），一个接口同时覆盖 A 股（sh/sz）与美股（us），
返回 GBK 文本，字段：1=名称, 2=代码, 3=现价, 4=昨收, 5=今开。
写入 market_ticks（symbol 用资产代码本身，和执行器 symbol_map 一致），
source='tencent-eq'，change24h 由 现价/昨收-1 算出。

用法：
    python3 scripts/fetch_equity_quotes.py            # 增量：库里近 7 天出现过的标的
    python3 scripts/fetch_equity_quotes.py --all      # 额外带上常用清单
    python3 scripts/fetch_equity_quotes.py --dry-run  # 只打印不写库
"""
from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
import time
import urllib.request

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "trident_event_bus.db")
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122 Safari/537.36"

# 非股票类标的（已有专门行情源，或根本不该由本脚本喂）
# v489：加了 "NONE" —— ai_decisions.target_asset 的占位值也是 NONE，
# 它会被 _to_tencent 变成 "usNONE"，每轮白拉一次还进"未取到"名单（纯噪音）。
SKIP = {"BTC", "ETH", "SOL", "BNB", "XAU", "GOLD", "WTI", "BRENT", "USDT",
        "", "CASH", "NONE", "-", "—"}
DEFAULT_LIST = [
    "NVDA", "MSTR", "META", "MARA", "TSM", "AMD", "TSLA", "AAPL", "MSFT", "GOOGL",
    "AMZN", "COIN", "PLTR", "SMCI", "INTC", "MU", "ORCL", "NFLX", "BABA", "PDD",
    "510300", "510500", "159915", "512880", "588000", "002241", "600519", "300750",
]


def _to_tencent(asset: str) -> str | None:
    """资产代码 → 腾讯行情代码。A 股 6 位数字按首位分 sh/sz，其余按美股 us 前缀。"""
    a = (asset or "").strip().upper()
    if not a:
        return None
    if re.fullmatch(r"\d{6}", a):
        # v489：原来判 `a[0] in "5639"` —— 把 3 开头的创业板也当成沪市，
        # 于是 300750 被请求成 sh300750（不存在）→ 每轮都进"未取到"名单，
        # 创业板标的价格永远缺。沪市只有 5(ETF)/6(主板)/9，深市 0/1/2/3。
        return ("sh" if a[0] in "569" else "sz") + a
    if re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,6}", a):
        return "us" + a.replace(".", "")
    return None


def _fetch(codes: list[str], timeout: float = 8.0) -> dict[str, tuple[float, float]]:
    """批量拉取，返回 {腾讯代码: (现价, 昨收)}。"""
    out: dict[str, tuple[float, float]] = {}
    for i in range(0, len(codes), 30):
        chunk = codes[i:i + 30]
        url = "https://qt.gtimg.cn/q=" + ",".join(chunk)
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": "https://gu.qq.com/"})
        try:
            raw = urllib.request.urlopen(req, timeout=timeout).read()
        except Exception as exc:
            print(f"[EQ] 拉取失败 {chunk[:3]}…: {exc}", file=sys.stderr)
            continue
        text = raw.decode("gbk", errors="replace")
        for line in text.splitlines():
            m = re.match(r'v_(.+?)="(.*)";', line.strip())
            if not m:
                continue
            code, body = m.group(1), m.group(2)
            f = body.split("~")
            if len(f) < 5:
                continue
            try:
                price = float(f[3])
                prev = float(f[4])
            except (TypeError, ValueError):
                continue
            if price <= 0:
                continue
            # v489：这里原来是 `prev or price` —— 取不到昨收就拿现价顶替，
            # 结果算出 0.00% 的假涨跌。现在原样保留 0，交给下游写 NULL。
            out[code] = (price, prev)
    return out


def _targets(conn: sqlite3.Connection, include_all: bool) -> list[str]:
    # 每轮都要刷新价（60 秒一轮），所以不能用「已有行情」做排除
    assets = {r[0] for r in conn.execute(
        "SELECT DISTINCT target_asset FROM ai_decisions "
        "WHERE created_at >= datetime('now','localtime','-7 days')")}
    wanted = {a.strip().upper() for a in assets if a and a.strip()}
    if include_all:
        wanted |= set(DEFAULT_LIST)
    wanted -= SKIP
    return sorted(w for w in wanted if _to_tencent(w))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="额外带上常用清单")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--db", default=DB_PATH)
    args = ap.parse_args()

    conn = sqlite3.connect(args.db, timeout=15)
    conn.execute("PRAGMA busy_timeout=15000")
    targets = _targets(conn, args.all)
    if not targets:
        print("[EQ] 无需补价（目标集为空）")
        return 0

    mapping = {a: _to_tencent(a) for a in targets}
    quotes = _fetch([c for c in mapping.values() if c])
    now = int(time.time())
    written = 0
    for asset, code in mapping.items():
        if not code or code not in quotes:
            continue
        price, prev = quotes[code]
        # v489：prev 缺失 → chg 为 None（未知），不要写 0.0（未知 ≠ 没波动）
        chg = (price / prev - 1.0) * 100 if prev > 0 else None
        if args.dry_run:
            _chg_txt = f"{chg:+.2f}%" if chg is not None else "—（无昨收）"
            print(f"[EQ] {asset:8s} <- {code:10s} {price:.3f} ({_chg_txt})")
            continue
        try:
            conn.execute(
                "INSERT OR REPLACE INTO market_ticks(symbol, ts, price, change24h, source, source_count) "
                "VALUES(?,?,?,?,?,?)",
                (asset, now, price, round(chg, 4) if chg is not None else None,
                 "tencent-eq", 1))
            written += 1
            # v494：分批提交。原先整张清单一个事务，清单大时要持写锁数分钟 ——
            # 实测 19:26 把全库写锁占住，engine/bridge/其它采集器全部
            # "database is locked"，决策落库停摆。每 50 行 commit 一次即可。
            if written % 50 == 0:
                conn.commit()
        except sqlite3.Error as exc:
            print(f"[EQ] 写库失败 {asset}: {exc}", file=sys.stderr)
    if not args.dry_run:
        conn.commit()
    conn.close()
    miss = [a for a in targets if mapping.get(a) and mapping[a] not in quotes]
    print(f"[EQ] 目标 {len(targets)} / 写入 {written} / 未取到 {len(miss)}"
          + (f"（{','.join(miss[:6])}）" if miss else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
