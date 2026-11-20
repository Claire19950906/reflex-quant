# -*- coding: utf-8 -*-
"""打包部署包 strategy_package_vN.zip（项目根，版本号自动递增）。

用法：python -X utf8 scripts/make_package.py
排除：node_modules / .next / __pycache__ / tests / *.db / *.bak / runtime / 调试脚本 / 旧部署包。
例外：backend/seed_config.db（只含配置类表：品种规则/经验/因果链/过滤词），随包携带，
      部署后用 deploy/restore_seed.py 导入 —— 否则新环境提示库全空。

版本号不再手写：扫项目根已有的 strategy_package_vN.zip 取最大 N + 1。
（此前硬编码 OUT 容易忘记改，实测出现过"包里的代码比磁盘旧一次修复"的情况。）
"""
import os
import re
import subprocess
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 项目根（scripts → 上一级）

_PKG_RE = re.compile(r"^strategy_package_v(\d+)\.zip$", re.IGNORECASE)


def _next_version() -> int:
    """扫项目根已有包，返回下一个可用版本号（无包时为 1）。"""
    top = 0
    try:
        for name in os.listdir(ROOT):
            m = _PKG_RE.match(name)
            if m:
                top = max(top, int(m.group(1)))
    except OSError:
        pass
    return top + 1


VERSION = _next_version()
OUT = os.path.join(ROOT, f"strategy_package_v{VERSION}.zip")


def refresh_seed() -> None:
    """打包前自动导出配置种子库，避免包内种子过期。"""
    script = os.path.join(ROOT, "scripts", "export_seed_config.py")
    if not os.path.exists(script):
        print("（未找到 export_seed_config.py，跳过种子库导出）")
        return
    try:
        r = subprocess.run(
            [sys.executable, "-X", "utf8", script],
            cwd=ROOT, capture_output=True, text=True, timeout=120,
            encoding="utf-8", errors="replace",  # Windows 默认 GBK 解码中文输出会抛 UnicodeDecodeError
        )
        tail = (r.stdout or "").strip().splitlines()
        print("（种子库已刷新）" + (tail[-1] if tail else ""))
        if r.returncode != 0:
            print("（种子库导出失败，沿用现有文件）")
    except Exception as exc:
        print("（种子库导出异常：%s，沿用现有文件）" % type(exc).__name__)


FILES = [
    ".env.example", "AGENTS.md", "README.md", ".gitignore",
    "backend/.env", "backend/init_db.py", "backend/README.md",
    "backend/requirements.txt", "backend/requirements-dev.txt",
    # 配置种子库：只含配置类表（品种规则/经验/因果链/过滤词），随包携带供 restore_seed.py 导入
    "backend/seed_config.db",
    "backend/src_python",
    "dashboard",
    "deploy",
    "docs",
    "frontend",
    "scripts",
]

EXCLUDE_NAMES = {"__pycache__", "node_modules", ".next", "tests", "runtime", "uploads", "dist", "news_exports", "backups", ".verify_out"}
EXCLUDE_EXT = {".pyc", ".db", ".bak", ".log", ".tmp", ".tsbuildinfo"}
EXCLUDE_FILES = {
    "backend/src_python/_md2docx.py",
}

ROOT_EXCLUDE = {
    "strategy_package_v2.zip", "strategy_package_v3.zip", "strategy_package_v6.zip",
    "strategy_package_v7.zip", "strategy_package_v8.zip", "strategy_package_v9.zip",
    "strategy_package_v10.zip", "strategy_package_v11.zip", "strategy_package_v12.zip",
    "strategy_package_v13.zip", "strategy_package_v14.zip", "strategy_package_v15.zip",
    "strategy_package_v16.zip", "strategy_package_v17.zip", "strategy_package_v18.zip",
    "strategy_package_v51.zip", "strategy_package_v52.zip",
    "frontend_font_patch.zip", "frontend_fred_patch.zip",
    "api_probe.log", "api_probe8010.log", "debug-cors-websocket.md",
    "NewsTrading.py", "_probe_cand.py", "_probe_cleanj10b.py", "_probe_fixpend.py",
    "_probe_j10test.py", "_probe_price_sources.py", "_probe_state2.py",
    "_py_libs",
}


def should_skip(rel_path: str) -> bool:
    parts = rel_path.replace("\\", "/").split("/")
    if any(p in EXCLUDE_NAMES for p in parts):
        return True
    # 旧部署包：按模式排除（此前是 130 多行硬编码文件名，加新版容易漏）
    if _PKG_RE.match(os.path.basename(rel_path)):
        return True
    if rel_path in ROOT_EXCLUDE or rel_path in EXCLUDE_FILES:
        return True
    # 白名单：配置种子库要随包携带（提示库的品种规则/经验/因果链/过滤词在 DB 里，
    # 而生产库 *.db 因含交易数据被排除 → 导出成只含配置表的 seed_config.db 带上）。
    if os.path.basename(rel_path) == "seed_config.db":
        return False
    ext = os.path.splitext(rel_path)[1].lower()
    if ext in EXCLUDE_EXT:
        return True
    base = os.path.basename(rel_path)
    if base.startswith("_probe") or base.startswith("_chk") or base.startswith("_test_") or base.startswith("_dbg"):
        return True
    return False


refresh_seed()

added = 0
with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as z:
    for entry in FILES:
        src = os.path.join(ROOT, entry)
        if not os.path.exists(src):
            print(f"[skip] 不存在: {entry}")
            continue
        if os.path.isfile(src):
            if not should_skip(entry):
                z.write(src, entry)
                added += 1
            continue
        for dirpath, dirnames, filenames in os.walk(src):
            dirnames[:] = [d for d in dirnames if d not in EXCLUDE_NAMES]
            for fn in filenames:
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, ROOT).replace("\\", "/")
                if should_skip(rel):
                    continue
                z.write(full, rel)
                added += 1

print(f"完成: {OUT} | 文件数 {added}")
print(f"大小: {os.path.getsize(OUT) / 1024 / 1024:.2f} MB")
































