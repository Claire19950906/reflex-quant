"""v480 数据完整性护栏：成交量缺失时量价指标必须「诚实不可用」，不许编数。

为什么需要这组测试：
  实测 XAU 日线 2623 根里只有 120 根有 volume（95.4% 为空）、HG 100% 为空。
  旧版 `volumes()` 用 `_clean` 压缩空值，返回的序列长度和 K 线对不上：
    - OBV 按原长度索引 → 直接 IndexError；
    - MFI/VWAP/vol_zscore 却会用稀疏点算出「看起来正常」的数
      （XAU 实测 MFI=24.14，下游会读成「超卖」）。
  这组测试锁死：覆盖不足 → 一律 None / 明确标注不可用；覆盖充足 → 行为不变。
"""
from __future__ import annotations

import os
import sys

import pytest

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BACKEND, "src_python"))

import technical_indicators as ti


def _c(i: int, close: float, vol=None) -> list:
    """一根 K 线：[ts, open, high, low, close, volume]。"""
    return [1700000000 + i * 86400, close - 1, close + 1, close - 2, close, vol]


def _series(n: int = 60, with_vol: bool = True) -> list:
    return [_c(i, 100 + i * 0.5, (1000 + i) if with_vol else None) for i in range(n)]


# ── 体检门本身 ──────────────────────────────────────────────────────────────

def test_availability_full():
    va = ti.volume_availability(_series(60, True))
    assert va["available"] is True
    assert va["coverage_pct"] == 100.0
    assert va["n_valid"] == 60
    assert va["note"] is None


def test_availability_empty():
    va = ti.volume_availability(_series(60, False))
    assert va["available"] is False
    assert va["coverage_pct"] == 0.0
    assert "量价类指标" in va["note"]


def test_availability_partial_below_threshold():
    """模拟 XAU：60 根里只有 2 根有量（3.3%）。"""
    c = _series(60, False)
    c[-1][ti.VOL] = 1000
    c[-2][ti.VOL] = 1001
    va = ti.volume_availability(c)
    assert va["available"] is False
    assert va["coverage_pct"] == pytest.approx(3.3, abs=0.1)


def test_availability_threshold_is_80pct():
    c = _series(100, False)
    for i in range(80):                      # 恰好 80%
        c[i][ti.VOL] = 1000 + i
    assert ti.volume_availability(c)["available"] is True
    c[79][ti.VOL] = None                     # 掉到 79%
    assert ti.volume_availability(c)["available"] is False


# ── OBV：以前会 IndexError ─────────────────────────────────────────────────

def test_obv_does_not_crash_with_sparse_volume():
    c = _series(60, False)
    c[-1][ti.VOL] = 1000                     # 只有最后一根有量
    out = ti.obv(c)                          # 旧版这里 IndexError
    assert len(out) == len(c)
    assert all(x is None for x in out)


def test_obv_no_crash_with_zero_volume():
    assert all(x is None for x in ti.obv(_series(40, False)))


def test_obv_unchanged_when_volume_full():
    """回归：全有量时行为必须与旧版一致（涨加量、跌减量从 0 起累加）。"""
    c = _series(30, True)
    out = ti.obv(c)
    cur = 0.0
    for i in range(1, len(c)):
        if c[i][ti.CLOSE] > c[i - 1][ti.CLOSE]:
            cur += c[i][ti.VOL]
        elif c[i][ti.CLOSE] < c[i - 1][ti.CLOSE]:
            cur -= c[i][ti.VOL]
    assert out[-1] == pytest.approx(cur)


# ── MFI / VWAP：以前会用稀疏点算出「看着正常」的错数 ────────────────────────

def test_mfi_none_when_volume_missing():
    out = ti.mfi(_series(60, False))
    assert len(out) == 60
    assert all(v is None for v in out)


def test_mfi_unchanged_when_volume_full():
    out = ti.mfi(_series(60, True))
    assert any(v is not None for v in out)
    assert 0.0 <= out[-1] <= 100.0


def test_vwap_none_when_volume_missing():
    out = ti.vwap(_series(60, False))
    assert len(out) == 60
    assert all(v is None for v in out)


def test_vwap_unchanged_when_volume_full():
    out = ti.vwap(_series(60, True))
    assert out[-1] is not None


