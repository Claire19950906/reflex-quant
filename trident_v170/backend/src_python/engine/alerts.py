"""Feishu (飞书) trading-signal alert push — fire-and-forget.

v171 升级：
- 时间字段用 tz_utils.format_dual 统一展示 UTC / 北京 / 美东 / 美西（按 TRIDENT_TZ 切换）
- 飞书原生 fields 数组替代单一 markdown 字符串（更易解析、便于样式）
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime

import config
import tz_utils

from .utils import _now


_feishu_warned = False  # 模块级标志位 — webhook 未配置时只警告一次
_mail_warned = False    # SMTP 未配置时只警告一次
_wechat_warned = False  # 微信通道未配置时只警告一次


def _send_mail_sync(subject: str, body: str) -> None:
    """SMTP 邮件告警（QQ 邮箱等 SSL 465；SMTP_PASS 用授权码）。fire-and-forget，失败只打印。"""
    global _mail_warned
    if not (config.SMTP_HOST and config.SMTP_USER and config.SMTP_PASS and config.SMTP_TO):
        if not _mail_warned:
            _mail_warned = True
            print("[MAIL] SMTP 未完整配置（SMTP_HOST/USER/PASS/TO），邮件告警已跳过")
        return
    try:
        import smtplib
        from email.header import Header
        from email.mime.text import MIMEText

        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = Header(subject, "utf-8")
        msg["From"] = config.SMTP_FROM or config.SMTP_USER
        msg["To"] = config.SMTP_TO
        with smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT, timeout=15) as server:
            server.login(config.SMTP_USER, config.SMTP_PASS)
            server.sendmail(msg["From"], [config.SMTP_TO], msg.as_string())
        print(f"[{_now()}] [MAIL] 已发送: {subject[:60]}")
    except Exception as e:
        print(f"[{_now()}] [MAIL] ERROR: {type(e).__name__}: {e}")


def _send_serverchan_sync(title: str, desp: str) -> None:
    """Server酱 微信推送 POST。fire-and-forget，失败只打印。"""
    global _wechat_warned
    if not config.SERVERCHAN_SENDKEY:
        if not _wechat_warned:
            _wechat_warned = True
            print("[WECHAT] SERVERCHAN_SENDKEY 未配置，微信告警(Server酱)跳过")
        return
    try:
        import urllib.parse
        url = f"https://sctapi.ftqq.com/{config.SERVERCHAN_SENDKEY}.send"
        data = urllib.parse.urlencode({"title": title, "desp": desp}).encode("utf-8")
        req = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"},
        )
        resp = urllib.request.urlopen(req, timeout=10)
        body = resp.read().decode("utf-8", errors="replace")
        print(f"[{_now()}] [WECHAT] HTTP {resp.status} — {body[:200]}")
    except Exception as e:
        print(f"[{_now()}] [WECHAT] ERROR: {type(e).__name__}: {e}")


def _send_pushplus_sync(title: str, content: str) -> None:
    """PushPlus 微信推送。fire-and-forget，失败只打印。"""
    if not config.PUSHPLUS_TOKEN:
        return
    try:
        payload = json.dumps(
            {"token": config.PUSHPLUS_TOKEN, "title": title, "content": content,
             "template": "markdown"},
            ensure_ascii=False,
        ).encode("utf-8")
        req = urllib.request.Request(
            "http://www.pushplus.plus/send",
            data=payload,
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        resp = urllib.request.urlopen(req, timeout=10)
        body = resp.read().decode("utf-8", errors="replace")
        print(f"[{_now()}] [PUSHPLUS] HTTP {resp.status} — {body[:200]}")
    except Exception as e:
        print(f"[{_now()}] [PUSHPLUS] ERROR: {type(e).__name__}: {e}")


def _fire_and_forget(fn, *args) -> None:
    """后台线程执行推送，同步/异步上下文均可调用，绝不阻塞调用方。"""
    try:
        threading.Thread(target=fn, args=args, daemon=True).start()
    except Exception:
        pass


_ACTION_CN = {"BUY": "买涨", "SELL": "买跌"}


def build_signal_title(asset: str = "", action: str = "", news_time: str = "") -> str:
    """手机推送标题：交易产品 + 买涨/买跌方向 + 时间（北京优先）。"""
    action = (action or "").strip().upper()
    cn = _ACTION_CN.get(action, "观望")
    label = (asset or "N/A").strip()
    time_str = ""
    if news_time:
        try:
            dual = tz_utils.format_dual(news_time) or {}
            raw = dual.get("cn") or dual.get("display") or str(news_time)
            time_str = str(raw)[:16]
        except Exception:
            time_str = str(news_time)[:16]
    if time_str:
        return f"{label} {cn} · {time_str}"
    return f"{label} {cn}"


def format_signal_body(*, asset: str = "", category: str = "", action: str = "",
                       impact: float = 0.0, heat: float = 0.0,
                       reason: str = "", analysis: str = "", news_text: str = "",
                       news_time: str = "", extra_line: str = "") -> str:
    """统一手机推送正文（微信 desp / 邮件 body / PushPlus 共用）。

    v3 排版（三通道换行兼容）：
      1) 交易信息放最前——资产 + 买涨/买跌方向 + 时间（北京优先）一眼定位；
      2) 之后才是影响分/热度、AI 逻辑、分析过程、新闻原文；
      3) 每个字段空行分隔（Markdown 段落换行），避免微信/PushPlus 把文字揉成一坨。
    """
    action = (action or "").strip().upper()
    action_cn = _ACTION_CN.get(action, "观望")
    emoji = {"BUY": "📈", "SELL": "📉"}.get(action, "📊")
    label = f"{asset or 'N/A'}".strip()
    if category:
        label = f"{label}（{category}）"

    # 时间：北京优先，紧跟交易信息
    time_cn = ""
    time_full = ""
    if news_time:
        try:
            dual = tz_utils.format_dual(news_time) or {}
        except Exception:
            dual = {}
        if dual.get("display") or dual.get("iso"):
            time_cn = dual.get("cn") or dual.get("display") or str(news_time)[:19]
            time_full = (
                f"UTC {dual.get('utc', '')} · 美东 {dual.get('us_eastern', '')} · "
                f"美西 {dual.get('us_pacific', '')}"
            )
        else:
            time_cn = str(news_time)[:19]

    # ── ① 交易信息（进入页面第一眼）──
    lines = [
        f"{emoji} {label}",
        f"方向：{action_cn}（{action}）",
    ]
    if time_cn:
        lines.append(f"时间：北京 {time_cn}")
        if time_full:
            lines.append(f"全时区：{time_full}")

    lines.append("————————————")

    # ── ② 强度 ──
    lines.append(f"影响分：{impact:.0f}/10")
    lines.append(f"热度：{heat:.0f}/10")
    if extra_line:
        lines.append(extra_line.strip())

    # ── ③ AI 逻辑 + 分析 ──
    lines.append(f"AI 逻辑：{reason or 'N/A'}")
    if analysis:
        lines.append(f"分析过程：\n{analysis.strip()}")

    # ── ④ 新闻原文 ──
    lines.append(f"新闻原文：{news_text or ''}")

    # 空行分隔每个字段：Markdown(微信/PushPlus) 以段落换行，邮件纯文本同样清晰
    return "\n\n".join(lines)


def send_important_news(title: str, body: str) -> None:
    """重要新闻 / 交易提醒手机推送——Server酱(微信) + 邮件 + PushPlus 三通道 fire-and-forget。

    各通道未配置时各自静默跳过（Server酱缺 key 只警告一次）。任何单通道失败不影响其它通道。
    """
    _fire_and_forget(_send_serverchan_sync, title, body)
    _fire_and_forget(_send_mail_sync, title, body)
    _fire_and_forget(_send_pushplus_sync, title, body)


def send_strong_buy_alert(title: str, body: str) -> None:
    """强买入提醒（SK 海力士等个股重要利多）——标题加「强买入提醒」前缀推送到手机。

    仅发送信号提醒，不触发自动交易（模拟盘下单仍由 decision_guard / paper_trading 各自闸门控制）。
    """
    send_important_news(f"强买入提醒：{title}", body)


def format_fed_anomaly_message(anomaly: Dict[str, Any],
                               expectation: Dict[str, Any],
                               *,
                               title_only: bool = False,
                               data_source: Optional[str] = None) -> str:
    """v226 异动消息格式化（手机推送 + 邮件 + PushPlus 共用）。

    v283: 重写为人话版 —— lead with 结论(鹰派/鸽派/不动) + 市场影响(↑↓),
    把 "bp" "无套利" "几何平均" 等 jargon 全部删掉。普通用户能直接看懂。

    v284: 新增「判断依据」板块 —— 用户问"是不是发生了什么事",
    这里告诉他:
      · 数据源(美联储官方 FRED / 代理估算 / 失败)
      · 触发原因(利率端动了 vs 预期端动了 vs 双触发,各超阈值多少)
      · 关联新闻(6h 内 raw_news 表 Fed 关键词,未关联则诚实标注)

    Parameters
    ----------
    anomaly : dict from fed_rate_expectation.fetch_expectation()["anomaly"]
        含 is_anomaly / type / severity / message / yield_move_bp / expect_move_bp /
        threshold_bp / threshold_regime / threshold_vix /
        trigger_reason / news_refs (v284 新增)
    expectation : dict from fed_rate_expectation.fetch_expectation()["expectation"]
        含 current_effr_pct / dgs2_pct / spread_bp / expected_24m_change_bp /
        direction / narrative / market_impact_simple / email_subject
    data_source : str | None
        取自 snap.data_source —— "fred" / "fred_dgs2_yfinance_irx_proxy" 等
    title_only : bool
        True → 返回短标题（≤ 50 字，手机推送栏可见）；
        False → 返回完整正文（结论 + 影响 + 数据 + 解释 + 判断依据）。

    Returns
    -------
    str
    """
    severity = (anomaly or {}).get("severity", "low")
    severity_cn = {"high": "🔴高", "med": "🟡中", "low": "🟢低"}.get(severity, "🟢低")
    direction = (expectation or {}).get("direction", "neutral")
    expect_bp = float((expectation or {}).get("expected_24m_change_bp", 0) or 0)
    yield_bp = float((anomaly or {}).get("yield_move_bp", 0) or 0)
    dgs2 = float((expectation or {}).get("dgs2_pct", 0) or 0)
    effr = float((expectation or {}).get("current_effr_pct", 0) or 0)

    # ─── 人话版结论 ───
    # 不再用 jargon:"bp / 无套利 / 几何平均 / 折现回来"。
    # 用户只关心:鹰派/鸽派/不动 + 加几次 + 涨跌什么
    abs_steps = abs(expect_bp) / 25.0  # 标准美联储一次动 25bp
    step_str = f"{abs_steps:.1f}".rstrip("0").rstrip(".")
    bp_str = f"{abs(expect_bp):.0f}"

    if direction == "hawkish":
        verdict = f"美联储要加息（鹰派）"
        verdict_explain = (
            f"市场认为未来 2 年美联储还会累计加息约 {bp_str} 个点"
            f"（标准动作 25 个点一次 → 大约 {step_str} 次）"
        )
        impact_lines = [
            "📈 美元        → 升值",
            "📉 美债        → 价格跌、收益率↑",
            "📉 股票        → 普遍承压,成长股/科技股跌得更多",
            "📉 黄金、加密币 → 以美元计价,跌",
            "📉 新兴市场    → 资金回流美国,跌",
        ]
    elif direction == "dovish":
        verdict = f"美联储要降息（鸽派）"
        verdict_explain = (
            f"市场认为未来 2 年美联储会累计降息约 {bp_str} 个点"
            f"（标准动作 25 个点一次 → 大约 {step_str} 次）"
        )
        impact_lines = [
            "📉 美元        → 贬值",
            "📈 美债        → 价格涨、收益率↓",
            "📈 股票        → 普遍上涨,成长股/科技股涨得更多",
            "📈 黄金、加密币 → 以美元计价,涨",
            "📈 新兴市场    → 资金流入,涨",
        ]
    else:
        verdict = "美联储按兵不动（中性）"
        verdict_explain = "市场认为未来 2 年美联储利率基本不动（±0.25% 内）"
        impact_lines = [
            "— 美元/美债/股票/黄金/加密  →  宏观层面波动小",
            "→ 个股/行业看自身逻辑(财报、地缘、油价)",
            "→ 不要因为这条信号就急着调仓位",
        ]

    # ─── 标题(短,推送栏可见)───
    # 旧版:"[Fed 异动] 🔴高 鹰派(加息) ｜ 2Y +30bp ｜ 预期 +114bp"
    # 新版:"[美联储要加息]🔴高 加息约4-5次,美元涨股票跌"
    if direction == "hawkish":
        title_core = f"美联储要加息 约{step_str}次"
        title_impact = "美元↑ 股票↓ 黄金↓"
    elif direction == "dovish":
        title_core = f"美联储要降息 约{step_str}次"
        title_impact = "美元↓ 股票↑ 黄金↑"
    else:
        title_core = "美联储按兵不动"
        title_impact = "宏观无方向 看个股"

    if title_only:
        title = f"[{verdict}] {severity_cn} {title_core},{title_impact}"
        return title[:60]

    # ─── 正文(完整)───
    yield_arrow = "↑" if yield_bp > 0 else ("↓" if yield_bp < 0 else "→")

    # v284: 「判断依据」—— 用户问"为什么有这个判断 / 是因为发生什么事了?"
    evidence_section = _format_evidence_section(anomaly, data_source)

    lines = [
        f"🚨 {verdict}",
        "",
        "【一句话结论】",
        verdict_explain,
        "",
        "【对市场的影响】",
        *impact_lines,
        "",
        "【当前数据】",
        f"  美联储基准利率：{effr:.2f}%",
        f"  美国 2 年期国债收益率：{dgs2:.2f}% （今天 {yield_arrow} {abs(yield_bp):.0f} 个点）",
        f"  2Y 与基准利差：{abs(expect_bp):.0f} 个点 → 市场对未来 2 年累计加息/降息的定价",
        f"  严重程度：{severity_cn}",
        "",
        evidence_section,  # v284: 判断依据板块
        "",
        "【白话解释】",
        "  2 年期国债收益率（市场每天交易的价格）减去美联储当前的基准利率，",
        "  就是市场对未来 2 年美联储「会加息几次 / 降息几次」的整体定价。",
        "  利差越大，市场预期美联储动作越大。",
        "",
        "【进一步操作】",
        "  · 看 /quant 页面 → 决策区有「美联储影响」面板,各品种实时方向",
        "  · 不理解具体品种怎么办?先看这条标题 + 影响那两段,够了",
    ]
    return "\n".join(lines)


# ─── v284: 判断依据板块 helper ──────────────────────────────────────────

_DATA_SOURCE_LABELS = {
    "fred": "美联储官方 FRED 数据（DGS2 美债 + DFF 联邦基金利率,最权威）",
    "fred_dgs2_yfinance_irx_proxy": (
        "部分 FRED + yfinance 代理（DGS2 是美联储官方,EFFR 用 ^IRX 13 周短期国债估算,误差约 5-25 个点）"
    ),
    "fred_dff_yfinance_5y_proxy": (
        "部分 FRED + yfinance 代理（EFFR 是美联储官方,2Y 用 ^FVX 5 年期国债估算,误差约 30-50 个点）"
    ),
    "yfinance_5y_irx_proxy": (
        "全 yfinance 代理（2Y 用 ^FVX 5 年期 + EFFR 用 ^IRX 13 周,误差 ±30 个点,精度低于 FRED）"
    ),
    "yfinance_5y_irx_proxy_fastinfo": "全 yfinance fast_info 代理（与上方类似,精度低于 FRED）",
    "unavailable": "❌ 所有数据源不可用,本次判断基于上一份缓存,可能不准",
    None: "数据源未标注",
}


def _format_data_source_label(data_source: Optional[str]) -> str:
    """把 snap.data_source 的机器编码翻译成人话。"""
    return _DATA_SOURCE_LABELS.get(
        data_source,
        f"{data_source}（未知数据源,人工核对）",
    )


def _format_evidence_section(anomaly: Dict[str, Any],
                             data_source: Optional[str]) -> str:
    """v284: 生成「判断依据」正文块 — 告诉用户:
    ① 这个判断是看什么数据算出来的;
    ② 为什么触发(利率端动了?预期端动了?双触发?各自超阈值多少);
    ③ 是不是因为某个具体新闻事件触发的(查 raw_news 最近 6h,没有则诚实标注)。

    设计原则(v282 honesty 宪法):
    · 不假设新闻 → 没找到就明说"未关联具体新闻事件"
    · 数据源透明 → 让用户知道是 FRED 官方还是代理估算
    · 触发原因细分 → 不只是"市场预期变了",而是"24m 累计加息 114bp 突破阈值 25bp"
    """
    lines = ["【判断依据】"]

    # 1) 数据源
    lines.append(f"  · 数据源:{_format_data_source_label(data_source)}")

    # 2) 触发原因(从 anomaly.trigger_reason 读,v284 新增)
    trigger_reason = (anomaly.get("trigger_reason") or "").strip()
    if not trigger_reason:
        # 兼容旧调用方没有 trigger_reason 字段
        atype = anomaly.get("type", "none")
        if atype == "none":
            trigger_reason = "无显著异动"
        else:
            trigger_reason = "（v284 前历史数据,缺触发原因详情）"
    lines.append(f"  · 触发原因:{trigger_reason}")

    # 3) VIX 自适应阈值(若有)
    regime = anomaly.get("threshold_regime", "static")
    vix = anomaly.get("threshold_vix")
    if regime and regime != "static" and vix:
        lines.append(
            f"  · 阈值模式:市场波动率 VIX={vix:.1f},系统自动调成「{regime}」档"
            f"（高波动时降噪,低波动时更敏感）"
        )

    # 4) 关联新闻(v284 新增)—— 没有则诚实标注
    news_refs = anomaly.get("news_refs") or []
    if news_refs:
        lines.append(f"  · 关联新闻:近 6 小时内采集到 {len(news_refs)} 条 Fed 相关报道(按重要度排序):")
        for i, ref in enumerate(news_refs[:3], 1):
            ts_str = time.strftime("%H:%M", time.localtime(ref.get("ts", 0)))
            impact = ref.get("impact_1to100", 0)
            impact_tag = f"[重要度 {impact}]" if impact else ""
            title = ref.get("title", "（无标题）")
            source = ref.get("source", "unknown")
            lines.append(
                f"    {i}. {ts_str} · {source} {impact_tag}\n"
                f"       {title}"
            )
        lines.append(
            "    → 异动可能由上述新闻触发,但也可能是利率自身波动 — 仅供参考"
        )
    else:
        lines.append(
            "  · 关联新闻:❓ 近 6 小时内未采集到 Fed 相关报道,"
            "本次异动可能源于市场情绪 / 算法交易 / 流动性事件 —"
            "不是每条信号都能找到对应新闻事件,本判断仅基于利率数据自身变动"
        )

    return "\n".join(lines)


def format_reversal_alert_message(prob: float, days_to: Optional[int],
                                 direction: str, asset: str,
                                 *, title_only: bool = False,
                                 extra: dict = None) -> str:
    """v226+ 周期反转信号格式化（来自 cycle_analyst.predict_reversal）。

    Parameters
    ----------
    prob : float 0~1
        反转概率（predict_reversal['prob']）。
    days_to : int | None
        预计距离反转的天数（predict_reversal['days_to']）。
    direction : str
        'up' / 'down' / 'neutral' — 预期反转方向。
    asset : str
        标的（BTC/BTC-USD/SOL 等）。
    title_only : bool
        True → 返回短标题（≤ 50 字）。
    extra : dict
        可选附加信息，如 {"phase": "distribution", "votes": 2,
                          "hurst": 0.65, "rsi": 78.5}。

    Returns
    -------
    str
    """
    prob = max(0.0, min(1.0, float(prob or 0)))
    direction_cn = {"up": "反弹", "down": "下挫", "neutral": "震荡"}.get(direction, direction)
    arrow = "↑" if direction == "up" else ("↓" if direction == "down" else "→")
    prob_pct = int(prob * 100)

    if title_only:
        days_part = f"{days_to}天" if days_to is not None and days_to >= 0 else "近期"
        return f"[周期反转] {asset} {direction_cn} {prob_pct}% ｜ {days_part}"[:60]

    lines = [
        f"🔀 周期反转信号 · {asset}",
        f"方向：{arrow} {direction_cn}",
        f"概率：{prob_pct}% （3 票中 ≥2 票）",
    ]
    if days_to is not None and days_to >= 0:
        lines.append(f"预计天数：{days_to} 天")
    else:
        lines.append("预计天数：未知（K 线 < 30 根）")

    if extra:
        if extra.get("phase"):
            lines.append(f"当前阶段：{extra['phase']}")
        if extra.get("votes") is not None:
            lines.append(f"投票数：{extra['votes']}/3")
        if extra.get("hurst") is not None:
            lines.append(f"Hurst 指数：{float(extra['hurst']):.2f}")
        if extra.get("rsi") is not None:
            lines.append(f"RSI：{float(extra['rsi']):.1f}")

    lines.extend([
        "",
        "⚠️ 提示：反转信号仅为概率判断，需结合宏观 / 资金面综合决策。",
        "💡 建议：查看 /api/discovery/cycle-scan 横向对比多周期",
    ])
    return "\n".join(lines)


def send_signal_alert(asset: str, action: str, impact: float, heat: float,
                      reason: str, news_time: str = "") -> None:
    """v186 新增：兜底推送——任何 BUY/SELL 落地信号都推一条简短提醒。

    与 send_important_news 区别：
    - send_important_news 走"高重要新闻"门槛（v186 已放宽到 impact/heat >= 5.0），推完整卡片；
    - send_signal_alert 不论分数，只看 action 是 BUY/SELL → 一条精简通知，确保用户
      永远能收到"信号出现"事件，避免高门槛过滤掉保守给分信号。
    - 仅推 Server酱（微信）+ PushPlus 两通道；不走 SMTP 邮件（避免短促信号刷邮箱）。
    """
    title = build_signal_title(asset, action, news_time)
    emoji = "📈" if action.upper() == "BUY" else "📉"
    cn = _ACTION_CN.get(action.upper(), "观望")
    short_body = (
        f"{emoji} {asset} {cn}\n"
        f"影响分：{impact:.0f}/10\n"
        f"热度：{heat:.0f}/10\n"
        f"理由：{reason[:120]}"
    )
    _fire_and_forget(_send_serverchan_sync, title, short_body)
    _fire_and_forget(_send_pushplus_sync, title, short_body)


async def send_feishu_alert(news_text: str, action: str, score: float, reason: str,
                           news_time: str = "") -> None:
    """
    Push a trading signal card to Feishu bot.

    Color logic: BUY/LONG → green, SELL/SHORT → red, else grey.
    Uses stdlib urllib in an executor thread — no aiohttp needed.
    Failures are silently swallowed (fire-and-forget).
    news_time: ISO timestamp of the news event, displayed prominently on the card.
    """
    loop = asyncio.get_running_loop()

    FEISHU_WEBHOOK = config.FEISHU_WEBHOOK_URL
    if not FEISHU_WEBHOOK:
        global _feishu_warned
        if not _feishu_warned:
            _feishu_warned = True
            print("[FEISHU] FEISHU_WEBHOOK_URL 未配置, 飞书告警已跳过")
        return

    # ── v171+：clock_jump 检测（运行时时钟跳变 → 告警 header 加红 banner）──
    clock_jump_active = False
    clock_jump_note = ""
    try:
        import clock as _clock_mod
        diag = _clock_mod.diagnostics() or {}
        if diag.get("jump_warned"):
            clock_jump_active = True
            age = diag.get("last_safe_age_seconds")
            age_str = f"{age:.1f}s" if isinstance(age, (int, float)) else "?"
            clock_jump_note = (
                f"⚠️ **时钟跳变检测**: 上次 safe_age={age_str}（可能 NTP 同步 / 系统休眠）"
            )
    except Exception:
        pass

    action_upper = action.upper()
    if "BUY" in action_upper or "LONG" in action_upper:
        color = "green"
    elif "SELL" in action_upper or "SHORT" in action_upper:
        color = "red"
    else:
        color = "grey"

    # ── 格式化新闻时间戳（v171 双时区 + UTC + ISO 三轨展示）──
    dual = tz_utils.format_dual(news_time) if news_time else None
    time_field_short = ""
    time_field_full = ""
    time_line = ""
    if dual and (dual.get("iso") or ""):
        # 主显示（display_tz，对中国用户为北京时间）
        time_field_short = dual.get("display") or ""
        # 全时区对位（UTC / 北京 / 美东 / 美西）
        time_field_full = (
            f"UTC {dual.get('utc','')} · 北京 {dual.get('cn','')} · "
            f"美东 {dual.get('us_eastern','')} · 美西 {dual.get('us_pacific','')}"
        )
        time_line = f"\n🕐 **新闻时间** ({dual.get('display_tz','')}): {time_field_short}"
    elif news_time:
        # fallback: 解析失败的原文截断
        time_field_short = str(news_time)[:19]
        time_line = f"\n🕐 **新闻时间**: {time_field_short}"

    # ── 飞书原生 fields 数组（v171 结构化升级） ──
    fields = [
        {"is_short": True, "text": {"tag": "lark_md", "content": f"**方向**: {action}"}},
        {"is_short": True, "text": {"tag": "lark_md", "content": f"**得分**: {score}"}},
        {"is_short": False, "text": {"tag": "lark_md", "content": f"**AI 逻辑**: {reason}"}},
    ]
    if time_field_full:
        fields.insert(0, {
            "is_short": False,
            "text": {"tag": "lark_md",
                     "content": f"🕐 **新闻时间** ({dual.get('display_tz','')}): {time_field_short}\n"
                                f"⏰ **全时区**: {time_field_full}"},
        })

    payload = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": "[Trident 交易信号]" + (" ⚠️时钟跳变" if clock_jump_active else ""),
                },
                "template": "red" if clock_jump_active else color,
            },
            "elements": [
                {
                    "tag": "markdown",
                    "content": f"**新闻原文**: {news_text}{time_line}",
                },
            ] + ([{"tag": "markdown", "content": clock_jump_note}] if clock_jump_note else []) + [
                {"tag": "hr"},
                {"tag": "div", "fields": fields},
            ],
        },
    }

    def _post_sync() -> None:
        try:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(
                FEISHU_WEBHOOK,
                data=data,
                headers={"Content-Type": "application/json; charset=utf-8"},
            )
            resp = urllib.request.urlopen(req, timeout=10)
            body = resp.read().decode("utf-8", errors="replace")
            print(f"[{_now()}] [FEISHU] HTTP {resp.status} — {body[:200]}")
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            print(f"[{_now()}] [FEISHU] HTTP {e.code} — {body[:200]}")
        except Exception as e:
            print(f"[{_now()}] [FEISHU] ERROR: {type(e).__name__}: {e}")

    await loop.run_in_executor(None, _post_sync)
