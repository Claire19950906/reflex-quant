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
        import review_agent
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
    _auto_import_seed_config()
    _rescue_deactivated_insights()
    # v200+: 模拟盘默认开启 (engine 直跑场景, 同样要保证 active_run 在 ai_worker 起来前就位,
    # 否则 ai_decisions 第一次写入时 paper_trading_run_id 拿不到). 异常不阻断启动.
    try:
        import paper_trading as _pt
        _pt.ensure_default_running()
    except Exception as _exc:
        print(f"[ENGINE] ensure_default_running 跳过: {type(_exc).__name__}: {str(_exc)[:80]}")
    asyncio.run(main())
