# -*- coding: utf-8 -*-
"""v452：版本号必须只有一个真值源，且与 CHANGELOG 首行对得上。

事故：CHANGELOG 已写到 v451，`/api/health` 报 v398、8100 报 v308-p1 ——
三个口径互相矛盾，出问题时无法用版本对账。
"""
from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "src_python"))

import config

_CHANGELOG = os.path.join(config.PROJECT_DIR, "CHANGELOG.md")


def test_code_version_defined():
    assert re.fullmatch(r"v\d+", str(config.CODE_VERSION)), config.CODE_VERSION


def test_health_and_bridge_do_not_hardcode_version():
    """两个 server 的 code_version 必须是变量，不能再写死字符串。"""
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    health = open(os.path.join(base, "src_python", "routers", "health.py"),
                  encoding="utf-8").read()
    bridge = open(os.path.join(base, "src_python", "bridge_server.py"),
                  encoding="utf-8").read()
    assert '"code_version": "v' not in health.replace(" ", "")
    assert '"code_version": "v' not in bridge.replace(" ", "")


def test_matches_changelog_head():
    """config.CODE_VERSION 必须等于 CHANGELOG 第一条版本号（发版时同步）。"""
    if not os.path.exists(_CHANGELOG):
        return
    head = ""
    with open(_CHANGELOG, encoding="utf-8") as fh:
        for line in fh:
            m = re.match(r"^##\s*(v\d+)", line.strip())
            if m:
                head = m.group(1)
                break
    assert head, "CHANGELOG 里没有找到版本标题"
    assert head == config.CODE_VERSION, f"CHANGELOG={head} config={config.CODE_VERSION}"
