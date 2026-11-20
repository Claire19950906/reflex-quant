# -*- coding: utf-8 -*-
"""v382 · 信号成绩单（可分享的只读快照）

为什么需要
----------
系统的真实数据都绑在本机 8100/8000 上，没法给外部看；而"给投资人/客户看一眼"
又不该让他们进后台。这个模块把关键口径做成**一张自包含 HTML**：

- 无外链、无脚本请求、单文件可发微信/邮件/放进仓库
- 数字全部当场从 SQLite 现算，并标注「数据截止时间」
- 明确写清样本量与局限（不写未来收益承诺）

生成：`python scorecard_share.py`（默认输出 docs/信号成绩单-YYYY-MM-DD.html）
"""
from __future__ import annotations

import argparse
import html
import os
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple


def _rows(conn, sql: str, params: Sequence[Any] | None = None) -> List[Tuple]:
    try:
        return conn.execute(sql, params or []).fetchall()
    except Exception:
        return []


def _scalar(conn, sql: str, params: Sequence[Any] | None = None, default: Any = 0) -> Any:
    r = _rows(conn, sql, params)
    return r[0][0] if r and r[0] else default


def collect(conn) -> Dict[str, Any]:
    """所有口径集中在这里，页面只负责渲染。任何一块查询失败都只是留空。"""
    now = datetime.now(timezone.utc)
    data: Dict[str, Any] = {"generated_at": now.strftime("%Y-%m-%d %H:%M UTC")}

    # ── 总览 ──
    data["news_total"] = int(_scalar(conn, "SELECT COUNT(*) FROM raw_news"))
    # v498: timestamp 存 epoch 秒的字符串，julianday('1795102900') 返回 NULL，
    #   这里此前恒等于 0（成绩单显示"最近 24h 0 条"，实际当日 1,500+）。改用 ts 数值窗口。
    data["news_24h"] = int(_scalar(
        conn, "SELECT COUNT(*) FROM raw_news WHERE ts >= strftime('%s','now','-24 hours')"))
    data["decisions"] = int(_scalar(conn, "SELECT COUNT(*) FROM ai_decisions"))
    data["settled"] = int(_scalar(
        conn, "SELECT COUNT(*) FROM ai_decisions WHERE settled=1 AND forward_pnl IS NOT NULL"))
    data["wins"] = int(_scalar(
        conn, "SELECT COUNT(*) FROM ai_decisions WHERE settled=1 AND forward_pnl>0"))
    data["avg_pnl"] = float(_scalar(
        conn, "SELECT COALESCE(AVG(forward_pnl),0) FROM ai_decisions WHERE settled=1", default=0.0))
    data["sum_pnl"] = float(_scalar(
        conn, "SELECT COALESCE(SUM(forward_pnl),0) FROM ai_decisions WHERE settled=1", default=0.0))
    data["winrate"] = round(data["wins"] / data["settled"] * 100, 1) if data["settled"] else None

    # ── 漏斗（新闻 → 分析 → 方向 → 闸门 → 结算 → 盈利）──
    analyzed_news = int(_scalar(conn, "SELECT COUNT(DISTINCT news_id) FROM ai_decisions"))
    directional = int(_scalar(
        conn, "SELECT COUNT(*) FROM ai_decisions WHERE UPPER(COALESCE(suggested_action,'')) IN ('BUY','SELL')"))
    gated = int(_scalar(
        conn, """SELECT COUNT(*) FROM ai_decisions
                  WHERE UPPER(COALESCE(suggested_action,'')) IN ('BUY','SELL')
                    AND COALESCE(trade_gate_reason,'')=''"""))
    data["funnel"] = [
        ("新闻入库", data["news_total"]),
        ("进入分析", analyzed_news),
        ("产出方向", directional),
        ("未被打回", gated),
        ("已结算", data["settled"]),
        ("盈利", data["wins"]),
    ]

    # ── 周对比（v391）：近 7 天 vs 前 7 天，只看已结算 ──
    try:
        week = _rows(conn, """
            SELECT COUNT(*), SUM(CASE WHEN forward_pnl>0 THEN 1 ELSE 0 END),
                   COALESCE(AVG(forward_pnl),0)
              FROM ai_decisions
             WHERE settled=1 AND forward_pnl IS NOT NULL
               AND julianday('now') - julianday(COALESCE(NULLIF(entry_time,''), created_at)) <= 7""")
        prev = _rows(conn, """
            SELECT COUNT(*), SUM(CASE WHEN forward_pnl>0 THEN 1 ELSE 0 END),
                   COALESCE(AVG(forward_pnl),0)
              FROM ai_decisions
             WHERE settled=1 AND forward_pnl IS NOT NULL
               AND julianday('now') - julianday(COALESCE(NULLIF(entry_time,''), created_at)) > 7
               AND julianday('now') - julianday(COALESCE(NULLIF(entry_time,''), created_at)) <= 14""")
        for key, rows in (("week", week), ("prev_week", prev)):
            r = rows[0] if rows else (0, 0, 0.0)
            n = int(r[0] or 0)
            w = int(r[1] or 0)
            data[key] = {"n": n, "wins": w,
                         "winrate": round(w / n * 100, 1) if n else None,
                         "avg_pnl": round(float(r[2] or 0), 2)}
        a, b = data["week"], data["prev_week"]
        data["week_delta"] = {
            "n": a["n"] - b["n"],
            "winrate": (round(a["winrate"] - b["winrate"], 1)
                        if a["winrate"] is not None and b["winrate"] is not None else None),
            "avg_pnl": round(a["avg_pnl"] - b["avg_pnl"], 2),
        }
    except Exception:
        data.setdefault("week", {"n": 0, "wins": 0, "winrate": None, "avg_pnl": 0.0})

    # ── 近 14 天逐日走势（v395）：已结算笔数 + 日均盈亏 ──
    try:
        rows = _rows(conn, """
            SELECT date(COALESCE(NULLIF(entry_time,''), created_at)) AS d,
                   COUNT(*), COALESCE(AVG(forward_pnl),0)
              FROM ai_decisions
             WHERE settled=1 AND forward_pnl IS NOT NULL
               AND julianday('now') - julianday(COALESCE(NULLIF(entry_time,''), created_at)) <= 14
             GROUP BY d ORDER BY d""")
        data["daily"] = [{"date": r[0], "n": int(r[1] or 0),
                          "avg_pnl": round(float(r[2] or 0), 2)} for r in rows if r[0]]
    except Exception:
        data["daily"] = []

    # ── 用户处置：静默多少品种、忽略多少笔（v387）──
    # 静默只影响后续写入，不追溯已结算记录；这里只报「当前仍生效」与「累计」
    try:
        now_ts = int(time.time())
        data["muted_now"] = int(_scalar(conn, """
            SELECT COUNT(DISTINCT asset) FROM decision_actions
             WHERE action='silence_asset' AND (expires_at=0 OR expires_at>?)""", (now_ts,), default=0))
        data["muted_total"] = int(_scalar(conn, """
            SELECT COUNT(*) FROM decision_actions WHERE action='silence_asset'""", default=0))
        data["ignored"] = int(_scalar(conn, """
            SELECT COUNT(*) FROM ai_decisions WHERE COALESCE(quarantined_at,'')!=''""", default=0))
        data["reversed_shadow"] = int(_scalar(conn, """
            SELECT COUNT(*) FROM decision_actions WHERE action='reverse'""", default=0))
    except Exception:
        data.setdefault("muted_now", 0)

    # ── 闸门拦截原因 top ──
    data["gate_reasons"] = [
        {"reason": (r[0] or "")[:90], "n": int(r[1] or 0)}
        for r in _rows(conn, """
            SELECT trade_gate_reason, COUNT(*) FROM ai_decisions
             WHERE COALESCE(trade_gate_reason,'') != ''
             GROUP BY trade_gate_reason ORDER BY 2 DESC LIMIT 8""")
    ]

    # ── 错误码 ──
    data["errors"] = [
        {"code": r[0], "n": int(r[1] or 0), "avg_w": float(r[2] or 0)}
        for r in _rows(conn, """
            SELECT error_code, COUNT(*), COALESCE(AVG(weight),0)
              FROM decision_errors GROUP BY error_code ORDER BY 2 DESC LIMIT 8""")
    ]

    # ── 漏检 ──
    data["missed"] = [
        {"kind": r[0], "n": int(r[1] or 0)}
        for r in _rows(conn, """
            SELECT COALESCE(subcategory, dimension, 'unknown'), COUNT(*)
              FROM missed_signals GROUP BY 1 ORDER BY 2 DESC LIMIT 8""")
    ]

    # ── 反思维度 ──
    data["dimensions"] = [
        {"dim": r[0], "n": int(r[1] or 0)}
        for r in _rows(conn, """
            SELECT COALESCE(dimension,'?'), COUNT(*) FROM trade_reflections_v2
             GROUP BY 1 ORDER BY 2 DESC LIMIT 6""")
    ]

    # ── 信息源 ──
    data["sources"] = [
        {"name": r[0], "n": int(r[1] or 0)}
        for r in _rows(conn, """
            SELECT source, COUNT(*) FROM raw_news GROUP BY source ORDER BY 2 DESC LIMIT 8""")
    ]

    # ── 结算器覆盖 ──
    data["signal_perf"] = int(_scalar(conn, "SELECT COUNT(*) FROM signal_performance"))
    return data


