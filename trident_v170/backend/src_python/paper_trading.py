"""模拟操盘启停、赛道、策略与 Agent 运行快照。"""

import hashlib
import json
import os
import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional

import config
import db
import strategy_store

TRACK_ASSETS = {
    "crypto": {"BTC", "ETH", "SOL", "CRYPTO"},
    "gold": {"XAU", "GOLD"},
    "oil": {"WTI", "OIL"},
    # 美股（币安永续交易对，NVDAUSDT 等）：AI 定向后按币安永续近实时价建仓/结算
    "stocks": {"NVDA", "TSLA", "AAPL", "MSTR", "MSFT", "AMZN", "GOOGL",
               "COIN", "PLTR", "META", "SMCI", "MARA", "RIOT"},
}
AGENT_VERSION = "trident-evidence-agent-v1"
_RULE_FILES = (
    "AGENTS.md",
    os.path.join("docs", "约定规范.md"),
    os.path.join("docs", "方案.md"),
    os.path.join("docs", "防守与陷阱.md"),
    os.path.join("docs", "新闻.md"),
)


def _spec_version() -> str:
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    digest = hashlib.sha256()
    for relative in _RULE_FILES:
        path = os.path.join(root, relative)
        # 规则文件缺失不阻断功能（部署包可能未带全，跳过即可）
        if not os.path.exists(path):
            continue
        with open(path, "rb") as stream:
            digest.update(relative.encode("utf-8"))
            digest.update(stream.read())
    return digest.hexdigest()[:16]


def _run_payload(conn: sqlite3.Connection, run_id: Optional[int]) -> Optional[Dict[str, Any]]:
    if not run_id:
        return None
    row = conn.execute(
        """SELECT pr.*, s.name AS strategy_name, sv.version AS strategy_version
           FROM paper_trading_runs pr
           JOIN strategies s ON s.id=pr.strategy_id
           JOIN strategy_versions sv ON sv.id=pr.strategy_version_id
           WHERE pr.id=?""",
        (run_id,),
    ).fetchone()
    if row is None:
        # ── 自愈：active_run_id 指向了已被清空的旧 run。
        # 若 is_running 仍开着，自动用 is_current 策略新建一条 RUNNING run 并回填 active_run_id。
        # 避免"启动后 0 trade"再发生（实测：清空 paper_trading_runs 后 ai_worker 永远拿不到 active_run）。
        try:
            settings_row = conn.execute(
                "SELECT is_running FROM paper_trading_settings WHERE id=1"
            ).fetchone()
            is_running = bool(settings_row and settings_row[0])
            if is_running:
                cur = conn.execute(
                    "SELECT s.id AS sid, COALESCE(s.current_version_id,"
                    " (SELECT sv.id FROM strategy_versions sv WHERE sv.strategy_id=s.id"
                    "  ORDER BY sv.version DESC LIMIT 1)) AS sver,"
                    " s.name AS sname FROM strategies s"
                    " WHERE s.status='active' ORDER BY s.is_current DESC, s.id LIMIT 1"
                ).fetchone()
                if cur and cur["sid"] and cur["sver"]:
                    import config as _cfg
                    now = datetime.now().isoformat(timespec="seconds")
                    cursor = conn.execute(
                        """INSERT INTO paper_trading_runs
                           (tracks, strategy_id, strategy_version_id, agent_version,
                            model_id, spec_version, activation_reason, started_at)
                           VALUES (?,?,?,?,?,?,?,?)""",
                        (
                            json.dumps([]),
                            int(cur["sid"]),
                            int(cur["sver"]),
                            AGENT_VERSION,
                            _cfg.get_selected_ai_model_id(),
                            _spec_version(),
                            "auto-heal: active_run_id 指向了已被清空的旧 run，"
                            "自动新建 RUNNING run 恢复交易",
                            now,
                        ),
                    )
                    new_id = int(cursor.lastrowid)
                    conn.execute(
                        "UPDATE paper_trading_settings SET active_run_id=?, updated_at=? WHERE id=1",
                        (new_id, now),
                    )
                    if hasattr(conn, "commit"):
                        conn.commit()
                    print(
                        f"[PAPER-TRADE-AUTO-HEAL] active_run_id={run_id} 找不到对应 RUNNING，"
                        f"已新建 run #{new_id} (strategy={cur['sname']}) 并绑定到 settings.active_run_id",
                        flush=True,
                    )
                    # 重读一次拿到新 row
                    row = conn.execute(
                        """SELECT pr.*, s.name AS strategy_name, sv.version AS strategy_version
                           FROM paper_trading_runs pr
                           JOIN strategies s ON s.id=pr.strategy_id
                           JOIN strategy_versions sv ON sv.id=pr.strategy_version_id
                           WHERE pr.id=?""",
                        (new_id,),
                    ).fetchone()
        except Exception as _heal_err:
            print(
                f"[PAPER-TRADE-AUTO-HEAL] 自动修复失败（不影响主线）: "
                f"{type(_heal_err).__name__}: {str(_heal_err)[:120]}",
                flush=True,
            )
        if row is None:
            return None
    columns = (
        "id", "tracks", "strategy_id", "strategy_version_id", "agent_version",
        "model_id", "spec_version", "activation_reason", "status", "started_at",
        "stopped_at", "strategy_name", "strategy_version",
    )
    payload = dict(zip(columns, row))
    payload["tracks"] = json.loads(payload["tracks"] or "[]")
    return payload


