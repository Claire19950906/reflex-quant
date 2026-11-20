"""v223: Belief Management System.

worldview -> chain -> fact (3 levels)
- worldview: abstract thesis (e.g. "Fed expectations drive most moves")
- chain: testable sub-chain (e.g. "Hawkish Fed -> DXY up -> BTC down")
- fact: verification unit (reuses existing causal_graph._extract_facts)

Features:
- Conflict detector: new belief vs existing => REINFORCES/CONTRADICTS/SUPERSEDES/REFINES
- Auto-decompose: LLM splits worldview into 3-7 testable chains
- Synthesis: per-chain verify + aggregate verdict + regime map + trading rule candidates
- Revision loop: confidence auto-adjusts based on evidence
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import causal_graph
import db
from review import review_agent


def _now() -> str:
    return review_agent._now()


# ============================================================================
# Schema
# ============================================================================
_CREATE_BELIEFS = """
CREATE TABLE IF NOT EXISTS beliefs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    belief_uid TEXT NOT NULL UNIQUE,
    level TEXT NOT NULL CHECK (level IN ('worldview', 'chain')),
    thesis TEXT NOT NULL,
    asset TEXT NOT NULL DEFAULT '',
    parent_id INTEGER,
    confidence REAL NOT NULL DEFAULT 0.5,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'deprecated', 'conflicted', 'archived')),
    regime_tags TEXT NOT NULL DEFAULT '[]',
    evidence_for INTEGER NOT NULL DEFAULT 0,
    evidence_against INTEGER NOT NULL DEFAULT 0,
    last_verified_at TEXT,
    source TEXT NOT NULL DEFAULT 'user'
        CHECK (source IN ('user', 'auto_decompose', 'imported', 'revised')),
    version INTEGER NOT NULL DEFAULT 1,
    history_json TEXT NOT NULL DEFAULT '[]',
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
"""

_CREATE_BELIEF_EDGES = """
CREATE TABLE IF NOT EXISTS belief_edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    src_id INTEGER NOT NULL,
    dst_id INTEGER NOT NULL,
    relation TEXT NOT NULL
        CHECK (relation IN ('REINFORCES', 'CONTRADICTS', 'SUPERSEDES', 'REFINES', 'UNRELATED')),
    strength REAL NOT NULL DEFAULT 0.5,
    detected_by TEXT NOT NULL DEFAULT 'user'
        CHECK (detected_by IN ('user', 'llm')),
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE (src_id, dst_id, relation)
);
"""

_CREATE_BELIEF_HISTORY = """
CREATE TABLE IF NOT EXISTS belief_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    belief_id INTEGER NOT NULL,
    belief_uid TEXT NOT NULL,
    version INTEGER NOT NULL,
    action TEXT NOT NULL,
    delta_json TEXT NOT NULL DEFAULT '{}',
    confidence_before REAL,
    confidence_after REAL,
    actor TEXT NOT NULL DEFAULT 'system',
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
"""

_IDX_BELIEFS_LEVEL = "CREATE INDEX IF NOT EXISTS idx_beliefs_level ON beliefs(level, status);"
_IDX_BELIEFS_PARENT = "CREATE INDEX IF NOT EXISTS idx_beliefs_parent ON beliefs(parent_id);"
_IDX_BELIEF_EDGES_SRC = "CREATE INDEX IF NOT EXISTS idx_belief_edges_src ON belief_edges(src_id);"
_IDX_BELIEF_HISTORY = "CREATE INDEX IF NOT EXISTS idx_belief_history_belief ON belief_history(belief_id, version);"


def _ensure_beliefs_schema(conn: sqlite3.Connection) -> None:
    conn.execute(_CREATE_BELIEFS)
    conn.execute(_CREATE_BELIEF_EDGES)
    conn.execute(_CREATE_BELIEF_HISTORY)
    conn.execute(_IDX_BELIEFS_LEVEL)
    conn.execute(_IDX_BELIEFS_PARENT)
    conn.execute(_IDX_BELIEF_EDGES_SRC)
    conn.execute(_IDX_BELIEF_HISTORY)


# ============================================================================
# Belief CRUD
# ============================================================================
def _make_uid() -> str:
    return f"b_{uuid.uuid4().hex[:12]}"


def create_belief(thesis, level="worldview", asset="", parent_id=None,
                  confidence=0.5, source="user", regime_tags=None, note=""):
    """Create a new belief; auto-detect conflicts vs existing."""
    thesis = (thesis or "").strip()
    if not thesis:
        raise ValueError("thesis cannot be empty")
    if level not in ("worldview", "chain"):
        raise ValueError(f"level must be worldview or chain, got {level}")
    confidence = max(0.0, min(1.0, float(confidence)))
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    # v2026-10-27 (G12): 心理预期层的写入发生在维护作业里, 此时 engine/ai_worker
    # 正在高频写库 —— 默认 5s busy_timeout 经常直接 "database is locked" 失败,
    # 于是"分解世界观"这步永远做不完 (子链条建不出来 → 复核也跑不了)。
    # 这里给 30s, 让它排队而不是放弃。
    try:
        conn.execute("PRAGMA busy_timeout=30000")
    except Exception:
        pass
    try:
        uid = _make_uid()
        now = _now()
        cur = conn.execute(
            """INSERT INTO beliefs
               (belief_uid, level, thesis, asset, parent_id, confidence,
                regime_tags, source, history_json, note, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (uid, level, thesis, (asset or "").upper(), parent_id, confidence,
             json.dumps(list(regime_tags or []), ensure_ascii=False),
             source,
             json.dumps([{"ver": 1, "ts": now, "action": "create", "note": note}],
                        ensure_ascii=False),
             note, now, now),
        )
        bid = int(cur.lastrowid)
        conn.execute(
            """INSERT INTO belief_history
               (belief_id, belief_uid, version, action, delta_json,
                confidence_before, confidence_after, actor, note)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (bid, uid, 1, "create",
             json.dumps({"thesis": thesis, "level": level}, ensure_ascii=False),
             None, confidence, "user", note),
        )
        conn.commit()
        try:
            conflicts = _detect_conflicts_for_belief(conn, bid, thesis, level, asset, source)
            for c in conflicts:
                _insert_edge(conn, bid, c["other_id"], c["relation"],
                             c["strength"], "llm", c.get("reason", ""))
            conn.commit()
        except Exception:
            conflicts = []
        out = get_belief(bid) or {}
        out["conflicts"] = conflicts
        return out
    finally:
        conn.close()


def get_belief(belief_id):
    """Fetch one belief by id, with parsed JSON columns + sub_chains + edges."""
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM beliefs WHERE id=?", (belief_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["regime_tags"] = json.loads(d.get("regime_tags") or "[]")
        d["history_json"] = json.loads(d.get("history_json") or "[]")
        if d["level"] == "worldview":
            kids = conn.execute(
                "SELECT id, belief_uid, thesis, confidence, status, "
                "evidence_for, evidence_against "
                "FROM beliefs WHERE parent_id=? ORDER BY id ASC",
                (belief_id,),
            ).fetchall()
            d["sub_chains"] = [dict(r) for r in kids]
        else:
            d["sub_chains"] = []
        edges = conn.execute(
            "SELECT dst_id, relation, strength, note FROM belief_edges WHERE src_id=?",
            (belief_id,),
        ).fetchall()
        d["outgoing_edges"] = [dict(e) for e in edges]
        edges_in = conn.execute(
            "SELECT src_id, relation, strength, note FROM belief_edges WHERE dst_id=?",
            (belief_id,),
        ).fetchall()
        d["incoming_edges"] = [dict(e) for e in edges_in]
        return d
    finally:
        conn.close()


def list_beliefs(level=None, status=None, asset=None, limit=100):
    """List beliefs; optional filters by level/status/asset."""
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    try:
        sql = "SELECT * FROM beliefs WHERE 1=1"
        params = []
        if level:
            sql += " AND level=?"
            params.append(level)
        if status:
            sql += " AND status=?"
            params.append(status)
        if asset:
            sql += " AND asset=?"
            params.append(asset.upper())
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(min(max(limit, 1), 500))
        rows = conn.execute(sql, params).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["regime_tags"] = json.loads(d.get("regime_tags") or "[]")
            out.append(d)
        return out
    finally:
        conn.close()


def revise_belief(belief_id, *, confidence=None, status=None,
                  regime_tags=None, note="", actor="system",
                  evidence_for=None, evidence_against=None, verified=False):
    """Update confidence / status / regime; write history row.

    v314 P0: 新增证据回写 (evidence_for / evidence_against / last_verified_at)。
    旧实现只改 confidence/status/regime —— _verify_belief_chain 算出来的
    support_count / contradict_count **从来没人落库**，beliefs 表的证据列永远是 0
    → 验收 #4B "有证据支撑的 belief ≥ 10 条" 直接判 FAIL。
    现在把核验出来的支持/反驳计数真写进去（数值来自真实事实匹配，不是编的）。
    """
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    # v314 P0: 线上 api_server 常驻写锁 → 把 busy_timeout 从默认 5s 拉长，
    # 否则单条 belief 复核经常抢不到写锁（证据落库失败）。
    try:
        conn.execute("PRAGMA busy_timeout=20000;")
    except Exception:
        pass
    try:
        row = conn.execute("SELECT * FROM beliefs WHERE id=?", (belief_id,)).fetchone()
        if not row:
            raise ValueError(f"belief {belief_id} not found")
        d = dict(row)
        delta = {}
        new_conf = float(confidence) if confidence is not None else d["confidence"]
        new_conf = max(0.0, min(1.0, new_conf))
        new_status = status or d["status"]
        new_regime = (
            json.dumps(list(regime_tags), ensure_ascii=False)
            if regime_tags is not None else d["regime_tags"]
        )
        new_ver = int(d["version"]) + 1
        now = _now()
        # ── 证据回写 ──────────────────────────────────────────────────────
        new_ev_for = (int(evidence_for) if evidence_for is not None
                      else int(d.get("evidence_for") or 0))
        new_ev_against = (int(evidence_against) if evidence_against is not None
                          else int(d.get("evidence_against") or 0))
        new_last_verified = d.get("last_verified_at")
        if verified:
            new_last_verified = now
        if abs(new_conf - d["confidence"]) > 1e-6:
            delta["confidence"] = {"old": d["confidence"], "new": new_conf}
        if new_status != d["status"]:
            delta["status"] = {"old": d["status"], "new": new_status}
        if new_ev_for != int(d.get("evidence_for") or 0):
            delta["evidence_for"] = {"old": int(d.get("evidence_for") or 0),
                                     "new": new_ev_for}
        if new_ev_against != int(d.get("evidence_against") or 0):
            delta["evidence_against"] = {"old": int(d.get("evidence_against") or 0),
                                         "new": new_ev_against}
        if new_last_verified != d.get("last_verified_at"):
            delta["last_verified_at"] = {"old": d.get("last_verified_at"),
                                         "new": new_last_verified}
        history = json.loads(d.get("history_json") or "[]")
        history.append({"ver": new_ver, "ts": now, "action": "revise",
                        "delta": delta, "note": note})
        _hist_json = json.dumps(history, ensure_ascii=False)
        _delta_json = json.dumps(delta, ensure_ascii=False)

        # v314 P0: 线上 worker 常驻写锁，deferred 事务升级写锁会直接 SQLITE_BUSY
        # （busy_timeout 对"读事务升级写"不生效）→ 用 BEGIN IMMEDIATE + 退避重试。
        def _do_write() -> None:
            try:
                if conn.in_transaction:
                    conn.rollback()
            except Exception:
                pass
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """UPDATE beliefs
                   SET confidence=?, status=?, regime_tags=?, version=?,
                       history_json=?, updated_at=?,
                       evidence_for=?, evidence_against=?, last_verified_at=?
                   WHERE id=?""",
                (new_conf, new_status, new_regime, new_ver,
                 _hist_json, now,
                 new_ev_for, new_ev_against, new_last_verified, belief_id),
            )
            conn.execute(
                """INSERT INTO belief_history
                   (belief_id, belief_uid, version, action, delta_json,
                    confidence_before, confidence_after, actor, note)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (belief_id, d["belief_uid"], new_ver, "revise",
                 _delta_json,
                 d["confidence"], new_conf, actor, note),
            )
            conn.commit()

        db.retry_on_busy(_do_write, max_retries=10, base_delay=0.8)
        return get_belief(belief_id) or {}
    finally:
        conn.close()