# ──────────────────────────────────────────────────────────────────────────────
# 渲染
# ──────────────────────────────────────────────────────────────────────────────
_CSS = """
:root{--bg:#0b0e14;--card:#131722;--line:#222736;--fg:#e6e8ee;--mut:#8a90a2;
--ok:#26a69a;--bad:#ef5350;--warn:#f0b90b;--accent:#4f8cff}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font-family:-apple-system,"PingFang SC","Microsoft YaHei",monospace;line-height:1.5}
.wrap{max-width:1080px;margin:0 auto;padding:32px 20px 64px}
h1{font-size:22px;margin:0 0 6px;letter-spacing:.5px}
.sub{color:var(--mut);font-size:12px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin:22px 0}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px}
.kpi .k{color:var(--mut);font-size:11px}
.kpi .v{font-size:26px;font-weight:600;margin-top:4px}
.kpi .n{color:var(--mut);font-size:11px;margin-top:2px}
.sec{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin:14px 0}
.sec h2{font-size:13px;margin:0 0 12px;color:var(--mut);font-weight:600;letter-spacing:.5px;text-transform:uppercase}
table{width:100%;border-collapse:collapse;font-size:12px}
th,td{padding:7px 8px;border-bottom:1px solid var(--line);text-align:left}
th{color:var(--mut);font-weight:500}
td.r{text-align:right;font-variant-numeric:tabular-nums}
.bar{height:6px;background:#1d2231;border-radius:3px;overflow:hidden}
.bar>i{display:block;height:100%;background:var(--accent)}
.funnel{display:flex;flex-direction:column;gap:10px}
.daily{display:flex;align-items:flex-end;gap:6px;height:64px}
.dbar{display:flex;align-items:flex-end;height:60px}
.dbar i{display:block;width:14px;border-radius:3px;opacity:.85}
.frow{display:grid;grid-template-columns:96px 1fr 64px;gap:10px;align-items:center;font-size:12px}
.note{color:var(--mut);font-size:11px;margin-top:10px}
.tag{display:inline-block;padding:2px 8px;border-radius:999px;font-size:11px;border:1px solid var(--line);color:var(--mut);margin-right:6px}
footer{color:var(--mut);font-size:11px;margin-top:24px;border-top:1px solid var(--line);padding-top:12px}
@media(max-width:640px){.wrap{padding:20px 14px}.kpi .v{font-size:22px}}
"""


