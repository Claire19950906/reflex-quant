#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""策略版本赛马场 CLI 包装。

用法（在 backend/ 下）：
    python3 scripts/run_variant_arena.py --days 1
    python3 scripts/run_variant_arena.py \
        --db runtime/backup/pre_prune_20261119_154634.db --days 1

真正的实现与口径说明见 `src_python/variant_arena.py`；本文件只负责把
`src_python` 加进 sys.path，保证从 backend/ 根目录跑时裸导入成立。
"""
from __future__ import annotations

import os
import sys

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_BACKEND, "src_python")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import variant_arena


if __name__ == "__main__":
    raise SystemExit(variant_arena.main())
