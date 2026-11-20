#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v322: LLM 额度保险丝（Budget Fuse）—— 给「巨额流量费」上硬闸门。

事故背景（2026-11-04，用户反馈「一下子把流量干没了」）：
    4 个 MiniMax 通道全挂（2×429 配额打满 + 2×401 key 失效），
    主循环每轮仍「认领一批 → 每条打主模型 + 3 个 fallback → 全失败 → 重试」，
    单条新闻最多 30+ 次 HTTP 请求 → 账户余额瞬间被打穿。

**为什么熔断器不够**：
    熔断只能防「通道全挂」这种离散故障，防不住「通道半死不活、偶尔成功一次」
    这种连续劣化 —— 那会持续、缓慢地烧掉巨额流量，而且熔断器永远不会触发。
    所以必须有一道**与通道健康度无关**的独立闸门：**看今天花了多少钱**。

本模块提供三道闸门（全部可配，0 = 不限制）：
    1. token 预算    LLM_DAILY_TOKEN_BUDGET    —— 今天最多消耗多少 token
    2. 请求数预算    LLM_DAILY_REQUEST_BUDGET  —— 今天最多发多少次 LLM 请求
                     （**最重要的一道**：失败请求也可能计费，按请求数卡才兜得住）
    3. 新闻条数预算  LLM_DAILY_NEWS_BUDGET     —— 今天最多让 AI 产出多少条信号

统计口径：ai_call_stats 表（v187 起每次 LLM 调用都写，含 tokens_in/tokens_out/status）
          + ai_decisions 表（今天产出的信号条数）。

设计原则：
    · 认领**之前**就检查 → 超预算时一条都不认领，零请求、零额度消耗；
    · 请求**发出之前**再检查一次 → 认领后预算被别的进程用光时也能挡住；
    · 统计查询失败时用**进程内计数器**兜底，绝不因为查不到账就放行无限烧；
    · 纯 stdlib + 可选 db 依赖，db 不可用时不崩（降级为进程内计数）。
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

# ── 进程内兜底计数（数据库不可用时用）────────────────────────────────────
_INPROC: Dict[str, int] = {"requests": 0, "tokens": 0}
_INPROC_LOCK = threading.Lock()

# ── 用量缓存（避免每次认领都查库）────────────────────────────────────────
_CACHE: Dict[str, Any] = {"ts": 0.0, "data": None}
_CACHE_LOCK = threading.Lock()
_CACHE_TTL_S = 30.0


def _cfg(name: str, default: float = 0.0) -> float:
    """从 config 读配置（config 读不到时退回环境变量，再退回默认值）。"""
    try:
        import config
        val = getattr(config, name, None)
        if val is not None:
            return float(val)
    except Exception:
        pass
    import os
    try:
        return float(os.getenv(name, str(default)) or 0)
    except (TypeError, ValueError):
        return float(default)


# ── v501 欠费熔断（按范围，不做"一刀切")──────────────────────────────
# 事故（2026-11-20）：MiniMax 套餐 402 后，异动归因一条异动把 9 个通道各撞一次，
# 5 秒 10 发；token=0 但请求配额照扣、零决策 —— 纯干烧。
# 关键事实：402 是**按 key/套餐**的，不是全站统一。实测 main/minimax2 套餐已耗尽，
# 但 minimax-gate、verify 的 peer 通道仍在正常出结果（127 token 一次）。
# 所以熔断按 scope 记（main / anomaly / gate / all），只停真死的那条，不停全站。
import json as _json

_QUOTA_STATE_PATH = None


def _quota_state_path():
    global _QUOTA_STATE_PATH
    if _QUOTA_STATE_PATH is None:
        import os
        _QUOTA_STATE_PATH = os.path.join(os.path.expanduser("~"),
                                         ".trident", "state", "LLM_QUOTA_DEAD.json")
    return _QUOTA_STATE_PATH


def _read_quota_state() -> dict:
    try:
        with open(_quota_state_path()) as f:
            d = _json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _write_quota_state(d: dict) -> None:
    import os
    try:
        p = _quota_state_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w") as f:
            _json.dump(d, f)
        os.replace(tmp, p)
    except Exception as e:
        print("[QUOTA-DEAD] 写状态失败：%s: %s" % (type(e).__name__, e), flush=True)


