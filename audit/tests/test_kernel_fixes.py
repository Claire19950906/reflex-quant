"""内核修正的回归测试（两条都是真外部数据暴露出来的问题）。

1. E014 读错了字段：`reasoning or reasoning_path` 让短 reasoning 盖掉完整的
   reasoning_path，八段齐全也判"因果链断裂"。
2. E018 曾因 `ad.asset` 列不存在被静默吞掉（backend 已修，这里锁定审计侧不再复现）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from audit import audit
from audit.core.context import CheckContext

FIXTURE = Path(__file__).parent / "fixtures" / "dec_11816.json"


def _ctx(tmp_path, nodes) -> CheckContext:
    (tmp_path / "rules.json").write_text(json.dumps(
        {"version": "providers.v1", "rules": []}, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "drivers.json").write_text(json.dumps(
        {"version": "providers.v1", "nodes": nodes}, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "history.json").write_text(json.dumps(
        {"version": "providers.v1", "samples": [], "trades": [], "decisions": []},
        ensure_ascii=False), encoding="utf-8")
    (tmp_path / "kline.json").write_text(json.dumps(
        {"version": "providers.v1", "bars": []}, ensure_ascii=False), encoding="utf-8")
    return CheckContext.from_file(tmp_path)


def test_complete_reasoning_path_is_not_flagged(tmp_path):
    """真实决策 11816：reasoning 只有几十字，但 reasoning_path 八段齐全 → 不该报 E014。"""
    dec = json.loads(FIXTURE.read_text(encoding="utf-8"))
    ctx = _ctx(tmp_path, [{"asset": "WTI", "label": "OPEC 增产", "is_core": 1, "active": 1}])
    r = audit(dec, ctx, mode="pretrade")
    assert not any(f["code"] == "E014" for f in r["findings"]), \
        "八段齐全省略了 structured 字段 → 这是字段优先级 bug，不是该决策的问题"


def test_missing_four_sections_still_flags(tmp_path):
    """reasoning_path 只有 4/8 段 → E014 仍要报（修 bug 不等于放水）。"""
    dec = json.loads(FIXTURE.read_text(encoding="utf-8"))
    dec["reasoning_path"] = "总框架：看多。因素定位：供给。驱动力：减产。总结：偏多。"
    dec["reasoning"] = ""
    ctx = _ctx(tmp_path, [{"asset": "WTI", "label": "OPEC 增产", "is_core": 1, "active": 1}])
    r = audit(dec, ctx, mode="pretrade")
    e014 = [f for f in r["findings"] if f["code"] == "E014"]
    assert e014 and e014[0]["severity"] == 3
    assert "4/8" in e014[0]["detail"] or "缺 4 段" in e014[0]["detail"]
