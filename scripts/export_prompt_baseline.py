# -*- coding: utf-8 -*-
"""导出 Prompt Force（非 JSON 模式）基线快照到 docs/prompt_force_baseline.md。

用途：改动提示词前后对照、改坏了回滚。真正生效的提示词在
backend/src_python/engine/ai_worker.py；提示库页面可写数据库覆盖
（ai_main_prompts 表），导出时会标注当前是否存在覆盖。

用法：cd backend && python ../scripts/export_prompt_baseline.py
"""

import datetime
import hashlib
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, "backend", "src_python")
sys.path.insert(0, SRC)

import config
from engine import ai_worker as aw


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def count_sections(text: str) -> int:
    """统计 ═══ 分区数（用字符本身，避免编码假设）。"""
    return text.count("\u2550\u2550\u2550") // 2


def override_info() -> str:
    """检查数据库中是否存在 json_force_prompt 覆盖。"""
    try:
        conn = sqlite3.connect(config.DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            rows = list(conn.execute("SELECT * FROM ai_main_prompts"))
        except sqlite3.OperationalError:
            return "表不存在（引擎/API 首次 migrate 后创建）"
        if not rows:
            return "0 条覆盖 —— 生效内容 = 本文件快照（代码常量）"
        keys = []
        for r in rows:
            d = dict(r)
            k = d.get("key") or d.get("name") or "?"
            v = d.get("content") or d.get("value") or ""
            keys.append("%s(%d字符)" % (k, len(str(v))))
        return "存在覆盖：%s" % ", ".join(keys)
    except Exception as exc:  # 读库失败不应阻断导出
        return "读取失败：%s" % exc
    finally:
        try:
            conn.close()
        except Exception:
            pass


def build() -> str:
    force = aw._JSON_PROMPT_FORCE
    system = aw._SYSTEM_PROMPT
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    fence = "`" * 3

    p = []
    p.append("# Prompt Force 基线（非 JSON 模式）")
    p.append("")
    p.append("> 本文件是 `_JSON_PROMPT_FORCE` 的**只读快照**，用于改动前后对照与回滚。")
    p.append("> 真正生效的提示词在 `backend/src_python/engine/ai_worker.py`；提示库页面可写数据库")
    p.append("> 覆盖（`ai_main_prompts` 表），下表标注导出时的覆盖状态。")
    p.append("")
    p.append("| 项 | 值 |")
    p.append("| --- | --- |")
    p.append("| 导出时间 | %s |" % ts)
    p.append("| 字符数 | %d |" % len(force))
    p.append("| 行数 | %d |" % (force.count("\n") + 1))
    p.append("| 分区数 | %d |" % count_sections(force))
    p.append("| SHA256(前16位) | `%s` |" % sha(force))
    p.append("| 数据库覆盖 | %s |" % override_info())
    p.append("")
    p.append("## 代码契约（改提示词时不可破坏）")
    p.append("")
    p.append("- 六段名（顺序不可变）：%s" % " / ".join(aw._SIX_STAGES))
    p.append("- 主体标注行：`[主体]对象：XXX ｜ 国别/市场：XXX`（前端 `_MARKER_RE` 依赖）")
    p.append("- 输出契约：17 个主字段 + 可选 `sibling_decisions`（后端解析依赖）")
    p.append("- 资产归类代码：XAU/WTI/BTC/ETH/SOL + 13 只美股清单 + NONE/OTHER")
    p.append("- 分区标题格式：`═══ 标题 ═══`（本文件按此统计分区数）")
    p.append("")
    p.append("## 改写规则的四段公式（新增规则时套用）")
    p.append("")
    p.append("```text")
    p.append("〔禁止什么〕 + 〔改做什么〕 + 〔限定范围/触发条件〕 + 〔违者怎么处理〕")
    p.append("```")
    p.append("")
    p.append("- 硬约束用 `必须 / 禁止 / 不得` + `★`；倾向性用 `优先 / 倾向 / 除非…否则`")
    p.append("- 对质量类要求，用「禁止某个陈词滥调模板」反向定义，比正面说「要有深度」有效")
    p.append("- 新增硬约束后，同步在最后一节「输出前自检清单」加一条 `□` 问句")
    p.append("- 禁止在 Prompt Force 写具体品种偏好（那属于品种专项规则，见提示库）")
    p.append("")
    p.append("---")
    p.append("")
    p.append("## 正文（逐字快照）")
    p.append("")
    p.append(fence + "text")
    p.append(force.strip())
    p.append(fence)
    p.append("")
    p.append("---")
    p.append("")
    p.append("## 附：JSON 模式提示词（`_SYSTEM_PROMPT`）逐字快照")
    p.append("")
    p.append("字符数 %d，行数 %d，SHA256 `%s`。" % (len(system), system.count("\n") + 1, sha(system)))
    p.append("")
    p.append("两条通道各用一份：OpenRouter/nassaapi（`json_mode=False`）用上面的 Prompt Force；")
    p.append("`json_mode=True` 的通道用下面这份。**改约束时两份都要改**，否则换通道即失效。")
    p.append("")
    p.append(fence + "text")
    p.append(system.strip())
    p.append(fence)
    p.append("")
    return "\n".join(p)


def main() -> int:
    out = os.path.join(ROOT, "docs", "prompt_force_baseline.md")
    if "--verify" in sys.argv:
        return verify(out)
    text = build()
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    print("已写入: %s" % out)
    print("字符数 %d | 行数 %d" % (len(text), text.count("\n") + 1))
    return 0


def verify(path: str) -> int:
    """校验基线快照的两份提示词正文是否与当前 ai_worker.py 常量逐字一致。

    两份都要校验：OpenRouter/nassaapi 走 Prompt Force、json_mode=True 的通道走
    _SYSTEM_PROMPT；只校验一份会漏掉"改了这份没改那份"的失效。
    """
    import re
    if not os.path.exists(path):
        print("基线文件不存在: %s" % path)
        return 1
    md = open(path, encoding="utf-8").read()
    fence = "`" * 3
    rc = 0

    def _extract(anchor_text: str) -> str:
        idx = md.find(anchor_text)
        if idx < 0:
            return ""
        m = re.search(re.escape(fence) + r"text\n(.*?)\n" + re.escape(fence), md[idx:], re.S)
        return m.group(1) if m else ""

    for label, anchor, orig in (
        ("Prompt Force（json_mode=False）", "## 正文（逐字快照）", aw._JSON_PROMPT_FORCE.strip()),
        ("_SYSTEM_PROMPT（json_mode=True）", "## 附：JSON 模式提示词", aw._SYSTEM_PROMPT.strip()),
    ):
        extracted = _extract(anchor)
        if not extracted:
            print("%s：基线里找不到快照小节" % label)
            rc = 1
            continue
        same = extracted == orig
        print("%s 逐字一致: %s（快照 %s / 源码 %s）"
              % (label, same, sha(extracted), sha(orig)))
        if not same:
            print("  提示词已改动 —— 如需更新基线，重跑本脚本（不加 --verify）")
            rc = 1
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
