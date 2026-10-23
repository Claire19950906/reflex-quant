"""数据源健康告警 — 周期性轮询 source_status.snapshot() 并在状态翻转 / 持续失败时推送飞书。

设计要点:
- 复用现有 source_status 内存视图,不做额外 DB 读(避免锁竞争)
- 状态翻转(ok↔error / online↔stale)即推一条,便于第一时间发现 API key 过期 / 代理挂掉
- 连续失败阈值(SOURCE_ALERT_THRESHOLD)防止瞬时抖动刷屏
- 静默期(SOURCE_ALERT_SILENT_MIN)防止同源反复推送
- 健康摘要(SOURCE_ALERT_HEARTBEAT_MIN, 默认 60)周期性报告总览,便于无故障时也知道 watcher 在跑
- 飞书 webhook 未配置时静默跳过(只警告一次)
- 全部 fire-and-forget,任何环节异常只 log,不阻塞 watcher 主循环
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

import config
import source_status

from .utils import _now


# 模块级状态:上次告警时间 + 上次状态镜像,均挂 _lock 守护
_lock = threading.RLock()
_last_alert_ts: Dict[str, float] = {}   # source_id -> 上次推飞书的时间戳
_last_state: Dict[str, str] = {}        # source_id -> 上次观测到的 status (online/error/stale/unknown)
_last_heartbeat_ts: float = 0.0         # 上次发送心跳摘要的时间戳


# 心跳通道白名单 — 这些是"心脏"通道,状态翻转即使只有 1 次失败也告警。
# 默认从 config.SOURCE_ALERT_HEARTBEAT_SOURCES(env 可覆盖)读取,不再硬编码。
HEARTBEAT_SOURCES = config.SOURCE_ALERT_HEARTBEAT_SOURCES


def _now_ts() -> float:
    return time.time()


def _fmt_age(secs: Optional[float]) -> str:
    if secs is None:
        return "n/a"
    if secs < 60:
        return f"{int(secs)}s 前"
    if secs < 3600:
        return f"{int(secs / 60)}m 前"
    if secs < 86400:
        return f"{secs / 3600:.1f}h 前"
    return f"{secs / 86400:.1f}d 前"


def _should_alert(source_id: str, *, now: float, threshold: int,
                  consecutive_failures: int, last_result: Optional[str],
                  status_now: str) -> Optional[str]:
    """判定是否需要告警。返回:alert_kind ('flip' / 'threshold') 或 None。

    判定逻辑:
    1. 静默期内:返回 None
    2. 白名单心脏通道 + last_result == 'error' + 之前不是 error:return 'flip'
    3. 普通源:状态翻转(对比 _last_state):return 'flip'
    4. 普通源:连续失败 >= threshold(且 last_result == 'error'):return 'threshold'
    """
    with _lock:
        prev_status = _last_state.get(source_id)
        prev_alert_ts = _last_alert_ts.get(source_id, 0.0)
        silent_for = now - prev_alert_ts

    if silent_for < config.SOURCE_ALERT_SILENT_MIN * 60:
        return None

    if source_id in HEARTBEAT_SOURCES and last_result == "error":
        if prev_status != "error":
            return "flip"
        return None

    if prev_status is not None and prev_status != status_now:
        return "flip"
    if consecutive_failures >= threshold and last_result == "error":
        return "threshold"
    return None


def _build_alert_card(source_id: str, entry: Dict[str, Any], kind: str) -> Dict[str, Any]:
    """组装飞书消息卡片 payload(原生 fields 数组格式,与现有 alerts.py 风格一致)。"""
    label = entry.get("label") or source_id
    cat = entry.get("category") or "数据源"
    status = entry.get("status", "unknown")
    last_error = entry.get("last_error", "") or "无错误信息"
    secs_since_ok = entry.get("secs_since_ok")
    secs_since_fail = entry.get("secs_since_fail")
    consecutive_failures = entry.get("consecutive_failures", 0)
    total_ok = entry.get("total_ok", 0)
    total_fail = entry.get("total_fail", 0)

    if kind == "flip":
        title_emoji = "🚨" if status == "error" else "✅"
        title_action = "已宕机" if status == "error" else "已恢复"
        color = "red" if status == "error" else "green"
    else:  # threshold
        title_emoji = "⚠️"
        title_action = "持续失败"
        color = "orange"

    title = f"{title_emoji} {cat} · {label} {title_action}"

    fields = [
        {"is_short": True, "text": {"tag": "plain_text", "content": f"**状态**: {status}"}},
        {"is_short": True, "text": {"tag": "plain_text", "content": f"**连续失败**: {consecutive_failures}"}},
        {"is_short": True, "text": {"tag": "plain_text", "content": f"**累计成功**: {total_ok}"}},
        {"is_short": True, "text": {"tag": "plain_text", "content": f"**累计失败**: {total_fail}"}},
        {"is_short": True, "text": {"tag": "plain_text",
                                     "content": f"**最后成功**: {_fmt_age(secs_since_ok)}"}},
        {"is_short": True, "text": {"tag": "plain_text",
                                     "content": f"**最后失败**: {_fmt_age(secs_since_fail)}"}},
    ]
    if last_error:
        fields.append({"is_short": False,
                       "text": {"tag": "plain_text", "content": f"**错误**: {last_error[:300]}"}})

    body_text = f"触发类型: `{kind}` · source_id: `{source_id}`"
    if entry.get("detail"):
        body_text += f"\n详情: {entry['detail'][:300]}"

    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": color,
            },
            "elements": [
                {"tag": "div", "fields": fields},
                {"tag": "hr"},
                {"tag": "note", "elements": [{"tag": "plain_text", "content": body_text}]},
            ],
        },
    }


def _build_heartbeat_card(stats: Dict[str, int], newly_recovered: List[str],
                          newly_failed: List[str]) -> Dict[str, Any]:
    """每小时心跳摘要:总览 + 本周期翻转的源。"""
    online = stats.get("online", 0)
    error = stats.get("error", 0)
    stale = stats.get("stale", 0)
    unknown = stats.get("unknown", 0)
    total = online + error + stale + unknown

    fields = [
        {"is_short": True, "text": {"tag": "plain_text", "content": f"**总计**: {total}"}},
        {"is_short": True, "text": {"tag": "plain_text", "content": f"**在线**: {online}"}},
        {"is_short": True, "text": {"tag": "plain_text", "content": f"**异常**: {error}"}},
        {"is_short": True, "text": {"tag": "plain_text", "content": f"**过期**: {stale}"}},
        {"is_short": True, "text": {"tag": "plain_text", "content": f"**未知**: {unknown}"}},
        {"is_short": True, "text": {"tag": "plain_text", "content": f"**时间**: {_now()[:19]}"}},
    ]

    elements: List[Dict[str, Any]] = [{"tag": "div", "fields": fields}]
    if newly_failed:
        elements.append({"tag": "hr"})
        elements.append({"tag": "div", "text": {"tag": "lark_md",
                                                "content": f"**新异常**: {', '.join(newly_failed[:20])}"}})
    if newly_recovered:
        elements.append({"tag": "hr"})
        elements.append({"tag": "div", "text": {"tag": "lark_md",
                                                "content": f"**新恢复**: {', '.join(newly_recovered[:20])}"}})

    return {
        "msg_type": "interactive",
        "card": {
            "header": {"title": {"tag": "plain_text", "content": "📡 数据源心跳摘要"},
                       "template": "blue"},
            "elements": elements,
        },
    }


def _send_feishu_sync(payload: Dict[str, Any], url: str, timeout: float = 10.0) -> bool:
    """同步推送飞书,fire-and-forget。返回是否成功。失败只 log,不抛。"""
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        resp = urllib.request.urlopen(req, timeout=timeout)
        body = resp.read().decode("utf-8", errors="replace")
        # 飞书成功响应:{"StatusCode":0,"code":0} 或新版 {"code":0,"msg":"success"}
        return resp.status == 200 and ('"code":0' in body)
    except Exception as exc:  # noqa: BLE001
        print(f"[{_now()}] [SOURCE-ALERT] FEISHU send error: {type(exc).__name__}: {str(exc)[:200]}")
        return False


def _fire_and_forget(fn, *args, **kwargs) -> None:
    try:
        threading.Thread(target=fn, args=args, kwargs=kwargs, daemon=True).start()
    except Exception:
        pass


def _tick_once() -> Dict[str, Any]:
    """执行一次轮询。返回本次推送统计 {alerts: int, heartbeat: bool, stats: dict}。

    设计为可独立调用(测试用)+ 主循环驱动(每 N 秒一次)。
    """
    if not config.SOURCE_ALERT_ENABLED:
        return {"alerts": 0, "heartbeat": False, "skipped": "disabled"}

    url = config.FEISHU_WEBHOOK_URL
    if not url:
        return {"alerts": 0, "heartbeat": False, "skipped": "no-feishu-url"}

    snap = source_status.snapshot()
    now = _now_ts()
    threshold = config.SOURCE_ALERT_THRESHOLD
    alert_count = 0
    newly_failed: List[str] = []
    newly_recovered: List[str] = []
    stats = {"online": 0, "error": 0, "stale": 0, "unknown": 0}

    for entry in snap:
        sid = entry.get("source_id", "")
        status_now = entry.get("status", "unknown")
        stats[status_now] = stats.get(status_now, 0) + 1

        last_result = entry.get("last_result")
        consecutive_failures = entry.get("consecutive_failures", 0) or 0

        kind = _should_alert(
            sid, now=now, threshold=threshold,
            consecutive_failures=consecutive_failures,
            last_result=last_result, status_now=status_now,
        )

        prev_status = _last_state.get(sid)
        with _lock:
            _last_state[sid] = status_now

        if not kind:
            continue

        # flip 分类:error=新异常,online/stale=恢复(error→online)
        if kind == "flip":
            if status_now == "error":
                newly_failed.append(entry.get("label") or sid)
            elif prev_status == "error" and status_now in ("online", "stale"):
                newly_recovered.append(entry.get("label") or sid)

        card = _build_alert_card(sid, entry, kind)
        _fire_and_forget(_send_feishu_sync, card, url)
        with _lock:
            _last_alert_ts[sid] = now
        alert_count += 1

    # 心跳:周期性发送健康摘要
    heartbeat_sent = False
    heartbeat_minutes = config.SOURCE_ALERT_HEARTBEAT_MIN
    if heartbeat_minutes > 0:
        global _last_heartbeat_ts
        with _lock:
            should_heartbeat = (now - _last_heartbeat_ts) >= heartbeat_minutes * 60
        if should_heartbeat:
            card = _build_heartbeat_card(stats, newly_recovered, newly_failed)
            _fire_and_forget(_send_feishu_sync, card, url)
            with _lock:
                _last_heartbeat_ts = now
            heartbeat_sent = True

    return {"alerts": alert_count, "heartbeat": heartbeat_sent, "stats": stats}


async def data_source_watcher(loop) -> None:
    """主循环:每 SOURCE_ALERT_INTERVAL 秒轮询一次。永不退出(until cancelled)。"""
    interval = max(30, config.SOURCE_ALERT_INTERVAL)
    print(f"[{_now()}] [SOURCE-ALERT] watcher 启动, 间隔 {interval}s, "
          f"阈值 {config.SOURCE_ALERT_THRESHOLD}, 静默 {config.SOURCE_ALERT_SILENT_MIN}m, "
          f"心跳 {config.SOURCE_ALERT_HEARTBEAT_MIN}m, enabled={config.SOURCE_ALERT_ENABLED}, "
          f"心脏源 {len(HEARTBEAT_SOURCES)} 个")

    # 冷启修复:把 _last_heartbeat_ts 初始化为 now,
    # 否则首轮 (now - 0.0) >= heartbeat_minutes*60 永远成立,
    # 重启即触发一次多余的心跳推送。
    global _last_heartbeat_ts
    _last_heartbeat_ts = _now_ts()

    # 首轮延后 5 秒,等 engine 其他 task 把 source_status 跑起来
    await asyncio.sleep(5)

    while True:
        try:
            await asyncio.to_thread(_tick_once)
        except asyncio.CancelledError:
            print(f"[{_now()}] [SOURCE-ALERT] watcher cancelled, exit")
            raise
        except Exception as exc:  # noqa: BLE001
            print(f"[{_now()}] [SOURCE-ALERT] tick error: {type(exc).__name__}: {str(exc)[:200]}")
        await asyncio.sleep(interval)


def reset_state() -> None:
    """测试钩子:清空模块级缓存状态。"""
    with _lock:
        _last_alert_ts.clear()
        _last_state.clear()
        global _last_heartbeat_ts
        _last_heartbeat_ts = 0.0