"""锚点回放：用"新闻发布时刻"的行情快照重跑一次判断，再按那一刻的价格结算。

为什么需要它（和 backdated_entry_replay 的区别）：
  那个脚本只把成交价换成锚点价，方向仍来自 13.5 分钟后的分析 —— 等于让决策用到了未来信息。
  本脚本把"决策"也放回锚点：只看锚点及之前的 K 线，重跑一次分析，再在锚点价成交。
  这样每条样本用的都是当时能看到的数据，可以进研究管道（仍不进绩效口径）。

三条纪律：
  1. 快照只取锚点**及之前**的 bar，不留任何未来数据；
  2. 回放结果写独立库 `backend/replay_anchor.db`，绝不写生产库的决策/成交流水；
  3. 缺行情、缺数据一律记 status=failed/skipped，不补造价格。

用法：
  python3 anchor_replay.py --limit 3 --live              # 真调 LLM（花钱）
  python3 anchor_replay.py --limit 20 --no-llm           # 只建快照+按现有方向结算（不花钱）
  python3 anchor_replay.py --limit 20 --live --horizon 240
  python3 anchor_replay.py --report                      # 只看已跑出来的结果
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SRC = Path(__file__).resolve().parent
sys.path.insert(0, str(SRC))

PROD_DB = Path(__file__).resolve().parents[1] / "trident_event_bus.db"
REPLAY_DB = Path(__file__).resolve().parents[1] / "replay_anchor.db"

TIMEFRAMES = (("1m", 60), ("15m", 900), ("1h", 3600), ("4h", 14400), ("1d", 86400))
MAX_STALE = {"1m": 900, "15m": 1800, "1h": 4500, "4h": 9000, "1d": 90000}
DEFAULT_COST_BPS = 18.0  # 实测单笔均值（16~30bps），回放统一按 18 计


# ─────────────────────────── 行情：锚点定价 ───────────────────────────
def _epoch(v) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f / 1000.0 if f > 1e12 else f


class AnchorPrices:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self._cov: Dict[Tuple[str, str], int] = {}
        for sym, tf, mx in conn.execute(
            "SELECT symbol, timeframe, MAX(ts) FROM kline_bars GROUP BY symbol, timeframe"
        ):
            self._cov[(sym, tf)] = int(mx or 0)

    def _tf(self, symbol: str, ts_sec: float) -> Optional[str]:
        ms = int(ts_sec * 1000)
        for tf, _ in TIMEFRAMES:
            if self._cov.get((symbol, tf), 0) >= ms:
                return tf
        return None

    def bars(self, symbol: str, ts_sec: float, n: int, tf: Optional[str] = None) -> List[Dict[str, float]]:
        """锚点及之前的 n 根 bar（升序），绝不包含锚点之后的 bar。"""
        tf = tf or self._tf(symbol, ts_sec)
        if not tf:
            return []
        rows = self.conn.execute(
            "SELECT ts, open, high, low, close, volume FROM kline_bars "
            "WHERE symbol=? AND timeframe=? AND ts<=? ORDER BY ts DESC LIMIT ?",
            (symbol, tf, int(ts_sec * 1000), n),
        ).fetchall()
        out = [{"t": int(r[0]) / 1000.0, "o": float(r[1] or 0), "h": float(r[2] or 0),
                "l": float(r[3] or 0), "c": float(r[4] or 0), "v": float(r[5] or 0)} for r in reversed(rows)]
        return out

    def close_at(self, symbol: str, ts_sec: float) -> Tuple[Optional[float], Optional[str], Optional[float]]:
        """由细到粗逐档尝试：最细的可信价优先，细档断源(陈旧超限)就退到粗档。"""
        best_stale = None
        for tf, _ in TIMEFRAMES:
            if self._cov.get((symbol, tf), 0) < int(ts_sec * 1000):
                continue
            row = self.conn.execute(
                "SELECT ts, close FROM kline_bars WHERE symbol=? AND timeframe=? AND ts<=? "
                "ORDER BY ts DESC LIMIT 1",
                (symbol, tf, int(ts_sec * 1000)),
            ).fetchone()
            if not row:
                continue
            stale = ts_sec - int(row[0]) / 1000.0
            if best_stale is None:
                best_stale = stale
            if stale <= MAX_STALE.get(tf, 900):
                return float(row[1]), tf, stale
        return None, None, best_stale  # 全档都不新鲜 —— 宁缺勿假


# ─────────────────────────── 快照：只用锚点前的 bar ───────────────────────────
def rsi(closes: List[float], n: int = 14) -> Optional[float]:
    if len(closes) < n + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    ag, al = sum(gains[-n:]) / n, sum(losses[-n:]) / n
    if al == 0:
        return 100.0
    return round(100 - 100 / (1 + ag / al), 1)


def atr_pct(bars: List[Dict[str, float]], n: int = 14) -> Optional[float]:
    if len(bars) < n + 1:
        return None
    trs = []
    for i in range(1, len(bars)):
        h, l, pc = bars[i]["h"], bars[i]["l"], bars[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if not trs or bars[-1]["c"] == 0:
        return None
    return round(sum(trs[-n:]) / n / bars[-1]["c"] * 100, 3)


def _trend(closes: List[float]) -> str:
    if len(closes) < 2:
        return "N/A"
    chg = (closes[-1] - closes[0]) / closes[0] * 100 if closes[0] else 0.0
    if chg > 2:
        return "Bull"
    if chg > 0.5:
        return "Mild Bull"
    if chg < -2:
        return "Bear"
    if chg < -0.5:
        return "Mild Bear"
    return "Range"


def snapshot_summary(px: AnchorPrices, anchor: float, assets=("BTC", "XAU", "WTI", "ETH")) -> str:
    """生成锚点时刻的市场摘要。只覆盖 K 线能算的部分：价格/24h/7d/趋势/ATR/RSI。
    微观结构（资金费率、OI、POC）历史无法还原，明确写 N/A，不编数。"""
    lines = ["── 市场快照（锚点回放，仅 K 线口径）──"]
    for sym in assets:
        b1h = px.bars(sym, anchor, 60, "1h")
        b4h = px.bars(sym, anchor, 40, "4h")
        b1d = px.bars(sym, anchor, 30, "1d")
        if not b1h:
            lines.append(f"{sym} N/A（无锚点前行情）")
            continue
        price = b1h[-1]["c"]
        chg24 = (price - b1h[-24]["c"]) / b1h[-24]["c"] * 100 if len(b1h) >= 24 and b1h[-24]["c"] else None
        chg7d = (price - b1d[-7]["c"]) / b1d[-7]["c"] * 100 if len(b1d) >= 7 and b1d[-7]["c"] else None
        rng = b1d[-7:] if len(b1d) >= 7 else b1d
        hi = max(x["h"] for x in rng) if rng else None
        lo = min(x["l"] for x in rng) if rng else None
        lines.append(
            f"{sym} {price:,.2f} | 24h {chg24:+.2f}% | 7d {chg7d:+.2f}% | 资金费率 N/A"
            if chg24 is not None and chg7d is not None else f"{sym} {price:,.2f} | 24h/7d N/A"
        )
        rng_txt = f"{lo:,.2f}–{hi:,.2f}" if lo is not None and hi is not None else "N/A"
        lines.append(
            f"  日线: {_trend([x['c'] for x in b1d])} | ATR={atr_pct(b1d) or 'NA'}% | 区间: {rng_txt}"
        )
        lines.append(
            f"  4H: {_trend([x['c'] for x in b4h])} ATR{atr_pct(b4h) or 'NA'}% | "
            f"1H: {_trend([x['c'] for x in b1h])} ATR{atr_pct(b1h) or 'NA'}%"
        )
        lines.append(
            f"  技术: 1H RSI{rsi([x['c'] for x in b1h])} | 微观结构（资金费率/OI/POC）N/A（历史不可还原）"
        )
    return "\n".join(lines)


# ─────────────────────────── 回放库 ───────────────────────────
DDL = """
CREATE TABLE IF NOT EXISTS replay_anchor_trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  news_id INTEGER, decision_id INTEGER, anchor_ts INTEGER, asset TEXT,
  action TEXT, score REAL, confidence REAL, model TEXT,
  horizon_min INTEGER, entry_price REAL, exit_price REAL,
  gross_pct REAL, net_pct REAL, hit INTEGER,
  cost_bps REAL, status TEXT, note TEXT, error TEXT,
  snapshot_hash TEXT, snapshot_json TEXT, reasoning TEXT,
  live_action TEXT, live_score REAL, live_pnl REAL,
  created_at TEXT
)
"""


def open_replay_db() -> sqlite3.Connection:
    conn = sqlite3.connect(REPLAY_DB)
    conn.execute(DDL)
    conn.commit()
    return conn


# ─────────────────────────── 主流程 ───────────────────────────
def pick_news(conn, since: int, until: int, limit: int, only_directional: bool = False) -> List[Dict[str, Any]]:
    """只挑"真做了分析、有标的、非降级"的历史决策 —— 保证可与线上逐条对照。
    only_directional：只挑线上已给出 BUY/SELL 的决策，用于对照"方向判断"本身。"""
    extra = " AND d.suggested_action IN ('BUY','SELL')" if only_directional else ""
    rows = conn.execute(
        f"""
        SELECT d.id AS decision_id, d.news_id, d.target_asset AS asset,
               d.suggested_action AS live_action, d.sentiment_score AS live_score,
               d.forward_pnl AS live_pnl, d.created_at, n.ts AS news_ts, n.content
        FROM ai_decisions d JOIN raw_news n ON n.id = d.news_id
        WHERE d.analysis_degraded = 0
          AND d.target_asset IN ('BTC','XAU','WTI','ETH','SOL')
          AND d.created_at >= ? AND d.created_at <= ?{extra}
        ORDER BY d.created_at ASC
        LIMIT ?
        """,
        (datetime.datetime.fromtimestamp(since).strftime("%Y-%m-%d %H:%M:%S"),
         datetime.datetime.fromtimestamp(until).strftime("%Y-%m-%d %H:%M:%S"),
         limit),
    ).fetchall()
    keys = ("decision_id", "news_id", "asset", "live_action", "live_score", "live_pnl",
            "created_at", "news_ts", "content")
    return [dict(zip(keys, r)) for r in rows]


def apply_strict_mode() -> List[str]:
    """严格模式：把 prompt 里所有"读现在库/抓现在网"的注入全部掐掉。

    线上 prompt 除了 market_context 之外还有十几个旁路块，它们的取数时点是"跑的时候"，
    不是"新闻发布的时候"——回放时若不关掉，等于把 11-18 的认知塞进 11-16 的 prompt。
    只关旁路，不动 system prompt 骨架（分析规则本身没有时间属性）。
    """
    from engine import ai_worker as aw
    import structured_input
    import server_verdict
    from market import market_style as ms

    disabled = []
    for name in (
        "_build_technical_state_block",   # 实时技术读数（现价/RSI/ADX）
        "_load_attribution_block",        # 近期异动归因（今天的解释）
        "_build_calendar_hint",           # 未来 24h 财经日历
        "_stock_context_for",             # 实时美股行情
        "_asia_context_for",              # 实时亚太行情
        "_origin_search_block_for",       # 实时全网搜索（后续报道 = 未来信息）
        "_load_reflection_block",         # 反思错误码（含 11-17/11-18 的教训）
        "_load_experience_block_sync",    # v186 经验库（当前状态）
        "_load_spa_applied_block_sync",   # v199 已应用 SPA（当前状态）
        "_build_graph_block",             # 因果图谱（当前自学习结果）
        "_build_chain_prior_block",       # 因果链命中先验（当前库）
        "_hard_data_block",               # 硬数据证据（当前库）
        "_load_kline_block",              # K 线块（若有）
    ):
        if hasattr(aw, name):
            setattr(aw, name, (lambda *a, **k: ""))
            disabled.append(f"ai_worker.{name}")

    if hasattr(ms, "market_style_block"):
        ms.market_style_block = (lambda *a, **k: "")
        disabled.append("market_style.market_style_block")

    if hasattr(server_verdict, "get_full_verdict_block"):
        server_verdict.get_full_verdict_block = (lambda *a, **k: "")
        disabled.append("server_verdict.get_full_verdict_block")

    if hasattr(structured_input, "_microstructure_block"):
        structured_input._microstructure_block = (lambda *a, **k: {})
        disabled.append("structured_input._microstructure_block")

    return disabled


class _DumpSession:
    """截获 payload 落盘后直接失败 —— 不花一分钱就能看 prompt 长什么样。"""

    def __init__(self, prefix: str):
        self.prefix = prefix

    def post(self, url, json=None, headers=None, timeout=None, **kw):
        body = json or {}
        msgs = body.get("messages") or []
        path = f"{self.prefix}.txt"
        with open(path, "w", encoding="utf-8") as f:
            for m in msgs:
                f.write(f"\n===== [{m.get('role')}] ({len(str(m.get('content') or ''))} chars) =====\n")
                f.write(str(m.get("content") or ""))
        print(f"    [DUMP] prompt 已落盘 {path}（未发出网络请求）", flush=True)
        raise RuntimeError("DUMP_ONLY：prompt 已落盘，未调用模型")


def _install_dump(prefix: str) -> None:
    from engine import ai_worker as aw
    stub = _DumpSession(prefix)
    aw._LLM_SESSION = stub
    aw._LLM_PROXY_SESSION = stub


def run(args) -> int:
    prod = sqlite3.connect(f"file:{PROD_DB}?mode=ro", uri=True)
    prod.row_factory = sqlite3.Row
    px = AnchorPrices(prod)
    rep = open_replay_db()

    items = pick_news(prod, args.since, args.until, args.limit, args.only_directional)
    print(f"候选 {len(items)} 条（{datetime.datetime.fromtimestamp(args.since):%m-%d %H:%M} → "
          f"{datetime.datetime.fromtimestamp(args.until):%m-%d %H:%M}）\n")

    cfg = None
    if args.live:
        from engine import ai_worker as aw
        pool = aw._build_model_pool()
        if not pool:
            print("没有可用模型通道，退出")
            return 2
        cfg = pool[0]
        print(f"模型：{cfg.get('label') or cfg.get('id')}；池内 {len(pool)} 槽")
        if args.strict:
            off = apply_strict_mode()
            print(f"【严格模式】已屏蔽 {len(off)} 处实时注入：")
            for n in off:
                print(f"  - {n}")
        if args.dump_prompt:
            _install_dump(args.dump_prompt)
            print(f"【DUMP 模式】prompt 将落盘到 {args.dump_prompt}_<id>.txt，不打模型")
        print()

    ok = fail = 0
    for it in items:
        anchor = _epoch(it["news_ts"])
        if not anchor:
            fail += 1
            continue
        entry, tf, stale = px.close_at(it["asset"], anchor)
        if entry is None:
            _save(rep, it, anchor, None, "skipped", note=f"anchor价格不可用 tf={tf} stale={stale}")
            fail += 1
            print(f"  跳过 #{it['decision_id']} {it['asset']}：锚点无有效价（{tf}/{stale}）")
            continue

        summary = snapshot_summary(px, anchor)
        snap_hash = hashlib.sha1(summary.encode("utf-8")).hexdigest()[:12]

        if args.no_llm:
            action, score, conf, reasoning, model, status = (
                it["live_action"], it["live_score"], None,
                "（--no-llm：沿用线上方向，仅验证管道）", "none", "no_llm")
        else:
            res = None
            last_err = None
            if args.dump_prompt:
                _install_dump(f"{args.dump_prompt}_{it['decision_id']}")
            for attempt in range(1 if args.dump_prompt else args.retries):
                slot = pool[attempt % len(pool)] if pool else cfg
                if attempt:
                    print(f"    重试 {attempt+1}/{args.retries}（{args.retry_wait}s 后，换通道 {slot.get('label')}）")
                    time.sleep(args.retry_wait)
                try:
                    ts_iso = datetime.datetime.fromtimestamp(anchor).strftime("%Y-%m-%d %H:%M:%S")
                    res = aw._call_llm_sync(
                        str(it["content"] or "")[:6000], slot,
                        market_context=summary,
                        news_timestamp=ts_iso,
                        purpose="main",
                        call_site="anchor_replay",
                    )
                    cfg = slot
                    break
                except Exception as e:
                    last_err = e
                    print(f"    ✗ 第 {attempt+1} 次：{type(e).__name__}: {str(e)[:70]}")
            if res is None:
                _save(rep, it, anchor, entry, "failed", note="LLM 调用失败",
                      error=f"{type(last_err).__name__}: {last_err}",
                      snapshot_hash=snap_hash, snapshot_json=summary)
                fail += 1
                print(f"  ✗ #{it['decision_id']} {it['asset']} LLM 失败（{args.retries} 次）")
                continue
            try:
                action = str(res.get("suggested_action") or "HOLD").upper()
                score = res.get("sentiment_score")
                conf = res.get("confidence") or res.get("evidence_confidence")
                reasoning = str(res.get("reasoning") or "")[:400]
                model = str(cfg.get("label") or cfg.get("id"))
                status = "replayed"
            except Exception as e:
                _save(rep, it, anchor, entry, "failed", note="LLM 调用失败", error=f"{type(e).__name__}: {e}",
                      snapshot_hash=snap_hash, snapshot_json=summary)
                fail += 1
                print(f"  ✗ #{it['decision_id']} {it['asset']} LLM 失败：{type(e).__name__}: {str(e)[:80]}")
                continue

        exit_p, _, _ = px.close_at(it["asset"], anchor + args.horizon * 60)
        gross = net = hit = None
        if exit_p is not None and action in ("BUY", "SELL"):
            move = (exit_p - entry) / entry * 100.0
            gross = move if action == "BUY" else -move
            net = gross - args.cost_bps / 100.0
            hit = 1 if net > 0 else 0
            if abs(move) < 1e-9:
                status = "flat_price"  # 入场=出场：冻结价，不进统计

        _save(rep, it, anchor, entry, status, action=action, score=score, confidence=conf,
              horizon=args.horizon, exit_price=exit_p, gross=gross, net=net, hit=hit,
              cost=args.cost_bps, model=model, reasoning=reasoning,
              snapshot_hash=snap_hash, snapshot_json=summary)
        ok += 1
        print(f"  ✓ #{it['decision_id']} {it['asset']} 线上={it['live_action']}/{it['live_score']} "
              f"回放={action}/{score} 入场={entry:.4f} 出场={exit_p if exit_p else 'NA'} "
              f"净={f'{net:+.3f}%' if net is not None else 'NA'}")

    print(f"\n完成：成功 {ok} / 失败 {fail}；结果库 {REPLAY_DB}")
    report(rep)
    return 0


def _save(conn, it, anchor, entry, status, *, action=None, score=None, confidence=None,
          horizon=240, exit_price=None, gross=None, net=None, hit=None, cost=DEFAULT_COST_BPS,
          model=None, reasoning=None, note=None, error=None, snapshot_hash=None, snapshot_json=None):
    conn.execute(
        """INSERT INTO replay_anchor_trades
        (news_id, decision_id, anchor_ts, asset, action, score, confidence, model,
         horizon_min, entry_price, exit_price, gross_pct, net_pct, hit, cost_bps,
         status, note, error, snapshot_hash, snapshot_json, reasoning,
         live_action, live_score, live_pnl, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (it["news_id"], it["decision_id"], int(anchor) if anchor else None, it["asset"],
         action, score, confidence, model, horizon, entry, exit_price, gross, net, hit, cost,
         status, note, error, snapshot_hash, snapshot_json, reasoning,
         it["live_action"], it["live_score"], it["live_pnl"],
         datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
    )
    conn.commit()


def report(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        "SELECT action, gross_pct, net_pct, hit, status FROM replay_anchor_trades WHERE net_pct IS NOT NULL"
    ).fetchall()
    n = len(rows)
    print("\n== 回放累计 ==")
    if not n:
        print("  暂无结算样本")
        return
    net = sum(r[2] for r in rows)
    gross = sum(r[1] for r in rows)
    print(f"  样本 {n} | 毛利合计 {gross:+.2f}% | 净利合计 {net:+.2f}% | 胜率 {100*sum(r[3] for r in rows)/n:.1f}%")
    for a in ("BUY", "SELL"):
        sub = [r for r in rows if r[0] == a]
        if sub:
            print(f"  {a}: n={len(sub)} 净合计 {sum(r[2] for r in sub):+.2f}% 胜率 {100*sum(r[3] for r in sub)/len(sub):.1f}%")
    live = conn.execute(
        "SELECT count(*), sum(live_pnl) FROM replay_anchor_trades WHERE live_pnl IS NOT NULL"
    ).fetchone()
    if live and live[0]:
        print(f"  同批线上：n={live[0]} 累计 {live[1]:+.2f}%")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-11-16 00:00")
    ap.add_argument("--until", default="2026-11-18 09:00")
    ap.add_argument("--limit", type=int, default=3)
    ap.add_argument("--horizon", type=int, default=240, help="持有分钟（默认 240）")
    ap.add_argument("--cost-bps", type=float, default=DEFAULT_COST_BPS)
    ap.add_argument("--retries", type=int, default=3)
    ap.add_argument("--retry-wait", type=int, default=45)
    ap.add_argument("--live", action="store_true", help="真调 LLM（花钱）")
    ap.add_argument("--no-llm", dest="no_llm", action="store_true", help="只建快照+按线上方向结算")
    ap.add_argument("--only-directional", dest="only_directional", action="store_true",
                    help="只挑线上已给 BUY/SELL 的决策")
    ap.add_argument("--strict", action="store_true", help="屏蔽所有实时/当前库注入，只留锚点快照与新闻正文")
    ap.add_argument("--dump-prompt", default="", help="把构建出的 prompt 落盘（不调用模型）")
    ap.add_argument("--report", action="store_true", help="只读已有结果")
    args = ap.parse_args()

    args.since = int(datetime.datetime.strptime(args.since, "%Y-%m-%d %H:%M").timestamp())
    args.until = int(datetime.datetime.strptime(args.until, "%Y-%m-%d %H:%M").timestamp())

    if args.report:
        report(open_replay_db())
        return 0
    if not args.live and not args.no_llm:
        ap.error("必须指定 --live 或 --no-llm")
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
