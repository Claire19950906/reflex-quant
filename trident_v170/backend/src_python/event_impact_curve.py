# -*- coding: utf-8 -*-
"""事件影响曲线 — 把事件发布后 5m / 30m / 2h / 1d 价格分桶结构化存储。

设计目的：
- 当前 `elasticity_window.minutes` 仅是聚合参数（影响持续分钟数）。
- 缺事件的"价格-时间"曲线 → 复盘 / LLM 归因 / 异常检测想做"5m vs 30m vs 2h 分桶"必须自己 join 行情。
- 本模块提供：
  1. `event_impact_curve` 表（标准化曲线点：window_label × asset × 时间）
  2. record_impact_point()  /  record_impact_curve() 写入器
  3. aggregate_impact_curve() 聚合 helper（缺数据时返回空 dict）

约束：
- 仅依赖 stdlib + 已有的 db / dim_tables / event_atom
- 表 schema 写在 db.py（唯一权威）；本文件只放 helper + 操作函数
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Dict, List, Optional

import db
import dim_tables


# ────────────────────────────────────────────────────────────────────────────
# 标准窗口（5m / 30m / 2h / 1d / extended）— 与 tz_utils.WINDOW_LABELS 对齐
# ────────────────────────────────────────────────────────────────────────────
WINDOW_LABELS = ("5m", "30m", "2h", "1d", "extended")

# 每个窗口的参考分钟数（用于 price_at_event / 后续插值）
WINDOW_MINUTES = {
    "5m":       5,
    "30m":     30,
    "2h":     120,
    "1d":    1440,
    "extended": 4320,   # 3 天兜底
}


def _now() -> int:
    try:
        from clock import safe_now
        return safe_now()
    except Exception:
        return int(time.time())


def _connect() -> sqlite3.Connection:
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    return conn


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """获取表实际列集合（兼容未迁移的旧库）。"""
    try:
        return db.table_columns(conn, table)
    except Exception:
        return set()


def record_impact_point(
    *,
    event_atom_id: int,
    asset_code: str,
    window_label: str,
    sample_ts: int,
    price: Optional[float] = None,
    delta_pct: Optional[float] = None,
    volume_ratio: Optional[float] = None,
    note: str = "",
    connection: Optional[sqlite3.Connection] = None,
) -> int:
    """记一个曲线点（事件 × 资产 × 时间窗口）。返回新 id，失败 -1。

    Args:
        event_atom_id: 关联 event_atom.id（必填，否则 -1）
        asset_code:    'XAU' / 'BTC' / 'WTI' 等
        window_label:  '5m' / '30m' / '2h' / '1d' / 'extended'
        sample_ts:     采样时刻 (epoch sec)
        price:         该时刻价格
        delta_pct:     距事件发布时刻的价格变化百分比
        volume_ratio:  量能比（>1 放量, ≈1 存量）
        note:          备注
        connection:    可选，复用外部 conn
    """
    if not event_atom_id or not asset_code or window_label not in WINDOW_LABELS:
        return -1

    owned = connection is None
    connection = connection or _connect()
    cols = _table_columns(connection, "event_impact_curve")
    if not cols:
        return -1

    asset_id = dim_tables.get_or_create_asset(asset_code, connection=connection) or 0
    try:
        cur = connection.execute(
            f"""INSERT INTO event_impact_curve
                (event_atom_id, asset_id, window_label, sample_ts,
                 price, delta_pct, volume_ratio, note, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                int(event_atom_id),
                int(asset_id),
                str(window_label),
                int(sample_ts),
                float(price) if price is not None else None,
                float(delta_pct) if delta_pct is not None else None,
                float(volume_ratio) if volume_ratio is not None else None,
                str(note or "")[:200],
                _now(),
            ),
        )
        if owned:
            connection.commit()
        return int(cur.lastrowid or 0)
    except Exception:
        return -1
    finally:
        if owned:
            try:
                connection.close()
            except Exception:
                pass


