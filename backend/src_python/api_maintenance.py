"""api_server 的运维类后台任务（重置/备份/看门狗）。

2026-11-18 从 api_server.py（10,127 行 / 203 个私有函数）里切出来的第一块。
选取标准不是"看起来像一类"，而是**依赖可闭合**：
   · `_clear_trading_traces`      零模块依赖（只碰 sqlite3 + 自带常量）
   · `infra_health_watcher`       只读 config/db/source_status
   · `_reverse_attribution_watcher` 只读自己的三个时间常量
   · `_auto_backup_trading_data`  只读 BACKUP_DIR（单一来源仍在 api_server）

`_reset_replay_sync`（编排"停→备份→清空→失效缓存"）**故意留在 api_server**：
它读写 `_QUICK_SIM_CACHE` 这个可变全局，跟快照/模拟盘逻辑绑在一起，
搬过来只能靠反向 import 取状态，属于把债从 A 挪到 B。

兼容口径：api_server 末尾 `from api_maintenance import ...` 接回同名函数，
所以 `api_server._clear_trading_traces` 这类引用/monkeypatch 照旧有效
（这也是没做成"签名壳 + 实现下沉"的原因：壳只有几行，直接接回更省）。
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import config
import db
import paper_trading
from feeds import source_status


_REVERSE_ATTR_INTERVAL_S = 1800  # 30min
_REVERSE_ATTR_WINDOW_HOURS = 24
_REVERSE_ATTR_LOOKBACK_S = 3600  # ±1h 内算相关


def _backup_dir() -> str:
    """备份目录的**单一来源仍在 api_server**（routers/backups.py 也从那里取）。

    这里不复制 `os.path.join(config.BASE_DIR, "backups")` 这个表达式 ——
    两处各写一遍迟早会分叉。懒 import 避开 import 期的循环依赖。
    """
    import api_server

    return api_server.BACKUP_DIR


def _auto_backup_trading_data(conn: sqlite3.Connection, backup_dir: Optional[str] = None) -> Dict[str, Any]:
    """重置前自动备份：把全部交易记录导出为 JSON（完整字段）+ Excel（可读表）。

    conn 需已设置 row_factory=sqlite3.Row；只读不改库，清空前调用。
    """
    directory = backup_dir or _backup_dir()
    os.makedirs(directory, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = os.path.join(directory, f"paper_trading_backup_{stamp}.json")
    excel_path = os.path.join(directory, f"paper_trading_backup_{stamp}.xlsx")

    decisions = [dict(row) for row in conn.execute(
        """SELECT ad.*, rn.timestamp AS news_timestamp, rn.source AS news_source,
                  rn.content AS news_text,
                  qs.entry_price, qs.exit_price, qs.pnl_pct AS forward_pnl,
                  qs.mfe_pct, qs.mae_pct, qs.mfe_time_mins,
                  qs.max_price, qs.min_price, qs.max_price_time, qs.min_price_time,
                  qs.verdict AS is_correct, qs.settled, qs.exit_ts
           FROM ai_decisions ad
           LEFT JOIN raw_news rn ON rn.id = ad.news_id
           LEFT JOIN quick_sim_trades qs ON qs.decision_id = ad.id
           WHERE qs.entry_price IS NOT NULL OR qs.settled = 1
           ORDER BY ad.id"""
    ).fetchall()]
    trades = [dict(row) for row in conn.execute(
        "SELECT * FROM quick_sim_trades ORDER BY id").fetchall()]
    runs = [dict(row) for row in conn.execute(
        "SELECT * FROM paper_trading_runs ORDER BY id").fetchall()]
    settings_row = conn.execute(
        "SELECT * FROM paper_trading_settings WHERE id=1").fetchone()

    payload = {
        "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "initial_equity_usdt": config.PAPER_INITIAL_EQUITY_USDT,
        "settings": dict(settings_row) if settings_row else None,
        "paper_trading_runs": runs,
        "quick_sim_trades": trades,
        "traded_decisions": decisions,
    }
    with open(json_path, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)

    import xlsxwriter
    workbook = xlsxwriter.Workbook(excel_path)
    worksheet = workbook.add_worksheet("模拟交易")
    headers = ["时间", "新闻内容", "品种", "方向", "入场价", "最大浮盈%", "最大浮亏%", "出场价", "盈亏%", "胜负", "大模型归因"]
    for column, header in enumerate(headers):
        worksheet.write(0, column, header)
    for index, row in enumerate(decisions, start=1):
        asset = "XAU" if row.get("target_asset") == "GOLD" else row.get("target_asset")
        action = row.get("suggested_action")
        worksheet.write(index, 0, row.get("entry_time") or row.get("created_at") or "")
        worksheet.write(index, 1, row.get("news_text") or "")
        worksheet.write(index, 2, asset or "")
        worksheet.write(index, 3, "多" if action == "BUY" else "空" if action == "SELL" else action or "")
        worksheet.write_number(index, 4, float(row.get("entry_price") or 0.0))
        worksheet.write_number(index, 5, float(row.get("mfe_pct") or 0.0))
        worksheet.write_number(index, 6, float(row.get("mae_pct") or 0.0))
        worksheet.write_number(index, 7, float(row.get("exit_price") or 0.0))
        worksheet.write_number(index, 8, float(row.get("forward_pnl") or 0.0))
        worksheet.write(index, 9, row.get("is_correct") or "")
        worksheet.write(index, 10, row.get("reasoning_path") or row.get("reasoning") or "")
    worksheet.set_column(0, 0, 20)
    worksheet.set_column(1, 1, 64)
    worksheet.set_column(2, 10, 16)
    workbook.close()

    return {
        "json": json_path,
        "excel": excel_path,
        "traded_decisions": len(decisions),
        "quick_sim_trades": len(trades),
        "paper_trading_runs": len(runs),
    }


def _pre_reset_snapshot(conn: sqlite3.Connection, tag: str = "pre_reset") -> str:
    """重置前强制留一份完整快照（VACUUM INTO，不锁库、可回溯）。

    为什么加：2026-11-19 08:31 全库 1,274 条决策被清空，`decision_ref_archive`
    里一行都没有、日志里也没有 [RESET] —— 事后只能靠 backups/ 下两个
    恰好存在的 .bak 才把历史找回来。删除是不可逆动作，不能指望"碰巧有备份"：
    快照失败就直接抛错、拒绝删除。

    Returns:
        快照文件的绝对路径。
    """
    from pathlib import Path as _P
    import time as _t

    # 库文件真实路径（mode=ro 的连接也能读到）
    row = conn.execute("PRAGMA database_list").fetchone()
    db_path = _P(row[2] or "").resolve()
    if not db_path.exists():
        raise RuntimeError(f"无法定位数据库文件: {row[2]!r}")

    bdir = db_path.parent / "backups"
    bdir.mkdir(parents=True, exist_ok=True)
    snap = bdir / f"{db_path.name}.snap_{_t.strftime('%Y%m%d_%H%M%S', _t.localtime())}_{tag}.db"
    conn.execute("VACUUM INTO ?", (str(snap),))
    if not snap.exists() or snap.stat().st_size < 1024:
        raise RuntimeError(f"快照写入异常: {snap}")
    return str(snap)


def _ensure_decision_history(conn: sqlite3.Connection) -> None:
    """建 ai_decisions_history（v477）：重置时把决策本体搬到这里。

    为什么要这张表：ai_decisions 会被"重置交易历史"DELETE，而胜负率/赔率
    需要跨重置累积样本。历史表只做**搬运**，字段口径与主表一致
    （多 reset_at / reset_batch 两个溯源列），统计时按 id 去重合并。
    """
    conn.execute(
        """CREATE TABLE IF NOT EXISTS ai_decisions_history (
            id INTEGER PRIMARY KEY,
            news_id INTEGER,
            created_at TEXT,
            ts INTEGER,
            suggested_action TEXT,
            sentiment_score REAL,
            target_asset TEXT,
            market_category TEXT,
            reasoning TEXT,
            settled INTEGER,
            is_correct INTEGER,
            forward_pnl REAL,
            entry_price REAL,
            entry_time TEXT,
            truth_verdict TEXT,
            strategy_id TEXT,
            original_action TEXT,
            gate_chain TEXT,
            reset_at TEXT,
            reset_batch TEXT
        )""")
    for col, typ in (("settled", "INTEGER"), ("is_correct", "INTEGER"),
                     ("forward_pnl", "REAL"), ("entry_price", "REAL"),
                     ("truth_verdict", "TEXT"), ("original_action", "TEXT"),
                     ("gate_chain", "TEXT")):
        try:
            conn.execute(f"ALTER TABLE ai_decisions_history ADD COLUMN {col} {typ}")
        except sqlite3.OperationalError:
            pass
    try:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_adhist_action_ts "
            "ON ai_decisions_history(suggested_action, created_at)")
    except sqlite3.OperationalError:
        pass


def _clear_trading_traces(conn: sqlite3.Connection) -> Dict[str, int]:
    """彻底归零：清空全部交易历史 + 历史新闻，统计从 0 开始。

    删除：ai_decisions（信号/持仓/结算）、quick_sim_trades、paper_trading_runs、
         trade_reflections（交易反思）、news_trade_reviews（新闻交易归因）、
         raw_news（历史新闻，界面不再出现重置前旧闻）+ news_events/factor_snapshots/
         signal_performance（孤儿事件数据）
    保留：review_insights（复盘经验）、filter_keywords、strategies
    """
    # 注意顺序：quick_sim_trades 有 ON DELETE CASCADE 外键指向 ai_decisions，
    # 若先删 ai_decisions，quick_sim_trades 会被级联删除、rowcount 不计入 → 先删子表。
    # ── v321.3 P2: 重置**前**先归档决策的引用 + 语义 ──────────────────────────
    # 为什么要归档: 这张表**不参与重置**(下面没有任何 DELETE 碰它), 所以重置之后
    # 决策本身(asset/action/decision_ts/结算结果)仍然留着 —— 它们是复盘与校准
    # 要用的真数据, 不该随"重置交易状态"被抹掉; 同时原始 news_id / ai_decision_id
    # 也留着, 等上游重建后可以自动对齐回去。
    # 没有这一步 = 实测那次留下 1998+1783 条死指针, 且上游已被物理删除、不可恢复。
    archive_info = {"archived": 0, "batch": ""}
    try:
        from signals import decision_ref_archive as _dra
        archive_info = _dra.archive_decisions(
            conn, note="pre_reset_backup")
        print(f"[RESET] 已归档 {archive_info.get('archived')} 条决策引用 "
              f"(批次 {archive_info.get('batch')})", flush=True)
    except Exception as _arch_exc:
        print(f"[RESET] ⚠️ 决策归档失败（原先静默吞掉）: "
              f"{type(_arch_exc).__name__}: {str(_arch_exc)[:120]}", flush=True)
    # 删除前强制快照 —— 失败就抛错，宁可不重置也不留不可逆的数据缺口。
    _n_dec = conn.execute("SELECT COUNT(*) FROM ai_decisions").fetchone()[0]
    snap_path = _pre_reset_snapshot(conn, tag="before_clear_trading_traces")
    print(f"[RESET] 即将删除 {_n_dec} 条决策；快照已留: {snap_path}", flush=True)

    # v477 P0：重置前把**决策本体**（含结算结果）搬进 ai_decisions_history。
    # 根因：ai_decisions 被 DELETE 之后，胜负率/赔率样本永远从 0 重新开始 ——
    # 实测 11-19 一天内被启动重置+trade 重置清了 5 次，"8 笔 0 胜 8 负"永远
    # 攒不到第二天的对照。归档是**真实数据的搬迁**，不是补造数据。
    hist_n = 0
    try:
        _ensure_decision_history(conn)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(ai_decisions)")]
        keep = [c for c in (
            "id", "news_id", "created_at", "ts", "suggested_action",
            "sentiment_score", "target_asset", "market_category", "reasoning",
            "settled", "is_correct", "forward_pnl", "entry_price", "entry_time",
            "truth_verdict", "strategy_id", "original_action", "gate_chain",
        ) if c in cols]
        if keep:
            _cl = ", ".join(f"'{c}'" for c in keep)
            conn.execute(
                f"INSERT INTO ai_decisions_history ({_cl}, reset_at, reset_batch) "
                f"SELECT {', '.join(keep)}, ?, ? FROM ai_decisions "
                f"WHERE id NOT IN (SELECT id FROM ai_decisions_history)",
                (time.strftime("%Y-%m-%d %H:%M:%S"),
                 f"reset@{time.strftime('%Y%m%d_%H%M%S')}"))
            hist_n = conn.execute(
                "SELECT COUNT(*) FROM ai_decisions_history").fetchone()[0]
            print(f"[RESET] 已归档决策 {len(keep)} 字段 → "
                  f"ai_decisions_history（累计 {hist_n} 条）", flush=True)
    except Exception as _hist_exc:
        print(f"[RESET] ⚠️ 决策归档失败（不阻断重置，仅丢历史）: "
              f"{type(_hist_exc).__name__}: {str(_hist_exc)[:120]}", flush=True)

    cleared_trades = conn.execute("DELETE FROM quick_sim_trades").rowcount
    cursor = conn.execute("DELETE FROM ai_decisions")
    cleared_signals = cursor.rowcount
    cleared_runs = conn.execute("DELETE FROM paper_trading_runs").rowcount
    cleared_reflections = conn.execute("DELETE FROM trade_reflections").rowcount
    cleared_reviews = conn.execute("DELETE FROM news_trade_reviews").rowcount
    # 历史新闻彻底删除（重置后界面/统计不再出现旧闻，从 0 开始）
    cleared_news = conn.execute("DELETE FROM raw_news").rowcount
    # ── v321.3 P1: trade_decisions 的上游引用必须一起清 ─────────────────────
    # 漏了这一步 = 「重置」后留下 1998 条**孤儿引用**: trade_decisions.ai_decision_id
    # 指向已被 DELETE 掉的 ai_decisions, news_id 指向已删的 raw_news。
    # 后果: ① 对账永远有悬空 (G09 判成「断链」); ② 任何按 ai_decision_id 的
    # 关联/统计全部错位; ③ 重置后新旧数据混在一个 id 空间里, 无法分辨。
    #
    # 这里**保留 trade_decisions 本身**（asset/action/decision_ts/结算结果是
    # 复盘与校准的宝贵数据, 删了可惜）, 只把指向已删上游的两个指针置 NULL,
    # 并用 context_json 记一笔「上游已随重置清除」。
    cleared_refs = 0
    try:
        cur = conn.execute(
            "UPDATE trade_decisions SET ai_decision_id=NULL, news_id=NULL, "
            "context_json=COALESCE(context_json,'{}') "
            "WHERE ai_decision_id IS NOT NULL OR news_id IS NOT NULL"
        )
        cleared_refs = cur.rowcount or 0
    except sqlite3.OperationalError as _ref_exc:
        print(f"[RESET] ⚠️ trade_decisions 引用清理失败: "
              f"{type(_ref_exc).__name__}: {str(_ref_exc)[:100]}", flush=True)
    # 关联孤儿数据（无 FK 的表，raw_news/ai_decisions 删除后残留）
    for orphan_table in ("news_events", "factor_snapshots", "signal_performance"):
        try:
            conn.execute(f"DELETE FROM {orphan_table}")
        except sqlite3.OperationalError as _orph_exc:
            # v322: 孤儿表没清掉 → 重置后统计/界面仍混入上一轮残留数据
            print(f"[RESET] ⚠️ 孤儿表 {orphan_table} 清理失败: "
                  f"{type(_orph_exc).__name__}: {str(_orph_exc)[:100]}", flush=True)
    # ── v482: 决策派生表孤儿清理 ────────────────────────────────────────────
    # 上面只删了 ai_decisions 与几张显式清单表，而 decision_errors / 多周期读数 /
    # 结构化反思等 12 张白名单表都用 decision_id / ai_decision_id 指回决策。
    # 漏掉 = 重置后统计分母混入幽灵行（实测 11-19 那次 decision_errors 有 231/781
    # 条指向已删决策，E001a/E001b 占比被系统性拉高）。这里在同一次事务里清掉，
    # 并把待删行导出 JSON（~/.trident/backups/decision_orphans_*.json）以备回溯。
    orphans = {"total_orphans": 0, "deleted": 0, "refused": []}
    try:
        from decision_orphans import purge as _purge_orphans
        orphans = _purge_orphans(conn, dry_run=False)
        print(f"[RESET] 孤儿清理: 待删 {orphans.get('total_orphans')} 条 / "
              f"已删 {orphans.get('deleted')} 条 / 拒删 {len(orphans.get('refused') or [])} 表；"
              f"dump={orphans.get('dump')}", flush=True)
    except Exception as _orph_exc:
        print(f"[RESET] ⚠️ 决策派生表孤儿清理失败（不阻断重置）: "
              f"{type(_orph_exc).__name__}: {str(_orph_exc)[:120]}", flush=True)
    # quick_sim_reset_at：重置时间戳（UTC，与 ai_decisions.created_at 同格式）。
    # 快速评测只处理该时间之后产生的信号，避免用重置前保留的历史信号重新建仓结算。
    reset_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        """UPDATE paper_trading_settings
           SET is_running=0, active_run_id=NULL, started_at=NULL, quick_sim_reset_at=? WHERE id=1""",
        (reset_at,),
    )
    return {
        "signals": cleared_signals,
        "quick_sim_trades": cleared_trades,
        "paper_trading_runs": cleared_runs,
        "trade_reflections": cleared_reflections,
        "news_trade_reviews": cleared_reviews,
        "raw_news": cleared_news,
        "trade_decision_refs_cleared": cleared_refs,
        "decision_archive": archive_info,
        "decision_orphans": orphans,
        "quick_sim_reset_at": reset_at,
    }


async def infra_health_watcher() -> None:
    import socket
    import sqlite3

    async def _probe_engine() -> None:
        def _sync() -> tuple[bool, str]:
            try:
                with socket.create_connection((config.LOCAL_HOST, config.ENGINE_WEBHOOK_PORT), timeout=2.0):
                    return True, ""
            except Exception as e:
                return False, f"{type(e).__name__}: {str(e)[:120]}"
        ok, err = await asyncio.to_thread(_sync)
        if ok:
            await source_status.areport("engine_webhook", True,
                                        detail=f"{config.LOCAL_HOST}:{config.ENGINE_WEBHOOK_PORT} reachable")
        else:
            await source_status.areport("engine_webhook", False, error=err or "TCP 连接失败")

    async def _probe_db() -> None:
        def _sync() -> tuple[bool, str]:
            try:
                db_path = config.DB_PATH  # config.DB_PATH = $TRIDENT_DB_PATH or BASE_DIR/trident_event_bus.db
                # v189-4: 改用 db.get_connection() 继承 PRAGMA (busy_timeout=60s + WAL + NORMAL),
                # 去掉 BEGIN IMMEDIATE — 健康探针不需要立刻写锁,默认 DEFERRED 即可,
                # 单 INSERT 自动 implicit BEGIN, ROLLBACK 释放.
                # 之前 timeout=2.0 配合 BEGIN IMMEDIATE 在多 watcher 并发时立刻 SQLITE_BUSY,
                # 是 api_server 持锁卡住其他 INSERT 的元凶之一.
                # v370: WAL 下多个进程同时写时, 探针自己也会撞上 "database is locked".
                # 原来一次失败就把整个 DB 源打成 error(健康度和总分一起掉), 实际锁是瞬时的.
                # 这里对锁冲突重试 3 次(每次 1.2s), 只有真正写不进去才算故障.
                last_err = ""
                for _attempt in range(3):
                    try:
                        conn = db.get_connection()
                        try:
                            conn.execute("CREATE TABLE IF NOT EXISTS _infra_health_probe(t INTEGER)")
                            conn.execute("INSERT INTO _infra_health_probe(t) VALUES (?)", (int(time.time()),))
                            conn.execute("DELETE FROM _infra_health_probe WHERE t < ?", (int(time.time()) - 3600,))
                            conn.rollback()
                            return True, "write+rollback OK"
                        finally:
                            conn.close()
                    except Exception as e:
                        last_err = f"{type(e).__name__}: {str(e)[:120]}"
                        if "locked" not in str(e).lower() and "busy" not in str(e).lower():
                            break
                        time.sleep(1.2)
                return False, last_err
            except Exception as e:
                return False, f"{type(e).__name__}: {str(e)[:120]}"
        ok, err = await asyncio.to_thread(_sync)
        if ok:
            await source_status.areport("trident_event_bus_db", True, detail=err)
        else:
            await source_status.areport("trident_event_bus_db", False, error=err or "DB 写入失败")

    async def _probe_self() -> None:
        # api_server 自身：检查事件循环响应时间（在本任务里 sleep 0 即代表事件循环未被卡住）
        try:
            t0 = time.time()
            await asyncio.sleep(0)
            latency_ms = (time.time() - t0) * 1000
            # 同时探测 /health（如果未来加了这个端点）；现在只检查事件循环
            await source_status.areport("api_server", True, detail=f"loop {latency_ms:.1f}ms", latency_ms=latency_ms)
        except Exception as e:
            await source_status.areport("api_server", False, error=f"{type(e).__name__}: {str(e)[:120]}")

    while True:
        try:
            await _probe_engine()
            await _probe_db()
            await _probe_self()
        except Exception as e:
            print(f"[INFRA-HEALTH] 探测异常: {type(e).__name__}: {str(e)[:120]}")
        await asyncio.sleep(30.0)


async def _reverse_attribution_watcher() -> None:
    """v176.7 P3-B: 反向归因 —— abnormal_signals ↔ ai_decisions 双向桥接。

    思路: 每个 abnormal 点 ±1h 内有 created_at 的决策 → 视为"有覆盖"；
    否则视为"漏检"（missed_signal=1）。写入 reverse_attributions 用于 v189 反思系统。
    零网络/零 LLM 调用，纯 SQL 扫描。
    """
    await asyncio.sleep(75)  # 错峰于 chain_archive
    while True:
        try:
            def _cycle() -> Dict[str, int]:
                totals = {"abnormals": 0, "covered": 0, "inserted": 0}
                # v490 P0-1 长事务拆短：原先整批复用一个连接、遍历 500 条异动后末尾
                # 一次 commit，首条 INSERT 起写锁跨整轮。改为只读扫描/关联查询走只读
                # 连接，每条 UPSERT 各自取短连接独立提交，写锁持有时长=单条 upsert。
                ro = db.get_connection()
                ro.row_factory = sqlite3.Row
                try:
                    # 扫最近 24h abnormal_signals（ts 是秒）
                    cutoff_ts = int(time.time()) - _REVERSE_ATTR_WINDOW_HOURS * 3600
                    rows = ro.execute(
                        """
                        SELECT id, asset, ts, signal_kind, detail_json
                          FROM abnormal_signals
                         WHERE ts >= ?
                         ORDER BY ts DESC
                         LIMIT 500
                        """,
                        (cutoff_ts,),
                    ).fetchall()
                    enriched = []
                    for r in rows:
                        totals["abnormals"] += 1
                        asset_v = (r["asset"] or "").upper()
                        ab_ts = int(r["ts"] or 0)
                        if not asset_v or ab_ts <= 0:
                            continue
                        # ±1h 内 ai_decisions（只读）
                        related = ro.execute(
                            """
                            SELECT id, is_correct, missed_signal
                              FROM ai_decisions
                             WHERE target_asset = ?
                               AND created_at >= datetime(?, 'unixepoch', '-1 hours')
                               AND created_at <= datetime(?, 'unixepoch', '+1 hours')
                            """,
                            (asset_v, ab_ts, ab_ts),
                        ).fetchall()
                        related_ids = [int(x["id"]) for x in related]
                        missed = sum(1 for x in related if x["missed_signal"] == 1)
                        wins = sum(1 for x in related if (x["is_correct"] or "").upper() == "WIN")
                        losses = sum(1 for x in related if (x["is_correct"] or "").upper() == "LOSS")
                        cov = min(1.0, len(related_ids) / 5.0)  # ≥5 决策算 100% 覆盖
                        pct = None
                        try:
                            dj = json.loads(r["detail_json"] or "{}")
                            pct = dj.get("pct_change") if isinstance(dj, dict) else None
                        except Exception:
                            pass
                        enriched.append((
                            asset_v, ab_ts, pct, r["signal_kind"] or "",
                            ",".join(str(x) for x in related_ids[:20]),
                            missed, wins, losses, cov,
                            json.dumps({"abnormal_id": int(r["id"]), "related_n": len(related_ids),
                                        "detail_json": r["detail_json"]}),
                            bool(related_ids),
                        ))
                finally:
                    ro.close()
                for (asset_v, ab_ts, pct, kind, rel_ids, missed, wins, losses,
                     cov, detail, has_rel) in enriched:
                    if has_rel:
                        totals["covered"] += 1
                    w = db.get_connection()
                    try:
                        w.execute(
                            """
                            INSERT OR REPLACE INTO reverse_attributions (
                                asset, abnormal_ts, abnormal_pct, abnormal_kind,
                                related_decision_ids, missed_signal_count,
                                forward_win_count, forward_loss_count, coverage_ratio,
                                detail_json, created_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                            """,
                            (asset_v, ab_ts, pct, kind, rel_ids, missed,
                             wins, losses, cov, detail),
                        )
                        w.commit()
                        totals["inserted"] += 1
                    finally:
                        w.close()
                return totals
            out = await asyncio.to_thread(_cycle)
            if out["abnormals"]:
                print(
                    f"[REVERSE-ATTR] abnormals={out['abnormals']} covered={out['covered']} "
                    f"inserted={out['inserted']} (window={_REVERSE_ATTR_WINDOW_HOURS}h)",
                    flush=True,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[REVERSE-ATTR] cycle crashed: {type(exc).__name__}: {str(exc)[:120]}", flush=True)
        await asyncio.sleep(_REVERSE_ATTR_INTERVAL_S)