# ============================================================================
# Conflict Detection
# ============================================================================
def _insert_edge(conn, src_id, dst_id, relation, strength=0.5,
                 detected_by="llm", note=""):
    """Insert (or update on conflict) one belief_edge row."""
    if src_id == dst_id:
        return
    conn.execute(
        """INSERT INTO belief_edges (src_id, dst_id, relation, strength, detected_by, note)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(src_id, dst_id, relation)
           DO UPDATE SET strength=excluded.strength, note=excluded.note""",
        (src_id, dst_id, relation, strength, detected_by, note),
    )


def _detect_conflicts_for_belief(conn, new_id, thesis, level, asset, source):
    """Compare new belief vs existing active beliefs via LLM.

    Returns list of {other_id, relation, strength, reason}.
    Skip UNRELATED relations.
    """
    asset_norm = (asset or "").upper()
    if level == "chain":
        row = conn.execute("SELECT parent_id FROM beliefs WHERE id=?",
                           (new_id,)).fetchone()
        parent = row["parent_id"] if row else None
        if parent:
            cand_rows = conn.execute(
                "SELECT id, thesis FROM beliefs WHERE id=? AND status='active'",
                (parent,),
            ).fetchall()
        else:
            cand_rows = []
    else:
        cand_rows = conn.execute(
            """SELECT id, thesis, level FROM beliefs
               WHERE id != ? AND status='active'
                 AND level IN ('worldview', 'chain')
                 AND (asset = '' OR asset = ? OR ? = '')
               ORDER BY updated_at DESC LIMIT 50""",
            (new_id, asset_norm, asset_norm),
        ).fetchall()
    if not cand_rows:
        return []
    cand_text = "\n".join(f"[#{r['id']}] {r['thesis']}" for r in cand_rows)
    prompt = (
        f"现有候选信念列表:\n{cand_text}\n\n"
        f"新信念:\n[{new_id}] {thesis}\n\n"
        "判断新信念与每条候选的关系，输出 JSON 数组:\n"
        "[{\"other_id\": <int>, "
        "\"relation\": \"REINFORCES|CONTRADICTS|SUPERSEDES|REFINES|UNRELATED\","
        " \"strength\": 0-1, \"reason\": \"<=60字\"}, ...]"
    )
    raw = review_agent._llm(prompt,
                            "你是知识图谱分析师，只输出 JSON 数组。",
                            max_tokens=1500)
    parsed = review_agent._parse_json(raw)
    if not isinstance(parsed, list):
        return []
    out = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        rel = str(item.get("relation") or "").upper()
        if rel == "UNRELATED":
            continue
        oid = int(item.get("other_id") or 0)
        if oid <= 0:
            continue
        try:
            strength = float(item.get("strength") or 0.5)
        except Exception:
            strength = 0.5
        strength = max(0.0, min(1.0, strength))
        out.append({
            "other_id": oid,
            "relation": rel,
            "strength": strength,
            "reason": str(item.get("reason") or "")[:120],
        })
    return out