def note_quota_dead(minutes: float = 30.0, reason: str = "", scope: str = "all") -> None:
    """标记某条链路欠费/用量打满，期间该 scope 的调用一律短路。"""
    import time as _t
    d = _read_quota_state()
    d[str(scope or "all")] = {"until": _t.time() + max(60.0, minutes * 60.0),
                             "reason": str(reason)[:160]}
    _write_quota_state(d)
    print("[QUOTA-DEAD] %s 短路 %d 分钟（%s）" % (scope, int(minutes), str(reason)[:80]),
          flush=True)


def quota_dead(scope: str = "all") -> bool:
    """True = 该 scope 处于欠费窗口，调用方应直接返回、不发请求。"""
    import time as _t
    d = _read_quota_state()
    now_s = _t.time()
    alive = {k: v for k, v in d.items()
             if isinstance(v, dict) and float(v.get("until") or 0) > now_s}
    if len(alive) != len(d):
        _write_quota_state(alive)
    hit = [k for k in ("all", str(scope or "all")) if k in alive]
    return bool(hit)


def clear_quota_dead(scope: str = "") -> None:
    if not scope:
        _write_quota_state({})
        return
    d = _read_quota_state()
    d.pop(scope, None)
    _write_quota_state(d)


def token_budget() -> float:
    return max(0.0, _cfg("LLM_DAILY_TOKEN_BUDGET", 0.0))


def request_budget() -> float:
    return max(0.0, _cfg("LLM_DAILY_REQUEST_BUDGET", 0.0))


def news_budget() -> float:
    return max(0.0, _cfg("LLM_DAILY_NEWS_BUDGET", 0.0))


# ── v322: 速率闸门（Rate Fuse）—— 治「瞬时打满」 ─────────────────────────
#
# 为什么光有「总量闸门」不够：
#   总量闸门管的是「今天一共能花多少」，管不住「这一秒同时打出去多少」。
#   事故形态（用户描述的「突然抽风」）：
#     ① 通道恢复 → 熔断冷却结束 → 所有排队请求同时醒来
#     ② 几十个并发同时打向同一个 key → 立刻 429
#     ③ 429 触发熔断 → 再次全挂 → 循环
#   这就是「恢复瞬间的补偿性重试风暴」，和总额度无关。
#
# 速率闸门在**真正发 HTTP 请求之前**扣一个名额：
#   · 跨进程统一计数（engine / api / review_agent / judge_worker 四个进程
#     各打各的额度池，不统一就会把并发放大 4 倍）
#   · 滑动窗口（最近 60 秒），不是固定整分钟窗口 —— 避免「整点对齐」的突发
#   · flock 排他锁 + 原子替换写，保证并发扣减不丢不重
#
# 默认 0 = 不限制（完全不影响使用）；生产建议 120。

_RPM_DIR = Path(os.getenv("TRIDENT_RATE_DIR") or
                (Path(__file__).resolve().parent.parent / "runtime"))
_RPM_LOCK = _RPM_DIR / "llm_rpm.lock"
_RPM_DATA = _RPM_DIR / "llm_rpm_slots.json"
_RPM_WINDOW_S = 60.0

# ── 2026-11-15: 最小间隔闸门（治 429 的并发风暴）──────────────────────────
_RPM_LAST = _RPM_DIR / "llm_last_call.json"
# v448（2026-11-18）：门卫单独一套节奏，不和主分析抢同一个 last_ts。
_RPM_LAST_GATE = _RPM_DIR / "llm_last_call_gate.json"
MAX_PACING_WAIT_S = 15.0   # 单次最大排队时长，闸门不把进程挂死


def min_interval() -> float:
    """两次 LLM 请求之间的最小间隔（秒）。0 = 关闭（默认，测试不受影响）。"""
    try:
        return max(0.0, float(os.getenv("LLM_MIN_INTERVAL_S", "0") or 0))
    except (TypeError, ValueError):
        return 0.0


def gate_min_interval() -> float:
    """只给 purpose='gate' 的最小间隔（秒）。默认 1.0：门卫请求量最大，
    完全不排队就是 429 风暴（实测单次 15.8s、33% http_error）。"""
    try:
        return max(0.0, float(_cfg("GATE_MIN_INTERVAL_S", 1.0) or 0))
    except (TypeError, ValueError):
        return 1.0


def _read_last_call(path=None) -> float:
    path = path or _RPM_LAST
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return float(data.get("last_ts") or 0.0)
    except Exception:
        return 0.0


