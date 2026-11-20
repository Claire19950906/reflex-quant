# -*- coding: utf-8 -*-
"""v2 能力扩展（只读 + 旁路，不碰下单链路）

三件事：
  1. 问账本（在 nlq.py 里，新增 pnl_why / drawdown 两个白名单口径）
  2. 情景树：给定事件 → 三条路径、概率、对持仓影响、应对动作
  3. 研报摄入：PDF/文本 → 结构化假设、信号、风险、标签 → 入库待回测

设计约束：
  - LLM 只做「结构化推演/抽取」，数字与持仓来自库，不许模型编；
  - LLM 不可用时返回 rule 模板并显式标注 source=rule，不假装是模型结论；
  - 不写交易表、不改策略参数，只写 research_notes（自己的表）。
"""
from __future__ import annotations

import base64
import io
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

MAX_TEXT_CHARS = 30000


# ── 基础设施 ─────────────────────────────────────────────────────────────────
def _conn():
    import db as _db
    return _db.get_connection()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    """模型偶尔会带 <think>、```json 或前后解释，这里剔掉思维块后取第一个可解析对象。"""
    if not text:
        return None
    text = re.sub(r"<think[^>]*>.*?</think>", "", text, flags=re.S | re.I)
    text = re.sub(r"</think>", "", text, flags=re.I)
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

    # 优先取 ```json 围栏
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if fenced:
        try:
            return json.loads(fenced.group(1))
        except Exception:
            pass

    # 依次尝试每个 '{'，取第一个能解析成 dict 的平衡块
    for start in [i for i, ch in enumerate(text) if ch == "{"]:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[start:i + 1])
                    except Exception:
                        break
                    if isinstance(obj, dict):
                        return obj
                    break
    return None


def _llm_json(system: str, user: str, max_tokens: int = 2400) -> Optional[Dict[str, Any]]:
    import logging
    log = logging.getLogger("trident.capabilities")
    try:
        import openai
        import config
        m = config.pick_model_for_purpose("advisor")
        key = str(m.get("api_key") or "").strip()
        if not key:
            log.warning("llm_json: no api key")
            return None
        client = openai.OpenAI(
            base_url=str(m["api_base"]).rstrip("/"),
            api_key=key,
            timeout=float(m.get("timeout") or 60.0),
        )
        extra = dict(m.get("extra_body") or {})
        extra.pop("max_tokens", None)
        extra.setdefault("thinking", {"type": "disabled"})
        resp = client.chat.completions.create(
            model=str(m.get("request_model") or m.get("id")),
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
            temperature=0.2,
            max_tokens=max_tokens,
            extra_body=extra,
        )
        text = resp.choices[0].message.content or ""
        data = _extract_json(text)
        if data is None:
            log.warning("llm_json: unparsable text head=%r len=%d", text[:80], len(text))
        return data
    except Exception as exc:
        log.warning("llm_json failed: %s: %s", type(exc).__name__, str(exc)[:200])
        return None


