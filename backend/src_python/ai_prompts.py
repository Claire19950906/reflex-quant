# -*- coding: utf-8 -*-
"""用户自定义 AI 分析 prompt 库。

启用的条目（enabled=1）会注入每次 AI 分析的 system prompt 尾部，
让用户可以不改代码就向模型追加分析指令（如"遇到降息新闻优先考虑黄金"）。
CRUD 走 API（api_server），engine（ai_worker）只读启用条目（5s TTL 缓存）。
"""

from __future__ import annotations

import os
import sqlite3
import time
from typing import Any, Dict, List, Optional

import db


def _connect() -> sqlite3.Connection:
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# CRUD（API 用）
# ---------------------------------------------------------------------------

def list_prompts(conn: Optional[sqlite3.Connection] = None) -> List[Dict[str, Any]]:
    """全部自定义 prompt（含停用），按 id 倒序。"""
    own = conn is None
    conn = conn or _connect()
    try:
        rows = conn.execute(
            "SELECT id, name, content, enabled, created_at, updated_at"
            " FROM ai_prompts ORDER BY id DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        if own:
            conn.close()


def add_prompt(name: str, content: str, conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    """新增自定义 prompt（enabled=1，立即生效，engine ≤5s 同步）。"""
    name = (name or "").strip()[:60]
    content = (content or "").strip()
    if not content:
        raise ValueError("prompt 内容不能为空")
    own = conn is None
    conn = conn or _connect()
    try:
        cur = conn.execute(
            "INSERT INTO ai_prompts (name, content, enabled) VALUES (?, ?, 1)",
            (name or content[:20], content),
        )
        conn.commit()
        return {
            "ok": True,
            "id": int(cur.lastrowid),
            "name": name or content[:20],
        }
    finally:
        if own:
            conn.close()


def delete_prompt(prompt_id: int, conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    """删除自定义 prompt（硬删除）。"""
    own = conn is None
    conn = conn or _connect()
    try:
        cur = conn.execute("DELETE FROM ai_prompts WHERE id=?", (int(prompt_id),))
        conn.commit()
        if cur.rowcount == 0:
            return {"ok": False, "error": "prompt 不存在"}
        return {"ok": True}
    finally:
        if own:
            conn.close()


def toggle_prompt(prompt_id: int, conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    """切换启停（1↔0）。"""
    own = conn is None
    conn = conn or _connect()
    try:
        row = conn.execute(
            "SELECT enabled FROM ai_prompts WHERE id=?", (int(prompt_id),)
        ).fetchone()
        if not row:
            return {"ok": False, "error": "prompt 不存在"}
        next_enabled = 0 if int(row["enabled"]) else 1
        conn.execute(
            "UPDATE ai_prompts SET enabled=?, updated_at=datetime('now','localtime')"
            " WHERE id=?",
            (next_enabled, int(prompt_id)),
        )
        conn.commit()
        return {"ok": True, "enabled": next_enabled}
    finally:
        if own:
            conn.close()


# ---------------------------------------------------------------------------
# engine 注入（只读启用条目，TTL 缓存）
# ---------------------------------------------------------------------------

_enabled_cache: Dict[str, Any] = {"ts": 0.0, "items": []}

# v436 P0-2：哪些自定义 prompt **不属于**主分析。
# 事故（2026-11-17 21:3x 实测）：ai_prompts 表里 4 条启用条目全部被拼进主分析
# system prompt，并且带着「优先级最高，必须遵守」的抬头：
#   v186_reflection_prompt  → "你是一个金融反思助手…识别系统性偏差"（给反思助手用）
#   v186_hold_prompt        → "你是一个金融置信度评估助手…"（给 HOLD 判定用）
#   v186_causal_extraction  → "你是一个金融因果链提取助手…"（给因果抽取用）
#   v186_insight_extraction → "你是一个金融经验沉淀助手…"（给经验提炼用）
# 这 4 条都不是主分析的指令，642 字符全是噪音，还让主模型扮演错误的角色。
# 现在按名字识别并跳过（只要命中下列任一关键词即视为"其它任务链的 prompt"）。
_SUBAGENT_PROMPT_MARKERS = (
    "reflection", "反思",
    "hold", "置信度",
    "causal_extraction", "因果链提取", "因果提取",
    "insight_extraction", "经验沉淀", "经验提炼",
    "review", "复盘助手",
)


def _is_subagent_prompt(name: str) -> bool:
    n = (name or "").strip().lower()
    return any(m in n for m in _SUBAGENT_PROMPT_MARKERS)


def load_enabled_prompts(ttl: float = 5.0) -> List[str]:
    """启用的自定义 prompt 内容列表（5s TTL 缓存，engine 每次分析前调用）。"""
    global _enabled_cache
    now = time.monotonic()
    if now - float(_enabled_cache.get("ts", 0.0)) < ttl:
        return list(_enabled_cache.get("items", []))
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT name, content FROM ai_prompts WHERE enabled=1 ORDER BY id"
        ).fetchall()
        items = []
        skipped = []
        for r in rows:
            name = str(r["name"] or "").strip()
            content = str(r["content"] or "").strip()
            if not content:
                continue
            if _is_subagent_prompt(name):
                skipped.append(name or "未命名")
                continue
            items.append(f"[自定义指令：{name or '未命名'}]\n{content}")
        if skipped:
            print(f"[PROMPTS] 跳过 {len(skipped)} 条非主分析用途的指令（其它任务链专用）："
                  f"{skipped}", flush=True)
        _enabled_cache.update({"ts": now, "items": items})
        return items
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 主 prompt 覆盖（页面可编辑：system_prompt / json_force_prompt /
# market_structure_block / doubao_prompt；无记录时引擎用代码内置默认）
# ---------------------------------------------------------------------------

_main_cache: Dict[str, Any] = {"ts": 0.0, "data": {}}

# 代码内置主 prompt 的代际版本。任何让内置 prompt 语义发生实质变化（补字段、
# 加八段约束、加风险坐实指令等）的升级都要 +1。DB 里 base_version 低于此值的
# 覆盖会被判定为"旧版覆盖"而忽略，回退到代码内置默认，防止旧内容遮蔽新约束。
MAIN_PROMPT_VERSION = 5

# 已告警过的旧版覆盖 kind（去重，避免 5s TTL 刷新时刷屏）
_warned_stale: set = set()


def load_main_prompt_overrides(ttl: float = 5.0) -> Dict[str, str]:
    """读 ai_main_prompts 覆盖表（kind → content，5s TTL），供 engine 覆盖内置 prompt。"""
    global _main_cache
    now = time.monotonic()
    if now - float(_main_cache.get("ts", 0.0)) < ttl:
        return dict(_main_cache.get("data", {}))
    conn = _connect()
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(ai_main_prompts)")}
        ver_col = ", base_version" if "base_version" in cols else ""
        rows = conn.execute(f"SELECT kind, content{ver_col} FROM ai_main_prompts").fetchall()
        data: Dict[str, str] = {}
        for r in rows:
            kind = str(r["kind"])
            content = str(r["content"])
            if not content:
                continue
            if "base_version" in cols and int(r["base_version"] or 0) < MAIN_PROMPT_VERSION:
                if kind not in _warned_stale:
                    _warned_stale.add(kind)
                    print(f"[ai_prompts] 忽略旧版主 prompt 覆盖 {kind}（base_version="
                          f"{r['base_version']} < {MAIN_PROMPT_VERSION}），回退代码内置默认")
                continue
            data[kind] = content
        _main_cache.update({"ts": now, "data": data})
        return data
    finally:
        conn.close()


def save_main_prompt(kind: str, content: str, conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    """保存主 prompt 覆盖（upsert）。kind ∈ system_prompt/json_force_prompt/
    market_structure_block/doubao_prompt。"""
    kind = (kind or "").strip()
    if kind not in ("system_prompt", "json_force_prompt", "market_structure_block", "doubao_prompt"):
        raise ValueError("未知的主 prompt 类型")
    content = (content or "").strip()
    if not content:
        raise ValueError("prompt 内容不能为空")
    own = conn is None
    conn = conn or _connect()
    try:
        # 确保 base_version 列存在（老库未跑 migrate 时也自愈），并写入当前代际
        db._ensure_columns(conn, "ai_main_prompts", db._AI_MAIN_PROMPTS_COLUMNS)
        conn.execute(
            """INSERT INTO ai_main_prompts (kind, name, content, base_version, updated_at)
               VALUES (?, ?, ?, ?, datetime('now','localtime'))
               ON CONFLICT(kind) DO UPDATE SET content=excluded.content,
                   base_version=excluded.base_version,
                   updated_at=datetime('now','localtime')""",
            (kind, kind, content, MAIN_PROMPT_VERSION),
        )
        conn.commit()
        _main_cache.update({"ts": 0.0, "data": {}})  # 失效缓存
        return {"ok": True, "kind": kind}
    finally:
        if own:
            conn.close()


def reset_main_prompt(kind: str, conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    """删除主 prompt 覆盖 → 引擎回到代码内置默认。"""
    kind = (kind or "").strip()
    if kind not in ("system_prompt", "json_force_prompt", "market_structure_block", "doubao_prompt"):
        raise ValueError("未知的主 prompt 类型")
    own = conn is None
    conn = conn or _connect()
    try:
        conn.execute("DELETE FROM ai_main_prompts WHERE kind=?", (kind,))
        conn.commit()
        _main_cache.update({"ts": 0.0, "data": {}})
        return {"ok": True, "kind": kind}
    finally:
        if own:
            conn.close()


# ---------------------------------------------------------------------------
# 品种专项分析规则（ai_asset_rules 表）：按金融品类单独设置分析规则
#   asset  = 品种代码（XAU/WTI/BTC/ETH/NVDA/TSLA…）或 all（全品种兜底）
#   注入方式：引擎拼 prompt 时把所有启用规则按品种分节注入（AI 归类后套用对应品种）
# ---------------------------------------------------------------------------

# 品种代码 → 显示名（前端下拉与注入标注用；未列出的用代码本身）
ASSET_DISPLAY: Dict[str, str] = {
    "all": "全品种",
    "XAU": "黄金", "WTI": "原油", "BTC": "比特币", "ETH": "以太坊", "SOL": "Solana",
    "NVDA": "英伟达", "TSLA": "特斯拉", "AAPL": "苹果", "MSTR": "微策略",
    "MSFT": "微软", "AMZN": "亚马逊", "GOOGL": "谷歌", "COIN": "Coinbase",
    "PLTR": "Palantir", "META": "Meta", "SMCI": "美超微", "MARA": "Marathon",
    "RIOT": "Riot",
}


def _norm_asset(asset: str) -> str:
    """品种代码归一：统一大写，但 all 保持小写（与前端下拉/ASSET_DISPLAY 约定一致）。"""
    code = (asset or "").strip()[:20]
    lowered = code.lower()
    if lowered in ("all", "全部", "全品种"):
        return "all"
    return code.upper()


def _asset_label(code: str, asset_name: str = "") -> str:
    """注入标注用的显示名：自定义名优先，其次内置建议映射（大小写不敏感）。

    映射未收录的自定义品种（如 COPPER/NATGAS）回落代码本身——这样前端
    placeholder 至少有值可显示，注入标注也是 [COPPER] 而非空白。
    """
    name = (asset_name or "").strip()
    if name:
        return name
    key = (code or "").strip()
    return ASSET_DISPLAY.get(key) or ASSET_DISPLAY.get(key.upper()) or key


def list_asset_rules(conn: Optional[sqlite3.Connection] = None) -> List[Dict[str, Any]]:
    """全部品种规则（含停用），按 asset + priority 排序。"""
    own = conn is None
    conn = conn or _connect()
    try:
        rows = conn.execute(
            "SELECT id, asset, asset_name, content, enabled, priority, created_at, updated_at"
            " FROM ai_asset_rules ORDER BY asset, priority, id"
        ).fetchall()
        out = []
        for r in rows:
            item = dict(r)
            item["display"] = _asset_label(str(item.get("asset") or ""), item.get("asset_name"))
            # 内置默认名（不含自定义名），供前端 input 的 placeholder 提示"留空会回退到什么"
            item["default_display"] = _asset_label(str(item.get("asset") or ""), "")
            out.append(item)
        return out
    finally:
        if own:
            conn.close()


def add_asset_rule(asset: str, content: str, asset_name: str = "",
                   priority: int = 0, conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    """新增品种规则（enabled=1，立即生效，engine ≤5s 同步）。"""
    code = _norm_asset(asset)
    content = (content or "").strip()
    if not code:
        raise ValueError("品种代码不能为空")
    if not content:
        raise ValueError("规则内容不能为空")
    own = conn is None
    conn = conn or _connect()
    try:
        cur = conn.execute(
            "INSERT INTO ai_asset_rules (asset, asset_name, content, enabled, priority)"
            " VALUES (?, ?, ?, 1, ?)",
            (code, (asset_name or "").strip()[:40], content, int(priority)),
        )
        conn.commit()
        _asset_cache.update({"ts": 0.0, "items": []})  # 失效缓存
        return {"ok": True, "id": int(cur.lastrowid), "asset": code}
    finally:
        if own:
            conn.close()


def update_asset_rule(rule_id: int, content: str = "", asset_name: str = "",
                      priority: Optional[int] = None,
                      conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    """更新品种规则内容（content 为空表示不改）。"""
    own = conn is None
    conn = conn or _connect()
    try:
        row = conn.execute("SELECT id FROM ai_asset_rules WHERE id=?", (int(rule_id),)).fetchone()
        if not row:
            return {"ok": False, "error": "规则不存在"}
        sets, params = [], []
        if (content or "").strip():
            sets.append("content=?")
            params.append(content.strip())
        if asset_name is not None:
            sets.append("asset_name=?")
            params.append((asset_name or "").strip()[:40])
        if priority is not None:
            sets.append("priority=?")
            params.append(int(priority))
        if not sets:
            return {"ok": True, "id": int(rule_id)}
        sets.append("updated_at=datetime('now','localtime')")
        params.append(int(rule_id))
        conn.execute(f"UPDATE ai_asset_rules SET {', '.join(sets)} WHERE id=?", params)
        conn.commit()
        _asset_cache.update({"ts": 0.0, "items": []})
        return {"ok": True, "id": int(rule_id)}
    finally:
        if own:
            conn.close()


def delete_asset_rule(rule_id: int, conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    """删除品种规则（硬删除）。"""
    own = conn is None
    conn = conn or _connect()
    try:
        cur = conn.execute("DELETE FROM ai_asset_rules WHERE id=?", (int(rule_id),))
        conn.commit()
        _asset_cache.update({"ts": 0.0, "items": []})
        if cur.rowcount == 0:
            return {"ok": False, "error": "规则不存在"}
        return {"ok": True}
    finally:
        if own:
            conn.close()


def toggle_asset_rule(rule_id: int, conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    """切换品种规则启停（1↔0）。"""
    own = conn is None
    conn = conn or _connect()
    try:
        row = conn.execute(
            "SELECT enabled FROM ai_asset_rules WHERE id=?", (int(rule_id),)
        ).fetchone()
        if not row:
            return {"ok": False, "error": "规则不存在"}
        next_enabled = 0 if int(row["enabled"]) else 1
        conn.execute(
            "UPDATE ai_asset_rules SET enabled=?, updated_at=datetime('now','localtime')"
            " WHERE id=?",
            (next_enabled, int(rule_id)),
        )
        conn.commit()
        _asset_cache.update({"ts": 0.0, "items": []})
        return {"ok": True, "enabled": next_enabled}
    finally:
        if own:
            conn.close()


_asset_cache: Dict[str, Any] = {"ts": 0.0, "items": []}


def load_asset_rules_block(ttl: float = 5.0) -> str:
    """启用品种规则的注入文本（5s TTL）。无规则 → ""（不注入、零开销）。

    输出按品种分节，AI 识别主体 + 归类后套用对应品种的规则：
        [XAU 黄金]
        规则内容…
        [WTI 原油]
        规则内容…
    """
    global _asset_cache
    now = time.monotonic()
    if now - float(_asset_cache.get("ts", 0.0)) < ttl:
        return str(_asset_cache.get("items") or "")
    conn = _connect()
    try:
        rows = conn.execute(
            # all 兜底规则恒排最后（无论 priority 取值），确保专属规则先出现
            "SELECT asset, asset_name, content FROM ai_asset_rules"
            " WHERE enabled=1"
            " ORDER BY CASE WHEN lower(asset)='all' THEN 1 ELSE 0 END, asset, priority, id"
        ).fetchall()
        sections: List[str] = []
        _seen_rule = set()
        for r in rows:
            code = _norm_asset(str(r["asset"] or ""))
            content = str(r["content"] or "").strip()
            if not code or not content:
                continue
            # v436 P0-2：同品种同内容去重。
            # 实测（2026-11-17）：24 条启用规则只对应 9 个品种 —— DXY/EURUSD/SOL/US10Y/USDJPY
            # 各重复 4 遍，注入块 1,098 字符里有约 700 是同一句话重复四遍。
            _dup_key = (code.lower(), content.replace(" ", ""))
            if _dup_key in _seen_rule:
                continue
            _seen_rule.add(_dup_key)
            label = _asset_label(code, r["asset_name"])
            head = f"[{code} {label}]" if label and label != code else f"[{code}]"
            sections.append(f"{head}\n{content}")
        block = ""
        if sections:
            block = (
                "═══ 品种专项分析规则（按品种套用，仅适用于对应品种）═══\n"
                "以下规则是用户为特定金融品种设定的分析偏好——分析 target_asset 或\n"
                "sibling_decisions 中对应品种时【必须遵守】；品种不匹配的规则不适用。\n"
                "多品种新闻中：主品种套用主品种规则、每个副品种套用其自身品种规则。\n"
                "与上文通用规则冲突时，以本节品种规则为准；但主体识别、资产归类、\n"
                "八段结构、score 锚定与输出格式（20 字段）不得因本节规则而省略或改写。\n\n"
                + "\n\n".join(sections)
            )
        _asset_cache.update({"ts": now, "items": block})
        return block
    except Exception:
        return ""
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# v192 #1: review_insights 注入 (5s TTL cache)
# review_agent.daemon_loop 24h 提炼的「实战经验规则」从此进入 AI 决策 prompt。
# ---------------------------------------------------------------------------
_INSIGHTS_CACHE_MAX = 8         # 注入上限：避免 token 爆炸
# v413: 样本下限与 config 对齐（此前这里写死 1，而 config.REVIEW_INSIGHTS_MIN_SAMPLES=5，
# 同一件事两套门槛 —— 实测 32 条规则里 17 条样本只有 2~4，却照样进 prompt）。
# 环境变量 INSIGHTS_MIN_SAMPLES 可临时覆盖。
try:
    _INSIGHTS_CACHE_MIN_SAMPLES = max(
        1, int(os.environ.get("INSIGHTS_MIN_SAMPLES")
               or getattr(__import__("config"), "REVIEW_INSIGHTS_MIN_SAMPLES", 5))
    )
except Exception:
    _INSIGHTS_CACHE_MIN_SAMPLES = 5
_INSIGHTS_CACHE: Dict[str, Any] = {"ts": 0.0, "items": ""}


def _insights_allow_unmeasured() -> bool:
    """v413: 是否允许注入「不可测」经验（observe_only=1）。

    默认 0 = 不注入。理由：`insight_hypothesis` 判定某条经验"解不成可测假设、
    不带方向、单位不等价"后给它打 observe_only=1，这类经验**无法被验证，
    也无法被自己纠错**——喂进 prompt 等于让系统按不可证伪的话做判断。
    实测 2026-11-18：可注入的 32 条里 32 条都是 observe_only=1。
    需要恢复：INSIGHTS_INJECT_UNMEASURABLE=1。
    """
    return os.environ.get("INSIGHTS_INJECT_UNMEASURABLE", "0") == "1"


def _norm_insights_asset(s: str) -> str:
    return str(s or "").strip().upper()


def load_recent_insights_block(
    target_asset: str = "",
    ttl: float = 5.0,
    max_items: int = _INSIGHTS_CACHE_MAX,
    return_ids: bool = False,
) -> Any:
    """从 review_insights 注入「实战经验规则」到 AI prompt。

    排序：① 命中 target_asset 的专属规则优先；② 其次是 asset='' 通则；
           ③ 同优先级按 source_samples DESC（多样本更可信）；④ 再按 id DESC（新规则优先）。
    限制：最多 max_items 条（默认 8），避免 prompt 膨胀。
    缓存：5s TTL（与 ai_asset_rules / ai_prompts 一致）；调用者传不同 target_asset 会各自缓存一次。
    无 insight → ""（不注入、零开销）。

    v200 P0-A3: return_ids=True 时同时返回 (text, [int_ids]) 元组，
    让 ai_worker 把命中 ids 写回 ai_decisions.insight_hit_ids，闭环可见。
    默认 return_ids=False 保持向后兼容（只返回 text）。
    """
    target = _norm_insights_asset(target_asset)
    cache_key = f"{target}:{max_items}:{1 if return_ids else 0}"
    cache_bucket = _insights_cache_store(cache_key)
    now = time.monotonic()
    if now - float(cache_bucket.get("ts", 0.0)) < ttl:
        cached_text = str(cache_bucket.get("items") or "")
        cached_ids = list(cache_bucket.get("ids") or [])
        return (cached_text, cached_ids) if return_ids else cached_text
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT id, rule, asset, scope, source_samples, kind, counter_factual"
            "  FROM review_insights"
            " WHERE active=1 AND manual_deactivated=0 AND kind=1"
            "   AND COALESCE(source_samples, 0) >= ?"
            # v331: 被客观验证判 failed 的规律**不进 prompt**。它们当初是
            # "同类决策多次 partial" 推出来的教训, 事后用真实 K 线一验就证伪
            # (实测 181 条仍 active, 175 条正被喂进 prompt) —— 假教训比没教训更糟。
            # 证据充分的那批已被 retire_failed_insights.py 停用; 这里再堵一层,
            # 保证即使停用漏跑也不会污染决策。
            # insufficient_sample 同理: "没验过"的经验不能当"实战教训"注入。
            "   AND COALESCE(verification_verdict, '') NOT IN ('failed', 'insufficient_sample')"
            # v413: 不可测经验不进 prompt（observe_only=1 表示"解不成可测假设"，
            # 这类规则无法验证、无法自我纠错，喂进去只是让模型按不可证伪的话下单）。
            "   AND (? = 1 OR COALESCE(observe_only, 0) = 0)"
            " ORDER BY CASE WHEN ? <> '' AND upper(COALESCE(asset,'')) = ? THEN 0"
            "              WHEN upper(COALESCE(asset,'')) IN ('', 'NONE') THEN 1 ELSE 2 END,"
            "          COALESCE(source_samples, 0) DESC, id DESC"
            " LIMIT ?",
            (1 if _insights_allow_unmeasured() else 0,
             _INSIGHTS_CACHE_MIN_SAMPLES, target, target, max_items),
        ).fetchall()
        block = ""
        hit_ids: List[int] = []
        if rows:
            lines: List[str] = []
            for r in rows:
                rule = str(r["rule"] or "").strip()
                if not rule:
                    continue
                ns = int(r["source_samples"] or 0)
                asset_tag = ""
                if r["asset"] and _norm_insights_asset(r["asset"]) not in ("", "NONE"):
                    asset_tag = f"[{_norm_insights_asset(r['asset'])}] "
                counter = str(r["counter_factual"] or "").strip()
                if counter:
                    # v453: 反例是错误码堆叠时清洗成人话，别把机器日志塞进 prompt
                    try:
                        from review.review_agent import clean_counter_factual as _clean_cf
                        counter = _clean_cf(counter, max_chars=60)
                    except Exception:
                        counter = counter[:60]
                # 单条格式:  "[XAU] 经验规则 (n=5)" 或 "[XAU] 经验规则 (n=5)  ← 反例: ..."
                # v195: asset='NONE' 显示成通则 (不标 [NONE] 前缀)
                tail = f" (n={ns})"
                if counter:
                    tail += f"  ← 反例: {counter[:80]}"
                lines.append(f"• {asset_tag}{rule}{tail}")
                try:
                    hit_ids.append(int(r["id"]))
                except (TypeError, ValueError):
                    pass
            block = (
                "═══ 近期实战经验规则（来源: review_agent 复盘提炼；按品种匹配，仅适用）═══\n"
                "以下是从最近实战复盘中沉淀的方向性经验规则，仅做方向参考。\n"
                "用法：分析结果与本节一致时可适度加权，与本节冲突时优先以新闻事实和当前\n"
                "宏观结构为准，不要机械执行。每条规则都标注样本量 n，n 越大可信度越高。\n"
                "标注了「反例」的规则表示已被反例削弱，应降低权重；asset 前缀匹配的规则\n"
                "只适用对应品种，未命中则忽略。\n\n"
                + "\n".join(lines)
            )
        cache_bucket.update({"ts": now, "items": block, "ids": hit_ids})
        return (block, hit_ids) if return_ids else block
    except Exception:
        return ("", []) if return_ids else ""
    finally:
        conn.close()


def _insights_cache_store(key: str) -> Dict[str, Any]:
    """按 (target_asset, max_items) 分桶缓存。"""
    buckets = _INSIGHTS_CACHE.setdefault("_buckets", {})
    if key not in buckets:
        buckets[key] = {"ts": 0.0, "items": ""}
    return buckets[key]