def _esc(v: Any) -> str:
    return html.escape(str(v))


def _pct(v: Optional[float], signed: bool = False) -> str:
    if v is None:
        return "—"
    return f"{v:+.2f}%" if signed else f"{v:.1f}%"


def render(data: Dict[str, Any]) -> str:
    kpis = [
        ("新闻入库", f"{data['news_total']:,}", f"最近 24h {data['news_24h']:,} 条"),
        ("已结算信号", f"{data['settled']}", f"共产出 {data['decisions']} 条判断"),
        ("胜率", _pct(data["winrate"]), f"{data['wins']} 笔盈利"),
        ("平均每笔", _pct(data["avg_pnl"], signed=True), f"累计 {data['sum_pnl']:+.2f}%"),
    ]
    kpi_html = "".join(
        f"<div class='kpi'><div class='k'>{_esc(k)}</div>"
        f"<div class='v'>{_esc(v)}</div><div class='n'>{_esc(n)}</div></div>"
        for k, v, n in kpis
    )

    daily = data.get("daily") or []
    daily_max = max(1, *(d["n"] for d in daily)) if daily else 1
    daily_html = "".join(
        f"<span class='dbar' title='{html.escape(str(d['date']))} · {d['n']} 笔 · {d['avg_pnl']:+.2f}%'>"
        f"<i style='height:{max(6, (d['n'] / daily_max) * 56):.0f}px;"
        f"background:{'#10b981' if d['avg_pnl'] >= 0 else '#ef4444'}'></i></span>"
        for d in daily
    ) or "<span class='hint'>近 14 天还没有已结算样本</span>"
    funnel_max = max(1, *(n for _, n in data["funnel"]))
    funnel_html = "".join(
        f"<div class='frow'><span>{_esc(name)}</span>"
        f"<div class='bar'><i style='width:{(n / funnel_max) * 100:.1f}%'></i></div>"
        f"<span class='r'>{n:,}</span></div>"
        for name, n in data["funnel"]
    )

    def table(headers: List[str], rows: List[List[Any]], right_from: int = 1) -> str:
        if not rows:
            return "<div class='note'>暂无数据</div>"
        th = "".join(f"<th>{_esc(h)}</th>" for h in headers)
        body = ""
        for row in rows:
            tds = "".join(
                f"<td class='{'r' if i >= right_from else ''}'>{_esc(c)}</td>"
                for i, c in enumerate(row)
            )
            body += f"<tr>{tds}</tr>"
        return f"<table><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table>"

    gate_html = table(
        ["拦截原因（截断）", "条数"],
        [[g["reason"], g["n"]] for g in data["gate_reasons"]],
    )
    err_html = table(
        ["错误码", "次数", "平均权重"],
        [[e["code"], e["n"], f"{e['avg_w']:.0f}"] for e in data["errors"]],
    )
    missed_html = table(["漏检类型", "条数"], [[m["kind"], m["n"]] for m in data["missed"]])
    dim_html = table(["反思维度", "条数"], [[d["dim"], d["n"]] for d in data["dimensions"]])
    src_html = table(["信息源", "条数"], [[s["name"], s["n"]] for s in data["sources"]])

    gap = ""
    if data["funnel"]:
        a = data["funnel"][0][1]
        b = data["funnel"][-1][1]
        if a:
            gap = f"从新闻到盈利的端到端转化率：{b / a * 100:.2f}%（{b:,} / {a:,}）"

    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>信号成绩单 · {_esc(data['generated_at'])}</title>
