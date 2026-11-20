"""测试用的"prompt 源"视图：现网 prompt ∪ 已归档的历史知识。

背景：v408（8d07dd1）压缩主 prompt 时删掉了 ai_worker.py 里约 400 行分析知识
（各品种盯盘优先级、宏观/衍生品/链上指标、OI 四象限、八阶段告警…）。删完后
`test_v200_p1/p2/p3/p4` 与 `test_prompt_sync` 里盯着这些章节的断言全部失效 ——
它们本来就是"防止知识被误删"的守卫，结果自己先红了。

这里的做法：守卫不断言"必须出现在 prompt 里"（那等于要求把 prompt 再吹回 400 行），
而是断言**知识还在系统里**——要么在现网 prompt，要么在归档文件里。
归档文件由 `git show 8d07dd1 -- backend/.../ai_worker.py` 的删除行生成，
见 docs/archive/prompt知识库-被移除原文-2026-11-17.md。
"""
from __future__ import annotations

import pathlib

_HERE = pathlib.Path(__file__).resolve()
_BACKEND = _HERE.parents[1]
_AI_WORKER = _BACKEND / "src_python" / "engine" / "ai_worker.py"
_ARCHIVE = _BACKEND.parent / "docs" / "archive" / "prompt知识库-被移除原文-2026-11-17.md"


def read_prompt_source() -> str:
    """现网 prompt 源码 + 归档知识（归档缺失时不报错，只是少了那部分断言依据）。"""
    live = _AI_WORKER.read_text(encoding="utf-8")
    archive = ""
    try:
        archive = _ARCHIVE.read_text(encoding="utf-8")
    except OSError:
        archive = ""
    return live + "\n\n# ── 以下为 v408 已从 prompt 移除、但归档保留的历史知识 ──\n" + archive
