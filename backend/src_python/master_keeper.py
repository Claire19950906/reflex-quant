"""9001 算法本体守护（v430）。

背景：`com.trident.master` 这个 launchd job 在本机一 bootstrap 就卡在 `xpcproxy`
（进程根本不 spawn），于是 9001 一直靠手工实例跑 —— 重启电脑就不在了。
既然 8000 的 `com.trident.api` 是 launchd 正常托管的，就让它顺带当守护者：
重启后 api_server 起来 → 这里把 9001 拉起来。

设计约束：
  · 只在 8000 进程内部起一个 daemon 线程，每 60s 探一次，绝不阻塞请求；
  · 冷却与次数上限（10 分钟内最多 5 次）避免端口被占时进入疯狂重启；
  · 任何异常都吞掉并写日志，不能影响 8000 自身存活。
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import urllib.request

_SRC = os.path.dirname(os.path.abspath(__file__))
_HOME = os.path.expanduser("~")
_LOG_DIR = os.path.join(_HOME, ".trident", "log")
_PLIST = os.path.join(_HOME, "Library", "LaunchAgents", "com.trident.master.plist")
STATUS = {"last_check": 0.0, "up": None, "restarts": 0, "last_error": None}
_LOCK = threading.Lock()
_THREAD: threading.Thread | None = None
_MAX_RESTARTS = 5
_WINDOW = 600.0
_starts: list[float] = []


def _log(msg: str) -> None:
    try:
        os.makedirs(_LOG_DIR, exist_ok=True)
        with open(os.path.join(_LOG_DIR, "master_keeper.log"), "a", encoding="utf-8") as fh:
            fh.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


def _token() -> str:
    tok = os.environ.get("TRIDENT_MASTER_TOKEN") or ""
    if tok:
        return tok
    try:
        out = subprocess.run(["plutil", "-extract",
                              "EnvironmentVariables.TRIDENT_MASTER_TOKEN", "raw", "-o", "-", _PLIST],
                             capture_output=True, text=True, timeout=5)
        return (out.stdout or "").strip()
    except Exception:
        return ""


def _alive() -> bool:
    """真活着 = /api/health 返回 200 且 body 里 ok 为真。

    v483: 原来注释写着"401 也算活着"—— 于是探针永远 401 也永远判活着，
    等于这个守护什么都没在守。现在 /api/health 已进白名单（master_api.py），
    改成真实校验：连不上 / 非 200 → 判定需要重启。
    """
    try:
        import config as _config
        with urllib.request.urlopen(_config.MASTER_BASE_URL + "/api/health", timeout=3) as r:
            if r.status != 200:
                return False
            body = r.read(200).decode("utf-8", "replace")
        return '"ok"' in body and "true" in body
    except Exception:
        return False


def _start() -> bool:
    now = time.monotonic()
    _starts[:] = [t for t in _starts if now - t < _WINDOW]
    if len(_starts) >= _MAX_RESTARTS:
        _log("10 分钟内已重启 %d 次，跳过本轮" % len(_starts))
        return False
    _starts.append(now)
    env = dict(os.environ)
    env.setdefault("TRIDENT_MASTER_HOST", "127.0.0.1")
    env.setdefault("TRIDENT_MASTER_PORT", "9001")
    tok = _token()
    if tok:
        env["TRIDENT_MASTER_TOKEN"] = tok
    os.makedirs(_LOG_DIR, exist_ok=True)
    out = open(os.path.join(_LOG_DIR, "master.out.log"), "ab")
    try:
        subprocess.Popen([sys.executable, os.path.join(_SRC, "master_api.py")],
                         cwd=_SRC, env=env, stdout=out, stderr=out,
                         start_new_session=True)
        _log("已拉起 master_api")
        return True
    except Exception as exc:
        STATUS["last_error"] = "%s: %s" % (type(exc).__name__, exc)
        _log("拉起失败 %s" % STATUS["last_error"])
        return False


def _loop() -> None:
    time.sleep(20)   # 让 8000 先把自身启动完
    while True:
        try:
            up = _alive()
            STATUS["last_check"] = time.time()
            STATUS["up"] = up
            if not up:
                if _start():
                    time.sleep(25)
                    if _alive():
                        _log("9001 恢复成功")
        except Exception as exc:
            STATUS["last_error"] = "%s: %s" % (type(exc).__name__, exc)
        time.sleep(60)


def ensure_keeper() -> bool:
    global _THREAD
    if os.environ.get("TRIDENT_MASTER_KEEPER", "1") in ("0", "false", "off"):
        return False
    with _LOCK:
        if _THREAD is not None and _THREAD.is_alive():
            return True
        _THREAD = threading.Thread(target=_loop, daemon=True, name="master-keeper")
        _THREAD.start()
        _log("keeper 已启动")
        return True
