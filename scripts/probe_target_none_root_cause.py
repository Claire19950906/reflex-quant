#!/usr/bin/env python3
"""漏斗根因探针（只读）：回答「为什么 89% 是降级 / 为什么几乎不出方向」。

用法：
    cd backend && python3 ../scripts/probe_target_none_root_cause.py

产出四段证据：
  1. 新闻漏斗：多少条真正进了分析，多少条被静默归档（FILTERED 但 is_noise=0）
  2. 黑洞按小时 + 门卫判定分布：区分「真噪音」「可交易」「未判定」
  3. target_none 分类：预路由本可拦下的（个股）vs 拦不住（别国宏观杂讯）
  4. 门卫规则兜底泄漏测试：rule_gate_fallback 对这批文本是否放行

不写库、不改状态、不调用 LLM。
"""
from __future__ import annotations

import sqlite3
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src_python"))

import config
from feeds import news_router as nr
from realtime_filter import rule_gate_fallback

DB = ROOT / "backend" / "trident_event_bus.db"


def ro() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=20)
    conn.row_factory = sqlite3.Row
    return conn


def sec1(conn):
    print("=" * 72)
    print("① 新闻漏斗（今日）")
    print("=" * 72)
    r = conn.execute("""
        SELECT COUNT(*) n,
               SUM(status='DONE') done,
               SUM(status='PENDING') pending,
               SUM(status='PROCESSING') proc,
               SUM(status='FILTERED' AND is_noise=0) black_hole,
               SUM(status='FILTERED' AND is_noise=1) noise
        FROM raw_news WHERE timestamp LIKE date('now','localtime') || '%'
    """).fetchone()
    n = r["n"] or 0
    if not n:
        print("今日无新闻，跳过")
        return
    print(f"入库 {n} 条 | 真分析 DONE {r['done'] or 0} ({(r['done'] or 0)*100/n:.1f}%)"
          f" | 待分析 {r['pending'] or 0} | 在途 {r['proc'] or 0}")
    print(f"**静默归档 FILTERED+is_noise=0 {r['black_hole'] or 0} ({(r['black_hole'] or 0)*100/n:.1f}%)**"
          f" → 认领 SQL 只取 status='PENDING' AND is_noise=0，这批结构上永远不会被分析")
    print(f"真噪音 FILTERED+is_noise=1 {r['noise'] or 0} ({(r['noise'] or 0)*100/n:.1f}%)")
    print(f"\n当前 NEWS_ANALYZE_MAX_AGE_HOURS={config.NEWS_ANALYZE_MAX_AGE_HOURS}"
          "（进程环境可能另有覆盖，用 `ps -E -p <engine_pid> | tr ' ' '\\n' | grep NEWS_ANALYZE` 核对）")


def sec2(conn):
    print("\n" + "=" * 72)
    print("② 黑洞分布：按小时 / 按门卫判定")
    print("=" * 72)
    rows = conn.execute("""
        SELECT substr(timestamp,12,2) h, COUNT(*) n
        FROM raw_news WHERE timestamp LIKE date('now','localtime') || '%'
          AND status='FILTERED' AND is_noise=0 GROUP BY 1 ORDER BY 1
    """).fetchall()
    print("按小时：", " ".join(f"{r['h']}时={r['n']}" for r in rows) or "无")
    rows = conn.execute("""
        SELECT COALESCE(gate_verdict,'(空)') v, COUNT(*) n
        FROM raw_news WHERE timestamp LIKE date('now','localtime') || '%'
          AND status='FILTERED' AND is_noise=0 GROUP BY 1 ORDER BY 2 DESC
    """).fetchall()
    print("按门卫判定：", " ".join(f"{r['v']}={r['n']}" for r in rows))
    print("→ gate_verdict='可交易' 的那一批是「门卫说能做、但没等到分析就被归档」的真信号损失")