# ── 量能 Z / 突破 / 量价关系 ────────────────────────────────────────────────

def test_vol_zscore_none_when_volume_missing():
    assert ti.vol_zscore(_series(60, False)) is None


def test_vol_zscore_works_when_volume_full():
    assert ti.vol_zscore(_series(60, True)) is not None


def test_vol_break_none_when_volume_missing():
    assert ti.vol_break(_series(60, False)) is None


def test_volume_regime_marks_unavailable():
    r = ti.volume_regime(_series(60, False))
    assert r["available"] is False
    assert r["regime"] == "成交量不可用"
    assert r["coverage_pct"] == 0.0
    assert r["vol_ratio"] is None


def test_volume_regime_available_when_volume_full():
    r = ti.volume_regime(_series(60, True))
    assert r["available"] is True
    assert r["vol_ratio"] is not None


# ── 快照必须带出体检结论 ───────────────────────────────────────────────────

def test_snapshot_exposes_volume_availability():
    s = ti.snapshot(_series(60, False))
    assert "volume_availability" in s
    assert s["volume_availability"]["available"] is False
    assert s["volume_regime"]["available"] is False


# ── 库路径唯一来源（防止再有人硬编码生产库） ────────────────────────────────

def test_db_path_single_source():
    import db_location
    import bets
    import divergence_ledger
    import routers.divergence as div_router

    from pathlib import Path
    for mod in (bets, divergence_ledger, div_router):
        # v487 修：不同模块把同一个路径存成 str / PosixPath 两种形态，
        # 直接 `==` 会在 str vs Path 上假红。统一 resolve 成绝对路径再比。
        assert Path(mod.DB_PATH).resolve() == Path(db_location.DB_PATH).resolve()
    # pytest 下 conftest 用 TRIDENT_DB_PATH 指向测试库，三个模块必须跟着走。
    # 若哪一天又把路径硬编码回生产库 trident_event_bus.db，这条会立刻红 —— 那就是
    # v483 记录的「pytest 把生产库当测试库、截断 ai_decisions」事故复发。
    # v487：判据从"文件名里含 test"改成"不等于生产库绝对路径"。
    # 原因：db_location 在 import 时固化路径，而 conftest 早期临时库名就叫
    # trident_event_bus.db（两个 tempdir 阶段的产物），全量跑时模块被早导入就会
    # 误判；真正要守的边界只有一条 —— 绝不能是生产库。
    from pathlib import Path
    _prod = (Path(__file__).resolve().parent.parent / "trident_event_bus.db").resolve()
    assert Path(db_location.DB_PATH).resolve() != _prod, (
        f"测试环境连到了生产库 {db_location.DB_PATH}")


# ── 管线级跳过（v486）：无量时不白算，直接给跳过原因 ──────────────────────

def test_pipeline_skips_volume_indicators_when_no_volume():
    p = ti.IndicatorPipeline()
    for name in ("volume_regime", "obv", "mfi", "rsi"):
        p.add(name)
    r = p.run(_series(60, False))
    for name in ("volume_regime", "obv", "mfi"):
        assert isinstance(r[name], dict)
        assert r[name]["skipped"] == "no_volume"
        assert r[name]["coverage_pct"] == 0.0
        assert "note" in r[name]
    assert isinstance(r["rsi"], list)          # 价格类照常


def test_pipeline_keeps_volume_profile_on_tpo():
    """volume_profile 有 TPO 兜底，无量时不能被跳过。"""
    p = ti.IndicatorPipeline().add("volume_profile")
    r = p.run(_series(60, False))["volume_profile"]
    assert r.get("skipped") is None
    assert r["weight"] == "tpo"


def test_pipeline_normal_when_volume_full():
    p = ti.IndicatorPipeline().add("obv").add("volume_regime")
    r = p.run(_series(60, True))
    assert isinstance(r["obv"], list)
    assert r["volume_regime"]["available"] is True


# ── 空值与零值必须分开报（零比空更隐蔽） ────────────────────────────────────

def test_availability_separates_null_and_zero():
    c = _series(60, False)
    for i in range(10):                        # 10 行 volume=0（被写成零，不是 NULL）
        c[i][ti.VOL] = 0
    va = ti.volume_availability(c)
    assert va["n_null"] == 50
    assert va["n_zero"] == 10
    assert va["available"] is False
