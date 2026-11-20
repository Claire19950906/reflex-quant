#!/usr/bin/env python3
"""额度哨兵 —— 让「额度/限速」这件事自己喊出来，而不是等人翻数据库。

背景（2026-11-15）：MiniMax 套餐限速（429 rate_limit_error 2062）导致 AI 决策链路
停摆 46 小时，全程没有任何通知。DLQ 里 8 条 decision_stall 全部 resolved=0。

本脚本每 10 分钟跑一次，判定四件事：
  1. 近 15 分钟 429 占比（限速风暴）
  2. 近 30 分钟是否零调用（通道全挂）
  3. 近 30 分钟是否零决策（决策链路停摆）
  4. DLQ 未解决条数（历史失败堆积）

只在「需要人管」时推送，同一条判定 6 小时内最多推一次（去重文件）。
只读数据库，不写业务表。

用法：
  python3 backend/scripts/llm_quota_sentinel.py           # 正常跑（可能推送）
  SENTINEL_DRY=1 python3 ...                              # 只判定不推送
  python3 ... --json                                      # 输出 JSON
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
DB_PATH = BACKEND / "trident_event_bus.db"
STATE = BACKEND / "reports" / "health" / ".sentinel_state.json"
DEDUP_HOURS = float(os.getenv("SENTINEL_DEDUP_HOURS", "6"))


def load_env() -> None:
    """从 backend/.env 读取推送所必需的环境变量（不覆盖已有值）。"""
    p = BACKEND / ".env"
    if not p.exists():
        return
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, v = s.split("=", 1)
            k = k.strip()
            if k and k not in os.environ:
                os.environ[k] = v.strip()
    except Exception:
        pass


def q(sql: str, params=()):
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=10)
    try:
        return con.execute(sql, params).fetchall()
    finally:
        con.close()


# 无 launchd 排程、只被偶发调用的 worker（心跳旧是正常态，不该按事故报）
ADHOC_WORKERS = {w.strip() for w in
                 os.getenv("SENTINEL_ADHOC_WORKERS", "kg_reconcile_add_rule").split(",")
                 if w.strip()}


def _probe_write_lock() -> dict:
    """探测写锁是否被长期占用，并指认最可能的"占锁者"。

    事故背景（2026-11-16）：一个 cron 批量任务（kline 聚合）跑了 8 分钟、48% CPU，
    把唯一写者名额占住 → 30+ 个写入方全部 busy → api 启动卡死、8000 端口不可用。
    症状和"锁僵死"一模一样，但只要看"谁在跑大批量写"就能秒判。
    """
    import sqlite3 as _s
    import time as _t
    out = {"writable": True, "wait_s": 0.0, "hog": ""}
    t0 = _t.time()
    try:
        conn = _s.connect(str(BACKEND / "trident_event_bus.db"), timeout=1.0)
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("ROLLBACK")
        conn.close()
    except Exception as exc:
        out["writable"] = False
        out["wait_s"] = round(_t.time() - t0, 3)
        out["err"] = type(exc).__name__
    else:
        out["wait_s"] = round(_t.time() - t0, 3)
        return out

    # 不可写 → 找 CPU 最高的、且打开了本库的 python 进程
    try:
        import subprocess as _sp
        ps = _sp.run(["ps", "-Ao", "pid,pcpu,etime,command"],
                     capture_output=True, text=True, timeout=8).stdout
        lsof = _sp.run(["lsof", "-nP", str(BACKEND / "trident_event_bus.db")],
                       capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return out
    pids = set()
    for line in lsof.splitlines()[1:]:
        parts = line.split()
        if len(parts) > 1 and parts[1].isdigit():
            pids.add(parts[1])
    best = (0.0, "")
    for line in ps.splitlines()[1:]:
        f = line.split(None, 3)
        if len(f) < 4 or f[0] not in pids:
            continue
        try:
            cpu = float(f[1])
        except Exception:
            continue
        if cpu > best[0]:
            best = (cpu, f"pid={f[0]} cpu={cpu}% 时长={f[2]} {f[3][:80]}")
    if best[1]:
        out["hog"] = best[1]
    return out


def collect() -> dict:
    now = dt.datetime.now()
    ts = lambda mins: int((now - dt.timedelta(minutes=mins)).timestamp())
    out: dict = {"now": now.strftime("%Y-%m-%d %H:%M:%S")}

    # 1) 近 15 分钟调用与 429 / 402 / 401·403
    total, err429, err402, errauth = q(
        "SELECT COUNT(*), "
        "SUM(CASE WHEN error_code='429' THEN 1 ELSE 0 END), "
        "SUM(CASE WHEN error_code='402' THEN 1 ELSE 0 END), "
        "SUM(CASE WHEN error_code IN ('401','403') THEN 1 ELSE 0 END) "
        "FROM ai_call_stats WHERE ts>=?", (ts(15),))[0]
    out["calls_15m"] = int(total or 0)
    out["err429_15m"] = int(err429 or 0)
    out["ratio429_15m"] = round((err429 or 0) / total, 3) if total else None
    out["err402_15m"] = int(err402 or 0)
    out["errauth_15m"] = int(errauth or 0)
    # 402 响应体摘录（套餐耗尽 vs 余额不足），便于一眼判断要充值还是换套餐
    try:
        body_row = q(
            "SELECT error_body FROM ai_call_stats WHERE error_code='402' "
            "AND ts>=? AND error_body<>'' ORDER BY id DESC LIMIT 1", (ts(30),))
        out["err402_body"] = str(body_row[0][0])[:120] if body_row else ""
    except Exception:
        out["err402_body"] = ""

    # 2) 近 30 分钟调用数 / 最近一次调用
    out["calls_30m"] = int(q("SELECT COUNT(*) FROM ai_call_stats WHERE ts>=?", (ts(30),))[0][0] or 0)
    last = q("SELECT MAX(ts) FROM ai_call_stats")[0][0]
    out["last_call"] = dt.datetime.fromtimestamp(last).strftime("%H:%M:%S") if last else None

    # 3) 近 30 分钟决策数 / 最近一次决策
    # created_at 是 ISO 格式（2026-11-16T08:38:02.458+08:00），
    # 用空格分隔的 '%Y-%m-%d %H:%M:%S' 去比是无效的字符串比较 —— 会比出永远通过。
    old30 = (now - dt.timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%S")
    out["decisions_30m"] = int(
        q("SELECT COUNT(*) FROM ai_decisions WHERE created_at>=?", (old30,))[0][0] or 0)

    # 3b) 空产出：调用花钱了、结论却是"LLM 没吐东西"
    try:
        out["empty_30m"] = int(q(
            "SELECT COUNT(*) FROM ai_decisions WHERE created_at>=? "
            "AND analysis_degraded_reason IN ('llm_empty','llm_failed',"
            "'think_only','empty_response')", (old30,))[0][0] or 0)
        out["main_tokens_30m"] = int(q(
            "SELECT COALESCE(SUM(tokens_in+tokens_out),0) FROM ai_call_stats "
            "WHERE purpose='main' AND ts>=?", (int((now - dt.timedelta(minutes=30)).timestamp()),)
        )[0][0] or 0)
    except Exception:
        out["empty_30m"], out["main_tokens_30m"] = 0, 0
    last_dec = q("SELECT MAX(created_at) FROM ai_decisions")[0][0]
    out["last_decision"] = last_dec

    # 4) DLQ 未解决
    out["dlq_open"] = int(q("SELECT COUNT(*) FROM dlq_events WHERE resolved=0")[0][0] or 0)

    # 5) 心跳（llm_channels 专项 + 全量过期扫描）
    try:
        hb = q("SELECT worker_name, last_tick_at, last_status FROM worker_heartbeat "
               "WHERE worker_name='llm_channels'")
        out["llm_channels_state"] = hb[0][2] if hb else None
    except Exception:
        out["llm_channels_state"] = None

    # 6) 心跳过期 —— 2026-11-16 新增
    # 背景: kg_reconcile_add_rule 从 2026-10-11 起就没再 tick，35 天没人发现。
    # 心跳表本来就有数据，缺的只是一条"过期就说出来"的规则。
    stale_warn_h = float(os.getenv("SENTINEL_STALE_WARN_H", "2"))
    stale_fail_h = float(os.getenv("SENTINEL_STALE_FAIL_H", "24"))
    stale, zombie, adhoc = [], [], []
    try:
        for name, tick, status in q(
                "SELECT worker_name, last_tick_at, last_status FROM worker_heartbeat"):
            age_h = None
            if tick:
                try:
                    age_h = (now - dt.datetime.strptime(str(tick)[:19],
                                                        "%Y-%m-%d %H:%M:%S")).total_seconds() / 3600
                except ValueError:
                    age_h = None
            rec = {"worker": name, "age_h": round(age_h, 1) if age_h is not None else None,
                   "status": status, "tick": str(tick)[:19] if tick else None}
            # 有些 worker 本来就没有 launchd 排程，只在手动/偶发调用时写心跳
            # （实测 kg_reconcile_add_rule 属此类）。它们"旧"不是事故，
            # 报成 FAIL 只会让人学会忽略告警 —— 归到 WARN 并注明"无排程"。
            if name in ADHOC_WORKERS:
                # v486：无排程 worker 的心跳旧是常态（kg_reconcile_add_rule 属
                # 「有 proposal 才 tick」的咨询性心跳，一天最多一次）。放进 stale
                # 会让 WARN 常亮，人就开始学会忽略告警 —— 单独归到 heartbeat_adhoc，
                # 只做信息展示，不触发任何级别的告警。
                rec["adhoc"] = True
                adhoc.append(rec)
                continue
            if age_h is None or age_h >= stale_fail_h:
                zombie.append(rec)
            elif age_h >= stale_warn_h:
                stale.append(rec)
    except Exception as exc:
        out["heartbeat_scan_error"] = f"{type(exc).__name__}: {exc}"
    # 7) 队列积压 —— 2026-11-16 新增
    # 今天暴露过：决策停摆 80 分钟、队列 386 条卡着、超 1 小时就被"归档超龄"丢掉，
    # 而监控只盯"决策产出"，产出为 0 时才喊，那时已经晚了。
    # 队列积压是**更早**的信号，且不需要任何 LLM 调用就能测。
    try:
        out["queue_pending"] = int(q(
            "SELECT COUNT(*) FROM raw_news WHERE status='PENDING'")[0][0] or 0)
        out["queue_processing"] = int(q(
            "SELECT COUNT(*) FROM raw_news WHERE status='PROCESSING'")[0][0] or 0)
        # 近 1 小时入库量，用来判断积压是"到货暴增"还是"产能不足"
        now_s = int(now.timestamp())
        out["arrivals_1h"] = int(q(
            "SELECT COUNT(*) FROM raw_news WHERE ts>=?", (now_s - 3600,))[0][0] or 0)
        # 吞吐量：引擎是「认领→PROCESSING→秒级落 DONE/FILTERED」的短事务模型，
        # 所以瞬时 PROCESSING 几乎永远是 0 —— 实测 21:16 引擎明明在认领
        # （日志 "Claimed 2 items"），哨兵却在同一时刻报"PROCESSING 0 = 没在认领"。
        # 用「有没有处理完的东西」代替「有没有正在处理的」，才是可观测的产能证据。
        out["processed_1h"] = int(q(
            "SELECT COUNT(*) FROM raw_news WHERE processing_at IS NOT NULL "
            "AND processing_at>=datetime('now','-1 hour')")[0][0] or 0)
        out["processed_30m"] = int(q(
            "SELECT COUNT(*) FROM raw_news WHERE processing_at IS NOT NULL "
            "AND processing_at>=datetime('now','-30 minutes')")[0][0] or 0)
        oldest = q("SELECT MIN(ts) FROM raw_news WHERE status='PENDING'")[0][0]
        out["pending_oldest_min"] = (round((now_s - int(oldest)) / 60.0, 1)
                                     if oldest else None)
        # 最近 6 小时的决策，用来区分"停摆"和"闸门把所有新闻都拦了"
        out["decisions_6h"] = int(q(
            "SELECT COUNT(*) FROM ai_decisions WHERE created_at>=?",
            ((now - dt.timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%S"),))[0][0] or 0)
    except Exception as exc:
        out["queue_error"] = f"{type(exc).__name__}: {exc}"
    out["heartbeat_stale"] = stale
    out["heartbeat_adhoc"] = adhoc
    out["heartbeat_zombie"] = zombie
    out["heartbeat_stale_warn_h"] = stale_warn_h
    out["heartbeat_stale_fail_h"] = stale_fail_h
    # 写锁探测：判断"整个库是不是被一个长事务占着"（见 _probe_write_lock 注释）
    try:
        out["write_lock"] = _probe_write_lock()
    except Exception as exc:
        out["write_lock"] = {"writable": None, "err": type(exc).__name__}
    return out


def judge(d: dict) -> list:
    """返回 [(code, level, title, body)]，level=FAIL/WARN。"""
    alerts = []
    r19 = d["ratio429_15m"]
    if d["calls_15m"] >= 5 and r19 is not None and r19 >= 0.8:
        alerts.append((
            "ratelimit_storm", "FAIL",
            "LLM 通道大面积限速",
            f"近 15 分钟 {d['calls_15m']} 次调用中 {d['err429_15m']} 次 429（{r19:.0%}）。"
            f"多半是套餐档位限速或额度耗尽，决策会随之停摆。",
        ))
    if d["calls_15m"] >= 5 and r19 is not None and r19 >= 0.4:
        alerts.append((
            "ratelimit_warn", "WARN",
            "LLM 通道限速比例偏高",
            f"近 15 分钟 429 占比 {r19:.0%}（{d['err429_15m']}/{d['calls_15m']}）。",
        ))

    # 只烧不出（v490）：token 在涨、决策为 0 —— 与"空产出风暴"不同，这条不要求
    # 决策里有降级标记，只看「成本走、产出停」这一件事，专门兜住额度充足时的空转。
    # 阈值取 40 万 token/30 分钟：正常产能约 200–250K/30min，翻倍仍零产出才判。
    if ((d.get("main_tokens_30m") or 0) >= 400000
            and (d.get("decisions_30m") or 0) == 0
            and (d.get("empty_30m") or 0) < 15):
        try:
            from common import llm_budget as _lb_bo
            _lb_bo.note_quota_dead(
                20, "只烧不出 %d token / 0 决策" % d["main_tokens_30m"], scope="main")
        except Exception:
            pass
        alerts.append((
            "burn_no_output", "FAIL",
            "只烧钱不出结果（已自动停 main 20 分钟）",
            "近 30 分钟 main 消耗 %d token，而 ai_decisions 新增 0 条。"
            "这属于「额度有、产出没有」，已短路 main 20 分钟；"
            "请查 engine.err.log 里的栈（kill -USR1 取）确认卡在哪。"
            % d["main_tokens_30m"],
        ))

    # 空产出风暴：调用成功、token 照扣，但结论全是"LLM 没输出" → 停 main 30 分钟
    if (d.get("empty_30m") or 0) >= 15 and (d.get("main_tokens_30m") or 0) > 300000:
        try:
            from common import llm_budget as _lb_es
            _lb_es.note_quota_dead(
                30, "空产出风暴 %d 条 / %d token"
                % (d["empty_30m"], d["main_tokens_30m"]), scope="main")
        except Exception:
            pass
        alerts.append((
            "main_empty_storm", "FAIL",
            "主分析空产出风暴（已自动停 main 30 分钟）",
            "近 30 分钟 %d 条决策为分析降级（llm_empty/llm_failed），非 0；"
            "同期 main 消耗 %d token。已短路 main 30 分钟，避免花钱空转。"
            % (d["empty_30m"], d["main_tokens_30m"]),
        ))

    # 402 = 余额/套餐用量耗尽：不会自愈，必须充值/升级套餐，重试无意义。
    if d.get("err402_15m", 0) > 0:
        body = d.get("err402_body") or ""
        alerts.append((
            "quota_exhausted", "FAIL",
            "LLM 余额/套餐额度耗尽(402)",
            f"近 15 分钟 {d['err402_15m']} 次 402（insufficient_balance / Token Plan 用量上限）。"
            f"需充值或升级套餐，等重试不会自愈。" + (f" 响应：{body}" if body else ""),
        ))

    # 401/403 = key 失效/无权限：需更换 API key。
    if d.get("errauth_15m", 0) > 0:
        alerts.append((
            "auth_failed", "FAIL",
            "LLM API key 失效/无权限(401/403)",
            f"近 15 分钟 {d['errauth_15m']} 次 401/403，需检查并更换 API key。",
        ))

    if d["calls_30m"] == 0:
        alerts.append((
            "no_call", "FAIL",
            "LLM 通道 30 分钟零调用",
            f"最近一次调用 {d['last_call'] or '无记录'}；引擎可能在「跳过认领」。",
        ))

    if d["decisions_30m"] == 0:
        # 闸门实测拦截 97%，30 分钟零决策是**常态**而不是事故 —— 原来一律判 FAIL，
        # 结果这条告警天天红，人自然学会忽略它（等于没有监控）。
        # 用 6 小时兜底：6 小时还零决策，才是链路真停了。
        age_h = None
        now_dt = dt.datetime.strptime(d["now"], "%Y-%m-%d %H:%M:%S")
        if d["last_decision"]:
            try:
                age_h = (now_dt - dt.datetime.strptime(d["last_decision"][:19],
                                                       "%Y-%m-%dT%H:%M:%S")).total_seconds() / 3600
            except ValueError:
                age_h = None
        if age_h is not None and age_h > 6:
            alerts.append((
                "decision_stall", "FAIL",
                "决策链路 6 小时零产出",
                f"最近一次决策 {d['last_decision']}（{age_h:.1f}h 前）；"
                f"DLQ 未解决 {d['dlq_open']} 条，近 6h 决策 {d.get('decisions_6h')} 条。",
            ))
        else:
            alerts.append((
                "decision_slow", "WARN",
                "决策 30 分钟无新增（闸门拦截所致）",
                f"最近一次决策 {d['last_decision'] or '无记录'}，近 6h 共 {d.get('decisions_6h')} 条；"
                f"近 30 分钟已处理 {d.get('processed_30m')} 条新闻。"
                f"闸门拦截率长期 97%，无新增属预期。",
            ))

    # 写锁被长期占用：所有写入方的共同瓶颈
    wl = d.get("write_lock") or {}
    if wl and wl.get("writable") is False:
        alerts.append((
            "write_lock_stuck", "FAIL",
            "数据库写锁被长期占用",
            f"写探测失败（等 {wl.get('wait_s')}s）；最可能的占锁者："
            f"{wl.get('hog') or '未识别到高 CPU 的持库进程'}。"
            f"这不是锁坏了，是有人合法地占着唯一写者名额（常见于批量 cron 大事务）。",
        ))

    # 队列积压（产能 vs 到货）
    qp = d.get("queue_pending") or 0
    proc = d.get("queue_processing") or 0
    # 判定「卡住」必须看产能证据，不能看瞬时 PROCESSING：
    # 引擎认领后一两秒就落 DONE/FILTERED，任意时刻采样 PROCESSING 基本都是 0。
    oldest_min = d.get("pending_oldest_min")
    # 判"卡住"的唯一可信证据是「最老的一条到底等了多久」：
    # 快照表说超 1 小时就丢，如果最老的 PENDING 已经 3 小时还在，说明
    # 认领速度连丢弃阈值都跟不上，积压是真的（实测 21:27：281 条、最老 241.8 分钟、
    # 1 小时入库 294 条而真正进入分析环节只有 8 条）。
    if qp >= 150 and (oldest_min or 0) > 180:
        alerts.append((
            "queue_stuck", "FAIL",
            "待分析队列卡住（1 小时零处理）",
            f"PENDING {qp} 条、最老一条已等 {oldest_min} 分钟、近 1 小时处理 0 条 —— "
            f"引擎确实没在认领。近 1 小时入库 {d.get('arrivals_1h')} 条，超 1 小时的会被丢弃。",
        ))
    elif qp >= 400:
        alerts.append((
            "queue_backlog", "FAIL",
            "待分析队列严重积压",
            f"PENDING {qp} 条（最老 {oldest_min} 分钟）、近 1h 处理 {d.get('processed_1h')} 条、"
            f"入库 {d.get('arrivals_1h')} 条 —— 到货远大于产能，多数会在过期前被丢弃。",
        ))
    elif qp >= 200:
        alerts.append((
            "queue_backlog_warn", "WARN",
            "待分析队列积压",
            f"PENDING {qp} 条（最老 {oldest_min} 分钟）、近 1h 处理 {d.get('processed_1h')} 条、"
            f"入库 {d.get('arrivals_1h')} 条。",
        ))

    # 心跳过期：僵尸 worker 不会自己喊，必须靠这条规则
    zombie = d.get("heartbeat_zombie") or []
    stale = d.get("heartbeat_stale") or []
    if zombie:
        names = "、".join(f"{z['worker']}（{z['age_h']}h）" if z["age_h"] is not None
                          else f"{z['worker']}（无 tick）" for z in zombie[:4])
        alerts.append((
            "worker_zombie", "FAIL",
            "Worker 心跳长期停跳",
            f"以下 worker 超过 {d.get('heartbeat_stale_fail_h')} 小时没有 tick：{names}。"
            f"进程可能已死、或循环被卡住，对应功能是静默失效的。",
        ))
    elif stale:
        names = "、".join(
            (f"{z['worker']}（{z['age_h']}h，无排程）" if z.get("adhoc")
             else f"{z['worker']}（{z['age_h']}h）") for z in stale[:4])
        alerts.append((
            "worker_stale", "WARN",
            "Worker 心跳偏旧",
            f"以下 worker 心跳已旧：{names}。标「无排程」的属预期，无需处理；"
            f"其余请确认进程是否还在。",
        ))
    return alerts


def dedup_ok(code: str) -> bool:
    try:
        state = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
    except Exception:
        state = {}
    last = float(state.get(code) or 0)
    return (dt.datetime.now().timestamp() - last) > DEDUP_HOURS * 3600


def mark(code: str) -> None:
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        state = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
    except Exception:
        state = {}
    state[code] = dt.datetime.now().timestamp()
    try:
        STATE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception:
        pass


def main() -> int:
    load_env()
    d = collect()
    alerts = judge(d)
    dry = os.getenv("SENTINEL_DRY") == "1" or "--dry" in sys.argv

    sent = []
    if alerts and not dry:
        try:
            sys.path.insert(0, str(BACKEND / "src_python"))
            import llm_budget # 复用既有推送通道（email / wechat）
        except Exception as e:  # pragma: no cover
            print(f"[SENTINEL] 推送模块不可用: {e}", flush=True)
            llm_budget = None  # type: ignore
        if llm_budget is not None:
            for code, level, title, body in alerts:
                if not dedup_ok(code):
                    continue
                try:
                    llm_budget.push_critical(f"[{level}] {title}", body)
                    mark(code)
                    sent.append(code)
                except Exception as e:
                    print(f"[SENTINEL] 推送失败 {code}: {e}", flush=True)

    # 落一份快照，供体检与面板读取
    try:
        out_dir = BACKEND / "reports" / "health"
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "quota_state.json").write_text(
            json.dumps({**d, "alerts": [a[0] for a in alerts], "sent": sent},
                       ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass

    if "--json" in sys.argv:
        print(json.dumps({**d, "alerts": [a[0] for a in alerts]}, ensure_ascii=False))
    else:
        print(f"[SENTINEL] {d['now']} 调用15m={d['calls_15m']} 429={d['err429_15m']} "
              f"队列P={d.get('queue_pending')}/{d.get('queue_processing')} "
              f"调用30m={d['calls_30m']} 决策30m={d['decisions_30m']} "
              f"DLQ未解决={d['dlq_open']} 通道={d['llm_channels_state']}")
        for code, level, title, body in alerts:
            print(f"  [{level}] {code} · {title} — {body}")
        if not alerts:
            print("  [OK] 四项判定均正常")
        if dry:
            print("  (dry-run：未推送)")
    return 1 if any(a[1] == "FAIL" for a in alerts) else 0


if __name__ == "__main__":
    raise SystemExit(main())
