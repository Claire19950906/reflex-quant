"""反思校验器 hook 真的接上了吗 —— 锁住 `_rc` 未定义那个 bug。

背景（2026-11-18）：
  review_agent._aggregate() 里有个 "反思系统 hook"，对每条 trade 跑程序化校验器
  并把 error 落库。它拼 checker_dec 时调用了 `_rc.synthesize_cross_asset_likely(...)`，
  但 `_rc` 在整个模块里从来没绑定过 —— 第一条 trade 就 NameError，被外层
  `except Exception` 吞成 `agg["_reflection_error"] = "name '_rc' is not defined"`，
  于是 `_reflection_errors_inserted` 恒为 0，这条链路**静默空转**。

  这种 bug 的特点是「不报错、不崩、只是没数据」，所以必须有断言盯着：
  只要 _aggregate 返回的 agg 里出现 `_reflection_error`，就说明 hook 又没跑起来。
"""
from __future__ import annotations

import review.review_agent as ra


def _trade(i: int, **kw) -> dict:
    base = {
        "id": i,
        "is_correct": "WIN",
        "action": "BUY",
        "sentiment_score": 0.5,
        "confidence": 0.6,
        "asset": "BTC",
        "forward_pnl": 0.02,
    }
    base.update(kw)
    return base


def test_aggregate_reflection_hook_has_no_name_error():
    """核心断言：hook 不能带着 NameError 静默退出。"""
    agg = ra._aggregate([_trade(1), _trade(2)])
    assert "_reflection_error" not in agg, (
        "反思 hook 抛异常被吞了：" + str(agg.get("_reflection_error"))
    )


def test_aggregate_reflection_hook_actually_writes(monkeypatch):
    """校验器要真被调用到（不依赖库里有没有 decision_errors 表）。

    把 run_all_checkers / save_errors 换成桩：只要 hook 还活着，桩就会被调用，
    返回值也会进 agg。这样断言的是"链路通"，不是"表建好了没"。
    """
    from review import reflection_checkers as rc

    seen = {"checker_dec": None, "conn": None}

    def fake_run_all(checker_dec, conn=None):
        seen["checker_dec"] = checker_dec
        seen["conn"] = conn
        return [{"code": "E_TEST", "detail": "stub"}]

    def fake_save(errs, conn=None):
        return len(errs)

    monkeypatch.setattr(rc, "run_all_checkers", fake_run_all, raising=True)
    monkeypatch.setattr(rc, "save_errors", fake_save, raising=True)

    agg = ra._aggregate([_trade(3)])
    assert agg.get("_reflection_errors_inserted") == 1
    assert "_reflection_error" not in agg
    # 传进去的 checker_dec 必须是补齐过的（含 cross_asset_likely 这些字段）
    dec = seen["checker_dec"] or {}
    assert dec.get("id") == 3
    assert "cross_asset_likely" in dec


def test_module_exposes_reflection_checkers_alias():
    """_rc 得能取到 synthesize_cross_asset_likely，否则 checker_dec 又拼不出来。"""
    from review import reflection_checkers as _rc

    assert callable(_rc.synthesize_cross_asset_likely)
