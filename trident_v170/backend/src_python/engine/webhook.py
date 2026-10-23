"""Pre-Translator Middleware (前置翻译拦截器)
+ Tree News / Telegram Webhook Listener.

Accepts POST JSON: {"text": "...", "source": "tree_news" | "telegram" | ...}
Allows third-party fast-news bots to inject into the Trident pipeline.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import ssl
import time
import urllib.request

from config import (
    DOUBAO_API_KEY,
    DOUBAO_BASE_URL,
    DOUBAO_MODEL,
    OPENROUTER_API_KEY,
    OPENROUTER_BASE_URL,
    MINIMAX_API_KEY,
    MINIMAX2_API_KEY,
    MINIMAX_BASE_URL,
    MINIMAX_MODEL,
    MINIMAX_USE_PROXY,
)

# 实时新闻过滤器 — ingest 阶段拦截垃圾新闻
from realtime_filter import evaluate_news
import db
import news_sources
import timeseries

from .utils import (
    _contains_chinese,
    _detect_vip,
    _is_content_junk,
    _now,
    _open_db,
    _ts,
)


# ---------------------------------------------------------------------------
# Pre-Translator Middleware (前置翻译拦截器)
# ---------------------------------------------------------------------------
# 在数据进入推演池前翻译英文标题，确保 6 模型推演时 100% 使用中文
# ---------------------------------------------------------------------------

def _is_english_text(text: str) -> bool:
    """
    激进英文检测 - 拦截一切可能的英文标题。
    多条件 OR 逻辑：满足任一条件即判定为英文。
    """
    t = text.strip()
    if not t or len(t) < 5:
        return False

    # ── 条件 1: 以英文关键词开头（如 BREAKING, NEWS, ALERT 等） ──
    english_prefixes = (
        "BREAKING", "NEWS", "ALERT", "UPDATE", "URGENT", "JUST IN",
        "FLASH", "Ticker", "Report", "Market", "Stock", "Crypto",
        "Fed", "SEC", "FOMC", "OPEC", "API", "ISM", "NFP", "CPI",
        "GDP", "PMI", "PCE", "NBER", "ECB", "BOE", "BOJ", "SNB"
    )
    words = re.split(r'[\s:，-]+', t.upper())
    if words and words[0] in english_prefixes:
        return True

    # ── 条件 2: 英文字母占比超过 30%（激进阈值） ──
    alpha_chars = sum(1 for c in t if c.isalpha())
    if alpha_chars > 0:
        english_alpha = sum(1 for c in t if c.isalpha() and ord(c) < 128)
        english_ratio = english_alpha / alpha_chars
        if english_ratio > 0.30:  # 30% 英文字母即判定
            return True

    # ── 条件 3: 包含连续 3 个英文单词 ──
    # 英文单词：至少 2 个字母，主要由 a-z 组成
    def is_english_word(w: str) -> bool:
        if len(w) < 2:
            return False
        english_count = sum(1 for c in w if c.isalpha() and ord(c) < 128)
        return english_count >= len(w) * 0.7

    consecutive_english = 0
    for word in words:
        if is_english_word(word):
            consecutive_english += 1
            if consecutive_english >= 3:
                return True
        else:
            consecutive_english = 0

    # ── 条件 4: 包含常见英文财经关键词 ──
    financial_keywords = (
        "inflation", "recession", "interest rate", "treasury", "yield",
        "unemployment", "payroll", "retail sales", "manufacturing",
        "services", "consumer", "producer", "price", "index", "durable",
        "orders", "trade", "balance", "surplus", "deficit", "imports",
        "exports", "oil", "inventory", "crude", "production", "supply",
        "demand", "earnings", "revenue", "guidance", "dividend", "buyback",
        "ipo", "merger", "acquisition", "default", "bankruptcy", "credit",
        "rating", "downgrade", "upgrade", "outlook", "forecast", "estimate"
    )
    t_lower = t.lower()
    for keyword in financial_keywords:
        if keyword in t_lower:
            return True

    # ── 条件 5: 包含数字和英文混合（如 "S&P 500", "10Y Yield"） ──
    # 模式：数字 + 英文 或 英文 + 数字
    if re.search(r'\d+[a-zA-Z]{2,}|[a-zA-Z]{2,}\d+', t):
        # 进一步检查：如果这个模式周围没有中文，可能是英文
        # 例如 "S&P 500" 是英文，"上证3000点" 是中文
        sample_words = [w for w in words if any(c.isalpha() for c in w)]
        if sample_words:
            english_word_ratio = sum(1 for w in sample_words if is_english_word(w)) / len(sample_words)
            if english_word_ratio > 0.4:
                return True

    return False


def _translate_title_sync(original_title: str) -> str:
    """
    翻译英文标题 - 完整的 Doubao -> Gemini -> DeepSeek 兜底链。
    这是一个同步调用，在 executor 线程中运行，不阻塞 async loop。
    """
    # 翻译 prompt：极其简单，要求纯翻译结果
    translate_prompt = "请将以下英文新闻标题翻译成简体中文，只需返回翻译结果，不要任何解释或额外内容："

    # ── 构建模型候选列表（按优先级排序） ──
    # 2026-09-17 重排：OpenRouter key 失效(401) → 国内直连的 MiniMax/MiniMax#2 提前，
    # OpenRouter 的 Gemini/DeepSeek 降为最后兜底；MiniMax/Doubao 用 use_proxy=False 强制直连。
    model_candidates = []

    # 候选 1: Doubao（火山引擎 - 国内最快，直连）
    if DOUBAO_API_KEY and DOUBAO_MODEL:
        model_candidates.append({
            "id": DOUBAO_MODEL,
            "label": "Doubao-Translator",
            "api_base": DOUBAO_BASE_URL,
            "api_key": DOUBAO_API_KEY,
            "use_proxy": False,
        })

    # 候选 2: MiniMax（国内直连，实测稳定 1-5s）
    if MINIMAX_API_KEY and MINIMAX_MODEL:
        model_candidates.append({
            "id": MINIMAX_MODEL,
            "label": "MiniMax-Translator",
            "api_base": MINIMAX_BASE_URL,
            "api_key": MINIMAX_API_KEY,
            "use_proxy": False,
        })

    # 候选 3: 第二个 MiniMax key（并行补充，旧 key 失败后再试）
    if MINIMAX2_API_KEY and MINIMAX_MODEL:
        model_candidates.append({
            "id": MINIMAX_MODEL,
            "label": "MiniMax#2-Translator",
            "api_base": MINIMAX_BASE_URL,
            "api_key": MINIMAX2_API_KEY,
            "use_proxy": False,
        })

    # 候选 4: Gemini 2.5 Flash（OpenRouter - 走代理，key 失效时快速失败兜底）
    if OPENROUTER_API_KEY:
        model_candidates.append({
            "id": "google/gemini-2.5-flash",
            "label": "Gemini-Translator",
            "api_base": OPENROUTER_BASE_URL,
            "api_key": OPENROUTER_API_KEY,
            "use_proxy": True,
        })

    # 候选 5: DeepSeek Chat（OpenRouter - 兜底）
    if OPENROUTER_API_KEY:
        model_candidates.append({
            "id": "deepseek/deepseek-chat",
            "label": "DeepSeek-Translator",
            "api_base": OPENROUTER_BASE_URL,
            "api_key": OPENROUTER_API_KEY,
            "use_proxy": True,
        })

    # ── 依次尝试每个模型，直到成功 ──
    last_error = None

    for i, model_cfg in enumerate(model_candidates):
        model_label = model_cfg["label"]
        is_last = (i == len(model_candidates) - 1)

        try:
            # ── 构建请求 ──
            payload = {
                "model": model_cfg["id"],
                "messages": [
                    {"role": "system", "content": translate_prompt},
                    {"role": "user", "content": original_title[:500]},
                ],
                "max_tokens": 256,
                "temperature": 0.1,
            }
            # v176.7：MiniMax-M 系列（M3/M2.7 等）默认带 thinking，翻译这种短任务下
            # 会把 "thinking...Let me translate it..." 整段混进正文，污染 raw_news.content，
            # 导致下游 M3 主分析拿到无意义文本 → 空 reasoning → 服务端兜底。
            # 显式禁用 thinking，只输出纯翻译结果（与主分析通道对齐）。
            _mdl_up = (model_cfg["id"] or "").upper()
            if any(t in _mdl_up for t in ("MINIMAX-M3", "MINIMAX-M2.7", "MINIMAX-M2")):
                payload["thinking"] = {"type": "disabled"}

            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(
                f"{model_cfg['api_base']}/chat/completions",
                data=data,
                headers={
                    "Authorization": f"Bearer {model_cfg['api_key']}",
                    "Content-Type": "application/json",
                },
            )

            ssl_ctx = ssl.create_default_context()
            try:
                ssl_ctx.set_ciphers("DEFAULT:@SECLEVEL=1")
            except Exception:
                pass

            # ── 发起请求（超时保护：15秒）──
            # 国内直连通道(use_proxy=False)用空代理强制直连，避免被 env HTTP_PROXY 劫持到不稳的美国代理；
            # OpenRouter(use_proxy=True)保留默认（读环境代理）以连通海外。
            https_handler = urllib.request.HTTPSHandler(context=ssl_ctx)
            if model_cfg.get("use_proxy"):
                opener = urllib.request.build_opener(https_handler)
            else:
                opener = urllib.request.build_opener(https_handler, urllib.request.ProxyHandler({}))
            resp = opener.open(req, timeout=15)
            body = json.loads(resp.read().decode("utf-8"))

            # ── 解析响应 ──
            if "choices" not in body or not body["choices"]:
                raise ValueError(f"响应格式异常: {body}")

            translated = body["choices"][0]["message"]["content"].strip()

            # 清理可能的 markdown 标记
            translated = re.sub(r'^```\w*\s*', '', translated)
            translated = re.sub(r'\s*```$', '', translated)

            # v176.7 兜底清洗：某些思维链模型（MiniMax-M3 等）即便未禁用 thinking，也可能把
            # "thinking...英文解释" 段混进正文。特征是 translated 以 "thinking" 开头；
            # 此时从第一个中文开始截取，只保留真正的中文翻译，剔除英文思维残句。
            _head = translated.lstrip()
            if _head.lower().startswith("thinking"):
                _zh = re.search(r'[\u4e00-\u9fff]', translated)
                if _zh:
                    translated = translated[_zh.start():].strip()

            # ── 验证翻译结果 ──
            if not translated:
                raise ValueError("翻译结果为空")

            if not _contains_chinese(translated):
                raise ValueError(f"翻译结果不包含中文: {translated[:100]}")

            # ── 翻译成功！ ──
            print(f"  [PRE-TRANSLATE] ✅ 翻译成功 ({model_label}): {translated[:100]}")
            return translated[:200]

        except Exception as exc:
            last_error = exc
            error_type = type(exc).__name__
            error_msg = str(exc)[:150]

            if is_last:
                # 最后一个模型也失败了 - 致命错误
                print(f"  [PRE-TRANSLATE] 🚨 翻译 API 全面崩溃！")
                print(f"  [PRE-TRANSLATE] 🚨 最后失败模型: {model_label}")
                print(f"  [PRE-TRANSLATE] 🚨 异常类型: {error_type}")
                print(f"  [PRE-TRANSLATE] 🚨 异常信息: {error_msg}")
                print(f"  [PRE-TRANSLATE] 🚨 原文标题: {original_title[:150]}")
                print(f"  [PRE-TRANSLATE] 🚨 ⚠️ 被迫降级为英文原文进入数据库！")
            else:
                # 中间模型失败 - 尝试下一个
                print(f"  [PRE-TRANSLATE] ⚠️ {model_label} 失败: {error_type} - 切换备选模型...")

    # ── 所有模型都失败，返回原文 ──
    return original_title[:500]


async def _translate_if_english(text: str, loop: asyncio.AbstractEventLoop) -> str:
    """
    异步包装器：检测英文并翻译。
    在 executor 线程中运行同步翻译调用，避免阻塞 async loop。
    """
    if not _is_english_text(text):
        return text

    # ── 高亮日志：拦截到英文新闻 ──
    print(f"  [PRE-TRANSLATE] 🔍 拦截到英文新闻，正在调用翻译 API...")
    print(f"  [PRE-TRANSLATE] 📖 原文: {text[:150]}")

    # 在线程池中执行同步翻译（完整兜底链）
    translated = await loop.run_in_executor(None, _translate_title_sync, text)

    # ── 翻译结果验证 ──
    if translated == text:
        print(f"  [PRE-TRANSLATE] ⚠️ 翻译失败，使用原文（前端将显示英文）")
    else:
        print(f"  [PRE-TRANSLATE] ✅ 最终结果: {translated[:100]}")

    return translated


# ---------------------------------------------------------------------------
# Tree News / Telegram Webhook Listener
# ---------------------------------------------------------------------------
# Accepts POST JSON: {"text": "...", "source": "tree_news" | "telegram" | ...}
# Allows third-party fast-news bots to inject into the Trident pipeline.
# ---------------------------------------------------------------------------

_TREE_NEWS_PORT = int(os.getenv("TREE_NEWS_PORT", "9000"))


async def _tree_news_handler(reader, writer) -> None:
    """Minimal async HTTP POST handler - with top-level exception guard."""
    loop = asyncio.get_running_loop()
    text = ""
    source = "tree_news"

    def _send_response(status_code, status_text, body):
        payload = body.encode("utf-8")
        header = (
            f"HTTP/1.1 {status_code} {status_text}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(payload)}\r\n"
            f"Connection: close\r\n\r\n"
        ).encode("utf-8")
        writer.write(header + payload)

    try:
        # P1 安全修复：先读取 header 行（最多 8KB）拿到 Content-Length，
        # 若 > 64KB 直接拒绝，避免 reader.read() 一口气吃进 64KB 后再判断。
        # 此举也防止恶意客户端发送 huge payload 撑爆内存。
        try:
            header_buf = await asyncio.wait_for(reader.read(8192), timeout=10.0)
        except asyncio.TimeoutError:
            return
        if not header_buf:
            return

        header_text = header_buf.decode("utf-8", errors="replace")
        header_lines = header_text.split("\r\n")
        if "POST" not in header_lines[0]:
            _send_response(405, "Method Not Allowed", json.dumps({"ok": False, "error": "POST only"}))
            await writer.drain()
            return

        # P1: 校验 Content-Length（前置 body 读取前）
        content_length = 0
        for hl in header_lines[1:]:
            if hl.lower().startswith("content-length:"):
                try:
                    content_length = int(hl.split(":", 1)[1].strip())
                except (ValueError, IndexError):
                    pass
                break
        if content_length > 65536:  # 64KB
            _send_response(413, "Payload Too Large", json.dumps({
                "ok": False, "error": f"Content-Length {content_length} exceeds 65536 bytes",
            }))
            await writer.drain()
            return

        # 读取剩余 body（可能跨多个 chunk；buffer 总量不超过 content_length + 8KB）
        body_start_in_buf = header_text.find("\r\n\r\n")
        if body_start_in_buf < 0:
            # headers 没结束（首包仅 partial）— 继续读
            extra = await asyncio.wait_for(reader.read(65536), timeout=10.0)
            raw = header_buf + extra
        else:
            raw = header_buf

        decoded = raw.decode("utf-8", errors="replace")
        body_start = decoded.find("\r\n\r\n")
        if body_start < 0:
            _send_response(400, "Bad Request", json.dumps({"ok": False, "error": "no body"}))
            await writer.drain()
            return

        body_text = decoded[body_start + 4:].strip()
        # P1: body 实际大小二次校验（防 Content-Length 撒谎 / chunked 编码）
        if len(body_text.encode("utf-8")) > 65536:
            _send_response(413, "Payload Too Large", json.dumps({
                "ok": False, "error": "actual body exceeds 65536 bytes",
            }))
            await writer.drain()
            return

        try:
            data = json.loads(body_text)
        except json.JSONDecodeError:
            _send_response(400, "Bad Request", json.dumps({"ok": False, "error": "invalid JSON"}))
            await writer.drain()
            return

        text = str(data.get("text", data.get("content", data.get("message", "")))).strip()
        source = str(data.get("source", "tree_news")).strip()[:64]
        original_text = text

        # ── Pre-filter: hard blacklist + junk detection ──
        if _is_content_junk(text):
            print(f"\n  [{_now()}] [Filter] Ignored junk content: {text[:80]}")
            _send_response(204, "No Content", json.dumps({"ok": True, "filtered": True, "reason": "junk"}))
            await writer.drain()
            return

        # ── 去重检查 (翻译前 — 省 API 调用) ──
        h = hashlib.sha256(original_text.encode()).hexdigest()[:16]
        def _check_dup_web(hash_val: str) -> bool:
            conn = _open_db()
            try:
                ex = conn.execute(
                    "SELECT id FROM raw_news WHERE content LIKE ? LIMIT 1",
                    (f"[hash:{hash_val}]%",),
                ).fetchone()
                return ex is not None
            finally:
                conn.close()
        if await loop.run_in_executor(None, _check_dup_web, h):
            _send_response(204, "No Content", json.dumps({"ok": True, "filtered": True, "reason": "duplicate"}))
            await writer.drain()
            return

        # ═════════════════════════════════════════════════════════════════════
        # PRE-TRANSLATOR INTERCEPTOR (前置翻译拦截器)
        # ═════════════════════════════════════════════════════════════════════
        # 检测英文新闻并立即翻译，确保后续所有处理（数据库 INSERT + 6 模型推演）
        # 统一使用中文标题。这是关注点分离的关键：翻译逻辑完全前置，模型专注推演。
        # ═════════════════════════════════════════════════════════════════════
        text = await _translate_if_english(original_text, loop)
        if text != original_text:
            print(f"  [{_now()}] [TRANSLATE] English → Chinese: {original_text[:40]} → {text[:40]}")

        vip_tag, vip_name = _detect_vip(text)

        def _webhook_insert() -> int | None:
            conn = _open_db()
            try:
                ex = conn.execute(
                    "SELECT id FROM raw_news WHERE content LIKE ? LIMIT 1",
                    (f"[hash:{h}]%",),
                ).fetchone()
                if ex:
                    return None
                source_label = f"WEB:{source}"
                if vip_tag:
                    source_label = f"{source_label} {vip_tag}"
                if not news_sources.allow_ingest(source_label, conn):
                    return None
                ts = _ts()
                ts_epoch = int(time.time())
                cleaned = f"[hash:{h}] {text[:1000]}"
                # v181: 传 ingested_ts 让 L0 过期检查用 ingest 时间戳兜底
                # (webhook 源一般不提供 published_at，所以走 ingest_ts fallback)
                f_result = evaluate_news(cleaned, ingested_ts=ts_epoch)
                news_id = db.insert_raw_news(
                    conn,
                    source=source_label,
                    content=cleaned,
                    timestamp=ts,
                    status="PENDING",
                    is_noise=int(f_result["is_noise"]),
                    relevance_score=float(f_result["relevance_score"]),
                    ts=ts_epoch,
                    gate=f_result.get("gate"),
                )
                timeseries.record_news_event(
                    news_id,
                    source=source_label,
                    is_noise=int(f_result["is_noise"]),
                    status="PENDING",
                    ts=ts_epoch,
                    connection=conn,
                )
                conn.commit()
                return news_id
            finally:
                conn.close()

        rowid = await loop.run_in_executor(None, _webhook_insert)
        if rowid is not None:
            vip_info = f" {vip_tag}" if vip_tag else ""
            print(f"\n  [{_now()}] WEBHOOK #{rowid} [{source}]{vip_info} | {text[:80]}")

        _send_response(200, "OK", json.dumps({"ok": True, "rowid": rowid, "vip": vip_tag or ""}))
        await writer.drain()

    except Exception as exc:
        # P1 安全修复：服务端打印完整异常供排查，但**不**把原始异常
        # 暴露给客户端（避免 stack trace / 路径 / SQL 报错泄露给攻击者）。
        print(f"\n  [{_now()}] WEBHOOK ERROR {type(exc).__name__}: {exc} | text={text[:80]}")
        try:
            _send_response(500, "Internal Server Error", json.dumps({
                "ok": False,
                "error": "internal_error",
                "type": type(exc).__name__,  # 仅返回异常类型名，不含具体信息
            }))
            await writer.drain()
        except Exception:
            pass
    finally:
        try:
            writer.close()
        except Exception:
            pass
