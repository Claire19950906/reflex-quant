#!/usr/bin/env python3
"""deps_audit.py — requirements.txt ↔ 当前环境实装版本的一致性审计

为什么需要：
  requirements.txt 用的是宽松区间（`fastapi>=0.115.0`），机器上装了什么没人核。
  2026-11-16 实测：18 个直接依赖只装了 9 个，`openai` / `httpx` / `xlsxwriter`
  在 api_server 里是**函数内懒加载**（import 时不报错，调用到那个端点才 ImportError），
  所以「服务能起来」不代表「依赖是齐的」。这个脚本就是把这种漂移变成退出码。

只读：不装包、不改环境、不连网络、不碰数据库。

用法：
  python3 backend/scripts/deps_audit.py                # 人类可读
  python3 backend/scripts/deps_audit.py --json         # 机器可读
  python3 backend/scripts/deps_audit.py --strict       # 版本不满足也算失败（默认只算缺失）
  python3 backend/scripts/deps_audit.py -f backend/requirements-dev.txt

退出码：
  0 全部满足；1 有缺失/不满足（可选依赖缺失不算）；2 参数或文件问题
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from importlib import metadata
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# PEP 508 名称：字母数字 + . - _
_NAME_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)")


def parse_requirements(path: Path) -> list[tuple[str, str]]:
    """返回 [(包名, 版本约束)]，忽略注释/空行/-r 之类。"""
    out: list[tuple[str, str]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        m = _NAME_RE.match(line)
        if not m:
            continue
        name = m.group(1)
        spec = line[len(name):].strip()
        # 去掉 extras（uvicorn[standard]>=0.30.0 → >=0.30.0），extras 不影响版本判定
        if spec.startswith("["):
            spec = spec.split("]", 1)[1].strip() if "]" in spec else ""
        out.append((name, spec))
    return out


def _norm(spec: str) -> list[str]:
    """把 `>=1.2.3,<2` 归一成 ['>=1.2.3','<2']。"""
    return [s for s in re.split(r"\s*,\s*", spec.strip("[] ")) if s]


def _cmp(v1: str, op: str, v2: str) -> bool:
    """语义化比较（够用版）：按数字段/字母段切分后逐段比。"""
    def key(v: str) -> list[tuple[int, object]]:
        parts = re.findall(r"\d+|[A-Za-z]+", v)
        return [(0, int(p)) if p.isdigit() else (1, p) for p in parts]

    a, b = key(v1), key(v2)
    if op == "==":
        return a == b
    if op == "!=":
        return a != b
    for x, y in zip(a, b):
        if x == y:
            continue
        return x < y if op == "<" else x > y
    # 前缀相同：短的那个更小
    return (len(a) < len(b)) if op == "<" else (len(a) > len(b))


def satisfies(installed: str, spec: str) -> tuple[bool, str]:
    """检查已装版本是否满足 requirements 的约束串。"""
    if not spec:
        return True, ""
    for token in _norm(spec.replace("~=", ">=")):
        m = re.match(r"^(==|!=|>=|<=|>|<)\s*([\w.\-+]+)$", token)
        if not m:
            continue  # `~=1.2` 之类拆不干净的先放过
        op, want = m.group(1), m.group(2)
        # 版本尾部通配 `==1.2.*`
        if want.endswith(".*"):
            ok = installed.startswith(want[:-2] + ".")
        else:
            ok = _cmp(installed, op, want)
        if not ok:
            return False, f"{installed} 不满足 {op}{want}"
    return True, ""


def audit(path: Path, optional: set[str] | None = None) -> dict:
    """optional: 允许缺失的包名（来自 requirements-optional.txt），缺了只报 warn 不算失败。"""
    optional = {n.lower().replace("_", "-") for n in (optional or set())}
    rows, missing, mismatch, skipped = [], [], [], []
    for name, spec in parse_requirements(path):
        try:
            ver = metadata.version(name)
        except metadata.PackageNotFoundError:
            if name.lower().replace("_", "-") in optional:
                rows.append({"name": name, "spec": spec, "installed": None,
                             "status": "OPTIONAL", "detail": "可选依赖，未装不影响主链路"})
                skipped.append(name)
                continue
            rows.append({"name": name, "spec": spec, "installed": None, "status": "MISSING"})
            missing.append(name)
            continue
        ok, why = satisfies(ver, spec)
        status = "OK" if ok else "VERSION"
        rows.append({"name": name, "spec": spec, "installed": ver, "status": status, "detail": why})
        if not ok:
            mismatch.append(f"{name}: {why}")
    return {
        "file": str(path),
        "python": sys.version.split()[0],
        "declared": len(rows),
        "installed": len(rows) - len(missing),
        "missing": missing,
        "optional_missing": skipped,
        "mismatch": mismatch,
        "rows": rows,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="requirements ↔ 实装版本一致性审计")
    ap.add_argument("-f", "--file", default="backend/requirements.txt")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--strict", action="store_true", help="版本不满足也返回非零")
    ap.add_argument("--optional-file", default="backend/requirements-optional.txt",
                    help="可选依赖清单；这里的包缺失只提示，不计失败")
    args = ap.parse_args()

    path = Path(args.file)
    if not path.is_absolute():
        path = REPO_ROOT / path
    if not path.exists():
        print(f"找不到 {path}", file=sys.stderr)
        return 2

    opt_path = Path(args.optional_file)
    if not opt_path.is_absolute():
        opt_path = REPO_ROOT / opt_path
    optional = {n for n, _ in parse_requirements(opt_path)} if opt_path.exists() else set()
    res = audit(path, optional)
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print(f"文件: {res['file']}")
        print(f"解释器: Python {res['python']}")
        print(f"声明 {res['declared']} 个，实装 {res['installed']} 个\n")
        for r in res["rows"]:
            mark = {"OK": "✓", "MISSING": "✗ 未安装", "VERSION": "△ 版本不符",
                    "OPTIONAL": "○ 可选未装"}[r["status"]]
            detail = f"  ({r.get('detail')})" if r.get("detail") else ""
            print(f"  {mark:<10} {r['name']:<22} 要求 {r['spec'] or '任意':<18} 实装 {r['installed'] or '-':<12}{detail}")
        if res["missing"]:
            print(f"\n未安装: {', '.join(res['missing'])}")
        if res.get("optional_missing"):
            print(f"可选未装（不计失败）: {', '.join(res['optional_missing'])}")
        if res["mismatch"]:
            print(f"版本不符: {'; '.join(res['mismatch'])}")

    bad = bool(res["missing"]) or (args.strict and bool(res["mismatch"]))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
