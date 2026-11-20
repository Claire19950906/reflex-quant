"""模型通道探测 + 健康度缓存（api_server 第二刀）。

从 api_server.py 切出来的第二个依赖闭合块：**12 个函数 / 539 行，闭包外零外部函数**。
判定方法不是"名字像一类"，而是脚本做的传递闭包 + 自由变量分析：
   种子 = 前缀 _probe / _model_health / _kick_model_health / _classify_probe / _funnel_overlay / _is_local_base
   扩张 = 种子体里引用的 api_server 顶层函数
   结果 = 12 个函数自洽，且这 10 个状态常量在 api_server 里**再无其他读者**

状态归属说明（这是本刀唯一需要交代的语义）：
- `_MODEL_HEALTH_CACHE` / `_PROBE_DEAD_STATE` / `_MODEL_HEALTH_LAST_ERROR` 都是**就地改**
  （`.update()` / `[k]=v`），全仓没有一处 `global` 重新绑定 → import 回 api_server 是同一个对象，
  不存在"两本账"。
- 后台线程单入口仍是 `_ensure_model_health_thread()`，由 api_server 的 lifespan 调用。
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple

import circuit_breaker
import config
import db
import requests


_MODEL_HEALTH_CACHE: Dict[str, Any] = {"ts": 0.0, "value": None}
_MODEL_HEALTH_TTL = 180.0
_ENGINE_HEALTH_STALE_SEC = 1800.0
_QUOTA_EXHAUSTED_MARKERS = (
    "Token Plan",            # minimax 套餐额度用尽
    "用量上限",               # minimax 中文提示
    "QUOTA_EXHAUSTED",       # xinshu: API_KEY_QUOTA_EXHAUSTED
    "额度已用完",
    "insufficient_balance",  # 余额不足 (402)
    "余额不足",
)
_PROBE_DEAD_WINDOW_S = {
    "quota_exhausted": 1800.0,   # 30 分钟 (对齐 ai_worker 的 _ANALYSIS_QUOTA_COOLDOWN_LONG_S)
    "auth": 900.0,               # 15 分钟 (key 随时可能被换, 但不必每分钟试一次)
}
_PROBE_DEAD_STATE: Dict[str, Dict[str, Any]] = {}   # breaker_key → {category, until, note}
_PROBE_SAME_KEY_STAGGER = 0.8
_MODEL_HEALTH_LOCK = threading.Lock()
_MODEL_HEALTH_THREAD: Optional[threading.Thread] = None   # 后台刷新线程句柄（_ensure_model_health_thread 持有）          # 非阻塞抢锁：同一时刻只允许一个刷新在跑
_MODEL_HEALTH_LAST_ERROR: Dict[str, Any] = {"msg": "", "ts": 0.0}
_MODEL_HEALTH_REFRESH_MIN_GAP = 60.0           # 两次后台刷新最小间隔（秒，v379: 10→60）


def _db_path() -> str:
    """DB 路径的单一来源在 api_server（测试会 monkeypatch `api_server.DB_PATH` 指向临时库）。

    所以这里**不能在 import 期快照**，每次懒取；两处各写一遍 config.DB_PATH 迟早分叉。
    """
    import api_server

    return api_server.DB_PATH




def _probe_model_channels_now() -> Dict[str, Any]:
    """真正探测三个 LLM 通道（**只有后台线程调用**，不要把这条放回请求路径）。

    Step 1: probe_specs 单源来自 config.AI_MODEL_ROSTER（消除双真相源）。
    Step 3: 每个通道 wire-in circuit_breaker。

    返回：channels（三通道探测）+ recent_models（最近实际分析用的模型分布）
    + pending_news（待分析堆积）+ analyzed_24h（24h 分析量）。
    """
    now = time.monotonic()

    # 单真相源：所有 provider 池成员 + 配置中启用但未进 roster 的（如 OpenRouter key 恢复）
    # 都从这里取，杜绝「roster 改了但 probe_specs 没改」的漂移。
    probe_specs: List[tuple] = []
    for m in getattr(config, "AI_MODEL_ROSTER", ()):
        # request_model 是实际请求的模型名（id 带通道前缀保持唯一），roster 已标准化
        # v323: 多带一个 is_local —— 本地 Ollama 冷启动要 ~25s, 探测超时必须单独放宽
        probe_specs.append((
            m["label"],
            m.get("request_model") or m["id"],
            m["api_base"],
            m["api_key"],
            bool(m.get("use_proxy", False)),
            bool(m.get("is_local", False)),
            str(m.get("key_alias") or ""),
            list(m.get("purpose") or []),
        ))

    channels: List[Dict[str, Any]] = []
    if probe_specs:
        # 去重 + 同 key 错峰：见 _probe_specs_deduped 的说明。
        channels = _probe_specs_deduped(probe_specs)
    recent_models: List[Dict[str, Any]] = []
    current_model: Optional[str] = None
    current_model_at: Optional[str] = None
    pending_news: Optional[int] = None
    analyzed_24h: Optional[int] = None
    funnel_db_error = ""
    engine_openrouter: Optional[Dict[str, Any]] = None
    # v314 修复: engine_model_pool 必须在 try **之外**初始化。
    # 原来它写在 try 块内(读 runtime/model_pool.json 那一段的前一行), 一旦前面
    # 的 SQL 抛异常(生产上最常见的就是 "database is locked"), except 会把异常
    # 吞掉 → 该变量从未被赋值 → 组装 payload 时 UnboundLocalError →
    # /api/health/models 直接 500, 前端模型健康面板整块挂掉。
    # 与 engine_openrouter 一样先给 None 初值, 后续分支再覆盖。
    engine_model_pool: Optional[List[str]] = None
    try:
        conn = db.get_connection()
        try:
            # 最近 20 条分析的实际模型分布（反映"当前正在用哪个模型"，切换后立即体现）
            rows = conn.execute(
                """SELECT COALESCE(NULLIF(agent_model_id, ''), '(未知)') AS model, COUNT(*) AS n
                   FROM (SELECT agent_model_id FROM ai_decisions ORDER BY id DESC LIMIT 20)
                   GROUP BY agent_model_id ORDER BY n DESC"""
            ).fetchall()
            recent_models = [{"model": str(r[0]), "count": int(r[1])} for r in rows]
            # 当前模型 = 最新一条分析实际用的模型（最接近"当下"）
            cur = conn.execute(
                """SELECT COALESCE(NULLIF(agent_model_id, ''), '(未知)') AS model, created_at
                   FROM ai_decisions ORDER BY id DESC LIMIT 1"""
            ).fetchone()
            if cur:
                current_model = str(cur[0])
                current_model_at = str(cur[1])[:19]
            # v409: 漏斗数字统一走 news_funnel（只读连接 + ISO 口径 + 失败显形为 None）。
            #   原实现两个 bug：① `except: pass` 把"没读成"变成 0；
            #   ② cutoff 用空格格式与 created_at 的 T 格式字符串比较 → 实测虚高 68%（521 vs 310）。
            _funnel = _funnel_overlay()
            pending_news = _funnel.get("pending_news")
            analyzed_24h = _funnel.get("analyzed_24h")
            funnel_db_error = _funnel.get("funnel_db_error", "")
        finally:
            conn.close()
        # 引擎视角的 OpenRouter 探测状态（runtime/llm_health.json，引擎每次探测写入）
        # 追加 enabled / stale / age_sec，避免把一份几小时前的快照当成"当前故障"：
        #   - OpenRouter 默认停用（OPENROUTER_ENABLED=0，key 失效 401 "User not found."），
        #     面板应显示"已停用"，而不是长期挂一个红叉；
        #   - 引擎只在"策略触发开仓动作"前探测一次，两次探测间隔可能很长，
        #     超过阈值即视为陈旧快照（stale=true）。
        health_file = os.path.join(config.BASE_DIR, "runtime", "llm_health.json")
        if os.path.exists(health_file):
            try:
                with open(health_file, "r", encoding="utf-8") as f:
                    engine_openrouter = json.loads(f.read())
            except (OSError, json.JSONDecodeError):
                engine_openrouter = None
        if isinstance(engine_openrouter, dict):
            engine_openrouter = dict(engine_openrouter)
            engine_openrouter["enabled"] = bool(getattr(config, "OPENROUTER_ENABLED", False))
            try:
                _ts = float(engine_openrouter.get("ts") or 0.0)
            except (TypeError, ValueError):
                _ts = 0.0
            _age = (time.time() - _ts) if _ts else None
            engine_openrouter["age_sec"] = round(_age, 1) if _age is not None else None
            engine_openrouter["stale"] = bool(_age is not None and _age > _ENGINE_HEALTH_STALE_SEC)
        # 引擎视角的模型池（runtime/model_pool.json）
        pool_file = os.path.join(config.BASE_DIR, "runtime", "model_pool.json")
        if os.path.exists(pool_file):
            try:
                with open(pool_file, "r", encoding="utf-8") as f:
                    pool_data = json.loads(f.read())
                    if isinstance(pool_data.get("pool"), list):
                        engine_model_pool = pool_data["pool"]
            except (OSError, json.JSONDecodeError):
                engine_model_pool = None
    except Exception:
        pass
    # v453 P2-E15：探测是"一次结果抄给同 key 同模型的多个通道"（shared_probe），
    # 面板上会出现"全部 available / 200 / 9.3s"，而同一时刻 gate 有 26% 超时。
    # 这里补一块**真实调用**的近 30 分钟成败（来自 ai_call_stats），
    # 让"探测通过"和"实际可用"两个事实分开摆，不再用一个抄来的值冒充所有通道。
    empirical: Dict[str, Any] = {}
    _alert: Dict[str, Any] = {"level": "ok", "reason": "", "codes": []}
    try:
        _ec = db.get_connection()
        try:
            _since = int(time.time()) - 1800
            _rows = _ec.execute(
                "SELECT purpose, status, COUNT(*) FROM ai_call_stats"
                " WHERE ts >= ? GROUP BY purpose, status", (_since,)
            ).fetchall()
            _agg: Dict[str, Dict[str, int]] = {}
            for _p, _st, _n in _rows:
                _a = _agg.setdefault(str(_p or "?"), {"ok": 0, "fail": 0})
                if str(_st) == "ok":
                    _a["ok"] += int(_n)
                else:
                    _a["fail"] += int(_n)
            for _a in _agg.values():
                _t = _a["ok"] + _a["fail"]
                _a["ok_rate"] = round(_a["ok"] / _t, 3) if _t else None
            empirical = _agg
            # 汇总告警：近 30 分钟出现 402(余额/套餐耗尽) 或 401/403(key 失效)
            _ecodes = _ec.execute(
                "SELECT error_code, COUNT(*) FROM ai_call_stats"
                " WHERE ts >= ? AND status != 'ok' AND error_code IN ('402','401','403')"
                " GROUP BY error_code", (_since,)
            ).fetchall()
            _code_map = {str(r[0]): int(r[1]) for r in _ecodes}
            if _code_map.get("402"):
                _alert = {"level": "quota_exhausted",
                          "reason": "余额/套餐用量耗尽(402)，需充值或升级 Token Plan",
                          "codes": _code_map}
            elif _code_map.get("401") or _code_map.get("403"):
                _alert = {"level": "auth_failed",
                          "reason": "API key 失效/无权限(401/403)，需更换 key",
                          "codes": _code_map}
        finally:
            _ec.close()
    except Exception as _e:
        empirical = {"error": f"{type(_e).__name__}: {str(_e)[:80]}"}
    payload: Dict[str, Any] = {
        "channels": channels,
        "llm_empirical_30m": empirical,
        "llm_alert": _alert,
        "recent_models": recent_models,
        "current_model": current_model,
        "current_model_at": current_model_at,
        "pending_news": pending_news,
        "analyzed_24h": analyzed_24h,
        "funnel_db_error": funnel_db_error,
        "engine_openrouter": engine_openrouter,
        "engine_model_pool": engine_model_pool,
    }
    if any(bool(c.get("shared_probe")) for c in channels):
        payload["probe_caveat"] = (
            "shared_probe=True 的通道共用一次探测结果；真实可用性以 llm_empirical_30m 为准"
        )
    # -- v323 P0-1: 通道探针 wire-in worker_healthbeat ------------------
    # v171 设计的 worker_heartbeat 表一直没接入（实测全库只有 1 行 10-11 的
    # kg_reconcile 旧数据）-> 前端/运维看不到"通道体检"到底有没有在跑。
    # 现在每次探测都写一行：worker_name='llm_channels'，
    # status = ok(全通) / busy(部分可用) / error(全挂)。
    #
    # ⚠️ 必须**异步写**：引擎批次事务会持写锁数十秒，heartbeat 的 INSERT
    # 若在请求线程里同步做，会把 /api/health/models 一起阻塞成"前端假死"。
    # 所以丢给守护线程 + 独立的短 busy_timeout 连接（1.5s 拿不到锁就放弃）。
    try:
        _hb_ok = sum(1 for c in channels if c.get("available") is True)
        _hb_err = sum(1 for c in channels if c.get("available") is False)
        _hb_status = "ok" if _hb_err == 0 else ("busy" if _hb_ok > 0 else "error")
        _hb_detail = "ok=%d err=%d total=%d channels=%s" % (
            _hb_ok, _hb_err, len(channels),
            ",".join(str(c.get("name") or "") for c in channels)[:300],
        )

        def _hb_writer() -> None:
            try:
                import sqlite3 as _sqlite3
                import threading as _threading
                import observability as _obs
                _conn = _sqlite3.connect(db.get_db_path(), timeout=1.5)
                try:
                    _conn.execute("PRAGMA busy_timeout=1500;")
                    _obs.heartbeat_tick(
                        _conn, "llm_channels",
                        processed=_hb_ok, status=_hb_status,
                        error=("" if _hb_ok else "all LLM channels unavailable"),
                        detail=_hb_detail,
                    )
                finally:
                    try:
                        _conn.close()
                    except Exception:
                        pass
            except Exception as _hb_exc:
                print(f"[HEARTBEAT] llm_channels 写入失败: "
                      f"{type(_hb_exc).__name__}: {str(_hb_exc)[:80]}", flush=True)

        # v406 修复：`_threading` 只在 _hb_writer 内 import，外层这里引用会 NameError，
        # 心跳线程从来没被真正启动过 —— LLM 通道健康在监控里永远是"没数据"。
        import threading as _threading
        _threading.Thread(target=_hb_writer, daemon=True, name="llm_channels_hb").start()
    except Exception as _hb_exc:
        print(f"[HEARTBEAT] llm_channels 调度失败: "
              f"{type(_hb_exc).__name__}: {str(_hb_exc)[:80]}", flush=True)

    _MODEL_HEALTH_CACHE.update({"ts": now, "value": payload})
    # v376: 同时落一份文件快照 —— 重启后立刻能读到上次探测结果。
    # 不用 DB：引擎批次事务会持写锁数十秒，心跳/落库都写不进去（实测心跳停在 09:56），
    # 文件写入不受锁影响，冷启动零等待。
    try:
        import json as _json
        import os as _os
        _d = _os.path.join(_os.path.expanduser("~"), ".trident", "state")
        _os.makedirs(_d, exist_ok=True)
        _fp = _os.path.join(_d, "llm_probe.json")
        _tmp = _fp + ".tmp"
        with open(_tmp, "w", encoding="utf-8") as _f:
            _ok_n = sum(1 for c in channels if c.get("available") is True)
            _err_n = sum(1 for c in channels if c.get("available") is False)
            _json.dump({"ts": time.time(), "ok": _ok_n, "err": _err_n,
                        "total": len(channels), "channels": [
                            {"name": c.get("name"), "available": c.get("available")}
                            for c in channels]}, _f, ensure_ascii=False)
        _os.replace(_tmp, _fp)
    except Exception:
        pass
    return payload


def _runtime_circuit(alias: str, purpose: List[str]) -> Dict[str, Any]:
    """读取**实时调用链路**的熔断状态（与门卫/主分析同一组落盘熔断器）。

    背景：探针自有熔断器键是 `llm:{name}` / `llm:key:{hash}:{model}`，而实时
    门卫用 `llm_net/quota/auth/ratelimit:{alias}`、主分析用 `llm_main_*:{alias}`，
    两套命名空间互不复用 —— 探针轻量探测 3s 成功复位的是前者，实时通道在后者
    里 open，面板就出现"全绿但门卫在故障窗口"的假绿。这里把实时状态并进来，
    与探针结果分开摆：`circuit_state`=探测熔断，`runtime_circuit.state`=实时熔断。

    只读 status()，不调 allow()/failure()，不会改变实时熔断计数。
    """
    if not alias:
        return {"state": None, "breakers": {}}
    purpose = list(purpose or [])
    prefixes: List[str] = []
    # gate 系在 realtime_filter，用 llm_ 前缀；main 及其余专用通道
    # (reflection/verify/advisor/sql/optimizer) 都在 ai_worker，用 llm_main_ 前缀。
    if "gate" in purpose:
        prefixes.append("llm")
    if "main" in purpose or any(p in purpose for p in
                               ("reflection", "verify", "advisor", "sql", "optimizer")):
        prefixes.append("llm_main")
    if not prefixes:
        prefixes = ["llm", "llm_main"]
    detail: Dict[str, str] = {}
    rank = {"closed": 0, "half_open": 1, "open": 2}
    worst = "closed"
    for pref in prefixes:
        for kind in ("net", "quota", "auth", "ratelimit"):
            bname = f"{pref}_{kind}:{alias}"
            try:
                st = circuit_breaker.get(bname).status().get("state")
            except Exception:
                st = None
            if st:
                detail[f"{pref}_{kind}"] = str(st)
                if rank.get(str(st), 0) > rank.get(worst, 0):
                    worst = str(st)
    return {"state": worst if detail else None, "breakers": detail}


def _probe_channel(name: str, model: str, base: str, key: str, use_proxy: bool,
                   breaker_key: Optional[str] = None,
                   is_local: Optional[bool] = None) -> Dict[str, Any]:
    """探测单个 LLM 通道：轻量 chat（max_tokens=5），返回可用性/状态码/延迟/分类/熔断状态。

    circuit_breaker wire-in: 每次探测前调用 allow()；成功 → success() 复位；
    失败 → failure() 累计，达阈值熔断。被熔断时直接返回 category='circuit_open'，
    不再发起网络请求，避免反复打必失败通道。

    breaker_key: 熔断器的分组键。默认按通道名分组；**去重探测**时同组
    (key, model) 的多个通道共享一次结果，熔断状态也必须共享，所以要显式传
    同一个 breaker_key，否则只有组内第一个通道会累计失败。
    """
    brk_key = breaker_key or f"llm:{name}"
    is_local = _is_local_base(base) if is_local is None else bool(is_local)

    # ── v323: 「不会自愈」的通道进入静默窗口, 不再重复探测 ─────────────────
    # quota_exhausted (套餐/余额用完) / auth (key 失效) 这两类, **探测也不会好**:
    # 每 60s 再打一次既浪费额度又让面板反复闪红。命中后进入静默窗口:
    #   quota_exhausted → 30 分钟 (对齐 ai_worker 的 _ANALYSIS_QUOTA_COOLDOWN_LONG_S)
    #   auth            → 15 分钟 (key 随时可能被换, 但不必每分钟试一次)
    # 窗口内直接返回已知分类, **零网络请求**; 窗口过后自动重新探测 → 充值/换 key
    # 之后能自动恢复, 不需要重启进程。
    dead = _PROBE_DEAD_STATE.get(brk_key)
    if dead and time.time() < float(dead.get("until", 0.0)):
        return {
            "name": name, "model": model, "available": False,
            "status_code": None, "latency_ms": 0.0,
            "category": str(dead.get("category") or "unknown"),
            "circuit_state": "open",
            "note": str(dead.get("note") or ""),
        }

    # 本地通道 (Ollama) 的熔断窗口放宽: 冷启动实测 25s (见下面 timeout 说明),
    # 一次冷启动失败不该把本地兜底通道判死好几分钟。
    brk = circuit_breaker.get(
        brk_key, fail_threshold=3,
        open_seconds=180.0 if is_local else 60.0,
    )
    if not brk.allow():
        # 熔断中：短路返回，不消耗网络/额度
        return {
            "name": name, "model": model, "available": False,
            "status_code": None, "latency_ms": 0.0,
            "category": "circuit_open",
            "circuit_state": brk.state,
            "note": "circuit breaker open, skipped (cooldown)",
        }
    t0 = time.monotonic()
    note = ""
    status_code: Optional[int] = None
    exc: Optional[BaseException] = None

    # ── v322: 额度闸门 —— 通道体检本身也是计费请求 ─────────────────────
    # 面板上点一次「测试通道」= 一次真实远程调用。额度用光时不该再探：
    # 探针结果对当前决策毫无意义（反正什么都发不出去）。
    try:
        import llm_budget as _lb
        _ok_b, _reason_b = _lb.check(0, purpose="probe")
        if not _ok_b:
            return {
                "name": name, "model": model, "available": False,
                "status_code": None, "latency_ms": 0.0,
                "category": "budget_blocked",
                "gate_blocked": True,
                "note": "额度闸门拦截：%s（未发出请求）" % _reason_b,
            }
    except Exception:
        pass  # 预算模块异常 → 照常探（不因保护机制故障停摆）

    # ── v322: 速率闸门（探活也是真实计费请求，同样要扣名额）────────────
    try:
        import llm_budget as _lb_rpm
        _ok_rpm, _why_rpm = _lb_rpm.consume_rate_slot(purpose="probe")
        if not _ok_rpm:
            return {
                "name": name, "model": model, "available": False,
                "status_code": None, "latency_ms": 0.0,
                "category": "rate_limited",
                "gate_blocked": True,
                "circuit_state": brk.state,
                "note": "速率闸门拦截：%s（未发出请求）" % _why_rpm,
            }
    except Exception:
        pass  # 闸门自身异常 → 照常探

    try:
        session = requests.Session()
        session.trust_env = use_proxy  # True=走 env HTTP_PROXY（OpenRouter）；False=直连
        session.headers.update({"User-Agent": "Mozilla/5.0"})
        resp = session.post(
            f"{base}/chat/completions",
            json={"model": model, "messages": [{"role": "user", "content": "ok"}],
                  "max_tokens": 5, "temperature": 0},
            headers={"Authorization": f"Bearer {key}"},
            # 超时 (connect, read)：探测要能区分「慢」和「挂」。
            # 原来 (4, 6) 太紧：MiniMax 冷启动/偶发排队时 6s read 会误报成
            # network 异常（2026-11-02 实测 M2.7-highspeed 单发只要 1.5s，
            # 但连续探测时偶发 >6s）。放宽到 (6, 15)：仍有界，不会挂住面板，
            # 同时把瞬时抖动和真故障区分开。
            #
            # v323: 本地 Ollama 通道单独放宽到 (10, 120)。实测 (2026-11-10):
            #   模型冷启动 (qwen2.5:3b 从磁盘载入内存) 需 ~25~74s, 热态只要 1.1s。
            #   旧的 (6, 15) → 冷启动必然 read timeout → 被归类成 network →
            #   连续 3 次后熔断 → **面板上本地兜底通道全挂, 实际它一直好着**。
            #   120s 覆盖 .env 里记录的历史最坏情况 ("加载 1.9GB 权重后被 swap 到
            #   磁盘, chat/completions 实测 90 秒不返回")。
            timeout=(10, 120) if is_local else (6, 15),
        )
        status_code = resp.status_code
        latency_ms = (time.monotonic() - t0) * 1000
        available = resp.status_code == 200
        body = "" if available else (resp.text or "")[:400]
        category = _classify_probe_failure(status_code, body=body)
        if not available:
            note = resp.text[:120]
            # 「不会自愈」的两类 → 记入静默窗口 (窗口内不再重复探测)
            win = _PROBE_DEAD_WINDOW_S.get(category)
            if win:
                reason = ("套餐/余额已耗尽, 需充值或换 key"
                          if category == "quota_exhausted" else "key 失效, 需更换 API key")
                _PROBE_DEAD_STATE[brk_key] = {
                    "category": category,
                    "until": time.time() + win,
                    "note": f"{note}｜{reason}（{int(win // 60)} 分钟内不再重复探测）",
                }
        if available:
            brk.success()
        else:
            brk.failure()
        return {
            "name": name, "model": model, "available": available,
            "status_code": status_code, "latency_ms": round(latency_ms, 1),
            "category": category,
            "circuit_state": brk.state,
            "note": note,
        }
    except Exception as e:
        exc = e
        latency_ms = (time.monotonic() - t0) * 1000
        category = _classify_probe_failure(None, e)
        # v323: 本地通道的**超时**不算故障 —— 冷启动 25~74s 是常态 (内存紧张时更久),
        # 把它计入熔断会让本地兜底通道被反复摘掉。只标记 timeout, 不累计失败。
        if not (is_local and category == "timeout"):
            brk.failure()
        return {
            "name": name, "model": model, "available": False, "status_code": None,
            "latency_ms": round(latency_ms, 1),
            "category": category,
            "circuit_state": brk.state,
            "note": f"{type(e).__name__}: {str(e)[:100]}",
        }


def _probe_specs_deduped(probe_specs: List[tuple]) -> List[Dict[str, Any]]:
    """按 (api_key, request_model) 去重后探测，结果回填给同组所有通道。

    背景：roster 里 9 个通道只落在 2 个 key / 3 个模型上（账户1 供 7 个专用通道，
    账户2 供 MiniMax#2 + peer复核）。原来的
    ThreadPoolExecutor(max_workers=len(probe_specs)) 会把 9 次请求**同时**打出去：
      1) 同一账户同一模型被重复探测 → 纯浪费额度；
      2) 同 key 并发 → MiniMax 账户级 Token Plan 限流(429) → 面板误报通道故障。
    去重后 9 次请求 → 3 次（同组结果回填），同 key 的组之间再错峰 0.8s。
    """
    grouped: Dict[Tuple[str, str], List[tuple]] = {}
    for spec in probe_specs:
        grouped.setdefault((spec[3], spec[1]), []).append(spec)

    by_key: Dict[str, List[Tuple[str, str]]] = {}
    for key_model in grouped:
        by_key.setdefault(key_model[0], []).append(key_model)

    def _probe_key(key: str, key_models: List[Tuple[str, str]]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for idx, (_k, model) in enumerate(key_models):
            if idx:
                time.sleep(_PROBE_SAME_KEY_STAGGER)
            specs = grouped[(_k, model)]
            label, _model, base, _key, use_proxy = specs[0][:5]
            # v323: 本地兜底通道 (Ollama) 需要更长的探测超时 (冷启动 ~25s)
            is_local = bool(specs[0][5]) if len(specs[0]) > 5 else None
            # 熔断器按 (key 指纹, model) 分组：同组通道共享探测结果，熔断状态也共享。
            breaker_key = f"llm:key:{hashlib.sha1(_key.encode()).hexdigest()[:8]}:{model}"
            probe = _probe_channel(label, model, base, _key, use_proxy,
                                   breaker_key=breaker_key, is_local=is_local)
            for spec in specs:
                item = dict(probe)
                item["name"] = spec[0]
                item["model"] = model
                item["shared_probe"] = len(specs) > 1
                # 实时熔断状态按各通道自己的 alias 读（同组可能是不同专用通道）
                _alias = spec[6] if len(spec) > 6 else ""
                _purpose = spec[7] if len(spec) > 7 else []
                item["runtime_alias"] = _alias
                item["runtime_circuit"] = _runtime_circuit(_alias, _purpose)
                out.append(item)
        return out

    results: List[Dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max(1, len(by_key))) as pool:
        for part in pool.map(lambda kv: _probe_key(kv[0], kv[1]), by_key.items()):
            results.extend(part)
    return results


def _classify_probe_failure(status_code: Optional[int], exc: Optional[BaseException] = None,
                             body: str = "") -> str:
    """把探针失败归类为用户可理解的 category。

    body: 失败响应体 (可选)。只有 body 里出现明确的额度耗尽标记时才归
    quota_exhausted; 普通 429 限流仍然归 rate_limit。
    """
    if status_code == 200:
        return "ok"
    if exc is not None:
        # 超时单列: 与「连不上」是两回事 (本地 Ollama 冷启动实测 25s),
        # 面板上要能区分「慢」与「断」。
        _timeout_cls = getattr(requests.exceptions, "Timeout", None)
        if _timeout_cls is not None and isinstance(exc, _timeout_cls):
            return "timeout"
        # 网络层异常（requests.exceptions.* 其余全部归 network）
        return "network"
    if status_code is None:
        return "network"
    if status_code in (401, 403):
        return "auth"           # key 失效 / 没权限
    if status_code == 402:
        return "quota_exhausted"  # 余额不足 —— 不会自愈, 要充值
    if status_code == 429:
        low = (body or "").lower()
        if any(m.lower() in low for m in _QUOTA_EXHAUSTED_MARKERS):
            return "quota_exhausted"
        return "rate_limit"     # 触发限流 (瞬时, 可重试)
    if 400 <= status_code < 500:
        return "api_error"      # 客户端请求格式 / model 名不被识别
    if 500 <= status_code < 600:
        return "api_error"      # 上游服务异常
    return "unknown"


def _model_health_skeleton() -> Dict[str, Any]:
    """缓存为空时的同结构占位 —— 字段与真实 payload 完全对齐，前端不必改代码。

    v409（2026-11-17）：占位里也要带上"能立刻算出来的东西"和"上次刷新为什么失败"。
    原实现返回 `warming_up=true` + 全 0，等于把两件事混成一件：
      a. 通道探测还没跑完（正常）
      b. 后台刷新一直崩（事故）
    实测现象：面板长期 `channels=[] / pending_news=0 / analyzed_24h=0`，
    而库里明明有 51 条 PENDING、当日 17 条决策 —— 因为 DB 统计和网络探测被耦合在
    同一个后台函数里，探测卡住 → DB 数字也一起不出现，且错误只写进一个没人看的变量。
    现在：漏斗数字（便宜、只读、不需要网络）在请求路径上直接算，探测失败单独暴露。
    """
    payload = {
        "channels": [],
        "recent_models": [],
        "current_model": None,
        "current_model_at": None,
        "pending_news": 0,
        "analyzed_24h": 0,
        "engine_openrouter": None,
        "engine_model_pool": None,
        "warming_up": True,
        "cache_age_sec": None,
        "stale": True,
    }
    payload.update(_funnel_overlay())
    if _MODEL_HEALTH_LAST_ERROR.get("msg"):
        payload["last_refresh_error"] = _MODEL_HEALTH_LAST_ERROR["msg"]
    return payload


def _funnel_overlay() -> Dict[str, Any]:
    """把漏斗数字叠到面板上（只读、3s 超时、失败显形为 None + funnel_db_error）。

    数字来源见 news_funnel.py 文件头：为什么不能用 0 冒充"没读成"。
    """
    try:
        import news_funnel
        stats = news_funnel.funnel_stats(
            _db_path(), getattr(config, "NEWS_ANALYZE_MAX_AGE_HOURS", 0) or 0)
    except Exception as exc:
        return {"funnel_db_error": f"{type(exc).__name__}: {str(exc)[:80]}"}
    out: Dict[str, Any] = {
        "pending_news": stats.get("pending_news"),
        "analyzed_24h": stats.get("analyzed_24h"),
    }
    if stats.get("db_error"):
        out["funnel_db_error"] = stats["db_error"]
    # v410: 门卫**真实判出率** —— 通道探针 available 不等于门卫在判。
    # 2026-11-17 实测：面板 9 条通道全 available，而近 30 分钟 37% 的新闻被冻结等回放、
    # 只有 7% 拿到门卫判定。把这个数字摆到面板上，避免再被"绿灯"骗一次。
    try:
        out["gate_health"] = news_funnel.gate_health(_db_path(), 30)
    except Exception as exc:
        out["gate_health"] = {"db_error": f"{type(exc).__name__}: {str(exc)[:80]}"}
    return out


def _probe_model_channels() -> Dict[str, Any]:
    """请求路径入口：**只读缓存**，永不发网络调用（见上方 v366 P0 说明）。

    - 有缓存：立即返回，附带 cache_age_sec / stale（超过 2×TTL 才算 stale）
    - 无缓存：立即返回同结构占位（warming_up=true），后台线程负责填充
    """
    _ensure_model_health_thread()
    cached = _MODEL_HEALTH_CACHE.get("value")
    age = time.monotonic() - float(_MODEL_HEALTH_CACHE.get("ts", 0.0) or 0.0)
    if cached is None:
        _kick_model_health_refresh()
        return _model_health_skeleton()
    if age > _MODEL_HEALTH_TTL:
        _kick_model_health_refresh()
    payload = dict(cached)
    # v409: 漏斗数字不跟着缓存走 —— 它是本地两次 COUNT，几十毫秒，没理由等通道探测。
    #       探测再卡，面板上的"待分析/今日分析量"也必须是当下的真值。
    payload.update(_funnel_overlay())
    payload["warming_up"] = False
    payload["cache_age_sec"] = round(max(age, 0.0), 1)
    payload["stale"] = bool(age > _MODEL_HEALTH_TTL * 2)
    if _MODEL_HEALTH_LAST_ERROR.get("msg"):
        payload["last_refresh_error"] = _MODEL_HEALTH_LAST_ERROR["msg"]
    return payload


def _kick_model_health_refresh() -> None:
    """踢一次后台刷新（非阻塞）。已有刷新在跑 / 距上次太近则直接返回。"""
    now = time.monotonic()
    last = float(_MODEL_HEALTH_CACHE.get("kick_ts", 0.0) or 0.0)
    if now - last < _MODEL_HEALTH_REFRESH_MIN_GAP:
        return
    if not _MODEL_HEALTH_LOCK.acquire(blocking=False):
        return
    _MODEL_HEALTH_CACHE["kick_ts"] = now

    def _run() -> None:
        try:
            _probe_model_channels_now()
        except Exception as exc:
            _MODEL_HEALTH_LAST_ERROR.update(
                {"msg": f"{type(exc).__name__}: {str(exc)[:100]}", "ts": time.time()})
        finally:
            _MODEL_HEALTH_LOCK.release()

    threading.Thread(target=_run, daemon=True, name="llm-health-kick").start()


def _model_health_refresh_loop() -> None:
    """后台刷新循环：只在守护线程里跑，抢不到锁就跳过本轮（不排队）。"""
    while True:
        try:
            _probe_model_channels_now()
        except Exception as exc:
            _MODEL_HEALTH_LAST_ERROR.update(
                {"msg": f"{type(exc).__name__}: {str(exc)[:100]}", "ts": time.time()})
        time.sleep(max(30.0, _MODEL_HEALTH_TTL))


def _ensure_model_health_thread() -> None:
    """惰性启动后台刷新线程（幂等，首次请求时调用即可，不动 startup 钩子）。"""
    global _MODEL_HEALTH_THREAD
    if _MODEL_HEALTH_THREAD is not None and _MODEL_HEALTH_THREAD.is_alive():
        return
    _MODEL_HEALTH_THREAD = threading.Thread(
        target=_model_health_refresh_loop, daemon=True, name="llm-health-refresh")
    _MODEL_HEALTH_THREAD.start()


def _is_local_base(base: str) -> bool:
    """base 是否指向本机 (Ollama 等本地兜底通道)。"""
    b = (base or "").lower()
    return b.startswith("http://localhost") or b.startswith("http://127.0.0.1")


def _probe_dead_state_reset() -> None:
    """清空静默窗口 (测试用)。"""
    _PROBE_DEAD_STATE.clear()
