# -*- coding: utf-8 -*-
"""v421 全站产出体检 —— 一次跑完，出"哪些产出是坏的"清单。

为什么需要它（2026-11-17 事故）：
    线上真实存在三类"看起来检查过、实际用起来就错"的缺陷，人工点页面全都没发现：
      1) `/api/news/report` 恒 500 —— 函数被搬进 routers/news.py 了，api_server
         里的调用点还按老名字引用，`NameError` 只在第一次访问时炸；
      2) `/openapi.json` 恒 500 —— 一个 Pydantic 模型定义在函数体内，schema 生成
         时前向引用解析失败；
      3) `/metrics` 的 LLM 指标恒 0 —— `json` 没 import，`NameError` 被
         `except Exception: pass` 吞掉，接口返回 200、数字全是 0。
    三者共同点：**没有一条断言会失败**。所以这里把"全站产出"做成可执行的检查。

两类检查：
    · 静态（--static，import 进程内 app，不需要服务在跑）
        - openapi 能否生成（schema 前向引用类错误在这里必现）
        - 同一 (方法,路径) 是否被注册两次（后注册的会静默覆盖前一个）
        - HTTP 层（api_server.py + routers/*.py）有没有 undefined name
          （有 pyflakes 才查；没有就跳过并如实标注 skipped，不假装 ok）
        - 线上 /openapi.json 的路径集 与 进程内生成的是否一致（不一致=线上跑旧代码）
    · 动态（--live，对运行中的服务逐条 GET，串行）
        - 2xx 且 body 非空 → ok
        - 2xx 但 body 是空壳（{}、[]、items/rows/data 全空）→ empty
        - 4xx → client_error（多为参数缺失，不算产出坏）
        - 5xx → error（这是真坏）
        - 超时 → timeout
    串行是硬规矩：并发探活会把服务自己的线程池打满，测出来的是"探针自己造成的
    超时"，不是接口慢（2026-11-17 14 个接口"25s 超时"就是这么误判的）。

用法：
    python3 backend/scripts/site_audit.py --static
    python3 backend/scripts/site_audit.py --live --base http://127.0.0.1:8000
    python3 backend/scripts/site_audit.py --static --live --json /tmp/site_audit.json
退出码：有 5xx / schema 生成失败 / 重复注册 / 线上旧代码 → 1，否则 0。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from collections import Counter

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src_python")

# 会改状态/打外部接口的路径，动态扫描默认跳过（宁可少测，不能误写）。
SIDE_EFFECT = re.compile(
    r"(refresh|reload|resync|backfill|rebuild|reset|trigger|seed|init|cleanup|"
    r"prune|expire|sync|warm|cron|repair|migrate|delete|create|update|save|"
    r"apply|cancel|start|stop|enable|disable|push|send|import|export|run)",
    re.I,
)


def _empty_payload(obj) -> bool:
    """判断 2xx 响应是不是"空壳"。"""
    if obj is None:
        return True
    if isinstance(obj, (list, tuple, str)):
        return len(obj) == 0
    if isinstance(obj, dict):
        if not obj:
            return True
        for k in ("items", "rows", "data", "list", "results", "records", "news"):
            v = obj.get(k)
            if isinstance(v, (list, tuple, dict)) and len(v) == 0 and len(obj) <= 3:
                return True
        return False
    return False


def static_audit(static_json: str | None = None) -> dict:
    sys.path.insert(0, SRC)
    out: dict = {"ok": True, "checks": []}

    def add(name, status, detail):
        out["checks"].append({"check": name, "status": status, "detail": detail})
        if status in ("broken",):
            out["ok"] = False

    # 1) openapi 能否生成
    try:
        import api_server as a
        schema = a.app.openapi()
        n_paths, n_get = len(schema["paths"]), sum(
            1 for ops in schema["paths"].values() if "get" in ops
        )
        add("openapi 生成", "ok", f"{n_paths} 条路径 / {n_get} 条 GET")
    except Exception as exc:
        add("openapi 生成", "broken", f"{type(exc).__name__}: {str(exc)[:160]}")
        return out

    # 2) 重复注册
    c = Counter()
    for r in a.app.routes:
        if hasattr(r, "methods") and hasattr(r, "path"):
            for m in (r.methods or []):
                c[(m, r.path)] += 1
    dups = [f"{m} {p} ×{n}" for (m, p), n in c.items() if n > 1]
    add("重复注册", "broken" if dups else "ok",
        "; ".join(dups[:8]) if dups else "无同(方法,路径)注册两次")

    # 3) HTTP 层 undefined name
    try:
        import pyflakes.api
        from pyflakes.reporter import Reporter
        import io as _io

        buf = _io.StringIO()
        rep = Reporter(buf, buf)
        targets = [os.path.join(SRC, "api_server.py")]
        rd = os.path.join(SRC, "routers")
        targets += [os.path.join(rd, f) for f in sorted(os.listdir(rd)) if f.endswith(".py")]
        from pyflakes import api as fa
        for t in targets:
            fa.checkPath(t, rep)
        raw = [ln for ln in buf.getvalue().splitlines() if "undefined name" in ln]
        # 只把"运行时真会炸"的算 broken；仅出现在注解里的（PEP563 延迟求值）降级为 note。
        real, annot = [], []
        for ln in raw:
            try:
                f, line_no, _col, msg = ln.split(":", 3)
                src_line = open(f, encoding="utf-8", errors="ignore").read().split("\n")[int(line_no) - 1]
            except Exception:
                src_line = ""
            (annot if re.search(r"(->|:\s*[A-Za-z_\"]\w)", src_line) else real).append(ln)
        add("HTTP 层 undefined name", "broken" if real else "ok",
            (f"{len(real)} 处真会炸: " + "; ".join(real[:6]) if real else "0 处")
            + (f"（另 {len(annot)} 处仅在注解中，PEP563 延迟求值，安全）" if annot else ""))
    except ImportError:
        add("HTTP 层 undefined name", "unknown",
            "pyflakes 未安装（pip install --target /tmp/flakes pyflakes 后 PYTHONPATH=/tmp/flakes 再跑）")

    # 4) 线上是否跑旧代码
    if static_json and os.path.exists(static_json):
        try:
            live = json.load(open(static_json, encoding="utf-8")).get("paths", {})
            same = set(live) == set(schema["paths"])
            miss = len(set(schema["paths"]) - set(live))
            add("线上/进程内 路径一致", "ok" if same else "broken",
                "一致" if same else f"线上缺 {miss} 条（线上加载的是旧代码）")
        except Exception as exc:
            add("线上/进程内 路径一致", "unknown", f"读不到线上 openapi: {exc}")

    out["generated_at"] = __import__("time").strftime("%Y-%m-%d %H:%M:%S")
    out["paths"] = schema["paths"]
    return out


def live_audit(base: str, endpoints: list[str], timeout: float = 6.0) -> dict:
    rows = []
    skipped = []
    for p in endpoints:
        if SIDE_EFFECT.search(p):
            skipped.append(p)
            continue
        url = base.rstrip("/") + p
        rec = {"path": p}
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            t0 = __import__("time").time()
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read()
                rec["ms"] = round((__import__("time").time() - t0) * 1000)
                rec["status"] = r.status
                try:
                    obj = json.loads(body.decode("utf-8", "replace"))
                    rec["empty"] = _empty_payload(obj)
                except Exception:
                    rec["empty"] = len(body) < 40
                rec["kind"] = "empty" if rec["empty"] else "ok"
        except urllib.error.HTTPError as e:
            rec["kind"] = "error" if e.code >= 500 else "client_error"
            rec["status"] = e.code
            rec["detail"] = (e.read()[:200] or b"").decode("utf-8", "replace")
        except Exception as exc:
            msg = str(exc).lower()
            if "timed out" in msg:
                rec["kind"] = "timeout"
            elif ("connection refused" in msg or "connection reset" in msg
                  or "incompleteread" in msg):
                # 目标进程被重启/挂掉 ≠ 接口坏了。分开记，否则又是一次"测了个假结果"。
                rec["kind"] = "unreachable"
            else:
                rec["kind"] = "error"
            rec["status"] = 0
            rec["detail"] = f"{type(exc).__name__}: {str(exc)[:120]}"
        rows.append(rec)
    counts = Counter(r["kind"] for r in rows)
    return {"base": base, "rows": rows, "counts": dict(counts),
            "skipped": len(skipped), "skipped_paths": skipped}


def inproc_audit(endpoints: list[str], timeout: float = 20.0) -> dict:
    """进程内探活：TestClient 直接打 app，不占端口、不怕别人重启进程。

    为什么不用线上 HTTP：实测线上扫描期间服务被外部反复重启，扫到一半全是
    `Connection refused` —— 那是「服务没了」，不是「接口坏了」。工具必须能把
    「目标不可达」和「接口 5xx」分开报，否则又是一次"看起来测了"。
    """
    import signal
    import time as _time

    sys.path.insert(0, SRC)
    from fastapi.testclient import TestClient
    import api_server as a

    client = TestClient(a.app, raise_server_exceptions=False)
    rows, skipped = [], []
    for p in endpoints:
        if SIDE_EFFECT.search(p):
            skipped.append(p)
            continue
        rec = {"path": p}
        t0 = _time.time()

        def _alarm(_sig, _frm):
            raise TimeoutError("inproc timeout")

        old = signal.signal(signal.SIGALRM, _alarm)
        signal.setitimer(signal.ITIMER_REAL, timeout)
        try:
            r = client.get(p, headers={"Accept": "application/json"})
            rec["ms"] = round((_time.time() - t0) * 1000)
            rec["status"] = r.status_code
            if r.status_code >= 500:
                rec["kind"] = "error"
                rec["detail"] = r.text[:200]
            else:
                try:
                    obj = r.json()
                    rec["empty"] = _empty_payload(obj)
                except Exception:
                    rec["empty"] = len(r.text) < 40
                if r.status_code >= 400:
                    rec["kind"] = "client_error"
                else:
                    rec["kind"] = "empty" if rec["empty"] else "ok"
        except TimeoutError:
            rec.update(ms=round((_time.time() - t0) * 1000), status=0, kind="timeout",
                       detail=f">{timeout:.0f}s")
        except Exception as exc:
            rec.update(status=0, kind="error", detail=f"{type(exc).__name__}: {str(exc)[:120]}")
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, old)
        rows.append(rec)
    counts = Counter(r["kind"] for r in rows)
    return {"inproc": True, "rows": rows, "counts": dict(counts),
            "skipped": len(skipped), "skipped_paths": skipped}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--static", action="store_true")
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--inproc", action="store_true", help="进程内 TestClient 探活（推荐）")
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--timeout", type=float, default=6.0)
    ap.add_argument("--json", default="")
    ap.add_argument("--paths-file", default="", help="直接给 GET 端点清单文件（每行一条）")
    args = ap.parse_args()

    result: dict = {}
    bad = False

    if args.static:
        live_schema = os.path.join("/tmp", "live_openapi.json")
        if os.path.exists(live_schema):
            pass
        result["static"] = static_audit(live_schema if os.path.exists(live_schema) else None)
        for c in result["static"]["checks"]:
            print(f"[静态] {c['check']:<24} {c['status']:<8} {c['detail']}")
        bad = bad or not result["static"]["ok"]
        paths = result["static"].get("paths")
    else:
        paths = None

    if args.inproc:
        if paths is None:
            try:
                paths = json.load(open("/tmp/live_openapi.json", encoding="utf-8"))["paths"]
            except Exception:
                print("[进程内] 拿不到端点清单：先跑 --static")
                return 2
        gets = sorted(p for p, ops in paths.items() if "get" in ops)
        res = inproc_audit(gets, args.timeout)
        result["inproc"] = res
        cnt = res["counts"]
        print(f"[进程内] 探了 {len(res['rows'])} 条，跳过 {res['skipped']} 条（会改状态）")
        print(f"         ok={cnt.get('ok',0)} empty={cnt.get('empty',0)} "
              f"5xx={cnt.get('error',0)} 4xx={cnt.get('client_error',0)} "
              f"timeout={cnt.get('timeout',0)}")
        for r in res["rows"]:
            if r["kind"] in ("error", "timeout"):
                print(f"         ✗ {r['path']} → {r.get('status')} {str(r.get('detail',''))[:100]}")
        bad = bad or cnt.get("error", 0) > 0

    if args.live:
        if args.paths_file:
            with open(args.paths_file, encoding="utf-8") as f:
                paths = {ln.strip(): {"get": {}} for ln in f if ln.strip()}
        if paths is None:
            try:
                paths = json.load(open("/tmp/live_openapi.json", encoding="utf-8"))["paths"]
            except Exception:
                print("[动态] 拿不到端点清单：先跑 --static 让 /tmp/live_openapi.json 就位")
                return 2
        gets = sorted(p for p, ops in paths.items() if "get" in ops)
        result["live"] = live_audit(args.base, gets, args.timeout)
        cnt = result["live"]["counts"]
        print(f"[动态] 探了 {len(result['live']['rows'])} 条，跳过 {result['live']['skipped']} 条（会改状态）")
        print(f"        ok={cnt.get('ok',0)} empty={cnt.get('empty',0)} "
              f"5xx={cnt.get('error',0)} 4xx={cnt.get('client_error',0)} "
              f"timeout={cnt.get('timeout',0)} unreachable={cnt.get('unreachable',0)}")
        for r in result["live"]["rows"]:
            if r["kind"] in ("error", "timeout"):
                print(f"        ✗ {r['path']} → {r.get('status')} {r.get('detail','')[:110]}")
        bad = bad or cnt.get("error", 0) > 0

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"[输出] {args.json}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