def _write_last_call(ts: float, path=None) -> None:
    path = path or _RPM_LAST
    tmp = None
    try:
        _RPM_DIR.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp.%d" % os.getpid())
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"last_ts": round(float(ts), 3)}, fh)
        os.replace(str(tmp), str(_RPM_LAST))
    except Exception:
        if tmp is not None:
            try:
                tmp.unlink()
            except Exception:
                pass


def max_rpm() -> float:
    """每分钟允许的最大 LLM 请求数（0 = 不限制）。"""
    return max(0.0, _cfg("LLM_MAX_RPM", 0.0))


def _rate_lock():
    """取得速率闸门的排他锁（跨进程）。"""
    import fcntl
    _RPM_DIR.mkdir(parents=True, exist_ok=True)
    fh = open(_RPM_LOCK, "a+")
    fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
    return fh


def _read_slots(now: float, window_s: float, path=None) -> list:
    """读最近窗口内的调用时间戳。path 缺省用共享窗口（探测走独立窗口）。"""
    path = path or _RPM_DATA
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        stamps = data.get("slots") if isinstance(data, dict) else None
        if not isinstance(stamps, list):
            return []
    except Exception:
        return []
    out = []
    for ts in stamps:
        try:
            ts = float(ts)
        except (TypeError, ValueError):
            continue
        if now - ts < window_s:
            out.append(ts)
    return out


def _write_slots(slots: list) -> None:
    _write_slots_to(slots, _RPM_DATA)


def _write_slots_to(slots: list, path) -> None:
    tmp = None
    try:
        _RPM_DIR.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp.%d" % os.getpid())
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"slots": [round(s, 3) for s in slots], "updated_at": time.time()},
                      fh)
        os.replace(str(tmp), str(path))
    except Exception:
        if tmp is not None:
            try:
                tmp.unlink()
            except Exception:
                pass


def consume_rate_slot(purpose: str = "main",
                      window_s: float = _RPM_WINDOW_S) -> Tuple[bool, str]:
    """**发请求前**调用：原子扣一个速率名额。

    返回 (是否放行, 原因)。放行=False 时**不要发请求**，稍后重试即可。
    扣减是跨进程的 —— 四个进程共享同一个滑动窗口，谁先到谁占位。

    2026-11-15 追加（根治 MiniMax 429）：`LLM_MIN_INTERVAL_S` 最小间隔闸门。
    背景：9 条"独立通道"实际是同一个账户（7 个 key 只有 1 个不同值），
    门卫/异动/主分析在同一秒并发 → 3 条 429；429 又被熔断器记成 quota，
    冷却最长 36 分钟，整条决策链路停摆。
    做法：在这里做"跨进程排队"——按 last_call_ts 预留下一个可发起时刻，
    到点才放行。调用方代码不用改，只是被自然拉开间距。
    设 0（默认）即完全关闭，行为与改动前一致。
    """
    cap = max_rpm()
    interval = min_interval()
    # v448：门卫单独节奏（独立 last_ts 文件），不和主分析抢同一个时间槽。
    _is_gate = (purpose or "").strip().lower() == "gate"
    _last_path = None
    if _is_gate:
        interval = max(interval, gate_min_interval())
        _last_path = _RPM_LAST_GATE
    if cap <= 0 and interval <= 0:
        return True, ""

    # v379: 探测用**独立速率窗口**，不与真实决策抢名额。
    # 原来探测和决策共用 30 次/分钟的窗口，探测 10 个通道一轮就把名额吃掉 1/3，
    # 决策高峰时探测必被拦 → 面板把通道误判成掉线（实测 10 个通道显示成 6/10）。
    # 独立窗口上限 = max(2, cap*0.2)，探测再怎么跑也吃不到 5 次/分钟以上。
    _is_probe = (purpose or "").strip().lower() == "probe"
    _slot_path = None
    if _is_probe:
        cap = max(2.0, cap * 0.2)
        try:
            _slot_path = _RPM_DATA.with_name(_RPM_DATA.name + ".probe")
        except Exception:
            _slot_path = None

    now = time.time()
    wait_s = 0.0
    fh = None
    try:
        fh = _rate_lock()
        if cap > 0:
            slots = _read_slots(now, window_s, _slot_path)
            if len(slots) >= cap:
                return False, ("LLM 速率闸门拦截（最近 %ds 内已 %d/%d 次，"
                               "purpose=%s）" % (int(window_s), len(slots), int(cap), purpose))
            slots.append(now)
            if _slot_path is not None:
                _write_slots_to(slots, _slot_path)
            else:
                _write_slots(slots)

        if interval > 0:
            # 预留"下一个可发起的时刻"：now 与 last+interval 取大者。
            # 预留是原子的（同一把锁），因此多个进程不会同时挤进同一时刻。
            last = _read_last_call(_last_path)
            slot_at = max(now, last + interval)
            wait_s = slot_at - now
            # 上限保护：闸门不能把进程挂死（最长 15s），宁可多一次 429。
            wait_s = min(wait_s, MAX_PACING_WAIT_S)
            _write_last_call(now + wait_s, _last_path)
        ok_out, why_out = True, ""
    except Exception:
        # 闸门自身故障 → 放行（保护机制不能反过来把系统打死）
        ok_out, why_out = True, ""
    finally:
        if fh is not None:
            try:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                fh.close()
            except Exception:
                pass
    # 注意：sleep 必须在释放锁之后，否则会把别的进程一起堵在这把锁上。
    if wait_s > 0:
        time.sleep(wait_s)
    return ok_out, why_out


