#!/usr/bin/env python3
"""抓取公司级财报并落库：美股三表 + 财报日历 + 一致预期，以及可选的 A股/港股读数（v406）。

为什么要单独一个脚本，而不是塞进宏观层：
  · 三表是季报，一天抓一次就够；宏观层每 22 分钟跑一轮，塞进去等于把配额烧着玩
  · AV 免费档 25 次/日（每只票最多 4 次：三表 + EARNINGS），FMP 免费档 statement 端点常返 402
    → 必须靠「TTL 内跳过 + 最旧优先轮转」每天刷一小批，而不是一轮全抓
  · 决策链路只读库（equity_fundamentals.db_pack / cn_fundamentals.read_one），抓取失败绝不影响判断

用法：
    cd backend && python3 scripts/cron_equity_fundamentals.py            # 增量：只刷过期的 6 只
    cd backend && python3 scripts/cron_equity_fundamentals.py --force    # 全量刷新基线名单
    cd backend && python3 scripts/cron_equity_fundamentals.py --cn 600519.SH,00700.HK
    cd backend && python3 scripts/cron_equity_fundamentals.py --tickers AMZN,MARA --dry-run --show

退出码：0 = 至少写入了数据（或本轮全部命中 TTL、无需写入）；1 = 一列都没写（源全挂/未配 key）。
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "src_python"))

import asset_kinds as ak
import cn_fundamentals as cf
import config
import db
import equity_fundamentals as ef


def _write(pack) -> int:
    """常驻引擎在跑时常量写库会撞 lock；抓取是幂等 upsert，重试而不是放弃。"""
    written, last_err = 0, None
    for attempt in range(3):
        conn = db.get_connection()
        try:
            ef.ensure_tables(conn)
            written = ef.persist(conn, pack)
            return written
        except sqlite3.OperationalError as exc:
            last_err = exc
            print(f"  ! 写库被锁（第 {attempt + 1} 次）: {exc}，5s 后重试")
            time.sleep(5)
        finally:
            conn.close()
    if last_err is not None and "locked" in str(last_err):
        print("[财报抓取] 写库失败：库被长时间占用，下次重试即可（数据仍是 pending 状态）")
    return written



def _yf_fill(spec: str, dry_run: bool = False) -> int:
    """用 yfinance 补三表：不消耗 FMP/AV 配额，专治"402 / 配额打满"的票。

    边界：只补**季报口径**的规模/盈利/现金流，不补市值历史 —— 市值仍由 eq.mktcap 每天落点，
    所以 PB 的"按日样本"不会因为这一步突然变多（不制造假的历史长度）。
    """
    tickers = [t.strip().upper() for t in str(spec or "").replace(" ", ",").split(",") if t.strip()]
    if not tickers:
        print("[yf 补表] 没有给 ticker")
        return 2
    conn = db.get_connection()
    try:
        ef.ensure_tables(conn)
        pack = {"statements": {}, "earnings": [], "asof": int(time.time())}
        for tk in tickers:
            rows = ef.fetch_yf_statements(tk)
            print(f"  {tk}: {len(rows)} 期"
                  + (f"，最新 {rows[0]['period_end']}" if rows else "（无数据）"), flush=True)
            if rows:
                pack["statements"][tk] = rows
        if dry_run:
            print("[yf 补表] dry-run：未写库")
            return 0 if pack["statements"] else 1
        written = ef.persist(conn, pack)
        conn.commit()
        print(f"[yf 补表] 写入 {written} 行，覆盖 {len(pack['statements'])}/{len(tickers)} 只")
        for tk, rows in pack["statements"].items():
            summary = ef.summarize(rows)
            ttm = summary.get("ttm") or {}
            print(f"  {tk} TTM 营收={_fmt(ttm.get('revenue'))} 净利={_fmt(ttm.get('net_income'))}"
                  f" 净资产={(rows[0].get('total_equity') or 0) / 1e9:.1f}B"
                  f" EPS={ttm.get('eps_diluted')}")
    finally:
        conn.close()
    return 0 if pack["statements"] else 1


def _fmt(v) -> str:
    try:
        return f"{float(v) / 1e9:.2f}B"
    except (TypeError, ValueError):
        return str(v)


# ── v454：三条补洞通道 ──────────────────────────────────────────────────────
def _sec_fill(spec: str, dry_run: bool = False) -> int:
    """SEC companyfacts 补净资产（免配额，季频，带真实申报日）。

    为什么走 SEC：v448 实测 AAPL/AMZN/GOOGL 的 total_equity 全 NULL —— AV 免费档
    的 balance 只给部分字段，FMP statement 直接 402。SEC 公开接口没有配额，
    而且每个数都带 `filed`（申报日），`_equity_asof` 可以据此真正防前视。
    """
    import sec_fundamentals as sec
    raw = str(spec or "").upper()
    tickers = [t.strip() for t in raw.replace(" ", ",").split(",") if t.strip()]
    if not tickers or tickers == ["ALL"]:
        tickers = list(config.EQUITY_TICKERS)
    conn = db.get_connection()
    total = {"inserted": 0, "updated": 0, "skipped": 0}
    ok = 0
    try:
        for tk in tickers:
            rows = sec.balance_rows(tk)
            if not rows:
                print(f"  {tk}: SEC 无读数（非美股发行人或不报 us-gaap:StockholdersEquity）")
                continue
            stats = sec.merge_into_db(conn, tk, rows=rows, dry_run=dry_run)
            for k in total:
                total[k] += stats[k]
            latest = rows[-1]
            ok += 1
            print(f"  {tk}: {len(rows)} 期，最新 {latest['period_end']}（申报 {latest['filed']}）"
                  f" 净资产={_fmt(latest.get('total_equity'))}"
                  f" {'派生' if latest.get('derived') else ''}"
                  f" → 新增 {stats['inserted']} / 补空 {stats['updated']}")
    finally:
        conn.close()
    print(f"[SEC 补表] {'dry-run ' if dry_run else ''}覆盖 {ok}/{len(tickers)} 只，"
          f"新增 {total['inserted']} 行 / 补空 {total['updated']} 行 / 跳过 {total['skipped']}")
    return 0 if ok else 1


def _mktcap_fill(days: int, dry_run: bool = False) -> int:
    """股本 × 收盘 → 市值序列（PB 的分子）。

    `--mktcap-fill 1` 只补今天（日常 cron 用）；`--mktcap-fill 400` 是把**已公布的
    股本序列** × **已有日线收盘** 推导一遍，用于首次建表。这不是重放采集积压：
    两个乘数都是真实已公布数据，口径在行内标 `derived`。
    """
    import equity_mktcap as em
    conn = db.get_connection()
    try:
        stats = em.fill_today(conn, days_back=max(1, int(days)), dry_run=dry_run)
    finally:
        conn.close()
    print(f"[市值推导] {'dry-run ' if dry_run else ''}覆盖 {stats['tickers']} 只 / 写入 {stats['rows']} 行"
          f"（缺股本 {stats['no_shares']} / 缺收盘 {stats['no_close']}）")
    return 0 if stats["rows"] or dry_run else 1


def _cn_valuation(spec: str, dry_run: bool = False) -> int:
    """A股/港股：用「已公布 EPS/BPS × 当日收盘」推一年 PE/PB 序列（v455）。

    为什么必须现在做：A股/港股每票只有 1~7 个东财快照点，60 天门槛根本到不了，
    估值分量恒为 None —— 分数只剩盈利增速，实测东山精密/新易盛直接打满 1.0。
    两个乘数都是已公布真数（口径同美股 v454 的 PB 通道），行内标 `derived`；
    东财原始快照同日已有则不覆盖（采到的优先于推出来的）。
    """
    import cn_fundamentals as cn
    raw = str(spec or "").upper().replace(" ", ",")
    tickers = [t.strip() for t in raw.split(",") if t.strip()]
    if not tickers or tickers == ["ALL"]:
        conn = db.get_connection()
        try:
            tickers = [r[0] + "|" + r[1] for r in conn.execute(
                "SELECT DISTINCT ticker, market FROM cn_equity_fundamentals").fetchall()]
        finally:
            conn.close()
    if not tickers:
        print("[CN 估值] 库内没有 A股/港股代码，跳过（先跑 --cn-auto）")
        return 0
    conn = db.get_connection()
    total = {"rows": 0, "pe": 0, "pb": 0, "skipped": 0}
    ok = 0
    try:
        for item in tickers:
            ticker, market = (item.split("|", 1) if "|" in item else
                              (item, "HK" if item.endswith(".HK") else "A"))
            try:
                stats = cn.backfill_valuation(conn, ticker, market, dry_run=dry_run)
            except Exception as exc:
                print(f"  ! {ticker}: {type(exc).__name__}: {str(exc)[:60]}")
                continue
            for k in total:
                total[k] += stats[k]
            if stats["rows"]:
                ok += 1
                print(f"  {ticker}({market}): {stats['rows']} 个交易日点（PE {stats['pe']} / PB {stats['pb']}）")
    finally:
        conn.close()
    print(f"[CN 估值] {'dry-run ' if dry_run else ''}{ok}/{len(tickers)} 只写入，"
          f"合计 {total['rows']} 行（PE {total['pe']} / PB {total['pb']} / 跳过 {total['skipped']}）")
    return 0 if ok or dry_run else 1


def _peer_warm() -> int:
    """预热同业可比倍数 + 行业归属。热路径只读缓存，不为此发网络请求。

    v456：名单 = 人工分组 ∪ 已自动识别的票 ∪ 决策名单（config.EQUITY_TICKERS）。
    把决策名单也带上，是为了让"名单里但没手写分组"的票（如 MSTR 之外的新增票）
    在 cron 阶段就被归好组，而不是等到决策时才现猜。
    """
    import peer_valuation as pv
    extra = list(getattr(config, "EQUITY_TICKERS", ()) or ())
    names = sorted({t for g in pv.SECTORS.values() for t in g}
                   | {p for p in pv._load_sectors().keys()} | set(extra))
    stats = pv.warm(tickers=names)
    print(f"[同业预热] 命中 {stats['ok']} 只 / 缺失 {stats['miss']} 只"
          f" / 已归组 {stats.get('grouped', 0)} 只")
    return 0 if stats["ok"] else 1


def _coverage() -> int:
    """只读自查：现在到底覆盖了几只、缺哪几只、配额还剩多少、哪些票真有估值分位。

    为什么单开一个命令：每次问"做完了吗"都得手工查库、翻日志，容易各说各话。
    这个命令只用库里的数据说话，不联网、不写库，随时可以复核。
    """
    import fundamental_factor as ff

    base = tuple(getattr(config, "EQUITY_TICKERS", ()) or ())
    conn = db.get_connection()
    try:
        pack = ef.db_pack(conn, tickers=base)
        summary = pack.get("summary") or {}
        covered = sorted(summary)
        missing = [t for t in base if t not in summary]
        cn_rows = 0
        try:
            cn_rows = int(conn.execute(
                "SELECT COUNT(*) FROM cn_equity_fundamentals").fetchone()[0])
        except Exception:
            cn_rows = 0
    finally:
        conn.close()

    print(f"[覆盖] 基线 {len(base)} 只 → 可读 {len(covered)} 只 / 缺 {len(missing)} 只")
    print(f"       已有: {', '.join(covered) or '无'}")
    print(f"       仍缺: {', '.join(missing) or '无'}")
    print(f"[配额] AV 今日 {ef.budget_state()}")
    print(f"[A股港股] 库内读数 {cn_rows} 条")

    val_ok, guard_hit, partial = [], [], []
    for tk in covered:
        try:
            r = ff.build(tk, use_cache=False)
        except Exception:
            continue
        comps = r.get("components") or {}
        if comps.get("valuation_percentile") is not None:
            val_ok.append(f"{tk}({comps.get('valuation_percentile'):+.2f})")
        if r.get("flags"):
            guard_hit.append(f"{tk}:{'/'.join(r['flags'])}")
        if (summary.get(tk) or {}).get("partial"):
            partial.append(tk)
    print(f"[估值分位] {len(val_ok)} 只可得: {', '.join(val_ok) or '无（样本<60 一律不作结论）'}")

    # v454: PB 不补历史，只看"攒了几天"。把缺口变成倒计时，避免又靠猜。
    # 注意 conn 在上面的 finally 已经关掉，这里单开一条只读连接（别复用死连接，
    # 否则异常被吞掉、每只票都显示 0 天，反而看起来"比实际更糟"）。
    import sqlite3 as _sq
    pb_days = []
    pb_conn = None
    try:
        pb_conn = _sq.connect(f"file:{config.DB_PATH}?mode=ro", uri=True, timeout=5)
    except Exception:
        pb_conn = None
    for tk in covered:
        n_mk = n_snap = 0
        if pb_conn is not None:
            try:
                n_mk = len(ef.pb_history(tk, days=365, conn=pb_conn))
            except Exception:
                n_mk = 0
            try:
                n_snap = pb_conn.execute(
                    """SELECT COUNT(DISTINCT CAST(ts/86400 AS INTEGER)) FROM macro_snapshots
                       WHERE metric_key='eq.pb' AND value IS NOT NULL AND payload LIKE ?""",
                    (f'%"label": "{tk}"%',),
                ).fetchone()[0]
            except Exception:
                n_snap = 0
        n_use = max(n_mk, n_snap)
        pb_days.append(f"{tk}:{n_use}天{'✓' if n_use >= 60 else ''}")
    eq_cov = 0
    cn_val_rows = []
    if pb_conn is not None:
        try:
            eq_cov = int(pb_conn.execute(
                """SELECT COUNT(DISTINCT ticker) FROM equity_fundamentals
                   WHERE COALESCE(total_equity, total_assets - total_liabilities) IS NOT NULL"""
            ).fetchone()[0])
        except Exception:
            eq_cov = 0
        # v455：必须在关连接之前查，否则异常被吞、看起来"一只都没有"（首轮就踩过）
        try:
            cn_val_rows = pb_conn.execute(
                """SELECT ticker, market, COUNT(*) FROM cn_valuation_snapshots
                   WHERE pe IS NOT NULL AND pe > 0 AND ts >= ?
                   GROUP BY ticker, market""",
                (int(time.time()) - 365 * 86400,)).fetchall()
        except Exception:
            cn_val_rows = []
        pb_conn.close()
    print(f"[PB 分位] 按日样本（门槛 60 天，✓=已可用）: {', '.join(pb_days) or '无'}")
    print(f"[净资产] {eq_cov}/{len(base)} 只有净资产（SEC companyfacts 免配额补缺）")
    # v455：A股/港股 估值分位（推导序列写库后按 60 点门槛判）
    ok_cn = [f"{r[0]}({r[2]})" for r in cn_val_rows if r[2] >= 60]
    sub_cn = [f"{r[0]}({r[2]})" for r in cn_val_rows if r[2] < 60]
    print(f"[A股港股估值] 分位可用 {len(ok_cn)} 只: {', '.join(ok_cn) or '无'}"
          f"｜样本不足: {', '.join(sub_cn) or '无'}")
    print(f"[护栏] 触发票: {', '.join(guard_hit) or '无'}")
    print(f"[部分覆盖] 只有盈利面、缺三表: {', '.join(partial) or '无'}")
    return 0



def _cn_auto(limit: int = 10) -> int:
    """从近 7 天的占位决策里反解出 A股/港股代码，然后抓这些代码的财务读数。

    这补的是最后一环：决策链路只读库（不联网），所以「新闻点名了东山精密、也反解出
    002384.SZ」之后，仍然要有数据才用得上。谁来抓？就是这里 —— 每天一次、离线决策、
    联网 cron，两边彻底分开。只抓还被决策引用过的票，不做全市场扫描（那是另一件事）。
    """
    conn = db.get_connection()
    codes: list = []
    try:
        maps = {"A": cf.name_map(conn, market="A"), "HK": cf.name_map(conn, market="HK")}
        rows = conn.execute(
            "SELECT d.id, d.target_asset, COALESCE(n.headline,''), COALESCE(n.body,'') "
            "FROM ai_decisions d LEFT JOIN news_events n ON n.news_id = d.news_id "
            "WHERE d.target_asset IN ('CN_STOCK','HK_STOCK') AND d.created_at >= ? "
            "GROUP BY d.id ORDER BY d.created_at DESC LIMIT 200",
            (int(time.time()) - 7 * 86400,)).fetchall()
    finally:
        conn.close()

    for _did, ta, headline, body in rows:
        market = "HK" if str(ta).upper() == "HK_STOCK" else "A"
        text = (str(headline or "") + " " + str(body or ""))[:400]
        res = ak.resolve_from_text(text, ta, maps.get(market))
        if not res:
            continue
        kind, code = res
        if kind in ("a_share", "hk") and code not in codes:
            codes.append(code)
        if len(codes) >= limit:
            break

    if not codes:
        print("[A股港股] 近 7 天没有可反解的占位决策，跳过")
        return 0
    print(f"[A股港股] 从近 7 天决策里反解出 {len(codes)} 只: {', '.join(codes)}")
    ok = 0
    for code in codes:
        market = "HK" if code.endswith(".HK") else "A"
        try:
            got = cf.fetch_history(market, code)
        except Exception as exc:
            print(f"  ! {code}: {type(exc).__name__}: {exc}")
            continue
        if got.get("error"):
            print(f"  ! {code}: {got['error']}")
            continue
        rec = got.get("latest") or {}
        conn = db.get_connection()
        try:
            cf.ensure_tables(conn)
            ok += cf.persist_history(conn, got.get("records") or [])
        finally:
            conn.close()
        print(f"  {code}: {rec.get('name')} {rec.get('report_date')} "
              f"净利同比={rec.get('net_profit_yoy')} PE={rec.get('pe_ttm')} "
              f"历史={got.get('count')}期")
    print(f"[A股港股] 已入库 {ok} 条")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="抓取公司级财报（美股三表 / A股港股读数，落库）")
    ap.add_argument("--tickers", default="", help="逗号分隔，缺省用 config.EQUITY_TICKERS ∪ 近30天决策过的票")
    ap.add_argument("--max-refresh", type=int, default=None,
                    help=f"本轮最多刷新几只（缺省 config.EQUITY_MAX_PER_RUN={getattr(config, 'EQUITY_MAX_PER_RUN', 6)}）")
    ap.add_argument("--force", action="store_true", help="忽略 TTL，强制重抓（会吃掉当日 AV 配额）")
    ap.add_argument("--cn", default="", help="A股/港股代码，逗号分隔，如 600519.SH,00700.HK（走东财，不耗 AV 配额）")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不写库")
    ap.add_argument("--show", action="store_true", help="打印每只票的 TTM 摘要")
    ap.add_argument("--name-map", action="store_true",
                    help="刷新「简称→代码」字典（A股约5800只/港股约2700只，联网约30s）")
    ap.add_argument("--cn-auto", type=int, default=0, metavar="N",
                    help="从近7天占位决策里反解公司→代码，抓 N 只的 A股/港股读数（联网，不耗 AV 配额）")
    ap.add_argument("--yf-fill", default="", metavar="T1,T2",
                    help="用 yfinance 季报三表补齐指定美股（不耗 AV/FMP 配额）")
    ap.add_argument("--sec-fill", default="", metavar="T1,T2,ALL",
                    help="v454：用 SEC companyfacts 补净资产/资产负债表（免配额，带申报日）")
    ap.add_argument("--mktcap-fill", type=int, default=0, metavar="DAYS",
                    help="v454：股本×收盘 推导市值写 equity_mktcap_daily（DAYS=回看天数，1=只补今天）")
    ap.add_argument("--peer-warm", action="store_true",
                    help="v454：预热同业可比倍数缓存（yfinance，供热路径只读缓存）")
    ap.add_argument("--cn-valuation", default="", metavar="T1,T2,ALL",
                    help="v455：A股/港股 用「已公布 EPS/BPS × 当日收盘」推导一年 PE/PB 序列（写 cn_valuation_snapshots）")
    ap.add_argument("--coverage", action="store_true",
                    help="只读自查：基线覆盖几只/缺哪几只、配额、估值分位与护栏可用的票（不联网）")
    args = ap.parse_args()

    os.environ.setdefault("TRIDENT_AV_THROTTLE_SEC", "13")   # 脚本里可以等，主链路不行

    if args.coverage:
        return _coverage()

    if args.yf_fill:                       # 纯 yfinance，先分派：不碰 FMP/AV
        return _yf_fill(args.yf_fill, dry_run=args.dry_run)

    if args.sec_fill:                      # v454：SEC companyfacts，免配额
        return _sec_fill(args.sec_fill, dry_run=args.dry_run)

    if args.mktcap_fill:                   # v454：股本×收盘 → 市值序列
        return _mktcap_fill(args.mktcap_fill, dry_run=args.dry_run)

    if args.peer_warm:                     # v454：同业倍数缓存预热
        return _peer_warm()

    if args.cn_valuation:                  # v455：A股/港股 估值历史推导
        return _cn_valuation(args.cn_valuation, dry_run=args.dry_run)

    if args.name_map:
        print(f"[名称字典] {cf.refresh_name_map()}")
    if args.cn_auto and not args.dry_run:
        return _cn_auto(args.cn_auto)

    conn_probe = db.get_connection()
    try:
        tickers = tuple(t.strip().upper() for t in args.tickers.split(",") if t.strip()) \
            or ef.universe(conn_probe) \
            or tuple(getattr(config, "EQUITY_TICKERS", ()) or ("NVDA",))
    finally:
        conn_probe.close()

    t0 = time.time()
    pack = ef.fetch_pack(tickers, skip_fresh=not args.force, max_refresh=args.max_refresh)
    print(f"[财报抓取] 状态={pack['status']} 标的数={len(tickers)} "
          f"三表行数={sum(len(v) for v in (pack.get('statements') or {}).values())} "
          f"财报事件={len(pack.get('earnings') or [])} 耗时={time.time() - t0:.0f}s")
    print(f"[财报抓取] 本轮刷新={','.join(pack.get('refreshed') or ()) or '无'} "
          f"| 复用={','.join(pack.get('reused') or ()) or '无'} "
          f"| 顺延={','.join(pack.get('deferred') or ()) or '无'} "
          f"| AV 配额={pack.get('av_budget')}")
    for err in (pack.get("errors") or [])[:8]:
        print(f"  ! {err}")
    if args.show:
        for tk, summary in sorted((pack.get("summary") or {}).items()):
            ttm = summary.get("ttm") or {}
            yoy = summary.get("yoy") or {}
            latest = summary.get("latest") or {}
            print(f"  {tk}: 季数={summary.get('n_quarters')} 期={latest.get('period_end')} "
                  f"TTM营收={ttm.get('revenue')} TTM_EPS={ttm.get('eps_diluted')} "
                  f"EPS同比={yoy.get('eps_diluted')} 营收同比={yoy.get('revenue')} "
                  f"毛利率={summary.get('gross_margin')} src={summary.get('eps_source')}")

    # ── A股/港股：走东财，不占 AV 配额，仍按 24h TTL 落库 ────────────────
    cn_written = 0
    for raw in [x.strip() for x in args.cn.split(",") if x.strip()]:
        kind, code = ak.classify(raw)
        if kind not in ("a_share", "hk") or not code:
            print(f"  ! {raw}: 不是可解析的 A股/港股代码（kind={kind}），跳过")
            continue
        market = "A" if kind == "a_share" else "HK"
        got = cf.fetch_history(market, code)
        if got.get("error"):
            print(f"  ! {raw} → {code}: {got['error']}")
            continue
        rec = got.get("latest") or {}
        print(f"  {code}: {rec.get('name')} {rec.get('report_date')}({rec.get('period_label')}) "
              f"PE={rec.get('pe_ttm')} PB={rec.get('pb')} 营收同比={rec.get('revenue_yoy')} "
              f"净利同比={rec.get('net_profit_yoy')} 历史={got.get('count')}期")
        if args.dry_run:
            continue
        conn = db.get_connection()
        try:
            cf.ensure_tables(conn)
            cn_written += cf.persist_history(conn, got.get("records") or [])
        finally:
            conn.close()

    if args.dry_run:
        print("[财报抓取] dry-run：未写库")
        return 0 if (pack.get("statements") or cn_written) else 1

    written = _write(pack)
    print(f"[财报抓取] 已写入 美股 {written} 行 / A股港股 {cn_written} 条")
    # 全部命中 TTL（written=0 且 reused 非空）也是正常成功：库里的数据够新
    return 0 if (written or cn_written or pack.get("reused")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
