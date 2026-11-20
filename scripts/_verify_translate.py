# -*- coding: utf-8 -*-
"""验证：翻译候选链加 MiniMax 后，OpenRouter 挂时仍能翻译"""
import os, sys, time

BASE = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))
sys.path.insert(0, os.path.join(BASE, "src_python"))
from engine.webhook import _translate_title_sync

text = "Jim Cramer Wants Nvidia to Buy Back $500 Billion. Can Apple's Playbook Work?"
t0 = time.monotonic()
out = _translate_title_sync(text)
dt = time.monotonic() - t0
print(f"耗时 {dt:.1f}s")
print("结果:", out)