def record_impact_curve(
    *,
    event_atom_id: int,
    asset_code: str,
    points: List[Dict[str, Any]],
    connection: Optional[sqlite3.Connection] = None,
) -> List[int]:
    """批量记曲线点。points 元素 keys: window_label / sample_ts / price / delta_pct / volume_ratio / note。

    Returns:
        插入成功的 id 列表
    """
    ids: List[int] = []
    for p in points or []:
        new_id = record_impact_point(
            event_atom_id=event_atom_id,
            asset_code=asset_code,
            window_label=str(p.get("window_label") or ""),
            sample_ts=int(p.get("sample_ts") or 0),
            price=p.get("price"),
            delta_pct=p.get("delta_pct"),
            volume_ratio=p.get("volume_ratio"),
            note=str(p.get("note") or ""),
            connection=connection,
        )
        if new_id > 0:
            ids.append(new_id)
    return ids


def get_impact_curve(
    *,
    event_atom_id: int,
    asset_code: Optional[str] = None,
    connection: Optional[sqlite3.Connection] = None,
) -> List[Dict[str, Any]]:
    """取一个事件的完整影响曲线（按窗口排序）。"""
    owned = connection is None
    connection = connection or _connect()
    cols = _table_columns(connection, "event_impact_curve")
    if not cols:
        return []
    sql = (
        "SELECT eic.*, a.code AS asset_code "
        "FROM event_impact_curve eic "
        "LEFT JOIN dim_asset a ON a.id = eic.asset_id "
        "WHERE eic.event_atom_id = ? "
    )
    params: List[object] = [int(event_atom_id)]
    if asset_code:
        sql += "AND a.code = ? "
        params.append(str(asset_code))
    sql += "ORDER BY CASE eic.window_label "
    sql += " WHEN '5m' THEN 1 WHEN '30m' THEN 2 WHEN '2h' THEN 3 "
    sql += " WHEN '1d' THEN 4 ELSE 5 END, eic.sample_ts"
    try:
        rows = connection.execute(sql, params).fetchall()
    except Exception:
        return []
    finally:
        if owned:
            try:
                connection.close()
            except Exception:
                pass
    return [dict(r) for r in rows]


def aggregate_impact_curve(
    *,
    event_atom_id: int,
    asset_code: Optional[str] = None,
    connection: Optional[sqlite3.Connection] = None,
) -> Dict[str, Dict[str, Any]]:
    """按窗口 label 聚合曲线点（多个 sample 时取首个 avg）。

    Returns:
        {
          '5m':   {'price': ..., 'delta_pct': ..., 'sample_ts': ..., 'volume_ratio': ...},
          ...
        }
        缺窗口 → 键缺失
    """
    out: Dict[str, Dict[str, Any]] = {}
    for row in get_impact_curve(
        event_atom_id=event_atom_id, asset_code=asset_code, connection=connection
    ):
        wl = str(row.get("window_label") or "")
        if wl in out:
            continue  # 取首个 sample
        out[wl] = {
            "asset_code":   str(row.get("asset_code") or asset_code or ""),
            "price":        row.get("price"),
            "delta_pct":    row.get("delta_pct"),
            "volume_ratio": row.get("volume_ratio"),
            "sample_ts":    int(row.get("sample_ts") or 0),
            "note":         str(row.get("note") or ""),
        }
    return out


def serialize_curve(curve: Dict[str, Dict[str, Any]]) -> str:
    """把聚合结果序列化为 JSON 字符串（用于 event_atom.curve_snapshot 列）。"""
    try:
        return json.dumps(curve, ensure_ascii=False, default=str)
    except Exception:
        return ""


def deserialize_curve(blob: Optional[str]) -> Dict[str, Dict[str, Any]]:
    """从 event_atom.curve_snapshot 反序列化。失败 / 空 → {}。"""
    if not blob:
        return {}
    try:
        v = json.loads(blob)
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


# ────────────────────────────────────────────────────────────────────────────
# build_minimal_curve — 从现有 elasticity_window + 已知初始价 估算曲线
# ────────────────────────────────────────────────────────────────────────────

