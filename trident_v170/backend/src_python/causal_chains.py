# -*- coding: utf-8 -*-
"""因果链库 — 宏观/市场因果知识条目，注入 AI 新闻分析的 [因果链分析] 段。

每条链 = 因(cause) + 果(effect) + 触发词(trigger_words) + 备注(note) + 启停(enabled)。
例：因=美元加息/降息；果=黄金/原油/加密承压或走强；触发词=美联储,加息,降息,fomc。

AI 分析新闻时：命中触发词的启用条目被注入到八段式之后的 [因果链分析] 段
（因：…；果：…），AI 结合新闻解读因果，并在"总结"与八段式互相印证。
CRUD 走 API（api_server），engine（ai_worker）只读命中条目（5s TTL 缓存）。
"""

from __future__ import annotations

import sqlite3
import sys
import time
from typing import Any, Dict, List, Optional

import db


def _derive_strategy(conn, chain_id: int) -> None:
    """把策略列从知识列派生出来 (best-effort, 失败不影响知识库入库)。

    bridge 的因果链匹配用 `WHERE active=1 AND asset=?`, asset 必须是由
    trigger_words 推导出的真实 ticker, 否则新链永远匹配不到、反馈闭环空转。
    推导规则与 causal_chain_evaluator.load_active_chains 同源。
    """
    try:
        from causal_chain_evaluator import derive_strategy_columns
        derive_strategy_columns(conn, chain_ids=[chain_id])
    except Exception as e:  # 策略列尚不存在 / 推导失败 → 不阻断入库
        print(f"[causal_chains] 派生策略列失败 (不影响入库): {e}", file=sys.stderr)


def _connect() -> sqlite3.Connection:
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_row(conn: Optional[sqlite3.Connection]) -> sqlite3.Connection:
    """外部传入 conn 时确保 row_factory=Row（避免 dict(r) 失败）。"""
    if conn is not None and conn.row_factory is not sqlite3.Row:
        conn.row_factory = sqlite3.Row
    return conn or _connect()


# ---------------------------------------------------------------------------
# CRUD（API 用）
# ---------------------------------------------------------------------------

