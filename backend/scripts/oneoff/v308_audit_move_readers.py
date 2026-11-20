#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v308 P1-fix3 — 量纲污染 (|move_pct| > 90%) reader 全量审计 (AST)
=====================================================================

为什么需要这个独立工具:
  v308_verify.sh 里的 AST 静态检查只扫 3 个文件 (bridge_server.py /
  calc_event_move_attribution.py / api_server.py)。那 3 个文件是**已知的**
  reader 宿主, 但漏了两类风险:
    ① 其它后端模块 (abnormal_detector / causal_graph / quick_sim ...) 里
       也可能有读 critical_price_moves / event_move_attribution 的 SQL;
    ② SQL 定义在**模块级常量**里时, 老的 resolve_sql 只在函数作用域内找
       赋值 → 解析不到 → 那条 SQL 被**静默跳过** (漏检, 不是误报)。

本工具是 v308_verify.sh 里那段检查器的**超集**版本:
  - 扫描 backend/src_python 下**全部** .py;
  - SQL 解析支持 函数内赋值 / 模块级常量 / 闭包变量 / `+=` 追加拼装 /
    隐式拼接 / f-string / + 拼接;
  - **解析不出 SQL 文本时不再静默跳过** → 报 SUSPECT 让人工确认
    (静默跳过 = 漏检, 而漏检正是本工具要消灭的失败模式);
  - 判定分级, 不把"看不清"和"确实没过滤"混为一谈:
      OK      —— 有可证明的量纲过滤
      SUSPECT —— 有过滤但方向/强度可疑 (只有下界 / BETWEEN 上界过大),
                 或用了 ? 占位符但看不到阈值来源, 或 SQL 解析不出来
                 → 只提示, **不阻断构建**
      MISSING —— SQL 里连 move_pct 比较都没有 → 真的漏了 → --strict 时 exit 1

  ⚠️ 与 v308_verify.sh 内联检查器 (4c) 的关系:
     - **同一条查询**: 判定顺序一致 (① SQL ③ 作用域 ② Python); 4d 的作用域规则
       额外接受别名/IFNULL 写法 → 对 4c 真正判过的那条查询, 4d 只会更宽,
       不会更严 → 不会出现"4c 过 / 4d 挂"。
     - **覆盖面**: 4d 远大于 4c —— 4c 只查 3 个已知宿主文件、只认 SELECT 开头,
       且 SQL 解析不出来时**静默跳过**。4d 查全部 .py、认 WITH(CTE)、能解析
       模块级常量 / 闭包变量 / += 拼装 → 这些 4c 的盲区 4d 会报 MISSING 并
       **卡构建**。这是**预期行为** (那些正是要抓的洞), 不是误报。

用法:
    python3 v308_audit_move_readers.py                    # 审计本树 backend/src_python
    python3 v308_audit_move_readers.py --root /path/tree  # 审计指定树
    python3 v308_audit_move_readers.py --strict           # 有 MISSING 时 exit 1
    python3 v308_audit_move_readers.py --files a.py,b.py  # 只扫指定文件
