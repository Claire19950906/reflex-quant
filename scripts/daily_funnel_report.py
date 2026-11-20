#!/usr/bin/env python3
"""每日漏斗日报：新闻 → 决策 → 闸门 → 风控 → 成交 → 结算，逐段算通过率并给主因。

为什么写成一个作业：
    之前每次排查都要手工翻库：决策 327 条、只有 2 条过闸门、只有 2 笔成交……
    这些问题每天都在变，靠人肉巡检必然滞后。这个脚本把整条漏斗固化下来，
    每天自动回答"系统今天卡在哪一段"。

只读，不写业务表；结果同时打印 + 落 markdown（便于贴到报告或飞书）。

用法：
    python3 scripts/daily_funnel_report.py            # 打印
    python3 scripts/daily_funnel_report.py --write    # 同时写 reports/daily_funnel_YYYY-MM-DD.md
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sqlite3
import sys

sys.path.insert(0, "backend/src_python")
import config
import db


def _q(conn, sql, params=()):
    """只读查询。**查询失败不再是静默 0**（v452）。

    事故：`news_today` 查 `news_events.created_at`——这一列根本不存在，
    异常被吞成 0，于是漏斗日报每天印「今日 0」而没人发现。
    现在失败会打到 stderr 并以 -1 返回，报表里显示"查询失败"而不是"0"。
    """
    try:
        return conn.execute(sql, params).fetchone()[0]
    except sqlite3.Error as e:
        print(f"[FUNNEL] 查询失败（不是 0，是没查成）: {e} | SQL={sql.strip()[:80]}",
              file=sys.stderr)
        return -1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect(f"file:{db.get_db_path()}?mode=ro", uri=True, timeout=30)
    gate = float(getattr(config, "EVIDENCE_CONFIDENCE_GATE", 0) or 0)
    today = dt.date.today().isoformat()
    day_start = int(dt.datetime.combine(dt.date.today(), dt.time.min).timestamp())

    news_total = _q(conn, "SELECT COUNT(*) FROM news_events")
    # v452：news_events 没有 created_at 列（只有 ts，epoch 秒）。
    # 原来查 created_at 被 _q 吞成 0 → 日报长期印「今日 0」。
    news_today = _q(conn, "SELECT COUNT(*) FROM news_events WHERE ts >= ?",
                    (day_start,))
    pending = _q(conn, "SELECT COUNT(*) FROM news_events WHERE status='PENDING'")
    # v477：news_events 是**事件投影/下游分析表**，不是引擎队列。
    # 引擎 (engine.ai_worker._build_claim_sql) 认领的是 raw_news：
    # 实测 2026-11-19 两表差 70 倍（news_events PENDING 6,589 vs raw_news 95），
    # 拿投影表当积压数会得出「引擎卡死」的错误结论。两个口径都印出来。
    raw_total = _q(conn, "SELECT COUNT(*) FROM raw_news")
    raw_today = _q(conn, "SELECT COUNT(*) FROM raw_news WHERE ts >= ?", (day_start,))
    raw_pending = _q(conn, "SELECT COUNT(*) FROM raw_news WHERE status='PENDING'")
    dec_total = _q(conn, "SELECT COUNT(*) FROM ai_decisions")
    dec_today = _q(conn, "SELECT COUNT(*) FROM ai_decisions WHERE ts >= ?", (day_start,))
    dec_hold = _q(conn, "SELECT COUNT(*) FROM ai_decisions "
                        "WHERE UPPER(suggested_action)='HOLD'")
    # v400 (反思评审 2026-11-16): "产出决策数"包含 89% 的降级兜底（没看见 ≠ 判断为观望），
    # 直接拿它当产能会虚报 20 倍。产能只看「有效判断」：未降级 + 方向性。
    dec_degraded = _q(conn, "SELECT COUNT(*) FROM ai_decisions "
                           "WHERE COALESCE(analysis_degraded,0)=1")
    # v407: 降级里有一类不是"没分析完"，而是"这条新闻本来就不指向任何交易标的"
    # （decision_kind=routing_miss，实测全是 A股个股/宏观/行业快讯）。
    # 它挂 HOLD 是正确行为，混进降级率会把"不该交易"说成"分析失败"。
    dec_routing_miss = _q(conn, "SELECT COUNT(*) FROM ai_decisions "
                                "WHERE decision_kind='routing_miss'")
    dec_effective = _q(conn, "SELECT COUNT(*) FROM ai_decisions "
                             "WHERE COALESCE(analysis_degraded,0)=0 "
                             "AND UPPER(suggested_action)<>'HOLD'")
    dec_none_asset = _q(conn, "SELECT COUNT(*) FROM ai_decisions "
                              "WHERE UPPER(COALESCE(target_asset,'')) IN ('','NONE')")
    # v399：把「没有证据」和「算出来的低分」分开统计。
    # 降级路径 coverage=0，置信度数学上不存在 → 落库为 NULL；
    # 只有真正算过、结果恰为 0 的才算 conf0。混在一起会把"没分析"说成"信心低"。
    conf_null = _q(conn, "SELECT COUNT(*) FROM ai_decisions "
                         "WHERE evidence_confidence IS NULL")
    conf0 = _q(conn, "SELECT COUNT(*) FROM ai_decisions "
                     "WHERE evidence_confidence = 0")
    passed = _q(conn, "SELECT COUNT(*) FROM ai_decisions "
                      "WHERE COALESCE(evidence_confidence,0)>=?", (gate,))
    trades = _q(conn, "SELECT COUNT(*) FROM quick_sim_trades")
    settled = _q(conn, "SELECT COUNT(*) FROM quick_sim_trades WHERE COALESCE(settled,0)=1 "
                       "AND COALESCE(verdict,'')<>'ABANDONED'")
    win = _q(conn, "SELECT COUNT(*) FROM quick_sim_trades "
                   "WHERE UPPER(COALESCE(verdict,''))='WIN'")
    realized = _q(conn, "SELECT COALESCE(SUM(pnl_usdt),0) FROM quick_sim_trades "
                        "WHERE COALESCE(settled,0)=1")
    # v396 P2: 扣费后绩效（alpha 与成本必须分开看，混在一个数里永远归因不了）
    net_avg = _q(conn, "SELECT COALESCE(AVG(pnl_pct),0) FROM quick_sim_trades "
                       "WHERE COALESCE(settled,0)=1 AND verdict<>'ABANDONED'")
    gross_avg = _q(conn, "SELECT COALESCE(AVG(gross_pnl_pct),0) FROM quick_sim_trades "
                         "WHERE COALESCE(settled,0)=1 AND gross_pnl_pct IS NOT NULL")
    cost_avg_bps = _q(conn, "SELECT COALESCE(AVG(cost_bps),0) FROM quick_sim_trades "
                            "WHERE COALESCE(settled,0)=1")
    valid_n = _q(conn, "SELECT COUNT(*) FROM quick_sim_trades "
                       "WHERE COALESCE(settled,0)=1 AND verdict<>'ABANDONED'")
    net_pos = _q(conn, "SELECT COUNT(*) FROM quick_sim_trades "
                       "WHERE COALESCE(settled,0)=1 AND verdict<>'ABANDONED' AND pnl_pct>0")
    abandoned = _q(conn, "SELECT COUNT(*) FROM quick_sim_trades WHERE verdict='ABANDONED'")
    perf = _q(conn, "SELECT COUNT(*) FROM signal_performance")
    eq = conn.execute("SELECT equity, drawdown_pct FROM sim_equity_snapshots "
                      "ORDER BY ts DESC LIMIT 1").fetchone()

    # ── v477: AI 调用成本（当日）────────────────────────────────────────────
    # 决策侧只看「产出多少条」，不看「花了多少 token」，削减就没有依据：
    # 实测 2026-11-19 单条完整分析输入 10,645 token，而 43.6% 决策标的为 NONE。
    # 这里把 purpose 维度的调用量/输入输出/失败率钉进日报，成本才能被追责。
    cost_rows: list[tuple] = []
    cost_err = ""
    try:
        cost_rows = conn.execute(
            "SELECT purpose, COUNT(*), "
            "COALESCE(ROUND(AVG(tokens_in)),0), COALESCE(ROUND(AVG(tokens_out)),0), "
            "COALESCE(SUM(tokens_in+tokens_out),0) "
            "FROM ai_call_stats WHERE ts >= ? GROUP BY purpose "
            "ORDER BY SUM(tokens_in+tokens_out) DESC", (day_start,)
        ).fetchall()
    except sqlite3.Error as e:
        cost_err = str(e)
    call_total = _q(conn, "SELECT COUNT(*) FROM ai_call_stats WHERE ts >= ?", (day_start,))
    call_fail = _q(conn, "SELECT COUNT(*) FROM ai_call_stats "
                          "WHERE ts >= ? AND status<>'ok'", (day_start,))
    tokens_total = _q(conn, "SELECT COALESCE(SUM(tokens_in+tokens_out),0) "
                            "FROM ai_call_stats WHERE ts >= ?", (day_start,))
    # 每决策 / 每有效判断的 token 成本（分母为 0 时留空，不造数）
    tok_per_dec = (tokens_total / dec_today) if (dec_today and dec_today > 0) else None
    tok_per_eff = (tokens_total / dec_effective) if (dec_effective and dec_effective > 0) else None

    # ── v477: 按代码指纹分层（成交）──────────────────────────────────────────
    # 换代后旧样本不可比。成交行自带 code_fingerprint（继承决策行），
    # 这里按代次分开算，避免把两代算法混成一个胜率。
    fp_rows: list[tuple] = []
    fp_err = ""
    try:
        fp_rows = conn.execute(
            "SELECT COALESCE(NULLIF(code_fingerprint,''),'(未标注)') AS fp, "
            "COUNT(*) AS n, SUM(COALESCE(settled,0)) AS settled, "
            "SUM(CASE WHEN UPPER(COALESCE(verdict,''))='WIN' THEN 1 ELSE 0 END) AS win, "
            "COALESCE(SUM(pnl_usdt),0) AS pnl "
            "FROM quick_sim_trades GROUP BY 1 ORDER BY n DESC"
        ).fetchall()
    except sqlite3.Error as e:
        fp_err = str(e)

    # v488：降级原因分布 + AI 失败归因
    # 为什么必须分：日报一直只说"降级 9 条"，但降级有 6 种不同的原因
    # （超时 / schema 失败 / 输入缺失 / 兜底…），不拆开就只能"感觉"，
    # 而失败 38.8% 里 2005 条被记成笼统的 network、1363 条空码 —— 归因不了就没法修。
    degr_rows, degr_err = [], ""
    try:
        degr_rows = conn.execute(
            "SELECT COALESCE(NULLIF(analysis_degraded_reason,''),'(未注明)') r, "
            "COUNT(*) n FROM ai_decisions WHERE ts >= ? "
            "AND COALESCE(analysis_degraded,0)=1 GROUP BY 1 ORDER BY 2 DESC LIMIT 10",
            (day_start,),
        ).fetchall()
    except sqlite3.Error as e:
        degr_err = str(e)
    fail_rows, fail_err = [], ""
    try:
        fail_rows = conn.execute(
            "SELECT status, COALESCE(NULLIF(error_code,''),'(空)') ec, COUNT(*) n "
            "FROM ai_call_stats WHERE ts >= ? AND status <> 'ok' "
            "GROUP BY 1,2 ORDER BY 3 DESC LIMIT 12",
            (day_start,),
        ).fetchall()
    except sqlite3.Error as e:
        fail_err = str(e)

    def pct(a, b):
        return f"{a / b * 100:.1f}%" if b else "—"

    lines = [
        f"# 漏斗日报 {today}",
        "",
        "| 环节 | 数量 | 相对上一环 | 备注 |",
        "|---|---|---|---|",
        f"| 新闻入库（事件投影 news_events） | {news_total}（今日 {news_today}） | — | PENDING {pending}｜下游分析用，非引擎队列 |",
        f"| **引擎队列（raw_news）** | **{raw_total}（今日 {raw_today}）** | — | "
        f"**PENDING {raw_pending}**｜引擎实际认领的表 |",
        f"| 产出决策 | {dec_total}（今日 {dec_today}） | {pct(dec_total, raw_total)} | 含降级兜底 {dec_degraded} 条，**不等于产能** |",
        f"| **有效判断（未降级且非 HOLD）** | **{dec_effective}** | {pct(dec_effective, dec_total)} | 产能只看这一行；分母先剔除「非交易新闻」{dec_routing_miss} 条 |",
        f"| 其中：非交易新闻（routing_miss） | {dec_routing_miss} | {pct(dec_routing_miss, dec_total)} | 新闻不指向 5 个核心品种，挂 HOLD 是正确行为，不算分析失败 |",
        f"| 方向性（非 HOLD） | {dec_total - dec_hold} | {pct(dec_total - dec_hold, dec_total)} | HOLD {dec_hold}（其中降级 {dec_degraded}） |",
        f"| 标的可交易（非 NONE） | {dec_total - dec_none_asset} | {pct(dec_total - dec_none_asset, dec_total)} | NONE {dec_none_asset} |",
        f"| 过证据闸门（≥{gate:g}） | {passed} | {pct(passed, dec_total)} | 无证据(NULL) {conf_null} 条 / 真 0 分 {conf0} 条 |",
        f"| 实际成交 | {trades} | {pct(trades, passed)} | — |",
        f"| 已结算 | {settled} | {pct(settled, trades)} | 赢 {win} 笔 |",
        "",
        f"- 累计已结算盈亏：**{realized:+.2f} USDT**"
        + (f"，当前权益 {eq[0]:.2f}（回撤 {eq[1]:.2f}%）" if eq else ""),
        f"- 扣费后绩效：平均每笔 **{net_avg:+.3f}%**（毛 {gross_avg:+.3f}% / 往返成本 {cost_avg_bps:.0f}bps），"
        f"净收益为正 {net_pos}/{valid_n} 笔",
        (f"- 有效样本 {valid_n} 笔 < 60 门槛 → **当前数字不构成任何结论**"),
        f"- 无效样本（ABANDONED / 无到期价被废弃）：{abandoned} 笔，不计入任何胜率与均值",
        f"- 信号绩效表 signal_performance：**{perf} 行**",
        "",
        "## AI 调用成本（今日）",
    ]

    if cost_err:
        lines.append(f"- 查询失败（不是 0，是没查成）：{cost_err}")
    else:
        lines += [
            "| 用途 | 调用 | 平均输入 | 平均输出 | token | 占当日 |",
            "|---|---|---|---|---|---|",
        ]
        for purpose, n, tin, tout, tok in cost_rows:
            lines.append(
                f"| {purpose or '—'} | {n} | {tin} | {tout} | {tok} | "
                f"{pct(tok, tokens_total)} |"
            )
        lines += [
            "",
            f"- 当日合计：**{call_total} 次调用 / {tokens_total:,} token**，"
            f"失败 {call_fail} 次（{pct(call_fail, call_total)}）",
            (f"- 每决策成本：**{tok_per_dec:,.0f} token/条**" if tok_per_dec else
             "- 每决策成本：—（今日无决策）"),
            (f"- 每有效判断成本：**{tok_per_eff:,.0f} token/条**" if tok_per_eff else
             "- 每有效判断成本：—（今日无有效判断）"),
        ]
        # 样本太小时比率会被放大（5 条决策除以百万 token → 六位数/条），先声明口径
        if dec_today >= 0 and dec_today < 20:
            lines.append(f"- 注：今日决策仅 {dec_today} 条（<20），上面两条单条成本仅供参考，"
                         "不要直接当稳定值用")
        # 成本卡点：不设死阈值，只在明显异常时提示
        if tok_per_eff and tok_per_eff > 150_000:
            lines.append(f"- ⚠️ 单条有效判断烧 {tok_per_eff:,.0f} token，"
                         f"其中 NONE 标的 {dec_none_asset} 条仍在走完整分析")

    lines += ["", "## 降级原因分布（今日）"]
    if degr_err:
        lines.append(f"- 查询失败（不是 0，是没查成）：{degr_err}")
    else:
        degr_total = sum(int(n or 0) for _r, n in degr_rows)
        lines += ["| 原因 | 条数 | 占降级 |", "|---|---|---|"]
        for reason, n in degr_rows:
            lines.append(f"| {reason} | {n} | {pct(n, degr_total)} |")
        if not degr_rows:
            lines.append("| — | 0 | — |")
        lines.append("- 降级 = 没看完就挂 HOLD，**不是「判断为观望」**；"
                     "拆开才能知道该修超时、修 schema 还是修输入")

    lines += ["", "## AI 失败归因（今日）"]
    if fail_err:
        lines.append(f"- 查询失败（不是 0，是没查成）：{fail_err}")
    else:
        fail_total = sum(int(n or 0) for _s, _e, n in fail_rows)
        lines += ["| status | error_code | 次数 | 占失败 |", "|---|---|---|---|"]
        for st, ec, n in fail_rows:
            lines.append(f"| {st} | {ec} | {n} | {pct(n, fail_total)} |")
        if not fail_rows:
            lines.append("| — | — | 0 | — |")
        empt = sum(int(n or 0) for _s, e, n in fail_rows if e == "(空)")
        if empt:
            lines.append(f"- ⚠️ {empt} 条失败**没记错误码**，这类只能靠日志反查，"
                         "修方向：调用层补记异常类型 + HTTP 状态 + 重试次数")

    lines += ["", "## 按代码指纹分层（成交）"]
    if fp_err:
        lines.append(f"- 查询失败（不是 0，是没查成）：{fp_err}")
    else:
        lines += [
            "| 指纹（git/参数） | 成交 | 已结算 | 胜 | 净PnL(USDT) |",
            "|---|---|---|---|---|",
        ]
        for fp, n, s, w, p in fp_rows:
            lines.append(f"| `{fp}` | {n} | {int(s or 0)} | {int(w or 0)} | {float(p or 0):+.2f} |")
        if len(fp_rows) > 1:
            lines.append("- 多指纹并存 = 两代及以上样本，**不要合并算胜率**；先看各自样本量再决定")

    lines += ["", "## 今日主要卡点"]

    blockers = []
    if raw_pending > 200:
        blockers.append(f"引擎队列 raw_news 积压 {raw_pending} 条未分析（吞吐跟不上入库）")
    if pending > 500 and pending > max(raw_pending, 1) * 5:
        blockers.append(f"事件投影 news_events PENDING {pending} 远高于引擎队列 {raw_pending}"
                        " —— 下游投影同步滞后，不是引擎卡死（别按大数排查）")
    if dec_total and (dec_total - dec_hold) < 10:
        blockers.append(f"决策几乎全是 HOLD（{dec_hold}/{dec_total}），"
                        f"其中降级兜底 {dec_degraded} 条 —— 不是'判断为观望'，是'没分析完'")
    if dec_total and dec_effective < 10:
        blockers.append(f"有效判断只有 {dec_effective} 条/日（未降级且非 HOLD）"
                        f"—— 这才是真实产能，低于 10 条说明链路没跑通")
    if dec_none_asset:
        blockers.append(f"{dec_none_asset} 条决策的标的识别失败（target_asset=NONE），"
                        "这类即使过闸门也会被风控以 empty_asset 拦下")
    if dec_total and passed / max(1, dec_total) < 0.05:
        blockers.append(f"证据闸门只放行 {passed}/{dec_total}："
                        f"闸门依赖 evidence_confidence —— 无证据(NULL) {conf_null} 条，"
                        f"算过且为 0 的 {conf0} 条")
    if trades == 0:
        blockers.append("今日无成交")
    if not blockers:
        blockers.append("无明显卡点")
    lines += [f"{i + 1}. {b}" for i, b in enumerate(blockers)]

    text = "\n".join(lines)
    print(text)

    if args.write:
        out_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "backend", "reports", "funnel")
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, f"daily_funnel_{today}.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        print(f"\n已写入 {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