def rate_state() -> Dict[str, Any]:
    """当前速率窗口用量（供面板/体检展示）。"""
    cap = max_rpm()
    now = time.time()
    try:
        used = len(_read_slots(now, _RPM_WINDOW_S))
    except Exception:
        used = 0
    return {
        "max_rpm": cap,
        "window_s": _RPM_WINDOW_S,
        "used_in_window": used,
        "ok": (cap <= 0) or (used < cap),
    }


# ── v322: 额度水位预警 ─────────────────────────────────────────────────────
#
# 治「用着用着突然全停」：
#   额度闸门是**硬闸门** —— 到点就一条都不放，引擎立刻进入 pool-blocked，
#   前端大面积显示「待分析」。用户感知就是「突然抽风/服务挂了」，
#   但其实只是额度刚好到线。
#   解决办法不是放开闸门，而是**提前告知**：
#   用量到 80% / 90% 时推一次微信，让你有时间决定充值还是调额。
#
# 去重：同一档位每天只推一次（跨进程，用同一把 flock 锁 + 原子写）。

_WARN_FILE = _RPM_DIR / "llm_budget_warn.json"
_WARN_LAST_CHECK = 0.0


def warn_levels() -> list:
    """水位档位（默认 0.8 / 0.9）。LLM_WARN_RATIO 可覆盖，逗号分隔。"""
    raw = os.getenv("LLM_WARN_RATIO", "0.8,0.9")
    out = []
    for part in str(raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            val = float(part)
        except ValueError:
            continue
        if 0.0 < val <= 1.0:
            out.append(val)
    return sorted(out)


def _warn_claim(tag: str) -> bool:
    """同一 tag 每天只推一次。返回 True 表示「本次该推」。"""
    day = _today_str()
    fh = None
    try:
        fh = _rate_lock()
        data: Dict[str, Any] = {}
        try:
            with open(_WARN_FILE, "r", encoding="utf-8") as fh2:
                loaded = json.load(fh2)
            if isinstance(loaded, dict):
                data = loaded
        except Exception:
            data = {}
        rec = data.get(tag)
        if isinstance(rec, dict) and rec.get("day") == day:
            return False
        data[tag] = {"day": day, "ts": time.time()}
        try:
            _RPM_DIR.mkdir(parents=True, exist_ok=True)
            tmp = _WARN_FILE.with_suffix(".tmp.%d" % os.getpid())
            with open(tmp, "w", encoding="utf-8") as fh2:
                json.dump(data, fh2, ensure_ascii=False)
            os.replace(str(tmp), str(_WARN_FILE))
        except Exception:
            pass
        return True
    except Exception:
        return False
    finally:
        if fh is not None:
            try:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                fh.close()
            except Exception:
                pass


def _push(title: str, body: str) -> None:
    """推预警（失败不影响主流程）。

    v322 修复：**多通道兜底 + 失败可见**。
    事故（2026-11-05）：额度打满时你「毫不知情」—— 因为预警**只发微信
    一个通道**，而 Server酱返回 400（SendKey 失效/被限）→ 预警石沉大海
    → 直到全局闸门拦停才发现「服务不更新了」。
    现在：微信 + 邮件 + PushPlus 并联派发（任一成功即算送达），
    并**打印每个通道的结果**（失败不再被吞掉）。
    """
    try:
        import notification_channels
        try:
            notification_channels.register_known_channels()
        except Exception:
            pass
        # 关键告警走**全部**已配置通道，不只微信 ——
        # 单通道故障 = 预警静默 = 用户被偷袭。
        res = notification_channels.dispatch(title, body)
        if isinstance(res, dict) and res:
            ok_list = [k for k, v in res.items() if (v or {}).get("ok")]
            bad_list = ["%s(%s)" % (k, (v or {}).get("info", "")[:60])
                        for k, v in res.items() if not (v or {}).get("ok")]
            if ok_list:
                print("[BUDGET-WARN] 预警已送达: %s" % ",".join(ok_list), flush=True)
            if bad_list:
                print("[BUDGET-WARN] 预警通道失败: %s" % "; ".join(bad_list),
                      flush=True)
    except Exception as e:
        print("[BUDGET-WARN] 预警派发异常（不影响主流程）: %s: %s"
              % (type(e).__name__, e), flush=True)


def push_critical(title: str, body: str) -> None:
    """关键告警派发（多通道并联 + 每个通道的送达结果都打印）。

    v322：额度水位预警之外，**「全通道不可用」**这类会让系统静默停摆的
    故障也必须主动推送 —— 否则用户只看到「待分析不涨」，以为系统抽风。
    复用 _push 的派发路径（微信 + 邮件 + PushPlus 并联）。
    """
    _push(title, body)


def check_warnings(force: bool = False) -> list:
    """检查额度水位，跨过档位则推送一次。返回本次推送的档位描述列表。"""
    global _WARN_LAST_CHECK
    now = time.time()
    if not force and (now - _WARN_LAST_CHECK) < 60.0:
        return []
    _WARN_LAST_CHECK = now
    fired = []
    try:
        levels = warn_levels()
        if not levels:
            return []
        u = usage()
        tok_cap = token_budget()
        req_cap = request_budget()
        dims = (
            ("token", float(u.get("tokens") or 0), tok_cap, "token"),
            ("request", float(u.get("requests") or 0), req_cap, "次请求"),
        )
        for ratio in levels:
            for dim, used, cap, unit in dims:
                if cap <= 0:
                    continue
                pct = used / cap
                if pct < ratio:
                    continue
                tag = "%s_%.0f" % (dim, ratio * 100)
                if not _warn_claim(tag):
                    continue
                title = "⚠️ LLM 额度水位 %.0f%%" % (ratio * 100)
                body = ("Trident 金融后端\n\n"
                        "%s 用量已达预算的 %.1f%%\n"
                        "已用：%s / %s\n\n"
                        "到 100%% 会触发硬闸门，AI 分析会**整批停摆**。\n"
                        "处理办法：① 充值额度；② 调大 .env 里的 "
                        "LLM_DAILY_TOKEN_BUDGET / LLM_DAILY_REQUEST_BUDGET；"
                        "③ 或设 LLM_GLOBAL_KILL_SWITCH=1 主动停掉别烧钱。\n\n"
                        "（每个档位每天只提醒一次）"
                        % (unit, pct * 100, format(int(used), ","),
                           format(int(cap), ",")))
                _push(title, body)
                fired.append("%s %.0f%%" % (unit, ratio * 100))
    except Exception:
        pass
    return fired


def _day_start_ts(now: Optional[float] = None) -> int:
    """今天本地时间 00:00:00 的 unix 时间戳。"""
    t = time.localtime(now if now is not None else time.time())
    return int(time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1)))