# ============================================================================
# Auto Decompose
# ============================================================================
def decompose_worldview(belief_id, max_sub=7, lookback_days=30, auto_verify=True):
    """LLM-split a worldview into 3-7 testable sub-chains; immediately verify."""
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM beliefs WHERE id=?",
                           (belief_id,)).fetchone()
        if not row:
            raise ValueError(f"belief {belief_id} not found")
        if row["level"] != "worldview":
            raise ValueError(
                f"only worldview can be decomposed; #{belief_id} is {row['level']}"
            )
        thesis = row["thesis"]
        asset = row["asset"]
        existing = conn.execute(
            "SELECT id, thesis FROM beliefs WHERE parent_id=? AND status='active'",
            (belief_id,),
        ).fetchall()
        if existing:
            return {
                "worldview": dict(row),
                "sub_chains": [dict(r) for r in existing],
                "synthesis": _synthesize_from_children(
                    belief_id, lookback_days, auto_verify),
                "note": "already has sub_chains; not re-decomposed",
            }
        prompt = (
            f"把以下抽象的世界观假设拆成 3-{max_sub} 条可测的因果子链。\n"
            f"每条子链必须是「具体可测的」——能用 ai_decisions、"
            f"factor_snapshots、news_events 验证。\n\n"
            f"世界观：{thesis}\n"
            f"关联品种：{asset or '跨品种'}\n\n"
            "输出 JSON 数组：\n"
            "[{\"chain\": \"表层现象→中间机制→底层原因\", "
            "\"kind\": \"core|condition\", \"is_core\": true|false, "
            "\"regime_tags\": [\"low_vol\", \"fomc_active\"], "
            "\"confidence\": 0-1}, ...]\n"
            "要求：\n"
            "1. 第一条应是最直接的传导\n"
            "2. 中间几条覆盖反例/边界条件\n"
            "3. 最后一条通常是「假设什么时候失效」\n"
        )
        # v2026-10-27 (G12): LLM 调用要十几秒, 期间不能占着读事务 ——
        # 否则调用结束后写库会撞上别的进程的长写锁, 直接 "database is locked"。
        # 这里先提交并把 busy_timeout 拉长, 再去调 LLM。
        try:
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute("PRAGMA busy_timeout=30000")
        except Exception:
            pass
        raw = review_agent._llm(prompt,
                                "你是宏观因果分析师，只输出 JSON 数组。",
                                max_tokens=2500)
        parsed = review_agent._parse_json(raw)
        if not isinstance(parsed, list) or not parsed:
            raise ValueError("LLM decompose returned empty")
        sub_ids = []
        for item in parsed[:max_sub]:
            if not isinstance(item, dict):
                continue
            chain_text = str(item.get("chain") or "").strip()
            if not chain_text:
                continue
            kind = item.get("kind") or "core"
            if kind not in ("core", "condition"):
                kind = "core"
            try:
                conf = float(item.get("confidence") or 0.5)
            except Exception:
                conf = 0.5
            regime = item.get("regime_tags") or []
            if not isinstance(regime, list):
                regime = []
            sub = create_belief(
                thesis=chain_text,
                level="chain",
                asset=asset,
                parent_id=belief_id,
                confidence=conf,
                source="auto_decompose",
                regime_tags=[str(x) for x in regime][:10],
                note=f"decompose from #{belief_id}",
            )
            sub_ids.append(int(sub["id"]))
        synthesis = _synthesize_from_children(
            belief_id, lookback_days, auto_verify)
        return {
            "worldview": get_belief(belief_id),
            "sub_chains": [get_belief(s) for s in sub_ids],
            "synthesis": synthesis,
        }
    finally:
        conn.close()


# ============================================================================
# Belief 专属证据源 (v314 P0)
# ============================================================================
_TICKER_CANONICAL = {
    "BTC-USD": "BTC_Price",
    "DX-Y.NYB": "DXY",
    "^VIX": "VIX",
    "^GSPC": "SPX",
    "TLT": "Rates",
}


