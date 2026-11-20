# -*- coding: utf-8 -*-
"""v500: 人工调权重 —— 只允许乘法、必须留痕、可撤销。

验收 #3「信号权重人工可调」此前是 FAIL：没有写入口，`policy_overrides` 是孤儿表。
这里锁住四条契约：
  1. 生效的覆盖必须真的改变 signal_weight，且 explain 里能看出是人改的；
  2. 过期 / 已撤销 / 品种不匹配 → 不生效（绝不"悄悄一直压着"）；
  3. 系数被夹在 0.5~2.0，乘积被夹在 0~1（防手滑把权重压成 0 或撑爆）；
  4. 表不存在 / 任何异常 → 返回原值，不阻断决策。

只跑内存库，不连生产库、不连网。
"""
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src_python"))

from signals import signal_weight_evaluator as swe


def _db(with_table: bool = True) -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    if with_table:
        swe.ensure_override_table(c)
    return c


def _add(c, asset="BTC", action="", mult=1.5, reason="回撤期压低 BTC 信号",
         actor="claire", expires_at=None, active=1, revoked_at=None):
    now = int(time.time())
    cur = c.execute(
        """INSERT INTO signal_weight_overrides
           (asset, action, multiplier, reason, actor, created_at, expires_at,
            revoked_at, active)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (asset, action, mult, reason, actor, now, expires_at, revoked_at, active))
    c.commit()
    return int(cur.lastrowid)


def _res(weight=0.40):
    return {"signal_weight": weight, "weight_tier": "medium",
            "components": {"base": 0.9, "impact": 1.2},
            "explain": "base=0.90 * ... = 0.400", "cold_signal": False}


# ── 1. 生效时真的改变权重，并且留痕 ─────────────────────────────────────────
def test_override_changes_weight_and_explains():
    c = _db()
    oid = _add(c, mult=1.5)
    out = swe.apply_manual_override(c, _res(0.40), "BTC", "BUY")
    assert abs(out["signal_weight"] - 0.60) < 1e-9
    assert "manual_override ×1.50" in out["explain"]
    assert f"#{oid}" in out["explain"] and "claire" in out["explain"]
    assert out["components"]["manual"] == 1.5
    assert out["manual_override"]["id"] == oid


def test_tier_is_recomputed_after_override():
    c = _db()
    _add(c, mult=2.0)
    out = swe.apply_manual_override(c, _res(0.30), "BTC")
    assert out["weight_tier"] == swe._to_tier(out["signal_weight"])


# ── 2. 过期/撤销/不匹配不生效 ──────────────────────────────────────────────
def test_expired_override_is_ignored():
    c = _db()
    _add(c, mult=2.0, expires_at=int(time.time()) - 60)
    out = swe.apply_manual_override(c, _res(0.40), "BTC")
    assert out["signal_weight"] == 0.40
    assert "manual_override" not in out["explain"]


def test_revoked_override_is_ignored():
    c = _db()
    _add(c, mult=2.0, revoked_at=int(time.time()) - 60, active=0)
    out = swe.apply_manual_override(c, _res(0.40), "BTC")
    assert out["signal_weight"] == 0.40


def test_asset_mismatch_is_ignored():
    c = _db()
    _add(c, asset="XAU", mult=2.0)
    out = swe.apply_manual_override(c, _res(0.40), "BTC")
    assert out["signal_weight"] == 0.40


def test_global_override_applies_but_specific_wins():
    c = _db()
    _add(c, asset="", mult=1.2, reason="全局保守")
    _add(c, asset="BTC", mult=1.8, reason="BTC 单独加压")
    out = swe.apply_manual_override(c, _res(0.40), "BTC")
    assert abs(out["signal_weight"] - 0.72) < 1e-9      # 1.8 而不是 1.2
    out2 = swe.apply_manual_override(c, _res(0.40), "ETH")
    assert abs(out2["signal_weight"] - 0.48) < 1e-9    # 全局 1.2


def test_action_scope():
    c = _db()
    _add(c, action="BUY", mult=2.0)
    sell = swe.apply_manual_override(c, _res(0.40), "BTC", "SELL")
    assert sell["signal_weight"] == 0.40
    buy = swe.apply_manual_override(c, _res(0.40), "BTC", "BUY")
    assert abs(buy["signal_weight"] - 0.80) < 1e-9


# ── 3. 夹取 ────────────────────────────────────────────────────────────────
def test_multiplier_is_clamped_and_weight_capped():
    c = _db()
    _add(c, mult=5.0)          # 库里被手写成 5（绕过 API），读侧仍夹到 2.0
    out = swe.apply_manual_override(c, _res(0.90), "BTC")
    assert abs(out["signal_weight"] - 1.0) < 1e-9      # 0.9*2=1.8 → 夹到 1.0
    assert out["components"]["manual"] == 2.0


def test_zero_weight_stays_zero():
    c = _db()
    _add(c, mult=2.0)
    out = swe.apply_manual_override(c, _res(0.0), "BTC")
    assert out["signal_weight"] == 0.0


# ── 4. 失败安全 ────────────────────────────────────────────────────────────
def test_no_table_is_noop():
    c = _db(with_table=False)
    out = swe.apply_manual_override(c, _res(0.40), "BTC")
    assert out["signal_weight"] == 0.40
    assert swe.get_active_override(c, "BTC") is None


def test_get_active_override_returns_newest():
    c = _db()
    _add(c, mult=1.2)
    newer = _add(c, mult=1.6)
    ov = swe.get_active_override(c, "BTC")
    assert ov["id"] == newer


# ── 5. 入口存在（验收 #3 的静态判据） ─────────────────────────────────────
def test_router_declares_override_endpoints():
    import capability_acceptance as acc
    has, detail = acc._manual_weight_entry()
    assert has, detail
    assert "signal-weights/override" in detail
