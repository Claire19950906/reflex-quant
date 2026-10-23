"""Task A — FinancialJuice WebSocket Ingest (Centrifugo protocol)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from typing import Any, List

# 实时新闻过滤器 — ingest 阶段拦截垃圾新闻
from realtime_filter import evaluate_news
import db
import event_atom
import news_sources
import timeseries

from .utils import (
    _clean_html,
    _detect_vip,
    _is_content_junk,
    _now,
    _open_db,
    _ts,
)
from .webhook import _translate_if_english
from .ws_client import (
    _FJ_HEADERS,
    _StdlibWebSocket,
    _extract_centrifugo_config,
)


# ---------------------------------------------------------------------------
# v174 异步事件提取调度器
# ---------------------------------------------------------------------------
# 原 ingest 在主推送路径上同步跑 event_atom.extract_and_persist（LLM 调用,
# 30s+),同时持有 INSERT raw_news 的事务写锁 → 与 ai_worker BEGIN IMMEDIATE
# 互锁 30s/batch. 修复: 主路径 commit 后立即把 event_atom 调度到独立 daemon 线程,
# 用独立短事务处理,绝不阻塞主推送或 ai_worker claim.
# ---------------------------------------------------------------------------
import threading as _threading

def _sched_event_extraction(news_text: str, raw_news_id: int, ts_epoch: int, source: str) -> None:
    """后台线程跑事件提取: 独立连接 + 短事务,绝不阻塞主推送路径."""
    def _worker():
        try:
            _conn = _open_db()
            try:
                event_atom.extract_and_persist(
                    news_text,
                    raw_news_id=raw_news_id,
                    default_event_time=ts_epoch,
                    event_time_source="platform_published_at",
                    event_time_confidence=0.8,
                    connection=_conn,
                )
                _conn.commit()
            except Exception as _ea_err:
                print(f"  [WS-EVENT-ATOM] news={raw_news_id} skipped: {_ea_err}", flush=True)
            finally:
                _conn.close()
        except Exception:
            pass
    _threading.Thread(target=_worker, daemon=True).start()


# FinancialJuice WebSocket — real-time ingest
FJ_WS_URL = "wss://rt.financialjuice.com/connection/websocket"
FJ_ORIGIN = "https://www.financialjuice.com"
FJ_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
    " AppleWebKit/537.36 (KHTML, like Gecko)"
    " Chrome/131.0.0.0 Safari/537.36"
)


def _extract_news_text(msg: dict) -> str | None:
    """
    Walk a Centrifugo publication message and extract news text.

    FinancialJuice structure (from debug output):
      push.pub.data = {"ev":"sendUpdates", "msg":"[{...}, {...}]"}
      where msg is a JSON-stringified array of objects with:
        NewsID, Title (TitleCase!), Description, Tags, PostedShort, etc.

    We try lowercase and TitleCase field names, and parse nested JSON strings.
    """
    parts: List[str] = []

    def _try_parse_json(s: str) -> Any:
        """If s looks like JSON, parse it and return the result."""
        s = s.strip()
        if (s.startswith("[") and s.endswith("]")) or \
           (s.startswith("{") and s.endswith("}")):
            try:
                return json.loads(s)
            except (json.JSONDecodeError, TypeError):
                pass
        return s

    def _dig(d: Any) -> None:
        if isinstance(d, dict):
            # Try both lowercase and TitleCase field names
            for k in ("title", "Title", "headline", "Headline",
                       "body", "Body", "text", "Text",
                       "content", "Content",
                       "summary", "Summary",
                       "description", "Description",
                       "name", "Name", "message", "Message"):
                v = d.get(k)
                if isinstance(v, str) and v.strip():
                    parts.append(v.strip())
            # Recurse into nested objects
            for v in d.values():
                _dig(v)
        elif isinstance(d, list):
            for item in d:
                _dig(item)
        elif isinstance(d, str):
            # FinancialJuice nests data inside JSON strings (the "msg" field)
            parsed = _try_parse_json(d)
            if parsed is not d:
                _dig(parsed)


def _extract_pub_time(msg: dict) -> str:
    """从 Centrifugo/FJ 推送里解析 published_at（平台声称发布时间），用于写库。

    来源优先级：
      1. Centrifugo `pub.time` (Unix 秒，整数)
      2. FJ 嵌套对象的 `PostedShort` / `Posted` / `Time` / `Timestamp` / `published_at`
      3. 都拿不到 → 返回 ""（写库时 published_at 留空，让 ai 看到"采集失真"）
    """
    try:
        pub = msg.get("pub") if isinstance(msg, dict) else None
        # 1. Centrifugo pub.time (Unix 秒)
        if isinstance(pub, dict):
            t = pub.get("time")
            if isinstance(t, (int, float)) and t > 1_000_000_000:
                # 转为 ISO + 北京时间（北京时间 = UTC+8）
                from datetime import datetime, timezone, timedelta
                dt = datetime.fromtimestamp(int(t), tz=timezone.utc).astimezone(
                    timezone(timedelta(hours=8))
                )
                return dt.strftime("%Y-%m-%d %H:%M:%S")
        # 2. FJ 嵌套字段
        if isinstance(pub, dict):
            data = pub.get("data")
            if isinstance(data, dict):
                for k in ("PostedShort", "Posted", "Time", "Timestamp",
                           "published_at", "post_time"):
                    v = data.get(k)
                    if isinstance(v, str) and v.strip():
                        return v.strip()[:64]
            # 也可能在 pub 顶层
            for k in ("PostedShort", "Posted", "Time", "Timestamp", "published_at"):
                v = pub.get(k)
                if isinstance(v, str) and v.strip():
                    return v.strip()[:64]
        # 3. 兜底：消息顶层
        if isinstance(msg, dict):
            for k in ("PostedShort", "Posted", "Time", "Timestamp", "published_at"):
                v = msg.get(k)
                if isinstance(v, str) and v.strip():
                    return v.strip()[:64]
    except Exception:
        pass
    return ""


    _dig(msg)
    if not parts:
        return None
    # Deduplicate while preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for p in parts:
        if p not in seen:
            seen.add(p)
            unique.append(p)
    return ". ".join(unique)


# ── WebSocket helpers ────────────────────────────────────────────────────

async def _safe_close(ws: _StdlibWebSocket) -> None:
    """Gracefully close a WebSocket, ignoring any errors."""
    try:
        await ws.close()
    except Exception:
        pass


async def websocket_ingest(loop: asyncio.AbstractEventLoop) -> None:
    """
    Persistent WSS connection to FinancialJuice real-time feed.

    Protocol: Centrifugo (JSON pub/sub over raw WebSocket frames).
      1) Fetch centrifugoToken from homepage HTML
      2) WebSocket connect + TLS upgrade
      3) Send connect: {"connect": {"token": "<token>"}}
      4) Loop: receive text frames, parse JSON, extract news text, INSERT.

    Token is re-fetched on every reconnect to prevent expiry.
    """

    reconnect_delay = 5
    _MAX_RECONNECT_DELAY = 300  # v177: token 持续为空(FJ 已清空匿名 token)时指数退避, 避免 5s 无限刷屏

    while True:
        # ---- Fetch fresh config + token + cookies ------------------------
        cfg = await loop.run_in_executor(None, _extract_centrifugo_config)
        token = cfg.get("token")
        if not token:
            print(f"[INGEST] No token — retrying in {reconnect_delay}s ...")
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, _MAX_RECONNECT_DELAY)
            continue

        fresh_cookies = cfg.get("cookies", "")

        # ---- Connect with format #3 (the only one Centrifugo accepts) --------
        connect_payload = {"id": 1, "connect": {"token": token}}
        ok_connect_resp = None

        ws = _StdlibWebSocket()
        try:
            ws_headers = {
                "Origin": FJ_ORIGIN,
                "User-Agent": _FJ_HEADERS["User-Agent"],
            }
            if fresh_cookies:
                ws_headers["Cookie"] = fresh_cookies

            await ws.connect(FJ_WS_URL, extra_headers=ws_headers, timeout=15.0)
            msg = json.dumps(connect_payload)
            await ws.send_text(msg)

            raw_resp = await ws.recv_text(timeout=10.0)
            if raw_resp is None:
                code_info = ws.close_info or "no reason"
                print(f"[INGEST] Centrifugo rejected: {code_info} — retrying in {reconnect_delay}s ...")
                await _safe_close(ws)
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, _MAX_RECONNECT_DELAY)
                continue

            resp = json.loads(raw_resp.strip())
            if isinstance(resp, dict) and resp.get("error"):
                print(f"[INGEST] Centrifugo error: {resp['error']} — retrying in {reconnect_delay}s ...")
                await _safe_close(ws)
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, _MAX_RECONNECT_DELAY)
                continue

            # Success
            print(f"[INGEST] Connected — {_now()}")
            reconnect_delay = 5  # 成功连上后重置退避
            ok_connect_resp = resp
        except (ConnectionError, OSError, asyncio.TimeoutError) as exc:
            print(f"[INGEST] Connect error: {exc} — retrying in {reconnect_delay}s ...")
            await _safe_close(ws)
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, _MAX_RECONNECT_DELAY)
            continue

        # ws is now the winning connection — start message loop
        try:
            # Parse connect response to find available channels
            try:
                conn_resp = ok_connect_resp
                subs = conn_resp.get("connect", {}).get("subs", {})
            except Exception:
                subs = {}
            channels = list(subs.keys())
            if not channels:
                channels = ["feed:all", "feed:lite"]

            print(f"[INGEST] Listening on {len(channels)} channels ...")

            # ---- Message loop ------------------------------------------
            inserted_total = 0

            while True:
                raw = await ws.recv_text(timeout=300.0)
                if raw is None:
                    print(f"[INGEST] Connection ended ({_now()}) — inserted={inserted_total}")
                    break

                raw_s = raw.strip()

                # Centrifugo sends {} as application-level heartbeat (ping).
                if raw_s == "{}":
                    await ws.send_text("{}")
                    continue
                if raw_s in ("", "[]"):
                    continue

                # Centrifugo sends one JSON object per frame (no \x1e needed)
                try:
                    msg = json.loads(raw_s)
                except json.JSONDecodeError:
                    continue

                if not isinstance(msg, dict):
                    continue

                # Handle ping (server heartbeat — must reply pong)
                ping_val = msg.get("ping")
                if ping_val is not None:
                    pong = json.dumps({"pong": ping_val})
                    await ws.send_text(pong)
                    continue

                # Handle push messages
                push = msg.get("push")
                if isinstance(push, dict):
                    channel = push.get("channel", "")
                    pub = push.get("pub") or push.get("data") or push
                    text = _extract_news_text(pub)
                    if text:
                        text = _clean_html(text)
                        if not _is_content_junk(text) and not text.startswith("http"):
                            original_text = text

                            # ── 去重检查 (翻译前 — 省 API 调用) ──
                            h = hashlib.sha256(original_text.encode()).hexdigest()[:16]
                            def _check_dup(hash_val: str) -> bool:
                                conn = _open_db()
                                try:
                                    ex = conn.execute(
                                        "SELECT id FROM raw_news WHERE content LIKE ? LIMIT 1",
                                        (f"[hash:{hash_val}]%",),
                                    ).fetchone()
                                    return ex is not None
                                finally:
                                    conn.close()
                            if await loop.run_in_executor(None, _check_dup, h):
                                continue  # 已处理过, 跳过

                            # ═════════════════════════════════════════════════════════════════════
                            # PRE-TRANSLATOR INTERCEPTOR (WebSocket 入口)
                            # ═════════════════════════════════════════════════════════════════════
                            text = await _translate_if_english(original_text, loop)
                            if text != original_text:
                                print(f"  [WS-TRANSLATE] {original_text[:30]} → {text[:30]}")

                            def _insert_push() -> int | None:
                                conn = _open_db()
                                try:
                                    source_label = f"WS:fj:{channel}" if channel else "WS:financialjuice"
                                    if not news_sources.allow_ingest(source_label, conn):
                                        return None
                                    ts = _ts()
                                    # 优先用推送自身 time 字段作 ts_epoch（更接近真实发布时间）；
                                    # 拿不到则回退到入库时刻。published_at 同理：优先用推送
                                    # 解析出的发布时间，否则留空 → 让 ai 视为"采集失真"。
                                    pub_time_epoch = None
                                    try:
                                        _pub = push.get("pub") or {}
                                        if isinstance(_pub, dict):
                                            _t = _pub.get("time")
                                            if isinstance(_t, (int, float)) and _t > 1_000_000_000:
                                                pub_time_epoch = int(_t)
                                    except Exception:
                                        pass
                                    ts_epoch = pub_time_epoch if pub_time_epoch else int(time.time())
                                    published_at = _extract_pub_time(push)
                                    cleaned = f"[hash:{h}] {text[:1000]}"
                                    # v181: 把发布时间 + 入库时间戳传给门卫做 L0 过期检查
                                    f_result = evaluate_news(
                                        cleaned,
                                        published_at=published_at,
                                        ingested_ts=ts_epoch,
                                    )
                                    source_name = f"WS:fj:{channel}" if channel else "WS:financialjuice"
                                    news_id = db.insert_raw_news(
                                        conn,
                                        source=source_name,
                                        content=cleaned,
                                        timestamp=ts,
                                        status="PENDING",
                                        is_noise=int(f_result["is_noise"]),
                                        relevance_score=float(f_result["relevance_score"]),
                                        ts=ts_epoch,
                                        published_at=published_at,
                                        gate=f_result.get("gate"),
                                    )
                                    # v174 修复: 立即 commit raw_news,让 ai_worker 立刻能 claim.
                                    # event_atom (LLM 调用,30s+) 与 timeseries 异步处理,绝不阻塞主推送.
                                    conn.commit()
                                    # 后台异步: 用独立线程 + 短事务,不影响主推送持锁
                                    try:
                                        _sched_event_extraction(
                                            text, int(news_id), int(ts_epoch), source_name,
                                        )
                                    except Exception as _sched_err:
                                        pass
                                    return news_id
                                finally:
                                    conn.close()

                            rowid = await loop.run_in_executor(None, _insert_push)
                            if rowid is not None:
                                inserted_total += 1
                                print(
                                    f"\n  [{_now()}] INGEST #{rowid}"
                                    f" | {text[:80]}"
                                )
                    continue

                # Handle result (publication or system ack)
                result = msg.get("result")
                if isinstance(result, dict):
                    channel = result.get("channel", "")
                    data = result.get("data")

                    # Subscribe / unsubscribe ack — skip silently
                    if result.get("type") is not None:
                        continue

                    # Publication payload (less common path; most news comes via push)
                    if data is not None:
                        text = _extract_news_text(data)
                        if not text:
                            text = _extract_news_text(result)

                        if text:
                            text = _clean_html(text)
                            if not _is_content_junk(text) and not text.startswith("http"):
                                original_text = text

                                # ── 去重检查 (翻译前) ──
                                h = hashlib.sha256(original_text.encode()).hexdigest()[:16]
                                def _check_dup2(hash_val: str) -> bool:
                                    conn = _open_db()
                                    try:
                                        ex = conn.execute(
                                            "SELECT id FROM raw_news WHERE content LIKE ? LIMIT 1",
                                            (f"[hash:{hash_val}]%",),
                                        ).fetchone()
                                        return ex is not None
                                    finally:
                                        conn.close()
                                if await loop.run_in_executor(None, _check_dup2, h):
                                    continue

                                # ═════════════════════════════════════════════════════════════════════
                                # PRE-TRANSLATOR INTERCEPTOR (Result 分支)
                                # ═════════════════════════════════════════════════════════════════════
                                text = await _translate_if_english(original_text, loop)
                                if text != original_text:
                                    print(f"  [WS-TRANSLATE] {original_text[:30]} → {text[:30]}")

                                def _insert() -> int | None:
                                    conn = _open_db()
                                    try:
                                        ex = conn.execute(
                                            "SELECT id FROM raw_news"
                                            " WHERE content LIKE ? LIMIT 1",
                                            (f"[hash:{h}]%",),
                                        ).fetchone()
                                        if ex:
                                            return None
                                        vip_tag, _ = _detect_vip(text)
                                        ts = _ts()
                                        # 优先用推送自身 time 字段作 ts_epoch（更接近真实发布时间）；
                                        # 拿不到则回退到入库时刻。published_at 同理：优先用推送
                                        # 解析出的发布时间，否则留空 → 让 ai 视为"采集失真"。
                                        pub_time_epoch = None
                                        try:
                                            _res = result if isinstance(result, dict) else {}
                                            _t = _res.get("time")
                                            if isinstance(_t, (int, float)) and _t > 1_000_000_000:
                                                pub_time_epoch = int(_t)
                                        except Exception:
                                            pass
                                        ts_epoch = pub_time_epoch if pub_time_epoch else int(time.time())
                                        published_at = _extract_pub_time(result)
                                        source_label = f"WS:fj:{channel}" if channel else "WS:financialjuice"
                                        if vip_tag:
                                            source_label = f"{source_label} {vip_tag}"
                                        cleaned = f"[hash:{h}] {text[:1000]}"
                                        # v181: 把发布时间 + 入库时间戳传给门卫做 L0 过期检查
                                        f_result = evaluate_news(
                                            cleaned,
                                            published_at=published_at,
                                            ingested_ts=ts_epoch,
                                        )
                                        news_id = db.insert_raw_news(
                                            conn,
                                            source=source_label,
                                            content=cleaned,
                                            timestamp=ts,
                                            status="PENDING",
                                            is_noise=int(f_result["is_noise"]),
                                            relevance_score=float(f_result["relevance_score"]),
                                            ts=ts_epoch,
                                            published_at=published_at,
                                            gate=f_result.get("gate"),
                                        )
                                        conn.commit()
                                        return news_id
                                    finally:
                                        conn.close()

                                rowid = await loop.run_in_executor(None, _insert)
                                if rowid is not None:
                                    inserted_total += 1
                                    print(
                                        f"\n  [{_now()}] INGEST #{rowid}"
                                        f" | {text[:80]}"
                                    )
                        continue

            # If we exit the recv loop cleanly (server closed)
            print(f"[INGEST] Server closed ({_now()}) — inserted={inserted_total}")

        except (ConnectionError, OSError, asyncio.TimeoutError) as exc:
            print(f"[INGEST] Connection error: {exc}")
        except Exception as exc:
            print(f"[INGEST] Unexpected error: {type(exc).__name__}: {exc}")
        finally:
            try:
                await ws.close()
            except Exception:
                pass

        print(f"[INGEST] Reconnecting in {reconnect_delay}s ...")
        await asyncio.sleep(reconnect_delay)