def _today_str(now: Optional[float] = None) -> str:
    t = time.localtime(now if now is not None else time.time())
    return time.strftime("%Y-%m-%d", t)


def _query(sql: str, params: Tuple[Any, ...] = ()) -> Optional[Tuple[Any, ...]]:
    """执行一条只读查询。返回 None 表示查询不可用（db 异常/表不存在）。

    v322：失败时**重试一次**（0.3s 后）。首次查询常撞上其它进程的写锁，
    单次失败就放弃会让额度账本短暂"失明" → 闸门误判为没超预算 → 放行烧钱。
    """
    import time as _t
    for attempt in (0, 1):
        try:
            import db
            conn = db.get_connection()
            cur = conn.cursor()
            cur.execute(sql, params)
            row = cur.fetchone()
            try:
                conn.close()
            except Exception:
                pass
            return row
        except Exception:
            if attempt == 0:
                _t.sleep(0.3)
    return None



def usage_by_purpose(purpose: str, refresh: bool = False) -> Dict[str, int]:
    """某个用途今天消耗的 token / 请求数。

    v322 新增：**按用途分账**。
    事故（2026-11-05）：20.08M token 只产出 43 条信号 —— 因为
    同行评审（purpose='verify'）每条高价值新闻要跑 2 个模型、
    每次都带完整八段 prompt（10K+ token）→ 评审把**主分析**的预算吃光，
    闸门到线后主循环直接停摆 → 「待分析」不降。

    分账后可以给次要用途单独设小预算：评审用完自己的额度就停，
    但**不会**让主分析无预算可用。
    """
    now = time.time()
    day_ts = _day_start_ts(now)
    row = _query(
        "SELECT COUNT(*), COALESCE(SUM(tokens_in + tokens_out), 0) "
        "FROM ai_call_stats WHERE ts >= ? AND key_alias != 'local' AND purpose = ? "
        "AND (status = 'ok' OR (tokens_in + tokens_out) > 0)",
        (day_ts, str(purpose or "")),
    )
    requests = 0
    tokens = 0
    if row is not None:
        try:
            requests = int(row[0] or 0)
            tokens = int(row[1] or 0)
        except (TypeError, ValueError, IndexError):
            pass
    return {"requests": requests, "tokens": tokens}


