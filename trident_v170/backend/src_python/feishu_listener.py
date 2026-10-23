#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""飞书消息接收器 — 金十推送 → 飞书群 → 长连接读群 → 入库。

原理：金十把快讯 POST 到飞书自定义机器人 webhook，消息出现在飞书群。
本模块用飞书**自建应用**的长连接（WebSocket）订阅 im.message.receive_v1，
实时收到群里消息（含金十机器人发的），解析文本后走
api_server._ingest_external_news_sync 入库（source='feishu_jin10'）
→ 自动进 AI 分析 / 信号台 / 模拟盘管道。

前置条件（open.feishu.cn 开发者后台）：
  1. 创建企业自建应用 → 开启"机器人"能力
  2. 事件订阅：使用"长连接接收事件"（不需公网回调地址）
  3. 添加事件 im.message.receive_v1（接收消息）
  4. 发布应用（可用范围含自己）
  5. 应用机器人进入"金十消息所在群"（内部群才行——外部群无法加自建应用机器人）
  6. pip install lark-oapi

配置（backend/.env）：
  FEISHU_APP_ID / FEISHU_APP_SECRET / FEISHU_LISTENER_ENABLED=1
  FEISHU_JIN10_CHAT_ID（可选：限定群 chat_id；留空则收全部群再按文本过滤）