def build_minimal_curve(
    *,
    elasticity_minutes: Optional[int],
    price_at_event: Optional[float],
    delta_pct_at_peak: Optional[float],
    peak_after_minutes: Optional[int],
    asset_code: str = "",
    sample_now: Optional[int] = None,
) -> Dict[str, Dict[str, Any]]:
    """从 elasticity_window 各字段估算一条最小曲线（无具体行情数据）。"""
    if not elasticity_minutes or elasticity_minutes <= 0:
        return {}
    d_pct = float(delta_pct_at_peak or 0.0)

    def _est(ratio: float) -> Optional[float]:
        return round(d_pct * ratio, 4) if d_pct else None

    curve = {
        "5m":  {"asset_code": asset_code, "delta_pct": _est(1.0)},
        "30m": {"asset_code": asset_code, "delta_pct": _est(0.85)},
        "2h":  {"asset_code": asset_code, "delta_pct": _est(0.55)},
        "1d":  {"asset_code": asset_code, "delta_pct": _est(0.25)},
    }
    sample_ts = int(sample_now) if sample_now else _now()
    for v in curve.values():
        v["sample_ts"] = sample_ts
        v["note"] = "estimated_from_elasticity_window"
    return curve


# ────────────────────────────────────────────────────────────────────────────
# ────────────────────────────────────────────────────────────────────────────
# v171+ 监控：每日 stat 聚合（写入 event_impact_stat 表）
# ────────────────────────────────────────────────────────────────────────────

def update_daily_stat(
    *,
    date_local: str = "",
    clock_jumps_delta: int = 0,
    connection: Optional[sqlite3.Connection] = None,
) -> int:
    """按 date_local (YYYY-MM-DD) 聚合当日统计写入 event_impact_stat。

    统计项：
      - impact_curve_filled: event_atom.curve_snapshot 非空行数
      - event_atoms_total: event_atom 当日新插入行数
      - fallback_used: event_atom.fallback_used=1 行数
      - avg_window_minutes: 当日 event_window_minutes 平均（仅统计在 ai_decisions JSON 里）
      - clock_jumps_delta: 本次调用传入的 clock_jump 增量（叠加）

    Returns:
        stat 行 id（UPSERT 后），失败 -1。
    """
    from datetime import datetime, timezone as _tz

    if not date_local:
        date_local = datetime.fromtimestamp(
            int(_now()), tz=_tz.utc
        ).strftime("%Y-%m-%d")

    # 计算该日 UTC 00:00 epoch
    try:
        date_epoch_start = int(
            datetime.fromisoformat(date_local).replace(tzinfo=_tz.utc).timestamp()
        )
    except Exception:
        return -1

    owned = connection is None
    connection = connection or _connect()

    stats = {
        "impact_curve_filled": 0,
        "event_atoms_total": 0,
        "fallback_used": 0,
        "avg_window_minutes": 0.0,
    }
    try:
        end_epoch = date_epoch_start + 86400
        cols = _table_columns(connection, "event_atom")
        if cols:
            row = connection.execute(
                """SELECT COUNT(*) AS c FROM event_atom
                   WHERE created_at >= ? AND created_at < ?""",
                (date_epoch_start, end_epoch),
            ).fetchone()
            stats["event_atoms_total"] = int(row["c"]) if row else 0
            if "curve_snapshot" in cols:
                row = connection.execute(
                    """SELECT COUNT(*) AS c FROM event_atom
                       WHERE created_at >= ? AND created_at < ?
                         AND curve_snapshot != ''""",
                    (date_epoch_start, end_epoch),
                ).fetchone()
                stats["impact_curve_filled"] = int(row["c"]) if row else 0
            if "fallback_used" in cols:
                row = connection.execute(
                    """SELECT COUNT(*) AS c FROM event_atom
                       WHERE created_at >= ? AND created_at < ?
                         AND fallback_used = 1""",
                    (date_epoch_start, end_epoch),
                ).fetchone()
                stats["fallback_used"] = int(row["c"]) if row else 0
        try:
            # v171+：ai_decisions 表是归一化列，event_window_minutes 暂未独立成列；
            # 此处先跳过 avg_window_minutes 聚合（保持 0.0），待 P5 工具层加列后再启用。
            stats["avg_window_minutes"] = 0.0
        except Exception:
            stats["avg_window_minutes"] = 0.0
    except Exception:
        if owned:
            try:
                connection.rollback()
            except Exception:
                pass
        return -1

    try:
        cols_stat = _table_columns(connection, "event_impact_stat")
        if not cols_stat:
            return -1
        row_existing = connection.execute(
            "SELECT clock_jumps FROM event_impact_stat WHERE date_local=?",
            (date_local,),
        ).fetchone()
        prev_clock_jumps = int(row_existing["clock_jumps"]) if row_existing else 0
        new_clock_jumps = prev_clock_jumps + int(clock_jumps_delta or 0)
        connection.execute(
            """INSERT INTO event_impact_stat(
                date_local, date_epoch_start,
                impact_curve_filled, event_atoms_total, fallback_used,
                avg_window_minutes, clock_jumps, created_at)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(date_local) DO UPDATE SET
                    date_epoch_start=excluded.date_epoch_start,
                    impact_curve_filled=excluded.impact_curve_filled,
                    event_atoms_total=excluded.event_atoms_total,
                    fallback_used=excluded.fallback_used,
                    avg_window_minutes=excluded.avg_window_minutes,
                    clock_jumps=excluded.clock_jumps,
                    created_at=excluded.created_at""",
            (
                date_local, date_epoch_start,
                int(stats["impact_curve_filled"]),
                int(stats["event_atoms_total"]),
                int(stats["fallback_used"]),
                float(stats["avg_window_minutes"]),
                int(new_clock_jumps),
                int(_now()),
            ),
        )
        if owned:
            connection.commit()
        row_id = connection.execute(
            "SELECT id FROM event_impact_stat WHERE date_local=?",
            (date_local,),
        ).fetchone()
        return int(row_id["id"]) if row_id else -1
    except Exception:
        if owned:
            try:
                connection.rollback()
            except Exception:
                pass
        return -1
    finally:
        if owned:
            try:
                connection.close()
            except Exception:
                pass


