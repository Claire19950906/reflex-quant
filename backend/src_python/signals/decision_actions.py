# -*- coding: utf-8 -*-
"""v383 · 信号快捷处置（decision_actions）

三个动作，都要「真的生效」，不做纯前端按钮：

| action | 真实效果 | 生效位置 |
|---|---|---|
| `silence_asset` | 该品种在 N 分钟内不再放行新信号 | `decision_writer.write_decision` 写入口统一拦截 |
| `ignore_decision` | 该笔决策标记 quarantined（退出统计口径） | `ai_decisions.quarantined_at/quarantine_reason` |
| `reverse` | 记录反向意图 + 当场算出反向影子盈亏 | `decision_actions.note` 存快照，复盘可用 |

为什么把静默放在写入口而不是前端过滤：信号有三条写入路径（log_decision /
algo_move / bridge_api），只在其中一条拦等于没拦。写入口是唯一收口点（v342 已统一）。
"""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Dict, List, Optional

ACTIONS = ("silence_asset", "ignore_decision", "reverse")
MAX_SILENCE_MIN = 60 * 24 * 30


def _norm_asset(asset: str) -> str:
    a = str(asset or "").upper().strip()
    return {"GOLD": "XAU", "OIL": "WTI"}.get(a, a)


def _decision(conn, decision_id: int) -> Optional[Dict[str, Any]]:
    cur = conn.execute(
        "SELECT id, target_asset, suggested_action, forward_pnl "
        "FROM ai_decisions WHERE id=?",
        (int(decision_id),),
    )
    row = cur.fetchone()
    if not row:
        return None
    keys = [d[0] for d in (cur.description or [])]
    return dict(zip(keys, row))


def is_asset_muted(conn, asset: str, now_ts: Optional[float] = None,
                   event_type: str = "") -> Optional[str]:
    """返回静默原因（字符串）或 None。任何异常都视为「未静默」，不阻断主链路。

    v390：作用域
      scope='asset' → 该品种下所有信号都被静默
      scope='event' → 只有同一 event_type 的信号被静默（避免"因为一个事件烦了，把整个品种关掉"）
    """
    a = _norm_asset(asset)
    if not a:
        return None
    et = str(event_type or "").strip().upper()
    now = int(now_ts if now_ts is not None else time.time())
    try:
        rows = conn.execute(
            """SELECT duration_min, expires_at, note, scope, event_type FROM decision_actions
                WHERE action='silence_asset' AND asset=?
                  AND (expires_at=0 OR expires_at > ?)
                ORDER BY id DESC LIMIT 20""",
            (a, now),
        ).fetchall()
    except Exception:
        return None
    for r in rows:
        scope = str(r[3] or "asset").lower()
        if scope == "event":
            if not et or str(r[4] or "").upper() != et:
                continue
        dur = int(r[0] or 0)
        label = f"{dur} 分钟" if dur else "长效"
        note = str(r[2] or "")
        tag = f"·仅事件{et}" if scope == "event" else ""
        return f"用户静默({label}{tag})" + (f"：{note}" if note else "")
    return None


