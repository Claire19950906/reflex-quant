# -*- coding: utf-8 -*-
"""
market_classifier.py
====================

**单一真理源（Single Source of Truth）** — 所有 market_category / target_asset
分类逻辑都收敛到这里，其它模块禁止重复定义白名单。

设计原则（与你的重构指令一一对应）：
  1. **堵死 OTHER 生成路径**：生产白名单 MARKET_CATEGORIES_PROD 不含 "OTHER"。
     `OTHER` 仅作为 debug 枚举值存在，受 `DEBUG_ALLOW_OTHER` env 控制；
     调试环境的目的是"找出我们漏写了哪些资产/品类"，而不是"批量刷 OTHER"。
  2. **未匹配 → 人工复核，不静默兜底**：所有推断失败都抛 `UnclassifiedError`，
     携带 `reason` / `target_asset` / `llm_market_category` / `candidates` 四要素，
     由调用方决定走 `unclassified_events` 表（人工/规则补全）还是 quarantined。
  3. **移除生产库的 OTHER 标签**：存量数据迁移脚本 `migrate_remove_other.py`
     已配套提供。本模块对历史脏数据**不提供隐式兼容路径**（不把 OTHER → MACRO），
     迁移由脚本一次性完成。

数据流（每一层都调用本模块的 infer/validate/map，禁止重复定义白名单）：
  LLM raw output
    → ai_worker._auto_fill_required_fields (Layer A)
    → ai_worker._sanitize step 12 (Layer B)
    → ai_worker._sanitize step 5 (Layer C)
    → ai_worker._infer_sibling_from_reasoning (Layer D)
    → ai_worker reasoning_path 模板 (Layer E)
    → ai_decisions 落库
    → api_server._row_to_event (Layer F, 前端渲染)

变更规范（必须遵守）：
  - 新增 target_asset 必须**同时**写：
    1. `TARGET_TO_MARKET` 字典（决定品类）
    2. ai_worker._ASSET_ALIASES（决定文本/代码识别）
    3. llm_schema.py schema enum（决定 LLM 能否合法输出）
    4. prompt 中的"## 3. 资产归类"段落（决定 LLM 何时调用）
  - 任何一处漏写都会触发 `UnclassifiedError` 进入 `unclassified_events`。
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional, Tuple

# ──────────────────────────────────────────────────────────────────────────
# 1. 白名单：生产环境永远不含 OTHER
# ──────────────────────────────────────────────────────────────────────────
MARKET_CATEGORIES_PROD: Tuple[str, ...] = (
    "CRYPTO",     # 加密货币主流币（BTC/ETH/SOL）
    "GOLD",       # 黄金（XAU）
    "OIL",        # 原油（WTI/BRENT）
    "STOCK",      # 股票聚合 / 个股 / A股 / 美股 / 港股 / 韩股
    "MACRO",      # 外汇 / 国债 / 利率 / CPI / 央行决议（无具体可交易标的的宏观面）
    "COMMODITY",  # 大宗商品（白银/铜/工业金属等非黄金非原油）
)

# 调试白名单：仅当 env DEBUG_ALLOW_OTHER=true 时才含 OTHER。
# 调用方应通过 `get_market_categories()` 取当前生效集合，不要直接引用此常量。
MARKET_CATEGORIES_DEBUG: Tuple[str, ...] = (*MARKET_CATEGORIES_PROD, "OTHER")


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes", "on")


def is_debug_other_allowed() -> bool:
    """是否启用调试模式（允许 OTHER 枚举值）。默认 False。"""
    return _env_flag("DEBUG_ALLOW_OTHER")


def get_market_categories() -> Tuple[str, ...]:
    """当前进程生效的 market_category 白名单。"""
    return MARKET_CATEGORIES_DEBUG if is_debug_other_allowed() else MARKET_CATEGORIES_PROD


# ──────────────────────────────────────────────────────────────────────────
# 2. target_asset → market_category 映射（单一真理源）
# ──────────────────────────────────────────────────────────────────────────
TARGET_TO_MARKET: Dict[str, str] = {
    # ── 加密主流币 ──
    "BTC":              "CRYPTO",
    "ETH":              "CRYPTO",
    "SOL":              "CRYPTO",
    # ── 黄金 / 白银 / 铜 ──
    "XAU":              "GOLD",
    "XAG":              "COMMODITY",
    "COPPER":           "COMMODITY",
    # ── 原油 ──
    "WTI":              "OIL",
    "BRENT":            "OIL",
    # ── 股票聚合 ──
    "CN_STOCK":         "STOCK",
    "US_STOCK":         "STOCK",
    "HK_STOCK":         "STOCK",
    "INDIVIDUAL_STOCK": "STOCK",
    # ── 个股 ──
    "SKHYNIX":          "STOCK",
    "NVDA":             "STOCK",
    "TSLA":             "STOCK",
    "AAPL":             "STOCK",
    "MSFT":             "STOCK",
    "AMZN":             "STOCK",
    "GOOGL":            "STOCK",
    "META":             "STOCK",
    "COIN":             "STOCK",
    "PLTR":             "STOCK",
    "MSTR":             "STOCK",
    "SMCI":             "STOCK",
    "MARA":             "STOCK",
    "RIOT":             "STOCK",
    # v211 P1-C：扩半导体个股 ticker（与海力士新闻常被同时提及）
    "MU":               "STOCK",  # 美光（DRAM/HBM 同行）
    "INTC":             "STOCK",  # 英特尔
    "TSMC":             "STOCK",  # 台积电
    "SAMSUNG":          "STOCK",  # 三星电子（韩股 005930，与 SKHYNIX 同 DRAM）
    # ── 外汇 / 国债（无具体可交易标的，归 MACRO）──
    "CNY":              "MACRO",
    "USD":              "MACRO",
    "EUR":              "MACRO",
    "JPY":              "MACRO",
    "US_BOND":          "MACRO",
    "CN_BOND":          "MACRO",
}


# ──────────────────────────────────────────────────────────────────────────
# 3. UnclassifiedError（人工复核触发器）
# ──────────────────────────────────────────────────────────────────────────
class UnclassifiedError(Exception):
    """无法归类时抛出的异常。

    调用方应根据 `reason` 字段决定：
      - `MISSING_TARGET_ASSET` / `UNKNOWN_TARGET_ASSET:*` /
        `NO_MAPPING_FOR_ASSET:*` → 写 `unclassified_events` 表，等待人工/规则补全
      - `OTHER_NOT_ALLOWED_IN_PROD` → 写 `unclassified_events` + 告警（说明 LLM 仍在用旧 enum）
      - `INVALID_MCATEGORY:*` / `CAT_MISMATCH:*` → 写 `unclassified_events` + 告警

    **禁止**调用方 `except: pass` 或 fallback 到某个非 PROD 白名单值。
    """

    def __init__(
        self,
        reason: str,
        target_asset: str = "",
        llm_market_category: str = "",
        candidates: Optional[Dict[str, Any]] = None,
        news_id: int = 0,
        layer: str = "",
    ):
        self.reason = reason
        self.target_asset = (target_asset or "").strip().upper()
        self.llm_market_category = (llm_market_category or "").strip().upper()
        self.candidates = candidates or {}
        self.news_id = news_id
        self.layer = layer  # 来自 ai_worker 的哪一层
        super().__init__(
            f"[{layer or '?'}] {reason} | asset={self.target_asset!r} "
            f"llm_cat={self.llm_market_category!r} news_id={news_id}"
        )


# ──────────────────────────────────────────────────────────────────────────
# 4. 核心 API
# ──────────────────────────────────────────────────────────────────────────
def map_target(target_asset: str) -> Optional[str]:
    """根据 target_asset 推断 market_category；不识别 → 返回 None（不是 OTHER）。"""
    if not target_asset:
        return None
    return TARGET_TO_MARKET.get(target_asset.strip().upper())


def validate_pair(target_asset: str, market_category: str) -> Tuple[str, str]:
    """验证 (target_asset, market_category) 二元组合法性。

    失败抛 UnclassifiedError，**不静默兜底**。
    """
    cat = (market_category or "").strip().upper()
    asset = (target_asset or "").strip().upper()
    allowed = get_market_categories()

    if cat == "OTHER":
        if is_debug_other_allowed():
            if not asset or asset == "NONE":
                raise UnclassifiedError(
                    reason="OTHER_WITH_EMPTY_ASSET",
                    target_asset=asset,
                    llm_market_category=cat,
                )
            return asset, cat
        raise UnclassifiedError(
            reason="OTHER_NOT_ALLOWED_IN_PROD",
            target_asset=asset,
            llm_market_category=cat,
        )

    if cat not in allowed:
        raise UnclassifiedError(
            reason=f"INVALID_MCATEGORY:{cat}",
            target_asset=asset,
            llm_market_category=cat,
        )

    if not asset or asset == "NONE":
        # 唯一合法的"无元配对"组合：(NONE, MACRO)
        # 用于"央行决议/CPI/会议无明确可交易标的但有宏观影响"的情况。
        # 其它 cat 配 NONE → 算非法（CRYPTO/CRYPTO+OLD 等需要具体资产）。
        if cat == "MACRO":
            return asset, cat
        raise UnclassifiedError(
            reason="MISSING_TARGET_ASSET",
            target_asset=asset,
            llm_market_category=cat,
        )

    expected_cat = map_target(asset)
    if expected_cat is None:
        raise UnclassifiedError(
            reason=f"UNKNOWN_TARGET_ASSET:{asset}",
            target_asset=asset,
            llm_market_category=cat,
        )

    if expected_cat != cat:
        raise UnclassifiedError(
            reason=f"CAT_MISMATCH:{cat}!={expected_cat}",
            target_asset=asset,
            llm_market_category=cat,
            candidates={"expected": expected_cat, "llm": cat},
        )

    return asset, cat


def infer_market_category(
    target_asset: str,
    market_category: str = "",
    *,
    layer: str = "",
    news_id: int = 0,
) -> str:
    """统一入口：根据 LLM 输出的 (asset, category) 推断最终 market_category。

    规则：
      - LLM 已给出且合法（属于 PROD 白名单 + 与 asset 配对一致）→ 直接返回
      - LLM 没给 / 给了非法品类但 asset 已知且有映射 → 用 TARGET_TO_MARKET 反推
      - 都不行 → 抛 UnclassifiedError（不兜底）

    调用方收到 UnclassifiedError 时**必须**写 `unclassified_events` 表，
    不可 `except: pass`，不可 fallback 到非 PROD 白名单值。
    """
    cat = (market_category or "").strip().upper()
    asset = (target_asset or "").strip().upper()

    # 路径 A：LLM 已给出 PROD 合法品类
    if cat and cat in MARKET_CATEGORIES_PROD:
        try:
            validate_pair(asset, cat)
        except UnclassifiedError as e:
            # 升级异常，注入 layer/news_id
            raise UnclassifiedError(
                reason=e.reason,
                target_asset=e.target_asset,
                llm_market_category=e.llm_market_category,
                candidates=e.candidates,
                news_id=news_id,
                layer=layer,
            )
        return cat

    # 路径 B：LLM 没给或给了非 PROD 品类 → 反推
    if not asset or asset == "NONE":
        raise UnclassifiedError(
            reason="MISSING_TARGET_ASSET",
            target_asset=asset,
            llm_market_category=cat,
            news_id=news_id,
            layer=layer,
        )
    inferred = map_target(asset)
    if inferred is None:
        raise UnclassifiedError(
            reason=f"NO_MAPPING_FOR_ASSET:{asset}",
            target_asset=asset,
            llm_market_category=cat,
            news_id=news_id,
            layer=layer,
        )
    return inferred


def is_sentinel(value: str) -> bool:
    """判断 value 是否是「占位 / 空」字符串（用于过滤 target_asset 等的合法判断）。"""
    if not value:
        return True
    return value.strip().upper() in ("", "NONE", "N/A", "NA", "NULL", "UNDEFINED", "TBD", "OTHER")


def row_is_unclassified(market_category: str) -> bool:
    """判断一行 ai_decisions 是否是「未归类」标记。

    生产环境合法值 = MARKET_CATEGORIES_PROD；除此之外（包括空串、'OTHER'）
    都视为待复核行，应进入 `unclassified_events` 表。
    """
    if not market_category:
        return True
    cat = market_category.strip().upper()
    if cat == "OTHER":
        return True
    return cat not in MARKET_CATEGORIES_PROD


# ──────────────────────────────────────────────────────────────────────────
# 5. 自检（debug / 单测用）
# ──────────────────────────────────────────────────────────────────────────
def self_check() -> Dict[str, Any]:
    """验证 TARGET_TO_MARKET 内部一致性。

    返回 {"ok": bool, "issues": [...]}。
    """
    issues: list = []
    seen_cats: Dict[str, list] = {}
    for asset, cat in TARGET_TO_MARKET.items():
        if cat not in MARKET_CATEGORIES_PROD:
            issues.append(f"{asset} -> {cat} (cat 不在生产白名单)")
        seen_cats.setdefault(cat, []).append(asset)
    return {"ok": not issues, "issues": issues, "categories": seen_cats}


if __name__ == "__main__":  # python -m market_classifier
    import json
    print(json.dumps(self_check(), ensure_ascii=False, indent=2))