def purpose_token_budget(purpose: str) -> float:
    """某用途的独立 token 上限（0 = 不限制，走全局预算）。

    配置：LLM_PURPOSE_TOKEN_BUDGET="verify=3000000,translate=2000000"
    未列出的用途 → 0（不限制）。
    """
    raw = os.getenv("LLM_PURPOSE_TOKEN_BUDGET", "") or ""
    for part in str(raw).split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, _, val = part.partition("=")
        if name.strip() != str(purpose or "").strip():
            continue
        try:
            return max(0.0, float(val))
        except (TypeError, ValueError):
            return 0.0
    return 0.0


def check_purpose_budget(purpose: str, est_tokens: int = 0) -> Tuple[bool, str]:
    """按用途的独立预算检查（主预算之外的一道）。

    与 `check()` 的区别：这里只看**该用途自己**的消耗。
    次要用途（评审/翻译）花超自己的额度就停 → 不再挤占主分析的预算。
    """
    cap = purpose_token_budget(purpose)
    if cap <= 0:
        return True, ""
    try:
        u = usage_by_purpose(purpose)
    except Exception:
        return True, ""          # 账本读不到 → 不在这里拦（主闸门会兜底）
    used = float(u.get("tokens") or 0)
    if used >= cap:
        return False, ("%s 用途预算已用尽 (%.0f/%.0f token)"
                       % (purpose, used, cap))
    if est_tokens > 0 and used + est_tokens > cap:
        return False, ("%s 用途剩余额度不足本次调用 (需≈%.0f，剩%.0f)"
                       % (purpose, est_tokens, cap - used))
    return True, ""


