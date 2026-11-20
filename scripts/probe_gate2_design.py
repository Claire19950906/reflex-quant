#!/usr/bin/env python3
"""第二道闸门（不同市场/不同品类）的设计测算 —— 只读，不改任何状态。

为什么要这个脚本：v408 的市场归属规则只拦下 target_none 的 3.5%（7/201 条唯一新闻），
上一份评估里"87.5% 是别国宏观"是粗分类，不能当覆盖承诺。要判断"再加规则能加多少、
会误杀多少"，必须拿两份真实样本对撞：

  应拦样本（target_none）：AI 分析完判了"无标的"的新闻 —— 这些是纯浪费（烧了两次 LLM 才丢掉）
  不该拦样本（gate_verdict='可交易'）：门卫判过"能做"的新闻 —— 新规则误杀它们才是事故

⚠️ 口径警告（上一份报告写错过）：
  ai_decisions 是"一条新闻 × 每个目标品种"扇出写入，所以
  target_none 279 条 = 201 条唯一新闻（一条新闻能产生 3 条 HOLD/NVDA·XAU·WTI）。
  报降级率时分母是**资产决策行**，不是新闻条数。这个脚本一律按 news_id 去重。

用法：cd backend && python3 ../scripts/probe_gate2_design.py
"""
from __future__ import annotations

import collections
import re
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "backend" / "src_python"))

from feeds import news_router as nr

DB = HERE / "backend" / "trident_event_bus.db"


def ro() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def anchorless(t: str) -> bool:
    """全文没有能推动池内品种的锚点 —— 政治/公司口水类规则的必备前提。"""
    return not nr._hit(t, nr.STRONG_ANCHORS) and not nr._hit(t, nr.POOL_KEYWORDS)


RULES = [
    ("A 别国本地统计（已上线 v408）",
     lambda t: bool(nr.foreign_local_stat_reason(t))),
    ("B 暗盘/纯报价快照",
     lambda t: "暗盘" in t),
    ("E 非金融事件（火箭/赛事）",
     lambda t: bool(re.search(r"(火箭|卫星|冰球|锦标赛|联赛|演唱会|赛事|发射升空)", t))),
    ("D 政治口水（需无锚点）",
     lambda t: anchorless(t) and bool(re.search(
         r"(众议院|参议院|议员|国务院|首相|总统|部长|发言人|听证会|记者：|白宫|五角大楼)", t))),
    ("C 海外公司资本动作（需无锚点）",
     lambda t: anchorless(t) and bool(re.search(
         r"(供股|收购|并购|要约|ADR|ADS|财报|季度业绩|回购)", t))),
]


def load(conn):
    tn = {r["id"]: (r["content"] or "").replace("\n", " ") for r in conn.execute("""
        SELECT n.id, n.content FROM ai_decisions d JOIN raw_news n ON n.id = d.news_id
        WHERE d.analysis_degraded = 1 AND d.analysis_degraded_reason = 'target_none'
        GROUP BY n.id""")}
    pos = {r["id"]: (r["content"] or "").replace("\n", " ") for r in conn.execute("""
        SELECT id, content FROM raw_news WHERE COALESCE(gate_verdict, '') = '可交易'
        GROUP BY id""")}
    return tn, pos


def main() -> int:
    conn = ro()
    try:
        tn, pos = load(conn)
        print("=" * 76)
        print("① 样本口径")
        print("=" * 76)
        raw = conn.execute("""SELECT COUNT(*) n FROM ai_decisions
            WHERE analysis_degraded=1 AND analysis_degraded_reason='target_none'""").fetchone()["n"]
        print(f"  target_none 决策行 {raw} 条 → 去重后唯一新闻 {len(tn)} 条"
              f"（扇出倍数 {raw / max(len(tn), 1):.1f}x）")
        print(f"  门卫判「可交易」的新闻 {len(pos)} 条（不该被任何新规则拦下）")

        print("\n" + "=" * 76)
        print("② 每条候选规则的收益 / 代价")
        print("=" * 76)
        print(f"  {'规则':<28}{'拦住':>5}{'占比':>7}{'误杀':>6}{'误杀率':>8}")
        cov = set()
        for name, fn in RULES:
            hit = [i for i, t in tn.items() if fn(t)]
            bad = [i for i, t in pos.items() if fn(t)]
            cov |= set(hit)
            print(f"  {name:<28}{len(hit):>5}{len(hit) * 100 / len(tn):>6.1f}%"
                  f"{len(bad):>6}{len(bad) * 100 / len(pos):>7.1f}%")
        print(f"\n  并集覆盖 {len(cov)}/{len(tn)} = {len(cov) * 100 / len(tn):.1f}%  "
              f"→ 纯词表规则的天花板大约就在这里")

        print("\n" + "=" * 76)
        print("③ 已上线规则 A 的副作用（要知情，不是要回滚）")
        print("=" * 76)
        bad = [(i, pos[i]) for i, t in pos.items() if nr.foreign_local_stat_reason(t)]
        bad = [(i, t) for i, t in bad][:8]
        print(f"  历史「门卫判可交易」里会被 A 拦下的：{len([i for i, t in pos.items() if nr.foreign_local_stat_reason(t)])} 条，样例：")
        for i, t in bad:
            print(f"    #{i} {nr.foreign_local_stat_reason(t)} | {t[:66]}")
        print("  → 绝大多数是法国/意大利/西班牙的单国 CPI·PPI·GDP，门卫把它们当「黄金/原油」驱动是误判；")
        print("    但「欧元区」「欧央行」在 STRONG_ANCHORS 里，真欧洲宏观不会被拦。")

        print("\n" + "=" * 76)
        print("④ 词表拦不住的 74%：到底是什么")
        print("=" * 76)
        rest = [(i, t) for i, t in tn.items() if i not in cov]
        src = collections.Counter()
        for i, _t in rest:
            s = conn.execute("SELECT source FROM raw_news WHERE id=?", (i,)).fetchone()
            src[(s["source"] if s else "?")] += 1
        print("  剩余", len(rest), "条，来源：",
              ", ".join(f"{k}={v}" for k, v in src.most_common(5)))
        print("  样例：")
        for i, t in rest[:10]:
            print(f"    #{i} {t[:80]}")
        print("  → 主体是「美国政客/官员口水」和「海外公司稿」，而『美国』本身是池内强锚点")
        print("    （WTI/XAU 对美政策高度敏感），所以加词表必然误杀。这类只能靠")
        print("    「这条新闻与五个品种有没有因果」的语义判断，词表做不到。")
    finally:
        conn.close()
    print("\n完成（只读，未改任何状态）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