<style>{_CSS}</style></head>
<body><div class="wrap">
<h1>信号成绩单</h1>
<div class="sub">数据截止 {_esc(data['generated_at'])} · 只读快照 · 不含任何未来收益承诺</div>
<div class="grid">{kpi_html}</div>

<div class="sec"><h2>周对比</h2>
<div class="kv">近 7 天 <b>{data.get('week', {}).get('n', 0)}</b> 笔 · 胜率 <b>{data.get('week', {}).get('winrate') if data.get('week', {}).get('winrate') is not None else '—'}</b> · 平均 <b>{data.get('week', {}).get('avg_pnl', 0):+.2f}%</b><br>
前 7 天 <b>{data.get('prev_week', {}).get('n', 0)}</b> 笔 · 胜率 <b>{data.get('prev_week', {}).get('winrate') if data.get('prev_week', {}).get('winrate') is not None else '—'}</b> · 平均 <b>{data.get('prev_week', {}).get('avg_pnl', 0):+.2f}%</b><br>
<span class="hint">样本以 10 笔为界；不足 10 笔的周对比只作方向参考，不当作改善/恶化结论。</span></div></div>
<div class="sec"><h2>近 14 天（已结算日均盈亏）</h2>
<div class="daily">{daily_html}</div>
<div class="hint">仅有结算的日期才会出现；柱高=笔数，颜色=当天平均盈亏（绿赚红亏）。</div></div>
<div class="sec"><h2>用户处置</h2>
<div class="kv">当前静默品种 <b>{data.get('muted_now', 0)}</b> 个 · 静默累计 <b>{data.get('muted_total', 0)}</b> 次 · 已忽略决策 <b>{data.get('ignored', 0)}</b> 笔 · 反手影子 <b>{data.get('reversed_shadow', 0)}</b> 笔<br>
<span class="hint">静默与忽略只作用于之后写入的信号，不改动已结算记录，因此不并入上方漏斗计数。</span></div></div>
<div class="sec"><h2>成交漏斗</h2><div class="funnel">{funnel_html}</div>
<div class="note">{_esc(gap) if gap else ''}　各层为独立计数：历史写入路径与口径差异会让个别层不严格包含，
转化率只在相邻层之间成立；「未被打回」指 direction 决策中没有 gate 原因的样本。</div></div>

