#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Trident 量化平台 — 全局联通中心 (Notification Channels).

统一管理所有出站告警 / 通知通道：
  - 飞书 webhook (FEISHU_WEBHOOK_URL)
  - 邮件 SMTP  (SMTP_HOST / SMTP_USER / SMTP_PASS / SMTP_TO)
  - Server酱  (SERVERCHAN_SENDKEY)        — 微信
  - PushPlus   (PUSHPLUS_TOKEN)           — 微信
  - Slack webhook (SLACK_WEBHOOK_URL)     — v281 新增
  - Discord webhook (DISCORD_WEBHOOK_URL) — v281 新增

设计原则（与 source_status.py 同源）：
  - 线程安全（RLock 防止同一线程重入死锁）
  - channel 探活走真实发送路径并记录成功/失败
  - 启动时 register_known_channels() 自动注册清单
  - snapshot() 返回面板需要的全部字段
  - dispatch()：把同一份 payload 同时发到所有 enabled 的通道
"""
from __future__ import annotations

import json
import smtplib
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from email.header import Header
from email.mime.text import MIMEText
from typing import Any, Dict, List, Optional, Tuple

import config

_lock = threading.RLock()
_registry: Dict[str, Dict[str, Any]] = {}
_warned: Dict[str, bool] = {}


def _now() -> float:
    return time.time()


def _channel_entry(channel_id: str, kind: str, label: str, description: str = "") -> Dict[str, Any]:
    return {
        "channel_id": channel_id,
        "kind": kind,
        "label": label,
        "description": description,
        "configured": False,
        "enabled": True,
        "status": "unknown",
        "last_ok_ts": None,
        "last_fail_ts": None,
        "last_error": "",
        "last_latency_ms": None,
        "consecutive_failures": 0,
        "total_ok": 0,
        "total_fail": 0,
    }


def _set(entry: Dict[str, Any], **kwargs: Any) -> None:
    for k, v in kwargs.items():
        if k in entry:
            entry[k] = v


def _classify(entry: Dict[str, Any]) -> str:
    if not entry.get("enabled", True):
        return "disabled"
    if not entry.get("configured"):
        return "unknown"
    now = _now()
    last_ok = entry.get("last_ok_ts")
    if last_ok is None:
        if entry.get("consecutive_failures", 0) > 0:
            return "error"
        return "unknown"
    if now - float(last_ok) <= 300:
        return "online"
    if now - float(last_ok) > 1800:
        return "stale"
    return "online"


def register(channel_id: str, *, kind: str, label: str, description: str = "") -> Dict[str, Any]:
    with _lock:
        if channel_id in _registry:
            return _registry[channel_id]
        entry = _channel_entry(channel_id, kind, label, description)
        _registry[channel_id] = entry
        return entry


def _detect_configured() -> Dict[str, bool]:
    return {
        "feishu":      bool(config.FEISHU_WEBHOOK_URL),
        "slack":       bool(getattr(config, "SLACK_WEBHOOK_URL", "")),
        "discord":     bool(getattr(config, "DISCORD_WEBHOOK_URL", "")),
        "email":       bool(config.SMTP_HOST and config.SMTP_USER and config.SMTP_PASS and config.SMTP_TO),
        "wechat_sct":  bool(config.SERVERCHAN_SENDKEY),
        "wechat_pp":   bool(config.PUSHPLUS_TOKEN),
    }


def register_known_channels() -> None:
    cfg = _detect_configured()
    catalog = [
        ("feishu",     "webhook", "飞书 (Feishu)", "FEISHU_WEBHOOK_URL — 飞书群机器人 webhook"),
        ("slack",      "webhook", "Slack",         "SLACK_WEBHOOK_URL — Slack Incoming Webhook"),
        ("discord",    "webhook", "Discord",       "DISCORD_WEBHOOK_URL — Discord Channel Webhook"),
        ("email",      "smtp",    "邮件 (SMTP)",   "SMTP_HOST / USER / PASS / TO — QQ 邮箱 SSL 465"),
        ("wechat_sct", "wechat",  "Server酱",      "SERVERCHAN_SENDKEY — 微信公众号推送"),
        ("wechat_pp",  "wechat",  "PushPlus",      "PUSHPLUS_TOKEN — 微信模板消息推送"),
    ]
    with _lock:
        for cid, kind, label, desc in catalog:
            entry = register(cid, kind=kind, label=label, description=desc)
            entry["configured"] = cfg[cid]


# ── 实际发送逻辑（每个通道一个 send 函数） ──────────────────────────────

def _send_feishu(url: str, title: str, body: str) -> Tuple[bool, str]:
    """飞书群机器人 — 简洁卡片格式（兼容老 webhook 不依赖卡片框架）。"""
    try:
        payload = {"msg_type": "interactive", "card": {
            "header": {"title": {"tag": "plain_text", "content": title[:80]}},
            "elements": [{"tag": "markdown", "content": body[:3500]}],
        }}
        req = urllib.request.Request(
            url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        resp = urllib.request.urlopen(req, timeout=10)
        return True, f"HTTP {resp.status}"
    except urllib.error.HTTPError as e:
        body_b = e.read().decode("utf-8", errors="replace")[:200]
        return False, f"HTTP {e.code}: {body_b}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def _send_slack(url: str, title: str, body: str) -> Tuple[bool, str]:
    """Slack Incoming Webhook — text 字段（mrkdwn 渲染）。"""
    try:
        text = f"*{title}*\n\n{body[:2900]}"
        payload = {"text": text}
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        resp = urllib.request.urlopen(req, timeout=10)
        return True, f"HTTP {resp.status}"
    except urllib.error.HTTPError as e:
        body_b = e.read().decode("utf-8", errors="replace")[:200]
        return False, f"HTTP {e.code}: {body_b}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def _send_discord(url: str, title: str, body: str) -> Tuple[bool, str]:
    """Discord Webhook — embeds 数组（比纯 content 更醒目）。"""
    try:
        payload = {
            "username": "Trident Quant",
            "embeds": [{
                "title": title[:200],
                "description": body[:3900],
                "color": 0x3b82f6,
            }],
        }
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        resp = urllib.request.urlopen(req, timeout=10)
        return True, f"HTTP {resp.status}"
    except urllib.error.HTTPError as e:
        body_b = e.read().decode("utf-8", errors="replace")[:200]
        return False, f"HTTP {e.code}: {body_b}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def _send_email(title: str, body: str) -> Tuple[bool, str]:
    """SMTP 邮件（QQ 邮箱 SSL 465；SMTP_PASS 用授权码）。"""
    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = Header(title, "utf-8")
        msg["From"] = config.SMTP_FROM or config.SMTP_USER
        msg["To"] = config.SMTP_TO
        with smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT, timeout=15) as s:
            s.login(config.SMTP_USER, config.SMTP_PASS)
            s.sendmail(msg["From"], [config.SMTP_TO], msg.as_string())
        return True, "smtp ok"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def _send_serverchan(title: str, body: str) -> Tuple[bool, str]:
    """Server酱 微信推送 POST。"""
    try:
        data = urllib.parse.urlencode({"title": title, "desp": body[:2900]}).encode("utf-8")
        req = urllib.request.Request(
            f"https://sctapi.ftqq.com/{config.SERVERCHAN_SENDKEY}.send",
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"},
        )
        resp = urllib.request.urlopen(req, timeout=10)
        return True, f"HTTP {resp.status}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def _send_pushplus(title: str, body: str) -> Tuple[bool, str]:
    """PushPlus 微信推送。"""
    try:
        payload = json.dumps({
            "token": config.PUSHPLUS_TOKEN,
            "title": title, "content": body[:3500],
            "template": "markdown",
        }, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            "http://www.pushplus.plus/send",
            data=payload,
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        resp = urllib.request.urlopen(req, timeout=10)
        return True, f"HTTP {resp.status}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


# ── 记录成功 / 失败 ──────────────────────────────────────────────────────

def _record_ok(channel_id: str, latency_ms: float) -> None:
    with _lock:
        e = _registry.get(channel_id)
        if e is None:
            return
        now = _now()
        _set(e,
            last_ok_ts=now,
            last_fail_ts=e.get("last_fail_ts"),
            last_error="",
            last_latency_ms=round(latency_ms, 1),
            consecutive_failures=0,
            total_ok=int(e.get("total_ok", 0)) + 1,
            status="online",
        )


def _record_fail(channel_id: str, err: str) -> None:
    with _lock:
        e = _registry.get(channel_id)
        if e is None:
            return
        now = _now()
        cf = int(e.get("consecutive_failures", 0)) + 1
        _set(e,
            last_fail_ts=now,
            last_error=err[:200],
            consecutive_failures=cf,
            total_fail=int(e.get("total_fail", 0)) + 1,
            status="error" if cf >= 2 else (e.get("status") or "error"),
        )


# ── 单通道发送（带健康记录） ────────────────────────────────────────────

def send(channel_id: str, title: str, body: str) -> Tuple[bool, str]:
    """对单个 channel 发送（带成功 / 失败记录）。返回 (ok, info)。"""
    with _lock:
        e = _registry.get(channel_id)
        configured = bool(e and e.get("configured"))
        enabled = bool(e and e.get("enabled", True))

    if e is None:
        return False, "unknown_channel"
    if not enabled:
        return False, "disabled"
    if not configured:
        if not _warned.get(channel_id):
            _warned[channel_id] = True
            print(f"[NOTIFY] {channel_id} 未配置，跳过发送（{title[:40]}）", flush=True)
        return False, "not_configured"

    t0 = time.time()
    try:
        if channel_id == "feishu":
            ok, info = _send_feishu(config.FEISHU_WEBHOOK_URL, title, body)
        elif channel_id == "slack":
            ok, info = _send_slack(config.SLACK_WEBHOOK_URL, title, body)
        elif channel_id == "discord":
            ok, info = _send_discord(config.DISCORD_WEBHOOK_URL, title, body)
        elif channel_id == "email":
            ok, info = _send_email(title, body)
        elif channel_id == "wechat_sct":
            ok, info = _send_serverchan(title, body)
        elif channel_id == "wechat_pp":
            ok, info = _send_pushplus(title, body)
        else:
            return False, f"unhandled_channel:{channel_id}"
    except Exception as e2:
        ok, info = False, f"{type(e2).__name__}: {e2}"

    latency = (time.time() - t0) * 1000.0
    if ok:
        _record_ok(channel_id, latency)
    else:
        _record_fail(channel_id, info)
    return ok, info


# ── 全通道并联派发 ──────────────────────────────────────────────────────

def dispatch(title: str, body: str, *,
             only: Optional[List[str]] = None,
             exclude: Optional[List[str]] = None,
             min_status: str = "any") -> Dict[str, Any]:
    """对所有 enabled + configured 的通道并联发送同一份 payload。

    Args:
      only:    仅发送指定 channel_id 列表（None = 全部）
      exclude: 排除指定 channel_id 列表
      min_status: 最低状态门槛 — 'any'(默认) / 'online'(只发最近成功的)

    Returns:
      {channel_id: {"ok": bool, "info": str, "latency_ms": float}, ...}
    """
    with _lock:
        ids = list(_registry.keys())

    if only is not None:
        ids = [i for i in ids if i in only]
    if exclude is not None:
        ids = [i for i in ids if i not in exclude]

    results: Dict[str, Any] = {}
    for cid in ids:
        e = _registry.get(cid)
        if e is None or not e.get("configured") or not e.get("enabled", True):
            continue
        if min_status == "online" and e.get("status") not in ("online",):
            continue
        t0 = time.time()
        ok, info = send(cid, title, body)
        results[cid] = {
            "ok": ok,
            "info": info,
            "latency_ms": round((time.time() - t0) * 1000.0, 1),
        }
    return results


# ── 面板快照 ────────────────────────────────────────────────────────────

def snapshot() -> List[Dict[str, Any]]:
    """返回所有 channel 的实时面板快照（与 source_status.snapshot() 同结构）。"""
    with _lock:
        out = []
        for cid, e in _registry.items():
            status = _classify(e)
            last_ok = e.get("last_ok_ts")
            last_fail = e.get("last_fail_ts")
            out.append({
                "channel_id": cid,
                "kind": e.get("kind", ""),
                "label": e.get("label", cid),
                "description": e.get("description", ""),
                "configured": bool(e.get("configured")),
                "enabled": bool(e.get("enabled", True)),
                "status": status,
                "last_ok_ts": last_ok,
                "last_fail_ts": last_fail,
                "secs_since_ok": (round(_now() - last_ok, 1) if isinstance(last_ok, (int, float)) else None),
                "secs_since_fail": (round(_now() - last_fail, 1) if isinstance(last_fail, (int, float)) else None),
                "last_error": e.get("last_error", ""),
                "last_latency_ms": e.get("last_latency_ms"),
                "consecutive_failures": int(e.get("consecutive_failures", 0)),
                "total_ok": int(e.get("total_ok", 0)),
                "total_fail": int(e.get("total_fail", 0)),
            })
        order = {"online": 0, "error": 1, "stale": 2, "unknown": 3, "disabled": 4}
        out.sort(key=lambda x: (order.get(x["status"], 9), -(x.get("last_ok_ts") or 0)))
        return out


# ── 测试钩子 ────────────────────────────────────────────────────────────

def record_test(channel_id: str, ok: bool, info: str = "") -> None:
    """手动标记一次成功 / 失败（不影响真实发送），用于 /api/connectivity/test-channel。"""
    if not ok:
        _record_fail(channel_id, info or "manual test failed")
    else:
        _record_ok(channel_id, 0.0)


def reset_channel(channel_id: str) -> None:
    """重置某个 channel 的健康状态（清零连续失败）。"""
    with _lock:
        e = _registry.get(channel_id)
        if e is None:
            return
        _set(e,
            last_error="",
            consecutive_failures=0,
            status=_classify(e),
        )
