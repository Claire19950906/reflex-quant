# -*- coding: utf-8 -*-
"""版本号路径 → 规范路径的别名层（只加别名，不删老路径）。

背景
----
仓库把「版本号」当命名空间用：106 条端点嵌在 /api/v1、/api/v186 … /api/v291
里，前端也只能跟着版本号调用（`/api/v291/stats`、`/api/v237/missed-signals`）。
逐条改名风险太高：剥掉前缀后能对上已有路径的只有 1 条，其余 78 条语义各不相同，
没法「合并」——所以本模块做的是**加一层规范名字**，而不是搬家：

* 每条 `/api/vNNN/xxx` 在路由表里追加一份 `/api/xxx`，指向**同一个 endpoint**
  （同一份代码、同一个口径、同一个依赖注入），老路径原样保留；
* 规范路径插在**原路由之后、catch-all 之前**（bridge 末尾有 /api/{full_path:path}
  兜底，顺序错了会被它先吞掉）；
* 规范路径默认不进 /docs（避免同一操作在 schema 里出现两次、operationId 冲突），
  映射关系由 `docs/archive/版本号命名空间冻结-2026-11-17.md` 与
  `backend/scripts/legacy_namespace_audit.py --aliases` 负责呈现。

用法（两个 app 各调一次）
------------------------
    from legacy_alias import add_canonical_aliases
    added = add_canonical_aliases(app)
"""
from __future__ import annotations

import re
from typing import List, Optional, Tuple

# /api/v1/xxx、/api/v291/xxx；只认 /api/ 之后紧跟的 v+数字
_VERSION_PATH_RE = re.compile(r"^/api/(v\d+)(/.*)$", re.IGNORECASE)


def canonical_path(path: str) -> Optional[str]:
    """把 /api/vNNN/xxx 变成 /api/xxx；不是版本化路径则返回 None。"""
    if not isinstance(path, str):
        return None
    m = _VERSION_PATH_RE.match(path)
    if not m:
        return None
    return "/api" + m.group(2)


def add_canonical_aliases(app, *, include_in_schema: bool = False) -> List[Tuple[str, str]]:
    """为 app 里所有版本化路由追加规范路径别名，返回 [(老路径, 规范路径)]。"""
    from fastapi.routing import APIRoute

    routes = app.router.routes
    existing_paths = {getattr(r, "path", "") for r in routes}

    added: List[Tuple[str, str]] = []
    i = 0
    while i < len(routes):
        route = routes[i]
        i += 1  # 先前进一位：后面插入的新路由不会再被本循环处理
        if not isinstance(route, APIRoute):
            continue  # Mount / WebSocketRoute / 静态资源
        old_path = getattr(route, "path", "")
        new_path = canonical_path(old_path)
        if not new_path or new_path == old_path:
            continue
        # 下划线开头的子路径是**蜜罐/调试**路径（honeypot.HONEYPOT_ROUTES 里的
        # /api/v1/_debug、_internal/*、_test/connection）——它们不是真功能，
        # 换个名字注册等于给爬虫多发一把钥匙，直接跳过。
        if new_path[len("/api"):].startswith("/_"):
            continue
        # 规范路径已被真实实现占用 → 保留真实实现，不再造别名（避免二选一歧义）
        if new_path in existing_paths:
            continue

        methods = sorted(m for m in (getattr(route, "methods", None) or set()) if m != "HEAD")
        if not methods:
            continue
        try:
            alias = APIRoute(
                path=new_path,
                endpoint=route.endpoint,
                methods=methods,
                name=f"{getattr(route, 'name', 'endpoint')}__canonical",
                response_model=getattr(route, "response_model", None),
                responses=getattr(route, "responses", None),
                dependencies=getattr(route, "dependencies", None),
                tags=getattr(route, "tags", None),
                include_in_schema=include_in_schema,
                deprecated=False,
            )
        except Exception as exc:  # pragma: no cover - 单条失败不影响其它别名
            print(
                f"[legacy_alias] ⚠️ 别名失败 {old_path} -> {new_path}: "
                f"{type(exc).__name__}: {str(exc)[:100]}",
                flush=True,
            )
            continue

        routes.insert(i, alias)  # 紧跟原路由，保证排在 catch-all 之前
        i += 1
        existing_paths.add(new_path)
        added.append((old_path, new_path))
    return added
