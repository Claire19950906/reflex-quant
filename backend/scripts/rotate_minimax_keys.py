#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一次性工具：把 .env 里的 MiniMax 各槽位统一指向指定 key。

用法:
    python3 scripts/rotate_minimax_keys.py <key>

行为:
    · 先备份 .env → .env.bak.<时间戳>
    · 把所有 MINIMAX_*_KEY 槽位改成同一个 key
    · 被替换掉的旧 key 以注释形式保留在文件末尾，方便回滚

为什么全部指向同一个 key:
    现在的配置是「账户1(429) + 账户2(401)」—— 两个槽位都不可用，
    引擎表现为 pool-blocked，看起来像「随机抽风」。
    只有一个 key 可用时，全指向它至少行为一致；等账户1 充值后
    再把 MINIMAX_API_KEY / MINIMAX_MAIN_KEY 单独切回去即可。
"""
import re
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"

KEY_SLOTS = [
    "MINIMAX_API_KEY",
    "MINIMAX2_API_KEY",
    "MINIMAX_MAIN_KEY",
    "MINIMAX_GATE_KEY",
    "MINIMAX_ANOMALY_KEY",
    "MINIMAX_REFLECT_KEY",
    "MINIMAX_PEER_KEY",
    "MINIMAX_PEER2_KEY",
]


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    new_key = sys.argv[1].strip()
    if not new_key.startswith("sk-"):
        print("❌ key 格式不对（应以 sk- 开头）")
        return 2

    if not ENV_FILE.exists():
        print("❌ 找不到 %s" % ENV_FILE)
        return 2

    # 1. 备份
    bak = ENV_FILE.with_suffix(".env.bak.%d" % int(time.time()))
    shutil.copy2(ENV_FILE, bak)
    print("✅ 已备份 → %s" % bak)

    # 2. 收集旧值
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
    old_keys = {}
    out = []
    for line in lines:
        m = re.match(r"^(MINIMAX[A-Z0-9_]*_KEY)\s*=\s*(\S+)\s*$", line)
        if m and m.group(1) in KEY_SLOTS:
            old_keys.setdefault(m.group(2), []).append(m.group(1))
            out.append("%s=%s" % (m.group(1), new_key))
        else:
            out.append(line)

    # 3. 末尾追加被换下的旧 key（注释形式，便于回滚）
    if old_keys:
        out.append("")
        out.append("# ── 2026-11-04 key 轮换：以下为被换下的旧 key（注释状态，需要时手动改回） ──")
        for old, slots in old_keys.items():
            out.append("# %s=%s   # 原用于: %s" % (slots[0], old, ",".join(slots)))

    ENV_FILE.write_text("\n".join(out) + "\n", encoding="utf-8")
    print("✅ 已更新 %d 个槽位" % len(KEY_SLOTS))
    for old, slots in old_keys.items():
        print("   旧 key %s…%s (%s) 已注释留存" % (old[:14], old[-4:], ",".join(slots)))
    print("\n⚠️  记得重启常驻服务让新 key 生效：")
    print("    launchctl kickstart -k gui/%d/com.trident.engine" % __import__("os").getuid())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