def usage(refresh: bool = False) -> Dict[str, int]:
    """今天的用量快照。

    返回 {"tokens": int, "requests": int, "news": int, "day": "YYYY-MM-DD"}
    tokens   = ai_call_stats 里 tokens_in + tokens_out 之和（**不含本地通道**）
    requests = ai_call_stats 的调用总条数（**不含本地通道**，含失败 —— 失败也可能计费）
    news     = ai_decisions 今天产出的信号条数

    **为什么排除本地通道**：本地 Ollama 不花钱。把它算进预算会导致
    "本地优先"这个省钱手段反过来更快耗尽请求预算（自相矛盾）——
    实测本地门卫每次调用都会写一条 ai_call_stats 埋点。
    """
    now = time.time()
    with _CACHE_LOCK:
        if (not refresh and _CACHE["data"] is not None
                and now - float(_CACHE["ts"]) < _CACHE_TTL_S):
            return dict(_CACHE["data"])  # type: ignore[arg-type]

    day_ts = _day_start_ts(now)
    day = _today_str(now)
    tokens = 0
    requests = 0
    news = 0
    db_ok = False

    # v480：请求预算只数「真的会计费」的请求。
    # 旧口径 COUNT(*) 把 status='http_error' 且 tokens=0 的失败一起算进预算
    # （门卫 read timeout / 429 都是这种），于是 9,000 次预算被白烧的失败提前吃光，
    # 门卫先被闸门拦停 → 新闻全部裸奔进主分析（单条 ~13k token，是门卫的 16 倍）→
    # 反而更快打穿 token 上限。实测 11-19：7,206 次里约一半是零 token 失败。
    # 新口径：status='ok' 或 tokens>0（计费的失败仍算），只排本地通道。
    row = _query(
        "SELECT COUNT(*), COALESCE(SUM(tokens_in + tokens_out), 0) "
        "FROM ai_call_stats WHERE ts >= ? AND key_alias != 'local' "
        "AND (status = 'ok' OR (tokens_in + tokens_out) > 0)",
        (day_ts,),
    )
    if row is not None:
        db_ok = True
        try:
            requests = int(row[0] or 0)
            tokens = int(row[1] or 0)
        except (TypeError, ValueError, IndexError):
            db_ok = False

    row2 = _query(
        "SELECT COUNT(*) FROM ai_decisions WHERE created_at >= ?",
        (day + " 00:00:00",),
    )
    if row2 is not None:
        try:
            news = int(row2[0] or 0)
        except (TypeError, ValueError, IndexError):
            pass
    elif not db_ok:
        # 数据库完全不可用 → 用进程内计数兜底
        with _INPROC_LOCK:
            requests = int(_INPROC["requests"])
            tokens = int(_INPROC["tokens"])

    data = {"tokens": tokens, "requests": requests, "news": news, "day": day,
            "db_ok": db_ok}
    # v322 三次加固：把「最近一次读到的真实用量」同步进进程内基线。
    # 为什么必须同步：埋点写库会因写锁失败（database is locked），
    # 于是 ai_call_stats 会**少记**实际发生的调用 → 账本偏小 → 闸门放松 →
    # 又放行一批请求。实测（2026-11-04）老进程计数到 4591 而库里只有 4584，
    # 差的 7 次就是「烧了但没记上」。
    # 同步后 _INPROC 始终 = 最近一次已知的真实用量（而不是 0 或陈旧基线），
    # DB 短暂失明时用它兜底 → 闸门按「已用不少」处理，方向保守。
    if db_ok:
        with _INPROC_LOCK:
            _INPROC["requests"] = max(_INPROC["requests"], requests)
            _INPROC["tokens"] = max(_INPROC["tokens"], tokens)
    with _CACHE_LOCK:
        _CACHE["ts"] = now
        _CACHE["data"] = dict(data)
    return data


def bootstrap() -> None:
    """进程启动时读一次当天用量，写进进程内基线。

    为什么必须做：`_INPROC` 是进程内计数，**重启后归零**。
    如果 DB 恰好在此时不可用，闸门会看到"requests=0" → 判定没超预算 → 放行，
    于是重启后的第一轮请求全部漏过闸门（2026-11-04 实测漏了 2 次）。

    先把 DB 里的当天真实用量灌进 _INPROC，之后即使 DB 再次失明，
    兜底值也仍然是正确的"当天已用量"而不是 0。
    """
    try:
        u = usage(refresh=True)
        if not u.get("db_ok"):
            return
        with _INPROC_LOCK:
            _INPROC["requests"] = max(_INPROC["requests"], int(u["requests"]))
            _INPROC["tokens"] = max(_INPROC["tokens"], int(u["tokens"]))
    except Exception:
        pass


def fail_closed_enabled() -> bool:
    """账本不可读时是否按"已超预算"处理（保守止血）。

    默认 1（fail-closed）：宁可停下也不烧钱。
    设 0 可恢复"账本读不到就放行"的宽松行为。
    """
    try:
        import config
        val = getattr(config, "LLM_BUDGET_FAIL_CLOSED", None)
        if val is not None:
            return bool(val)
    except Exception:
        pass
    import os
    return os.getenv("LLM_BUDGET_FAIL_CLOSED", "1").strip().lower() in (
        "1", "true", "yes")


def record_call(tokens: int = 0) -> None:
    """进程内计数兜底 + 失效缓存（让下一次 usage() 立刻反映新消耗）。"""
    with _INPROC_LOCK:
        _INPROC["requests"] += 1
        _INPROC["tokens"] += max(0, int(tokens))
    with _CACHE_LOCK:
        _CACHE["ts"] = 0.0
        _CACHE["data"] = None


def estimate_call_tokens(prompt_text: str, max_output_tokens: int = 4000) -> int:
    """粗估一次调用的 token 消耗。

    中文/英文混排的经验系数：字符数 / 2.2 ≈ token（比英文的 /4 保守）。
    估高不估低 —— 宁可提前拦住，也不要烧超。
    """
    try:
        chars = len(prompt_text or "")
    except Exception:
        chars = 0
    return int(chars / 2.2) + max(0, int(max_output_tokens))


