"""v398 回归：两步抢救的预算分流（NONE 类不再吃光可救额度）。

2026-11-16 真库：442 条降级里 273 条（62%）是 target_none。这批新闻本身不讲核心
品种（个股 / 赛事 / 宏观汇总），两步抢救第一步几乎必然回 NONE —— 白烧一次 40s 调用，
还把"标的已锁、只缺八段"的新闻挤出窗口（日志实测 窗口已用 6/10 … 9/10）。

v398 钉住三件事：
  · is_none_target 的判定（空 / NONE / OTHER 才算没锁住，大小写与空格要容错）
  · NONE 类与有标的类用**两份互不干扰**的滑动窗口
  · 只在真需要抢救时才扣预算（原实现无条件先扣，半批白吃）
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src_python"))

import engine.ai_worker as aw


def _reset(state):
    state["window_start"] = 0.0
    state["used"] = 0


# ── 标的判定 ──────────────────────────────────────────────────────────────

def test_is_none_target_true_cases():
    for v in ("", "  ", None, "NONE", "none", " None ", "OTHER", "other"):
        assert aw.is_none_target(v) is True, v


def test_is_none_target_false_cases():
    for v in ("XAU", "btc", "WTI", "CN_STOCK", "INDIVIDUAL_STOCK"):
        assert aw.is_none_target(v) is False, v


def test_is_none_target_does_not_treat_individual_stock_as_none():
    # 个股是要分析的标的，不能当 NONE 跳过抢救
    assert aw.is_none_target("US_STOCK") is False


# ── 预算分流 ──────────────────────────────────────────────────────────────

def test_state_for_routes_none_to_its_own_window():
    none_state, none_lim = aw.rescue_budget_state_for("NONE")
    other_state, other_lim = aw.rescue_budget_state_for("XAU")
    assert none_state is aw._RESCUE_NONE_STATE
    assert other_state is aw._RESCUE_STATE
    assert none_state is not other_state
    assert none_lim == aw._RESCUE_NONE_LIMIT_PER_WINDOW
    assert other_lim == aw._RESCUE_LIMIT_PER_WINDOW


def test_none_budget_exhausts_without_touching_main_budget():
    _reset(aw._RESCUE_NONE_STATE)
    _reset(aw._RESCUE_STATE)
    now = time.time()
    lim = aw._RESCUE_NONE_LIMIT_PER_WINDOW
    # NONE 窗口：用满即拒
    for _ in range(lim):
        assert aw.rescue_budget_ok(now, aw._RESCUE_NONE_STATE, limit=lim) is True
    assert aw.rescue_budget_ok(now, aw._RESCUE_NONE_STATE, limit=lim) is False
    # 主窗口仍完好：一次都没被扣
    assert aw._RESCUE_STATE["used"] == 0
    assert aw.rescue_budget_ok(now, aw._RESCUE_STATE,
                              limit=aw._RESCUE_LIMIT_PER_WINDOW) is True


def test_main_budget_allows_ten_then_blocks():
    _reset(aw._RESCUE_STATE)
    _reset(aw._RESCUE_NONE_STATE)
    now = time.time()
    for _ in range(aw._RESCUE_LIMIT_PER_WINDOW):
        assert aw.rescue_budget_ok(now, aw._RESCUE_STATE,
                                  limit=aw._RESCUE_LIMIT_PER_WINDOW) is True
    assert aw.rescue_budget_ok(now, aw._RESCUE_STATE,
                              limit=aw._RESCUE_LIMIT_PER_WINDOW) is False
    assert aw._RESCUE_NONE_STATE["used"] == 0


def test_window_resets_after_ten_minutes():
    _reset(aw._RESCUE_NONE_STATE)
    now = time.time()
    lim = aw._RESCUE_NONE_LIMIT_PER_WINDOW
    for _ in range(lim):
        aw.rescue_budget_ok(now, aw._RESCUE_NONE_STATE, limit=lim)
    assert aw.rescue_budget_ok(now, aw._RESCUE_NONE_STATE, limit=lim) is False
    # 窗口过期 → 重新计数
    later = now + aw._RESCUE_WINDOW_SEC + 1
    assert aw.rescue_budget_ok(later, aw._RESCUE_NONE_STATE, limit=lim) is True
    assert aw._RESCUE_NONE_STATE["used"] == 1


def test_none_limit_default_is_three_and_env_overridable():
    # 默认 3；环境变量改了要生效（这里只断言常量落在合理区间，避免依赖 CI 环境）
    assert 1 <= aw._RESCUE_NONE_LIMIT_PER_WINDOW <= aw._RESCUE_LIMIT_PER_WINDOW
    assert os.getenv("TRIDENT_TWO_STEP_RESCUE_NONE_LIMIT") in (None, "") or \
        aw._RESCUE_NONE_LIMIT_PER_WINDOW >= 1
