#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v500：全模块 import 冒烟（放在子进程里跑，避免污染测试进程）。

为什么需要它：`src_python` 下 438 个模块，很多是没人 import 的脚本。
改了公共模块（config / db / catalog）之后，脚本侧的 import 断裂
没有任何测试会红 —— 直到某天 cron 静默不跑。
这里一次性把 438 个模块全 import 一遍，靠一条断言守住。

子进程的理由：`bridge_server`、`api_server` 这类模块 import 时就会起后台线程、
装信号处理器。放进 pytest 进程会把测试跑成"带着别人家的线程"。
"""
import os
import subprocess
import sys
import textwrap
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
SRC = BACKEND / "src_python"

SCRIPT = textwrap.dedent(
    """
    import importlib, sys, pathlib
    sys.path.insert(0, "src_python")
    files = sorted(p for p in pathlib.Path("src_python").glob("*.py")
                   if p.name != "__init__.py")
    bad = []
    for p in files:
        try:
            importlib.import_module(p.stem)
        except BaseException as e:
            bad.append(f"{p.stem}: {type(e).__name__}: {e}")
    print(f"IMPORTED={len(files)-len(bad)}/{len(files)}")
    for line in bad:
        print("BROKEN " + line)
    sys.exit(1 if bad else 0)
    """
)


def test_every_module_imports():
    if not SRC.exists():
        import pytest
        pytest.skip("src_python 不存在")
    env = dict(os.environ)
    env["TRIDENT_OFFLINE"] = "1"
    proc = subprocess.run(
        [sys.executable, "-c", SCRIPT],
        cwd=str(BACKEND), env=env, capture_output=True, text=True, timeout=180,
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    assert proc.returncode == 0, "有模块 import 不了：\n" + out[-2000:]
    assert "IMPORTED=" in out
    imported = out.split("IMPORTED=")[1].split()[0]
    ok, total = imported.split("/")
    assert int(total) > 300 and int(ok) == int(total)
