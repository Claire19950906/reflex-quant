# -*- coding: utf-8 -*-
"""attribution_ai_verify.py — 归因的 **AI 双重验证** (需求 #6 / G01)。

背景
----
event_move_attribution 表里有 ai_called / ai_explanation / ai_confidence 三列,
但写入端从来只写 0 —— 也就是"AI 双重验证"这一步**从未真正执行过**:
归因页上的分数 100% 来自规则匹配 (关键词/资产别名/宏观兜底), 没有第二意见,
用户也无从知道这条归因到底有多可信。

本模块用 LLM (MiniMax-M3) 对每条归因做一次**独立复核**:
  输入: 异动 (资产/幅度/形态/时间) + 被归因的新闻 (标题/来源/正文片段)
        + 规则引擎的打分理由
  输出: 判定 (confirm / partial / reject) + 0~1 置信度 + 一句话理由
  落库: ai_called=1, ai_explanation=理由, ai_confidence=置信度

只做规则引擎**做不了**的事: 判断"这条新闻真的能解释这波异动" —— 规则只能看
关键词命中, 判断不了语义 (比如"利好兑现"型新闻其实是反向的)。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

SYSTEM_PROMPT = (
    "你是交易系统的归因复核员。给你一笔价格异动和一条被归因的新闻, "
    "你要判断: 这条新闻是否真的能解释这波异动。只输出 JSON。"
)

PROMPT_TMPL = """价格异动: 标的 {asset}, 幅度 {move_pct:+.2f}%, 形态 {move_kind}, 持续 {duration_min} 分钟, 时间 {ts_iso}
规则引擎的打分理由: {reasons}
规则引擎给出的分数: {algo_score:.2f}
被归因的新闻 (来源 {source}, 时间 {news_ts_iso}):
标题: {headline}
正文片段: {body}

请复核这条归因, 输出严格 JSON:
{{"verdict": "confirm|partial|reject", "confidence": 0.0-1.0, "reason": "一句话中文理由(不超过40字)"}}

