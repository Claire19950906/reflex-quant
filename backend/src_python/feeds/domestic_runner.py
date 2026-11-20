"""国内源抓取的**独立进程**入口（v412-F）。

为什么要有这个文件：引擎进程跑了几小时后，对 gate / 新浪 / 腾讯 / 东财这批域名的
TLS 连接会整体失效（同一时刻终端 `curl` 200、独立脚本能取 8–9/9，但引擎进程里
`qt.gtimg.cn` 都"预算内未返回"，`engine.err.log` 里 SSLError 累计三千多行）。
在引擎进程里重试、加预算都没用——因为它那一套连接本身坏了。

所以把抓取放到**短命子进程**里跑：每个子进程都是干净的连接与 DNS 状态，用完即退。
代价是一次 Python 启动 + import（约 0.1–0.3s），换来的是"引擎侧也能拿到真实行情"。

用法（父进程只读它写出的 JSON 文件，stdout/stderr 都留给日志）：
    python3 feeds/domestic_runner.py <out.json> quotes BTC,ETH,SOL,XAU,WTI
    python3 feeds/domestic_runner.py <out.json> mtf    BTC,ETH,SOL,XAU,WTI
"""

import contextlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _run(out_path: str, mode: str, assets) -> int:
    from feeds import domestic_quotes as dq

    # 抓取函数会往 stdout 打 `[DOMESTIC] …` / `[KLINE-LOCAL] …`，必须挪到 stderr，
    # 否则父进程读到的 JSON 会被日志行污染。
    buf = sys.stderr
    with contextlib.redirect_stdout(buf):
        if mode == "mtf":
            res = dq.fetch_mtf(list(assets))
        else:
            res = dq.fetch_all(list(assets))
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(res or {}, f, ensure_ascii=False, default=str)
    os.replace(tmp, out_path)   # 原子替换，父进程不会读到半个文件
    return 0


def main() -> int:
    if len(sys.argv) < 4:
        print(__doc__, file=sys.stderr)
        return 2
    out_path = sys.argv[1]
    mode = sys.argv[2]
    assets = [a for a in sys.argv[3].split(",") if a]
    try:
        return _run(out_path, mode, assets)
    except Exception as e:
        try:
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump({}, f)
        except Exception:
            pass
        print(f"[DOMESTIC-RUNNER] fail: {type(e).__name__}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
