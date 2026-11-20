#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""审计：静默吞异常（`except: pass` / `except: return None`）。

起因（2026-11-07 生产事故）
--------------------------
ai_worker._expire_stale_news 里一行 `except Exception: return 0`，
把「数据库 CHECK 拒绝写入」这个异常吞得干干净净 —— 日志一行都没有，
功能静默死亡两天，排查成本极高。

这个脚本把「关键路径上吞异常且不打日志」的位置全部列出来。

判定标准（两个都满足才算问题）：
  1. except 块体**没有任何日志/打印/告警**
  2. 该 except 之前的上下文里有 **DB 写入**（execute/commit）或 **外部网络调用**

用法：
    python3 scripts/audit_silent_except.py
    python3 scripts/audit_silent_except.py --verbose   # 把所有 except 都列出来
"""
from __future__ import annotations

import argparse
import ast
import os
import re
import sys
from typing import Dict

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(BASE_DIR, "src_python")

# 关键模块（会真正影响生产数据的）；其它模块的静默 except 通常无害
CRITICAL = {
    "ai_worker.py", "db.py", "api_server.py", "realtime_filter.py",
    "llm_budget.py", "circuit_breaker.py", "judge_worker.py",
    "review_agent.py", "utils.py", "ingest.py", "event_status.py",
    "engine.py", "main.py", "webhook.py", "gate_replay_guard.py",
    "attribute_multi_factor.py", "structured_reflection.py",
    "notification_channels.py", "alerts.py",
}

#: 允许「全库扫描」模式：把所有模块都纳入检查。
#: v322 起默认全扫 —— 因为「静默吞异常」的代价太高（本次事故就是它藏了两天），
#: 而漏扫的代价（以为已经清干净）更高。
ALL_MODULES = True

#: Finder/iCloud 复制副本的文件名模式：" X.py"（X 为任意数字）
_DUP_RE = re.compile(r" \d+\.py$")

WRITE_HINTS = (
    ".execute(", "commit()", "requests.post", "requests.get",
    "urlopen(", "httpx.", "session.post", "session.get",
)


class Visitor(ast.NodeVisitor):
    def __init__(self, path, rel):
        self.path = path
        self.rel = rel
        self.hits = []
        self.all_except = []

    def _ctx_before(self, node):
        """节点之前的源码文本（用于判断是不是写/调用上下文）。"""
        try:
            with open(self.path, encoding="utf-8") as fh:
                src = fh.read()
            lines = src.splitlines()
            lo = max(0, node.lineno - 25)
            return "\n".join(lines[lo:node.lineno])
        except Exception:
            return ""

    @staticmethod
    def _is_cleanup_only(handler) -> bool:
        """except 体只是资源清理（close/flush/release 后 pass）→ 无害，不算吞错。

        典型：文件句柄关闭、锁释放、连接回收。这类失败不影响主数据正确性，
        保留 pass 是合理的；扫描器不把它们计入"静默吞错"。
        纯 `pass`（无任何清理调用）不算清理型，仍按静默吞错处理。
        """
        cleanup_calls = {"close", "flush", "release", "unlock", "detach", "cancel"}
        has_call = False
        for stmt in handler.body:
            if isinstance(stmt, ast.Pass):
                continue
            for sub in ast.walk(stmt):
                if isinstance(sub, ast.Call):
                    has_call = True
                    fn = sub.func
                    nm = fn.attr if isinstance(fn, ast.Attribute) else (
                        fn.id if isinstance(fn, ast.Name) else "")
                    if nm not in cleanup_calls:
                        return False
        return has_call

    def _is_silent(self, handler):
        body = handler.body
        if len(body) != 1:
            return False
        st = body[0]
        if isinstance(st, ast.Pass):
            return not self._is_cleanup_only(handler)
        if isinstance(st, ast.Return) and (st.value is None):
            return True
        if isinstance(st, ast.Expr) and isinstance(st.value, ast.Constant) \
                and st.value.value is Ellipsis:
            return True
        return False

    def _has_log(self, node):
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                fn = sub.func
                name = ""
                if isinstance(fn, ast.Attribute):
                    name = fn.attr
                elif isinstance(fn, ast.Name):
                    name = fn.id
                if name in ("print", "log", "logger", "warn", "warning",
                            "error", "info", "debug", "critical", "exception"):
                    return True
                if name.startswith("log_") or name.startswith("warn"):
                    return True
        return False

    @staticmethod
    def _body_has_write(body) -> bool:
        """try 体里是否有**真正的数据写入 / 外部调用**。

        只认两种（避免把 .get()/.read() 之类误判成写）：
          1. execute/executemany 的 SQL 字面量里含 INSERT/UPDATE/DELETE
          2. 外部网络调用（requests.post / urlopen / session.post …）
        """
        sql_words = ("insert", "update", "delete", "replace")
        for stmt in body:
            for sub in ast.walk(stmt):
                if not isinstance(sub, ast.Call):
                    continue
                fn = sub.func
                nm = fn.attr if isinstance(fn, ast.Attribute) else \
                    (fn.id if isinstance(fn, ast.Name) else "")
                # ① 写库
                if nm in ("execute", "executemany", "executescript"):
                    for a in sub.args:
                        sv = None
                        if isinstance(a, ast.Constant) and isinstance(a.value, str):
                            sv = a.value
                        elif isinstance(a, ast.JoinedStr):
                            sv = "".join(
                                v.value for v in a.values
                                if isinstance(v, ast.Constant)
                                and isinstance(v.value, str))
                        if sv and any(w in sv.lower() for w in sql_words):
                            return True
                # ② 外部网络
                if nm in ("post", "urlopen", "request") and \
                        nm != "request" or nm == "urlopen":
                    return True
                if nm == "post":
                    return True
        return False

    def visit_Try(self, node):
        risky = self._body_has_write(node.body)
        for h in node.handlers:
            silent = self._is_silent(h)
            logged = self._has_log(h)
            rec = {
                "line": h.lineno,
                "type": (ast.unparse(h.type) if h.type else "bare"),
                "silent": silent,
                "logged": logged,
                "risky": risky,
                "ctx_head": self._ctx_before(node).strip().splitlines()[-3:],
            }
            self.all_except.append(rec)
            if silent and risky and not logged:
                self.hits.append(rec)
        self.generic_visit(node)

    def visit_ExceptHandler(self, node):
        self.generic_visit(node)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--top", type=int, default=0,
                    help="输出全库「静默吞错（未打日志、非资源清理）」按文件 TOP N；不挡 CI。")
    ap.add_argument("--json", metavar="PATH", default="",
                    help="把全库静默吞错存量按文件明细写到机读 JSON（含写路径高危 hits）。")
    args = ap.parse_args()

    total_hits = 0
    files_with_hits = 0
    silent_by_file: Dict[str, int] = {}
    detail: Dict[str, Dict[str, object]] = {}
    for root, _dirs, files in os.walk(SRC):
        if "__pycache__" in root:
            continue
        for fn in sorted(files):
            if not fn.endswith(".py"):
                continue
            # 排除 Finder/iCloud 复制产生的 "* N.py" 死文件（" 2.py" / " 3.py" …）。
            # ⚠️ 注意两点：
            #   1. 文件名带空格 → Python 永远 import 不到，改它们没有意义
            #      （而且会让人误以为改生效了）。
            #   2. **iCloud/Finder 会在后台不断生成新的副本**（实测移走一批，
            #      十分钟后又冒出 " 3.py"，连 mtime 都是当天）→ 规则必须覆盖
            #      **任意数字**，不能只写死 " 2.py"。
            if _DUP_RE.search(fn):
                continue
            if not ALL_MODULES and fn not in CRITICAL:
                continue
            path = os.path.join(root, fn)
            rel = os.path.relpath(path, BASE_DIR)
            try:
                tree = ast.parse(open(path, encoding="utf-8").read(), filename=path)
            except SyntaxError:
                continue
            v = Visitor(path, rel)
            v.visit(tree)
            # 全库静默吞错存量统计：未打日志、且被判定为静默（含非写路径），
            # 供分批治理排优先级；写路径高危项仍在 hits 里单独报。
            n_silent = sum(1 for h in v.all_except if h["silent"] and not h["logged"])
            if n_silent:
                silent_by_file[rel] = silent_by_file.get(rel, 0) + n_silent
                detail[rel] = {
                    "silent": n_silent,
                    "write_path_hits": len(v.hits),
                    "lines": [h["line"] for h in v.all_except
                              if h["silent"] and not h["logged"]],
                }
            if not v.hits:
                if args.verbose and v.all_except:
                    print("— %s: %d 个 except（均已打日志或非写路径）"
                          % (rel, len(v.all_except)))
                continue
            files_with_hits += 1
            print("❌ %s —— %d 处「写路径上静默吞异常」" % (rel, len(v.hits)))
            for h in v.hits:
                total_hits += 1
                print("   L%-6d except %s%s"
                      % (h["line"], h["type"],
                         "   ← 上下文有写/调用，但异常被吞" if h["risky"] else ""))
                for c in h["ctx_head"]:
                    print("        | %s" % c.strip()[:110])
            print()

    if args.top > 0:
        ranked = sorted(silent_by_file.items(), key=lambda kv: -kv[1])[:args.top]
        print("=" * 70)
        print("全库静默吞错（未打日志/非清理型）按文件 TOP %d，合计 %d 处："
              % (args.top, sum(silent_by_file.values())))
        for rel, n in ranked:
            print("  %4d  %s" % (n, rel))

    if args.json:
        import json as _json
        payload = {
            "scope": "all" if ALL_MODULES else "critical",
            "write_path_hits": total_hits,
            "silent_total": sum(silent_by_file.values()),
            "by_file": [
                {"file": rel, **detail[rel]}
                for rel, _ in sorted(silent_by_file.items(), key=lambda kv: -kv[1])
            ],
        }
        try:
            with open(args.json, "w", encoding="utf-8") as fh:
                _json.dump(payload, fh, ensure_ascii=False, indent=2)
        except OSError:
            pass

    print("=" * 70)
    scope = "全库" if ALL_MODULES else "关键模块(%d 个)" % len(CRITICAL)
    print("%s 扫描完成，命中 %d 处" % (scope, total_hits))
    if total_hits:
        print("❌ 这些位置失败时**不留任何痕迹** —— 这类静默失效最难查。")
        return 1
    print("✅ 关键写路径上没有静默吞异常")
    return 0


if __name__ == "__main__":
    sys.exit(main())