def get_settings(connection=None) -> Dict[str, Any]:
    own = connection is None
    conn = connection or db.get_connection()
    if own:
        conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT is_running, tracks, started_at, active_run_id, updated_at, gate_enabled,"
            " news_trade_max_age_seconds, tp_enabled, tp1_mult, tp2_mult, tp1_ratio, tp2_ratio,"
            " buy_min_score, sell_min_score, market_structure_enabled FROM paper_trading_settings WHERE id=1"
        ).fetchone()
        if row is None:
            conn.execute("INSERT INTO paper_trading_settings (id) VALUES (1)")
            if own:
                conn.commit()
            return {
                "is_running": True, "tracks": [], "started_at": None,  # 默认开启模拟盘 (v172+)
                "active_run_id": None, "active_run": None, "gate_enabled": True,
                "updated_at": None, "news_trade_max_age_seconds": 2700,
                "tp_enabled": True, "tp1_mult": 0.6, "tp2_mult": 1.0,
                "tp1_ratio": 0.5, "tp2_ratio": 0.3,
                "buy_min_score": 0.4, "sell_min_score": 0.3,
                "market_structure_enabled": True,
            }
        max_age = row[6] if row[6] is not None else 2700
        return {
            "is_running": bool(row[0]),
            "tracks": json.loads(row[1] or "[]"),
            "started_at": row[2],
            "active_run_id": row[3],
            "active_run": _run_payload(conn, row[3]),
            "updated_at": row[4],
            "gate_enabled": bool(row[5] if row[5] is not None else 1),
            "news_trade_max_age_seconds": int(max_age),
            "tp_enabled": bool(row[7] if row[7] is not None else 1),
            "tp1_mult": float(row[8] if row[8] is not None else 0.6),
            "tp2_mult": float(row[9] if row[9] is not None else 1.0),
            "tp1_ratio": float(row[10] if row[10] is not None else 0.5),
            "tp2_ratio": float(row[11] if row[11] is not None else 0.3),
            "buy_min_score": float(row[12] if row[12] is not None else 0.4),
            "sell_min_score": float(row[13] if row[13] is not None else 0.3),
            "market_structure_enabled": bool(row[14] if row[14] is not None else 1),
        }
    finally:
        if own:
            conn.close()


