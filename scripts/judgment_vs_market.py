#!/usr/bin/env python3
"""判断能力 vs 市场：一条命令出一份可存档的报告。

为什么单独做一个脚本（而不是只靠记分卡）
----------------------------------------
记分卡是"总表"，塞的是 20+ 项指标；而"对市场到底行不行"需要**同批样本对齐**
的三个数（裸命中 / 市场基线 / 平衡准确率）+ 一个扣费净期望。混在总表里读起来
容易只看命中率。这个脚本只干这一件事，并把结果写成带日期的 md + json，
方便每次迭代后做"改动前 / 改动后"的对照。

用法
----
    python3 scripts/judgment_vs_market.py                 # 影子账本，写当日报告
    python3 scripts/judgment_vs_market.py --no-write       # 只看不写
    python3 scripts/judgment_vs_market.py --source reflections
    python3 scripts/judgment_vs_market.py --fee-bps 20
    python3 scripts/judgment_vs_market.py --fixture bias   # 用合成样本看格式（不碰库）

数据还没进来时：报告会写"样本不足 n=0/200"，这是**正确答案**，不是失败。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend" / "src_python"))

import judgment_benchmark as jb

REPORT_DIR = ROOT / "docs" / "reports"


def _p(x: float, unit: str = "%") -> str:
    """None 一律渲染成 ——，绝不显示 None%。"""
    return "—" if x is None else f"{x:.1f}{unit}"


def _ci(c) -> str:
    if not c or c[0] is None:
        return "—"
    return f"[{c[0]:.1f}, {c[1]:.1f}]"


def _md(pack: dict, source: str) -> str:
    b, d, e, v = pack["baseline"], pack["by_direction"], pack["expectancy"], pack["verdict"]
    tp = pack.get("throughput") or {}
    n = pack["n_settled"]
    lines = [
        f"# 判断能力 vs 市场 · {time.strftime('%Y-%m-%d')}",
        "",
        f"> 口径 source=`{source}` · 已结算 n={n} · 门槛 {pack['gate']}"
        f" · 成本 {pack['fee_bps']:.0f}bps/往返 · 自动生成，只读。",
        "",
        "## 零、产能（样本会不会来）",
        "",
        "| 项 | 值 |",
        "|---|---|",
    ]
    if tp:
        eta = tp.get("eta_days")
        lines += [
            f"| 近 {tp['window_hours']}h 决策 | {tp['decisions']} 条 |",
            f"| 其中带方向 | {tp['directional']} 条（{_p(tp.get('directional_per_hour'), '/小时')}） |",
            f"| 其中池内可结算 | {tp.get('settleable')} 条（{_p(tp.get('settleable_per_hour'), '/小时')}） |",
            f"| 已结算 / 门槛 | {tp.get('settled_total') or 0} / {pack['gate']} |",
            "| 距可判定 | " + (f"约 {eta:.0f} 天" if eta else "**永远到不了（池内方向决策为 0）**") + " |",
        ]
    else:
        lines += ["| 近 24h 决策 | — |", "| 已结算 / 门槛 | — |"]

    if n == 0:
        lines += [
            "",
            "## 一、结论（现在是 0 样本，不给任何准确率数字）",
            "",
            f"**{v['headline']}**",
            "",
            v["detail"],
            "",
            "## 二、数据进来后会看到什么",
            "",
            "同一份报告会自动补上六段：裸命中 → 同批市场基线（无脑做空/同配比盲选）"
            " → 去偏置判别力（平衡准确率 BA + 区间）→ 扣费净期望 → 按品种 → 按方向。",
            "",
            "- 只有 `n≥" + str(pack["gate"]) + "` 且区间下界 > 50 才写“有判别力”；",
            "- 命中率高于基线但 BA≈50 → 判 beta（行情白送），不算能力；",
            "- 判对方向但净期望为负 → 追加成本警告，不判达标。",
            "",
            f"口径与用法：`docs/v490-判断力对市场-基准层.md`。",
        ]
        return "\n".join(lines)

    lines += [
        "",
        "## 一、裸命中（现状口径，只作参考）",
        "",
        "| 项 | 值 | 95% 区间 | n |",
        "|---|---|---|---|",
        f"| 方向命中 | {_p(pack['hit_rate'])} | {_ci(pack['hit_ci'])} | {n} |",
        f"| 其中 SELL | {_p(d['SELL']['rate'])} | {_ci(d['SELL']['ci'])} | {d['SELL']['n']} |",
        f"| 其中 BUY | {_p(d['BUY']['rate'])} | {_ci(d['BUY']['ci'])} | {d['BUY']['n']} |",
        "",
        "## 二、同批样本的市场基线（拿掉“行情白送”）",
        "",
        "| 项 | 值 | 说明 |",
        "|---|---|---|",
        f"| 真实涨/跌/平 | {b['up']} / {b['down']} / {b['flat']} | 平盘不计入分母 |",
        f"| 多空配比 | BUY {_p(b['p_buy'])} / SELL {_p(b['p_sell'])} | 系统自己的仓位结构 |",
        f"| 无脑做空基线 | {_p(b['always_sell'])} | 闭眼喊 SELL 也能到这个命中率 |",
        f"| 同配比盲选基线 | {_p(b['mix_implied'])} | 配比一样、方向与行情无关 |",
        f"| **裸命中 − 无脑做空** | **{_p(pack['excess_vs_always_sell'], 'pt')}** | 负数 = 不如闭眼做空 |",
        f"| 裸命中 − 同配比盲选 | {_p(pack['excess_vs_mix'], 'pt')} | 正数才有讨论价值 |",
        "",
        "## 三、去偏置后的判别力",
        "",
        f"- **平衡准确率 BA（真实标签）**：{_p(pack['ba_label']['value'])}"
        f"　区间 {_ci(pack['ba_label']['ci'])}",
        f"- 对照口径 BA'（按喊的方向）：{_p(pack['ba_pred'])}",
        "- 读数：BA 才是“有没有区分涨跌的能力”；命中率高于基线但 BA≈50 = beta，不是能力。",
        "",
        "## 四、扣费后的净期望",
        "",
        f"- 毛期望 {_p(e['gross_bps'], 'bps')} − 成本 {e['fee_bps']:.0f}bps = "
        f"**净 {_p(e['net_bps'], 'bps')}/笔** 区间 {_ci(e['ci'])}（n={e['n']}）",
        "- 判对方向 ≠ 赚钱；幅度不够覆盖成本时净期望照样为负。",
        "",
        "## 五、按品种",
        "",
        "| 品种 | n | 命中 | 期间均值 |",
        "|---|---|---|---|",
    ]
    for a in pack["by_asset"]:
        lines.append(f"| {a['asset']} | {a['n']} | {_p(a['rate'])} | {a['avg_move']:+.3f}% |")
    if not pack["by_asset"]:
        lines.append("| — | 0 | — | — |")

    lines += [
        "",
        "## 六、结论",
        "",
        f"**{v['headline']}**",
        "",
        v["detail"],
        "",
        "---",
        "",
        "读数规则：n < 门槛只报区间、不下结论；BA 达标要求“点估计 ≥55 且区间下界 >50”。",
        "改动代码后要做的对照：同一口径下 BA 与净期望有没有变（口径变了就不能比）。",
    ]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="")
    ap.add_argument("--source", default="ledger", choices=["ledger", "reflections"])
    ap.add_argument("--fee-bps", type=float, default=jb.DEFAULT_FEE_BPS)
    ap.add_argument("--gate", type=int, default=jb.GATE)
    ap.add_argument("--since-days", type=int, default=0)
    ap.add_argument("--fixture", default="", choices=["", "coin", "bias", "skill", "anti"])
    ap.add_argument("--no-write", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.fixture:
        samples = jb.make_fixture(args.fixture)
        source = f"fixture:{args.fixture}"
    else:
        dbp = Path(args.db).expanduser() if args.db else ROOT / "backend" / "trident_event_bus.db"
        if not dbp.exists():
            print(f"[环境错误] 找不到数据库: {dbp}", file=sys.stderr)
            return 2
        conn = sqlite3.connect(f"file:{dbp.resolve()}?mode=ro", uri=True, timeout=30)
        try:
            if args.source == "ledger":
                samples = jb.load_shadow_ledger(conn, since_days=args.since_days or None)
            else:
                samples = jb.load_shadow_reflections(conn, since_days=args.since_days or None)
        finally:
            conn.close()
        source = args.source

    pack = jb.benchmark(samples, fee_bps=args.fee_bps, gate=args.gate)
    pack["source"] = source
    if not args.fixture:
        conn = sqlite3.connect(f"file:{dbp.resolve()}?mode=ro", uri=True, timeout=30)
        try:
            pack["throughput"] = jb.throughput(conn, gate=args.gate)
        finally:
            conn.close()
        pack["verdict"] = jb.judge(pack, gate=args.gate)   # 产能信息会改写结论措辞

    if args.json:
        print(json.dumps(pack, ensure_ascii=False, indent=2))
    else:
        print(jb.render(pack))

    if not args.no_write:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d")
        suffix = f"-{args.fixture}" if args.fixture else ""
        (REPORT_DIR / f"judgment-vs-market-{stamp}{suffix}.md").write_text(_md(pack, source), encoding="utf-8")
        (REPORT_DIR / f"judgment-vs-market-{stamp}{suffix}.json").write_text(
            json.dumps(pack, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n[OK] 报告已写入 {REPORT_DIR / f'judgment-vs-market-{stamp}{suffix}.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