def sec3(conn):
    print("\n" + "=" * 72)
    print("③ target_none 决策：预路由能拦住多少")
    print("=" * 72)
    rows = conn.execute("""
        SELECT n.id nid, n.source, n.content
        FROM ai_decisions d JOIN raw_news n ON n.id = d.news_id
        WHERE d.analysis_degraded=1 AND d.analysis_degraded_reason='target_none'
    """).fetchall()
    if not rows:
        print("无 target_none 决策")
        return
    caught, rescue_stock, other, by_scope = [], [], [], []
    for r in rows:
        c = (r["content"] or "").replace("\n", " ").strip()
        reason = nr.pool_out_reason(c[:120], c)
        if reason:
            caught.append(r)
            if reason.startswith("池外宏观"):   # v408 新增的市场归属规则
                by_scope.append(r)
            continue
        # 去掉池关键词门后，是否仍是「公司行为+公司主体」的个股稿
        body = c[:520]
        if nr._NONPOOL_TICKER.search(body) or (
            nr._hit(body, nr.COMPANY_ACTIONS)
            and (nr._hit(body, nr.COMPANY_ENTITIES)
                 or nr._BRACKET.match(c[:120]) or nr._NAME_COLON.match(c[:120]))
        ):
            rescue_stock.append(r)
        else:
            other.append(r)
    n = len(rows)
    print(f"target_none 共 {n} 条")
    print(f"  预路由会拦下（个股/非池代码）      {len(caught):>4} ({len(caught)*100/n:.1f}%)")
    print(f"  被 POOL_KEYWORDS 宽词救回的个股稿  {len(rescue_stock):>4} ({len(rescue_stock)*100/n:.1f}%)")
    print(f"  别国宏观/海外杂讯（v408 前无人管） {len(other):>4} ({len(other)*100/n:.1f}%)")
    print(f"  └ v408 市场归属规则能拦下的         {len(by_scope):>4} ({len(by_scope)*100/n:.1f}%)")
    src = Counter((r["source"] or "?") for r in rows)
    print("  来源：", ", ".join(f"{k}={v}" for k, v in src.most_common(5)))
    print("\n  别国宏观样例：")
    for r in other[:6]:
        print(f"    #{(r['content'] or '')[:78]}")


def sec4(conn):
    print("\n" + "=" * 72)
    print("④ 门卫规则兜底的泄漏：rule_gate_fallback 对别国宏观是否放行")
    print("=" * 72)
    rows = conn.execute("""
        SELECT n.content FROM ai_decisions d JOIN raw_news n ON n.id = d.news_id
        WHERE d.analysis_degraded=1 AND d.analysis_degraded_reason='target_none'
    """).fetchall()
    passed = 0
    for r in rows:
        c = (r["content"] or "").replace("\n", " ").strip()
        if not nr.pool_out_reason(c[:120], c) and rule_gate_fallback(c[:120], c).get("pass"):
            passed += 1
    print(f"target_none 文本中，被规则兜底判「放行」的：{passed}/{len(rows)}")
    print("→ 兜底规则不是主渠道（只覆盖一小部分）；主渠道是 L1/L2 的正向关键词放行：")
    print("   「CPI / 美联储 / 黄金 / 原油」既是池内驱动词，也是别国本地数据的常用词，")
    print("   于是「以色列CPI」这类稿子一路过闸。缺的不是更长的词表，是「资产归属」判定。")


def sec5(conn):
    print("\n" + "=" * 72)
    print("⑤ v408 改动的验效：归档是否留痕 / 真信号是否还在丢")
    print("=" * 72)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(raw_news)")}
    if "filter_reason" not in cols:
        print("  raw_news 还没有 filter_reason 列 → v408 的 db 迁移未执行")
    else:
        row = conn.execute("""
            SELECT COUNT(*) n,
                   SUM(CASE WHEN filter_reason='stale_expired' THEN 1 ELSE 0 END) tracked
            FROM raw_news WHERE status='FILTERED'
        """).fetchone()
        print(f"  FILTERED 行 {row['n']} 条，其中带 stale_expired 留痕 {row['tracked'] or 0} 条")
        print("  （0 是正常的：留痕从下一次归档开始写；老行无法追溯，只能靠新口径）")
    row = conn.execute("""
        SELECT COUNT(*) n FROM raw_news
        WHERE status='FILTERED' AND is_noise=0 AND COALESCE(gate_verdict,'')='可交易'
    """).fetchone()
    print(f"  历史上「门卫判可交易却被归档」的存量：{row['n']} 条 ← v408 起不再这样归档")
    print("  生效判据（重启后看一天）：")
    print("    a. 日志出现 [BATCH] 归档超龄新闻 N 条（…门卫可交易的保留…）")
    print("    b. 上一条计数里 可交易 不再增长")
    print("    c. target_none 里「别国本地统计」类归零")


def main() -> int:
    conn = ro()
    try:
        sec1(conn)
        sec2(conn)
        sec3(conn)
        sec4(conn)
        sec5(conn)
    finally:
        conn.close()
    print("\n完成（只读，未改任何状态）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
