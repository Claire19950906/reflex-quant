# -*- coding: utf-8 -*-
"""v171+ P5：tz_utils / clock CLI 测试。"""
from __future__ import annotations

import json
import os
import sys
import subprocess

import pytest

# 子进程 `python3 -m tz_utils` 需要能 import 到 src_python 下的模块。
# 父进程的 sys.path 改动（conftest 注入的）不会传给子进程，必须显式经 PYTHONPATH 传递。
_SRC_PYTHON = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src_python")
)


def _run_module(module_name: str, args: list, cwd=None) -> subprocess.CompletedProcess:
    """子进程运行 python3 -m <module> <args>... 返回 CompletedProcess。"""
    cmd = [sys.executable, "-m", module_name] + args
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        _SRC_PYTHON if not existing else _SRC_PYTHON + os.pathsep + existing
    )
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=10, cwd=cwd, env=env,
    )


# ── tz_utils CLI ─────────────────────────────────────────────────────────────

def test_cli_tz_utils_validate_ok():
    cp = _run_module("tz_utils", ["validate", "2025-09-21T14:30:00Z"])
    assert cp.returncode == 0
    data = json.loads(cp.stdout.strip())
    assert data["ok"] is True


def test_cli_tz_utils_validate_fail_returns_nonzero():
    cp = _run_module("tz_utils", ["validate", "上周五"])
    assert cp.returncode != 0
    data = json.loads(cp.stdout.strip())
    assert data["ok"] is False


def test_cli_tz_utils_dual_outputs_all_tz():
    cp = _run_module("tz_utils", ["dual", "1758400000", "--tz", "Asia/Shanghai"])
    assert cp.returncode == 0
    data = json.loads(cp.stdout.strip())
    assert data["epoch"] == 1758400000
    assert data["display_tz"] == "Asia/Shanghai"
    assert "utc" in data and data["utc"]
    assert "cn" in data and data["cn"]
    assert "us_eastern" in data and data["us_eastern"]
    assert "us_pacific" in data and data["us_pacific"]


def test_cli_tz_utils_label_window():
    """`tz_utils label 60` → 2h（60 ≤ 120 落在 2h 窗口）。"""
    cp = _run_module("tz_utils", ["label", "60"])
    assert cp.returncode == 0
    assert cp.stdout.strip() == "2h"


def test_cli_tz_utils_label_5min():
    cp = _run_module("tz_utils", ["label", "3"])
    assert cp.returncode == 0
    assert cp.stdout.strip() == "5m"


def test_cli_tz_utils_label_extended():
    cp = _run_module("tz_utils", ["label", "5000"])
    assert cp.returncode == 0
    assert cp.stdout.strip() == "extended"


def test_cli_tz_utils_tz_reports_env():
    cp = _run_module("tz_utils", ["tz"])
    assert cp.returncode == 0
    assert "env TRIDENT_TZ" in cp.stdout
    assert "config.json display.tz" in cp.stdout
    assert "effective display_tz" in cp.stdout


# ── clock CLI ─────────────────────────────────────────────────────────────────

def test_cli_clock_now_outputs_int():
    cp = _run_module("clock", ["now"])
    assert cp.returncode == 0
    assert cp.stdout.strip().isdigit()
    assert int(cp.stdout.strip()) > 1_000_000_000


def test_cli_clock_age_outputs_int():
    import time
    past = int(time.time()) - 3600
    cp = _run_module("clock", ["age", str(past)])
    assert cp.returncode == 0
    age = int(cp.stdout.strip())
    assert 3590 < age < 3610


def test_cli_clock_diagnostics_outputs_json():
    cp = _run_module("clock", ["diagnostics"])
    assert cp.returncode == 0
    data = json.loads(cp.stdout.strip())
    assert "last_wall" in data
    assert "jump_warned" in data


def test_cli_clock_simulate_jump_sets_warned():
    """simulate-jump → 单 process 内注入跳变 + 输出 diagnostics（jump_warned=True）。"""
    cp = _run_module("clock", ["simulate-jump", "60"])
    assert cp.returncode == 0
    data = json.loads(cp.stdout.strip())
    assert data["jump_warned"] is True


def test_cli_clock_no_args_runs_selftest():
    cp = _run_module("clock", [])
    assert cp.returncode == 0
    assert "=== safe_now ===" in cp.stdout
    assert "=== clock_diagnostics ===" in cp.stdout