def record(conn, decision_id: int, action: str, duration_min: int = 0,
           note: str = "", source: str = "ui",
           scope: str = "asset", event_type: str = "") -> Dict[str, Any]:
    """执行一次处置。返回 {ok, action, decision_id, asset, effect}。"""
    action = str(action or "").strip().lower()
    if action not in ACTIONS:
        return {"ok": False, "error": f"未知动作 {action}，可选：{'/'.join(ACTIONS)}"}
    d = _decision(conn, decision_id)
    if not d:
        return {"ok": False, "error": f"决策 #{decision_id} 不存在"}

    asset = _norm_asset(d.get("target_asset") or "")
    now = int(time.time())
    effect = ""

    if action == "silence_asset":
        if not asset or asset == "NONE":
            return {"ok": False, "error": "该决策没有具体品种（NONE），无法按品种静默"}
        dur = max(0, min(int(duration_min or 0), MAX_SILENCE_MIN))
        expires_at = now + dur * 60 if dur else 0
        scope_v = "event" if str(scope or "").lower() == "event" else "asset"
        et_v = str(event_type or "").strip().upper()
        if scope_v == "event" and not et_v:
            return {"ok": False, "error": "按事件静默需要 event_type"}
        conn.execute(
            """INSERT INTO decision_actions
               (decision_id, asset, action, duration_min, expires_at, note, source,
                scope, event_type)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (int(decision_id), asset, action, dur, expires_at, str(note or "")[:500], source,
             scope_v, et_v),
        )
        if scope_v == "event":
            effect = f"{asset} 仅 {et_v} 事件静默 {dur or '长'} 分钟"
        else:
            effect = f"{asset} 静默 {dur} 分钟" if dur else f"{asset} 长期静默"

    elif action == "ignore_decision":
        conn.execute(
            """UPDATE ai_decisions
                  SET quarantined_at=datetime('now'),
                      quarantine_reason=?
                WHERE id=? AND COALESCE(quarantined_at,'')=''""",
            (("user_ignore" + (f"：{note}" if note else ""))[:200], int(decision_id)),
        )
        conn.execute(
            """INSERT INTO decision_actions
               (decision_id, asset, action, duration_min, expires_at, note, source)
               VALUES (?,?,?,?,?,?,?)""",
            (int(decision_id), asset, action, 0, 0, str(note or "")[:500], source),
        )
        effect = "该笔已退出统计口径（quarantined）"

    elif action == "reverse":
        snap: Dict[str, Any] = {}
        try:
            import counterfactual as _cf
            legs = _cf.four_legs(int(decision_id), conn=conn).get("legs") or []
            rev = next((x for x in legs if x.get("key") == "reverse"), None)
            if rev:
                snap = {"pnl_pct": rev.get("pnl_pct"), "mfe_pct": rev.get("mfe_pct"),
                        "mae_pct": rev.get("mae_pct"), "entry_price": rev.get("entry_price"),
                        "exit_price": rev.get("exit_price")}
        except Exception as exc:
            snap = {"error": f"{type(exc).__name__}: {str(exc)[:60]}"}
        payload = json.dumps({"note": note, "shadow_reverse": snap}, ensure_ascii=False)
        conn.execute(
            """INSERT INTO decision_actions
               (decision_id, asset, action, duration_min, expires_at, note, source)
               VALUES (?,?,?,?,?,?,?)""",
            (int(decision_id), asset, action, 0, 0, payload[:2000], source),
        )
        effect = ("已记录反向意图；影子反向盈亏 "
                  f"{snap.get('pnl_pct')}%（不下真实单）")

    try:
        conn.commit()
    except Exception:
        pass
    return {"ok": True, "action": action, "decision_id": int(decision_id),
            "asset": asset, "effect": effect, "at": now}


def list_recent(conn, limit: int = 30) -> List[Dict[str, Any]]:
    try:
        rows = conn.execute(
            """SELECT id, decision_id, asset, action, duration_min, expires_at,
                      note, source, created_at, scope, event_type
                 FROM decision_actions ORDER BY id DESC LIMIT ?""",
            (max(1, min(int(limit), 200)),),
        ).fetchall()
        return [
            {"id": r[0], "decision_id": r[1], "asset": r[2], "action": r[3],
             "duration_min": r[4], "expires_at": r[5], "note": r[6],
             "source": r[7], "created_at": r[8],
             "scope": r[9] if len(r) > 9 else "asset",
             "event_type": r[10] if len(r) > 10 else ""}
            for r in rows
        ]
    except Exception:
        return []


def muted_assets(conn) -> List[Dict[str, Any]]:
    now = int(time.time())
    try:
        rows = conn.execute(
            """SELECT asset, MAX(duration_min), MAX(expires_at), MAX(created_at)
                 FROM decision_actions
                WHERE action='silence_asset' AND (expires_at=0 OR expires_at > ?)
                GROUP BY asset ORDER BY 4 DESC""",
            (now,),
        ).fetchall()
        return [{"asset": r[0], "duration_min": r[1], "expires_at": r[2],
                 "created_at": r[3],
                 "remaining_min": (max(0, int((r[2] - now) / 60)) if r[2] else 0)}
                for r in rows]
    except Exception:
        return []


def active_silences(conn) -> List[Dict[str, Any]]:
    """当前仍生效的静默（带行 id，便于逐条撤销）。"""
    now = int(time.time())
    try:
        rows = conn.execute(
            """SELECT id, asset, duration_min, expires_at, source, created_at, scope, event_type
                 FROM decision_actions
                WHERE action='silence_asset' AND (expires_at=0 OR expires_at > ?)
                ORDER BY id DESC LIMIT 50""",
            (now,),
        ).fetchall()
        return [{"id": r[0], "asset": r[1], "duration_min": r[2], "expires_at": r[3],
                 "source": r[4], "created_at": r[5],
                 "scope": r[6] if len(r) > 6 else "asset",
                 "event_type": r[7] if len(r) > 7 else "",
                 "remaining_min": (max(0, int((r[3] - now) / 60)) if r[3] else 0)}
                for r in rows]
    except Exception:
        return []


def unsilence(conn, action_id: int = 0, asset: str = "") -> Dict[str, Any]:
    """撤销静默：把 expires_at 置为过去时刻，而不是删行（留痕可审计）。

    v388 —— 之前静默只能等过期，用户误点就没法恢复；这里补上恢复路径。
    """
    now = int(time.time())
    try:
        if action_id:
            rows = conn.execute(
                """SELECT id FROM decision_actions
                    WHERE id=? AND action='silence_asset'
                      AND (expires_at=0 OR expires_at > ?)""",
                (int(action_id), now),
            ).fetchall()
        elif asset:
            rows = conn.execute(
                """SELECT id FROM decision_actions
                    WHERE asset=? AND action='silence_asset'
                      AND (expires_at=0 OR expires_at > ?)""",
                (_norm_asset(asset), now),
            ).fetchall()
        else:
            return {"ok": False, "error": "需要 action_id 或 asset"}
        ids = [r[0] for r in rows]
        for rid in ids:
            conn.execute(
                "UPDATE decision_actions SET expires_at=? WHERE id=?", (now - 1, rid))
        conn.commit()
        return {"ok": True, "revoked": len(ids), "ids": ids}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:120]}"}


def _main(argv: Optional[List[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="信号快捷处置（写库）")
    ap.add_argument("decision_id", type=int)
    ap.add_argument("--action", required=True, choices=list(ACTIONS))
    ap.add_argument("--minutes", type=int, default=0)
    ap.add_argument("--note", default="")
    args = ap.parse_args(argv)
    import db as _db
    conn = _db.get_connection()
    try:
        print(json.dumps(record(conn, args.decision_id, args.action,
                                args.minutes, args.note, source="cli"),
                         ensure_ascii=False, indent=2))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