判据:
- confirm: 新闻内容与异动方向、标的、时点都能对上, 是这波异动的合理驱动;
- partial: 相关但不是主因 (例如只解释方向不解释幅度, 或时点偏早/偏晚);
- reject: 新闻与这波异动无关, 或方向相反 (例如"利好兑现"实为利空)。"""


def _clean(text: Any, limit: int = 200) -> str:
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    return s[:limit]


def _verdict_from_text(text: str) -> Optional[str]:
    """v342: 模型没吐 JSON 时退一步按关键词判。

    实测队列里有 3 条卡在 ``parse_fail``（模型回了一段中文解释，没有花括号），
    整条证据就这样被丢掉。这里只做保守兜底：先否定、再部分、最后确认 ——
    宁可标 partial（进主表但置信度低）也不把证据直接扔了。
    """
    low = (text or "").lower()
    if not low.strip():
        return None
    reject_hits = ("reject", "不成立", "无关", "不是主因", "方向相反", "不匹配")
    partial_hits = ("partial", "部分", "不是主因", "间接", "有一定关系", "偏早", "偏晚")
    confirm_hits = ("confirm", "成立", "合理驱动", "主因", "直接驱动")
    if any(k in low for k in reject_hits):
        return "reject"
    if any(k in low for k in partial_hits):
        return "partial"
    if any(k in low for k in confirm_hits):
        return "confirm"
    return None


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    raw = re.sub(r"<think>.*?</think>", "", text.strip(), flags=re.S)
    raw = re.sub(r"```json", "", raw)
    raw = re.sub(r"```", "", raw).strip()
    m = re.search(r"\{.*\}", raw, flags=re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except (json.JSONDecodeError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _fmt_ts(ts: Any) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(int(ts)))
    except (TypeError, ValueError):
        return ""


def build_prompt(row: Dict[str, Any], news: Dict[str, Any]) -> str:
    return PROMPT_TMPL.format(
        asset=_clean(row.get("asset"), 16),
        move_pct=float(row.get("move_pct") or 0.0),
        move_kind=_clean(row.get("move_kind"), 24) or "-",
        duration_min=int(row.get("duration_min") or 0),
        ts_iso=_fmt_ts(row.get("move_ts")),
        reasons=_clean(row.get("algo_reasons"), 240) or "-",
        algo_score=float(row.get("algo_score") or 0.0),
        source=_clean(news.get("source"), 40) or "-",
        news_ts_iso=_fmt_ts(news.get("ev_ts") or news.get("ts")),
        headline=_clean(news.get("headline"), 160) or "-",
        body=_clean(news.get("body"), 240) or "-",
    )


def verify_one_ex(row: Dict[str, Any], news: Dict[str, Any]
                  ) -> tuple[Optional[Dict[str, Any]], Optional[str]]:
    """对一条归因做 LLM 复核。返回 (结果, 错误原因)。

    v340: 把「失败」区分成 通道失败(429/超时/401) 与 解析失败 ——
    队列需要据此决定退避多久：通道失败说明额度/网络有问题，同一批全部退避；
    解析失败是单条 prompt 的问题，下一次重试通常就好。
    """
    try:
        from engine.ai_worker import chat
    except Exception as exc:
        return None, "import_fail:%s" % type(exc).__name__
    try:
        raw = chat(
            build_prompt(row, news),
            # v342: 原来走 purpose="advisor"，而 advisor 池只有一支
            # MiniMax-M2.7-highspeed，这支被熔断后归因复核整条链路 0 产出
            # （实测连续两轮 "复核 0 条 (失败 1)"）。verify 池有两支可回退。
            purpose="verify",
            system=SYSTEM_PROMPT,
            max_tokens=160,   # v340: 400 时本地 qwen2.5:3b 常撞 90s 读超时
            temperature=0.1,
            call_site="attribution_ai_verify",
        )
    except Exception as exc:
        msg = str(exc)[:80]
        return None, "llm_error:%s:%s" % (type(exc).__name__, msg)
    data = _extract_json(raw or "")
    if not data:
        # v342: 不再直接判 parse_fail —— 文本里能读出倾向就用倾向，救回证据。
        v = _verdict_from_text(raw or "")
        if not v:
            return None, "parse_fail"
        data = {"verdict": v, "confidence": 0.4,
                "reason": _clean(raw, 40) or "text_fallback"}
    verdict = str(data.get("verdict") or "").lower().strip()
    if verdict not in ("confirm", "partial", "reject"):
        verdict = "partial"
    try:
        conf = float(data.get("confidence"))
    except (TypeError, ValueError):
        conf = 0.5
    conf = max(0.0, min(1.0, conf))
    reason = _clean(data.get("reason"), 120)
    if not reason:
        reason = "模型未给出理由"
    label = {"confirm": "确认", "partial": "部分成立", "reject": "不成立"}[verdict]
    return {
        "ai_called": 1,
        "ai_confidence": round(conf, 4),
        "ai_explanation": "[%s %.2f] %s" % (label, conf, reason),
        "verdict": verdict,
    }, None


def verify_one(row: Dict[str, Any], news: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """兼容旧调用方：只取结果，失败返回 None。"""
    return verify_one_ex(row, news)[0]



# ─────────────────────────────────────────────────────────────────────────────
# 批量执行
# ─────────────────────────────────────────────────────────────────────────────

def load_pending(conn: sqlite3.Connection, limit: int = 0,
                 only_missing: bool = True) -> List[Dict[str, Any]]:
    """v323 P0-2: 待复核行 = 主表里没复核过的 + **待复核区**里没复核过的。

    待复核区 (event_move_attribution_pending) 存的是 |move_pct|>=5% 的大异动 ——
    它们在拿到 AI 判定之前不许进主表 (闸三), 但**照样要复核**。
    """
    sql = ("SELECT a.id, a.move_id, a.news_event_id, a.asset, a.algo_score, a.algo_reasons, "
           "a.time_lag_sec, a.move_ts, a.move_pct, a.move_kind, a.duration_min, a.ai_called, "
           "a.ev_news_id, a.ev_source, a.ev_action, a.ev_score, a.ev_ts, "
           "n.headline, n.body, n.source, n.ts AS news_ts, n.id AS news_row_id "
           "FROM event_move_attribution a "
           "LEFT JOIN news_events n ON n.id = a.news_event_id")
    if only_missing:
        sql += " WHERE COALESCE(a.ai_called,0)=0"
    sql += " ORDER BY a.move_ts DESC"
    if limit > 0:
        sql += " LIMIT %d" % int(limit)
    rows = [dict(r) for r in conn.execute(sql).fetchall()]
    for r in rows:
        r["_src"] = "main"
    # v323 P0-2: 待复核区 (大异动)
    try:
        psql = ("SELECT p.id, p.move_id, p.news_event_id, p.asset, p.algo_score, p.algo_reasons, "
                "p.time_lag_sec, p.move_ts, p.move_pct, p.move_kind, p.duration_min, p.ai_called, "
                "p.ev_news_id, p.ev_source, p.ev_action, p.ev_score, p.ev_ts, "
                "n.headline, n.body, n.source, n.ts AS news_ts, n.id AS news_row_id "
                "FROM event_move_attribution_pending p "
                "LEFT JOIN news_events n ON n.id = p.news_event_id")
        if only_missing:
            psql += " WHERE COALESCE(p.ai_called,0)=0"
        psql += " ORDER BY p.move_ts DESC"
        if limit > 0:
            psql += " LIMIT %d" % int(limit)
        for r in conn.execute(psql).fetchall():
            d = dict(r)
            d["_src"] = "pending"
            rows.append(d)
    except sqlite3.OperationalError:
        pass
    return rows


_ROW_COLS = ("a.id, a.move_id, a.news_event_id, a.asset, a.algo_score, a.algo_reasons, "
             "a.time_lag_sec, a.move_ts, a.move_pct, a.move_kind, a.duration_min, a.ai_called, "
             "a.ev_news_id, a.ev_source, a.ev_action, a.ev_score, a.ev_ts, "
             "n.headline, n.body, n.source, n.ts AS news_ts, n.id AS news_row_id")


def load_rows_by_ids(conn: sqlite3.Connection, specs: List[Dict[str, Any]]
                     ) -> List[Dict[str, Any]]:
    """按队列认领结果回主表/待复核区取原行（队列只存 id，不复制业务字段）。"""
    out: List[Dict[str, Any]] = []
    for src, table in (("main", "event_move_attribution"),
                       ("pending", "event_move_attribution_pending")):
        ids = [int(sp["src_id"]) for sp in specs if sp["src_table"] == src]
        if not ids:
            continue
        alias = "a" if src == "main" else "p"
        cols = _ROW_COLS.replace("a.", alias + ".")
        sql = ("SELECT " + cols + " FROM " + table + " " + alias +
               " LEFT JOIN news_events n ON n.id=" + alias + ".news_event_id"
               " WHERE " + alias + ".id IN (" + ",".join("?" * len(ids)) + ")")
        try:
            rows = [dict(r) for r in conn.execute(sql, ids).fetchall()]
        except sqlite3.OperationalError:
            continue
        by_id = {int(r["id"]): r for r in rows}
        for sp in specs:
            if sp["src_table"] != src:
                continue
            r = by_id.get(int(sp["src_id"]))
            if not r:
                continue
            r["_src"] = src
            r["_qid"] = sp["id"]
            r["_attempts"] = sp.get("attempts") or 0
            out.append(r)
    return out


def _rejected_insert(conn: sqlite3.Connection, row: Dict[str, Any],
                     res: Dict[str, Any], orig_row_id: Optional[int] = None) -> None:
    """v323 P0-2 闸四: 把被 AI 判「不成立」的行**留痕**到 rejected 表。

    留痕 = 原行全字段 + reject_reason, 供审计"当初为什么被否"。
    """
    try:
        conn.execute(
            "INSERT INTO event_move_attribution_rejected "
            "(orig_row_id,move_id,news_event_id,asset,time_lag_sec,algo_score,algo_reasons,"
            "ai_called,ai_explanation,ai_confidence,ai_verdict,"
            "move_ts,move_pct,move_kind,duration_min,"
            "ev_news_id,ev_source,ev_action,ev_ts,reject_reason,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (orig_row_id, row.get("move_id"), row.get("news_event_id"), row.get("asset"),
             row.get("time_lag_sec"), row.get("algo_score"), row.get("algo_reasons"),
             res.get("ai_called"), res.get("ai_explanation"), res.get("ai_confidence"),
             res.get("verdict"),
             row.get("move_ts"), row.get("move_pct"), row.get("move_kind"),
             row.get("duration_min"),
             row.get("ev_news_id"), row.get("ev_source"), row.get("ev_action"),
             row.get("ev_ts"), res.get("ai_explanation"), int(time.time())),
        )
    except sqlite3.Error as exc:
        print(f"[WARN] rejected 留痕失败: {type(exc).__name__}: {exc}", flush=True)


def run(conn: sqlite3.Connection, limit: int = 0, workers: int = 6,
        only_missing: bool = True, queue_mode: bool = False) -> Dict[str, Any]:
    """对缺失 AI 复核的归因行执行 LLM 复核并落库。

    queue_mode=True (v340): 走 attribution_ai_queue —— 每行有独立的 attempts /
    next_retry_at，失败退避、游标前进，不会像旧实现那样每轮重试同一批最新行。
    """
    q = None
    specs: List[Dict[str, Any]] = []
    gone_specs: List[Dict[str, Any]] = []
    if queue_mode:
        import attribution_ai_queue as q
        q.ensure_schema(conn)
        q.reap_zombies(conn)
        enq = q.enqueue_all(conn)
        print("[QUEUE] 新入队 %d 条 (已复核跳过 %d)" %
              (enq["added"], enq["skipped_done"]), flush=True)
        specs = q.claim(conn, limit if limit > 0 else 40)
        rows = load_rows_by_ids(conn, specs)
        claimed_ids = {(sp["src_table"], int(sp["src_id"])) for sp in specs}
        got_ids = {(r.get("_src"), int(r["id"])) for r in rows}
        # 源行已被删（归因表每 5 分钟整表重建）→ 队列里标 gone，别再空转
        gone_specs = [sp for sp in specs if (sp["src_table"], int(sp["src_id"])) not in got_ids]
        for sp in gone_specs:
            q.fail(conn, sp["id"], "source row removed", sp.get("attempts") or 0, gone=True)
        conn.commit()
        if not rows:
            print("[OK] 队列暂无到期任务 (认领 %d, 源行消失 %d)" %
                  (len(specs), len(gone_specs))
                  + "｜覆盖率 " + str(q.coverage(conn)))
            return {"candidates": 0, "verified": 0, "failed": 0,
                    "queue": q.stats(conn)}
    else:
        rows = load_pending(conn, limit, only_missing)
    if not rows:
        print("[OK] 没有待复核的归因行")
        return {"candidates": 0, "verified": 0, "failed": 0}
    t0 = time.time()
    # 读阶段结束: 放掉读事务, 后面只在批量写时用这个连接 (v321.3 锁行为修复)
    # isolation_level=None 下 commit() 是 no-op, 必须显式 ROLLBACK 才能
    # 结束 SELECT 开的事务, 否则落库阶段的 BEGIN IMMEDIATE 会报
    # "cannot start a transaction within a transaction"。
    try:
        conn.rollback()
    except sqlite3.Error:
        pass

    def work(row: Dict[str, Any]):
        news = {
            "headline": row.get("headline"),
            "body": row.get("body"),
            "source": row.get("source"),
            "ts": row.get("news_ts"),
        }
        try:
            return row, verify_one_ex(row, news)
        except Exception as exc:
            return row, (None, "work_error:%s" % type(exc).__name__)

    verified = 0
    failed = 0
    # ── v323 P0-2 闸四: 按「判定 + 来源」分流 ─────────────────────────────
    #   main_updates    主表 confirm/partial -> UPDATE (带 ai_verdict)
    #   main_rejects    主表 reject          -> DELETE 主表 + rejected 留痕
    #   pending_to_main 待复核区 confirm/partial -> INSERT 主表 + DELETE 待复核区
    #   pending_rejects 待复核区 reject        -> rejected 留痕 + DELETE 待复核区
    main_updates: List[tuple] = []
    main_rejects: List[tuple] = []
    pending_to_main: List[tuple] = []
    pending_rejects: List[tuple] = []
    fails: List[tuple] = []   # (row, err) 供队列退避
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for row, pair in pool.map(work, rows):
            res, err = pair if isinstance(pair, tuple) and len(pair) == 2 else (pair, None)
            if not res:
                failed += 1
                fails.append((row, err or "unknown"))
                continue
            verified += 1
            if verified % 20 == 0:
                print("[PROGRESS] %d/%d (%.0fs)" % (verified, len(rows), time.time() - t0))
            src = row.get("_src", "main")
            verdict = res.get("verdict") or "partial"
            if src == "pending":
                (pending_rejects if verdict == "reject" else pending_to_main).append((row, res))
            elif verdict == "reject":
                main_rejects.append((row, res))
            else:
                main_updates.append((res["ai_called"], res["ai_explanation"],
                                     res["ai_confidence"], verdict, int(row["id"])))
    # ── v321.3 P0 (锁行为修复) ──────────────────────────────────────────────
    # 这个作业一度把**全库写锁按住 17 分钟**: 旧实现每拿到一条结果就 UPDATE 一次、
    # 每 10 条才 commit, 而 LLM 调用 (worker 线程) 内部要写 ai_call_stats
    # (同一个 DB) → worker 的写与主线程的写事务互相阻塞 → busy_timeout 耗尽 →
    # 失败重试 → 其他作业 (causal-link / signal-weight / news) 全部报
    # "database is locked"。现在**读写彻底分离**: 读阶段结束就放掉事务,
    # worker 只做 LLM 不碰 DB, 全部算完后一次 executemany + 一次 commit ——
    # 写锁只持有一次。
    # ── v323 P0-3: 落库阶段带**写锁重试** ─────────────────────────────────
    # LLM 调用动辄几分钟, 这期间别的作业 (engine / 归因重算 / signal-weight)
    # 完全可能正占着写锁; 旧实现一次 OperationalError 就把这几十条 LLM
    # 结果全丢了 (实测 "[WARN] 批量写入失败: database is locked", 34 条白跑)。
    # 现在: BEGIN IMMEDIATE + 指数退避重试, 最多 6 次。
    _write_attempts = 0
    while True:
        _write_attempts += 1
        try:
            conn.execute("BEGIN IMMEDIATE")
            if main_updates:
                conn.executemany(
                    "UPDATE event_move_attribution SET ai_called=?, ai_explanation=?, "
                    "ai_confidence=?, ai_error=NULL, ai_verdict=? WHERE id=?",
                    main_updates,
                )
            # 闸四: AI 判「不成立」→ 出主表, 但在 rejected 表**留痕**
            for row, res in main_rejects:
                _rejected_insert(conn, row, res, orig_row_id=row.get("id"))
                conn.execute("DELETE FROM event_move_attribution WHERE id=?",
                             (int(row["id"]),))
            # 闸三: 待复核区里被判成立的 -> 迁入主表
            for row, res in pending_to_main:
                conn.execute(
                    "INSERT INTO event_move_attribution "
                    "(move_id,news_event_id,asset,time_lag_sec,algo_score,algo_reasons,"
                    "ai_called,ai_explanation,ai_confidence,ai_error,"
                    "move_ts,move_pct,move_kind,duration_min,"
                    "ev_news_id,ev_source,ev_action,ev_score,ev_ts,ai_verdict) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (row.get("move_id"), row.get("news_event_id"), row.get("asset"),
                     row.get("time_lag_sec"), row.get("algo_score"), row.get("algo_reasons"),
                     res["ai_called"], res["ai_explanation"], res["ai_confidence"], None,
                     row.get("move_ts"), row.get("move_pct"), row.get("move_kind"),
                     row.get("duration_min"),
                     row.get("ev_news_id"), row.get("ev_source"), row.get("ev_action"),
                     row.get("ev_score"), row.get("ev_ts"), res.get("verdict")),
                )
                conn.execute("DELETE FROM event_move_attribution_pending WHERE id=?",
                             (int(row["id"]),))
            # 闸四: 待复核区里被判不成立的 -> 留痕 + 出待复核区
            for row, res in pending_rejects:
                _rejected_insert(conn, row, res, orig_row_id=row.get("id"))
                conn.execute("DELETE FROM event_move_attribution_pending WHERE id=?",
                             (int(row["id"]),))
            conn.commit()
            break
        except sqlite3.Error as exc:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            msg = str(exc).lower()
            if ("locked" not in msg and "busy" not in msg):
                print(f"[WARN] 批量写入失败: {type(exc).__name__}: {exc}", flush=True)
                break
            if _write_attempts >= 10:
                # 10 次退避 (5+10+...+30 ≈ 3 分钟) 仍拿不到锁 → 放弃本轮。
                # 此时**没有半截数据**: BEGIN IMMEDIATE 没拿到, 所有 UPDATE/DELETE
                # 都没执行, 表保持原样。下一轮 cron 会重新挑 ai_called=0 的行
                # 重跑 (LLM 结果这轮白费, 但不会写坏数据)。
                print(f"[WARN] 落库放弃(写锁被占用约 3 分钟): {exc} —— "
                      f"本轮结果未落库, 等下一轮 cron 重跑", flush=True)
                break
            wait = min(5 * _write_attempts, 30)
            print(f"[WARN] 写锁占用, {wait}s 后重试落库 (第 {_write_attempts} 次)", flush=True)
            time.sleep(wait)
    if main_rejects or pending_rejects or pending_to_main:
        print("[OK] v323 分流: 主表更新 %d, 判不成立出表 %d(+待复核区 %d), "
              "待复核区迁入主表 %d"
              % (len(main_updates), len(main_rejects), len(pending_rejects),
                 len(pending_to_main)), flush=True)
    _channel_down = False
    if queue_mode and fails:
        # v340: 全批失败且都是通道级错误（429/超时/熔断）→ 判定为"通道没容量"，
        # 整批退回 pending 且不消耗 attempts，15 分钟后再试。
        # 否则一次持续一天的配额枯竭会把积压全打成 dead（12 次上限）。
        _chan_errs = [e for _, e in fails
                      if str(e).startswith(("llm_error", "import_fail"))]
        if len(_chan_errs) >= max(3, len(rows)) and len(_chan_errs) == len(fails):
            _channel_down = True
            print("[QUEUE] LLM 通道整体不可用（%d/%d 全失败）→ 本轮不消耗重试次数, "
                  "15 分钟后再试" % (len(_chan_errs), len(rows)), flush=True)
            for row, err in fails:
                if row.get("_qid"):
                    q.defer(conn, row["_qid"], err, 900)
            conn.commit()

    if queue_mode and not _channel_down:
        ok_qids = [r["_qid"] for r in rows if r.get("_qid")
                   and (r.get("_src"), int(r["id"])) not in
                   {(k.get("_src"), int(k["id"])) for k, _ in fails}]
        fail_map = {}
        for row, err in fails:
            if row.get("_qid"):
                fail_map[row["_qid"]] = (err, row.get("attempts") or 0)
        done_n = 0
        for qid in ok_qids:
            if qid not in fail_map:
                q.complete(conn, qid)
                done_n += 1
        for qid, (err, att) in fail_map.items():
            q.fail(conn, qid, err, att)
        conn.commit()
        print("[QUEUE] 本轮完成 %d, 失败 %d (已排下次重试)" % (done_n, len(fail_map),
                                                          ), flush=True)
    total = conn.execute("SELECT COUNT(*) FROM event_move_attribution").fetchone()[0]
    called = conn.execute(
        "SELECT COUNT(*) FROM event_move_attribution WHERE ai_called=1").fetchone()[0]
    pct = (called / total * 100.0) if total else 0.0
    print("[OK] 复核 %d 条 (失败 %d), 表内 ai_called=1: %d/%d = %.1f%% (%.0fs)"
          % (verified, failed, called, total, pct, time.time() - t0))
    extra = {}
    if queue_mode:
        extra["queue"] = q.stats(conn)
    return {"candidates": len(rows), "verified": verified, "failed": failed,
            "called_total": called, "rows_total": total, "coverage_pct": round(pct, 1),
            **extra}


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="归因 AI 双重验证")
    ap.add_argument("--db", default=os.environ.get("TRIDENT_DB_PATH") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "trident_event_bus.db"))
    ap.add_argument("--limit", type=int, default=0, help="本轮最多复核多少条")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--all", action="store_true", help="连已复核的一起重做")
    ap.add_argument("--queue", dest="queue", action="store_true", default=True,
                    help="队列模式(默认): 逐行重试退避, 游标前进")
    ap.add_argument("--no-queue", dest="queue", action="store_false",
                    help="旧语义: 直接挑 ai_called=0 的最新 N 条")
    ap.add_argument("--stats", action="store_true", help="只打印队列与覆盖率")
    args = ap.parse_args()
    conn = sqlite3.connect(args.db, timeout=120)
    conn.row_factory = sqlite3.Row
    # v323 P0-3: 手动事务控制 —— 否则 python 的隐式 deferred 事务会和
    # 落库阶段的 BEGIN IMMEDIATE 打架 ("cannot start a transaction within
    # a transaction"), 而且 busy_timeout 对"读事务升级写事务"无效。
    conn.isolation_level = None
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=120000")
    if args.stats:
        import json as _json
        import attribution_ai_queue as q
        q.ensure_schema(conn)
        print(_json.dumps(q.stats(conn), ensure_ascii=False, indent=2))
        conn.close()
        return 0
    run(conn, limit=args.limit, workers=args.workers, only_missing=not args.all,
        queue_mode=args.queue)
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
