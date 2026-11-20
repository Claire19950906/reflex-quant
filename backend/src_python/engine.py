#!/usr/bin/env python3
"""
Trident Agent MVP — Python Production Engine (thin entry point)

Implementation lives in the `engine/` package (main / ingest / ai_worker /
forward / webhook / alerts / prices / ws_client / utils).

Usage:
  python src_python/engine.py
"""

import asyncio
import os
import sys
from pathlib import Path

# 兼容嵌入式 Python：把本脚本所在目录加入 sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent))

# 服务器可能缺少本地 CA 根证书（SSL: CERTIFICATE_VERIFY_FAILED）：
# 统一用 certifi 的完整证书包作为 SSL 默认 CA（对 urllib/ssl 全部生效）
try:
    import certifi
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
except Exception:
    pass

from engine.main import main


def _print_build_stamp() -> None:
    """启动时打印关键模块的构建指纹 —— 一行看清"进程跑的是哪份代码"。

    为什么要它：实测反复出现"改了代码但忘了重启引擎"，于是排查时复现不出线上现象
    （库里那批决策其实是改动前写进去的），白查很久。指纹 = 关键文件的
    mtime + 大小 + 内容 hash 前 8 位：同一份代码不同进程打出来必然一致，
    不一致立刻能看出是进程太旧。
    """
    try:
        import hashlib
        import time
        here = Path(__file__).resolve().parent
        parts = []
        for rel in ("engine/ai_worker.py", "review_agent.py", "config.py"):
            p = here / rel
            if not p.exists():
                continue
            raw = p.read_bytes()
            parts.append(
                f"{rel}={hashlib.sha1(raw).hexdigest()[:8]}"
                f"@{time.strftime('%H:%M:%S', time.localtime(p.stat().st_mtime))}"
                f"({len(raw)}B)"
            )
        print(f"[BUILD] {'  '.join(parts)}", flush=True)
        print(
            "[BUILD] 改动代码后必须重启本进程才会生效"
            "（留痕开关：TRIDENT_SIGNAL_TRACE=1）",
            flush=True,
        )
    except Exception:
        pass


def _auto_import_seed_config() -> None:
    """兜底：配置表为空时自动导入随包种子库（幂等，失败不影响启动）。

    部署时最容易忘的一步就是导入 `seed_config.db`；漏了的话提示库是空的，
    且症状隐蔽（界面正常、分析照跑，只是没有品种规则与经验在起作用）。
    这里在引擎启动时补一次：只在配置表为空时导入，已有数据不覆盖。
    """
    try:
        import config
        import seed_restore
        # v189-4: 用 db.get_connection() 继承 PRAGMA (busy_timeout=60s, synchronous=NORMAL,
        # cache_size=-64MB), 避免裸 sqlite3.connect 默认 busy_timeout=0 立即 SQLITE_BUSY.
        from db import get_connection
        conn = get_connection()
        try:
            seed_restore.auto_import_from_seed(conn)
        finally:
            conn.close()
    except Exception as exc:
        print(f"[SEED] 自动导入跳过（{type(exc).__name__}）", flush=True)


def _rescue_deactivated_insights() -> None:
    """回滚"误杀"的复盘经验 —— **每个库只做一次**（幂等，失败不影响启动）。

    背景：旧的"连坐"判定曾一次停用全部经验；其中有大量经验从没被命中过
    （无任何针对它的证据）。而双向判定只复查"有命中样本"的经验 → 这些误杀会
    永久停在停用。实测生产库与部署种子库都是 110 条全停用，新环境部署后
    提示库等于空的。

    两道保护，保证"你停用的不会被复活"：
      ① **一次性标记** `<库名>.insight_rescued`：做过一次就不再扫描
      ② 只处理 `manual_deactivated = 0` 的（人工在界面上停用的永不动）
    """
    try:
        if os.environ.get("TRIDENT_NO_AUTO_SETUP"):
            return
        import sqlite3
        import config
        from review import review_agent
        import seed_restore

        marker = seed_restore.insight_marker_path()
        if os.path.exists(marker):
            return   # 这个库已经做过一次

        # v189-4: 用 db.get_connection() 继承 PRAGMA (busy_timeout=60s, synchronous=NORMAL).
        from db import get_connection
        conn = get_connection()
        conn.row_factory = sqlite3.Row
        try:
            out = review_agent._evaluate_insights(conn)
            rescued = out.get("rescued_no_sample") or []
            if rescued:
                print(f"[INSIGHT] 启动自愈：恢复 {len(rescued)} 条"
                      f"「停用但无命中样本」的经验（旧连坐停用遗留）", flush=True)
            # 只有真正跑成功才写标记（失败不写 → 下次还会再试）
            try:
                with open(marker, "w", encoding="utf-8") as f:
                    f.write("insight rescue done\n")
            except OSError:
                pass
        finally:
            conn.close()
    except Exception as exc:
        print(f"[INSIGHT] 启动自愈跳过（{type(exc).__name__}）", flush=True)