# ── 上下文：持仓与日历 ───────────────────────────────────────────────────────
def open_positions(limit: int = 30) -> List[Dict[str, Any]]:
    try:
        conn = _conn()
        rows = conn.execute(
            """SELECT asset, action, notional_usdt, entry_price, entry_ts, strategy_id
                 FROM quick_sim_trades WHERE settled=0
                ORDER BY entry_ts DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        conn.close()
    except Exception:
        return []
    out = []
    for r in rows:
        out.append({
            "asset": r[0], "side": r[1],
            "notional_usdt": r[2], "entry_price": r[3],
            "entry_ts": r[4], "strategy_id": r[5],
        })
    return out


def upcoming_events(days: int = 7, limit: int = 12) -> List[Dict[str, Any]]:
    since = int(datetime.now(timezone.utc).timestamp())
    until = since + max(1, days) * 86400
    try:
        conn = _conn()
        rows = conn.execute(
            """SELECT country, title, ts, impact, forecast, previous
                 FROM econ_events WHERE ts >= ? AND ts <= ?
                ORDER BY ts ASC LIMIT ?""",
            (since, until, limit),
        ).fetchall()
        conn.close()
    except Exception:
        return []
    return [{"country": r[0], "title": r[1],
             "time": datetime.fromtimestamp(int(r[2]), timezone.utc).strftime("%m-%d %H:%M"),
             "impact": r[3], "forecast": r[4], "previous": r[5]} for r in rows]


# ── 情景树 ───────────────────────────────────────────────────────────────────
_SCENARIO_SYSTEM = """你是量化交易系统的情景推演器。只输出 JSON，不要解释、不要 Markdown。
要求：
1) 给三条互斥路径（覆盖超预期/符合/低于预期，或事件不发生），概率相加约等于 1；
2) impacts 只写事件对已给持仓品种的方向与大致幅度区间，幅度必须给区间，不许给单点精确值；
3) actions 是现在就能执行的动作（减仓比例、止损上移、暂停开新仓等），不写空话；
4) 没有持仓或没有数据时，impacts 留空数组，并在 assumptions 里说明缺什么；
5) 只基于用户给的事实推演，不许编造未给出的数据、价格或新闻。"""

_SCENARIO_SCHEMA = {
    "event": "事件原文",
    "as_of": "YYYY-MM-DD HH:MM",
    "paths": [
        {"name": "路径名", "probability": 0.33,
         "triggers": ["可观察的触发信号"],
         "impacts": [{"asset": "BTC", "direction": "down", "range": "-5% ~ -8%", "note": "一句话机制"}],
         "actions": ["可执行动作"]}
    ],
    "watch": ["开盘后要盯的指标或价位"],
    "confidence": "high|medium|low",
    "assumptions": ["缺什么数据、哪一步是假设"],
}


def _rule_scenario(event: str, positions: List[Dict[str, Any]]) -> Dict[str, Any]:
    """模型不可用时的结构模板：只给框架，不伪造概率与幅度。"""
    return {
        "event": event,
        "as_of": _now(),
        "paths": [
            {"name": "强于预期", "probability": None,
             "triggers": ["实际值高于预期", "表态偏鹰/偏紧"],
             "impacts": [], "actions": ["暂停开新仓，等第一根 5 分钟收线", "已有浮盈单止损上移"]},
            {"name": "符合预期", "probability": None,
             "triggers": ["实际值落在预期附近", "市场反应平淡"],
             "impacts": [], "actions": ["按原计划执行", "不追第一波"]},
            {"name": "弱于预期", "probability": None,
             "triggers": ["实际值低于预期", "表态偏鸽/偏松"],
             "impacts": [], "actions": ["等回踩确认再进", "止损按原计划不动"]},
        ],
        "watch": ["事件公布后 5/15 分钟成交量与价差", "关键前高前低是否被扫"],
        "confidence": "low",
        "assumptions": ["模型通道不可用，这里只给结构模板；概率与影响幅度需人工填。",
                        f"当前未平仓 {len(positions)} 笔。"],
        "source": "rule",
    }


def scenario_tree(event: str, horizon_days: int = 3, use_llm: bool = True) -> Dict[str, Any]:
    event = (event or "").strip()
    if not event:
        raise ValueError("event 不能为空")
    positions = open_positions()
    calendar = upcoming_events(horizon_days)
    if not use_llm:
        return _rule_scenario(event, positions)

    user = json.dumps({
        "event": event,
        "horizon_days": horizon_days,
        "open_positions": positions,
        "calendar": calendar,
        "schema": _SCENARIO_SCHEMA,
    }, ensure_ascii=False)
    data = _llm_json(_SCENARIO_SYSTEM, user)
    if not data:
        import logging
        logging.getLogger("trident.capabilities").warning("scenario: llm returned no data")
        return _rule_scenario(event, positions)
    if not isinstance(data.get("paths"), list) or not data["paths"]:
        import logging
        logging.getLogger("trident.capabilities").warning(
            "scenario: bad shape keys=%s", list(data.keys())[:8])
        return _rule_scenario(event, positions)

    paths = []
    for p in data["paths"][:3]:
        if not isinstance(p, dict):
            continue
        paths.append({
            "name": str(p.get("name") or "未命名路径"),
            "probability": p.get("probability"),
            "triggers": [str(x) for x in (p.get("triggers") or [])][:4],
            "impacts": [x for x in (p.get("impacts") or []) if isinstance(x, dict)][:8],
            "actions": [str(x) for x in (p.get("actions") or [])][:5],
        })
    if not paths:
        return _rule_scenario(event, positions)

    return {
        "event": event,
        "as_of": _now(),
        "paths": paths,
        "watch": [str(x) for x in (data.get("watch") or [])][:5],
        "confidence": str(data.get("confidence") or "medium"),
        "assumptions": [str(x) for x in (data.get("assumptions") or [])][:5],
        "positions": positions,
        "calendar": calendar,
        "source": "llm",
    }


# ── 研报摄入 ─────────────────────────────────────────────────────────────────
_RESEARCH_SYSTEM = """你是量化研究助理。把研报/纪要压成可验证的结构，只输出 JSON，不要解释。
要求：
1) hypotheses 必须是可否证的陈述，falsifiable_by 写「什么数据出现就说明它错了」；
2) signals 只收原文明确给出的规则或参数，没给参数就写空对象，不许补；
3) 不许把相关性改写成因果；
4) 原文没有的数字、结论、标的，一律不写。"""

_RESEARCH_SCHEMA = {
    "title": "标题",
    "source": "机构/作者",
    "published_at": "YYYY-MM-DD 或空",
    "summary": "三句话以内",
    "hypotheses": [{"text": "可否证的陈述", "falsifiable_by": "反证条件"}],
    "signals": [{"name": "信号名", "rule": "原文规则", "params": {}, "expected": "预期效果"}],
    "risks": ["原文提到的风险"],
    "tags": ["品种/主题标签"],
}


def _decode_text(filename: str, content_b64: str) -> str:
    raw = base64.b64decode(content_b64)
    name = (filename or "").lower()
    if name.endswith(".pdf"):
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(raw))
        parts = []
        for page in reader.pages[:80]:
            try:
                parts.append(page.extract_text() or "")
            except Exception:
                continue
        return "\n".join(parts)
    return raw.decode("utf-8", errors="ignore")


def _rule_research(text: str, filename: str) -> Dict[str, Any]:
    clean = re.sub(r"\s+", " ", text).strip()
    return {
        "title": filename or "未命名研报",
        "source": "",
        "published_at": "",
        "summary": clean[:300] + ("…" if len(clean) > 300 else ""),
        "hypotheses": [],
        "signals": [],
        "risks": [],
        "tags": [],
        "source_kind": "rule",
        "chars": len(text),
    }


def ingest_research(filename: str, content_b64: str, use_llm: bool = True) -> Dict[str, Any]:
    text = _decode_text(filename, content_b64)
    text = (text or "").strip()
    if not text:
        raise ValueError("没抽出文字：可能是扫描版 PDF，需要先 OCR")
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS]

    data = None
    if use_llm:
        user = json.dumps({"filename": filename, "text": text, "schema": _RESEARCH_SCHEMA},
                          ensure_ascii=False)
        data = _llm_json(_RESEARCH_SYSTEM, user, max_tokens=2000)

    if not data:
        data = _rule_research(text, filename)
    else:
        data.setdefault("title", filename)
        data.setdefault("hypotheses", [])
        data.setdefault("signals", [])
        data.setdefault("risks", [])
        data.setdefault("tags", [])
        data["source_kind"] = "llm"
        data["chars"] = len(text)

    conn = _conn()
    try:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS research_notes (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   created_at TEXT NOT NULL,
                   filename TEXT,
                   title TEXT,
                   payload TEXT NOT NULL,
                   status TEXT DEFAULT '待回测')"""
        )
        cur = conn.execute(
            "INSERT INTO research_notes (created_at, filename, title, payload, status) VALUES (?,?,?,?,?)",
            (_now(), filename, str(data.get("title") or filename),
             json.dumps(data, ensure_ascii=False), "待回测"),
        )
        conn.commit()
        note_id = cur.lastrowid
    finally:
        conn.close()

    return {"ok": True, "id": note_id, "note": data}


def list_research(limit: int = 50) -> List[Dict[str, Any]]:
    conn = _conn()
    try:
        rows = conn.execute(
            """SELECT id, created_at, filename, title, status, payload
                 FROM research_notes ORDER BY id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    except Exception:
        return []
    finally:
        conn.close()
    out = []
    for r in rows:
        try:
            payload = json.loads(r[5])
        except Exception:
            payload = {}
        out.append({"id": r[0], "created_at": r[1], "filename": r[2],
                    "title": r[3], "status": r[4], "note": payload})
    return out


def get_research(note_id: int) -> Optional[Dict[str, Any]]:
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT id, created_at, filename, title, status, payload FROM research_notes WHERE id=?",
            (note_id,),
        ).fetchone()
    except Exception:
        return None
    finally:
        conn.close()
    if not row:
        return None
    try:
        payload = json.loads(row[5])
    except Exception:
        payload = {}
    return {"id": row[0], "created_at": row[1], "filename": row[2],
            "title": row[3], "status": row[4], "note": payload}
