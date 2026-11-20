#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
selftest_reflection_dimension_map.py — v309 P0-1 离线自检
====================================================================
为什么单独写一个脚本而不是只靠 pytest:
  本机 pytest 需要装依赖, 且 conftest 会重定向 DB 路径; 而映射表是
  **纯数据 + 纯函数** (零外部依赖), 用一个零依赖脚本就能在任何环境
  (包括只有生产机的 shell) 上跑, 验证口径没被改坏。

用法:
    python3 selftest_reflection_dimension_map.py
退出码: 0 = 全过, 1 = 有失败。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from review import reflection_dimension_map as rdm

_failures: list = []


def _check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}" + (f" — {detail}" if detail else ""))
        _failures.append(name)


def main() -> int:
    print("=" * 66)
    print("v309 P0-1 三维度映射表 自检")
    print("=" * 66)

    # 1. E 码映射合法性
    bad = [f"{c}->{d}/{s}" for c, (d, s) in rdm.E_CODE_TO_DIMENSION.items()
           if d not in ("A", "B", "C") or not rdm.is_valid_subcategory(d, s)]
    _check("所有 E 码映射到合法 (维度, 小类)", not bad, ", ".join(bad[:5]))

    # 2. reason_code 映射合法性
    bad = [f"{c}->{d}/{s}" for c, (d, s) in rdm.REASON_CODE_TO_DIMENSION.items()
           if d not in ("A", "B", "C") or not rdm.is_valid_subcategory(d, s)]
    _check("所有 reason_code 映射合法", not bad, ", ".join(bad[:5]))

    # 3. ontology 映射合法性 + 同 layer 同维度
    bad = []
    for layer, (d, s) in rdm.LAYER_TO_DIMENSION.items():
        if d not in ("A", "B", "C") or not rdm.is_valid_subcategory(d, s):
            bad.append(f"layer {layer}")
    for (layer, sl), (d, s) in rdm.SUB_LAYER_TO_DIMENSION.items():
        if d not in ("A", "B", "C") or not rdm.is_valid_subcategory(d, s):
            bad.append(f"{layer}/{sl}")
        base = rdm.LAYER_TO_DIMENSION.get(layer)
        if base and base[0] != d:
            bad.append(f"{layer}/{sl} 维度与 layer 默认不一致 ({d} vs {base[0]})")
    _check("所有 ontology 映射合法且同 layer 同维度", not bad, ", ".join(bad[:5]))

    # 4. 三个维度都被用到
    used_dims = {v[0] for v in rdm.E_CODE_TO_DIMENSION.values()}
    _check("E 码覆盖全部三个维度", used_dims == {"A", "B", "C"}, str(used_dims))

    # 5. 12 个小类无死桶
    used_subs = {v[1] for v in rdm.E_CODE_TO_DIMENSION.values()}
    used_subs |= {v[1] for v in rdm.REASON_CODE_TO_DIMENSION.values()}
    used_subs |= {v[1] for v in rdm.LAYER_TO_DIMENSION.values()}
    used_subs |= {v[1] for v in rdm.SUB_LAYER_TO_DIMENSION.values()}
    all_subs = {s for v in rdm.SUBCATEGORIES_BY_DIMENSION.values() for s in v}
    _check("12 个小类均被至少一条路径覆盖",
           used_subs == all_subs, f"未覆盖: {all_subs - used_subs}")

    # 6. 归一化
    _check("错误码归一 (e014/E014/ 014 /14 一致)",
           rdm.dimension_for_error_code("e014") == rdm.dimension_for_error_code("E014")
           == rdm.dimension_for_error_code("014") == rdm.dimension_for_error_code("14"))

    # 6b. 子码归一 (回归: 曾把 'E001a' 整体 upper 成 'E001A' 导致查不到)
    sub_hit = rdm.dimension_for_error_code("E001a")
    _check("子码归一 (E001a/e001a/E001A 一致且能查到)",
           sub_hit is not None
           and rdm.dimension_for_error_code("e001a") == sub_hit
           and rdm.dimension_for_error_code("E001A") == sub_hit
           and rdm.dimension_for_error_code(" e001A ") == sub_hit)
    _bad_sub = [k for k in rdm.E_CODE_TO_DIMENSION if k[-1].isalpha()
                and rdm.dimension_for_error_code(k) != rdm.E_CODE_TO_DIMENSION[k]]
    _check("所有子码经自身/大小写变体都能查到", not _bad_sub, ", ".join(_bad_sub[:5]))

    # 7. 未知输入不猜
    _check("未知 E 码返回 None", rdm.dimension_for_error_code("E999") is None)
    _check("越界 reason_code 返回 None", rdm.dimension_for_reason_code(999) is None)
    _check("空输入 resolve 返回 None",
           rdm.resolve_dimension([], None, "", "") is None)
    _check("无法归类的行返回空维度",
           rdm.classify_reflection_row({}) == ("", ""))

    # 8. 优先级
    e_hit = rdm.dimension_for_error_code("E014")
    _check("优先级: E 码 > reason_code",
           rdm.resolve_dimension(["E014"], 6, "analysis", "llm_output") == e_hit)
    r_hit = rdm.dimension_for_reason_code(6)
    _check("优先级: reason_code > ontology",
           rdm.resolve_dimension([], 6, "analysis", "llm_output") == r_hit)
    _check("ontology 兜底生效",
           rdm.resolve_dimension([], None, "data", "latency")
           == (rdm.DIMENSION_C, "data_feed_timeout"))

    # 9. describe_mapping 可导出
    d = rdm.describe_mapping()
    _check("describe_mapping 结构完整",
           set(d.get("dimensions", {})) == {"A", "B", "C"}
           and bool(d.get("error_codes")) and bool(d.get("reason_codes")))

    print("=" * 66)
    if _failures:
        print(f"FAILED: {len(_failures)} 项 — {', '.join(_failures)}")
        return 1
    print("ALL PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