"""

from __future__ import annotations

import json
import re
import threading
import time
from typing import Any, Dict, List, Optional

import config

# 金十快讯特征（无 chat_id 限定时的文本过滤；与金十其它通道同关键词）
_JIN10_HINTS = ("金十", "jin10", "快讯")

# 上次入库的飞书消息 id 水位（防同一消息重推；进程内足够，hash 去重兜底）
_seen_content: set[str] = set()   # 内容指纹去重（同内容不同 msg_id 也拦）
_seen_lock = threading.Lock()


def _parse_content(message: Dict[str, Any]) -> Dict[str, Any]:
    """解析飞书消息 content JSON → dict；失败返回 {}。"""
    try:
        content = json.loads(str(message.get("content") or "{}"))
    except (json.JSONDecodeError, TypeError):
        content = {}
    return content if isinstance(content, dict) else {}


def _extract_text(message: Dict[str, Any]) -> str:
    """从飞书消息对象提取纯文本。

    im.message.receive_v1 事件里 message.content 是 JSON 字符串：
      text 消息 → {"text": "..."}；post 富文本 → {"content":[[{"tag":"text","text":"..."}]]}
    """
    content = _parse_content(message)
    # 纯文本
    text = str(content.get("text") or "").strip()
    if text:
        return text
    return _walk_post_text(content)


def _walk_post_text(content: Dict[str, Any]) -> str:
    """post 富文本 → 拼接所有 tag=text 片段。"""
    parts: List[str] = []
    for row in content.get("content") or []:
        if not isinstance(row, list):
            continue
        for seg in row:
            if isinstance(seg, dict) and str(seg.get("tag")) == "text":
                parts.append(str(seg.get("text") or ""))
    return " ".join(p.strip() for p in parts if p.strip())


def _extract_text_and_links(message: Dict[str, Any]) -> tuple[str, List[str]]:
    """提取纯文本 + 所有链接（post 富文本里 tag=a 的 href；text 消息正文里裸 URL）。

    返回 (text, links)。链接用于：保存原文出处 + 尝试解析金十发布时间。
    """
    content = _parse_content(message)
    text_parts: List[str] = []
    links: List[str] = []
    # 纯 text 消息
    text = str(content.get("text") or "").strip()
    if text:
        # 正文里可能带裸 URL（如 https://...）
        for m in re.finditer(r"https?://[^\s<>\"']+", text):
            links.append(m.group(0).rstrip("，。,；;）)]】》\u201d\u201c"))
        return text, links
    # post 富文本：顺序遍历，text 段入文本、a 段只取 href（链接文字如"查看原文"不拼入正文）
    for row in content.get("content") or []:
        if not isinstance(row, list):
            continue
        for seg in row:
            if not isinstance(seg, dict):
                continue
            tag = str(seg.get("tag") or "")
            if tag == "text":
                text_parts.append(str(seg.get("text") or ""))
            elif tag == "a":
                href = str(seg.get("href") or seg.get("link") or "").strip()
                if href:
                    links.append(href)
    full_text = " ".join(p.strip() for p in text_parts if p.strip())
    # 文本里若有裸 URL 也补进 links
    for m in re.finditer(r"https?://[^\s<>\"']+", full_text):
        u = m.group(0).rstrip("，。,；;）)]】》")
        if u not in links:
            links.append(u)
    return full_text, links


# 公司公告/个股/监管特征排除词：金十快讯里"A股公司公告/可转债/减持/罚款"等
# 常混入宽泛关键词（美元/债券/ETF），即使命中白名单也应丢弃（非策略赛道）。
# 命中任一词 → 判为无关公司新闻（除非同时命中强宏观词，见 _is_strategy_relevant）。
_EXCLUDE_KEYWORDS = (
    "公告", "减持", "增持", "回购", "董事会", "股东大会", "年报", "半年报", "季报",
    "营收", "净利润", "股东", "公司", "集团", "股份", "可换股债券", "可转债",
    "罚款", "处罚", "立案", "退市", "停牌", "复牌", "中标", "签约", "投产",
    "上证", "深证", "创业板", "科创板", "北交所", "沪指", "深成指", "A股", "港股",
    "股票", "配股", "定增", "并购重组", "募集", "基金", "私募", "监管",
)

# 强宏观信号词：即使含公司词（如"央行""美联储"开头的）也放行——优先于排除词
_STRONG_MACRO_HINTS = (
    "央行", "美联储", "欧央行", "英国央行", "日本央行", "鲍威尔", "沃什", "贝利",
    "cpi", "ppi", "非农", "pmi", "利率决议", "加息", "降息", "量化宽松", "缩表",
    "财政部", "国债", "美债", "联邦基金", "通胀", "fomc", "opec", "欧佩克",
)


def _is_strategy_relevant(text: str) -> bool:
    """策略相关性：白名单命中（加密/黄金/原油/宏观/地缘）+ 排除公司公告/个股特征。

    与金十 MCP 采集层同款白名单为底座，但加一层排除：
    命中白名单却含"A股公司公告/可转债/股东减持"等特征 → 判无关丢弃
    （避免"联想 6.75 亿美元可换股债券"这种混入宽泛词"美元/债券"的漏网）。
    强宏观信号（央行/美联储/OPEC 等开头）优先于排除词放行。
    """
    try:
        from jin10_mcp import _is_relevant
        if not _is_relevant(text):
            return False
    except Exception:
        pass  # 白名单依赖异常 → 靠排除词与 L1 噪声兜底
    # 强宏观信号直接放行（即使标题含"央行""美联储"等）
    low = text.lower()
    if any(h in low for h in _STRONG_MACRO_HINTS):
        return True
    # 命中排除词 → 判为公司公告/个股类无关新闻
    if any(kw in text for kw in _EXCLUDE_KEYWORDS):
        return False
    return True


def _content_fingerprint(raw_text: str) -> str:
    """内容指纹：清洗后的文本主体 hash（去时间戳/来源后缀/链接行差异）。

    金十同一内容可能经不同通道推两次（快讯/APP重磅），msg_id 不同但内容几乎相同。
    指纹用于内容级去重：取标题主体前 80 字符，去掉"[来自金十数据快讯]"/"9月4日讯"
    等通道/时间噪音，再取 hash —— 同内容不同通道也能拦下。
    """
    t = re.sub(r"\[来自金十数据[^\]]*\]", "", raw_text or "")
    t = re.sub(r"\d{1,2}月\d{1,2}日", "", t)
    t = re.sub(r"原文链接[:：]?\s*https?://\S+", "", t)
    t = re.sub(r"https?://\S+", "", t)
    t = re.sub(r"\s+", "", t)
    import hashlib
    return hashlib.sha256(t[:120].encode("utf-8")).hexdigest()[:24]


def _is_jin10_message(raw_text: str, chat_id: str, msg_id: str) -> bool:
    """是否金十推送消息：限定群直接收；否则靠文本特征。"""
    if config.FEISHU_JIN10_CHAT_ID:
        return chat_id == config.FEISHU_JIN10_CHAT_ID
    low = (raw_text or "").lower()
    return any(hint.lower() in low for hint in _JIN10_HINTS) and len(raw_text) > 8


def handle_feishu_message(event: Dict[str, Any]) -> Optional[int]:
    """处理一条飞书消息事件 → 入库。返回入库条数；非金十/失败返回 None。

    event 来自 lark-oapi ws Client 的 P2ImMessageReceiveV1：
      event.message: {message_id, chat_id, create_time, msg_type, content}
      create_time: 毫秒时间戳（UTC）
    """
    ev = event.get("event") or {}
    message = ev.get("message") or {}
    msg_id = str(message.get("message_id") or "")
    chat_id = str(message.get("chat_id") or "")
    raw_text, links = _extract_text_and_links(message)
    if not raw_text:
        return None
    if not _is_jin10_message(raw_text, chat_id, msg_id):
        return None
    # 清洗：去掉飞书富文本标记/链接噪音，单行化
    cleaned = re.sub(r"<at[^>]*>.*?</at>", "", raw_text)
    cleaned = re.sub(r"\s*\n+\s*", " ", cleaned).strip()
    title = cleaned[:300]
    # ── 策略相关性过滤：与金十 MCP 同款白名单（加密/黄金/原油/宏观/地缘）──
    # 金十快讯大量是 A股/公司公告/监管等与策略赛道无关的内容（如"证监会私募基金办法"），
    # 不命中白名单 → 直接丢弃不入库（与 jin10_mcp 采集层行为一致，避免 AI 打成"其他"浪费配额）。
    if not _is_strategy_relevant(title):
        print(
            f"[FEISHU-LISTEN] 丢弃无关新闻（非加密/黄金/原油/宏观/地缘）: {title[:70]}",
            flush=True,
        )
        return None
    # ── 内容级去重：同内容不同 msg_id（金十多通道重复推）也拦下 ──
    fp = _content_fingerprint(raw_text)
    with _seen_lock:
        if fp in _seen_content:
            print(f"[FEISHU-LISTEN] 丢弃重复内容: {title[:60]}", flush=True)
            return None
        _seen_content.add(fp)
        if len(_seen_content) > 3000:
            _seen_content.clear()
    # 原文链接（金十快讯出处；保存供回溯，也尝试从 URL/快照解析发布时间）
    link = links[0] if links else ""
    # 飞书到达时间（毫秒 UTC）→ 作为"到达时刻"兜底（统一转北京入库）
    create_ms = message.get("create_time") or ""
    arrived_at = ""
    try:
        import datetime as _dt
        if str(create_ms).isdigit():
            arrived_at = config.fmt_bj(_dt.datetime.fromtimestamp(
                int(create_ms) / 1000.0, tz=_dt.timezone.utc,
            ))
    except Exception:
        pass
    # 尝试从链接解析金十原始发布时间（页面/API 有则用，失败回退飞书到达时间）
    published_at = _resolve_publish_time(link, title, arrived_at)
    # 入库（走外部新闻统一入口：关键词/噪声过滤 + 新鲜度闸 + hash 去重）
    try:
        from api_server import _ingest_external_news_sync
        summary = ""
        if link:
            summary = f"原文链接: {link}"
        item = {
            # id 用内容指纹：DB 层 [hash:] 去重同样基于它 → 重启后同内容也不会再入库
            "id": f"feishu:{fp}",
            "source": "feishu_jin10",
            "title": title,
            "summary": summary,
            "published_at": published_at,
        }
        inserted = _ingest_external_news_sync([item])
        if inserted:
            print(
                f"[FEISHU-LISTEN] 入库金十快讯: {title[:60]}"
                + (f"（发布时间 {published_at} / 飞书到达 {arrived_at}）" if published_at else "")
                + (f" 链接: {link[:80]}" if link else ""),
                flush=True,
            )
        return inserted
    except Exception as exc:
        print(f"[FEISHU-LISTEN] 入库失败: {type(exc).__name__}: {exc}", flush=True)
        return None


def _resolve_publish_time(link: str, title: str, arrived_at: str) -> str:
    """解析金十原始发布时间。

    优先级：
      1. 链接/文本含金十 flash 时间戳（flashYYYYMMDDHHMMSS…，时间编码在 id 里）→ 精确到秒
      2. 标题内嵌日期（"金十数据9月4日讯"→ 仅当日）→ 与到达同日时用到达时刻更准
      3. 兜底：飞书到达时刻
    """
    # 1) 金十 flash 时间戳：flash 后接 14 位 YYYYMMDDHHMMSS（兼容 /flash/2026... 路径分隔）
    try:
        import datetime as _dt
        m = re.search(r"flash[/_-]?(20\d{12})\d*", f"{link} {title}")
        if m:
            dt = _dt.datetime.strptime(m.group(1), "%Y%m%d%H%M%S").replace(tzinfo=config.TZ_SHANGHAI)
            return config.fmt_bj(dt)
    except Exception:
        pass
    # 2) 标题内嵌日期："金十数据9月4日讯" → 补当前年
    try:
        import datetime as _dt
        m = re.search(r"(\d{1,2})月(\d{1,2})日", title)
        if m:
            month, day = int(m.group(1)), int(m.group(2))
            now = _dt.datetime.now(config.TZ_SHANGHAI)
            # 若日期明显晚于今天（跨年场景），年份-1
            year = now.year
            if (month, day) > (now.month, now.day):
                year -= 1
            dt = _dt.datetime(year, month, day, tzinfo=config.TZ_SHANGHAI)
            # 只保留日期（时间未知）：与飞书到达同日时用到达时刻更准
            if dt.date() == now.date() and arrived_at:
                return arrived_at
            return config.fmt_bj(dt)
    except Exception:
        pass
    return arrived_at


def start_feishu_listener() -> None:
    """启动飞书长连接接收器（阻塞；应在线程/子进程中跑）。

    依赖 lark-oapi（未安装/未配置 → 打印提示并返回，不阻塞引擎）。
    """
    if not config.FEISHU_APP_ID or not config.FEISHU_APP_SECRET:
        print("[FEISHU-LISTEN] FEISHU_APP_ID / FEISHU_APP_SECRET 未配置，接收器跳过", flush=True)
        return
    try:
        # v176.5: lark-oapi 新版 API（>=1.4）
        from lark_oapi.ws.client import Client as WsClient
        from lark_oapi.api.im.v1 import P2ImMessageReceiveV1
        from lark_oapi.core.enum import LogLevel
        from lark_oapi.event.dispatcher_handler import EventDispatcherHandler
    except ImportError:
        print(
            "[FEISHU-LISTEN] 未安装 lark-oapi —— 请在 backend 目录执行:"
            " pip install lark-oapi ，然后重启。接收器暂不启动",
            flush=True,
        )
        return

    def _on_message(data: P2ImMessageReceiveV1) -> None:
        try:
            payload = data
            # lark-oapi ws 事件对象：可直接转 dict 或访问字段
            ev = getattr(data, "event", data)
            msg = getattr(ev, "message", None)
            if msg is None:
                # 尝试原始 dict
                raw = payload if isinstance(payload, dict) else {}
                handle_feishu_message(raw)
                return
            message = {
                "message_id": str(getattr(msg, "message_id", "") or ""),
                "chat_id": str(getattr(msg, "chat_id", "") or ""),
                "create_time": getattr(msg, "create_time", "") or "",
                "msg_type": str(getattr(msg, "message_type", "") or ""),
                "content": getattr(msg, "content", "") or "",
            }
            handle_feishu_message({"event": {"message": message}})
        except Exception as exc:
            print(f"[FEISHU-LISTEN] 事件处理异常: {type(exc).__name__}: {exc}", flush=True)

    try:
        handler = (
            EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(_on_message)
            .build()
        )
        client = WsClient(
            config.FEISHU_APP_ID,
            config.FEISHU_APP_SECRET,
            event_handler=handler,
            log_level=LogLevel.WARNING,  # 避免心跳刷屏
        )
        print("[FEISHU-LISTEN] 长连接启动，等待金十群消息…", flush=True)
        client.start()  # 阻塞，异常会抛出后重试由上层决定
    except Exception as exc:
        print(f"[FEISHU-LISTEN] 长连接启动失败: {type(exc).__name__}: {exc}", flush=True)


def listener_main_loop() -> None:
    """带自动重连的主循环（供后台线程调用）。"""
    while True:
        try:
            start_feishu_listener()
        except Exception as exc:
            print(f"[FEISHU-LISTEN] 连接中断，10s 后重连: {type(exc).__name__}: {exc}", flush=True)
        time.sleep(10)
