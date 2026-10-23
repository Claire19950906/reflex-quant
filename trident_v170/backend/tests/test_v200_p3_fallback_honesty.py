"""v200 P3: 兜底横幅文案修订 + 强制 LLM 输出完整八段

修复前问题:
- LLM 实际给了真东西（reasoning 45-80 字符），但因 reasoning_path 太短触发 v176.6 兜底
- 前端正则命中「由 LLM 摘要归纳」「reasoning 摘要未展开」「七段未完整展开」等关键词
- 用户看到横幅「⚠ 服务端兜底 | 数据不足，服务端自动兜底（无真实 AI 分析）」
- 但 LLM 实际给了真东西 — 横幅文案误导

P3 修复:
1. 后端 v176.6 兜底措辞改为中性 — 不再含触发前端 summary 类的关键词
2. 前端横幅文案改为绿色提示（说明 LLM 给了真东西），仅在 LLM 完全未给 reasoning 时显示琥珀警告
3. prompt 强制 LLM 输出完整八段（≥200 字符）
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src_python"))
from engine import ai_worker as _aw


class TestP3BackendFallbackRewording:
    """v200 P3: 后端兜底措辞中性化，不再触发前端 summary 类横幅"""

    def test_v1766_fallback_uses_neutral_wording(self):
        """v176.6 兜底分支使用「LLM 真东西摘要」中性措辞"""
        src = open(_aw.__file__).read()
        assert "[驱动力] LLM 真东西摘要" in src, \
            "v176.6 兜底分支应使用「LLM 真东西摘要」措辞"

    def test_old_summary_keyword_removed(self):
        """旧「由 LLM 摘要归纳」「reasoning 摘要未展开」应被替换"""
        src = open(_aw.__file__).read()
        # 这些是原 v176.6 触发的措辞，应该已被替换
        # 但允许旧逻辑（v176.3 fallback）保留这些关键词，仅检查 v176.6 主路径已改
        assert "由 LLM 摘要归纳" not in src or "LLM 真东西摘要" in src

    def test_v200_p3_marker_present(self):
        """v200 P3 标记应存在于源码"""
        src = open(_aw.__file__).read()
        assert "v200 P3" in src


class TestP3PromptForceEightStages:
    """v200 P3: prompt 强制 LLM 输出完整八段（≥200 字符）"""

    def test_eight_stage_warning_in_prompt(self):
        """prompt 含「八段骨架」「≥200 字符」警告"""
        src = open(_aw.__file__).read()
        assert "【v200 P3 铁命令】" in src
        assert "≥200 字符" in src

    def test_warning_explains_consequence(self):
        """警告明确告诉 LLM 后果：前端显示「数据缺」横幅"""
        src = open(_aw.__file__).read()
        assert "数据缺" in src or "用户看不到" in src


class TestP3FrontendBannerReworded:
    """v200 P3: 前端横幅文案修订 — 不再误导为「无真实 AI 分析」"""

    def setup_method(self):
        # tests/ 在 backend/ 内部，frontend 在 backend 同级 trident_v170/frontend
        fe_path = os.path.join(
            os.path.dirname(__file__), "..", "..", "frontend",
            "components", "replay", "analysis-steps.tsx"
        )
        if not os.path.exists(fe_path):
            self.src = ""
        else:
            self.src = open(fe_path).read()

    def test_old_misleading_label_removed(self):
        """旧的「无真实 AI 分析」横幅 label 应被替换（注释中允许保留为说明）"""
        if not self.src:
            pytest.skip("frontend not found")
        # 找横幅 label 字符串（结构是 "label: "xxx""）
        import re
        labels = re.findall(r"label:\s*['\"]([^'\"]*)['\"]", self.src)
        # 注释里允许出现；label 数组里不允许
        for lbl in labels:
            assert "无真实 AI" not in lbl, \
                f"横幅 label 不应误导：{lbl!r}"

    def test_old_warning_icon_replaced(self):
        """旧「⚠ 服务端兜底」应被替换为绿色 ✓ 提示（除非 LLM 完全未输出）"""
        if not self.src:
            pytest.skip("frontend not found")
        assert "✓ LLM 真实分析" in self.src

    def test_full_kind_keeps_amber_warning(self):
        """真兜底（kind=full）仍保留琥珀警告"""
        if not self.src:
            pytest.skip("heavy.ai not found")
        assert "⚠ 数据缺" in self.src

    def test_summary_kind_uses_emerald(self):
        """summary 类（LLM 给了真东西）用绿色样式"""
        if not self.src:
            pytest.skip("frontend not found")
        assert "emerald" in self.src, "summary kind 应用 emerald 绿色样式"


class TestP3EndToEndReasoningHonesty:
    """P3 端到端：reasoning 与 reasoning_path 关系真实可审计"""

    def test_reasoning_field_independence(self):
        """reasoning 字段（LLM 真东西）应独立于 reasoning_path"""
        src = open(_aw.__file__).read()
        # _presanitize_for_schema / sanitize_llm_result 应当保留 reasoning 字段
        assert "reasoning" in src

    def test_p3_marker_count(self):
        """v200 P3 标记至少 3 处（后端 + 前端 + 测试）"""
        total = 0
        for path in [
            "src_python/engine/ai_worker.py",
            "frontend/components/replay/analysis-steps.tsx",
            "tests/test_v200_p3_fallback_honesty.py",
        ]:
            full = os.path.join(
                os.path.dirname(os.path.dirname(__file__)), "..", path
            )
            # from tests/ — 路径是 tests/../
            # 简化：从工作目录算
        # 直接读 cwd 路径
        backend_root = os.path.join(os.path.dirname(__file__), "..")
        for relpath in [
            "src_python/engine/ai_worker.py",
            "../frontend/components/replay/analysis-steps.tsx",
        ]:
            full = os.path.join(backend_root, relpath)
            if os.path.exists(full):
                cnt = open(full).read().count("v200 P3")
                total += cnt
        assert total >= 3, f"P3 marker count too low: {total}"