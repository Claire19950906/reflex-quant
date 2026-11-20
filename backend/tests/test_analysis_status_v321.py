# -*- coding: utf-8 -*-
"""v321：分析状态协议回归测试。

覆盖三个真实事故：
  1. api_server 漏传 is_noise → 1550 条噪声显示成"待 AI 分析"（8000 端口）
  2. gate_unavailable=1 的冻结行没状态 → 759 条永久"待 AI 分析"
  3. 门卫回放从未被调度 → 冻结数据永久解不开
"""
import os
import sys

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "src_python")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import event_status


class TestStatusConstants:
    def test_gate_pending_is_valid_status(self):
        """GATE_PENDING 必须在 VALID_STATUSES 里（DB CHECK 约束同源）。"""
        assert event_status.STATUS_GATE_PENDING in event_status.VALID_STATUSES
        assert event_status.STATUS_GATE_PENDING == "GATE_PENDING"

    def test_gate_pending_is_not_terminal(self):
        """GATE_PENDING 不是终态 —— 它还在等门卫回放。"""
        assert event_status.STATUS_GATE_PENDING not in event_status.TERMINAL_STATUSES

    def test_labels_and_reasons_cover_all_statuses(self):
        """每个**非终态**状态都要有徽章文案和 reason 文案（前端直接展示）。

        DONE 不在列 —— 它是终态，前端 AnalysisStatusBadge 对 DONE 直接返回 null
        （不显示任何徽章），所以不需要 label。
        """
        for st in event_status.VALID_STATUSES:
            if st == event_status.STATUS_DONE:
                continue
            assert st in event_status.STATUS_LABELS, st
            assert st in event_status.STATUS_REASONS, st

    def test_gate_pending_label_distinct_from_pending(self):
        """「门卫待回放」必须和「待 AI 分析」文案不同，否则等于没修。"""
        assert (event_status.STATUS_LABELS[event_status.STATUS_GATE_PENDING]
                != event_status.STATUS_LABELS[event_status.STATUS_PENDING])


class TestResolveAnalysisStatus:
    def test_noise_is_skipped(self):
        """噪声 → SKIPPED（不是"待 AI 分析"）。"""
        assert event_status.resolve_analysis_status(
            "PENDING", is_noise=True) == event_status.STATUS_SKIPPED

    def test_gate_pending_flag_yields_gate_pending(self):
        """gate_unavailable → GATE_PENDING。"""
        assert event_status.resolve_analysis_status(
            "PENDING", gate_pending=True) == event_status.STATUS_GATE_PENDING

    def test_gate_pending_beats_has_decision(self):
        """脏数据（既有决策又 gate 冻结）→ GATE_PENDING 更能反映真相。"""
        assert event_status.resolve_analysis_status(
            "PENDING", has_decision=True, gate_pending=True
        ) == event_status.STATUS_GATE_PENDING

    def test_normal_pending_stays_pending(self):
        """普通待分析仍是 PENDING（不能被误判）。"""
        assert event_status.resolve_analysis_status(
            "PENDING", is_noise=False, gate_pending=False
        ) == event_status.STATUS_PENDING

    def test_noise_beats_gate_pending(self):
        """噪声优先于 gate 冻结（噪声永远不会被分析）。"""
        assert event_status.resolve_analysis_status(
            "PENDING", is_noise=True, gate_pending=True
        ) == event_status.STATUS_SKIPPED

    def test_has_decision_yields_done(self):
        """有决策 → DONE。"""
        assert event_status.resolve_analysis_status(
            "PENDING", has_decision=True) == event_status.STATUS_DONE

    def test_raw_status_passthrough(self):
        """raw_status 合法值原样透传。"""
        for st in ("PENDING", "PROCESSING", "DONE", "FAILED", "SKIPPED"):
            assert event_status.resolve_analysis_status(st) == st

    def test_unknown_status_falls_back_to_pending(self):
        """未知状态 → PENDING（诚实：还需要被处理）。"""
        assert event_status.resolve_analysis_status("WHATEVER") == event_status.STATUS_PENDING
        assert event_status.resolve_analysis_status(None) == event_status.STATUS_PENDING

    def test_status_reason_for_gate_pending(self):
        """GATE_PENDING 的 reason 文案要说清是"等回放"而非"待分析"。"""
        r = event_status.status_reason(event_status.STATUS_GATE_PENDING)
        assert "回放" in r



