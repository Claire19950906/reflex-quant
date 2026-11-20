#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v483：失败 AI 调用不得写空 error_code。"""
from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_SRC = _HERE.parent / "src_python"
sys.path.insert(0, str(_SRC))

import db


def _capture(monkeypatch):
    box = {}

    def _fake(params):
        box["params"] = params
        return True

    monkeypatch.setattr(db, "_call_stats_enqueue", _fake)
    return box


def test_failed_call_defaults_to_unknown(monkeypatch):
    box = _capture(monkeypatch)
    db.log_ai_call(purpose="gate", key_alias="k", model_id="m",
                   status="http_error", error_code="")
    assert box["params"][7] == "http_error"
    assert box["params"][8] == "unknown"


def test_ok_call_can_keep_empty(monkeypatch):
    box = _capture(monkeypatch)
    db.log_ai_call(purpose="gate", key_alias="k", model_id="m", status="ok")
    assert box["params"][8] == ""
