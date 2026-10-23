#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""event_atom entity table helpers (PR-3).

Each raw_news can produce 0..N standardized events. This module
converts the output of event_struct.extract_events() into DB rows
in event_atom (one row per event) plus optional event_time_candidates.

Naming note:
  - existing `news_events` (in timeseries.py) = "AI decision signal stream"
  - this module's `event_atom` = "standardized objective event"
  - two tables have different semantics and are NOT merged.

Failure mode: every helper is best-effort, never raises to the caller.
If DB is in a bad state, we return -1 / [] and the caller can downgrade.
"""
from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import db
import dim_tables
import event_struct
import tz_utils  # v171+ 默认 display_tz


# ────────────────────────────────────────────────────────────────────
# Translation maps (event_struct emits Chinese)
# ────────────────────────────────────────────────────────────────────

EVENT_TYPE_TO_CODE: Dict[str, str] = {
    "加息": "rate_hike",
    "降息": "rate_cut",
    "CPI": "cpi",
    "非农": "nfp",
    "OPEC减产": "opec_cut",
    "OPEC增产": "opec_increase",
    "原油库存": "oil_inventory",
    "地缘冲突": "geopolitical_conflict",
    "地缘缓和": "geopolitical_easing",
    "央行购金": "central_bank_gold",
    "加密ETF": "crypto_etf",
    "美元指数": "dxy",
    "TIPS": "tips",
    # ── v181 P2 ──
    "技术突破": "technology_breakthrough",
    "芯片量产": "chip_capacity",
    "财报": "earnings_release",
    "并购重组": "merger_acquisition",
    "安全事件": "hack_attack",
    "监管政策": "regulation_policy",
    "IPO上市": "ipo_listing",
    "央行表态": "macro_speech",
    "自然灾害": "natural_disaster",
    "疫情": "pandemic_outbreak",
    "供应链中断": "supply_chain",
    "关税与制裁": "trade_tariff",
}

DIRECTION_CN_TO_DB: Dict[str, str] = {
    "利多": "bullish",
    "利空": "bearish",
    "中性": "neutral",
}
DIRECTION_DB_TO_CN: Dict[str, str] = {v: k for k, v in DIRECTION_CN_TO_DB.items()}
# English labels pass through unchanged
DIRECTION_DB_VALUES: Dict[str, str] = {
    "bullish": "bullish",
    "bearish": "bearish",
    "neutral": "neutral",
    "mixed": "mixed",
    "unknown": "unknown",
}

PARSER_VERSION = "event_struct.v1"


def _now() -> int:
    return int(time.time())


def _table_columns_event_atom(conn: sqlite3.Connection) -> set:
    """读 event_atom 实际列集合（兼容未迁移的旧库）。"""
    try:
        return db.table_columns(conn, "event_atom")
    except Exception:
        return set()


def _get_default_display_tz() -> str:
    """读取 TRIDENT_TZ，默认 Asia/Shanghai。"""
    try:
        return tz_utils.get_display_tz_name()
    except Exception:
        return "Asia/Shanghai"


def _connect() -> sqlite3.Connection:
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    return conn


def event_type_label_to_code(label: str) -> Optional[str]:
    """Map Chinese event label (e.g. '加息') to dim_event_type.code ('rate_hike')."""
    return EVENT_TYPE_TO_CODE.get(label)


# v194: 给 asset=NULL 的事件类型填默认资产（兜底）
# 背景：geopolitical_conflict 等类型 news 没明确说 XAU/WTI 时，
#       extract_events 返回 target_assets=[]，导致 direction_for 不被调用。
#       用「黄金是最常关联的避险资产」/「WTI 是原油首要标的」这类常识兜底。
# 优先级：news 明确说 asset (target_assets 非空) 优先；asset 为空时 fallback 到这里。
DEFAULT_ASSET_FOR_EVENT_TYPE: Dict[str, str] = {
    "geopolitical_conflict": "XAU",   # 地缘冲突 → 黄金避险
    "geopolitical_easing": "XAU",     # 地缘缓和 → 黄金避险缓解
    "opec_cut": "WTI",                # OPEC 减产 → 原油
    "opec_increase": "WTI",           # OPEC 增产 → 原油
    "central_bank_gold": "XAU",       # 央行购金 → 黄金
    "oil_inventory": "WTI",           # 原油库存 → 原油（兜底，规则不抢答方向但挂个资产便于检索）
    "natural_disaster": "WTI",        # 自然灾害 → 原油（基础设施影响）
    "trade_tariff": "XAU",            # 关税 → 黄金（避险）/ WTI（需求侧）
}


# v197: fallback 路径的 direction_confidence 调低到 0.4（vs news 明确 0.7）。
# 理由：news 没提具体资产、靠 event_type 兜底推断时，方向信号置信度应低于
#       「news 直接点名 XAU + 规则推断方向」的情形。下游 ai_worker 可读此值
#       在 BUY/SELL 决策时降权（避免把低置信方向当成高分信号使用）。
FALLBACK_DIR_CONFIDENCE: float = 0.4


def direction_cn_to_db(label: str) -> str:
    """Normalize a direction label (Chinese OR English) to a DB enum value.

    Accepts Chinese labels (利多/利空/中性) and English enum labels
    (bullish/bearish/neutral/mixed/unknown). Anything else -> 'unknown'.
    """
    if not label:
        return "unknown"
    if label in DIRECTION_DB_VALUES:
        return DIRECTION_DB_VALUES[label]
    return DIRECTION_CN_TO_DB.get(label, "unknown")


# ────────────────────────────────────────────────────────────────────
# Single-event insert
# ────────────────────────────────────────────────────────────────────

def insert_event_atom(
    *,
    raw_news_id: int,
    event_type_code: str,
    event_time: int,
    asset_code: Optional[str] = None,
    event_time_confidence: float = 1.0,
    event_time_source: str = "platform_published_at",
    magnitude_value: Optional[float] = None,
    magnitude_unit: str = "",
    actor: str = "",
    direction: str = "unknown",
    direction_confidence: float = 0.0,
    direction_source: str = "rule_bias",
    evidence_count: int = 1,
    cluster_id: Optional[int] = None,
    reasoning: str = "",
    parser_version: str = PARSER_VERSION,
    # ── v171+：时间结构化语义字段 ────────────────────────────
    curve_snapshot: str = "",
    event_time_utc_epoch: Optional[int] = None,
    event_time_utc_iso: str = "",
    event_time_display_tz: str = "",
    fallback_used: int = 0,
    connection: Optional[sqlite3.Connection] = None,
) -> int:
    """Insert one event_atom row. Returns new id (or existing id if unique conflict).

    Returns -1 on failure (caller should downgrade; never raises).
    """
    if not raw_news_id or not event_type_code or not event_time:
        return -1
    owned = connection is None
    connection = connection or _connect()
    try:
        et_id = dim_tables.get_or_create_event_type(
            event_type_code, connection=connection,
        )
        if et_id <= 0:
            return -1
        asset_id = 0
        if asset_code:
            asset_id = dim_tables.get_or_create_asset(
                asset_code, connection=connection,
            )
            if asset_id <= 0:
                asset_id = 0

        if direction in DIRECTION_CN_TO_DB:
            direction = DIRECTION_CN_TO_DB[direction]

        # Try INSERT; on UNIQUE conflict, fetch the existing row id.
        # SQLite's cur.rowcount is unreliable for INSERT OR IGNORE so we
        # always end with a SELECT id by the natural unique key.
        # ── v171+：自动派生 utc_* 三列（缺省回退）+ 接受调用方传入 ────
        cols = _table_columns_event_atom(connection)
        utc_epoch = (
            int(event_time_utc_epoch) if event_time_utc_epoch
            else int(event_time)
        )
        utc_iso = (
            str(event_time_utc_iso) if event_time_utc_iso
            else datetime.fromtimestamp(int(event_time), tz=timezone.utc)
                .strftime("%Y-%m-%dT%H:%M:%SZ")
        )
        display_tz = (
            str(event_time_display_tz) if event_time_display_tz
            else _get_default_display_tz()
        )

        # 基础列（必须有）
        base_cols = [
            "raw_news_id", "event_type_id", "asset_id", "event_time",
            "event_time_confidence", "event_time_source",
            "magnitude_value", "magnitude_unit", "actor",
            "direction", "direction_confidence", "direction_source",
            "evidence_count", "parser_version", "cluster_id",
            "reasoning", "created_at",
        ]
        base_vals = [
            int(raw_news_id), et_id, asset_id, int(event_time),
            float(event_time_confidence), str(event_time_source),
            magnitude_value, str(magnitude_unit or ""), str(actor or ""),
            str(direction), float(direction_confidence), str(direction_source),
            int(evidence_count), str(parser_version), cluster_id,
            str(reasoning or "")[:120], _now(),
        ]
        # 增量列（按列存在情况追加）
        extra_cols = []
        extra_vals = []
        if "curve_snapshot" in cols:
            extra_cols.append("curve_snapshot"); extra_vals.append(str(curve_snapshot or ""))
        if "event_time_utc_epoch" in cols:
            extra_cols.append("event_time_utc_epoch"); extra_vals.append(utc_epoch)
        if "event_time_utc_iso" in cols:
            extra_cols.append("event_time_utc_iso"); extra_vals.append(utc_iso)
        if "event_time_display_tz" in cols:
            extra_cols.append("event_time_display_tz"); extra_vals.append(display_tz)
        if "fallback_used" in cols:
            extra_cols.append("fallback_used"); extra_vals.append(int(fallback_used))

        all_cols = base_cols + extra_cols
        all_vals = base_vals + extra_vals
        placeholders = ",".join(["?"] * len(all_cols))
        connection.execute(
            f"""INSERT OR IGNORE INTO event_atom({",".join(all_cols)})
                VALUES({placeholders})""",
            tuple(all_vals),
        )
        if owned:
            connection.commit()
        row = connection.execute(
            """SELECT id FROM event_atom
               WHERE raw_news_id=? AND event_type_id=? AND event_time=? AND asset_id=?""",
            (int(raw_news_id), et_id, int(event_time), int(asset_id)),
        ).fetchone()
        return int(row["id"]) if row else -1
    except Exception:
        return -1
    finally:
        if owned:
            connection.close()


def insert_event_time_candidate(
    *,
    raw_news_id: int,
    candidate_ts: int,
    tz: str = "Asia/Shanghai",
    source: str = "platform_published_at",
    confidence: float = 1.0,
    reasoning: str = "",
    is_primary: bool = False,
    parser_version: str = PARSER_VERSION,
    connection: Optional[sqlite3.Connection] = None,
) -> int:
    """Insert one event_time_candidates row. Returns new id, -1 on failure."""
    if not raw_news_id or not candidate_ts:
        return -1
    owned = connection is None
    connection = connection or _connect()
    try:
        cur = connection.execute(
            """INSERT INTO event_time_candidates(
                raw_news_id, candidate_ts, tz, source, confidence,
                reasoning, is_primary, parser_version, created_at
            ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                int(raw_news_id), int(candidate_ts), str(tz), str(source),
                float(confidence), str(reasoning or "")[:200],
                1 if is_primary else 0, str(parser_version), _now(),
            ),
        )
        if owned:
            connection.commit()
        return int(cur.lastrowid) if cur.lastrowid else -1
    except Exception:
        return -1
    finally:
        if owned:
            connection.close()


