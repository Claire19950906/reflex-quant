#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v435：重启审计脚本的解析 / 匹配 / 短命确认。

背景：引擎被外部周期性 SIGTERM（今日近 1 小时 9 次），但仓库里没有调用点，
需要从进程表侧把「谁调了 launchctl」抓出来。抓的过程有两个坑，这里各锁一条：
  1. 不能只按「文本里出现 kickstart」匹配 —— 包装它的 `bash -c "..."`
     以及采集/审计脚本自身的命令行都会命中，日志会被灌成噪声；
  2. 真正的杀手掌是**短命进程**，必须在下一个采样周期确认 pid 已消失才落盘。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import restart_audit as ra


_SAMPLE = """  501     1   501  ttys001 launchctl kickstart -k gui/501/com.trident.engine
  900   501   501  ttys002 /bin/bash -c launchctl kickstart -k gui/501/com.trident.engine
  901   900   501  ttys002 zsh -l
  123     1   501  ??     /bin/bash /Users/claire/.trident/wrappers/restart_backend.sh
  456     1   501  ??     python3 engine.py
  777     1   501  ??     /bin/bash -c vim 不只是前台
  888 54321   501  ??     python3 backend/scripts/restart_audit.py
"""


def test_parse_ps_keeps_fields_aligned():
    rows = ra.parse_ps(_SAMPLE)
    assert [r["pid"] for r in rows] == ["501", "900", "901", "123", "456", "777", "888"]
    assert rows[0]["ppid"] == "1"
    assert rows[1]["cmd"].startswith("/bin/bash -c launchctl")
    assert rows[3]["cmd"].endswith("restart_backend.sh")


def test_match_detects_killer_commands():
    assert ra.match_line("launchctl kickstart -k gui/501/com.trident.engine")
    assert ra.match_line("/bin/bash -c launchctl kickstart -k gui/501/com.trident.engine")
    assert ra.match_line("launchctl bootout gui/501/com.trident.engine")
    assert ra.match_line("/bin/bash /Users/claire/.trident/wrappers/restart_backend.sh")
    assert ra.match_line("kill -TERM 12345  # engine.py")


def test_match_ignores_plain_engine_and_readers():
    # 引擎自己、普通命令、以及审计脚本自身都不能算命中
    assert not ra.match_line("python3 engine.py")
    assert not ra.match_line("tail -f /Users/claire/.trident/log/engine.out.log")
    assert not ra.match_line("grep kickstart CHANGELOG.md")
    assert not ra.match_line("python3 backend/scripts/restart_audit.py")


def test_scan_once_collects_ancestry():
    hits = ra.scan_once(ra.parse_ps(_SAMPLE))
    pids = {h["pid"] for h in hits}
    assert pids == {"501", "900", "123"}
    hit900 = next(h for h in hits if h["pid"] == "900")
    # 父进程是 launchd（pid 1）时链条要断开，不能无限往上走
    assert hit900["ancestry"][0].startswith("900 ")
    assert hit900["ancestry"][-1] == "<launchd pid=1>"
    # 中间层（zsh）不参与 —— 900 的 ppid 是 501，不是 901
    assert "900 /bin/bash" in hit900["ancestry"][0]


def test_confirm_only_when_process_vanished():
    cand = {"900": {"pid": "900", "cmd": "launchctl kickstart"}}
    # 还活着 → 不落盘（可能是长驻 shell）
    assert ra.confirm_short_lived(cand, {"900", "123"}) == []
    assert "900" in cand
    # 消失了 → 确认是短命调用方
    out = ra.confirm_short_lived(cand, {"123"})
    assert [r["pid"] for r in out] == ["900"]
    assert cand == {}


def test_ancestry_stops_on_cycle():
    rows = ra.parse_ps(
        "  10  11   501  ??  a\n  11  10   501  ??  b\n")
    chain = ra.ancestry(rows, "10")
    assert len(chain) == 2  # a → b，然后成环即停，不会死循环


def test_match_rejects_long_heredoc_wrappers():
    """误报实测（20:11:25）：在 heredoc 里写文档时，命令行含 kickstart 字样，
    被当成调用方记了下来。长命令行必须直接排除。"""
    long_line = (
        '/bin/bash --noprofile --norc --noediting -c exec 9>/tmp/x.txt || exit "$?"'
        "export CLI_CLOUD_CONFIG_FILE='/Users/claire/Library/cloud_config.json'\\012"
        "export CUA_BASE_PACK_DIR='/Users/claire/Library/agent'\\012"
        "cd /Users/claire/Desktop/金融1.0 && cat > docs/v435.md <<'EOF'\\012"
        "…这里在写文档，正文里提到 launchctl kickstart -k gui/501/com.trident.engine…\\012EOF"
        " && python3 -m pytest tests/ -q && tail -50 ~/.trident/log/engine.out.log"
    )
    assert len(long_line) > 220
    assert not ra.match_line(long_line)
    # 真实的调用命令依旧要命中
    assert ra.match_line('bash -c "cd /Users/claire/Desktop/金融1.0 && launchctl '
                         'kickstart -k gui/501/com.trident.engine"')
