# -*- coding: utf-8 -*-
"""ruler_selfcheck.py — 尺子自检：确认这把尺没被改动、且同口径可复现。

跑法：
    cd backend/src_python
    /Library/Frameworks/Python.framework/Versions/3.14/bin/python3 ruler_selfcheck.py

退出码：0 = 尺子完好且可复现；1 = 有问题（下面会指出是哪一项）。
它不写任何快照，只在内存里把尺子跑两遍做对比。
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
FROZEN = HERE / "frozen_ruler.py"


def load_ruler():
    spec = importlib.util.spec_from_file_location("frozen_ruler", FROZEN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


FAIL = []


def check(name, ok, detail=""):
    print(f"{'[PASS]' if ok else '[FAIL]'} {name}{('  ' + detail) if detail else ''}")
    if not ok:
        FAIL.append(name)


def main() -> int:
    fr = load_ruler()
    print("=" * 78)
    print(f"尺子自检 · 版本 {fr.RULER_VERSION}")
    print("=" * 78)

    # ── 1) 配置完整性：五条铁律是否都在代码里 ──
    lb = fr.LABEL
    check("铁律1 单一标签：进场 = cutoff 之后第一根收盘",
          lb.get("entry") == "first_close_after_cutoff_date", lb.get("entry"))
    check("铁律1 单一标签：出场 = 下一根收盘且超额剥 beta",
          lb.get("exit") == "next_bar_close" and lb.get("beta_strip") == "cross_section_median")
    check("铁律1 单一标签：中性波动剔除阈值 0.05%",
          abs(float(lb.get("exclude_neutral_move", -1)) - 0.0005) < 1e-9)
    check("铁律2 进场晚于所有可观测信息",
          lb.get("cutoff") == "max(signal_ts, observed_window_end)", lb.get("cutoff"))

    g = fr.GATES
    check("铁律4 样本门槛 n>=200", int(g.get("min_n", 0)) == 200, str(g.get("min_n")))
    check("铁律4 区间用 95%（z=1.96）", abs(float(g.get("z_ci", 0)) - 1.96) < 1e-9)
    check("毕业线 55%", abs(float(g.get("target", 0)) - 55.0) < 1e-9)

    # 铁律3：基准必须是实测出来的，不是写死 50
    conn = sqlite3.connect(f"file:{fr.DB_PATH}?mode=ro", uri=True)
    book = fr.PriceBook(conn)
    br, br_n = fr.base_rate(book)
    check("铁律3 基准为实测值（不是默认 50）",
          br_n >= 1000 and 45.0 <= br <= 55.0,
          f"实测 {br:.2f}% (n={br_n})")

    # 铁律5：每个指标有独立指纹，且指标清单非空
    ids = [s["id"] for s in fr.SIGNALS]
    fps = {s["id"]: fr._fingerprint_signal(s) for s in fr.SIGNALS}
    check("铁律5 指标级指纹齐全", len(fps) == len(ids) == len(set(fps.values())),
          f"{len(ids)} 个指标")
    check("指标清单数量（R3 应为 8）", len(ids) == 8, " ".join(ids))

    # ── 2) 可复现性：同一把尺跑两遍，逐字段必须完全一致 ──
    def strip(rep):
        d = {"base_rate": round(float(rep["base_rate"]["value"]), 4),
             "base_n": int(rep["base_rate"]["n"]),
             "rows": [{k: r.get(k) for k in ("id", "value", "n", "lo", "hi", "status", "fp")}
                      for r in rep["results"]]}
        return d

    rep1 = strip(fr.run(conn))
    rep2 = strip(fr.run(conn))
    ok = json.dumps(rep1, sort_keys=True, ensure_ascii=False) == \
         json.dumps(rep2, sort_keys=True, ensure_ascii=False)
    check("可复现：连跑两次逐字段一致", ok)
    if not ok:
        for a, b in zip(rep1["rows"], rep2["rows"]):
            for k in a:
                if a[k] != b[k]:
                    print(f"      差异 {a['id']}.{k}: {a[k]} vs {b[k]}")

    # ── 3) 结果快照表（供对照，不参与判定） ──
    print("-" * 78)
    print(f"基准 = {rep1['base_rate']:.2f}%  (n={rep1['base_n']})")
    print(f"{'ID':<5}{'值':>9}{'n':>7}  {'判定':<12}{'指纹'}")
    for r in rep1["rows"]:
        v = r["value"]
        if v is None:
            v = "—"
        elif str(r["id"]).startswith("E5"):      # ρ 类指标，按原值显示，不换算成百分比
            v = f"{v:+.3f}"
        else:
            v = f"{v:.2f}%"
        print(f"{r['id']:<5}{v:>9}{r['n']:>7}  {r['status']:<12}{r['fp']}")

    print("=" * 78)
    if FAIL:
        print(f"[SELFCHECK FAIL] {len(FAIL)} 项：{FAIL}")
        print("含义：这把尺被改动过，或数据底座变了 —— 历史对比不可用。")
        return 1
    print("[SELFCHECK PASS] 尺子完好：五条铁律在位、连续两次运行逐字段一致。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
