#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Dictionary layer for event structuring (PR-1).

Provides CRUD for dim_asset and dim_event_type tables, idempotent
get_or_create_* by natural key (code), and direction_for() lookup.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, List, Optional

import db


def _connect() -> sqlite3.Connection:
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    return conn


# ────────────────────────────────────────────────────────────────────
# dim_asset
# ────────────────────────────────────────────────────────────────────

def get_or_create_asset(
    code: str,
    *,
    name: str = "",
    kind: str = "other",
    venue: str = "",
    connection: Optional[sqlite3.Connection] = None,
) -> int:
    """Idempotent upsert by code; returns asset id. Empty code -> -1."""
    code = (code or "").upper().strip()
    if not code:
        return -1
    if kind not in {"crypto", "commodity", "equity_index", "fx", "bond", "other"}:
        kind = "other"
    owned = connection is None
    connection = connection or _connect()
    try:
        connection.execute(
            "INSERT OR IGNORE INTO dim_asset(code, name, kind, venue) VALUES(?,?,?,?)",
            (code, name or "", kind, venue or ""),
        )
        row = connection.execute(
            "SELECT id FROM dim_asset WHERE code = ?", (code,)
        ).fetchone()
        if owned:
            connection.commit()
        return int(row["id"]) if row else -1
    finally:
        if owned:
            connection.close()


def get_asset_by_code(
    code: str,
    connection: Optional[sqlite3.Connection] = None,
) -> Optional[Dict[str, Any]]:
    """Lookup asset by code; returns dict or None."""
    code = (code or "").upper().strip()
    if not code:
        return None
    owned = connection is None
    connection = connection or _connect()
    try:
        row = connection.execute(
            "SELECT id, code, name, kind, venue, active FROM dim_asset WHERE code = ?",
            (code,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        if owned:
            connection.close()


def list_assets(
    active_only: bool = True,
    connection: Optional[sqlite3.Connection] = None,
) -> List[Dict[str, Any]]:
    owned = connection is None
    connection = connection or _connect()
    try:
        sql = "SELECT id, code, name, kind, venue, active FROM dim_asset"
        if active_only:
            sql += " WHERE active = 1"
        sql += " ORDER BY kind, code"
        return [dict(r) for r in connection.execute(sql).fetchall()]
    finally:
        if owned:
            connection.close()


# ────────────────────────────────────────────────────────────────────
# dim_event_type
# ────────────────────────────────────────────────────────────────────

_ALLOWED_CATEGORIES = {"macro", "commodity", "crypto", "geopolitics", "other"}


def get_or_create_event_type(
    code: str,
    *,
    name_zh: str = "",
    name_en: str = "",
    category: str = "other",
    direction_bias=None,
    connection: Optional[sqlite3.Connection] = None,
) -> int:
    """Idempotent upsert by code; returns event_type id. Empty code -> -1."""
    code = (code or "").lower().strip()
    if not code:
        return -1
    if category not in _ALLOWED_CATEGORIES:
        category = "other"
    if isinstance(direction_bias, dict):
        bias_json = json.dumps(direction_bias, ensure_ascii=False, sort_keys=True)
    elif isinstance(direction_bias, str):
        bias_json = direction_bias
    else:
        bias_json = "{}"

    owned = connection is None
    connection = connection or _connect()
    try:
        connection.execute(
            "INSERT OR IGNORE INTO dim_event_type"
            "(code, name_zh, name_en, category, direction_bias) VALUES(?,?,?,?,?)",
            (code, name_zh or "", name_en or "", category, bias_json),
        )
        if direction_bias is not None:
            connection.execute(
                "UPDATE dim_event_type SET direction_bias = ? WHERE code = ?",
                (bias_json, code),
            )
        row = connection.execute(
            "SELECT id FROM dim_event_type WHERE code = ?", (code,)
        ).fetchone()
        if owned:
            connection.commit()
        return int(row["id"]) if row else -1
    finally:
        if owned:
            connection.close()


def get_event_type_by_code(
    code: str,
    connection: Optional[sqlite3.Connection] = None,
) -> Optional[Dict[str, Any]]:
    """Lookup event type by code; direction_bias deserialized to dict."""
    code = (code or "").lower().strip()
    if not code:
        return None
    owned = connection is None
    connection = connection or _connect()
    try:
        row = connection.execute(
            "SELECT id, code, name_zh, name_en, category, direction_bias, active"
            " FROM dim_event_type WHERE code = ?",
            (code,),
        ).fetchone()
        if not row:
            return None
        item = dict(row)
        try:
            item["direction_bias"] = json.loads(item.get("direction_bias") or "{}")
        except (json.JSONDecodeError, TypeError):
            item["direction_bias"] = {}
        return item
    finally:
        if owned:
            connection.close()


def list_event_types(
    active_only: bool = True,
    connection: Optional[sqlite3.Connection] = None,
) -> List[Dict[str, Any]]:
    owned = connection is None
    connection = connection or _connect()
    try:
        sql = ("SELECT id, code, name_zh, name_en, category, direction_bias, active "
               "FROM dim_event_type")
        if active_only:
            sql += " WHERE active = 1"
        sql += " ORDER BY category, code"
        out: List[Dict[str, Any]] = []
        for r in connection.execute(sql).fetchall():
            item = dict(r)
            try:
                item["direction_bias"] = json.loads(item.get("direction_bias") or "{}")
            except (json.JSONDecodeError, TypeError):
                item["direction_bias"] = {}
            out.append(item)
        return out
    finally:
        if owned:
            connection.close()


def direction_for(
    event_type_code: str,
    asset_code: str,
    connection: Optional[sqlite3.Connection] = None,
) -> Optional[str]:
    """Look up direction bias for (event_type, asset); None if not declared."""
    et = get_event_type_by_code(event_type_code, connection=connection)
    if not et:
        return None
    bias = et.get("direction_bias") or {}
    if not isinstance(bias, dict):
        return None
    code_up = (asset_code or "").upper().strip()
    if not code_up:
        return None
    return bias.get(code_up)