class TestApiServerPassthrough:
    """api_server 必须把 is_noise / gate_unavailable 透传进去。"""

    def test_api_server_normalize_passes_noise_and_gate(self):
        import api_server
        import inspect
        src = inspect.getsource(api_server._normalize_analysis_status)
        assert "is_noise=is_noise" in src
        assert "gate_pending=gate_pending" in src

    def test_api_server_row_to_event_reads_flags(self):
        """_row_to_event 必须从行里读 is_noise / gate_unavailable。"""
        import api_server
        import inspect
        src = inspect.getsource(api_server._row_to_event)
        assert '"is_noise"' in src
        assert '"gate_unavailable"' in src

    def test_bridge_server_passes_gate_pending(self):
        # 2026-11-18: 新闻行→事件这段（含 gate_pending 判定）已搬进 bridge_news，
        # 断言改成"两个模块的源码里能找到"——它锁的是行为，不是文件位置。
        import bridge_server
        import bridge_news
        import inspect
        src = inspect.getsource(bridge_server) + inspect.getsource(bridge_news)
        # 仅 gate_unavailable=1（冻结待回放）算 GATE_PENDING；
        # =2 是降级放行，不能挂待回放 —— 断言判定必须是 == 1，而不是 truthy。
        assert 'gate_pending=int(_rget(r, "gate_unavailable", 0) or 0) == 1' in src

    def test_event_select_actually_selects_the_flags(self):
        """v321 fix9 回归：SELECT 里必须真的带出这两列。

        这是本轮最隐蔽的 bug —— _row_to_event 读 _safe(row, "gate_unavailable", 0)，
        但 _EVENT_SELECT 根本没 SELECT 这一列 → _safe 恒返回 0 →
        724 条门卫冻结行在 8000 端口仍显示「待 AI 分析」。
        光看 Python 代码完全看不出问题，必须断言 SQL 文本。
        """
        import api_server
        assert "rn.gate_unavailable" in api_server._EVENT_SELECT
        assert "rn.is_noise" in api_server._EVENT_SELECT

    def test_bridge_legacy_queries_select_gate_unavailable(self):
        """bridge 的 news_events 兜底路径也要 SELECT 出该列。"""
        import bridge_server
        import inspect
        src = inspect.getsource(bridge_server)
        # 两处 news_events 基准查询（/api/events 兜底 + SSE 兜底）
        assert src.count("rn.gate_unavailable,") >= 2


class TestGateReplayGuard:
    """门卫回放守护：健康预检 + 幂等 + 失败不丢数据。"""

    def test_backlog_count_query_shape(self):
        from engine import gate_replay_guard
        assert callable(gate_replay_guard.count_backlog)
        assert callable(gate_replay_guard.run_once)
        assert callable(gate_replay_guard.probe_channels)

    def test_guard_module_has_probe_gate(self):
        """回放前必须有通道健康预检（不健康就不消耗额度）。"""
        from engine import gate_replay_guard
        import inspect
        src = inspect.getsource(gate_replay_guard.run_once)
        assert "probe_channels" in src
        assert "skip_probe" in src

    def test_guard_uses_independent_transactions(self):
        """每条回放单独 commit（不能攒批持写锁）。"""
        from engine import gate_replay_guard
        import inspect
        src = inspect.getsource(gate_replay_guard._replay_one)
        assert "conn.commit()" in src

    def test_guard_never_writes_filtered_status(self):
        """status 必须是 PENDING。

        'FILTERED' 违反 raw_news 的 CHECK 约束（PENDING/PROCESSING/DONE/FAILED），
        写它会 IntegrityError 让整批回放崩掉。注意源码注释里**提到**了
        FILTERED（解释为什么不能写），所以这里断言的是实际 SQL 片段。
        """
        from engine import gate_replay_guard
        import inspect
        src = inspect.getsource(gate_replay_guard._replay_one)
        assert "status='PENDING'" in src
        # UPDATE 语句里不能出现 'FILTERED' 作为 status 取值
        update_stmt = src.split("UPDATE raw_news", 1)[-1].split("WHERE id=?", 1)[0]
        assert "FILTERED" not in update_stmt