def kill_switch_on() -> bool:
    """全局急停开关：打开时**任何** LLM 调用都不发出去。"""
    try:
        import config
        return bool(getattr(config, "LLM_GLOBAL_KILL_SWITCH", False))
    except Exception:
        import os
        return os.getenv("LLM_GLOBAL_KILL_SWITCH", "0").strip().lower() in (
            "1", "true", "yes")


def check(est_tokens: int = 0, purpose: str = "main") -> Tuple[bool, str]:
    """额度闸门检查。返回 (是否放行, 原因)。

    调用点有三个：
        · 主循环认领前   check(0)           —— 0 表示不预估单条成本
        · 每次请求发出前 check(est_tokens)   —— 预估本次要花多少
        · 门卫等辅助路径                        —— 同样受限
    """
    if kill_switch_on():
        return False, "全局急停开关已打开（LLM_GLOBAL_KILL_SWITCH=1）"

    u = usage()

    # ── v322: 账本不可读 → fail-closed ─────────────────────────────────
    # 账本失明时不能当成"没超预算"（那等于把闸门自己关掉）。
    # 事故复现：引擎重启后 _INPROC 归零，DB 恰好被写锁挡住 →
    # 闸门看到 requests=0 → 放行 → 重启后第一批请求全部漏过闸门。
    if not u.get("db_ok", True) and fail_closed_enabled():
        return False, ("额度账本不可读（DB 异常），按保守策略拦截"
                       "（设 LLM_BUDGET_FAIL_CLOSED=0 可放开）")
    tok_cap = token_budget()
    req_cap = request_budget()
    news_cap = news_budget()

    # ── v322: 速率闸门（只读检查，不扣名额）───────────────────────────────
    # 这里只看不说；真正扣名额在 consume_rate_slot()（发请求前调）。
    # 扣减必须放在发请求那一刻 —— 放在这里会白扣（认领阶段可能根本不发）。
    rpm_cap = max_rpm()
    if rpm_cap > 0:
        try:
            used = len(_read_slots(time.time(), _RPM_WINDOW_S))
        except Exception:
            used = 0
        if used >= rpm_cap:
            return False, ("LLM 速率已到上限（最近 %ds 内 %d/%d 次）"
                           % (int(_RPM_WINDOW_S), used, int(rpm_cap)))

    if req_cap > 0 and u["requests"] >= req_cap:
        return False, (f"日请求数预算已用尽 "
                       f"({u['requests']:.0f}/{req_cap:.0f} 次)")
    if news_cap > 0 and u["news"] >= news_cap:
        return False, (f"日信号条数预算已用尽 "
                       f"({u['news']:.0f}/{news_cap:.0f} 条)")
    if tok_cap > 0:
        used = u["tokens"]
        if used >= tok_cap:
            return False, (f"日 token 预算已用尽 "
                           f"({used:.0f}/{tok_cap:.0f})")
        if est_tokens > 0 and used + est_tokens > tok_cap:
            return False, (f"剩余额度不足本次调用 "
                           f"(需≈{est_tokens:.0f}，剩 {tok_cap - used:.0f})")
    # ── v322: 水位预警（提前告知，避免到线突然全停）────────────────────
    # 内部有 60 秒节流 + 每天每档去重，不会成为热路径负担。
    try:
        check_warnings()
    except Exception:
        pass
    return True, ""


def state() -> Dict[str, Any]:
    """完整额度状态（供 scripts/budget_status.py 与 /api 面板）。"""
    u = usage()
    tok_cap = token_budget()
    req_cap = request_budget()
    news_cap = news_budget()
    ok, reason = check()
    return {
        "day": u["day"],
        "tokens_used": u["tokens"],
        "requests_used": u["requests"],
        "news_used": u["news"],
        "token_budget": tok_cap,
        "request_budget": req_cap,
        "news_budget": news_cap,
        "token_remaining": max(0.0, tok_cap - u["tokens"]) if tok_cap > 0 else None,
        "request_remaining": max(0.0, req_cap - u["requests"]) if req_cap > 0 else None,
        "news_remaining": max(0.0, news_cap - u["news"]) if news_cap > 0 else None,
        "ok": ok,
        "reason": reason,
        "rate": rate_state(),
    }


# ── 进程启动即灌入当天用量基线 ─────────────────────────────────────────
# 必须在任何 check() 之前完成，否则重启后第一轮可能漏过闸门。
try:
    bootstrap()
except Exception:
    pass


if __name__ == "__main__":
    import json
    print(json.dumps(state(), ensure_ascii=False, indent=2))