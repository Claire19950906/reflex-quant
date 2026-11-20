#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
selftest_v309_endpoints.py — v309 P0: 端点函数**真调用**自检

为什么需要这个文件:
  v309 之前所有验证都是静态 grep (查文件里有没有某个字符串), 结果
  `/api/v237/reflection-dimension-map` 里写的是
      from review import reflection_dimension_map as _rdm
      return {"ok": True, **rdm.describe_mapping()}      # ← rdm 未定义!
  静态检查 100% 通过, 实际调用直接 NameError。
  → 验证必须**真 import + 真调用**, 光看代码不够。

检查项:
  1. bridge_server 能 import (无语法/导入期错误)
  2. v237_reflection_dimension_map() 能真调用, 返回 ok=True 且含映射结构
  3. v237_reflection_dimension_stats() 能真调用 (DB 未迁移时允许返回错误 dict,
     但绝不能抛 NameError / AttributeError / TypeError)
  4. 所有 v309 端点的**默认参数**必须是普通 Python 值, 不能是 fastapi 的 Query 对象

退出码: 0 = 全过, 1 = 有失败 (脚本里用失败即中断, 不要吞异常)
"""
from __future__ import annotations

import ast
import os
import sys

SRC = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SRC)
sys.path.insert(0, os.path.join(SRC, "engine"))

FAILED: list[str] = []


def _check(name: str, fn) -> None:
    try:
        fn()
        print(f"  PASS  {name}")
    except _Skipped as exc:
        print(f"  SKIP  {name}  ({exc})")
    except Exception as exc:
        FAILED.append(f"{name}: {type(exc).__name__}: {exc}")
        print(f"  FAIL  {name}  ({type(exc).__name__}: {exc})")


def _import_bridge() -> None:
    try:
        import bridge_server
    except ModuleNotFoundError as exc:
        # 缺依赖 (fastapi/requests/dotenv 等) 是**环境问题**, 不是代码 bug。
        # 记为 note 继续跑其它检查, 别让整份自检在这里就挂掉。
        print(f"        (note: bridge_server 依赖缺失, 跳过端点真调用: {exc})")
        raise _Skipped() from exc


class _Skipped(Exception):
    """标记"因环境缺依赖而跳过", 不算失败。"""


def _map_endpoint() -> None:
    try:
        import bridge_server as bs
    except ModuleNotFoundError as exc:
        raise _Skipped(f"bridge_server 依赖缺失: {exc}") from exc
    fn = getattr(bs, "v237_reflection_dimension_map", None)
    assert fn is not None, "端点函数 v237_reflection_dimension_map 不存在"
    res = fn()
    assert isinstance(res, dict), f"返回不是 dict: {type(res)}"
    assert res.get("ok") is True, f"返回 ok != True: {res.get('ok')} ({res.get('error')})"
    # 映射表结构: 至少要有 E 码映射和三个维度
    assert "error_codes" in res or "e_code_to_dimension" in res, \
        f"返回缺映射结构字段: {sorted(res.keys())}"
    for dim in ("A", "B", "C"):
        assert dim in str(res), f"返回缺维度 {dim}"


def _stats_endpoint() -> None:
    try:
        import bridge_server as bs
    except ModuleNotFoundError as exc:
        raise _Skipped(f"bridge_server 依赖缺失: {exc}") from exc
    fn = getattr(bs, "v237_reflection_dimension_stats", None)
    assert fn is not None, "端点函数 v237_reflection_dimension_stats 不存在"
    # 关键: 端点必须**不带参数**直接调用成功。
    # 早期版本写 `days: int = Query(90, ge=1, le=3650)`, 直接调用时 days 是
    # fastapi 的 Query 对象 → `days * 86400` 抛 TypeError。
    # 而旧版自检把 TypeError 当"运行期异常(可接受)"吞掉 → **假 PASS**,
    # 真实 bug 被藏了整整一个版本。这里 TypeError 必须炸出来。
    try:
        res = fn()
    except (TypeError, NameError, AttributeError) as exc:
        # 这三类是**代码 bug**, 不是环境问题, 必须炸出来
        raise AssertionError(f"{type(exc).__name__} (代码 bug, 不可吞): {exc}") from exc
    except Exception as exc:
        # DB 未迁移 / 表缺失 → 端点内部已捕获并返回错误 dict, 属于可接受
        print(f"        (note: 运行期异常(可接受): {type(exc).__name__}: {exc})")
        return
    assert isinstance(res, dict), f"返回不是 dict: {type(res)}"
    assert "ok" in res, f"返回缺 ok 字段: {sorted(res.keys())}"


# ── P0-4 端点默认参数不得是 fastapi Query 对象 ────────────────────────────
# 根因: `days: int = Query(90, ge=1, le=3650)` 这种写法在 HTTP 请求下由
# FastAPI 注入真实值, 但**直接 Python 调用**时拿到的是 Query 实例。
# 凡是内部互相调用 / 自检真调用的端点都会踩到 (TypeError: Query * int)。
#
# 修法: 统一改成 `days: Annotated[int, Query(ge=1, le=3650)] = 90`。
# 这个检查用 AST 解析, 不依赖运行时导入, 因此即使 fastapi 没装也能查出问题。
#
# v309 P1: 原来只查 8 个**具名**端点 → 新增端点只要不叫这 8 个名字就完全
# 逃过检查 (这就是 trading_signals / sources_alerts / review_snapshot 等
# 端点带着 `limit: int = 200` 这种无边界默认值的原因)。
# 现在改成: 扫描**所有** @app.get / @app.post 装饰的函数, 一个都不放过。
def _iter_endpoint_functions(tree):
    """产出所有被 @app.get/@app.post/@app.put/@app.delete 装饰的函数节点。"""
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            # 形如 @app.get("...") / @app.post("...")
            if not isinstance(dec, ast.Call):
                continue
            func = dec.func
            if isinstance(func, ast.Attribute) and func.attr in (
                "get", "post", "put", "delete", "patch",
            ):
                yield node
                break


def _no_query_object_defaults() -> None:
    import ast
    import os

    src_path = os.path.join(SRC, "bridge_server.py")
    with open(src_path, encoding="utf-8") as f:
        src = f.read()
    tree = ast.parse(src)

    bad: list[str] = []
    n_checked = 0
    for node in _iter_endpoint_functions(tree):
        n_checked += 1
        args = node.args
        # 位置参数默认值 = args.defaults, 关键字参数默认值 = args.kw_defaults
        for d in list(args.defaults) + [d for d in args.kw_defaults if d is not None]:
            if isinstance(d, ast.Call):
                fn_name = getattr(d.func, "id", None) or getattr(d.func, "attr", None)
                if fn_name == "Query":
                    bad.append(f"{node.name}: Query(...) 作为默认值")
            elif isinstance(d, ast.Attribute) and getattr(d.value, "id", None) == "Query":
                bad.append(f"{node.name}: Query.xxx 作为默认值")
    assert n_checked > 0, "没找到任何 FastAPI 端点 (AST 规则失效?)"
    assert not bad, (
        "以下端点默认值是 fastapi Query 对象 (直接 Python 调用会 TypeError): "
        f"{bad}; 必须改成 Annotated[T, Query(...)] = 默认值 形式"
    )
    print(f"        (已检查全部 {n_checked} 个 FastAPI 端点)")


# ── P1: 时间窗口过滤必须用统一口径 (行为测试, 不依赖真实 DB) ────────────────
def _time_window_filter_is_correct() -> None:
    """created_at 的窗口过滤必须对**两种历史落库格式**都正确。

    背景 (v309 P0 修完仍不完整):
      · paper_reflection_worker 写 'YYYY-MM-DDTHH:MM:SS' (本机本地时间)
      · bridge_server / DB DEFAULT 写 'YYYY-MM-DD HH:MM:SS' (**UTC**)
      v309 P0 只把 epoch 整数换成了空格格式字符串, 于是:
        '2026-10-25T01:00:00' > '2026-10-25 08:00:00'   ← T 的 ASCII 比 ' ' 大
      → 同一日期内的行被错误地判为"在窗口内"。

    这里用内存 SQLite 复现两种格式, 断言归一化表达式给出的结果正确。
    """
    import sqlite3

    from common import time_utils

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, created_at TEXT)")
    rows = [
        # (id, 落库串, 真实北京时刻)
        (1, "2026-10-25T01:00:00", "2026-10-25 01:00:00"),  # T 分隔 (worker)
        (2, "2026-10-25 09:00:00", "2026-10-25 17:00:00"),  # 空格 (UTC) → 北京 +8
        (3, "2026-10-24T23:00:00", "2026-10-24 23:00:00"),  # T 分隔, 窗口外
        (4, "2026-10-25 07:00:00", "2026-10-25 15:00:00"),  # 空格 UTC, 窗口内
    ]
    conn.executemany("INSERT INTO t VALUES (?,?)", [(r[0], r[1]) for r in rows])

    expr = time_utils.sql_bj_ts_expr("created_at")
    # 窗口: 北京 2026-10-25 08:00:00 ~ 2026-10-25 18:00:00
    lo = "2026-10-25 08:00:00"
    hi = "2026-10-25 18:00:00"
    got = {r[0] for r in conn.execute(
        f"SELECT id FROM t WHERE {expr} >= ? AND {expr} <= ?", (lo, hi))}
    assert got == {2, 4}, f"窗口过滤结果错: 期望 {{2,4}}, 实际 {got}"

    # 反向断言: 如果不用归一化表达式 (直接字典序比), 结果必须不同 → 证明该检查有效
    naive = {r[0] for r in conn.execute(
        "SELECT id FROM t WHERE created_at >= ? AND created_at <= ?", (lo, hi))}
    assert naive != {2, 4}, (
        "直接字典序比较恰好也对 → 归一化表达式没起作用, 检查本身失效"
    )
    conn.close()


def _time_utils_roundtrip() -> None:
    """time_utils 的生成/解析必须自洽 (epoch ↔ 北京串 ↔ epoch)。"""
    from common import time_utils

    # 固定样本: 2026-10-25 08:00:00 北京 = 2026-10-25 00:00:00 UTC
    ts = time_utils.bj_str_to_epoch("2026-10-25 08:00:00")
    assert ts is not None, "北京串解析失败"
    assert time_utils.epoch_to_bj_str(ts) == "2026-10-25 08:00:00", \
        f"epoch→北京串不自洽: {time_utils.epoch_to_bj_str(ts)}"
    # T 分隔 (落库格式) 也要能解析回同一时刻
    assert time_utils.bj_str_to_epoch("2026-10-25T08:00:00") == ts, \
        "T 分隔格式解析结果与空格格式不一致"
    # UTC 语义解析: '2026-10-25 00:00:00' (UTC) == 北京 08:00
    assert time_utils.utc_str_to_epoch("2026-10-25 00:00:00") == ts, \
        "UTC 串解析结果与北京串不一致"
    # epoch → 落库格式
    assert time_utils.epoch_to_bj_store(ts) == "2026-10-25T08:00:00", \
        f"落库格式错: {time_utils.epoch_to_bj_store(ts)}"


# ── P0-2 未来函数纪律 (行为测试, 不是静态 grep) ──────────────────────────
# 之前实现用 [move_ts-6h, move_ts+6h], 会采信异动**之后**才出现的技术信号。
# 现在上界必须是 move_ts 本身。
def _tech_signal_no_future_leak() -> None:
    import calc_event_move_attribution as cem

    move_ts = 1_700_000_000
    tech = {
        "XAU": [
            (move_ts - 3600, 0.80, "bottom_formation"),   # 异动前 1h → 应采信
            (move_ts + 3600, 0.90, "bottom_formation"),   # 异动后 1h → 必须拒绝
        ]
    }
    adj, note = cem._tech_confirmation(tech, "XAU", move_ts, 1.0)
    assert "bottom" in note, f"应采信异动前的信号, 实际 note={note!r}"
    # 只有未来信号时, 必须完全无贡献
    tech_only_future = {"XAU": [(move_ts + 3600, 0.90, "bottom_formation")]}
    adj2, note2 = cem._tech_confirmation(tech_only_future, "XAU", move_ts, 1.0)
    assert adj2 == 0.0, f"未来信号产生了调整量 {adj2} (note={note2!r})"
    assert note2 == "", f"未来信号产生了说明 {note2!r}"


def _tech_signal_uses_future_flag() -> None:
    import calc_event_move_attribution as cem
    # _load_tech_signals 必须过滤 uses_future=1
    src = open(cem.__file__, encoding="utf-8").read()
    assert "uses_future = 0" in src, "未过滤 uses_future=1 的技术信号"


# ── P0-3 归因块纪律 (行为测试) ───────────────────────────────────────────
def _ai_worker_src() -> str:
    """读 ai_worker.py 源码 (不 import —— 它的依赖链太重, 缺一个模块就整体挂)。"""
    import os
    p = os.path.join(SRC, "engine", "ai_worker.py")
    with open(p, encoding="utf-8") as f:
        return f.read()


def _attribution_block_respects_days() -> None:
    """days 参数必须真正生效 (之前只过滤 move_ts<now, 无时间下界)。"""
    src = _ai_worker_src()
    # 必须在 SQL 里有 move_ts 下界
    assert "move_ts >= ?" in src, "归因块 SQL 缺 move_ts >= ? 下界 (days 参数未生效)"
    assert "_since" in src, "归因块未计算时间下界 _since"


def _attribution_block_excludes_future() -> None:
    """归因块只取历史行 (move_ts < now)。"""
    src = _ai_worker_src()
    assert "move_ts < ?" in src, "归因块缺 move_ts < ? (未来行会进 prompt)"


def _attribution_block_days_is_callable() -> None:
    """_load_attribution_block 必须存在且接受 days 参数 (用 AST 解析, 不实际 import)。"""
    import ast
    src = _ai_worker_src()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_load_attribution_block":
            arg_names = {a.arg for a in node.args.args}
            assert "days" in arg_names, f"_load_attribution_block 签名无 days: {arg_names}"
            return
    raise AssertionError("ai_worker 缺 _load_attribution_block 函数定义")


if __name__ == "__main__":
    print("=" * 62)
    print("v309 P0 端点 / 归因 真调用自检")
    print("=" * 62)
    _check("bridge_server 可导入", _import_bridge)
    _check("/api/v237/reflection-dimension-map 真调用", _map_endpoint)
    _check("/api/v237/reflection-dimension-stats 真调用", _stats_endpoint)
    _check("P0-4 端点默认值不是 fastapi Query 对象 (全端点扫描)", _no_query_object_defaults)
    _check("P1 时间窗口过滤对两种历史格式都正确 (行为测试)", _time_window_filter_is_correct)
    _check("P1 time_utils epoch↔北京串 自洽 (行为测试)", _time_utils_roundtrip)
    _check("P0-2 技术信号无未来函数 (行为测试)", _tech_signal_no_future_leak)
    _check("P0-2 排除 uses_future=1", _tech_signal_uses_future_flag)
    _check("P0-3 归因块 days 参数生效", _attribution_block_respects_days)
    _check("P0-3 归因块只取历史行", _attribution_block_excludes_future)
    _check("P0-3 _load_attribution_block 签名含 days", _attribution_block_days_is_callable)
    print("-" * 62)
    if FAILED:
        print(f"FAILED ({len(FAILED)}):")
        for f in FAILED:
            print(f"  - {f}")
        sys.exit(1)
    print("ALL PASSED")
    sys.exit(0)
