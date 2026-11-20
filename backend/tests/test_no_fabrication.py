"""v282+: 防止捏造数据回归的自动 watchdog。

扫描 backend/src_python 与 frontend/{components,hooks,lib} 下所有源文件,
发现 `_honesty.py` 中声明的禁用模式出现在非豁免位置 → test fail。

用法:
    cd backend && python3 -m pytest tests/test_no_fabrication.py -v
    或
    cd backend && python3 tests/test_no_fabrication.py

跑通条件:
    - 任何禁止模式仅出现在 _honesty.py / honesty.ts / 本测试文件
    - 任何 24h 涨跌 / 价格 / 涨跌幅字段绝不 fallback 成 0
    - 任何"假装有数"的 placeholder 必须经过 _honesty.assert_honest 注释
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# 真理源
ROOT = Path(__file__).resolve().parents[1]  # backend/
SRC_PY = ROOT / "src_python"
FRONTEND = ROOT.parent / "frontend"

# 必须和 _honesty.py 一致
# 注意: 只针对「用户可见的数据」字段(24h 涨跌 / 价格变化 / 收益率),
# 不扫内部聚合(score/length/count — DB NULL → 0 是合法语义)
FORBIDDEN_PATTERNS: tuple[str, ...] = (
    "value or 0.0",
    "value or 0",
    "_PLACEHOLDER_PRICES",
    "_PLACEHOLDER_",
    "return (price, price)",
    # 用户可见字段的 null → 0 fallback
    "change24h ?? 0",
    "pct_change ?? 0",
    "change_pct ?? 0",
    "change24h || 0",
    "pct_change || 0",
    "info?.change24h ?? 0",
    "change24h or 0",
    "pct_change or 0",
    "info?.price ?? 0",
)

# 豁免文件 — 这些文件里的模式是合法的(本测试本身就触发模式)
EXEMPT_PATHS: frozenset[str] = frozenset({
    "_honesty.py",
    "honesty.ts",
    "test_no_fabrication.py",
})

# 每个模式附上「为什么是捏造」的解释,测试失败时打印给开发者
PATTERN_WHY: dict[str, str] = {
    "value or 0.0": (
        "把 None 假装成 0.0 → XAU/WTI 涨跌永远显示 0% 是假数据。"
        " 必须用 None 透传,前端按 '—' 显示。"
    ),
    "value or 0": "同上,但发生在整数上下文。",
    "_PLACEHOLDER_PRICES": (
        "硬编码精确价格做'已知假样本'过滤 → 金价涨到 4139 附近会误伤真实 K 线。"
        " v252.2 已改用 _is_flat_fake_bar 检测扁平假 bar。"
    ),
    "_PLACEHOLDER_": "通用的 name/placehoder 占位 — 任何以 _PLACEHOLDER_ 开头的常量都是可疑的。",
    "return (price, price)": (
        "假装 prev_close = current → 涨跌永远是 0% (假数据)。"
        " 应 return (price, None),前端按 '—' 显示。"
    ),
    "change24h ?? 0": "24h 涨跌 null → 0 会让 UI 显示假 0%。前端应 isRealNumber 检测 null → '—'。",
    "pct_change ?? 0": "涨跌幅 pct_change null → 0 同样会让 UI 显示假 0%。",
    "change_pct ?? 0": "同上(其他命名)。",
    "change24h || 0": "JS 端 24h 涨跌 null → 0 同上。",
    "pct_change || 0": "同上。",
    "info?.change24h ?? 0": "price-ticker 之前用这个 — 已修复,使用 isRealNumber。",
    "change24h or 0": "Python 端 24h 涨跌 null → 0。",
    "pct_change or 0": "同上。",
    "info?.price ?? 0": "价格 null → 0 会显示假 $0.00。",
}


def _scan_file(path: Path) -> list[tuple[int, str, str]]:
    """扫一个文件,返回 [(line_no, matched_pattern, line_text), ...]。"""
    hits: list[tuple[int, str, str]] = []
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return hits
    lines = text.splitlines()
    exempt_window = 3  # EXEMPT-HONESTY 在当前行或前 3 行内有效
    for i, raw_line in enumerate(lines):
        ln = i + 1
        stripped = raw_line.lstrip()
        # 跳过注释行
        if stripped.startswith(("#", "//", "*", '"""', "'''")):
            continue
        # 检查本行 + 前 3 行有没有 EXEMPT-HONESTY 标记(允许注释在上方)
        window_start = max(0, i - exempt_window)
        if any("EXEMPT-HONESTY:" in lines[j] for j in range(window_start, i + 1)):
            continue
        for pattern in FORBIDDEN_PATTERNS:
            if pattern in raw_line:
                hits.append((ln, pattern, raw_line.strip()))
    return hits