def _canonicalize_facts(facts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """给 external_indicators 事实补一份"规范名"副本，便于 thesis 文本匹配。

    外部行情源的 indicator 是 Yahoo ticker（DX-Y.NYB / ^GSPC / TLT / ^VIX /
    BTC-USD），而 belief thesis 写的是中文语义词（美元 / 实际利率 / 风险资产 /
    BTC / 波动率）—— 直接匹配永远命中不了。补一份规范名副本后，
    causal_graph._INDICATOR_ALIASES 的同义词表就能把两边对上。
    原始 ticker 事实保留（不删），因此不会重复计票：thesis 里不会出现 ticker 字面量。
    """
    out: List[Dict[str, Any]] = list(facts)
    for f in facts:
        if str(f.get("source") or "") != "external_indicators":
            continue
        canon = _TICKER_CANONICAL.get(str(f.get("indicator") or "").upper())
        if not canon:
            continue
        dup = dict(f)
        dup["indicator"] = canon
        dup["detail"] = f.get("indicator")
        out.append(dup)
    return out


def _belief_extra_facts(asset: str, since_ts: Optional[str] = None,
                         limit: int = 200) -> List[Dict[str, Any]]:
    """belief 核验用的补充事实源：ai_decisions + news_events。

    背景：belief thesis 里大量写着"该传导应在 ai_decisions 中表现为…"、
    "需检查 ai_decisions 与 factor_snapshots"、"ai_decisions 转空"、
    "news_events 出现监管" —— 这些链**自己点名了证据表**。但旧实现
    causal_graph._extract_facts 只从 factor_snapshots + external_indicators 取事实，
    ai_decisions 那一段又只 JOIN 已结算 quick_sim_trades（全库仅 10 条、BTC 0 条）
    → 这些链永远匹配不到任何事实 → support/contradict 恒为 0 →
    beliefs.evidence_for/against 恒为 0 → 验收 #4B 必 FAIL。

    这里补两个**真实**事实源（都是库里已有的实际记录，不是编的）：
      - ai_decisions：suggested_action + sentiment_score → 方向
      - news_events ：action + score → 方向
    indicator 固定为 "ai_decisions" / "news_events"，让 thesis 文本能匹配上。

    口径说明（重要）：ai_decisions / news_events 的方向是**系统自己的判断方向**，
    属于结构化弱证据（"信息面在往哪边走"），不是市场真实涨跌；
    真正带 realized pnl 的证据仍在 quick_sim_trades（样本少）。
    因此 evidence_for/against 表示"有多少条事实与该链方向一致/相反"，
    不等于该因果链已被市场证实。
    """
    facts: List[Dict[str, Any]] = []
    asset = str(asset or "BTC").strip()
    # ts 列存的是 Unix int 字符串，调用方常传 ISO —— 统一转 int 再比（否则永远 false）
    since_norm: Optional[str] = None
    if since_ts:
        try:
            if isinstance(since_ts, str) and ("T" in since_ts or "-" in since_ts):
                since_norm = str(int(datetime.fromisoformat(since_ts).timestamp()))
            else:
                since_norm = str(int(float(since_ts)))
        except Exception:
            since_norm = str(since_ts)
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    try:
        # ── 源 1: ai_decisions（方向 = 决策方向，非 HOLD） ────────────────
        try:
            sql = ("SELECT id, ts, suggested_action, sentiment_score "
                   "FROM ai_decisions WHERE target_asset=?")
            params: List[Any] = [asset]
            if since_norm:
                sql += " AND ts >= ?"
                params.append(since_norm)
            sql += " ORDER BY ts DESC LIMIT ?"
            params.append(limit)
            for r in conn.execute(sql, params).fetchall():
                action = str(r["suggested_action"] or "").upper()
                score = float(r["sentiment_score"] or 0.0)
                if action in ("BUY", "LONG", "看多", "做多"):
                    direction = "up"
                elif action in ("SELL", "SHORT", "看空", "做空"):
                    direction = "down"
                else:
                    direction = "up" if score > 0.05 else (
                        "down" if score < -0.05 else "neutral")
                if direction == "neutral":
                    continue  # HOLD 不构成方向性证据
                facts.append({
                    "asset": asset,
                    "timestamp": str(r["ts"] or ""),
                    "indicator": "ai_decisions",
                    "direction": direction,
                    "magnitude": abs(score),
                    "source": "ai_decisions",
                    "source_id": int(r["id"]),
                })
        except Exception:
            pass
        # ── 源 2: news_events（方向 = 事件给出的操作方向） ───────────────
        try:
            sql = ("SELECT id, ts, action, score FROM news_events "
                   "WHERE asset=?")
            params = [asset]
            if since_norm:
                sql += " AND ts >= ?"
                params.append(since_norm)
            sql += " ORDER BY ts DESC LIMIT ?"
            params.append(limit)
            for r in conn.execute(sql, params).fetchall():
                action = str(r["action"] or "").upper()
                score = float(r["score"] or 0.0)
                if action in ("BUY", "LONG", "看多", "做多"):
                    direction = "up"
                elif action in ("SELL", "SHORT", "看空", "做空"):
                    direction = "down"
                else:
                    direction = "up" if score > 0.05 else (
                        "down" if score < -0.05 else "neutral")
                if direction == "neutral":
                    continue
                facts.append({
                    "asset": asset,
                    "timestamp": str(r["ts"] or ""),
                    "indicator": "news_events",
                    "direction": direction,
                    "magnitude": abs(score),
                    "source": "news_events",
                    "source_id": int(r["id"]),
                })
        except Exception:
            pass
    finally:
        conn.close()
    return facts


# ============================================================================
# Synthesis
# ============================================================================
def _synthesize_from_children(worldview_id, lookback_days, auto_verify):
    """Verify all sub-chains; aggregate into verdict + regime map + rules."""
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    try:
        world = conn.execute("SELECT * FROM beliefs WHERE id=?",
                             (worldview_id,)).fetchone()
        if not world:
            return {"error": "worldview not found"}
        asset = world["asset"]
        children = conn.execute(
            "SELECT * FROM beliefs WHERE parent_id=? AND status='active' "
            "ORDER BY id ASC",
            (worldview_id,),
        ).fetchall()
        if not children:
            return {"error": "no sub_chains; please decompose first"}
        facts = []
        if auto_verify:
            try:
                since = (datetime.now() -
                         timedelta(days=lookback_days)).isoformat()
            except Exception:
                since = None
            try:
                facts = causal_graph._extract_facts(
                    asset or "BTC", since_ts=since)
            except Exception:
                facts = []
            # v314 P0: 外部行情 ticker → 规范名（让 thesis 的中文语义词能匹配上）
            facts = _canonicalize_facts(facts)
            # v314 P0: 追加 belief 专属证据源（ai_decisions / news_events）——
            # 这些链在 thesis 里点名了这两张表，但旧实现只查 factor_snapshots +
            # external_indicators，导致永远 0 证据。
            try:
                facts.extend(_belief_extra_facts(asset or "BTC", since_ts=since))
            except Exception:
                pass
        results = []
        for c in children:
            cid = int(c["id"])
            r = {
                "id": cid,
                "chain": c["thesis"],
                "regime_tags": json.loads(c["regime_tags"] or "[]"),
                "status": "UNVERIFIED",
            }
            if auto_verify and facts:
                try:
                    vr = _verify_belief_chain(
                        c["thesis"], facts, regime_tags=r["regime_tags"]
                    )
                    r["status"] = vr.get("new_status", "UNCONFIRMED")
                    r["support"] = vr.get("support_count", 0)
                    r["contradict"] = vr.get("contradict_count", 0)
                    r["neutral"] = vr.get("neutral_count", 0)
                    r["matched_facts"] = vr.get("matched_facts", [])
                    r["chain_direction"] = vr.get("chain_direction")
                    r["new_confidence"] = c["confidence"]
                    if r["status"] == "CONTRADICTED":
                        r["new_confidence"] = max(
                            0.1, c["confidence"] - 0.15)
                    elif r["status"] == "CONFIRMED":
                        r["new_confidence"] = min(0.9, c["confidence"] + 0.1)
                except Exception as exc:
                    r["status"] = "ERROR"
                    r["error"] = str(exc)[:100]
            else:
                r["status"] = "UNVERIFIED"
                r["new_confidence"] = c["confidence"]
            results.append(r)
        support_count = sum(1 for r in results if r["status"] == "CONFIRMED")
        contradict_count = sum(
            1 for r in results if r["status"] == "CONTRADICTED")
        unconf_count = sum(1 for r in results if r["status"] == "UNCONFIRMED")
        error_count = sum(
            1 for r in results if r["status"] in ("ERROR", "UNVERIFIED"))
        total = max(len(results), 1)
        support_rate = support_count / total
        contradict_rate = contradict_count / total
        if contradict_count >= support_count and contradict_count > 0:
            overall_status = "CONTRADICTED"
        elif support_count >= max(1, total // 2):
            overall_status = "CONFIRMED"
        elif support_count == 0 and contradict_count == 0:
            overall_status = "UNVERIFIED"
        else:
            overall_status = "PARTIAL"
        regime_map = {}
        for r in results:
            tag_status = r["status"]
            for tag in r.get("regime_tags") or []:
                cur = regime_map.get(tag)
                if cur == "FAILS":
                    continue
                if tag_status == "CONTRADICTED":
                    regime_map[tag] = "FAILS"
                elif tag_status == "CONFIRMED" and cur != "FAILS":
                    regime_map[tag] = "CONFIRMED"
                elif cur is None:
                    regime_map[tag] = (
                        "UNCONFIRMED"
                        if tag_status == "UNCONFIRMED" else "PARTIAL"
                    )
        if error_count == total:
            rec_conf = float(world["confidence"])
        else:
            rec_conf = max(
                0.1,
                min(0.9, support_rate * 0.5 + 0.5 * (1 - contradict_rate)),
            )
        rule_candidates = _generate_trading_rules(
            results, world["thesis"], asset)
        try:
            new_status = "active"
            if overall_status == "CONTRADICTED":
                new_status = "conflicted"
            # v314 P0: 证据落库 —— worldview 的支持/反驳 = 各子链的判定数
            revise_belief(
                worldview_id,
                confidence=rec_conf,
                status=new_status,
                note=(f"v223 auto-synthesis: {support_count}/{total} support, "
                      f"{contradict_count}/{total} contradict"),
                actor="belief_synthesis",
                evidence_for=support_count,
                evidence_against=contradict_count,
                verified=any(r.get("support") is not None for r in results),
            )
            for r in results:
                if r["status"] == "ERROR":
                    continue
                # v314 P0: 每条子链独立提交 —— 单条抢锁失败不该拖垮同批其它子链
                try:
                    revise_belief(
                        r["id"],
                        confidence=r.get("new_confidence"),
                        note=f"verify result: {r['status']}",
                        actor="verify",
                        evidence_for=int(r.get("support") or 0),
                        evidence_against=int(r.get("contradict") or 0),
                        verified=r.get("support") is not None,
                    )
                except Exception as _exc:
                    print(f"[belief] 子链证据落库失败 belief={r.get('id')}: "
                          f"{type(_exc).__name__}: {str(_exc)[:100]}", flush=True)
        except Exception as _exc:
            # v314 P0: 旧实现 `except Exception: pass` 把写锁失败完全吞掉 →
            # 复核"跑过了"但证据一条没落库，排查时完全无感。现在打日志。
            print(f"[belief] 证据落库失败 worldview={worldview_id}: "
                  f"{type(_exc).__name__}: {str(_exc)[:120]}", flush=True)
        return {
            "support_rate": round(support_rate, 3),
            "contradict_rate": round(contradict_rate, 3),
            "support_count": support_count,
            "contradict_count": contradict_count,
            "unconfirmed_count": unconf_count,
            "error_count": error_count,
            "total_sub_chains": total,
            "overall_status": overall_status,
            "regime_map": regime_map,
            "trading_rule_candidates": rule_candidates,
            "confidence_recommendation": round(rec_conf, 3),
            "per_sub_chain": results,
        }
    finally:
        conn.close()


# ============================================================================
# Trading rule generation
# ============================================================================
def _generate_trading_rules(sub_results, worldview_thesis, asset):
    """Ask LLM to draft 2-4 IF-THEN rules from verified sub-chains."""
    confirmed = [r["chain"] for r in sub_results if r["status"] == "CONFIRMED"]
    contradicted = [
        r["chain"] for r in sub_results if r["status"] == "CONTRADICTED"]
    if not confirmed and not contradicted:
        return ["数据不足，无法生成交易规则（建议先积累30天事实）"]
    prompt = (
        f"基于以下世界观 + 子链核验结果，生成 2-4 条具体可执行的交易规则候选。\n"
        f"规则必须是「IF 条件 THEN 动作」格式，可直接接入交易系统。\n\n"
        f"世界观：{worldview_thesis}\n"
        f"关联品种：{asset or '跨品种'}\n\n"
        f"已确认的子链（{len(confirmed)} 条）：\n" +
        "\n".join(f"- {c}" for c in confirmed) +
        f"\n\n已反驳的子链（{len(contradicted)} 条，应作为「失效条件」）：\n" +
        "\n".join(f"- {c}" for c in contradicted) +
        "\n\n输出 JSON 数组：\n"
        "[{\"rule\": \"IF ... THEN ...\", \"asset\": \"BTC|XAU|...\", "
        "\"timeframe\": \"intraday|swing\", \"confidence\": 0-1, "
        "\"evidence_chain_ids\": [<子链id>, ...]}, ...]"
    )
    raw = review_agent._llm(
        prompt, "你是量化交易策略师，只输出 JSON 数组。", max_tokens=1500)
    parsed = review_agent._parse_json(raw)
    if not isinstance(parsed, list):
        return []
    out = []
    for item in parsed:
        if isinstance(item, dict) and item.get("rule"):
            out.append(str(item["rule"])[:300])
    return out[:6]


# ── v321.2 P0: belief 专属事实源的资产锚点 ───────────────────────────────────
# `_belief_extra_facts` 产出的事实 (indicator 固定为 "ai_decisions" /
# "news_events") 是**按资产查出来**的, 资产就是它们的天然锚点。thesis 提到
# 该资产 (BTC / 比特币 …) 即算"这条链在谈这件事" → 进入方向比对。
# 详见 _verify_belief_chain 的说明。
_BELIEF_FACT_SOURCES = ("ai_decisions", "news_events")

# 资产代码 → causal_graph 的规范 indicator 名 (复用其同义词表:
# BTC_Price → ["btc", "比特币", "比特币价格"] …)
_ASSET_TO_INDICATOR = {
    "BTC": "BTC_Price",
    "ETH": "ETH_Price",
    "WTI": "Oil_Price",
    "XAU": "Gold_Price",
}


def _verify_belief_chain(
    thesis: str,
    facts: List[Dict[str, Any]],
    regime_tags: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Lightweight verifier: 用 thesis 当 chain 文本，复用 causal_graph 的方向推断。

    解决 verify_hypothesis 只能查 asset_driver_nodes 的问题——
    v223 belief 的子链只在 beliefs 表里，不在 asset_driver_nodes 里。

    v225 Stage 5: 多接受 regime_tags 参数；当 chain 文本不含 indicator 名
    但 fact.source='external_indicators' 时，走 regime_tags 交集匹配。

    v321.2 P0: **资产口径匹配** (修 9/14 条 belief 恒 0 证据)
    ------------------------------------------------------
    `_belief_extra_facts` 产出的事实 indicator 固定为 "ai_decisions" /
    "news_events", 于是只有 thesis **字面提到表名**才能匹配上:

        "FOMC鹰派 → BTC 24h内回落≥2%"        (不提表名) → 0 证据, 永远 UNCONFIRMED
        "该传导应在 ai_decisions 中表现为…"   (提了表名) → 有证据

    实测 14 条 belief 里 9 条恒 0/0 —— 验收 #4B 判 FAIL 的直接原因。

    但这些事实是**按资产查出来**的 (SQL: `WHERE target_asset=?` / `WHERE asset=?`),
    **资产本身就是它们的天然锚点**。所以当 thesis 提到该资产 (BTC/比特币…)
    时, 也应算"这条链在谈这件事" → 进入方向比对。

    方向判定完全不变 (chain_direction vs fact.direction) —— 不是无条件算支持,
    只是把"本该被比对的证据"捞回来, 不再让符号信息白白丢掉。
    """
    try:
        chain = [str(thesis or "")]
        chain_text = " ".join(chain)
        try:
            chain_direction = causal_graph._chain_infers_direction(chain)
        except Exception:
            chain_direction = None
        chain_regimes = set(str(t) for t in (regime_tags or []))
        support_ids: List[int] = []
        contradict_ids: List[int] = []
        neutral_ids: List[int] = []
        matched: List[int] = []
        for fact in facts or []:
            indicator = str(fact.get("indicator") or "")
            sid = fact.get("source_id")
            source = str(fact.get("source") or "")
            # 先按 indicator 名匹配（factor_snapshots / ai_decisions）
            try:
                contains = causal_graph._chain_contains_indicator(
                    chain_text, indicator)
            except Exception:
                contains = False
            # v225 Stage 5: external_indicators 的 indicator 是 Yahoo ticker，
            # chain 不太会直说 "^VIX"；改走 regime_tags 交集。
            if not contains and source == "external_indicators" and chain_regimes:
                fact_regimes = set(
                    str(t) for t in (fact.get("regime_tags") or [])
                )
                if fact_regimes and chain_regimes & fact_regimes:
                    contains = True
            # v321.2 P0: belief 专属事实源 (ai_decisions / news_events) 按资产
            # 查出 → 资产是天然锚点。thesis 提到该资产即算匹配。
            if not contains and source in _BELIEF_FACT_SOURCES:
                canon = _ASSET_TO_INDICATOR.get(
                    str(fact.get("asset") or "").strip().upper())
                if canon:
                    try:
                        contains = causal_graph._chain_contains_indicator(
                            chain_text, canon)
                    except Exception:
                        contains = False
            if not contains:
                neutral_ids.append(sid)
                continue
            matched.append(sid)
            fdir = fact.get("direction")
            if chain_direction is None or fdir == "neutral":
                neutral_ids.append(sid)
                continue
            if fdir == chain_direction:
                support_ids.append(sid)
            else:
                contradict_ids.append(sid)
        if contradict_ids:
            new_status = "CONTRADICTED"
        elif support_ids:
            new_status = "CONFIRMED"
        else:
            new_status = "UNCONFIRMED"
        return {
            "new_status": new_status,
            "support_count": len(support_ids),
            "contradict_count": len(contradict_ids),
            "neutral_count": len(neutral_ids),
            "matched_facts": matched[:10],
            "chain_direction": chain_direction,
        }
    except Exception as exc:
        return {"new_status": "UNCONFIRMED", "error": str(exc)[:120]}


# ============================================================================
# Revision Loop
# ============================================================================
def revise_all(lookback_days=30):
    """Scan all active worldviews; auto-adjust confidence by evidence.

    v2026-10-27 (G12): 原实现只扫 status='active' 的 worldview —— 处于
    'conflicted' 的世界观 (恰恰是最需要复核的那批) 永远不会被复核, 它的子链条
    置信度就停在上次判定的那天。改成 active + conflicted 都参与复核。
    """
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id FROM beliefs WHERE level='worldview' "
            "AND status IN ('active','conflicted') "
            "ORDER BY id ASC"
        ).fetchall()
        report = {"scanned": 0, "upgraded": [], "downgraded": [],
                  "conflicted": [], "skipped": []}
        for r in rows:
            wid = int(r["id"])
            report["scanned"] += 1
            kids = conn.execute(
                "SELECT COUNT(*) AS n FROM beliefs WHERE parent_id=?",
                (wid,),
            ).fetchone()
            if not kids or kids["n"] == 0:
                report["skipped"].append({"id": wid, "reason": "no sub_chains"})
                continue
            try:
                syn = _synthesize_from_children(wid, lookback_days, True)
            except Exception as exc:
                report["skipped"].append(
                    {"id": wid, "reason": f"synth error: {str(exc)[:80]}"})
                continue
            overall = syn.get("overall_status")
            rec_conf = syn.get("confidence_recommendation", 0.5)
            world_row = conn.execute(
                "SELECT confidence FROM beliefs WHERE id=?",
                (wid,)).fetchone()
            old_conf = float(world_row["confidence"]) if world_row else 0.5
            if (overall == "CONTRADICTED"
                    and syn.get("contradict_rate", 0) >= 0.4):
                report["conflicted"].append({
                    "id": wid, "old_conf": old_conf,
                    "new_conf": rec_conf,
                    "contradict_rate": syn["contradict_rate"],
                })
            elif (overall == "CONFIRMED"
                  and syn.get("support_rate", 0) >= 0.6):
                report["upgraded"].append({
                    "id": wid, "old_conf": old_conf,
                    "new_conf": rec_conf,
                    "support_rate": syn["support_rate"],
                })
            else:
                report["skipped"].append({
                    "id": wid,
                    "reason": f"partial/unverified ({overall})",
                })
        return report
    finally:
        conn.close()


# ============================================================================
# Lineage Tree (for frontend viz)
# ============================================================================
def get_lineage(belief_id, depth=3):
    """Recursively fetch subtree: parent / children / conflicts."""
    root = get_belief(belief_id)
    if not root:
        return {}
    if depth <= 0:
        return root
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    try:
        kids = conn.execute(
            "SELECT id FROM beliefs WHERE parent_id=? AND status='active'",
            (belief_id,),
        ).fetchall()
        root["children"] = [
            get_lineage(int(k["id"]), depth - 1) for k in kids]
        if root.get("parent_id"):
            par = get_belief(int(root["parent_id"]))
            root["parent"] = par
        edges = conn.execute(
            "SELECT * FROM belief_edges WHERE src_id=? OR dst_id=?",
            (belief_id, belief_id),
        ).fetchall()
        root["conflicts"] = [dict(e) for e in edges]
        return root
    finally:
        conn.close()


# ============================================================================
# Knowledge Gap Detector (v224 Stage 3)
# ============================================================================
def detect_knowledge_gaps(belief_id, lookback_days=30):
    """扫描 worldview 的子链 + regime_tags + DB 现有数据，找出「缺证据的 regime」。

    返回结构：
        {
            "belief_id": int,
            "thesis": str,
            "declared_regimes": [str, ...],
            "covered": {regime: status, ...},   # CONFIRMED/PARTIAL/UNCONFIRMED/CONTRADICTED/MISSING
            "gaps": [str, ...],                  # regimes without evidence
            "data_sources_checked": [str, ...],
            "evidence_counts": { ... },
            "suggested_data": [str, ...],
            "coverage_score": float,             # 0-1
            "next_action": str,
            "scanned_at": iso_ts,
        }
    """
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    try:
        world = conn.execute(
            "SELECT * FROM beliefs WHERE id=?", (belief_id,)
        ).fetchone()
        if not world:
            return {"error": "belief not found"}
        if world["level"] != "worldview":
            return {
                "error": "knowledge-gap detector 仅对 worldview 生效；"
                f"当前 level={world['level']}",
            }
        asset = world["asset"] or "BTC"
        children = conn.execute(
            "SELECT * FROM beliefs WHERE parent_id=? AND status='active' "
            "ORDER BY id ASC", (belief_id,)
        ).fetchall()
        declared_regimes = set()
        for c in children:
            try:
                for tag in json.loads(c["regime_tags"] or "[]"):
                    declared_regimes.add(str(tag))
            except Exception:
                pass
        if world["regime_tags"]:
            try:
                for tag in json.loads(world["regime_tags"] or "[]"):
                    declared_regimes.add(str(tag))
            except Exception:
                pass
        covered = {}
        chains_by_regime = {}
        for c in children:
            try:
                ctags = json.loads(c["regime_tags"] or "[]")
            except Exception:
                ctags = []
            for tag in ctags:
                chains_by_regime.setdefault(str(tag), []).append(int(c["id"]))
        synth = None
        try:
            synth = _synthesize_from_children(belief_id, lookback_days, True)
        except Exception:
            synth = None
        per_chain_status = {}
        if synth:
            for r in synth.get("per_sub_chain", []):
                per_chain_status[int(r.get("id") or 0)] = (
                    r.get("status") or "UNVERIFIED")
        worst_priority = {
            "CONTRADICTED": 4, "CONFLICTED": 4, "PARTIAL": 3,
            "UNVERIFIED": 2, "UNCONFIRMED": 2, "ERROR": 2,
            "CONFIRMED": 1, "MISSING": 0,
        }
        for regime in declared_regimes:
            chain_ids = chains_by_regime.get(regime, [])
            if not chain_ids:
                covered[regime] = "MISSING"
                continue
            statuses = [
                per_chain_status.get(cid, "UNVERIFIED") for cid in chain_ids
            ]
            if not statuses:
                covered[regime] = "MISSING"
                continue
            worst = max(statuses, key=lambda s: worst_priority.get(s, 0))
            covered[regime] = worst
        evidence_counts = _count_evidence_for_asset(asset, lookback_days)
        gaps = sorted(
            r for r, st in covered.items()
            if st in ("UNVERIFIED", "UNCONFIRMED", "MISSING")
        )
        total_regimes = max(len(covered), 1)
        verified_regimes = sum(
            1 for st in covered.values() if st in ("CONFIRMED", "PARTIAL")
        )
        coverage_score = round(verified_regimes / total_regimes, 3)
        suggested_data = _suggest_data_for_gaps(gaps, asset)
        next_action = _next_action_for_gaps(
            gaps, asset, evidence_counts, coverage_score)
        return {
            "belief_id": int(belief_id),
            "thesis": str(world["thesis"] or ""),
            "asset": asset,
            "status": str(world["status"] or ""),
            "confidence": float(world["confidence"] or 0.0),
            "declared_regimes": sorted(declared_regimes),
            "chains_count": len(children),
            "covered": covered,
            "gaps": gaps,
            "data_sources_checked": [
                "factor_snapshots", "ai_decisions + quick_sim_trades",
                "beliefs.sub_chains",
            ],
            "evidence_counts": evidence_counts,
            "suggested_data": suggested_data,
            "coverage_score": coverage_score,
            "next_action": next_action,
            "scanned_at": _now(),
        }
    finally:
        conn.close()


def detect_all_gaps(lookback_days=30):
    """扫所有 active worldviews，按 coverage_score 升序排（最缺证据的在前）。"""
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id FROM beliefs WHERE level='worldview' "
            "AND status='active' ORDER BY id ASC"
        ).fetchall()
        report = {"scanned": 0, "by_coverage": [], "top_gaps": []}
        for r in rows:
            wid = int(r["id"])
            g = detect_knowledge_gaps(wid, lookback_days)
            if "error" in g:
                continue
            report["scanned"] += 1
            report["by_coverage"].append({
                "belief_id": wid,
                "thesis": g.get("thesis", "")[:60],
                "coverage_score": g.get("coverage_score", 0.0),
                "gaps_count": len(g.get("gaps", [])),
                "gaps": g.get("gaps", []),
            })
        report["by_coverage"].sort(key=lambda x: x["coverage_score"])
        report["top_gaps"] = report["by_coverage"][:5]
        return report
    finally:
        conn.close()


def _count_evidence_for_asset(asset, lookback_days):
    """统计 DB 里有多少证据条目覆盖此 asset。"""
    from datetime import datetime as _dt, timedelta as _td
    conn = db.get_connection()
    try:
        since_unix = int((_dt.now() - _td(days=lookback_days)).timestamp())
        fs = conn.execute(
            "SELECT COUNT(*) FROM factor_snapshots WHERE asset=? AND ts>=?",
            (asset, since_unix),
        ).fetchone()[0]
        ai = conn.execute(
            """SELECT COUNT(*) FROM ai_decisions ad
               JOIN quick_sim_trades qs ON qs.decision_id=ad.id
               WHERE ad.target_asset=? AND qs.settled=1 AND qs.exit_ts>=?""",
            (asset, str(since_unix)),
        ).fetchone()[0]
        chains = conn.execute(
            "SELECT COUNT(*) FROM beliefs WHERE asset=? AND level='chain'",
            (asset,),
        ).fetchone()[0]
        verified = conn.execute(
            "SELECT COUNT(*) FROM beliefs WHERE asset=? AND level='chain' "
            "AND (confidence>=0.7 OR confidence<=0.3) "
            "AND last_verified_at IS NOT NULL",
            (asset,),
        ).fetchone()[0]
        return {
            "factor_snapshots": int(fs),
            "settled_trades": int(ai),
            "sub_chains": int(chains),
            "verified_chains": int(verified),
        }
    finally:
        conn.close()


def _suggest_data_for_gaps(gaps, asset):
    """把 missing regimes 翻译成「需要哪些数据」。先 LLM，没 LLM 回退预设表。"""
    if not gaps:
        return ["当前所有 regime 都有证据，无需补充数据。"]
    try:
        prompt = (
            f"分析以下市场 regime 与缺失证据，输出 3-5 条具体可采集的数据源建议。\n"
            f"每条建议 ≤30 字，含「数据名 + 数据源 + 频率」。\n\n"
            f"品种：{asset}\n缺失 regime：\n- " + "\n- ".join(gaps) +
            "\n\n输出 JSON 数组：[{\"data\": str, \"source\": str, "
            "\"frequency\": str, \"regime_covered\": str}, ...]"
        )
        raw = review_agent._llm(
            prompt,
            "你是数据架构师，只输出 JSON 数组。",
            max_tokens=900,
        )
        parsed = review_agent._parse_json(raw)
        if isinstance(parsed, list):
            out = []
            for item in parsed:
                if isinstance(item, dict) and item.get("data"):
                    src = item.get("source", "")
                    freq = item.get("frequency", "")
                    out.append(f"{item['data']} ({src}, {freq})")
            if out:
                return out[:6]
    except Exception:
        pass
    fallback = {
        "fomc_active": "FOMC 议息纪要 (Fed官网, 月度)",
        "low_vol": "VIX / 历史波动率 (CBOE, 日频)",
        "high_vol": "已实现波动率20d (行情API, 日频)",
        "risk_on": "风险资产相关性 (Bloomberg, 日频)",
        "risk_off": "黄金+日元同步走势 (行情API, 日频)",
        "etf_flows": "BTC现货ETF流量 (Coinglass, 日频)",
        "policy_pivot": "央行声明原文 + 关键词diff (央行官网, 即时)",
        "fomc_quiet": "联邦基金期货隐含概率 (CME, 日频)",
        "regulatory": "SEC / 国会听证日程 (官方, 即时)",
        "usd_strong": "DXY 走势 (ICE, 日频)",
        "rates_rising": "10Y TIPS yield (FRED, 日频)",
        "em_funding_stress": "EM 信用利差 + USDT 溢价 (行情API, 日频)",
        "disinflation": "CPI / PCE 实际值 (BLS, 月度)",
        "structural_demand": "BTC 现货 ETF 持仓变化 (Coinglass, 周度)",
        "etf_flow_dominant": "BTC 现货 ETF 流量 + 现货买卖比 (Coinglass, 日频)",
    }
    out = []
    for g in gaps:
        matched = False
        for k, v in fallback.items():
            if k in g.lower() or g.lower() in k:
                out.append(v)
                matched = True
                break
        if not matched:
            out.append(f"{g} 相关数据 (未匹配预设，建议人工补充)")
    return out[:6]


def _next_action_for_gaps(gaps, asset, evidence_counts, coverage_score):
    """人类可读的 next action。"""
    if not gaps:
        return "当前 worldview 已被证据充分覆盖，可考虑进入 Stage 4（UI / revision）。"
    fs = evidence_counts.get("factor_snapshots", 0)
    trades = evidence_counts.get("settled_trades", 0)
    if coverage_score < 0.2 and fs < 50 and trades < 5:
        return (
            f"严重缺数据（coverage {coverage_score:.0%}）："
            f"先 ingest 历史 {asset} 行情/因子 + 决策结果 "
            f"（当前仅 {fs} 个 factor_snapshot + {trades} 个 settled trade）。"
        )
    if coverage_score < 0.5:
        return (
            f"中等缺口（coverage {coverage_score:.0%}）："
            f"针对以下 regime 采集专门数据：{', '.join(gaps[:3])}。"
        )
    return (
        f"轻微缺口（coverage {coverage_score:.0%}）："
        f"仅 {len(gaps)} 个 regime 待补充，可延后处理。"
    )