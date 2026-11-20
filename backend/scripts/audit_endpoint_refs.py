#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只读复核：零引用候选补查【定义文件之外】的后端/脚本/cron/前端引用，分类标注。不删路由。"""
import json, re, pathlib, collections

# 脚本位于 backend/scripts/，项目根为上两级
ROOT = pathlib.Path(__file__).resolve().parents[2]
BE = ROOT/"backend"; SRC = BE/"src_python"; SCR = BE/"scripts"; CRON = BE/"cron"; FE = ROOT/"frontend"
inv = json.load(open(ROOT/"docs/ops/endpoint-inventory-2026-11-19.json"))

def files(root):
    if not root.exists(): return []
    out=[]
    for f in root.rglob("*"):
        if f.is_file() and f.suffix in (".py",".sh",".plist",".ts",".tsx",".js",".jsx"):
            if "node_modules" in f.parts or "__pycache__" in f.parts: continue
            out.append(f)
    return out

be_files = files(SRC)+files(SCR)+files(CRON)
fe_files = files(FE)

def rel(f): return str(f.relative_to(BE))

def has_ref(flist, path, self_file=None):
    cand = path.rstrip("/").replace("{}", "[A-Za-z0-9_.-]+")
    rx = re.compile(cand)
    for f in flist:
        if self_file and rel(f)==self_file: continue
        try: t=f.read_text(encoding="utf-8",errors="ignore")
        except OSError: continue
        if rx.search(t): return True
    return False

# 第二轮：模块级函数复用核对（除 api_server 挂载外，是否有 worker/cron/其他模块 import 该 router）
def module_reusers(file):
    mod = pathlib.Path(file).stem
    rx = re.compile(rf"(from routers\.{mod}|import {mod}|routers\.{mod})")
    out=[]
    for f in be_files:
        if f.relative_to(BE).as_posix() == pathlib.Path(file).as_posix(): continue
        try: t=f.read_text(encoding="utf-8",errors="ignore")
        except OSError: continue
        if rx.search(t):
            name=pathlib.Path(f).name
            if name!="api_server.py": out.append(name)
    return sorted(set(out))

rows=[]
for r in inv["unreferenced"]:
    method,path,file = r["method"], r["path"], r["file"]
    if file.endswith("selftest_v309_endpoints.py"):
        cat="test_file"
    elif "{full_path" in path or path in ("/api/{}","/{}","/","/api/...","/..."):
        cat="catch_all"
    elif "{}" in path and "data_poison.py" in file:
        cat="docstring_example"
    else:
        fe = has_ref(fe_files, path)
        be = has_ref(be_files, path, file)
        if fe and be: cat="frontend+internal"
        elif fe: cat="frontend_only"
        elif be: cat="internal_only"
        else: cat="truly_candidate"
    entry={"method":method,"path":path,"file":file,"category":cat}
    if cat=="truly_candidate":
        entry["module_reusers"]=module_reusers(file)
        entry["reuse_class"]="reused_by_worker" if entry["module_reusers"] else "server_only"
    rows.append(entry)

c=collections.Counter(x["category"] for x in rows)
print("== 复核分类 ==", dict(c))
t=[x for x in rows if x["category"]=="truly_candidate"]
print("真候选复用分桶:", dict(collections.Counter(x["reuse_class"] for x in t)))
for cat in ("truly_candidate","frontend_only","internal_only","frontend+internal","catch_all","test_file","docstring_example"):
    xs=[x for x in rows if x["category"]==cat]
    print(f"\n== {cat} ({len(xs)}) ==")
    for x in xs:
        print(f"  {x['method']:5} {x['path']:42} {x['file'].split('/')[-1]}")
json.dump(rows, open(BE/"scripts/endpoint-ref-audit.json","w",encoding="utf-8"), ensure_ascii=False, indent=2)
