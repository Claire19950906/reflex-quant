#!/usr/bin/env python3
"""calc_event_move_attribution.py - v291 enhanced.
Link critical_price_moves with news_events into event_move_attribution.

Four-tier matching (v291+):
  1) Direct asset tag hit (+0.15)
  2) Headline/body semantic hit (incl. synonyms / Chinese names) (+0.10)
  3) Cross-asset semantic: same group + same direction (+0.03)
  4) Macro news: no asset tag + high score (+0.05)
"""
from __future__ import annotations
import sqlite3
import sys
import argparse
from pathlib import Path
from datetime import datetime, timezone

try:
    from cem_constants import ASSET_SYNONYMS, CROSS_ASSET_GROUPS
except ImportError:
    ASSET_SYNONYMS = {
        "BTC": ["BTC", "bitcoin"], "ETH": ["ETH", "ethereum"],
        "SOL": ["SOL", "solana"], "XAU": ["XAU", "gold"],
        "WTI": ["WTI", "crude oil", "crude"],
        "NVDA": ["NVDA", "nvidia"], "TSLA": ["TSLA", "tesla", "elon musk"],
        "MU": ["MU", "micron"], "SKHYNIX": ["SKHYNIX", "sk hynix", "000660.KS"],
        "ADA": ["ADA", "cardano"], "DOGE": ["DOGE", "dogecoin"],
        "XRP": ["XRP", "ripple"], "BNB": ["BNB", "binance coin"],
        "AAPL": ["AAPL", "apple"], "MSFT": ["MSFT", "microsoft"],
        "GOOGL": ["GOOGL", "google", "alphabet"],
        "AMZN": ["AMZN", "amazon"], "META": ["META", "facebook"],
    }
    CROSS_ASSET_GROUPS = {
        "crypto":  ["BTC", "ETH", "SOL", "ADA", "DOGE", "XRP", "BNB"],
        "metal":   ["XAU"], "energy": ["WTI"],
        "us_tech": ["NVDA", "TSLA", "AAPL", "MSFT", "GOOGL", "AMZN", "META"],
        "memory":  ["MU", "SKHYNIX"],
    }

BACKEND_DIR = Path(__file__).resolve().parents[1]
DB_PATH = BACKEND_DIR / "trident_event_bus.db"

TIME_WINDOW_SEC = 432000  # 5 days (v291+ 扩展: news 实时采集, moves 历史回填, 跨度大)
TOP_N_PER_MOVE = 3
MACRO_NEWS_SCORE = 0.7