# ────────────────────────────────────────────────────────────────────────────
# Self-test
# ────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=== build_minimal_curve ===")
    c = build_minimal_curve(
        elasticity_minutes=120,
        price_at_event=2400.0,
        delta_pct_at_peak=0.85,
        peak_after_minutes=15,
    asset_code="XAU",
    )
    for k, v in c.items():
        print(f"  {k:10s} → {v}")

    print("\n=== serialize / deserialize roundtrip ===")
    s = serialize_curve(c)
    d = deserialize_curve(s)
    print(f"  roundtrip keys: {list(d.keys())}")

    print("\n=== WINDOW_MINUTES ===")
    for k, m in WINDOW_MINUTES.items():
        print(f"  {k:10s} = {m} min")


def build_sigmoid_curve(
    *,
    elasticity_minutes: Optional[int],
    price_at_event: Optional[float],
    delta_pct_at_peak: Optional[float],
    peak_after_minutes: Optional[int],
    asset_code: str = "",
    sample_now: Optional[int] = None,
    k_steepness: float = 0.02,
) -> Dict[str, Dict[str, Any]]:
    """Sigmoid-like 平滑衰减曲线（比线性更接近实际价格冲击形态）。

    模型：
      delta(t) = delta_peak / (1 + exp(k * (t - peak_minutes)))
      - t < peak：陡升段（事件刚爆发）
      - t = peak：峰值
      - t > peak：缓慢回归到 ~0
      - 当 elasticity_minutes 缺失或 ≤0 → 返回 {}
    """
    if not elasticity_minutes or elasticity_minutes <= 0:
        return {}
    d_pct = float(delta_pct_at_peak or 0.0)
    peak = int(peak_after_minutes or 0)

    def _sigmoid(minutes: int) -> Optional[float]:
        if not d_pct:
            return None
        # sigmoid 平滑：1 / (1 + exp(k * (t - peak)))
        try:
            ratio = 1.0 / (1.0 + __import__("math").exp(k_steepness * (minutes - peak)))
        except Exception:
            return None
        return round(d_pct * ratio, 4)

    sample_ts = int(sample_now) if sample_now else _now()
    curve = {}
    for wl, minutes in WINDOW_MINUTES.items():
        if wl == "extended":
            continue  # sigmoid 不算 extended（避免与 fallback 重复）
        delta = _sigmoid(minutes)
        curve[wl] = {
            "asset_code": asset_code,
            "delta_pct": delta,
            "sample_ts": sample_ts,
            "note": "estimated_from_sigmoid",
        }
    return curve