class TestAiWorkerSelfHeal:
    """ai_worker 主循环必须接入自愈回放。"""

    def test_ai_worker_calls_gate_replay(self):
        import inspect
        import engine.ai_worker as aw
        src = inspect.getsource(aw)
        assert "_load_gate_replay_guard" in src
        assert "_GATE_REPLAY_INTERVAL_S" in src
        assert "_GATE_REPLAY_BATCH" in src

    def test_loader_returns_callable(self):
        """加载器必须返回真函数（不是 None）。"""
        from engine.ai_worker import _load_gate_replay_guard
        fn = _load_gate_replay_guard()
        assert fn is not None, "gate_replay_guard 加载失败 → 自愈不会运行"
        assert callable(fn)

    def test_loader_works_without_engine_dir_on_syspath(self):
        """⚠️ 核心回归：引擎真实 sys.path 里**没有** src_python/engine/。

        引擎启动方式：`cd src_python && python3 engine.py`
          → engine.py 只把 src_python/ 插入 sys.path
          → engine/main.py 用 `from .ai_worker import ...`（包内导入）
          → sys.path 里有 src_python/，**没有** src_python/engine/

        所以平铺导入 `from gate_replay_guard import run_once` 必然 ImportError。
        历史 bug：这段被 try/except 静默吞掉 → 自愈从未运行 → 759 条永久冻结，
        而且没有任何报错提示。本测试锁死正确行为。
        """
        import importlib
        import os
        import sys as _sys

        engine_dir = os.path.join(SRC, "engine")
        saved = list(_sys.path)
        try:
            # 严格复刻运行时：只留 src_python/，去掉 engine/
            _sys.path = [p for p in _sys.path
                         if os.path.abspath(p or ".") != os.path.abspath(engine_dir)]
            if SRC not in _sys.path:
                _sys.path.insert(0, SRC)

            # 平铺导入必须失败（证明这个 bug 是真实存在的）
            # 先清 sys.modules 缓存，否则别的测试先 import 过会命中缓存
            _sys.modules.pop("gate_replay_guard", None)
            try:
                importlib.import_module("gate_replay_guard")
                raise AssertionError(
                    "平铺导入竟然成功了 —— 说明 engine/ 在 sys.path 里，"
                    "本测试的前提（复刻运行时）已失效，需重新核对")
            except ImportError:
                pass

            # 包导入必须成功
            mod = importlib.import_module("engine.gate_replay_guard")
            assert callable(mod.run_once)

            # 加载器必须能正确解析出这个函数
            from engine.ai_worker import _load_gate_replay_guard
            fn = _load_gate_replay_guard()
            assert fn is not None, "运行时 sys.path 下加载器解析失败"
            assert callable(fn)
            assert fn.__module__ == "engine.gate_replay_guard"
        finally:
            _sys.path = saved
            _sys.modules.pop("gate_replay_guard", None)

    def test_startup_selfcheck_exists(self):
        """启动自检：guard 不可用时必须**明确报错**，不能静默。"""
        import inspect
        import engine.ai_worker as aw
        src = inspect.getsource(aw.ai_worker)
        assert "_load_gate_replay_guard()" in src
        # 静默吞掉 = 历史 bug 复发，必须有明确的错误输出
        assert "致命" in src

    def test_replay_runs_off_event_loop(self):
        """⚠️ 回放必须放线程跑，不能阻塞 async 事件循环。

        回放是同步 LLM 调用，每条 2-4 秒，一轮 50 条 = 100-200 秒。
        直接在 async 函数里同步调用 → 事件循环被卡死 2-3 分钟 →
        ingest / reflection / 推送全部饿死 → 引擎看起来"死了"。
        必须用 asyncio.to_thread 挪到工作线程。
        """
        import inspect
        import engine.ai_worker as aw
        src = inspect.getsource(aw.ai_worker)
        assert "asyncio.to_thread" in src
        # 不能是裸调用（同步阻塞）
        assert "_gr_once(limit=_GATE_REPLAY_BATCH)" not in src