def headline_contains_asset(headline, asset):
    if not headline:
        return False
    h = headline.upper()
    for kw in ASSET_SYNONYMS.get(asset, [asset]):
        if kw.upper() in h or kw in headline:
            return True
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--enable-spillover", action="store_true",
                    help="Tier 2: 同组资产 24h spillover 归因 (Q4 覆盖率提升)")
    ap.add_argument("--enable-macro-fallback", action="store_true",
                    help="Tier 3: |move|>threshold 且无 news 时的 macro 归因")
    ap.add_argument("--macro-threshold", type=float, default=5.0,
                    help="macro fallback 触发阈值 (默认 |move_pct|>=5.0)")
    ap.add_argument("--spillover-window", type=int, default=86400,
                    help="spillover 时间窗 (默认 86400=24h)")
    args = ap.parse_args()
    if not DB_PATH.exists():
        print(f"[FATAL] DB not found: {DB_PATH}", file=sys.stderr)
        return 1
    cutoff = int(datetime.now(tz=timezone.utc).timestamp()) - args.days * 86400
    print(f"[INFO] Calc event_move_attribution since {datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat()}")
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        moves = conn.execute(
            "SELECT id, asset, ts, move_pct, move_kind, duration_min, algo_score "
            "FROM critical_price_moves WHERE ts >= ? ORDER BY ts DESC",
            (cutoff,),
        ).fetchall()
        news = conn.execute(
            "SELECT id, source, headline, body, assets, score, ts, action "
            "FROM news_events WHERE ts >= ? ORDER BY ts DESC",
            (cutoff,),
        ).fetchall()
        print(f"[INFO] Loaded {len(moves)} moves, {len(news)} news events")
        if not moves or not news:
            print("[INFO] Nothing to do")
            return 0
        cur_del = conn.execute("DELETE FROM event_move_attribution WHERE move_ts >= ?", (cutoff,))
        print(f"[INFO] Cleared {cur_del.rowcount} prior rows")
        asset_group_map = {a: g for g, members in CROSS_ASSET_GROUPS.items() for a in members}
        inserts = []
        match_stats = {"asset": 0, "semantic": 0, "cross": 0, "macro": 0,
                       "spillover": 0, "macro_fallback": 0}

        for m in moves:
            m_id, m_asset, m_ts, m_pct, m_kind, m_dur, m_algo = m
            m_group = asset_group_map.get(m_asset)
            scored = []
            for n in news:
                n_id, n_src, n_headline, n_body, n_assets, n_score, n_ts, n_action = n
                # v291+ 双向归因:
                #   news before move (lag > 0)  → "事前归因" (新闻是触发因素)
                #   news after move  (lag < 0)  → "事后归因" (新闻是对已发生事件的报道/解释)
                # 两种都接受, 但事前归因 recency 系数略高
                lag = m_ts - n_ts
                abs_lag = abs(lag)
                if abs_lag > TIME_WINDOW_SEC:
                    continue
                is_retrospective = lag < 0
                match_type = None
                n_asset_list = [a.strip().upper() for a in (n_assets or "").split(",") if a.strip()]
                n_asset_set = set(n_asset_list)
                if m_asset.upper() in n_asset_set:
                    match_type = "asset"
                elif headline_contains_asset(n_headline or "", m_asset) or headline_contains_asset(n_body or "", m_asset):
                    match_type = "semantic"
                elif m_group and n_asset_list:
                    n_in_same_group = [a for a in n_asset_list if a in asset_group_map and asset_group_map[a] == m_group]
                    if n_in_same_group:
                        n_act = (n_action or "").upper()
                        move_dir = 1 if (m_pct or 0) > 0 else -1
                        news_dir = 1 if n_act in ("BUY",) else (-1 if n_act in ("SELL",) else 0)
                        if news_dir == 0 or news_dir == move_dir:
                            match_type = "cross"
                if match_type is None:
                    if not n_asset_list and (n_score or 0) >= MACRO_NEWS_SCORE:
                        match_type = "macro"
                if match_type is None:
                    continue
                base = 0.35
                if match_type == "asset":
                    base += 0.15
                elif match_type == "semantic":
                    base += 0.10
                elif match_type == "macro":
                    base += 0.05
                price_react = min(0.3, abs(m_pct) / 20.0)
                news_impact = (n_score or 0) * 0.3
                score = base + news_impact + price_react
                recency = max(0.5, 1.0 - abs_lag / TIME_WINDOW_SEC)
                if is_retrospective:
                    recency *= 0.85
                score = round(min(0.99, score * recency), 3)
                scored.append((score, n_id, n_src, lag, match_type, is_retrospective))
                match_stats[match_type] += 1
            scored.sort(reverse=True)
            for score, n_id, n_src, lag, match_type, is_retrospective in scored[:TOP_N_PER_MOVE]:
                reasons = f"match={match_type}; lag={lag}s; {'retro' if is_retrospective else 'predictive'}"
                inserts.append((
                    m_id, n_id, m_asset, lag, score, reasons,
                    0, None, None, None,
                    m_ts, m_pct, m_kind, m_dur,
                    n_id, n_src, match_type.upper(),
                    None, n_ts,
                ))
        if not inserts:
            print("[INFO] No attribution rows generated")
            return 0

        # === Tier 2: 同组资产 24h spillover (Q4 覆盖率提升) ===
        if args.enable_spillover:
            spillover_window = args.spillover_window
            # 按 (asset_group, day_bucket) 分组, 找 group 内有大幅 move 的同伴
            from collections import defaultdict
            group_moves: dict = defaultdict(list)  # group -> [(move, day_bucket, magnitude)]
            for m in moves:
                m_id, m_asset, m_ts, m_pct, m_kind, m_dur, m_algo = m
                m_group = asset_group_map.get(m_asset)
                if not m_group:
                    continue
                if abs(m_pct or 0) < 3.0:  # 仅显著 move 触发 spillover
                    continue
                day_bucket = int(m_ts) // spillover_window
                group_moves[(m_group, day_bucket)].append(m)
            # 对每个 move, 找同组同窗口的"显著同伴", 生成 spillover 归因
            t1_count = sum(1 for ins in inserts if ins[15] is not None)
            for m in moves:
                m_id, m_asset, m_ts, m_pct, m_kind, m_dur, m_algo = m
                m_group = asset_group_map.get(m_asset)
                if not m_group:
                    continue
                # 是否已有 Tier 1 归因?
                already = any(ins[0] == m_id for ins in inserts)
                if already:
                    continue  # Tier 1 已覆盖, 不再叠加
                day_bucket = int(m_ts) // spillover_window
                peers = [p for p in group_moves.get((m_group, day_bucket), [])
                         if p[0] != m_id]
                if not peers:
                    continue
                # 用最强 peer 当 spillover 来源 (algo_score = 弱相关)
                peer = max(peers, key=lambda p: abs(p[3] or 0))
                p_id, p_asset, p_ts, p_pct, p_kind, p_dur, p_algo = peer
                spillover_score = round(min(0.99, 0.30 + abs(p_pct or 0) / 30.0), 3)
                lag = m_ts - p_ts
                inserts.append((
                    m_id, None, m_asset, lag, spillover_score,
                    f"match=spillover; group={m_group}; peer={p_asset}; lag={lag}s",
                    0, None, None, None,
                    m_ts, m_pct, m_kind, m_dur,
                    None, "spillover_calc", "SPILLOVER",
                    None, p_ts,
                ))
                match_stats["spillover"] += 1

        # === Tier 3: macro_fallback (无任何归因且 |move|>threshold 时兜底) ===
        if args.enable_macro_fallback:
            th = args.macro_threshold
            covered_move_ids = {ins[0] for ins in inserts}
            for m in moves:
                m_id, m_asset, m_ts, m_pct, m_kind, m_dur, m_algo = m
                if m_id in covered_move_ids:
                    continue  # 已 Tier 1/2 覆盖
                if abs(m_pct or 0) < th:
                    continue
                # 低置信度 macro 兜底, 标 algo_score = 0.30 + magnitude_bonus
                macro_score = round(min(0.55, 0.30 + abs(m_pct or 0) / 50.0), 3)
                inserts.append((
                    m_id, None, m_asset, 0, macro_score,
                    f"match=macro_fallback; |move_pct|={abs(m_pct):.1f}>={th}; no news matched",
                    0, None, None, None,
                    m_ts, m_pct, m_kind, m_dur,
                    None, "macro_fallback", "MACRO",
                    None, m_ts,
                ))
                match_stats["macro_fallback"] += 1

        conn.executemany(
            "INSERT INTO event_move_attribution "
            "(move_id,news_event_id,asset,time_lag_sec,algo_score,algo_reasons,"
            "ai_called,ai_explanation,ai_confidence,ai_error,"
            "move_ts,move_pct,move_kind,duration_min,"
            "ev_news_id,ev_source,ev_action,ev_score,ev_ts) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            inserts,
        )
        conn.commit()
        after = conn.execute("SELECT COUNT(*) FROM event_move_attribution").fetchone()[0]
        by_asset = conn.execute(
            "SELECT asset, COUNT(*) FROM event_move_attribution GROUP BY asset ORDER BY asset"
        ).fetchall()
        print(f"[OK] Inserted {len(inserts)} rows (table total: {after})")
        for a, c in by_asset:
            print(f"  {a}: {c}")
        print(f"[INFO] Match types: {match_stats}")
        # Q4 覆盖率计算
        total_moves = len(moves)
        covered = len({ins[0] for ins in inserts})
        cov_pct = round(100 * covered / max(total_moves, 1), 1)
        print(f"[Q4] Coverage: {covered}/{total_moves} moves = {cov_pct}% (target >=30%)")
        return 0
    except Exception as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        conn.rollback()
        return 2
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