"""
from __future__ import annotations

import argparse
import ast
import pathlib
import re
import sys
from typing import Dict, List, Optional, Tuple

# ── 常量 ──────────────────────────────────────────────────────────────────
TABLES = ("critical_price_moves", "event_move_attribution")
THRESH_NAMES = ("MAX_PLAUSIBLE_MOVE_PCT",)
# 可信上界: 与 bridge_server.MAX_PLAUSIBLE_MOVE_PCT / calc_event_move_attribution
# 保持同值。超过这个数的比较拦不住污染行。
MAX_PLAUSIBLE = 90.0

# 列名允许带表别名 (cpm.move_pct / t.move_pct) —— 否则带别名的查询会被误判 MISSING。
# \b 防止 "xmove_pct" 这种字段名尾部误匹配。
_MP_VAL = r"(?:[A-Za-z_]\w*\.)?\bmove_pct\b"
# 操作数: COALESCE(move_pct, 0) / IFNULL(move_pct, 0) / 裸 move_pct
_VAL_EXPR = (r"(?:(?:coalesce|ifnull)\s*\(\s*" + _MP_VAL
             + r"\s*,\s*-?\d+(?:\.\d+)?\s*\)"
             + r"|" + _MP_VAL + r")")
# 一次完整的 ABS(...) 包裹: ABS(move_pct) / ABS(COALESCE(move_pct, 0))
_ABS_EXPR = r"abs\s*\(\s*" + _VAL_EXPR + r"\s*\)"
# ABS 出现即算 —— 与 v308_verify.sh 内联检查器 rule① **同口径**, 再加上表别名写法。
# 与内联检查器同口径 → 对同一条查询两处判定不会打架 → 不会误报卡构建。
RE_ABS_TEXT = re.compile(_ABS_EXPR, re.I)
# 比较位 (运算符, 值): 值是数字 / ? 占位符 / 命名参数。
# ABS(...) 与裸 move_pct 都要匹配 —— 裸的写在上层会单独降级成"弱证据"。
# 命名参数 (:max / @max / $max) 说明阈值在别处 → 不能当成"上界已证实"
# → 交由上层按 PARAM 处理 (人工/上层确认阈值来源)。
_NAMED = r"[:@$]\w+"
RE_ABS_SITE = re.compile(
    _ABS_EXPR + r"\s*(>=|<=|>|<|=)\s*(-?\d+(?:\.\d+)?|\?|" + _NAMED + r")", re.I)
# 裸 move_pct 比较: 只认**字面数字**阈值 (真正写死的上界)。
# 占位符 / 命名参数走 RE_CMP_Q → PARAM (阈值未知, 交给上层找阈值来源)。
RE_SITE = re.compile(
    _VAL_EXPR + r"\s*(>=|<=|>|<|=)\s*(-?\d+(?:\.\d+)?)", re.I)
# BETWEEN 只认**正序**的 "BETWEEN lo AND hi"; "NOT BETWEEN" 是取反, 不是上界。
RE_BETWEEN = re.compile(
    _MP_VAL + r"\s+between\s+(-?\d+(?:\.\d+)?)\s+and\s+(-?\d+(?:\.\d+)?)", re.I)
RE_BETWEEN_Q = re.compile(_MP_VAL + r"\s+between\s+\?\s+and\s+\?", re.I)
RE_CMP_Q = re.compile(_MP_VAL + r"\s*(>=|<=|>|<|=)\s*(\?|" + _NAMED + r")", re.I)
# 命名参数 BETWEEN (:lo / :hi / @x) —— 内联检查器按 flat 文本 "move_pctbetween"
# 判定为"已过滤", 本工具也必须认, 否则同一条 reader 两处判定不一致 → 误报。
RE_BETWEEN_ANY = re.compile(_MP_VAL + r"\s+between\s+\S+\s+and\s+\S+", re.I)



# ── 通用小工具 ────────────────────────────────────────────────────────────
def flat(text: str) -> str:
    """去掉所有空白 + 转小写 → 便于跨写法比对。"""
    return "".join(text.split()).lower()


def build_parents(tree: ast.AST) -> Dict[ast.AST, ast.AST]:
    parents: Dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


def enclosing(node: ast.AST, parents: Dict[ast.AST, ast.AST], kinds) -> Optional[ast.AST]:
    cur = node
    while cur in parents:
        cur = parents[cur]
        if isinstance(cur, kinds):
            return cur
    return None


def enclosing_chain(node: ast.AST, parents: Dict[ast.AST, ast.AST]) -> List[ast.AST]:
    """从内到外的函数作用域链 —— 用来解析**闭包变量**。

    老实现只看**最近的一层**函数: 如果 SQL 变量定义在外层函数、被内层函数
    拿去 execute, 老实现解析不到 → SQL 文本为空 → 那条查询被**静默跳过**
    (漏检)。这正是本工具要消灭的失败模式, 所以这里必须沿作用域链往外找。
    """
    chain: List[ast.AST] = []
    cur = node
    while cur in parents:
        cur = parents[cur]
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
            chain.append(cur)
    return chain


def own_scope_nodes(fn: ast.AST) -> List[ast.AST]:
    """函数**自身**作用域内的语句 (不进嵌套函数/类定义)。

    ast.walk(fn) 会把嵌套函数体也捞进来, 于是内层函数的同名局部变量会
    污染外层的解析结果 → 可能把别的 SQL 当成这条查询的 SQL。
    """
    out: List[ast.AST] = []
    stack: List[ast.AST] = list(getattr(fn, "body", []) or [])
    while stack:
        n = stack.pop()
        out.append(n)
        for child in ast.iter_child_nodes(n):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef,
                                  ast.Lambda, ast.ClassDef)):
                continue
            stack.append(child)
    return out


def scope_assignments(fn: ast.AST, name: str) -> List[tuple]:
    """函数自身作用域内对 `name` 的赋值, 按源码顺序。

    返回 (lineno, kind, value_node), kind ∈ {"set", "aug"}:
      set —— 普通赋值 (取最后一次, 符合 Python 语义)
      aug —— `sql += "..."` 追加 (老实现只认 Assign → 追加的那段 SQL 丢失)
    """
    out: List[tuple] = []
    for n in own_scope_nodes(fn):
        if isinstance(n, ast.Assign):
            for tgt in n.targets:
                if isinstance(tgt, ast.Name) and tgt.id == name:
                    out.append((n.lineno, "set", n.value))
        elif isinstance(n, ast.AnnAssign):
            if (isinstance(n.target, ast.Name) and n.target.id == name
                    and n.value is not None):
                out.append((n.lineno, "set", n.value))
        elif isinstance(n, ast.AugAssign):
            if isinstance(n.target, ast.Name) and n.target.id == name:
                out.append((n.lineno, "aug", n.value))
    out.sort(key=lambda t: t[0])
    return out


def scope_of(node: ast.AST, parents: Dict[ast.AST, ast.AST], src: str) -> Tuple[str, str]:
    """返回 (作用域源码, 作用域描述)。

    粒度与 v308_verify.sh 内联检查器一致:
      - 在函数里 → 整个函数体 (能看到动态 where 拼装 / Python 侧过滤)
      - 在模块级 → **最近的那条语句** (不能用整个模块: 模块里别处出现一次
        过滤写法, 就会把这里这个**真的漏了**的模块级 reader 也放过去 →
        恰好掩盖掉本工具要查的那类洞)
    """
    fn = enclosing(node, parents, (ast.FunctionDef, ast.AsyncFunctionDef))
    if fn is not None:
        seg = ast.get_source_segment(src, fn) or ""
        name = getattr(fn, "name", "?")
        return seg, f"func {name}"
    stmt = enclosing(node, parents, (ast.stmt,))
    if stmt is not None:
        seg = ast.get_source_segment(src, stmt) or ""
        return seg, "module-stmt"
    return "", "module"


def collect_assigns(tree: ast.AST) -> Dict[str, ast.AST]:
    """**模块级**常量: 名字 → 赋值节点的 value。

    只取 Module.body 里直接出现的 Assign —— 不能用 ast.walk 全树收集,
    否则会把**别的函数**里的局部变量也收进来, 解析时可能拿到错的 SQL。
    同一名字多次赋值时取**最后一次**(Python 模块级语义)。
    """
    out: Dict[str, ast.AST] = {}
    body = getattr(tree, "body", []) or []
    for node in body:
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    out[tgt.id] = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.value is not None:
                out[node.target.id] = node.value
    return out


def resolve_sql(node: ast.AST, parents: Dict[ast.AST, ast.AST],
                module_assigns: Dict[str, ast.AST], depth: int = 0) -> str:
    """取 SQL 文本。args[0] 是变量名时, 回溯它的赋值表达式。

    支持: 常量 / 隐式拼接 / f-string / + 拼接 / 变量(函数内 + 模块级) /
    **拼接里含变量** (如 `SQL_ALL + " AND ABS(...) <= ?"`)。
    最后这一种是内联检查器的老盲区: 它只认纯字面量, 拼上变量的 SQL 解析
    不出来 → 整条被静默跳过。
    """
    if depth > 6:                      # 防递归爆掉 (a = b; b = a)
        return ""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = []
        for v in node.values:
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                parts.append(v.value)
            else:
                parts.append("?")
        return "".join(parts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return (resolve_sql(node.left, parents, module_assigns, depth + 1)
                + resolve_sql(node.right, parents, module_assigns, depth + 1))
    if isinstance(node, ast.IfExp):
        return (resolve_sql(node.body, parents, module_assigns, depth + 1)
                + " " + resolve_sql(node.orelse, parents, module_assigns, depth + 1))
    # `"... WHERE x IN ({}) ...".format(",".join(...))` —— 模板本身是字面量,
    # 占位内容不影响"读没读目标表 / 有没有 move_pct 上界"的判定 → 取模板即可。
    # (取不到模板的话, 这类查询会被当成"解析不出来" → 报成 SUSPECT 噪音)
    if (isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "format"):
        return resolve_sql(node.func.value, parents, module_assigns, depth + 1)
    if isinstance(node, ast.Name):
        return resolve_name(node, parents, module_assigns, depth)
    return ""


def resolve_name(node: ast.Name, parents: Dict[ast.AST, ast.AST],
                 module_assigns: Dict[str, ast.AST], depth: int = 0) -> str:
    """解析一个变量当前持有的 SQL 文本。

    老实现漏掉的三种写法 (漏掉 = 解析不出 SQL = 那条查询被**静默跳过**
    = 漏检, 而漏检正是本工具要消灭的失败模式):
      ① 闭包: SQL 变量定义在**外层**函数, 被内层函数拿去 execute
      ② 追加式拼装: `sql += " AND ABS(...)"` (AugAssign, 不是 Assign)
      ③ 同一变量**多次**赋值: 要取最后一次, 不是第一次
    """
    name = node.id
    use_line = getattr(node, "lineno", 10 ** 9)
    # ① 函数作用域链 (内→外): 闭包变量也能找到
    for fn in enclosing_chain(node, parents):
        assigns = scope_assignments(fn, name)
        if not assigns:
            continue
        prior = [a for a in assigns if a[0] <= use_line]
        if not prior:
            # 赋值在使用点之后 → 该变量此刻还没被赋值 → 解析不了
            return ""
        last_set = -1
        for i, (ln, kind, val) in enumerate(prior):
            if kind == "set":
                last_set = i
        if last_set < 0:
            continue
        text = resolve_sql(prior[last_set][2], parents, module_assigns, depth + 1)
        # 最后一次赋值之后的 `+=` 追加也要拼上
        for (ln, kind, val) in prior[last_set + 1:]:
            if kind == "aug":
                extra = resolve_sql(val, parents, module_assigns, depth + 1)
                if extra:
                    text = (text + " " + extra) if text else extra
        return text
    # ② 再找**模块级常量** (老检查器漏这一档 → 静默跳过 = 漏检)
    if name in module_assigns:
        return resolve_sql(module_assigns[name], parents, module_assigns, depth + 1)
    return ""



# ── 过滤判定 ──────────────────────────────────────────────────────────────
def sql_filter_kind(sql: str) -> Tuple[str, str]:
    """只看 SQL 文本, 判断有没有可证明的量纲过滤。

    返回 (verdict, reason), verdict ∈ {"OK", "SUSPECT", "PARAM", "NONE"}:
        OK      —— 有可证明的过滤 (ABS(move_pct) 上界 / move_pct BETWEEN 界够小)
        SUSPECT —— 有过滤但方向/强度可疑 (只有下界 > N, 或 BETWEEN 上界过大)
        PARAM   —— 用了 ? 占位符, 需要外部阈值 (交由上层判定)
        NONE    —— 文本里没有过滤

    ⚠️ 方向很关键:
       - "move_pct > 90" 是**下界** → 9867% 的污染行照样进来 → 不能算过滤;
       - 裸 "move_pct <= 90" 只挡住**正向**污染, -9867% 仍会通过 → 也只能算弱证据;
       - 只有 "ABS(...move_pct...) <= N" 才是**双向**上界 → 真正可信。
    """
    # ① ABS(...) 包裹 —— 唯一真正双向可信的写法
    if RE_ABS_TEXT.search(sql):
        has_upper = False
        for m in RE_ABS_SITE.finditer(sql):
            if m.group(1) in ("<", "<="):
                has_upper = True
                # 阈值来源: 只有**字面数字**才算"已证实"。
                # ? / :max / @max / $max → 数值在别处 → 只能算 PARAM。
                val = m.group(2)
                thr_unknown = not re.fullmatch(r"-?\d+(?:\.\d+)?", val)
                bound = None
                if not thr_unknown:
                    bound = abs(float(val))
                break
        if has_upper and thr_unknown:
            return "PARAM", "SQL 内 ABS(...move_pct...) 上界, 但阈值不是字面数字"
        # ⚠️ 上界**数值**也要查: ABS(move_pct) <= 9999 拦不住 9867% 的污染
        #    → 阈值必须 <= MAX_PLAUSIBLE 才算可信过滤, 否则只是"看着像过滤"。
        if has_upper and bound is not None and bound > MAX_PLAUSIBLE:
            return "SUSPECT", (f"SQL 内 ABS(...move_pct...) 上界 {bound} 过大 "
                               f"(> {MAX_PLAUSIBLE}) —— 拦不住量纲污染")
        if has_upper:
            return "OK", "SQL 内 ABS(...move_pct...) 上界过滤"
        return "SUSPECT", "SQL 内有 ABS(...move_pct...), 但没找到上界比较 (方向可能是下界)"

    # ② BETWEEN lo AND hi —— 上界 hi 够小才拦得住污染
    m = RE_BETWEEN.search(sql)
    if m:
        hi = abs(float(m.group(2)))
        if hi <= MAX_PLAUSIBLE:
            return "OK", f"SQL 内 move_pct BETWEEN {m.group(1)} AND {m.group(2)}"
        return "SUSPECT", f"SQL 内 move_pct BETWEEN {m.group(1)} AND {m.group(2)} 上界过大"
    if RE_BETWEEN_Q.search(sql) or RE_BETWEEN_ANY.search(sql):
        return "PARAM", "SQL 用占位符比较 move_pct (BETWEEN)"

    # ③ 裸 move_pct 比较 —— 只挡正向污染, 负向 (-9867%) 仍会漏过 → 弱证据
    if RE_SITE.search(sql):
        return "SUSPECT", "SQL 内只有裸 move_pct 比较 (未取 ABS), 拦不住负向污染"
    if RE_CMP_Q.search(sql):
        return "PARAM", "SQL 用 ? 占位符比较 move_pct"

    return "NONE", "SQL 内无 move_pct 过滤"


def has_threshold(text: str) -> bool:
    return any(name in text for name in THRESH_NAMES)


def scope_has_filter(scope_src: str) -> bool:
    """作用域文本里有没有"看起来像"量纲过滤的写法。

    这是 v308_verify.sh 内联检查器规则③ (动态 where 拼装 → 同函数内出现过滤
    即放行) 的**原样口径**: 前三条与内联检查器逐字一致, 最后一条是正则口径,
    额外覆盖带表别名 / IFNULL 的写法 (cpm.move_pct) —— 只放宽不放严。

    ⚠️ 它只回答"有没有", 不回答"是不是这条 SQL 自己写的" → 直接用它当判定
    条件会把这条 SQL 自己的过滤写法回显成"别处有过滤" (见
    scope_has_filter_outside_sql 的说明)。
    """
    f = flat(scope_src)
    return (
        "abs(coalesce(move_pct,0))" in f
        or "abs(move_pct)" in f
        or "move_pctbetween" in f
        or bool(RE_ABS_TEXT.search(scope_src))
    )


# "过滤证据"的统一口径: 任何**针对 move_pct 的过滤尝试** ——
#   ABS(...move_pct...) / move_pct BETWEEN / 裸 move_pct 比较。
# 用来比较"作用域里的过滤证据"与"这条 SQL 自己的过滤证据" —— 只有前者**更多**,
# 才说明过滤是**别处**做的 (动态 where 拼装), 而不是这条 SQL 自己在打回旋镖。
RE_FILTER_EVIDENCE = re.compile(
    r"abs\s*\(\s*(?:coalesce|ifnull)?\s*\(?\s*(?:[A-Za-z_]\w*\.)?move_pct\b"
    r"|\bmove_pct\b\s+between\b"
    r"|\bmove_pct\b\s*(?:>=|<=|>|<|=)\s*[-+0-9?.@:$]",
    re.I)

# 只数 "abs(" 的出现次数 —— 用来判断作用域里有没有**这条 SQL 之外**的
# Python 侧 abs(...move_pct...) 过滤 (例如取回行之后再 abs 一下筛掉)。
RE_ABS_TOKEN = re.compile(r"\babs\s*\(", re.I)


def count_filter_evidence(text: str) -> int:
    """文本里"过滤证据"的条数 (针对 move_pct 的过滤尝试)。"""
    return len(RE_FILTER_EVIDENCE.findall(text or ""))


def count_abs_calls(text: str) -> int:
    """文本里 abs( 的出现次数。"""
    return len(RE_ABS_TOKEN.findall(text or ""))


def scope_has_filter_outside_sql(scope_src: str, sql: str) -> bool:
    """作用域里有没有**这条 SQL 之外**的过滤证据。

    为什么不能直接看整个作用域: 作用域源码里**必然包含这条 SQL 本身** → 用
    整段作用域去匹配, 这条 SQL 自己的过滤写法会把**自己**的问题"回显"成
    "别处有过滤" → 方向检查 (只有下界 / BETWEEN 上界过大) 全被掩盖 → 白检。

    做法: 比较两边的过滤证据条数 ——
      作用域条数 > 这条 SQL 条数 → 过滤是**别处**做的 (动态 where 拼装) → 放行
      否则 → 这条 SQL 就是全部证据 → 按 SQL 层的方向判定走 (SUSPECT)
    """
    if not scope_has_filter(scope_src):
        return False
    return count_filter_evidence(scope_src) > count_filter_evidence(sql)


def scope_has_python_side_filter(scope_src: str, sql: str) -> bool:
    """作用域里有没有**这条 SQL 之外**的 Python 侧 abs 过滤。

    与 scope_has_filter_outside_sql 同理 (作用域里必然含这条 SQL 自己),
    所以比较两边的 abs( 次数 —— 次数更多才说明**别处**还有一个 abs(...)。
    另外要求作用域里提到 move_pct, 避免把"跟 move_pct 无关的 abs"算进来。
    """
    if count_abs_calls(scope_src) <= count_abs_calls(sql):
        return False
    return "move_pct" in flat(scope_src)


# ── 显式豁免标记 ────────────────────────────────────────────────────────
# 有些 reader 是**故意**要看未过滤的原始数据的:
#   · 数据质量审计脚本 (要靠"脏数据"本身来发现问题)
#   · 修复前/修复后分布对比 (一旦过滤, 前后就恒等 → 验证功能直接失效)
# 给它们硬套 ABS(move_pct) <= 90 = 让这些脚本**永远看不到污染**。
#
# 所以提供**显式**豁免: 在 execute 上方写一行注释
#     # v308-audit-exempt: <为什么需要看原始数据>
# 审计器认这个标记 → 降级成 SUSPECT (不阻断, 但输出里会写明"已豁免")。
# 这是**可审计的放宽** (有标记、有理由、能 grep 出来), 不是偷偷放宽。
RE_EXEMPT = re.compile(r"v308-audit-exempt", re.I)


def has_exempt_marker(src_lines: List[str], lineno: int, lookback: int = 8) -> bool:
    """execute 调用上方 lookback 行内有没有显式豁免标记 (只认注释行)。"""
    for i in range(max(0, lineno - 1 - lookback), lineno):
        line = src_lines[i]
        if "#" in line and RE_EXEMPT.search(line):
            return True
    return False


def audit_file(path: pathlib.Path, src: str) -> List[dict]:
    findings: List[dict] = []
    try:
        tree = ast.parse(src)
    except SyntaxError as exc:
        return [{"file": str(path), "line": 0, "verdict": "ERROR",
                 "detail": f"语法错误: {exc}", "sql": "", "scope": ""}]

    parents = build_parents(tree)
    module_assigns = collect_assigns(tree)
    unresolvable_scopes = set()
    src_lines = src.splitlines()

    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and node.args):
            continue
        if not (isinstance(node.func, ast.Attribute) and node.func.attr == "execute"):
            continue

        scope_src, scope_desc = scope_of(node, parents, src)
        call_src = ast.get_source_segment(src, node) or ""
        flat_scope = flat(scope_src)

        sql = resolve_sql(node.args[0], parents, module_assigns)
        head = sql.strip().upper()

        if not sql:
            # ⚠️ SQL 文本**解析不出来** —— 老实现在这里直接 continue, 那条查询
            #    就被**静默跳过** (= 漏检)。而"解析不出来"恰恰是最可疑的情况
            #    (SQL 是运行时拼的 / 从别处传进来的)。
            #    处理: 作用域里如果同时出现了目标表 + move_pct, 就报 SUSPECT
            #    让人工看一眼; 报 SUSPECT 而不是 MISSING, 因为过滤可能做在
            #    Python 侧 (静态分析看不出来)。同一作用域只报一次, 免得刷屏。
            if (scope_src not in unresolvable_scopes
                    and any(t in flat_scope for t in TABLES)
                    and "move_pct" in flat_scope):
                unresolvable_scopes.add(scope_src)
                findings.append({
                    "file": str(path), "line": node.lineno, "verdict": "SUSPECT",
                    "detail": "SQL 文本无法静态解析 (可能是运行时拼装), 需人工确认",
                    "sql": " ".join(call_src.split())[:110], "scope": scope_desc,
                })
            continue

        if not (head.startswith("SELECT") or head.startswith("WITH")):
            continue                      # INSERT / DELETE / UPDATE 不读污染值
        sql_lower = sql.lower()
        if not any(t in sql_lower for t in TABLES):
            continue
        if "move_pct" not in sql_lower:
            continue                      # 只取 ts/count 的查询不受影响

        skind, sreason = sql_filter_kind(sql)

        # 判定顺序与 v308_verify.sh 内联检查器一致 (① SQL ③ 作用域 ② Python),
        # 另加两档放宽 → 对 4c 真正判过的那条查询, 本工具只会更宽不会更严
        # → 不会出现"4c 过 / 4d 挂"(阻断构建层面)。
        # 覆盖面比 4c 大得多 (4c 解析不出的 SQL / 只认 SELECT / 只查 3 个文件)
        # → 那些盲区 4d 会报 MISSING 并卡构建, 属预期。
        #
        # ⚠️ 作用域那两档必须用"回显感知"版本 (outside_sql / python_side):
        #    作用域源码里**必然含这条 SQL 自己** → 直接看整段作用域, 这条 SQL
        #    自己的过滤写法会把**自己**的问题回显成"别处有过滤" → 方向检查
        #    (只有下界 / BETWEEN 上界过大) 全被吞掉, 形同虚设。
        if skind == "OK":
            verdict, reason = "OK", sreason
        # ③ 动态 where 拼装: 过滤条件在**别的**查询里, 不在这条 SQL 文本内
        elif scope_has_filter_outside_sql(scope_src, sql):
            verdict, reason = "OK", f"{scope_desc} 内别处动态拼装了过滤条件"
        # ② Python 侧过滤: 作用域里同时出现 move_pct 和阈值常量
        elif "move_pct" in flat_scope and has_threshold(scope_src):
            verdict, reason = "OK", f"{scope_desc} 内 Python 侧按阈值过滤"
        # ④ ? 占位符: 看 execute 的实参里有没有把阈值传进去
        elif skind == "PARAM" and (has_threshold(call_src) or has_threshold(scope_src)):
            verdict, reason = "OK", "? 占位符 + 作用域内可见阈值常量"
        elif has_threshold(scope_src):
            verdict, reason = "OK", f"{scope_desc} 内出现阈值常量"
        # ②' Python 侧 abs 过滤: 作用域里**这条 SQL 之外**还有一处 abs(...)
        elif scope_has_python_side_filter(scope_src, sql):
            verdict, reason = "OK", f"{scope_desc} 内对 move_pct 取 ABS 后比较"
        # ⚠️ 下面两档都**不阻断构建** (只有 MISSING 才让 --strict exit 1):
        #    SUSPECT —— SQL 里有 move_pct 比较, 但方向/强度可疑 (只有下界,
        #               或 BETWEEN 上界过大) → "看着像过滤但拦不住污染";
        #    NONE   —— SQL 文本里连 move_pct 比较都没有 → 真的漏了。
        elif skind == "SUSPECT":
            verdict, reason = "SUSPECT", sreason
        elif skind == "PARAM":
            verdict, reason = "SUSPECT", "? 占位符比较 move_pct, 但看不到阈值来源"
        # 显式豁免 (诊断脚本刻意看原始数据) → 降级 SUSPECT, 仍不阻断构建
        elif has_exempt_marker(src_lines, node.lineno):
            verdict, reason = ("SUSPECT",
                               "显式豁免 (v308-audit-exempt): 诊断用途, 刻意看原始数据")
        else:
            verdict, reason = "MISSING", "没有找到量纲污染过滤"

        findings.append({
            "file": str(path),
            "line": node.lineno,
            "verdict": verdict,
            "detail": reason,
            "sql": " ".join(sql.split())[:110],
            "scope": scope_desc,
        })
    return findings



# ── 自检: 用固定 SQL 样本钉住判定逻辑 ───────────────────────────────────
# 判定规则改过好几轮 (方向 / 强度 / 占位符 / 命名参数), 用样本逐条钉死预期
# → 以后再改判定, 跑一次 --selftest 就知道有没有改坏。只测纯文本判定层,
# 不依赖任何文件 / 不需要数据库 / 不需要桥接在跑。
SELFTEST_CASES = (
    # (SQL, 期望 verdict)
    # ── OK: 双向上界 + 字面数字阈值
    ("SELECT move_pct FROM critical_price_moves WHERE ABS(COALESCE(move_pct,0)) <= 90", "OK"),
    ("SELECT move_pct FROM critical_price_moves WHERE ABS(move_pct) <= 90.0", "OK"),
    ("SELECT move_pct FROM critical_price_moves WHERE ABS(cpm.move_pct) <= 90", "OK"),
    ("SELECT move_pct FROM critical_price_moves WHERE ABS(IFNULL(move_pct,0)) < 90", "OK"),
    ("SELECT move_pct FROM critical_price_moves WHERE move_pct BETWEEN -90 AND 90", "OK"),
    # ── SUSPECT: 有比较但方向/强度拦不住污染
    ("SELECT move_pct FROM critical_price_moves WHERE ABS(move_pct) > 90", "SUSPECT"),
    ("SELECT move_pct FROM critical_price_moves WHERE move_pct > 90", "SUSPECT"),
    ("SELECT move_pct FROM critical_price_moves WHERE move_pct <= 90", "SUSPECT"),
    ("SELECT move_pct FROM critical_price_moves WHERE move_pct BETWEEN 0 AND 9999", "SUSPECT"),
    # ── SUSPECT: 上界"看着像过滤"但数值太大 → 拦不住 9867% 的污染
    ("SELECT move_pct FROM critical_price_moves WHERE ABS(move_pct) <= 9999", "SUSPECT"),
    ("SELECT move_pct FROM critical_price_moves WHERE ABS(move_pct) <= 1000.5", "SUSPECT"),
    # ── PARAM: 阈值是占位符/命名参数 → 数值在别处, 不能算"已证实"
    ("SELECT move_pct FROM critical_price_moves WHERE ABS(move_pct) <= ?", "PARAM"),
    ("SELECT move_pct FROM critical_price_moves WHERE ABS(move_pct) <= :limit", "PARAM"),
    ("SELECT move_pct FROM critical_price_moves WHERE ABS(move_pct) <= @limit", "PARAM"),
    ("SELECT move_pct FROM critical_price_moves WHERE move_pct <= ?", "PARAM"),
    ("SELECT move_pct FROM critical_price_moves WHERE move_pct BETWEEN ? AND ?", "PARAM"),
    # ── NONE: 根本没有 move_pct 比较 → 真的漏了
    ("SELECT ts, symbol FROM critical_price_moves", "NONE"),
    ("SELECT ts FROM critical_price_moves WHERE ABS(close_pct) <= 90", "NONE"),
)


def selftest() -> int:
    """跑一遍内置样本, 验证判定逻辑没被改坏。返回 exit code。"""
    failed = 0
    for sql, want in SELFTEST_CASES:
        got, why = sql_filter_kind(sql)
        if got != want:
            failed += 1
            print(f"SELFTEST_FAIL  期望={want} 实际={got}  ({why})")
            print(f"    SQL: {sql}")
    total = len(SELFTEST_CASES)
    if failed:
        print(f"selftest: {total - failed}/{total} passed —— {failed} 条不符预期")
        return 1
    print(f"selftest: {total}/{total} passed")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="v308 量纲污染 reader 全量审计")
    ap.add_argument("--root", default=None,
                    help="仓库根 (含 backend/src_python); 默认按脚本位置推断")
    ap.add_argument("--files", default=None,
                    help="逗号分隔文件名; 只扫这些 (相对 backend/src_python)")
    ap.add_argument("--strict", action="store_true",
                    help="存在 MISSING 时 exit 1")
    ap.add_argument("--show-suspect", action="store_true",
                    help="同时打印 SUSPECT 明细 (默认只打印计数)")
    ap.add_argument("--selftest", action="store_true",
                    help="只跑内置样本自检 (验证判定逻辑, 不扫任何文件)")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    here = pathlib.Path(__file__).resolve().parent
    root = pathlib.Path(args.root).resolve() if args.root else here.parent.parent
    base = root / "backend" / "src_python"

    if not base.is_dir():
        print(f"ERROR 找不到 {base}", file=sys.stderr)
        return 1

    if args.files:
        targets = [base / f.strip() for f in args.files.split(",") if f.strip()]
    else:
        targets = sorted(base.rglob("*.py"))

    all_findings: List[dict] = []
    for p in targets:
        if not p.exists():
            print(f"ERROR 文件不存在: {p}", file=sys.stderr)
            return 1
        try:
            src = p.read_text(encoding="utf-8")
        except Exception as exc:
            print(f"ERROR 读取失败 {p}: {exc}", file=sys.stderr)
            return 1
        all_findings.extend(audit_file(p, src))

    counts = {"OK": 0, "SUSPECT": 0, "MISSING": 0, "ERROR": 0}
    for f in all_findings:
        counts[f["verdict"]] = counts.get(f["verdict"], 0) + 1

    for f in all_findings:
        if f["verdict"] == "ERROR":
            print(f"ERROR {f['file']}: {f['detail']}")
        elif f["verdict"] == "MISSING":
            print(f"MISSING {f['file']}:{f['line']}  [{f['scope']}] {f['detail']}")
            print(f"         SQL: {f['sql']}")
        elif f["verdict"] == "SUSPECT" and args.show_suspect:
            print(f"SUSPECT {f['file']}:{f['line']}  [{f['scope']}] {f['detail']}")
            print(f"         SQL: {f['sql']}")

    print(f"scanned_files={len(targets)} readers={len(all_findings)} "
          f"OK={counts['OK']} SUSPECT={counts['SUSPECT']} "
          f"MISSING={counts['MISSING']} ERROR={counts['ERROR']}")

    if counts["ERROR"]:
        return 1
    if args.strict and counts["MISSING"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