class TestEventsCachePerformance:
    """v321：/api/events 缓存的并发与性能护栏。

    实测事故：api_server._safe() 用 `key in row.keys()` 判断列存在，
    而 sqlite3.Row.keys() 每次调用都新建完整列名 list →
    _row_to_event 每行调 78 次 → 4380 行 = 34 万次重建列表 →
    单次 reload 18 秒。TTL 60s 过期后每个并发请求各自 reload →
    线程池打满 → 接口 90s 无响应。
    """

    def test_safe_uses_constant_time_lookup(self):
        """_safe 不能用 row.keys()（每次新建列名 list）。"""
        import ast
        import inspect
        import api_server
        src = inspect.getsource(api_server._safe)
        # 去掉 docstring 再断言 —— 文档里会解释"为什么要改"，
        # 直接对整段源码 grep 会命中说明文字。
        tree = ast.parse(src)
        fn = tree.body[0]
        if (fn.body and isinstance(fn.body[0], ast.Expr)
                and isinstance(fn.body[0].value, ast.Constant)
                and isinstance(fn.body[0].value.value, str)):
            fn.body = fn.body[1:]
        code = ast.unparse(fn)
        assert "row.keys()" not in code, "_safe 必须改用 try/except，避免 O(列数) 开销"
        assert "except" in code

    def test_safe_semantics_unchanged(self):
        """_safe 新实现必须与旧实现语义完全一致（sqlite3.Row + dict）。"""
        import sqlite3
        import api_server
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE t (a INTEGER, b TEXT)")
        conn.execute("INSERT INTO t VALUES (1, 'x')")
        row = conn.execute("SELECT * FROM t").fetchone()

        def old_safe(r, k, default=None):
            return r[k] if k in r.keys() else default

        for r in (row, {"a": 1, "b": None}):
            for k, d in (("a", None), ("b", None), ("nope", None),
                         ("nope", "DEF"), ("a", "DEF")):
                assert api_server._safe(r, k, d) == old_safe(r, k, d), (k, d)

    def test_cache_reload_is_single_flight(self):
        """并发请求只触发一次 reload（单飞），其余直接用旧缓存。

        没有单飞时，TTL 一过期每个请求都各自跑一遍 18s 的 reload，
        线程池打满 → /api/events 表现为挂死。
        """
        import inspect
        import api_server
        src = inspect.getsource(api_server._events_15d_get_cached)
        assert "_EVENTS_15D_RELOAD_LOCK" in src
        assert "blocking=False" in src, "必须是**非阻塞**抢锁（抢不到就用旧缓存）"

    def test_concurrent_requests_trigger_single_reload(self):
        """并发 8 个请求 → reload 只跑 1 次，且**请求线程不阻塞**。

        v321 语义：reload 丢后台线程跑，请求方立刻拿旧缓存返回。
        所以 8 个请求都应该在 <0.5s 内返回，而不是等 reload 完成。
        """
        import threading
        import time
        import api_server

        calls = []
        orig = api_server._events_15d_load_sync

        def slow_load():
            calls.append(1)
            time.sleep(1.0)
            return [{"id": i, "analysis_status": "PENDING"} for i in range(3)]

        api_server._events_15d_load_sync = slow_load
        saved_cache = list(api_server._EVENTS_15D_CACHE)
        saved_last = api_server._EVENTS_15D_LAST_LOAD
        try:
            with api_server._EVENTS_15D_LOCK:
                api_server._EVENTS_15D_CACHE.clear()
                api_server._EVENTS_15D_CACHE.extend(
                    [{"id": 0, "analysis_status": "DONE"}])
                # 强制过期
                api_server._EVENTS_15D_LAST_LOAD = 0.0

            results = []
            durations = []

            def worker():
                s = time.time()
                results.append(api_server._events_15d_get_cached(10))
                durations.append(time.time() - s)

            threads = [threading.Thread(target=worker) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)

            # 等后台 reload 跑完，确认只跑了一次
            deadline = time.time() + 10
            while time.time() < deadline and len(calls) < 1:
                time.sleep(0.05)
            time.sleep(0.3)

            assert len(results) == 8
            assert len(calls) == 1, "并发请求触发了 %d 次 reload，应为 1 次" % len(calls)
            # 所有请求都必须**立即**返回（请求线程不等 reload）
            slow = [d for d in durations if d >= 0.5]
            assert not slow, "有 %d 个请求被 reload 阻塞，耗时=%s" % (len(slow), durations)
            # 所有请求都拿到非空结果
            for r in results:
                assert len(r) > 0
        finally:
            api_server._events_15d_load_sync = orig
            with api_server._EVENTS_15D_LOCK:
                api_server._EVENTS_15D_CACHE.clear()
                api_server._EVENTS_15D_CACHE.extend(saved_cache)
            api_server._EVENTS_15D_LAST_LOAD = saved_last

    def test_claim_resets_retry_count(self):
        """认领时重置 retry_count（给满 3 次重试机会）。"""
        import inspect
        import engine.ai_worker as aw
        src = inspect.getsource(aw)
        assert "retry_count=0" in src
