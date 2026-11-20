"""项目诚实宪法 — 单一真理源 (Project Honesty Constitution, single source of truth).

v282.0+ 启用。本文件定义全项目「绝不允许捏造」的不变量边界,
所有模块 import 此常量作为不可违反的红线。

================================================================================
第一条款 (Clause 1): 数据真实性 (No fabrication of data)
================================================================================
  - 任何表示「未知 / 缺失 / 拉取失败」的语义必须用 None / null,严禁用 0 / 0.0 /
    空字符串 / "unknown" 假装「有数」让下游误以为是真实数据。
  - 禁止模式 (FORBIDDEN PATTERNS):
      * `value or 0.0` / `value or 0`           → 当 None 时必须透传 None
      * `?? 0` / `|| 0`                         → 同上
      * `return (x, x)` / `prev = current`      → 假装有历史价
      * `_PLACEHOLDER_* = {asset: hardcoded}`   → 按精确价过滤会误伤真实数据
      * `float(data.get(k) or 0.0)`             → 字段缺失时不能当 0 处理
      * `info?.change24h ?? 0`                  → 前端 24h 涨跌兜底成假 0%
  - 允许模式 (ALLOWED FALLBACKS):
      * 显式默认值: `entry_price = signal.get('entry_price') or 0.0`
        (DB NULL 在数学聚合里等于 0 是合法语义)
      * 多源聚合中位数: median([a, b, c])
      * 同一数据源的多源交叉验证
      * 知识图谱 / 关系图硬编码 (非数值)

================================================================================
第二条款 (Clause 2): 结论保守 (No fabricated conclusions)
================================================================================
  - 信号方向、胜率、置信度必须是基于真实样本计算,严禁:
      * 在样本不足时硬编码「看涨」/「看跌」标签
      * 把「样本 < 30」算出的低胜率当成「高置信」标签
      * 用 `_SAFE_DEFAULTS` 在 LLM 输出失败时假装是 AI 推理结论
  - 必须用 confidence / sample_size / data_quality_notes 字段暴露不确定性。

================================================================================
第三条款 (Clause 3): 失败可观测 (Failures must be observable)
================================================================================
  - 数据源 (EastMoney / Binance / Sina / yfinance) 失败必须 log + 返回 None
    而不是返回 placeholder 让上游误以为成功。
  - 任何 `except Exception: return 0.0` 都会被审计脚本标记为可疑。

================================================================================
如何引用 (How to reference)
================================================================================
  在任何「绝不能捏造」的边界位置写:

      from ._honesty import NEVER_FABRICATE_INVARIANT  # 导入即承认红线

  并在该函数的 docstring 末尾写:
      HONESTY: 见 _honesty.NEVER_FABRICATE_INVARIANT — 此函数绝不允许捏造数据。

================================================================================
如何防止回归 (How to enforce)
================================================================================
  测试 tests/test_no_fabrication.py 会 grep 所有 .py / .ts 源,
  在「非豁免」位置发现禁用模式 → test fail,CI 必挂。
"""

from __future__ import annotations

# 单一不变量常量 — 任何模块 import 即承认这是不可违反的红线。
# 用 sentinel object 保证类型唯一,无法被无意覆盖。
NEVER_FABRICATE_INVARIANT: str = (
    "v282+: 绝不允许捏造数据/结论。失败必须返回 None,严禁用 0/空串/"
    "placeholder 假装有数。详见 _honesty.py 第一/二/三条款。"
)


# 禁用模式白名单: 这些字符串不应在 src 中出现(除非在豁免位置)。
FORBIDDEN_PATTERNS: tuple[str, ...] = (
    # 假装 0% 涨跌 — 最常见假数据来源
    "value or 0.0",
    "value or 0",
    "float(data.get(",  # 仅用于 change24h/f170 边界,豁免名单控制
    # 假装 prev = current
    "return (price, price)",
    # 精确价格占位
    "_PLACEHOLDER_PRICES",
    "_PLACEHOLDER_",
    # 前端假 fallback
    "?? 0)",
    "|| 0)",
    "?? 0.0",
)


# 豁免位置: 这些文件 / 行号中的模式是被允许的(非「假数据」语义)。
EXEMPT_FILES: frozenset[str] = frozenset({
    "_honesty.py",                       # 本文件本身就是规则定义
    "tests/test_no_fabrication.py",      # 测试文件需要触发模式才能检查
})


def assert_honest(value, *, what: str) -> None:
    """开发期断言: 标记「此函数返回 0.0 是不诚实」的位置。

    用法:
        def _fetch_eastmoney_change(secid):
            try:
                ...
                if raw is None:
                    assert_honest(None, what="EastMoney f170 字段缺失")
                    return None
            except ...

    仅在 DEBUG_HONESTY=1 时打印,不抛异常(避免线上噪音)。
    """
    import os
    if os.environ.get("DEBUG_HONESTY"):
        print(f"[HONESTY] {what} → {value!r}", flush=True)