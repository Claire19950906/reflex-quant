"""国内源缓存刷新（v412-F 最终方案）。

为什么不是"引擎 spawn 子进程"：实测在引擎（多线程）进程里 fork 出来的子进程，
抓取会一直挂到 timeout 才被杀——不论限 5s / 6.5s / 8s，都是恰好在限制点被砍；
而同一脚本在终端跑 4.8s 就完成。这是 macOS 上"多线程进程 fork 后做网络 IO
死锁"的典型形态，不是超时不够，加多少预算都没用。

所以改成**文件邮箱**：本脚本由 launchd 每 45 秒独立跑一次（干净的独立进程），
把结果原子写进缓存文件；引擎只读文件、只判新鲜度，完全不碰网络、也不 fork。

缓存格式：
    {"ts": <float>, "mode": "mtf"|"quotes", "data": {...}}
"""

import contextlib
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CACHE_DIR = os.path.expanduser("~/.trident/cache")
QUOTES_CACHE = os.path.join(CACHE_DIR, "domestic_quotes.json")
MTF_CACHE = os.path.join(CACHE_DIR, "domestic_mtf.json")

_QUOTE_ASSETS = ["BTC", "ETH", "SOL", "XAU", "WTI", "NVDA", "AMZN", "META", "SKHYNIX"]
_MTF_ASSETS = ["BTC", "ETH", "SOL", "XAU", "WTI"]


def _atomic_write(path: str, payload: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, default=str)
    os.replace(tmp, path)


def refresh() -> int:
    from feeds import domestic_quotes as dq  # 同目录

    os.makedirs(CACHE_DIR, exist_ok=True)
    written = 0

    # 报价与 K 线各跑一次；一个失败不影响另一个
    for mode, fn, assets, path in (
        ("quotes", dq.fetch_all, _QUOTE_ASSETS, QUOTES_CACHE),
        ("mtf", dq.fetch_mtf, _MTF_ASSETS, MTF_CACHE),
    ):
        t0 = time.time()
        try:
            with contextlib.redirect_stdout(sys.stderr):
                data = fn(list(assets)) or {}
            if data:
                _atomic_write(path, {"ts": time.time(), "mode": mode, "data": data})
                written += 1
                print(f"[DOMESTIC-REFRESH] {mode} {len(data)} 条 / {time.time()-t0:.2f}s",
                      file=sys.stderr)
            else:
                print(f"[DOMESTIC-REFRESH] {mode} 空（保留旧缓存）", file=sys.stderr)
        except Exception as e:
            print(f"[DOMESTIC-REFRESH] {mode} 失败 {type(e).__name__}: {e}", file=sys.stderr)
    return 0 if written else 1


if __name__ == "__main__":
    sys.exit(refresh())