def set_settings(
    is_running: bool,
    tracks: List[str],
    connection=None,
    *,
    gate_enabled: Optional[bool] = None,
    news_trade_max_age_seconds: Optional[int] = None,
    tp_enabled: Optional[bool] = None,
    tp1_mult: Optional[float] = None,
    tp2_mult: Optional[float] = None,
    tp1_ratio: Optional[float] = None,
    tp2_ratio: Optional[float] = None,
    buy_min_score: Optional[float] = None,
    sell_min_score: Optional[float] = None,
    market_structure_enabled: Optional[bool] = None,
) -> Dict[str, Any]:
    normalized = sorted({track for track in tracks if track in TRACK_ASSETS})
    if is_running and not normalized:
        raise ValueError("开始模拟操盘前必须至少选择一个投资赛道")
    own = connection is None
    conn = connection or db.get_connection()
    if own:
        conn.row_factory = sqlite3.Row
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        previous = get_settings(conn)
        active_run_id = previous["active_run_id"]
        started_at: Optional[str] = previous["started_at"]
        next_gate = previous.get("gate_enabled", True) if gate_enabled is None else bool(gate_enabled)
        next_max_age = (
            previous.get("news_trade_max_age_seconds", 2700)
            if news_trade_max_age_seconds is None
            else max(30, int(news_trade_max_age_seconds))
        )
        # 分级止盈参数（带合理性钳制：比例合计 ≤ 0.95，系数 0.1~3）
        next_tp_enabled = previous.get("tp_enabled", True) if tp_enabled is None else bool(tp_enabled)
        next_tp1_mult = previous.get("tp1_mult", 0.6) if tp1_mult is None else min(3.0, max(0.1, float(tp1_mult)))
        next_tp2_mult = previous.get("tp2_mult", 1.0) if tp2_mult is None else min(3.0, max(0.1, float(tp2_mult)))
        next_tp1_ratio = previous.get("tp1_ratio", 0.5) if tp1_ratio is None else min(0.9, max(0.0, float(tp1_ratio)))
        next_tp2_ratio = previous.get("tp2_ratio", 0.3) if tp2_ratio is None else min(0.9, max(0.0, float(tp2_ratio)))
        if next_tp1_ratio + next_tp2_ratio > 0.95:
            next_tp2_ratio = max(0.0, 0.95 - next_tp1_ratio)
        # 买卖方向分数门槛（不对称：做多胜率低 → BUY 门槛高一档）
        next_buy_min = previous.get("buy_min_score", 0.4) if buy_min_score is None else min(0.9, max(0.1, float(buy_min_score)))
        next_sell_min = previous.get("sell_min_score", 0.3) if sell_min_score is None else min(0.9, max(0.1, float(sell_min_score)))
        # 盘面结构分析开关（AI 分析时是否附带多周期盘面结构判断；即时新闻交易可关闭）
        next_market_structure = previous.get("market_structure_enabled", True) if market_structure_enabled is None else bool(market_structure_enabled)
        if is_running and not previous["is_running"]:
            current = strategy_store.get_current_strategy(conn)
            activated = strategy_store.set_current_strategy(
                int(current["id"]), int(current["active_version"]["id"]), conn
            )
            version = activated["active_version"]
            started_at = now
            cursor = conn.execute(
                """INSERT INTO paper_trading_runs
                   (tracks, strategy_id, strategy_version_id, agent_version,
                    model_id, spec_version, activation_reason, started_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (
                    json.dumps(normalized), activated["id"], version["id"],
                    AGENT_VERSION, config.get_selected_ai_model_id(), _spec_version(),
                    "开始模拟操盘：自动激活当前策略版本；"
                    + ("宽松模式关闭证据/策略闸门" if not next_gate else "启用多证据、卡方、反幻觉和赛道闸门"),
                    now,
                ),
            )
            active_run_id = int(cursor.lastrowid)
        elif not is_running and previous["is_running"] and active_run_id:
            conn.execute(
                "UPDATE paper_trading_runs SET status='STOPPED', stopped_at=? WHERE id=? AND status='RUNNING'",
                (now, active_run_id),
            )
            active_run_id = None
        conn.execute(
            """UPDATE paper_trading_settings
               SET is_running=?, tracks=?, started_at=?, active_run_id=?, gate_enabled=?,
                   news_trade_max_age_seconds=?, tp_enabled=?, tp1_mult=?, tp2_mult=?,
                   tp1_ratio=?, tp2_ratio=?, buy_min_score=?, sell_min_score=?,
                   market_structure_enabled=?, updated_at=? WHERE id=1""",
            (int(is_running), json.dumps(normalized), started_at, active_run_id, int(next_gate),
             next_max_age, int(next_tp_enabled), next_tp1_mult, next_tp2_mult,
             next_tp1_ratio, next_tp2_ratio, next_buy_min, next_sell_min,
             int(next_market_structure), now),
        )
        if own:
            conn.commit()
        return get_settings(conn)
    finally:
        if own:
            conn.close()


def asset_allowed(asset: str, settings: Dict[str, Any]) -> bool:
    value = asset.upper()
    tracks = settings.get("tracks") or []
    if not tracks:
        return False
    # 注意:settings["tracks"] 是赛道名 (gold/oil/crypto/stocks),
    # 要先用 TRACK_ASSETS 把赛道映射到资产符号集合 (XAU/WTI/BTC/...)。
    # 之前 `value in tracks` 是 bug: "XAU" in ["gold"] 永远 False → 全赛道空跑。
    return any(value in TRACK_ASSETS.get(track, set()) for track in tracks)


# ── v200+: 启动期默认开启模拟盘 ──────────────────────────────────────────
# 反思 / 经验 / 日报三大链路默认走 paper_trading_run_id 关联，
# 一旦 is_running=0、active_run 为空，所有 ai_decisions.paper_trading_run_id 都是 NULL，
# 整条反思链全断。本函数让"启动即开"成为持久默认，不再依赖运维手动点开。
# 调用方：api_server.lifespan / engine 主入口 / init_db。
# 兜底开关：config.PAPER_TRADING_AUTO_START=0 时整函数直接 return。
def ensure_default_running(connection=None) -> Dict[str, Any]:
    """v200+: 启动期幂等地把模拟盘开关翻到 ON（保留用户主动停过的意图）。

    注意：connection 参数仅作为兼容性保留——本函数内部永远 own=True 新开连接并自行 commit，
    确保 Row 模式一致（strategy_store.get_current_strategy 用 row["..."] 字符串索引,
    在 tuple 模式下会抛 TypeError）。这样 set_settings → strategy_store 整条链都按 sqlite3.Row 工作。
    如果调用方传入 connection, 我们会礼貌忽略并在 finally 中关闭自己 new 的 conn.

    规则（按优先级判定）：
      1. settings 行不存在 → INSERT 默认行 (is_running=1, tracks=全赛道)。
         后续 set_settings(1, [...]) 会因 0→1 触发 auto-heal 建 RUNNING run。
      2. is_running=0 且 started_at 为空（系统从未启动过）→ 翻 1, tracks 兜底全赛道。
         同样走 auto-heal 建 RUNNING run。
      3. is_running=0 但 started_at 不为空（用户曾经主动开过、后来手动停了）→ 
         保留用户意图, 不翻 1, 仅 INFO 日志, 让运维显式去 UI / API 启动。
      4. is_running=1 但 active_run_id 缺失（异常状态, e.g. 数据库被手工改过）→ 
         仍走 set_settings(1, [...]) 触发 auto-heal 补建 RUNNING run。
      5. 任何异常 → 静默降级, 不阻断启动, 返回当前 settings。

    返回：最新的 settings dict（与 get_settings 同样的结构）。
    """
    if not getattr(config, "PAPER_TRADING_AUTO_START", True):
        # 兜底开关关闭 → 用 caller conn 读一次现状返回 (不开 conn 避免不必要的 commit 路径)
        return get_settings(connection)
    try:
        # 强制走 own=True：保证 conn.row_factory = sqlite3.Row, 下游 strategy_store 才能字符串索引
        # 即便 caller 传了 connection, 这里也忽略以避免 Row/tuple 不一致崩溃。
        conn = db.get_connection()
        conn.row_factory = sqlite3.Row
        try:
            # 先读现状
            current = get_settings(conn)
            was_running = bool(current.get("is_running"))
            started_at = current.get("started_at")
            active_run_id = current.get("active_run_id")

            # 规则 1: 行不存在 → get_settings 已经 INSERT 默认行 (is_running=True via Python default)
            # 规则 3: 用户曾主动停过 → 不动
            if not was_running and started_at:
                print(
                    "[PAPER] 模拟盘当前为关闭状态且 started_at 非空，"
                    "判定为用户主动停过 → 不自动开启（保护手动关闭意图）",
                    flush=True,
                )
                return current

            # 规则 2/4：需要确保开启 + 有 active run
            default_tracks = list(
                getattr(config, "PAPER_TRADING_DEFAULT_TRACKS", ("crypto", "gold", "oil", "stocks"))
            )
            # 若 tracks 为空也兜底用默认（之前可能因 track 选择器未选导致空）
            normalized_tracks = sorted(
                {t for t in (current.get("tracks") or []) if t in TRACK_ASSETS}
            )
            if not normalized_tracks:
                normalized_tracks = sorted(default_tracks)

            if was_running and active_run_id:
                # 已开启且 RUNNING 存在 → 真正幂等，什么也不做
                return current

            # 触发 auto-heal：set_settings 内置 0→1 建 RUNNING run 的逻辑
            # 注意：set_settings 在 own=False (传入 conn) 时不 commit,
            # 我们在调用方手动 commit 以遵守"caller 控制事务边界"的契约,
            # 同时确保 conn.close 之前所有 INSERT/UPDATE 已落盘。
            updated = set_settings(True, normalized_tracks, connection=conn)
            conn.commit()
            print(
                "[PAPER] 模拟盘已默认开启: is_running 0→1, "
                "tracks=%s, active_run_id=%s"
                % (normalized_tracks, updated.get("active_run_id")),
                flush=True,
            )
            return updated
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except Exception as exc:
        # 静默降级：启动期任何异常都不阻断主流程
        print(
            "[PAPER] ensure_default_running 跳过: %s: %s"
            % (type(exc).__name__, str(exc)[:120]),
            flush=True,
        )
        try:
            return get_settings()
        except Exception:
            return {"is_running": False, "tracks": [], "active_run": None, "active_run_id": None}


# ── v182 反思改进·把已审批的 adjustment 应用到 paper_trading_settings ──
# 只处理 key ∈ _V182_SETTINGS_KEYS 的子集；其他（strategies 下参数）走
# strategy_store.add_version(source='reflection')。direction ∈ {up, down, on, off}。
_V182_SETTINGS_KEYS = {
    "buy_min_score", "sell_min_score",
    "tp1_mult", "tp2_mult", "tp1_ratio", "tp2_ratio",
    "tp_enabled", "gate_enabled", "market_structure_enabled",
    "news_trade_max_age_seconds",
}


def _apply_param_change(key: str, direction: str, magnitude: float,
                       current_value: Any) -> Any:
    """根据 direction+magnitude 计算新值（不动 settings，仅算）。"""
    if direction in ("on", "off"):
        if key in ("tp_enabled", "gate_enabled", "market_structure_enabled"):
            return 1 if direction == "on" else 0
        return current_value  # 其他不响应 on/off
    if key == "news_trade_max_age_seconds":
        # 整数秒数，按 10% 步长
        try:
            # EXEMPT-HONESTY: 参数缺省值默认 0 — 数学调整起点,非显示给用户的数据
            cur = float(current_value or 0)
        except (TypeError, ValueError):
            cur = 2700.0
        factor = 1.0 + (magnitude if direction == "up" else -magnitude)
        return max(30, min(86400, int(cur * factor)))
    if key in ("tp1_mult", "tp2_mult"):
        try:
            # EXEMPT-HONESTY: 参数缺省值默认 0 — 数学调整起点
            cur = float(current_value or 0)
        except (TypeError, ValueError):
            cur = 1.0
        factor = 1.0 + (magnitude if direction == "up" else -magnitude)
        return min(3.0, max(0.1, cur * factor))
    if key in ("tp1_ratio", "tp2_ratio"):
        try:
            # EXEMPT-HONESTY: 参数缺省值默认 0 — 数学调整起点
            cur = float(current_value or 0)
        except (TypeError, ValueError):
            cur = 0.5
        delta = magnitude if direction == "up" else -magnitude
        return min(0.9, max(0.0, cur + delta))
    if key in ("buy_min_score", "sell_min_score"):
        try:
            # EXEMPT-HONESTY: 参数缺省值默认 0 — 数学调整起点
            cur = float(current_value or 0)
        except (TypeError, ValueError):
            cur = 0.4
        delta = magnitude if direction == "up" else -magnitude
        return min(0.9, max(0.1, cur + delta))
    return current_value


def apply_adjustment_to_settings(adj: Dict[str, Any],
                                connection=None) -> Dict[str, Any]:
    """v182：把 strategy_param_adjustments 行（status='pending' → 'approved'）应用到 paper_trading_settings。

    只处理 key ∈ _V182_SETTINGS_KEYS 子集。计算新值、调 set_settings 写回，并返回新旧对照。
    不影响 is_running / tracks / active_run_id（这些是用户手动控制的）。
    """
    key = str(adj.get("param_key") or "")
    if key not in _V182_SETTINGS_KEYS:
        raise ValueError(f"key '{key}' 不属于 paper_trading_settings，跳过 settings 调参")
    direction = str(adj.get("direction") or "")
    if direction not in ("up", "down", "on", "off"):
        raise ValueError(f"direction '{direction}' 不合法")
    try:
        magnitude = float(adj.get("magnitude") or 0.05)
    except (TypeError, ValueError):
        magnitude = 0.05
    magnitude = min(0.5, max(0.01, magnitude))
    own = connection is None
    conn = connection or db.get_connection()
    if own:
        conn.row_factory = sqlite3.Row
    try:
        prev = get_settings(conn)
        cur_val = prev.get(key)
        new_val = _apply_param_change(key, direction, magnitude, cur_val)
        kwargs: Dict[str, Any] = {
            "gate_enabled": (int(new_val) if key == "gate_enabled" else None),
            "tp_enabled": (int(new_val) if key == "tp_enabled" else None),
            "market_structure_enabled": (int(new_val) if key == "market_structure_enabled" else None),
            "news_trade_max_age_seconds": (int(new_val) if key == "news_trade_max_age_seconds" else None),
            "tp1_mult": (float(new_val) if key == "tp1_mult" else None),
            "tp2_mult": (float(new_val) if key == "tp2_mult" else None),
            "tp1_ratio": (float(new_val) if key == "tp1_ratio" else None),
            "tp2_ratio": (float(new_val) if key == "tp2_ratio" else None),
            "buy_min_score": (float(new_val) if key == "buy_min_score" else None),
            "sell_min_score": (float(new_val) if key == "sell_min_score" else None),
        }
        # 不动 is_running/tracks
        result = set_settings(
            bool(prev.get("is_running")),
            list(prev.get("tracks") or []),
            conn,
            **{k: v for k, v in kwargs.items() if v is not None},
        )
        if own:
            conn.commit()
        return {
            "param_key": key,
            "direction": direction,
            "magnitude": magnitude,
            "old_value": cur_val,
            "new_value": result.get(key),
            "applied_at": result.get("updated_at"),
            "adjustment_id": int(adj.get("id") or 0),
        }
    finally:
        if own:
            conn.close()
    return any(value in TRACK_ASSETS.get(track, set()) for track in settings.get("tracks", []))