def list_chains(conn: Optional[sqlite3.Connection] = None) -> List[Dict[str, Any]]:
    """全部因果链（含停用），按 id 倒序。"""
    own = conn is None
    conn = _ensure_row(conn)
    try:
        rows = conn.execute(
            "SELECT id, name, cause, effect, trigger_words, note, enabled,"
            " COALESCE(origin, 'manual') AS origin,"
            " COALESCE(weight, 1) AS weight,"
            " COALESCE(mechanism, '') AS mechanism,"
            " COALESCE(precondition, '') AS precondition,"
            " COALESCE(invalidation, '') AS invalidation,"
            " created_at, updated_at FROM causal_chains ORDER BY id DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        # 老库尚未补 origin/weight 列 → 降级查询
        rows = conn.execute(
            "SELECT id, name, cause, effect, trigger_words, note, enabled,"
            " created_at, updated_at FROM causal_chains ORDER BY id DESC"
        ).fetchall()
        out = [dict(r) for r in rows]
        for item in out:
            item["origin"] = "manual"
            item["weight"] = 1
            item["mechanism"] = ""
            item["precondition"] = ""
            item["invalidation"] = ""
        return out
    finally:
        if own:
            conn.close()


def add_chain(
    cause: str, effect: str, *, name: str = "", trigger_words: str = "",
    note: str = "", mechanism: str = "", precondition: str = "",
    invalidation: str = "", origin: str = "manual", weight: float = 1.0,
    conn: Optional[sqlite3.Connection] = None,
) -> Dict[str, Any]:
    """新增因果链（enabled=1，立即生效，engine ≤5s 同步）。

    origin: manual=手工添加 / auto_real=历史交易复盘自动提炼 /
            auto_quick_sim=快速评测自动提炼 / auto_mixed=合并样本提炼
    weight: 权重（越大命中时占比越高）；异常值回落到 1.0。
    """
    cause = (cause or "").strip()
    effect = (effect or "").strip()
    if not cause or not effect:
        raise ValueError("因(cause)与果(effect)都不能为空")
    if origin not in ("manual", "auto_real", "auto_quick_sim", "auto_mixed"):
        origin = "manual"
    try:
        weight_f = float(weight)
    except (TypeError, ValueError):
        weight_f = 1.0
    if weight_f < 0 or weight_f > 100:
        weight_f = 1.0
    own = conn is None
    conn = conn or _connect()
    try:
        cur = conn.execute(
            "INSERT INTO causal_chains (name, cause, effect, trigger_words, note, mechanism,"
            " precondition, invalidation, enabled, origin, weight)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
            ((name or "").strip()[:60], cause[:500], effect[:500],
             (trigger_words or "").strip()[:300], (note or "").strip()[:300],
             (mechanism or "").strip()[:1000], (precondition or "").strip()[:500],
             (invalidation or "").strip()[:500], origin, weight_f),
        )
        conn.commit()
        new_id = int(cur.lastrowid)
        # 派生策略列 (chain_id/asset/trigger_keywords/expected_direction/...)
        _derive_strategy(conn, new_id)
        conn.commit()
        _invalidate_cache()
        return {"ok": True, "id": new_id}
    finally:
        if own:
            conn.close()


def update_chain(
    chain_id: int, cause: str, effect: str, *, name: str = "", trigger_words: str = "",
    note: str = "", mechanism: str = "", precondition: str = "",
    invalidation: str = "", weight: Optional[float] = None,
    origin: Optional[str] = None,  # v283+ · 来源透传(None=不改)
    conn: Optional[sqlite3.Connection] = None,
) -> Dict[str, Any]:
    """编辑因果链（weight 为 None 时不改权重；origin 为 None 时不改）。"""
    cause = (cause or "").strip()
    effect = (effect or "").strip()
    if not cause or not effect:
        raise ValueError("因(cause)与果(effect)都不能为空")
    weight_f: Optional[float] = None
    if weight is not None:
        try:
            weight_f = float(weight)
        except (TypeError, ValueError):
            weight_f = None
        if weight_f is not None and (weight_f < 0 or weight_f > 100):
            weight_f = None
    own = conn is None
    conn = conn or _connect()
    try:
        # v283+ · 校验 origin 白名单
        if origin is not None and origin not in (
            "manual", "manual_from_panel", "auto_real", "auto_quick_sim", "auto_mixed",
        ):
            origin = None
        if weight_f is not None and origin is not None:
            cur = conn.execute(
                "UPDATE causal_chains SET name=?, cause=?, effect=?, trigger_words=?,"
                " note=?, mechanism=?, precondition=?, invalidation=?,"
                " origin=?, weight=?, updated_at=datetime('now','localtime') WHERE id=?",
                ((name or "").strip()[:60], cause[:500], effect[:500],
                 (trigger_words or "").strip()[:300], (note or "").strip()[:300],
                 (mechanism or "").strip()[:1000], (precondition or "").strip()[:500],
                 (invalidation or "").strip()[:500], origin, weight_f, int(chain_id)),
            )
        elif weight_f is not None:
            cur = conn.execute(
                "UPDATE causal_chains SET name=?, cause=?, effect=?, trigger_words=?,"
                " note=?, mechanism=?, precondition=?, invalidation=?,"
                " weight=?, updated_at=datetime('now','localtime') WHERE id=?",
                ((name or "").strip()[:60], cause[:500], effect[:500],
                 (trigger_words or "").strip()[:300], (note or "").strip()[:300],
                 (mechanism or "").strip()[:1000], (precondition or "").strip()[:500],
                 (invalidation or "").strip()[:500], weight_f, int(chain_id)),
            )
        elif origin is not None:
            cur = conn.execute(
                "UPDATE causal_chains SET name=?, cause=?, effect=?, trigger_words=?,"
                " note=?, mechanism=?, precondition=?, invalidation=?,"
                " origin=?, updated_at=datetime('now','localtime') WHERE id=?",
                ((name or "").strip()[:60], cause[:500], effect[:500],
                 (trigger_words or "").strip()[:300], (note or "").strip()[:300],
                 (mechanism or "").strip()[:1000], (precondition or "").strip()[:500],
                 (invalidation or "").strip()[:500], origin, int(chain_id)),
            )
        else:
            cur = conn.execute(
                "UPDATE causal_chains SET name=?, cause=?, effect=?, trigger_words=?,"
                " note=?, mechanism=?, precondition=?, invalidation=?,"
                " updated_at=datetime('now','localtime') WHERE id=?",
                ((name or "").strip()[:60], cause[:500], effect[:500],
                 (trigger_words or "").strip()[:300], (note or "").strip()[:300],
                 (mechanism or "").strip()[:1000], (precondition or "").strip()[:500],
                 (invalidation or "").strip()[:500], int(chain_id)),
            )
        conn.commit()
        _invalidate_cache()
        if cur.rowcount == 0:
            return {"ok": False, "error": "因果链不存在"}
        return {"ok": True, "id": int(chain_id)}
    finally:
        if own:
            conn.close()


def set_weight(chain_id: int, weight: float, conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    """只更新权重（列表行内改权重用，不动其它字段）。"""
    try:
        weight_f = float(weight)
    except (TypeError, ValueError):
        return {"ok": False, "error": "权重必须是数字"}
    if weight_f < 0 or weight_f > 100:
        return {"ok": False, "error": "权重需在 0-100 之间"}
    own = conn is None
    conn = conn or _connect()
    try:
        cur = conn.execute(
            "UPDATE causal_chains SET weight=?, updated_at=datetime('now','localtime') WHERE id=?",
            (weight_f, int(chain_id)),
        )
        conn.commit()
        _invalidate_cache()
        if cur.rowcount == 0:
            return {"ok": False, "error": "因果链不存在"}
        return {"ok": True, "id": int(chain_id), "weight": weight_f}
    finally:
        if own:
            conn.close()


def delete_chain(chain_id: int, conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    """删除因果链（硬删除）。"""
    own = conn is None
    conn = conn or _connect()
    try:
        cur = conn.execute("DELETE FROM causal_chains WHERE id=?", (int(chain_id),))
        conn.commit()
        _invalidate_cache()
        if cur.rowcount == 0:
            return {"ok": False, "error": "因果链不存在"}
        return {"ok": True}
    finally:
        if own:
            conn.close()


def set_enabled(chain_id: int, enabled: int, conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    """v302+ 直接设置因果链启停状态(0/1)。

    与 toggle_chain 不同: 本函数是绝对值, 用于 reconcile worker 把
    kg_improvements 写回 causal_chains 的 enable_rule / disable_rule 提案。
    """
    if enabled not in (0, 1):
        return {"ok": False, "error": "enabled 必须是 0 或 1"}
    own = conn is None
    conn = conn or _connect()
    try:
        row = conn.execute(
            "SELECT id FROM causal_chains WHERE id=?", (int(chain_id),)
        ).fetchone()
        if not row:
            return {"ok": False, "error": "因果链不存在"}
        conn.execute(
            "UPDATE causal_chains SET enabled=?, updated_at=datetime('now','localtime')"
            " WHERE id=?",
            (int(enabled), int(chain_id)),
        )
        conn.commit()
        _invalidate_cache()
        return {"ok": True, "enabled": int(enabled)}
    finally:
        if own:
            conn.close()


def toggle_chain(chain_id: int, conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    """切换启停（1↔0）。"""
    own = conn is None
    conn = conn or _connect()
    try:
        row = conn.execute(
            "SELECT enabled FROM causal_chains WHERE id=?", (int(chain_id),)
        ).fetchone()
        if not row:
            return {"ok": False, "error": "因果链不存在"}
        next_enabled = 0 if int(row["enabled"]) else 1
        conn.execute(
            "UPDATE causal_chains SET enabled=?, updated_at=datetime('now','localtime')"
            " WHERE id=?",
            (next_enabled, int(chain_id)),
        )
        conn.commit()
        _invalidate_cache()
        return {"ok": True, "enabled": next_enabled}
    finally:
        if own:
            conn.close()


def bulk_toggle_chains(
    *,
    ids: Optional[List[int]] = None,
    scope: str = "",
    enabled: Optional[int] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> Dict[str, Any]:
    """v283+ · 批量启停(API 用)。

    三种调用模式:
      ❶ ids=[..] + enabled=None → 对每个 id 翻转
      ❷ ids=[..] + enabled=0/1 → 对每个 id 设值
      ❸ ids=[]  + scope='all_enabled/all_disabled/all' → 按 scope 操作全表

    enabled 必须 ∈ {None, 0, 1};scope 必须 ∈ {'', 'all_enabled', 'all_disabled', 'all'}。
    """
    if ids is None:
        ids = []
    if enabled is not None and enabled not in (0, 1):
        raise ValueError("enabled 必须是 0 / 1 / None")
    if scope and scope not in ("all_enabled", "all_disabled", "all"):
        raise ValueError("scope 必须是 all_enabled / all_disabled / all")
    if not ids and not scope:
        raise ValueError("ids 或 scope 至少传一个")

    own = conn is None
    conn = conn or _connect()
    try:
        # ── 构造 WHERE 子句 ──
        where_clauses: List[str] = []
        params: List[Any] = []
        if ids:
            placeholders = ",".join("?" * len(ids))
            where_clauses.append(f"id IN ({placeholders})")
            params.extend(int(i) for i in ids)
        if scope == "all_enabled":
            where_clauses.append("enabled=1")
        elif scope == "all_disabled":
            where_clauses.append("enabled=0")
        # 'all' 不加 enabled 条件

        where_sql = (" WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

        # ── 计算目标 enabled 值(逐行) ──
        if enabled is None:
            # 翻转:取出所有当前 enabled,逐行翻
            rows = conn.execute(
                f"SELECT id, enabled FROM causal_chains{where_sql}", params
            ).fetchall()
            if not rows:
                return {"ok": True, "affected": 0, "mode": "flip"}
            affected = 0
            for r in rows:
                cur_e = int(r["enabled"])
                new_e = 0 if cur_e else 1
                conn.execute(
                    "UPDATE causal_chains SET enabled=?,"
                    " updated_at=datetime('now','localtime') WHERE id=?",
                    (new_e, int(r["id"])),
                )
                affected += 1
            conn.commit()
            _invalidate_cache()
            return {"ok": True, "affected": affected, "mode": "flip"}
        else:
            # 设值:统一 UPDATE
            cur = conn.execute(
                f"UPDATE causal_chains SET enabled=?,"
                f" updated_at=datetime('now','localtime'){where_sql}",
                (int(enabled), *params),
            )
            conn.commit()
            _invalidate_cache()
            return {"ok": True, "affected": int(cur.rowcount), "mode": "set", "enabled": int(enabled)}
    finally:
        if own:
            conn.close()


# ---------------------------------------------------------------------------
# engine 注入：匹配命中（只读启用条目，TTL 缓存）
# ---------------------------------------------------------------------------

_enabled_cache: Dict[str, Any] = {"ts": 0.0, "items": []}


def _invalidate_cache() -> None:
    """写操作后清 enabled 缓存（add/update/set_weight/toggle/delete），立即生效。"""
    _enabled_cache.update({"ts": 0.0, "items": []})


def load_enabled_chains(ttl: float = 5.0) -> List[Dict[str, Any]]:
    """启用的全部因果链（5s TTL 缓存）。"""
    global _enabled_cache
    now = time.monotonic()
    if now - float(_enabled_cache.get("ts", 0.0)) < ttl:
        return list(_enabled_cache.get("items", []))
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT id, name, cause, effect, trigger_words, note, weight,"
            " mechanism, precondition, invalidation"
            " FROM causal_chains WHERE enabled=1 ORDER BY id"
        ).fetchall()
        items = [dict(r) for r in rows]
        _enabled_cache.update({"ts": now, "items": items})
        return items
    finally:
        conn.close()


def add_chains_dedup(chains: List[Dict[str, Any]], conn: Optional[sqlite3.Connection] = None,
                     origin: str = "auto_real") -> Dict[str, Any]:
    """批量添加因果链并去重（AI 复盘自动沉淀用）。

    去重规则：cause+effect 已存在（含停用）→ 跳过；无触发词的链跳过
    （因果链必须能匹配新闻才有注入意义）。返回 {added, skipped}。
    origin 默认 auto_real；每条链可自带 origin 覆盖（auto_real/auto_quick_sim/auto_mixed）。
    手工页面添加走 add_chain(origin='manual')。
    """
    own = conn is None
    conn = _ensure_row(conn)
    try:
        existing = set()
        for r in conn.execute("SELECT cause, effect FROM causal_chains").fetchall():
            existing.add((str(r["cause"] or "").strip(), str(r["effect"] or "").strip()))
        added = 0
        skipped = 0
        for ch in chains or []:
            cause = str(ch.get("cause") or "").strip()
            effect = str(ch.get("effect") or "").strip()
            if not cause or not effect:
                skipped += 1
                continue
            if (cause, effect) in existing:
                skipped += 1
                continue
            ch_origin = str(ch.get("origin") or origin)
            if ch_origin not in ("auto_real", "auto_quick_sim", "auto_mixed"):
                ch_origin = "auto_real"
            try:
                add_chain(
                    cause, effect,
                    name=str(ch.get("name") or ""), trigger_words=str(ch.get("trigger_words") or ""),
                    note=str(ch.get("note") or ""), origin=ch_origin, conn=conn,
                )
                # v189-7: per-chain commit, 避免 N 条因果链同一事务持锁 ~N×5ms.
                if own:
                    conn.commit()
                existing.add((cause, effect))
                added += 1
            except sqlite3.OperationalError as exc:
                # 单条 chain 入库失败 (lock) 不影响其他 chain.
                skipped += 1
                print(f"[causal_chains] skip chain {cause[:30]}|{effect[:30]}: {type(exc).__name__}: {str(exc)[:80]}")
                continue
        return {"added": added, "skipped": skipped}
    finally:
        if own:
            conn.close()


def match_chains(text: str, max_chains: int = 3, ttl: float = 5.0) -> List[Dict[str, Any]]:
    """按触发词匹配因果链：新闻文本命中某链的 trigger_words 任一 → 命中。

    无触发词的链（trigger_words 空）不自动匹配（避免全量注入刷屏）；
    命中超过 max_chains 条时按库序取前 N（防 prompt 过长）。
    权重不做排序截断——权重只作为注入时的标注强度（见 _build_causal_block），
    由 AI 按（权重 N）自行侧重高权重链。
    """
    if not text:
        return []
    low = (text or "").lower()
    matched: List[Dict[str, Any]] = []
    for chain in load_enabled_chains(ttl=ttl):
        triggers = str(chain.get("trigger_words") or "").strip()
        if not triggers:
            continue
        words = [w.strip().lower() for w in triggers.replace("，", ",").split(",") if w.strip()]
        if not words:
            continue
        if any(w in low for w in words):
            matched.append(chain)
            if len(matched) >= max_chains:
                break
    return matched


# ---------------------------------------------------------------------------
# 种子链（fresh deployment 初始化；幂等，与 scripts/seed_causal_chains.py 共用）
# ---------------------------------------------------------------------------

DEFAULT_SEEDS: List[Dict[str, str]] = []



def seed_defaults(conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    """幂等写入 DEFAULT_SEEDS（已存在同名+同因则跳过）。

    供 init_db.py 在 fresh deployment migrate 后自动调用；也可 CLI 手动执行。
    返回 {"added": int, "total": int}。
    """
    own = conn is None
    conn = _ensure_row(conn)
    try:
        existing = list_chains(conn)
        existing_names = {(str(e["name"]), str(e["cause"])) for e in existing}
        added = 0
        for s in DEFAULT_SEEDS:
            key = (s["name"], s["cause"])
            if key in existing_names:
                continue
            add_chain(
                s["cause"], s["effect"],
                name=s["name"], trigger_words=s["trigger_words"], note=s["note"],
                conn=conn,
            )
            added += 1
        if own:
            conn.commit()
        return {"added": added, "total": len(existing) + added}
    finally:
        if own:
            conn.close()