# ────────────────────────────────────────────────────────────────────
# Read helpers
# ────────────────────────────────────────────────────────────────────

def get_event_atoms_by_news(
    raw_news_id: int,
    connection: Optional[sqlite3.Connection] = None,
) -> List[Dict[str, Any]]:
    """Return all event_atom rows for a news id, joined with dim names."""
    owned = connection is None
    connection = connection or _connect()
    try:
        rows = connection.execute(
            """SELECT ea.id, ea.raw_news_id, ea.event_type_id,
                      det.code AS event_type_code, det.name_zh AS event_type_zh,
                      det.category AS event_category,
                      ea.asset_id, da.code AS asset_code,
                      ea.event_time, ea.event_time_confidence, ea.event_time_source,
                      ea.magnitude_value, ea.magnitude_unit, ea.actor,
                      ea.direction, ea.direction_confidence, ea.direction_source,
                      ea.evidence_count, ea.parser_version, ea.cluster_id,
                      ea.reasoning, ea.created_at
               FROM event_atom ea
               JOIN dim_event_type det ON det.id = ea.event_type_id
               LEFT JOIN dim_asset da ON da.id = ea.asset_id
               WHERE ea.raw_news_id = ?
               ORDER BY ea.event_time, ea.id""",
            (int(raw_news_id),),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        if owned:
            connection.close()


def get_event_time_candidates(
    raw_news_id: int,
    connection: Optional[sqlite3.Connection] = None,
) -> List[Dict[str, Any]]:
    """Return all event_time_candidates rows for a news id."""
    owned = connection is None
    connection = connection or _connect()
    try:
        rows = connection.execute(
            """SELECT id, candidate_ts, tz, source, confidence,
                      reasoning, is_primary, parser_version, created_at
               FROM event_time_candidates
               WHERE raw_news_id = ?
               ORDER BY is_primary DESC, confidence DESC, id""",
            (int(raw_news_id),),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        if owned:
            connection.close()


def count_events_by_type(
    event_type_code: Optional[str] = None,
    asset_code: Optional[str] = None,
    since_ts: Optional[int] = None,
    connection: Optional[sqlite3.Connection] = None,
) -> Dict[str, int]:
    """Group event_atom counts by event_type_code (and optionally asset)."""
    owned = connection is None
    connection = connection or _connect()
    try:
        sql = """SELECT det.code AS code, COUNT(*) AS cnt
                  FROM event_atom ea
                  JOIN dim_event_type det ON det.id = ea.event_type_id
                  LEFT JOIN dim_asset da ON da.id = ea.asset_id
                  WHERE 1=1"""
        params: List[Any] = []
        if event_type_code:
            sql += " AND det.code = ?"
            params.append(event_type_code.lower())
        if asset_code:
            sql += " AND da.code = ?"
            params.append(asset_code.upper())
        if since_ts is not None:
            sql += " AND ea.event_time >= ?"
            params.append(int(since_ts))
        sql += " GROUP BY det.code ORDER BY cnt DESC"
        return {r["code"]: int(r["cnt"]) for r in connection.execute(sql, params).fetchall()}
    finally:
        if owned:
            connection.close()


# ────────────────────────────────────────────────────────────────────
# Batch conversion: text -> event_atom rows
# ────────────────────────────────────────────────────────────────────

def extract_and_persist(
    text: str,
    *,
    raw_news_id: int,
    default_event_time: int,
    event_time_source: str = "platform_published_at",
    event_time_confidence: float = 0.8,
    direction_confidence: float = 0.7,
    # ── v171+：LLM 输出的时间结构化字段（ai_worker 调用时传入） ──
    impact_curve: Optional[Dict[str, Any]] = None,
    event_window_minutes: Optional[int] = None,
    data_anchor_ts: str = "",
    fallback_used: int = 0,
    connection: Optional[sqlite3.Connection] = None,
) -> List[int]:
    """Take news text, run event_struct.extract_events, persist all atoms.

    Returns list of new event_atom ids (may be empty). Best-effort:
    individual row failures don't abort the rest.

    v171+：若 impact_curve 非空，每个新插入的 atom 自动写 event_impact_curve 表
          （用 build_minimal_curve 的逆序 → 实际曲线由 ai_worker 真实行情采样覆盖）。
    """
    if not text or not raw_news_id or not default_event_time:
        return []
    owned = connection is None
    connection = connection or _connect()
    ids: List[int] = []
    try:
        atoms = event_struct.extract_events(text)
        # Record the primary event_time candidate (single source for now).
        # Future: extract multi-source candidates here.
        insert_event_time_candidate(
            raw_news_id=raw_news_id,
            candidate_ts=int(default_event_time),
            source=event_time_source,
            confidence=float(event_time_confidence),
            is_primary=True,
            connection=connection,
        )

        # 把 LLM 给的 impact_curve 序列化为 snapshot（写入 event_atom.curve_snapshot）
        curve_snapshot_blob = ""
        if impact_curve:
            try:
                import event_impact_curve as _eic
                curve_snapshot_blob = _eic.serialize_curve(impact_curve)
            except Exception:
                curve_snapshot_blob = ""

        for atom in atoms:
            label = atom.get("event_type") or ""
            code = event_type_label_to_code(label)
            if not code:
                continue
            target_assets = atom.get("target_assets") or []
            asset_code = target_assets[0] if target_assets else None
            # v194: target_assets 为空时尝试按 event_type 填默认资产（geopol → XAU 等）
            asset_source = "rule_bias"
            if not asset_code and code in DEFAULT_ASSET_FOR_EVENT_TYPE:
                asset_code = DEFAULT_ASSET_FOR_EVENT_TYPE[code]
                asset_source = "rule_bias_default_asset"
            direction = "unknown"
            d_conf = 0.0
            if asset_code:
                bias_cn = dim_tables.direction_for(code, asset_code, connection=connection)
                if bias_cn:
                    direction = direction_cn_to_db(bias_cn)
                    # v197: 区分 news 明确 (0.7) vs fallback (0.4)
                    # fallback = 资产是按 event_type 兜底推断而非 news 显式提到
                    if asset_source == "rule_bias_default_asset":
                        d_conf = FALLBACK_DIR_CONFIDENCE  # 0.4
                    else:
                        d_conf = float(direction_confidence)  # 0.7
            actor = atom.get("actor") or ""
            magnitude_value = atom.get("magnitude_value")
            magnitude_unit = atom.get("magnitude_unit") or ""
            reasoning_text = atom.get("raw") or ""

            new_id = insert_event_atom(
                raw_news_id=int(raw_news_id),
                event_type_code=code,
                event_time=int(default_event_time),
                asset_code=asset_code,
                event_time_confidence=float(event_time_confidence),
                event_time_source=str(event_time_source),
                magnitude_value=magnitude_value,
                magnitude_unit=str(magnitude_unit),
                actor=str(actor),
                direction=direction,
                direction_confidence=d_conf,
                direction_source=asset_source,
                reasoning=reasoning_text,
                curve_snapshot=curve_snapshot_blob,
                fallback_used=int(fallback_used),
                connection=connection,
            )
            if new_id > 0:
                ids.append(new_id)
                # v171+：若 LLM 给的 impact_curve 有值，写入 event_impact_curve 表
                if impact_curve and asset_code:
                    try:
                        import event_impact_curve as _eic
                        # 默认 sample_ts 用事件时间 + 每个窗口的偏移
                        sample_base = int(default_event_time)
                        points = []
                        for wl in _eic.WINDOW_LABELS[:4]:  # 5m/30m/2h/1d
                            v = impact_curve.get(wl)
                            if v is None or not isinstance(v, dict):
                                # 容许简写：{'5m': 0.5} 而非 {'5m': {'delta_pct': 0.5}}
                                if isinstance(v, (int, float)):
                                    v = {"delta_pct": float(v)}
                            if isinstance(v, dict):
                                off = _eic.WINDOW_MINUTES.get(wl, 0)
                                points.append({
                                    "window_label": wl,
                                    "sample_ts": sample_base + off * 60,
                                    "delta_pct": v.get("delta_pct"),
                                    "volume_ratio": v.get("volume_ratio"),
                                    "price": v.get("price"),
                                    "note": "llm_predicted",
                                })
                        if points:
                            _eic.record_impact_curve(
                                event_atom_id=int(new_id),
                                asset_code=asset_code,
                                points=points,
                                connection=connection,
                            )
                    except Exception:
                        pass
        if owned:
            connection.commit()
        return ids
    except Exception:
        if owned:
            try:
                connection.rollback()
            except Exception:
                pass
        return ids
    finally:
        if owned:
            connection.close()