# -*- coding: utf-8 -*-
"""v450：运维引用体检的测试 —— 防"定时任务调了不存在的脚本还无人发现"。

真实环境里这项检查会扫 ~/Library/LaunchAgents 和 ~/.trident/wrappers；
CI/别的机器上没有这些目录，就退化成"扫描器自身逻辑"测试，不阻塞。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src_python"))

import ops_ref_check as orc


def test_scanner_runs_and_reports_shape():
    rep = orc.scan()
    assert set(rep) == {"jobs", "checked_scripts", "missing", "ok"}
    assert isinstance(rep["missing"], list)
    assert rep["ok"] == (len(rep["missing"]) == 0)


def test_every_missing_entry_has_job_and_path():
    for m in orc.scan()["missing"]:
        assert m.get("job") and m.get("script")


def test_absolute_py_paths_only():
    # 只认 /Users/... 绝对路径，避免把 "scripts/x.py" 这类相对路径误判成断了
    assert orc.PY_RE.pattern.startswith("(/Users/")