<div class="sec"><h2>闸门拦截原因（Top 8）</h2>{gate_html}
<div class="note">拦截不是坏事：它把"没有把握"显式写出来，比硬发信号更接近真实。</div></div>

<div class="sec"><h2>错误码分布</h2>{err_html}</div>
<div class="sec"><h2>漏检分布</h2>{missed_html}</div>
<div class="sec"><h2>反思维度覆盖</h2>{dim_html}
<div class="note">A 维=事件/信号识别，B 维=方向判断，C 维=执行与结算。C 维占绝大多数属于正常结构。</div></div>
<div class="sec"><h2>信息源构成</h2>{src_html}</div>

<footer>
口径说明：胜率 = 已结算且 forward_pnl&gt;0 的笔数 / 已结算笔数；平均每笔为百分比收益的算术均值，未计手续费与滑点。
样本量 &lt;30 时不具统计显著性。本页由脚本从本地 SQLite 现算生成，不含人工修改。
</footer>
</div></body></html>"""


def generate(out_path: Optional[str] = None, conn=None) -> str:
    own = False
    if conn is None:
        import db as _db
        conn = _db.get_connection()
        own = True
    try:
        data = collect(conn)
    finally:
        if own:
            conn.close()
    if not out_path:
        base = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "docs")
        os.makedirs(base, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d")
        out_path = os.path.join(base, f"信号成绩单-{stamp}.html")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(render(data))
    return out_path


def _main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="生成可分享的信号成绩单（只读）")
    ap.add_argument("--out", default="", help="输出路径（默认 docs/信号成绩单-YYYY-MM-DD.html）")
    args = ap.parse_args(argv)
    path = generate(args.out or None)
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