def _iter_source_files() -> list[Path]:
    """列出所有需要扫描的源文件。"""
    files: list[Path] = []
    if SRC_PY.exists():
        for p in SRC_PY.rglob("*.py"):
            if "__pycache__" in p.parts:
                continue
            if p.name in EXEMPT_PATHS:
                continue
            files.append(p)
    fe_dirs = [FRONTEND / "components", FRONTEND / "hooks", FRONTEND / "lib"]
    for d in fe_dirs:
        if not d.exists():
            continue
        for p in d.rglob("*.ts"):
            if p.name in EXEMPT_PATHS:
                continue
            files.append(p)
        for p in d.rglob("*.tsx"):
            if p.name in EXEMPT_PATHS:
                continue
            files.append(p)
    return files


def test_no_fabrication_patterns() -> None:
    """v282+: 全项目 grep 扫描禁用模式。"""
    all_hits: dict[Path, list[tuple[int, str, str]]] = {}
    for f in _iter_source_files():
        h = _scan_file(f)
        if h:
            all_hits[f] = h

    if all_hits:
        lines = ["\n❌ 检测到潜在的捏造数据模式 (v282+ 诚实宪法违反):", ""]
        for f, hits in all_hits.items():
            rel = f.relative_to(ROOT.parent)
            lines.append(f"  📁 {rel}")
            for ln, pat, txt in hits:
                why = PATTERN_WHY.get(pat, "(无解释)")
                lines.append(f"    L{ln}  [{pat}]  {txt[:120]}")
                lines.append(f"           ↳ {why}")
            lines.append("")
        lines.append("修复方法:")
        lines.append("  1. 把 fallback 改成 None / null 透传")
        lines.append("  2. 前端用 lib/honesty.ts 的 UNAVAILABLE_PLACEHOLDER 显示 '—'")
        lines.append("  3. 后端用 _honesty.NEVER_FABRICATE_INVARIANT 引用真理源")
        lines.append("  4. 如果是允许的 fallback(比如 DB NULL 聚合为 0),在文件顶部加 EXEMPT")
        raise AssertionError("\n".join(lines))


def test_honesty_truth_source_exists() -> None:
    """v282+: 真理源模块必须存在。"""
    assert (SRC_PY / "_honesty.py").exists(), (
        "缺少 backend/src_python/_honesty.py — 诚实宪法真理源不存在!"
    )
    assert (FRONTEND / "lib" / "honesty.ts").exists(), (
        "缺少 frontend/lib/honesty.ts — 前端诚实边界不存在!"
    )

    py_text = (SRC_PY / "_honesty.py").read_text(encoding="utf-8")
    assert "NEVER_FABRICATE_INVARIANT" in py_text, "_honesty.py 缺少 NEVER_FABRICATE_INVARIANT 常量"
    assert "第一条款" in py_text, "_honesty.py 缺少第一条款"

    fe_text = (FRONTEND / "lib" / "honesty.ts").read_text(encoding="utf-8")
    assert "NEVER_FABRICATE_INVARIANT" in fe_text, "honesty.ts 缺少 NEVER_FABRICATE_INVARIANT"


def test_honesty_contract_is_imported_in_hotspots() -> None:
    """v282+: 6 个已修关键点必须 import 真理源,作为代码层承诺。"""
    required_imports: dict[Path, str] = {
        SRC_PY / "multi_source_fetcher.py": "NEVER_FABRICATE",
        SRC_PY / "insight_verifier.py": "NEVER_FABRICATE",
        SRC_PY / "api_server.py": "NEVER_FABRICATE",
        FRONTEND / "components" / "quant" / "price-ticker.tsx": "NEVER_FABRICATE",
    }
    for path, token in required_imports.items():
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        assert token in text, (
            f"{path.relative_to(ROOT.parent)} 必须 import {token} — "
            "关键边界未引用诚实宪法真理源!"
        )


if __name__ == "__main__":
    try:
        test_no_fabrication_patterns()
        test_honesty_truth_source_exists()
        test_honesty_contract_is_imported_in_hotspots()
        print("✅ 所有诚实宪法检查通过 (v282+)")
    except AssertionError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)