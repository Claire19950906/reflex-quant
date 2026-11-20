# -*- coding: utf-8 -*-
"""v322: 把两类「静默失效」审计做成**常驻门禁**。

背景（2026-11-07 生产事故）
--------------------------
「待分析」4851 条两天不降，根因是：

  raw_news.status 的 CHECK 不允许 'FILTERED' → 每条归档 UPDATE 被数据库拒绝
  → ai_worker._expire_stale_news 里的 `except Exception: return 0` 把异常吞掉
  → 库里 0 条 FILTERED、日志 0 行 → **排查两天才发现是数据库约束**

两个可复用的教训：
  1. **静默吞异常 = 功能静默死亡**，必须全局清零
  2. **代码写入的值必须与数据库 CHECK 约束一致**，静态可查

本文件把两个审计脚本接进测试：以后有人新写一个 `except: pass`
或写了一个 CHECK 不允许的状态值，**测试直接失败**，不会等到生产上才发现。

注意：审计脚本扫描的是 src_python/ 源码，跑一次约十几秒，
所以这里只跑「静默吞异常」这一个（纯 AST，快），
status/CHECK 那个需要连库，放在 scripts/ 里由 doctor.py 统一调用。
"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(BASE_DIR, "scripts")
PY = sys.executable


def _run(script: str, timeout: int = 300):
    return subprocess.run(
        [PY, os.path.join(SCRIPTS, script)],
        capture_output=True, text=True, timeout=timeout, cwd=BASE_DIR)


def test_no_silent_except_on_write_paths():
    """写库/外部调用路径上不得有静默吞异常。"""
    r = _run("audit_silent_except.py")
    assert r.returncode == 0, (
        "检测到「写路径上静默吞异常」——失败不留痕 = 功能静默死亡：\n"
        + (r.stdout or "")[-4000:] + (r.stderr or "")[-2000:])


def test_audit_script_detects_synthetic_silent_except(tmp_path):
    """自检：审计脚本本身要能抓到人造的静默吞异常（防审计脚本失效）。"""
    import ast
    if SCRIPTS not in sys.path:
        sys.path.insert(0, SCRIPTS)
    import audit_silent_except as aud

    src = tmp_path / "bad_mod.py"
    src.write_text(
        "def f(conn):\n"
        "    try:\n"
        "        conn.execute(\"UPDATE t SET a='b'\")\n"
        "    except Exception:\n"
        "        pass\n",
        encoding="utf-8")
    v = aud.Visitor(str(src), "bad_mod.py")
    import ast
    v.visit(ast.parse(src.read_text(encoding="utf-8")))
    assert len(v.hits) == 1, "审计脚本必须能抓到人造的静默吞异常"

    ok = tmp_path / "good_mod.py"
    ok.write_text(
        "def f(conn):\n"
        "    try:\n"
        "        conn.execute(\"UPDATE t SET a='b'\")\n"
        "    except Exception as e:\n"
        "        print('failed', e)\n",
        encoding="utf-8")
    v2 = aud.Visitor(str(ok), "good_mod.py")
    v2.visit(ast.parse(ok.read_text(encoding="utf-8")))
    assert len(v2.hits) == 0, "打了日志的 except 不应被判定为问题"


def test_raw_news_status_allows_filtered():
    """raw_news.status 的 CHECK 必须允许 'FILTERED'（归档终态）。"""
    import sys as _sys
    src = _sys.path
    if os.path.join(BASE_DIR, "src_python") not in src:
        _sys.path.insert(0, os.path.join(BASE_DIR, "src_python"))
    import db
    assert "'FILTERED'" in db._CREATE_RAW_NEWS, (
        "db._CREATE_RAW_NEWS 必须允许 status='FILTERED'，"
        "否则归档 UPDATE 会被数据库拒绝 → 「待分析」永不下降")


def test_event_status_knows_filtered():
    """event_status 的合法状态集合必须认得 FILTERED。"""
    import sys as _sys
    if os.path.join(BASE_DIR, "src_python") not in _sys.path:
        _sys.path.insert(0, os.path.join(BASE_DIR, "src_python"))
    import event_status
    assert "FILTERED" in event_status.VALID_STATUSES
    assert "FILTERED" in event_status.TERMINAL_STATUSES
    assert event_status.STATUS_LABELS.get("FILTERED")
    assert event_status.STATUS_REASONS.get("FILTERED")