if __name__ == "__main__":
    _print_build_stamp()
    # ── v483: 引擎自己也认暂停标记 ──────────────────────────────────────────
    # 事故：16:34 已置 DATA_PAUSED，引擎被 watchdog / 外部会话的 restart_backend.sh
    # 反复拉起（17:15、17:19 两次），暂停期照样产出决策。
    # 只在"启动器"这层拦截是拦不住的 —— 谁都能直接 python engine.py。
    # 所以把判定放进进程自身的入口：标记在就不启动，并明确打出是谁拉起的。
    try:
        from pathlib import Path as _P
        _flag = _P.home() / ".trident" / "state" / "DATA_PAUSED"
        if _flag.exists():
            print(f"[ENGINE-REFUSED] 数据暂停标记存在（{_flag}）→ 引擎不启动。"
                  f"恢复请先 bash ~/.trident/wrappers/resume_data.sh", flush=True)
            raise SystemExit(0)
    except SystemExit:
        raise
    except Exception as _exc_pause:
        print(f"[ENGINE-REFUSED] 暂停标记检查异常（按继续启动处理）: "
              f"{type(_exc_pause).__name__}", flush=True)
    # v321.3 P0: fd 泄漏防护 (根治「AI分析挂了」) —— 详见 yf_fd_guard.py 文件头。
    # engine 也调 yfinance (es_feed / nq_feed / asia_prices / fed_rate_expectation ...),
    # 同一个根因同样会打死 engine 的事件循环。
    try:
        import yf_fd_guard
        yf_fd_guard.install()
    except Exception as _exc:
        print(f"[FD-GUARD] yf_fd_guard 安装失败 (忽略): {type(_exc).__name__}: {str(_exc)[:120]}",
              flush=True)
    try:
        import fd_guard
        fd_guard.start_fd_monitor()
    except Exception as _exc2:
        print(f"[FD-GUARD] fd_guard 启动失败 (忽略): {type(_exc2).__name__}: {str(_exc2)[:120]}",
              flush=True)
    # v321.3 P1: 出网代理 (需求 #6 挂单量/资金流 / #14 外部源)
    try:
        import config as _cfg
        _cfg.apply_proxy_env()
    except Exception as _exc3:
        print(f"[PROXY] 应用失败 (忽略): {type(_exc3).__name__}: {str(_exc3)[:120]}",
              flush=True)
    _auto_import_seed_config()
    _rescue_deactivated_insights()

    # v409.1: 收到 SIGTERM 时留痕 —— 实测 09:24–09:47 引擎被连杀 8 次，
    # 日志里只有新进程的 BUILD，旧进程"无声消失"，分不清是自己崩了还是被外部
    # restart_backend.sh / launchd 杀的（launchctl list 显示退出码 -15 = SIGTERM，
    # 但进程侧没有任何证据）。这里只打印一行再恢复默认行为，不改退出语义。
    #
    # v435：再加一条机器可读的记账（runtime/restart_audit.jsonl）——
    # 只有「被杀时刻 + 当时 step + 在办条数」这一半信息还不够，还要能和
    # scripts/restart_audit.py 采到的「调用方」按时间对上，才知道每次重启是谁付的账。
    # handler 里只读小文件（progress json），不碰数据库，避免在信号上下文里阻塞。
    try:
        import signal as _sig
        import time as _tmod

        # v409.4: 信号里不带发送方身份，但父进程能说明「是谁在管我」——
        # ppid=1(launchd) 基本就指向 `launchctl kickstart -k`；否则把父进程命令记下来。
        _parent_info = {"ppid": os.getppid(), "parent_cmd": ""}
        try:
            import subprocess as _sp
            _parent_info["parent_cmd"] = _sp.run(
                ["ps", "-o", "command=", "-p", str(os.getppid())],
                capture_output=True, text=True, timeout=2).stdout.strip()[:200]
        except Exception:
            pass

        # v409.4: 信号本身不带发送方身份，但**退出前那一刻**谁在跑就能说明是谁干的。
        # 原做法是后台线程每 2s 采样，实测两次都采到空（线程没跑起来 / 被杀太快），
        # 改成 handler 里同步采一次：进程本来就要退出，3s 的 ps 不会有副作用。
        def _snapshot_suspects():
            """读 run_engine.sh 里独立采样进程每 2s 写的「可疑杀进程」快照。

            为什么不在 handler 里 fork `ps`：信号上下文里 fork 会死锁（实测两次都
            采到空，且 handler 是唯一证据来源，不能赌）。
            """
            try:
                _p = os.path.expanduser("~/.trident/state/kill_suspects.txt")
                with open(_p, "r", encoding="utf-8") as _fh:
                    lines = [ln.strip()[:180] for ln in _fh if ln.strip()]
                return [{"ts": _tmod.strftime("%Y-%m-%d %H:%M:%S"),
                         "hits": lines[:5]}]
            except Exception:
                return []

        def _write_sigterm_ledger() -> None:
            try:
                import json as _json
                _base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                _rt = os.path.join(_base, "runtime")
                _step = None
                try:
                    with open(os.path.join(_rt, "ai_worker_progress.json"),
                              "r", encoding="utf-8") as _fh:
                        _step = (_json.load(_fh) or {}).get("step")
                except Exception:
                    _step = None
                _rec = {
                    "role": "engine_sigterm",
                    "ts": round(_tmod.time(), 3),
                    "ts_str": _tmod.strftime("%Y-%m-%d %H:%M:%S"),
                    "pid": os.getpid(),
                    "ppid": _parent_info.get("ppid"),
                    "parent_cmd": _parent_info.get("parent_cmd") or "",
                    "suspects": _snapshot_suspects(),
                    "step": _step,
                }
                os.makedirs(_rt, exist_ok=True)
                with open(os.path.join(_rt, "restart_audit.jsonl"),
                          "a", encoding="utf-8") as _fh2:
                    _fh2.write(_json.dumps(_rec, ensure_ascii=False) + "\n")
            except Exception:
                pass   # 记账失败绝不能影响退出

        def _on_term(_signum, _frame):
            print(f"[ENGINE-EXIT] 收到 SIGTERM ({_signum}) @ "
                  f"{_tmod.strftime('%Y-%m-%d %H:%M:%S')} —— 外部要求退出"
                  f"（不是崩溃；查是谁在调 restart_backend.sh）", flush=True)
            _write_sigterm_ledger()
            _sig.signal(_sig.SIGTERM, _sig.SIG_DFL)
            os.kill(os.getpid(), _sig.SIGTERM)

        _sig.signal(_sig.SIGTERM, _on_term)
    except Exception as _exc_sig:
        print(f"[ENGINE-EXIT] SIGTERM 钩子安装失败 (忽略): {type(_exc_sig).__name__}",
              flush=True)
    # v200+: 模拟盘默认开启 (engine 直跑场景, 同样要保证 active_run 在 ai_worker 起来前就位,
    # 否则 ai_decisions 第一次写入时 paper_trading_run_id 拿不到). 异常不阻断启动.
    try:
        import paper_trading as _pt
        _pt.ensure_default_running()
    except Exception as _exc:
        print(f"[ENGINE] ensure_default_running 跳过: {type(_exc).__name__}: {str(_exc)[:80]}")
    asyncio.run(main())
