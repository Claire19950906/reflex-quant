#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v536：README 首屏那组数字，必须能从仓库里的库跑出来。

背景：README 曾经写过「735 次评估 / 拦下 136 / 87.2%」，指的是真库的历史快照。
那次库被清空重建后，那条 SQL 跑出来是 0 —— 也就是说首屏最显眼的数字**不可复现**。
改成 `demo.db` 的 96/128（75%）之后，加这条测试把口径钉住：
  ① 首屏表格的四个数，必须等于 demo.db 里真实查出来的四个数；
  ② 徽章必须和表格同一个口径（96/128 · 75%），不许再挂 87.2%；
  ③ README 里给的那条 `sqlite3` 命令必须指向 demo.db，不许指向会返回 0 的真库。
demo.db 若被重新生成、数字变了，这条测试会先红 —— 逼着 README 跟着改，
而不是等访客把命令粘过去、发现跑出 0。
"""
import re
import sqlite3
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve()
BACKEND = TESTS.parents[1]
ROOT = TESTS.parents[2]
DEMO_DB = BACKEND / "demo.db"
READMES = [ROOT / "README.md", ROOT / "README.en.md"]
HEADER_PAT = re.compile(r"\|\s*(?:闸门评估|Gate evaluations)\s*\|")
ROW_PAT = re.compile(r"\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*\*\*(\d+)\s*·\s*(\d+)%\*\*")


def _gate_counts():
    """(总数, 方向性, 放行, 拦下)，直接问 demo.db。"""
    if not DEMO_DB.exists():
        pytest.skip("demo.db 不在仓库里，README 数字无从复现")
    with sqlite3.connect(f"file:{DEMO_DB}?mode=ro", uri=True) as conn:
        total = conn.execute("SELECT COUNT(*) FROM gate_evaluations").fetchone()[0]
        directional = conn.execute(
            "SELECT COUNT(*) FROM gate_evaluations WHERE action IN ('BUY','SELL')"
        ).fetchone()[0]
        passed = conn.execute(
            "SELECT COUNT(*) FROM gate_evaluations WHERE action IN ('BUY','SELL') AND passed=1"
        ).fetchone()[0]
        blocked = conn.execute(
            "SELECT COUNT(*) FROM gate_evaluations WHERE action IN ('BUY','SELL') AND passed=0"
        ).fetchone()[0]
    return total, directional, passed, blocked


def _readme_table_numbers(text, name):
    lines = text.splitlines()
    idx = next((i for i, ln in enumerate(lines) if HEADER_PAT.search(ln)), None)
    assert idx is not None, f"{name} 找不到首屏「闸门评估」表头"
    for ln in lines[idx + 1 : idx + 6]:
        m = ROW_PAT.search(ln)
        if m:
            return tuple(int(g) for g in m.groups())  # 5 个数：4 个计数 + 百分比
    pytest.fail(f"{name} 的表头下面没找到『192 | 128 | 32 | **96 · 75%**』这种行")


@pytest.mark.parametrize("readme", READMES, ids=lambda p: p.name)
def test_readme_table_matches_demo_db(readme):
    """首屏表格 = demo.db 的实查结果，一位不差。"""
    assert readme.exists(), f"{readme.name} 不存在"
    total, directional, passed, blocked = _gate_counts()
    expected = (
        total,
        directional,
        passed,
        blocked,
        round(blocked / directional * 100) if directional else 0,
    )
    got = _readme_table_numbers(readme.read_text(encoding="utf-8"), readme.name)
    assert got == expected, f"{readme.name} 首屏表格 {got} ≠ demo.db 实查 {expected}"


@pytest.mark.parametrize("readme", READMES, ids=lambda p: p.name)
def test_badge_agrees_with_table(readme):
    """徽章不能和表格两套口径。"""
    text = readme.read_text(encoding="utf-8")
    _, directional, _, blocked = _gate_counts()
    pct = round(blocked / directional * 100)
    badge = f"blocked-{blocked}%2F{directional}%20%C2%B7%20{pct}%25"
    assert badge in text, f"{readme.name} 徽章应与表格同口径：{badge}"
    assert "blocked-87.2%25" not in text, f"{readme.name} 还挂着不可复现的 87.2% 徽章"


@pytest.mark.parametrize("readme", READMES, ids=lambda p: p.name)
def test_reproduction_command_targets_demo_db(readme):
    """README 给的复现命令必须指向 demo.db，指向真库只会查出 0。"""
    text = readme.read_text(encoding="utf-8")
    assert "sqlite3 backend/demo.db" in text, f"{readme.name} 缺少指向 demo.db 的复现命令"
    assert "sqlite3 backend/trident_event_bus.db" not in text, (
        f"{readme.name} 仍在用真库当复现入口（该库关键表为空，跑出来是 0）"
    )


def test_demo_db_numbers_are_the_headline_ones():
    """单独把那一组数字钉一次，防止 demo.db 被换掉后静默漂移。"""
    total, directional, passed, blocked = _gate_counts()
    assert (total, directional, passed, blocked) == (192, 128, 32, 96)
    assert passed + blocked == directional
    assert round(blocked / directional * 100) == 75
