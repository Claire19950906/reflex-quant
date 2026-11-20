# -*- coding: utf-8 -*-
"""版本号路径别名回归测试（P3 命名治理收尾）。

命题（这几条必须被锁死，否则「前端改用规范路径」会静默失效）:
    1. **每条版本化端点都有规范名** —— /api/v186/experiences → /api/experiences，
       指向**同一个 endpoint 对象**（不是另抄一份代码）；
    2. **蜜罐不别名** —— /api/v1/_debug、_internal/*、_test/connection 是给爬虫
       准备的陷阱，换个名字注册等于多发一把钥匙；
    3. **老路径不删** —— 版本化路由仍然在表里（冻结期，外部调用方还在用）；
    4. **顺序** —— 8100 的别名必须排在 /api/{full_path:path} catch-all 之前，
       否则会被兜底先吞掉（这是 P1 踩过的坑）。
"""
from __future__ import annotations

import os
import re
import sys

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import api_server
import bridge_server

_VERSION_RE = re.compile(r"^/api/(v\d+)(/.*)$")
_HONEYPOT_SUBPATH = ("/_debug", "/_internal", "/_test")


def _rs(route):
    return getattr(route, "methods", None) or set()


def test_api_every_versioned_route_has_canonical_alias():
    aliases = dict(getattr(api_server, "_CANON_ALIASES", []))
    assert aliases, "api_server 没有安装别名（legacy_alias 未生效）"

    paths = [getattr(r, "path", "") for r in api_server.app.routes]
    missing = []
    for path in paths:
        m = _VERSION_RE.match(path)
        if not m:
            continue
        canon = "/api" + m.group(2)
        if canon[len("/api"):].startswith(_HONEYPOT_SUBPATH):
            continue
        if canon in paths:      # 真实实现已占用，保留它
            continue
        if path not in aliases:
            missing.append(path)
    assert not missing, f"这些版本化端点没有规范名: {missing}"


def test_alias_shares_the_same_endpoint_object():
    """别名不是复制粘贴的副本，必须指向原 endpoint。"""
    by_path = {}
    for r in api_server.app.routes:
        p = getattr(r, "path", "")
        if p:
            by_path.setdefault(p, []).append(r)

    checked = 0
    for old, new in getattr(api_server, "_CANON_ALIASES", []):
        for r_old in by_path.get(old, []):
            endpoints_new = {getattr(r, "endpoint", None) for r in by_path.get(new, [])}
            assert getattr(r_old, "endpoint", None) in endpoints_new, (
                f"{old} 与 {new} 不是同一个 endpoint（说明抄了一份新实现）"
            )
            checked += 1
    assert checked >= 60, f"只校验了 {checked} 条，样本太少"


def test_honeypot_paths_are_not_aliased():
    paths = {getattr(r, "path", "") for r in api_server.app.routes}
    for hp in ("/api/_debug", "/api/_internal/strategy", "/api/_internal/weights",
               "/api/_test/connection"):
        assert hp not in paths, f"蜜罐路径被别名注册了: {hp}"


def test_legacy_paths_are_still_registered():
    """冻结期：老路径不许消失（外部调用方 + 前端旧缓存还在打）。"""
    n_versioned = sum(1 for r in api_server.app.routes
                      if _VERSION_RE.match(getattr(r, "path", "")))
    assert n_versioned == 79, f"版本化路由数量变了: {n_versioned}（应从 79 起只减不增）"


def test_bridge_aliases_registered_before_catch_all():
    aliases = getattr(bridge_server, "_BRIDGE_CANON_ALIASES", [])
    assert len(aliases) >= 20, f"bridge 别名太少: {len(aliases)}"

    paths = [getattr(r, "path", "") for r in bridge_server.app.routes]
    catch_all = paths.index("/api/{full_path:path}")
    for _old, new in aliases:
        assert new in paths, f"bridge 别名没进路由表: {new}"
        assert paths.index(new) < catch_all, f"bridge 别名排在 catch-all 之后，会被吞掉: {new}"


def test_bridge_canonical_resolves_same_endpoint():
    by_path = {}
    for r in bridge_server.app.routes:
        by_path.setdefault(getattr(r, "path", ""), []).append(r)
    for old, new in getattr(bridge_server, "_BRIDGE_CANON_ALIASES", []):
        for r_old in by_path.get(old, []):
            assert getattr(r_old, "endpoint", None) in {
                getattr(r, "endpoint", None) for r in by_path.get(new, [])
            }, f"{old} / {new} endpoint 不一致"