# ────────────────────────────────────────────────────────────────────────────
# v171+ P6：aggregate_by_asset — 多资产聚合（跨 atom_id 的 4 窗口平均）
# ────────────────────────────────────────────────────────────────────────────


def aggregate_by_asset(
    asset_code: str,
    *,
    days: int = 7,
    connection: Optional[sqlite3.Connection] = None,
) -> Dict[str, Dict[str, Any]]:
    """聚合最近 N 天某资产的曲线（4 窗口的平均 delta_pct / volume_ratio）。

    Returns:
        {'5m': {'avg_delta_pct': ..., 'avg_volume_ratio': ..., 'sample_count': ...}, ...}
    """
    owned = connection is None
    connection = connection or _connect()
    try:
        import time as _t
        from datetime import datetime, timezone as _tz
        now_epoch = _t.time()
        cutoff = now_epoch - days * 86400
        rows = connection.execute(
            """SELECT eic.window_label, eic.delta_pct, eic.volume_ratio
                 FROM event_impact_curve eic
                 JOIN dim_asset da ON da.id = eic.asset_id
                WHERE da.code = ? AND eic.sample_ts >= ?""",
            (asset_code, cutoff),
        ).fetchall()
        out: Dict[str, Dict[str, Any]] = {}
        for wl in WINDOW_LABELS[:4]:
            relevant = [r for r in rows if r["window_label"] == wl]
            deltas = [float(r["delta_pct"]) for r in relevant
                      if r["delta_pct"] is not None]
            vols = [float(r["volume_ratio"]) for r in relevant
                    if r["volume_ratio"] is not None]
            out[wl] = {
                "avg_delta_pct": round(sum(deltas) / len(deltas), 4) if deltas else None,
                "avg_volume_ratio": round(sum(vols) / len(vols), 4) if vols else None,
                "sample_count": len(relevant),
            }
        return out
    except Exception:
        return {}
    finally:
        if owned:
            try:
                connection.close()
            except Exception:
                pass


# ────────────────────────────────────────────────────────────────────────────
# v171+ P6：archive_old_curves — 老数据自动归档（sample_ts > N 天 → soft-delete）
# ────────────────────────────────────────────────────────────────────────────


def archive_old_curves(
    *,
    days: int = 30,
    connection: Optional[sqlite3.Connection] = None,
) -> int:
    """软归档老曲线数据（不删行，只标 is_archived=1）。

    Returns:
        归档行数。失败 -1。
    """
    owned = connection is None
    connection = connection or _connect()
    try:
        cols = _table_columns(connection, "event_impact_curve")
        if "is_archived" not in cols:
            # 第一次：ALTER TABLE 加列
            connection.execute(
                "ALTER TABLE event_impact_curve ADD COLUMN is_archived INTEGER NOT NULL DEFAULT 0"
            )
            connection.commit()
            cols.add("is_archived")
        import time as _t
        cutoff = _t.time() - days * 86400
        cur = connection.execute(
            "UPDATE event_impact_curve SET is_archived = 1 WHERE sample_ts < ? AND is_archived = 0",
            (cutoff,),
        )
        if owned:
            connection.commit()
        return cur.rowcount if cur.rowcount is not None else 0
    except Exception:
        if owned:
            try:
                connection.rollback()
            except Exception:
                pass
        return -1
    finally:
        if owned:
            try:
                connection.close()
            except Exception:
                pass


# ────────────────────────────────────────────────────────────────────────────
# Self-test
# ────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=== build_minimal_curve ===")
    c = build_minimal_curve(
        elasticity_minutes=120,
        price_at_event=2400.0,
        delta_pct_at_peak=0.85,
        peak_after_minutes=15,
        asset_code="XAU",
    )
    for k, v in c.items():
        print(f"  {k:10s} → {v}")

    print("\n=== serialize / deserialize roundtrip ===")
    s = serialize_curve(c)
    d = deserialize_curve(s)
    print(f"  roundtrip keys: {list(d.keys())}")

    print("\n=== WINDOW_MINUTES ===")
    for k, m in WINDOW_MINUTES.items():
        print(f"  {k:10s} = {m} min")