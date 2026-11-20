"""Task B — Concurrent Batch AI Worker
+ requests-based OpenAI-compatible LLM client + model roster + performance feedback.
"""

from __future__ import annotations

import asyncio
import json

# ── SERVER VERDICT 用的快照 cache（模块级，让嵌套 _do_api_call 能读到） ──
# 每次 _ai_worker_loop 成功拉快照后写入；snapshot 失败时保留上一次成功的快照。
# _do_api_call 注入 SERVER VERDICT 块时读这里，避免重复请求行情源。
_SERVER_SNAPSHOT_CACHE: dict = {"ts": 0.0, "summary": "", "json": "{}"}
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import db # v189-8: 用于 db.retry_on_busy 包 _batch_claim 的 UPDATE RETURNING
import requests
import causal_graph # v200 P0+ 严谨兜底: L2 业务知识兜底用通用驱动结构
import factor_ontology  # v423 注入埋点(只观测, 不影响决策)
import news_cutoff  # v444: 断档恢复后不补历史新闻（用户 2026-11-18 定策）
from common import time_utils  # v317: 时间口径唯一入口（北京时间裸串解读），见 time_utils.py 文件头
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from config import (
    AI_MODEL_ROSTER,
    DEFAULT_AI_MODEL_ID,
    AIPING_API_KEY,
    AIPING_BASE_URL,
    AIPING_EXTRA_BODY,
    get_selected_ai_model_id,
    VIP_SCORE_BOOST,
    BATCH_SIZE,
    HERMES_AGENT_ENABLED,
    MACRO_CONTEXT_ENABLED,
    _AGG_WINDOW_HOURS,
    _AGG_MIN_SCORE,
    MECHANISM_GATE_ENABLED,
    MIN_ASSET_IMPACT_FOR_TRADE,
)
import config

# 2026-11-18: 纯字面量常量搬到 worker_config.py，用 * 接回（保 _SYSTEM_PROMPT 等老引用）
from engine.worker_config import *
# 2026-11-18 第二批：自闭环函数（校验/补字段/降级文案/模型池）搬到 worker_helpers
from engine.worker_helpers import *

# ═══════════════════════════════════════════════════════════════════════════
# v200 P0+ 严谨兜底: 字段源头追溯 + completeness_score 计算
# 设计动机: 之前 LLM 漏填字段时 ai_worker 写 "服务端兜底：LLM 未填 X" 占位字符串,
#   反思系统跑在脏数据上 → review_insights → prompt 注入 → LLM 学坏.
# 修复: 每个字段打 source 标记(5 类), 算 completeness_score.
#   反思系统按 completeness_score 过滤脏决策不入深度分析.
#
# 5 类允许的兜底来源(按严谨度排序):
#   L1 数学公式反推    (rule_xxx)
#   L2 业务知识硬编码   (business_knowledge_v176_2)
#   L3 历史模式查询     (historical_pattern)
#   L4 多 LLM 投票     (llm_ensemble_consensus)
#   L5 留空 + 强标记    (missing)
# 3 类绝对禁止:
#   X1 任意字符串占位   ("服务端兜底：LLM 未填 X")
#   X2 fake reasoning 模板 (八段模板拼凑)
#   X3 跨层借用       (脏数据污染反思)
# ═══════════════════════════════════════════════════════════════════════════


# v200+ P2: invalidation_condition LLM 兜底（X1 假字符串禁止 但不能留空）
# 当 LLM 留空/<4 字符时，按 event_type_code 分类给一个真实事件级失效条件，
# 既不让字段为空（确保 ai_decisions.invalidation_condition 始终有值），
# 又保留 mark_field_missing 让 L5 完整性扣分（LLM 实际没填，扣分不当）。
# 模板含义必须能让用户读懂"什么情况下这条建议失效"，不是占位字符串。
_INVAL_COND_TEMPLATES: Dict[str, str] = {
    # 利率/央行政策类
    "rate_cut": "市场对降息幅度定价反向或美联储措辞转鹰",
    "rate_hike": "通胀粘性回落或美联储暂停加息周期",
    "rate_decision": "利率路径与 OIS 隐含路径偏离 >25bp",
    "central_bank_speech": "央行措辞与此前指引明显背离",
    # 数据发布
    "cpi": "CPI 核心环比偏离预期 >0.3pp",
    "nfp": "非农新增就业偏离预期 >2 倍标准差",
    "ppi": "PPI 同比偏离预期 >0.5pp",
    "pmi": "PMI 跌破枯荣线或回升超 3 个点",
    "data_release": "数据值偏离市场共识 >2σ",
    "earnings": "业绩 EPS 与一致预期偏离 >15%",
    "guidance_update": "公司业绩指引反向调整 >10%",
    # 地缘/供给
    "geopolitical_conflict": "地缘事件缓和或避险溢价快速回落",
    "geopolitical_escalation": "停火协议落地或冲突降级",
    "opec_decision": "OPEC 转向增产或豁免国违约",
    "supply_disruption": "供给中断解除或库存回补",
    # 政策/法规
    "policy_decision": "政策细则与本周预期相反或推迟实施",
    "regulatory": "监管细则放松或豁免范围扩大",
    "trade_tariff": "关税豁免或税率低于此前市场定价",
    # 技术/资金
    "market_event": "价格反向突破 1.5×ATR 或关键支撑位失守",
    "funding_shift": "资金费率反向或 OI 骤降 >30%",
}


def _fallback_invalidation_condition(event_type_code: str, asset: str = "",
                                     action: str = "") -> str:
    """v200+ P2: invalidation_condition LLM 兜底。

    入参：event_type_code (如 'rate_cut'/'geopolitical_conflict'/...)、target_asset、suggested_action。
    出参：≤40 字真实失效条件（不是占位字符串）。

    规则：
      1. event_type_code 在 _INVAL_COND_TEMPLATES → 用对应模板
      2. 否则按 event_type_code 前缀/包含匹配（如 'rate_*'、'*geopol*'）
      3. 还不行 → 按 asset + action 给通用兜底
      4. HOLD 决策统一给"异动级别回落 <0.5%"
    """
    code = (event_type_code or "").strip().lower()
    asset_u = (asset or "").strip().upper()
    act_u = (action or "").strip().upper()

    # 1. 完全匹配
    if code in _INVAL_COND_TEMPLATES:
        return _INVAL_COND_TEMPLATES[code]

    # 2. 前缀/包含匹配
    if code.startswith("rate_") or "rate" in code:
        return _INVAL_COND_TEMPLATES["rate_decision"]
    if code.startswith("cpi") or "cpi" in code:
        return _INVAL_COND_TEMPLATES["cpi"]
    if code.startswith("nfp") or "nonfarm" in code:
        return _INVAL_COND_TEMPLATES["nfp"]
    if code.startswith("ppi") or "ppi" in code:
        return _INVAL_COND_TEMPLATES["ppi"]
    if code.startswith("pmi") or "pmi" in code:
        return _INVAL_COND_TEMPLATES["pmi"]
    if "geopol" in code or "conflict" in code or "war" in code:
        return _INVAL_COND_TEMPLATES["geopolitical_conflict"]
    if "opec" in code or "supply" in code:
        return _INVAL_COND_TEMPLATES["supply_disruption"]
    if "policy" in code or "regulat" in code or "tariff" in code:
        return _INVAL_COND_TEMPLATES["policy_decision"]
    if "earn" in code or "guidance" in code:
        return _INVAL_COND_TEMPLATES["earnings"]
    if "funding" in code or "liquidity" in code or "oi" in code:
        return _INVAL_COND_TEMPLATES["funding_shift"]

    # 3. HOLD 决策通用兜底
    if act_u == "HOLD":
        return "异动级别回落（<0.5%）且无新增催化"

    # 4. 通用兜底
    if asset_u:
        return f"{asset_u} 价格反向 1.5×ATR 或关键支撑/阻力位失守"
    return "价格反向突破 1.5×ATR 或关键支撑/阻力位失守"


# _fallback_business_knowledge_drivers 已外迁 engine.worker_helpers（import * 接回）


# v187: 公共 chat() 函数 — 简单 prompt in / string out 接口
# 走 purpose-routed 模型池(参见 config.get_pool_for_purpose)
# 自动 token 埋点(写到 ai_call_stats 表)
# 失败返回 ""(永不抛),便于反思/顾问等低频路径直接调
def chat(prompt: str, *,
         purpose: str = "main",
         system: str = "",
         max_tokens: int = 1500,
         temperature: float = 0.3,
         call_site: str = "") -> str:
    """v187: 简单 chat 接口. 自动选 purpose 池最高权重模型;失败降级本地;埋 token.

    Args:
        prompt: 用户内容
        purpose: main/gate/anomaly/reflection/advisor/sql/optimizer
        system: 可选 system 消息
        max_tokens: 输出上限 (覆盖 extra_body 默认)
        temperature: 0.0-1.0
        call_site: 调用方标记(写到 ai_call_stats),如 'blindspot_reflector'
    Returns:
        message.content 字符串;全失败返回 "".
    """
    import db as _db
    pool = config.get_pool_for_purpose(purpose)
    if not pool:
        return ""
    last_err = ""
    for m in pool:
        cfg = {
            "id": m["id"],
            "request_model": m.get("request_model") or m["id"],
            "label": m.get("label", m["id"]),
            "api_base": m["api_base"],
            "api_key": m["api_key"],
            "extra_body": dict(m.get("extra_body") or {}),
            "use_proxy": bool(m.get("use_proxy", False)),
            "json_mode": False,
            "is_local": bool(m.get("is_local", False)),
            "timeout": float(m.get("timeout") or 60.0),
            "key_alias": m.get("key_alias", "unknown"),
        }
        # ── v413: reflection 通道不再兜底到本地 3B ─────────────────────
        # 实测（2026-11-18 13:44 前后）：minimax-reflect 配额熔断 30 分钟后，
        # 兜底到 local/qwen2.5:3b-reflect，**5 次全部 ReadTimeout，每次硬等 90s**。
        # 3B 本地模型也不可能产出可用的结构化归因（它是这一轮反思为空的原因之一，
        # 但更根本的是没有已结算样本）。所以：宁可明确失败，不要静默烧 90 秒。
        # 需要恢复本地兜底：REFLECTION_ALLOW_LOCAL=1。
        if cfg["is_local"] and purpose == "reflection" \
                and os.environ.get("REFLECTION_ALLOW_LOCAL", "0") != "1":
            last_err = (f"{cfg['label']} 本地兜底对 reflection 关闭"
                        "（3B 模型产不出可用归因；REFLECTION_ALLOW_LOCAL=1 可恢复）")
            continue
        if cfg["is_local"] and purpose == "reflection":
            # 允许本地兜底时也不许 90s：3B 要么 25s 内答，要么永远答不出来
            cfg["timeout"] = min(cfg["timeout"], 25.0)
        # ── v322: 通道熔断（与主分析同一套熔断器）──────────────────────
        # chat() 是 sql / advisor / reflect / anomaly / gate2 等**低频辅助通道**
        # 的统一入口。实测（2026-11-04）这些通道在配额打满时同样疯狂重试 429
        # （llm_call_failures 里 minimax-sql / minimax-advisor / minimax-reflect
        #  全是 429），一起把配额烧光 —— 不能只保护主分析。
        _c_alias = str(cfg.get("key_alias") or cfg.get("label") or "unknown")
        _c_brk_q = _analysis_breaker(_c_alias, "quota")
        _c_brk_n = _analysis_breaker(_c_alias, "net")
        if _c_brk_q is not None and _c_brk_n is not None:
            if not (_c_brk_q.allow() and _c_brk_n.allow()):
                last_err = f"{cfg['label']} 通道熔断中"
                continue
        # 把 max_tokens 覆盖到 extra_body
        cfg["extra_body"]["max_tokens"] = max_tokens
        try:
            t0 = time.perf_counter()
            session = _LLM_PROXY_SESSION if cfg["use_proxy"] else _LLM_SESSION
            headers = {
                "Authorization": f"Bearer {cfg['api_key']}",
                "Content-Type": "application/json",
            }
            messages: List[Dict[str, Any]] = []
            if system:
                messages.append({"role": "system", "content": system})
            messages.append({"role": "user", "content": prompt})
            payload: Dict[str, Any] = {
                "model": cfg["request_model"],
                "messages": messages,
                "temperature": temperature,
                **cfg["extra_body"],
                "max_tokens": max_tokens,
            }
            # ── v322: 速率闸门（每次真实请求前扣名额）──────────────────
            try:
                from common import llm_budget as _lb_rpm
                _ok_rpm, _why_rpm = _lb_rpm.consume_rate_slot(purpose="chat")
                if not _ok_rpm:
                    last_err = _why_rpm
                    break
            except Exception:
                pass  # 闸门自身异常 → 放行
            resp = session.post(
                f"{cfg['api_base']}/chat/completions",
                json=payload, headers=headers, timeout=cfg["timeout"],
            )
            dt_ms = int((time.perf_counter() - t0) * 1000)
            if resp.status_code >= 400:
                last_err = f"HTTP {resp.status_code}: {resp.text[:150]}"
                # v322: 配额/鉴权类记配额熔断，其余记网络熔断
                if resp.status_code in (429, 401, 402, 403):
                    if _c_brk_q is not None:
                        _c_brk_q.failure()
                elif _c_brk_n is not None:
                    _c_brk_n.failure()
                # 埋失败日志
                _db.log_ai_call(
                    purpose=purpose, key_alias=cfg["key_alias"],
                    model_id=cfg["id"], tokens_in=0, tokens_out=0,
                    latency_ms=dt_ms, status="http_error",
                    error_code=str(resp.status_code), call_site=call_site,
                    error_body=(resp.text or "")[:300],
                )
                continue
            body = resp.json()
            text = (body.get("choices", [{}])[0].get("message", {}).get("content", "") or "").strip()
            usage = body.get("usage", {}) or {}
            tok_in = int(usage.get("prompt_tokens") or 0)
            tok_out = int(usage.get("completion_tokens") or 0)
            tok_cached = int((usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
            # v322: 成功 → 熔断器清零闭合
            if _c_brk_q is not None:
                _c_brk_q.success()
            if _c_brk_n is not None:
                _c_brk_n.success()
            _db.log_ai_call(
                purpose=purpose, key_alias=cfg["key_alias"],
                model_id=cfg["id"], tokens_in=tok_in, tokens_out=tok_out,
                cached_tokens=tok_cached, latency_ms=dt_ms, status="ok",
                error_code="", call_site=call_site,
            )
            return text
        except Exception as exc:
            last_err = f"{type(exc).__name__}: {str(exc)[:120]}"
            # v322: 网络异常 → 记网络类熔断
            if _c_brk_n is not None:
                _c_brk_n.failure()
            try:
                _db.log_ai_call(
                    purpose=purpose, key_alias=cfg.get("key_alias", "unknown"),
                    model_id=cfg.get("id", "?"), tokens_in=0, tokens_out=0,
                    latency_ms=int((time.perf_counter() - t0) * 1000),
                    status="network_error" if not isinstance(exc, TimeoutError) else "timeout",
                    error_code=type(exc).__name__, call_site=call_site,
                    error_body=str(exc)[:300],
                )
            except Exception:
                pass
            continue
    print(f"[chat:{purpose}] all {len(pool)} models failed; last_err={last_err}", flush=True)
    return ""


# 市场快照 — 每轮 AI batch 前拉取一次 BTC/XAU 行情
from market_snapshot import get_snapshot
import strategy_store
import paper_trading
import decision_guard


import fundamental_factor # v405: 公司级基本面方向因子（估值分位+盈利修正）
from review import gate_drop_policy # FIX-8: 门卫"看不清"≠"没信号"，状态判定抽成纯函数（可单测）
import evidence
import hermes_agent
import macro_context
import timeseries
from feeds import news_sources

from .alerts import build_signal_title, format_signal_body, send_feishu_alert, send_important_news, send_signal_alert, send_strong_buy_alert
from .prices import _get_current_price
from .utils import _detect_vip, _now, _open_db, _ts

# ── 市场分类单一真理源 ──
# 不再 inline 写 (CRYPTO,GOLD,OIL,STOCK,MACRO,COMMODITY) 这种白名单。
# 所有 market_category 推断/校验都走这里。失败抛 UnclassifiedError，
# 调用方负责写 unclassified_events 表（人工/规则补全）。
from market.market_classifier import (
    MARKET_CATEGORIES_PROD,
    TARGET_TO_MARKET,
    UnclassifiedError,
    infer_market_category,
    is_debug_other_allowed,
    map_target,
    row_is_unclassified,
    validate_pair,
)

# HOLD 弱分阈值：|score| 不超过此值时视为中性观望，不伪造符号。

# v176.7: 单条新闻分析因超时/DB 锁退回 PENDING 的最大次数；超限标 FAILED，打断重复分析死循环。

# v322: 落库失败后的**再认领冷却**（秒）。
# 为什么必须是 15 分钟而不是原来的 120 秒：
#   实测单批 wall=390 秒（16 条并发 + 行情源慢），而冷却只有 120 秒
#   → 冷却一过，同一批立刻又被认领 → 再跑 390 秒 → 再失败
#   → **无限循环**，队头永远被同一批占住 → 新新闻永远排不上
#   → 「待分析」只增不减（这正是 2026-11-05 观测到的形态：
#     `claimed 16 rows (ids=[11974, 11975, ...])` 反复出现同一批 id）。
# 冷却必须 **> 单批最坏耗时**，否则等于没冷却。
# 取 900 秒（15 分钟）：与看门狗的僵尸阈值同量级；
# 冷却期内同批不会被再认领 → 新一批能顶上来 → 队头能轮转。
# v345.13: 批级通道轮转计数 —— 一批 16 条锁同一个 key 以命中 prompt 缓存，
# 换批才换通道（保留配额分散）。轮转单位从"每条"改成"每批"。
# v398g: 逐批轮转实测命中不了前缀缓存。2026-11-16 21:20-21:34 生产日志：
# sysfp 完全一致（b59ca85178）但 cached_tokens 恒为 128 —— 因为相邻批次
# 换了 key（minimax1 → minimax → minimax2），而缓存是按 key 记的，
# 换 key 等于缓存从零开始。改成**按时间窗口粘住同一个 key**：
# 窗口内所有批次用同一通道，第 2 次调用起命中 system 前缀（探针实测
# 6,644/6,645），窗口到了再换下一个通道，配额分散的初衷仍保留。
_MAIN_STICKY_WINDOW_S = float(os.getenv("TRIDENT_MAIN_KEY_STICKY_S", "0"))  # 0=每批轮转


# v322: baseline 在线刷新的进程内节流时间戳。
# 场景：backtest_event_outcomes 表在事务里被 refresh（失败即回滚）→ 表长期为空 →
# MAX(last_updated) 恒为 None → 「基线是否过期」的判定会每条新闻都重跑一次 refresh。
# 现在：进程内 1 小时只真正 refresh 一次。
# ⚠️ 实测更正（2026-11-05）：这条**不是**「待分析」不降的主因 ——
#    signal_performance / backtest_event_outcomes 两张表都是空的，那次 90 天 5 表 JOIN
#    聚合实测只花 0.001 秒。真正瓶颈是行情源连接超时（Yahoo ~89 秒/次 × 16 条
#    ≈ 42 分钟/批）叠加看门狗回收在办批次。节流保留为防御性措施，
#    但注释/结论不要再归因于耗时。

# v322: 本进程「正在处理」的批次登记表 —— news_id → 最近一次心跳的单调时钟。
# 事故（2026-11-05 实测，根因）：
#   1) 行情源(OKX/Yahoo)全部连接超时 → 每条新闻落库多耗 1-2 分钟 → 单批 16 条跑 ~42 分钟；
#   2) 看门狗按「认领后 15 分钟未完成即僵尸」回收 → 把**还在处理中**的 PROCESSING 行
#      改回 PENDING；
#   3) 批事务跑完时 `UPDATE ... WHERE status='PROCESSING'` 匹配 0 行 → 一条都进不了 DONE；
#   4) 下轮又认领同一批（id 最小 → 队头）→ 无限循环 → 「待分析」永不下降。
# 为什么不用「心跳写 processing_at」：批事务全程持 SQLite 写锁，其它连接写不进去，
# 心跳永远提交不了（而且会把锁冲突放大）。同进程内存表没有这个问题。
#
# 租约语义（v322 补强）：光有集合不够 —— 批处理中途抛异常时「注销」不会执行，
# 那批 PROCESSING 行会被永久排除在回收之外 → 永远卡在 PROCESSING。所以：
#   · 登记时起一个 daemon 心跳线程，每 30 秒刷新一次租约；
#   · 心跳线程发现「本批被新批次取代 / 批次主线程已死 / 超过 1 小时硬上限」，
#     就**主动注销本批**（把 id 从保护名单摘掉）→ 残留的 PROCESSING 立刻交回看门狗回收；
#   · 批次正常结束显式注销 → 心跳线程退出。
# 另有租约兜底：超过 _ACTIVE_BATCH_LEASE_S 没有任何心跳进展 → 不再受保护。
# 进程崩溃时内存表随进程消失 → 真正的僵尸仍会被看门狗回收。
_ACTIVE_BATCH_IDS: Dict[int, float] = {}
_ACTIVE_BATCH_LOCK = threading.Lock()
# 租约时长：超过这个时长没有任何进展 → 视为卡死 → 允许看门狗回收。
# 取 30 分钟：看门狗僵尸阈值 15 分钟，实测单批最长 ~42 分钟（行情源全超时）；
# 修好行情源快速失败后单批应回到分钟级 → 30 分钟既宽松又不会让僵尸永久滞留。
# 硬上限：登记后超过这个时长仍未注销（无论什么原因）→ 心跳线程主动放弃保护，
# 让看门狗把残留的 PROCESSING 当僵尸回收。1 小时远大于正常单批耗时（分钟级），
# 也大于实测最坏 42 分钟 —— 属于兜底，正常路径永远碰不到。
# v322: 在办批次代次 —— 每次登记 +1。心跳线程发现自己被新批次取代就注销本批。


def _active_batch_register(ids: List[int]) -> threading.Event:
    """登记在办批次并起心跳线程。返回停止事件（正常结束时传给 _active_batch_release）。"""
    global _ACTIVE_BATCH_GEN
    now = time.monotonic()
    deadline = now + _ACTIVE_BATCH_MAX_S
    ids = [int(i) for i in ids]
    with _ACTIVE_BATCH_LOCK:
        _ACTIVE_BATCH_GEN += 1
        gen = _ACTIVE_BATCH_GEN
        for _i in ids:
            _ACTIVE_BATCH_IDS[_i] = now
    stop = threading.Event()
    owner = threading.current_thread()

    def _beat():
        while not stop.wait(_ACTIVE_BATCH_HEARTBEAT_S):
            with _ACTIVE_BATCH_LOCK:
                if (gen != _ACTIVE_BATCH_GEN or not owner.is_alive()
                        or time.monotonic() > deadline):
                    # 三种异常收尾，处置方式相同 —— 这批行还挂在 PROCESSING 上 = 僵尸，
                    # 交回看门狗回收：
                    #   ① 被新批次取代（上一批没走正常注销，例如 continue 跳出）
                    #   ② 批次主线程已死（异常逃出批处理）
                    #   ③ 超过硬上限
                    for _i in ids:
                        _ACTIVE_BATCH_IDS.pop(_i, None)
                    break
                _t = time.monotonic()
                for _i in ids:
                    if _i in _ACTIVE_BATCH_IDS:
                        _ACTIVE_BATCH_IDS[_i] = _t

    _th = threading.Thread(target=_beat, daemon=True, name="ai-active-batch-hb")
    _th.start()
    return stop


def _active_batch_release(ids: List[int], stop: Optional[threading.Event] = None) -> None:
    """批次结束 → 注销在办批次 + 停心跳。"""
    if stop is not None:
        stop.set()
    with _ACTIVE_BATCH_LOCK:
        for _i in ids:
            _ACTIVE_BATCH_IDS.pop(int(_i), None)


def _active_batch_live_ids() -> List[int]:
    """租约未过期的在办批次 id（过期的算僵尸，交给看门狗回收）。"""
    _now = time.monotonic()
    with _ACTIVE_BATCH_LOCK:
        return sorted(
            _i for _i, _ts in _ACTIVE_BATCH_IDS.items()
            if _now - _ts < _ACTIVE_BATCH_LEASE_S
        )

# v322: 看门狗回收 SQL —— 提到模块级，便于单测直接锁死两条不变量：
#   ① 必须排除本进程「在办批次」（_ACTIVE_BATCH_IDS），否则在办批次会被当僵尸回收；
#   ② 保留「认领后 15 分钟未完成即僵尸」+ retry_count 递增 + 达阈值标 FAILED。
#
# v322 二次修正：**回收后必须冷却**，不能置 NULL。
#   原来 `processing_at = NULL` → 回收后这行**立刻**可被认领
#   → 引擎重启后第一批又认领同一批 id（实测 ids=[11974,11975,...] 反复出现）
#   → 又跑 390 秒 → 又失败 → 无限循环 → 队头永久霸占 → 新新闻排不上。
#   改成写「冷却到什么时候」，与落库失败回写用同一个冷却时长 →
#   无论是被看门狗回收还是落库失败，同一批都不会立刻被重新认领。


# v321.1: 批量认领 SQL 提到模块级 —— 便于单测直接锁死「认领不重置 retry_count」
# 这条不变量（事故：重置错放在这里 → 每次认领都清零 → retry_count 永远到不了
# 阈值 → 失败行无限重试，主分析整体空转）。
#
# 不变量拆解：
#   · 认领：只改 status / processing_at，**不动 retry_count**
#   · 失败回写（超时/看门狗回收）：retry_count + 1，达阈值才标 FAILED
#   · 重新入队（门卫回放解冻）：由 gate_replay_guard 写 retry_count=0
# v336 P0-4: 认领幂等 —— 已经有决策行的新闻不再进队列。
# 实测：812 个 (news_id, target_asset) 组合被**重复分析**，2096 条决策是同一条
# 新闻同一品种的复制品（权重逐位相同，如 news 11715 的 ETH/XAU/WTI 在 10-23
# 08:15/08:23/08:30 连写三遍，11-04 又写五遍）。占全部决策的 23.6%，
# 把 S11/S12 的分母虚高，也把 S4 的秩相关稀释。
# 逐条追因：决策写进去了，但 raw_news 状态只在 done_news_ids 里置 DONE；
# 一旦该批没进 done 集合，看门狗 15 分钟回收 PROCESSING → 回 PENDING →
# 再分析一遍 → 再写一遍。同一份 LLM 输出被反复计分。
# 需要重放历史新闻做回归时：TRIDENT_ALLOW_REANALYZE=1
_ALLOW_REANALYZE = os.environ.get("TRIDENT_ALLOW_REANALYZE", "0").strip() == "1"
_CLAIM_DEDUP = ("" if _ALLOW_REANALYZE else
                "     AND NOT EXISTS (SELECT 1 FROM ai_decisions d"
                "                     WHERE d.news_id = raw_news.id)")

_BATCH_CLAIM_SQL = (
    "UPDATE raw_news"
    " SET status='PROCESSING',"
    "     processing_at=datetime('now','localtime')"
    " WHERE id IN ("
    "   SELECT id FROM raw_news"
    "   WHERE status='PENDING' AND is_noise=0"
    "     AND COALESCE(gate_unavailable, 0) IN (0, 2)"
    # v322 队头阻塞兜底：processing_at 在 PENDING 状态下表示「冷却到什么时候」。
    # 上一轮落库失败的新闻会被写入 now+120s，这里跳过它们 → 队头不会立刻被
    # 同一批反复霸占（否则 ORDER BY id ASC 让这批一直霸占 → 新新闻永远排队）。
    "     AND (processing_at IS NULL OR processing_at <= datetime('now','localtime'))"
    + _CLAIM_DEDUP +
    "   ORDER BY id ASC LIMIT ?"
    " )"
    " RETURNING *"
)

# v322: 生产用的认领 SQL —— 在上面基础上加**时效门槛**。
# 事故（2026-11-05 实测）：队列积压 3700+ 条，认领是「最老优先」→ 引擎一条条
# 分析 **14 天前**的老新闻，而建仓闸（45 分钟）又把它们的信号全部丢弃
# → 「仍入库统计不交易」：**额度照烧，信号一条也用不上**，
# 而且队列永远追不上（新新闻进来，老新闻排队）。
# 现在：超过 NEWS_ANALYZE_MAX_AGE_HOURS 的新闻**不送去分析**。
#
# ⚠️ 时间口径：用 `ts`（epoch 整数，**无歧义**），**不用** timestamp。
# raw_news.timestamp 有三种形态混存（'T' 分隔北京 / ' ' 分隔 UTC / ' ' 分隔北京），
# time_utils.sql_bj_ts_expr 会把 ' ' 形态当 UTC 再 +8 小时 → 对北京墙钟串多算 8 小时
# → 新鲜度判断整体偏移（实测：6 小时前的新闻被算成「未来」→ 永远不匹配门槛）。
# `ts` 全表都是 integer（实测 8464 行）→ 直接和 epoch 比较，零歧义。
_BATCH_CLAIM_FRESH_SQL = (
    "UPDATE raw_news"
    " SET status='PROCESSING',"
    "     processing_at=datetime('now','localtime')"
    " WHERE id IN ("
    "   SELECT id FROM raw_news"
    "   WHERE status='PENDING' AND is_noise=0"
    "     AND COALESCE(gate_unavailable, 0) IN (0, 2)"
    "     AND (processing_at IS NULL OR processing_at <= datetime('now','localtime'))"
    + _CLAIM_DEDUP +
    "     AND (? <= 0 OR (ts IS NOT NULL AND ts >= ?))"
    # v444（2026-11-18，用户定策）：**断档水位** —— 通道恢复之前入库的新闻一律不认领。
    # 与上面那条 6h 时效闸的区别：6h 是"太旧"，这条是"发生在停摆期间"。
    # 事故形态：额度打满 5 小时 → 积压 216 条 → 恢复后若照旧认领，额度先被
    # 这批已经错失窗口的旧闻烧光，新新闻排不上队（用户原话"我这个打不住的"）。
    # 水位 0 = 不限制。
    # v453 P0-4：门卫判「可交易」的行不受水位约束 —— 它已经被判定为真信号，
    # 只是恰好落在断档期。归档侧不删它、认领侧也不挡它（见 _archive_below_floor）。
    "     AND (? <= 0 OR (ts IS NOT NULL AND ts >= ?)"
    "          OR COALESCE(gate_verdict, '') = '可交易')"
    # v432（2026-11-17）：门卫未判定行（gate_unavailable=2）只认领「30 分钟内」的。
    # 背景：实测一批 6 条要 8–21 分钟，而建仓窗口只有 45 分钟；更老的行排到时
    # 早已过期，分析完也只产出 routing_miss（近 25 分钟 4/5 条是 routing_miss），
    # 等于拿唯一的主分析产能去处理废料。更老的交给 2h 归档档位（degraded_stale）。
    "     AND (COALESCE(gate_unavailable, 0) <> 2 OR (? <= 0) OR ts >= ?)"
    # v323 实时性修复：原来 ORDER BY id ASC（最老优先）→ 实测积压中位 105 分钟，
    # 每条新闻分析完就已经超过建仓的 45 分钟有效期，等于"分析额度照烧、信号一条用不上"。
    # 改成 ts DESC（最新优先）：新新闻先进先出，才有机会在有效期内变成可交易信号。
    # 旧积压仍会在队列空闲时被处理（6 小时门槛之外直接排除），只是不再抢占队头。
    #
    # v413（2026-11-17）：门卫判「可交易」的**再优先一层**。
    # 实测当日 62 条判「可交易」，只 8 条被分析；v408 之后这些行不再被归档，
    # 于是会长期占着 PENDING，而认领只按 ts DESC → 和「未判定/未过闸」同队排队，
    # 积压时永远排不到（当日静默归档里 33 条就是这样丢的）。
    # 加一层 CASE 前缀后，同档内仍按 ts DESC —— v323 的实时性语义不变。
    #
    # v418（2026-11-17）：可交易还要再分「还在建仓窗口内」与「已经过期」两档。
    # 实测 10:00 积压：45 条「可交易」平均已等 148 分钟，全部超过 45 分钟的
    # 建仓窗口（2700s）——它们被分析了也只会打印「信号过期，跳过建仓」，
    # 却因为排在档 0 而把**新进的**可交易信号挤到后面一起过期。
    # 分层后：窗口内的可交易（真能成交）永远第一顺位；过期可交易降到第二档
    # （仍会被分析、仍进统计，只是不再抢占队头）。
    # v419（2026-11-17）：门卫**没给出判定**的行（gate_unavailable=2，冻结 30 分钟后
    # 自动放行）再插一档，排在「有判定但非可交易」之前。
    # 实测背景：11-10~11-16 每天 159~1538 条、合计 4683 条这样消失 ——
    # 它们放行后落进 PENDING 队尾，按 ts DESC 永远排不过新新闻，6 小时门槛一到就被
    # _expire_stale_news 归档成 FILTERED/stale_expired，ai_decisions 里一条都没有
    # （其中 4405 条 retry_count=0，等于从没被分析过）。
    # 顺序语义：窗口内可交易(0) → 过期可交易(1) → 门卫未判定(2) → 其余(3)。
    # 档内仍按 ts DESC，v323/v413/v418 的实时性语义不变。
    "   ORDER BY (CASE"
    "              WHEN COALESCE(gate_verdict, '') = '可交易' AND ts >= ? THEN 0"
    "              WHEN COALESCE(gate_verdict, '') = '可交易' THEN 1"
    "              WHEN COALESCE(gate_unavailable, 0) = 2 THEN 2"
    "              ELSE 3 END),"
    "            ts DESC, id DESC LIMIT ?"
    " )"
    " RETURNING *"
)


# ─── v336: impact_score 缺省值兜底 ────────────────────────────────────────
# 实测 8865 条决策里 8379 条（94.5%）的 impact_score 恰好 = 5.0：
# 不是模型判断"中性"，而是模型没返回这个字段，_parse_result 按 default 写入 5.0，
# 落库后与"真的打了 5 分"完全无法区分。这直接导致 base_mult 近乎恒定
# （168 个取值覆盖 8865 条），权重分辨率 S14 只有 14.6%。
#
# 而同一批新闻里有 7263 条（86.7%）在 raw_news.gate_impact_1to100 已经有门卫
# 给的 0-100 分（最大 95），却没被用上 —— 传进去 base 的取值可从 168 升到 356。
#
# 这里只做一件事：当模型没给出可区分的分数（缺失或恰为 5.0）时，用门卫分换算。
# 不动公式、不动阈值、不改 suggested_action。


# v397：阶梯抽到 impact_score_ladder（纯函数、零依赖），
# 引擎、回填脚本、测试共用同一份实现，避免"代码里一套、脚本里一套"。
from impact_score_ladder import resolve_impact_score, source_label as _impact_source_label


def _build_claim_sql(limit: Optional[int] = None) -> Tuple[str, List[Any]]:
    """生产认领 SQL + 参数（含时效门槛的 cutoff）。

    limit 省略时用生产批大小（BATCH_SIZE）。

    v418：多传一个 `trade_cutoff` —— 「还在建仓窗口内」的起点。
    它来自 `paper_trading_settings.news_trade_max_age_seconds`（线上 2700s），
    默认 2700s 与引擎里「信号过期，跳过建仓」用的是同一个口径。
    """
    _max_age = int(getattr(config, "NEWS_ANALYZE_MAX_AGE_HOURS", 0) or 0)
    _cutoff = (int(time.time()) - _max_age * 3600) if _max_age > 0 else 0
    sql = _BATCH_CLAIM_FRESH_SQL
    _lim = int(limit) if limit is not None else int(BATCH_SIZE)
    # 建仓窗口（秒）。默认 2700 = 45 分钟，与「信号过期，跳过建仓」同源；
    # 取不到就用默认，不影响排序正确性（只是分档边界略保守）。
    try:
        _trade_win = int(os.getenv("TRIDENT_TRADE_WINDOW_SEC", "2700") or 2700)
    except Exception:
        _trade_win = 2700
    _trade_cutoff = int(time.time()) - max(_trade_win, 0)
    # v432：门卫未判定行的认领时效（分钟）。0 = 不限制（退回旧行为）。
    try:
        _deg_min = int(os.getenv("TRIDENT_DEGRADED_CLAIM_MAX_AGE_MIN", "30") or 0)
    except Exception:
        _deg_min = 30
    _deg_cutoff = int(time.time()) - _deg_min * 60 if _deg_min > 0 else 0
    # v444：断档水位（epoch 秒）。0 = 不限制。
    # 参数顺序必须与 SQL 里的占位符顺序一致：紧跟 (max_age, cutoff) 之后。
    try:
        _floor = int(news_cutoff.get_floor())
    except Exception:
        _floor = 0
    return sql, [_max_age, _cutoff, _floor, _floor,
                 _deg_min, _deg_cutoff, _trade_cutoff, _lim]


# v322: 超龄新闻**归档**SQL —— 超过时效门槛的 PENDING 行，直接标 FILTERED。
# 为什么需要：加了认领时效门槛后，2500 条积压（大半超过 6 小时）永远不会被认领，
# 却一直占着「待分析」计数 → 前端显示 2500 条待分析，实际一条也处理不了。
#
# ⚠️ **不过滤 is_noise**：实测 2340 条 PENDING 里 is_noise=1 的占 61%，
# 而抽样显示它们是**被门卫误杀**的正常新闻（波兰央行/IIF 数据/美联储讲话…，
# gate_verdict 全是「排除」）。它们同样是「永远不会被分析」的僵尸，
# 所以归档时必须一并处理，否则「待分析」计数降不下来。
#
# 语义：FILTERED ≠ 丢数据 —— 原始行还在库里可查、可回放，
# 只是不再占「待分析」计数、不再被送去分析（省额度）。
#
# 与认领门槛同一时间口径（ts epoch），避免时区坑。
#
# v408（2026-11-17）两处改动，都是为了把"放行"和"丢弃"分开：
#   A. 写入 filter_reason='stale_expired' —— 原来归档后 is_noise 保持 0、status 变
#      FILTERED，两种语义共用一个终态；11-16 实测 742/1115 条（66.5%）这样消失，
#      含 52 条门卫已判「可交易」的。留痕后它们可计数、可回放、可追责。
#   B. 门卫判「可交易」的行**不归档** —— 那是真信号，宁可让它占着「待分析」。
#      代价是通道长期故障时「待分析」计数会涨，这是正确的可见性（比静默蒸发好）。
#      `NEWS_EXPIRE_KEEP_TRADEABLE=0` 可退回旧行为。


def _archive_below_floor(conn) -> int:
    """v444（2026-11-18，用户定策）：把**断档水位之下**的 PENDING 行归档。

    与"超龄归档"是两件不同的事：
      · 超龄（`stale_expired`）= 新闻本身时间太旧；
      · 水位之下（`pre_recovery_skip`）= 它是在 LLM 断档期间进的，通道恢复后
        系统按用户规则**不补历史**（"只分析现在进来的"）。

    为什么不分析它们：额度是唯一稀缺资源。2026-11-18 实测断档 5.3 小时积了
    216 条，恢复后若照旧逐条补，额度先被这批已经错失 45 分钟建仓窗口的旧闻烧光，
    新新闻排不上队 —— 用户原话"堆积的多的话我这个打不住的"。

    FILTERED ≠ 丢数据：原始行仍在库里，`filter_reason` 留痕可查、可计数。
    真要重放历史：`NEWS_SKIP_BACKLOG_ON_RECOVERY=0` 或删掉
    `backend/runtime/news_cutoff.json`。
    """
    try:
        _floor = int(news_cutoff.get_floor())
    except Exception:
        return 0
    if _floor <= 0:
        return 0
    _has_reason = False
    try:
        _has_reason = "filter_reason" in db.table_columns(conn, "raw_news")
    except Exception:
        _has_reason = False
    _sql = (
        "UPDATE raw_news SET status='FILTERED', processing_at=NULL"
        + (", filter_reason='pre_recovery_skip'" if _has_reason else "")
        + " WHERE status='PENDING' AND ts IS NOT NULL AND ts < ?"
        # v453 P0-4：门卫判「可交易」的行**不归档**。
        # 注释里早就写了这条（"那是真信号，宁可让它占着待分析"），SQL 却没实现 ——
        # 11-18 实测水位之下 852 条里 396 条是可交易，白丢。认领侧已同步放宽水位。
        + " AND COALESCE(gate_verdict, '') <> '可交易'"
    )
    cur = conn.execute(_sql, (_floor,))
    n = int(cur.rowcount or 0)
    if n:
        print(f"  [CUTOFF] 水位之下（{time.strftime('%H:%M:%S', time.localtime(_floor))} "
              f"之前入库）的 PENDING 新闻 {n} 条 → 归档不分析"
              f"（filter_reason={'pre_recovery_skip' if _has_reason else '无列(老库)'}）",
              flush=True)
    return n


def reap_stale_processing(conn=None, stale_min: Optional[int] = None) -> int:
    """v453 P1-3：把卡死的 PROCESSING 行回收成 PENDING（僵尸回收）。

    事故形态：某条新闻的主分析卡在没返回的 HTTP 上，`processing_at` 留着旧时间，
    行一直显示"处理中"、不降也不重试；批级超时只能覆盖"当批还活着"的场景，
    进程重启/任务丢失（cancel 后回写失败）就管不到。这里每 5 分钟扫一遍，
    **认领超过 15 分钟仍未收尾**的行 → retry_count+1，达上限标 FAILED。
    返回本次回收条数。
    """
    _own = conn is None
    try:
        if stale_min is None:
            stale_min = int(os.getenv("PROCESSING_STALE_MIN", "15") or 15)
        if _own:
            conn = _open_db()
        cur = conn.execute(
            "UPDATE raw_news SET retry_count = COALESCE(retry_count, 0) + 1,"
            " processing_at = NULL,"
            " status = CASE WHEN COALESCE(retry_count, 0) + 1 >= ?"
            "          THEN 'FAILED' ELSE 'PENDING' END"
            " WHERE status='PROCESSING'"
            "   AND processing_at IS NOT NULL"
            "   AND datetime(processing_at) < datetime('now','localtime','-' || ? || ' minutes')",
            (_MAX_PENDING_RETRIES, int(stale_min)),
        )
        n = int(cur.rowcount or 0)
        if _own:
            conn.commit()
        if n:
            print(f"  [AI] 僵尸回收：{n} 条 PROCESSING 超过 {stale_min} 分钟未收尾"
                  f" → PENDING（retry_count+1）", flush=True)
        return n
    except Exception as _e:
        print(f"  [AI] ⚠️ 僵尸回收失败: {type(_e).__name__}: {str(_e)[:120]}",
              flush=True)
        return 0
    finally:
        if _own and conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _expire_stale_news(conn) -> int:
    """把超过时效门槛的 PENDING 行标为 FILTERED。返回处理条数。

    ⚠️ 失败**必须留痕**（v322 事故教训）
    ----------------------------------
    2026-11-07：这一行原来是 `except Exception: return 0` —— 静默吞异常。
    而线上 raw_news.status 的 CHECK 不允许 'FILTERED' → 每条 UPDATE 都被拒
    → 异常被吞 → 库里 0 条 FILTERED → 日志一行都没有 → 前端「待分析」4850 条
    永不下降。**排查了两天才定位到是数据库约束，不是逻辑问题。**
    任何"更新 N 条记录"的函数失败都必须打出来。

    v419 两段式归档：
      · 第一段：普通超龄行（含门卫有判定但非可交易）→ stale_expired
      · 第二段：门卫未判定行 → 用 `NEWS_EXPIRE_GATE_DOWN_GRACE_H`（默认 24h）
        的宽限再给一次机会 → expired_gate_down
    """
    try:
        _max_age = int(getattr(config, "NEWS_ANALYZE_MAX_AGE_HOURS", 0) or 0)
        if _max_age <= 0:
            return 0
        _cutoff = int(time.time()) - _max_age * 3600
        _keep = str(os.getenv("NEWS_EXPIRE_KEEP_TRADEABLE", "1")).strip() != "0"
        # v419：门卫未判定行默认保留（交给第二段宽限），关掉即退回 v408 行为
        _keep_gd = str(os.getenv("NEWS_EXPIRE_KEEP_GATE_DOWN", "1")).strip() != "0"
        try:
            _grace_h = float(os.getenv("NEWS_EXPIRE_GATE_DOWN_GRACE_H", "24") or 24)
        except Exception:
            _grace_h = 24.0
        _has_reason = False
        try:
            _has_reason = "filter_reason" in db.table_columns(conn, "raw_news")
        except Exception:
            _has_reason = False

        _sql = _expire_sql(_keep, _has_reason, keep_gate_down=_keep_gd)
        cur = conn.execute(_sql, (_max_age, _cutoff))
        n = int(cur.rowcount or 0)

        gd_n = 0
        if _keep_gd and _grace_h > _max_age:
            # 宽限必须比常规门槛更长，否则等于没分档
            _gd_cutoff = int(time.time()) - int(_grace_h * 3600)
            cur2 = conn.execute(_expire_gate_down_sql(_has_reason), (_max_age, _gd_cutoff))
            gd_n = int(cur2.rowcount or 0)

        if n:
            print(f"  [BATCH] 归档超龄新闻 {n} 条（>{_max_age}h，门卫可交易的"
                  f"{'保留' if _keep else '一并归档'}，门卫未判定的"
                  f"{'保留' if _keep_gd else '一并归档'}，filter_reason="
                  f"{'stale_expired' if _has_reason else '无列(老库)'}）", flush=True)
        if gd_n:
            print(f"  [BATCH] 归档门卫未判定且超宽限 {gd_n} 条（>{_grace_h:g}h，"
                  f"filter_reason={'expired_gate_down' if _has_reason else '无列(老库)'}）",
                  flush=True)

        # v432（2026-11-17）：门卫未判定行（gate_unavailable=2）按**建仓窗口**归档。
        # 为什么不能再等 24h：这些行是"门卫没给判定但放行"，主分析认领它们
        # （档位 2），实测积压 1000+ 条、单条分析 40s → 要 11 小时才清空，
        # 等轮到它们时早已超过 45 分钟建仓窗口，分析额度照烧、一条也用不上，
        # 还占着「待分析」计数。默认 2h（远大于 2700s 窗口又留了冗余）。
        #
        # v433（P0-3，2026-11-17）：归档理由从 `degraded_stale` 改成 `gate_down`。
        # 为什么必须改：`degraded_stale` 读起来是「新闻旧了」，但这批是**门卫故障**
        # 导致没人判——两种事实一个标记，看板只会显示「已过滤」，故障被藏起来。
        # 实测当天 885 条走这条路径，其中 529 条发布时间 ≤6h（是新鲜新闻）。
        # 改后：理由=gate_down（门卫没判 → 退出建仓候选，但**仍留在库里可回放**），
        # 漏斗单列 gate_down，pipeline_guard 断言「静默归档=0」仍成立。
        deg_n = 0
        try:
            # 兼容旧 env（小时）；缺省取 config.NEWS_EXPIRE_GATE_DOWN_MIN（贴近建仓窗口）
            _deg_h_env = os.getenv("NEWS_EXPIRE_GATE_DOWN_H") or os.getenv("NEWS_EXPIRE_DEGRADED_H")
            if _deg_h_env:
                _deg_h = float(_deg_h_env)
            else:
                _deg_h = getattr(config, "NEWS_EXPIRE_GATE_DOWN_MIN", 50) / 60.0
        except Exception:
            _deg_h = 50 / 60.0
        if _deg_h > 0:
            _deg_cutoff = int(time.time()) - int(_deg_h * 3600)
            _deg_sql = (
                "UPDATE raw_news SET status='FILTERED'"
                + (", filter_reason='gate_down'" if _has_reason else "")
                + " WHERE status='PENDING' AND COALESCE(gate_unavailable,0)=2"
                "   AND COALESCE(gate_verdict,'') <> '可交易' AND ts < ?"
            )
            cur3 = conn.execute(_deg_sql, (_deg_cutoff,))
            deg_n = int(cur3.rowcount or 0)
            if deg_n:
                print(f"  [BATCH] 门卫未判定超 {_deg_h:g}h → 退出建仓候选 {deg_n} 条"
                      f"（filter_reason={'gate_down（可回放）' if _has_reason else '无列(老库)'}）",
                      flush=True)

        # v436：只处理新鲜新闻 —— gate=0 正常判定的非可交易行，超过建仓窗口即
        # 退出候选。可交易行保留；超窗行不再回炉烧主分析额度，也不占待分析计数。
        fresh_n = 0
        try:
            _fresh_min = int(getattr(config, "NEWS_TRADE_MAX_AGE_MINUTES", 45) or 45)
            if _fresh_min > 0:
                _fresh_cutoff = int(time.time()) - _fresh_min * 60
                _fresh_sql = (
                    "UPDATE raw_news SET status='FILTERED', processing_at=NULL"
                    + (", filter_reason='stale_expired'" if _has_reason else "")
                    + " WHERE status='PENDING' AND COALESCE(gate_unavailable,0)=0"
                    + "   AND COALESCE(gate_verdict,'') <> '可交易' AND ts < ?"
                )
                cur4 = conn.execute(_fresh_sql, (_fresh_cutoff,))
                fresh_n = int(cur4.rowcount or 0)
                if fresh_n:
                    print(f"  [BATCH] 正常判定超建仓窗口 {_fresh_min}min → 退出建仓候选 {fresh_n} 条"
                          f"（filter_reason=stale_expired）", flush=True)
        except Exception:
            pass

        return n + gd_n + deg_n + fresh_n
    except Exception as exc:
        print(f"  [BATCH] 归档超龄新闻失败: {type(exc).__name__}: {str(exc)[:150]}",
              flush=True)
        try:
            conn.rollback()
        except Exception:
            pass
        return 0


def _run_expire_stale_news() -> int:
    """独立连接执行超龄归档，返回处理条数。

    为什么单独开连接、不放在认领事务里：
      · 归档只是一条 UPDATE，**不花任何 LLM 额度**；
      · 之前它被埋在 ai_worker._batch_claim() 内部 → 一旦通道全熔断
        （main_pool_ready()=False，主循环直接 continue）就永远跑不到
        → 「待分析」计数在通道故障期间只涨不降；
      · 放在认领事务里，归档失败还会连累认领（DB 锁 → 整批认领失败）。
    """
    conn = None
    try:
        conn = _open_db()
        # v444（2026-11-18）：先判"是否断过档"，恢复则把水位推进到此刻，
        # 再把水位之下的 PENDING 归档。两步都不花 LLM 额度，放在最前面，
        # 保证「通道一恢复 → 先丢历史 → 再认领新新闻」这个顺序不会被认领抢跑。
        try:
            news_cutoff.maybe_advance_on_recovery(conn)
        except Exception as _cut_exc:
            print(f"  [CUTOFF] 断档判定跳过（{type(_cut_exc).__name__}: "
                  f"{str(_cut_exc)[:80]}）", flush=True)
        # v445（2026-11-18）：再判"队列是否超过容量上限"。断档水位只管"彻底停摆
        # 后恢复"，管不住"分析速度追不上新闻流速"导致的慢性堆积（批 4 条/10 分钟
        # vs 到达 200 条/小时）。超过上限就把老的推下去、只留最新 N 条。
        try:
            news_cutoff.maybe_advance_on_backlog(conn)
        except Exception as _cap_exc:
            print(f"  [CUTOFF] 队列兜底跳过（{type(_cap_exc).__name__}: "
                  f"{str(_cap_exc)[:80]}）", flush=True)
        n = _archive_below_floor(conn) + _expire_stale_news(conn)
        if n:
            conn.commit()
        return n
    except Exception as exc:
        print(f"  [BATCH] 归档超龄新闻异常: {type(exc).__name__}: {str(exc)[:150]}",
              flush=True)
        return 0
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


# v321: 门卫回放自愈的节流参数（主循环每轮检查，间隔到才真跑）。
# 默认 5 分钟一轮、每轮最多 50 条 —— 通道健康预检通过才回放。
# v422（2026-11-17）：300 → 900。回放每 5 分钟一轮时，同一条冻结新闻在 30 分钟
# 保质期内会被重判 6 次（今天 320 条冻结 ≈ 1,900 次白跑，gate 调用 3,959 次 /
# 833 条 raw = 4.75 次/条，而健康日只有 0.88 次/条）。放到 15 分钟 → 每条最多 2 次。
# 代价：门卫瞬时抖动的自愈从 5 分钟延迟到 15 分钟，可接受（大幅减少 429 空转）。
_GATE_REPLAY_INTERVAL_S = float(os.getenv("TRIDENT_GATE_REPLAY_INTERVAL_S", "900") or 900.0)

# v322: 门卫冻结保质期（秒）。超过这个时长仍未被回放判定的冻结新闻，
# 主循环自动降级放行（gate_unavailable=0）—— 冻结不是永久状态。
# 取 30 分钟：门卫瞬时抖动（个位数条）会被回放正常吃掉；
# 系统性故障（通道 401/429 持续数小时）不会造成"待分析"永久积压。
# v422（2026-11-17）：默认 1800 不变（**不能改小**）。
# 为什么不能改小：冻结行到期会"降级放行"，直接进主分析——一条主分析 ~13.5K token，
# 比一次门卫重判（533 token）贵 25 倍。把 TTL 压到 5 分钟 = 通道一抖就把
# 320 条没判过的新闻塞进主分析，反而更烧。
# 真正的浪费在**回放频率**：回放每 5 分钟一轮 × 冻结 30 分钟 = 同一条被重判 6 次。
# 所以这里把回放间隔放到 15 分钟（每条冻结最多重判 2 次），TTL 保持 30 分钟。
_GATE_FREEZE_TTL_S = int(os.getenv("TRIDENT_GATE_FREEZE_TTL_S", "1800") or 1800)


# v321.2: 门卫回放的后台调度（模块级 → 可被单测直接覆盖）
#
# ⚠️ 历史事故（2026-11-04 生产实测）：v321 把回放内联在主循环里写成
#    `await asyncio.to_thread(once, limit=...)`。阻塞调用虽然挪到了工作线程，
#    但**主循环协程本身仍 await 在这一句上** → 后面的 Phase 1 认领要等整轮
#    回放跑完。实测一轮 50 条要 11-12 分钟（每条 6-13s），期间主分析一条
#    都不认领（症状：可认领量堆积到 22 条、DONE 停在 06:45）。
#    现在回放丢后台任务，主循环立刻继续认领；单飞标志保证同一时刻只有一轮。
async def run_gate_replay_background(once, limit: int = _GATE_REPLAY_BATCH) -> None:
    """后台跑一轮门卫回放。异常一律吞掉（自愈失败不得影响主分析）。"""
    try:
        stat = await asyncio.to_thread(once, limit=limit)
        if stat.get("ok"):
            print("[gate-replay] 自动回放 %d 条，剩余 %s（%s）"
                  % (stat.get("ok", 0), stat.get("remaining"), stat.get("reason")),
                  flush=True)
        elif stat.get("skipped"):
            print("[gate-replay] 跳过（通道不可用）：%s" % stat.get("reason"),
                  flush=True)
    except Exception as exc:
        print("[gate-replay] 自愈异常（不影响主循环）: %s: %s"
              % (type(exc).__name__, str(exc)[:150]), flush=True)


def schedule_gate_replay(once, holder: Dict[str, Any],
                         limit: int = _GATE_REPLAY_BATCH) -> Optional[asyncio.Task]:
    """把门卫回放丢到后台任务（单飞）。

    · 返回新建的 Task；上一轮还在跑时返回 None（不重复开轮）
    · **调用方绝不能 await 这个 Task** —— 一 await 就退化成 v321 的阻塞行为
    """
    prev = holder.get("task")
    if prev is not None and not prev.done():
        return None
    task = asyncio.create_task(run_gate_replay_background(once, limit=limit))
    holder["task"] = task
    return task


def _load_gate_replay_guard():
    """加载 gate_replay_guard.run_once，失败返回 None。

    ⚠️ 为什么要两套导入：ai_worker 是 **engine 包内模块**（engine/main.py 用
    `from .ai_worker import ai_worker`），运行时的 sys.path 里有 src_python/，
    但 **没有** src_python/engine/。所以平铺导入 `from gate_replay_guard import`
    会 ImportError。必须优先用包导入 `from engine.gate_replay_guard import`。

    ⚠️ 为什么不能静默失败：以前这段包在 try/except 里，导入失败也只打一行
    日志 —— 结果自愈**从未真正生效**，而 759 条数据一直冻结。
    现在导入失败会明确暴露（返回 None + 调用方报错 + 启动自检）。
    """
    try:
        from engine.gate_replay_guard import run_once
        return run_once
    except Exception:
        pass
    try:  # 兜底：被当作平铺脚本直接运行时
        from gate_replay_guard import run_once
        return run_once
    except Exception:
        return None


# ── 核心字段白名单：LLM 结果整箱过滤时用的合法取值集合 ──
# _MARKET_CATEGORIES 保留为兼容旧调用方的别名；新代码必须用 market_classifier.MARKET_CATEGORIES_PROD
_MARKET_CATEGORIES = MARKET_CATEGORIES_PROD  # v1 重构：直接复用真理源（无 OTHER）


def _build_evidence_from_local(asset: str) -> str:
    """从本地 ohlcv_intraday 拼硬数据（quote_sources 拿不到该资产时的兜底）。

    v344 起因（实测）：
      quote_sources.route("WTI") → {'market': 'domestic_spot', 'symbol': None}
      于是 _build_evidence_for_asset 直接 return ""，WTI **永远没有硬数据证据**，
      market_confirmation 也就恒为 unknown。但库里明明有 9,229 根 WTI 分钟线。
      NVDA(yahoo)/NDX(None) 等也是同一类空路由。

    格式与 _build_evidence_for_asset 保持一致：
      "WTI 13:04 close=88.4250 pct=+0.03% vol=0 | WTI 11-16 13:00 1H close=..."
    """
    try:
        import sqlite3 as _s3
        conn = _s3.connect(f"file:{db.get_db_path()}?mode=ro", uri=True, timeout=5)
        try:
            # v409: 多取几根再挑 —— 整点刚过时最新 1m 线只有一笔成交（四价全等），
            # 会被误判成"冻结报价"，XAU/WTI 因此有 184 条决策白白丢了硬数据。
            recent = conn.execute(
                "SELECT ts,open,high,low,close,volume FROM ohlcv_intraday "
                "WHERE asset=? AND interval='1m' ORDER BY ts DESC LIMIT 8",
                (asset,)).fetchall()
            hour = conn.execute(
                "SELECT ts,open,high,low,close,volume FROM ohlcv_intraday "
                "WHERE asset=? AND interval='1m' ORDER BY ts DESC LIMIT 120",
                (asset,)).fetchall()
        finally:
            conn.close()
    except Exception:
        return ""
    if not recent:
        return ""
    import hard_data_block as _hdb
    sel = _hdb.select_bar(recent, time.time())
    if not sel:
        return ""
    # 1H 聚合必须从选中的那根往前数，否则会把"进行中"的小时当成完整小时。
    # hour 与 recent 都是 DESC、最新在前，所以同一根在两张表里下标一致。
    try:
        idx = recent.index(sel)
    except ValueError:
        idx = 0
    hour_from_sel = hour[idx:] if 0 <= idx < len(hour) else hour
    return _hdb.build_block(asset, sel, hour_from_sel, now_ts=time.time())


def _build_evidence_for_asset(asset: str, ts_iso: str = "") -> str:
    """P1-6 服务端拼硬数据：抓 1m + 1H K 线最后一根 → 拼接成 hard_data_evidence。

    用于 LLM 漏填 hard_data_evidence 时的兜底。失败返回空字符串。

    v176.11 fix：之前无脑用 fetch_okx_candles → BTC/ETH/SOL 走 OKX 必然失败（OKX 上没有
      BTC-USDT-SWAP 这种合约，这些币只在币安上市）；现在按 quote_sources.route(asset) 路由表
      选对数据源：
        - binance_fapi / binance_spot → fetch_binance_spot_candles（spot 国内直连稳定）
        - okx_swap                    → fetch_okx_candles（XAU/XAG 已有国内现货兜底）
        - yahoo / domestic_spot       → 返回空（暂未实现 candles fetcher，标记为 N/A 更诚实）
      路由失败的资产（不在 ASSET_ROUTE 里）返回空字符串，避免误用别币种 K 线。
    """
    try:
        from feeds import quote_sources as _qs
        rt = _qs.route(asset)
        if not rt:
            # 路由表里没有该资产 → 本地库兜底（v344）
            return _build_evidence_from_local(asset)
        market = rt.get("market", "")
        sym = rt.get("symbol") or ""
        if not sym:
            # WTI/OIL 等无永续合约（route 返回 symbol=None）→ 先走本地库兜底，
            #   拿不到才是真的没有证据（v344：此前直接 return ""，WTI 永远无证据）
            return _build_evidence_from_local(asset)

        # ── 选 fetcher ──
        rows_1m: list = []
        rows_1h: list = []
        try:
            import datetime as _dt
            if market in ("binance_fapi", "binance_spot"):
                # 币安币种（BTC/ETH/SOL）—— 用现货 spot（国内直连稳定），不再走 OKX
                rows_1m = _qs.fetch_binance_spot_candles(sym, "1m", 2) or []
                rows_1h = _qs.fetch_binance_spot_candles(sym, "1h", 2) or []
            elif market == "okx_swap":
                # OKX 优先 → Yahoo 兜底（v2026.10.05）
                rows_1m = _qs.fetch_asset_candles_with_yahoo_fallback(asset, "1m", 2) or []
                rows_1h = _qs.fetch_asset_candles_with_yahoo_fallback(asset, "1H", 2) or []
            elif market in ("yahoo", "domestic_spot"):
                # v491：原来这里直接回本地库（美股/原油永远没有硬数据）。
                # 现在多源链已就绪：美股 1h 走 Yahoo→（日线）腾讯，原油走 Yahoo CL=F→新浪日线。
                rows_1h = _qs.fetch_daily_multisource(asset, interval="60m", limit=2) or []
                if not rows_1h and market == "domestic_spot":
                    rows_1h = _qs.fetch_daily_multisource(asset, interval="1d", limit=2) or []
                if not rows_1h:
                    return _build_evidence_from_local(asset)
            else:
                return _build_evidence_from_local(asset)
        except Exception:
            return _build_evidence_from_local(asset)

        if not rows_1m and not rows_1h:
            return _build_evidence_from_local(asset)

        lines: list = []
        if rows_1m:
            r = rows_1m[-1]
            # r = [ts_ms, open, high, low, close, volume]
            import datetime as _dt
            t_str = _dt.datetime.fromtimestamp(r[0] / 1000).strftime("%H:%M")
            change_pct = ((r[4] - r[1]) / r[1] * 100.0) if r[1] else 0.0
            lines.append(f"{asset} {t_str} close={r[4]:.4f} pct={change_pct:+.2f}% vol={r[5]:.0f}")
        if rows_1h:
            r = rows_1h[-1]
            import datetime as _dt
            t_str = _dt.datetime.fromtimestamp(r[0] / 1000).strftime("%m-%d %H:%M")
            change_pct = ((r[4] - r[1]) / r[1] * 100.0) if r[1] else 0.0
            lines.append(f"{asset} {t_str} 1H close={r[4]:.4f} pct={change_pct:+.2f}%")
        return " | ".join(lines)
    except Exception:
        return _build_evidence_from_local(asset)


# ─── v397: 硬数据前置注入 ────────────────────────────────────────────────
# 实测（2026-11-16 线上 470 条决策）：
#   hard_data_evidence 只有 99 条（21.1%）；market_confirmation 466 条（99.1%）
#   是 unknown。模型不是不想判断盘面，是 **prompt 里根本没有可引用的数** ——
#   硬数据此前只在"LLM 漏填后由服务端回填"（_sanitize_llm_result / 落库兜底），
#   模型自己看不见，于是 [水位博弈] 只能写"未注入盘面快照，按中性处理"。
# 这里把同一份数据在调用前就给它：本地库优先（毫秒级、离线可用），
# 本地没有才走网络 fetcher；同品种同时间片走 TTL 缓存，避免每条新闻读一次库。
_HARD_DATA_BLOCK_CACHE: Dict[str, str] = {}


def _hard_data_block(asset: str, *, now_ts: Optional[float] = None) -> str:
    """按品种取一条可直接引用的硬数据证据（60s TTL 缓存）。

    返回 "" 表示该品种确实没有可用 K 线（如 AMZN / CN_STOCK 这类无本地线的品种）。
    带"数据质量提示"的块照样返回 —— 让模型知道"有数但不能作方向依据"，
    这比让它以为"没数"更接近事实。
    """
    asset_key = str(asset or "").strip().upper()
    if not asset_key or asset_key in ("NONE", "OTHER"):
        return ""
    ts = float(now_ts) if now_ts is not None else time.time()
    try:
        import hard_data_block as _hdb
        _ck = "%s|%d" % _hdb.cache_key(asset_key, ts, _HARD_DATA_BLOCK_TTL_SEC)
    except Exception:
        _ck = "%s|%d" % (asset_key, int(ts // _HARD_DATA_BLOCK_TTL_SEC))
    if _ck in _HARD_DATA_BLOCK_CACHE:
        return _HARD_DATA_BLOCK_CACHE[_ck]
    block = ""
    try:
        # 本地库优先：不走网络 → 不拖慢批处理，也不受代理/限频影响
        block = _build_evidence_from_local(asset_key)
    except Exception:
        block = ""
    # 本地这份若是"冻结/过期"，说明只作记录 —— 再给网络一次机会拿真盘面。
    # 网络也拿不到就保留本地这份（带质量提示），比"没有数"更接近事实。
    try:
        import hard_data_block as _hdb2
        _usable = _hdb2.is_usable(block)
    except Exception:
        _usable = bool(block)
    if not _usable:
        try:
            _net_block = _build_evidence_for_asset(asset_key)
            if _net_block:
                block = _net_block
        except Exception:
            pass
    if block:
        # 缓存只保留最近 256 个时间片，防长跑内存漂移
        if len(_HARD_DATA_BLOCK_CACHE) > 256:
            _HARD_DATA_BLOCK_CACHE.clear()
        _HARD_DATA_BLOCK_CACHE[_ck] = block
    return block


def _compute_market_style(asset: str) -> str:
    """P2-9 市场风格感知：最近 1H/4H K 线算 ATR% + 趋势方向 → 输出风格标签。

    返回字符串格式："{style}|atr_pct%|trend_pct%"。style ∈
      trending_up / trending_down / mean_revert / volatile_high / volatile_low / unknown
    """
    try:
        from feeds import quote_sources as _qs
        rt = _qs.route(asset)
        if not rt:
            return "unknown|n/a|n/a"
        market = rt.get("market", "")
        sym = rt.get("symbol") or ""
        if not sym:
            return "unknown|n/a|n/a"
        # v176.11 fix：与 _build_evidence_for_asset 同 bug —— 按市场路由选 fetcher，
        #   不再无脑走 OKX（BTC/ETH/SOL 不在 OKX 上市）。
        # v2026.10.05：okx_swap 分支加 Yahoo 兜底，避免 OKX SNI 阻断时 ATR/趋势为空。
        h1 = []
        try:
            if market in ("binance_fapi", "binance_spot"):
                h1 = _qs.fetch_binance_spot_candles(sym, "1h", 24) or []
            elif market == "okx_swap":
                h1 = _qs.fetch_asset_candles_with_yahoo_fallback(asset, "1H", 24) or []
            else:
                return "unknown|n/a|n/a"
        except Exception:
            return "unknown|n/a|n/a"
        if not h1 or len(h1) < 5:
            return "unknown|n/a|n/a"
        # ATR% = mean(high-low)/close * 100
        atr_sum = 0.0
        atr_n = 0
        closes = []
        for r in h1:
            try:
                h_l = float(r[2]) - float(r[3])
                atr_sum += h_l
                atr_n += 1
                closes.append(float(r[4]))
            except Exception:
                continue
        if atr_n == 0 or not closes:
            return "unknown|n/a|n/a"
        atr_pct = atr_sum / atr_n / closes[-1] * 100.0
        # trend: 24h 前后 close 对比
        trend_pct = (closes[-1] - closes[0]) / closes[0] * 100.0 if closes[0] else 0.0
        # 风格判定
        if atr_pct > 1.5:
            style = "volatile_high"  # 高波动：新闻冲击钝化，胜率偏低
        elif atr_pct < 0.3:
            style = "volatile_low"   # 低波动：新闻冲击被放大
        elif abs(trend_pct) > 1.5:
            style = "trending_up" if trend_pct > 0 else "trending_down"
        else:
            style = "mean_revert"
        return f"{style}|{atr_pct:.2f}|{trend_pct:+.2f}"
    except Exception:
        return "unknown|n/a|n/a"


# ── v176.2：市场风格持久化 + 决策反思 + prev 决策传递 ─────────────────────


def _snapshot_market_style(asset: str, regime_hint: str = "") -> int:
    """把 _compute_market_style 输出 + 5 层 alerts 落 market_style_snapshot 表。"""
    try:
        import time
        import db as _db
        from market import market_style as _ms
        style_str = _compute_market_style(asset)
        style = style_str.split("|", 1)[0] if style_str else "unknown"
        try:    layer1 = ",".join(_ms.layer1_price_structure_alerts(asset))
        except Exception: layer1 = ""
        try:    layer2 = ",".join(_ms.layer2_funding_flow_alerts(asset))
        except Exception: layer2 = ""
        try:    layer3 = ",".join(_ms.layer3_positioning_alerts(asset))
        except Exception: layer3 = ""
        try:    layer4 = ",".join(_ms.layer4_macro_rate_alerts(asset))
        except Exception: layer4 = ""
        try:    layer5 = ",".join(_ms.layer5_event_alerts(asset))
        except Exception: layer5 = ""
        try:    style_block = _ms.market_style_block(asset)
        except Exception: style_block = ""
        conn = _db.get_connection()
        try:
            conn.execute(
                "INSERT INTO market_style_snapshot "
                "(asset, regime, layer1_alerts, layer2_alerts, layer3_alerts, "
                "layer4_alerts, layer5_alerts, style_block, trigger, ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (asset or "", regime_hint or style, layer1, layer2, layer3,
                 layer4, layer5, style_block, "auto", int(time.time())),
            )
            conn.commit()
            return 1
        finally:
            # v189-8: 该函数原先 return 1 / except 两条路径都不关连接 → fd 单调增长
            conn.close()
    except Exception:
        return 0


def _reflect_recent_decisions(asset: str, window: str = "1h", n: int = 8) -> int:
    """读最近 N 条同品种决策 + 真实 K 线方向，算胜率 → 写反思。

    v186 audit 修复: 即使 settled=0 也计算虚拟 PnL (entry_price → 当前价).
    原版: 当 _resolve_post_decision_pnl 返回 None (因为 settled=0) → all flat →
    n_decided=0 → return 0 → 反思永远空转. 反馈环断在第一步.

    新逻辑:
    1. 优先用 _resolve_post_decision_pnl (真实结算的 PnL)
    2. fallback: 从 entry_price + 当前 _get_current_price_cached 算虚拟 PnL
    3. 仍 fallback: 从 max_price/min_price 算 MFE/MAE 推断方向
    4. 只在完全没信号时才返回 0

    这样即使 forward_tracker 没跑 (settled=0), 反思 worker 也能产出.
    """
    conn = None
    try:
        import time
        import db as _db
        conn = _db.get_connection()
        rows = conn.execute(
            "SELECT id, suggested_action, entry_price, max_price, min_price, "
            "       entry_time, settled, forward_pnl "
            "FROM ai_decisions "
            "WHERE target_asset=? AND LENGTH(COALESCE(reasoning_path,'')) > 50 "
            "ORDER BY id DESC LIMIT ?",
            (asset, n),
        ).fetchall()
        if len(rows) < 3:
            return 0
        win = loss = flat = 0
        pnl_sum = 0.0
        try:
            from prices import _resolve_post_decision_pnl as _pp
        except Exception:
            _pp = None
        try:
            from prices import _get_current_price_cached as _cp
        except Exception:
            _cp = None

        for row in rows:
            rid = row["id"]
            action = row["suggested_action"]
            entry = row["entry_price"]
            cur_max = row["max_price"]
            cur_min = row["min_price"]
            settled = row["settled"]
            fwd_pnl = row["forward_pnl"]

            pnl = None
            # 1) 真实结算优先
            if settled == 1 and fwd_pnl is not None:
                pnl = float(fwd_pnl)
            # 2) _resolve_post_decision_pnl
            elif _pp:
                try:
                    pnl = _pp(rid, asset, "1h")
                except Exception:
                    pnl = None
            # 3) v186 fallback: 从 entry_price + 当前价算虚拟 PnL
            if pnl is None and entry and entry > 0 and _cp:
                try:
                    cur_price = _cp(asset)
                except Exception:
                    cur_price = None
                if cur_price and cur_price > 0:
                    pnl = (cur_price - entry) / entry
            # 4) v186 fallback: 从 max/min 推断方向
            if pnl is None and entry and entry > 0 and (cur_max or cur_min):
                if cur_max and cur_max > entry:
                    pnl = (cur_max - entry) / entry  # 假设拿到最优
                elif cur_min and cur_min < entry:
                    pnl = (cur_min - entry) / entry

            if pnl is None:
                flat += 1; continue
            pnl_sum += pnl
            action_u = (action or "").upper()
            if action_u == "BUY" and pnl > 0.0005:        win += 1
            elif action_u == "SELL" and pnl < -0.0005:    win += 1
            elif action_u == "HOLD":                      flat += 1
            elif abs(pnl) < 0.0005:                       flat += 1
            else:                                         loss += 1
        n_decided = win + loss
        if n_decided == 0: return 0
        win_rate = win / n_decided
        bias_text = ""; trigger = ""
        if loss >= 3 and win_rate < 0.30:
            bias_text = f"{asset} {window} 连{loss}输{win}赢胜率{win_rate:.0%}—下次降一档或转HOLD"
            trigger = f"连亏{loss}次"
        elif win >= 5 and win_rate > 0.70:
            bias_text = f"{asset} {window} {win}胜{loss}负—同方向信心可上调"
            trigger = f"连胜{win}次"
        elif loss > win * 2 and n_decided >= 4:
            bias_text = f"{asset} {window} 亏/盈={loss}/{win}—方向偏好系统性偏差，复核[驱动力]"
            trigger = "systematic_bias"
        if not bias_text: return 0
        avg_pnl = pnl_sum / max(1, win + loss + flat)
        conn.execute(
            "INSERT INTO decision_reflection "
            "(asset, window, n_decisions, win_n, loss_n, flat_n, avg_pnl_pct, "
            "bias_text, trigger, ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (asset, window, len(rows), win, loss, flat, avg_pnl, bias_text,
             trigger, int(time.time())),
        )
        conn.commit()
        return 1
    except Exception:
        return 0
    finally:
        # v189-8: 本函数有 4 条 return 路径 (len(rows)<3 / n_decided==0 / 无 bias_text /
        # 正常写入) 且外层 except 吞掉所有异常, 原先一条都不关连接 → 每次调用泄漏 1 个 fd,
        # 且 `len(rows) < 3` 这条在低活跃时段是**必然命中**的路径。
        if conn is not None:
            conn.close()


def _auto_match_insights(news_text: str, target_asset: str = "", top_n: int = 3) -> List[int]:
    """v186: 当 AI 输出 insight_hit_ids 为空时,自动从 review_insights 中按关键词匹配 top-N.

    目的: 让"经验"真的参与决策的命中统计. 原版 AI 总是返回 [] 因为 prompt 里
    虽然列出了 EX# 但模型没法靠直觉判断"这条新闻属于哪几条经验". 我们用
    关键词打分 (rule 中关键词 × 新闻中出现次数), 选 top-3.

    算法: tokenize rule 和 news_text → 计算 jaccard 相似度 (set intersection / union).
    返回 matched insight_id 列表 (按相似度降序).
    """
    if not news_text:
        return []
    try:
        from review.review_agent import load_active_insight_items
        import re as _re
        items = load_active_insight_items()
        if not items:
            return []
        # tokenize news_text → set
        news_tokens = set(
            t.lower() for t in _re.findall(r"[\w\u4e00-\u9fff]+", news_text) if len(t) >= 2
        )
        if not news_tokens:
            return []
        scored = []
        for it in items:
            rule = it.get("rule", "")
            if not rule:
                continue
            rule_tokens = set(
                t.lower() for t in _re.findall(r"[\w\u4e00-\u9fff]+", rule) if len(t) >= 2
            )
            if not rule_tokens:
                continue
            inter = news_tokens & rule_tokens
            if not inter:
                continue
            # jaccard
            sim = len(inter) / len(news_tokens | rule_tokens)
            # bonus: asset match (规则里直接点名 asset 加分)
            if target_asset and target_asset.upper() in rule.upper():
                sim += 0.2
            scored.append((sim, int(it["id"])))
        scored.sort(reverse=True)
        return [iid for _, iid in scored[:top_n]]
    except Exception:
        return []


def _load_prev_decision(asset: str) -> dict:
    """查同 asset 最近 1 条决策，返回 prev/regime/bias 5 字段。"""
    out = {"prev_decision_id": None, "prev_action_same_asset": "",
           "prev_score_same_asset": None, "regime_at_decision": "",
           "bias_after_reflection": ""}
    conn = None
    try:
        import db as _db
        conn = _db.get_connection()
        row = conn.execute(
            "SELECT id, suggested_action, sentiment_score FROM ai_decisions "
            "WHERE target_asset=? AND LENGTH(COALESCE(reasoning_path,'')) > 50 "
            "ORDER BY id DESC LIMIT 1", (asset,),
        ).fetchone()
        if row:
            out["prev_decision_id"] = row[0]
            out["prev_action_same_asset"] = row[1] or ""
            out["prev_score_same_asset"] = row[2]
        row2 = conn.execute(
            "SELECT regime FROM market_style_snapshot WHERE asset=? "
            "ORDER BY id DESC LIMIT 1", (asset,),
        ).fetchone()
        if row2: out["regime_at_decision"] = row2[0]
        row3 = conn.execute(
            "SELECT bias_text FROM decision_reflection WHERE asset=? "
            "ORDER BY id DESC LIMIT 1", (asset,),
        ).fetchone()
        if row3: out["bias_after_reflection"] = row3[0]
    except Exception:
        pass
    finally:
        # v189-8: 原函数 bare get_connection() 后从不 close, 每条新闻泄漏 1 个 fd
        if conn is not None:
            conn.close()
    return out


# 从 reasoning 抽取因素时的元信息/交易动作噪声词（命中即丢弃整条候选）


def _extract_factors_fallback(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    """v200 P0+ 严谨兜底: factor_ranking 缺失/全空时的真实因素抽取.

    设计原则（取代 v176 旧"服务端兜底因素"占位）:
      - X1 假数据禁止: 不写"服务端兜底因素"假字符串
      - 优先级: 从 LLM 真实产出(asset_nature/reasoning) 抽取 → L2 业务知识兜底 → L5 留空
      - 所有 fallback 抽取出来的 factor 打 source 标记, 不让 LLM 误以为 LLM 真输出
    """
    action = str(result.get("suggested_action") or "HOLD").strip().upper()
    direction = {"BUY": "利多", "SELL": "利空"}.get(action, "中性")
    target = str(result.get("target_asset") or "NONE").upper()

    # 只从 LLM 真实产出的两个字段提取。reasoning_path 在 80% 决策里是服务端
    # 模板占位（501/557 分支生成），从中提取会带入「目标资产 WTI」等噪声，故不作为源。
    # 且本函数在 sanitize 链条中运行，此时 reasoning/reasoning_path 可能已被前面的
    # 兜底步骤改写，必须跳过带服务端兜底标记的源，避免把「LLM 未填 reasoning」当因素。
    sources: List[str] = []
    an = str(result.get("asset_nature") or "")
    if an and "服务端兜底" not in an:
        sources.append(an)
    rsn = str(result.get("reasoning") or "")
    if rsn and not any(t in rsn for t in ("服务端兜底", "LLM 未填", "关键词推断", "未做全文分析")):
        sources.append(rsn)

    factors: List[str] = []
    seen = set()
    for src in sources:
        seg = re.sub(r"^\[[^\]]+\]\s*", "", src.strip())
        # 拎出 'xxx 主导驱动/驱动/性质：' 之后的宾语
        seg = re.sub(r"^.*?(?:主导驱动|核心驱动|驱动|性质)[:：]\s*", "", seg)
        # 去掉括号备注、数值百分比，换成逗号便于拆分
        seg = re.sub(r"[（(][^）)]*[）)]", "，", seg)
        seg = re.sub(r"[-+]?\d+(?:\.\d+)?%?", " ", seg)
        seg = re.sub(r"\d+/\d+", " ", seg)
        # 用中文/英文标点拆分（保留英文短语内部空格，如 'risk on'）
        for part in re.split(r"[+、，,；;：:/|]+", seg):
            cand = part.strip().rstrip("，,。.;；")
            cand = re.sub(r"^(利多|利空|中性|看多|看空|观望)\s*", "", cand).strip()
            # 丢弃以 '[' 开头的模板标记（如 '[副品种 WTI] 事件类型'、'[MiniMax] ...'）
            if cand.startswith("["):
                continue
            if _looks_placeholder(cand):
                continue
            if len(cand) < 2 or len(cand) > 16:
                continue
            # v342: 降级模板里的整句会被当成"因素"写进 factor_ranking
            # （实测出现 factor='该标的**不在**核心监控清单'）—— 这是口径噪声，不是因素。
            if any(t in cand for t in _DEGRADE_TEXT_MARKS):
                continue
            if cand.upper() == target:
                continue
            if cand in _FACTOR_STOPWORDS:
                continue
            if any(w in cand for w in _FACTOR_STOPWORDS):
                continue
            if cand not in seen:
                seen.add(cand)
                factors.append(cand)

    if factors:
        return [{"factor": f[:200], "rank": i, "direction": direction}
                for i, f in enumerate(factors[:3], 1)]

    # ── L2 业务知识兜底: **只认已学到的因果图谱驱动** ──
    # v408: 旧实现允许退回到 inject_block 的"通用硬编码结构"(美元DXY反向/实际利率反向),
    # 于是任何品种都能拿到 4 条一模一样的"驱动" —— 那不是学到的知识, 是套话, 却被打上
    # business_knowledge 的标。宁可留空(L5), 也不把通用框架当该品种的驱动写进 factor_ranking。
    drivers = _fallback_business_knowledge_drivers(target, allow_generic=False)
    if drivers:
        return [{"factor": d[:200], "rank": i, "direction": direction}
                for i, d in enumerate(drivers[:5], 1)]

    # ── L5 留空: 不写假字符串, 让上游知道是缺失 ──
    return []


# 降级模板的固定措辞：这些整句若被抽成"因素"，会把噪声写进 factor_ranking。
# classify_analysis_degraded / _is_resolved_asset 已外迁 engine.worker_helpers（import * 接回）


# strip_think_blocks / is_empty_llm_output 已外迁 engine.worker_helpers（import * 接回）


def _presanitize_for_schema(result: Dict[str, Any], cleaned: str = "") -> Dict[str, Any]:
    """v176：把 schema 校验必填字段强制达标（在 schema 校验前再补一遍）。

    llm_schema.AI_DECISION_SCHEMA 必填 + 强约束：asset_nature(minLen 4)/verification(minLen 4)
    /event_type_code(minLen 3)/reasoning_path(minLen 100)/hard_data_evidence(minLen 4)
    /reasoning(minLen 4)/asset_reasoning(minLen 4)/invalidation_condition(minLen 4)
    /factor_ranking 至少 1 个合规 item。

    兜底顺序：sanitize → presanitize → schema validate。

    v176.9 新增参数 cleaned：LLM 原文（剥掉 <think> 后的）。
    用于全空兜底时从原文末尾抽 80 字当 [总结] 占位，
    避免 reasoning_path 全是 "LLM 未输出八段" 这种零信息占位。
    """
    if not isinstance(result, dict):
        result = {}

    # 0) v176.8 防御：剥离 reasoning 里的 <think>…</think> 标签并尝试兜底取真实内容。
    # 现象：MiniMax-M3 主分析在 thinking disabled 但 prompt 极长时偶发产出
    #   reasoning="<think>…让我先梳理…</think>"（即整段都是 thinking 块），被 strip 后是空 → 兜底。
    # 修复：先剥 think 标签 → 仍空则从 reasoning_path 的 [总结] 段取 → 再空则用 asset_reasoning。
    _think_re = re.compile(r"<think>.*?</think>", re.DOTALL)
    # v342 观测：登记这次 LLM 原始输出（长度 + 开头 80 字），供落库侧探针使用
    try:
        from common import llm_degrade_probe as _ldp_raw
        _ldp_raw.note_raw(cleaned)
    except Exception:
        pass
    def _strip_think(s: str) -> str:
        s = _think_re.sub("", s or "")
        s = re.sub(r"</?think[^>]*>", "", s).strip()
        return s
    for _k in ("reasoning", "asset_reasoning", "asset_nature", "invalidation_condition"):
        _v = str(result.get(_k) or "")
        if "<think>" in _v or "<think" in _v or _v.lstrip().startswith("<think>"):
            result[_k] = _strip_think(_v)
    _rsn0 = str(result.get("reasoning") or "").strip()
    if len(_rsn0) < 4:
        # 兜底 1：从 reasoning_path 的 [总结] 段取（如果 LLM 给了完整八段推理）
        _rp0 = str(result.get("reasoning_path") or "")
        _sm = re.search(r"\[总结\]\s*([^\n]+)", _rp0)
        if _sm:
            _sm_text = _sm.group(1).strip()
            if len(_sm_text) >= 4 and "兜底" not in _sm_text:
                result["reasoning"] = _sm_text[:200]
        # 兜底 2：用 asset_reasoning（如果已 sanitize 后 >=4）
        if len(str(result.get("reasoning") or "").strip()) < 4:
            _ar = str(result.get("asset_reasoning") or "").strip()
            _ar_clean = re.sub(r"^[^:：]+[:：]\s*(服务端兜底.*)$", "", _ar).strip()
            if len(_ar_clean) >= 4:
                result["reasoning"] = _ar_clean[:200]

    # 1) reasoning_path：min 100 字符。空/短 → 用 v176.2 [总框架] + [因素定位] + [总结] 模板占位
    # v176.2：100 字符的阈值让 LLM 输出"XAU: 服务端兜底"（~30 字符）被无端覆盖。
    # 改为：只在完全空（<10 字符）/解析失败 时覆盖；>=10 字符视为 LLM 真实输出，保留。
    rp = str(result.get("reasoning_path") or "").strip()
    # v176.2.1：把 LLM 误读的"未注入因果图谱"字面文本替换为真实情况——
    # 即使 graph_block 已注入，LLM 仍输出口语化的"未注入"占位。覆盖成"因果图谱已注入"
    # （更准确反映注入事实），同时解除 100 字符阈值对该段的影响。
    if "未注入因果图谱" in rp:
        rp = rp.replace(
            "[总框架] 服务端兜底：未注入因果图谱",
            "[总框架] 服务端兜底：因果图谱已注入（但 LLM 未应用）"
        )
        rp = rp.replace(
            "[总框架] N/A | 服务端未注入因果图谱",
            "[总框架] 服务端兜底：因果图谱已注入（但 LLM 未应用）"
        )
        result["reasoning_path"] = rp
    if len(rp) < 10:
        ta = str(result.get("target_asset") or "NONE").upper()
        an = str(result.get("asset_nature") or "N/A").strip() or "N/A"
        rsn = str(result.get("reasoning") or "").strip()
        # v176.3 fix: 三段式兜底
        # 1) LLM 给了 10+ 字符的摘要 → 保留原文 + 追加兜底说明
        # 2) LLM 没给 reasoning_path 但给了 reasoning（真实判断） → 用 reasoning 作 [总结] 段，剩余段标注"未展开"
        # 3) 两者都没给 → 全段硬兜底（最后兜底）
        if rp and len(rp) >= 10:
            result["reasoning_path"] = (
                f"[LLM 摘要] {rp}\n"
                f"[兜底补全] LLM 输出了简短摘要，未展开八段推演；"
                f"置信度降低，{ta} 标记为 HOLD（数据不足）"
            )
        elif rsn and len(rsn) >= 4:
            # v176.6: 用 reasoning + asset_nature + action/score 等已 sanitize 字段，把八段填成
            # 有信息量的叙述，而不是"服务端自动补全：需人工复核"这类空占位。LLM 虽未展开八段，
            # 但给了 reasoning 摘要 —— 把它作为 [总结] 实体内容，其余段按事件类型/方向导出。
            an = str(result.get("asset_nature") or "").strip()
            ticker = str(result.get("news_ticker") or result.get("ticker") or "").strip()
            ev_code = str(result.get("event_type_code") or "").strip() or "market_event"
            impact = result.get("asset_impact") or result.get("impact_score")
            impact_s = f"{impact:.1f}/10" if isinstance(impact, (int, float)) else "未评估"
            news_summary = rsn[:140] if len(rsn) > 140 else rsn
            action = str(result.get("suggested_action") or "HOLD").strip().upper()
            score = result.get("sentiment_score")
            score_s = f"{score:+.2f}" if isinstance(score, (int, float)) else "未评估"
            direction = ("看多" if action in ("BUY", "LONG") else
                         "看空" if action in ("SELL", "SHORT") else "观望")
            ev_label = {
                "market_event": "市场事件",
                "macro_policy": "宏观政策",
                "geopolitical_conflict": "地缘冲突",
                "earnings": "财报/业绩",
                "liquidity_flow": "流动性",
                "sentiment_shift": "情绪转向",
                "supply_demand": "供需变化",
                # v170 P0+ 修复：原 "other": "其它事件" 会让历史脏数据继续显示"其它"
                # 现在的不可分类新闻已 step7 → 'low_value' + quarantined_at，
                # 这里给 low_value 一个明确标识，让用户/前端一眼看到"被隔离"。
                "low_value": "⚠️ 低价值（已隔离）",
            }.get(ev_code, ev_code)
            # v200 P3: 改中性措辞 — LLM 真输出（reasoning 摘要），不要再被前端标"无真实 AI"
            # 之前用「由 LLM 摘要归纳」「reasoning 摘要未展开」触发了前端 summary 类横幅，
            # 让用户误以为「没真实 AI 分析」。实际 LLM 给了真东西（45-80 字 reasoning），
            # 仅未展开八段。改写后用户看到「LLM 真东西摘要」即可明白。
            result["reasoning_path"] = (
                f"[总框架] 目标资产 {ta}；性质：{an or '未明'}；事件类型：{ev_label}；影响分 {impact_s}\n"
                f"[因素定位] 命中「{ev_label}」传导链（{ticker or ta}）；关键变量及其价格弹性见 [总结] 摘要\n"
                f"[驱动力] LLM 真东西摘要：{news_summary}（方向 {direction}）\n"
                f"[水位博弈] 盘面水位：未注入盘面快照（{ta} 监控品种外或数据通道受限），按中性处理不抢跑\n"
                f"[跨资产资金] 跨资产联动：LLM 摘要未展开多品种资金传导，按 LLM 单资产判断处理\n"
                f"[反共识结论] 独立判断：sentiment={score_s}，action={action}（{direction}）；反共识点见 [总结]\n"
                f"[逻辑自检] 自洽性：action/score 方向与服务端规则对齐；八段骨架已按 LLM 真东西铺开\n"
                f"[总结] {rsn}"
            )
        else:
            # v176.9 fix：全空兜底时，从 cleaned 文本（前一步剥离 <think> 后的 LLM 原文）
            # 里抽 80 字当 [总结] 占位 —— 至少不是完全空白，让用户能看到 LLM 实际产出了什么
            _act2 = str(result.get("suggested_action") or "HOLD").strip().upper()
            _mc2 = str(result.get("market_category") or "").strip().upper()
            _sc2 = result.get("sentiment_score")
            _sc2_s = f"{_sc2:+.2f}" if isinstance(_sc2, (int, float)) else "未评估"
            # 从 cleaned 末尾抓 80 字作为 LLM 真实输出片段（保留信息，不丢字）
            _raw_tail = cleaned.strip()
            # 截掉开头的 JSON 块（"{" 到对应 "}"）—— 只看 JSON 之后的内容
            _json_match = re.search(r"^.*?\}\s*", _raw_tail, re.DOTALL)
            if _json_match and _json_match.end() < len(_raw_tail):
                _raw_tail = _raw_tail[_json_match.end():].strip()
            # 也截掉 markdown code fence
            _raw_tail = re.sub(r"^```(?:json)?\s*|\s*```$", "", _raw_tail).strip()
            if len(_raw_tail) > 80:
                _raw_tail = _raw_tail[:80] + "…"
            _summary_line = _raw_tail if _raw_tail else f"{ta} rule_no_data_conclusion_{_act2}"
            # v176.12 fix：若抽出的尾巴是 token 截断的 JSON 残片（news_88=140 实测），重置为服务端兜底，
            # 避免前端展示 raw JSON 碎片。自然语言叙述不会带引号写这些字段键。
            if re.search(r'"(?:factor|rank|direction|impact_magnitude|factor_ranking|reasoning_path|suggested_action|target_asset)"', _summary_line):
                _summary_line = f"{ta} rule_no_data_conclusion_{_act2}"
            # v200 P1-B: 把"事件不在监控范围 → 强制 HOLD"硬套话换成有信息量的版本。
            # 背景: 此前 NONE 决策 89% 都被这串套话覆盖，用户反馈"分析说一些模型莫名其妙的话"。
            # 改进点:
            #   1) [总框架] 加入 ticker 和新闻真实摘要（如果有），不再纯说"事件不在监控范围"
            #   2) [因素定位] / [驱动力] 说明为什么是 NONE（标的不可交易/无明确标的/汇总类），而不是空洞否定
            #   3) [总结] 优先用 LLM 真实摘要(_raw_tail)，只在完全空时才用兜底
            #   4) 保留 HOLD 结论和置信度低说明（这些是有用的告警，不是套话）
            _ticker = str(result.get("news_ticker") or result.get("ticker") or "").strip()
            _ticker_tag = f"；涉及标的 {_ticker}" if _ticker else ""
            # 取 LLM 给的 reasoning（如有）作为 [总结] 内容，比纯兜底信息量大
            _rsn_fb = str(result.get("reasoning") or "").strip()
            if _rsn_fb and len(_rsn_fb) >= 4 and "服务端兜底" not in _rsn_fb:
                _summary_line = _rsn_fb[:140]
            # v336: 按"是否核心品种"分支说实话（旧实现一律写"不在核心监控品种"，
            # 导致 3692 条核心品种被误标成范围外 → 30.8% 的 HOLD 被误读成模型判断）。
            _in_scope = asset_in_core_scope(ta)
            result["reasoning_path"] = degraded_reasoning_template(
                asset=ta, market_category=_mc2, ticker_tag=_ticker_tag,
                action=_act2, score_s=_sc2_s, summary_line=_summary_line,
                in_scope=_in_scope,
            )
            if _in_scope and not str(result.get("no_trade_reason") or "").strip():
                # 喂给反思本体：让它归到"技术错误/源降级"，而不是"逻辑框架错误"
                result["no_trade_reason"] = "LLM 源降级：空输出/截断（非无方向判断）"

    # 2) asset_nature：min 4 字符 (v200 P0+: L2 业务知识兜底驱动列表, 非假字符串)
    _init_result_completeness(result)
    an = str(result.get("asset_nature") or "")
    if not an.strip() or len(an) < 4:
        ta = str(result.get("target_asset") or "NONE").upper()
        drivers = _fallback_business_knowledge_drivers(ta, allow_generic=False)
        if drivers:
            # L2: 用第一个驱动定性(非假字符串, 是真实业务知识)
            result["asset_nature"] = f"{ta} {drivers[0]}驱动属性"
            _mark_field_source(result, "asset_nature", "business_knowledge_causal_graph_v176_2")
        else:
            # L5 留空: 标记缺失, 不写假字符串
            _mark_field_missing(result, "asset_nature")

    # 3) asset_reasoning：min 4 字符 (v200 P0+: L5 留空, 绝不写"LLM 未填"假字符串)
    ar = str(result.get("asset_reasoning") or "")
    if not ar.strip() or len(ar) < 4:
        # X1 假字符串禁止: 不再写 f"{ta}: 服务端兜底（LLM 未填）"
        _mark_field_missing(result, "asset_reasoning")

    # 4) reasoning：min 4 max 200
    # v200 P0+: 缺时不写"LLM 未填"假字符串. 优先尝试 L3 历史模式, 其次 L5 留空.
    rsn = str(result.get("reasoning") or "").strip()
    if not rsn or len(rsn) < 2:
        # L3 历史模式兜底尝试(若无 conn 则跳过)
        ta = str(result.get("target_asset") or "NONE").upper()
        _hf = None
        try:
            _hf = _fallback_historical_reasoning(
                ta,
                str(result.get("event_type_code") or ""),
                str(result.get("suggested_action") or ""),
                None,  # _presanitize 阶段 conn 未传, 跳过 L3
            )
        except Exception:
            _hf = None
        if _hf and _hf.get("text"):
            result["reasoning"] = _hf["text"][:200]
            _mark_field_source(result, "reasoning", "historical_pattern")
            try:
                _refs = json.loads(result.get("historical_ref_ids") or "{}")
                if isinstance(_refs, dict):
                    _refs["reasoning"] = _hf.get("ref_decision_id")
                    result["historical_ref_ids"] = json.dumps(_refs, ensure_ascii=False)
            except (json.JSONDecodeError, TypeError):
                pass
        else:
            # L5 留空: 标记缺失, 不写假字符串
            _mark_field_missing(result, "reasoning")
    if len(result.get("reasoning") or "") > 200:
        result["reasoning"] = result["reasoning"][:197] + "..."

    # 5) invalidation_condition：min 4 max 100 (v200 P0+: L5 留空)
    # v200+ P2: LLM 留空时给真实事件级兜底（不是占位字符串），
    # 同时仍 mark_field_missing 让 L5 完整性扣分（标记 LLM 真实未输出）。
    ic = str(result.get("invalidation_condition") or "").strip()
    if len(ic) < 4:
        # X1 假字符串禁止: 不再写"服务端兜底：未填失效条件"
        _mark_field_missing(result, "invalidation_condition")
        # v200+ P2: 用 event_type_code 兜底出真实失效条件
        # (确保 ai_decisions.invalidation_condition 字段始终有值，便于复盘查阅)
        _fb_ic = _fallback_invalidation_condition(
            event_type_code=str(result.get("event_type_code") or ""),
            asset=str(result.get("target_asset") or ""),
            action=str(result.get("suggested_action") or ""),
        )
        result["invalidation_condition"] = _fb_ic[:100]
        # 标注 source 让 field_sources 区分 llm / rule_template (便于审计)
        try:
            _fs = json.loads(result.get("field_sources") or "{}")
            if not isinstance(_fs, dict):
                _fs = {}
            _fs["invalidation_condition"] = "rule_template"
            result["field_sources"] = json.dumps(_fs, ensure_ascii=False)
        except (json.JSONDecodeError, TypeError):
            pass
    if len(result.get("invalidation_condition") or "") > 100:
        result["invalidation_condition"] = result["invalidation_condition"][:97] + "..."

    # 6) factor_ranking：至少 1 个 item，每个 item 必含 factor/rank/direction
    fr = result.get("factor_ranking")
    if not isinstance(fr, list) or not fr:
        result["factor_ranking"] = _extract_factors_fallback(result)
    else:
        clean_fr = []
        rank_n = 0
        for it in fr:
            if not isinstance(it, dict):
                continue
            factor = str(it.get("factor") or "").strip()
            if _looks_placeholder(factor):
                continue
            if len(factor) < 2:
                continue
            rank_n += 1
            direction = str(it.get("direction") or "").strip()
            if direction not in ("利多", "利空", "中性"):
                direction = "中性"
            try:
                rank_v = int(it.get("rank") or rank_n)
            except (TypeError, ValueError):
                rank_v = rank_n
            if rank_v < 1:
                rank_v = 1
            clean_fr.append({"factor": factor[:200], "rank": rank_v, "direction": direction})
        if not clean_fr:
            clean_fr = _extract_factors_fallback(result)
        result["factor_ranking"] = clean_fr

    # 7) event_type_code：min 3 字符
    # v170 P0+ 修复：原 .get(mc, "other") 兜底导致 664 条 event_type_code='other'
    # 现在严禁兜底到 "other"。不可分类 → 'low_value' + quarantined_at（id_reconcile 隔离）
    if not str(result.get("event_type_code") or "").strip() or len(str(result.get("event_type_code") or "")) < 3:
        mc = str(result.get("market_category") or "").upper()
        _etc_default = {
            "CRYPTO": "market_event",
            "GOLD": "macro_policy",
            "OIL": "geopolitical_conflict",
            "STOCK": "earnings",
            "MACRO": "macro_policy",
            "COMMODITY": "supply_chain",
        }.get(mc, "low_value")
        result["event_type_code"] = _etc_default
        if _etc_default == "low_value":
            # 标记为低价值 + 隔离（与 user 反馈"不该出现其他兜底"对齐）
            from datetime import datetime as _dt
            result["quarantined_at"] = _dt.utcnow().isoformat(timespec="seconds") + "Z"
            result["trade_gate_reason"] = result.get("trade_gate_reason") or "low_value_no_classify"

    # 8) verification：min 4 字符 (v200 P0+: L5 留空, _auto_fill_required_fields 兜底)
    if len(str(result.get("verification") or "").strip()) < 4:
        # _auto_fill_required_fields 会用 rule_action_score_consistency 兜底
        # 这里只标记 missing, 不写假字符串
        _mark_field_missing(result, "verification")

    # 9) hard_data_evidence：min 4 字符 (v200 P0+: L5 留空)
    if len(str(result.get("hard_data_evidence") or "").strip()) < 4:
        # _auto_fill_required_fields 会用 rule_kline_snapshot 兜底
        # 这里只标记 missing, 不写假字符串
        _mark_field_missing(result, "hard_data_evidence")

    # 10) snapshot_proximity：必须是 number 或 string。"N/A" 字符串允许。
    sp = result.get("snapshot_proximity")
    if sp is None:
        result["snapshot_proximity"] = "N/A"
    elif isinstance(sp, bool):
        result["snapshot_proximity"] = "N/A"
    elif not isinstance(sp, (int, float, str)):
        result["snapshot_proximity"] = "N/A"

    # 10.5) data_anchor_ts：ISO-8601 严格匹配，否则填 "N/A"
    das = result.get("data_anchor_ts")
    if not isinstance(das, str):
        das = "" if das is None else str(das)
    das = das.strip()
    if das and das not in ("N/A", "AMBIGUOUS"):
        import re as _das_re
        if not _das_re.match(r"^(N/A|\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(Z|[+\-]\d{2}:?\d{2})?)$", das):
            # 模糊文本（如"今晚"/"刚刚"）→ 兜底 N/A
            das = "N/A"
    elif not das:
        das = "N/A"
    result["data_anchor_ts"] = das[:60]

    # 11) target_asset：必须落在 enum 内
    # v200 P1-A: 加 HK_STOCK（港股），让港股个股/财报不再被规范化到 NONE。
    # 背景:康方生物 09926.HK / 希音 00625.HK 等港股新闻此前全归 NONE 走兜底，
    # 89% 的 NONE 决策被"事件不在监控范围 → 强制 HOLD"套话覆盖。
    # 港股与美股/A股归类逻辑一致（都属 STOCK 类目），但单列 enum 以便未来做交易规则区分。
    _target_enum = ("BTC", "ETH", "SOL", "XAU", "XAG", "COPPER",
                    "WTI", "BRENT", "CN_STOCK", "US_STOCK",
                    "INDIVIDUAL_STOCK", "HK_STOCK", "SKHYNIX",
                    "DXY", "FX",  # v200 P1-F: 美元指数/外汇类（无直接交易通道，但信号不丢）
                    "US_BOND", "CN_BOND",
                    "NONE")
    ta = str(result.get("target_asset") or "").strip().upper()
    if ta not in _target_enum:
        ta_norm = ta.replace("/", "-").replace(" ", "")
        # v200 P1-E: 空字符串不再误命中第一个 enum（空串 in "BTC" 是 True 会落到 BTC）
        if not ta_norm:
            ta = "NONE"
        else:
            # v200 P1-F: 外汇代码（USD/MXN/EUR/JPY 等）→ FX 类目
            _fx_codes = ("USD", "EUR", "JPY", "GBP", "MXN", "CAD", "AUD",
                         "CNY", "CNH", "CHF", "NZD", "SGD", "HKD")
            if ta_norm in _fx_codes:
                ta = "FX"
            else:
                for ok in _target_enum:
                    if ok in ta_norm or ta_norm in ok:
                        ta = ok
                        break
                else:
                    ta = "NONE"
        result["target_asset"] = ta

    # 11b) v483 别名回退：模型判 NONE，但原文只有一个具体品种 → 只补标的，不改方向。
    # 必须“唯一且具体”：黄金+原油同时出现、或只提到“美股/个股”这类聚合桶时不动，
    # 避免把一条宏观新闻硬塞进某个品种。分析降级标记仍由 classify_analysis_degraded 决定。
    if str(result.get("target_asset") or "").upper() == "NONE" and cleaned:
        try:
            from target_resolver import resolve_specific_one as _resolve_specific_one
            _alias_target = _resolve_specific_one(cleaned)
        except Exception:
            _alias_target = None
        if _alias_target:
            result["target_asset"] = _alias_target
            result["asset_route"] = "alias_fallback"
            try:
                import json as _json
                _fs = _json.loads(result.get("field_sources") or "{}")
                if not isinstance(_fs, dict):
                    _fs = {}
                _fs["target_asset"] = "alias_fallback"
                result["field_sources"] = _json.dumps(_fs, ensure_ascii=False)
            except Exception:
                pass

    # 12) market_category：必须是 PROD enum；不可映射 → 写 unclassified_events（人工/规则补全）
    # v1 重构：删除原 _mc_map + "OTHER/MACRO" 兜底；改走 market_classifier.infer_market_category()
    # 该函数会抛 UnclassifiedError，调用方（_do_api_call 包层）捕获后入 unclassified_events。
    mc_in = str(result.get("market_category") or "").strip().upper()
    ta2 = str(result.get("target_asset") or "").strip().upper()
    try:
        result["market_category"] = infer_market_category(
            ta2, mc_in, layer="B_sanitize_step12"
        )
    except UnclassifiedError as e:
        # 把异常挂在 result 上，由 _do_api_call 兜底写入 unclassified_events 表
        # 同时把 market_category 设为临时空串（落库前的「待复核」标记），
        # 避免 ai_decisions 表出现非法值；这里用 "_UNCLASSIFIED_*" 形式以便后续审计定位。
        result["_unclassified_reason"] = e.reason
        result["_unclassified_layer"] = "B_sanitize_step12"
        result["_unclassified_llm_mc"] = e.llm_market_category
        result["_unclassified_asset"] = e.target_asset
        result["market_category"] = ""  # 待复核：留空让前端渲染徽章
        # 仍标 quarantined + low_value 走隔离路径（不会触发下单）
        from datetime import datetime as _dt2
        if not result.get("quarantined_at"):
            result["quarantined_at"] = _dt2.utcnow().isoformat(timespec="seconds") + "Z"
        result["trade_gate_reason"] = result.get("trade_gate_reason") or f"unclassified:{e.reason}"
        if not str(result.get("event_type_code") or "").strip() or \
           str(result.get("event_type_code") or "") == "other":
            result["event_type_code"] = "low_value"

    # 13) prediction_type / event_phase / market_confirmation / expected_horizon / event_strength 必填 enum
    _replace_str_enum = {
        "prediction_type": ("reversal", "continuation", "breakout"),
        "event_phase": ("early", "mid", "late"),
        "market_confirmation": ("positive", "negative", "unknown"),
        "expected_horizon": ("intraday", "1-3d", "1w+"),
        "event_strength": ("low", "medium", "high"),
    }
    for k, allowed in _replace_str_enum.items():
        v = str(result.get(k) or "").strip().lower()
        if v not in allowed:
            if k == "prediction_type":
                v = "continuation"
            elif k == "event_phase":
                v = "mid"
            elif k == "market_confirmation":
                v = "unknown"
            elif k == "expected_horizon":
                v = "intraday"
            elif k == "event_strength":
                try:
                    ai = float(result.get("asset_impact") or 0)
                except (TypeError, ValueError):
                    ai = 0.0
                if ai >= 7:
                    v = "high"
                elif ai >= 4:
                    v = "medium"
                else:
                    v = "low"
        result[k] = v

    # 14) heat_score / impact_score / asset_impact：必填 0-10 数字
    for k in ("heat_score", "impact_score", "asset_impact"):
        v = result.get(k)
        try:
            v_num = float(v if v is not None else 0)
        except (TypeError, ValueError):
            v_num = 0.0
        v_num = max(0.0, min(10.0, v_num))
        result[k] = v_num

    # 15) direct_catalyst：必须 bool
    dc = result.get("direct_catalyst")
    if not isinstance(dc, bool):
        result["direct_catalyst"] = str(dc).strip().lower() in ("true", "1", "yes")

    # 16) elasticity_window：必须是 {minutes, decay, peak_after_minutes} 三键
    ew = result.get("elasticity_window")
    if not isinstance(ew, dict):
        try:
            ew = json.loads(str(ew)) if isinstance(ew, str) and ew else {}
        except Exception:
            ew = {}
    if not isinstance(ew, dict):
        ew = {}
    try:
        minutes = int(ew.get("minutes", 60) or 60)
    except (TypeError, ValueError):
        minutes = 60
    decay = str(ew.get("decay", "medium")).strip().lower()
    if decay not in ("fast", "medium", "slow"):
        decay = "medium"
    try:
        peak = int(ew.get("peak_after_minutes", 30) or 30)
    except (TypeError, ValueError):
        peak = 30
    result["elasticity_window"] = {
        "minutes": max(0, minutes),
        "decay": decay,
        "peak_after_minutes": max(0, peak),
    }

    # 17) impact_curve：必须至少有 1 个键（5m/30m/2h/1d）
    ic = result.get("impact_curve")
    if not isinstance(ic, dict) or not ic:
        result["impact_curve"] = {"5m": 0.0, "30m": 0.0, "2h": 0.0, "1d": 0.0}
    else:
        _clean_ic = {}
        for k in ("5m", "30m", "2h", "1d"):
            if k in ic:
                v = ic[k]
                if isinstance(v, bool):
                    continue
                if v is None or isinstance(v, (int, float, str)):
                    _clean_ic[k] = v
        if not _clean_ic:
            _clean_ic = {"5m": 0.0, "30m": 0.0, "2h": 0.0, "1d": 0.0}
        result["impact_curve"] = _clean_ic

    # 18) event_window_minutes：必须 int 0-4320 或 'N/A'
    ewm = result.get("event_window_minutes")
    if isinstance(ewm, bool):
        result["event_window_minutes"] = 60
    elif isinstance(ewm, int):
        result["event_window_minutes"] = max(0, min(4320, ewm))
    elif isinstance(ewm, float):
        result["event_window_minutes"] = max(0, min(4320, int(ewm)))
    elif isinstance(ewm, str):
        s = ewm.strip()
        if s in ("", "N/A"):
            result["event_window_minutes"] = "N/A"
        else:
            try:
                result["event_window_minutes"] = max(0, min(4320, int(float(s))))
            except ValueError:
                result["event_window_minutes"] = 60
    else:
        result["event_window_minutes"] = 60

    return result


def _record_unclassified_event(
    news_id: int,
    *,
    reason: str,
    layer: str,
    target_asset: str,
    llm_market_category: str,
    title: str = "",
    source: str = "",
    news_published_at: str = "",
) -> None:
    """把一条「无法归类」的事件写入 unclassified_events 表，供人工/规则补全。

    调用方（_sanitize_llm_result 后的所有 caller）都应该传 news_id 进来；
    news_id=0 表示「上下文丢失」，仅用于兜底写日志。

    title/source/news_published_at 若 caller 没传，会从 raw_news 表按 news_id 补全，
    这样数据迁移脚本能直接 SELECT 出待复核清单给人工处理。
    """
    try:
        import db as _db_uc
        conn = _db_uc.get_connection()
        try:
            # 缺字段时按 news_id 从 raw_news 补全
            if news_id and (not title or not source):
                try:
                    _row = conn.execute(
                        "SELECT title, source, published_at FROM raw_news WHERE id = ?",
                        (int(news_id),),
                    ).fetchone()
                    if _row:
                        title = title or (_row["title"] if "title" in _row.keys() else "") or ""
                        source = source or (_row["source"] if "source" in _row.keys() else "") or ""
                        news_published_at = news_published_at or (
                            _row["published_at"] if "published_at" in _row.keys() else ""
                        ) or ""
                except Exception:
                    pass
            conn.execute(
                """
                INSERT INTO unclassified_events (
                    created_at, news_id, title, source, published_at,
                    target_asset, llm_market_category, llm_target_asset,
                    reason, layer, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
                """,
                (
                    datetime.utcnow().isoformat(timespec="seconds") + "Z",
                    int(news_id or 0),
                    (title or "")[:500],
                    (source or "")[:128],
                    (news_published_at or "")[:32],
                    target_asset or "",
                    llm_market_category or "",
                    target_asset or "",  # llm_target_asset 同 target_asset（重构前同一字段）
                    reason or "UNKNOWN",
                    layer or "",
                ),
            )
            conn.commit()
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except Exception as _e:
        # 人工复核队列落库失败只记日志，不能影响主链路
        print(
            f"  [unclassified] record failed (reason={reason}, layer={layer}, "
            f"news_id={news_id}): {type(_e).__name__}: {str(_e)[:120]}",
            flush=True,
        )


def _consume_unclassified_marker(
    result: Dict[str, Any],
    news_id: int,
    *,
    title: str = "",
    source: str = "",
    news_published_at: str = "",
) -> None:
    """检查 result 上挂的 _unclassified_* 标记，若存在则写入 unclassified_events。"""
    if not isinstance(result, dict):
        return
    if not result.get("_unclassified_reason"):
        return
    # v323：news_id 取不到时（result 里没有 news_id，调用方传 None）原本会写 news_id=0。
    # 实测全表 11,357 条里 10,208 条是 news_id=0 —— 这种记录回溯不到任何一条新闻，
    # 既不能复核也不能补全，只会把表撑大并额外抢写锁。直接不写。
    try:
        if not int(news_id or 0):
            return
    except (TypeError, ValueError):
        return
    _record_unclassified_event(
        news_id=news_id,
        reason=str(result.get("_unclassified_reason") or "UNKNOWN"),
        layer=str(result.get("_unclassified_layer") or "?"),
        target_asset=str(result.get("_unclassified_asset") or ""),
        llm_market_category=str(result.get("_unclassified_llm_mc") or ""),
        title=title,
        source=source,
        news_published_at=news_published_at,
    )
    # 写完清理标记，避免重复写
    for k in ("_unclassified_reason", "_unclassified_layer",
              "_unclassified_llm_mc", "_unclassified_asset"):
        result.pop(k, None)


# _derive_cross_net_from_noi 已外迁 engine.worker_helpers（import * 接回）


def _sanitize_llm_result(result: Dict[str, Any]) -> Dict[str, Any]:
    """整箱白名单过滤 LLM 结果 —— 幻觉兜底，绝不整条丢弃新闻。

    对 LLM 返回的 dict 逐字段做白名单/夹取/补默认：
      - 缺失键补安全默认，保证下游任何 res["key"] 直取都不会 KeyError；
      - 类型非法(数字给了字符串、资产给了列表等)按白名单净化或回落；
      - 数值越界夹取到合法区间。
    方向与分数符号的一致性不在此判定（交由 reconcile_score_action /
    _norm_score_action），避免重复实现两套口径。

    v200 P0+: 入口处初始化 completeness/source, 标记 LLM 真输出的字段为 source="llm".
    """
    if not isinstance(result, dict):
        result = {}

    # ── v200 P0+: 初始化 completeness/source/missing ──
    _init_result_completeness(result)
    _LLM_TRACKED_FIELDS = (
        "reasoning", "reasoning_path", "asset_reasoning", "asset_nature",
        "factor_ranking", "invalidation_condition", "verification",
        "hard_data_evidence", "data_anchor_ts", "elasticity_window",
        "low_impact", "attention_score", "impact_magnitude",
        # v343: evidence_confidence 加进跟踪列表 —— 它是选股硬闸门的输入，
        # 之前既不在这里也不在 schema 里，导致"有没有填"从来没被记录。
        "evidence_confidence",
    )
    for _f in _LLM_TRACKED_FIELDS:
        _v = result.get(_f)
        # 非空且不是服务端兜底标记 → 视为 LLM 真输出
        if _v is not None and str(_v).strip() and "服务端兜底" not in str(_v):
            _mark_field_source(result, _f, "llm")

    # sentiment_score：兼容三档范围
    #   (a) 0-100 整数（新约定，期望；50=中性, 0-30=偏 SELL, 70-100=偏 BUY）
    #   (b) 0-1 float（部分老 LLM 输出，0=空, 0.5=中, 1=极多）
    #   (c) -1~1 float（最早约定，负数=SELL，正数=BUY）
    # 内部统一为 -1~1 后存到 DB：所有下游 decision_guard/quick_sim 兼容 0 改动。
    result["sentiment_score"] = _normalize_sentiment_score(result.get("sentiment_score", 0.0))

    # suggested_action：白名单；缺省/非法 → HOLD
    _act = result.get("suggested_action")
    action = (_act if isinstance(_act, str) else "").strip().upper()
    result["suggested_action"] = action if action in _SCORE_ACTIONS else "HOLD"

    # reasoning / reasoning_path：仅接受字符串，其余滤空（展示截断由调用点决定）。
    # v176.7：MiniMax-M3 在 thinking disabled 下会把 reasoning_path 输出成嵌套 dict
    # （step1_fact_check/step2_... 结构）而非字符串八段式，直接滤空会触发
    # _presanitize_for_schema 的"LLM 未输出八段"兜底 → 前端整段变"服务端兜底：LLM 未填 reasoning"。
    # 这里先把 dict/list 展平成可读文本再过滤（与 factor_ranking 的 dict 兼容对齐）。
    _rp = result.get("reasoning_path")
    if isinstance(_rp, dict):
        _segs = []
        for _k, _v in _rp.items():
            if isinstance(_v, (dict, list, tuple)):
                _v = json.dumps(_v, ensure_ascii=False)
            _segs.append(f"[{_k}] {str(_v).strip()}")
        result["reasoning_path"] = "\n".join(_segs)
    elif isinstance(_rp, (list, tuple)):
        result["reasoning_path"] = "\n".join(str(x) for x in _rp)
    elif isinstance(_rp, str):
        result["reasoning_path"] = _rp
    else:
        result["reasoning_path"] = ""

    # reasoning：仅接受字符串；空/非法时用展平后的 reasoning_path 首段作摘要，
    # 避免 reasoning 与 reasoning_path 同时落空导致双重兜底。
    _rsn = result.get("reasoning")
    if isinstance(_rsn, str) and _rsn.strip():
        result["reasoning"] = _rsn
    else:
        _rp_flat = (result.get("reasoning_path") or "").strip()
        if _rp_flat:
            _first = re.sub(r"^\[[^\]]+\]\s*", "", _rp_flat.split("\n")[0]).strip()
            result["reasoning"] = _first if len(_first) >= 4 else ""
        else:
            result["reasoning"] = ""

    # target_asset：仅接受字符串，非空 → 大写截断；否则 NONE（后续再归一主品种）
    _ta = result.get("target_asset")
    target = (_ta if isinstance(_ta, str) else "").strip().upper()[:20]
    result["target_asset"] = target or "NONE"

    # market_category：白名单 + 配对校验（v1 重构）
    # 不再 fallback 到 MACRO 或保留 OTHER；全部走 market_classifier.validate_pair。
    # 这一步是 _sanitize 的最后兜底，理论上前一步 step12 已经把一切非法值清掉了；
    # 留作 belt-and-suspenders，避免 LLM 兜底串从 step12 之后才介入（race window）。
    _mc = result.get("market_category")
    cat = (_mc if isinstance(_mc, str) else "").strip().upper()
    # v323 修复：模型经常把 market_category 留空（实测一天写进 unclassified_events 逾万条，
    # 原因全是 INVALID_AT_STEP5: 空分类），但 target_asset 已经明确给了 BTC/XAU/WTI 之类。
    # 分类本来就是"资产→分类"的确定性映射（market_classifier.TARGET_TO_MARKET 是单一真源），
    # 不该因为模型没填这个字段就把整条分析丢弃。这里在资产明确时用真源补分类。
    if not cat and result.get("target_asset"):
        cat = str(TARGET_TO_MARKET.get(str(result["target_asset"]).strip().upper(), "") or "").strip().upper()
    _ta_s = str(result.get("target_asset") or "").strip().upper()
    if cat in MARKET_CATEGORIES_PROD or (cat == "OTHER" and is_debug_other_allowed()):
        try:
            validate_pair(_ta_s, cat)
            result["market_category"] = cat
        except UnclassifiedError as e:
            # Layer B 已经挂过 _unclassified_* 标记的，这里只更新 layer 名（最新层优先）
            result["_unclassified_reason"] = e.reason
            result["_unclassified_layer"] = "C_sanitize_step5"
            result["market_category"] = ""
    else:
        # 不在 PROD 白名单且非 debug → 留空，由 _do_api_call 兜底入 unclassified_events
        result["_unclassified_reason"] = f"INVALID_AT_STEP5:{cat}"
        result["_unclassified_layer"] = "C_sanitize_step5"
        result["market_category"] = ""
        if not result.get("quarantined_at"):
            from datetime import datetime as _dt4
            result["quarantined_at"] = _dt4.utcnow().isoformat(timespec="seconds") + "Z"

    # direct_catalyst：布尔白名单（容错 true/false 或字符串 true/1/yes）
    dc = result.get("direct_catalyst")
    if not isinstance(dc, bool):
        dc = str(dc).strip().lower() in ("true", "1", "yes")
    result["direct_catalyst"] = dc

    # ── 八步推演新增字段（阶段1）：资产性质 / 因素排行 / 影响数量级 ──
    # v323 修复：模型常把 asset_nature 返回成嵌套对象（{"属性":"避险","理由":...}），
    # 原代码 `if isinstance(str) else ""` 直接把内容清空 → 空字段 → 整条分析被判 schema
    # 失败丢弃。这里改成"能读就读出来"：对象/数组展平成文本，只有真的没有才留空。
    _an = result.get("asset_nature")
    if isinstance(_an, str):
        result["asset_nature"] = _an.strip()
    elif isinstance(_an, dict):
        result["asset_nature"] = "；".join(
            f"{k}:{v}" if not isinstance(v, (dict, list)) else f"{k}:{json.dumps(v, ensure_ascii=False)}"
            for k, v in _an.items()
        )[:200]
    elif isinstance(_an, (list, tuple)):
        result["asset_nature"] = "；".join(str(x) for x in _an)[:200]
    else:
        result["asset_nature"] = ""

    # factor_ranking：schema 要求的是**数组**。原代码把 list 用 json.dumps 转成字符串
    # → 类型就不对了。这里保持 list；字符串形态则尝试解析回数组，解析不了才弃用。
    _fr = result.get("factor_ranking")
    if isinstance(_fr, list):
        result["factor_ranking"] = _fr
    elif isinstance(_fr, dict):
        result["factor_ranking"] = [_fr]
    elif isinstance(_fr, str) and _fr.strip():
        try:
            _parsed_fr = json.loads(_fr)
            result["factor_ranking"] = _parsed_fr if isinstance(_parsed_fr, list) else [_parsed_fr]
        except Exception:
            result["factor_ranking"] = [{"factor": _fr.strip()[:40], "rank": 1, "direction": "中性"}]
    else:
        result["factor_ranking"] = []

    # ── v174 新增三字段规范化 ──
    # event_type_code: 必须从 dim_event_type 25 个选 1, LLM 自由字符串会被 schema 拒绝
    _etc = result.get("event_type_code")
    result["event_type_code"] = _etc if isinstance(_etc, str) else ""

    # verification: 跨字段自检字符串, 限制 ≤500
    _ver = result.get("verification")
    if isinstance(_ver, str):
        result["verification"] = _ver[:500]
    else:
        result["verification"] = ""

    # snapshot_proximity: 数字或 "N/A" 字符串
    _sp = result.get("snapshot_proximity")
    if _sp is None:
        result["snapshot_proximity"] = ""
    elif isinstance(_sp, str):
        result["snapshot_proximity"] = _sp[:32]
    else:
        try:
            _sp_num = float(_sp)
            result["snapshot_proximity"] = str(int(_sp_num)) if _sp_num.is_integer() else str(_sp_num)
        except (TypeError, ValueError):
            result["snapshot_proximity"] = "N/A"

    try:
        _im = float(result.get("impact_magnitude", 0.0))
    except (TypeError, ValueError):
        _im = 0.0
    result["impact_magnitude"] = max(0.0, min(100.0, _im))

    # ── 需求5 新增子模块输出（关注度得分 / 弹性窗 / 原始数据戳 / 硬数据截图）──
    try:
        _atn = float(result.get("attention_score", 0.0))
    except (TypeError, ValueError):
        _atn = 0.0
    result["attention_score"] = max(0.0, min(100.0, _atn))

    _ew = result.get("elasticity_window")
    if isinstance(_ew, (dict, list)):
        result["elasticity_window"] = json.dumps(_ew, ensure_ascii=False)
    elif isinstance(_ew, str):
        result["elasticity_window"] = _ew
    else:
        result["elasticity_window"] = ""

    _das = result.get("data_anchor_ts")
    result["data_anchor_ts"] = _das if isinstance(_das, str) else ""
    _hde = result.get("hard_data_evidence")
    result["hard_data_evidence"] = _hde if isinstance(_hde, str) else ""

    # ── v176 服务端兜底：5 个字段 LLM 经常留空/填 0，sanitize 阶段强制填有意义默认值 ──
    # 1) low_impact：bool (0/1)，根据 action/score/asset_impact 反推
    try:
        _li = result.get("low_impact")
        if _li is None or _li == "":
            try:
                _ai = float(result.get("asset_impact", 0) or 0)
            except (TypeError, ValueError):
                _ai = 0.0
            try:
                _sc = abs(float(result.get("sentiment_score") or result.get("score") or 0))
            except (TypeError, ValueError):
                _sc = 0.0
            # asset_impact<=3 或 score<0.10 → 标记低影响（不入激进建仓池）
            result["low_impact"] = 1 if (_ai <= 3 or _sc < 0.10) else 0
        else:
            result["low_impact"] = 1 if str(_li).strip().lower() in ("1", "true", "yes") else 0
    except Exception:
        result["low_impact"] = 0

    # 2) attention_score：REAL [0,100]，LLM 经常空 → 根据 source_credibility × event_type 推断
    try:
        _atn = float(result.get("attention_score", -1.0))
    except (TypeError, ValueError):
        _atn = -1.0
    if _atn <= 0:
        try:
            _sc2 = abs(float(result.get("sentiment_score") or result.get("score") or 0))
            _ai2 = float(result.get("asset_impact", 0) or 0)
            _das2 = str(result.get("data_anchor_ts") or "").strip()
            # v176.2 调整: 基础分 = 评分×60 + impact×4 + 数据锚+5 + event_type+8
            # 兜底最低 25 (不再 < 5)，让 attention_score 真正反映"已分析的决策"
            _base = _sc2 * 60 + _ai2 * 4
            if _das2 and _das2 not in ("", "N/A", "AMBIGUOUS"):
                _base += 5  # 有数据锚说明事件可追溯
            if str(result.get("event_type_code") or "").strip():
                _base += 8
            _base = max(_base, 25.0)  # v176.2 兜底保底 25
            result["attention_score"] = max(0.0, min(100.0, round(_base, 1)))
            # v200 P0+: L1 数学规则兜底, 打 source 标记
            _mark_field_source(result, "attention_score", "rule_attention_v176_2")
        except Exception:
            result["attention_score"] = 25.0  # v176.2 默认 25 (从 0 改)
            _mark_field_source(result, "attention_score", "rule_default_attention_v176_2")

    # 3) impact_magnitude：REAL [0,100]，LLM 经常空 → 反推：score×100×asset_impact/10
    try:
        _im2 = float(result.get("impact_magnitude", -1.0))
    except (TypeError, ValueError):
        _im2 = -1.0
    if _im2 <= 0:
        try:
            _sc3 = abs(float(result.get("sentiment_score") or result.get("score") or 0))
            _ai3 = float(result.get("asset_impact", 0) or 0)
            _es = str(result.get("event_strength") or "medium").strip().lower()
            _es_mult = {"low": 0.5, "medium": 1.0, "high": 1.5}.get(_es, 1.0)
            _mag = _sc3 * 100 * max(_ai3, 1) / 10 * _es_mult
            # v323 同类修复：sentiment 常为 0，反推幅度会塌成 0；
            # 此时用 impact_score(0-10)/10 作为影响力兜底（与 signal_weight_evaluator 口径一致）。
            if _mag <= 0:
                try:
                    _imp3 = float(result.get("impact_score") or 0)
                except (TypeError, ValueError):
                    _imp3 = 0.0
                if _imp3 > 0:
                    _mag = _imp3 * 10 * _es_mult
            result["impact_magnitude"] = max(0.0, min(100.0, round(_mag, 1)))
            # v200 P0+: L1 数学规则兜底, 打 source 标记
            _mark_field_source(result, "impact_magnitude", "rule_impact_v176")
        except Exception:
            result["impact_magnitude"] = 0.0
            _mark_field_source(result, "impact_magnitude", "rule_default_impact_v176")

    # 4) verification：必填字符串，已在 _auto_fill_required_fields 有兜底；这里也补 (v200 P0+ L1 规则)
    if not str(result.get("verification") or "").strip():
        try:
            sa = (result.get("suggested_action") or "").upper()
            # v323 修复：这里读的键是 "score"，但分析结果里根本没有 score 字段
            # （只有 sentiment_score）→ 永远取到 0.0 → 凡是 BUY/SELL 都被判成
            # "动作与分数不一致" → 自检 warn → 强制 HOLD → 分数归零 → 过不了闸门。
            # 这就是"有方向却全变 HOLD、score 恒为 +0.00"的源头。
            sc4 = float(result.get("sentiment_score") or result.get("score") or 0.0)
            if (sa in ("LONG", "BUY") and sc4 > 0) or (sa in ("SHORT", "SELL") and sc4 < 0):
                result["verification"] = "pass | rule_action_score_consistent"
            else:
                result["verification"] = "warn | rule_action_score_inconsistent"
            _mark_field_source(result, "verification", "rule_action_score_consistency_v176_5")
        except Exception:
            # X1 假字符串禁止: 不再写"warn | 服务端兜底：字段解析失败"
            _mark_field_missing(result, "verification")

    # 5) hard_data_evidence：服务端拼 snapshot K 线兜底（与 _auto_fill_required_fields 同语义）
    if not str(result.get("hard_data_evidence") or "").strip():
        try:
            _ta2 = str(result.get("target_asset") or "").upper()
            if _ta2 and _ta2 not in ("NONE", "", "OTHER"):
                _ev = _build_evidence_for_asset(_ta2)
                if _ev:
                    result["hard_data_evidence"] = _ev[:500]
                    # v200 P0+: L1 数学规则兜底, 打 source 标记
                    _mark_field_source(result, "hard_data_evidence", "rule_kline_snapshot_v176_11")
        except Exception:
            pass

    # ── 全网溯源：AI 自由回答（自然语言），不做校验 ──
    _noi = result.get("net_origin_info")
    if not isinstance(_noi, str):
        _noi = ""
    result["net_origin_info"] = _noi.strip()[:200]

    # v176.11 fix：当 LLM 没填 cross_net_first_seen_ts 但在 net_origin_info 里写了
    #   "推断时间:YYYY-MM-DD HH:MM:SS"（典型 LLM 输出：来源+推断时间组合说明），
    #   反向抽取时间回填，让首页 NetLagBadge 从"灰色 未确认"升到"紫色 精确时间"。
    # 仅在没有现成 cn_ts 时尝试，避免覆盖 AI 已给出的精确首发时间。
    # source 兜底用整个 net_origin_info（>=4 字符过 _validate_cross_net_ts 第⑤层），
    # 因为 "财联社"/"路透社" 等中文机构名只有 3 字符，直接取它们会被 ≥4 字符校验拒收。
    _cn_ts_curr = str(result.get("cross_net_first_seen_ts") or "").strip()
    if not _cn_ts_curr:
        try:
            _noi_full = _noi.strip()[:200]  # 已经清洗过
            _ts_back, _src_back = _derive_cross_net_from_noi(_noi_full)
            if _ts_back:
                result["cross_net_first_seen_ts"] = _ts_back
        except Exception:
            pass
    # 来源兜底：与上面的时间回填**独立**执行（v317 fix）
    #
    # 旧写法把来源兜底嵌在"时间回填"分支内（只有 cn_ts 为空时才走），
    # 于是 _extract_cross_net_from_text 已经填好 cn_ts、但没匹配到机构名时
    # （noi 形如 "推断时间:2026-10-11 08:07:00"），cn_src 永远是空 →
    # _validate_cross_net_ts 第⑤层（source 需 ≥4 字符）拒收 → **时间与来源双双被清空**。
    # 实测 2268 行 net_origin_info 里明明有可用的时间，却全部落库为空。
    try:
        _cn_src_now = str(result.get("cross_net_source") or "").strip()
        if len(_cn_src_now) < 4:
            _, _src_back2 = _derive_cross_net_from_noi(_noi.strip()[:200])
            if _src_back2:
                result["cross_net_source"] = _src_back2
    except Exception:
        pass

    # ── 全网溯源：结构化字段（前端徽章三态用）──
    # 这里只做"清洗+轻校验"，重活（5 层防护）由 _validate_cross_net_ts 在 ai_decisions
    # INSERT 后做。sanitize 只负责非字符串 → 空串 / 长串截断 / 不让污染 ai_decisions INSERT。
    _cn_ts = result.get("cross_net_first_seen_ts")
    if isinstance(_cn_ts, str):
        result["cross_net_first_seen_ts"] = _cn_ts.strip()[:32]
    else:
        result["cross_net_first_seen_ts"] = ""

    _cn_src = result.get("cross_net_source")
    if isinstance(_cn_src, str):
        result["cross_net_source"] = _cn_src.strip()[:300]
    else:
        result["cross_net_source"] = ""

    return result


# ────────────────────────────────────────────────────────────────────────────
# 全网溯源：5 层校验（防 AI 幻觉时间污染 raw_news）
# ────────────────────────────────────────────────────────────────────────────
# 设计要点：
#   * 三态：精确时间串（"YYYY-MM-DD HH:MM:SS"）/ "AMBIGUOUS" / ""（拒绝）
#   * 5 层防护 ①格式正则 ②下限1990 ③不能未来(±60s) ④不能晚于入库(±60s) ⑤source ≥4字符且
#     AMBIGUOUS 必须含"候选1"或"/"或"或"（两个并列表达）
#   * 任一层失败 → 返回 ""（视为无溯源信息，前端不渲染徽章）
#   * 校验通过 → 标准化为 "YYYY-MM-DD HH:MM:SS"（即使输入 "T" 分隔也归一为空白分隔）

import re as _re_cross

_CN_TS_RE = _re_cross.compile(r"^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})$")


def _validate_cross_net_ts(value, source, our_ts_iso):
    """结构化全网溯源时间 5 层校验。

    参数：
      value      — AI 给出的 cross_net_first_seen_ts（可能是 None/空串/精确串/"AMBIGUOUS"）
      source     — AI 给出的 cross_net_source（依据字符串）
      our_ts_iso — 本条新闻我们入库时间（"YYYY-MM-DD HH:MM:SS" 格式；用于第④层）

    返回：
      校验通过：标准化后的精确时间串 "YYYY-MM-DD HH:MM:SS" 或 "AMBIGUOUS"
      校验失败：""（前端不渲染徽章；不污染 DB）
    """
    if not value or not isinstance(value, str):
        return ""
    v = value.strip()
    if not v:
        return ""

    # AMBIGUOUS 分支：必须有"候选1 / 或"等并列表达；source 缺失或太短也拒
    if v == "AMBIGUOUS":
        src = (source or "").strip()
        if len(src) < 4:
            return ""
        # 必须有两个并列表达（候选1 / 候选2 或 "或" / "/"）
        if not ("候选1" in src or "/" in src or "或" in src):
            return ""
        return "AMBIGUOUS"

    # 精确时间分支：5 层
    m = _CN_TS_RE.match(v)
    if not m:
        return ""
    try:
        y, mo, d, h, mi, s = (int(x) for x in m.groups())
    except Exception:
        return ""

    # ②下限：不能早于 1990
    if (y, mo, d) < _CN_EARLIEST:
        return ""

    # ③不能未来：候选时间 > 当前时间 + 60s 容差 → 拒（防模型用未来时间）
    #   v317 fix：改用 time_utils.parse_ts_to_bj（北京时间唯一入口）。
    #   旧写法用 time.mktime((y,mo,d,...))，它按**本机 localtime** 解释裸元组 ——
    #   本机 TZ 若不是 +08:00，判定就会整体偏移 8 小时（实测机器 TZ=CST 恰好正确，
    #   但换台机器/换 TZ 就静默错判）。裸串按北京解读是项目统一约定（time_utils 文件头）。
    candidate_dt = time_utils.parse_ts_to_bj(f"{y:04d}-{mo:02d}-{d:02d} {h:02d}:{mi:02d}:{s:02d}")
    if candidate_dt is None:
        return ""
    if candidate_dt > datetime.now(time_utils.BJ) + timedelta(seconds=60):
        return ""

    # ④不能晚于自己入库时间 + 60s 容差 → 拒（事件不可能在入库后发生）
    #   v317 fix：同样走 time_utils.parse_ts_to_bj。
    #   旧写法 strptime(our_ts_iso[:19], "%Y-%m-%d %H:%M:%S") 对真实落库格式
    #   '2026-10-11T16:05:15+08:00'（T 分隔 + 偏移）**必然解析失败** → except 分支
    #   直接放行 → 这道"时间不能晚于入库"的护栏实际上一直处于**失效**状态
    #   （实测 319 行 cn_ts 晚于入库数小时仍被当作有效）。
    if isinstance(our_ts_iso, str) and our_ts_iso:
        our_dt = time_utils.parse_ts_to_bj(our_ts_iso)
        if our_dt is not None and candidate_dt > our_dt + timedelta(seconds=60):
            return ""

    # ⑤source 必须 ≥4 字符（精确时间必须有依据）
    src = (source or "").strip()
    if len(src) < 4:
        return ""

    # 标准化：T 分隔 → 空白分隔
    return f"{y:04d}-{mo:02d}-{d:02d} {h:02d}:{mi:02d}:{s:02d}"


# ---------------------------------------------------------------------------
# 全网溯源弱信号抽取（Fallback 2.5）— 中文文本正则兜底
# ---------------------------------------------------------------------------
# MiniMax-M3 等模型常输出 2000 字中文八段推演但**没有 JSON 包裹** → 上游 json.loads
# 拿不到任何结构化字段。本函数尝试从纯文本里抽"X 日宣布/报道"等模式，作为
# cross_net_first_seen_ts / cross_net_source / net_origin_info 的弱依据。
#
# 设计取舍（保守优先，避免污染 raw_news）：
#   - 时间：仅识别"Y年M月D日"+"HH:MM"组合，且映射到系统时间上下 30 天内
#     → 否则保守返回空（让 _validate_cross_net_ts 拒收）
#   - 来源：白名单 14 个高信度模式（"X 报道"/"X 宣布"/"X 官网新闻稿"等）
#   - net_origin_info：直接塞入"来源 + 推断时间"组合，最长 200 字符（DB 限制）
#
# 返回 (cn_ts, cn_src, noi)：三个都可能是空串，由调用方写进 result。
_CN_TS_PATTERNS = [
    # 2026年9月19日 22:00 / 2026年9月19日22时 / 2026年9月19日22时00分
    _re_cross.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日\s*(\d{1,2})\s*[::时]\s*(\d{1,2})(?:\s*分)?"),
    # 2026-09-19 22:00 / 2026/09/19 22:00
    _re_cross.compile(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})[ T](\d{1,2})[::时](\d{1,2})"),
    # 2026年9月19日（无时间） → 取 00:00
    _re_cross.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日"),
    # 9月19日 22:00 / 9月19日22时
    _re_cross.compile(r"(\d{1,2})\s*月\s*(\d{1,2})\s*日\s*(\d{1,2})\s*[::时]\s*(\d{1,2})"),
    # ── v176.9 fix：N月N日 不带时分（"金十数据9月23日讯"）→ 取 00:00 ──
    # 之前只匹配带时分的"X月X日 22:00"，对中文财经媒体最常见的"N月N日讯"格式漏抽
    # v176.10 fix：负向后瞻 (?<![年\d]) 排除"1989年1月1日"这类"年份+月日"里的"月日"
    #   独立补当前年——带年份的已由 pattern 0/2 处理（早于 1990 会被拒），不能被这里
    #   拆出的"1月1日"绕过限制变成"2026-01-01"。
    _re_cross.compile(r"(?<![年\d])(\d{1,2})\s*月\s*(\d{1,2})\s*日"),
]
# 字符类：中文 + "社"（让"新华社"整体作为机构名抓取） + 字母数字 + 中点/连字符
# 注意：必须包在 () 里构成捕获组，否则 m.groups() 为空导致 cn_src 取到整段 match
# v176.9 fix：源名专用字符类，**禁止数字**（避免"金十数据9月23日"贪婪匹配整段）
# org_chars 的 1:-1 是去掉外层 ()，得到字符类本体
_CN_SRC_PATTERNS = [
    # 高信度来源短语（捕获前面 2-12 字的机构名）
    _re_cross.compile(_CN_ORG_CHARS + r"\s*(?:官网|官方)\s*(?:新闻稿|公告|声明)"),
    # 排除"紧急/立即/即将"等中文动词，避免贪婪匹配"美联储紧急宣布"时吞进"紧急"
    _re_cross.compile(_CN_ORG_CHARS + r"(?!\s*(?:紧急|立即|即将|正在|已经|已\s+))(宣布|发布|公布|披露)"),
    _re_cross.compile(r"(?:据|根据)\s*(" + _CN_ORG_CHARS_NODIGIT[1:-1] + r")\s*(?:报道|消息|消息人士|新闻|快讯|通讯社|电)"),
    _re_cross.compile(r"(" + _CN_ORG_CHARS_NODIGIT[1:-1] + r")\s*(?:电|报道|消息)"),
    _re_cross.compile(r"(" + _CN_ORG_CHARS_NODIGIT[1:-1] + r")\s*官方网站"),
    _re_cross.compile(r"(" + _CN_ORG_CHARS_NODIGIT[1:-1] + r")\s*新闻稿"),
    _re_cross.compile(r"(" + _CN_ORG_CHARS_NODIGIT[1:-1] + r")\s*官方声明"),
    _re_cross.compile(r"(" + _CN_ORG_CHARS_NODIGIT[1:-1] + r")\s*(?:通讯社|电视台|广播|网)"),
    _re_cross.compile(r"(" + _CN_ORG_CHARS_NODIGIT[1:-1] + r")\s*Press"),
    # ── v176.9 fix：中文财经媒体"X讯/电"结尾（金十数据/财联社/东方财富等）──
    # 必须用 NODIGIT 版本，否则贪婪匹配"金十数据9月23日"整段
    # 允许 "金十数据9月23日讯" 这种 "机构名 + 日期 + 讯/电" 复合结构
    # v176.10 fix：org 用非贪婪 {2,12}?，避免 "[来自金十数据快讯]" 里"快"被吞进
    #   机构名（贪婪会得到"金十数据快"，不以"数据"结尾被 org_suffixes 拒收，
    #   导致 feishu_jin10 的"[来自金十数据快讯]"结尾漏抽首发平台）。
    _re_cross.compile(r"([\u4e00-\u9fa5社A-Za-z·\-]{2,12}?)\s*(?:\d{1,2}\s*月\s*\d{1,2}\s*日)?\s*(?:讯|电|消息|快讯)"),
    # 英文机构名（直接捕获）
    _re_cross.compile(r"(Reuters|Bloomberg|CNBC|Nikkei|Xinhua|CCTV)"),
]


def _extract_cross_net_from_text(cleaned: str) -> tuple:
    """从 LLM 输出的中文推演文本里抽 (cross_net_first_seen_ts, cross_net_source, net_origin_info)。

    返回三个值都可能是空串。失败时不抛异常（正则不匹配返回空 → 上游空串兜底）。
    """
    if not cleaned or not isinstance(cleaned, str):
        return "", "", ""

    # ── 时间抽取（先于来源抽取，便于下一步 mask 掉时间字符串防污染）──
    cn_ts = ""
    ts_mask_spans: list = []  # (start, end) — 时间匹配的范围，后续来源抽取时跳过
    try:
        import datetime as _dt_cn
        now = _dt_cn.datetime.now()
        for pat_idx, pat in enumerate(_CN_TS_PATTERNS):
            m = pat.search(cleaned)
            if not m:
                continue
            groups = m.groups()
            try:
                if pat_idx == 0 or pat_idx == 1:
                    y = int(groups[0]); mo = int(groups[1]); d = int(groups[2])
                    h = int(groups[3]); mi = int(groups[4]) if groups[4] else 0
                elif pat_idx == 2:
                    y = int(groups[0]); mo = int(groups[1]); d = int(groups[2])
                    h = 0; mi = 0
                elif pat_idx == 3:
                    # "9月19日22时" → 年补当前年（若跨年回退一年）
                    y = now.year
                    mo = int(groups[0]); d = int(groups[1])
                    h = int(groups[2]); mi = int(groups[3]) if groups[3] else 0
                    try:
                        cand = _dt_cn.datetime(y, mo, d)
                        if (cand - now).days > 30:
                            y = y - 1
                    except ValueError:
                        pass
                else:
                    # pat_idx == 4 (v176.9): "9月23日" 不带时分 → 年补当前年, h=mi=0
                    y = now.year
                    mo = int(groups[0]); d = int(groups[1])
                    h = 0; mi = 0
                    try:
                        cand = _dt_cn.datetime(y, mo, d)
                        if (cand - now).days > 30:
                            y = y - 1
                    except ValueError:
                        pass

                # 范围保护
                if y < 1990 or y > now.year + 1 or h > 23 or mi > 59 or mo < 1 or mo > 12 or d < 1 or d > 31:
                    continue
                # v176.10 fix：跳过"…以来(新高/新低/首次/低位/高位/纪录)"这类历史基准日期。
                # 它描述的是 KPI 对比的历史参照点，不是本条新闻的首发时间。金十常见正文
                # 里"创2025年10月8日以来新低"会覆盖标题"金十数据9月23日讯"的首发日期，
                # 导致 cross_net_first_seen_ts 误写成历史日期。跳过它，让更靠前/更贴首发
                # 的"N月N日"（如"9月23日"）胜出。
                if cleaned[m.end():m.end() + 2] == "以来":
                    continue
                cn_ts = f"{y:04d}-{mo:02d}-{d:02d} {h:02d}:{mi:02d}:00"
                ts_mask_spans.append((m.start(), m.end()))
                break
            except (ValueError, TypeError):
                continue
    except Exception:
        cn_ts = ""

    # ── 来源抽取（在 mask 掉时间字符串的 cleaned 上做）──
    cn_src = ""
    masked = cleaned
    for s, e in ts_mask_spans:
        masked = masked[:s] + (" " * (e - s)) + masked[e:]
    for pat in _CN_SRC_PATTERNS:
        m = pat.search(masked)
        if not m:
            continue
        groups = m.groups()
        if groups:
            candidates = [g for g in groups if g and len(g.strip()) >= 2]
        else:
            # 英文机构名（无捕获组）→ 用整段 match
            candidates = [m.group(0)] if m.group(0) and len(m.group(0)) >= 4 else []
        if not candidates:
            continue
        cn_src = max(candidates, key=len).strip()
        # 过滤助词/虚词（开头包含"据/根据"也要清掉）
        if cn_src in ("据", "根据", "官方", "今日", "刚刚", "消息人士"):
            cn_src = ""  # v176.9 fix：continue 前清空 cn_src，避免错误候选污染后续 pattern
            continue
        if cn_src.startswith(("据", "根据")):
            # "据路透社" → 去掉前缀取"路透社"
            cn_src = cn_src.lstrip("据根据").strip()
            if not cn_src or len(cn_src) < 2:
                cn_src = ""
                continue
        # v176.10 fix："[来自金十数据快讯]" 里 org 会连"来自"一起捕获成"来自金十数据"，
        # 剥掉"来自/来源"前缀，还原首发平台名（金十数据）。
        if cn_src.startswith(("来自", "来源")):
            cn_src = cn_src[2:].strip()
            if not cn_src or len(cn_src) < 2:
                cn_src = ""
                continue
        # 去掉尾部时间/前缀词残留（"沙特能源部今日" → "沙特能源部"）
        for tail in ("今日", "刚刚", "官方", "今天", "昨日", "本周", "本月"):
            if cn_src.endswith(tail):
                cn_src = cn_src[:-len(tail)].strip()
        # 必须不全是数字（过滤纯数字残留）和不含"年/月/日/时/分"（防时间污染）
        if cn_src.isdigit():
            cn_src = ""
            continue
        if any(k in cn_src for k in ("年", "月", "日", "时", "分")):
            cn_src = ""
            continue
        # 必须以高信度机构后缀结尾（社/网/台/局/部/院/所/银行/公司/委员会/联储/央行/通讯社），
        # 否则容易吞进 "美联储紧急/沙特能源部今日" 这种含动词的伪机构名
        # v176.9 fix：加 "数据"（金十数据/财联社数据等中文财经媒体后缀）
        org_suffixes = (
            "社", "网", "台", "局", "部", "院", "所", "行", "公司",
            "委员会", "联储", "央行", "通讯社", "通社", "新闻", "News",
            "Press", "Reuters", "Bloomberg", "CNBC", "Nikkei", "Xinhua", "CCTV",
            "数据",  # 金十数据/财联社数据/东方财富数据
        )
        if not cn_src.endswith(org_suffixes):
            cn_src = ""  # v176.9 fix：reject 后清空，bug 修复前会保留错误候选污染最终结果
            continue
        if len(cn_src) >= 4:
            break

    # ── net_origin_info 拼装 ──
    noi = ""
    if cn_src or cn_ts:
        parts = []
        if cn_src:
            parts.append(f"来源:{cn_src}")
        if cn_ts:
            parts.append(f"推断时间:{cn_ts}")
        noi = " | ".join(parts)[:200]

    return cn_ts, cn_src, noi


# ---------------------------------------------------------------------------
# Optional imports — not required; kept for compatibility
# ---------------------------------------------------------------------------

try:
    from openai import AsyncOpenAI as _AsyncOpenAI  # type: ignore[import-untyped]
    HAS_OPENAI = True
except ImportError:
    HAS_OPENAI = False


def attribute_5m_abnormal(asset: str, anchor_ts: int, pct_change: Optional[float],
                          vol_ratio: Optional[float], trigger_type: str,
                          news_before: List[Dict[str, Any]]) -> Dict[str, Any]:
    """3 分钟异动冷归因：判断异动与时间锚附近公开新闻的关联（轻量，不叠加八段式主 prompt）。

    只引用传入的公开新闻列表 + 公开常识；无法确定就列多个候选并标 speculative=True，
    严禁编造新闻标题/来源/数字。返回：
      {"summary": str, "candidates": [{"cause","source","confidence","speculative"}]}
    任何失败返回 {"error": str}——调用方静默降级（星标仍显示、归因留空），绝不阻塞主链路。

    v187: 优先走 anomaly 通道（独立 key + minimax-anomaly），按 purpose 选池。
    """

    cfgs: List[Dict[str, Any]] = []
    # v187：按 anomaly purpose 取池（替代原全 ROSTER 循环）
    try:
        for m in config.get_pool_for_purpose("anomaly"):
            try:
                c = _resolve_model_cfg(m)
                if c:
                    c["key_alias"] = m.get("key_alias", "unknown")
                    cfgs.append(c)
            except Exception:
                continue
    except Exception:
        pass
    # 兜底：保留旧的全 roster 路径(向后兼容)
    if not cfgs:
        for m in AI_MODEL_ROSTER:
            try:
                c = _resolve_model_cfg(m)
                if c:
                    cfgs.append(c)
            except Exception:
                continue
        mm = _minimax_fallback_cfg()
        if mm:
            cfgs.append(mm)
        mm2 = _minimax_fallback_cfg2()
        if mm2:
            cfgs.append(mm2)
    if not cfgs:
        return {"error": "no model available"}

    if news_before:
        news_lines = "\n".join(
            f"[{i + 1}] {n.get('ts_str', '')} | {n.get('source', '')} | {n.get('content', '')}"
            for i, n in enumerate(news_before[:12])
        )
    else:
        news_lines = "（时间锚前窗口内无该品种公开新闻记录）"

    # 图谱取证：注入该品种涨跌驱动本质（causal_graph 自学习知识），辅助反硬归因。
    # 图谱无此品种知识时返回空串，静默跳过（不阻塞归因）。
    graph_block = ""
    try:
        import causal_graph
        graph_block = causal_graph.inject_block(asset)
        factor_ontology.log_injection(asset, graph_block, mode="attribute")
    except Exception:
        graph_block = ""

    ts_str = datetime.fromtimestamp(int(anchor_ts)).strftime("%Y-%m-%d %H:%M:%S")
    pct_s = f"{pct_change:+.2f}%" if pct_change is not None else "未知"
    vol_s = f"{vol_ratio:.2f}×" if vol_ratio is not None else "未知"
    trig_map = {"pct": "单根涨跌幅超阈", "volume": "成交量放大超阈", "both": "涨跌+量能同时超阈"}
    trig = trig_map.get(trigger_type, str(trigger_type or "未知"))

    system_prompt = (
        "你是严谨的宏观/加密市场归因助手。给你一个 3 分钟 K 线的异动点（品种/时间/涨跌/量比/触发类型）"
        "以及该时间锚之前一小段时间内的公开新闻列表，判断该异动最可能由哪条新闻/事件引起。\n\n"
        "硬性要求：\n"
        "0. 若输入含「品种涨跌本质」背景，优先用它判断异动是否由该品种核心/条件驱动触发；"
        "时间窗内若无与该驱动直接相关的新闻，判为市场自发/技术性波动或未捕捉因素，严禁仅因时间就近就硬归因。\n"
        "1. 只引用「新闻列表」里出现的来源与内容，加上公开常识；不确定时列多个候选并按可能性排序。\n"
        "2. 严禁编造新闻标题、来源、具体数字；没有依据就写「未见直接催化，属市场自发波动」并标 speculative=true。\n"
        "3. 只输出一个 JSON 对象，不要任何解释性文字，格式严格为：\n"
        '{"summary":"一句话归因结论","candidates":[{"cause":"原因","source":"来源","confidence":"high|medium|low","speculative":false}]}\n'
        "candidates 最多 3 个，summary 不超过 60 字。"
    )
    user_text = (
        f"品种：{asset}\n异动时间：{ts_str}\n单根涨跌幅：{pct_s}\n量比：{vol_s}\n触发类型：{trig}\n\n"
        f"{graph_block + chr(10) if graph_block else ''}"
        f"时间锚前公开新闻：\n{news_lines}"
    )

    _tried_live = 0
    for cfg in cfgs:
        # v501：只跳过"这个 key 已 402"的通道，还活着的不动 —— 产能优先
        try:
            from common import llm_budget as _lbk
            if _lbk.quota_dead("key:%s" % cfg.get("key_alias", "unknown")):
                continue
            _tried_live += 1
        except Exception:
            _tried_live += 1
        payload: Dict[str, Any] = {
            "model": str(cfg.get("request_model") or cfg.get("id")),
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_text},
            ],
            "max_tokens": 400,
            "temperature": 0.1,
            **(cfg.get("extra_body") or {}),
        }
        if cfg.get("json_mode"):
            payload["response_format"] = {"type": "json_object"}
        headers = {
            "Authorization": f"Bearer {cfg.get('api_key')}",
            "Content-Type": "application/json",
        }
        url = f"{cfg.get('api_base')}/chat/completions"
        session = _LLM_PROXY_SESSION if cfg.get("use_proxy") else _LLM_SESSION

        # ── v500: 暂停态硬挡 —— 暂停期间这条路径仍在打 402 重试，
        #   每条新闻把通道池挨个试一遍，纯烧请求配额。
        try:
            import os as _os_p
            if _os_p.path.exists(_os_p.path.join(
                    _os_p.path.expanduser("~"), ".trident", "state", "DATA_PAUSED")):
                print("[ATTR-5M] 数据暂停中，跳过 LLM 调用", flush=True)
                return None
        except Exception:
            pass

        # ── v322: 额度闸门 —— 这条路径是裸 session.post，不走 _do_api_call，
        # 此前完全没有额度管控（事故：2026-11-04 额度被打穿）。
        try:
            from common import llm_budget as _lb
            _est_a = _lb.estimate_call_tokens(
                str(payload.get("messages", "")), int(payload.get("max_tokens") or 400))
            _ok_a, _reason_a = _lb.check(_est_a, purpose="anomaly")
            if not _ok_a:
                print(f"[ATTR-5M] 额度闸门拦截：{_reason_a}（未发出请求）", flush=True)
                return None
        except Exception as _e:
            print(f"[ATTR-5M] 额度闸门异常（放行）：{type(_e).__name__}: {_e}", flush=True)

        # ── v322: 速率闸门（发请求前扣名额）──────────────────────────
        try:
            from common import llm_budget as _lb_rpm
            _ok_rpm, _why_rpm = _lb_rpm.consume_rate_slot(purpose="anomaly")
            if not _ok_rpm:
                print(f"[ATTR-5M] {_why_rpm}", flush=True)
                return None
        except Exception:
            pass  # 闸门自身异常 → 放行
        try:
            _t0 = time.perf_counter()
            resp = session.post(url, json=payload, headers=headers, timeout=25)
            _dt_ms = int((time.perf_counter() - _t0) * 1000)
            if resp.status_code >= 400:
                # v187: 失败埋点
                try:
                    import db as _db_a
                    _db_a.log_ai_call(
                        purpose="anomaly", key_alias=cfg.get("key_alias", "unknown"),
                        model_id=str(cfg.get("id") or "?"), tokens_in=0, tokens_out=0,
                        latency_ms=_dt_ms, status="http_error",
                        error_code=str(resp.status_code), call_site="attribute_5m_abnormal",
                    )
                except Exception:
                    pass
                if resp.status_code == 402:
                    # v501：这个 key 欠费 → 只标记它 30 分钟，继续试下一个还活着的通道
                    try:
                        from common import llm_budget as _lbq1
                        _lbq1.note_quota_dead(
                            30, "attribute_5m_abnormal 402", scope="key:%s" % cfg.get("key_alias", "unknown"))
                    except Exception:
                        pass
                    _tried_live = max(0, _tried_live - 1)
                    if _tried_live <= 0:
                        return {"error": "quota_dead"}
                    continue
                continue
            raw = (resp.json()["choices"][0]["message"]["content"] or "").strip()
            if not raw:
                continue
            cleaned = re.sub(r"^```(?:json)?\s*", "", raw)
            cleaned = re.sub(r"\s*```$", "", cleaned)
            obj = json.loads(cleaned)
            if not isinstance(obj, dict):
                continue
            summary = str(obj.get("summary") or "").strip()
            cands = obj.get("candidates")
            if not isinstance(cands, list):
                cands = []
            out_cands: List[Dict[str, Any]] = []
            for c in cands[:3]:
                if not isinstance(c, dict):
                    continue
                cause = str(c.get("cause") or "").strip()
                if not cause:
                    continue
                out_cands.append({
                    "cause": cause,
                    "source": str(c.get("source") or "").strip(),
                    "confidence": str(c.get("confidence") or "medium").strip(),
                    "speculative": bool(c.get("speculative", False)),
                })
            return {"summary": summary, "candidates": out_cands}
        except Exception:
            continue
    return {"error": "all attribution models failed"}


def attribute_market_drivers(asset: str, events: List[Dict[str, Any]]) -> Dict[str, Any]:
    """星标事件 AI 归因：从近 24h 高分新闻里找出行情「主要驱动事件」。

    只引用传入的公开新闻（原文/来源/方向），严禁编造；无法确定则降级为候选并列。
    返回：
      {"summary": str, "drivers": [{"ts","content","source","action","score","reason","importance"}]}
    任何失败返回 {"error": str} —— 调用方静默降级（前端不展示 AI 块），绝不阻塞主链路。

    v187: 同样走 anomaly 通道(独立 key)
    """
    cfgs: List[Dict[str, Any]] = []
    # v187：anomaly purpose 池
    try:
        for m in config.get_pool_for_purpose("anomaly"):
            try:
                c = _resolve_model_cfg(m)
                if c:
                    c["key_alias"] = m.get("key_alias", "unknown")
                    cfgs.append(c)
            except Exception:
                continue
    except Exception:
        pass
    if not cfgs:
        for m in AI_MODEL_ROSTER:
            try:
                c = _resolve_model_cfg(m)
                if c:
                    cfgs.append(c)
            except Exception:
                continue
        mm = _minimax_fallback_cfg()
        if mm:
            cfgs.append(mm)
        mm2 = _minimax_fallback_cfg2()
        if mm2:
            cfgs.append(mm2)
    if not cfgs:
        return {"error": "no model available"}

    news_lines = "\n".join(
        f"[{i + 1}] ts={n.get('ts', '')} | action={n.get('action', '')} | score={n.get('score', '')} | "
        f"src={n.get('source', '')} | {n.get('content', '')}"
        for i, n in enumerate(events[:8])
    ) or "（无高分新闻）"

    # 图谱取证：注入该品种涨跌驱动本质，辅助反硬归因；无此品种知识时静默跳过。
    graph_block = ""
    try:
        import causal_graph
        graph_block = causal_graph.inject_block(asset)
        factor_ontology.log_injection(asset, graph_block, mode="attribute")
    except Exception:
        graph_block = ""

    system_prompt = (
        "你是严谨的加密/宏观市场归因助手。给你一个品种近 24h 内分数最高的若干条公开新闻"
        "（含时间戳 ts、方向 action、分数 score、来源 src、原文），判断哪 1~3 条最可能构成该品种行情的主要驱动事件。\n\n"
        "硬性要求：\n"
        "0. 若输入含「品种涨跌本质」背景，优先用它判断哪些新闻最可能构成该品种的核心/条件驱动；"
        "与驱动无关的新闻不得仅因分数高就当作主导事件（严禁硬归因）。\n"
        "1. 只引用列表里出现的新闻原文与来源，不得编造标题/来源/数字。\n"
        "2. 按对行情影响排序输出主要驱动事件；无明确依据时 summary 写「未见单一主导事件，多因素共振」。\n"
        "3. 只输出一个 JSON 对象，格式严格为：\n"
        '{"summary":"一句话总结主要驱动","drivers":[{"ts":<事件ts整数>,"reason":"为何是主要驱动","importance":"high|medium"}]}\n'
        "drivers 最多 3 个，summary 不超过 60 字，reason 不超过 40 字，ts 必须原样照抄列表里的整数。"
    )
    user_text = (
        f"品种：{asset}\n\n"
        f"{graph_block + chr(10) if graph_block else ''}"
        f"近 24h 高分新闻：\n{news_lines}"
    )

    for cfg in cfgs:
        payload: Dict[str, Any] = {
            "model": str(cfg.get("request_model") or cfg.get("id")),
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_text},
            ],
            "max_tokens": 400,
            "temperature": 0.1,
            **(cfg.get("extra_body") or {}),
        }
        if cfg.get("json_mode"):
            payload["response_format"] = {"type": "json_object"}
        headers = {
            "Authorization": f"Bearer {cfg.get('api_key')}",
            "Content-Type": "application/json",
        }
        url = f"{cfg.get('api_base')}/chat/completions"
        session = _LLM_PROXY_SESSION if cfg.get("use_proxy") else _LLM_SESSION

        # ── v322: 额度闸门 —— 裸 session.post，此前无任何额度管控。
        try:
            from common import llm_budget as _lb
            _est_d = _lb.estimate_call_tokens(
                str(payload.get("messages", "")), int(payload.get("max_tokens") or 400))
            _ok_d, _reason_d = _lb.check(_est_d, purpose="driver")
            if not _ok_d:
                print(f"[ATTR-DRV] 额度闸门拦截：{_reason_d}（未发出请求）", flush=True)
                return None
        except Exception as _e:
            print(f"[ATTR-DRV] 额度闸门异常（放行）：{type(_e).__name__}: {_e}", flush=True)

        # ── v322: 速率闸门（每次真实请求前扣名额）────────────────────
        # 这是遍历多个通道的循环 —— 每个通道的请求都要扣，
        # 否则 fallback 会把并发放大好几倍。
        try:
            from common import llm_budget as _lb_rpm
            _ok_rpm, _why_rpm = _lb_rpm.consume_rate_slot(purpose="driver")
            if not _ok_rpm:
                print(f"[ATTR-DRV] {_why_rpm}", flush=True)
                return None
        except Exception:
            pass  # 闸门自身异常 → 放行
        try:
            resp = session.post(url, json=payload, headers=headers, timeout=25)
            if resp.status_code >= 400:
                continue
            raw = (resp.json()["choices"][0]["message"]["content"] or "").strip()
            if not raw:
                continue
            cleaned = re.sub(r"^```(?:json)?\s*", "", raw)
            cleaned = re.sub(r"\s*```$", "", cleaned)
            obj = json.loads(cleaned)
            if not isinstance(obj, dict):
                continue
            summary = str(obj.get("summary") or "").strip()
            raw_drivers = obj.get("drivers")
            if not isinstance(raw_drivers, list):
                raw_drivers = []
            by_ts = {int(e.get("ts") or 0): e for e in events}
            out: List[Dict[str, Any]] = []
            for idx, d in enumerate(raw_drivers[:3]):
                if not isinstance(d, dict):
                    continue
                ts = int(d.get("ts") or 0)
                ev = by_ts.get(ts)
                if ev is None and idx < len(events):
                    ev = events[idx]  # AI 未照抄 ts 时，按输出顺序退化为事件顺序对应
                reason = str(d.get("reason") or "").strip()
                if not ev or not reason:
                    continue
                out.append({
                    "ts": int(ev.get("ts") or 0),
                    "content": str(ev.get("content") or "")[:160],
                    "source": str(ev.get("source") or "").strip(),
                    "action": str(ev.get("action") or "HOLD").upper(),
                    "score": float(ev.get("score") or 0.0),
                    "reason": reason,
                    "importance": str(d.get("importance") or "medium").strip(),
                })
            return {"summary": summary, "drivers": out}
        except Exception:
            continue
    return {"error": "all attribution models failed"}


# ── OpenRouter 健康检测 + MiniMax 备用通道 ──
# OpenRouter 余额不足(402)/不可用时，把 OpenRouter 通道替换为 MiniMax（保留权重）；
# 恢复后自动切回（探测结果缓存 _OPENROUTER_HEALTH_TTL 秒，旧注释误写"5 分钟"）。
_OPENROUTER_HEALTH: Dict[str, Any] = {"ts": 0.0, "ok": True}

# 健康探测自动轮询开关（测试阶段暂停）：关闭后 _openrouter_healthy() 不再发同步
# 探测请求，直接读缓存（默认 True），去掉每批次空转轮询，降低 token 消耗与资源占用。
# 保留 _probe_openrouter() 作基础存活校验，实盘前改为仅在“策略触发开仓动作”时执行一次。

# OpenRouter 熔断：实际调用连续失败后冷却 60s（期间直接走 MiniMax，避免每条死等 65s 超时）
_openrouter_circuit: Dict[str, float] = {"until": 0.0}

# 引擎侧 OpenRouter 探测状态落盘（runtime/llm_health.json），供前端直接查看
_LLM_HEALTH_FILE = os.path.join(config.BASE_DIR, "runtime", "llm_health.json")


def _write_llm_health(ok: bool, status: Any, error: str) -> None:
    """把引擎视角的 OpenRouter 探测结果写入 runtime 文件（API/前端可读）。"""
    try:
        os.makedirs(os.path.dirname(_LLM_HEALTH_FILE), exist_ok=True)
        with open(_LLM_HEALTH_FILE, "w", encoding="utf-8") as f:
            json.dump({
                "openrouter_ok": bool(ok),
                "status_code": status,
                "error": error[:200],
                "ts": time.time(),
                "probe_model": "openai/gpt-4o-mini",
            }, f, ensure_ascii=False)
    except Exception:
        pass


def _probe_openrouter() -> bool:
    """基础存活校验：发一次探测并更新健康缓存（写入 runtime/llm_health.json）。

    仅在需要时显式调用——实盘阶段在“策略触发开仓动作”前调用一次即可，而不是
    每批次/定时高频轮询。用与分析一致的 max_tokens(600)：余额不足时 1 token 仍会
    200，600 token 才会按 402 正确暴露；探测成功不产生额外计费。
    """
    now = time.monotonic()
    ok = False
    status = None
    error = ""
    # ── v322: 额度闸门 —— 探测也是真实计费请求（max_tokens=600！）────
    # 额度用光时探针毫无意义（反正什么都发不出去），直接放弃。
    try:
        from common import llm_budget as _lb
        _ok_b, _reason_b = _lb.check(0, purpose="probe")
        if not _ok_b:
            print(f"[AI] OpenRouter 探针被额度闸门拦截：{_reason_b}（未发出请求）", flush=True)
            return False
    except Exception:
        pass  # 预算模块异常 → 照常探（不因保护机制故障停摆）
    # ── v322: 速率闸门（探针也是真实计费请求）────────────────────────
    try:
        from common import llm_budget as _lb_rpm
        _ok_rpm, _why_rpm = _lb_rpm.consume_rate_slot(purpose="probe")
        if not _ok_rpm:
            print(f"[AI] OpenRouter 探针{_why_rpm}", flush=True)
            return False
    except Exception:
        pass  # 闸门自身异常 → 照常探
    try:
        resp = _LLM_PROXY_SESSION.post(
            "https://openrouter.ai/api/v1/chat/completions",
            json={
                "model": "openai/gpt-4o-mini",
                "messages": [{"role": "user", "content": "ok"}],
                "max_tokens": 600,  # 与分析一致，余额不足时同样 402
                "temperature": 0.0,
            },
            headers={"Authorization": f"Bearer {config.OPENROUTER_API_KEY}"},
            timeout=20,
        )
        status = resp.status_code
        ok = status == 200
        if status == 402:
            print(f"[AI] OpenRouter 余额不足(402)，切换到 MiniMax 备用通道（{config.MINIMAX_MODEL}）", flush=True)
        elif status != 200:
            error = f"HTTP {status}: {resp.text[:120]}"
            print(f"[AI] OpenRouter 不可用(HTTP {status})，切换到 MiniMax 备用通道", flush=True)
    except Exception as exc:
        error = f"{type(exc).__name__}: {str(exc)[:120]}"
        print(f"[AI] OpenRouter 检测失败({type(exc).__name__})，切换到 MiniMax 备用通道", flush=True)
    _write_llm_health(ok, status, error)
    _OPENROUTER_HEALTH.update({"ts": now, "ok": ok})
    return ok


def _probe_openrouter_if_stale() -> None:
    """开仓动作触发的健康探测（带 TTL 节流）：距上次探测超过 TTL 才真正发请求。

    由开仓分支用 loop.run_in_executor 后台触发，不阻塞开仓动作与事件循环；
    结果写 _OPENROUTER_HEALTH 缓存，供 _openrouter_healthy() 判断是否切 MiniMax。
    """
    now = time.monotonic()
    if now - float(_OPENROUTER_HEALTH.get("ts", 0.0)) < _OPENROUTER_HEALTH_TTL:
        return
    _probe_openrouter()


def _openrouter_healthy() -> bool:
    """读取 OpenRouter 健康缓存（供 _resolve_model_cfg 判断是否切 MiniMax）。

    测试阶段 _OPENROUTER_AUTO_PROBE=False：不做同步探测，直接返回缓存值（默认
    True），避免在 async 事件循环主线程里同步阻塞 + 空转消耗 token/资源。实盘
    需要实时健康度时，在开仓前显式调用 _probe_openrouter() 再读本函数。
    """
    if not _OPENROUTER_AUTO_PROBE:
        return bool(_OPENROUTER_HEALTH.get("ok", True))
    now = time.monotonic()
    if now - float(_OPENROUTER_HEALTH.get("ts", 0.0)) < _OPENROUTER_HEALTH_TTL:
        return bool(_OPENROUTER_HEALTH.get("ok", True))
    return _probe_openrouter()


def _resolve_model_cfg(m: Dict[str, Any]) -> Dict[str, Any]:
    """OpenRouter 通道不可用且有 MiniMax Key 时，用 MiniMax 替代（保留原权重）。

    优先级：OpenRouter 通 → 用它；OpenRouter 不通或熔断冷却中 → MiniMax。
    """
    cfg = build_model_config(m["id"])
    if (
        cfg.get("use_proxy")
        and config.MINIMAX_API_KEY
        and (not _openrouter_healthy() or time.monotonic() < _openrouter_circuit["until"])
    ):
        return {
            "id": config.MINIMAX_MODEL,
            "label": f"{config.MINIMAX_MODEL} (MiniMax 备用)",
            "api_base": config.MINIMAX_BASE_URL,
            "api_key": config.MINIMAX_API_KEY,
            "extra_body": {},
            "use_proxy": bool(getattr(config, "MINIMAX_USE_PROXY", True)),
            "json_mode": False,
            "full_prompt": True,  # 走完整 _SYSTEM_PROMPT（含多品种 sibling 模板），
                                  # MiniMax 无 response_format 但给足模板能原生输出嵌套
            # v322: 补齐 key_alias / timeout —— 用的是账户1 的 key，
            # 熔断必须与 minimax1 归到同一个账户桶里，否则熔断器分裂。
            "key_alias": "minimax1",
            "timeout": float(getattr(config, "MINIMAX_TIMEOUT", 60.0) or 60.0),
            "is_local": False,
            "purpose": ["main"],
        }
    return cfg


def _minimax_backup_active() -> bool:
    """OpenRouter 不可用/熔断冷却中且配了 MiniMax key ⇒ 兜底已激活。

    与 _resolve_model_cfg 的兜底触发条件保持一致。
    """
    return bool(config.MINIMAX_API_KEY) and (
        not _openrouter_healthy() or time.monotonic() < _openrouter_circuit["until"]
    )


def _build_model_pool() -> List[Dict[str, Any]]:
    """构建实际用于并行分片的模型池（含 OpenRouter 失效时的 MiniMax 兜底扇出）。

    正常：主池各通道按 weight 复制槽位。
    OpenRouter 失效：use_proxy 槽位合并为 1 个 MiniMax 兜底（不按 weight 复制），
                     避免"失效通道按权重把 MiniMax 调用放大 9 倍"的 bug。
                     再追加 1 个第二个 MiniMax key 槽位作并行补充。

    v322：**跳过已熔断的通道**。
      事故背景（2026-11-04）：4 个远程通道全挂时，池里仍然保留它们，
      每条新闻被轮询分配到 minimax → 撞一次 429/401 → 再走 fallback 才落到本地。
      等于每条新闻都要先浪费两次失败请求（含重试退避），且轮询分配让
      "已熔断"这件事完全没被感知。
      现在熔断打开的通道直接不进池 → 轮询只会分配到**活的**通道，
      恢复速度从"每条先失败两次"变成"直接用兜底通道"。
      熔断按 key_alias（= 账户）粒度，与实际配额归属一致：
      账户1 挂了就该跳过该账户的全部通道。
    """
    pool: List[Dict[str, Any]] = []
    backup_active = _minimax_backup_active()
    or_substituted = False  # 已合并过 use_proxy 失效槽位则不再追加
    skipped: List[str] = []
    for m in AI_MODEL_ROSTER:
        # ── v322: 主分析池**只收 purpose 含 main 的通道** ──
        # 事故背景（2026-11-04）：原来这里遍历的是**整个 roster**，不按 purpose 过滤
        # → gate 通道 / reflect 通道 / 本地 Ollama 通道全被塞进"主分析池"。
        # 后果：轮询分片 (model_pool[idx % len]) 会把 1/3 的新闻分配给
        # local/qwen2.5:3b（主分析 prompt 10k+ tokens → 必然 ReadTimeout），
        # 同时把门卫专用的高频通道拉进主路径抢配额。
        # 修法：只留 purpose 含 "main" 的条目 —— 池与 purpose 语义对齐。
        if "main" not in (m.get("purpose") or []):
            continue
        cfg = _resolve_model_cfg(m)
        # ── v322: 熔断中的通道不进池 ──
        # v423（2026-11-17）：这里必须用 available()（只读判定），不能用 allow()。
        # allow() 有副作用：open 冷却到期时会顺手把状态推到 half_open 并把
        # _probe_in_flight 置 True —— 等于把唯一那次探测名额在这儿就用掉了，
        # 而组池本身**不发任何请求**。结果：池子建好了、真到调用点再 allow()
        # 拿到 False → 每条新闻都记「通道熔断中（跳过请求）」，零决策、
        # PENDING 一路涨，可账户额度其实是好的。实测 17:41 又复现：
        # 「跳过认领：全部通道熔断: minimax1[quota(probe占用)]」。
        alias = str(m.get("key_alias") or cfg.get("id") or "?")
        brk_q = _analysis_breaker(alias, "quota")
        brk_n = _analysis_breaker(alias, "net")
        if brk_q is not None and brk_n is not None:
            if not (brk_q.available() and brk_n.available()):
                skipped.append(str(m.get("id") or alias))
                continue
        weight = max(1, int(m.get("weight", 1)))
        if m.get("use_proxy") and backup_active:
            # 失效的 use_proxy 槽位只产生 1 个 MiniMax 兜底（不按 weight 复制）
            if not or_substituted:
                pool.append(cfg)
                or_substituted = True
        else:
            # 正常 slot 按 weight 复制
            pool.extend([cfg] * weight)
    if backup_active:
        cfg2 = _minimax_fallback_cfg2()
        if cfg2:
            # 追加 1 个第二个 MiniMax key 槽位作并行补充（不再按比例放大）
            pool.append(cfg2)
    if skipped:
        print(f"  [POOL] 熔断跳过 {len(skipped)} 个通道: {', '.join(skipped[:6])}",
              flush=True)
    if not pool:
        # 全部熔断 → 不能返回空池（调用方会做 idx % len(pool) 崩）。
        # 退回未过滤的 main 池，由 _do_api_call 的熔断检查去逐个跳过
        # （每个通道直接 raise「熔断中」，不消耗额度）。
        print("  [POOL] ⚠️ 全部通道熔断，回退到未过滤池（调用点会逐个跳过）",
              flush=True)
        return [
            _resolve_model_cfg(m) for m in AI_MODEL_ROSTER
            if "main" in (m.get("purpose") or [])
        ]
    return pool


# Compatibility export for callers that still inspect MODELS at import time.
MODELS: List[Dict[str, Any]] = [build_model_config(DEFAULT_AI_MODEL_ID)]

# 并发限流：批量请求分波发出避免打爆上游通道；充值后 OpenRouter 限流宽松可到 8。
# 单条新闻（1<8）不受影响。慢通道（如 nassaapi 波动 100s+）不再拖死单条。
# v323：并发 10 条 × (主分析 + peer 复核) 会把同一账户的请求瞬间打满 →
# 实测日志 "熔断跳过 1 个通道 / 通道熔断中" → 整批 16 条全失败（Batch done 0/16 ok）。
# 降成可配置（默认 4），先把通道喂稳；吞吐不够再往上调。
_LLM_SEMAPHORE = asyncio.Semaphore(max(1, int(os.getenv("LLM_MAX_CONCURRENCY", "4"))))

# 主模型请求 session — 直连（nassaapi/aiping 均可直连，走本地代理反而
# ProxyError/挂起，实测 nassaapi 走代理 22307 会 59s 超时）。
_LLM_SESSION = requests.Session()
_LLM_SESSION.trust_env = False  # 忽略 env HTTP_PROXY，直连
_LLM_SESSION.headers.update({"User-Agent": "Mozilla/5.0"})

# ── 连接层快速重试（TLS 加固）────────────────────────────────────────
# 间歇 SSL 握手失败(SSLEOFError)/连接瞬断是秒级错误，用 urllib3 Retry 在「连接/读」
# 层快速重试几次即可吃掉绝大多数抽风；read 只重试 1 次，避免 60s×N 拖死 ai_worker。
#
# v322 (2026-11-04) 事故修复：**429 必须从 status_forcelist 里拿掉**
# ──────────────────────────────────────────────────────────────────────
# 原配置把 429 放进了 urllib3 的 status_forcelist，于是**每一条 429 会在
# 连接层自动重试 4 次**（backoff 1+2+4s）。再叠加应用层 _do_api_call 的
# "429 retry once after 2s"、每条新闻最多 3 个 fallback 模型、批内 N 条并发，
# 单条新闻最多打出 **30+ 次 HTTP 请求** —— 配额被瞬间打穿，429 反而变多，
# 形成"越限流越重试、越重试越限流"的正反馈。
# 实测：runtime/llm_call_failures.jsonl 累计 5872 条失败，其中 2135 条是 429。
#
# 429 是**限流信号**，正确做法是"快速失败 + 切 fallback"，而不是硬扛重试。
# 5xx/502/503/504 属于瞬时故障，保留自动重试（确实能救回）。
_llm_retry = Retry(
    total=3,
    connect=3,  # SSL 握手/连接失败 → 秒级重试 3 次
    read=1,     # 读响应超时 → 只重试 1 次
    backoff_factor=1.0,
    # ⚠️ 不含 429：限流交给应用层快速失败切 fallback（见 _do_api_call）
    status_forcelist=[502, 503, 504],
    allowed_methods=["POST"],
)
_LLM_SESSION.mount("https://", HTTPAdapter(max_retries=_llm_retry))


# ── v322: 主分析通道熔断（此前 ai_worker 完全没有熔断保护）──────────────
# 事故背景（2026-11-04 实测）：
#   runtime/llm_call_failures.jsonl 累计 5872 条失败（2135 条 429），
#   而 ai_worker.py 里 **一次 circuit_breaker 都没 import** ——
#   realtime_filter（门卫）有熔断，主分析（最热、prompt 最长、调用最频繁）却没有。
#
#   结果就是：4 个通道全挂（2 个 429 配额打满 + 2 个 401 key 失效）时，
#   主循环每轮仍然「认领一批 → 每条打主模型 + 3 个 fallback → 全失败 → 重试」，
#   单轮几十次 HTTP 请求全部打空 —— 这就是用户说的"一下子把流量干没了"。
#
# 修法（与 realtime_filter 同构）：
#   · 配额类失败（429/401/402/403）→ 长冷却 10 分钟
#   · 网络类失败（超时/SSL/连接）→ 短冷却 60 秒
#   · 认领**之前**先看主池是否全熔断 → 全熔断就**不认领**（零额度消耗），
#     等熔断冷却结束自动恢复。
# v322 二次加固（用户反馈"额度被瞬间打穿"后）：
#   配额/鉴权类失败（429/401/402/403）**一次就熔断**。
#   原来阈值 2 意味着：第一次 429 → 睡 2s 重试 → 第二次 429 → 才熔断，
#   实际每个通道要打 3 次请求才停；再乘以 3 个 fallback × 批内并发 → 雪崩。
#   429 的语义是"配额没了/被限流"，再打一次的结果是可预测的 → 不该重试。

# v322: 鉴权类失败（401/403）= key 失效 → **不会自己恢复**，
#   所以用极长冷却（默认 24h）而不是 10 分钟后自动探测。
#   10 分钟一次探测 = 一天 144 次无效请求，每次都可能计费。
# 配额类（429/402 = 余额/限流）冷却拉长到 30 分钟，减少探测次数。

# 2026-11-15: 「瞬时限速」单独成类。
# 事故背景：MiniMax 返回 429 + {"type":"rate_limit_error","message":"已达到 Token Plan
# 速率限制 (2062)"}，这**不是**额度耗尽，而是套餐档位的瞬时速率限制 —— 通常几十秒
# 就恢复。原来它与"余额打满"共用 30 分钟冷却，一次瞬时限速被放大成 36 分钟静默，
# 整条决策链路停摆 46 小时。
# 现在：rate_limit 类 → 45 秒短冷却、阈值 2（不重试、不烧额度），且**不污染** quota 熔断。
_ANALYSIS_RATELIMIT_COOLDOWN_S = float(os.getenv("ANALYSIS_RATELIMIT_COOLDOWN_S", "45"))
_ANALYSIS_RATELIMIT_FAIL_THRESHOLD = int(os.getenv("ANALYSIS_RATELIMIT_FAIL_THRESHOLD", "2"))


def _analysis_breaker(key_alias: str, kind: str):
    """取主分析通道的熔断器。

    kind:
        'quota' —— 429/402：配额打满/余额不足。冷却 30 分钟。
        'auth'  —— 401/403：key 失效。冷却 24 小时（不会自己好，别去探测）。
        'net'   —— 超时/SSL/连接错误。冷却 60 秒。

    熔断器名带 'llm_main_' 前缀，与 realtime_filter 的 'llm_quota:'/'llm_net:'
    分开 —— 两边的通道集合与冷却策略不同，共用一个会在语义上误导。
    """
    try:
        from circuit_breaker import get as _cb_get
        if kind == "quota":
            return _cb_get(
                f"llm_main_quota:{key_alias}",
                fail_threshold=_ANALYSIS_QUOTA_FAIL_THRESHOLD,
                open_seconds=_ANALYSIS_QUOTA_COOLDOWN_LONG_S,
            )
        if kind == "ratelimit":
            return _cb_get(
                f"llm_main_ratelimit:{key_alias}",
                fail_threshold=_ANALYSIS_RATELIMIT_FAIL_THRESHOLD,
                open_seconds=_ANALYSIS_RATELIMIT_COOLDOWN_S,
            )
        if kind == "auth":
            return _cb_get(
                f"llm_main_auth:{key_alias}",
                fail_threshold=_ANALYSIS_QUOTA_FAIL_THRESHOLD,
                open_seconds=_ANALYSIS_AUTH_COOLDOWN_S,
            )
        return _cb_get(
            f"llm_main_net:{key_alias}",
            fail_threshold=_ANALYSIS_NET_FAIL_THRESHOLD,
            open_seconds=_ANALYSIS_NET_COOLDOWN_S,
        )
    except Exception:
        return None


# ── v322: 「全通道不可用」主动告警 ────────────────────────────────────────
# 事故背景（2026-11-07）：临时 key 一旦额度用尽/失效，主循环进入
# pool-blocked → **不认领、不消耗额度、也完全不告警** → 用户只看到
# 「待分析不涨」，以为系统抽风（正是用户反复投诉的现象）。
# 现在把「全通道不可用」变成主动推送（微信 + 邮件 + PushPlus 并联）。
#
# ⚠️ 限流：Server酱免费版**每天只有 5 次**发送额度（日志实测撞过
#   "[AUTH] 超过当天的发送次数限制[5]"）→ 必须严格节流：
#   · 首次阻塞立即告警
#   · 持续阻塞每 6 小时再告警一次（≤ 4 次/天）
#   · 恢复时告警一次（告诉用户可以换回 key 了）
# 状态落盘 → 跨进程共享 + 重启后不重复轰炸。
_POOL_ALERT_FILE = os.path.join(config.BASE_DIR, "runtime", "pool_blocked_alert.json")


def _pool_alert_state() -> Dict[str, Any]:
    try:
        with open(_POOL_ALERT_FILE, "r", encoding="utf-8") as fh:
            return json.loads(fh.read() or "{}") or {}
    except Exception:
        return {}


def _pool_alert_save(st: Dict[str, Any]) -> None:
    try:
        os.makedirs(os.path.dirname(_POOL_ALERT_FILE), exist_ok=True)
        tmp = "%s.tmp.%d" % (_POOL_ALERT_FILE, os.getpid())
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(st, fh, ensure_ascii=False)
        os.replace(tmp, _POOL_ALERT_FILE)
    except Exception:
        pass


def notify_pool_blocked(reason: str, *, recovered: bool = False) -> bool:
    """全通道不可用 / 恢复时的主动告警。返回本次是否真的派发了。

    节流规则见上方注释（Server酱每天只有 5 次额度，不能乱发）。
    """
    now = time.time()
    st = _pool_alert_state()
    try:
        if recovered:
            if not st.get("notified"):
                return False      # 没告警过阻塞 → 不需要报恢复
            _pool_alert_save({"last_ts": now, "reason": "", "notified": False})
            _dispatch_pool_alert(
                "✅ 引擎已恢复：LLM 通道重新可用",
                "之前的阻塞原因：%s\n\n"
                "AI 分析已恢复认领。若你换了新 key，现在可以确认一下：\n"
                "  cd ~/Desktop/金融1.0/backend\n"
                "  python3 scripts/rotate_minimax_keys.py <新key>\n"
                "  launchctl kickstart -k gui/$(id -u)/com.trident.engine"
                % (st.get("reason") or "-"))
            return True

        last_ts = float(st.get("last_ts") or 0.0)
        last_reason = str(st.get("reason") or "")
        if (st.get("notified") and last_reason == reason
                and (now - last_ts) < _POOL_ALERT_INTERVAL_S):
            return False
        _pool_alert_save({"last_ts": now, "reason": reason, "notified": True})
        _dispatch_pool_alert(
            "🚨 LLM 通道全挂 —— AI 分析已暂停",
            "原因：%s\n\n"
            "影响：引擎**不再认领**新闻（零额度消耗），但「待分析」会一直涨。\n\n"
            "处理：\n"
            "  1) 换一把可用 key：\n"
            "     cd ~/Desktop/金融1.0/backend\n"
            "     python3 scripts/rotate_minimax_keys.py <新key>\n"
            "  2) 重启引擎加载新 key：\n"
            "     launchctl kickstart -k gui/$(id -u)/com.trident.engine\n"
            "  3) 或者给现有 key 充值（MiniMax Token Plan / 积分）\n\n"
            "自检：python3 scripts/doctor.py" % (reason or "-"))
        return True
    except Exception as exc:
        print(f"[POOL-ALERT] 告警异常（不影响主流程）: {type(exc).__name__}: {exc}",
              flush=True)
        return False


def _dispatch_pool_alert(title: str, body: str) -> None:
    try:
        from common import llm_budget
        llm_budget.push_critical(title, body)
    except Exception as exc:
        print(f"[POOL-ALERT] 派发失败: {type(exc).__name__}: {exc}", flush=True)


# -- v323 P0-1: 决策链停摆告警（FIX-8 补调用方 + 补告警出口）-----------
# 事故背景（2026-11-09 实测）：ai_decisions 最新一条停在 05:03，raw_news
# 一直在进，整条决策链静默停了 14 小时，**没有任何告警**。
# 根因不是没写检测器 —— pipeline_stall_guard.py 早把判活逻辑写好了，
# 但它**零调用方**（只在自己的 __main__ 里跑）→ 等于没有这个巡检。
# 现在挂到 ai_worker 主循环：池子健康但 30 分钟没有新决策 →
#   ① 写 dlq_events(kind='decision_stall') 留痕
#   ② 推 critical 告警（飞书/邮件/Server酱/PushPlus 并联）
# 节流：查 dlq_events 近 6h 有无同类记录 → 跨进程/跨重启都有效
# （Server酱免费版每天只有 5 次额度，必须严格节流）。
# 用可变容器存巡检时刻：ai_worker() 里赋值不需要 global 声明


def _auto_resolve_recovered_stalls(conn, min_age_hours: float = 2.0) -> int:
    """把「已经恢复产出」的 `decision_stall` 告警自动结单。

    为什么要有（2026-11-18 实测）：dlq_events 里 3 条 decision_stall 一直
    `resolved=0`（11-16 08:38 / 11-16 22:47 / 11-17 13:14），而决策链早就恢复了 ——
    quota-sentinel 每 5 分钟照本宣科报一次 `DLQ未解决=3`，真正的告警被这些
    历史噪音埋掉。结单判据用「事实」而不是「时间到了」：
      · 告警存在 ≥ min_age_hours（避免刚报警就自己关掉）；
      · 且此刻 ai_decisions 最新一条**晚于**该告警的创建时间（= 停摆期已过）。
    两条同时满足才 resolve。只动 resolved 标记，不改告警原文（原文后面追加结单说明）。
    """
    from datetime import datetime as _dt, timedelta as _td
    n = 0
    try:
        rows = conn.execute(
            "SELECT id, created_at FROM dlq_events"
            " WHERE kind='decision_stall' AND resolved=0"
        ).fetchall()
        if not rows:
            return 0
        latest = conn.execute(
            "SELECT datetime(max(created_at),'+8 hours') FROM ai_decisions"
        ).fetchone()[0]
        if not latest:
            return 0
        try:
            latest_dt = _dt.strptime(str(latest)[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return 0
        cutoff = _dt.now() - _td(hours=float(min_age_hours))
        for _id, _ts in rows:
            try:
                ts_dt = _dt.strptime(str(_ts)[:19], "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
            # 两个条件：够老 + 产出时间已晚于告警时刻
            if ts_dt > cutoff or latest_dt <= ts_dt:
                continue
            try:
                cur = conn.execute(
                    "UPDATE dlq_events SET resolved=1,"
                    " resolved_at=datetime('now','localtime'),"
                    " last_error=coalesce(last_error,'') || ?"
                    " WHERE id=? AND resolved=0",
                    (f" | resolved: auto(产出已恢复, 最新决策 {latest})", int(_id)),
                )
                conn.commit()
                if cur.rowcount:
                    n += 1
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass
        if n:
            print(f"  [DLQ-AGING] 自动结单 {n} 条已恢复的 decision_stall 告警",
                  flush=True)
    except Exception as _exc:
        print(f"  [DLQ-AGING] 跳过: {type(_exc).__name__}: {str(_exc)[:100]}",
              flush=True)
    return n


def _check_and_alert_decision_stall() -> None:
    """决策链停摆检测 + 告警。异常全部吞掉：告警链路不能反过来搞挂主循环。"""
    try:
        import pipeline_stall_guard as _psg
    except Exception:
        return
    try:
        conn = db.get_connection()
    except Exception:
        return
    # v409.4: 每次巡检先清掉「已经恢复产出」的历史停摆告警，别让它们把
    # 真实告警埋掉（实证：3 条 11-16/11-17 的 decision_stall 挂了 20+ 小时）。
    try:
        _auto_resolve_recovered_stalls(conn)
    except Exception:
        pass
    try:
        d = _psg.check_stall(conn, stall_hours=_STALL_ALERT_HOURS)
        if not d.get("stalled"):
            return
        # 跨重启节流：近 6h 已有同类告警就不重复推
        try:
            recent = int(conn.execute(
                "SELECT COUNT(*) FROM dlq_events WHERE kind='decision_stall'"
                " AND datetime(created_at) > datetime('now','-6 hours')"
            ).fetchone()[0])
        except Exception:
            recent = 0
        if recent > 0:
            return
        detail = _psg.format_report(d)
        try:
            import observability as _obs
            _obs.dlq_record(
                conn, kind="decision_stall", source="ai_worker",
                payload=d, error=str(d.get("last_decision") or "")[:200],
            )
        except Exception:
            pass
        print(f"  [STALL-ALERT] {detail}", flush=True)
        try:
            from common import llm_budget
            llm_budget.push_critical(
                "决策链停摆：AI 分析已停止产出",
                detail + "\n\n引擎活着但没有新决策 → LLM 通道 / 批次处理 / 写库链路需要排查。",
            )
        except Exception:
            pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


def main_pool_ready() -> tuple[bool, str]:
    """主分析池是否还有可用通道 + 今日额度是否还有余额。

    返回 (可认领, 说明)。以下任一情况返回 (False, ...)：
        · 全部通道熔断（quota/auth/net 任一类）
        · 今日额度预算已用尽（token / 请求数 / 信号条数任一）

    此时主循环应跳过认领，避免在通道全挂或超预算时持续烧额度。

    注意：调用 allow() 有副作用（open→half_open），这是熔断器的正常语义
    —— half_open 放行一次探测请求。若探测成功即自动闭合。
    """
    # ── v322: 额度闸门优先于通道体检 ──
    # 顺序很关键：额度用光时，就算通道全部健康也不该再发请求。
    try:
        from common import llm_budget
        _ok, _reason = llm_budget.check(0)
        if not _ok:
            return False, f"额度闸门：{_reason}"
    except Exception:
        pass  # 预算模块异常 → 不因闸门本身故障而停摆

    try:
        pool = config.get_pool_for_purpose("main")
    except Exception:
        return True, "pool 查询失败（不拦截）"
    if not pool:
        return True, "主池为空（不拦截，交由认领逻辑处理）"
    blocked: list = []
    for m in pool:
        alias = str(m.get("key_alias") or m.get("id") or "?")
        brk_q = _analysis_breaker(alias, "quota")
        brk_a = _analysis_breaker(alias, "auth")
        brk_n = _analysis_breaker(alias, "net")
        if brk_q is None or brk_n is None:
            return True, "熔断器不可用（不拦截）"
        # 任一类熔断即视为该通道不可用（auth 熔断 = key 失效，24h 内不探测）
        # v409.2：这里**必须**用只读的 available()，不能用 allow()。
        # allow() 有副作用：open 冷却到期时会顺手推到 half_open 并占掉唯一一次探测名额，
        # 而体检本身不发请求 —— 真正发请求的调用点再 allow() 就拿到 False，
        # 通道被卡在 half_open 达 probe_timeout（quota 是 30 分钟），日志表现为
        # 「熔断中」（无倒计时）、零决策、PENDING 一直涨，而账户额度其实是好的。
        if (brk_q.available() and brk_n.available() and
                (brk_a is None or brk_a.available())):
            return True, ""
        # v322: 说清是**哪一类**熔断 + 剩余多少秒 → 运维一眼能判断该等还是该换 key。
        # 原来只输出 "全部通道熔断: minimax1,minimax2"，看不出是 429（等 30 分钟）
        # 还是 401（换 key 也没用，等 24 小时白等）—— 两种情况的处置完全相反。
        _why = []
        for _brk, _kind in ((brk_q, "quota"), (brk_n, "net"), (brk_a, "auth")):
            if _brk is None:
                continue
            try:
                if _brk.state == "open":
                    _wait = float(getattr(_brk, "_wait_until", 0.0) or 0.0)
                    _left = max(0, int(_wait - time.time()))
                    _why.append("%s(%ss)" % (_kind, _left))
                elif _brk.state == "half_open":
                    # v409.2：half_open 也要报，否则日志只有「熔断中」四个字，
                    # 分不清是"冷却中"还是"探测名额被占死"（后者要 30 分钟才自愈）。
                    _in_flight = bool(getattr(_brk, "_probe_in_flight", False))
                    _why.append("%s(probe%s)" % (
                        _kind, "占用" if _in_flight else "待放行"))
            except Exception:
                pass
        blocked.append("%s[%s]" % (alias, ",".join(_why) if _why else "熔断中"))
    return False, "全部通道熔断: " + ",".join(blocked)


def pool_recovery_eta(pool=None) -> Optional[float]:
    """池内通道**最早**可能恢复的剩余秒数（只读：不占探测名额、不发请求）。

    v434（v413 P0-A1）背景：pool-blocked 期间日志只有「熔断中」四个字，
    运维看不出该等 30 分钟（quota）还是该换 key（auth 24 小时、等也没用），
    11-17 那次 11,400s 的空等就是这样被藏住的。这里把三个熔断器的放行
    时刻直接算出来，给出「最近可恢复」倒计时。

    返回：
        None —— 无法计算（池为空 / 熔断器取不到 / 全部 closed）；
        0.0  —— 已经可以放行（main_pool_ready 此时本应返回 True，仅作兜底）；
        >0   —— 还要等待的秒数（取池内**最快**恢复的那条通道）。

    注意：只用 `state` / `_wait_until` / `probe_timeout` 这些只读属性，
    **不调用** `allow()`——那会占掉 half_open 唯一的探测名额（见 v409.2 事故）。
    """
    try:
        if pool is None:
            pool = config.get_pool_for_purpose("main")
    except Exception:
        return None
    if not pool:
        return None

    _now = time.time()
    _eta: Optional[float] = None
    for m in pool:
        alias = str(m.get("key_alias") or m.get("id") or "?")
        _chan_eta = 0.0
        _has_breaker = False
        for _kind in ("quota", "net", "auth"):
            try:
                _brk = _analysis_breaker(alias, _kind)
            except Exception:
                _brk = None
            if _brk is None:
                continue
            _has_breaker = True
            try:
                _state = getattr(_brk, "state", "closed")
                if _state == "open":
                    _wait = float(getattr(_brk, "_wait_until", 0.0) or 0.0)
                    _chan_eta = max(_chan_eta, max(0.0, _wait - _now))
                elif (_state == "half_open"
                      and bool(getattr(_brk, "_probe_in_flight", False))):
                    # 探测进行中：按 probe_timeout 估算最坏情况（名额被占死时
                    # 日志只有「熔断中」，看不出还要等 30 分钟）
                    _pt = float(getattr(_brk, "probe_timeout", 0.0) or 0.0)
                    _pa = float(getattr(_brk, "_probe_at", 0.0) or 0.0)
                    _chan_eta = max(_chan_eta, max(0.0, _pt - (_now - _pa)))
            except Exception:
                continue
        if not _has_breaker:
            continue
        _eta = _chan_eta if _eta is None else min(_eta, _chan_eta)
    return _eta

# 海外通道（OpenRouter）走代理 — trust_env=True 读 env HTTP_PROXY
_LLM_PROXY_SESSION = requests.Session()
_LLM_PROXY_SESSION.headers.update({"User-Agent": "Mozilla/5.0"})

# 重要事件日历提示缓存（金十 list_calendar，10 分钟 TTL——日历变化慢，避免每条新闻打爆 MCP 配额）
_CALENDAR_HINT_CACHE: Dict[str, Any] = {"ts": 0.0, "text": ""}

# 日历通道最新探测状态（用于前端/运维可见：配额耗尽 vs 数据空 vs 正常）
# 字段：ts, ok, count, error, sample_event
_CALENDAR_STATUS: Dict[str, Any] = {
    "ts": 0.0,
    "ok": False,
    "count": 0,
    "error": "",
    "sample_event": "",
    "quota_exhausted": False,   # 全部 token 今日 1500/1500 都用完
}


def _build_calendar_hint() -> str:
    """即将发布的重要财经日历事件提示（缓存 10 分钟，MCP 失败静默）。

    供主分析 prompt 注入：让 AI 意识到未来数小时有 CPI/非农等
    高影响数据即将公布，避免把「公布前的预期波动」误判为新闻即时催化。

    2026-11-03：原实现只缓存**非空** text（`if _CALENDAR_HINT_CACHE["text"] and ...`），
    于是「配额耗尽 / MCP 故障」这种**结果为空**的情况永远不命中缓存 → 每条新闻
    的 prompt 构建都重新打一轮 MCP（配额耗尽时还要白等 7 次 token 轮换），
    把主分析 + 评审调用一起顶到超时边界。现在改为**空结果也缓存**（负缓存），
    并把探测配额状态的第二次 MCP 调用换成只读的 jin10_mcp.quota_state()。
    """
    now = time.monotonic()
    # 缓存命中判定不再要求 text 非空 → 失败/配额耗尽也走缓存（负缓存）
    if now - _CALENDAR_HINT_CACHE["ts"] < 600.0:
        return _CALENDAR_HINT_CACHE["text"]
    text = ""
    status_err = ""
    status_count = 0
    sample = ""
    quota_exhausted = False
    try:
        from feeds import jin10_mcp

        events = jin10_mcp.upcoming_events(min_star=2, horizon_hours=24.0)
        if events:
            lines = [f"· {e['pub_time'][:16]} ★{e['star']} {e['title']}" for e in events[:6]]
            text = (
                "[重要事件日历提示] 未来24h内将公布以下重要经济数据/事件"
                "（注意区分『数据公布前预期交易』与『公布后即时反应』）：\n"
                + "\n".join(lines)
            )
            status_count = len(events)
            sample = events[0].get("title", "")[:60] if events else ""
    except Exception as exc:
        status_err = f"{type(exc).__name__}: {str(exc)[:120]}"
    # 探测配额耗尽（只读状态，**不再额外打一次 MCP**）
    try:
        from feeds import jin10_mcp as _jm

        st = _jm.quota_state("list_calendar")
        quota_exhausted = bool(st.get("quota_exhausted") or st.get("all_tokens_exhausted"))
        if not status_err and quota_exhausted:
            status_err = "配额耗尽（所有 token 今日 1500/1500 用完）"
    except Exception:
        pass

    _CALENDAR_HINT_CACHE.update({"ts": now, "text": text})
    _CALENDAR_STATUS.update({
        "ts": now,
        "ok": bool(text),
        "count": status_count,
        "error": status_err,
        "sample_event": sample,
        "quota_exhausted": quota_exhausted,
    })
    return text


# ---------------------------------------------------------------------------
# DeepSeek API client (stdlib-only, OpenAI-compatible)
# ---------------------------------------------------------------------------

# v500 成本：主分析正文上限（原 2000 字符 ≈ 1250 token；多数新闻 1200 字符内已含全部事实）
_MAIN_NEWS_CHARS = max(600, int(os.getenv("MAIN_NEWS_CHARS", "1200") or 1200))
# v500 成本：K 线知识块上限（原实测 ~1.9K 字符，每条约 1.2K token）
_MAIN_KLINE_CHARS = max(400, int(os.getenv("MAIN_KLINE_CHARS", "1000") or 1000))

# v174: 结构化字段约束 — 必须从八段同源抽取 (放在 _SYSTEM_PROMPT 之前供字符串拼接)


_SYSTEM_PROMPT = """
你是掌管 10 亿美元宏观对冲基金的量化决策大脑，输出直接驱动真实交易：严谨、可追溯、不臆造。

# 任务
每条新闻做两层分析，只输出一个 JSON：①这是什么、是否即时、影响哪些品种（按影响力排序）②对主品种做七段推演，给 BUY/SELL/HOLD。

# 两个溯源字段必填
cross_net_first_seen_ts（三态：精确 'YYYY-MM-DD HH:MM:SS' / 'AMBIGUOUS' / 空串 ''）与 cross_net_source（出处；AMBIGUOUS 写"候选1 / 候选2"）。不知道就留空，严禁编时间。

# 第0步：先判标的（在写任何段落之前）
- 从 BTC / ETH / SOL / XAU / XAG / COPPER / WTI / BRENT / US_STOCK / INDIVIDUAL_STOCK / SKHYNIX / NVDA / TSLA / AAPL / MSFT / AMZN / GOOGL / META / COIN / MSTR / MU / INTC / TSMC / SAMSUNG / PLTR / SMCI / MARA / RIOT / USD / CNY / EUR / JPY / US_BOND 里选一个，target_asset 放 JSON 第一个键。⚠️ A股/港股（CN_STOCK/HK_STOCK）已移出交易池（v424），不再输出这两个值。
- 若新闻**点名了具体公司**，额外输出 "news_ticker":"代码"（A股 600519.SH / 港股 00700.HK / 美股 NVDA）；只说行业、板块或泛指则给空串。target_asset 仍按上面的枚举填，不要用代码替换枚举值。
- NONE 只用于确无可交易标的（会议/人事/纯表态/无落地转述），且必须写明具体理由；拿不准就选"被冲击最大"的那个，并在 [逻辑自检] 标注不确定点。
- 连锁冲击：写影响最大者，其它品种放 [因素定位] 的传导说明。

# 第一层：是什么、能否下单
1) 主体：宏观/政策、公司个股、交易所、大宗商品、人物表态、行业。宏观必须判国别；只有美国主体可把 US10Y/DXY/VIX/TIPS 当直接论据，其它国别写 N/A，禁用美国数据冒充。
2) 性质：事实事件（政策落地/数据公布/断供实际发生/协议签署/央行决议/供需变化）→ **必须给方向**，即使确定性不高也要输出 BUY 或 SELL（低置信度配小仓位 + 紧止损），不得因"信号不够强"退回 HOLD；观点/预测/汇总/转述未落地 → HOLD 并写"观点或汇总类，不作交易信号"；无明确标的（会议/峰会/访问/人事）→ target_asset=NONE、market_category=MACRO、HOLD、中性分，写明"无可交易标的"。禁输出 market_category="OTHER"。只有"纯观点且无任何可交易标的"才允许 HOLD。
2b) 方向优先级（v424 硬约束，防止"技术面未确认"把方向吃掉）：
   - 事件本身的方向就是主判断；盘面（价格 / OI / 费率 / 趋势 / 资金流）只用于**仓位大小与止损位**，不得因为"技术面未确认""资金未确认""方向未获统一"把有方向的事件降级为 HOLD。
   - 只有两种情况允许 HOLD：(a) 事件已被完全定价（已公布且价格已明显反映）；(b) 盘面与事件方向**明确相反**且处于极端波动。HOLD 时必须写明属 (a) 还是 (b)，写不出来就必须给 BUY 或 SELL（低置信度配小仓位）。
   - 低置信度表达方式：仓位减半 / 只观察不建仓的止损条件放宽，而不是 action=HOLD。

3) 资产归类（按"谁被影响最大"）：【v424 交易池 = 除 A股/港股 外全品类】
   - 加密：主流币优先 BTC；公链/交易所/ETF/监管/升级 → BTC 或 ETH/SOL；XRP/DOGE 等小币种新闻归 BTC，不单独给山寨币信号。
   - 贵金属与大宗：黄金 → XAU，白银 → XAG，铜 → COPPER，原油 → WTI，布伦特 → BRENT。
   - 美股指数（纳指/标普/道指/罗素）→ US_STOCK；点名个股 → 对应 ticker（NVDA/TSLA/AAPL/MSFT/AMZN/GOOGL/META/COIN/MSTR/MU/INTC/TSMC/SAMSUNG/PLTR/SMCI/MARA/RIOT）；不在列表的个股 → INDIVIDUAL_STOCK。
   - 外汇：美元指数/美元 → USD，人民币 → CNY，欧元 → EUR，日元 → JPY；其它货币对归 USD 并在 [因素定位] 写传导。
   - 美债/美利率/收益率 → US_BOND；中国国债 → CN_BOND。
   - 基金/ETF（宽基、行业、商品）：归到其跟踪标的所属品类（黄金ETF→XAU，白银ETF→XAG，半导体ETF→US_STOCK 或 SKHYNIX，原油ETF→WTI）。
   - ⚠️ A股/港股**不交易，但信息可参考**（v425）：市场级信息（指数/政策/资金/行业景气/监管动向）必须用来判断对池内品种的传导 —— 中国刺激政策→COPPER/WTI/BTC，人民币贬值→USD，地产链→COPPER，半导体政策→US_STOCK/SKHYNIX；**确有传导就归到受影响品种并正常给方向**。纯 A股/港股个股事件（财报/增减持/回购/业绩预告）若无传导，target_asset=NONE、action=HOLD，并在 [逻辑自检] 写"参考市场，无传导"。**禁止输出 CN_STOCK/HK_STOCK 作为 target_asset**。
   - 聚合信号（"美股多家/沪市N家"集体回购/预增/分红）：美股归 US_STOCK、action=HOLD；A股/港股按上一条处理（有传导给池内品种，无传导判 NONE）。
   - 配对铁律：GOLD↔XAU，SILVER↔XAG，OIL↔WTI，COMMODITY↔XAG/COPPER，CRYPTO↔BTC/ETH/SOL，STOCK↔US_STOCK/INDIVIDUAL_STOCK/ticker，FX↔USD/CNY/EUR/JPY，BOND↔US_BOND。
4) 盯盘优先级（写 [水位博弈] 与 [跨资产资金] 时用）：
   - 黄金：TIPS 实际利率 > DXY > 地缘/央行购金。
   - 原油：库存+地缘航运 > DXY > OI 拥挤度。
   - 加密：TIPS+纳指 > 费率+OI+爆仓 > DXY。价涨+OI涨=多头拥挤，价涨+OI跌=空头回补，价跌+OI涨=空头进场，价跌+OI跌=去杠杆见底；8h 费率 >0.03% 偏多拥挤、>0.1% 高拥挤，负值同理；连续 3 根同号=情绪惯性。
   - 联动：实际利率下行→黄金先涨，风险偏好同时回升则 BTC 共振；地缘推油→通胀预期升→利率高→压加密；缓和/油跌→降息预期→黄金与 BTC 同利好。
   - 跨品种对比优先引用 [SERVER CROSS-ASSET COMPARE]（24h/7d 涨跌、费率、OI、1H RSI），结论必须带具体差异数据，不得凭印象。
5) 时效：对比 [新闻时间] 与该品种 15m/1h/4h 首次异动——发布前已异动=被抢跑（追单价值低），发布后异动=即时；转载/复盘即使时间新也无交易价值；超过约 45 分钟按过时处理，倾向 HOLD。

# 第二层：七段推演
reasoning_path 必须输出完整八段（每段以方括号段名开头，禁止用编号/冒号/破折号替代，缺一不可，总长≥200字）。每段 1-2 句，无数据写"无明显信号"，不得用简短摘要代替。

[总框架] 引用 prompt 注入的【核心驱动】一句（≤80字），声明该品种当前属性（避险/抗通胀/风险资产/供给驱动商品）以及该属性是增强还是削弱本条传导；禁止套话；未注入因果图谱时必须写≥40字的占位说明。
[因素定位] 定位本条新闻命中的 1-2 条传导子链（黄金：实际利率/央行购金/地缘溢价/美元避险；原油：OPEC供给/需求/库存/地缘溢价；BTC：流动性/ETF资金/监管/风险偏好），说明打在哪个变量、该变量历史弹性多大；禁列通用 Top5 排行。
[驱动力] 子链上具体什么在驱动价格，紧扣新闻主体，写出 asset_impact 打分依据（说不出依据一律≤3）；输入含 [门卫四步] 时先复述 core_impact 那根筋及其驱动，再用盘面/资金证据判断印证还是推翻；说明最近 5 小时同类信息与市场涨跌态度。
[水位博弈] 必须引用快照真实指标：RSI>70 超买、<30 超卖；ADX<20 震荡、>25 趋势；现价与 VWAP 关系；量能比>1.5x 放量、≈1x 存量。持仓拥挤度引用 COT/黄金ETF/费率，未接入写"未接入(N/A)"。判断行情是否已走完。
[跨资产资金] 至少 2-3 类真实资金数据：费率 funding、多空持仓比 ls_ratio/top_ls_ratio、OI、CVD/taker、margin_debt、us_equity_mcap、oi_usd_bybit、crypto.total_mcap、btc_etf_net、btc_dominance/eth_dominance、cot_noncomm_net。CVD 正=净买盘、负=净卖盘；缺数据写"未接入(N/A)"；单点异常/跨所口径不一/周度滞后写"数据不稳定(N/A)"并下调置信度。
[反共识结论] 给与表面叙事相反或易被忽略的具体证据（价格已 price in、7d 区间位置、费率爆仓拥挤、数据与叙事背离）；禁"尽管X…但Y谨慎"空模板。
[逻辑自检] 先假设结论是错的，用真实数据核对论点与论据；被证伪写明矛盾并降级 HOLD 或下调置信度。
[总结] 逐品种一行，写清"最终怎么看、依据、方向"，与 suggested_action 一致；禁"综上所述/总体来看"。

# 时间字段
[新闻时间] 多为入站采集时刻，不等于平台发布时间；published_at 仅部分源可用。同 batch 的 [新闻时间] 全相同视为采集失真，改用关键词+价格异动起点对位。cross_net_first_seen_ts 是全网真实首发，严禁抄 [新闻时间] 或 first_seen_ts。

# 数据缺失
硬数据标 N/A/未接入/不稳定 → 一律写 N/A，不得编造；幻觉降级 HOLD 并扣分。宏观常识可承接（如"地缘升级利好 XAU"）。技术面缺失不自动 HOLD（见铁律4）。

# 输出体量
reasoning_path 每段≤5行，不复述 prompt 原文，相同论据不重复；禁"综上所述"类空模板。

# 多品种
- sibling 三条全过才放：有明确直接传导链、能给明确方向、能写出独立的八段。说不清的品种只在 [跨资产资金] 一句带过。
- 八段内每个品种以"[品种代码]："独立成行，主品种在前；禁"同上/见主品种"，写不出要写明原因；[总结] 逐品种一行。
- 地缘同时强影响黄金与原油：主选其一，另一个必进 sibling；加密弱相关一句带过。

# sentiment_score（-1.0~1.0，两位小数，2h 窗口方向与幅度置信度）
看多 +0.15~+0.90，看空 -0.15~-0.90；HOLD 也可给方向分（HOLD≠没方向）；只有与该品种确实无方向关系才给 0.00。禁 0-100 量纲、禁一律 50。
强度参考：0.15-0.4 = 口头表态/第三方评论/监管传闻；0.45-0.7 = 直接推动供需或风险偏好（CPI/NFP 明显偏离、美元剧震、ETF 获批/拒绝、OPEC 减产、制裁升级）；0.75-0.9 = 结构突变/黑天鹅（战争爆发、主权违约、交易所破产、FOMC 意外转向）；反向取镜像。
叠加：direct_catalyst=true 上浮一档；event_strength=low 上限 0.7、medium 上限 0.9；market_confirmation=negative 下调一档；趋势同向 +0.05、反向 -0.05。

# 门卫四步（输入含 [门卫四步] 时必须核对）
字段：core_impact、main_asset、kind、timing、strength(0-10)、impact_1to100、verdict。同意 → 顺 core_impact 推演并写进 [驱动力]；不同意 → 在 [逻辑自检] 写推翻理由（基于快照真实数据），不得默默忽略；verdict=排除仍给兜底建议；无此块则自行判断。

# Hermes 四席与历史绩效（软校验）
四席多数与 suggested_action 不一致 → [逻辑自检] 说明分歧并下调置信度（宁可 HOLD），最终方向独立给出。历史同类 2h 结算胜率<30% 降置信、>70% 可上调，样本不足忽略。


# JSON 输出（只输出 JSON 对象；下列键全部给出，括号内为取值范围/枚举，只能取列出的值）
reasoning_path(八段用 → 连接) | asset_nature(≤80字) | asset_classification(避险|供给驱动|高Beta|宏观锚|政策敏感)
fund_driver(≤80字) | consensus_vs_us(一致|反共识) | factor_ranking[{factor,rank,direction:利多|利空|中性}]
impact_magnitude(1-100) | asset_reasoning(逐品种一行≤60字) | sentiment_score(-1.0~1.0)
suggested_action(BUY|SELL|HOLD) | reasoning(主品种结论≤50字) | market_category(CRYPTO|GOLD|OIL|STOCK|MACRO|COMMODITY)
target_asset(XAU|XAG|WTI|BTC|ETH|SOL|CN_STOCK|US_STOCK|HK_STOCK|INDIVIDUAL_STOCK|SKHYNIX|NONE)
prediction_type(reversal|continuation|breakout) | event_phase(early|mid|late) | market_confirmation(positive|negative|unknown)
expected_horizon(intraday|1-3d|1w+) | invalidation_condition(≤40字) | event_strength(low|medium|high)
direct_catalyst(true|false) | heat_score|impact_score|asset_impact(0-10) | attention_score(0-100)
elasticity_window{minutes,decay:fast|medium|slow,peak_after_minutes} | data_anchor_ts | hard_data_evidence
net_origin_info | cross_net_first_seen_ts | cross_net_source | sibling_decisions(副品种数组，键同主品种)
insight_hit_ids(样式 [EX#1,EX#2]，未采纳 [])

# 字段口径（只保留校验与打分真正用到的）
- asset_reasoning：主品种在前、副品种在后，各≤60字；无真实传导写 "暂无明确传导路径(N/A)"。
- asset_classification：5 选 1，NONE 时选最接近的一项；fund_driver 无结论写 "无明显驱动力(N/A)"。
- expected_horizon：intraday=分钟级、1-3d≈2天、1w+≈7天；[FRED发布]→intraday，[FRED趋势]→1-3d/1w+。
- 打分档位（heat/impact/asset_impact 同口径）：0-3 间接背景(2)、4-6 间接传导(4或6)、7-8 直接催化(7或8)、9-10 决定性(9)；必须给档内具体值，禁一律 5.0；依据写进 [驱动力]。
- direct_catalyst：true=事件直接针对该资产，false=间接传导。attention_score 仅参考，不定方向。
- 溯源三字段 net_origin_info/cross_net_first_seen_ts/cross_net_source 须同时成立；校验只拒：早于 1990、晚于入库超 60s、source 不足 4 字符。
# 铁律（输出前逐条核对）
1. reasoning_path 八段用方括号、顺序固定、缺一不可，禁编号/冒号/破折号替代。
2. suggested_action 与推演方向一致：偏多不许 SELL，偏空不许 BUY；隐含反向风险必须写出。
3. 只分析新闻主体及其直接相关资产，禁张冠李戴、跨资产硬扯。
4. 只用 prompt 提供的真实数据，缺失写 N/A，禁编造；结论必须带 hard_data_evidence（品种+时间+数值）与 data_anchor_ts。技术面缺失不自动 HOLD：基本面强信号（政策落地/地缘升级/利率决议/重大落地）仍可给 BUY/SELL，此时 market_confirmation=unknown、两字段写 N/A、asset_impact≤5。
5. 利多+已大涨+费率极端 → 警惕利好出尽；利空+已大跌+负费率极端 → 警惕空头拥挤；快照与方向矛盾 → market_confirmation=negative。
6. 防追单：利多未反应果断 BUY、已大涨不追；利空未反应果断 SELL、已大跌不追空；恐慌利空按分钟级窗口。
7. BTC 是纯风险资产：战争中 SELL、宽松中 BUY，无避险属性。
8. 观点/预测/汇总/无标的类 → HOLD（唯一保留的 HOLD 兜底场景）；缺技术面数据只改 market_confirmation，不自动 HOLD。
9. attention_score / elasticity_window / data_anchor_ts / hard_data_evidence / asset_classification / fund_driver / consensus_vs_us 必填，缺失写 N/A 或占位。
10. target_asset / key_factors / self_check / final_action 由系统从其它字段派生，你无需额外输出。
""" + _SYSTEM_PROMPT_SUFFIX
# v398g：静态知识附录（_STATIC_KNOWLEDGE）暂不拼入 system。
# 实测（21:26-21:34）：拼上后 system 变 14,669 字符、单次输入 12.9K-13.5K，
# 但 cached_tokens 仍是 128 —— 生产环境的 key 是逐批轮转的
# （minimax1 / minimax / minimax2 交替），而前缀缓存是按 key 记的，
# 换 key 就等于缓存全失效。补内容只增成本不换缓存，故撤回，
# 保留常量备将来"固定 key + 需要更多静态知识"时再用。

# ── v323 输出契约（恒定附加在 system prompt 最末尾）──────────────────────
# 事故（2026-11-12）：prompt 已 15,843 字，模型把八段推演写在 JSON 外面，
# 引擎只解析 JSON 字段 → reasoning / asset_nature / factor_ranking 全空 →
# schema 校验失败 → 整条分析被丢弃，一条决策都产不出来。同一模型给明确字段清单时
# 9.6 秒就返回完整字段。所以末尾（模型最重视的位置）再加一条硬约束。
# v398f 瘦身：本段与正文有重复，压缩为一屏可读的 3 条，字段清单以正文 JSON 模板为准。
_OUTPUT_CONTRACT = """

═══ 输出格式铁律（最高优先级，覆盖上文任何"分段落/写标题/结尾总结"的写法）═══
1) 整条回复必须是一个 JSON 对象：从 { 开始到 } 结束，前后不得有解释、标题、总结或 markdown 代码围栏。
2) 所有分析内容写进 JSON 字段里：reasoning_path 字符串、reasoning、asset_nature、factor_ranking（≥1 项含 factor/rank/direction）、suggested_action（BUY|SELL|HOLD）、target_asset、market_category、sentiment_score(-1~1)。
   reasoning_path 必须依次包含这 8 个方括号标题（**原样**，不得改写、不得省段，每段正文≥20 字）：
   [总框架] [因素定位] [驱动力] [水位博弈] [跨资产资金] [反共识结论] [逻辑自检] [总结]
   段与段之间用 \\n 分隔；整段≥100 字。少一段就等于"因果链断裂"，会被审计判 reject。
3) 宁可字段写得短，也不能把内容写在 JSON 外面。
""" + _SYSTEM_PROMPT_SUFFIX
_JSON_PROMPT_FORCE = _SYSTEM_PROMPT  # v170 改造：双通道已废弃，统一用 _SYSTEM_PROMPT；
                                 # 保留别名仅为兼容 /api/ai/prompts 展示与 ratio 计算引用。
                                 # 历史：原本是独立短版 prompt（含两层分析+八段说明+JSON 模板），
                                 #       与 _SYSTEM_PROMPT 重复约 70 行（~1100 tokens），
                                 #       实际分析路径已 100% 走 _SYSTEM_PROMPT（use_full_prompt=True）。


# ── 盘面结构分析指令块（按后台开关动态拼接）────────────────────────────────
# 开关开：附加到水位博弈段（多周期结构判断）；关：不附加（纯筹码情绪判断）。
# 即时新闻交易可关闭结构分析，避免结构判断影响追单/急拉决策。

# ── 八段推理链强制校验 ──────────────────────────────────────────────
# v176.2 结构改革：删掉"主体识别"+"资产性质"+"因素排行" 三段（靠 prompt 注入的
# 因果图谱回灌块承担，避免 LLM 凭印象写错黄金叙事），改为六段 + 总框架前置：
#   1) [总框架] → 引用 prompt 注入的"因果图谱回灌"硬先验（核心驱动/资金面/传导链）
#   2) [因素定位] → 在总框架下定位本新闻属于哪条传导路径，影响哪条子链
#   3) [驱动力] → 在该子链上具体什么在驱动价格（不脱离总框架）
#   4) [水位博弈] → 该品种当前盘面位置（RSI/ADX/VWAP/量能）
#   5) [跨资产资金] → ETF/COT/杠杆资金动向
#   6) [反共识结论] → 与表面叙事相反的具体证据点
#   7) [逻辑自检] → 反例验证 + 自我降级
# 校验 reasoning_path 是否按新格式输出；不完整（AI 自由发挥/漏段）时带纠正提示重试 1 次。

# 段名标记（用于按段切分 reasoning_path，供"副品种逐段覆盖"校验）。
# 与展示层 _MARKER_RE 同语义：段名后必须紧跟分隔符，避免误切正文里的同名词。
# 含 [总结]：用户要求"有关系就要有分析+总结"，总结是八段之后的独立段（按品种分行）。


def _lean_schema_fill(missing: List[str], news_text: str, result: Dict[str, Any],
                      *, max_tokens: int = 900) -> str:
    """v398d：只补「缺口字段」的精简调用（≈2K prompt），不再重跑 13K 全量 prompt。

    事故（2026-11-16 19:26 实测）：一批 8 条跑了 10+ 分钟，日志里每条新闻都出现
    `schema retry 补全 2/8 字段` —— 那一次 schema retry 走的是 `_do_api_call`，
    **原样的 17K system + 11K user 再发一遍**，只为了补几个字符串字段；
    补完还可能只填上 2 个、于是再触发两步抢救。单条新闻 2-4 分钟、13K×2 的额度
    就是这么烧掉的，队列自然追不上。

    这里改成：把新闻原文截到 1.2K + 已判定的标的/方向/分数作为已知条件，
    只问缺的那几个字段。prompt 从 ~28K 降到 ~2K，延迟从 45s 降到 3-6s。
    模型给不出（返回空/解析失败）时，调用方仍会退回原来的全量 retry，不降级质量。
    """
    try:
        if not missing:
            return ""
        _known = (
            f"target_asset={result.get('target_asset')}，"
            f"suggested_action={result.get('suggested_action')}，"
            f"impact_score={result.get('impact_score')}，"
            f"market_category={result.get('market_category')}"
        )
        prompt = (
            "【任务】只补全缺失字段，不要重写已有判断，不要重复已判定内容。\n"
            f"【新闻原文（截断）】{str(news_text or '')[:1200]}\n"
            f"【已判定】{_known}\n"
            f"【只输出这些字段】{'、'.join(missing)}\n"
            "【字段要求】data_anchor_ts=行情快照时间字符串；"
            "hard_data_evidence=你实际引用的真实数据(品种+时间+数值)，没有就 \"N/A\"；"
            "elasticity_window={\"minutes\":整数,\"decay\":\"fast|medium|slow\","
            "\"peak_after_minutes\":整数}；verification=中文自检结论；"
            "asset_classification=避险/抗通胀|供给驱动|高Beta|宏观锚|政策敏感 之一；"
            "fund_driver=不超过80字的驱动力；consensus_vs_us=一致|反共识；"
            "factor_ranking=数组，每项包含 factor/weight(0-100)/direction(利多|利空|中性)；"
            "reasoning=不超过120字的结论；asset_nature=不超过40字的资产性质。"
            "严禁编造数值，没有就写 \"N/A\"。只输出一个 JSON 对象。"
        )
        return chat(prompt, purpose="main", max_tokens=max_tokens,
                    call_site="schema_fill_lean") or ""
    except Exception:
        return ""


_RESCUE_LIMIT_PER_WINDOW = int(os.getenv("TRIDENT_TWO_STEP_RESCUE_LIMIT", "10") or 10)

# v398：target=NONE 的新闻单独一份预算。
# 依据（2026-11-16 真库）：442 条降级里 273 条（62%）是 target_none —— 这类新闻
# 本身就不讲核心品种（个股/赛事/宏观汇总），第一步几乎必然回 NONE，抢救是白烧。
# 但它和"标的已锁、只缺八段"的新闻共用同一个 10 条窗口：一批 8 条 NONE 就能把窗口
# 吃干净（日志实测 窗口已用 6/10 … 9/10），真正可救的那类反而抢不到预算。
# 现在 NONE 类最多占 NONE 预算，默认 3 条 / 10 分钟，其余额度留给有标的的。
_RESCUE_NONE_LIMIT_PER_WINDOW = int(
    os.getenv("TRIDENT_TWO_STEP_RESCUE_NONE_LIMIT", "3") or 3)

# v409: server_fallback（服务端模板回灌）单独一份预算。
# 它不是"模型没被问"，而是这一轮主调用被上游打回 —— 重试的期望收益比 target_none 高，
# 但它和 llm_empty 抢同一个 10 条窗口，实测常被前者吃光（日志里 "窗口已用 9/10"）。
# 默认 4 条 / 10 分钟，可关。
_RESCUE_SF_LIMIT_PER_WINDOW = int(
    os.getenv("TRIDENT_TWO_STEP_RESCUE_SF_LIMIT", "4") or 4)

# v414：单批抢救条数上限。
# 依据（2026-11-17 实测）：上面的滑动窗口只限"每 10 分钟几条"，不限"一批里几条"。
# 一批 8 条如果全部命中抢救，每条要 +2 次调用（约 +45~90s），整批落库被拖到
# 15 分钟以上 → progress 停在 analyze-done → 看门狗判挂死 → 重启 → 整批退回 PENDING
# → 永远跑不完。而落库阶段拖长的代价不是"慢"，是**新闻过期**：
# 日志 `信号过期，跳过建仓: news=#32145 已 100 分钟（> 45 分钟）` 今日出现 20 次，
# 意味着分析做完了但不下单 —— 产能问题直接变成"0 笔成交"。
# 现在每批最多救 N 条，剩下的留到下一批（它们仍在队列里，不会丢）。
# 0 = 关闭该限制（恢复旧行为，便于对比）。
_RESCUE_MAX_PER_BATCH = int(
    os.getenv("TRIDENT_TWO_STEP_RESCUE_MAX_PER_BATCH", "3") or 3)

# ── v424：批末 spool（防外部 SIGTERM 打断导致整批成果作废）────────────────────
# 背景（2026-11-17 实测）：外部会话周期性 `launchctl kickstart -k` 重启引擎。
# 18:08:06 那一次正好落在 `[BATCH-RESULT] 成功 6 / 失败 0` 之后、`_batch_persist()`
# 之前 —— 6 条已完成的主分析（每条 ~12–13k prompt）随进程蒸发，启动复位又把它们
# 退回 PENDING 重跑 → 认领多次、落库 0 批，token 全烧在重复推演上。
# 现在：分析一出结果就先写一行 JSONL 到 runtime/ai_batch_spool.jsonl；落库成功后
# 清空。若进程中途被杀，下次启动先把 spool 补写进 ai_decisions —— 补写完
# `_STARTUP_RECOVER_DONE_SQL` 会把对应 raw_news 直接标 DONE，不会再重跑。
_SPOOL_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "runtime", "ai_batch_spool.jsonl",
)


_SPOOLED_NEWS_IDS: set = set()


def _spool_batch(successes) -> None:
    """把本批已分析出的结果追加到 spool（只留补库必需的字段）。

    v493：同一进程内按 news_id 去重 —— 逐条落盘（见 tasks 包装处）之后，
    批次收尾还会再调一次本函数，没去重就会把同一条分析写两遍
    （drain 端虽然按 news_id 去重，但 spool 文件会无谓膨胀、也不好排查）。
    """
    if not successes:
        return
    os.makedirs(os.path.dirname(_SPOOL_PATH), exist_ok=True)
    with open(_SPOOL_PATH, "a", encoding="utf-8") as fh:
        for s in successes:
            r = s.get("result") or {}
            rec = {
                "news_id": int(s.get("news_id") or 0),
                "suggested_action": str(r.get("suggested_action") or "HOLD"),
                "sentiment_score": float(r.get("sentiment_score") or 0.0),
                "reasoning": str(r.get("reasoning") or r.get("reasoning_path") or "")[:2000],
                "target_asset": str(r.get("target_asset") or "NONE"),
                "market_category": str(r.get("market_category") or "OTHER"),
                "created_at": datetime.now().astimezone().isoformat(timespec="milliseconds"),
            }
            if rec["news_id"] > 0:
                if rec["news_id"] in _SPOOLED_NEWS_IDS:
                    continue
                _SPOOLED_NEWS_IDS.add(rec["news_id"])
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _spool_clear() -> None:
    try:
        if os.path.exists(_SPOOL_PATH):
            os.remove(_SPOOL_PATH)
    except Exception:
        pass


def _spool_drain(conn) -> int:
    """启动时把 spool 里没落库的结果补写成决策行，返回补写条数。"""
    if not os.path.exists(_SPOOL_PATH):
        return 0
    rows = []
    try:
        with open(_SPOOL_PATH, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        return 0
    n = 0
    _sp_orphan = 0
    try:
        for rec in rows:
            nid = int(rec.get("news_id") or 0)
            if nid <= 0:
                continue
            if conn.execute(
                "SELECT 1 FROM ai_decisions WHERE news_id = ? LIMIT 1", (nid,)
            ).fetchone():
                continue
            # v453: 新闻已被重置/清理删掉时，ai_decisions.news_id 的外键必然失败，
            # 而整条 INSERT 挂在 for 循环里 —— 一条坏记录会把**整批**补写全部打断
            # （11-19 实证：IntegrityError: FOREIGN KEY constraint failed，8 条已完成
            # 的分析全丢）。落库前先确认源新闻还在，不在就跳过并计数。
            if not conn.execute(
                "SELECT 1 FROM raw_news WHERE id = ? LIMIT 1", (nid,)
            ).fetchone():
                _sp_orphan += 1
                print(f"  [SPOOL] 跳过孤儿分析 news_id={nid}（源新闻已被删除，避免外键打断整批）",
                      flush=True)
                continue
            # v421: spool 补写原先不带 reasoning_path、也不过门禁 ——
            # 12495/12496 就是这样漏审的（短 reasoning、空路径、audit_gate_log 无记录）。
            _cur = conn.execute(
                "INSERT INTO ai_decisions"
                " (news_id, sentiment_score, suggested_action, reasoning,"
                "  reasoning_path, created_at, market_category, target_asset)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (
                    nid,
                    float(rec.get("sentiment_score") or 0.0),
                    str(rec.get("suggested_action") or "HOLD"),
                    str(rec.get("reasoning") or ""),
                    str(rec.get("reasoning_path") or rec.get("reasoning") or ""),
                    str(rec.get("created_at") or ""),
                    str(rec.get("market_category") or "OTHER"),
                    str(rec.get("target_asset") or "NONE"),
                ),
            )
            # v453: spool 补写链也要落指纹 —— 11-19 实测 4/4 条新决策都出自这条路径、
            # 指纹全空，只挂主分析链等于没做。
            try:
                from code_fingerprint import stamp as _cfp_stamp_sp
                _cfp_stamp_sp(conn, _cur.lastrowid)
            except Exception as _cfp_sp_err:
                print(f"  [FINGERPRINT:spool] skipped: {type(_cfp_sp_err).__name__}: {_cfp_sp_err}",
                      flush=True)
            try:
                import audit_gate_hook as _agh_sp
                _agh_sp.gate(conn, int(_cur.lastrowid or 0), tag="spool")
            except Exception as _agh_sp_e:
                print(f"  [AUDIT-GATE:spool] skipped: {type(_agh_sp_e).__name__}: {_agh_sp_e}",
                      flush=True)
            n += 1
        if n or _sp_orphan:
            print(f"[AI] spool 补写落库：{n} 条（上次被杀时已完成的分析，不再重跑）"
                  + (f"；跳过孤儿 {_sp_orphan} 条" if _sp_orphan else ""),
                  flush=True)
        return n
    except Exception as e:
        print(f"[AI] ⚠️ spool 补写失败: {type(e).__name__}: {str(e)[:100]}", flush=True)
        return 0


# v415：启动复位的两条 SQL（抽成模块级常量，便于单测；逻辑见 ai_worker() 里的注释）。
# 事故（2026-11-17）：引擎被外部反复重启，每次启动都把 PROCESSING 全退回 PENDING。
# 但其中一部分**决策行已经落库了**（同批先后 INSERT，落库中途被杀），退回后会被
# 重新认领、重新分析 → 白烧额度 + 产出重复决策。实测当日 10 条在排队等重跑。
# 现在先"捞回"已落库的（标 DONE），只把真正没落成决策的退回队列。
# v415 续：**PENDING 且有决策** 的行也要扶正为 DONE。
# 它们不会被重新认领（_CLAIM_DEDUP 挡着），但状态一直挂着 PENDING，
# 让「待分析」计数虚高、前端看起来像积压。实测 2026-11-17 有 10 条
# （其中 32097 WTI / 32099 XAU 是真可交易），全部是重启中途被退回造成的。
# 仅在不允许重分析的模式下执行（TRIDENT_ALLOW_REANALYZE=1 是回归重放场景，
# 那时确实要保留 PENDING 以便重跑）。


def _rescue_log(record: dict) -> None:
    """把抢救结果写一行 JSONL（诊断用，失败绝不影响主流程）。

    为什么要有：72 条 llm_empty 一直"补不回来"，但没人知道是**没被选中**（预算耗尽）、
    **没文本**（raw_news 空）、还是**补了还是空**。这三件事的修法完全不同。
    文件固定 `backend/runtime/rescue_log.jsonl`，按天滚动不需要——体量按条算，很小。
    """
    try:
        import json as _json
        import time as _t
        from pathlib import Path as _P
        p_ = _P(__file__).resolve().parents[2] / "runtime" / "rescue_log.jsonl"
        p_.parent.mkdir(parents=True, exist_ok=True)
        rec = dict(record)
        rec["ts"] = _t.strftime("%Y-%m-%d %H:%M:%S", _t.localtime())
        with open(p_, "a", encoding="utf-8") as f:
            f.write(_json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def rescue_budget_ok(now_ts: float, state: Dict[str, Any],
                     limit: int = None, window: float = None) -> bool:
    """两步抢救的滑动窗口限流（每 10 分钟最多 N 条）。

    为什么需要：每条抢救要多 2 次调用（约 +45~90s），不限流时一个 8 条的批次会被
    拖到十几分钟，后面的新闻全排队等。这里只限制"抢救"，正常分析不受影响。
    """
    lim = _RESCUE_LIMIT_PER_WINDOW if limit is None else int(limit)
    win = _RESCUE_WINDOW_SEC if window is None else float(window)
    start = float(state.get("window_start") or 0.0)
    if start <= 0 or (now_ts - start) >= win:
        state["window_start"] = now_ts
        state["used"] = 0
    if int(state.get("used") or 0) >= lim:
        return False
    state["used"] = int(state.get("used") or 0) + 1
    return True


# v420：**只含品种名**的关键词表（用于判断"新闻是否提到可交易标的"）。
# 刻意剔除 news_router.POOL_KEYWORDS 里的宏观驱动词（美联储/通胀/CPI/非农/
# 关税/中东/制裁/央行/降息/降准…）——那些是"影响品种的原因"，不是"提到了品种"。
# 用 POOL_KEYWORDS 的后果实测：'美联储官员表示通胀仍需观察' 会命中"美联储""通胀"
# → 被判"提到品种" → 快路径对宏观新闻**永远命中不了**，而宏观恰是 NONE 的大头。


# v420：`market_category` 里哪些取值算"具体品种"（说明模型看到了标的）。
# 其余（空 / MACRO / 宏观 / 政策 / 地缘 / 行业 / 评论 …）都是非品种分类，
# 与 target_asset=NONE 自洽，不构成"可能有标的没锁住"的证据。


def rescue_budget_state_for(target_asset: Any) -> tuple:
    """v398：按标的返回 (预算状态, 上限)。

    NONE 类走独立窗口（默认 3/10min），有标的的仍用原来的 10 条窗口 ——
    避免"一批 8 条 NONE"把真正可救的额度吃干（实测日志 窗口已用 6/10…9/10）。
    """
    if is_none_target(target_asset):
        return _RESCUE_NONE_STATE, _RESCUE_NONE_LIMIT_PER_WINDOW
    return _RESCUE_STATE, _RESCUE_LIMIT_PER_WINDOW


# ── v346 精简两步通道：把"只思考不回答"从根上绕开 ──────────────────────────
# 实测（v342~v345）：生产 prompt 13.2K tokens 时，MiniMax-M3 常把 900~1300
# completion tokens 全花在 <think> 里，正文为空 → 记为 llm_empty。
# 同一模型在 2K 以内的短 prompt 上则稳定出 JSON。
# 因此两步抢救不再复用全量 prompt（原来每一步都是 13K），改为：
#   ① 短 prompt（~2K）只定 标的/方向/分数/品类 —— 实测 12/12 成功
#   ② 中 prompt（~6K）只要八段与证据，新闻原文 + ①的结论
# 单条抢救成本从 26K 降到 ~8K，且成功率显著提高。


# ── v422 精简"补总结"通道 ────────────────────────────────────────────────
# 背景：主分析跑完后有一条"缺 [总结] 段 → retrying with summary hint"，
# 原来走的是**全量 prompt 再跑一遍**（13.5K token/次）。而 11-16 的 504 条决策里
# 499 条是 HOLD —— 为一句"观望"的总结再付一次 13.5K，是当天最大的一笔白烧。
# 现在：HOLD/NONE 用这条 ~300 token 的短通道只补 [总结] 段再拼回去；
# BUY/SELL（真下单）仍走原来那次全量重试，保证推演完整。


# _parse_result_light 已外迁 engine.worker_helpers（import * 接回）


def _maybe_repair_sections(news_text: str, asset: str, action: str,
                           res: Dict[str, Any], model_cfg: Optional[Dict[str, Any]],
                           news_ts: str = "", *, tag: str = "") -> bool:
    """v423: 方向单八段不全时，用精简通道（mode=b）重写一次 reasoning_path。

    只补这一段，不动标的/方向/分数/因子；补不动就原样返回 False（宁可 warn 不造假）。
    抽成独立函数是为了可单测（注入 stub 的 two_step_llm_call 即可，不碰网络）。
    """
    try:
        from review.reflection_checkers import (
            _ANALYSIS_STEPS as _AS,
            _extract_reasoning_steps as _ERS,
        )
        if action not in ("BUY", "SELL"):
            return False
        _rp = str(res.get("reasoning_path") or "")
        _miss = [x for x in _AS if x not in _ERS(_rp)]
        if len(_miss) < 2:
            return False
        if not _miss:
            return False
        if not model_cfg:
            # 区分"不需要补"和"补不了"：模型池冷却/为空时必须留痕，否则现场只能看到
            # 一段短路径却查不到为什么没补。
            print(f"  [SECTION-REPAIR] {tag} {asset} 缺{len(_miss)}段，但模型池无可用配置 → 跳过",
                  flush=True)
            return False
        if not str(news_text or "").strip():
            print(f"  [SECTION-REPAIR] {tag} {asset} 缺{len(_miss)}段，但取不到新闻正文 → 跳过",
                  flush=True)
            return False
        raw = two_step_llm_call(
            model_cfg, mode="b", news_text=news_text, timestamp=news_ts,
            asset=asset, action=action, timeout=60.0, call_site="section_repair",
        ) or ""
        _rp_new = str((_parse_result_light(raw).get("reasoning_path") or "")).strip()
        _miss_new = [x for x in _AS if x not in _ERS(_rp_new)]
        if _rp_new and len(_miss_new) <= len(_miss) - 1:
            res["reasoning_path"] = _rp_new
            print(f"  [SECTION-REPAIR] {tag} {asset} 缺{len(_miss)}段 → 缺{len(_miss_new)}段",
                  flush=True)
            return True
        print(f"  [SECTION-REPAIR] {tag} {asset} 补写无效（仍缺{len(_miss_new)}段），保留原样",
              flush=True)
    except Exception as _e:
        print(f"  [SECTION-REPAIR] skipped: {type(_e).__name__}: {_e}", flush=True)
    return False


def two_step_llm_call(model_cfg: Dict[str, Any], *, mode: str, news_text: str,
                      gate_context: str = "", timestamp: str = "",
                      asset: str = "", action: str = "", timeout: float = 45.0,
                      call_site: str = "two_step") -> str:
    """v346：精简两步调用（不经过全量 prompt 组装）。

    mode="a" → 只要标的/方向/分数；mode="b" → 只要八段与证据。
    返回正文文本（可能为空串，调用方自行降级）。
    """
    _m = str(mode or "").strip().lower()
    if _m == "a":
        system = _STEP_A_LEAN_SYSTEM
        max_tokens = 400
    elif _m == "summary":
        # v422：只补 [总结] 段（HOLD/NONE 用，避免为一句"观望"再跑全量 13.5K）
        system = _SUMMARY_LEAN_SYSTEM.format(asset=asset or "NONE",
                                             action=action or "HOLD")
        max_tokens = 400
    else:
        system = _STEP_B_LEAN_SYSTEM.format(asset=asset or "NONE", action=action or "HOLD")
        max_tokens = 2200
    _head = ""
    if timestamp:
        _head = f"[新闻时间] {str(timestamp)[:19]}\n"
    user = f"{_head}{str(news_text or '')[:1600]}"
    if gate_context:
        user = user + "\n\n" + str(gate_context)[:300]
    payload: Dict[str, Any] = {
        "model": model_cfg.get("request_model") or model_cfg.get("id"),
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": 0.1,
        "max_tokens": max_tokens,
    }
    # 只在通道显式要求 JSON 时启用（部分端点 response_format 会引发空回答）
    if str(os.getenv("TRIDENT_TWO_STEP_JSON_MODE", "0")).strip() == "1":
        payload["response_format"] = {"type": "json_object"}
    _exp = dict(model_cfg.get("extra_body") or {})
    _exp.pop("max_tokens", None)
    payload.update(_exp)
    if _action := str(os.getenv("TRIDENT_TWO_STEP_THINKING", "disabled")).strip():
        payload.setdefault("thinking", {"type": _action})
    headers = {"Authorization": f"Bearer {model_cfg['api_key']}",
               "Content-Type": "application/json"}
    url = f"{model_cfg['api_base']}/chat/completions"
    _sess = _LLM_PROXY_SESSION if model_cfg.get("use_proxy") else _LLM_SESSION
    _alias = str(model_cfg.get("key_alias") or model_cfg.get("label") or "unknown")
    # ── v343: 额度闸门 ─────────────────────────────────────────────
    # v346 新增的两步通道（step A 锁标的 / step B 补八段）是真实计费请求，
    # 但当时直接裸发，被 audit_llm_gates.py 判为"未受保护的 LLM 调用点"。
    # 这里补上与主链路同一套 checks：额度 + 速率。
    try:
        from common import llm_budget as _lb2
        _ok_b, _why_b = _lb2.check(0)
        if not _ok_b:
            raise RuntimeError(f"budget_blocked: {_why_b}")
        _ok_r, _why_r = _lb2.consume_rate_slot(purpose="two_step")
        if not _ok_r:
            raise RuntimeError(f"rate_limited: {_why_r}")
    except RuntimeError:
        raise
    except Exception:
        pass  # 闸门自身异常 → 不因保护机制故障停摆

    _t0 = time.perf_counter()
    resp = _sess.post(url, json=payload, headers=headers,
                      timeout=float(timeout or 45.0))
    _lat = int((time.perf_counter() - _t0) * 1000)
    try:
        import db as _db2
        if resp.status_code >= 400:
            _db2.log_ai_call(purpose="main", key_alias=_alias,
                             model_id=str(model_cfg.get("id") or "?"),
                             tokens_in=0, tokens_out=0, latency_ms=_lat,
                             status="http_error", error_code=str(resp.status_code),
                             call_site=call_site)
            raise RuntimeError(f"{model_cfg.get('label')} HTTP {resp.status_code} ({_m})")
        body = resp.json()
        txt = (body.get("choices", [{}])[0].get("message", {}) or {}).get("content") or ""
        _u = body.get("usage") or {}
        _db2.log_ai_call(purpose="main", key_alias=_alias,
                         model_id=str(model_cfg.get("id") or "?"),
                         tokens_in=int(_u.get("prompt_tokens") or 0),
                         tokens_out=int(_u.get("completion_tokens") or 0),
                         cached_tokens=int((_u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0),
                         latency_ms=_lat, status="ok", error_code="", call_site=call_site)
    except RuntimeError:
        raise
    except Exception:
        pass
    return str(txt)


# ── 针对性闸门（单一实现，主品种路径与副品种路径共用）──────────────────────
# 原则：方向单必须建立在"针对该品种的分析"之上。八段里提都没提到它 → 依据不足 →
# 不给方向单（降级 HOLD，不建仓）。阈值 2 段：只提 1 段往往是"跨资产顺带点名"，
# 不构成对它的分析。
# 原因：XAU/WTI/BTC 等主品种在 [总框架] 段就会被点名为锚品种，
# 即使后续段落不再重复提及（如白银 ETF 短新闻），也已是"主品种在分析中"。
# 原来 >=2 段把这类"短新闻·主品种锚定"误判为"分析无针对性"标 FAILED，
# 导致 reasoning_path 被服务端兜底覆盖，前端显示一片 N/A。
# 不参与针对性判定："没有可交易标的"与"其它品类"本来就不该有品种分析
# 关键词推断兜底结果没有八段，不参与判定

# 闸门降级后的 reasoning 前缀（主/副品种各一份）。
# 它既是给用户看的理由，也是"这条已被针对性闸门降级"的标记 —— 下游分支据此
# 避免把降级结果换成别处的方向单。
# 主品种「没分析」不再伪装成 HOLD 观望 → 实事求是标记「分析失败」
_FOCUS_GATE_PREFIX = _FOCUS_GATE_PREFIX_BY_ROLE["主品种"]  # 兼容既有引用


def _focus_downgrade(result: Dict[str, Any], asset: str = "",
                     path: str = "", role: str = "主品种",
                     reason: str = "") -> tuple:
    """针对性闸门：某品种八段提及段数不足却给了方向单 → 就地降级。

    主品种标「分析失败」(FAILED)，副品种降为 HOLD：
    - 主品种没分析到 = 本次分析失效，实事求是报「分析失败」，绝不伪装成观望；
    - 副品种无独立分析 / 主品种未立住 = 副品种不给方向单（观望）。
    返回 (是否降级, 提及段数)。asset 为空时自动取 result.target_asset。
    reason 非空时用它当降级说明（用于"主品种未立住 → 副品种无依据"这类情形）。
    被降级时写回 suggested_action/sentiment_score/reasoning，调用方需自行同步
    局部变量（下方两处调用点均已同步，历史踩过"日志显示已降级、结果仍是 BUY"）。
    """
    if not isinstance(result, dict):
        return (False, 0)
    code = str(asset or result.get("target_asset") or "").strip().upper()
    if code in _FOCUS_EXEMPT_ASSETS:
        _signal_trace("gate", asset=code, verdict="豁免(无品种)",
                      action=str(result.get("suggested_action") or ""))
        return (False, 0)
    body = str(path or result.get("reasoning_path") or "")
    if not body or body.startswith(_FOCUS_EXEMPT_PATH_PREFIX):
        _signal_trace("gate", asset=code, verdict="豁免(无八段)",
                      path_len=len(body),
                      action=str(result.get("suggested_action") or ""))
        return (False, 0)
    hits = _asset_mentioned_stage_count(body, code)
    action_in = str(result.get("suggested_action") or "").upper()
    if not reason and hits >= _FOCUS_MIN_MENTIONED_STAGES:
        _signal_trace("gate", asset=code, verdict="达标放行", hits=hits,
                      action=action_in, path_len=len(body))
        return (False, hits)
    if action_in not in ("BUY", "SELL"):
        _signal_trace("gate", asset=code, verdict="本就无方向单", hits=hits,
                      action=action_in, path_len=len(body))
        return (False, hits)  # 本来就是 HOLD，无需降级
    # v406: 降级 ≠ 删除。模型给过的方向与分数要留在 result 上，
    # 落库时进 original_action + gate_chain（带 raw score）。
    # 实测 62 条 summary_only 里有 22 条带 BUY/SELL，过去被这里直接丢掉，
    # 导致「方向样本只有 3 条」——准不准根本无从验证。
    result.setdefault("_raw_action", action_in)
    result.setdefault("_raw_score", result.get("sentiment_score"))
    if role == "副品种":
        # 副品种「无独立分析 / 主品种未立住」→ 副品种不给方向单（观望），不是「分析失败」
        result["suggested_action"] = "HOLD"
        result["sentiment_score"] = 0.05
        result["reasoning"] = (
            reason or f"分析未落到{role}（八段仅 {hits} 段提及 {code}），依据不足故降级观望"
        )
        _signal_trace("gate", asset=code,
                      verdict="已降级HOLD(主品种未立住)" if reason else "已降级HOLD",
                      hits=hits, action=action_in, path_len=len(body), note=reason[:80])
    else:
        # 主品种「没分析」（八段没落到主品种）→ 实事求是标「分析失败」，绝不伪装成观望
        result["suggested_action"] = _FAILED_ACTION
        result["sentiment_score"] = 0.0
        result["reasoning"] = (
            f"{_FAILED_GATE_PREFIX}：{code} 八段未被有效分析（仅 {hits} 段提及）"
        )
        _signal_trace("gate", asset=code, verdict="分析失败(主品种无有效分析)",
                      hits=hits, action=action_in, path_len=len(body))
    return (True, hits)


# ── v176.5f: 闸门归因记录器（gate_chain 落库 + 自动评估）────────
# 每条决策落库时附带 3 个新字段:
#   gate_chain      TEXT    JSON 数组: [{gate, verdict, reason}, ...] 闸门调用链
#   original_action TEXT    LLM 给出的原始方向单 (BUY/SELL/HOLD)
#   ungated_action  TEXT    如果所有闸门都不拦，会用什么 action（影子模式）
# 设计目标:
#   - 用 SQL 直接回答"过去 24h 哪些闸门拦截最多 BUY"
#   - 用 ungated_action + forward_verdict 反事实评估"闸门过度保守还是正确"
#   - 不影响现有逻辑，仅做旁路观测 + 结构化留痕
class _GateRecorder:
    __slots__ = ("entries", "original_action", "ungated_action", "final_action")

    def __init__(self, llm_action: str):
        self.entries: List[Dict[str, Any]] = []
        self.original_action = str(llm_action or "").upper()
        self.ungated_action = self.original_action  # 影子模式初值=LLM 原始
        self.final_action = self.original_action     # 最终落库 action

    def record(self, gate: str, verdict: str, reason: str = "") -> None:
        """记录一次闸门判定。verdict='pass' / 'block' / 'skip'"""
        self.entries.append({
            "gate": gate,
            "verdict": verdict,
            "reason": str(reason or "")[:200],
        })

    def apply_block(self, gate: str, reason: str = "") -> None:
        """闸门判定 block：把 final_action 强制 HOLD，ungated_action 保持 LLM 原始。"""
        self.record(gate, "block", reason)
        self.final_action = "HOLD"

    def apply_pass(self, gate: str, reason: str = "") -> None:
        """闸门判定 pass：不改 action，仅记录。"""
        self.record(gate, "pass", reason)

    def apply_skip(self, gate: str, reason: str = "") -> None:
        """闸门被强信号跳过 (v176.5e strength>=_GATE_PASS_THR 路径)：记录但不拦。"""
        self.record(gate, "skip", reason)

    def to_json(self) -> str:
        return json.dumps(self.entries, ensure_ascii=False)

    def blocked_count(self) -> int:
        return sum(1 for e in self.entries if e.get("verdict") == "block")

    def blocked_gates(self) -> List[str]:
        return [e.get("gate", "") for e in self.entries if e.get("verdict") == "block"]


# ── v174: 事件结构化驱动的交易闸门 ────────────────────────────────
# 在 LLM 输出后、INSERT ai_decisions 之前同步拦截。三类拦截:
#   1. structured_check.verdict=fabricated (LLM 自检说不一致) → 强制 HOLD
#      (但 INSERT 时 judge 还没跑,这是 v174 schema 校验阶段的本地等价物)
#   2. verification 字段以 "fail" 开头 → LLM 自报存在字段矛盾 → 强制 HOLD
#   3. snapshot_proximity 数字 ≥ 60 (分钟) → 引用快照已严重滞后 → HOLD
#   4. event_type_code 空 + factor_ranking 空 → 结构化字段全缺, 信息不足 → HOLD
# 不拦截: structured_input 缺失 (数据库层兜底), reasoning 文本 OK (说明 LLM 给得出分析)
# 这些拦截只动 suggested_action / no_trade_reason,绝不丢整条 reasoning_path (用于诊断)。


# ── 传导机制闸门 ────────────────────────────────────────────────────
# 用户原则（原话）：
#   "新闻和主品种、副品种有关系，是指新闻的内容会影响到它们的价格波动，
#    这个影响可能大也可能小（影响小的就不下单交易），而不是指新闻某字和它们有关系。"
#
# 提示词里早已写了"文本提到该品种名 ≠ 新闻显著影响该品种"和准入三条自检，
# 但**代码从不检查**——只查"提及段数 >= 2"。于是"顺带点名"与"有传导链"在代码眼里
# 是同一件事。这里把关：方向单必须有该品种自己的传导链措辞，
# 说不清"事件 → 该品种价格"的就不给方向单（影响小/说不清 → 观望，符合用户规则）。
#
# 注意「因…而…」是中文最常见因果句式（"可能因地缘风险上升而受到资金流入"），
# 必须单独覆盖，否则会把明显有传导的句子误判成"无传导"。


def _mechanism_downgrade(result: Dict[str, Any], asset: str = "", strength: int = 0) -> bool:
    """传导机制闸门：方向单但说不出传导链 → 降级 HOLD。返回是否降级。

    与针对性闸门互补：
      · 针对性闸门 = 有没有【分析】（提及 >= 2 段）
      · 本闸门    = 有没有【传导】（说出事件如何影响该品种价格）
    两者都过，方向单才成立：有分析 + 有传导。

    v176.5: strength>=8 跳过本闸门 (信任 LLM 强信号, 不再因没写出传导链就降级)
    """
    if not isinstance(result, dict):
        return False
    if not MECHANISM_GATE_ENABLED:
        return False
    if strength >= 8:
        return False  # 强信号不降级
    code = str(asset or result.get("target_asset") or "").strip().upper()
    if code in _FOCUS_EXEMPT_ASSETS:
        return False
    body = str(result.get("reasoning_path") or "")
    if not body or body.startswith(_FOCUS_EXEMPT_PATH_PREFIX):
        return False
    if str(result.get("suggested_action") or "").upper() not in ("BUY", "SELL"):
        return False
    if _asset_has_mechanism(body, code):
        return False
    result["suggested_action"] = "HOLD"
    result["sentiment_score"] = 0.05
    result["reasoning"] = (
        f"未能写出 {code} 的传导链（新闻如何影响其价格），影响不明确故不给方向单"
    )
    _signal_trace("mechanism", asset=code, verdict="无传导链→降级HOLD",
                  path_len=len(body))
    return True


# 空骨架：模型只把段名用箭头连写回显（`[总框架]→[因素定位]→…`），
# 段间无实质正文。_six_stage_complete 只看段名是否出现，会误判这种输出为"完整"，
# 需单独识别并触发格式重试（链路上格式重试不受 _is_slow_channel 限制，MiniMax 也生效）。
# _is_eight_stage_skeleton / _TIGHT_STAGE_JOIN_RE 已外迁 worker_helpers（import * 接回）


# 资产 → market_category（多品种兜底解析用）

# 八段文本中可识别的副品种（含别名），排除主品种后扫描提及


def _rescue_target_asset_from_content(
    content: str,
    current_target: str = "",
    *,
    preferred_categories: Optional[Tuple[str, ...]] = None,
) -> str:
    """服务端兜底：LLM 把 target_asset 写成 NONE/OTHER 时，从新闻正文里识别真实 ticker。

    v211 P1-B 修复：
      历史 710 条 NO_MAPPING_FOR_ASSET:NONE 脏数据几乎全部是因为 LLM 在「个股/港股」等
      扩展品种上未输出 target_asset（如 "三星 HBM 占 DRAM 全球产能近三成" 应是 SKHYNIX，
      LLM 却写 OTHER/NONE）。本函数复用 _ASSET_ALIASES 关键词扫描正文，
      找到第一个命中的 ticker 兜底，避免数据进 unclassified_events 队列。

    优先规则：
      1. 优先尝试 SKHYNIX（韩股海力士，国内财经源常误判为 NONE）
      2. 然后其它个股 ticker（NVDA/TSLA/AAPL 等美股）
      3. 最后才是大宗品类（XAU/WTI/BTC 等），避免把「海力士 HBM 利好」误判为 MACRO

    入参:
      content:  新闻正文（必传；空字符串直接返回 current_target）
      current_target: 现有 target_asset；非 NONE/OTHER 时直接返回
      preferred_categories: 候选品类白名单；None 时按默认顺序扫描
    """
    cur = (current_target or "").strip().upper()
    if cur and cur not in ("", "NONE", "OTHER", "N/A", "NULL"):
        return current_target
    if not content or not content.strip():
        return current_target or "NONE"

    low = content.lower()

    # ── 优先级1：SKHYNIX 类（海力士专属，不含 HBM/DRAM 等存储泛词，避免把美光新闻误判） ──
    for kw in ("海力士", "sk hynix", "skhynix", "hynix", "000660"):
        if kw in low:
            return "SKHYNIX"

    # ── 优先级2：其它个股 ticker，按 _ASSET_ALIASES 顺序扫描（先到先得） ──
    #    所有纯英文别名统一走 word-boundary regex 匹配，避免子串误命中：
    #      "coin" → "Cointelegraph / Blockchain.com"
    #      "intel" → "Cointelegraph"
    #      "mu" → "目 / 模"（但中文不会命中，regex 已排除）
    #      "tsmc" → "xTSMCx" 类包装内容
    #    中文别名仍用 substring 匹配（中文无 word 概念）。
    import re as _re_rescue
    for asset, aliases in _ASSET_ALIASES:
        if asset == "SKHYNIX":  # 已扫过
            continue
        for alias in sorted(aliases, key=len, reverse=True):
            if not alias:
                continue
            # 中文别名 → substring 匹配
            if any('\u4e00' <= ch <= '\u9fff' for ch in alias):
                if alias in low:
                    return asset
                continue
            # 纯英文别名 → word-boundary 匹配
            pattern = r"(?<![a-z0-9])" + _re_rescue.escape(alias) + r"(?![a-z0-9])"
            if _re_rescue.search(pattern, low):
                return asset

    return current_target or "NONE"


# 盘面结构开关缓存（5s TTL，与 news_trade_max_age 同级）
_ms_enabled_cache: Dict[str, Any] = {"ts": 0.0, "value": True}


def _market_structure_enabled() -> bool:
    """读 paper_trading_settings.market_structure_enabled（5s TTL 缓存，后台可调）。"""
    global _ms_enabled_cache
    now = time.monotonic()
    if now - float(_ms_enabled_cache.get("ts", 0.0)) < 5.0:
        return bool(_ms_enabled_cache.get("value", True))
    value = True
    try:
        from paper_trading import get_settings as _get_pt_settings
        value = bool(_get_pt_settings().get("market_structure_enabled", True))
    except Exception:
        pass
    _ms_enabled_cache.update({"ts": now, "value": value})
    return value


def _load_custom_prompts_sync() -> List[str]:
    """加载启用的用户自定义分析指令（ai_prompts 表，模块内 5s TTL）。

    与 _market_structure_enabled 同模式：engine 每次分析拼 prompt 时调用，
    后台（API /prompts 页）增删启停后 ≤5s 生效，无需重启引擎。
    """
    try:
        from ai_prompts import load_enabled_prompts
        return load_enabled_prompts()
    except Exception:
        return []


def _load_asset_rules_block_sync() -> str:
    """加载品种专项分析规则注入块（ai_asset_rules 表，5s TTL）。

    按品种分节的规则文本；无启用规则 → ""（不注入、零 token 开销）。
    """
    try:
        from ai_prompts import load_asset_rules_block
        return load_asset_rules_block()
    except Exception:
        return ""


def _build_causal_block(news_content: str) -> str:
    """[v176.3 已停用] 构造 [因果链分析] 段要求（追加到 system prompt）。

    分析流程已删除 [因果链分析] 段，本函数不再被调用（因果链库数据/管理页仍保留）。
    无论是否命中因果链都要求 AI 在八段式之后输出 [因果链分析] 段，保证前端
    八段+因果链展示结构完整：
      - 命中启用因果链（触发词匹配）→ 注入链内容，AI 结合新闻解读因果
      - 未命中 → 要求输出"因：暂无匹配；果：暂无匹配结果"，仍保留该段结构
    无因果链库可用（异常）→ 返回 ""（不强制，不改变输出格式）。
    """
    try:
        from causal_chains import match_chains
        chains = match_chains(news_content, max_chains=3)
    except Exception:
        return ""
    if chains:
        lines = []
        for ch in chains:
            name = str(ch.get("name") or "").strip()
            cause = str(ch.get("cause") or "").strip()
            effect = str(ch.get("effect") or "").strip()
            note = str(ch.get("note") or "").strip()
            try:
                weight = float(ch.get("weight") or 1.0)
            except (TypeError, ValueError):
                weight = 1.0
            mechanism = str(ch.get("mechanism") or "").strip()
            precondition = str(ch.get("precondition") or "").strip()
            invalidation = str(ch.get("invalidation") or "").strip()
            head = f"[因果链 {name}]" if name else "[因果链]"
            line = f"{head} 因：{cause}；果：{effect}"
            if mechanism:
                line += f"（机理：{mechanism[:200]}）"
            if precondition:
                line += f"（前提：{precondition[:120]}）"
            if invalidation:
                line += f"（失效边界：{invalidation[:120]}）"
            if weight != 1.0:
                line += f"（权重 {weight:g}）"
            if note:
                line += f"（备注：{note}）"
            lines.append(line)
        return (
            "═══ 因果链分析（命中以下宏观因果链，必须在 reasoning_path 八段之后"
            "追加输出 [因果链分析] 段）═══\n"
            + "\n".join(lines)
            + "\n\n要求：\n"
            + "  1. 在八段式（[驱动力]…[逻辑自检]）之后，追加一段 [因果链分析]：结合本条"
              "新闻，用『因：…』说明当前事件对应的宏观/因果起点，用『果：…』推演其对"
              "黄金/原油/加密/美债等的传导方向与幅度。命中多条时按标注的（权重 N）"
              "权衡侧重——权重越高该链越是本条新闻的主导因果框架，分析要更侧重它。\n"
              "    ★多品种时因果链也要分品种：对主品种 + 每个 sibling 品种分别写"
              "『品种X：因…果…』（各自传导方向/幅度），禁止只写主品种。\n"
            + "  2. 对每条因→果，必须强制拆解三层并在 [因果链分析] 里写明："
              "①传导机理——A 为什么会造成 B，中间经过哪一层金融/物理机制；"
              "②生效前提——什么条件下这条链成立；③失效边界——什么场景下失效/反转"
              "（如金融危机时 TIPS 上行黄金未必跌）。命中的链未标注这三层时，"
              "AI 须按金融常识补出；说不出机理的关联一律不得当因果采信。\n"
            + "  3. 因果链仅作分析框架：若新闻与因果链方向矛盾或证据不足，明说并降级，"
              "不得机械套用。\n"
            + "  4. 若输出了 [因果链分析]，reasoning 结尾必须补一句『总结：』，"
              "多品种时总结也按品种分行——每个品种：因果链结论与八段式结论"
              "相互印证/存在分歧（说明采信哪边及原因）——两者互相认证。\n"
        )
    # 未命中任何因果链：仍要求输出该段（显示"暂无匹配"），保证结构完整
    return (
        "═══ 因果链分析（本条新闻未命中因果链库，仍需输出 [因果链分析] 段）═══\n\n"
        "要求：在八段式（[驱动力]…[逻辑自检]）之后，追加一段 [因果链分析]，"
        "内容写：『因：暂无匹配；果：暂无匹配结果』。若你能从宏观常识判断本条新闻"
        "有明确因果传导，也可简述（因：…；果：…），但不要编造复杂链条。\n"
        "多品种时若对各品种有不同因果判断，按品种分行简述（品种X：因…果…）。\n"
    )


def _build_chain_prior_block(target_asset: str, news_content: str) -> str:
    """v337: 命中因果链先验（需求 #8）。

    与 v176.3 停用的 `_build_causal_block` 的区别：
      · 按**具体品种**过滤（链的 asset 必须等于本品种，跨资产链不参与）；
      · 带**证据强度**（hit_rate / n_outcomes / posterior），而不是只看权重；
      · 给 LLM 的是"优先框架 + 反向需给反例"的硬约束，不是机械套用模板。

    无命中 / 模块异常 → 返回 ""（不阻塞主分析）。
    """
    try:
        import chain_prior as _cp
        conn = db.get_connection()
        try:
            priors = _cp.match_by_headline(
                conn, str(target_asset or "").upper(), news_content or ""
            )
        finally:
            conn.close()
        return _cp.build_prompt_block(priors)
    except Exception as exc:
        print(f"  [CHAIN-PRIOR] skipped: {type(exc).__name__}: {exc}", flush=True)
        return ""


def _build_event_block(news_content: str) -> str:
    """构造「事件结构化抽取」块：规则确定性抽出事件原子 + 要求 AI 交叉校验。

    事件原子（根因/量级/方向/标的）与 LLM 的七段式分析互补：
    规则负责把客观字段抽准，LLM 负责机理拆解。这里要求 AI 显式确认规则
    抽出的量级是否与新闻一致，不一致时在 [逻辑自检] 写『事件结构化校验』
    修正标记，供人按关键词复核（即"标黄"机制）。
    """
    try:
        from event_struct import (
            extract_data_points, extract_events, to_data_points_block, to_prompt_block,
        )
        events = extract_events(news_content or "")
        data_points = extract_data_points(news_content or "")
    except Exception:
        return ""
    block = to_prompt_block(events)
    dp_block = to_data_points_block(data_points)
    if not block and not dp_block:
        return ""
    body = block
    if dp_block:
        body = (body + "\n" + dp_block) if body else ("═══ 事件结构化抽取（数据三值锚点）═══\n" + dp_block)
    return (
        body
        + "\n要求：若上面规则抽出的事件/三值锚点与新闻一致，直接采纳其量级写进 [因素排行]/[驱动力]；"
        "若不一致（抽错事件/量级错误/遗漏关键事件/三值错位），必须在 [逻辑自检] 写"
        "『事件结构化校验：规则抽出X，实为Y』修正。任何方向性结论若依赖于被修正的量级，"
        "须以修正后的量级为准。\n"
    )


def _load_attribution_block(asset: str, days: int = 30, top_n: int = 3) -> str:
    """v309 P0-3: 注入「该品种近期异动的历史归因」块。

    为什么需要 (这是审计里 P0-3 的核心缺口):
      归因系统 (event_move_attribution) 一直在算"哪条新闻解释了这波异动",
      但结论**从未回到 ai_worker 的决策上下文**。AI 每次分析同一条新闻都在
      凭印象重来, 完全不知道"上次这个品种这样异动时, 归因是什么"。

    注入的是**已经发生过的**历史归因 (move_ts 在过去), 用于给 AI 一个
    "这类事件在这个品种上历史通常怎么走" 的参照, 属于研究参考,
    不强制采纳 (与 baseline / microstructure 块的定位一致)。

    未来函数纪律:
      · 只取 move_ts < now 的行 (历史事实);
      · 剔除量纲污染行 (|move_pct| > 90%), 否则 +9867% 的假异动会进 prompt;
      · 只取有新闻来源的归因行 (ev_news_id / ev_source 非空), 否则是无信息行;
      · 表不存在或无数据 → 返回 "" (零开销, 不阻塞主流程)。
    """
    asset_u = str(asset or "").strip().upper()
    if not asset_u or asset_u in ("NONE", "OTHER", "N/A"):
        return ""
    conn = None
    try:
        import db as _db_attr
        conn = _db_attr.get_connection()
        # v308 P1-fix3 同款量纲过滤 —— 污染行绝不能进 prompt。
        #
        # v309 P0-3 修复: 之前 days 参数**形同虚设** —— 签名里有 days=30,
        # 但 SQL 只过滤 move_ts < now, 没有任何"最近 N 天"下界 →
        # 半年前的历史归因也会被注入 prompt (且 LIMIT 会取到最老的那些,
        # 因为 ORDER BY move_ts DESC + LIMIT 取的是最新的, 但 top_n*3 的窗口
        # 之外数据仍会挤占配额, 且 2024 年的旧新闻对当前毫无参考价值)。
        # 现在补上 move_ts >= now - days*86400 的下界, 让 days 真正生效。
        _now = int(time.time())
        _since = _now - int(max(1, days)) * 86400
        rows = conn.execute(
            "SELECT move_ts, move_pct, move_kind, algo_score, ev_source, "
            "       ev_action, ev_news_id "
            "FROM event_move_attribution "
            "WHERE asset = ? AND move_ts < ? AND move_ts >= ? "
            "  AND ABS(COALESCE(move_pct,0)) <= 90 "
            "  AND (ev_news_id IS NOT NULL OR COALESCE(ev_source,'') != '') "
            "ORDER BY move_ts DESC LIMIT ?",
            (asset_u, _now, _since, int(max(1, top_n) * 3)),
        ).fetchall()
    except Exception:
        return ""
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    if not rows:
        return ""

    # 按 move_ts 去重: 同一波异动可能有多条新闻归因, 只保留归因分最高的那条,
    # 否则一波行情会占满整个块。key=ts, value=(score, ts, pct, kind, src, act)
    best_by_move: Dict[int, Any] = {}
    for r in rows:
        try:
            ts = int(r[0]); pct = float(r[1] or 0.0); kind = r[2]
            score = float(r[3] or 0.0); src = r[4]; act = r[5]
        except (TypeError, ValueError, IndexError):
            continue
        if abs(pct) < 1.0:
            continue  # 幅度过小的"异动"没有参考价值
        prev = best_by_move.get(ts)
        if prev is None or score > prev[0]:
            best_by_move[ts] = (score, ts, pct, kind, src, act)

    items = sorted(best_by_move.values(), key=lambda x: x[0], reverse=True)[:int(max(1, top_n))]
    if not items:
        return ""

    lines = []
    for score, ts, pct, kind, src, act in items:
        src_s = str(src or "unknown")[:24]
        act_s = str(act or "?").upper()[:6]
        kind_s = str(kind or "?")[:16]
        try:
            ts_s = datetime.fromtimestamp(int(ts)).strftime("%m-%d %H:%M")
        except (TypeError, ValueError, OSError):
            ts_s = "?"
        lines.append(
            f"  - {ts_s} {pct:+.2f}% ({kind_s}) | 归因新闻方向 {act_s} | 来源 {src_s} | 归因分 {score:.2f}"
        )
    if not lines:
        return ""
    return (
        "═══ 历史归因参照（该品种近期异动由哪条新闻解释, 来自 event_move_attribution）═══\n"
        + "\n".join(lines)
        + "\n要求：以上是**历史已发生**的归因事实，用于校准「这类事件在这个品种上"
          "通常怎么传导」的参照；不得把它当作本次新闻的直接证据，也不得据此"
          "臆造未发生的新闻。\n"
    )


def _build_baseline_block(event_type_code: str, target_asset: str) -> str:
    """构造「历史绩效（baseline）」块：从 backtest_event_outcomes 表读历史 BUY/SELL/HOLD 样本。

    输入：event_type_code (例 'geopolitical_conflict'), target_asset (例 'XAU')
    输出：prompt 段文本。

    设计：
      - 优先取 (type_code, asset) 精确匹配的样本
      - 否则取同 type_code 不限 asset 的样本
      - 否则取全局样本
      - 样本 < 3 时显式标 "(样本不足,仅供参考)"
      - 表为空 / 数据库错 → 返回 "" (静默,主流程不阻塞)
    """
    if not event_type_code:
        return ""
    conn = None
    try:
        import db as _db
        conn = _db.get_connection() if hasattr(_db, "get_connection") else None
        if conn is None:
            return ""
        cur = conn.cursor()

        # 1) (type, asset) 精确匹配
        cur.execute(
            "SELECT sample_n, win_n, loss_n, avg_pnl_4h FROM backtest_event_outcomes "
            "WHERE type_code=? AND asset=? LIMIT 1",
            (event_type_code, target_asset or ""),
        )
        exact = cur.fetchone()

        # 2) 同 type 跨 asset (取样本数最大的非精确匹配组合)
        cur.execute(
            "SELECT asset, sample_n, win_n, loss_n, avg_pnl_4h FROM backtest_event_outcomes "
            "WHERE type_code=? AND asset != ? ORDER BY sample_n DESC LIMIT 1",
            (event_type_code, target_asset or ""),
        )
        same_type_other_asset = cur.fetchone()

        # 3) 全局汇总
        cur.execute(
            "SELECT COUNT(*) AS n, SUM(CASE WHEN win_n>0 THEN 1 ELSE 0 END) AS any_win, "
            "       SUM(loss_n) AS total_loss, SUM(sample_n) AS total_sample, "
            "       AVG(avg_pnl_4h) AS global_avg "
            "FROM backtest_event_outcomes"
        )
        global_row = cur.fetchone()
    except Exception as _e:
        print(f"[BASELINE] 查询失败(静默): {type(_e).__name__}: {_e}", flush=True)
        return ""
    finally:
        # v189-8: 原函数 2 条返回路径都不关连接, 每次调用泄漏 1 个 fd
        if conn is not None:
            conn.close()

    def _fmt(sample_n, win_n, loss_n, avg_pnl, label, asset=""):
        if sample_n <= 0:
            return None
        win_rate = (win_n / sample_n * 100.0) if sample_n > 0 else 0.0
        sample_flag = " [样本不足,仅供参考]" if sample_n < 3 else ""
        asset_part = f" / {asset}" if asset else ""
        return (f"  - {label}{asset_part}: n={sample_n}, 胜率={win_rate:.0f}%, "
                f"平均24h收益={avg_pnl:+.2f}%{sample_flag}")

    lines = []
    if exact:
        s, w, l, p = exact[0], exact[1], exact[2], exact[3] or 0.0
        line = _fmt(s, w, l, p, f"同事件同品种历史 (type={event_type_code}, asset={target_asset or 'N/A'})")
        if line:
            lines.append(line)

    if same_type_other_asset:
        s, w, l, p = same_type_other_asset[1], same_type_other_asset[2], same_type_other_asset[3], same_type_other_asset[4] or 0.0
        a = same_type_other_asset[0] or "(不限品种)"
        line = _fmt(s, w, l, p, f"同事件类型历史 (type={event_type_code})", asset=a)
        if line:
            lines.append(line)

    if global_row and global_row[3] and global_row[3] > 0:
        total_sample = global_row[3]
        total_loss = global_row[1] or 0  # any_win
        total_loss_n = global_row[2] or 0
        global_avg = global_row[4] or 0.0
        line = _fmt(total_sample, total_loss, total_loss_n, global_avg, "全事件类型历史汇总")
        if line:
            lines.append(line)

    if not lines:
        return ""

    body = "\n".join(lines)
    header = ("═══ 历史绩效（baseline.backtest_event_outcomes）═══\n"
              "以下数据来自历史 ai_decisions 在做出去向后的真实 PnL 回测（24h 窗口），"
              "用于校准本次分析的「同类事件过去是否赚钱」。\n")
    footer = ("\n要求：在 [因素排行] 和 [总结] 中显式引用上面至少 1 条历史数据，作为本次方向的"
              "概率校准依据（不要忽略样本不足警告）。\n")
    return header + body + footer


def _build_microstructure_block(
    target_assets: Optional[List[str]] = None,
    max_chars: int = 800,
) -> str:
    """v200+ P0-1: 拼 microstructure block 给 LLM 看（注入 user_text 头部）。

    数据：coinglass_lsr.get_latest_samples_by_symbols + funding_rate_feed.fetch_funding_rates。
    在 _build_baseline_block 之后注入,与历史胜率并列 → 决策前先看：
      - 持仓量/资金费率(最大);
      - [market_structure_block] 中已有的市场风格(regime)、价格等"宏观背景"提示
      - 本块 = "微观结构"(散户/大户持仓 + 永续费率),从概率上校准本次方向
      - 不直接采纳其方向,但要求在 [逻辑自检] 和 [总结] 中显式引用 ≥ 1 条 microstructure 字段
      - 标记 stale=True 的数据视为「过期」, 引用时须注明「数据已滞后」

    失败/无样本 → 返回 "" (静默,主流程不阻塞)
    """
    if not target_assets:
        # 退化:用默认四件套
        target_assets = ["BTC", "ETH", "SOL", "XAU"]
    try:
        # 延迟 import 避免 engine 启动循环依赖
        import structured_input as _si
        micro = _si._microstructure_block(target_assets=target_assets)
    except Exception:
        return ""
    if not isinstance(micro, dict):
        return ""
    lsr = micro.get("lsr") or {}
    funding = micro.get("funding") or {}
    stale_syms = set(micro.get("stale_symbols") or [])
    if not lsr and not funding:
        return ""
    fresh_ts = int(micro.get("fresh_ts") or 0)
    fresh_str = ""
    if fresh_ts:
        try:
            from datetime import datetime as _dt
            fresh_str = _dt.utcfromtimestamp(fresh_ts).strftime("%Y-%m-%d %H:%M UTC")
        except Exception:
            fresh_str = str(fresh_ts)
    header = ("═══ 微观结构（持仓量/资金费率,最新样本）═══\n"
              "以下数据用于校准本次方向的概率,不是直接采纳的信号源。\n"
              f"采样时间:{fresh_str or 'N/A'} | 数据源:{micro.get('source') or 'empty'}\n")
    lines: List[str] = []

    # ── LSR 部分(持仓量/多空比/大户持仓) ──
    for sym in sorted(lsr.keys()):
        s = lsr[sym]
        if not isinstance(s, dict):
            continue
        ratio_pct = str(s.get("ratio_pct") or "")
        d4 = str(s.get("delta_4h_pct") or "")
        d30 = str(s.get("delta_30m_pct") or "")
        long_q = float(s.get("ov_long_qty") or 0.0)
        short_q = float(s.get("ov_short_qty") or 0.0)
        whale_long = float(s.get("whale_long_qty") or 0.0)
        whale_short = float(s.get("whale_short_qty") or 0.0)
        oi_mcap = float(s.get("oi_mcap") or 0.0)
        cache_age = int(s.get("cache_age_s") or 0)
        stale_flag = " [数据已滞后]" if sym in stale_syms else ""
        # ratio_pct 通常形如 "55.20%" / "-3.14%" / "0" (无数据)
        line = (f"  - {sym}: 持仓比={ratio_pct or 'N/A'}, "
                f"4h 变化={d4 or 'N/A'}, 30m 变化={d30 or 'N/A'}, "
                f"多仓={long_q:.0f}/空仓={short_q:.0f}, "
                f"大户多={whale_long:.0f}/大户空={whale_short:.0f}, "
                f"OI/市值={oi_mcap:.3f}"
                f"{stale_flag} (cache_age={cache_age}s)")
        lines.append(line)
    # ── Funding 部分(永续费率) ──
    for sym in sorted(funding.keys()):
        f = funding[sym]
        if not isinstance(f, dict):
            continue
        rate_pct = float(f.get("funding_rate_pct") or 0.0)
        is_extreme = bool(f.get("is_extreme"))
        nft = f.get("next_funding_time")
        ext_flag = " ⚠极端" if is_extreme else ""
        nft_str = f" (下次结算:{nft})" if nft else ""
        sign = "+" if rate_pct >= 0 else ""
        lines.append(f"  - {sym}: 资金费率={sign}{rate_pct:.4f}%/8h{ext_flag}{nft_str}")
    # ── Funding Extreme 全局提醒 ──
    fext = micro.get("funding_extreme") or []
    if fext:
        for e in fext[:3]:
            if isinstance(e, dict):
                lines.append(
                    f"  ⚠ 极端费率提醒: {e.get('inst', 'N/A')} = "
                    f"{e.get('rate_pct', 0):+.4f}%/8h"
                )
    if not lines:
        return ""
    body = "\n".join(lines)
    footer = ("\n要求:在 [逻辑自检] 和 [总结] 中显式引用至少 1 条 microstructure 字段；\n"
              "  - 标记 [数据已滞后] 的字段不可作决策唯一依据,只能作风险提示；\n"
              "  - 资金费率 ≥ 0.2%/8h 即视为极端,市场情绪单边过热,慎追单边；\n"
              "  - 大户持仓 / 散户持仓背离超过 20% → 关注大户动作。\n")
    text = header + body + footer
    if len(text) > max_chars:
        text = text[: max_chars - 60] + "...\n(以上 microstructure 数据已截断,以原文表格为准)"
    return text


def _build_technical_state_block(
    target_assets: Optional[List[str]] = None,
    timeframe: str = "4h",
) -> str:
    """v343: 技术状态读数段（注入 user_text 头部，紧跟 microstructure）。

    与 microstructure 分工：那一块回答"谁在持仓、费率高不高"，
    这一块回答"盘面现在是什么状态"——趋势阶段/波动位置/价值区/价-OI 象限/
    主动买占比/大户 vs 散户。

    只用本地库 + `technical_state.py`（与 CLI、快照、闸门同源），不联网、不造数。
    失败/无数据 → 返回 ""（静默，主流程不阻塞）。
    """
    if not target_assets:
        target_assets = ["BTC", "ETH", "XAU"]
    try:
        import technical_state as _tsst
        return _tsst.block(list(target_assets), timeframe=timeframe)
    except Exception:
        return ""


def build_prompt_preview(mode: str = "force") -> Dict[str, Any]:
    """装配「最终发给模型的 system prompt」，供提示库页面查看（只读，不联网、不建库）。

    为什么需要：提示库此前只显示 `_JSON_PROMPT_FORCE` 常量原文，而真正发给模型的
    prompt 是「常量 + 若干运行时分区」（盘面结构块 / 用户自定义指令 / 品种专项规则 /
    复盘经验）动态拼出来的。用户看不到这些分区，就会误以为"经验没加进去"。

    mode: "force" = 非 JSON 模式（_JSON_PROMPT_FORCE）；"system" = JSON 模式（_SYSTEM_PROMPT）。

    v176.3：分析流程已删除 [因果链分析] 段，因果链库不再注入 prompt（数据/管理页保留）。
    """
    # 需求3：统一单通道——预览始终显示 System Prompt（完整八段式），
    # Prompt Force 通道已废弃（实际分析也只走 System Prompt）。
    use_full = True
    base_default = _SYSTEM_PROMPT if use_full else _JSON_PROMPT_FORCE
    base = _main_prompt("system_prompt" if use_full else "json_force_prompt", base_default)

    segments: List[Dict[str, Any]] = [
        {"key": "base", "title": "主 Prompt（提示库可编辑）", "text": base,
         "editable": True, "injected": True},
    ]
    prompt = base

    # ① 盘面结构块（开关控制）
    if _market_structure_enabled():
        block = _main_prompt("market_structure_block", _MARKET_STRUCTURE_BLOCK)
        prompt += block
        segments.append({"key": "market_structure", "title": "盘面结构指令块（开关开启时附加）",
                         "text": block, "editable": True, "injected": True})

    # ② 用户自定义分析指令
    custom_prompts = _load_custom_prompts_sync()
    if custom_prompts:
        block = "\n\n═══ 用户自定义分析指令（优先级最高，必须遵守）═══\n" + "\n\n".join(custom_prompts)
        prompt += block
        segments.append({"key": "custom", "title": "用户自定义分析指令", "text": block,
                         "editable": True, "injected": True})

    # ③ 品种专项规则（提示库页面可编辑）
    try:
        asset_block = _load_asset_rules_block_sync()
    except Exception:
        asset_block = ""
    if asset_block:
        prompt = prompt + "\n\n" + asset_block
        segments.append({"key": "asset_rules", "title": "品种专项分析规则（提示库可编辑）",
                         "text": asset_block, "editable": True, "injected": True})

    # ④ 因果链库（v176.3）：[因果链分析] 分析环节已删除，因果链库不再注入 prompt；
    #    数据与「因果链库」管理页仍保留，仅作知识库维护，不再生成分析段落。
    causal_note = (
        "因果链库：v176.3 起 [因果链分析] 分析环节已删除，因果链库不再注入分析流程；"
        "数据与「因果链库」页面仍保留，仅作知识库维护。"
    )

    # ⑤ 复盘经验分区（每条经验在提示库页面可停用/启用/删除）
    try:
        insights_block = _load_insights_section()
    except Exception:
        insights_block = ""
    if insights_block:
        prompt = prompt + "\n\n" + insights_block
        segments.append({"key": "insights", "title": "当前生效经验规则（启用即出现在此）",
                         "text": insights_block, "editable": False, "injected": True})
    else:
        segments.append({"key": "insights", "title": "当前生效经验规则（启用即出现在此）",
                         "text": "", "editable": False, "injected": False})

    # ⑥ 反思错误码注入块（v176.3 新增）— 从 decision_errors 取最近错误码 → 一句话指导
    # 与 insights 互补：insights 是教训性经验规则，reflection 是近期决策具体错误码（更即时）
    try:
        reflection_block = _load_reflection_block()
    except Exception:
        reflection_block = ""
    if reflection_block:
        prompt = prompt + "\n\n" + reflection_block
        segments.append({"key": "reflection", "title": "反思错误码（最近触发错误的真实规避）",
                         "text": reflection_block, "editable": False, "injected": True})
    else:
        segments.append({"key": "reflection", "title": "反思错误码（最近触发错误的真实规避）",
                         "text": "", "editable": False, "injected": False})

    return {
        "mode": mode,
        "prompt": prompt,
        "length": len(prompt),
        "segments": segments,
        "note": causal_note,
    }


# _main_prompt 已外迁 engine.worker_helpers（import * 接回）


# _stock_context_for / _asia_context_for / _origin_search_block_for 已外迁 worker_helpers（import * 接回）


# ── v175 重要新闻 / 海力士强买入 → 手机推送（Server酱 + 邮件，fire-and-forget）──
import hashlib as _hashlib
_PHONE_PUSH_SEEN: Dict[str, float] = {}  # content 指纹 -> 上次推送 ts
# v186 推送门槛放宽：原 8.0（"高重要新闻"严苛阈值）→ 5.0
# 原因：模型给分保守（实测常态 impact=5~7），原阈值下 99% BUY/SELL 信号被静默过滤，
# 用户反映"信号出现没收到通知"。5.0 是平衡线——既保留过滤噪音能力（拒掉纯背景新闻），
# 又保证所有真正有方向的落地信号都到手机。


def _maybe_push_phone_alerts(successes: List[Dict[str, Any]], content_map: Dict[int, str],
                             timestamp_map: Optional[Dict[int, str]] = None,
                             published_map: Optional[Dict[int, str]] = None) -> None:
    """批处理落库后：命中海力士 → 强买入提醒；其它高重要新闻 → 重要提醒。

    仅推 BUY/SELL（HOLD/无方向不打扰手机）；正文统一走 alerts.format_signal_body。
    仅发提醒，不触发自动交易（SKHYNIX/CN_STOCK 不在 paper_trading.TRACK_ASSETS，
    asset_allowed 恒 False，模拟盘不会对它们建仓）。
    """
    now = time.time()
    ts_map = timestamp_map or {}
    pub_map = published_map or {}
    try:
        import asia_prices as _ap
    except Exception:
        _ap = None
    for s in successes:
        try:
            if not s or s.get("error"):
                continue
            nid = s.get("news_id")
            text = (content_map.get(nid) or "").strip()
            if not text:
                continue
            res = s.get("result") or {}
            if not isinstance(res, dict):
                continue
            asset = str(res.get("target_asset") or "NONE").strip().upper()
            action = str(res.get("suggested_action") or "HOLD").strip().upper()
            reason = str(res.get("reasoning") or "").strip()[:200]
            rpath = str(res.get("reasoning_path") or "").strip()
            try:
                impact = float(res.get("impact_score") or 0.0)
                heat = float(res.get("heat_score") or 0.0)
            except (TypeError, ValueError):
                impact = heat = 0.0
            dc = _is_truthy(res.get("direct_catalyst"))

            # ── 只推有方向的落地信号：HOLD/无方向一律不打扰手机 ──
            if action not in ("BUY", "SELL"):
                continue

            # 去重节流：同内容 10 分钟内不重复推
            fp = _hashlib.md5(text[:120].encode("utf-8", "replace")).hexdigest()
            last = _PHONE_PUSH_SEEN.get(fp, 0.0)
            if now - last < _PHONE_PUSH_DEDUP_S:
                continue
            _PHONE_PUSH_SEEN[fp] = now

            news_time = pub_map.get(nid) or ts_map.get(nid) or ""

            is_hynix = asset == "SKHYNIX" or (_ap is not None and _ap.detect_hynix_strict(text))
            if is_hynix:
                hynix_quote = ""
                if _ap is not None:
                    try:
                        hynix_quote = _ap.hynix_price_line()
                    except Exception:
                        hynix_quote = ""
                extra = (
                    f"{hynix_quote}\n（仅提醒信号，不自动交易）"
                    if hynix_quote else "（仅提醒信号，不自动交易）"
                )
                body = format_signal_body(
                    asset="SK海力士 (SKHYNIX)",
                    action=action,
                    impact=impact,
                    heat=heat,
                    reason=reason or "N/A",
                    analysis=rpath,
                    news_text=text[:300],
                    news_time=news_time,
                    extra_line=extra,
                )
                send_strong_buy_alert(build_signal_title("SK海力士", action, news_time), body)
                continue
            # 其它高重要新闻：只有"有直接催化 + 影响分够高"才推
            # v450(2026-11-19): 用户反馈"多因子共振/强买入瞎发"。
            # 旧条件 `dc or impact>=5 or heat>=5` 是"任一满足就推"——heat(热度) 与方向对不对
            # 无关，5.0 判据正则 OK；三条并列导致绝大多数 BUY/SELL 都能通过。
            # 新条件：必须有直接催化（新闻本身指向该资产），且 impact ≥ 7.0。
            if dc and impact >= 7.0:
                body = format_signal_body(
                    asset=asset,
                    category=str(res.get("market_category") or ""),
                    action=action,
                    impact=impact,
                    heat=heat,
                    reason=reason or "N/A",
                    analysis=rpath,
                    news_text=text[:300],
                    news_time=news_time,
                )
                title = build_signal_title(asset, action, news_time)
                ok = send_important_news(title, body)
                print(
                    f"[{_now()}] [ALERT] {title} impact={impact:.1f} heat={heat:.1f}"
                    f" dc={dc} 推送={ok}"
                )
            # v186 兜底推送：任何 BUY/SELL 都发一条。
            # v450：默认关闭（SIGNAL_FALLBACK_PUSH=1 才开）。这是"短信轰炸"的另一半来源——
            # 同时刻既有完整卡片又有精简通知，等于每条信号推两遍。
            if os.getenv("SIGNAL_FALLBACK_PUSH", "0") == "1":
                send_signal_alert(asset, action, impact, heat, reason, news_time)
        except Exception:
            continue


# ---------------------------------------------------------------------------
# Phase 1 — Performance Feedback Injection
# ---------------------------------------------------------------------------
# Queries historical settled-signal performance for key asset × action ×
# prediction_type combos.  Injected into the LLM prompt as research reference
# — does NOT auto-modify LLM output.  Only the LLM decides.
# ---------------------------------------------------------------------------

# Assets and actions we care about for performance feedback
# Minimum sample for "reliable" display


def _build_performance_context() -> str:
    """Build [Historical Performance] block for prompt injection.

    Queries settled ai_decisions for each asset × action × prediction_type
    combo.  Returns a concise multi-line string suitable for prepending to
    the LLM user prompt.  Runs once per batch (O(1) near-instant SQL).
    """
    try:
        conn = _open_db()
    except Exception:
        return ""

    try:
        lines: List[str] = []
        lines.append("[Historical Performance]")
        lines.append("Similar signals:")

        # Query: for each (asset, action, prediction_type) combo
        for asset in _PERF_ASSETS:
            for action in _PERF_ACTIONS:
                # ── Overall (all prediction_types) ──
                overall = conn.execute(
                    "SELECT COUNT(*) AS n, "
                    "  SUM(CASE WHEN is_correct = 'WIN' THEN 1 ELSE 0 END) AS wins, "
                    "  SUM(CASE WHEN is_correct = 'LOSS' THEN 1 ELSE 0 END) AS losses, "
                    "  AVG(CASE WHEN forward_pnl IS NOT NULL THEN forward_pnl ELSE NULL END) AS avg_pnl "
                    "FROM ai_decisions "
                    "WHERE settled = 1 "
                    "  AND is_correct IN ('WIN', 'LOSS') "
                    "  AND target_asset = ? "
                    "  AND suggested_action = ? "
                    "  AND strftime('%s', created_at) >= strftime('%s', 'now', ?)",
                    (asset, action, f"-{_PERF_LOOKBACK_DAYS} days"),
                ).fetchone()

                for pt in _PERF_PREDICTION_TYPES:
                    row = conn.execute(
                        "SELECT COUNT(*) AS n, "
                        "  SUM(CASE WHEN is_correct = 'WIN' THEN 1 ELSE 0 END) AS wins, "
                        "  SUM(CASE WHEN is_correct = 'LOSS' THEN 1 ELSE 0 END) AS losses, "
                        "  AVG(CASE WHEN forward_pnl IS NOT NULL THEN forward_pnl ELSE NULL END) AS avg_pnl "
                        "FROM ai_decisions "
                        "WHERE settled = 1 "
                        "  AND is_correct IN ('WIN', 'LOSS') "
                        "  AND target_asset = ? "
                        "  AND suggested_action = ? "
                        "  AND prediction_type = ? "
                        "  AND strftime('%s', created_at) >= strftime('%s', 'now', ?)",
                        (asset, action, pt, f"-{_PERF_LOOKBACK_DAYS} days"),
                    ).fetchone()

                    n = row["n"] or 0
                    wins = row["wins"] or 0
                    losses = row["losses"] or 0
                    decided = wins + losses
                    avg_pnl = row["avg_pnl"]

                    if n < _PERF_MIN_SAMPLE or decided == 0:
                        lines.append(
                            f"{asset} {pt} {action}: "
                            f"Insufficient sample"
                        )
                    else:
                        wr = wins / decided
                        pnl_str = (
                            f"{avg_pnl:+.2f}%" if avg_pnl is not None else "N/A"
                        )
                        lines.append(
                            f"{asset} {pt} {action}: "
                            f"sample: {decided} "
                            f"win rate: {wr:.0%} "
                            f"avg pnl: {pnl_str}"
                        )

                # ── Also add the overall (all prediction_types) line ──
                n_all = overall["n"] or 0
                wins_all = overall["wins"] or 0
                losses_all = overall["losses"] or 0
                decided_all = wins_all + losses_all
                if decided_all >= _PERF_MIN_SAMPLE:
                    wr_all = wins_all / decided_all
                    pnl_all = overall["avg_pnl"]
                    pnl_str = (
                        f"{pnl_all:+.2f}%" if pnl_all is not None else "N/A"
                    )
                    lines.append(
                        f"{asset} * {action}: "
                        f"sample: {decided_all} "
                        f"win rate: {wr_all:.0%} "
                        f"avg pnl: {pnl_str}"
                    )

        return "\n".join(lines)

    except Exception as e:
        print(f"[PERF] 查询历史绩效失败: {type(e).__name__}: {str(e)[:80]}")
        return ""
    finally:
        conn.close()


# 品种 → 中文名（取 _ASSET_ALIASES 里的首个中文别名），供 asset_reasoning 括号名纠偏用
_ASSET_CN_NAMES: Dict[str, tuple] = {
    code: tuple(a for a in aliases if not a.isascii())
    for code, aliases in _ASSET_ALIASES
}


def _fix_main_line(ln: str, ta: str) -> str:
    """把「主品种 XXX(名字)：正文」这一行的代码与括号名对齐到 ta。

    只替换代码与括号名、正文原样保留：
      "主品种 XAU(黄金)：避险需求" + ta=WTI → "主品种 WTI(原油)：避险需求"
      "主品种 XAU(美股)：…"        + ta=XAU → "主品种 XAU(黄金)：…"（括号名纠偏）
    括号名只在该品种自己的中文名列表里才保留（避免"主品种 WTI(黄金)"这类自相矛盾）。
    """
    head, sep, body = ln.partition("：")
    if not sep:
        head, sep, body = ln.partition(":")
    paren = ""
    pm = re.search(r"[（(]([^）)]{0,8})[）)]", head)
    if pm:
        old_name = pm.group(1).strip()
        names = _ASSET_CN_NAMES.get(ta, ())
        if old_name and old_name in names:
            paren = pm.group(0)
        else:
            paren = f"({names[0]})" if names else ""
    return f"主品种 {ta}{paren}：{body.strip()}".rstrip("：")


def _load_dim_event_type_block() -> str:
    """v174: 从 dim_event_type 表拉所有 active 的 type_code,生成「事件类型选项清单」注入 prompt。
    让 LLM 知道有这套维表,而不是自由输出 event_type 字符串。
    输出格式:
      【事件类型选项】(从 dim_event_type 选 1)
        - geopolitical_conflict (地缘冲突) → XAU/WTI 利多
        - rate_hike (加息) → BTC/ETH/WTI/XAU 利空
        ...
    """
    _conn = None
    try:
        import json as _json_det
        import db as _db_det
        _conn = _db_det.get_connection()
        # db.get_connection() 不设 row_factory, 这里用整数索引取值
        rows = _conn.execute(
            "SELECT code, name_zh, name_en, category, direction_bias FROM dim_event_type "
            "WHERE active = 1 ORDER BY id"
        ).fetchall()
        if not rows:
            return ""
        # v435 P0-2：清单从「一行一个 code + 中文名 + 英文名 + 分类 + 方向先验」压成
        # 紧凑表 —— 原格式 44 行约 2,718 字符，每条新闻都要发一遍（≈1.4k token）。
        # 实测（21:2x [PARTS] 诊断）它是 user 消息里最大的一块，比新闻正文还大；
        # 模型真正需要的只有 code + 中文名 + 方向先验，英文名/分类标签对选型没帮助。
        _compact = ["\n\n═══ 事件类型清单（必填 event_type_code，从下表严格选 1）═══",
                    "格式：code（中文名；历史方向先验，非硬规则）"]
        for r in rows:
            code_val = r[0]
            zh = str(r[1] or '').strip()
            dir_bias = r[4] or '{}'
            try:
                bias_dict = _json_det.loads(dir_bias) if str(dir_bias).strip().startswith('{') else {}
            except Exception:
                bias_dict = {}
            _tokens = []
            for k, v in list(bias_dict.items())[:4]:
                if isinstance(v, str):
                    _v = v.strip().lower()
                    if _v.startswith("bull") or _v in ("+", "up", "long"):
                        _tokens.append(f"{k}+")
                    elif _v.startswith("bear") or _v in ("-", "down", "short"):
                        _tokens.append(f"{k}-")
                elif isinstance(v, (int, float)):
                    _tokens.append(f"{k}{'+' if float(v) > 0 else '-'}")
            _inner = []
            if zh and zh != code_val:
                _inner.append(zh)
            if _tokens:
                _inner.append(",".join(_tokens))
            _compact.append(code_val + ("（" + "；".join(_inner) + "）" if _inner else ""))
        _compact.append('字段："event_type_code": "<上表 code 之一>"（都不贴合就选最近的并在 reasoning 说明）')
        return "\n".join(_compact)
    except Exception as _exc:
        # 失败兜底:返回最简清单让 LLM 知道有这个字段
        return ("\n\n═══ 事件类型选项清单(v174 必填)═══\n"
                "[ERROR] 加载 dim_event_type 失败, 请回退默认类型 (geopolitical_conflict/rate_cut/rate_hike/cpi/earnings_release 等)。\n"
                'JSON 必填新增字段: "event_type_code": "<code>"')
    finally:
        # v189-8: 该函数 3 条返回路径 (空表 / 正常 / 异常兜底) 原先都不关连接
        if _conn is not None:
            _conn.close()


def _normalize_main_asset(result: Dict[str, Any]) -> Dict[str, Any]:
    """把「主品种」统一到 target_asset，并让 asset_reasoning 与之对齐。

    为什么要做：实测模型会自相矛盾 —— target_asset 写 WTI，asset_reasoning 却写
    「主品种 XAU(黄金)：…」；或 target_asset=NONE 却把真实主品种塞进 sibling_decisions。
    前端一个字段用 target_asset、另一个用 asset_reasoning → 界面出现
    「各品种交易决策」显示主品种是 A、而「品种相关推理逻辑」说是 B 的混乱。

    规则（target_asset 为唯一权威）：
      ① target_asset 为 NONE/OTHER 且有 sibling → 提升第一个副品种为主品种，
         并从 sibling_decisions 移除它（否则它既是主又是副）。
      ② asset_reasoning 里的「主品种 X」若与 target_asset 不一致 → 改成 target_asset；
         找不到主品种行则补一行（内容取该品种自身的副品种行或占位说明）。
    """
    try:
        ta = str(result.get("target_asset") or "").strip().upper()
        # v477 修复：主品种闸门与 rescue 两段互相打架。
        # 实测日志：`[RESCUE-TARGET] 从正文找回 NVDA` 之后紧接着
        # `[MAIN-GATE] 主品种未成立（NONE）→ 强制 HOLD（原 SELL）` ——
        # 说明走到这里时 result["target_asset"] 还是 NONE，模型给出的 SELL
        # 被当成"无标的"砍掉。这里在**同一段文本证据**上再补一次识别
        # （用模型自己写的 asset_reasoning / reasoning，不引入任何新信息）：
        # 认出来就把主品种补上，让后面的闸门按"有标的"处理。
        if ta in ("", "NONE", "OTHER"):
            _self_text = " ".join(
                str(result.get(k) or "") for k in ("asset_reasoning", "reasoning")
            ).strip()
            if _self_text:
                try:
                    _rescued_norm = _rescue_target_asset_from_content(_self_text, ta)
                except Exception:
                    _rescued_norm = ""
                if str(_rescued_norm or "").strip().upper() not in (
                        "", "NONE", "OTHER", "N/A", "NULL"):
                    result["target_asset"] = str(_rescued_norm).strip().upper()
                    ta = result["target_asset"]
                    try:
                        from market import market_classifier as _mc_norm
                        _rc = _mc_norm.map_target(ta)
                        if _rc:
                            result["market_category"] = str(_rc).upper()
                    except Exception:
                        pass
                    print(f"  [ASSETFIX] 主品种闸门前从模型自述文本找回 {ta}", flush=True)
        sibs = result.get("sibling_decisions")
        sibs = sibs if isinstance(sibs, list) else []
        # ① NONE/OTHER 且有副品种 → 提升第一个副品种
        if ta in ("", "NONE", "OTHER") and sibs:
            first = None
            for s in sibs:
                if not isinstance(s, dict):
                    continue
                code = str(s.get("target_asset") or "").strip().upper()
                if code and code not in ("", "NONE", "OTHER"):
                    first = s
                    break
            if first is not None:
                promoted = str(first.get("target_asset") or "").strip().upper()
                result["target_asset"] = promoted
                result["market_category"] = str(
                    first.get("market_category") or result.get("market_category") or ""
                ).upper()
                # 主品种的字段取被提升那条副品种的判断。
                # ★ 但若主分析已被针对性闸门降级为 HOLD（reasoning 带闸门说明），
                #   绝不能把这套字段照搬过来 —— 那等于用副品种的方向单覆盖掉
                #   "主品种分析无依据"的结论，闸门白拦（实测 news=4661：库里留下
                #   SELL -0.3 并真建了仓，而闸门日志显示已降级）。
                _gate_hold = (
                    str(result.get("suggested_action") or "").upper() == "HOLD"
                    and str(result.get("reasoning") or "").startswith(_FOCUS_GATE_PREFIXES)
                )
                if not _gate_hold:
                    for k in ("sentiment_score", "suggested_action", "reasoning",
                              "expected_horizon", "event_strength"):
                        if first.get(k) not in (None, ""):
                            result[k] = first[k]
                else:
                    print(
                        f"  [ASSETFIX] target_asset=NONE → 提升副品种 {promoted} 为主品种；"
                        f"主分析已被针对性闸门降级，保留 HOLD（不采用副品种方向单）",
                        flush=True,
                    )
                # 该副品种已升为主品种，从 sibling 列表移除（避免主副重复）
                result["sibling_decisions"] = [
                    s for s in sibs
                    if str(s.get("target_asset") or "").strip().upper() != promoted
                ]
                ta = promoted
                # v342: 品种变了，降级文案也必须跟着改 —— 否则记录里写着
                # "目标资产 NONE…不在核心监控清单"，实际却是 XAU/WTI/BTC，
                # 反思会据此把「LLM 空输出」学成「品种不在监控范围」。
                rerender_degrade_text_for_asset(
                    result, promoted, market_category=result.get("market_category"))
                if not _gate_hold:
                    print(f"  [ASSETFIX] target_asset=NONE → 提升副品种 {promoted} 为主品种", flush=True)

        # ①-B 主品种纠正：八段实际在讲的品种 ≠ 声明的 target_asset → 以内容为准纠正
        # 实测案例：新闻通篇讲 WTI 油价，模型却把 target_asset 写成 BTC
        # （只因跨资产段提过 BTC 资金费率），而八段每一段都在分析 WTI。
        # 判定：声明的主品种在八段里提及 <2 段（几乎没分析它），
        #       而另一个品种提及 ≥3 段且明显更多 → 后者才是真主品种。
        body_path = str(result.get("reasoning_path", "") or "")
        if ta and ta not in ("NONE", "OTHER") and body_path:
            declared_n = _asset_mentioned_stage_count(body_path, ta)
            if declared_n < 2:
                dom_code, dom_n = _dominant_asset_in_path(body_path, exclude=ta)
                if dom_code and dom_n >= 3 and dom_n > declared_n:
                    print(
                        f"  [MAINFIX] 声明主品种 {ta} 在八段仅提及 {declared_n} 段，"
                        f"而 {dom_code} 提及 {dom_n} 段 → 纠正主品种为 {dom_code}",
                        flush=True,
                    )
                    # 原主品种降为副品种（若它确实被分析到）；纠正后的品种从 sibling 移除
                    _old = ta
                    result["target_asset"] = dom_code
                    ta = dom_code
                    # v342: 与 ASSETFIX 同理 —— 品种纠正后，降级文案里的旧品种
                    # 也要重写，否则会出现"讲 XAU 的记录却写着不在监控清单"
                    rerender_degrade_text_for_asset(
                        result, dom_code, market_category=result.get("market_category"))
                    _kept = []
                    for s in sibs:
                        if not isinstance(s, dict):
                            continue
                        code_s = str(s.get("target_asset") or "").strip().upper()
                        if code_s == dom_code:
                            continue  # 已升为主品种
                        _kept.append(s)
                    # 原主品种若正文里确实提过（≥1 段）→ 作为副品种保留，避免丢信息
                    if _asset_mentioned_stage_count(body_path, _old) >= 1 and not any(
                        str(x.get("target_asset") or "").strip().upper() == _old for x in _kept
                    ):
                        _kept.append({
                            "target_asset": _old,
                            "market_category": str(result.get("market_category") or ""),
                            "sentiment_score": 0.0,
                            "suggested_action": "HOLD",
                            "reasoning": f"由 {_old} 降为副品种（八段实际分析对象为 {dom_code}）",
                        })
                    result["sibling_decisions"] = _kept
                    sibs = _kept

        # ①-C 主品种最终仍未成立（NONE/OTHER）→ 不保留副品种。
        #   用户原则："先有主品种，后面才会有副品种。主品种都没有，何来副品种。"
        #   可交易品种的副品种在上面的①已被提升为主品种并移出列表；能走到这里的
        #   是 OTHER 这类不可提升项 —— 留着一个"没有主品种依托的品种行"，界面会
        #   显示成来源不明的信号，宽松模式下还可能真建仓。
        if ta in _FOCUS_EXEMPT_ASSETS and result.get("sibling_decisions"):
            _n_drop = len(result["sibling_decisions"])
            result["sibling_decisions"] = []
            sibs = []
            print(
                f"  [MAIN-GATE] 主品种未成立（{ta or 'NONE'}）"
                f" → 移除 {_n_drop} 个无主品种依托的副品种",
                flush=True,
            )

        # ①-D 主品种仍未成立（NONE/OTHER）→ 无标的硬兜底：不给方向单。
        #   此前只靠 prompt 约束（股票/公司/行业/ETF→OTHER→NONE→HOLD），模型偶尔不听，
        #   实测"东方财富 ETF 净申购"新闻被判 OTHER/NONE 却仍输出 BUY 并入方向候选——
        #   这里强制 HOLD + 中性分，杜绝"品类为其他/无可交易标的却给方向"。
        if ta in _FOCUS_EXEMPT_ASSETS:
            _bogus = str(result.get("suggested_action") or "").upper()
            if _bogus in ("BUY", "SELL"):
                result["suggested_action"] = "HOLD"
                result["sentiment_score"] = 0.05
                if not _is_gate_downgraded(result):
                    result["reasoning"] = (
                        f"无可交易标的（{ta or 'NONE'}/OTHER），不给方向单"
                    )
                _signal_trace(
                    "main-null", asset=ta or "NONE", verdict="无标的→强制HOLD",
                    action=_bogus,
                )
                print(
                    f"  [MAIN-GATE] 主品种未成立（{ta or 'NONE'}）→ 强制 HOLD（原 {_bogus}）",
                    flush=True,
                )

        # ② asset_reasoning 里的「主品种」对齐 target_asset
        ar = str(result.get("asset_reasoning") or "")
        if ar and ta and ta not in ("NONE", "OTHER"):
            lines: List[str] = []
            main_fixed = False
            for raw in ar.split("；"):
                ln = raw.strip()
                if not ln:
                    continue
                if ln.startswith("主品种"):
                    if main_fixed:
                        continue  # 模型写多个"主品种"行 → 只留一行（否则界面出现两个主品种）
                    lines.append(_fix_main_line(ln, ta))
                    main_fixed = True
                else:
                    lines.append(ln)
            if not main_fixed:
                # 模型没写主品种行 → 补一行（取同品种副品种行的内容，避免编造）
                ctx = ""
                for s in sibs:
                    if isinstance(s, dict) and str(s.get("target_asset") or "").strip().upper() == ta:
                        ctx = str(s.get("reasoning") or "").strip()[:60]
                        break
                lines.insert(0, f"主品种 {ta}：{ctx}" if ctx else f"主品种 {ta}：AI 未显式输出关联推理（N/A）")
            result["asset_reasoning"] = "；".join(lines)[:400]
        return result
    except Exception:
        return result


def _build_gate_context(row) -> str:
    """从 raw_news.gate_* 列拼出 [门卫四步] 分析先验；缺列/无核心影响返回空串。"""
    def _g(col: str) -> str:
        try:
            return str(row[col] or "").strip()
        except Exception:
            return ""

    core = _g("gate_core_impact")
    verdict = _g("gate_verdict")
    if not core and not verdict:
        return ""
    main_asset = _g("gate_main_asset")
    kind = _g("gate_kind")
    timing = _g("gate_timing")
    strength = _g("gate_strength")
    if not core:
        core = "（门卫未给出核心影响，自行判断）"
    parts = [f"核心影响: {core}"]
    if main_asset:
        parts.append(f"主品种: {main_asset}")
    if kind:
        parts.append(f"性质: {kind}")
    if timing:
        parts.append(f"时效: {timing}")
    if strength:
        parts.append(f"强度: {strength}")
    if verdict:
        parts.append(f"判词: {verdict}")
    if main_asset and main_asset.upper() in ("BTC", "XAU", "WTI", "ETH", "SOL"):
        # v451：门卫已锁定池内主品种时，主分析不得再判 NONE/OTHER。
        # 实测 21:30 一批 8 条里"美、布两油走低""沃勒称可能加息50bp"全被判
        # 非核心 → routing_miss，LLM 明明拿到了 [门卫四步] 却没照用。
        parts.append(f"硬约束: target_asset 必须填 {main_asset}，禁止 NONE/OTHER")
    return "[门卫四步] " + "｜".join(parts)


def _call_llm_sync(news_content: str, model_cfg: Dict[str, str],
                   market_context: str = "",
                   performance_context: str = "",
                   news_timestamp: str = "",
                   writing_context: str = "",
                   insights_section: str = "",
                   reference_signals: str = "",
                   gate_context: str = "",
                   gate_target_asset: str = "",
                   gate_decision_id: int = 0,
                   event_type_code: str = "",
                   gate_strength: int = 0,
                   news_source: str = "",
                   news_published_at: str = "",  # v188：tier-3 平台首发兜底用
                   purpose: str = "main",
                   call_site: str = "ai_worker",
                   lean: bool = False) -> Dict[str, Any]:
    """
    Call any OpenAI-compatible LLM API synchronously (runs in executor thread).

    lean=True 时走**精简模式**：跳过所有上下文注入块（market_context /
    performance_context / review_insights / verdict / kline / dim / calendar /
    stock / asia / origin / spa …），只发新闻原文 + 最小指令。
    用途：同行评审（purpose=verify）只需要"方向 + 分数"，
    却每次要带 10K+ token 的完整主分析 prompt —— 实测 2026-11-05：
    评审 757 次调用烧了 13.75M token（占总消耗 60%），比主分析还多 56%。
    精简后每次约 1K token → 评审成本降 85%+。

    model_cfg.keys: id, label, api_base, api_key
    market_context:  Optional multi-line market snapshot string, prepended to user_content.
    news_timestamp:  ISO datetime string of the news event — prepended so AI knows the
                     historical time point when replaying old news for backtesting.
    purpose: v187 — 标签用途(main/gate/anomaly/reflection/advisor/sql/optimizer),
             写到 ai_call_stats 便于按通道独立计费
    call_site: v187 — 调用方标识(ai_worker/_call_llm_sync/reflection_loop/...)
    Returns: {"sentiment_score", "suggested_action", "reasoning", "model_label", "model_id",
              ... + 5 metadata fields}

    Retry strategy: if json_mode=True produces unparseable output (keyword fallback),
    retry once with json_mode=False (prompt-based JSON enforcement).
    """

    # v422 诊断：TRIDENT_PROMPT_TRACE=1 时打印每次主分析的上下文体积构成。
    # 目的：13.5K/次的输入到底由哪些块组成（system 6K + K线 1.9K字 + VERDICT 1.6K字
    # + 绩效/复盘/快照/宏观…），改之前先用真实数据定位，不靠猜。
    try:
        if os.getenv("TRIDENT_PROMPT_TRACE") == "1" and not lean and purpose == "main":
            print("  [PROMPT-TRACE] " + " ".join([
                f"news={len(news_content or '')}",
                f"market={len(market_context or '')}",
                f"perf={len(performance_context or '')}",
                f"insights={len(insights_section or '')}",
                f"refsig={len(reference_signals or '')}",
                f"gate={len(gate_context or '')}",
                f"write={len(writing_context or '')}",
                f"sys={len(_JSON_PROMPT_FORCE)}",
            ]), flush=True)
    except Exception:
        pass

    # v200 P0-A3: 共享容器 —— 注入 asset 经验时命中的 insight ids。
    # 原本定义在 _do_api_call 内层闭包(局部变量), 外层 _parse_result / return dict
    # 取不到 → NameError, insight_hit_ids 100% 写空。改为在 _call_llm_sync 层
    # 建可变列表, 闭包内写入, 外层读取(容器原地修改, 无需 nonlocal)。
    _asset_insight_hit_ids: List[int] = []

    # ── v322: 评审专用精简 system prompt ────────────────────────────────────
    # 评审只需要「方向 + 分数」，不需要八段推理链。
    # 原来它走完整主分析 prompt（10K+ token 输入）→ 单次 18K token，
    # 757 次评审烧掉 13.75M（占全天 60%）。
    # 现在评审只发这段（约 200 字）+ 新闻原文 → 单次约 1K token。
    _PEER_REVIEW_SYSTEM_PROMPT = (
        "你是金融新闻方向复核员。读一条新闻，判断它对金融市场的影响方向。\n"
        "只输出一个 JSON 对象，不要任何解释文字：\n"
        '{"sentiment_score": <方向分 -1.0 到 1.0，正数=偏多，负数=偏空，0=中性>, '
        '"suggested_action": "BUY" | "SELL" | "HOLD", '
        '"reasoning": "<40字以内理由>", '
        '"target_asset": "XAU" | "WTI" | "BTC" | "NONE"}\n'
        "规则：\n"
        "1. 只根据新闻文本本身判断，不得编造未提及的事实或数字。\n"
        "2. 拿不准方向时给 HOLD + sentiment_score=0，宁可观望不要瞎猜。\n"
        "3. target_asset 只能是上述四个之一，判断不了就给 NONE。\n"
        "4. reasoning 必须简短（40 字以内）。"
    )

    # ------------------------------------------------------------------
    # Inner: make one HTTP call and return raw response text + full body
    # ------------------------------------------------------------------
    def _do_api_call(use_json: bool, retry_hint: str = "", prefill: str = "") -> str:
        """Execute one LLM API call. Returns raw_text (or raises).
        retry_hint: 非空时作为追加 user 消息发给模型（用于格式纠正重试）。
        prefill: 非空时以 assistant 预填内容**强行开场**（v342）。实测 MiniMax
                 系列在"先思考再作答"时会把 max_tokens 全花在 <think> 里，
                 最终一个字的正文都没有（finish=stop、content 只有 think）。
                 预填 "{" 后模型会直接从 JSON 接着写，完全跳过思考段。"""
        # ── v322: 精简模式（同行评审专用）────────────────────────────────
        # 事故（2026-11-05 实测）：评审 757 次调用烧了 13.75M token，
        # 占全天消耗 60%，比主分析（8.2M）还多 56% —— 而评审只需要
        # 「方向 + 分数」。因为它走的是**完整主分析 prompt**：
        # 八段推理链 + market_context + performance_context + review_insights
        # + verdict + kline + dim + calendar + stock + asia + origin + spa …
        # 十几个注入块堆出 10K+ token 的输入。
        #
        # 精简模式只发：极简 system（只要方向+分数）+ 新闻原文。
        # 预期每次 ~1K token → 评审成本降 85%+，
        # 且**不再挤占主分析的预算**（这才是「待分析」不降的直接原因）。
        if lean:
            prompt = _PEER_REVIEW_SYSTEM_PROMPT
            user_text = str(news_content or "")[:_MAIN_NEWS_CHARS]
            payload: Dict[str, Any] = {
                "model": model_cfg.get("request_model") or model_cfg["id"],
                "messages": [
                    # v323：契约恒定附加在 system prompt 末尾（模型最重视的位置）
                {"role": "system", "content": prompt + _OUTPUT_CONTRACT},
                    {"role": "user", "content": user_text},
                ],
                "temperature": 0.1,
                # v322: 注意 ** 后面不能直接跟 `or`（语法错误）→ 先算好再展开
                **(model_cfg.get("extra_body") or {}),
                # 评审只要一个方向+分数 → 200 token 足够
                # （主分析用 4000 是因为要输出八段推理链）
                "max_tokens": 300,
            }
            if retry_hint:
                payload["messages"].append({"role": "user", "content": retry_hint})
            if use_json:
                payload["response_format"] = {"type": "json_object"}
            headers = {
                "Authorization": f"Bearer {model_cfg['api_key']}",
                "Content-Type": "application/json",
            }
            url = f"{model_cfg['api_base']}/chat/completions"
            _brk_alias = str(model_cfg.get("key_alias") or model_cfg.get("label") or "unknown")
            _brk_q = _analysis_breaker(_brk_alias, "quota")
            _brk_a = _analysis_breaker(_brk_alias, "auth")
            _brk_n = _analysis_breaker(_brk_alias, "net")
            _brk_r = _analysis_breaker(_brk_alias, "ratelimit")
            if _brk_q is not None and _brk_n is not None:
                if not (_brk_q.allow() and _brk_n.allow() and (
                        _brk_a is None or _brk_a.allow())):
                    raise RuntimeError(
                        f"{model_cfg.get('label')} 通道熔断中（跳过请求，不消耗额度）"
                    )
            try:
                from common import llm_budget as _lb_lean
                _est_lean = _lb_lean.estimate_call_tokens(
                    str(payload.get("messages", "")), 300)
                _ok_lean, _why_lean = _lb_lean.check(_est_lean, purpose=purpose)
                if not _ok_lean:
                    raise RuntimeError(
                        f"{model_cfg.get('label')} 额度闸门拦截：{_why_lean}（未发出请求）"
                    )
                _ok_rpm_lean, _why_rpm_lean = _lb_lean.consume_rate_slot(purpose=purpose)
                if not _ok_rpm_lean:
                    raise RuntimeError(
                        f"{model_cfg.get('label')} {_why_rpm_lean}"
                    )
            except RuntimeError:
                raise
            except Exception:
                pass
            _t_lean = time.perf_counter()
            _ti = _to = _tc = 0
            try:
                _sess = _LLM_PROXY_SESSION if model_cfg.get("use_proxy") else _LLM_SESSION
                _resp = _sess.post(
                    url, json=payload, headers=headers,
                    timeout=float(model_cfg.get("timeout") or 60.0),
                )
                _lat_lean = int((time.perf_counter() - _t_lean) * 1000)
                if _resp.status_code >= 400:
                    try:
                        import db as _db_lean
                        _db_lean.log_ai_call(
                            purpose=purpose, key_alias=_brk_alias,
                            model_id=str(model_cfg.get("id") or "?"),
                            tokens_in=0, tokens_out=0, latency_ms=_lat_lean,
                            status="http_error",
                            error_code=str(_resp.status_code), call_site=call_site,
                        )
                    except Exception:
                        pass
                    raise RuntimeError(
                        f"{model_cfg.get('label')} HTTP {_resp.status_code}")
                _body = _resp.json()
                _txt = (_body.get("choices", [{}])[0].get("message", {})
                        .get("content", "") or "")
                try:
                    _usage = _body.get("usage", {}) or {}
                    _ti = int(_usage.get("prompt_tokens") or 0)
                    _to = int(_usage.get("completion_tokens") or 0)
                    _tc = int((_usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
                except Exception:
                    _ti = _to = _tc = 0
                try:
                    import db as _db_lean2
                    _db_lean2.log_ai_call(
                        purpose=purpose, key_alias=_brk_alias,
                        model_id=str(model_cfg.get("id") or "?"),
                        tokens_in=_ti, tokens_out=_to, cached_tokens=_tc,
                        latency_ms=_lat_lean,
                        status="ok", error_code="", call_site=call_site,
                    )
                except Exception:
                    pass
                # v398b：精简通道（评审/复核）同样拦 think-only —— M2.7 在 300 与
                # 900 token 下都只吐 <think>、正文 0 字（2026-11-16 实测），
                # 当成功返回会让上层剥成空串、重试 10 轮把整批拖到看门狗回收。
                _v398b_le = strip_think_blocks(_txt or "").strip()
                if not _v398b_le or ("<think" in (_txt or "") and len(_v398b_le) < 40):
                    print(f"  [{model_cfg.get('label')}] THINK-ONLY（精简通道）"
                          f"原文 {len(_txt or '')} 字 → 正文 {len(_v398b_le)} 字"
                          f" → 判失败换通道", flush=True)
                    raise ValueError("think-only response (lean)")
                return _txt
            except RuntimeError:
                raise
            except Exception as _e_lean:
                # think-only 是模型只吐 <think>、正文 0 字的输出问题，不是网络故障：
                # 单列 empty_response/think_only，不污染 network_error 统计、也不计入网络熔断。
                _is_think_only = isinstance(_e_lean, ValueError) and "think-only" in str(_e_lean)
                try:
                    import db as _db_lean3
                    _db_lean3.log_ai_call(
                        purpose=purpose, key_alias=_brk_alias,
                        model_id=str(model_cfg.get("id") or "?"),
                        tokens_in=_ti, tokens_out=_to, cached_tokens=_tc,
                        latency_ms=locals().get("_lat_lean", 0),
                        status="empty_response" if _is_think_only else "network_error",
                        error_code="think_only" if _is_think_only else type(_e_lean).__name__,
                        call_site=call_site,
                    )
                except Exception:
                    pass
                raise RuntimeError(
                    f"{model_cfg.get('label')} {type(_e_lean).__name__}: {_e_lean}")

        if model_cfg.get("label") == "Doubao":
            prompt = _main_prompt("doubao_prompt", _DOUBAO_SYSTEM_PROMPT)
        else:
            # 需求3：统一单通道——所有模型统一走 System Prompt（完整八段式），
            # 废弃 Prompt Force（_JSON_PROMPT_FORCE）独立通道，避免双通道内容叠加
            # 干扰八段式框架。JSON mode（response_format）仍按平台能力单独启用。
            use_full_prompt = True
            prompt = _main_prompt(
                "system_prompt" if use_full_prompt else "json_force_prompt",
                _SYSTEM_PROMPT if use_full_prompt else _JSON_PROMPT_FORCE,
            )
            # v345.12: 按新闻变化的内容一律不进 system。
            # 实测 MiniMax 自动前缀缓存：system 完全相同时第 2 次起 6,982/6,983 命中，
            # 命中价约 0.06 元/1M（全价 4.20）。但只要 system 尾部拼了一段每条新闻
            # 都不同的文字（因果链先验/图谱/经验/反思…），整段 system 就全部失效 →
            # 实测 cached_tokens 恒为 0。所以把动态块收集起来，改挂到 user 消息里。
            _dyn_parts: List[str] = []
            # v397：硬数据前置注入（本地库优先，60s 缓存）—— 让模型在推理前就看得见数。
            # 此前 hard_data_evidence 只在 LLM 漏填后由服务端回填，模型看不到 → 引用不了 →
            # market_confirmation 只能写 unknown（实测 99.1%）。
            try:
                _hd_block = _hard_data_block(str(gate_target_asset or ""))
                if _hd_block:
                    _dyn_parts.append(
                        "═══ 硬数据（服务端实测，引用须带时间）═══\n"
                        f"{_hd_block}\n"
                        "引用规则：上面有数时，[水位博弈] 与 hard_data_evidence 必须引用它并带时间戳；"
                        "带「数据质量提示」的行只说明该品种盘面不可用（冻结/过期），"
                        "不得据其判方向，方向判断需另行说明依据。")
                    print(f"  [HARD-DATA] {gate_target_asset} 注入 ({len(_hd_block)} chars)",
                          flush=True)
            except Exception as _hd_e:
                print(f"  [HARD-DATA] 注入失败(静默): {type(_hd_e).__name__}: {str(_hd_e)[:80]}",
                      flush=True)
            # 盘面结构分析开关：开 → 在水位博弈段附加多周期结构判断指令；关 → 不加
            if _market_structure_enabled():
                prompt = prompt + _main_prompt("market_structure_block", _MARKET_STRUCTURE_BLOCK)
            # 用户自定义分析指令（ai_prompts 表，启用的注入；5s TTL）
            custom_prompts = _load_custom_prompts_sync()
            if custom_prompts:
                prompt = (
                    prompt
                    + "\n\n═══ 用户自定义分析指令（优先级最高，必须遵守）═══\n"
                    + "\n\n".join(custom_prompts)
                )
            # 品种专项分析规则（ai_asset_rules 表）：按金融品类单独设置的分析规则。
            # 注入所有启用规则（按品种分节）——分析时 AI 已先做主体识别+归类，
            # 由 AI 对 target_asset / sibling 各自套用对应品种的规则。
            asset_rules_block = _load_asset_rules_block_sync()
            if asset_rules_block:
                prompt = prompt + "\n\n" + asset_rules_block
            # v176.3：分析流程已删除 [因果链分析] 段，因果链库不再注入 prompt；
            # 因果链库（causal_chains 表）与「因果链库」管理页仍保留，仅由页面维护数据。
            # v337：改为注入**资产级、证据加权**的命中链先验（chain_prior），
            # 让"出现新闻先按因果链决策"真正生效（原 v176.3 只是停用，没有替代）。
            chain_block = _build_chain_prior_block(gate_target_asset or "", news_content)
            if chain_block:
                _dyn_parts.append(chain_block)
                print(f"  [CHAIN-PRIOR] 命中因果链注入 ({len(chain_block)} chars) "
                      f"asset={gate_target_asset}", flush=True)
            # 事件结构化抽取（event_struct 规则确定性抽取）：挂载在品种规则之后，
            # 要求 AI 交叉校验规则量级与新闻是否一致（不一致在 [逻辑自检] 标黄修正）。
            event_block = _build_event_block(news_content)
            if event_block:
                _dyn_parts.append(event_block)
            # 长期慢变量视角 + 机理挖根 + 数据不全降级：恒定附加要求（不因事件为空而省略）
            longterm_block = _build_longterm_block()
            if longterm_block:
                prompt = prompt + "\n\n" + longterm_block
            # v176 因果图谱回灌：黄金/原油叙事逻辑硬先验（来自 causal_graph 自学习）
            graph_block = _build_graph_block(gate_target_asset or "")
            if graph_block:
                _dyn_parts.append(graph_block)
                print(f"  [GRAPH-BLOCK] 因果图谱注入 ({len(graph_block)} chars) asset={gate_target_asset}", flush=True)
            else:
                print(f"  [GRAPH-BLOCK] 因果图谱为空 asset={gate_target_asset}", flush=True)
            # v176 市场风格 + 5 层预警（ATR/趋势衰竭/费率/量能/OI）
            try:
                from market import market_style as _ms
                ms_block = _ms.market_style_block(gate_target_asset or "")
                if ms_block:
                    _dyn_parts.append(ms_block)
                    print(f"  [MS-BLOCK] 市场风格注入 ({len(ms_block)} chars) asset={gate_target_asset}", flush=True)
            except Exception:
                pass
            # v176.2 决策间长期影响传递：把"上次同品种判断 + regime + 反思"塞进 prompt
            # 让 LLM 知道历史方向与本期 bias（防止每条新闻凭印象写）
            try:
                _prev_ctx = _load_prev_decision(gate_target_asset or "")
                _ctx_parts = []
                if _prev_ctx.get("regime_at_decision"):
                    _ctx_parts.append(f"市场风格(regime): {_prev_ctx['regime_at_decision']}")
                if _prev_ctx.get("prev_action_same_asset"):
                    _ctx_parts.append(
                        f"上次同品种方向: {_prev_ctx['prev_action_same_asset']} "
                        f"(score={_prev_ctx.get('prev_score_same_asset')})"
                    )
                if _prev_ctx.get("bias_after_reflection"):
                    _ctx_parts.append(f"反思: {_prev_ctx['bias_after_reflection']}")
                if _ctx_parts:
                    _dyn_parts.append(
                        "═══ 决策间长期影响传递（必须参考，禁止忽略）═══\n" + "；".join(_ctx_parts))
            except Exception:
                pass
            # 复盘经验分区（═══ 复盘经验 ═══）：作为正式分区拼进 prompt 尾部，
            # 与品种规则并列。此前是挤成一行塞进 user 消息，模型难逐条对照。
            if insights_section:
                _dyn_parts.append(insights_section)

            # v192 #2: 按 target_asset 注入更精准的实战经验规则（asset-specific + TTL 缓存）
            # 与上面全局 insights 互补：上面是硬铁律/软启发（无 asset 区分），
            # 这里是从 review_insights 按 target_asset 匹配 + 按 source_samples 排序 + counter_factual 反例标注
            # v200 P0-A3: 同时拉回命中 ids，注入成功后写回 ai_decisions.insight_hit_ids，闭环可见
            # 注意: 复用 _call_llm_sync 层的共享容器 _asset_insight_hit_ids(原地更新),
            # 不再在此处 shadow 成新的局部列表(否则外层读不到 → NameError)。
            if gate_target_asset:
                try:
                    from ai_prompts import load_recent_insights_block
                    _asset_insights_res = load_recent_insights_block(
                        target_asset=gate_target_asset,
                        return_ids=True,
                    )
                    # 兼容旧版返回值（仅 str）
                    if isinstance(_asset_insights_res, tuple):
                        _asset_insights, _hit_ids_asset = _asset_insights_res
                        _asset_insight_hit_ids.clear()
                        _asset_insight_hit_ids.extend(
                            [int(x) for x in _hit_ids_asset
                             if isinstance(x, (int, float)) and int(x) > 0]
                        )
                    else:
                        _asset_insights = _asset_insights_res
                    if _asset_insights:
                        _dyn_parts.append(_asset_insights)
                        print(
                            f"  [INSIGHTS-ASSET] {gate_target_asset} 注入 "
                            f"({len(_asset_insights)} chars, "
                            f"hit_ids={_asset_insight_hit_ids})",
                            flush=True,
                        )
                except Exception as _exc:
                    # 不影响主流程；零注入
                    print(
                        f"  [INSIGHTS-ASSET] {gate_target_asset} 注入失败: "
                        f"{type(_exc).__name__}: {str(_exc)[:80]}",
                        flush=True,
                    )

            # v176.3 反思错误码注入：与 insights 互补 — "近期决策触发的具体错误码"
            # 30s TTL 缓存；任何异常 → 不注入（零开销）。从 decision_errors 表取。
            try:
                _ref_block = _load_reflection_block()
                if _ref_block:
                    _dyn_parts.append(_ref_block)
                    print(f"  [REFLECTION-BLOCK] 反思错误码注入 ({len(_ref_block)} chars)", flush=True)
            except Exception:
                pass

            # v186 经验系统注入 — 按当前 asset/gate_kind/actor 上下文从
            # experience_entries 取最相关 top-5 条经验。与上面 insights/reflection 互补:
            #   insights(全局硬铁律) + reflection(近期错误码) + experience(当前场景特化)
            try:
                # actor 从 news_source 或 gate_context 中提取;为安全起见取空串也行
                _actor_hint = ""
                try:
                    if news_source and ("特朗普" in news_source or "Trump" in news_source or "Powell" in news_source
                                        or "Gensler" in news_source or "Fed" in news_source
                                        or "SEC" in news_source or "V神" in news_source or "Vitalik" in news_source):
                        _actor_hint = news_source[:80]
                except Exception:
                    _actor_hint = ""
                _exp_block = _load_experience_block_sync(
                    asset=gate_target_asset or "",
                    gate_kind=event_type_code or "",
                    actor=_actor_hint,
                    decision_id=int(gate_decision_id or 0),
                )
                if _exp_block:
                    _dyn_parts.append(_exp_block)
                    print(f"  [EXP-BLOCK] v186 经验注入 ({len(_exp_block)} chars) "
                          f"asset={gate_target_asset or 'N/A'} actor={_actor_hint or 'N/A'}", flush=True)
            except Exception:
                pass

            # v379 语义记忆检索（P0）— 按「品种×事件类型×本条新闻」做向量相似度召回
            # 与上面几块的区别：insights/reflection/experience 都是「按规则挑」，
            # 这里是「按相似度挑」——同机理不同措辞的历史教训也能召回来，且只给 top-5。
            try:
                from semantic_memory import load_semantic_block
                _sem_block = load_semantic_block(
                    asset=gate_target_asset or "",
                    event_type=event_type_code or "",
                    query=news_content or "",
                )
                if _sem_block:
                    _dyn_parts.append(_sem_block)
                    print(f"  [SEMANTIC] 语义记忆注入 ({len(_sem_block)} chars) "
                          f"asset={gate_target_asset or 'N/A'} event={event_type_code or 'N/A'}",
                          flush=True)
            except Exception as _sem_e:
                print(f"  [SEMANTIC] 注入失败(静默): {type(_sem_e).__name__}: {str(_sem_e)[:80]}",
                      flush=True)

            # v199: SPA applied 设置注入 — 从 strategy_param_adjustments 读 status='applied' 的设置,
            # 按 asset 聚合注入 prompt, 让 LLM 决策时知道「已被 AI 系统采纳的过滤/分析/仓位/分数调整」。
            try:
                _spa_block = _load_spa_applied_block_sync(
                    asset=gate_target_asset or "", max_chars=600,
                )
                if _spa_block:
                    _dyn_parts.append(_spa_block)
                    print(f"  [SPA-APPLY-BLOCK] v199 已应用 SPA 注入 ({len(_spa_block)} chars) "
                          f"asset={gate_target_asset or 'N/A'}", flush=True)
            except Exception:
                pass

        # 组装: [新闻时间] + [市场快照] + [Hermes 分席] + [历史绩效] + [新闻正文]
        user_text = news_content[:_MAIN_NEWS_CHARS]
        # 门卫四步标签：紧贴新闻正文，作为分析先验（核心影响/主品种/判词）
        if gate_context:
            user_text = user_text + "\n\n" + gate_context
        if news_timestamp:
            try:
                ts_dt = datetime.fromisoformat(news_timestamp.replace("Z", "+00:00"))
                ts_str = ts_dt.strftime("%Y-%m-%d %H:%M UTC")
            except (ValueError, TypeError):
                ts_str = news_timestamp[:19]
            user_text = f"[新闻时间] {ts_str}\n\n{user_text}"
        # ── 历史绩效（baseline.by_type）：从 backtest_event_outcomes 读历史 PnL ──
        # v174: 在 SERVER VERDICT 之前注入,让 AI 先看历史胜率再看实时快照 → 决策更稳
        baseline_block = _build_baseline_block(event_type_code, gate_target_asset)
        if baseline_block:
            user_text = baseline_block + "\n\n" + user_text
            print(f"[BASELINE] 已注入历史绩效段 | event_type={event_type_code or 'N/A'} | asset={gate_target_asset or 'N/A'} | {len(baseline_block)} chars", flush=True)
        # ── v200+ P0-1: 微观结构(持仓/资金费率) ── 在 baseline 后注入,让 LLM 在历史胜率和微观仓位之间交叉看 →
        try:
            _micro_targets: List[str] = []
            if gate_target_asset and gate_target_asset.upper() != "NONE":
                _micro_targets = [str(gate_target_asset).upper()]
            # 再补几条关联品种(用于跨资产传导参考)
            for _sym in ("BTC", "ETH", "XAU"):
                if _sym not in _micro_targets:
                    _micro_targets.append(_sym)
            micro_block = _build_microstructure_block(target_assets=_micro_targets)
            if micro_block:
                user_text = micro_block + "\n\n" + user_text
                print(f"[MICROSTRUCTURE] 已注入微观结构段 | targets={_micro_targets} | {len(micro_block)} chars", flush=True)
        except Exception as _e:
            print(f"[MICROSTRUCTURE] 注入失败(静默): {type(_e).__name__}: {str(_e)[:80]}", flush=True)
        # ── v343: 技术状态读数 —— 与 microstructure 并列 ────────────────────
        # microstructure 说"谁在持仓"，本段说"盘面处在什么状态"。
        # 读数只描述状态，不是入场信号；prompt 里明确要求归到 [水位博弈] 段引用。
        try:
            _tech_targets: List[str] = []
            if gate_target_asset and gate_target_asset.upper() != "NONE":
                _tech_targets = [str(gate_target_asset).upper()]
            for _sym in ("BTC", "ETH", "XAU"):
                if _sym not in _tech_targets:
                    _tech_targets.append(_sym)
            tech_block = _build_technical_state_block(target_assets=_tech_targets)
            if tech_block:
                user_text = tech_block + "\n\n" + user_text
                print(f"[TECH_STATE] 已注入技术状态读数 | targets={_tech_targets} | {len(tech_block)} chars", flush=True)
        except Exception as _e:
            print(f"[TECH_STATE] 注入失败(静默): {type(_e).__name__}: {str(_e)[:80]}", flush=True)
        # ── v309 P0-3: 历史归因参照（该品种近期异动由哪条新闻解释）──
        # 与 baseline / microstructure 同级: 是研究参照, 不是信号源。
        # 放在 microstructure 之后, 让 AI 先看盘面与持仓, 再看历史归因。
        try:
            _attr_block = _load_attribution_block(gate_target_asset or "")
            if _attr_block:
                user_text = _attr_block + "\n\n" + user_text
                print(f"[ATTRIBUTION-BLOCK] 已注入历史归因参照 ({len(_attr_block)} chars) "
                      f"asset={gate_target_asset or 'N/A'}", flush=True)
        except Exception as _e:
            print(f"[ATTRIBUTION-BLOCK] 注入失败(静默): {type(_e).__name__}: {str(_e)[:80]}", flush=True)
        # ── v174: 事件类型选项清单（dim_event_type 维表）──
        # 让 LLM 知道有 25 个标准 type_code 可选, 而不是自由输出 event_type 字符串。
        # 必填字段: event_type_code, 否则视为 schema 失败(影响 baseline JOIN 率)。
        dim_block = _load_dim_event_type_block()
        if dim_block:
            user_text = dim_block + "\n\n" + user_text
            print(f"[DIM-EVENT-TYPE] 已注入事件类型清单 | {dim_block.count(chr(10))} 行", flush=True)
        # ── SERVER VERDICT：server 端预生成的水位/价格/预期结论 ──
        # 让 AI 直接引用 server 算好的大结论，不必再算 RSI/ADX/VWAP → 节省 token 也减少误判。
        # market_context 是摘要文本（render_prompt_block），用于 AI 看到原始数据自校。
        # 注意：必须在 market_context 之后、calendar_hint 之前注入；CAL-HINT 不应被 VERDICT 覆盖。
        try:
            # v200 P4: 调公共 API get_full_verdict_block（等价 build_verdict + 跨品种对比段）
            # 同时保留 build_verdict 向后兼容（其他模块/测试可能仍调旧函数）
            from server_verdict import get_full_verdict_block
            # _last_good_snapshot 是 _ai_worker_loop 的局部变量，嵌套函数 _do_api_call 看不到。
            # 我们通过 ai_worker.py 模块级 cache（_SERVER_SNAPSHOT_CACHE）读快照。
            global _SERVER_SNAPSHOT_CACHE
            snap_dict = _SERVER_SNAPSHOT_CACHE.get("json") or {}
            if isinstance(snap_dict, str):
                try:
                    snap_dict = json.loads(snap_dict)
                except Exception:
                    snap_dict = {}
            # 防御：news_timestamp / gate_target_asset 可能为 None，build_verdict 期望 str/str/int
            _safe_ts = str(news_timestamp) if news_timestamp else ""
            _safe_ta = str(gate_target_asset) if gate_target_asset else ""
            _safe_id = int(gate_decision_id) if gate_decision_id else 0
            verdict_block = get_full_verdict_block(
                news_ts=_safe_ts,
                target_asset=_safe_ta,
                current_id=_safe_id,
                snap=snap_dict if isinstance(snap_dict, dict) else None,
            )
            if verdict_block:
                # v422：体积上限。实测 SERVER VERDICT 块 1,619–1,637 字符，而每次
                # 主分析都带一遍（一条决策 1.5 次调用）→ 光这一块每天 ≈ 0.4M token。
                # 保留头部（当前标的结论 + 最近的对照），尾部跨品种长表按需截断。
                _vd_max = int(os.getenv("TRIDENT_VERDICT_MAX_CHARS", "1200") or 1200)
                if _vd_max > 0 and len(verdict_block) > _vd_max:
                    _cut = verdict_block[:_vd_max].rstrip()
                    verdict_block = (
                        _cut + "\n（VERDICT 块超出上限已截断，仅保留与本次标的相关的头部）"
                    )
                user_text = verdict_block + "\n\n" + user_text
                # 同步更新至 cache["verdict"]（便于后续 fallback 重试注入同一份结论，不重复算）
                print(f"[VERDICT] 已注入 SERVER VERDICT 块 ({len(verdict_block)} chars, 含 P4 跨品种对比) | target={_safe_ta or 'N/A'} | snap_status={(snap_dict or {}).get('status','none')}", flush=True)
        except Exception as _e:
            import traceback
            print(f"[VERDICT] 注入失败（不影响 AI 主流程）: {type(_e).__name__}: {_e}", flush=True)
            print(f"[VERDICT] TRACEBACK:\n{traceback.format_exc()}", flush=True)
        if market_context:
            user_text = market_context + "\n\n" + user_text
        calendar_hint = _build_calendar_hint()
        if calendar_hint:
            user_text = calendar_hint + "\n\n" + user_text
            # 留痕：让运维/前端可观测日历提示确实注入了 prompt（非空时记条数）
            n_events = calendar_hint.count("★")
            print(f"[CAL-HINT] 已注入 {n_events} 条 24h 内重要财经事件到 prompt", flush=True)
        else:
            # 静默问题修复：配额耗尽/MCP 不可达也要可见，避免"日历提示缺失"无任何留痕
            status = _CALENDAR_STATUS
            if status.get("quota_exhausted"):
                print("[CAL-HINT] 日历提示为空：所有 JIN10_MCP_TOKEN 今日配额耗尽，明日 0 点重置", flush=True)
            elif status.get("error"):
                print(f"[CAL-HINT] 日历提示为空: {status.get('error')[:200]}", flush=True)
            else:
                print("[CAL-HINT] 日历提示为空：当前 24h 内无 ★2+ 重要财经事件", flush=True)
        # 美股行情上下文：新闻提到币安永续股票（NVDA/TSLA/AAPL/MSTR 等）时附上当前价
        # （10 分钟缓存；拉不到不阻塞分析——行情只是上下文）
        try:
            stock_line = _stock_context_for(news_content)
            if stock_line:
                user_text = stock_line + "\n\n" + user_text
        except Exception:
            pass
        # v175：SK 海力士 / A股 行情上下文（10 分钟缓存；拉不到不阻塞分析）
        try:
            asia_line = _asia_context_for(news_content)
            if asia_line:
                user_text = asia_line + "\n\n" + user_text
        except Exception:
            pass
        # v175：实时全网搜索证据（免费源）→ 供 AI 客观填 cross_net_*，替代凭记忆猜
        try:
            origin_block = _origin_search_block_for(news_content)
            if origin_block:
                user_text = origin_block + "\n\n" + user_text
        except Exception:
            pass
        if writing_context:
            user_text = writing_context + "\n\n" + user_text
        if performance_context:
            user_text = performance_context + "\n\n" + user_text
        # 注意：复盘经验不再拼进 user 消息，已作为「═══ 复盘经验 ═══」分区注入 system prompt
        if reference_signals:
            user_text = reference_signals + "\n\n" + user_text
        # v345.12: 上面从 system 挪出来的动态块统一挂到 user 尾部之前，
        # 让 system 保持恒定以命中前缀缓存。
        if "_dyn_parts" in dir() and _dyn_parts:
            user_text = "\n\n".join(_dyn_parts) + "\n\n" + user_text

        # v435 诊断：user 消息 13.5k 字符到底是谁占的（TRIDENT_PROMPT_PARTS=1 才打）。
        try:
            if str(os.getenv("TRIDENT_PROMPT_PARTS", "0")).strip() == "1" and purpose == "main":
                _parts = {k: len(v) for k, v in locals().items()
                          if isinstance(v, str) and len(v) >= 200}
                _top = sorted(_parts.items(), key=lambda kv: -kv[1])[:8]
                print("[PARTS] " + " | ".join(f"{k}={n}" for k, n in _top), flush=True)
        except Exception:
            pass
        # v345.13 诊断：把 system 指纹打进日志，用来确认"缓存不命中是不是因为
        # system 内容在变"。同一批里出现不同 sysfp 就说明还有动态内容没挪干净。
        try:
            import hashlib as _hl
            # v442 诊断：TRIDENT_PROMPT_DUMP=1 时把真实 prompt 落盘，用来核对
            # "12k token 到底花在哪个分区"。默认关闭，只在排查时开。
            if str(os.getenv("TRIDENT_PROMPT_DUMP", "0")).strip() == "1" and purpose == "main":
                try:
                    with open("/tmp/trident_prompt_dump.txt", "w") as _f:
                        _f.write("=== SYSTEM (" + str(len(prompt)) + ") ===\n")
                        _f.write(prompt + "\n=== USER (" + str(len(user_text)) + ") ===\n")
                        _f.write(user_text + "\n")
                except Exception:
                    pass
            if purpose in ("main", "verify"):
                print(f"  [CACHE-DBG] syslen={len(prompt)} userlen={len(user_text)} "
                      f"sysfp={_hl.md5(prompt.encode('utf-8')).hexdigest()[:10]} "
                      f"model={model_cfg.get('request_model') or model_cfg['id']}", flush=True)
        except Exception:
            pass
        payload: Dict[str, Any] = {
            "model": model_cfg.get("request_model") or model_cfg["id"],
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": user_text},
            ],
            "temperature": 0.1,
            **model_cfg["extra_body"],
            # 八段推理链 + 20 字段 JSON + 因果链 + 总结：1500 token 易在 [自检] 之后被截断，
            # 2500 token 仍可能让长 prompt 下段间连接词被压扁。
            # v176.7：6000 → 4000。实测 thinking 已禁用、直接输出 JSON 后 reasoning_path 不会
            # 超过 4000 token；6000 上限会让 M3 在长 prompt 下生成时间拖长、撞 65s 外层超时
            # （llm_call_failures 16:19-17:00 大量 TimeoutError）。4000 留足八段篇幅同时压生成时长。
            # max_tokens 必须放最后 — 覆盖 extra_body 里 MiniMax M3 的 4000 默认。
            # v471：改为可配置。实测 M3 长 prompt 下先输出大段 <think>，4000 被截断成
            # "剥 think 后只剩 73 字"，JSON 写不完 → 标的恒 NONE。抬到 6000 需同时
            # 抬 MINIMAX_TIMEOUT，否则撞 65s 外层超时。
            "max_tokens": int(os.getenv("MINIMAX_MAX_TOKENS", "4000") or 4000),
        }
        if retry_hint:
            payload["messages"].append({"role": "user", "content": retry_hint})
        if prefill:
            payload["messages"].append({"role": "assistant", "content": prefill})
        if use_json and not prefill:
            # 预填 assistant 与 response_format=json_object 互斥（部分网关会 400）
            payload["response_format"] = {"type": "json_object"}

        # ── requests 客户端 ──
        # timeout 对「连接 + 读取」都生效（urllib 在个别代理/响应路径可能不触发超时，
        # 导致 LLM 调用无限挂起拖死 ai_worker）。默认 trust_env 走 env HTTP_PROXY，
        # 与实测路径一致（aiping 走代理 1~3s 响应）。
        headers = {
            "Authorization": f"Bearer {model_cfg['api_key']}",
            "Content-Type": "application/json",
        }
        url = f"{model_cfg['api_base']}/chat/completions"

        # HTTP 429 retry logic — upstream rate limits are transient
        # v432（2026-11-17）：重试阶梯改为 env 可配，默认保持原三档。
        # 实测当日 main 调用 347 次只产出 22 条决策（约 15.8 次/条），
        # 其中单条最多要打 1+3=4 次；把阶梯收成两档可把最坏放大从 4x 降到 2x，
        # 不需要改分析逻辑（429 已由熔断器拦截，重试只对网络抖动有意义）。
        retry_delays = [
            float(x) for x in str(os.getenv("LLM_RETRY_DELAYS", "3,6,12")).split(",")
            if x.strip()
        ]  # progressive backoff（缩短：批量限流时更快恢复）
        last_err: Optional[str] = None
        # v187：记录本次调用开始时间（统计 latency_ms 用）
        _t_call_start = time.perf_counter()
        # ── v322: 通道熔断（主分析路径此前完全没有）────────────────────
        # 配额类(429/401/402/403) 与 网络类 分别计失败，各自冷却。
        # 熔断打开时**直接跳过本通道**（不再发任何请求）→ 通道全挂时零额度消耗。
        _brk_alias = str(model_cfg.get("key_alias") or model_cfg.get("label") or "unknown")
        # v501：这个 key 已经 402 欠费 → 直接跳过，交给下一个通道（不白打请求）
        try:
            from common import llm_budget as _lbk_main
            # 两档：整条 main 链路被闸（空产出风暴）或这一把 key 欠费
            if _lbk_main.quota_dead("main") or _lbk_main.quota_dead("key:%s" % _brk_alias):
                raise RuntimeError(
                    f"{model_cfg.get('label')} 额度欠费短路中（跳过请求，不消耗额度）"
                )
        except RuntimeError:
            raise
        except Exception:
            pass
        _brk_q = _analysis_breaker(_brk_alias, "quota")
        _brk_a = _analysis_breaker(_brk_alias, "auth")
        _brk_n = _analysis_breaker(_brk_alias, "net")
        _brk_r = _analysis_breaker(_brk_alias, "ratelimit")
        if _brk_q is not None and _brk_n is not None:
            if not (_brk_q.allow() and _brk_n.allow() and (
                    _brk_a is None or _brk_a.allow())):
                raise RuntimeError(
                    f"{model_cfg.get('label')} 通道熔断中（跳过请求，不消耗额度）"
                )

        # ── v322: 额度闸门（发请求之前的最后一道）────────────────────────
        # 认领前的体检只防"通道全挂"；这里防的是"额度已经见底"。
        # 估算本次要花的 token，剩余不够就直接拒 —— 一个字节都不发出去。
        _est_tokens = 0
        try:
            from common import llm_budget
            _est_tokens = llm_budget.estimate_call_tokens(
                str(payload.get("messages", "")),
                int(config.LLM_MAIN_MAX_OUTPUT_TOKENS),
            )
            _cap_one = int(getattr(config, "LLM_MAX_TOKENS_PER_NEWS", 30000))
            if _est_tokens > _cap_one:
                raise RuntimeError(
                    f"{model_cfg.get('label')} 单条预估 {_est_tokens} token "
                    f"超过单条上限 {_cap_one}（放弃本条，避免一条吃掉全天预算）"
                )
            _ok, _reason = llm_budget.check(_est_tokens, purpose=purpose)
            if not _ok:
                raise RuntimeError(
                    f"{model_cfg.get('label')} 额度闸门拦截：{_reason}（未发出请求）"
                )
        except RuntimeError:
            raise
        except Exception:
            pass  # 预算模块异常 → 放行（不能因为闸门故障停摆生产）
        for attempt, delay in enumerate([0] + retry_delays):
            if delay > 0:
                time.sleep(delay)
            # ── v322: 速率闸门（每次真实请求前扣名额）──────────────────
            # 重试循环里的**每一次**请求都要扣 —— 否则重试会绕过限流，
            # 这正是「同一条新闻连打 30 次把额度烧穿」的形态。
            try:
                from common import llm_budget as _lb_rpm
                _ok_rpm, _why_rpm = _lb_rpm.consume_rate_slot(purpose=purpose)
                if not _ok_rpm:
                    raise RuntimeError(
                        f"{model_cfg.get('label')} 速率闸门拦截：{_why_rpm}（未发出请求）"
                    )
            except RuntimeError:
                raise
            except Exception:
                pass  # 闸门自身异常 → 放行（不能因为闸门故障停摆生产）
            try:
                session = _LLM_PROXY_SESSION if model_cfg.get("use_proxy") else _LLM_SESSION
                # v322: timeout 不能硬编码 60 —— 本地 Ollama 通道需要更长时间。
                # 事故背景（2026-11-04）：本地兜底通道 local/qwen2.5:3b 被硬编码的
                # 60s 砍掉（config 里 LOCAL_LLM_TIMEOUT=180 形同虚设）→ 主分析
                # 全部 ReadTimeout → 本地兜底对主分析**完全无效**。
                # 门卫那条路走 realtime_filter._post_openai_compat（用配置里的 timeout），
                # 所以门卫能成功（实测 175s）而主分析不行 —— 同一个模型，两种待遇。
                _req_timeout = float(model_cfg.get("timeout") or 60.0)
                resp = session.post(
                    url,
                    json=payload,
                    headers=headers,
                    timeout=_req_timeout,
                )
                _is_rate_limit = False
                if resp.status_code == 429:
                    # v322 二次加固：429 **不再重试**。
                    # 429 = 配额打满 / 被限流，重试的结果是可预测的 —— 再打一次
                    # 只是在烧用户的额度。原来"睡 2s 重试一次"是漏钱口之一。
                    # 现在：立刻记配额类失败（阈值 1 → 立即熔断 30 分钟）+ 快速失败切 fallback。
                    #
                    # 2026-11-15: 429 还要再分一层 ——「瞬时限速」与「额度耗尽」不是一回事。
                    # 服务端返回 {"type":"rate_limit_error","message":"已达到 Token Plan
                    # 速率限制 (2062)"} 时，额度根本没打满，几十秒就会恢复；若按 quota 冷冻
                    # 30 分钟，会把一次瞬时限速放大成半小时静默（实测曾连续 46 小时零决策）。
                    last_err = "HTTP 429"
                    _rl_body = ""
                    try:
                        _rl_body = (resp.text or "")[:400].lower()
                    except Exception:
                        _rl_body = ""
                    _is_rate_limit = any(
                        k in _rl_body for k in ("rate_limit", "速率限制", "tpm", "rpm",
                                                "too many requests", "concurrency")
                    )
                    if _is_rate_limit:
                        if _brk_r is not None:
                            _brk_r.failure()
                        print(
                            f"  [{model_cfg['label']}] HTTP 429 瞬时限速（不重试，"
                            f"{int(_ANALYSIS_RATELIMIT_COOLDOWN_S)}s 后重试；不计入配额熔断）",
                            flush=True,
                        )
                        last_err = "HTTP 429 rate_limit"
                    else:
                        if _brk_q is not None:
                            _brk_q.failure()
                        print(
                            f"  [{model_cfg['label']}] HTTP 429 配额打满，"
                            f"立即熔断该通道（不重试）",
                            flush=True,
                        )
                        last_err = "HTTP 429 quota"
                    # 直接进失败分支（不再 continue）
                # 中转平台上游瞬时错误（403/5xx）也重试 2 次
                elif resp.status_code in (403, 500, 502, 503) and attempt < 2:
                    print(f"  [{model_cfg['label']}] HTTP {resp.status_code}, retry {attempt+1}/2 after {delay}s", flush=True)
                    last_err = f"HTTP {resp.status_code}"
                    # v322: 403 → 鉴权类（key 失效，24h 不探测）；5xx → 网络类
                    if resp.status_code == 403:
                        if _brk_a is not None:
                            _brk_a.failure()
                        elif _brk_q is not None:
                            _brk_q.failure()
                    elif _brk_n is not None:
                        _brk_n.failure()
                    time.sleep(max(delay, 3.0))
                    continue
                if resp.status_code >= 400:
                    # 错误分类（与 model_probe._classify_probe_failure 口径一致）：
                    #   401/403     = key 失效/无权限  -> auth 熔断（15 分钟不探测）
                    #   402         = 套餐/余额耗尽   -> quota 熔断（30 分钟，需充值/换套餐）
                    #   429         = 配额/瞬时限速    -> quota / ratelimit 熔断
                    #   其余         = 网络类          -> net 熔断（60 秒）
                    if resp.status_code == 402:
                        # 402 = insufficient_balance / 套餐用量上限，属 quota 类（不是 key 失效）
                        if _brk_q is not None:
                            _brk_q.failure()
                        elif _brk_a is not None:
                            _brk_a.failure()
                        try:
                            from common import llm_budget as _lbq2
                            _lbq2.note_quota_dead(
                                30, "main 402",
                                scope="key:%s" % (model_cfg.get("key_alias") or "unknown"))
                        except Exception:
                            pass
                        print(
                            f"  [{model_cfg['label']}] HTTP 402 余额/套餐用量耗尽 "
                            f"-> 配额熔断 {int(_ANALYSIS_QUOTA_COOLDOWN_LONG_S // 60)} 分钟（需充值/升级套餐）",
                            flush=True,
                        )
                    elif resp.status_code in (401, 403):
                        if _brk_a is not None:
                            _brk_a.failure()
                        elif _brk_q is not None:
                            _brk_q.failure()
                        print(
                            f"  [{model_cfg['label']}] HTTP {resp.status_code} "
                            f"key 失效/无权限 -> auth 熔断 {int(_ANALYSIS_AUTH_COOLDOWN_S // 60)} 分钟（需更换 API key）",
                            flush=True,
                        )
                    elif resp.status_code == 429:
                        # 瞬时限速已在上面记入 ratelimit 熔断，这里不再重复记 quota
                        if not _is_rate_limit and _brk_q is not None:
                            _brk_q.failure()
                    elif _brk_n is not None:
                        _brk_n.failure()
                    raise RuntimeError(
                        f"{model_cfg['label']} HTTP {resp.status_code}: {resp.text[:300]}"
                    )
                body = resp.json()
                raw_text = (body["choices"][0]["message"]["content"] or "").strip()
                if prefill:
                    # 预填只是"开场白"，模型接着写；拼回来才是完整 JSON
                    raw_text = prefill + raw_text
                if not raw_text:
                    finish = body["choices"][0].get("finish_reason", "unknown")
                    body_preview = resp.text[:500]
                    print(f"  [{model_cfg['label']}] EMPTY RESPONSE | finish_reason={finish} | body_preview={body_preview}", flush=True)
                    # v346：空响应不再直接抛错（抛错只会让上层换模型重来，又是 13K 一次）。
                    # 就地改走精简两步：短 prompt 锁标的 → 中 prompt 补八段 → 拼成完整 JSON 返回。
                    # 实测短 prompt 出正文率远高于全量，且总成本约为一次全量的 2/3。
                    try:
                        _ra_t = _parse_result(two_step_llm_call(
                            model_cfg, mode="a", news_text=str(news_content or ""),
                            gate_context=str(gate_context or ""),
                            timestamp=str(news_timestamp or ""),
                            timeout=float(model_cfg.get("timeout") or 45.0),
                            call_site="empty_fallback_a",
                        ) or "")
                        _ta_t = str(_ra_t.get("target_asset") or "").strip().upper()
                        if _ta_t and _ta_t not in ("NONE", "OTHER"):
                            _rb_t = _parse_result(two_step_llm_call(
                                model_cfg, mode="b", news_text=str(news_content or ""),
                                gate_context=str(gate_context or ""),
                                timestamp=str(news_timestamp or ""),
                                asset=_ta_t,
                                action=str(_ra_t.get("suggested_action") or "HOLD"),
                                timeout=float(model_cfg.get("timeout") or 60.0),
                                call_site="empty_fallback_b",
                            ) or "")
                            _merged_t, _nts_t = merge_two_step(_ra_t, _ra_t, _rb_t)
                            if not is_skeleton_result(_merged_t):
                                print(f"  [{model_cfg['label']}] ★ 空响应已由精简两步补出"
                                      f"（{_ta_t}，{', '.join(_nts_t[:4])}）", flush=True)
                                return json.dumps(_merged_t, ensure_ascii=False)
                    except Exception as _fb_exc:
                        print(f"  [{model_cfg['label']}] 空响应兜底失败: "
                              f"{type(_fb_exc).__name__}", flush=True)
                    raise ValueError(f"empty response from {model_cfg['label']}")
                # v345.13: 空壳响应判定。实测 18% 的主分析调用返回 28-31 token 的
                # "没有正文的 JSON"（日志里是「预填 '{' 仍无正文」）——这不是有效答案，
                # 但也不是空串，于是被当作成功：12,000 token 输入照付，下游只能兜底
                # 出一张 HOLD/0 分的假决策。主分析契约是 20 字段 JSON，正常输出 800-1,400
                # token；低于 120 且不含关键字段的，一律判失败走 fallback 重试。
                # v398b：think-only 响应对所有用途一律判失败。
                # 实测（2026-11-16，同一个 key 连打 5 种参数变体：无 thinking 字段 /
                # thinking.disabled / chat_template_kwargs.enable_thinking=false /
                # reasoning_effort=none / 预填 "{"）——MiniMax-M2.7 通道全部只吐
                # 一段 <think>（66-263 字）然后 finish=stop，正文 0 字。
                # 不是 token 不够（300/900 都试过），是这条通道根本不出正文。
                # 旧逻辑只在 purpose=="main" 做空壳判定，评审/两步这些通道拿到
                # think-only 文本后当成功返回 → 上层剥 think 得空 → 判骨架 →
                # 换窗口重试 10 轮 → 单条新闻吃掉整批 15 分钟 → 看门狗回收整批。
                # 现在：任何用途剥完 think 都没正文 → 立刻抛错换下一个模型，
                # 不在一条不出正文的通道上烧重试。
                _v398_stripped = strip_think_blocks(raw_text).strip()
                if "<think" in raw_text and len(_v398_stripped) < 40:
                    print(f"  [{model_cfg['label']}] THINK-ONLY 响应"
                          f"（原文 {len(raw_text)} 字，剥 think 后 {len(_v398_stripped)} 字）"
                          f" → 判失败，换下一条通道", flush=True)
                    raise ValueError(f"think-only response from {model_cfg['label']}")
                if purpose == "main":
                    # 用「JSON 字段个数」判断，而不是长度/关键词：空壳响应往往带
                    # 一两个字段（如 {"sentiment_score":0,"suggested_action":"HOLD"}），
                    # 长度检查会放过它，但它产出的是 0 分 HOLD 的假决策。
                    # 主分析契约是 20 字段，正常输出 14-20 个 key。
                    _n_keys = len(re.findall(r'"[a-z_]+"\s*:', raw_text))
                    if _n_keys < 4:
                        print(f"  [{model_cfg['label']}] SHELL RESPONSE "
                              f"(keys={_n_keys}, {len(raw_text)} chars) → 判失败重试 | "
                              f"head={raw_text[:160]!r}", flush=True)
                        raise ValueError(f"shell response keys={_n_keys} from {model_cfg['label']}")
                # v187：捕获 token 用量,写到 ai_call_stats(主路径)
                try:
                    _usage = body.get("usage") or {}
                    _tok_in = int(_usage.get("prompt_tokens") or 0)
                    _tok_out = int(_usage.get("completion_tokens") or 0)
                    # v345.12: 缓存命中数（MiniMax 自动前缀缓存，命中部分按低价计费）
                    _tok_cached = int((_usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
                    # 估算延迟(整个 retry-loop 累计)
                    _latency_ms = int((time.perf_counter() - _t_call_start) * 1000)
                    import db as _db_stats
                    _db_stats.log_ai_call(
                        purpose=purpose,
                        key_alias=str(model_cfg.get("key_alias") or model_cfg.get("label") or "unknown"),
                        model_id=str(model_cfg.get("id") or "?"),
                        tokens_in=_tok_in, tokens_out=_tok_out,
                        cached_tokens=_tok_cached,
                        latency_ms=_latency_ms, status="ok",
                        call_site=call_site,
                    )
                except Exception:
                    pass
                # v322: 调用成功 → 熔断器清零并闭合
                if _brk_q is not None:
                    _brk_q.success()
                if _brk_n is not None:
                    _brk_n.success()
                if _brk_r is not None:
                    _brk_r.success()
                return raw_text
            except requests.RequestException as exc:
                status_code = getattr(exc.response, "status_code", None)
                if status_code == 429 and attempt < 1:
                    print(f"  [{model_cfg['label']}] HTTP 429, retry once after 2s", flush=True)
                    last_err = "HTTP 429"
                    if _brk_q is not None:
                        _brk_q.failure()
                    time.sleep(2.0)
                    continue
                # 网络瞬断（SSL 握手失败/连接失败/超时）也重试，不再一次失败即记入故障
                if isinstance(exc, (requests.exceptions.SSLError,
                                    requests.exceptions.ConnectionError,
                                    requests.exceptions.Timeout)) and attempt < 3:
                    print(f"  [{model_cfg['label']}] 网络瞬断 {type(exc).__name__}, retry {attempt + 1}/3 after {max(delay, 2.0):.0f}s", flush=True)
                    last_err = type(exc).__name__
                    if _brk_n is not None:
                        _brk_n.failure()
                    time.sleep(max(delay, 2.0))
                    continue
                # v322: 最终失败 → 记网络类熔断（连续 3 次即熔断 60s）
                if _brk_n is not None:
                    _brk_n.failure()
                raise RuntimeError(
                    f"{model_cfg['label']} 请求失败: {type(exc).__name__}: {str(exc)[:150]}"
                ) from exc
        # All retries exhausted — v187：写失败 token 埋点
        try:
            _latency_ms = int((time.perf_counter() - _t_call_start) * 1000)
            import db as _db_stats
            _db_stats.log_ai_call(
                purpose=purpose,
                key_alias=str(model_cfg.get("key_alias") or model_cfg.get("label") or "unknown"),
                model_id=str(model_cfg.get("id") or "?"),
                tokens_in=0, tokens_out=0,
                latency_ms=_latency_ms, status="network_error",
                error_code=str(last_err)[:32] or "retries_exhausted",
                call_site=call_site,
            )
        except Exception:
            pass
        raise RuntimeError(f"{model_cfg['label']} HTTP 429 (exhausted retries): {last_err}")

    # ------------------------------------------------------------------
    # Inner: parse raw text → result dict (with all three fallback tiers)
    # ------------------------------------------------------------------
    def _parse_result(raw_text: str) -> Dict[str, Any]:
        """Parse LLM output — never returns empty dict (keyword fallback guarantees)."""
        # v323 一次性取证：把模型原始返回落盘，用于确认"空字段"到底是模型没写还是解析丢了。
        # 仅在环境变量 TRIDENT_DUMP_RAW=1 时生效，且最多留 20 份，避免刷盘。
        try:
            import os as _os_dbg, pathlib as _pl_dbg
            if _os_dbg.getenv("TRIDENT_DUMP_RAW", "0") == "1":
                _d = _pl_dbg.Path("/tmp/trident_raw")
                _d.mkdir(exist_ok=True)
                if len(list(_d.glob("*.txt"))) < 20:
                    (_d / f"{int(time.time()*1000)}_{_os_dbg.getpid()}.txt").write_text(raw_text, encoding="utf-8")
        except Exception:
            pass
        # ── 剥离思维链块（M3 / 新一代模型常输出 <think>...</think> 然后才给 JSON；
        #    不剥掉会让 json.loads 在第一个 { 前撞到 "<think>"，JSON 解析失败 → 误入 fallback） ──
        # v342: 未闭合 <think> 不再"删到文末"（会连 JSON 一起吃），改为抢救标签后的 JSON。
        _had_unclosed = has_unclosed_think(raw_text)
        cleaned = strip_think_blocks(raw_text)
        cleaned = cleaned.strip()
        if _had_unclosed:
            print(
                f"  [THINK-RESCUE] 未闭合 <think>：原文 {len(raw_text)} 字 → 保留 {len(cleaned)} 字",
                flush=True,
            )
        # v323 诊断：模型只写 "<think>" 不闭合时，上面第二行会把**从 <think> 到文末**
        # 整段删掉（连 JSON 一起）→ 解析出空壳 → 空字段 → schema 判失败。
        # 这里只在"剥完几乎不剩东西"时打一行，用来确认这个猜测而不是长期刷日志。
        if "<think>" in raw_text and len(cleaned) < 80:
            print(
                f"  [PARSE-DEBUG] 原文 {len(raw_text)} 字 → 剥 think 后只剩 {len(cleaned)} 字"
                f" | 原文头: {raw_text[:100]!r}",
                flush=True,
            )
        # Parse JSON — handle markdown wrapping + embedded/truncated JSON
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)

        # v176.2：解析前先把 reasoning_path 内的旧段名硬替换为新段名
        # （LLM 训练惯性会写 [资产性质][因素排行] → 改写为 [总框架][因素定位]，
        #  让"LLM 凭印象写"自动合规；旧段名彻底从数据中消失）
        # 注：JSON 字符串内换行是 \\n，正则同时覆盖 \n 和 \\n。
        _OLD2NEW = (
            (r"\[资产性质\]", "[总框架]"),
            (r"\[资产性质 ?\]", "[总框架]"),
            (r"\[因素排行\]", "[因素定位]"),
            (r"\[主体识别\]", "[总框架]"),
            (r"\[主体识别 ?\]", "[总框架]"),
            # [主体] 单独出现歧义大，只在前面紧跟空白+换行时才替换（限定段首）
            (r"(?:^|\n)\[主体\]", "\\n[总框架]"),
        )
        cleaned = re.sub(r"\\n|\n", "\n", cleaned)  # 先把 \\n 转真 \n，再做替换
        for old, new in _OLD2NEW:
            cleaned = re.sub(old, new, cleaned)

        result: Dict[str, Any] = {}
        try:
            result = json.loads(cleaned)
            if not isinstance(result, dict):
                raise ValueError("not a dict")
        except (json.JSONDecodeError, ValueError):
            # --- Fallback 1: repair truncated JSON ---
            repaired = cleaned.strip()
            if repaired.startswith("{") and not repaired.endswith("}"):
                inner = repaired[1:].strip()
                chunks = [c.strip() for c in inner.split(",") if c.strip()]
                complete: List[str] = []
                for chunk in chunks:
                    try:
                        json.loads("{" + chunk + "}")
                        complete.append(chunk)
                    except json.JSONDecodeError:
                        pass  # truncated field — drop
                if complete:
                    repaired = "{" + ",".join(complete) + "}"
                    try:
                        result = json.loads(repaired)
                    except json.JSONDecodeError:
                        pass

            # --- Fallback 2: regex-extract JSON containing required keys ---
            if not result:
                m = re.search(
                    r'\{[^{}]*"sentiment_score"[^{}]*"suggested_action"[^{}]*"reasoning"[^{}]*\}',
                    cleaned, re.DOTALL,
                )
                if not m:
                    m = re.search(
                        r'\{[^{}]*"(?:sentiment_score|suggested_action|reasoning)"[^{}]*\}',
                        cleaned, re.DOTALL,
                    )
                if m:
                    try:
                        result = json.loads(m.group(0))
                    except json.JSONDecodeError:
                        frag = m.group(0).rstrip().rstrip(",") + "}"
                        try:
                            result = json.loads(frag)
                        except json.JSONDecodeError:
                            pass

            # --- Fallback 2.5 (v170 全网溯源补丁)：LLM 长推演模式下的中文抽取 ---
            # MiniMax-M3 等模型常输出 2000 字中文八段推演但**没有 JSON 包裹** → 上方
            # json.loads / Fallback 2 都拿不到结果 → 最终落 fallback3（中性观望）。
            # 但中文推演里其实常有可用的"X 日宣布/报道/发布"信号，可作为 cross_net_*
            # 的弱依据填进 result（前端能渲染"AMBIGUOUS"灰徽章而不是"未溯源"无徽章）。
            #
            # 设计要点：
            #   - 只在 result 已部分可用（不是 fallback3 兜底结果）时合并抽取结果
            #   - 抽取失败的字段留空串，不污染 DB
            #   - 时间必须能映射到 YYYY-MM-DD HH:MM:SS（用本机系统时间补全缺失部分）
            #   - 来源优先匹配"X 报道/宣布/发布/消息/通讯社/官网/新闻稿"等 14 个高信度模式
            #
            # v176.2 强化：主路径（LLM 给完整 JSON）也跑一次抽取。
            # 之前 0% 填充率是因为这个分支只在 fallback2（无 JSON）触发，主路径 LLM
            # 给了 JSON 但 cross_net_* 经常留空时，DB 就空着。
            # 现在用正则从新闻原文/标题里抽"X 时 X 分 X 报道/宣布/发布"，对金十/Yahoo/东财/财联社/finnhub/oilprice
            # 这些主渠道（之前 0% 填充率）补上 cross_net 弱信号。
            if not result.get("cross_net_first_seen_ts") and not result.get("net_origin_info"):
                _cn_ts_guess, _cn_src_guess, _noi_guess = _extract_cross_net_from_text(cleaned)
                if _cn_ts_guess or _cn_src_guess or _noi_guess:
                    result["cross_net_first_seen_ts"] = _cn_ts_guess
                    result["cross_net_source"] = _cn_src_guess
                    result["net_origin_info"] = _noi_guess

            # --- v176.2 二次回灌：news 原文 + reasoning 一起喂抽取器 ---
            # cleaned 主要是 LLM 输出，已不含原始 news text。需补一句：把原始 news text
            # 也送进抽取器，专门为金十/Yahoo/东财/oilprice/finnhub 这些"LLM 经常留空
            # cross_net_ 但原文里有'北京时间 21:30 路透社报道'等明文"的主渠道补弱信号。
            try:
                # v176.10 fix：原代码引用不存在的变量 `news`（本函数形参只有 news_content），
                # 导致 `'news' in dir()` 恒 False、`news.get("content")` 抛 NameError 被 except 吞掉，
                # 二次回灌分支从未执行 → 主路径 LLM 留空 cross_net_* 时引擎不回补原文弱信号。
                # 现在直接用形参 news_content（完整原文）+ news_source（由调用点传入）。
                _src_for_xnet = (news_source or "").strip()
                _text_for_xnet = news_content or ""
                if not result.get("cross_net_first_seen_ts") and _text_for_xnet:
                    # 主渠道：金十/Yahoo/东财/财联社/oilprice/finnhub/fxstreet/marketwatch/feishu_jin10
                    _MAIN_CHAN = ("jin10", "feishu_jin10", "yahoo_finance", "东方财富", "finnhub",
                                  "oilprice", "fxstreet", "marketwatch", "cointelegraph",
                                  "TechFlow", "techflow")
                    if any(ch in _src_for_xnet for ch in _MAIN_CHAN):
                        _cn_ts2, _cn_src2, _noi2 = _extract_cross_net_from_text(_text_for_xnet)
                        if _cn_ts2 or _cn_src2 or _noi2:
                            # v188-fix: 当 regex 抽到 cn_ts 但抽不到 cn_src 时，用
                            # news_source 作 cn_src 兜底。否则 _validate_cross_net_ts 第⑤层
                            # （source ≥4 字符）会拒掉 cn_ts，导致 DB 仍留空。
                            if _cn_ts2 and not _cn_src2:
                                _cn_src2 = (news_source or "").strip() or "聚合平台"
                            result["cross_net_first_seen_ts"] = result.get("cross_net_first_seen_ts") or _cn_ts2
                            result["cross_net_source"] = result.get("cross_net_source") or _cn_src2
                            result["net_origin_info"] = result.get("net_origin_info") or _noi2
            except Exception:
                pass  # 静默失败，不影响主路径

            # --- v188 Tier-3 平台首发兜底 ---
            # 前两层都失败（LLM 没填、regex 也没抽到），但 source 是已知聚合器 → 用
            # news.published_at 作"平台首发"时间弱信号。比"未溯源"无徽章强，前端可
            # 显示"聚合器首发"灰徽章而非空白。校验只在 source ≥4 字符 + 时间合法
            # 时写入（_validate_cross_net_ts 第④层会拒绝晚于 first_seen_ts 的值）。
            # v188-fix: 二次回灌只填 cn_ts 但不填 cn_src 时（典型：金十"X月X日X时X讯"
            # 抽到时间但没有"X 报道/宣布"匹配），Tier-3 跳过条件变 False（cn_ts 非空），
            # 但 cn_src 仍空 → _validate_cross_net_ts 第⑤层拒掉 → DB 留空。
            # 修复后二次回灌已给 cn_src 兜底，但仍有部分场景二次回灌完全不触发
            # （如 source 不在 _MAIN_CHAN 内、或 LLM 已正确填了 cn_ts）。
            # 此 Tier-3 保留作为最后一道兜底，仅在 result 完全空时触发。
            # v188-fix: Tier-3 触发条件——结构化两字段都为"可写入"时才认为已溯源。
            # 之前用 `not result.get("cross_net_source")` 不够：regex 可能捕到短串
            # （如 "新闻" 2 字符），让 cn_source 非空 → Tier-3 跳过 → 但
            # _validate_cross_net_ts 第⑤层（source ≥4 字符）会拒掉 → DB 仍留空。
            # 改进：cn_source 长度 <4 也视为未溯源，Tier-3 接管。
            _existing_cn_src = (str(result.get("cross_net_source") or "").strip())
            if (not result.get("cross_net_first_seen_ts")
                    and (not _existing_cn_src or len(_existing_cn_src) < 4)
                    and news_published_at):
                try:
                    _ts_m = re.match(
                        r"^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})(?::(\d{2}))?",
                        str(news_published_at),
                    )
                    if _ts_m:
                        _valid_pub = (
                            f"{_ts_m.group(1)}-{_ts_m.group(2)}-{_ts_m.group(3)} "
                            f"{_ts_m.group(4)}:{_ts_m.group(5)}:00"
                        )
                        _src_label = (news_source or "").strip()
                        result["cross_net_first_seen_ts"] = _valid_pub
                        result["cross_net_source"] = (
                            f"{_src_label} 平台首发" if len(_src_label) >= 2 else "聚合平台首发"
                        )
                        result["net_origin_info"] = (
                            f"由 {_src_label or '聚合器'} 于 {_valid_pub} 平台首发"
                        )
                except Exception as exc:
                    pass  # Tier-3 静默失败（不影响主路径）

            # --- Fallback 3: keyword-based heuristics ---
            # 关键词推断不是分析：它没有做全文/八段推演，产出 BUY/SELL 只会制造
            # “没分析却给方向”的伪结论（实测：『取消出售计划』因命中 sell/sale 被误判
            # 成偏空）。这里统一降级为中性观望、不下任何方向——方向判定完全交给真实 LLM 分析。
            #
            # v170 全网溯源补丁：fallback3 也要带 cross_net_* / net_origin_info 三个字段占位。
            # 不写会让 sanitize 默认空串 + DB 存空 → 前端无法区分"AI 看了拒答"与"LLM 没填"。
            # 落空串是规范上的"拒绝回答"语义，frontend 4 档徽章会渲染为"未溯源"而非崩页。
            if not result:
                result = {
                    "reasoning_path": "关键词推断 → 中性观望（未做全文分析，不下方向）",
                    "sentiment_score": 0.0,
                    "suggested_action": "HOLD",
                    "reasoning": "关键词推断:无明确方向(未做全文分析)",
                    "market_category": "",
                    "target_asset": "NONE",
                    "event_strength": "low",
                    "direct_catalyst": False,
                    "timeframe_match": "intraday",
                    # v170 全网溯源 — fallback 阶段无任何信号可据，标准三态全空（=拒绝回答）
                    "cross_net_first_seen_ts": "",
                    "cross_net_source": "",
                    "net_origin_info": "",
                }

        # ── 元数据字段默认值 (LLM 可能不返回或返回无效值) ──
        _META_DEFAULTS: Dict[str, Any] = {
            "prediction_type":        ("reversal", "continuation", "breakout"),
            "event_phase":            ("early", "mid", "late"),
            "market_confirmation":    ("positive", "negative", "unknown"),
            "expected_horizon":       ("intraday", "1-3d", "1w+"),
            "invalidation_condition": "",
            # Phase 2: Event Quality Layer
            "event_strength":         ("low", "medium", "high"),
            "timeframe_match":        ("intraday", "swing", "macro"),
        }
        for key, valid in _META_DEFAULTS.items():
            if key not in result or not isinstance(result[key], str):
                # If valid is a tuple, take the last (most conservative) value; if str, use empty
                result[key] = valid[-1] if isinstance(valid, tuple) else valid
            elif isinstance(valid, tuple):
                val_lower = result[key].strip().lower()
                # Check if the value matches any valid option (fuzzy)
                ok = False
                for v in valid:
                    if v in val_lower or val_lower == v:
                        result[key] = v  # normalize to canonical form
                        ok = True
                        break
                if not ok:
                    result[key] = valid[-1]  # default conservative

        # direct_catalyst 布尔值特殊处理 (LLM 可能返回 JSON true/false 或字符串)
        dc = result.get("direct_catalyst")
        if isinstance(dc, bool):
            pass  # already correct
        elif isinstance(dc, str) and dc.strip().lower() in ("true", "1", "yes"):
            result["direct_catalyst"] = True
        else:
            result["direct_catalyst"] = False

        # ── 影响评估数值字段 (0-10，LLM 可能不返回/越界 → clamp 兜底) ──
        for numeric_key, default in (("heat_score", 5.0), ("impact_score", 5.0), ("asset_impact", 5.0)):
            raw = result.get(numeric_key, default)
            try:
                val = float(raw)
            except (TypeError, ValueError):
                val = default
            result[numeric_key] = max(0.0, min(10.0, val))

        # ── 整箱白名单过滤：核心字段在此统一兜底，保证返回即三键齐全 ──
        # v176：先 sanitize 通用白名单，再 presanitize 把 schema 必填字段强制达标
        # v176.9：传 cleaned 给 presanitize，用于全空兜底时从原文末尾抽 [总结]
        # v1 重构：sanitize 后消费 _unclassified_* 标记（仅在主入口有；这里是 fallback 链 news_id=0）
        result = _sanitize_llm_result(result)
        # v323：这里原本是 `_consume_unclassified_marker(result, news_id=0)` —— 兜底链拿不到
        # news_id，就用 0 写一条"人工复核"记录。实测 10 分钟写 394 条、全表 news_id=0 的记录
        # 已上万条：① 对定位毫无用处；② 每写一次就要抢一次写锁，反过来加剧 database is locked；
        # ③ 这些记录永远不会被"补全"，只是把表撑大。兜底结果本身已被 _presanitize_for_schema
        # 兜成三键齐全，不需要再留档。
        result = _presanitize_for_schema(result, cleaned)
        return result

    # ==================================================================
    # Main call flow
    # ==================================================================
    use_json_mode = model_cfg.get("json_mode", True)

    raw_text = _do_api_call(use_json_mode)
    result = _parse_result(raw_text)

    # v342: 空输出补一次（只一次）—— 历史上这里直接落到降级兜底，
    # 于是"调用成功但决策是兜底"这件事在库里完全看不出来。
    # 只在**真没内容**时重试，正常 HOLD 的短 JSON 不会触发。
    # v342: 原来只判"原文为空"，漏掉了"原文不短、但正文是骨架"这一类
    # （tokens_out=33 的那种）。骨架同样要补一次，否则后面只能记降级。
    if ((is_empty_llm_output(raw_text) or is_skeleton_result(result))
            and model_cfg.get("label") != "Doubao"):
        try:
            print(f"  [{model_cfg['label']}] 输出为空（{len(str(raw_text or ''))} 字）→ 先走精简两步",
                  flush=True)
            # v346：全量 prompt 上模型常"只思考不落笔"，再补一次全量大概率还是空。
            # 先用短 prompt 锁标的（~2K），再单独要八段（~6K）——实测成功率远高于全量重试，
            # 且成本低于一次全量调用。成功则直接跳过下面的全量重试。
            _lean_ok = False
            if str(os.getenv("TRIDENT_TWO_STEP_LEANPROMPT", "1")).strip() != "0":
                try:
                    _la = two_step_llm_call(
                        model_cfg, mode="a", news_text=str(news_content or ""),
                        gate_context=str(gate_context or ""),
                        timestamp=str(news_timestamp or ""),
                        timeout=float(model_cfg.get("timeout") or 45.0),
                        call_site="lean_recover_a",
                    )
                    _ra = _parse_result(_la) if _la else {}
                    _ta_l = str(_ra.get("target_asset") or "").strip().upper()
                    if _ta_l and _ta_l not in ("NONE", "OTHER"):
                        _act_l = str(_ra.get("suggested_action") or "HOLD").upper()
                        _lb = two_step_llm_call(
                            model_cfg, mode="b", news_text=str(news_content or ""),
                            gate_context=str(gate_context or ""),
                            timestamp=str(news_timestamp or ""),
                            asset=_ta_l, action=_act_l,
                            timeout=float(model_cfg.get("timeout") or 60.0),
                            call_site="lean_recover_b",
                        )
                        _rb = _parse_result(_lb) if _lb else {}
                        _merged, _nts = merge_two_step(_ra, _ra, _rb)
                        if not is_skeleton_result(_merged):
                            result = _merged
                            _lean_ok = True
                            print(f"  [{model_cfg['label']}] ★ 精简两步救回：{_ta_l}/{_act_l} "
                                  f"({', '.join(_nts[:5])})", flush=True)
                except Exception as _lean_exc:
                    print(f"  [{model_cfg['label']}] 精简两步失败: {type(_lean_exc).__name__}",
                          flush=True)
            if _lean_ok:
                # 已在精简两步拿到可用结果 → 跳过昂贵的全量重试，但**不 return**，
                # 后面的 schema 补全 / 降级记账 / 落库链路照常执行。
                raw_text = "<lean-two-step>"
                print(f"  [{model_cfg['label']}] 已由精简两步补齐，跳过多余全量重试",
                      flush=True)
            else:
                # v398h：这一步原来"先裸重试、再预填重试"，最坏情况同一条新闻
                # 要再发两遍全量 prompt（每遍 ~10K）。预填 "{" 已是实测最可靠的
                # 抢救姿势（见下方注释），直接把预填提到第一遍，省掉一次全量调用。
                print(f"  [{model_cfg['label']}] 精简两步未救回 → 回退全量重试(预填)", flush=True)
                _raw_retry = _do_api_call(False, prefill="{")
                _res_retry = _parse_result(_raw_retry) if _raw_retry else {}
                if (not is_empty_llm_output(_raw_retry)
                        and not is_skeleton_result(_res_retry)):
                    raw_text = _raw_retry
                    result = _res_retry
                else:
                    # v398h：原来这里还有"第三遍：预填 '{{'"，与上面刚改成预填的
                    # 那一遍重复。同一条新闻连发三遍全量 prompt（~30K）只为抢一段
                    # 正文，收益远小于成本 —— 两遍没救回就直接落兜底记账。
                    print(f"  [{model_cfg['label']}] 预填重试仍无正文，保留兜底", flush=True)
        except Exception as _empty_exc:
            print(f"  [{model_cfg['label']}] 空输出重试失败: {type(_empty_exc).__name__}",
                  flush=True)
            try:
                from common import llm_degrade_probe as _ldp_e
                _ldp_e.note_retry_failed(type(_empty_exc).__name__)
            except Exception:
                pass

    # Retry: if strict JSON mode fell through to keyword heuristics, try prompt mode
    if (use_json_mode
            and model_cfg.get("label") != "Doubao"
            and result.get("reasoning_path", "").startswith("关键词推断")
            and len(raw_text) > 10):
        print(f"  [{model_cfg['label']}] json_mode=True → keyword, retrying prompt-mode...", flush=True)
        try:
            raw_text2 = _do_api_call(False)
            result2 = _parse_result(raw_text2)
            if not result2.get("reasoning_path", "").startswith("关键词推断"):
                result = result2
                print(f"  [{model_cfg['label']}] retry OK (prompt-JSON)", flush=True)
        except Exception:
            pass  # Keep original keyword result if retry fails

    # ── 八段格式强制：reasoning_path 八段不完整（AI 自由发挥/漏段），
    #    或"八段空骨架"（段名都在但段间无实质内容）→ 带纠正提示重试 1 次 ──
    if (model_cfg.get("label") != "Doubao"
            and not str(result.get("reasoning_path", "")).startswith("关键词推断")
            and (not _six_stage_complete(str(result.get("reasoning_path", "")))
                 or _is_eight_stage_skeleton(str(result.get("reasoning_path", ""))))):
        print(f"  [{model_cfg['label']}] reasoning_path 八段不完整, retrying with format fix...", flush=True)
        try:
            raw_text_fix = _do_api_call(use_json_mode, retry_hint=_SIX_STAGE_RETRY_HINT)
            result_fix = _parse_result(raw_text_fix)
            path_fix = str(result_fix.get("reasoning_path", ""))
            if _six_stage_complete(path_fix) and not path_fix.startswith("关键词推断"):
                result = result_fix
                print(f"  [{model_cfg['label']}] format-fix retry OK", flush=True)
            else:
                print(f"  [{model_cfg['label']}] format-fix retry still incomplete, keep original", flush=True)
        except Exception as exc:
            print(f"  [{model_cfg['label']}] format-fix retry failed: {type(exc).__name__}", flush=True)

    # ── P0-2：schema 必填字段补全 retry（data_anchor_ts / hard_data_evidence /
    # elasticity_window / verification 任何一个缺失 → 带 hint 重试 1 次）
    # 这是"LLM 必填字段 0% 填全"问题的根因修复：prompt 已前置+示意，但 LLM 仍可能忘
    # 就单独问一次补全，避免整条新闻被降级为 HOLD。
    # v398e：market_category 为空会被 JSON Schema 判非法（枚举不含 ''），
    # 每次校验失败都会连带触发一轮补全；它的取值其实由 target_asset 决定，
    # 本地就能定，不必问模型。hard_data_evidence 写 "N/A" 又会撞 "too short"，
    # 统一补成一句可读的占位说明。
    try:
        if not str(result.get("market_category") or "").strip():
            _ta = str(result.get("target_asset") or "").upper()
            if _ta in ("BTC", "ETH", "SOL", "BNB", "XRP", "DOGE"):
                result["market_category"] = "CRYPTO"
            elif _ta in ("XAU", "GOLD", "XAG", "GLD"):
                result["market_category"] = "GOLD"
            elif _ta in ("WTI", "BRENT", "OIL", "USO"):
                result["market_category"] = "OIL"
            elif _ta in ("", "NONE"):
                result["market_category"] = "MACRO"
            else:
                result["market_category"] = "STOCK"
        _hde = str(result.get("hard_data_evidence") or "").strip()
        if not _hde or _hde.upper() == "N/A":
            result["hard_data_evidence"] = (
                "N/A | 本条无可用硬数据快照（行情源当时连接失败，未引用任何数值）")
    except Exception:
        pass
    _missing_required = []
    # v398d：能由服务端事实确定的两个字段先本地填，别让它们再去触发一次 LLM 补全。
    #   · data_anchor_ts = 本条新闻的时间（事实，不是模型输出）
    #   · hard_data_evidence = 行情硬数据（v397 只在落库前补，这里提前到缺口计算之前）
    try:
        if not str(result.get("data_anchor_ts") or "").strip() and str(news_timestamp or "").strip():
            result["data_anchor_ts"] = str(news_timestamp)[:19]
        if not str(result.get("hard_data_evidence") or "").strip():
            _ev_pre = _hard_data_block(str(result.get("target_asset") or ""))
            if _ev_pre:
                result["hard_data_evidence"] = _ev_pre[:500]
    except Exception:
        pass
    # v435 P0-2：结构字段先本地推导（描述/分类类字段不再发请求去补），
    # 只把「真判断字段」的缺口交给后面的 schema 抢救链。
    try:
        _af = _autofill_soft_fields(result)
        if _af:
            print(f"  [{model_cfg['label']}] 结构字段本地补全 {len(_af)} 个（零请求）：{_af}",
                  flush=True)
    except Exception:
        pass

    # v342: target_asset 也是必填 —— 它缺失时下游只能写 NONE，
    # 于是"没有标的"被当成"没有方向"记账（实测 191/272 条降级全是 NONE 起步）。
    # 把它算进必填缺口，让既有的 schema 补全 retry 真正触发。
    if not str(result.get("target_asset", "") or "").strip() \
            or str(result.get("target_asset", "") or "").strip().upper() in ("NONE", "OTHER"):
        _missing_required.append("target_asset")
    # v342: 线上实际报的 5 错里，前三错是「asset_nature 空 / factor_ranking 空 /
    # reasoning 空」—— 它们不算进必填缺口时，schema 补全 retry 永远不会触发，
    # 结果只能落降级兜底（实测 70% 决策这么来的）。这里把它们一并计入。
    if len(str(result.get("asset_nature", "") or "").strip()) < 4:
        _missing_required.append("asset_nature")
    if not (isinstance(result.get("factor_ranking"), list) and result.get("factor_ranking")):
        _missing_required.append("factor_ranking")
    if len(str(result.get("reasoning", "") or "").strip()) < 10:
        _missing_required.append("reasoning")
    if not str(result.get("data_anchor_ts", "") or "").strip():
        _missing_required.append("data_anchor_ts")
    if not str(result.get("hard_data_evidence", "") or "").strip():
        _missing_required.append("hard_data_evidence")
    _ew = result.get("elasticity_window")
    if not _ew or (isinstance(_ew, str) and not _ew.strip()):
        _missing_required.append("elasticity_window")
    if not str(result.get("verification", "") or "").strip():
        _missing_required.append("verification")
    # v181 结构化产出 3 判断项：任一缺失即视为必填缺口（准优先）
    if not str(result.get("asset_classification", "") or "").strip():
        _missing_required.append("asset_classification")
    if not str(result.get("fund_driver", "") or "").strip():
        _missing_required.append("fund_driver")
    if not str(result.get("consensus_vs_us", "") or "").strip():
        _missing_required.append("consensus_vs_us")
    _struct_missing = {"asset_classification", "fund_driver", "consensus_vs_us"} & set(_missing_required)
    _slow = _is_slow_channel(model_cfg)
    _allow_slow = str(os.getenv("TRIDENT_ALLOW_SLOW_SCHEMA_RETRY", "1")).strip() != "0"
    # v396.2：先判"这条新闻天生没有交易池标的"（模型如实 NONE + 无分类 + 全文无池关键词）。
    # 命中后**不再**做 schema 补全 / 两步抢救 / 落库前抢救 —— 抢救不可能凭空造出标的，
    # 只是每条多烧 3-5 次模型调用（实测单条 1-2 分钟、一批 8 条拖到 20 分钟）。
    # 用环境变量可关：NEWS_NONCORE_FASTPATH=0 恢复旧行为，便于对比。
    _noncore_fast = (str(os.getenv("NEWS_NONCORE_FASTPATH", "1")).strip() != "0"
                     and declares_non_core_news(result, str(news_content or "")))
    if _noncore_fast:
        result["_declared_non_core"] = True
    _do_schema_retry = (not _noncore_fast) and should_schema_retry(
        _slow, _missing_required, _allow_slow)
    if _noncore_fast:
        print(f"  [{model_cfg['label']}] 非核心新闻（无池内标的）：跳过 schema 补全 / 两步抢救 "
              f"| news={str(news_content or '')[:40]!r}", flush=True)
    if (model_cfg.get("label") != "Doubao"
            and _do_schema_retry
            and (len(_missing_required) >= 2 or bool(_struct_missing))
            and not str(result.get("reasoning_path", "")).startswith("关键词推断")):
        print(f"  [{model_cfg['label']}] 缺 P0-2 必填字段 {_missing_required}, retrying"
              f"{'（慢通道抢救）' if _slow else ''}...", flush=True)
        try:
            # v398d：先走精简补全（≈2K prompt，3-6s）。原来的做法是重跑一次
            # 17K system + 11K user 的全量 prompt，只为补几个字符串字段 ——
            # 单条新闻因此多花 45s + 13K token，一批 8 条就是 10 分钟起步。
            _lean_txt = _lean_schema_fill(
                _missing_required, str(news_content or ""), result)
            result_schema = _parse_result(_lean_txt) if str(_lean_txt or "").strip() else {}
            _filled = 0
            for k in _missing_required:
                v = result_schema.get(k)
                if v is not None and (not isinstance(v, str) or v.strip()):
                    result[k] = v
                    _filled += 1
            if _filled > 0:
                print(f"  [{model_cfg['label']}] schema 精简补全 {_filled}/"
                      f"{len(_missing_required)} 字段（省一次全量 retry）", flush=True)
            if _filled < len(_missing_required):
                # 精简补全没补齐 → 退回原来的全量 retry，质量不打折
                raw_text_schema = _do_api_call(
                    use_json_mode, retry_hint=_SCHEMA_REQUIRED_FIELDS_RETRY_HINT)
                result_schema = _parse_result(raw_text_schema)
                _filled2 = 0
                for k in _missing_required:
                    if str(result.get(k) or "").strip():
                        continue
                    v = result_schema.get(k)
                    if v is not None and (not isinstance(v, str) or v.strip()):
                        result[k] = v
                        _filled2 += 1
                print(f"  [{model_cfg['label']}] schema retry 补全 "
                      f"{_filled2}/{len(_missing_required) - _filled} 字段", flush=True)
        except Exception as _exc:
            print(f"  [{model_cfg['label']}] schema retry failed: {type(_exc).__name__}", flush=True)

    # ── v342 两步抢救：先锁标的与方向，再单独要八段 ──────────────────────────
    # 为什么拆两步：一次调用要模型同时干"选标的 + 写八段 + 填 8 个证据字段"时，
    # 实测它会交回 skeleton（asset_nature 空 / factor_ranking 空 / reasoning 空），
    # 重试也一样。拆开后第一步输出极短、几乎不会空；锁住标的再要八段，命中率高得多。
    try:
        _ta_now = str(result.get("target_asset") or "").strip().upper()
        _need_two_step = (
            (not result.get("_declared_non_core"))
            and ((_ta_now in ("", "NONE", "OTHER")) or (len(_missing_required) >= 4))
        )
        _two_step_on = str(os.getenv("TRIDENT_TWO_STEP_RESCUE", "1")).strip() != "0"
        # v398：NONE / 空标的 走独立预算（默认 3 条/10 分钟），其余仍用原 10 条窗口。
        # 另外：原实现无论是否需要抢救都会先扣一次预算（rescue_budget_ok 内部自增），
        # 一批 16 条里有半数根本不需要抢救，却把窗口白吃掉了 → 现在只在真需要时才扣。
        _is_none_target = is_none_target(_ta_now)
        _win_state, _win_limit = rescue_budget_state_for(_ta_now)
        _budget_ok = False
        if _need_two_step:
            _budget_ok = rescue_budget_ok(time.time(), _win_state, limit=_win_limit)
        if _need_two_step and _two_step_on and not _budget_ok and _is_none_target:
            print(f"  [{model_cfg['label']}] 两步抢救跳过（NONE 预算已用 "
                  f"{_RESCUE_NONE_STATE['used']}/{_RESCUE_NONE_LIMIT_PER_WINDOW}，"
                  f"标的={_ta_now or '空'}）", flush=True)
        if _need_two_step and _two_step_on and _budget_ok and model_cfg.get("label") != "Doubao":
            print(f"  [{model_cfg['label']}] 进入两步抢救（标的={_ta_now or '空'}，缺口 {len(_missing_required)}，"
                  f"窗口已用 {_win_state['used']}/{_win_limit}）",
                  flush=True)
            # v346：优先走精简两步（短 prompt 稳定出正文，成本约为全量的 1/3）
            _lean_two = str(os.getenv("TRIDENT_TWO_STEP_LEANPROMPT", "1")).strip() != "0"
            if _lean_two:
                _raw_a = two_step_llm_call(
                    model_cfg, mode="a", news_text=str(news_content or ""),
                    gate_context=str(gate_context or ""),
                    timestamp=str(news_timestamp or ""),
                    timeout=float(model_cfg.get("timeout") or 45.0),
                    call_site="two_step_a",
                )
            else:
                _raw_a = _do_api_call(use_json_mode, retry_hint=_TWO_STEP_A_HINT)
            _res_a = _parse_result(_raw_a)
            _ta_a = str(_res_a.get("target_asset") or "").strip().upper()
            if _ta_a and _ta_a not in ("NONE", "OTHER"):
                _act = str(_res_a.get("suggested_action") or "HOLD").upper()
                print(f"  [{model_cfg['label']}] 两步抢救① 锁住标的 {_ta_a} / {_act}", flush=True)
                if _lean_two:
                    _raw_b = two_step_llm_call(
                        model_cfg, mode="b", news_text=str(news_content or ""),
                        gate_context=str(gate_context or ""),
                        timestamp=str(news_timestamp or ""),
                        asset=_ta_a, action=_act,
                        timeout=float(model_cfg.get("timeout") or 60.0),
                        call_site="two_step_b",
                    )
                else:
                    _raw_b = _do_api_call(
                        use_json_mode,
                        retry_hint=_TWO_STEP_B_HINT_TMPL.format(asset=_ta_a, action=_act),
                    )
                _res_b = _parse_result(_raw_b)
                result, _notes = merge_two_step(result, _res_a, _res_b)
                print(f"  [{model_cfg['label']}] 两步抢救② 合并 {len(_notes)} 项: "
                      f"{', '.join(_notes[:6])}", flush=True)
                try:
                    rerender_degrade_text_for_asset(result, _ta_a)
                except Exception:
                    pass
            else:
                print(f"  [{model_cfg['label']}] 两步抢救① 仍锁不住标的，保留原结果", flush=True)
    except Exception as _ex_ts:
        print(f"  [{model_cfg['label']}] 两步抢救失败: {type(_ex_ts).__name__}: {str(_ex_ts)[:80]}",
              flush=True)

    # ── [总结] 段强制：用户要求"有关系就要有分析 + 总结"→ 缺总结段或没按品种分行时重试 1 次 ──
    # 实测背景：原先"总结"要求只写在[因果链命中]块里、且要求补进会被截断到 80 字的
    # reasoning 字段 → 300 条样本里含"总结"的 0 条。现在按 [总结] 段校验。
    try:
        _rp_sum = str(result.get("reasoning_path", "") or "")
        _ta_sum = str(result.get("target_asset", "") or "").strip().upper()
    except Exception:
        _rp_sum, _ta_sum = "", ""
    _need_summary = bool(_rp_sum) and not _rp_sum.startswith("关键词推断")
    if _need_summary and _ta_sum and _ta_sum not in ("NONE", "OTHER"):
        _need_summary = not _has_summary_for(_rp_sum, _ta_sum)
    elif _need_summary:
        _need_summary = not _summary_text(_rp_sum)
    if (model_cfg.get("label") != "Doubao"
            and not _is_slow_channel(model_cfg)
            and _need_summary):
        # v422（2026-11-17）：按方向分流。
        # 实测 11-16：504 条决策里 499 条 HOLD、5 条真方向 —— 也就是说这条
        # "缺总结 → 全量重试（13.5K token）"几乎全花在**观望**结论上。
        # 现在：HOLD/NONE 走 ~300 token 的精简通道只补 [总结] 段再拼回去；
        # BUY/SELL 仍走原来那次全量重试（真下单要完整推演）。
        _act_sum = str(result.get("suggested_action") or "").upper()
        _tradeable_sum = _act_sum in ("BUY", "SELL") and _ta_sum not in ("NONE", "OTHER")
        if not _tradeable_sum:
            try:
                _txt = two_step_llm_call(
                    model_cfg, mode="summary",
                    news_text=str(news_content or "")[:1200],
                    asset=_ta_sum or "NONE", action=_act_sum or "HOLD",
                    timeout=float(model_cfg.get("timeout") or 45.0),
                    call_site="summary_lean",
                )
                _sm = str(_parse_result_light(_txt).get("summary") or "").strip()
                if _sm:
                    result["reasoning_path"] = (
                        str(result.get("reasoning_path") or "").rstrip()
                        + "\n[总结] " + _sm
                    )
                    print(f"  [{model_cfg['label']}] 精简补总结 OK（{_act_sum or 'NONE'} 路径，"
                          f"省一次全量 13.5K）", flush=True)
                    _signal_trace("summary", verdict="lean补成", asset=_ta_sum,
                                  path_len=len(_sm))
                else:
                    print(f"  [{model_cfg['label']}] 精简补总结未出内容, keep original",
                          flush=True)
            except Exception as exc:
                print(f"  [{model_cfg['label']}] 精简补总结失败: "
                      f"{type(exc).__name__}: {str(exc)[:60]}", flush=True)
        else:
            print(f"  [{model_cfg['label']}] 缺 [总结] 段（{_act_sum} 方向单）, "
                  f"retrying with summary hint...", flush=True)
            try:
                raw_text_s = _do_api_call(use_json_mode, retry_hint=_SUMMARY_RETRY_HINT)
                result_s = _parse_result(raw_text_s)
                path_s = str(result_s.get("reasoning_path", "") or "")
                ta_s = str(result_s.get("target_asset", "") or "").strip().upper()
                ok_s = bool(path_s) and not path_s.startswith("关键词推断") and (
                    _has_summary_for(path_s, ta_s) if ta_s and ta_s not in ("NONE", "OTHER")
                    else bool(_summary_text(path_s))
                )
                if ok_s:
                    result = result_s
                    print(f"  [{model_cfg['label']}] summary retry OK", flush=True)
                else:
                    print(f"  [{model_cfg['label']}] summary retry 未改善, keep original", flush=True)
                    # 留痕：部署后用它评估"总结要求"的真实达成率，
                    # 再决定要不要把"缺总结"升级成不给方向单（目前只重试、不拦交易）。
                    _signal_trace("summary", verdict="retry未改善", asset=ta_s or _ta_sum,
                                  path_len=len(path_s or _rp_sum))
            except Exception as exc:
                print(f"  [{model_cfg['label']}] summary retry failed: {type(exc).__name__}", flush=True)

    # ── 方向一致性强制：八段推演方向与 suggested_action 相反 → 带纠正提示重试 1 次 ──
    #    按 target_asset 判定（该品种自己的专属行），多品种混合文本不会互相抵消。
    action_now = str(result.get("suggested_action", "")).upper()
    target_now = str(result.get("target_asset", "") or "").strip().upper()
    if (model_cfg.get("label") != "Doubao"
            and action_now in ("BUY", "SELL")
            and _direction_conflict(str(result.get("reasoning_path", "")), action_now,
                                    target_now)):
        print(f"  [{model_cfg['label']}] 推演方向与 action 相反, retrying with consistency fix...", flush=True)
        try:
            raw_text_fix = _do_api_call(use_json_mode, retry_hint=_DIRECTION_RETRY_HINT)
            result_fix = _parse_result(raw_text_fix)
            action_fix = str(result_fix.get("suggested_action", "")).upper()
            path_fix = str(result_fix.get("reasoning_path", ""))
            target_fix = str(result_fix.get("target_asset", "") or "").strip().upper()
            if (action_fix in ("BUY", "SELL")
                    and not _direction_conflict(path_fix, action_fix, target_fix)
                    and not path_fix.startswith("关键词推断")):
                result = result_fix
                print(f"  [{model_cfg['label']}] consistency-fix retry OK", flush=True)
            else:
                print(f"  [{model_cfg['label']}] consistency-fix retry still conflicting, keep original", flush=True)
        except Exception as exc:
            print(f"  [{model_cfg['label']}] consistency-fix retry failed: {type(exc).__name__}", flush=True)

    # ── 副品种逐品种覆盖强制：sibling 八段里缺逐品种行 → 带纠正提示重试 1 次 ──
    # 规则来源（用户明确诉求 2+1）：列进副品种就必须有它自己的完整八段；
    # 说不出独立分析就不该列进来。提示里给了两条出路：补写逐段行（并说明无数据原因），
    # 或干脆把它从 sibling_decisions 删掉（宁缺毋滥）。
    # 校验按【段】判（用户要求"每一段都要有它的行"），不只判"整篇有没有提过"。
    # 慢通道（MiniMax）不重试；快通道都重试（此前误用 use_json_mode 导致永不触发）。
    if model_cfg.get("label") != "Doubao" and not _is_slow_channel(model_cfg):
        _missing_map = _siblings_missing_stages(result)
        if not _missing_map:
            # 退化兜底：整篇都没有该品种行的情况（段名解析失败时 _missing_map 可能是空的）
            _flat = _siblings_missing_in_stages(result)
            if _flat:
                _missing_map = {c: list(_COVERAGE_STAGES) for c in _flat}
        if _missing_map:
            _desc = "；".join(
                f"{c} 缺 {'/'.join(stages)}" for c, stages in _missing_map.items()
            )
            # 提示语带上具体缺哪些段，让模型有的放矢
            _hint = _PER_ASSET_RETRY_HINT + (
                "\n本次具体缺口：" + _desc + "。请针对这些品种、这些段补写逐品种行。"
            )
            print(
                f"  [{model_cfg['label']}] 副品种逐段覆盖不足（{_desc}）, "
                "retrying with per-asset hint...",
                flush=True,
            )
            try:
                raw_text_p = _do_api_call(use_json_mode, retry_hint=_hint)
                result_p = _parse_result(raw_text_p)
                if not str(result_p.get("reasoning_path", "")).startswith("关键词推断"):
                    _after_map = _siblings_missing_stages(result_p)
                    # 接受条件：总缺口段数减少（补齐了，或把说不出的品种从 sibling 里删了）
                    _before_n = sum(len(v) for v in _missing_map.values())
                    _after_n = sum(len(v) for v in _after_map.values())
                    if _after_n < _before_n:
                        result = result_p
                        print(
                            f"  [{model_cfg['label']}] per-asset retry OK"
                            f"（缺口 {_before_n} 段 → {_after_n} 段）",
                            flush=True,
                        )
                    else:
                        print(
                            f"  [{model_cfg['label']}] per-asset retry 未改善"
                            f"（缺口仍 {_after_n} 段）, keep original",
                            flush=True,
                        )
            except Exception as exc:
                print(f"  [{model_cfg['label']}] per-asset retry failed: {type(exc).__name__}", flush=True)

    # ── 主品种分析针对性强制（A）：判了主品种却没分析它 → 带提示重试 1 次 ──
    # 实测跑偏案例：主品种判 XAU，八段却通篇讲"中亚峰会/区域合作"，零黄金分析，
    # 还据此出了 BUY —— 分析无针对性等于给下游一个没有依据的信号。
    # 判定：八段里"提及主品种"的段数 < 2（宽松计数，含中文别名），且主品种是可交易品种。
    _focus_retried = False
    if model_cfg.get("label") != "Doubao" and not _is_slow_channel(model_cfg):
        try:
            _ta_now = str(result.get("target_asset") or "").strip().upper()
            _rp = str(result.get("reasoning_path", "") or "")
            if _ta_now and _ta_now not in ("NONE", "OTHER") and _rp and not _rp.startswith("关键词推断"):
                _hits = _asset_mentioned_stage_count(_rp, _ta_now)
                if _hits < 2:
                    print(
                        f"  [{model_cfg['label']}] 主品种 {_ta_now} 八段仅提及 {_hits} 段（分析跑偏）, "
                        "retrying with focus hint...",
                        flush=True,
                    )
                    _focus_retried = True
                    raw_text_f = _do_api_call(use_json_mode, retry_hint=_MAIN_FOCUS_RETRY_HINT)
                    result_f = _parse_result(raw_text_f)
                    _rp_f = str(result_f.get("reasoning_path", "") or "")
                    _ta_f = str(result_f.get("target_asset") or "").strip().upper()
                    if not _rp_f.startswith("关键词推断") and _rp_f:
                        _hits_f = (
                            _asset_mentioned_stage_count(_rp_f, _ta_f)
                            if _ta_f and _ta_f not in ("NONE", "OTHER")
                            else 99  # 改成 NONE/HOLD 也算"解决了跑偏"
                        )
                        if _hits_f > _hits:
                            result = result_f
                            print(
                                f"  [{model_cfg['label']}] focus retry OK"
                                f"（提及 {_hits} 段 → {_hits_f if _hits_f != 99 else 'NONE/HOLD'}）",
                                flush=True,
                            )
                        else:
                            print(f"  [{model_cfg['label']}] focus retry 未改善, keep original", flush=True)
        except Exception as exc:
            print(f"  [{model_cfg['label']}] focus retry failed: {type(exc).__name__}", flush=True)

    # ── 主品种一致性归一：target_asset 为唯一权威，对齐 asset_reasoning / sibling ──
    # （模型会自相矛盾：target_asset=WTI 但 asset_reasoning 写「主品种 XAU」；
    #  或 target_asset=NONE 却把真实主品种塞进 sibling → 界面显示混乱）
    result = _normalize_main_asset(result)

    # ── v174: JSON Schema 硬约束 + 价格幻觉校验 ──
    # 校验失败 → 在 result 里贴 _v174_validation 标签(供后续 structured_check 写入 DB),
    # 不再 retry (前面已重试 6 次, 再重试会拖慢主路径); fabrication ≥50% 降级 HOLD
    try:
        from common import llm_schema as _ls_v174
        # 拉 structured_input.snapshot 用于价格幻觉校验(若有 _SERVER_SNAPSHOT_CACHE)
        _snap_v174 = None
        try:
            global _SERVER_SNAPSHOT_CACHE
            _msd_raw_v174 = _SERVER_SNAPSHOT_CACHE.get("json") if isinstance(_SERVER_SNAPSHOT_CACHE, dict) else None
            if isinstance(_msd_raw_v174, str):
                import json as _json_v174
                try:
                    _msd_v174 = _json_v174.loads(_msd_raw_v174)
                    _snap_v174 = _msd_v174.get("snapshot") if isinstance(_msd_v174, dict) else None
                except Exception:
                    _snap_v174 = None
            elif isinstance(_msd_raw_v174, dict):
                _snap_v174 = _msd_raw_v174.get("snapshot")
        except Exception:
            _snap_v174 = None
        # snapshot 应该是 list, 包成 dict 传给 llm_schema
        _snapshot_for_check = {"snapshot": _snap_v174} if isinstance(_snap_v174, list) else _snap_v174
        _v174_label = _ls_v174.validate_and_label(
            result, _snapshot_for_check, fabrication_tolerance=0.3
        )
        result["_v174_validation"] = _v174_label
        # fabrication ≥50% 或 schema 完全失败 → 降级 HOLD (不直接 reject, 留 fallback)
        # v176：fabricated_soft / warn 不强制 HOLD，仅写 verification 提示
        if _v174_label["verdict"] == "fabricated" and not str(result.get("reasoning_path", "")).startswith("关键词推断"):
            score = 0.0
            action = "HOLD"
            reasoning = (f"{action}信号[价格幻觉降级] ({_v174_label['fabrication_rate']*100:.0f}% 数字无法对位): "
                         + (str(result.get("reasoning") or "")[:40]))
            print(
                f"  [V174-SCHEMA] {model_cfg['label']} 价格幻觉 {_v174_label['fabrication_rate']*100:.0f}% ≥50% → 降级 HOLD",
                flush=True,
            )
        elif _v174_label["verdict"] == "fabricated_soft":
            # 30-50% 软边界 — 不降级但写 verification WARN，前端展示提示
            _cur_ver = str(result.get("verification") or "")
            if "价格幻觉软边界" not in _cur_ver:
                result["verification"] = (
                    f"warn | 价格幻觉软边界 {_v174_label['fabrication_rate']*100:.0f}% "
                    f"(30-50%); {_cur_ver[:200]}".strip("; ")
                )
            print(
                f"  [V174-SCHEMA] {model_cfg['label']} 价格幻觉软边界 {_v174_label['fabrication_rate']*100:.0f}% → 不强制 HOLD",
                flush=True,
            )
        elif _v174_label["verdict"] == "warn":
            _cur_ver = str(result.get("verification") or "")
            if "价格轻微偏差" not in _cur_ver:
                result["verification"] = (
                    f"warn | 价格轻微偏差 {_v174_label['fabrication_rate']*100:.0f}%; {_cur_ver[:200]}".strip("; ")
                )
            print(
                f"  [V174-SCHEMA] {model_cfg['label']} warn fabrication_rate={_v174_label['fabrication_rate']*100:.1f}%",
                flush=True,
            )
        elif _v174_label["verdict"] == "schema_invalid":
            print(
                f"  [V174-SCHEMA] {model_cfg['label']} JSON Schema 校验失败 ({len(_v174_label['schema_errors'])} 错)"
                " | 明细: " + " ; ".join(str(e) for e in (_v174_label.get("schema_errors") or [])[:3]),
                flush=True,
            )
        else:
            print(
                f"  [V174-SCHEMA] {model_cfg['label']} pass (fabrication_rate={_v174_label['fabrication_rate']*100:.1f}%)",
                flush=True,
            )
    except ImportError:
        pass  # llm_schema 未安装,跳过(理论上 v174+ 应该安装)
    except Exception as _e_v174:
        print(f"  [V174-SCHEMA] 校验异常(不影响主流程): {type(_e_v174).__name__}: {_e_v174}", flush=True)

    # ── 副品种准入兜底（宁缺毋滥）：重试后仍写不出独立分析的副品种 → 降级剔除 ──
    # 用户原则："列进副品种就必须有它自己的八段分析，否则它就跟副品种没关系"。
    # 重试已尝试过补写；仍缺大部分段 → 说明它本就说不清传导链，留在列表里只会
    # 让前端显示一片空白。这里把它从 sibling_decisions 移除（主品种不受影响）。
    try:
        _still = _siblings_missing_stages(result)
        if _still:
            _sibs_all = result.get("sibling_decisions") or []
            _kept, _dropped = [], []
            for _s in _sibs_all:
                if not isinstance(_s, dict):
                    continue
                _code = str(_s.get("target_asset") or "").strip().upper()
                _miss = _still.get(_code) or []
                # 缺 4 段以上（即八段里不到 1/3 有它的行）→ 判定"说不清"，剔除
                if _code and len(_miss) >= 4:
                    _dropped.append(_code)
                else:
                    _kept.append(_s)
            if _dropped:
                result["sibling_decisions"] = _kept
                _detail = "、".join(f"{c}(缺{len(_still.get(c, []))}段)" for c in _dropped)
                print(
                    f"  [SIBDROP] 副品种 {_detail} 八段里几乎无独立分析 → 已从副品种移除",
                    flush=True,
                )
                # 同步清掉 asset_reasoning 里对应品种的推理行（避免"推理逻辑"里
                # 还列着一个已经不算副品种的品种）
                try:
                    _ar = str(result.get("asset_reasoning") or "")
                    if _ar:
                        _lines = []
                        for _ln in _ar.split("；"):
                            _t = _ln.strip()
                            if not _t:
                                continue
                            if _t.startswith("副品种") and any(
                                _t.lstrip("副品种").lstrip().upper().startswith(c) for c in _dropped
                            ):
                                continue
                            _lines.append(_t)
                        result["asset_reasoning"] = "；".join(_lines)[:400]
                except Exception:
                    pass
    except Exception:
        pass

    # ── Doubao fast path: simplified response, no scoring or CoT ──
    if model_cfg.get("label") == "Doubao":
        action = str(result.get("suggested_action", "HOLD")).upper()
        if action not in ("BUY", "SELL", "HOLD"):
            action = "HOLD"
        direct_reasoning = str(result.get("direct_reasoning", "")).strip()[:80]
        if not direct_reasoning:
            direct_reasoning = f"{action} 直觉判断"
        print(f"  [{model_cfg['label']}] {action} | {direct_reasoning}")
        score, action = reconcile_score_action(0.0, action)
        return {
            "sentiment_score": score,
            "suggested_action": action,
            "reasoning": direct_reasoning,
            "translated_title": direct_reasoning[:40],  # 使用 reasoning 的前 40 字符作为显示标题
            "market_category": "",
            "target_asset": "NONE",
            "reasoning_path": "",
            "model_label": model_cfg["label"],
            "model_id": model_cfg["id"],
            # Phase 2: Event Quality Layer (Doubao 不输出这些 → 默认值)
            "event_strength": "medium",
            "direct_catalyst": False,
            "timeframe_match": "intraday",
        }

    # Validate & normalise fields
    score = float(result.get("sentiment_score", 0))
    score = max(-1.0, min(1.0, score))
    # Safety net: never store exactly 0.0 — it's useless for aggregation
    if abs(score) < 0.001:
        score = 0.05  # tiny bullish bias, better than zero
    action = str(result.get("suggested_action", "HOLD")).upper()
    if action not in ("BUY", "SELL", "HOLD", _FAILED_ACTION):
        action = "HOLD"
    reasoning = str(result.get("reasoning", "")).strip()[:80]
    # ── 没分析 → 实事求是标「分析失败」，绝不伪装成观望(HOLD) ──
    # 空骨架（八段段名连写、无任何实质正文）= 模型没真正分析。
    if action != _FAILED_ACTION and _is_eight_stage_skeleton(
            str(result.get("reasoning_path", "") or "")):
        action = _FAILED_ACTION
        reasoning = f"{_FAILED_GATE_PREFIX}：模型未输出有效八段分析"
    # ── AI 结果为主：输出观望(HOLD)/分析失败(FAILED)都不建仓 ──
    # 不管 sentiment_score / 策略积分多强，都绝不把 HOLD/FAILED 改写成方向单；
    # 只有 AI 明确输出 BUY/SELL（且后续闸门+策略积分满足）才可能建仓。
    if action in ("HOLD", _FAILED_ACTION):
        score = 0.0  # 观望/失败：分数归中性（不再出现"说观望却强分"的矛盾）
        if not reasoning:
            reasoning = ("观望：该新闻不足以给出明确方向" if action == "HOLD"
                         else f"{_FAILED_GATE_PREFIX}：模型未输出有效八段分析")
    # ── v318：方向与分数必须同号 ────────────────────────────────────────
    # 之前 reconcile_score_action 只在 Doubao 快速路径调用，主路径没有对齐：
    # LLM 输出 suggested_action=SELL 而 sentiment_score=+0.42 的情况被原样入库，
    # 于是「AI 说 SELL、分数却是正的」—— 闸门按分数符号算方向，SELL 永远出不来
    # （实测 SELL 行里 53/78 的 sentiment 为正），direction_consistent 也会报
    # 「LLM 方向与分数符号矛盾」。这里按既有约定统一对齐：方向为准。
    if action in ("BUY", "SELL"):
        score, action = reconcile_score_action(score, action)
    # Empty reasoning fallback
    if not reasoning:
        reasoning = f"{action}信号,得分{score:+.2f}"

    # ── Display title: 从 reasoning 提取简短中文标题（用于前端显示） ──
    # 注意：前置翻译拦截器已确保输入为中文，这里仅做显示用途的截取
    display_title = reasoning[:40]

    market_category = str(result.get("market_category", "")).upper().strip()
    # 不在 PROD 白名单 → 留空（前端会渲染为"待复核"）；不再 fallback 到 OTHER
    if market_category and market_category not in MARKET_CATEGORIES_PROD:
        if not (market_category == "OTHER" and is_debug_other_allowed()):
            market_category = ""
    target_asset = str(result.get("target_asset", "NONE")).upper().strip()[:20]
    if not target_asset:
        target_asset = "NONE"
    # ── 分类与目标资产一致性：AI 偶尔报 market_category 与分析对象(target_asset)脱节
    # （如"标签原油但分析 BTC"）→ 以 target_asset 为准修正 market_category（前端标签与
    # AI 分析对象保持一致）。走 market_classifier 单一真理源；MACRO 保留模型判断（外汇
    # /国债本质无具体可交易标的）；target_asset=NONE 不改。
    _reconciled = map_target(target_asset)
    if _reconciled and market_category not in ("MACRO",) and market_category:
        if market_category != _reconciled:
            print(
                f"  [CATFIX] market_category={market_category} 与 target_asset={target_asset}"
                f" 不一致 → 修正为 {_reconciled}",
                flush=True,
            )
        market_category = _reconciled
    elif _reconciled and not market_category:
        # market_category 留空 + target_asset 有映射 → 反推回填（避免漏空）
        market_category = _reconciled

    # ── 分析针对性风控兜底（B）：重试后仍无针对性 → 不给方向单（标「分析失败」）──
    # 原则：方向单必须建立在"针对该品种的分析"之上。若八段始终不讲主品种，
    # 说明这次判断缺乏依据，实事求是标「分析失败」（不建仓），绝不伪装成观望(HOLD)。
    # 实现已抽到 _focus_downgrade()：副品种展开路径（_expand_multi_asset_results）
    # 共用同一份规则，避免两处判定条件漂移。
    try:
        _ta_final = str(result.get("target_asset") or "").strip().upper()
        _rp_final = str(result.get("reasoning_path", "") or "")
        _downgraded, _hits_final = _focus_downgrade(result, _ta_final, _rp_final)
        _gtrace = result.setdefault("_gate_trace", [])
        if _downgraded:
            # 同步本地变量：下面返回时用的是 score/action 局部变量，
            # 不同步会导致降级被旧值覆盖（实测踩过：日志显示已降级、结果仍是 BUY）。
            score = 0.0
            action = _FAILED_ACTION
            reasoning = str(result.get("reasoning") or "")
            _gtrace.append({"gate": "FOCUS", "verdict": "block",
                            "reason": (f"主品种 {_ta_final} 分析无针对性（仅 {_hits_final} 段提及）"
                                       f"；raw={result.get('_raw_action')} "
                                       f"score={result.get('_raw_score')}")})
            print(
                f"  [FOCUS-GATE] 主品种 {_ta_final} 分析无针对性（仅 {_hits_final} 段提及）"
                f" → 标记分析失败（不建仓）",
                flush=True,
            )
        else:
            _gtrace.append({"gate": "FOCUS", "verdict": "pass",
                            "reason": f"主品种 {_ta_final} 针对性 OK"})
    except Exception as exc:
        # 不吞错：以前这里静默 pass，闸门失效时线上完全无迹可查
        print(f"  [FOCUS-GATE] 跳过（{type(exc).__name__}: {exc}）", flush=True)

    # ── 传导机制闸门：方向单必须说出"新闻 → 该品种价格"的传导链 ──
    # 用户原则："有关系 = 新闻内容会影响它的价格波动，而不是新闻某字和它有关系"。
    # 针对性闸门只保证"有分析"（提及 >= 2 段），这里再保证"有传导"。
    try:
        _mech_asset = str(result.get("target_asset") or "").strip().upper()
        # v176.5: 直接用 gate_strength (>=8 时不降级, 信任 LLM 强信号)
        _gtrace = result.setdefault("_gate_trace", [])
        if _mechanism_downgrade(result, _mech_asset, strength=gate_strength):
            score = 0.05
            action = "HOLD"
            reasoning = str(result.get("reasoning") or "")
            _gtrace.append({"gate": "MECH", "verdict": "block",
                            "reason": f"主品种 {_mech_asset} 未写出传导链"})
            print(f"  [MECH-GATE] 主品种 {_mech_asset} 未写出传导链 (strength={gate_strength})"
                  f" → 方向单降级 HOLD（影响不明确，不下单）", flush=True)
        else:
            _gtrace.append({"gate": "MECH", "verdict": "pass",
                            "reason": f"主品种 {_mech_asset} 传导链 OK (strength={gate_strength})"})
    except Exception as exc:
        print(f"  [MECH-GATE] 跳过（{type(exc).__name__}: {exc}）", flush=True)

    # Extract reasoning_path — stored in DB & pushed to Feishu
    reasoning_path = str(result.get("reasoning_path", "")).strip()[:2000]
    if reasoning_path:
        print(f"  [{model_cfg['label']}] 推导链: {reasoning_path[:120]}")
    elif news_content:
        # Fallback: if model didn't provide CoT, show truncated news as debug
        short = news_content[:60].replace('\n', ' ')
        print(f"  [{model_cfg['label']}] (无CoT) 新闻: {short}")

    # ── 品种相关推理逻辑（AI 逐品种解释为什么关联此品种；缺省留空，前端老数据不显示区块）──
    asset_reasoning = str(result.get("asset_reasoning", "")).strip()[:400]

    # ── 多品种：AI JSON 显式 sibling_decisions，缺失则从八段文本兜底补 ──
    #    主品种未成立（NONE/OTHER）时不兜底推断副品种：没有主品种就没有"因"，
    #    推断出来的只是些被顺带点名的品种，不该作为可交易信号的存在理由。
    _sibs_out = result.get("sibling_decisions")
    if (not isinstance(_sibs_out, list) or not _sibs_out):
        if str(target_asset or "").strip().upper() in _FOCUS_EXEMPT_ASSETS:
            _sibs_out = []
        else:
            _sibs_out = _infer_sibling_from_reasoning(
                reasoning_path, main_asset=target_asset,
            ) or []
    # ── asset_reasoning 覆盖兜底：sibling 有副品种但推理文本缺行时，用副品种
    #    reasoning/八段上下文补一行占位，保证前端「品种相关推理逻辑」逐品种显示 ──
    asset_reasoning = _ensure_asset_reasoning_coverage(
        asset_reasoning, _sibs_out, main_asset=target_asset,
    )
    # ── 二次主品种归一：上面的兜底会补出 sibling（_infer_sibling_from_reasoning），
    #    而第一次归一（_call_llm_sync 内）跑在补 sibling 之前 → target_asset=NONE
    #    时没有可提升对象，导致库里仍留 NONE（实测 news=4617/4618 就是这种）。
    #    这里在 sibling 补全后再归一一次，保证 NONE 有副品种时被提升为主品种。
    _final = _normalize_main_asset({
        "target_asset": target_asset,
        "market_category": market_category,
        "sentiment_score": score,
        "suggested_action": action,
        "reasoning": reasoning,
        "asset_reasoning": asset_reasoning,
        "sibling_decisions": _sibs_out,
    })
    target_asset = str(_final.get("target_asset") or target_asset)
    market_category = str(_final.get("market_category") or market_category)
    score = float(_final.get("sentiment_score", score))
    action = str(_final.get("suggested_action", action)).upper()
    reasoning = str(_final.get("reasoning", reasoning))
    asset_reasoning = str(_final.get("asset_reasoning", asset_reasoning))
    _sibs_out = _final.get("sibling_decisions") or []

    # ── 主品种前置（最后一次对齐）：主品种最终仍未成立（NONE/OTHER）→ 不保留副品种。
    #    "先有主品种，后面才会有副品种"：兜底推断出来的副品种此时没有可依托的主品种，
    #    留着只会在界面上给出一个没有来源的"其它品种信号"，且宽松模式下会真建仓。
    if str(target_asset or "").strip().upper() in _FOCUS_EXEMPT_ASSETS and _sibs_out:
        print(
            f"  [MAIN-GATE] 主品种未成立（{str(target_asset or 'NONE').upper()}）"
            f" → 移除 {len(_sibs_out)} 个无主品种依托的副品种",
            flush=True,
        )
        _sibs_out = []

    # ── v211 P1-B：服务端兜底 — 二次归一后 target_asset 仍为 NONE/OTHER 时，
    #    扫描 news_content 用 _ASSET_ALIASES 找回真实 ticker（避免数据进 unclassified_events）。
    #    根因：LLM 在「个股/港股/韩股」类扩展品种上常输出 NONE，710 条 NO_MAPPING_FOR_ASSET:NONE
    #    几乎全因 LLM 不会写 SKHYNIX/MU 等小众 ticker。修复只覆盖 LLM 输出 NONE/OTHER 的情况，
    #    不主动覆盖 LLM 已正确填写的 target_asset。
    try:
        _rescued_target = _rescue_target_asset_from_content(
            news_content, target_asset,
        )
        if _rescued_target and _rescued_target != target_asset and str(_rescued_target or "").strip().upper() not in (
            "", "NONE", "OTHER", "N/A", "NULL",
        ):
            # market_category 用 market_classifier 重新映射（一致性修正）
            try:
                from market import market_classifier as _mc_v211
                _rescued_cat = _mc_v211.map_target(_rescued_target) or market_category
            except Exception:
                _rescued_cat = market_category
            print(
                f"  [RESCUE-TARGET] LLM 输出 NONE/OTHER，从正文找回 {_rescued_target}"
                f"（market_category {_rescued_cat}）",
                flush=True,
            )
            target_asset = _rescued_target
            market_category = _rescued_cat
    except Exception as _exc_rescue:
        print(f"  [RESCUE-TARGET] 跳过（{type(_exc_rescue).__name__}: {_exc_rescue}）", flush=True)

    # ── 把 _v174_validation (JSON Schema + 价格幻觉) 透传到下游,供 structured_check 合并 ──
    _v174_validation_out = result.get("_v174_validation")
    _factor_ranking_out = result.get("factor_ranking") or []
    _asset_nature_out = result.get("asset_nature") or ""

    return {
            "sentiment_score": score,
            "suggested_action": action,
            "reasoning": reasoning,
            "translated_title": display_title,  # 使用 reasoning 的前 40 字符作为显示标题
            "market_category": market_category,
            "target_asset": target_asset,
            "reasoning_path": reasoning_path,
            "asset_reasoning": asset_reasoning,
            "sibling_decisions": _sibs_out,
            "model_label": model_cfg["label"],
            "model_id": model_cfg["id"],
            # Phase 2: Event Quality Layer — 补齐 LLM 元数据字段（_parse_result 已有默认兜底）
            "prediction_type": result.get("prediction_type", "continuation"),
            "event_phase": result.get("event_phase", "mid"),
            "market_confirmation": result.get("market_confirmation", "unknown"),
            "expected_horizon": result.get("expected_horizon", "1-3d"),
            "invalidation_condition": result.get("invalidation_condition", ""),
            "event_strength": result.get("event_strength", "medium"),
            "direct_catalyst": bool(result.get("direct_catalyst", False)),
            "timeframe_match": result.get("timeframe_match", "intraday"),
            # 影响评估（0-10，_parse_result 已 clamp）
            "heat_score": result.get("heat_score", 5.0),
            "impact_score": result.get("impact_score", 5.0),
            "asset_impact": result.get("asset_impact", 5.0),
            # v174: JSON Schema 校验 + 价格幻觉标签 (供 ai_worker 后续 structured_check 写入 DB)
            "_v174_validation": _v174_validation_out,
            # v174: 因素排行 / 资产性质 (即使 LLM 没填也透传,structured_input 可观察)
            "factor_ranking": _factor_ranking_out,
            "asset_nature": _asset_nature_out,
            # v174: 新增三字段 (event_type_code / verification / snapshot_proximity)
            "event_type_code": result.get("event_type_code") or "",
            "verification": result.get("verification") or "",
            "snapshot_proximity": result.get("snapshot_proximity"),
            # v181: 主分析阶段 FOCUS/MECH 闸门判定留痕（供主循环回放到 gate_chain）
            "_gate_trace": result.get("_gate_trace") or [],
            # v188 fix：返回时透传全网溯源三字段（Tier-3 兜底/LLM 抽签/正则抽取都写到 result，
            # 但此 return dict 没透出 → 调用方 res.get() 永远拿空 → DB 留空）。
            # 不透传的话，Tier-3 平台首发兜底、regex 主渠道抽取全部白干——本次会话
            # 直接定位为 bug 根因（之前误以为是 Tier-3 没触发，其实是 return dict 没出口）。
            "cross_net_first_seen_ts": result.get("cross_net_first_seen_ts") or "",
            "cross_net_source": result.get("cross_net_source") or "",
            "net_origin_info": result.get("net_origin_info") or "",
            # v200 P0-A3 修复: _asset_insight_hit_ids 原本定义在内层闭包 _do_api_call 里,
            # 是局部变量, 外层调用方永远取不到 → NameError, insight_hit_ids 100% 写空。
            # 同样按 _gate_trace / _v174_validation 的模式从 return dict 透出。
            "_asset_insight_hit_ids": _asset_insight_hit_ids,
        }


# ---------------------------------------------------------------------------
# Task B — Concurrent Batch AI Worker (BATCH_SIZE 定义见 config.py)
# ---------------------------------------------------------------------------


# 新闻时效门槛（秒）：后台可调，引擎按 TTL 读取最新值（≤5s 生效）
_news_max_age_cache: Dict[str, Any] = {"ts": 0.0, "value": config.NEWS_TRADE_MAX_AGE_MINUTES * 60}


def _get_news_trade_max_age_seconds() -> int:
    """从 paper_trading_settings 读后台配置的时效门槛（秒），5s TTL 缓存。"""
    now = time.monotonic()
    if now - float(_news_max_age_cache.get("ts", 0.0)) < 5.0:
        return int(_news_max_age_cache["value"])
    value = config.NEWS_TRADE_MAX_AGE_MINUTES * 60
    try:
        from paper_trading import get_settings as _get_pt_settings
        st = _get_pt_settings()
        stored = st.get("news_trade_max_age_seconds")
        if isinstance(stored, (int, float)) and stored >= 30:
            value = int(stored)
    except Exception:
        pass
    _news_max_age_cache.update({"ts": now, "value": value})
    return value


def _load_insights_section() -> str:
    """复盘经验注入分区（结构化标题 + 逐条内容）。

    独立成区而非挤成一行 " | "：模型能逐条对照，且带样本数（可靠性依据）。
    硬铁律(H#)/软启发(EX#) 分两区，由 build_active_insights_section 输出。
    条数由 config.REVIEW_INSIGHTS_LIMIT 控制（默认不限 = 启用多少注入多少）。
    """
    try:
        from review.review_agent import build_active_insights_section, seed_analysis_experience
        try:
            seed_analysis_experience()  # 幂等播种硬铁律/软启发种子（首次分析写入）
        except Exception:
            pass
        # v415: 回流 A/B（批次级集群随机，默认 30% 对照）。
        # 对照组只撤软启发(kind=1)，硬铁律(kind=0)任何组都保留 —— 那是人工约束，不能拿去做实验。
        # 分组失败一律按 treatment（宁可多给经验，也不静默丢规则）。
        grp, key, bstart = "treatment", "", 0
        try:
            from reflection_ab import assign_group, log_assignment
            grp, key, bstart = assign_group()
        except Exception:
            grp, key = "treatment", ""
        text = (build_active_insights_section(include_soft=False)
                if grp == "control" else build_active_insights_section())
        if key and grp in ("treatment", "control"):
            try:
                import re as _re_ab
                ids = [int(x) for x in _re_ab.findall(r"\[EX#(\d+)", text)]
                log_assignment(key, grp, ids, bstart)
            except Exception:
                pass
        return text
    except Exception:
        return ""


# v176.3: 反思错误码注入分区 — 让 AI 决策时看到最近决策触发的程序化校验错误码
def _load_reflection_block(limit: int = 10) -> str:
    """从 decision_errors 表取最近 N 个去重错误码 → 一句话指导 → 注入 system prompt。

    与复盘经验 (insights section) 互补 — insights 是"教训性的经验规则"，
    reflection 是"近期决策触发的具体错误码"（更即时、更具体）。
    30s TTL 缓存，避免每次分析都查 DB；任何异常 → 返回 ""（零注入）。
    """
    global _REFLECTION_BLOCK_CACHE
    now = time.monotonic()
    try:
        cached = _REFLECTION_BLOCK_CACHE
        if cached and (now - cached.get("ts", 0)) < 30:
            return cached.get("text", "")
    except Exception:
        pass
    try:
        from review.reflection_checkers import get_active_error_codes_summary, format_compact_guidance
        import db as _db
        conn = _db.get_connection()
        try:
            conn.row_factory = _db.sqlite3.Row  # db.get_connection() 不设 row_factory, 这里手动设
            codes = get_active_error_codes_summary(limit=limit, conn=conn)
            text = format_compact_guidance(codes) if codes else ""
        finally:
            # v189-8: 原先成功/异常两条路径都不关连接
            conn.close()
    except Exception:
        text = ""
    try:
        _REFLECTION_BLOCK_CACHE = {"ts": now, "text": text}
    except Exception:
        pass
    return text


# 反思错误码注入块缓存（30s TTL）
_REFLECTION_BLOCK_CACHE: Dict[str, Any] = {"ts": 0.0, "text": ""}


# v186 经验系统注入 — 按当前 asset/gate_kind/actor 上下文从 experience_entries
# 取最相关的 top-5 条经验,作为 system prompt 末尾的一个分区。与 insights 互补:
#   insights = 复盘历史沉淀的硬铁律/软启发(全局通用, 跨品种)
#   experience = 当前品种/当前人物的特化经验(精准, 短期信号校准)
# v490: 单条全局缓存 -> 按 (asset,gate,actor) 分键。旧的单条缓存会在多品种并发时
# 互相踢(每次换品种就失效), 只影响命中率; 但缓存内容塞错 key 的风险不值得留。
# 结构: {(asset,gk,actor): {"ts": monotonic, "text": str, "exp_ids": [..]}}
_EXPERIENCE_BLOCK_CACHE: Dict[Any, Dict[str, Any]] = {}
_EXPERIENCE_BLOCK_CACHE_MAX = 64


# v188 P0-2: 最近一次真正注入到 prompt 的经验 ID 列表(用于 ai_decisions 写入后标 llm_invoked=1)
# v490: 加线程本地副本 —— 分析线程和 gate 线程会并行跑, 进程级单值会把 A 决策的经验
# 记到 B 决策头上。全局变量保留做兼容档(single-thread 场景/老测试仍可读)。
_LAST_INJECTED_EXP_IDS: List[int] = []
_EXP_IDS_TLS = threading.local()


def _set_injected_exp_ids(ids) -> None:
    """写当前线程的经验 id 标记(同时同步全局, 兼容老代码)。"""
    global _LAST_INJECTED_EXP_IDS
    try:
        _list = [int(x) for x in (ids or []) if x and int(x) > 0]
    except Exception:
        _list = []
    try:
        _EXP_IDS_TLS.ids = list(_list)
    except Exception:
        pass
    _LAST_INJECTED_EXP_IDS = list(_list)


def _get_injected_exp_ids() -> List[int]:
    """读当前线程的经验 id 标记; 本线程没有就回落到全局。"""
    try:
        tls_ids = getattr(_EXP_IDS_TLS, "ids", None)
    except Exception:
        tls_ids = None
    if tls_ids:
        return [int(x) for x in tls_ids if x and int(x) > 0]
    try:
        return [int(x) for x in (_LAST_INJECTED_EXP_IDS or []) if x and int(x) > 0]
    except Exception:
        return []


def _load_spa_applied_block_sync(asset: str = "", max_chars: int = 600,
                                 ttl: float = 60.0) -> str:
    """v199: 加载 strategy_param_adjustments (status='applied') 设置块注入 LLM prompt。

    数据源: strategy_param_adjustments (status='applied')
    匹配规则:
      1. asset 完全匹配 (SPA.asset == 查询 asset)
      2. SPA.asset ∈ {NONE, *, ''} 作为兜底 (跨 asset 通用)
    输出格式:
      # 策略调整已应用 (v199 SPA Applied Block)
      → asset={asset} 同 asset {n} 条, 全局兜底 {n2} 条
      [SPA1] [param_key] direction mag=n (n=sample, conf=0.xx)
    60s TTL cache (按 asset 维度)。
    """
    global _SPA_APPLIED_BLOCK_CACHE
    asset_norm = (asset or "").upper()
    now = time.monotonic()
    try:
        cached = _SPA_APPLIED_BLOCK_CACHE
        if cached and cached.get("key") == asset_norm and (now - cached.get("ts", 0)) < ttl:
            return cached.get("text", "")
    except Exception:
        pass
    try:
        import sqlite3 as _sq3
        conn = db.get_connection()
        conn.row_factory = _sq3.Row
        try:
            rows = conn.execute("""
                SELECT asset, param_key, direction, magnitude,
                       sample_size, avg_confidence, applied_setting_change,
                       updated_at
                  FROM strategy_param_adjustments
                 WHERE status='applied'
                   AND applied_setting_change != ''
                 ORDER BY updated_at DESC LIMIT 50
            """).fetchall()
        finally:
            conn.close()
        if not rows:
            return ""
        matched = []
        generic = []
        for r in rows:
            ra = str(r["asset"] or "").upper()
            if not asset_norm or asset_norm in ("NONE", "*", ""):
                matched.append(dict(r))
            elif ra == asset_norm:
                matched.append(dict(r))
            elif ra in ("NONE", "*", ""):
                generic.append(dict(r))
        items = matched + generic[:3]
        if not items:
            return ""
        lines = ["# 策略调整已应用 (v199 SPA Applied Block)"]
        lines.append(f"  → asset={asset_norm or 'ANY'} 同 asset {len(matched)} 条, 全局兜底 {len(generic)} 条")
        lines.append("")
        for i, it in enumerate(items[:8], 1):
            try:
                conf_v = float(it.get("avg_confidence") or 0)
            except Exception:
                conf_v = 0.0
            lines.append(
                f"[SPA{i}] [{it['param_key']}] {it.get('direction','')} "
                f"mag={it.get('magnitude',0)} "
                f"(n={it.get('sample_size',0)}, conf={conf_v:.2f})"
            )
        block = "\n".join(lines)
        if len(block) > max_chars:
            block = block[:max_chars - 50] + "\n...(SPA 块过长已截断)"
    except Exception as exc:
        print(f"[SPA-APPLY-BLOCK] 加载失败: {type(exc).__name__}: {exc}", flush=True)
        block = ""
    try:
        _SPA_APPLIED_BLOCK_CACHE = {"ts": now, "text": block, "key": asset_norm}
    except Exception:
        pass
    return block


_SPA_APPLIED_BLOCK_CACHE: Dict[str, Any] = {}


def _load_experience_block_sync(asset: str = "", gate_kind: str = "",
                                actor: str = "", ttl: float = 30.0,
                                decision_id: int = 0) -> str:
    """v186 经验系统注入分区。30s TTL 缓存。
    asset: 主品种(BTC/ETH/SOL/XAU/WTI 等)
    gate_kind: gate 类型(总结类/政策类/事件类 ...)
    actor: 人物/机构(特朗普/Powell/Gensler/Vitalik ...)
    """
    now = time.monotonic()
    key = (asset.upper(), gate_kind.lower(), actor.lower())
    try:
        cached = _EXPERIENCE_BLOCK_CACHE.get(key) or {}
        if cached and (now - float(cached.get("ts", 0.0) or 0.0)) < ttl:
            # 命中缓存时, 同步还原上次 inject 的 ids
            _set_injected_exp_ids(cached.get("exp_ids") or [])
            return cached.get("text", "")
    except Exception:
        pass
    try:
        import experience_reflector as _er
        # v188 P0-2: build_prompt_block 返回 (text, ids) — ids 是本次真正 inject 的 exp id 列表
        _result = _er.build_prompt_block(asset=asset or "", gate_kind=gate_kind or "",
                                          actor=actor or "", max_chars=1800,
                                          decision_id=int(decision_id or 0))
        if isinstance(_result, tuple) and len(_result) == 2:
            block, ids = _result
        else:
            block, ids = _result, []
        _set_injected_exp_ids(ids or [])
    except Exception as exc:
        print(f"[EXP-BLOCK] 经验注入失败: {type(exc).__name__}: {exc}", flush=True)
        block = ""
        ids = []
    try:
        if len(_EXPERIENCE_BLOCK_CACHE) >= _EXPERIENCE_BLOCK_CACHE_MAX:
            # 简单淘汰: 丢掉最老的一半, 避免无界增长
            _sorted = sorted(_EXPERIENCE_BLOCK_CACHE.items(),
                             key=lambda kv: float((kv[1] or {}).get("ts", 0.0) or 0.0))
            for _k, _ in _sorted[:len(_sorted) // 2]:
                _EXPERIENCE_BLOCK_CACHE.pop(_k, None)
        _EXPERIENCE_BLOCK_CACHE[key] = {"ts": now, "text": block,
                                        "exp_ids": list(ids or [])}
    except Exception:
        pass
    return block


def _record_injected_experiences(decision_id: int) -> int:
    """v482: 决策落库后立刻把"本次注入过哪些经验"记账。

    旧实现放在落库后的一个长 try 里(依赖全局 _LAST_INJECTED_EXP_IDS),
    中间任何一步抛异常整段就被跳过 —— 实测 experience_applications 恒 0、
    6 条经验 apply_count 全 0, 于是"经验用了之后赢没赢"永远学不到。
    这里紧跟 decision_id 生成之后调用, 并在用完后清空标记, 避免串到下一条决策。
    """
    ids = _get_injected_exp_ids()
    _set_injected_exp_ids([])
    if not decision_id or not ids:
        return 0
    n = 0
    try:
        import experience_reflector as _er_mark
        for eid in ids:
            try:
                if _er_mark.apply_experience(eid, int(decision_id),
                                             applied_action="prompt_inject",
                                             llm_invoked=1):
                    n += 1
            except Exception:
                continue
    except Exception as exc:
        print(f"  [EXP-INJECT-MARK] failed: {type(exc).__name__}: {exc}", flush=True)
    return n


_last_kline_persist_ts: float = 0.0      # K 线持久化冷却（惰性 persist）
# WTI/XAU K 线 09-14 后断流的根因：UA `TridentAgentMVP/1.0` 被 Yahoo TLS RST。
# 修复:UA → Mozilla（knowledge_base）+ persist 频率 300s → 60s + 不再"快照空才 persist"。


def _load_kline_block() -> str:
    """历史 K 线知识库 block：每 60s 主动 persist + 读最新 kline_snapshots。

    只描述位置/趋势、不直接给方向（反共识对照），数据缺失标 N/A 不造数。
    """
    global _last_kline_persist_ts
    try:
        import knowledge_base
        if time.monotonic() - _last_kline_persist_ts >= _KLINE_PERSIST_COOLDOWN_S:
            _last_kline_persist_ts = time.monotonic()
            try:
                knowledge_base.persist()
            except Exception as persist_err:
                print(f"  [KLINE] persist 失败: {type(persist_err).__name}: {str(persist_err)[:80]}")
        snaps = knowledge_base.latest_snapshots()
        if snaps:
            _kb = knowledge_base.render_kline_block(snaps)
            # v500：这块实测 ~1.9K 字符（≈1.2K token），每条新闻都发一遍。
            #   裁到 1000 字符只保留最近几根与关键位，超出部分对方向判断贡献很低。
            return (_kb[:_MAIN_KLINE_CHARS] + "\n…（K线明细已截断）") if len(_kb) > _MAIN_KLINE_CHARS else _kb
        return ""
    except Exception as e:
        print(f"  [KLINE] 构建失败: {type(e).__name_}: {str(e)[:80]}")
        return ""


# ── 多模型交叉验证：高价值新闻触发（VIP/高相关/金十日历/FRED 官方）──
# v314 P0: 同行评审抽样率（确定性，按 news_id 哈希）。
# 原来只有"高价值新闻"才做交叉验证，实测同行评审填充率仅 1.7%（81/4726）
# → 验收 #4B 判"心理维度没有可核验的输入"。这里加一层**确定性抽样**，
# 保证线上持续产出 ≥10% 的有评审决策，且不看价格/结果（无信息泄漏、可复现）。
_VERIFY_SAMPLE_RATE = float(os.getenv("PEER_REVIEW_SAMPLE_RATE", "0.2") or 0.2)


def _peer_review_sampled(news_row: Any) -> bool:
    """按 news_id 哈希做确定性抽样（同一 id 永远同一结论，可复现）。"""
    if _VERIFY_SAMPLE_RATE <= 0:
        return False
    try:
        nid = int(news_row["id"]) if "id" in news_row.keys() else 0
    except Exception:
        nid = 0
    if nid <= 0:
        return False
    try:
        h = _hashlib.sha1(str(nid).encode("utf-8")).digest()
    except Exception:
        return False
    return (h[0] / 256.0) < _VERIFY_SAMPLE_RATE


# ── 阶段2：多模型共识闸门 ────────────────────────────────────────────
# peer_reviews 交叉验证从"仅展示/标注"升级为"参与主决策降级/拦截"：
# 验证模型方向与主模型不一致时，方向单就地降级 HOLD（宁可观望也不建仓）。


def _is_high_value_news(news_row: Any, content: str) -> bool:
    """高价值新闻 → 需要多模型交叉验证（普通新闻单模型即可）。

    v314 P0: 追加**确定性抽样**（_peer_review_sampled）—— 交叉验证不再只覆盖
    高价值新闻, 而是稳定覆盖约 PEER_REVIEW_SAMPLE_RATE 的新闻, 让同行评审
    填充率(=多模型共识的可核验输入) 持续高于验收阈值 10%。

    v453 P1-2（2026-11-19）：额度优先给主分析，评审收窄到「值钱」的新闻：
      · 门卫强度 >= PEER_REVIEW_MIN_GATE_STRENGTH（默认 7）→ 必评审；
      · 低强度新闻默认**不再**做哈希抽样（PEER_REVIEW_SAMPLE_LOW_STRENGTH=0），
        只保留来源白名单 / 高相关度 / VIP 三条入口；
      · 原先"每条都抽样 20%"在 11-18 实测里评审吃掉 19.6 万 tokens，
        而评审票本身只影响降级、不产生方向单 —— 省下来给主分析更划算。
    """
    # v453 P1-2：高门卫强度 = 明确值得交叉验证
    try:
        _gs = int(news_row["gate_strength"] or 0) if "gate_strength" in news_row.keys() else 0
    except Exception:
        _gs = 0
    try:
        _min_gs = int(os.getenv("PEER_REVIEW_MIN_GATE_STRENGTH", "7") or 7)
    except Exception:
        _min_gs = 7
    if _gs >= _min_gs:
        return True
    _low_strength_sample = (str(os.getenv("PEER_REVIEW_SAMPLE_LOW_STRENGTH", "0"))
                            .strip().lower() not in ("0", "off", "false", "no"))
    if (_gs <= 0 or _low_strength_sample) and _peer_review_sampled(news_row):
        return True
    try:
        source = str(news_row["source"] if "source" in news_row.keys() else "")
    except Exception:
        source = ""
    if source in _VERIFY_SOURCES:
        return True
    try:
        rel = news_row["relevance_score"]
        if rel is not None and float(rel) >= _VERIFY_RELEVANCE_MIN:
            return True
    except Exception:
        pass
    vip_tag, _ = _detect_vip(content)
    return bool(vip_tag)


def _pick_verify_cfgs(main_cfg: Dict[str, Any], pool: List[Dict[str, Any]], n: int = 2,
                      fallback: int = 0) -> List[Dict[str, Any]]:
    """选 n 个验证模型：**优先跨模型族**，其次不同 id（保持池序、去重）。

    fallback>0 时，在 n 个主选之外再补这么多个不同 id 的兜底候选（标记
    `_verify_fallback=True`）：主选通道 think-only/异常时由 _run_verify 串行回退，
    避免单条评审票因主选模型吐思考块而直接丢失。兜底只在主选失败后才调用，
    不增加正常路径的请求量。

    v314 P0（验收 #4 "准确评估共识与多模型一致性"）：
    验证模型若与**彼此**同族，signal_weight_evaluator._consensus_mult 会把同族
    评审折叠成一票 → 票数 <2 → 共识未定义 → fail-closed 回 1.0，
    consensus_mult 于是**恒为 1.0**（= "共识空转"），extra_models_consensus 也全空。
    实测根因（2026-11-02）：运行时模型池只剩 minimax2/MiniMax-M3 一个族
    （MINIMAX_MAIN_WEIGHT=0），门卫/反思通道因 key 相同被跳过 → 评审模型和
    主模型都是 M3，两个评审彼此同族 → 折成 1 票。

    这里优先挑**与主模型不同族**的通道（config purpose=verify 的 peer 通道
    = MiniMax-M2.7，独立账户/独立配额）；同族的不同 id 只作兜底。
    """
    try:
        from signals.signal_weight_evaluator import _model_family as _fam
    except Exception:  # pragma: no cover — 兜底: 去掉通道前缀即可
        def _fam(x):
            m = str(x or "").strip().lower()
            return m.rsplit("/", 1)[-1] if "/" in m else m

    out: List[Dict[str, Any]] = []
    seen = {str(main_cfg.get("id", ""))}
    main_family = _fam(main_cfg.get("id") or main_cfg.get("request_model"))

    # 候选 = **verify 专用池优先**（peer 通道）+ 主池，按 id 去重
    # 2026-11-02：原来顺序是"主池 + verify 池"，主池里 gate/reflect 通道排在
    # 前面 → 评审默认落到 gate 通道（它同时承担 realtime_filter，30 分钟内
    # 847 次调用，是最忙的通道），既给它加压又让评审依赖池的排列顺序。
    # 现在让**专用 peer 通道优先**：peer(M2.7 族) + peer2(M3 族) 天然是两个
    # 不同族，与主模型是 M3 还是 M2.7 都无关 → 永远能凑齐 2 票。
    cands: List[Dict[str, Any]] = []
    cseen = set()
    try:
        for m in config.get_pool_for_purpose("verify"):
            mid = str(m.get("id", ""))
            if not mid or mid in cseen:
                continue
            cseen.add(mid)
            try:
                cands.append(_resolve_model_cfg(m))
            except Exception:
                cands.append(m)
    except Exception:
        pass
    for m in list(pool or []):
        mid = str(m.get("id", ""))
        if mid and mid not in cseen:
            cseen.add(mid)
            cands.append(m)

    # ── 选 n 个评审通道：必须**彼此不同族**，否则共识会被折叠成 1 票 ────────
    # signal_weight_evaluator._consensus_mult 会把**评审之间**同族的票折叠成一票，
    # 折叠后不足 2 票 → 共识未定义 → fail-closed 回 1.0（= "恒 1.0"）。
    # 2026-11-02 实测漏洞：主模型是 M2.7 族（reflect 通道）时，第一轮"跨族"
    # 只剩 M3 族可选（peer2 / minimax / anomaly 都是 M3）→ 两个评审同族 →
    # 折成 1 票 → 共识又空了。所以第二轮兜底必须优先挑**族还没被占用**的
    # 通道（如 M2.7-highspeed 族），而不是随手再拿一个 M3。
    chosen_fams: set = set()
    picked_ids: set = set()

    def _pick(m: Dict[str, Any]) -> None:
        mid = str(m.get("id", ""))
        picked_ids.add(mid)
        fam = _fam(m.get("id") or m.get("request_model"))
        if fam:
            chosen_fams.add(fam)
        out.append(m)

    # 第一轮：跨族（与主模型不同族）且族未被占用
    for m in cands:
        if len(out) >= n:
            break
        mid = str(m.get("id", ""))
        if mid in seen or mid in picked_ids:
            continue
        fam = _fam(m.get("id") or m.get("request_model"))
        if main_family and fam and fam != main_family and fam not in chosen_fams:
            _pick(m)
    # 第二轮：族未占用的通道（允许与主模型同族，如 M2.7 主模型 + M2.7 评审）
    for m in cands:
        if len(out) >= n:
            break
        mid = str(m.get("id", ""))
        if mid in seen or mid in picked_ids:
            continue
        fam = _fam(m.get("id") or m.get("request_model"))
        if fam and fam in chosen_fams:
            continue
        _pick(m)
    # 第三轮：不同 id 兜底（保证至少还有第二个评审通道；族可能重复）
    for m in cands:
        if len(out) >= n:
            break
        mid = str(m.get("id", ""))
        if mid in seen or mid in picked_ids:
            continue
        _pick(m)
    # 第四轮：额外兜底候选（主选 think-only/失败后回退用），与主选不同 id 即可，
    # 族允许重复——单票评审场景下同族兜底不会触发共识折叠（最终只落一张票）。
    if fallback > 0:
        for m in cands:
            mid = str(m.get("id", ""))
            if mid in seen or mid in picked_ids:
                continue
            m = dict(m)
            m["_verify_fallback"] = True
            out.append(m)
            picked_ids.add(mid)
            if len([x for x in out if x.get("_verify_fallback")]) >= fallback:
                break
    return out


def _build_extra_models_consensus(peer_reviews: Any, main_action: str = "") -> str:
    """把同行评审投影成 ai_decisions.extra_models_consensus（legacy dict 形态）。

    形态: {"<模型标签>": {"agree": bool, "action": ..., "score": ...,
                          "reasoning": ..., "model_id": ...}}

    · agree = 该评审 action 与**主模型 action 一致**（主模型 action 未知 → 无法判断
      一致性 → 返回 "" 不写，让 _consensus_mult 走 fail-closed 1.0，绝不虚高）；
    · 同一模型族只保留一条（_consensus_mult 的 dict 形态不做族去重，
      这里先去重，避免"一个模型自己问自己两次"被当成两票）；
    · 返回 "" 表示没有可用的跨模型评审 → 调用方写 NULL。

    v314 P0：这个字段此前**全库为空**（生产只写 peer_reviews），
    验收 #4 要求 extra_models_consensus 有内容才算"多模型共识真的采了"。
    """
    if not peer_reviews:
        return ""
    try:
        from signals.signal_weight_evaluator import _model_family as _fam
    except Exception:
        def _fam(x):
            m = str(x or "").strip().lower()
            return m.rsplit("/", 1)[-1] if "/" in m else m
    main = str(main_action or "").strip().upper()
    if not main:
        return ""
    out: Dict[str, Any] = {}
    seen = set()
    for r in peer_reviews:
        if not isinstance(r, dict):
            continue
        action = str(r.get("action") or "").strip().upper()
        if not action:
            continue
        fam = _fam(r.get("model_id") or r.get("model_label"))
        if fam:
            if fam in seen:
                continue
            seen.add(fam)
        label = str(r.get("model_label") or r.get("model_id") or "reviewer")
        out[label] = {
            "agree": bool(action == main),
            "action": action,
            "score": r.get("score"),
            "reasoning": (r.get("reasoning") or "")[:300],
            "model_id": r.get("model_id") or label,
        }
    if not out:
        return ""
    try:
        return json.dumps(out, ensure_ascii=False)
    except Exception:
        return ""


async def _process_single(
    news_row: sqlite3.Row,
    model_cfg: Dict[str, str],
    loop: asyncio.AbstractEventLoop,
    market_context: str = "",
    performance_context: str = "",
    writing_context: str = "",
    insights_section: str = "",
    reference_signals: str = "",
    fallback_cfgs: List[Dict[str, Any]] = (),
    verify_cfgs: List[Dict[str, Any]] = (),
) -> Dict[str, Any]:
    """
    Process a single raw_news row through the primary model's LLM pipeline.

    多模型 Failover：主模型超时/失败 → 自动换池内其它模型重试，
    慢通道（如 nassaapi 波动 100s+）不再拖死单条。

    多模型交叉验证（verify_cfgs 非空时）：主模型成功后，额外让验证模型
    独立分析同一条新闻（peer_reviews），仅展示/标注，不改变主决策。

    Returns:
      {"news_id": int, "pre_ts": str, "model_label": str,
       "result": dict | None, "error": str | None,
       "peer_reviews": list | None}
    """
    news_id = news_row["id"]
    content = re.sub(r'\[hash:[a-fA-F0-9]+\]\s*', '', news_row["content"])
    # 优先用 published_at(金十/新闻源声称的发布时间,通常早于入库时间 5-15min),
    # fallback timestamp(本系统入库时刻,WS 源无 published_at 时使用)。
    # 这是 SERVER VERDICT 中 "price-in 对比" 的关键时间锚 — 用入库时间会全部被算成"已 price-in"。
    try:
        _pa = str(news_row["published_at"] or "").strip()
    except Exception:
        _pa = ""
    pre_ts = _pa if _pa else news_row["timestamp"]
    # 门卫四步标签：从 raw_news.gate_* 列拼出分析先验（老库缺列/未过门卫 → 空串）
    gate_context = _build_gate_context(news_row)
    # SERVER VERDICT 用的 target_asset：直接读 raw_news.gate_main_asset
    # （[门卫四步] 拼出的也是这个值；显式取防止解析错误）
    try:
        gate_target_asset = str(news_row["gate_main_asset"] or "").strip().upper()
    except Exception:
        gate_target_asset = ""
    # v174: 查 event_type_code 用于 baseline 注入 (raw_news → event_atom → dim_event_type)
    event_type_code = ""
    try:
        import db as _db_v174
        _conn_v174 = _db_v174.get_connection() if hasattr(_db_v174, "get_connection") else None
        if _conn_v174 is not None:
            try:
                _r = _conn_v174.execute(
                    "SELECT det.code FROM event_atom ea "
                    "JOIN dim_event_type det ON det.id = ea.event_type_id "
                    "WHERE ea.raw_news_id = ? LIMIT 1",
                    (news_id,),
                ).fetchone()
                if _r:
                    event_type_code = str(_r[0] or "")
            finally:
                # v189-8: 原先从不关闭, 每条新闻泄漏 1 个 fd
                _conn_v174.close()
    except Exception as _e_v174:
        print(f"[BASELINE] 查 event_type_code 失败(静默): {type(_e_v174).__name__}: {_e_v174}", flush=True)
        event_type_code = ""

    async def _analyze_with(cfg: Dict[str, str], timeout: float = 65.0,
                           purpose: str = "main", call_site: str = "ai_worker_main"):
        _lean_call = (purpose == "verify")
        async with _LLM_SEMAPHORE:
            return await asyncio.wait_for(
                loop.run_in_executor(
                    None, _call_llm_sync, content, cfg, market_context,
                    performance_context, pre_ts, writing_context, insights_section,
                    reference_signals, gate_context,
                    gate_target_asset, news_id,
                    event_type_code,
                    int(news_row["gate_strength"] or 0) if "gate_strength" in news_row.keys() else 0,  # v176.5: 传给 MECH-GATE
                    str(news_row["source"] or ""),  # v176.10: 二次回灌 cross_net 需要原文来源
                    str(news_row["published_at"] or ""),  # v188：tier-3 平台首发兜底用
                    purpose,                        # v187: purpose=main/verify
                    call_site,                     # v187: call_site
                    _lean_call,                    # v322: lean（评审精简）
                ),
                timeout=timeout,
            )

    peer_reviews: List[Dict[str, Any]] = []

    def _review_from(vcfg: Dict[str, Any], vres: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "model_label": vcfg.get("label", vcfg.get("id", "?")),
            "model_id": vcfg.get("id"),
            "action": vres.get("suggested_action"),
            "score": vres.get("sentiment_score"),
            "reasoning": (vres.get("reasoning") or "")[:300],
            "reasoning_path": (vres.get("reasoning_path") or "")[:3000],
            # 关键维度（与主模型对比展示用）
            "expected_horizon": vres.get("expected_horizon"),
            "event_strength": vres.get("event_strength"),
            "market_confirmation": vres.get("market_confirmation"),
            "direct_catalyst": vres.get("direct_catalyst"),
            "heat_score": vres.get("heat_score"),
            "impact_score": vres.get("impact_score"),
            "asset_impact": vres.get("asset_impact"),
        }

    async def _run_verify(vcfgs) -> Optional[Dict[str, Any]]:
        # ── v322: 评审**独立预算**护栏 ──────────────────────────────────
        # 事故（2026-11-05）：20.08M token 只产出 43 条信号 ——
        # 同行评审（purpose=verify）每条高价值新闻跑 2 个模型、每次带完整
        # 八段 prompt（10K+ token），把**主分析**的预算吃光 → 闸门到线
        # → 主循环停摆 → 「待分析」永不下降。
        #
        # 评审是「锦上添花」，主分析才是主产出。所以评审必须有**自己的**
        # 额度上限：花完就跳过评审，主分析继续跑。
        cfgs = list(vcfgs or [])
        if not cfgs:
            return None
        try:
            from common import llm_budget as _lb_v
            _est_v = _lb_v.estimate_call_tokens(
                str(content or "")[:12000], 2000)
            _ok_v, _why_v = _lb_v.check_purpose_budget("verify", _est_v)
            if not _ok_v:
                print(f"  [PEER-SKIP] 评审跳过：{_why_v}", flush=True)
                return None
        except Exception:
            pass  # 预算模块异常 → 不拦评审
        # 主选 → 兜底串行回退：主选 think-only/异常/空票时，再试下一个候选，
        # 避免单条评审票因主选模型吐思考块而直接丢失。兜底只在主选失败后调用。
        for _idx, vcfg in enumerate(cfgs):
            _tag = "兜底" if vcfg.get("_verify_fallback") else "主选"
            try:
                vres = await _analyze_with(
                    vcfg,
                    timeout=config.PEER_REVIEW_TIMEOUT_SEC,
                    purpose="verify",
                    call_site="ai_worker_verify",
                )
                if vres and str(vres.get("suggested_action") or "").strip():
                    return _review_from(vcfg, vres)
                print(f"  [PEER-{_tag}] {vcfg.get('label')} 返回空票，尝试下一候选", flush=True)
            except Exception as _e_v:
                print(f"  [PEER-{_tag}] {vcfg.get('label')} 失败({type(_e_v).__name__})，尝试下一候选", flush=True)
        return None  # 全部候选失败不影响主结果

    async def _collect_reviews(task) -> List[Dict[str, Any]]:
        """收回评审票；评审自身异常不牵连主决策。"""
        try:
            v_results = await task
        except Exception:
            return []
        return [r for r in (v_results or []) if r]

    # ── 评审与主分析**真正并行**（2026-11-02 23:3x）─────────────────────
    # 旧实现是"先 await 主模型，成功后再 gather 评审"（注释写的是并行，
    # 代码其实串行），带来两个硬伤：
    #   ① 总耗时 = 主 + 评审（不是 max）→ 批次被拖长；
    #   ② 主模型一失败/超时走 failover，peer_reviews 直接置 None →
    #      **这条新闻的交叉验证票全丢**（实测评审填充率只有 3.6%，
    #      而抽样率是 20%；近 3h 主路径失败 47 次 ≈ 丢 47 轮票）。
    # 现在评审先起、主分析随后并行等；failover 路径同样收回评审票。
    # 主选并行；兜底候选（_verify_fallback）附到第一个主选，主选失败后串行回退。
    _v_primary = [c for c in verify_cfgs if not c.get("_verify_fallback")]
    _v_fallback = [c for c in verify_cfgs if c.get("_verify_fallback")]
    _v_groups: List[List[Dict[str, Any]]] = []
    for _i, _c in enumerate(_v_primary):
        _v_groups.append([_c] + (_v_fallback if _i == 0 else []))
    verify_task = (
        asyncio.ensure_future(asyncio.gather(*(_run_verify(g) for g in _v_groups)))
        if _v_groups else None
    )
    try:
        # 主模型单条正常 3-8s；超时独立可配（config.LLM_MAIN_TIMEOUT_SEC，
        # 默认 120s）—— HTTP 层单次尝试 60s + 网络异常重试 3 次，旧值 65s
        # 比"一次网络抖动"还短 → 整条新闻被判失败并丢掉全部评审票。
        #
        # v322: 本地兜底通道需要**更长**的外层超时。
        # 实测（2026-11-04）：本地 qwen2.5:3b 跑门卫短 prompt 要 175s，
        # 主分析的八段式 prompt 更长 —— 用默认 120s 必然被砍 → 本地兜底
        # 对主分析仍然无效。这里按通道自身 timeout 兜底（+20% 余量）。
        _outer_timeout = config.LLM_MAIN_TIMEOUT_SEC
        _cfg_timeout = float(model_cfg.get("timeout") or 0.0)
        if _cfg_timeout > _outer_timeout:
            _outer_timeout = _cfg_timeout * 1.2
        llm_result = await _analyze_with(model_cfg, timeout=_outer_timeout)
        # ── 多模型交叉验证：高价值新闻，主成功后跑验证模型（不改变主决策）──
        # 存完整分析（八段 reasoning_path + 全文结论 + 关键维度），供前端详细对比展示
        # 并行跑验证模型；验证用**独立超时**（config.PEER_REVIEW_TIMEOUT_SEC，
        # 默认 120s）—— 2026-11-02 实测完整八段式分析单次调用普遍 22-49s
        # （ai_call_stats: M3 12-30s / M2.7-highspeed 32-49s / M2.7 22-29s），
        # 原来的 20s 短超时**必然**把票全丢掉 → peer_reviews 空/一票 →
        # _consensus_mult fail-closed 回 1.0 → consensus_mult 恒为 1.0。
        if verify_task is not None:
            peer_reviews = await _collect_reviews(verify_task)
        return {
            "news_id": news_id,
            "pre_ts": pre_ts,
            "model_label": model_cfg["label"],
            "result": _apply_freshness(llm_result, pre_ts),
            "error": None,
            "peer_reviews": peer_reviews or None,
        }
    except Exception as first_exc:
        # 主模型失败 → 评审**已经在并行跑**，票要保住（否则这条新闻零评审）

        # ── Failover：主模型超时/失败 → 换池内其它模型重试 ──
        # OpenRouter 实际调用失败 → 熔断 60s（避免每条死等 65s 超时）
        if model_cfg.get("use_proxy"):
            _openrouter_circuit["until"] = time.monotonic() + 60.0
            print(
                f"  [{model_cfg['label']}] 调用失败({type(first_exc).__name__})，"
                f"OpenRouter 熔断 60s → 直接走 MiniMax",
                flush=True,
            )
        # 记录主模型失败原因（诊断：为什么 OpenRouter 探测 OK 但实际分析失败）
        try:
            _fail_file = os.path.join(config.BASE_DIR, "runtime", "llm_call_failures.jsonl")
            os.makedirs(os.path.dirname(_fail_file), exist_ok=True)
            with open(_fail_file, "a", encoding="utf-8") as _ff:
                _ff.write(json.dumps({
                    "model": model_cfg.get("id"),
                    "label": model_cfg.get("label"),
                    "error": str(first_exc)[:200],
                    "err_type": type(first_exc).__name__,
                    "ts": time.time(),
                    "time_str": datetime.now(config.TZ_SHANGHAI).strftime("%H:%M:%S"),
                }, ensure_ascii=False) + "\n")
        except Exception:
            pass
        if fallback_cfgs:
            # v422（2026-11-17）：额度/鉴权类失败**不再换通道重发**。
            # 实测 11-16 有 171 次 failover，每次都是把同一份 13.4K prompt 原样
            # 再喂给另一个模型 = 2.29M token 纯浪费；而"429 / 额度用完 / 401"
            # 这类错误换 key、换模型多半也一样失败，等于白烧第二遍。
            # 现在：识别为额度/鉴权类 → 直接把失败抛回调用方（走冷却/重新排队），
            # 只有网络抖动/超时/解析失败才走 failover。
            _err_txt = str(first_exc or "").lower()
            _quota_like = any(k in _err_txt for k in (
                "quota", "429", "额度", "insufficient", "balance",
                "401", "unauthorized", "invalid api key", "鉴权",
            ))
            if _quota_like:
                print(f"  [{model_cfg['label']}] 额度/鉴权类失败 → 不换通道重发"
                      f"（省掉一次 13.4K 重复 prompt）", flush=True)
                fallback_cfgs = []
        if fallback_cfgs:
            for fb in fallback_cfgs:
                try:
                    async with _LLM_SEMAPHORE:
                        llm_result = await asyncio.wait_for(
                            loop.run_in_executor(
                                None, _call_llm_sync, content, fb, market_context,
                                performance_context, pre_ts, writing_context,
                                # v398h：failover 只带"判断必需"的块 —— insights_section
                                # 与 reference_signals 属锦上添花，实测 failover 单次
                                # 平均 13.4K 输入（167 次/2.24M/天），丢掉这两块
                                # 每条能省约 4-5K，且不影响方向/评分依据。
                                "", "",
                                gate_context,
                                gate_target_asset, news_id, event_type_code,
                                int(news_row["gate_strength"] or 0) if "gate_strength" in news_row.keys() else 0,  # v176.5: 传给 MECH-GATE
                                str(news_row["source"] or ""),  # v176.10: 二次回灌 cross_net 需要原文来源
                                str(news_row["published_at"] or ""),  # v188：tier-3 平台首发兜底用
                                "main",                              # v187: purpose=main (failover)
                                "ai_worker_failover",                 # v187: call_site
                            ),
                            timeout=max(
                                config.LLM_MAIN_TIMEOUT_SEC,
                                float(fb.get("timeout") or 0.0) * 1.2,
                            ),
                        )
                    print(f"  [{model_cfg['label']}] 失败 → {fb['label']} 重试成功，news=#{news_id}")
                    # 主模型失败但评审已并行跑完 → 票照样落库（原来这里写死 None）
                    if verify_task is not None:
                        peer_reviews = await _collect_reviews(verify_task)
                    return {
                        "news_id": news_id,
                        "pre_ts": pre_ts,
                        "model_label": fb["label"],
                        "result": _apply_freshness(llm_result, pre_ts),
                        "error": None,
                        "peer_reviews": peer_reviews or None,
                    }
                except Exception as fb_exc:
                    # fallback 也失败（含超时）：记录原因（诊断 OpenRouter 挂起用）
                    try:
                        _fail_file = os.path.join(config.BASE_DIR, "runtime", "llm_call_failures.jsonl")
                        with open(_fail_file, "a", encoding="utf-8") as _ff:
                            _ff.write(json.dumps({
                                "model": fb.get("id"),
                                "label": fb.get("label"),
                                "error": str(fb_exc)[:200],
                                "err_type": type(fb_exc).__name__,
                                "ts": time.time(),
                                "time_str": datetime.now(config.TZ_SHANGHAI).strftime("%H:%M:%S"),
                            }, ensure_ascii=False) + "\n")
                    except Exception:
                        pass
                    continue  # 尝试下一个 fallback
        # ── 全部失败 ──
        # 评审是并行跑的 → 这里也要收回（否则任务悬挂 + 白跑的钱）
        if verify_task is not None:
            peer_reviews = await _collect_reviews(verify_task)
        if isinstance(first_exc, asyncio.TimeoutError):
            # 超时且无可用 fallback：把该条新闻退回 PENDING，下轮重新认领重试（不标死）。
            try:
                conn = _open_db()
                conn.execute(
                    "UPDATE raw_news SET retry_count = retry_count + 1,"
                    " processing_at = NULL,"
                    " status = CASE WHEN retry_count + 1 >= ? THEN 'FAILED' ELSE 'PENDING' END"
                    " WHERE id=? AND status='PROCESSING'",
                    (_MAX_PENDING_RETRIES, news_id),
                )
                conn.commit()
                conn.close()
            except Exception as _to_exc:
                # v322: 退回失败 → 这条新闻卡在 PROCESSING → 要等看门狗 15 分钟后才回收
                print(f"  [{model_cfg['label']}] ⚠️ 退回 PENDING 失败 "
                      f"(news=#{news_id}): {type(_to_exc).__name__}: "
                      f"{str(_to_exc)[:100]}", flush=True)
            print(f"  [{model_cfg['label']}] LLM 超时({int(config.LLM_MAIN_TIMEOUT_SEC)}s)，news=#{news_id} 退回 PENDING 重试")
            return {
                "news_id": news_id,
                "pre_ts": pre_ts,
                "model_label": model_cfg["label"],
                "result": None,
                "error": "LLM timeout(%ds)" % int(config.LLM_MAIN_TIMEOUT_SEC),
            }
        # 其它异常：安全 Fallback → HOLD 对象
        fallback_result = {
            "sentiment_score": 0.05,
            "suggested_action": "HOLD",
            "reasoning": f"{model_cfg['label']} 异常降级: {type(first_exc).__name__}",
            "market_category": "",
            "target_asset": "NONE",
            # 异常兜底必须自带可被 classify_analysis_degraded 识别的降级标记：
            # 否则这条 fallback 万一被落库，降级标会漏写（历史脏行 F12）。
            "reasoning_path": "异常降级兜底：模型调用未返回可用分析",
            "prediction_type": "continuation",
            "event_phase": "mid",
            "market_confirmation": "unknown",
            "expected_horizon": "1-3d",
            "invalidation_condition": "系统异常,无失效条件",
            "event_strength": "low",
            "direct_catalyst": False,
            "timeframe_match": "intraday",
            "model_label": model_cfg["label"],
            "model_id": model_cfg["id"],
        }
        return {
            "news_id": news_id,
            "pre_ts": pre_ts,
            "model_label": model_cfg["label"],
            "result": fallback_result,
            "error": f"{type(first_exc).__name__}: {str(first_exc)[:150]}",
            "peer_reviews": peer_reviews or None,
        }


# ---------------------------------------------------------------------------
# Active-trade aggregation helpers (_AGG_WINDOW_HOURS / _AGG_MIN_SCORE 见 config.py)
# ---------------------------------------------------------------------------


def _expand_multi_asset_results(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """多品种展开：result.sibling_decisions 拆成独立 decision 条目。

    AI 一次分析可给出多个受影响品种（sibling_decisions，最多 2 个），每个品种是
    独立可交易信号 → 展开成多行 decision（共享同一 reasoning_path 八段，各自
    action/score/horizon/市场类别），走同一条 INSERT/guard/建仓链路，quick_sim
    与复盘也自然按行适配。主品种条目排第一（主决策不变，主行保留
    peer_reviews 交叉验证；副品种行 reasoning 前缀 `[副品种 ASSET]` 标记，
    前端据此聚合展示与识别主/副行）。

    result 结构：{news_id, model_label, pre_ts, peer_reviews?, result: {...}}
    每个条目 result 含 20 字段主结构 + 可选 sibling_decisions。
    """
    expanded: List[Dict[str, Any]] = []
    for _s in results or []:
        _res = _s.get("result") or {}
        _sibs = _res.get("sibling_decisions") or []
        if not isinstance(_sibs, list) or not _sibs:
            expanded.append(_s)  # 无副品种：原样
            continue
        _main = dict(_s)  # 主条目（去掉 sibling 字段，避免重复解析）
        _main_result = dict(_res)
        _main_result.pop("sibling_decisions", None)
        _main["result"] = _main_result
        expanded.append(_main)
        _main_asset = str(_res.get("target_asset") or "NONE").upper().strip()
        # ── 前置条件：主品种必须"立住"，副品种才谈得上（用户原则：
        #    "先有主品种，后面才会有副品种。主品种都没有，何来副品种。"）
        #    主品种是 NONE（无可交易标的）或已被针对性闸门降级（八段没分析它）
        #    → 这次分析连"因"都没立住，副品种的传导链无从谈起，方向单一律降级。
        _main_ok, _main_block_reason = _main_established(_res, _main_asset)
        for _sib in _sibs[:2]:  # 最多 2 个副品种
            if not isinstance(_sib, dict):
                continue
            _sib_asset = str(_sib.get("target_asset") or "NONE").upper().strip()
            if not _sib_asset or _sib_asset in ("NONE", "") or _sib_asset == _main_asset:
                continue  # 跳过无品种/与主品种重复
            # 副品种独立 result：共享 reasoning_path，字段取自 sibling；
            # score/action 走与主行同款规范化（clamp/HOLD归零/0分兜底）
            _sib_score, _sib_action = _norm_score_action(
                _sib.get("sentiment_score", 0.0),
                _sib.get("suggested_action") or "HOLD",
            )
            _sib_result = {
                "reasoning_path": _res.get("reasoning_path", ""),
                "sentiment_score": _sib_score,
                "suggested_action": _sib_action,
                "reasoning": f"[副品种 {_sib_asset}] " + str(_sib.get("reasoning") or ""),
                "market_category": str(_sib.get("market_category") or "").upper(),
                "target_asset": _sib_asset,
                # 关键：带上主分析的真实模型标记。
                # 副品种与主品种共享同一份 analysis（reasoning_path 就是主模型产出的），
                # 但此前没带 model_id → 入库时落到 active_run["model_id"] 兜底，
                # 而那个值是建运行时的旧模型（实测遗留 gpt-5.4-mini/nassaapi，已 403 废弃）
                # → 副品种行的"分析模型"显示错误。
                "model_id": _res.get("model_id") or _s.get("model_id") or "",
                "model_label": _s.get("model_label"),
                "prediction_type": _sib.get("prediction_type", "continuation"),
                "event_phase": _sib.get("event_phase", "mid"),
                "market_confirmation": _sib.get("market_confirmation", "unknown"),
                "expected_horizon": _sib.get("expected_horizon", "1-3d"),
                "invalidation_condition": _sib.get("invalidation_condition", ""),
                "event_strength": _sib.get("event_strength", "medium"),
                "direct_catalyst": _sib.get("direct_catalyst", False),
                "heat_score": _sib.get("heat_score", 5.0),
                "impact_score": _sib.get("impact_score", 5.0),
                "asset_impact": _sib.get("asset_impact", 5.0),
            }
            # ── 副品种同样过针对性闸门 ──
            # 此前闸门只在主分析里跑，副品种展开后直接建仓：八段里没有该品种的
            # 独立分析（实测多数副品种 0 段）也照样给 BUY/SELL——宽松模式下就是
            # 真建仓。规则与主品种一致：无针对该品种的分析 → 不给方向单。
            # 更前置的一条：主品种没立住（NONE/已被降级）→ 副品种直接降级，
            # 连"八段有没有它"都不必再看——因没立住，果无从谈起。
            _sib_hits = _asset_mentioned_stage_count(
                str(_sib_result.get("reasoning_path") or ""), _sib_asset)
            _sib_dg, _ = _focus_downgrade(
                _sib_result, _sib_asset, role="副品种",
                reason=(f"{_main_block_reason}，故副品种 {_sib_asset} 不给方向单"
                        if not _main_ok else ""),
            )
            if _sib_dg:
                print(
                    f"  [FOCUS-GATE] 副品种 {_sib_asset} 方向单降级 HOLD（"
                    + (_main_block_reason if not _main_ok
                       else f"分析无针对性，仅 {_sib_hits} 段提及")
                    + "）",
                    flush=True,
                )
            # ── 副品种同样过传导机制闸门 ──
            # 用户原则："有关系 = 新闻内容会影响它的价格波动"。副品种若说不出
            # "事件 → 它自己的价格"的传导链，就只是被顺带点名 → 不给方向单。
            if _mechanism_downgrade(_sib_result, _sib_asset):
                print(
                    f"  [MECH-GATE] 副品种 {_sib_asset} 未写出传导链"
                    f" → 方向单降级 HOLD（影响不明确，不下单）",
                    flush=True,
                )
            expanded.append({
                "news_id": _s["news_id"],
                "model_label": _s["model_label"],
                "pre_ts": _s["pre_ts"],
                "peer_reviews": None,  # 交叉验证只做主品种
                "is_sibling": True,
                "result": _sib_result,
            })
    return expanded


# ── v398c: 进度心跳线程（与事件循环解耦）──────────────────────────────
# 背景（2026-11-16 实测）：今日引擎被拉起 28 次，决策 id 在重启点前后出现
# 回退（11946-11949 写入后被抹掉，表回到 11903）。查证结果：
#   · 看门狗 scripts/engine_watchdog.py --threshold 600 只看
#     runtime/ai_worker_progress.json 的 mtime/ts；
#   · 原来这个文件只由主协程（以及批内协程 _batch_heartbeat）写，
#     一旦事件循环被同步调用卡住（同步 requests、sqlite 等锁 20s、DNS），
#     心跳同时停摆 → 600s 后被判定"挂死" → kickstart -k 强杀；
#   · 强杀发生在批次中途，本批已落库/待落库的行随事务回滚一起消失。
# 这里改成**独立 daemon 线程**每 45s 写一次心跳：线程不受事件循环阻塞影响，
# 只要进程活着、主循环还在推进（step 有变化），看门狗就不会误杀。
_PROGRESS_STATE: Dict[str, Any] = {"step": "loop", "step_ts": time.time(), "thread_started": False}


def _start_progress_thread() -> None:
    """启动心跳线程（幂等）。每 45s 落一次 progress，与事件循环无关。"""
    if _PROGRESS_STATE.get("thread_started"):
        return
    _PROGRESS_STATE["thread_started"] = True

    def _heartbeat() -> None:
        _path = os.path.join(config.BASE_DIR, "runtime", "ai_worker_progress.json")
        while True:
            try:
                with open(_path, "w", encoding="utf-8") as _pf:
                    json.dump({
                        "step": _PROGRESS_STATE.get("step") or "loop",
                        "ts": time.time(),
                        "time_str": datetime.now(config.TZ_SHANGHAI).strftime("%H:%M:%S"),
                        "step_ts": _PROGRESS_STATE.get("step_ts") or time.time(),
                        "heartbeat": "thread",
                    }, _pf, ensure_ascii=False)
            except Exception:
                pass
            time.sleep(45.0)

    try:
        import threading as _th
        _th.Thread(target=_heartbeat, name="progress-heartbeat", daemon=True).start()
        print("[PROGRESS] 心跳线程已启动（45s/次，与事件循环解耦）", flush=True)
    except Exception as _hb_exc:
        print(f"[PROGRESS] 心跳线程启动失败: {type(_hb_exc).__name__}", flush=True)


async def ai_worker(loop: asyncio.AbstractEventLoop) -> None:
    """
    Concurrent batch processor.

    Every 1 second:
      1) Atomically claim up to BATCH_SIZE PENDING rows (BEGIN IMMEDIATE)
      2) Fire all LLM calls concurrently via asyncio.gather
      3) Persist successes (-> DONE) and failures (-> FAILED) in one transaction.

    Head-of-line blocking is eliminated: 10 items complete in ~2-3 s
    instead of 10 * 2 s = 20 s serial.
    """

    idle_ticks = 0  # heartbeat counter when no PENDING data

    # v398c：任务一被调度就起心跳线程，不等进主循环。
    # 实测（2026-11-16 19:04 重启）引擎在启动期同步拉取行情/日历（主线程卡在
    # SSL 握手 400+ 采样），progress 文件十几分钟不更新 → 看门狗按"挂死"强杀，
    # 而 ai_worker 主循环还没跑起来，线程版心跳也就一直没起。
    _start_progress_thread()

    # 启动即落盘模型池（诊断：不依赖主循环是否卡住，引擎启动后即可查看实际通道构成）
    try:
        _pool_file = os.path.join(config.BASE_DIR, "runtime", "model_pool.json")
        os.makedirs(os.path.dirname(_pool_file), exist_ok=True)
        _startup_pool = _build_model_pool()
        with open(_pool_file, "w", encoding="utf-8") as f:
            json.dump({
                "pool": [m["id"] for m in _startup_pool],
                "labels": [m.get("label", m["id"]) for m in _startup_pool],
                "ts": time.time(),
            }, f, ensure_ascii=False)
        print(f"[AI] 启动模型池: {[m['id'] for m in _startup_pool]}", flush=True)
        # v342: 门卫假冗余自检（"面板两条通道、实际一份配额"必须吵出来）
        try:
            from review import gate_pool_health as _gph
            _gr_rep = _gph.gate_pool_report(list(config.AI_MODEL_ROSTER))
            if _gr_rep.get("warning"):
                print(f"[AI] ⚠️  {_gr_rep['warning']}", flush=True)
            else:
                print(f"[AI] 门卫通道 {_gr_rep['channels']} 条 / 独立 key "
                      f"{_gr_rep['distinct_keys']} 个 ✅", flush=True)
        except Exception:
            pass
    except Exception as _pool_exc:
        print(f"[AI] 模型池落盘失败: {type(_pool_exc).__name__}: {str(_pool_exc)[:100]}", flush=True)

    # v321 启动自检：门卫回放自愈必须可用。导入失败要**立刻**报出来，
    # 否则会重演「759 条冻结、自愈从未运行、却没人发现」的历史事故。
    if _load_gate_replay_guard() is None:
        print("[gate-replay] 致命：启动自检失败 —— gate_replay_guard 无法导入，"
              "门卫自愈不会运行（冻结新闻将永久滞留）", flush=True)
    else:
        print("[gate-replay] 启动自检通过：门卫回放自愈已就位"
              "（每 %ds 一轮，每轮最多 %d 条）"
              % (int(_GATE_REPLAY_INTERVAL_S), _GATE_REPLAY_BATCH), flush=True)

    # 启动时把上次异常退出残留的 PROCESSING 新闻重置为 PENDING（否则永久卡死不被认领）
    try:
        conn = _open_db()
        # v415：重置前先"捞回"已经写完决策的那部分。
        # 事故（2026-11-17 实测）：引擎反复重启 → 启动即把 PROCESSING 全退回 PENDING。
        # 但其中一部分新闻**决策行已经落库了**（同批里先后 INSERT，落库中途被杀），
        # 退回 PENDING 后会被重新认领、重新分析 → 白烧一轮额度，还会产出重复决策。
        # 实测当日就有 10 条「已有决策但状态是 PENDING」的行在排队等着被重跑。
        # 现在：有决策行的直接标 DONE（分析成果已落库，没有重跑的理由），
        # 只有真正没落成决策的才退回队列。
        # v424：先把上次被杀时已分析完的结果补写进决策表
        # （补完下面的 _STARTUP_RECOVER_DONE_SQL 会把这些 raw_news 直接标 DONE）
        _spool_ok = False
        try:
            _spool_drain(conn)
            _spool_ok = True
        except Exception as _sp_exc:
            print(f"[AI] ⚠️ spool 补写异常（不致命）: {type(_sp_exc).__name__}: "
                  f"{str(_sp_exc)[:100]}", flush=True)
        # v409.4: 启动就清一次「产出已恢复」的历史停摆告警（否则 quota-sentinel
        # 每 5 分钟报一次 DLQ 未解决=3，把真告警埋了）。
        try:
            _auto_resolve_recovered_stalls(conn)
        except Exception:
            pass
        _recovered = conn.execute(_STARTUP_RECOVER_DONE_SQL).rowcount
        _reset = conn.execute(_STARTUP_RESET_PENDING_SQL).rowcount
        # v415：把「PENDING 但决策已落库」的僵尸扶正（重放模式下跳过）
        _fixed = 0 if _ALLOW_REANALYZE else conn.execute(
            _STARTUP_FIX_PENDING_WITH_DECISION_SQL).rowcount
        conn.commit()
        conn.close()
        if _spool_ok:
            _spool_clear()
        if _recovered or _reset or _fixed:
            print(f"[AI] 启动复位：{_recovered} 条已有决策 → DONE（避免重跑），"
                  f"{_reset} 条未落库 → PENDING，"
                  f"{_fixed} 条 PENDING 但已落库 → DONE", flush=True)
    except Exception as _reset_exc:
        # v322: **不能静默** —— 这段失败 = 上次异常退出的 PROCESSING 僵尸永不复位
        # → 那批新闻永远不被认领 → 「待分析」不降（与 10.23 的 CHECK 事故同款）。
        # v432（2026-11-17）：写锁常被其它后台任务占住 → 单次失败很常见，
        # 但失败一次就意味着僵尸永久卡在 PROCESSING，所以必须重试。
        print(f"[AI] ⚠️ 启动重置 PROCESSING 失败: {type(_reset_exc).__name__}: "
              f"{str(_reset_exc)[:120]}（重试中）", flush=True)
        try:
            if 'conn' in dir() and conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
        except Exception:
            pass
        for _attempt in (1, 2, 3):
            try:
                time.sleep(3.0 * _attempt)
                _rc = _open_db()
                try:
                    _rc.execute("PRAGMA busy_timeout=60000;")
                except Exception:
                    pass
                _recovered2 = _rc.execute(_STARTUP_RECOVER_DONE_SQL).rowcount
                _reset2 = _rc.execute(_STARTUP_RESET_PENDING_SQL).rowcount
                _fixed2 = 0 if _ALLOW_REANALYZE else _rc.execute(
                    _STARTUP_FIX_PENDING_WITH_DECISION_SQL).rowcount
                _rc.commit()
                _rc.close()
                print(f"[AI] 启动重置重试成功（第 {_attempt} 次）："
                      f"{_recovered2} 条→DONE，{_reset2} 条 PROCESSING→PENDING，"
                      f"{_fixed2} 条 PENDING 但已落库→DONE", flush=True)
                break
            except Exception as _r2e:
                print(f"[AI] 启动重置重试第 {_attempt} 次仍失败: "
                      f"{type(_r2e).__name__}: {str(_r2e)[:80]}", flush=True)

    # v321: 门卫回放自愈的节流状态（函数作用域，主循环内使用）。
    # 初始 0 → 引擎启动后第一轮就会跑一次回放（把通道故障期冻结的数据解冻）。
    _last_gate_replay_ts: float = 0.0
    # v321.2: 回放后台任务句柄（单飞：上一轮没跑完就不开新一轮）
    _gr_holder: Dict[str, Any] = {}
    # v322: "跳过认领"日志的节流时间戳（每 60 秒最多一行）
    _last_pool_block_log_ts: float = 0.0
    # v322: 通道体检结果的节流时间戳（每 60 秒最多一行）
    _last_pool_check_log_ts: float = 0.0
    # v322: 超龄归档的节流时间戳（每 60 秒最多跑一次）
    _last_archive_ts: float = 0.0
    # v434（v413 P0-A1）：熔断干等期间的现场状态 —— 原来 progress 里 step 恒为
    # "pool-blocked"，看不出「第几轮、等了多久、还要等多久」，11-17 那次 11,400s
    # 空等就是这样被藏住的。这里只做记录与展示，不改变任何决策行为。
    _pool_blocked_since: float = 0.0
    _pool_blocked_round: int = 0

    # 失败冷却: snapshot 连续 DOWN 后 5 分钟内不重试, 减少无意义等待
    _SNAPSHOT_COOLDOWN_S = 300
    _last_snapshot_down_ts: float = 0.0
    # 最近一次成功快照缓存（复用）：快照拉取超时/失败/冷却时，若缓存 ≤300s 内
    # （与冷却同长——代理全挂期间也始终有数据）成功过，用缓存数据让 AI 仍有盘面
    # 锚点。宏观/趋势判断对 5 分钟内的价格滞后不敏感；带"Xs 前缓存"标注提醒模型。
    # 避免"代理整体不可用 → 整批无快照 → 水位/跨资产段无价格可写"。
    _SNAPSHOT_CACHE_TTL_S = 600
    _last_good_snapshot: Dict[str, Any] = {"ts": 0.0, "summary": "", "json": "{}"}
    # macro_context（FRED/盘面结构/爆仓）同样：失败冷却期用上次成功摘要，不让 AI 丢数据层
    _MACRO_CACHE_TTL_S = 300
    _last_good_macro: Dict[str, Any] = {"ts": 0.0, "summary": ""}

    # 失败冷却: 宏观外部源(盘面/舆情/Fed/资金流)串行请求在弱网/代理下可累计数分钟,
    # 加整体 20s 超时 + 10 分钟冷却, 避免拖垮每轮 AI 分析节奏。
    _MACRO_COOLDOWN_S = 600
    _last_macro_down_ts: float = 0.0

    # 历史 K 线知识库：快照 ≤30min 复用（历史 K 线分钟级不变）；persist 失败/冷却 5min
    _KLINE_CACHE_TTL_S = 1800
    _KLINE_COOLDOWN_S = 300
    _last_kline_block: Dict[str, Any] = {"ts": 0.0, "block": ""}
    _last_kline_down_ts: float = 0.0

    while True:
        await asyncio.sleep(1)

        # 主循环进度落盘（诊断：定位引擎卡在哪一步）
        # v398c：心跳改由**独立线程**写（见下方 _start_progress_thread），
        # 协程版 _batch_heartbeat 依赖事件循环 —— 事件循环一旦被同步调用
        # （同步 requests / sqlite 等锁 20s / DNS）卡住，60s 心跳也跟着停，
        # 看门狗 600s 判挂 → kickstart → 正在跑的整批连同已 INSERT 的行一起没了。
        # 实测今日 28 次启动、决策时间戳在重启点前后被回退，就是这个形态。
        _PROGRESS_STATE["step"] = _PROGRESS_STATE.get("step") or "loop"
        def _log_progress(step: str, **extra: Any) -> None:
            # v398c：step 只在真正换步时刷新 step_ts；但 "loop" 表示主循环空转
            # （没有 PENDING 新闻），空转本来就该一直被看作健康 —— 必须每轮刷新，
            # 否则看门狗会把「闲着的引擎」当成「卡死的引擎」在 30 分钟后杀掉。
            # v434：extra 用于把「等了多久、还要等多久」这类现场信息一并落盘，
            # 不改 step 名（看门狗的 pool-blocked 判据是精确匹配，改名前缀会误判）。
            if step != _PROGRESS_STATE.get("step") or step == "loop":
                _PROGRESS_STATE["step_ts"] = time.time()
            _PROGRESS_STATE["step"] = step
            try:
                _progress_file = os.path.join(config.BASE_DIR, "runtime", "ai_worker_progress.json")
                with open(_progress_file, "w", encoding="utf-8") as _pf:
                    json.dump({
                        "step": step,
                        "ts": time.time(),
                        "time_str": datetime.now(config.TZ_SHANGHAI).strftime("%H:%M:%S"),
                        "step_ts": _PROGRESS_STATE.get("step_ts") or time.time(),
                        **extra,
                    }, _pf, ensure_ascii=False)
            except Exception:
                pass

        _log_progress("loop")
        _start_progress_thread()

        # -- v323 P0-1: 批内心跳（防止看门狗误杀在跑的批次）---------------
        async def _batch_heartbeat() -> None:
            """批处理期间每 60s 写一次 progress。

            看门狗（scripts/engine_watchdog.py --threshold 600）按 progress 文件
            判活：超过 10 分钟没更新就 kickstart 掉引擎。而 progress 原来
            **只在批边界写**（批开始 -> 批结束），一批 16 条的 wall time 可远超
            10 分钟 → 看门狗把引擎连同在跑的批次一起杀掉 → 决策永远出不来
            （实测 engine_watchdog_state.json: restart_count=707）。
            现在批内每 60s 写一次，看门狗看得到"还在推进"，不再误杀。
            """
            _ticks = 0
            while True:
                await asyncio.sleep(60.0)
                _ticks += 1
                try:
                    _log_progress("analyze")
                except Exception:
                    pass
                # 最多 30 分钟，防止批次异常提前 return 时 ticker 泄漏
                if _ticks >= 30:
                    return

        # ── 自愈看门狗：回收卡死的 PROCESSING ──
        # 按"认领时间 processing_at"判断（非新闻入库 ts）——claim 超过 15 分钟仍未
        # 完成的视为卡死（正常批几秒~几分钟），回滚 PENDING 下轮重试，无需人工重启；
        # 积压老新闻 claim 后 processing_at 是新时间，不会被误杀。
        try:
            _reclaim_conn = _open_db()
            # v322：ingest 的长写事务几乎持续占锁 → 原来 5s busy_timeout 一撞锁就
            # 整轮放弃（而且 except: pass 静默吞掉）→ PROCESSING 僵尸永不回收 →
            # retry_count 永远 0 → 队头那 16 条反复霸占批次 → 「待分析」永不下降。
            # 现在：30s 超时 + retry_on_busy 重试 + 失败必须留下日志（不再静默）。
            try:
                _reclaim_conn.execute("PRAGMA busy_timeout=30000;")
            except Exception:
                pass

            def _do_reclaim():
                # v322: 跳过本进程「正在处理」的批次（租约未过期）。
                # 单批耗时可能远超 15 分钟（行情源全超时 → 实测 42 分钟），
                # 看门狗若把在办批次当僵尸回收，批事务末尾
                # `UPDATE ... WHERE status='PROCESSING'` 会匹配 0 行 →
                # 一条都进不了 DONE → 队头那批被无限重认领 → 「待分析」永不下降。
                # 租约过期（心跳停了 = 批次确实卡死/已死）→ 不再排除 → 正常回收。
                _skip = _active_batch_live_ids()
                _sql, _params = _build_reclaim_sql(_skip)
                _c = _reclaim_conn.execute(_sql, _params)
                _n = _c.rowcount or 0
                _reclaim_conn.commit()
                return _n

            _reclaimed = db.retry_on_busy(_do_reclaim)
            if _reclaimed:
                print(f"  [WATCHDOG] 回收卡死 PROCESSING {int(_reclaimed)} 条 → "
                      f"retry_count+1（达 {_MAX_PENDING_RETRIES} 标 FAILED）", flush=True)
            _reclaim_conn.close()
        except Exception as _reclaim_exc:
            # 不再静默：看门狗失效是「待分析」永不下降的直接原因
            print(f"  [WATCHDOG] 回收失败（DB 忙？）: {type(_reclaim_exc).__name__}: "
                  f"{str(_reclaim_exc)[:120]}", flush=True)

        # ── v322 自愈：门卫冻结过期自动放行（根治「待分析」只增不减）──────
        # 背景（2026-11-04 实测）：门卫通道 key 401 失效（invalid api key 2049）
        # 持续数小时 → 每条新入库新闻都被打 gate_unavailable=1 冻结；
        # 主分析认领 SQL 明确排除该标记，回放流程又因通道同样 401 而永远补判不了
        # → 324 条新闻永久冻结，前端「待分析」只涨不跌。
        #
        # 语义修正：**冻结必须有保质期**。超过阈值仍未被回放的冻结新闻，
        # 降级放行（gate_unavailable=0 + retry_count=0）——
        #   · 宁可让主分析在缺门卫标签下处理（决策层对缺门卫有既有兜底路径，
        #     见 gate_verdict_map 为空时走 STRONG-PASS），也不能让数据无限期躺着；
        #   · 通道恢复后，gate_replay_guard 仍会给未回放的行补判（它按 gate_unavailable
        #     取行，而这里已经把标记清了 → 不会重复处理，数据不冲突）。
        #
        # 阈值 30 分钟：足够覆盖门卫瞬时抖动（那类量级只有个位数），
        # 又不会让系统性故障（小时级）造成长期积压。
        try:
            _unfreeze_conn = _open_db()
            _unfreeze_cur = _unfreeze_conn.execute(
                # FIX-8b: 冻结超龄后必须"真的放行" → gate_unavailable=2。
                # 写 0 等于告诉主分析"门卫正常判定为无信号" → 被 FILTERED，放了又拦。
                "UPDATE raw_news SET gate_unavailable = 2, retry_count = 0"
                " WHERE COALESCE(gate_unavailable, 0) = 1"
                "   AND status = 'PENDING'"
                "   AND COALESCE(is_noise, 0) = 0"
                "   AND ts IS NOT NULL"
                "   AND ts < strftime('%s','now') - ?",
                (_GATE_FREEZE_TTL_S,),
            )
            _unfrozen = _unfreeze_cur.rowcount or 0
            _unfreeze_conn.commit()
            _unfreeze_conn.close()
            if _unfrozen > 0:
                print(
                    "[gate-unfreeze] 冻结过期自动放行 %d 条（避免「待分析」永久积压）"
                    % _unfrozen, flush=True,
                )
        except Exception as _uf_exc:
            # v322: 放行失败 → 门卫冻结的新闻永远卡住 → 「待分析」不降（事故同款）
            print(f"[gate-unfreeze] ⚠️ 冻结过期放行失败: {type(_uf_exc).__name__}: "
                  f"{str(_uf_exc)[:120]}", flush=True)

        # ── v321 自愈：自动回放「门卫未判定」的冻结新闻 ──
        # 背景：门卫(LLM)通道故障期间入库的新闻带 gate_unavailable=1，
        # 主分析不认领它们（_batch_claim 排除该标记）→ 永久冻结 → 前端
        # 永远显示「待 AI 分析」。原先只有人工跑 replay_deferred_gate.py
        # 才能解冻，而它**从未被任何调度器调用**（实测冻结 759 条）。
        # 这里接入引擎主循环：引擎本来就在跑，复用进程，不新增常驻进程。
        # 节流：默认 5 分钟一轮，且通道健康预检不通过就跳过（不消耗额度）。
        #
        # ⚠️ v321.2: 回放必须放**后台任务** —— 旧实现 `await asyncio.to_thread(...)`
        #    只是把阻塞调用挪到工作线程，主循环协程仍 await 在这一句上，
        #    Phase 1 认领被推迟到整轮回放结束（实测 11-12 分钟）→ 主分析饿死。
        #    现在 create_task + 单飞：主循环立即继续，回放并行跑。
        try:
            _gr_now = time.time()
            if (_gr_now - _last_gate_replay_ts) >= _GATE_REPLAY_INTERVAL_S:
                _last_gate_replay_ts = _gr_now
                _gr_once = _load_gate_replay_guard()
                if _gr_once is None:
                    # 绝不能静默：guard 导入失败 = 自愈彻底失效（历史 bug 根因）
                    print("[gate-replay] 致命：gate_replay_guard 导入失败，"
                          "自愈未生效（冻结新闻不会被解冻）", flush=True)
                else:
                    _gr_task_new = schedule_gate_replay(_gr_once, _gr_holder)
                    if _gr_task_new is None:
                        print("[gate-replay] 上一轮仍在后台运行，本轮跳过（单飞）", flush=True)
                    else:
                        print("[gate-replay] 已转入后台执行（不阻塞主分析认领）", flush=True)
        except Exception:
            pass

        # ==================================================================
        # Phase 1 - Batch atomic claim
        # ==================================================================
        def _batch_claim() -> List[sqlite3.Row]:
            """Phase 1 原子认领: PENDING → PROCESSING.

            v174 修复: 用「单 UPDATE + RETURNING」替代 BEGIN IMMEDIATE 包裹,
            消除与 ingest worker 长 INSERT 事务的持锁竞争.
            - 旧逻辑(BEGIN IMMEDIATE): 先开事务,在 ingest 长事务未提交期间
              busy_timeout 30s 后 SQLITE_BUSY → OperationalError,30s 浪费.
            - 新逻辑: 直接单条 UPDATE 自动拿写锁直到语句结束,RETURNING 拿回
              完整行（SQLite 3.35+ 支持, 与 ai_worker 其它位置一致）.
            """
            import time as _bt
            import traceback as _bt_stk
            # v431（2026-11-17）：主分析日预算 —— 第二道防烧穿闸。
            # 实测当日 2836 次调用里门卫吃掉 2473 次（96%）、main 只 167 次；
            # 门卫预算（REALTIME_GATE_DAILY_BUDGET=300）已把大头掐掉，
            # 这里再给 main 兜一道上限，防止某个环节失控又把配额烧空。
            # 超出后认领直接返回空 → 主循环空转等待，新闻留在 PENDING 不会丢。
            _mb = int(os.getenv("AI_MAIN_DAILY_BUDGET", "600") or 600)
            if _mb > 0:
                try:
                    _bc = _open_db()
                    _bn = _bc.execute(
                        "SELECT COUNT(*) FROM ai_call_stats WHERE purpose='main'"
                        " AND ts>=strftime('%s','now','localtime','start of day','utc')"
                    ).fetchone()[0]
                    _bc.close()
                    if int(_bn) >= _mb:
                        return []
                except Exception:
                    pass
            _t0 = _bt.time()
            conn = _open_db()
            # v176.2: 临时 busy_timeout 8s → 30s 跟全局对齐, 避免长 ingest
            # 持锁(>8s)时 _batch_claim 仍报 SQLITE_BUSY. ingest 的写入时间
            # 通常 5-15s, 30s 才有足够冗余;同时全局 db.py/utils.py 已是 30s.
            # v189-8: 30s → 5s 与 db.py / engine/utils.py 对齐, 配合 retry_on_busy
            # 重试,_batch_claim 单次最坏阻塞 5s + 重试退避 ≈ 5.4s 兜底.
            # v431（2026-11-17）：这里原来写死 5s，会把 db.py 全局的 20s 覆盖掉。
            # 实测 1 小时 271 次 `database is locked`，多数就发生在认领这一句上
            # （ingest 侧的写事务还没提交完）。统一到 20s：抢不到就等，不要立刻失败。
            try:
                conn.execute("PRAGMA busy_timeout=20000;")
            except Exception:
                pass
            # v189-8: 包 retry_on_busy,UPDATE RETURNING 拿锁失败时 5s+退避重试,
            # 消除 14 次 [BATCH] FAILED in 36-74s (engine 旧版 60s 死等浪费).
            # 配合 busy_timeout=5s,单次最坏阻塞 ≈ 5.4s,失败即抛给外层打印.
            def _do_claim():
                # v322: 认领 SQL 带**时效门槛**（陈旧新闻不送去分析）
                # v432: 窗口内有「新鲜可交易」→ 只认领 1 条，插队直跑
                _fresh_tradeable = _has_fresh_tradeable(conn)
                # v450（2026-11-18）：可交易插队**只认领 1 条**是吞吐黑洞 ——
                # 实测 21:25 有 122 条可交易待分析，主分析每轮只跑 1 条 → 约 15 条/小时，
                # 而入库 380 条/小时，队列只会越涨越高。
                # 改成小批插队（默认 8，可用 AI_TRADEABLE_BATCH 调），保持"优先"但不堵死吞吐。
                try:
                    _tb = max(1, int(os.getenv("AI_TRADEABLE_BATCH", "8") or 8))
                except Exception:
                    _tb = 8
                _sql_claim, _params_claim = _build_claim_sql(_tb if _fresh_tradeable else None)
                if _fresh_tradeable:
                    print(f"  [BATCH] 检测到建仓窗口内的可交易行 → 小批插队直跑({_tb} 条)", flush=True)
                # v322: 归档已移到主循环的独立步骤（_run_expire_stale_news）。
                # 不再放在这里的原因：归档与认领共用一个事务时，
                # 归档 UPDATE 一旦撞上 DB 锁 → 整批认领一起失败（实测
                # "[BATCH] FAILED in 57.88s: database is locked"）。
                rows = conn.execute(
                    _sql_claim,
                    _params_claim,
                ).fetchall()
                # v406: 分析前范围前置过滤 —— 只丢「确定性越界」（桶标签/个股特征/
                # category=股票），no_evidence 一律保留。同事务内完成，原子。
                # 当天实测 gate:main = 14:1、92 条 PENDING 压着 → 这里省的是排队时间。
                # 默认关闭（抽查发现 stock_hints 档会误杀白银/宏观日历）；
                # 收紧判据并抽查通过后再用 ANALYSIS_PREFILTER=1 打开。
                try:
                    from analysis.prefilter import apply as _prefilter_apply
                    rows = _prefilter_apply(conn, rows)
                except Exception as _pf_exc:
                    print(f"  [PREFILTER] 跳过（{type(_pf_exc).__name__}: {str(_pf_exc)[:80]}）", flush=True)
                conn.commit()
                return rows
            try:
                rows = db.retry_on_busy(_do_claim)
                if rows:
                    ids = [r["id"] for r in rows]
                    print(f"  [BATCH] claimed {len(rows)} rows in {_bt.time()-_t0:.2f}s (ids={ids[:5]})", flush=True)
                return rows
            except Exception as _claim_exc:
                try:
                    conn.rollback()
                except Exception:
                    pass
                _dt = _bt.time()-_t0
                print(f"  [BATCH] FAILED in {_dt:.2f}s: {type(_claim_exc).__name__}: {str(_claim_exc)[:100]}", flush=True)
                # v174 诊断: 长 lock 时 dump 所有活跃线程栈 + 列出持锁者
                if _dt > 5.0:
                    try:
                        import sys
                        import threading as _thr
                        # os 已在模块顶部导入；此前这里二次 import 会让 os 变成函数局部变量，
                        # 使本函数前半段 os.getenv(AI_MAIN_DAILY_BUDGET) 抛 UnboundLocalError。
                        def _w(s):
                            try: os.write(1, (s + "\n").encode())
                            except Exception: pass
                        _w(f"  [BATCH-DIAG] === ALL THREAD STACKS (lock blocked {_dt:.1f}s) ===")
                        for tid, frame in sys._current_frames().items():
                            _w(f"  [BATCH-DIAG] Thread {tid} (name={next((t.name for t in _thr.enumerate() if t.ident==tid), '?')}):")
                            for ln in _bt_stk.format_stack(frame, limit=15):
                                _w("    " + ln.rstrip())
                        _w(f"  [BATCH-DIAG] active threads: {_thr.active_count()}")
                    except Exception as _diag_err:
                        print(f"  [BATCH-DIAG] dump failed: {_diag_err}", flush=True)
                return []
            finally:
                conn.close()

        # ==================================================================
        # Phase 0 — Pull market snapshot (once per batch, before LLM calls)
        # ==================================================================

        market_context = ""
        decision_context = "{}"  # 完整快照 JSON, 供 Hermes 复盘
        now_ts = time.time()
        snap_ok_now = False
        # v323：快照每批重拉一次要 10–28 秒（外网全挂时在等超时），
        # 而单批总共才 78 秒 → 光这一项就吃掉 1/4 吞吐。
        # 本地 market_ticks 兜底后价格本来就是秒级新鲜的，
        # 缓存在 _SNAPSHOT_CACHE_TTL_S 内直接复用，别让每批都等一遍。
        _cached_age = now_ts - float(_last_good_snapshot.get("ts", 0.0) or 0.0)
        if _last_good_snapshot.get("summary") and _cached_age <= _SNAPSHOT_CACHE_TTL_S:
            market_context = str(_last_good_snapshot.get("summary", ""))
            decision_context = str(_last_good_snapshot.get("json", "{}"))
            snap_ok_now = True
            print(f"  [SNAPSHOT] 复用 {int(_cached_age)}s 前快照（跳过重复拉取）", flush=True)
        elif now_ts - _last_snapshot_down_ts > _SNAPSHOT_COOLDOWN_S:
            try:
                # 行情快照必须加超时：外部行情源挂起时会拖死整个 AI 主循环
                # （症状：PROCESSING 卡住、零分析产出、progress 停在 loop）
                snap = await asyncio.wait_for(get_snapshot(), timeout=15.0)
                if snap.get("status") == "down" or not snap.get("summary"):
                    raise RuntimeError("snapshot down/empty")
                market_context = snap.get("summary", "")
                decision_context = json.dumps(snap, ensure_ascii=False)
                # 成功 → 更新缓存
                _last_good_snapshot.update({"ts": now_ts, "summary": market_context, "json": decision_context})
                # 同步到模块级 cache（让嵌套 _do_api_call 注入 SERVER VERDICT 时能读到完整 snap）
                global _SERVER_SNAPSHOT_CACHE
                _SERVER_SNAPSHOT_CACHE.update({"ts": now_ts, "summary": market_context, "json": decision_context})
                snap_ok_now = True
                if market_context:
                    print(f"\n  [SNAPSHOT] {snap['status'].upper()} | "
                          f"BTC={snap['assets']['BTC'].get('price_str','?')} | "
                          f"XAU={snap['assets']['XAU'].get('price_str','?')}")
            except Exception as e:
                print(f"  [SNAPSHOT] 获取失败: {type(e).__name__}: {str(e)[:80]}")
                _last_snapshot_down_ts = now_ts
        else:
            # 冷却中, 跳过本轮 snapshot 请求
            remaining = _SNAPSHOT_COOLDOWN_S - int(now_ts - _last_snapshot_down_ts)
            if int(now_ts) % 60 == 0:  # 每分钟只打一次
                print(f"  [SNAPSHOT] 跳过 (冷却中, {remaining}s 后重试)")

        # 拉取失败/冷却 → 尝试复用 ≤60s 前的成功快照（价格几十秒内变化可忽略）
        if not snap_ok_now:
            cache_age = now_ts - float(_last_good_snapshot.get("ts", 0.0))
            if _last_good_snapshot.get("summary") and cache_age <= _SNAPSHOT_CACHE_TTL_S:
                market_context = (
                    f"（市场快照为 {int(cache_age)}s 前缓存）\n"
                    + str(_last_good_snapshot.get("summary", ""))
                )
                decision_context = str(_last_good_snapshot.get("json", "{}"))
                print(f"  [SNAPSHOT] 复用 {int(cache_age)}s 前成功快照（本次拉取失败/冷却）", flush=True)

        if MACRO_CONTEXT_ENABLED:
            # 外部源串行请求在弱网/代理下可累计数分钟, 这里加整体超时 + 失败冷却;
            macro_ok_now = False
            if now_ts - _last_macro_down_ts > _MACRO_COOLDOWN_S:
                try:
                    macro_pack = await asyncio.wait_for(
                        loop.run_in_executor(None, macro_context.build_context),
                        timeout=20.0,
                    )
                    if macro_pack.get("summary") and macro_pack.get("status") != "unavailable":
                        market_context = (
                            (market_context + "\n\n" + macro_pack["summary"]).strip()
                            if market_context else macro_pack["summary"]
                        )
                        _last_good_macro.update({"ts": now_ts, "summary": macro_pack["summary"]})
                        macro_ok_now = True
                        print(f"  [MACRO] {macro_pack['status']} {macro_pack['ok']}/{macro_pack['total']}")
                    if macro_pack.get("status") == "unavailable":
                        _last_macro_down_ts = now_ts
                except asyncio.TimeoutError:
                    _last_macro_down_ts = now_ts
                    print(f"  [MACRO] 外部源超时(20s), 进入 {int(_MACRO_COOLDOWN_S)}s 冷却")
                except Exception as e:
                    _last_macro_down_ts = now_ts
                    print(f"  [MACRO] 拉取失败: {type(e).__name__}: {str(e)[:80]}")
            else:
                remaining = _MACRO_COOLDOWN_S - int(now_ts - _last_macro_down_ts)
                if int(now_ts) % 60 == 0:
                    print(f"  [MACRO] 跳过 (冷却中, {remaining}s 后重试)")

            # 失败/冷却 → 复用 ≤300s 前的成功宏观层摘要（FRED/盘面/爆仓分钟级变化，
            # 不因代理抖动丢失数据层）
            if not macro_ok_now:
                macro_age = now_ts - float(_last_good_macro.get("ts", 0.0))
                if _last_good_macro.get("summary") and macro_age <= _MACRO_CACHE_TTL_S:
                    cached_macro = (
                        f"（宏观数据为 {int(macro_age)}s 前缓存）\n"
                        + str(_last_good_macro.get("summary", ""))
                    )
                    market_context = (
                        (market_context + "\n\n" + cached_macro).strip()
                        if market_context else cached_macro
                    )
                    print(f"  [MACRO] 复用 {int(macro_age)}s 前成功宏观数据", flush=True)

        # 历史 K 线知识库：追加在宏观层之后，作为"位置/趋势"反共识对照（失败/冷却复用缓存）
        if getattr(config, "KLINE_KB_ENABLED", True):
            kline_ok_now = False
            if now_ts - _last_kline_down_ts > _KLINE_COOLDOWN_S:
                try:
                    kline_block = await asyncio.wait_for(
                        loop.run_in_executor(None, _load_kline_block), timeout=15.0
                    )
                    if kline_block:
                        # v422：K 线块体积上限。实测每条 1,901–1,912 字，且每轮批量都
                        # 整块拼进 market_context（一次决策 1.5 次调用都要带）。
                        # 头部就是"位置/趋势"的关键结论，尾部明细长表截断不影响判读。
                        _kl_max = int(os.getenv("TRIDENT_KLINE_MAX_CHARS", "900") or 900)
                        if _kl_max > 0 and len(kline_block) > _kl_max:
                            kline_block = (kline_block[:_kl_max].rstrip()
                                           + "\n（K 线块超出上限已截断，保留位置/趋势结论）")
                        market_context = (
                            (market_context + "\n\n" + kline_block).strip()
                            if market_context else kline_block
                        )
                        _last_kline_block.update({"ts": now_ts, "block": kline_block})
                        kline_ok_now = True
                        print(f"  [KLINE] 历史K线注入 ({len(kline_block)} 字)", flush=True)
                    else:
                        _last_kline_down_ts = now_ts
                except asyncio.TimeoutError:
                    _last_kline_down_ts = now_ts
                    print("  [KLINE] 拉取超时(15s), 进入冷却", flush=True)
                except Exception as e:
                    _last_kline_down_ts = now_ts
                    print(f"  [KLINE] 失败: {type(e).__name__}: {str(e)[:80]}", flush=True)
            if not kline_ok_now and _last_kline_block.get("block"):
                kline_age = now_ts - float(_last_kline_block.get("ts", 0.0))
                if kline_age <= _KLINE_CACHE_TTL_S:
                    cached_kline = f"（历史K线为 {int(kline_age)}s 前缓存）\n" + _last_kline_block["block"]
                    market_context = (
                        (market_context + "\n\n" + cached_kline).strip()
                        if market_context else cached_kline
                    )

        # ==================================================================
        # Phase 0.5 — Build historical performance reference (once per batch)
        # ==================================================================
        _log_progress("macro")
        performance_context = ""
        try:
            performance_context = await loop.run_in_executor(
                None, _build_performance_context,
            )
            if performance_context:
                print(f"  [PERF] 历史绩效已注入 prompt ({len(performance_context)} chars)")
        except Exception as e:
            print(f"  [PERF] 构建失败: {type(e).__name__}: {str(e)[:80]}")
            performance_context = ""

        # 复盘经验回灌：以独立分区（═══ 复盘经验 ═══）拼进 prompt 尾部，
        # 不再挤成一行塞 user 消息 —— 结构化后模型能逐条对照且带样本数。
        # 变量名刻意与参数 insights_section 区分：后者是内层 _do_api_call 读取的
        # 闭包变量，若这里同名赋值会让 `if insights_section:` 变成"赋值前引用"而报错。
        insights_block_text = ""
        try:
            insights_block_text = await loop.run_in_executor(None, _load_insights_section)
            insights_section = insights_block_text
            if insights_block_text:
                # 减去标题与使用说明 6 行，粗算经验条数（用于日志）
                n_ins = max(1, insights_block_text.count("\n") - 6)
                ratio = len(insights_block_text) / max(1, len(_JSON_PROMPT_FORCE)) * 100
                print(
                    f"  [REVIEW] 复盘经验分区已注入 prompt（约 {n_ins} 条 · "
                    f"{len(insights_block_text)} 字 · 占 Prompt Force {ratio:.0f}%）",
                    flush=True,
                )
                # 体积告警：超过 30% 时提示会挤压八段推演空间（可用 REVIEW_INSIGHTS_LIMIT 收敛）
                if ratio > 30:
                    print(
                        f"  [REVIEW] ⚠ 经验分区偏大（{ratio:.0f}%），可能挤压八段推演；"
                        "可用 .env 的 REVIEW_INSIGHTS_LIMIT 限制条数或停用低价值经验",
                        flush=True,
                    )
            else:
                print("  [REVIEW] 暂无生效的复盘经验", flush=True)
        except Exception as e:
            print(f"  [REVIEW] 经验加载失败: {type(e).__name__}: {str(e)[:80]}")

        # 参考信号回灌：提炼的统计信号实时注入 prompt（反映最新已结算交易）
        reference_signals = ""
        try:
            from review.review_agent import derive_reference_signals

            def _sig_lines(items):
                out = []
                for it in items[:4]:
                    if it.get("summary"):
                        out.append(it["summary"])
                    else:
                        wr = f"{it['winrate'] * 100:.0f}%" if it.get("winrate") is not None else "?"
                        out.append(f"{it['label']}:{it['total']}笔 胜率{wr} 盈亏{it['total_pnl']:+.2f}%")
                return out

            sig = await loop.run_in_executor(None, derive_reference_signals)
            sig_parts = []
            if sig.get("assets"):
                sig_parts.append("品种 " + "; ".join(_sig_lines(sig["assets"])))
            if sig.get("directions"):
                sig_parts.append("方向 " + "; ".join(_sig_lines(sig["directions"])))
            if sig.get("phases"):
                sig_parts.append("阶段 " + "; ".join(_sig_lines(sig["phases"])))
            if sig.get("strengths"):
                sig_parts.append("强度 " + "; ".join(_sig_lines(sig["strengths"])))
            if sig.get("intervals"):
                sig_parts.append("入场时机 " + "; ".join(_sig_lines(sig["intervals"])))
            if sig.get("combos"):
                sig_parts.append("品种×方向 " + "; ".join(_sig_lines(sig["combos"])))
            if sig_parts:
                reference_signals = "[Reference Signals] 历史统计信号（实时提炼自已结算模拟交易，仅作参考）：" + " | ".join(sig_parts)
                print(f"  [SIGNAL] 参考信号已注入 prompt ({len(sig_parts)} 组)", flush=True)
            else:
                print("  [SIGNAL] 暂无参考信号", flush=True)
        except Exception as e:
            print(f"  [SIGNAL] 信号加载失败: {type(e).__name__}: {str(e)[:80]}")

        _log_progress("signals")

        # ── v322: 认领前做通道体检 —— 全熔断则**不认领**（零额度消耗）────
        # 事故背景（2026-11-04 实测）：4 个通道全挂（2×429 配额打满 + 2×401 key 失效），
        # 主循环却仍然每轮「认领一批 → 打主模型 + 3 个 fallback → 全失败 → 重试」，
        # 单轮几十次 HTTP 请求全部打空 —— 用户反馈"一下子把流量干没了"。
        #
        # 熔断打开时**不认领**：既不消耗额度，也不会把新闻推到 retry_count 上限
        # 变成 FAILED（那会二次杀死这批好数据）。熔断冷却结束后自动恢复认领。
        #
        # ── v322: 超龄归档放在熔断检查**之前** ──────────────────────────
        # 归档只是一条 UPDATE，**不花任何 LLM 额度**。
        # 之前它被埋在 _batch_claim() 内部 → 通道全熔断时主循环直接 continue，
        # 归档永远跑不到 → 「待分析」计数在通道故障期间只涨不降。
        _now_arch = time.time()
        if _now_arch - _last_archive_ts >= 60.0:
            _last_archive_ts = _now_arch
            try:
                _n_arch = await loop.run_in_executor(None, _run_expire_stale_news)
                if _n_arch:
                    print(f"  [BATCH] 归档超龄新闻 {_n_arch} 条（超过 "
                          f"{getattr(config, 'NEWS_ANALYZE_MAX_AGE_HOURS', 0)} 小时，"
                          f"已过期不可交易）→ 不再占用待分析计数", flush=True)
            except Exception as _arch_exc:
                print(f"  [BATCH] 归档超龄新闻异常: {type(_arch_exc).__name__}: "
                      f"{str(_arch_exc)[:120]}", flush=True)

        # -- v340 P1: 决策链停摆巡检必须在**通道体检之前** --------------
        # 原实现把它放在 `if not _pool_ok: continue` 之后 —— 结果是"通道全挂
        # 恰恰是最该告警的时候不巡检"，引擎一条决策不出也悄无声息
        # （实测 2026-11-15: 熔断 → ai_decisions=0 → 无任何告警）。
        _now_stall = time.time()
        if _now_stall - _STALL_STATE["last_check_ts"] >= 300.0:
            _STALL_STATE["last_check_ts"] = _now_stall
            try:
                _check_and_alert_decision_stall()
            except Exception:
                pass

        _pool_ok, _pool_reason = main_pool_ready()
        # 体检结果**无条件**节流打印（60s 一行）—— 不打就没法确认它到底返回什么，
        # 之前"只在被拦时打印"导致"熔断已开但体检没拦"这类问题完全不可见。
        _now_pool = time.time()
        if _now_pool - _last_pool_check_log_ts >= 60.0:
            _last_pool_check_log_ts = _now_pool
            print(f"[AI] 通道体检 ready={_pool_ok} reason={_pool_reason or '-'}",
                  flush=True)
        if not _pool_ok:
            # v434（v413 P0-A1）：干等必须可见 —— 轮次 / 已等待 / 最近可恢复时刻
            # 全部落进 progress。看门狗与看板这才分得清「上游挂了在等」和
            # 「进程真卡死」；运维也能一眼判断该等还是该换 key（auth 是 24h）。
            _pool_blocked_round += 1
            if not _pool_blocked_since:
                _pool_blocked_since = _now_pool
            _pool_blocked_elapsed = max(0.0, _now_pool - _pool_blocked_since)
            # 只有「真熔断」才值得算倒计时；额度闸门是本地预算，与通道无关
            _pool_eta = (pool_recovery_eta() if "熔断" in (_pool_reason or "") else None)
            _log_progress(
                "pool-blocked",
                pool_blocked_round=_pool_blocked_round,
                pool_blocked_since=round(_pool_blocked_since, 1),
                pool_blocked_elapsed_s=round(_pool_blocked_elapsed, 1),
                pool_blocked_reason=(_pool_reason or "")[:160],
                pool_next_recovery_s=(None if _pool_eta is None else round(_pool_eta, 1)),
            )
            if _now_pool - _last_pool_block_log_ts >= 60.0:
                _last_pool_block_log_ts = _now_pool
                _eta_txt = ""
                if _pool_eta is not None:
                    _eta_dt = (datetime.now(config.TZ_SHANGHAI)
                               + timedelta(seconds=int(_pool_eta)))
                    _eta_txt = (f"｜最近可恢复 {_eta_dt.strftime('%H:%M')}"
                                f"（还要 {int(_pool_eta)}s）")
                print(
                    f"[AI] 跳过认领：{_pool_reason}"
                    f"（熔断冷却中，零额度消耗｜第 {_pool_blocked_round} 轮，"
                    f"已等 {_pool_blocked_elapsed:.0f}s{_eta_txt}）",
                    flush=True,
                )
                # v322: 全通道不可用 → **主动告警**。
                # 此前这里完全静默：引擎停摆但用户收不到任何通知，
                # 只能靠「待分析不涨」反推 —— 正是被投诉的"随机抽风"。
                # notify_pool_blocked 内部自带 6 小时节流 + 状态落盘。
                try:
                    notify_pool_blocked(_pool_reason)
                except Exception:
                    pass
            await asyncio.sleep(5.0)
            continue

        # v322: 通道恢复 → 告警一次（告诉用户可以换回 key 了）
        try:
            notify_pool_blocked("", recovered=True)
        except Exception:
            pass
        # v434：恢复后清零现场状态（下一轮 _log_progress 会把旧字段整份覆盖掉）
        _pool_blocked_since = 0.0
        _pool_blocked_round = 0


        batch = await loop.run_in_executor(None, _batch_claim)
        _log_progress("batch")

        # -- v323 P0-1: 整批心跳（分析 + 持久化全程）------------------
        # 必须在**批次开始**就起，而不是只包住 LLM 分析段：持久化段同样会
        # 因 SQLite 写锁竞争而长时间阻塞（实测线程全卡在 sqlite3_step），
        # 那段时间 progress 不更新 → 看门狗 600s 后误杀引擎 → 批次白跑。
        _hb_task = asyncio.ensure_future(_batch_heartbeat())
        if not batch:
            idle_ticks += 1
            if idle_ticks % 30 == 1:
                print(f"[{_now()}] [AI] idle ({idle_ticks}s)")
            continue

        idle_ticks = 0  # reset heartbeat on activity

        batch_ids = [r["id"] for r in batch]
        # v322: 登记为「在办批次」→ 看门狗跳过。
        # 单批耗时可能超过看门狗 15 分钟阈值（行情源全超时时实测 42 分钟），
        # 若中途被回收，批事务末尾 `WHERE status='PROCESSING'` 匹配 0 行 → 一条都进不了 DONE。
        # 登记同时起心跳线程刷新租约；批次异常退出时心跳自停 → 租约过期 → 僵尸仍可回收。
        _hb_stop = _active_batch_register(batch_ids)
        # Map news_id → content for downstream use (Feishu alerts etc.)
        content_map: Dict[int, str] = {}
        timestamp_map: Dict[int, str] = {}
        published_map: Dict[int, str] = {}
        # 门卫四步标签(L3)：news_id → gate_verdict，供决策层收口（v181：排除 → HOLD；
        # "只能看" 已删除，但老数据中仍可能有此值 → 历史兜底走 STRONG-PASS 路径）
        gate_verdict_map: Dict[int, str] = {}
        # v176.5: 同时存 strength，用于 "strength>=_GATE_PASS_THR 放行"（兜底）
        gate_strength_map: Dict[int, int] = {}
        # v323：门卫阶段算出来的真实影响力（0-100）。实测 ai_decisions 里
        # impact_score 8,375/8,786 条都是默认 5.0 —— 模型基本不返回这个字段，
        # 落库全按 default 兜底，等于"影响力评估"名存实亡。
        # 这里把门卫的真实 0-100 分带进来，作为默认值缺失时的替代口径。
        gate_impact_map: Dict[int, int] = {}
        # v176.5: 批内最大 strength (供 MECH-GATE 共享 — 强信号全批放行)
        _batch_max_strength: int = 0
        for row in batch:
            c = row["content"]
            # Strip [hash:xxx] prefix for cleaner display
            c = re.sub(r'\[hash:[a-zA-Z0-9]+\]\s*', '', c)
            content_map[row["id"]] = c
            timestamp_map[row["id"]] = row["timestamp"] or ""
            # 平台声称的发布时间（旧库无该列时为空 → 回落入库时间）
            try:
                published_map[row["id"]] = row["published_at"] or ""
            except Exception:
                published_map[row["id"]] = ""
            try:
                gate_verdict_map[row["id"]] = row["gate_verdict"] or ""
            except Exception:
                gate_verdict_map[row["id"]] = ""
            try:
                gate_strength_map[row["id"]] = int(row["gate_strength"] or 0)
                _batch_max_strength = max(_batch_max_strength, int(row["gate_strength"] or 0))
            except Exception:
                gate_strength_map[row["id"]] = 0
            try:
                gate_impact_map[row["id"]] = int(row["gate_impact_1to100"] or 0)
            except Exception:
                gate_impact_map[row["id"]] = 0
        print(
            f"\n[{_now()}] [AI] Claimed {len(batch)} items: {batch_ids}"
        )

        # ==================================================================
        # Phase 2 - Concurrent LLM analysis (news × models)
        # ==================================================================
        batch_start = _ts()
        # 多模型并行分片：批次内按 weight 加权把新闻分给模型池各模型，
        # 快且稳的通道多分（deepseek > gpt-5.5/gpt-5.4-mini），
        # 总吞吐 ×N 且每模型并发低（降低上游限流/403 概率）。
        # OpenRouter 不可用(402)时对应槽位自动切换 MiniMax 备用通道。
        model_pool = _build_model_pool()
        print(f"[{_now()}] [AI] Model pool: {[m['id'] for m in model_pool]}")
        # 引擎视角的模型池落盘（前端可查看实际分析通道构成）
        try:
            _pool_file = os.path.join(config.BASE_DIR, "runtime", "model_pool.json")
            os.makedirs(os.path.dirname(_pool_file), exist_ok=True)
            with open(_pool_file, "w", encoding="utf-8") as f:
                json.dump({
                    "pool": [m["id"] for m in model_pool],
                    "labels": [m.get("label", m["id"]) for m in model_pool],
                    "ts": time.time(),
                }, f, ensure_ascii=False)
        except Exception:
            pass
        hermes_skills: List[Dict[str, Any]] = []
        writing_map: Dict[int, str] = {}
        if HERMES_AGENT_ENABLED:
            try:
                hermes_agent.refresh_skills()
                hermes_skills = hermes_agent.load_skills()
                if hermes_skills:
                    print(f"  [HERMES] 已结算技能 {len(hermes_skills)} 条回灌")
            except Exception as e:
                print(f"  [HERMES] 技能刷新失败: {type(e).__name__}: {str(e)[:80]}")
                hermes_skills = []
            for row in batch:
                nid = row["id"]
                try:
                    packet = hermes_agent.persist_news_writing(
                        nid,
                        content_map.get(nid, ""),
                        source=row["source"] if "source" in row.keys() else "",
                        market_context=market_context,
                    )
                    writing_map[nid] = hermes_agent.render_writing_context(
                        packet["desks"], hermes_skills
                    )
                except Exception as e:
                    print(f"  [HERMES] 写作沉淀失败 news={nid}: {type(e).__name__}: {str(e)[:80]}")
                    try:
                        writing_map[nid] = hermes_agent.build_prompt_context(
                            content_map.get(nid, ""), market_context
                        )
                    except Exception:
                        writing_map[nid] = ""
        # 构建每条的 fallback 链：优先 MiniMax（直连/代理皆快），再补池内其他非重复模型。
        # 避免「OpenRouter 主模型挂起 → fallback 仍是 OpenRouter → 继续挂」的无效重试。
        _mm_cfg: Optional[Dict[str, Any]] = None
        if config.MINIMAX_API_KEY:
            _mm_cfg = _minimax_cfg(config.MINIMAX_API_KEY)  # 复用 M3/Text-01 extra_body 规则（含 thinking disabled）
            _mm_cfg["full_prompt"] = True  # 完整 _SYSTEM_PROMPT 出嵌套 sibling
        tasks = []
        # v345.13: 批内锁通道（sticky），不再逐条轮转。
        # 原来 main_cfg = model_pool[idx % len(model_pool)] → 同一批 16 条轮流用
        # 4 个 key，每条都是该 key 的"第一次" → MiniMax 自动前缀缓存永远不命中
        # （实测 cached_tokens 恒 0/128）。改成整批用同一个通道：批内第 2 条起
        # 直接命中 system 段（实测命中 8,192–11,904，占 65–100%）；下一批再换下
        # 一个通道，配额分散的初衷仍然保留。
        _MAIN_BATCH_ROTATE["n"] += 1
        # v398h：粘 key 已撤回。2026-11-16 实测结论：该通道的缓存是"整条 prompt
        # 完全一致才命中"，同 system 换 user 文本即 cached=128（probe6：①A=128
        # ②B=128），只有整条完全一致才 4,393/4,394 命中（TTL ≥ 330s 不是问题）。
        # 每条新闻的 user 都不同 → 生产永远命中不了，粘 key 只白白集中配额。
        # 恢复逐批轮转，保留配额分散。
        _now_sticky = time.time()   # v404：必须用 float 时间戳——_ts() 返回字符串，和 since(float) 相减会 TypeError 打断整个主循环
        if (_MAIN_STICKY["idx"] is None
                or _now_sticky - _MAIN_STICKY["since"] > _MAIN_STICKY_WINDOW_S
                or not (0 <= int(_MAIN_STICKY["idx"]) < len(model_pool))):
            _MAIN_STICKY["idx"] = _MAIN_BATCH_ROTATE["n"] % len(model_pool)
            _MAIN_STICKY["since"] = _now_sticky
            print(f"  [MAIN-KEY] 切换主通道 index={_MAIN_STICKY['idx']} "
                  f"(粘住 {_MAIN_STICKY_WINDOW_S:.0f}s 以命中前缀缓存)", flush=True)
        _batch_main_cfg = model_pool[int(_MAIN_STICKY["idx"]) % len(model_pool)]
        for idx, row in enumerate(batch):
            main_cfg = _batch_main_cfg
            # 多模型交叉验证：高价值新闻 → 主模型外再让 2 个模型独立分析（仅展示/标注）
            verify_cfgs = (
                # v323：额度紧张。原来每条新闻都拉 2 个验证模型（每条 = 主分析+2 张票），
                # 8 条一批就要 24 次请求 → 实测把账户打到"全部通道熔断"。
                # 降成 1 个验证模型：交叉验证（双验证）仍然成立，请求量省 1/3。
                _pick_verify_cfgs(main_cfg, model_pool, 1, fallback=1)
                if _is_high_value_news(row, content_map.get(row["id"], ""))
                else []
            )
            tasks.append(
                _process_single(
                    row,
                    main_cfg,
                    loop,
                    market_context,
                    performance_context,
                    writing_map.get(row["id"], ""),
                    insights_section,
                    reference_signals,
                    fallback_cfgs=[
                        *( [] if _mm_cfg is None else [_mm_cfg] ),
                        *[
                            fb for i2, fb in enumerate(model_pool)
                            if (i2 % len(model_pool)) != (idx % len(model_pool))
                            and fb["id"] != main_cfg["id"]
                            and ( _mm_cfg is None or fb["id"] != _mm_cfg["id"] )
                        ][:2],
                    ][:3],
                    verify_cfgs=verify_cfgs,
                )
            )
        # v398e：批级硬超时。原来这里是无超时的 asyncio.gather ——
        # 只要一条新闻的链路（主分析 + schema + 两步 + 评审）里有一次卡在
        # 没返回的 HTTP/DNS 上，整批 8 条就**永远不结束**：8 行一直占着
        # PROCESSING、pending 只增不减、决策一条不出（2026-11-16 实测
        # 最近 1 小时 417 条 PENDING、0 条 DONE、0 条新决策）。
        # 现在给整批一个预算：超时就把未完成的判成失败并取消任务，
        # 批次照常收尾 → 新闻按冷却退回 PENDING → 下一批能继续认领。
        _BATCH_BUDGET_S = float(os.getenv("TRIDENT_BATCH_TIMEOUT_S", "420"))
        # v398f：先串行跑批内第 1 条，再并发其余 —— 为前缀缓存预热。
        # 实测（2026-11-16 21:05）30 次主分析 cached_tokens 平均只有 131：
        # 批内 8 条是 asyncio.gather 同时发出的，MiniMax 的前缀缓存按
        # 「同 key + 同前缀 + 已有一次完成」生效 —— 8 条同时在飞时，
        # 谁都不是「第二条」，system 段（约 4.5K tokens）一次也没命中。
        # 串行第 1 条之后，剩下 7 条能命中 system 前缀，单批省约 3 万 tokens。
        # v493（2026-11-19）：**逐条落 spool**，不再等整批结束。
        # 背景：外部会话每 2–8 分钟就 SIGTERM 一次引擎，批大小已压到 2，
        # 单条约 2.5 分钟 —— 整批 gather 完之后才写 spool，一旦在"分析完、
        # 批还没收尾"这段被杀，这批 token 全作废（历史决策 0 增长就是这么来的）。
        # 包一层之后：哪条先分析完，哪条立刻落盘，被杀最多丢 1 条。
        _raw_tasks = tasks

        async def _spool_on_done(_t):
            _r = await _t
            try:
                if isinstance(_r, dict) and int(_r.get("news_id") or 0) > 0:
                    _spool_batch([_r])
            except Exception as _e_sp:
                print(f"  [SPOOL] 单条落盘失败: {type(_e_sp).__name__}: "
                      f"{str(_e_sp)[:60]}", flush=True)
            return _r

        tasks = [_spool_on_done(_t) for _t in _raw_tasks]

        _warm_first = (os.getenv("TRIDENT_CACHE_WARM_FIRST", "1").strip() != "0"
                       and len(tasks) > 1)
        try:
            if _warm_first:
                _first_res = await asyncio.wait_for(
                    asyncio.gather(tasks[0], return_exceptions=True),
                    timeout=_BATCH_BUDGET_S,
                )
                _rest = await asyncio.wait_for(
                    asyncio.gather(*tasks[1:], return_exceptions=True),
                    timeout=_BATCH_BUDGET_S,
                )
                results: List[Dict[str, Any]] = list(_first_res) + list(_rest)
            else:
                results = await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True),
                    timeout=_BATCH_BUDGET_S,
                )
        except asyncio.TimeoutError:
            print(f"  [AI] ⚠️ 批次超时 {_BATCH_BUDGET_S:.0f}s，"
                  f"未完成的 {len(tasks)} 条判失败（避免整批悬死）", flush=True)
            for _t in tasks:
                try:
                    _t.cancel()
                except Exception:
                    pass
            # v453 P1-3：超时是按 batch 里**真实 news_id** 退回队列，而不是笼统
            # 造一个 news_id=-1 的失败记录 —— 后者不会碰 DB，8 行就一直卡在
            # PROCESSING，只能等启动重置或人工处理。这里直接把它们改回 PENDING
            # （retry_count+1，达上限才 FAILED），下一批能重新认领。
            _timeout_ids: List[int] = []
            for _row in batch:
                try:
                    _timeout_ids.append(int(_row["id"]))
                except Exception:
                    pass
            if _timeout_ids:
                try:
                    _tc = _open_db()
                    _ph = ",".join("?" for _ in _timeout_ids)
                    _tc.execute(
                        "UPDATE raw_news SET retry_count = COALESCE(retry_count, 0) + 1,"
                        " processing_at = NULL,"
                        " status = CASE WHEN COALESCE(retry_count, 0) + 1 >= ?"
                        "          THEN 'FAILED' ELSE 'PENDING' END"
                        " WHERE id IN (" + _ph + ") AND status='PROCESSING'",
                        (_MAX_PENDING_RETRIES, *_timeout_ids),
                    )
                    _tc.commit()
                    _tc.close()
                    print(f"  [AI] 批次超时 → {len(_timeout_ids)} 条退回 PENDING"
                          f"（retry_count+1）", flush=True)
                except Exception as _to_exc:
                    print(f"  [AI] ⚠️ 批次超时退回失败: {type(_to_exc).__name__}: "
                          f"{str(_to_exc)[:120]}", flush=True)
            results = [asyncio.TimeoutError("batch_timeout") for _ in tasks]
        _log_progress("analyze-done")
        batch_end = _ts()

        # Separate successes from failures
        successes: List[Dict[str, Any]] = []
        failures: List[Dict[str, Any]] = []

        for r in results:
            if isinstance(r, Exception):
                failures.append({
                    "news_id": -1, "pre_ts": "?", "model_label": "?",
                    "error": f"gather:{type(r).__name__}",
                })
                continue
            if r["error"] is not None:
                failures.append(r)
            else:
                successes.append(r)

        # ── v451（2026-11-18）：门卫主品种兜底 ──
        # 规则门卫已把 原油/炼油厂→WTI、美联储/加息→XAU、加密→BTC 写进 gate_main_asset，
        # 但主分析偶尔还是判 NONE（拿不到池内标的）→ routing_miss，决策出不来。
        # 这里在落库前把 target_asset 补成门卫主品种（只在 NONE/OTHER/空 时补，不覆盖模型结论）。
        try:
            _row_map = {}
            for _row in batch:
                try:
                    _row_map[int(_row["id"])] = _row
                except Exception:
                    pass
            _POOL = ("BTC", "XAU", "WTI", "ETH", "SOL")
            for _r in successes:
                try:
                    _res = _r.get("result") or {}
                    _ta = str(_res.get("target_asset") or "").strip().upper()
                    if _ta not in ("", "NONE", "OTHER"):
                        continue
                    _row = _row_map.get(int(_r.get("news_id") or -1))
                    if _row is None:
                        continue
                    _ga = str(_row["gate_main_asset"] or "").strip().upper()
                    if _ga not in _POOL:
                        continue
                    _res["target_asset"] = _ga
                    _res["target_asset_source"] = "gate_main_asset(v451)"
                    _r["result"] = _res
                    print(f"  [GATE-ASSET] news#{_r.get('news_id')} 模型判 NONE → 补成门卫主品种 {_ga}", flush=True)
                except Exception:
                    pass
        except Exception:
            pass

        # v323 诊断：每批只打一行"成功几条/失败几条/失败原因"，否则回写不动时全无线索
        print(
            f"  [BATCH-RESULT] 成功 {len(successes)} / 失败 {len(failures)}"
            + ("" if not failures else " | 失败原因: " + " ; ".join(
                f"news{r.get('news_id')}:{str(r.get('error'))[:60]}" for r in failures[:3]
            )),
            flush=True,
        )

        # v424：分析成果立刻落 spool —— 外部 `kickstart -k` 若在这之后、落库之前
        # 把进程杀掉，下次启动由 _spool_drain() 补写决策，已花的 token 不作废。
        try:
            _spool_batch(successes)
        except Exception as _spool_exc:
            print(f"  [SPOOL] 写入失败（不致命）: {type(_spool_exc).__name__}: "
                  f"{str(_spool_exc)[:80]}", flush=True)

        # ==================================================================
        # Phase 3 - Batch persist (model label in reasoning prefix)
        # ==================================================================
        def _batch_persist():
            """
            Two-phase merge:
              Phase 3a — primary model results: INSERT ai_decisions (aggregation, entry price).
              Phase 3b — Doubao results: UPDATE the matching primary model row with
                         doubao_action / doubao_reasoning.
            Both phases complete inside ONE transaction — SSE consumers see
            fully-populated rows with no race window where Doubao data is missing.
            """
            conn = _open_db()
            # v323 P0-1: 持久化是**关键路径**，写锁等待窗口必须覆盖最坏持锁时长。
            # 引擎内多个后台任务会各自持写锁数十秒（db.py 注释实测：
            # "forward_tracker 在 _track_cycle 期间持有 write 锁 30-49s"），
            # 叠加后写锁几乎 100% 被占 → 默认 busy_timeout=5s 永远抢不到 →
            # 批次反复重认领、决策卡在 #10618（实测 external BEGIN IMMEDIATE
            # 5s/15s 均失败，只读 0.007s）。
            # 取 60s：一次写事务最多多等 60s 就能在锁的间隙拿到，
            # 拿到后整批写入不再等待（事务内自己持锁）。
            try:
                conn.execute("PRAGMA busy_timeout=60000;")
            except Exception:
                pass
            written: List[Dict[str, Any]] = []
            done_news_ids: set[int] = set()
            # v414：本批已用掉几次落库前抢救（见 _RESCUE_MAX_PER_BATCH 的说明）
            _rescue_used_in_batch = 0
            parent_cache: Dict[str, int | None] = {}
            # news_id → decision_id mapping for Doubao UPDATE pass
            primary_decision_ids: Dict[int, int] = {}
            try:
                trading_settings = paper_trading.get_settings(conn)
                active_run = trading_settings.get("active_run")
                # 只在策略配置变化时才写 strategies 表（避免每批无谓 UPDATE）。
                current_strategy = strategy_store.get_current_strategy(conn)
                if trading_settings["is_running"] and active_run:
                    cs_id = int(current_strategy["id"])
                    cs_ver = int(current_strategy["active_version"]["id"])
                    run_sid = int(active_run["strategy_id"])
                    run_sver = int(active_run["strategy_version_id"])
                    if cs_id != run_sid or cs_ver != run_sver:
                        current_strategy = strategy_store.set_current_strategy(
                            run_sid, run_sver, conn,
                        )
                active_version = current_strategy["active_version"]
                active_params = active_version["params"]
                # ============================================================
                # Phase 3a — primary model INSERT — aggregation + tracking
                # ============================================================
                # 多模型分片：所有成功结果都属主分析池（无 Doubao 时无 secondary）
                # 多品种：sibling_decisions 拆成独立 decision 条目（各品种独立交易信号）
                primary_results = _expand_multi_asset_results(successes)

                # 单源开关只过滤模拟盘交易（不影响新闻采集/分析入库）：
                # 建仓前按该新闻的 source 查 news_sources 开关。
                news_source_settings = news_sources.get_settings(conn)
                source_map = {
                    row["id"]: row["source"] if "source" in row.keys() else ""
                    for row in batch
                }

                # 规则事件结构化提取按 news_id 缓存：主行/副行复用，避免重复 re 抽取
                event_struct_cache: Dict[str, str] = {}

                for s in primary_results:
                    # 整箱白名单过滤：缺字段补默认/非法夹取，绝不整条丢弃
                    res = _sanitize_llm_result(s.get("result") or {})
                    # v176：schema 必填字段强制达标
                    try:
                        res = _presanitize_for_schema(
                            res, str((content_map or {}).get(s.get("news_id")) or ""))
                    except Exception:
                        pass
                    # v342 观测：把"降级"落一行，让"哪一步坏的"可量化（不改判定）
                    try:
                        from common import llm_degrade_probe as _ldp
                        _ldp.record(conn, news_id=nid,
                                    asset=res.get("target_asset") or asset,
                                    result=res)
                    except Exception:
                        pass
                    # P1-5 服务端兜底：即便 P0-2 retry 失败、LLM 漏填 4 字段，也强制填出 N/A
                    # 用 published_map / timestamp_map 推 ISO 时间，避免 ai_decisions 16.1 字段 0%
                    _nid = s.get("news_id")
                    _pub = published_map.get(_nid, "") if _nid else ""
                    # v1 重构：消费 _unclassified_* 标记 → 写 unclassified_events 表
                    _consume_unclassified_marker(
                        res, news_id=_nid,
                        title="", source="", news_published_at=_pub,
                    )
                    _ts_iso = ""
                    if _pub:
                        _ts_iso = _pub[:32]
                    elif _nid and _nid in timestamp_map:
                        _ts_iso = timestamp_map[_nid][:32]
                    res = _auto_fill_required_fields(
                        res,
                        snapshot_ts_iso=_ts_iso,
                        news_published_at=_pub,
                        evidence_lines=None,
                    )
                    # P1-6 服务端拼 hard_data_evidence：用 target_asset 抓 K 线数据
                    if not res.get("hard_data_evidence") or res.get("hard_data_evidence") == "N/A":
                        try:
                            _ta = res.get("target_asset") or ""
                            if _ta and _ta not in ("NONE", "", "OTHER"):
                                _ev = _build_evidence_for_asset(_ta, _ts_iso)
                                if _ev:
                                    res["hard_data_evidence"] = _ev[:500]
                        except Exception:
                            pass
                    # P1-7 服务端补 snapshot_proximity：published_at 与 timestamp 之差（分钟）
                    if not res.get("snapshot_proximity") or res.get("snapshot_proximity") == "":
                        try:
                            from datetime import datetime as _dt
                            _pub_dt = None
                            if _pub:
                                for _fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ",
                                             "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
                                    try:
                                        _pub_dt = _dt.strptime(_pub[:19], _fmt)
                                        break
                                    except Exception:
                                        continue
                            _row_ts = timestamp_map.get(_nid, "")
                            if _pub_dt and _row_ts:
                                _row_dt = _dt.strptime(_row_ts[:19], "%Y-%m-%d %H:%M:%S")
                                _delta_min = int((_pub_dt - _row_dt).total_seconds() / 60.0)
                                res["snapshot_proximity"] = str(_delta_min)
                            else:
                                res["snapshot_proximity"] = "N/A"
                        except Exception:
                            res["snapshot_proximity"] = "N/A"
                    # P2-9 注入市场风格到 reasoning_path 末尾
                    try:
                        _ta2 = res.get("target_asset") or ""
                        if _ta2 and _ta2 not in ("NONE", "", "OTHER"):
                            _ms = _compute_market_style(_ta2)
                            if _ms and _ms != "unknown|n/a|n/a":
                                _ms_line = f"[市场风格] {_ms} → LLM 应据此调整 reasoning 的胜率预期"
                                _rp = res.get("reasoning_path") or ""
                                if "[市场风格]" not in _rp:
                                    res["reasoning_path"] = (_rp + "\n" + _ms_line)[:4000]
                    except Exception:
                        pass
                    s["result"] = res
                    label = s["model_label"]
                    nid = s["news_id"]
                    # 本行"分析模型"标记：优先真实调用模型；缺失时才退回运行配置，
                    # 但运行配置可能过期（不在当前模型池内）→ 那就不用它，退回本批模型，
                    # 否则前端会显示一个早已废弃的通道（实测遗留 gpt-5.4-mini）。
                    _pool_ids = {m["id"] for m in model_pool}
                    _run_model = str((active_run or {}).get("model_id") or "")
                    _model_for_row = (
                        str(res.get("model_id") or "").strip()
                        or (_run_model if _run_model in _pool_ids else "")
                        or (model_pool[0]["id"] if model_pool else "")
                    )
                    done_news_ids.add(nid)
                    tagged_reason = f"[{label}] {res['reasoning']}"
                    score = res["sentiment_score"]
                    action = res["suggested_action"]
                    # v323 取证：模型原始分 vs 落库分对不上时，从这里开始逐点追踪
                    if os.getenv("TRIDENT_TRACE_SCORE", "0") == "1":
                        print(f"  [SCORE-TRACE] news=#{nid} 模型原始 sentiment_score={res.get('sentiment_score')} action={action} → score={score}", flush=True)
                    # ── v176.5f: 闸门归因记录器（gate_chain 落库）──
                    # 在闸门调用前初始化，影子模式记录 LLM 原始 action。
                    # v406: original_action 必须是**降级之前**模型给的方向，
                    # 否则「模型说 BUY、系统记 HOLD」看起来像模型没给方向。
                    _gr = _GateRecorder(llm_action=str(res.get("_raw_action") or action))
                    # v181: 回放主分析阶段已判定的 FOCUS/MECH 闸门（彼时 _gr 尚未创建），
                    # 让 gate_chain 完整覆盖 FOCUS/MECH/CONSENSUS 三道门，门卫漏报/误报可归因。
                    for _ge in (res.get("_gate_trace") or []):
                        if isinstance(_ge, dict) and _ge.get("gate"):
                            _gr.record(
                                str(_ge.get("gate") or ""),
                                str(_ge.get("verdict") or ""),
                                str(_ge.get("reason") or "")[:200],
                            )
                    # ── v176.9 (2026-09-22)：门卫 LLM 失效防御 ──
                    # 若该新闻 gate_strength=0 且 gate_verdict="无信号"（门卫瞎了），
                    # 直接丢弃，不写入 ai_decisions，避免前端展示伪造的"AI 观望"卡片。
                    # 兜底逻辑：ingest 层 _attach_gate v176.9 已经 FILTERED，
                    # 但万一历史数据或边缘 case 漏过，这里再卡一次。
                    _gm_check = conn.execute(
                        "SELECT gate_strength, gate_verdict, is_noise, gate_unavailable, retry_count"
                        " FROM raw_news WHERE id=?",
                        (nid,),
                    ).fetchone()
                    # ── FIX-8（2026-11-09）：区分"门卫看不清"与"门卫说没信号" ──
                    # 修复前：两种情况一律 status='FAILED' 且 retry_count 不动
                    #   → 门卫不可用时的行被当终态埋掉，永不重试，决策链静默停摆。
                    # 现在：看不清 → PENDING + retry_count+1（带冷却，等门卫回放）；
                    #        真无信号 → FILTERED（系统主动不分析，不是失败）。
                    # 判定逻辑在 gate_drop_policy（纯函数，见 tests/test_gate_drop_policy_v3215.py）。
                    _gm_st, _gm_rc, _gm_why = (
                        gate_drop_policy.resolve_drop_status(
                            gate_strength=(_gm_check["gate_strength"] if _gm_check else None),
                            gate_verdict=(_gm_check["gate_verdict"] if _gm_check else None),
                            is_noise=(_gm_check["is_noise"] if _gm_check else None),
                            gate_unavailable=(_gm_check["gate_unavailable"] if _gm_check else None),
                            retry_count=(_gm_check["retry_count"] if _gm_check else None),
                            max_retries=_MAX_PENDING_RETRIES,
                        )
                        if _gm_check else (None, 0, "")
                    )
                    if _gm_st is not None:
                        # 标记 _focus_downgrade 状态为 GATE-MISS（便于调试）
                        try:
                            _gr.apply_block("GATE-MISS", f"门卫未给出可用判定({_gm_why}) → 跳过 ai_decisions 写入")
                        except Exception:
                            pass
                        print(
                            f"  [GATE-MISS] news=#{nid} gate_strength={_gm_check['gate_strength']} "
                            f"verdict='{_gm_check['gate_verdict']}' is_noise={_gm_check['is_noise']} "
                            f"gate_unavailable={_gm_check['gate_unavailable']} "
                            f"→ {_gm_st}（{_gm_why}, retry {_gm_check['retry_count']}→{_gm_rc}），"
                            f"不写 ai_decisions",
                            flush=True,
                        )
                        try:
                            if _gm_st == gate_drop_policy.STATUS_PENDING:
                                # 带冷却：否则立刻又会被认领，形成热循环
                                conn.execute(
                                    "UPDATE raw_news SET retry_count=?, status='PENDING',"
                                    " processing_at=datetime('now','localtime','+' || ? || ' seconds')"
                                    " WHERE id=?",
                                    (_gm_rc, _BATCH_RETRY_COOLDOWN_SEC, nid),
                                )
                            else:
                                conn.execute(
                                    "UPDATE raw_news SET retry_count=?, status=?, processing_at=NULL"
                                    " WHERE id=?",
                                    (_gm_rc, _gm_st, nid),
                                )
                            conn.commit()
                        except Exception as _gm_exc:
                            # v322: 标 FAILED 失败 → 该行卡 PROCESSING → 15 分钟后才被看门狗回收
                            print(f"  [AI] ⚠️ 门卫排除行标 FAILED 失败 (news=#{nid}): "
                                  f"{type(_gm_exc).__name__}: {str(_gm_exc)[:100]}",
                                  flush=True)
                        continue
                    # ── v181: 取消 L3-MENWEI 闸门（realtime_filter 已二分类为"可交易/排除"，
                    # 排除的新闻在 ingest 阶段就被打 is_noise=1，根本进不到 ai_worker ；
                    # 进入此处的 _batch_claim 都是 gate_verdict="可交易" 或历史老数据）。
                    # 保留 strength>=thr 的 STRONG-PASS 兜底：旧"只能看"数据如有遗留，
                    # 仍按 v176.5c 信任 LLM 自评的强信号。
                    _gate_verdict = str(gate_verdict_map.get(nid, ""))
                    _gate_strength = int(gate_strength_map.get(nid, 0) or 0)
                    _GATE_PASS_THR = int(os.getenv("GATE_PASS_STRENGTH", "5"))  # 可调，默认 5（v176.5d）
                    if _gate_strength >= _GATE_PASS_THR:
                        # 不动 action, 让 LLM 给的 BUY/SELL 落库; 若 LLM 给 HOLD 也保留 HOLD
                        _gr.apply_pass("L3-MENWEI", f"strength={_gate_strength} verdict={_gate_verdict} → STRONG-PASS")
                        print(
                            f"  [GATE-STRONG-PASS] news=#{nid} strength={_gate_strength} verdict={_gate_verdict} → 信任强信号, action={action}",
                            flush=True,
                        )
                    elif _gate_verdict == "排除":
                        action = "HOLD"
                        _gr.apply_block("L3-MENWEI", f"verdict=排除 → HOLD")
                        print(
                            f"  [GATE-BLOCK] news=#{nid} 门卫标签={_gate_verdict} → HOLD（只分析不下单）",
                            flush=True,
                        )
                    # 其余（可交易 / 老数据残留的"只能看"）→ 信任 MECH/v174 后续闸门判断
                    if _gate_verdict == "只能看":
                        # 历史兜底：v176.5c 等价行为 — 保留 STRONG-PASS 路径，
                        # 否则让 LLM 自己的 action 走完成。打印一行便于排查老数据。
                        print(
                            f"  [GATE-LEGACY] news=#{nid} gate_verdict=只能看（老数据）→ 信任 LLM action={action}",
                            flush=True,
                        )
                    # ── v174 事件结构化闸门：verification / snapshot_proximity / 双空 → HOLD ──
                    # v176.5d: strength>=_GATE_PASS_THR 跳过 V174 (信任 LLM 强信号, 不让 verification="warn" 兜底强拦)
                    if _gate_strength >= _GATE_PASS_THR:
                        # 已通过 STRONG-PASS, 不再让 V174 把 LLM 给的 BUY/SELL 强制 HOLD
                        _v174_act = None
                        _gr.apply_skip("V174", f"strength={_gate_strength} → 跳过 V174")
                        print(
                            f"  [V174-SKIP] news=#{nid} strength={_gate_strength} → 跳过 V174 闸门 (trust strong signal)",
                            flush=True,
                        )
                    else:
                        _v174_act, _v174_reason = _v174_event_gate(res)
                        if _v174_act == "HOLD":
                            action = "HOLD"
                            _gr.apply_block("V174", _v174_reason)
                            print(
                                f"  [V174-GATE] news=#{nid} asset={str(res.get('target_asset') or '')} "
                                f"event_type={str(res.get('event_type_code') or '')} {_v174_reason}",
                                flush=True,
                            )
                        else:
                            _gr.apply_pass("V174", f"pass (verification/structure ok)")
                    # 留痕：真正落库的这一行的方向单（与 _focus_downgrade 的 gate
                    # 事件按 news_id+asset 对照，即可看出降级是在哪一步丢的）
                    _rp_trace = str(res.get("reasoning_path") or "")[:2000]
                    _signal_trace(
                        "row",
                        news_id=nid,
                        asset=str(res.get("target_asset") or ""),
                        action=str(action),
                        score=score,
                        is_sibling=1 if s.get("is_sibling") else 0,
                        hits=_asset_mentioned_stage_count(
                            _rp_trace, str(res.get("target_asset") or "")),
                        path_len=len(_rp_trace),
                        reasoning=str(res.get("reasoning") or "")[:120],
                        model=str(res.get("model_id") or ""),
                    )

                    # VIP score boost — multiply |score| by 1.25 when VIP matched
                    # 只对主决策行生效：sibling 副品种的 score 是 AI 对该品种的独立
                    # 判断，用主新闻 VIP 放大会失真（地缘新闻若含 VIP 人物，不应把
                    # 副品种 XAU 的分数也 ×1.25）。
                    is_sibling_row = bool(s.get("is_sibling"))
                    news_text_for_vip = content_map.get(nid, "")
                    vip_tag, vip_name = _detect_vip(news_text_for_vip)
                    if (not is_sibling_row) and vip_tag and action in ("BUY", "SELL"):
                        boosted = round(score * VIP_SCORE_BOOST, 4)
                        if abs(boosted) <= 1.0:
                            score = boosted
                    category = res.get("market_category", "") or ""
                    asset = res.get("target_asset", "NONE")

                    # ── 阶段2：多模型共识闸门（在聚合/建仓前执行）──
                    # verification 由"仅留痕"升级为"参与主决策降级"：交叉验证只在
                    # 主品种做（副品种 peer_reviews=None → verification=None 天然跳过）。
                    # 放在聚合前，确保降级后的 HOLD 行不会按旧方向被误聚合到 BUY/SELL parent。
                    verification = _build_verification(res, s.get("peer_reviews"))
                    # v176: verification 兜底（schema minLength=4，否则 INSERT 写 NULL 会让前端读到 None）
                    if not verification or len(str(verification).strip()) < 4:
                        if is_sibling_row:
                            verification = "skip | 副品种无 peer_reviews"
                        else:
                            verification = "skip | 单模型无交叉验证"
                    if (not is_sibling_row) and _consensus_downgrade(res, verification):
                        score = 0.05
                        action = "HOLD"
                        tagged_reason = f"[{label}] {res['reasoning']}"
                        _gr.apply_block("CONSENSUS", f"多模型共识不足（verification={verification}）")
                        print(
                            f"  [CONSENSUS-GATE] {asset} 多模型共识不足"
                            f" → 方向单降级 HOLD（不下单）",
                            flush=True,
                        )
                    elif is_sibling_row:
                        _gr.apply_skip("CONSENSUS", "副品种无 peer_reviews，跳过交叉验证")
                    else:
                        _gr.apply_pass("CONSENSUS", f"verification={verification}")

                    # ── 阶段2.5：Hermes 四席方向票软校验（多数反对 → 降分+留痕，不强制改单）──
                    if (not is_sibling_row) and HERMES_AGENT_ENABLED and action in ("BUY", "SELL"):
                        try:
                            hv = hermes_agent.desk_direction_votes(
                                content_map.get(nid, ""), market_context, asset)
                            tally = hermes_agent.desk_vote_summary(hv, action)
                            if tally["dissent"]:
                                factor = 0.8
                                score = round(max(-1.0, min(1.0, score * factor)), 4)
                                _signal_trace(
                                    "hermes_dissent",
                                    news_id=nid,
                                    asset=asset,
                                    action=action,
                                    opposed=tally["opposed"],
                                    actionable=tally["actionable"],
                                    votes=json.dumps(hv, ensure_ascii=False)[:500],
                                )
                                print(
                                    f"  [HERMES] {asset} 四席多数反对 {action}"
                                    f"（{tally['opposed']}/{tally['actionable']}）→ 软降分 ×{factor}，"
                                    f"不强制改单",
                                    flush=True,
                                )
                        except Exception:
                            pass

                    # ── Aggregation: bundle into parent if same asset+direction ──
                    parent_id: int | None = None
                    agg_key = ""
                    # 聚合判据 = 「方向相同」，不再用 sentiment_score 绝对值当门槛。
                    # 原因（v316 审计实测）：sentiment_score 有 94.6% 塌陷为 0.0
                    #   （LLM 中性化 50 + HOLD 压平 + 降级路径写 0/0.05），
                    #   |score| >= _AGG_MIN_SCORE(0.25) 几乎从不成立 →
                    #   聚合从不触发 → parent_id 恒 None → cluster_size 恒 1 →
                    #   decision_guard 的 cluster_heat 因子(权重 0.10) 永久为 0 →
                    #   final_score 更无区分度 → 更多 HOLD（自我强化死循环）。
                    # 聚合本意是「同族新闻聚类」，判据应是方向一致而非情绪分高低；
                    #   情绪分仍用于 _bump_parent_score 的加权聚合，语义不变。
                    if action in ("BUY", "SELL"):
                        agg_key = f"{category}|{asset}|{action}"
                        if agg_key in parent_cache:
                            parent_id = parent_cache[agg_key]
                        else:
                            parent_id = _find_active_parent(
                                conn, category, asset, action
                            )
                            parent_cache[agg_key] = parent_id

                    # ── 元数据提取 (LLM 可能不返回 → 默认值兜底) ──
                    pred_type = res.get("prediction_type", "continuation")
                    evt_phase = res.get("event_phase", "mid")
                    mkt_confirm = res.get("market_confirmation", "unknown")
                    exp_horizon = res.get("expected_horizon", "1-3d")
                    inval_cond = res.get("invalidation_condition", "")
                    # Phase 2: Event Quality Layer
                    evt_strength = res.get("event_strength", "medium")
                    direct_cat = 1 if res.get("direct_catalyst", False) else 0
                    tf_match = res.get("timeframe_match", "intraday")
                    # 影响评估（0-10）
                    heat_score = res.get("heat_score", 5.0)
                    # v336：统一走 resolve_impact_score —— 模型缺失/恰为 5.0 时用门卫分
                    # （旧实现只覆盖"恰为 5.0"，而 _parse_result 会把缺失写成 5.0,
                    #  两者本来是同一个值，所以门卫分在多数路径上并没被用上）
                    _gate_imp = int(gate_impact_map.get(nid, 0) or 0)
                    _gate_str = int(gate_strength_map.get(nid, 0) or 0)
                    impact_score, _impact_src = resolve_impact_score(
                        res.get("impact_score"), _gate_imp, gate_strength=_gate_str)
                    # v397：把"这个分从哪来"落进 field_sources ——
                    # 5.0 到底是模型判断还是兜底，复盘时必须能分辨。
                    try:
                        _mark_field_source(
                            res, "impact_score", _impact_source_label(_impact_src))
                    except Exception:
                        pass
                    asset_impact = res.get("asset_impact", 5.0)

                    # ── 【v181 P1+P2 前置】guard evaluate 在 INSERT 之前先跑 ──
                    # 把 decision_guard 评估从 INSERT 之后前移到 INSERT 之前，让 evidence_action / confidence
                    # 在 INSERT 时就能决定 suggested_action。这样下游 quick_sim 读 ai_decisions.suggested_action
                    # 不会被 AI 原始 action 误导而误建仓（之前 INSERT 后 UPDATE 才能修正，期间 quick_sim 仍可能建仓）。
                    _orig_action = action  # 留底，便于排查
                    _guard_pre: Dict[str, Any] = {
                        "action": action,
                        "confidence": 0.0,
                        "verdict": "guard_pre_failed",
                        "confidence_detail": {"passed_gate": False},
                    }
                    try:
                        _history_sample_pre = evidence.collect_settled_sample(asset, connection=conn)
                        # 前置评估的 cluster_size 必须用真实同族数量（与下面 post-INSERT 的
                        # cluster_hint 同源）。原来这里写死 1，导致「前置算出的
                        # suggested_action / evidence_action / evidence_confidence」
                        # 全部建立在 cluster_size=1 之上 → cluster_heat 因子(权重 0.10)
                        # 永久为 0。post-INSERT 的那次评估虽然传了真实值，但它的
                        # final_score 只写进 factor_snapshots，不回写 ai_decisions。
                        _cluster_hint_pre = 1
                        if parent_id is not None:
                            _p_row_pre = conn.execute(
                                "SELECT child_count FROM ai_decisions WHERE id = ?",
                                (parent_id,),
                            ).fetchone()
                            if _p_row_pre:
                                # v322：同 10680 —— 脏 child_count 不得炸掉整批落库
                                _cluster_hint_pre = _safe_child_count(
                                    _p_row_pre["child_count"]) + 1
                        # v405: 基本面读数（零网络读库；模块内 30 分钟缓存，批内共用）
                        try:
                            _fund_pre = fundamental_factor.build(
                                asset, conn=conn,
                                hint=_fundamental_hint(res, locals().get("news_text_for_vip", "")))
                        except Exception as _fund_exc:
                            _fund_pre = {"available": False,
                                         "detail": f"基本面因子跳过: {type(_fund_exc).__name__}"}
                        _guard_pre = decision_guard.evaluate_decision({
                            "sentiment_score": score,
                            "fundamental": _fund_pre,
                            # v318：把 AI 的方向判断一起给闸门 —— 闸门据此对齐锚点符号
                            # （历史数据存在「SELL 但分数为正」的错位）。
                            "suggested_action": action,
                            "target_asset": asset,
                            "market_confirmation": mkt_confirm,
                            "decision_context": decision_context,
                            "cluster_size": _cluster_hint_pre,
                            "history_sample": _history_sample_pre,
                            "heat_score": heat_score,
                            "impact_score": impact_score,
                            "asset_impact": asset_impact,
                            "gate_verdict": _gate_verdict,
                        })
                    except Exception as _exc:
                        print(f"  [GUARD-PRE] 前置评估失败: {type(_exc).__name__}: {str(_exc)[:80]}", flush=True)
                        _guard_pre = {
                            "action": "HOLD",
                            "confidence": 0.0,
                            "verdict": f"guard_pre_failed:{type(_exc).__name__}",
                            "confidence_detail": {"passed_gate": False},
                        }
                    _evidence_action_pre = str(_guard_pre.get("action") or "HOLD").upper()
                    # ── v399 诚实口径：置信度只在"真有因子可用"时才算数 ────────────
                    # 降级路径（target_none / llm_empty / summary_only / server_fallback）
                    # 与 guard 异常兜底里 coverage=0 —— 此时 0.0 不是"置信度很低"，而是
                    # "没有证据可算"。写 0 会被统计成"低分决策"（实测 447/494 条被算进
                    # 「置信度=0」），违反铁律 4「0 不得作为有效值参与统计」。
                    # 判定集中在 confidence_availability（纯函数，可离线单测）：
                    # 落库无证据写 NULL；闸门给"无可证据"的独立原因；统计口径单列。
                    try:
                        import confidence_availability as _cav
                        _conf_unavailable = _cav.is_unavailable(_guard_pre)
                    except Exception:
                        _conf_unavailable = True
                    # v404：guard 没给 confidence 时不能"0.0 顶上"——0 会被下游当成
                    # "算出来的低分"参与统计。这里标记为不可用，落库写 NULL。
                    _conf_raw = _guard_pre.get("confidence")
                    if _conf_raw is None:
                        _conf_unavailable = True
                        _evidence_confidence_pre = 0.0
                    else:
                        try:
                            _evidence_confidence_pre = float(_conf_raw)
                        except (TypeError, ValueError):
                            _conf_unavailable = True
                            _evidence_confidence_pre = 0.0
                    _force_hold_reason = ""

                    # ── P2：reasoning_path 超长 → confidence 扣减（过度分析惩罚）──
                    # 阈值/扣分由 .env 控制：OVERANALYSIS_THRESHOLD（默认 800）、OVERANALYSIS_PENALTY（默认 10）。
                    # 每超过阈值 100 字再扣 1 分（封顶额外扣 20）。
                    _rp_path = str(res.get("reasoning_path") or "")
                    _rp_len = len(_rp_path)
                    try:
                        _over_thr = int(os.getenv("OVERANALYSIS_THRESHOLD", "1800"))
                    except (TypeError, ValueError):
                        _over_thr = 800
                    try:
                        _over_pen = float(os.getenv("OVERANALYSIS_PENALTY", "10"))
                    except (TypeError, ValueError):
                        _over_pen = 10.0
                    _over_penalized = False
                    # v396 P3：不再按字数罚。全部交给 text_quality.overanalysis_penalty：
                    # 长 + 结构完整 + 不重复 → 不罚（原来会把"推演认真"当缺点，实测把
                    # 全库唯一一条 BTC SELL 从 17.9 压到闸门下）。真正罚的是重复啰嗦。
                    try:
                        from common import text_quality as _tq
                        _over_factor, _over_note = _tq.overanalysis_penalty(
                            _rp_path, threshold=_over_thr, base_pen=_over_pen)
                    except Exception:
                        _over_factor, _over_note = 1.0, ""
                    if _over_factor < 0.999:
                        _evidence_confidence_pre = max(
                            0.0, _evidence_confidence_pre * _over_factor)
                        _over_penalized = True
                        _force_hold_reason = _over_note

                    # ── P1-a：evidence_action=HOLD 强约束 → 强制 action=HOLD ──
                    # AI 给出 BUY/SELL 但 evidence 判定 HOLD → 禁止 BUY/SELL 覆盖（之前
                    # quick_sim 可能在 UPDATE 之前就按 AI 原始 action 建仓，这是 P1 根因）。
                    # v176.5e: strength>=_GATE_PASS_THR (STRONG-PASS) 跳过 evidence 闸门, 信任 LLM 强信号
                    if _gate_strength >= _GATE_PASS_THR and _evidence_action_pre == "HOLD" and action in ("BUY", "SELL"):
                        _gr.apply_skip("EVIDENCE", f"strength={_gate_strength} → 信任强信号, 跳过 evidence=HOLD")
                        print(f"  [HOLD-GATE-SKIP] {asset} {action} evidence=HOLD 但 strength={_gate_strength} → 信任强信号, 不强制 HOLD", flush=True)
                    elif _evidence_action_pre == "HOLD" and action in ("BUY", "SELL"):
                        _force_hold_reason = (
                            _force_hold_reason + ("；" if _force_hold_reason else "")
                            + f"evidence_action=HOLD 强约束：AI 给 {_orig_action} 但 evidence 判定 HOLD → 禁止 BUY/SELL 覆盖"
                        )
                        action = "HOLD"
                        tagged_reason = _force_hold_reason
                        _gr.apply_block("EVIDENCE", f"evidence=HOLD 强约束（AI 给 {_orig_action}）")
                        print(f"  [HOLD-GATE] {asset} {_orig_action} → HOLD (evidence=HOLD)", flush=True)
                    else:
                        _gr.apply_pass("EVIDENCE", f"evidence={_evidence_action_pre} (confidence={_evidence_confidence_pre:.1f})")

                    # ── v318：过度分析惩罚后的**最终再闸门** ──────────────────
                    # 原来只扣分不重新判定：置信度已经被扣到闸门以下，action 却仍是
                    # BUY/SELL → 出现「confidence=3.2 但 action=BUY」的自相矛盾，
                    # 前端看到的也是"有方向单但统一置信度极低"。
                    # 这里以闸门为唯一裁决：置信度低于闸门就必须是 HOLD。
                    if _over_penalized and action in ("BUY", "SELL"):
                        try:
                            _gate_final = float(evidence.confidence_gate())
                        except (TypeError, ValueError):
                            _gate_final = 10.0
                        if _evidence_confidence_pre < _gate_final:
                            _force_hold_reason = (
                                _force_hold_reason + ("；" if _force_hold_reason else "")
                                + f"过度分析惩罚后置信度 {_evidence_confidence_pre:.1f} "
                                  f"< 闸门 {_gate_final:.0f} → 降级 HOLD"
                            )
                            action = "HOLD"
                            tagged_reason = _force_hold_reason
                            _gr.apply_block(
                                "EVIDENCE",
                                f"over-analysis penalty → confidence={_evidence_confidence_pre:.1f} "
                                f"< gate={_gate_final:.0f}",
                            )
                            print(
                                f"  [OVERANALYSIS-GATE] {asset} → HOLD "
                                f"(confidence={_evidence_confidence_pre:.1f} < {_gate_final:.0f})",
                                flush=True,
                            )

                    # ── P1-b：BTC SELL 严格闸门 ──
                    # BTC 方向偏好：SELL BTC 必须 evidence_action=SELL 且 confidence ≥ 60（BTC_SELL_CONFIDENCE_GATE）。
                    try:
                        _btc_gate_thr = float(os.getenv("BTC_SELL_CONFIDENCE_GATE", "60"))
                    except (TypeError, ValueError):
                        _btc_gate_thr = 60.0
                    if action == "SELL" and str(asset or "").upper() == "BTC":
                        # v176.5e: strength>=_GATE_PASS_THR (STRONG-PASS) 跳过 BTC SELL 严格闸门
                        if _gate_strength >= _GATE_PASS_THR:
                            _gr.apply_skip("BTC-SELL", f"strength={_gate_strength} → 信任强信号")
                            print(f"  [BTC-GATE-SKIP] {asset} SELL strength={_gate_strength} → 信任强信号, 不强制 HOLD", flush=True)
                        elif _evidence_action_pre != "SELL" or _evidence_confidence_pre < _btc_gate_thr:
                            _btc_msg = (
                                f"BTC SELL 严格闸门：BTC 方向偏好 → SELL BTC 必须 evidence_action=SELL 且 "
                                f"confidence≥{_btc_gate_thr:.0f}；实际 evidence_action={_evidence_action_pre}, "
                                f"confidence={_evidence_confidence_pre:.1f} → 降级 HOLD"
                            )
                            _force_hold_reason = (
                                _force_hold_reason + ("；" if _force_hold_reason else "")
                                + _btc_msg
                            )
                            action = "HOLD"
                            tagged_reason = _force_hold_reason
                            _gr.apply_block("BTC-SELL", _btc_msg)
                            print(f"  [BTC-GATE] {asset} SELL 不通过 → HOLD", flush=True)
                        else:
                            _gr.apply_pass("BTC-SELL", f"evidence=SELL confidence={_evidence_confidence_pre:.1f}")

                    # 规则事件结构化抽取 → 落库 JSON，供"规则 vs LLM"交叉校验标黄（主行/副行复用缓存）
                    event_struct_json = event_struct_cache.get(nid)
                    if event_struct_json is None:
                        try:
                            from event_struct import extract_events as _extract_events
                            event_struct_json = json.dumps(
                                _extract_events(content_map.get(nid, "") or ""), ensure_ascii=False
                            )[:1000]
                        except Exception:
                            event_struct_json = ""
                        event_struct_cache[nid] = event_struct_json
                    # PR-3: 同步把事件原子写入 event_atom 表（独立于 ai_decisions 行）
                    # 失败 try/except 兜底 — 绝不阻断主 INSERT 流程
                    try:
                        import event_atom as _event_atom
                        _news_row_ts = conn.execute(
                            "SELECT ts FROM raw_news WHERE id = ?", (nid,)
                        ).fetchone()
                        _news_ts = int(_news_row_ts["ts"]) if _news_row_ts and _news_row_ts["ts"] else int(time.time())
                        _before_atom = conn.execute(
                            "SELECT COUNT(*) FROM event_atom WHERE raw_news_id=?", (nid,)
                        ).fetchone()[0]
                        _event_atom.extract_and_persist(
                            content_map.get(nid, "") or "",
                            raw_news_id=int(nid),
                            default_event_time=_news_ts,
                            event_time_source="ai_worker_review",
                            event_time_confidence=0.7,
                            # v171+：LLM 输出的时间结构化字段（来自 _ai_worker_loop 批 LLM 结果）
                            impact_curve=res.get("impact_curve") if isinstance(res, dict) else None,
                            event_window_minutes=res.get("event_window_minutes") if isinstance(res, dict) else None,
                            data_anchor_ts=res.get("data_anchor_ts", "") if isinstance(res, dict) else "",
                            fallback_used=int(bool(res.get("_fallback_used"))) if isinstance(res, dict) else 0,
                            connection=conn,
                        )
                        _after_atom = conn.execute(
                            "SELECT COUNT(*) FROM event_atom WHERE raw_news_id=?", (nid,)
                        ).fetchone()[0]
                        # v174: 规则没命中 → 用 LLM 输出的 event_type_code 兜底回填 1 行
                        # 解决"22 类正则词典外的 news 永远不进 event_atom"的问题
                        if _after_atom == _before_atom:
                            _llm_code = str(res.get("event_type_code") or "").strip()
                            # v176.5: LLM 也没给 code → 强制落 'market_event' 占位行
                            # 保证每条新闻都有 event_atom 行 (供 refresh_backtest_event_outcomes JOIN)
                            if not _llm_code:
                                _llm_code = "market_event"
                            # 校验是 dim_event_type 里合法的 code, 转 FK id
                            _det = conn.execute(
                                "SELECT id FROM dim_event_type WHERE code=? AND active=1", (_llm_code,)
                            ).fetchone()
                            # dim_event_type 没有此 code → 尝试动态插入 (market_event 兜底)
                            if not _det and _llm_code == "market_event":
                                _det_id = conn.execute(
                                    "INSERT INTO dim_event_type (code, name_zh, name_en, category, active) "
                                    "VALUES ('market_event', '市场事件', 'Market Event', 'other', 1) "
                                    "ON CONFLICT(code) DO UPDATE SET active=1 RETURNING id"
                                ).fetchone()
                                if _det_id:
                                    _det = {"id": _det_id[0]} if isinstance(_det_id, tuple) else _det_id
                            if _det:
                                _atom_id = conn.execute(
                                    "INSERT INTO event_atom "
                                    "(raw_news_id, event_type_id, asset_id, event_time, "
                                    " event_time_confidence, event_time_source, "
                                    " direction, direction_confidence, direction_source, "
                                    " evidence_count, parser_version, created_at) "
                                    # v176.5 fix: asset_id NOT NULL DEFAULT 0, created_at NOT NULL
                                    "VALUES (?, ?, 0, ?, 0.6, 'llm_fallback', "
                                    "        'unknown', 0.0, 'llm', 1, 'llm_fallback.v1', ?)",
                                    (nid, _det["id"] if isinstance(_det, dict) else _det[0], _news_ts, int(time.time())),
                                ).lastrowid
                                print(f"  [LLM-FALLBACK-EA] news={nid} event_type={_llm_code} atom_id={_atom_id}", flush=True)
                                # v318：llm_fallback 这一路也要留事件时间候选 ——
                                # 原来只写 event_atom，事件时间候选表缺这一路，溯源链断在这里。
                                try:
                                    _event_atom.insert_event_time_candidate(
                                        raw_news_id=int(nid),
                                        candidate_ts=int(_news_ts),
                                        source="llm_fallback",
                                        confidence=0.6,
                                        reasoning=f"LLM 未给出结构化事件，按 LLM 兜底类型({_llm_code})落库",
                                        is_primary=False,
                                        connection=conn,
                                    )
                                except Exception as _cand_err:
                                    print(f"  [LLM-FALLBACK-CAND] news={nid} skipped: {_cand_err}", flush=True)
                    except Exception as _ea_err:
                        print(f"  [AI-EVENT-ATOM] news={nid} skipped: {_ea_err}", flush=True)

                    # v176.2 (backlog item 15): 收集本次产生的 event_atom_ids, 写进 ai_decisions.
                    # 双向索引: ai_decisions.event_atom_ids → atom; event_atom.raw_news_id → news.
                    _event_atom_ids_json = ""
                    try:
                        _atom_rows = conn.execute(
                            "SELECT id FROM event_atom WHERE raw_news_id=? ORDER BY id",
                            (nid,),
                        ).fetchall()
                        if _atom_rows:
                            _event_atom_ids_json = json.dumps(
                                [r["id"] for r in _atom_rows], ensure_ascii=False
                            )
                    except Exception as _ea_ids_err:
                        print(f"  [AI-EVENT-ATOM-IDS] news={nid} skipped: {_ea_ids_err}", flush=True)

                    # v173+ 结构化输入层旁路: 拼 6 字段 payload + LLM 返回 reasoning 后做引用校验。
                    # 不动原 LLM 调用路径。原推理逻辑 (解析/打分/反思/沉淀) 一字不改。
                    # 任何上游失败 -> 对应字段填 [],{},null,绝不阻断 INSERT。
                    _structured_input_json = ""
                    _structured_check_json = ""
                    # v414：**必须**在外层先初始化。
                    # _news_ts_val 原来只在下面这个 try 内部（12656 行）赋值；一旦该 try 在
                    # 更早的位置抛出（实测 09:00 那批 `import structured_input` / 快照查询失败），
                    # 后面「落库前抢救」读它就会 UnboundLocalError → 整段抢救被 except 吞掉 →
                    # 本该补回标的的新闻被判 NONE。日志特征：`[落库前抢救] 跳过: UnboundLocalError`
                    _news_ts_val = None
                    try:
                        import structured_input as _si
                        import structured_check as _sc

                        # PR-5 兜底 refresh: 若 backtest_event_outcomes 表空 / 超过 6h 未刷,
                        # 在 hook 阶段轻量刷一次(失败不阻断主流程)。
                        # 主调度在 api_server / scheduler 调用 refresh_backtest_event_outcomes(),
                        # 此处只是「在线兜底」,确保 baseline.by_type 不长期为空。
                        # v174: 阈值从 6h 缩到 1h, 让 baseline 数据更"新鲜"; min_sample=1 让
                        # 冷启动期小样本也进 by_type (给 LLM 看到结构, 而不是空 dict)
                        try:
                            import db as _db_hook
                            import time as _time_hook
                            # v322: 进程内节流 —— 同一进程 1 小时内只真正 refresh 一次。
                            # 事故原因：refresh 写在批事务里（批失败即回滚）→ 表长期为空 →
                            # MAX(last_updated) 恒为 None → 每条新闻都重跑一次 90 天 5 表 JOIN，
                            # 落库阶段每条多耗 1-2 分钟，整批 16 条要十几分钟 → 「待分析」降得极慢。
                            global _BASELINE_REFRESH_TS
                            _now_hook = _time_hook.time()
                            if (_now_hook - _BASELINE_REFRESH_TS) < 1 * 3600:
                                pass  # 节流窗口内，跳过重活
                            else:
                                _last_refresh = conn.execute(
                                    "SELECT MAX(last_updated) FROM backtest_event_outcomes"
                                ).fetchone()[0]
                                _needs_refresh = True
                                if _last_refresh:
                                    # last_updated 是字符串（SQLite 无 datetime 类型），
                                    # 直接 time.time() - str 会 TypeError → 被外层 except 吞掉
                                    # → 每条都重刷。必须先解析成时间戳再比较。
                                    #
                                    # v322 关键修复：局部变量**不能叫 `_ts`**。
                                    # ai_worker.py 顶层 `from .utils import ... _ts` 导入的是
                                    # 一个**函数**；Python 规定「函数内只要出现同名赋值，
                                    # 该名字在整个函数内都是局部变量」→ 同一函数里任何
                                    # `_ts()` 调用都会抛
                                    #   UnboundLocalError: cannot access local variable '_ts'
                                    # → 整批 16 条落库回滚 → 「待分析」永不下降。
                                    # （实测 2026-11-05：Batch persist 失败 UnboundLocalError）
                                    _last_refresh_ts = None
                                    for _fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
                                        try:
                                            _last_refresh_ts = _time_hook.mktime(
                                                _time_hook.strptime(str(_last_refresh), _fmt)
                                            )
                                            break
                                        except Exception:
                                            continue
                                    if _last_refresh_ts is None:
                                        try:  # epoch 兜底
                                            _last_refresh_ts = float(_last_refresh)
                                        except Exception:
                                            _last_refresh_ts = None
                                    if _last_refresh_ts is not None:
                                        _needs_refresh = (
                                            (_now_hook - _last_refresh_ts) > 1 * 3600
                                        )
                                if _needs_refresh:
                                    _db_hook.refresh_backtest_event_outcomes(
                                        conn, days=90, min_sample=1
                                    )
                                    _BASELINE_REFRESH_TS = _now_hook
                                    print(
                                        f"  [BASELINE-HOOK] 在线 refresh baseline "
                                        f"(last_refresh={_last_refresh})",
                                        flush=True,
                                    )
                        except Exception as _bl_exc:
                            # v322: baseline 回写失败 → 回测命中率静默失真（指标看着正常，
                            # 实际是旧数据），且没有任何线索
                            print(f"  [AI] ⚠️ baseline refresh 失败: "
                                  f"{type(_bl_exc).__name__}: {str(_bl_exc)[:120]}",
                                  flush=True)

                        # 1) 查 event_atom 行的快照(PR-3 已落库) + raw_news 原文 + market_snapshot
                        # FIX(v173+): raw_news 实际字段名是 content,不是 text —— 之前用 text 触发
                        # OperationalError 走 except,导致 structured_input/check 永远空字符串。
                        # 这里同时取两个字段并 fallback,兼容旧/新 schema。
                        try:
                            _news_for_payload = conn.execute(
                                "SELECT id, source, content AS text, published_at, ts,"
                                " category, symbols, source_url FROM raw_news WHERE id = ?",
                                (nid,),
                            ).fetchone()
                        except Exception:
                            # 旧 schema 兼容: 字段更少
                            _news_for_payload = conn.execute(
                                "SELECT id, source, content AS text, published_at, ts"
                                " FROM raw_news WHERE id = ?",
                                (nid,),
                            ).fetchone()
                        # FIX(v173+): event_atom 真实 schema 是 asset_id(外键) → dim_asset.code
                        # 不是 asset_code。event_type_id → dim_event_type.code 正确。
                        # 用 try/except 兼容未来 dim_asset 可能缺表的场景。
                        try:
                            _event_atom_rows = conn.execute(
                                """SELECT ea.id, ea.event_time, ea.actor,
                                          ea.magnitude_value, ea.magnitude_unit,
                                          da.code AS asset_code,
                                          ea.direction, ea.direction_confidence, ea.direction_source,
                                          ea.evidence_count, det.code AS event_type_code,
                                          det.name_zh AS type_name_zh
                                   FROM event_atom ea
                                   LEFT JOIN dim_event_type det ON det.id = ea.event_type_id
                                   LEFT JOIN dim_asset da ON da.id = ea.asset_id
                                   WHERE ea.raw_news_id = ?
                                   ORDER BY ea.id LIMIT 8""",
                                (nid,),
                            ).fetchall()
                        except Exception:
                            # 兼容: 没有 dim_asset 表/列时退到只用 event_atom
                            try:
                                _event_atom_rows = conn.execute(
                                    """SELECT ea.id, ea.event_time, ea.actor,
                                              ea.magnitude_value, ea.magnitude_unit,
                                              NULL AS asset_code,
                                              ea.direction, ea.direction_confidence, ea.direction_source,
                                              ea.evidence_count,
                                              (SELECT code FROM dim_event_type WHERE id=ea.event_type_id) AS event_type_code,
                                              (SELECT name_zh FROM dim_event_type WHERE id=ea.event_type_id) AS type_name_zh
                                       FROM event_atom ea WHERE ea.raw_news_id = ?
                                       ORDER BY ea.id LIMIT 8""",
                                    (nid,),
                                ).fetchall()
                            except Exception:
                                _event_atom_rows = []

                        # 2) event_struct 已抽事件(降级用)
                        _event_struct_events = []
                        if event_struct_json:
                            try:
                                _event_struct_events = json.loads(event_struct_json) or []
                            except Exception:
                                _event_struct_events = []

                        # 3) 拼 payload (news_row 是 _news_for_payload)
                        # v174: 传 SERVER VERDICT 缓存的完整 market_snapshot_dict + news_ts,
                        #       让 structured_input.snapshot 不再是 [],并注入价格时间序列。
                        # snap[ts] 是 server 抓快照时的 server-side 时间戳,作为 snap_ts。
                        try:
                            _msd_raw = _SERVER_SNAPSHOT_CACHE.get("json") if isinstance(_SERVER_SNAPSHOT_CACHE, dict) else None
                            if isinstance(_msd_raw, str):
                                import json as _json
                                try:
                                    _msd = _json.loads(_msd_raw)
                                except Exception:
                                    _msd = None
                            elif isinstance(_msd_raw, dict):
                                _msd = _msd_raw
                            else:
                                _msd = None
                        except Exception:
                            _msd = None
                        # news_ts: 优先 raw_news.timestamp (UTC 秒);不存在用 published_at_iso 解析
                        try:
                            _news_ts_val = None
                            if 'timestamp' in _news_for_payload:
                                _news_ts_val = _news_for_payload['timestamp']
                            elif 'published_at' in _news_for_payload:
                                _news_ts_val = _news_for_payload['published_at']
                            if _news_ts_val:
                                _news_ts_val = int(_news_ts_val)
                        except Exception:
                            _news_ts_val = None

                        _payload = _si.build_structured_input(
                            _news_for_payload,
                            market_snapshot_dict=_msd,  # v174: 接通 SERVER VERDICT 时间结构化
                            event_struct_events=_event_struct_events,
                            event_atom_rows=_event_atom_rows,
                            connection=conn,
                            news_ts=_news_ts_val,  # v174: 事件时间锚
                        )
                        _structured_input_json = _si.serialize(_payload)

                        # 4) 引用校验(reasoning vs structured_input)
                        _reasoning_text = res.get("reasoning", "") or ""
                        _check = _sc.check_reasoning_vs_structured_input(
                            _reasoning_text, _payload
                        )
                        # v174: 把 JSON Schema + 价格幻觉校验结果合并进 _check
                        # 这样 structured_check 字段一次性包含了所有 v174 质量评估
                        try:
                            _v174_val = res.get("_v174_validation") if isinstance(res, dict) else None
                            if _v174_val:
                                _check["v174_quality"] = {
                                    "schema_valid": _v174_val.get("schema_valid"),
                                    "schema_errors_count": len(_v174_val.get("schema_errors", [])),
                                    "fabrication_rate": _v174_val.get("fabrication_rate"),
                                    "verdict": _v174_val.get("verdict"),
                                }
                        except Exception:
                            pass
                        _structured_check_json = _sc.serialize_check(_check)
                    except Exception as _si_err:
                        # 失败兜底:写空字符串,绝不阻断 INSERT
                        print(f"  [STRUCTURED-INPUT] news={nid} skipped: {_si_err}", flush=True)

                    # v176.2：INSERT 前查同 asset 的 prev/regime/bias，5 字段一并写入
                    _prev = _load_prev_decision(asset or "")

                    # v181：结构化产出摘要（7 项）在 INSERT 前拼好，final_action 复用最终 action 变量
                    _struct_summary_json = _build_struct_summary(res, action, asset)

                    # v186 经验系统：计算 persona_weight（actor 对 asset 的影响力权重）
                    # 同时记录命中的 experience_entries.id → ai_decisions.persona_match_id
                    try:
                        import experience_reflector as _er
                        # 从 news_source / news_text 提取 actor hint
                        _news_actor = ""
                        try:
                            if isinstance(news_source, str) and news_source:
                                _news_actor = news_source[:120]
                            else:
                                _nt = (res.get("news_text") or res.get("source_text") or "")
                                if _nt:
                                    _news_actor = _nt[:120]
                        except Exception:
                            _news_actor = ""
                        _pw = _er.compute_persona_weight(asset or "", _news_actor)
                        _pm_id = 0  # 命中 id(0=无命中, 中性)
                        try:
                            _pm_id = _er.get_matched_experience_id(asset or "", _news_actor)
                        except Exception:
                            _pm_id = 0
                    except Exception:
                        _pw, _pm_id = 1.0, 0

                    # v342: 落库前最后一道口径 —— 文案里的"目标资产 NONE / 不在核心监控清单"
                    # 必须与最终品种一致（历史 93 行就是 target_asset 已被 ASSETFIX 改成
                    # WTI/XAU，文案却仍写着 NONE，导致复盘时"降级原因"读不懂）。
                    try:
                        if asset:
                            rerender_degrade_text_for_asset(res, asset)
                    except Exception:
                        pass

                    # v342: 降级标记（"没分析"≠"判断为观望"）
                    _ad_flag, _ad_why = classify_analysis_degraded(
                        str(res.get("reasoning_path") or ""),
                        str(res.get("no_trade_reason") or ""),
                        str(asset or ""),      # v409: 原因码要看最终标的
                    )

                    # ── v346 落库前最后一道：真降级 + 真品种 → 用精简两步补一次 ──
                    # 为什么放在这里而不是主流程：实测 llm_empty 这类行并非来自主调用的
                    # 空响应（EMPTY RESPONSE 计数为 0），而是上游分流/副品种链把空结果
                    # 落成了降级文案。落库点是所有路径的共同终点，在这里补最省事也最确定。
                    _rs_state = (_RESCUE_SF_STATE
                                 if str(_ad_why or "") == "server_fallback"
                                 else _RESCUE_STATE)
                    _rs_limit = (_RESCUE_SF_LIMIT_PER_WINDOW
                                 if str(_ad_why or "") == "server_fallback"
                                 else None)
                    if (_ad_flag == 1
                            and str(os.getenv("TRIDENT_TWO_STEP_LEANPROMPT", "1")).strip() != "0"
                            and str(asset or "").strip().upper() not in ("", "NONE", "OTHER")
                            and (_RESCUE_MAX_PER_BATCH <= 0
                                 or _rescue_used_in_batch < _RESCUE_MAX_PER_BATCH)
                            and rescue_budget_ok(time.time(), _rs_state, limit=_rs_limit)):
                        _rs_try = True
                        _rescue_used_in_batch += 1
                    else:
                        _rs_try = False
                        if _ad_flag == 1 and str(asset or "").strip().upper() not in ("", "NONE", "OTHER"):
                            # v409.3: 这里**不能**引用 decision_id —— 它要等 INSERT 之后
                            # （`decision_id = cur.lastrowid`，约 13800 行）才被赋值。
                            # 之前写的是 `"decision": decision_id`，后果有两层：
                            #   ① 本批第一条就抛 UnboundLocalError → 整段预算判定被 except
                            #      吞掉（rescue_log 实证：news=35047 `skip=exception:UnboundLocalError`）；
                            #   ② 后面的条目不会抛，但会捞到**上一条新闻**残留的 decision_id
                            #      → 日志把预算拒绝记到了错误的决策上（实证 34860 -> 12100）。
                            # 抢救发生在落库之前，本来就没有 decision id，写 0 才是事实。
                            _rescue_log({"news": nid, "decision": 0,
                                         "why": str(_ad_why or ""), "ok": 0,
                                         "skip": ("batch_cap" if (
                                             _RESCUE_MAX_PER_BATCH > 0
                                             and _rescue_used_in_batch >= _RESCUE_MAX_PER_BATCH
                                         ) else "budget_exhausted")})
                    if _rs_try:
                        try:
                            _nid_txt = ""
                            try:
                                _row_t = conn.execute(
                                    "SELECT content FROM raw_news WHERE id=?", (nid,)
                                ).fetchone()
                                if _row_t and _row_t[0]:
                                    _nid_txt = str(_row_t[0])
                            except Exception:
                                _nid_txt = ""
                            if _nid_txt.strip():
                                _cfg_t = _pick_main_model_cfg()
                                if _cfg_t:
                                    _ra_p = _parse_result_light(two_step_llm_call(
                                        _cfg_t, mode="a", news_text=_nid_txt,
                                        timestamp=str(_news_ts_val or ""),
                                        timeout=float(_cfg_t.get("timeout") or 45.0),
                                        call_site="persist_fix_a",
                                    ) or "")
                                    _ta_p = str(_ra_p.get("target_asset") or "").strip().upper()
                                    if _ta_p and _ta_p not in ("NONE", "OTHER"):
                                        _rb_p = _parse_result_light(two_step_llm_call(
                                            _cfg_t, mode="b", news_text=_nid_txt,
                                            timestamp=str(_news_ts_val or ""),
                                            asset=_ta_p,
                                            action=str(_ra_p.get("suggested_action") or "HOLD"),
                                            timeout=float(_cfg_t.get("timeout") or 60.0),
                                            call_site="persist_fix_b",
                                        ) or "")
                                        _mrg_p, _nts_p = merge_two_step(res, _ra_p, _rb_p)
                                        _rs_ok = (not is_skeleton_result(_mrg_p)
                                                  and str(_mrg_p.get("reasoning_path") or "").find("未返回可用分析") < 0)
                                        # v409.3: 同 13599 行的说明 —— 抢救在 INSERT 之前，
                                        # 没有 decision id；引用未赋值的 decision_id 会抛
                                        # UnboundLocalError（实证 news=35047）。写 0。
                                        _rescue_log({"news": nid, "decision": 0,
                                                     "why": str(_ad_why or ""), "ok": 1 if _rs_ok else 0,
                                                     "skip": "", "asset": asset})
                                        if _rs_ok:
                                            res = _mrg_p
                                            _ad_flag, _ad_why = classify_analysis_degraded(
                                                str(res.get("reasoning_path") or ""),
                                                str(res.get("no_trade_reason") or ""),
                                                str(asset or ""),   # v409
                                            )
                                            print(f"  [落库前抢救] news={nid} asset={asset} "
                                                  f"→ {_ta_p}，降级={_ad_why}", flush=True)
                        except Exception as _pf_exc:
                            # v409.1: 异常路径以前只 print 不落 rescue_log —— 09:51 那次上游
                            # HTTP 529 就是这样"看不见"的：一段抢救明明发生了，日志里却
                            # 只有一行 `跳过: RuntimeError`，rescue_log 一条都没有。
                            # 现在把异常也记一行（skip=exception:<类型>），让「没选中」
                            # 「上游挂了」「补完还是空」三种情况可分。
                            _err_txt = f"{type(_pf_exc).__name__}: {str(_pf_exc)[:120]}"
                            print(f"  [落库前抢救] 跳过: {_err_txt}", flush=True)
                            _rescue_log({"news": nid, "decision": 0,
                                         "why": str(_ad_why or ""), "ok": 0,
                                         "skip": f"exception:{type(_pf_exc).__name__}",
                                         "asset": asset, "err": _err_txt})

                    # v397 落库前最后一道：硬数据是**事实**，不是模型输出。
                    # 各解析分支（含降级/两步/兄弟链）都会漏掉回填，实测只有 21% 行带证据；
                    # 这里按最终品种统一补一次，已有值一律不覆盖。
                    try:
                        _hd_final = str(res.get("hard_data_evidence") or "").strip()
                        if (not _hd_final) or _hd_final.upper() in ("N/A", "NA"):
                            _ev_final = _hard_data_block(str(asset or ""))
                            if _ev_final:
                                res["hard_data_evidence"] = _ev_final[:500]
                                _mark_field_source(
                                    res, "hard_data_evidence", "rule_server_hard_data_v397")
                    except Exception:
                        pass

                    # v453: 源新闻已被重置/清理删除时，ai_decisions.news_id 的外键
                    # 必然失败，而这条 INSERT 在循环内、整批共用一个事务 ——
                    # 一条孤儿会把**整批已完成的分析**全部回滚（11-19 09:15 实证：
                    # Batch persist 失败 IntegrityError，8 条分析白跑）。
                    # 落库前先确认源新闻还在，不在就跳过这一条并留痕。
                    if not conn.execute(
                        "SELECT 1 FROM raw_news WHERE id=?", (nid,)
                    ).fetchone():
                        print(f"  [PERSIST] 跳过孤儿决策 news={nid}（源新闻已删除，避免整批回滚）",
                              flush=True)
                        continue

                    # v423：八段补齐（只对 BUY/SELL）—— 见 _maybe_repair_sections()
                    _sr_text = ""
                    try:
                        _rw_sr = conn.execute(
                            "SELECT content FROM raw_news WHERE id=?", (nid,)
                        ).fetchone()
                        _sr_text = str(_rw_sr[0]) if _rw_sr and _rw_sr[0] else ""
                    except Exception:
                        _sr_text = ""
                    _maybe_repair_sections(_sr_text, str(asset or ""), str(action or ""),
                                           res, _pick_main_model_cfg(),
                                           str(_news_ts_val or ""), tag=f"news#{nid}")

                    # v396.2：绑定参数逐项过 _db_safe —— 模型偶尔把字符串字段返回成
                    # list/dict（实测 reasoning_path 回来是个数组 → sqlite3
                    # "Error binding parameter 11: type 'list' is not supported"），
                    # 一个字段的类型错就能让**整批 8 条**回滚重来。这里在绑定前统一
                    # 把 list/dict 序列化成 JSON 文本，其余非标量转 str，保证"一条坏值
                    # 只影响它自己那一格，不会炸掉整批"。
                    # v417: factor_ranking 落库统一成 JSON（保留前 8 项），
                    # 不再用 str(list) 存 Python repr —— 检查器/因果图都按 JSON 读。
                    _fr_raw = res.get("factor_ranking") or []
                    if isinstance(_fr_raw, str):
                        _fr_json = _fr_raw
                    else:
                        try:
                            _fr_json = json.dumps(
                                (_fr_raw[:8] if isinstance(_fr_raw, list) else _fr_raw),
                                ensure_ascii=False)
                        except Exception:
                            _fr_json = "[]"
                    cur = conn.execute(
                        """
                        INSERT INTO ai_decisions
                          (news_id, sentiment_score, suggested_action,
                           reasoning, created_at, market_category, target_asset,
                           parent_id, child_count, aggregation_key, reasoning_path, vip_tag,
                           prediction_type, event_phase, market_confirmation,
                           expected_horizon, invalidation_condition,
                           event_strength, direct_catalyst, timeframe_match,
                           decision_context, strategy_id, strategy_version_id,
                           heat_score, impact_score, asset_impact, peer_reviews, is_sibling,
                           asset_reasoning, low_impact, asset_nature, factor_ranking,
                           impact_magnitude, verification, attention_score, elasticity_window, data_anchor_ts, hard_data_evidence, event_struct,
                           event_type_code, snapshot_proximity,
                           prev_decision_id, prev_action_same_asset, prev_score_same_asset,
                           regime_at_decision, bias_after_reflection,
                            structured_input, structured_check, event_atom_ids,
                             gate_chain, original_action, ungated_action,
                             structured_summary,
                             persona_weight, persona_match_id,
                             analysis_degraded, analysis_degraded_reason,
                             ts)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                ?, ?, ?, ?, ?,
                                ?, ?, ?,
                                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                ?, ?, ?);
                        """,
                        tuple(_db_safe(_px) for _px in (
                            nid,
                            score,
                            action,
                            tagged_reason,
                            batch_end,
                            category,
                            asset,
                            parent_id,
                            # v322 根因修复：这两个参数曾写反 —— child_count 收到 agg_key，
                            # aggregation_key 收到 0。后果：child_count 列被写成 'OIL|WTI|BUY'，
                            # 后续 int(p_row["child_count"]) 抛 ValueError → 整批 16 条
                            # 落库作废 → 队头反复占位 → 前端「待分析」永不下降。
                            0,                                          # child_count
                            agg_key if parent_id is None else "",       # aggregation_key
                            res.get("reasoning_path", ""),
                            vip_tag,
                            pred_type,
                            evt_phase,
                            mkt_confirm,
                            exp_horizon,
                            inval_cond[:200],  # 截断超长失效条件
                            evt_strength,
                            direct_cat,
                            tf_match,
                            decision_context,
                            current_strategy["id"],
                            active_version["id"],
                            heat_score,
                            impact_score,
                            asset_impact,
                            # 多模型交叉验证（验证模型独立分析结果 JSON；无则 NULL）
                            json.dumps(s.get("peer_reviews"), ensure_ascii=False) if s.get("peer_reviews") else None,
                            # 多品种：1=副决策行（同新闻其它品种）；0=主决策/普通单
                            1 if s.get("is_sibling") else 0,
                            # 品种相关推理逻辑（为什么这条新闻关联主/副品种）：仅主行存 AI 全量
                            str(res.get("asset_reasoning") or "")[:400] if not s.get("is_sibling") else "",
                            # "影响小"标记：方向单但 asset_impact 低于阈值（默认阈值 0 = 从不标记）。
                            # 先把"影响小的单子"标出来观察真实表现，再决定要不要硬拦（见 config 注释）。
                            1 if (
                                action in ("BUY", "SELL")
                                and MIN_ASSET_IMPACT_FOR_TRADE > 0
                                and float(asset_impact or 0) < MIN_ASSET_IMPACT_FOR_TRADE
                            ) else 0,
                            # ── 八步新增字段 ──
                            str(res.get("asset_nature") or "")[:80],
                            # v417: 这里原来是 str(list)[:500] —— 落库成 Python repr
                            # （单引号 + 500 字截断）。检查器按 JSON 解析必然拿到空，
                            # 于是"有因子"的方向单被 E001c 误报成"无因子方向"（7 天 40 条）。
                            # 改成 JSON：保留前 8 项（足够表达排序），不截断出半个对象。
                            (_fr_json[:4000]),
                            res.get("impact_magnitude"),
                            verification,
                            res.get("attention_score"),
                            str(res.get("elasticity_window") or "")[:200],
                            str(res.get("data_anchor_ts") or "")[:60],
                            str(res.get("hard_data_evidence") or "")[:500],
                            event_struct_json,
                            # v174 新增三字段: event_type_code 必须从 dim_event_type 25 个选 1
                            str(res.get("event_type_code") or "")[:64],
                            str(res.get("snapshot_proximity") or "")[:32],
                            # v176.2 新增 5 字段: 决策间长期影响传递
                            _prev.get("prev_decision_id"),
                            str(_prev.get("prev_action_same_asset") or "")[:16],
                            _prev.get("prev_score_same_asset"),
                            str(_prev.get("regime_at_decision") or "")[:32],
                            str(_prev.get("bias_after_reflection") or "")[:200],
                            # v173+ 结构化输入层 (旁路,不动原推理)
                            _structured_input_json,
                            _structured_check_json,
                            # v176.2 (item 15): event_atom ↔ ai_decisions 双向索引
                            _event_atom_ids_json,
                            # v176.5f: 闸门归因（gate_chain + original_action + ungated_action 影子模式）
                            _gr.to_json(),
                            _gr.original_action,
                            _gr.ungated_action,
                            # v181 结构化产出摘要 JSON
                            _struct_summary_json,
                            # v186 经验系统：actor 对 asset 的影响力权重 + 命中的经验 id
                            float(_pw),
                            int(_pm_id),
                            # v342: 降级标记（"没分析"≠"判断为观望"）
                            int(_ad_flag),
                            _ad_why,
                            # v302+ P0+【修复】INSERT 路径补 ts (UTC 秒).
                            # 历史 BUG: ai_decisions.ts 是 v302+ 加的无 default 列,
                            # INSERT 完全不写 → 全 NULL → v302+ 窗口查询 (ts>=lo/hi/fresh_lo)
                            # 全部漏空 → /api/events/today live_signal_count 永远 0,
                            # 前端 StatCards 大字显示 0. 现显式写入, 并由 BEFORE INSERT
                            # trigger (trg_ai_decisions_autots) 兜底防漏.
                            int(time.time()),
                        )),
                    )
                    decision_id = cur.lastrowid
                    # v490 P0-1 兜底收口：INSERT 不带 exec_state，后续建仓/风险拒绝/
                    # 闸门拦截/观望各分支会分别覆盖成明确状态。但父子回填、
                    # 降级旁路等窄分支可能只补 entry_price / reason 而漏写状态，
                    # 留下"状态透明失踪"。这里在 id 落库后先按本行动作/资产/
                    # 有无原因给一个基线状态；后续明确分支再覆盖。
                    try:
                        _base_state = (
                            "not_traded"
                            if action in ("BUY", "SELL")
                            and str(asset or "") not in ("", "NONE")
                            else "hold"
                        )
                        conn.execute(
                            "UPDATE ai_decisions SET exec_state=? WHERE id=? AND exec_state IS NULL",
                            (_base_state, decision_id),
                        )
                    except Exception as _base_e:
                        print(f"  [EXEC-STATE] 基线状态落库失败: {type(_base_e).__name__}: {_base_e}", flush=True)
                    # v482: 经验注入记账 —— 紧贴 id 生成, 不跟后续长 try 共命运
                    try:
                        _record_injected_experiences(int(decision_id or 0))
                    except Exception as _expm_e:
                        print(f"  [EXP-INJECT-MARK] 外层兜底: "
                              f"{type(_expm_e).__name__}: {_expm_e}", flush=True)
                    # v453: 落代码指纹 —— 换代时可分层统计，不必再靠删库保口径。
                    # 单独一条 UPDATE 而不是塞进那条 40+ 列的 INSERT：后者列清单
                    # 极易错位，而指纹只是旁注信息，失败了也不该拖垮落库。
                    try:
                        from code_fingerprint import fingerprint as _cfp
                        conn.execute(
                            "UPDATE ai_decisions SET code_fingerprint=? WHERE id=?",
                            (_cfp(), decision_id),
                        )
                    except Exception as _cfp_err:  # v452：失败必须留痕（静默异常守卫）
                        print(
                            f"[WARN] code_fingerprint 落库失败（不影响决策本身）: {_cfp_err}",
                            flush=True,
                        )
                    # v400 W3: 同步审计门禁（shadow）—— 决策刚落库、尚未 commit,
                    # 用同一连接审, findings 直落 decision_errors, verdict 落 audit_gate_log。
                    # 原反思是周一 09:00 的周报（实测覆盖率 0.09%）；这里每条都审。
                    # 默认只审不改（shadow）：AUDIT_GATE=off 关掉。
                    # 真拦（降级为 HOLD）尚未接线，见 audit/gate.py。
                    _audit_block_reason = ""
                    try:
                        import audit_gate_hook as _agh
                        _gate_res = _agh.gate(conn, decision_id)
                        # v419 canary：仅当 critical 码只有 E014（推理链缺失）时真拦，
                        # 其它 reject 继续 shadow。AUDIT_GATE_ENFORCE=1 才生效。
                        _audit_block_reason = _agh.should_block(_gate_res)
                        if _audit_block_reason:
                            print(f"  [AUDIT-GATE:ENFORCE] decision#{decision_id} "
                                  f"blocked: {_audit_block_reason}", flush=True)
                    except Exception as _agh_e:
                        print(f"  [AUDIT-GATE] hook failed: "
                              f"{type(_agh_e).__name__}: {_agh_e}", flush=True)
                    # v314 P0: 多模型共识落库 (extra_models_consensus)。
                    # 生产此前只写 peer_reviews，extra_models_consensus **全库为空**
                    # → 验收 #4 判"多模型共识根本没采"（填充率 0%）。
                    # 这里把同行评审**同源投影**成 legacy dict 形态写进该列
                    # （值全部来自真实评审输出，无任何编造）。
                    # 必须在 evaluate_and_persist **之前**写：权重计算读的就是这个
                    # 字段，consensus_mult 才会真的动（而不是恒 1.0）。
                    try:
                        if decision_id and s.get("peer_reviews"):
                            _emc = _build_extra_models_consensus(
                                s.get("peer_reviews"), main_action=action)
                            if _emc:
                                conn.execute(
                                    "UPDATE ai_decisions SET extra_models_consensus=? "
                                    "WHERE id=?", (_emc, decision_id))
                    except Exception as _emc_exc:
                        print(f"[ai_worker] extra_models_consensus write id={decision_id} "
                              f"failed: {_emc_exc}", flush=True)
                    # v20251014: 即时算 signal_weight (无市场反应 → reaction_mult=1.0).
                    # 失败静默, 不影响主流程 (INSERT 已 commit).
                    try:
                        if decision_id:
                            from signals import signal_weight_evaluator as _swe
                            _swe.evaluate_and_persist(
                                conn, decision_id,
                                trigger_kind="insert", now_ts=int(time.time()),
                            )
                    except Exception as _swe_exc:
                        print(f"[ai_worker] signal_weight evaluate id={decision_id} failed: {_swe_exc}", flush=True)
                    # v200 P0+ 严谨兜底: 回写 field_sources / missing_fields /
                    #   completeness_score / 5 类 source 计数 / historical_ref_ids
                    #   到 ai_decisions, 让反思系统按 completeness 过滤脏决策.
                    try:
                        if decision_id:
                            _fs = json.loads(res.get("field_sources") or "{}")
                            _mf = json.loads(res.get("missing_fields") or "[]")
                            _cs = float(res.get("completeness_score") or 1.0)
                            _hr = res.get("historical_ref_ids") or "{}"
                            _counts = _count_sources(_fs)
                            conn.execute(
                                """UPDATE ai_decisions SET
                                    field_sources = ?,
                                    missing_fields = ?,
                                    completeness_score = ?,
                                    llm_field_count = ?,
                                    rule_field_count = ?,
                                    business_kb_field_count = ?,
                                    historical_field_count = ?,
                                    historical_ref_ids = ?,
                                    llm_retry_count = ?
                                  WHERE id = ?""",
                                (
                                    json.dumps(_fs, ensure_ascii=False),
                                    json.dumps(_mf, ensure_ascii=False),
                                    _cs,
                                    int(_counts["llm"]),
                                    int(_counts["rule"]),
                                    int(_counts["business_kb"]),
                                    int(_counts["historical"]),
                                    str(_hr),
                                    int(res.get("llm_retry_count") or 0),
                                    int(decision_id),
                                ),
                            )
                    except Exception as _v200_e:
                        # v200 写入失败不应阻塞 INSERT, 仅 print
                        print(f"  [V200-COMPLETENESS-WRITE] failed: "
                              f"{type(_v200_e).__name__}: {_v200_e}", flush=True)
                    # v399: 来源留痕 —— 路由来源 + 硬数据缺口原因（只描述事实, 不打分）。
                    # 没有这两列时,"29.7% 无标的"分不清是真没标的还是路由漏了,
                    # "56% 硬数据缺失"也看不出缺在个股没源还是本地线断档。
                    try:
                        if decision_id:
                            from signals import decision_provenance as _dpv
                            # v409: 桶标签（CN_STOCK/US_STOCK…）不是"没数据源"，
                            # 是"公司认出来了、代码没解析出来"。落库前拿新闻原文
                            # 做一次显式代码提取（只认 $NVDA / 600519.SH / NASDAQ: NVDA
                            # 这类写法，裸数字不认），提到就把 target_asset 一起改掉。
                            _tk_how = ""
                            try:
                                import ticker_resolve as _tr
                                _tk_txt = ""
                                try:
                                    _tk_row = conn.execute(
                                        "SELECT content FROM raw_news WHERE id=?", (nid,)
                                    ).fetchone()
                                    _tk_txt = str(_tk_row[0] or "") if _tk_row else ""
                                except Exception:
                                    _tk_txt = ""
                                if _tk_txt.strip():
                                    _tk_asset, _tk_how, _tk_why = _tr.maybe_resolve(
                                        str(asset or ""), _tk_txt)
                                    if _tk_how:
                                        print(f"  [TICKER-RESOLVE] news={nid} "
                                              f"{_tk_why}", flush=True)
                                        asset = _tk_asset
                            except Exception as _tk_e:
                                print(f"  [TICKER-RESOLVE] skipped: "
                                      f"{type(_tk_e).__name__}: {_tk_e}", flush=True)
                            _r_tag, _hd_tag = _dpv.tags(
                                str(asset or ""),
                                str(res.get("hard_data_evidence") or ""),
                                ts=time.time(),   # v409: 休市判定要"决策那一刻"
                            )
                            # v409: 无标的 = 没有可分析对象, 记 routing_miss。
                            # S16 降级率只在 analysis 池里算, 免得把"路由没打上"
                            # 算成"模型没被问"。
                            _dkind = ("routing_miss" if _r_tag == "target_none"
                                      else "analysis")
                            # v483：保留别名回退来源，别被 provenance 的 target_ok 覆盖
                            _route_tag = ("alias_fallback"
                                          if str(res.get("asset_route") or "") == "alias_fallback"
                                          else _r_tag)
                            if _tk_how:
                                conn.execute(
                                    "UPDATE ai_decisions SET target_asset=?, asset_route=?, "
                                    "hard_data_status=?, decision_kind=? WHERE id=?",
                                    (str(asset or ""), _route_tag, _hd_tag, _dkind,
                                     int(decision_id)))
                            else:
                                conn.execute(
                                    "UPDATE ai_decisions SET asset_route=?, hard_data_status=?, "
                                    "decision_kind=? WHERE id=?",
                                    (_route_tag, _hd_tag, _dkind, int(decision_id)))
                    except Exception as _dpv_e:
                        print(f"  [PROVENANCE-WRITE] failed: "
                              f"{type(_dpv_e).__name__}: {_dpv_e}", flush=True)
                    # v188 P0-2: 把刚才 inject 到 prompt 的经验标 llm_invoked=1
                    # v490: 改读线程本地标记; 若主路径已经记账并清空, 这里拿到空列表就跳过,
                    # 不会重复写(UNIQUE 也挡得住), 但不再串到别的线程。
                    try:
                        _pending_ids = _get_injected_exp_ids()
                        if decision_id and _pending_ids:
                            import experience_reflector as _er2
                            for _eid in _pending_ids:
                                if _eid and _eid > 0:
                                    _er2.apply_experience(_eid, int(decision_id),
                                                         applied_action="prompt_inject",
                                                         llm_invoked=1)
                            _set_injected_exp_ids([])
                    except Exception as _exp_e:
                        # 失败兜底: 仅 print, 绝不断 INSERT 已成功的决策
                        print(f"  [EXP-INJECT-MARK] failed: {type(_exp_e).__name__}: {_exp_e}",
                              flush=True)
                    # v200 P0-A3: 把 _asset_insights 注入时命中的 insight ids 写回 ai_decisions,
                    # 让"经验参与决策"在 DB 层可见 (原 594/594 全空)。主品种和副品种都写,
                    # 副品种另写一份"为副品种匹配的子集", 便于后续按 asset 维度统计命中归因。
                    try:
                        # v200 P0-A3 修复: 原先直接引用内层闭包 _do_api_call 的局部变量
                        # _asset_insight_hit_ids → NameError, insight_hit_ids 100% 写空。
                        # 改为从本条新闻的最终结果取: s 是包装 dict, LLM 结果在 s["result"]
                        # (_call_llm_sync 已把该 key 放进 return dict 透传)。
                        # 不用 res/llm_result: 二者不在本函数作用域(_process_single 内),
                        # 且 failover 会覆盖 llm_result。
                        _hit_ids = ((s or {}).get("result") or {}).get("_asset_insight_hit_ids") or []
                        if decision_id and _hit_ids:
                            _hit_json = json.dumps(
                                [int(x) for x in _hit_ids
                                 if isinstance(x, (int, float)) and int(x) > 0],
                                ensure_ascii=False,
                            )
                            conn.execute(
                                "UPDATE ai_decisions SET insight_hit_ids=? WHERE id=?",
                                (_hit_json, int(decision_id)),
                            )
                            print(
                                f"  [INSIGHTS-HIT-WRITE] decision#{decision_id} "
                                f"asset={asset} hit={len(_hit_ids)} ids",
                                flush=True,
                            )
                    except Exception as _hit_e:
                        print(f"  [INSIGHTS-HIT-WRITE] failed: "
                              f"{type(_hit_e).__name__}: {str(_hit_e)[:80]}",
                              flush=True)
                    if not s.get("is_sibling"):
                        primary_decision_ids[nid] = decision_id

                        # ── v173: structured_check verdict=fabricated/partial → 入真实性飞轮 ──
                        # 失败兜底: 异常时仅 print,绝不阻断 INSERT 已经成功的决策。
                        try:
                            from engine import structured_reflection as _sr
                            _check_dict = {}
                            try:
                                _check_dict = json.loads(_structured_check_json or "{}") if _structured_check_json else {}
                            except Exception:
                                _check_dict = {}
                            _verdict_now = str(_check_dict.get("verdict") or "")
                            if _verdict_now in ("fabricated", "partial"):
                                # 取最新 event_atom 的 event_type_code 作为关键维度
                                _sr_etc = str(res.get("event_type_code") or "")
                                try:
                                    _sr_rows = conn.execute(
                                        "SELECT det.code FROM event_atom ea "
                                        "LEFT JOIN dim_event_type det ON det.id=ea.event_type_id "
                                        "WHERE ea.raw_news_id=? ORDER BY ea.event_time DESC LIMIT 1",
                                        (nid,),
                                    ).fetchone()
                                    if _sr_rows and _sr_rows["code"]:
                                        _sr_etc = str(_sr_rows["code"])
                                except Exception:
                                    pass
                                _sr.enqueue(
                                    conn,
                                    decision_id=int(decision_id),
                                    news_id=int(nid) if nid else None,
                                    asset=str(asset or ""),
                                    event_type_code=_sr_etc,
                                    verdict=_verdict_now,
                                    score_coverage=float(_check_dict.get("score_coverage") or 0.0),
                                    score_fabrication=float(_check_dict.get("score_fabrication") or 0.0),
                                    entities_fabricated=list(_check_dict.get("entities_fabricated") or []),
                                    events_missing=list(_check_dict.get("events_missing") or []),
                                )
                        except Exception as _sr_err:
                            print(f"  [STRUCT-REFLECT-HOOK] news={nid} enqueue 跳过: "
                                  f"{type(_sr_err).__name__}: {str(_sr_err)[:80]}", flush=True)

                        # ── v174: event_struct 从 event_atom 派生升级 ──
                        # 解决 "event_struct 70% 是 []" 的假性结构化问题:
                        # INSERT 时 event_struct 字段是 event_struct.extract_events() 结果
                        # (22 类正则词典,覆盖率低,70% 返回 [])
                        # 现在 event_atom 表已经在 L5548 写入(规则 + LLM 兜底)
                        # → 用 event_atom 数据升级 event_struct 字段,
                        #   保证 event_struct 字段填充率 = event_atom 覆盖率
                        try:
                            # v323 P0-1: 列名修正 —— event_atom 表里这一列叫
                            # `reasoning`（见 event_atom.py insert_event_atom(reasoning=...)），
                            # 这里却写成 `ea.reasoning_text` → 每条决策都抛
                            # "no such column: ea.reasoning_text" → event_struct 字段
                            # 100% 回写失败（前端"结构化"徽章永远不亮）。
                            _atom_rows = conn.execute(
                                "SELECT ea.event_type_id, det.code AS event_code,"
                                " ea.direction, ea.direction_confidence, ea.evidence_count,"
                                " ea.actor, ea.magnitude_value, ea.magnitude_unit,"
                                " ea.event_time, ea.reasoning"
                                " FROM event_atom ea"
                                " LEFT JOIN dim_event_type det ON det.id = ea.event_type_id"
                                " WHERE ea.raw_news_id = ?"
                                " ORDER BY ea.event_time DESC",
                                (nid,),
                            ).fetchall()
                            if _atom_rows:
                                _v174_struct = [
                                    {
                                        "event_type": r["event_code"] or "",
                                        "actor": r["actor"] or "",
                                        "action": r["direction"] or "",
                                        "magnitude_value": r["magnitude_value"],
                                        "magnitude_unit": r["magnitude_unit"] or "",
                                        "target_assets": [],  # event_atom 不强约束具体资产
                                        "raw": (r["reasoning"] or "")[:200],
                                        "source": "event_atom",
                                        "confidence": r["direction_confidence"] or 0.0,
                                    }
                                    for r in _atom_rows
                                ]
                                _v174_struct_json = json.dumps(
                                    _v174_struct, ensure_ascii=False
                                )[:1000]
                                if _v174_struct_json and _v174_struct_json != "[]":
                                    conn.execute(
                                        "UPDATE ai_decisions SET event_struct = ? WHERE id = ?",
                                        (_v174_struct_json, decision_id),
                                    )
                                    # 静默: 不刷日志(高频调用)
                        except Exception as _v174_err:
                            # v322: 不刷日志会**静默丢 event_struct** → 前端"结构化"徽章
                            # 永远不亮，而且没人知道是写库失败（与 10.23 同类静默失效）。
                            print(f"  [AI] ⚠️ event_struct 回写失败 (decision=#{decision_id}): "
                                  f"{type(_v174_err).__name__}: {str(_v174_err)[:100]}",
                                  flush=True)

                        # ── 全网溯源：主行才写。AI 自由回答的文本直接存 ──
                        # 同时存结构化字段（前端徽章三态显示用）。
                        # 三层防护 + 校验函数（_validate_cross_net_ts）：
                        #   ① 格式正则 ②下限1990 ③不能未来(±60s) ④不能晚于入库(±60s)
                        #   ⑤source ≥4字符 / AMBIGUOUS 必须含"候选1"或"/"或"或"
                        # 校验失败 → 清空（前端不渲染徽章；不污染 DB）
                        try:
                            _noi = str(res.get("net_origin_info") or "").strip()[:200]
                            _cn_ts_raw = str(res.get("cross_net_first_seen_ts") or "").strip()[:32]
                            _cn_src = str(res.get("cross_net_source") or "").strip()[:300]
                            _our_ts = timestamp_map.get(nid, "") or ""
                            _cn_ts = _validate_cross_net_ts(_cn_ts_raw, _cn_src, _our_ts)
                            # source 只在 ts 校验通过时写入；空串/失败时 source 也清空，避免误用
                            if not _cn_ts:
                                _cn_src = ""
                            # 同步写 net_origin_info（已有）+ cross_net_first_seen_ts/_source（新）
                            if _noi or _cn_ts:
                                conn.execute(
                                    "UPDATE raw_news SET net_origin_info = ?, "
                                    "cross_net_first_seen_ts = ?, cross_net_source = ? "
                                    "WHERE id = ?",
                                    (_noi[:200] if _noi else "", _cn_ts, _cn_src[:300] if _cn_src else "", nid),
                                )
                        except Exception as _cn_exc:
                            # v322: 全网溯源字段写失败 → 前端"全网首发"徽章永远不亮
                            print(f"  [AI] ⚠️ 全网溯源字段回写失败 (news=#{nid}): "
                                  f"{type(_cn_exc).__name__}: {str(_cn_exc)[:100]}",
                                  flush=True)

                    # v173 决策证据链：每条决策钉死"用了哪条新闻作 trigger"——后续 /api/decisions/:id
                    # 可全文溯源到原文 + 平台发布时间 + 抓到时间 + 内容指纹。
                    # 同族聚合（parent_id 非空）→ 关联 parent 的 nid；sibling 行关联同 nid。
                    try:
                        evidence_news_id = nid if nid is not None else (
                            parent_row[0] if (parent_id is not None and parent_row and parent_row[0]) else None
                        )
                        if evidence_news_id is not None:
                            db.record_evidence(
                                conn,
                                decision_id=int(decision_id),
                                news_id=int(evidence_news_id),
                                used_for="trigger",
                            )
                    except Exception as _exc:
                        # 接入失败不阻断主流程（evidence 是可观测性，不是业务关键路径）
                        pass

                    history_sample = evidence.collect_settled_sample(asset, connection=conn)
                    # 聚合热度：传入真实同族数量（parent 的 child_count + 当前这条），
                    # 修复闸门 cluster_heat 因子恒为 0 的问题。
                    cluster_hint = 1
                    if parent_id is not None:
                        p_row = conn.execute(
                            "SELECT child_count FROM ai_decisions WHERE id = ?",
                            (parent_id,),
                        ).fetchone()
                        if p_row:
                            # v322：child_count 历史上被写坏过（存的是 'OIL|WTI|BUY' 这类
                            # 聚合键），int() 会抛 ValueError 并让**整批**落库作废。
                            # 脏值一律当 0 处理，绝不让单个字段炸掉整批。
                            cluster_hint = _safe_child_count(p_row["child_count"]) + 1
                    if os.getenv("TRIDENT_TRACE_SCORE", "0") == "1":
                        print(f"  [SCORE-TRACE] news=#{nid} 落库前 score={score} action={action} asset={asset}", flush=True)
                    # v342: 预期差（actual vs forecast）—— 只做反方证据，不改方向权重
                    _surprise_block: Dict[str, Any] = {"available": False,
                                                       "detail": "未取到宏观事件"}
                    try:
                        import surprise_factor as _suf
                        _surprise_block = _suf.enrich_by_news_id(conn, nid, asset)
                    except Exception as _suf_exc:
                        print(f"  [AI] 预期差跳过 (news=#{nid}): "
                              f"{type(_suf_exc).__name__}: {str(_suf_exc)[:80]}", flush=True)
                    try:
                        _fund_block = fundamental_factor.build(
                            asset, conn=conn, hint=_fundamental_hint(res))
                    except Exception as _fund_exc2:
                        _fund_block = {"available": False,
                                       "detail": f"基本面因子跳过: {type(_fund_exc2).__name__}"}
                    guard = decision_guard.evaluate_decision({
                        "sentiment_score": score,
                        "target_asset": asset,
                        "surprise": _surprise_block,
                        "fundamental": _fund_block,
                        "market_confirmation": mkt_confirm,
                        "decision_context": decision_context,
                        "cluster_size": cluster_hint,
                        "history_sample": history_sample,
                        # 影响评估（0-10）参与闸门加权
                        "heat_score": heat_score,
                        "impact_score": impact_score,
                        "asset_impact": asset_impact,
                        # 门卫四步标签(L3)：v181 二分类后只剩"排除"会触发 HOLD
                        "gate_verdict": _gate_verdict,
                    })
                    timeseries.record_factor_snapshot(nid, asset, guard, conn)
                    # v448：基本面 0.08 权重的影子账本 —— 同一批行情结算「含/不含」
                    # 两个方向分，把这条先验权重变成可验证的数字。记账失败不影响判定。
                    try:
                        import fundamental_shadow as _fshadow
                        _fshadow.record(
                            conn, decision_id=decision_id, news_id=nid, asset=asset,
                            guard=guard, action=str(guard.get("action") or action),
                            flags=(guard.get("fundamental") or {}).get("flags") or [],
                            components=(guard.get("fundamental") or {}),
                        )
                    except Exception as _fse:
                        print(f"  [SHADOW] 基本面影子账跳过: {type(_fse).__name__}: {str(_fse)[:60]}", flush=True)
                    timeseries.record_news_event(
                        nid,
                        source="",
                        is_noise=0,
                        status="DONE",
                        decision_id=decision_id,
                        asset=asset,
                        action=action,
                        score=score,
                        connection=conn,
                    )
                    if HERMES_AGENT_ENABLED:
                        hermes_agent.persist_decision_observation(
                            nid,
                            decision_id,
                            asset=asset,
                            action=action,
                            score=score,
                            extras={
                                "prediction_type": pred_type,
                                "event_strength": evt_strength,
                                "confidence": guard.get("confidence"),
                            },
                            connection=conn,
                        )
                    direction_consistent = (
                        action == "HOLD"  # 观望恒自洽（AI 观望不会因分数判矛盾）
                        or (action == "BUY" and score > 0)
                        or (action == "SELL" and score < 0)
                    )
                    gate_passed = (
                        guard["confidence_detail"]["passed_gate"]
                        and guard["action"] == action
                        and direction_consistent
                    )
                    if action == "HOLD":
                        # AI 主动观望：也写闸门结论供前端展示（之前一律空字符串导致 dashboard 看不到原因）
                        # 优先级：传导链闸门 > 前置强制(P1/P1-b/P2) > 闸门降级 (action=AI 期望 vs guard 推荐不一致) > LLM 倾向 HOLD 本身
                        if _force_hold_reason:
                            gate_reason = _force_hold_reason
                        elif str(_gate_verdict) == "排除":
                            gate_reason = f"门卫四步标签=排除：只分析不下单"
                        elif str(_gate_verdict) == "只能看":
                            # 历史兜底：老数据残留的"只能看"标签不再触发 HOLD（v181 已删除此分类）
                            gate_reason = f"门卫四步标签=只能看(老数据兜底)，信任后续闸门"
                        elif not direction_consistent:
                            gate_reason = "LLM 方向与分数符号矛盾"
                        elif _conf_unavailable and not guard["confidence_detail"]["passed_gate"]:
                            # 没有证据可算 ≠ 置信度低 —— 两种 HOLD 在复盘里必须能分开。
                            # 注意不能直接用 guard["verdict"]：那句文案是
                            # 「统一置信度 0.0 低于闸门 X」——在"没算过"的场景里 0.0 是
                            # 占位值，把占位值当结论报出去就是自相矛盾（v399 修正）。
                            # 降级原因取落库同源的 `_ad_why`（target_none / llm_empty / …）。
                            _degr_text = str(_ad_why or "").strip()
                            gate_reason = (
                                "无可证据（coverage=0，置信度不适用）："
                                + (_degr_text or "降级分析，未建仓")
                            )
                        elif not guard["confidence_detail"]["passed_gate"]:
                            gate_reason = guard["verdict"] or f"统一置信度 {guard['confidence']} 低于闸门，未建仓"
                        else:
                            # AI 主动 HOLD（但 evidence 已通过）—— 记录"AI 倾向观望"以便审计
                            gate_reason = f"AI 主动观望 (evidence={guard['confidence']}，方向={action})"
                    elif gate_passed:
                        gate_reason = guard["verdict"]
                    elif not direction_consistent:
                        gate_reason = "LLM 方向与分数符号矛盾"
                    elif not guard["confidence_detail"]["passed_gate"]:
                        gate_reason = guard["verdict"]
                    else:
                        # 置信度过闸但闸门方向与 LLM 方向不一致 → 明确拒绝，
                        # 不写"证据充分"（否则 quick_sim 的 gate_passed 标记会误标）。
                        gate_reason = (
                            f"闸门方向与信号方向不一致 (闸门={guard['action']}, 信号={action})"
                        )
                    # ── v400: HOLD 必须有可归因原因（与「分析降级」分开统计）──
                    # 现状：no_trade_reason 全库为空，reflection_ontology 里按
                    # 「数据不可用 / 延迟 / 源降级」归因的分支从不触发。
                    # 这里用 analysis.contract 的同一套五终态口径派生，写库失败
                    # 只打印，绝不影响已成功的 INSERT（延续 v200 的兜底约定）。
                    _ntr_reason = ""
                    try:
                        from analysis import contract as _ac_v400
                        from signals import decision_provenance as _dp_v400
                        _hd_v400 = _dp_v400.hard_status(
                            str(asset or ""), str(res.get("hard_data_evidence") or ""),
                            ts=time.time())
                        _st_v400 = _ac_v400.derive_state(
                            degraded=bool(_ad_flag),
                            reason=str(_ad_why or ""),
                            asset=str(asset or ""),
                            hard_status=_hd_v400,
                        )
                        _ntr_reason = _ac_v400.derive_no_trade_reason(
                            action=action,
                            state=_st_v400,
                            gate_reason=str(gate_reason or ""),
                            # 新闻年龄用本批的 timestamp_map（pre_ts 属于 _process_single 作用域，
                            # 这里是 ai_worker 主循环，取不到）
                            news_age_min=_news_age_minutes(timestamp_map.get(nid, "") or ""),
                        )
                    except Exception as _ntr_e:
                        print(f"  [NO-TRADE-REASON] 派生失败（保留空串）: "
                              f"{type(_ntr_e).__name__}: {str(_ntr_e)[:80]}", flush=True)
                    # ── v452: 降级决策不许落"有效数值"（0 号铁律）──
                    # 降级（llm_empty / target_none / summary_only / server_fallback）
                    # 按定义没有判断，置信度只能是 NULL；闸门原因也不能留
                    # 「统一置信度 0.0 低于闸门 15.0」——那句话把"没算"说成了"算出来是 0"。
                    # 此前只按 coverage 判定，覆盖率>0 的降级行仍写 0.0 → fake_data F1
                    # 实测 11-19 早 125 条且每小时新增。
                    try:
                        import confidence_availability as _cav_db
                        _conf_db = _cav_db.db_value_for_decision(
                            _guard_pre, _evidence_confidence_pre, bool(_ad_flag))
                        _gr_db = (_cav_db.gate_reason(True, str(gate_reason or ""))
                                  if (_conf_unavailable or _ad_flag) else gate_reason)
                    except Exception as _cav_db_e:
                        print(f"  [CONF-DB] 口径判定失败，按 NULL 落库: "
                              f"{type(_cav_db_e).__name__}: {str(_cav_db_e)[:80]}", flush=True)
                        _conf_db, _gr_db = None, str(gate_reason or "")
                    conn.execute(
                        """UPDATE ai_decisions
                           SET paper_trading_run_id=?, agent_model_id=?,
                               evidence_confidence=?, evidence_action=?, trade_gate_reason=?,
                               no_trade_reason=?, exec_state=?
                           WHERE id=?""",
                        (
                            active_run["id"] if active_run else None,
                            # 记录实际成功调用的模型（OpenRouter/MiniMax/nassaapi），
                            # 而非模拟盘运行配置——否则前端「当前分析」永远显示配置模型。
                            # 运行配置的 model_id 可能是历史遗留值（池子换过模型后不在池内，
                            # 实测遗留 gpt-5.4-mini/nassaapi 已 403）→ 不在池内就不用它，
                            # 退回本批实际使用的模型。
                            _model_for_row,
                            # 【v181 P2】写入已扣 overanalysis 惩罚的 confidence（前置 guard）
                            # v399：无证据时写 NULL（不写 0）——0 是"算出来的低分"，
                            # NULL 是"没东西可算"，两者在统计里不能混。
                            # v452：降级决策一律 NULL（不只是 coverage=0 的行）。
                            _conf_db,
                            # 【v181 P1】写入前置 evidence_action（可能已强制为 HOLD）
                            _evidence_action_pre,
                            _gr_db,
                            _ntr_reason,
                            # v490 P0-1：落库即把 exec_state 写明确，不允许出现"没价、没原因、
                            # 状态空"的透明失踪。方向单被闸门/条件拦下 → not_traded；
                            # 观望 HOLD → hold；后续建仓/缺价分支再覆盖成 '' / missing_entry_price。
                            ("not_traded" if action in ("BUY", "SELL")
                             and asset not in ("", "NONE") and not _ntr_reason.startswith("行情源")
                             else "hold"),
                            decision_id,
                        ),
                    )

                    # 严格模式：冻结策略 + 多证据闸门均通过后才建仓。
                    # 宽松模式：关闭闸门，BUY/SELL 只要有真实行情即自动建仓。
                    entry_price_val: Optional[float] = None
                    now_ts = _ts()
                    gate_enforced = bool(trading_settings.get("gate_enabled", True))
                    strategy_ok = strategy_store.signal_matches(
                        active_params,
                        action=action if not gate_enforced else guard["action"],
                        score=score,
                        asset=asset,
                        event_strength=evt_strength,
                        direct_catalyst=direct_cat,
                    )
                    # 新闻时效门槛：新闻发布超过后台配置的时长（秒）不再建仓
                    # （BUY/SELL 通用，防止用旧新闻追单；解析失败视为新鲜放行）
                    # 优先用"平台声称的发布时间"计算年龄——防平台补发旧闻刚入库被当新鲜；
                    # 无平台时间（WS/webhook 实时流）时回落入库时间（到达即发布）。
                    max_age_sec = _get_news_trade_max_age_seconds()
                    pub_raw = published_map.get(nid, "") or ""
                    age_basis = pub_raw if pub_raw else timestamp_map.get(nid, "")
                    news_age = _news_age_minutes(age_basis)
                    news_fresh = news_age is None or news_age * 60 <= max_age_sec
                    if not news_fresh:
                        print(
                            f"  [AI] 信号过期，跳过建仓: news=#{nid} 已 {news_age:.0f} 分钟"
                            f"（> {max_age_sec // 60} 分钟 / {max_age_sec}s），仍入库统计不交易",
                            flush=True,
                        )
                    auto_trade = (
                        trading_settings["is_running"]
                        and active_run is not None
                        and paper_trading.asset_allowed(asset, trading_settings)
                        and news_sources.source_enabled(source_map.get(nid, ""), news_source_settings)
                        and action in ("BUY", "SELL")
                        and asset not in ("", "NONE")
                        and news_fresh
                        and (not _audit_block_reason)
                        and (
                            (not gate_enforced)
                            or (gate_passed and strategy_ok)
                        )
                    )
                    if auto_trade:
                        # 健康检查仅开仓触发：后台探测一次 OpenRouter 存活度并写缓存
                        # （fire-and-forget，不阻塞开仓与事件循环）。结果由后续分析的
                        # _openrouter_healthy() 读取，用于判断是否切 MiniMax 备用通道。
                        try:
                            loop.run_in_executor(None, _probe_openrouter_if_stale)
                        except Exception:
                            pass
                        entry_price_val = _get_current_price(asset)
                        if entry_price_val is not None:
                            # ── v176 P2-16B/E：risk_manager 接入（仓位/连亏/日损三道门） ──
                            # 默认规模 = 1000 USDT（与 risk_manager.MAX_POSITION_USDT 兼容）；
                            # 若 max_allowed < suggested → 不建仓 + 写 no_trade_reason（前台可观测）。
                            try:
                                import risk_manager as _rm
                                _eval = _rm.evaluate_trade(
                                    asset, action, 1000.0,
                                )
                            except Exception as _rm_e:
                                _eval = {"allow": True, "reason": f"risk_manager import err: {_rm_e}"}
                            if not _eval.get("allow", True):
                                conn.execute(
                                    "UPDATE ai_decisions SET no_trade_reason=?, exec_state=? WHERE id=?",
                                    (f"risk_manager:{_eval.get('reason', 'rejected')}",
                                     "rejected_by_risk", decision_id),
                                )
                                print(
                                    f"  [RISK-MANAGER] {asset} {action} news=#{nid} rejected: {_eval.get('reason')}",
                                    flush=True,
                                )
                            else:
                                entry_unix = int(time.time())
                                # 单笔持仓周期：按 AI 的 expected_horizon 映射；
                                # intraday → NULL（沿用策略默认周期），1-3d → 2 天，1w+ → 7 天。
                                horizon_minutes = {
                                    "1-3d": 2 * 24 * 60,
                                    "1w+": 7 * 24 * 60,
                                }.get(exp_horizon)
                                # v489：exec_state 标记"这条方向单到底落地了没有"。
                                # 空串 = 正常执行（有价、有仓）；其余值是未落地原因。
                                conn.execute(
                                    """UPDATE ai_decisions
                                       SET entry_price=?, entry_time=?, max_price=?, min_price=?,
                                           max_price_time=?, min_price_time=?, holding_horizon_minutes=?,
                                           no_trade_reason='', exec_state=''
                                       WHERE id=?""",
                                    (entry_price_val, now_ts, entry_price_val, entry_price_val,
                                     entry_unix, entry_unix, horizon_minutes, decision_id),
                                )
                                # P4 命中验证：记录本单 AI 实际采纳的经验 id（仅供结算后统计遵守率）。
                                # 只写 AI 在 insight_hit_ids 里声明的 id，并过滤到当前生效经验集合，
                                # 防止模型幻觉出不存在的 id 污染命中归因统计；副品种(sibling)行不写。
                                # v186 audit 修复: 当 AI 返回空时, 自动从 review_insights 按关键词
                                # 匹配 top-3,让经验真正参与决策(原 0/83 → 现在应该有命中).
                                try:
                                    from review.review_agent import normalize_insight_hit_ids, load_active_insight_items
                                    _claimed = normalize_insight_hit_ids(res.get("insight_hit_ids")) if not s.get("is_sibling") else []
                                    _hit_src = "model"
                                    if _claimed:
                                        _valid = {int(it["id"]) for it in load_active_insight_items()}
                                        if _valid:
                                            _claimed = [i for i in _claimed if i in _valid]
                                    # v186 fallback: AI 没声明时,用 _auto_match_insights 兜底
                                    if not _claimed and not s.get("is_sibling"):
                                        try:
                                            _news_text = ""
                                            try:
                                                _news_row = conn.execute(
                                                    "SELECT content, title FROM raw_news WHERE id=?",
                                                    (nid,),
                                                ).fetchone()
                                                if _news_row:
                                                    _news_text = (_news_row["title"] or "") + " " + (_news_row["content"] or "")
                                            except Exception:
                                                pass
                                            _claimed = _auto_match_insights(_news_text, asset or "", top_n=3)
                                            # v415: 兜底不是"模型采纳"，只打了关键词相似。
                                            # 单独标来源，统计时不能再算成"经验影响了决策"。
                                            _hit_src = "auto" if _claimed else ""
                                        except Exception:
                                            _claimed = []
                                            _hit_src = ""
                                    if _claimed:
                                        conn.execute(
                                            "UPDATE ai_decisions SET insight_hit_ids=?, insight_hit_source=? WHERE id=?",
                                            (json.dumps(_claimed), _hit_src, decision_id),
                                        )
                                except Exception as _ih_exc:
                                    # v322: 命中经验回写失败 → 复盘经验面板空着，没人知道
                                    print(f"  [AI] ⚠️ insight_hit_ids 回写失败 "
                                          f"(decision=#{decision_id}): "
                                          f"{type(_ih_exc).__name__}: {str(_ih_exc)[:100]}",
                                          flush=True)
                        else:
                            # 闸门/条件全过，但行情源取不到价格 → 未建仓
                            if action in ("BUY", "SELL") and asset not in ("", "NONE"):
                                # v489：这就是审计里"89% 方向单没建仓价、连原因都没有"的黑洞。
                                # 现在必须留下 exec_state，前端和复盘都能分清"没下单"与"下了没记价"。
                                conn.execute(
                                    "UPDATE ai_decisions SET no_trade_reason=?, exec_state=? WHERE id=?",
                                    ("行情源不可用，未能取到价格", "missing_entry_price", decision_id),
                                )
                    else:
                        # ── 方向性信号(BUY/SELL)未实际建仓 → 记录原因（实时信号台展示）──
                        if action in ("BUY", "SELL") and asset not in ("", "NONE"):
                            no_reason = ""
                            if not trading_settings["is_running"]:
                                no_reason = "模拟盘未运行"
                            elif active_run is None:
                                no_reason = "无激活的模拟盘运行"
                            elif not paper_trading.asset_allowed(asset, trading_settings):
                                no_reason = f"资产 {asset} 不在模拟盘允许交易列表"
                            elif not news_sources.source_enabled(source_map.get(nid, ""), news_source_settings):
                                no_reason = "该新闻源已关闭（模拟盘交易层）"
                            elif not news_fresh:
                                no_reason = (
                                    f"新闻已发布 {news_age:.0f} 分钟，超过时效门槛"
                                    f" {max_age_sec // 60} 分钟"
                                )
                            elif _audit_block_reason:
                                no_reason = f"审计门禁:{_audit_block_reason}"
                            elif gate_enforced and not gate_passed:
                                # 复用闸门拒绝原因（置信度不足/LLM方向矛盾/闸门方向不一致）
                                no_reason = gate_reason or "证据闸门未通过"
                            elif gate_enforced and not strategy_ok:
                                # 策略闸门拒绝的具体原因（信号强度不足/资产过滤/事件强度/直接催化剂等）
                                no_reason = strategy_store.signal_mismatch_reason(
                                    active_params,
                                    action=action if not gate_enforced else guard["action"],
                                    score=score,
                                    asset=asset,
                                    event_strength=evt_strength,
                                    direct_catalyst=direct_cat,
                                ) or "信号不匹配当前策略规则"
                            if no_reason:
                                # 未建仓也要能被 SQL 一眼筛出来（原因细节在 no_trade_reason）
                                conn.execute(
                                    "UPDATE ai_decisions SET no_trade_reason=?, exec_state=? WHERE id=?",
                                    (no_reason[:200], "not_traded", decision_id),
                                )

                    if parent_id is not None:
                        # Backfill parent's entry_price if missing (pre-feature data)
                        parent_row = conn.execute(
                            "SELECT entry_price, evidence_confidence, trade_gate_reason"
                            " FROM ai_decisions WHERE id = ?",
                            (parent_id,),
                        ).fetchone()
                        # 只有 parent 自身证据通过（且尚未建仓）时才补 entry——
                        # 避免把从未获批交易的 parent 拉进 2h 结算，污染统计。
                        if parent_row is not None and parent_row["entry_price"] is None and auto_trade:
                            p_conf = parent_row["evidence_confidence"]
                            p_reason = parent_row["trade_gate_reason"] or ""
                            parent_approved = (
                                (p_conf is not None and p_conf >= float(os.getenv("EVIDENCE_CONFIDENCE_GATE", "40")))
                                or p_reason.startswith("证据充分")
                            )
                            if parent_approved:
                                parent_backfill = _get_current_price(asset)
                                if parent_backfill is not None:
                                    entry_unix = int(time.time())
                                    conn.execute(
                                        """UPDATE ai_decisions
                                           SET entry_price=?, entry_time=?, max_price=?, min_price=?,
                                               max_price_time=?, min_price_time=?, strategy_id=?, strategy_version_id=?
                                           WHERE id=?""",
                                        (parent_backfill, now_ts, parent_backfill, parent_backfill,
                                         entry_unix, entry_unix, current_strategy["id"],
                                         active_version["id"], parent_id),
                                    )
                        conn.execute(
                            "UPDATE ai_decisions SET child_count = child_count + 1"
                            " WHERE id = ?",
                            (parent_id,),
                        )
                        _bump_parent_score(conn, parent_id, score, tagged_reason)

                        # ── Consensus: count cluster density within 30 min ──
                        cluster_count = conn.execute(
                            """
                            SELECT COUNT(*) FROM ai_decisions
                            WHERE (parent_id = ? OR id = ?)
                              AND strftime('%s', created_at) > strftime('%s', 'now', '-30 minutes')
                            """,
                            (parent_id, parent_id),
                        ).fetchone()[0]
                        conn.execute(
                            """
                            UPDATE ai_decisions SET cluster_size = ?
                            WHERE (parent_id = ? OR id = ?)
                              AND strftime('%s', created_at) > strftime('%s', 'now', '-30 minutes')
                            """,
                            (cluster_count, parent_id, parent_id),
                        )
                    else:
                        conn.execute(
                            "UPDATE ai_decisions SET aggregation_key = ? WHERE id = ?",
                            (agg_key, decision_id),
                        )

                    written.append({
                        "decision_id": decision_id,
                        "news_id": nid,
                        "model_label": label,
                        "pre_ts": s["pre_ts"],
                        "result": res,
                        "parent_id": parent_id,
                    })

                    # v323 P0：**每条提交**。
                    # 原实现整批 8–16 条共用一个事务（10355 循环 → 11980 才 commit），
                    # 事务里还夹着证据查询/闸门回放/告警推送等慢操作 →
                    # 实测单批持写锁数分钟、WAL 涨到 121MB，其他任务全被
                    # "database is locked" 挡回去；更糟的是引擎自己长期持锁，
                    # 后续批次写不进、决策 id 停在 11382 不动（重启即恢复写入）。
                    # 逐条提交后有 3 个好处：锁只持毫秒级；单条失败不再回滚整批；
                    # WAL 能被自动 checkpoint 掉，不再无限膨胀。
                    try:
                        conn.commit()
                    except Exception as _cm_exc:
                        print(f"  [AI] ⚠️ 逐条提交失败 (news=#{nid}): "
                              f"{type(_cm_exc).__name__}: {str(_cm_exc)[:80]}", flush=True)

                # ============================================================
                # Phase 3b — Doubao UPDATE — merge into existing primary model rows
                # ============================================================
                doubao_results = [s for s in successes if s["model_label"] == "Doubao"]
                for d in doubao_results:
                    nid = d["news_id"]
                    res = _sanitize_llm_result(d.get("result") or {})
                    try:
                        res = _presanitize_for_schema(
                            res, str((content_map or {}).get(d.get("news_id")) or ""))
                    except Exception:
                        pass
                    # v1 重构：消费 _unclassified_* 标记 → 写 unclassified_events
                    _consume_unclassified_marker(res, news_id=nid)
                    d["result"] = res
                    if nid in primary_decision_ids:
                        conn.execute(
                            "UPDATE ai_decisions SET doubao_action = ?, doubao_reasoning = ? WHERE id = ?",
                            (res["suggested_action"], res["reasoning"], primary_decision_ids[nid]),
                        )
                        # Append Doubao info to the written record for terminal output
                        for w in written:
                            if w["news_id"] == nid:
                                w["doubao"] = {"action": res["suggested_action"], "reasoning": res["reasoning"]}
                                break
                    else:
                        # The primary model failed, so Doubao has nothing to merge into.
                        pass

                # ============================================================
                # Mark DONE for all news_ids that produced a primary model row
                # ============================================================
                for nid in done_news_ids:
                    conn.execute(
                        "UPDATE raw_news SET status = 'DONE', processing_at = NULL"
                        " WHERE id = ? AND status = 'PROCESSING';",
                        (nid,),
                    )
                # v323：区分"分析失败"与"根本没拿到模型响应"。
                # 实测额度耗尽期间每批都是 0/8 ok | 8 failed，失败原因全是通道熔断；
                # 而下面的回写会 retry_count+1，三次之后这条新闻就被永久标 FAILED
                # —— 一次额度事故就把新闻烧成死数据（全表 599 条 FAILED 里就混着这类）。
                # 通道/额度类失败不计重试次数，只退回 PENDING 等额度恢复。
                _CHANNEL_FAIL_KW = ("熔断", "quota", "429", "所有通道", "无可用通道",
                                    "no available", "容量不足", "额度")

                def _is_channel_failure(err) -> bool:
                    _t = str(err or "")
                    return any(k.lower() in _t.lower() for k in _CHANNEL_FAIL_KW)

                for f in failures:
                    nid = f["news_id"]
                    if nid > 0 and nid not in done_news_ids:
                        if _is_channel_failure(f.get("error")):
                            # 只清 PROCESSING 占位 + 冷却，不动 retry_count
                            conn.execute(
                                "UPDATE raw_news SET status='PENDING',"
                                " processing_at = datetime('now','localtime','+' || ? || ' seconds')"
                                " WHERE id = ? AND status = 'PROCESSING';",
                                (_BATCH_RETRY_COOLDOWN_SEC, nid),
                            )
                            continue
                        # ── FIX-8（2026-11-09）：一次失败不是终态 ──
                        # 修复前：直接 status='FAILED' 且 retry_count 不动
                        #   → 实测 322 条 retry_count=0 就进了 FAILED（一次没成永久放弃）。
                        # 现在：retry_count+1 + 冷却，达 _MAX_PENDING_RETRIES 才 FAILED。
                        # 注意 SQLite 的 UPDATE 里所有表达式都读**更新前**的值，故 CASE 用 retry_count+1 一致。
                        conn.execute(
                            "UPDATE raw_news SET retry_count = COALESCE(retry_count, 0) + 1,"
                            " status = CASE WHEN COALESCE(retry_count, 0) + 1 >= ?"
                            "               THEN 'FAILED' ELSE 'PENDING' END,"
                            " processing_at = CASE WHEN COALESCE(retry_count, 0) + 1 >= ?"
                            "               THEN NULL"
                            "               ELSE datetime('now','localtime','+' || ? || ' seconds') END"
                            " WHERE id = ? AND status = 'PROCESSING';",
                            (
                                _MAX_PENDING_RETRIES,
                                _MAX_PENDING_RETRIES,
                                _BATCH_RETRY_COOLDOWN_SEC,
                                nid,
                            ),
                        )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
            return written

        # 调用 _batch_persist，如果数据库锁定则重试
        max_retries = 6
        retry_delay = 0.5  # 秒
        decision_infos = []
        try:
            for attempt in range(max_retries):
                try:
                    decision_infos = _batch_persist()
                    # v424：落库成功 → 清掉 spool（成果已经在 ai_decisions 里了）
                    try:
                        _spool_clear()
                    except Exception:
                        pass
                    break  # 成功则跳出重试循环
                except sqlite3.OperationalError as e:
                    if "database is locked" in str(e).lower() and attempt < max_retries - 1:
                        print(f"[{_now()}] [AI] 数据库锁定，第 {attempt + 1} 次重试...")
                        await asyncio.sleep(retry_delay * (attempt + 1))  # 指数退避
                    else:
                        raise  # 重试次数用完或其他错误，抛出异常
        except Exception as exc:
            # 批次落库失败不再杀死 ai_worker：记录后跳过本批，
            # 并把 PROCESSING 的新闻退回 PENDING 以便下轮重新认领。
            print(f"[{_now()}] [AI] Batch persist 失败，跳过本批: {type(exc).__name__}: {str(exc)[:200]}")
            # v322：打印完整堆栈 —— 之前只有异常摘要，定位脏字段靠猜。
            # 历史事故：ValueError: invalid literal for int() with base 10: 'OIL|WTI|BUY'
            # → 整批 16 条成果作废 → 队头反复占位 → 「待分析」永不下降。
            try:
                import traceback as _tb_persist
                _tb_persist.print_exc()
            except Exception:
                pass
            try:
                conn = _open_db()
                conn.execute(
                    "UPDATE raw_news SET retry_count = retry_count + 1,"
                    # v322：PENDING 状态下 processing_at 当作「冷却到什么时候」用。
                    # 原来置 NULL → 这批立刻被重新认领 → ORDER BY id ASC 让队头
                    # 永远被同一批占住 → 新新闻永远排在后面 → 「待分析」只增不减。
                    #
                    # v322 二次修正：**冷却必须长于单批耗时**，否则冷却形同虚设。
                    # 实测单批 wall=390 秒（行情源慢 + 16 条并发分析），
                    # 而冷却只有 120 秒 → 冷却一过同一批立刻又被认领
                    # → 再跑 390 秒 → 再失败 → **无限循环**（队头永久霸占）。
                    # 冷却取「单批最坏耗时 + 余量」= 15 分钟，与看门狗的僵尸
                    # 阈值同量级 → 冷却期内同批不会被再认领，新新闻能顶上来。
                    " processing_at = datetime('now', 'localtime', '+' || ? || ' seconds'),"
                    " status = CASE WHEN retry_count + 1 >= ? THEN 'FAILED' ELSE 'PENDING' END"
                    " WHERE status = 'PROCESSING'",
                    (_BATCH_RETRY_COOLDOWN_SEC, _MAX_PENDING_RETRIES),
                )
                conn.commit()
                conn.close()
            except Exception as _rollback_exc:
                # v322: **整批退回 PENDING 失败 = 全批卡在 PROCESSING**
                # → 要等看门狗 15 分钟才回收，期间这批新闻反复被认领、
                # 又反复失败 → 队头阻塞（「待分析」不降）。必须留痕。
                print(f"  [AI] ⚠️ 失败批次退回 PENDING 失败: "
                      f"{type(_rollback_exc).__name__}: {str(_rollback_exc)[:120]}",
                      flush=True)

        # v322: 本批处理完毕（成功或失败）→ 注销「在办批次」+ 停心跳，
        # 看门狗恢复正常回收（此时残留的 PROCESSING 才是真僵尸）。
        _active_batch_release(batch_ids, _hb_stop)

        # v175：重要新闻 / 海力士强买入 → 手机推送（Server酱+邮件，fire-and-forget，不阻塞）
        try:
            _maybe_push_phone_alerts(successes, content_map, timestamp_map, published_map)
        except Exception as _push_exc:
            # v322: 推送失败 → 你**不会收到**重要信号提醒，却以为一切正常
            print(f"  [AI] ⚠️ 手机推送失败: {type(_push_exc).__name__}: "
                  f"{str(_push_exc)[:120]}", flush=True)

        # ==================================================================
        # Phase 4 - Print comparison & send Feishu (with Chinese translation)
        # ==================================================================
        # Group decisions by news_id
        by_news: Dict[int, List[Dict[str, Any]]] = {}
        for d in decision_infos:
            by_news.setdefault(d["news_id"], []).append(d)

        for nid, group in by_news.items():
            # 优先使用真正的新闻原文，而不是模型的 translated_title
            # translated_title 实际上是主模型的推理结论，不应该作为新闻原文显示
            original_news = content_map.get(nid, f"News #{nid}")
            display_title = original_news
            print(f"\n  ┌─ News #{nid}: {display_title[:70]}")
            # Collect for Feishu card (one card per news item)
            feishu_lines: List[str] = []
            consensus: Dict[str, int] = {}
            for d2 in group:
                res2 = d2["result"]
                label = d2["model_label"]
                action2 = res2["suggested_action"]
                score2 = res2["sentiment_score"]
                # 飞书推送只用短结论（完整八段式太长，手机端阅读负担重）
                model_reason = (str(res2.get("reasoning") or "").strip()[:120]
                                or str(res2.get("reasoning_path") or "").strip()[:120])
                print(
                    f"  │ [{label:8s}] {action2:4s} {score2:+.3f} | {model_reason[:100]}"
                )
                consensus[action2] = consensus.get(action2, 0) + 1
                feishu_lines.append(
                    f"**{label}**: {action2} ({score2:+.3f}) — {model_reason}"
                )
                # Doubao secondary verification — if present, append to Feishu card
                doubao = d2.get("doubao")
                if doubao:
                    db_action = doubao["action"]
                    db_reason = str(doubao["reasoning"])[:120]
                    print(
                        f"  │ [Doubao  ] {db_action:4s}       | {db_reason}"
                    )
                    consensus[db_action] = consensus.get(db_action, 0) + 1
                    feishu_lines.append(
                        f"**Doubao**: {db_action} — {db_reason}"
                    )

            # Consensus line
            parts = [f"{v}×{k}" for k, v in sorted(consensus.items(), key=lambda x: -x[1])]
            consensus_str = " | ".join(parts)
            print(f"  └─ Consensus: {consensus_str}")
            # HOLD/无方向：共识里没有任何 BUY/SELL 票 → 不发飞书卡片（避免噪音轰炸）
            if not any(k in ("BUY", "SELL") for k in consensus):
                continue
            # Fire-and-forget Feishu comparison card (Chinese title)
            feishu_body = "\n\n".join(feishu_lines)
            news_ts = timestamp_map.get(nid, "")
            asyncio.create_task(
                send_feishu_alert(
                    f"{display_title}\n\n**Consensus**: {consensus_str}",
                    consensus_str,
                    0.0,
                    feishu_body,
                    news_ts,
                )
            )

        for f in failures:
            label = f.get("model_label", "?")
            error_msg = f.get("error", "Unknown error")
            print(
                f"[{_now()}] [AI] news=#{f['news_id']} [{label}] FAILED:"
                f" {error_msg}"
            )
        # Batch summary
        try:
            s_dt = datetime.fromisoformat(batch_start)
            e_dt = datetime.fromisoformat(batch_end)
            batch_latency = (e_dt - s_dt).total_seconds()
        except Exception:
            batch_latency = -1
        total_tasks = len(batch)
        print(
            f"[{_now()}] [AI] Batch done | {len(successes)}/{total_tasks} ok"
            f" | {len(failures)} failed | wall={batch_latency:.1f}s"
        )
