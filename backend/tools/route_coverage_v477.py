#!/usr/bin/env python3
"""v477 接口覆盖台账：561 条路由，逐条判断"页面上能不能看到"。

为什么需要：64 项能力 + 输入层已经上页，但后端仍有几百条路由只存在于接口层。
用户问"所有功能都展现了吗"时，答案必须是数字，不是感觉。

分类口径（全部基于可复核的证据，不做语义猜测）：
  shown    前端源码里出现了同一路径 → 直接调用
  alias    前端调用了"同尾两段"的另一个版本（v188/v189/v190/v291 世代别名）
  action   写操作（POST/PUT/DELETE/PATCH）—— 不是"看的功能"，是"做的事"
  internal admin/debug/cron/health/probe/backup 等运维面
  orphan   只读且没有任何前端痕迹 —— 真正"看不见"的部分

注意：前端存在路径动态拼接（如 `${API_BASE}/api/v188/${kind}`），字符串匹配会
把它们算成 alias/orphan。台账只用于定位缺口，不作为"确实没调用"的最终证据。

输出：
  docs/接口覆盖台账-2026-11-19.md
  frontend/public/api-coverage.json   （供 /review 的「接口台账」面板读取）

用法：cd backend && python3 tools/route_coverage_v477.py
"""
from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]          # 金融1.0/
BE = ROOT / "backend" / "src_python"
FE = ROOT / "frontend"
DOC = ROOT / "docs" / "接口覆盖台账-2026-11-19.md"
OUT = FE / "public" / "api-coverage.json"

OS_MARK = re.compile(r"admin|debug|metrics|cron|internal|_probe|backup|selftest|/health|/test")


def norm(p: str) -> str:
    return re.sub(r"\{[^}]+\}", "{x}", p).rstrip("/")


def collect_frontend(known: set[str]) -> set[str]:
    """收集前端实际请求的路径。

    三类写法都要接住，否则会把"已展示"误判成"没入口"：
      1) 字面量 `/api/xxx`                     —— 最老的一批
      2) 模板 `${API_BASE}/crowd/ranking`       —— API_BASE 自带 /api，字面量里没有 /api
      3) `apiGet('/xxx')` / `apiSend(...)`      —— helper 里再拼 API_BASE
    """
    out = set()
    for p in FE.rglob("*"):
        s = str(p)
        if p.suffix not in (".ts", ".tsx") or "node_modules" in s or ".next" in s:
            continue
        t = p.read_text(errors="ignore")
        for m in re.finditer(r"/api/[A-Za-z0-9_\-./%]+", t):
            out.add(norm(m.group(0).rstrip(".").split("?")[0]))
        for m in re.finditer(r"\$\{(?:API_BASE|BRIDGE_BASE)\}([A-Za-z0-9_\-./%${}]+)?", t):
            tail = (m.group(1) or "").strip("/")
            tail = re.split(r"[?$]", tail)[0]          # 去掉 ?qs / ${qs ? ...}
            if tail:
                out.add(norm("/api/" + tail))
        # 4) 常量数组里的裸路径 '/market/es' —— 只有能对上后端路由才收，避免把静态资源算进来
        for m in re.finditer(r"[\"'`](/[A-Za-z0-9_\-./%]{3,})[\"'`]", t):
            cand = "/api" + m.group(1)
            if cand in known or any(k.startswith(cand + "/") for k in known):
                out.add(norm(cand))
        for m in re.finditer(r"(?:usePoll|useApiPoll|useSWR|apiFetch|getJson)\w*\s*(?:<[^>]*>)?\(\s*[\"'`](/[^\"'`?]+)", t):
            tail = re.sub(r"\$\{[^}]+\}", "{x}", m.group(1).strip("/"))
            if tail and not tail.startswith("api/"):
                out.add(norm("/api/" + tail))
        for m in re.finditer(r"api(?:Get|Send|GetCached)\s*(?:<[^>]*>)?\(\s*[\"'`]([^\"'`]+)", t):
            tail = m.group(1).strip("/")
            tail = re.split(r"[?$]", tail)[0]
            tail = re.sub(r"\$\{[^}]+\}", "{x}", tail)
            if tail and not tail.startswith("api/"):
                out.add(norm("/api/" + tail))
    return out


def collect_routes() -> list[dict]:
    sys.path.insert(0, str(BE))
    os.environ.setdefault("TRIDENT_NO_AUTO_SETUP", "1")
    import bridge_server

    rows = []
    for r in bridge_server.app.routes:
        path = getattr(r, "path", "")
        if not path.startswith("/api"):
            continue
        for m in sorted(getattr(r, "methods", []) or []):
            if m in ("GET", "POST", "PUT", "DELETE", "PATCH"):
                rows.append({"m": m, "p": norm(path), "raw": path})
    return rows


def main() -> None:
    routes = collect_routes()
    known = {norm(r["p"]) for r in routes}
    fe = collect_frontend(known)

    fe_tails: dict[str, list[str]] = defaultdict(list)
    for p in fe:
        for n in (2, 1):
            fe_tails["/".join(p.split("/")[-n:])].append(p)

    items, counts = [], Counter()
    seen = set()
    for r in routes:
        key = (r["m"], r["p"])
        if key in seen:
            continue
        seen.add(key)
        cls = None
        if r["p"] in fe:
            cls = "shown"
        elif r["m"] != "GET":
            cls = "action"
        elif OS_MARK.search(r["p"]):
            cls = "internal"
        else:
            t2 = "/".join(r["p"].split("/")[-2:])
            t1 = r["p"].split("/")[-1]
            if fe_tails.get(t2) or fe_tails.get(t1):
                cls = "alias"
            else:
                cls = "orphan"
        counts[cls] += 1
        items.append({
            "path": r["p"], "method": r["m"], "class": cls,
            "module": "/" + "/".join(r["p"].split("/")[1:3]),
            "via": (fe_tails.get("/".join(r["p"].split("/")[-2:]))
                    or fe_tails.get(r["p"].split("/")[-1]) or [])[:2],
        })

    total = len(items)
    mods = []
    for mod, rows in sorted(defaultdict(list, {m: [i for i in items if i["module"] == m]
                                               for m in {i["module"] for i in items}}).items()):
        c = Counter(i["class"] for i in rows)
        mods.append({"module": mod, "total": len(rows), "shown": c["shown"],
                     "alias": c["alias"], "action": c["action"],
                     "internal": c["internal"], "orphan": c["orphan"]})
    mods.sort(key=lambda x: (-x["orphan"], -x["total"]))

    orphans = [i for i in items if i["class"] == "orphan"]
    payload = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "totals": {"routes": total, **counts},
        "modules": mods,
        "orphans": [{"path": i["path"], "method": i["method"], "module": i["module"]} for i in orphans],
        "notes": [
            "分类基于前端源码字符串匹配；动态拼接的路径可能被误判为 alias/orphan",
            "action 类（POST/PUT/DELETE）是写操作，不属于需要“看”的页面功能",
        ],
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1))

    lines = [
        "# 接口覆盖台账（2026-11-19）", "",
        f"自动生成于 {payload['generated_at']}，脚本 `backend/tools/route_coverage_v477.py`。", "",
        "## 总数", "",
        "| 分类 | 条数 | 含义 |", "|---|---|---|",
        f"| 路由 | {total} | 去重后的 方法+路径 |",
        f"| 已展示 shown | {counts['shown']} | 前端源码里有同一路径 |",
        f"| 别名 alias | {counts['alias']} | 前端调了同尾两段的另一版本 |",
        f"| 写操作 action | {counts['action']} | POST/PUT/DELETE，是“做事”不是“看” |",
        f"| 运维面 internal | {counts['internal']} | admin/debug/cron/health |",
        f"| **疑似未展示 orphan** | **{counts['orphan']}** | 只读且无前端痕迹 |", "",
        "## 按模块（只列 orphan > 0，前 20）", "",
        "| 模块 | 路由 | 已展示 | 别名 | 写 | 运维 | 疑似未展示 |", "|---|---|---|---|---|---|---|",
    ]
    for m in mods[:20]:
        lines.append(f"| `{m['module']}` | {m['total']} | {m['shown']} | {m['alias']} | "
                     f"{m['action']} | {m['internal']} | {m['orphan']} |")
    lines += ["", "## 口径提醒", "",
              "- 前端存在 `${API_BASE}/api/v188/${kind}` 这类动态拼接，字符串匹配会把它算成未引用；",
              "- 因此 orphan 只用于定位缺口，不等于“这些功能一定没被用”。",
              "- 写操作（如 `/api/exchange/stop_all`）本就不该有“展示页”，列出来是为了让账平。"]
    DOC.write_text("\n".join(lines))

    print(f"routes={total} shown={counts['shown']} alias={counts['alias']} "
          f"action={counts['action']} internal={counts['internal']} orphan={counts['orphan']}")
    print(f"-> {OUT}")
    print(f"-> {DOC}")


if __name__ == "__main__":
    main()
