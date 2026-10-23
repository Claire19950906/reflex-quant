# -*- coding: utf-8 -*-
"""v172 事件结构化输出层 API 测试。

覆盖：
  - /api/event_atoms          列表 + 多重过滤
  - /api/event_atoms/{id}     详情 + 候选时间 + 影响曲线
  - /api/news/{id}/atoms      单新闻原子
  - /api/news/{id}/event_timeline  时间线
  - /api/news/{id}/structured 结构化输入/校验 + verdict
  - /api/event_impact_curves  曲线列表
  - /api/event_impact_curves/by_atom/{id}
  - /api/event_impact_stat    每日统计
  - /api/event_dim/assets     维度表
  - /api/event_dim/event_types
  - /api/event_atom/summary   聚合汇总

每个测试都用 FastAPI TestClient 调用，确保路由路径正确 + JSON 字段完整。
"""
from __future__ import annotations

import json
import os
import sys
import sqlite3
import unittest
from pathlib import Path

# DB 隔离已由 conftest.py 顶层统一处理（TRIDENT_DB_PATH → 临时库）。
# 此处不再 setdefault：若被先 import config 的模块固化掉真实库路径会误导后人。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src_python"))

import db  # noqa: E402
import dim_tables as _dim  # noqa: E402
import event_atom as _event_atom  # noqa: E402
import event_impact_curve as _eic  # noqa: E402

db.get_connection()  # 触发首次 connect + 创建迁移后连接
# 显式 migrate（FastAPI lifespan 内会做，但 TestClient 测试时 import 阶段还没跑）
conn = db.get_connection()
db.migrate(conn)
_dim.list_assets()
_dim.list_event_types()


def _wipe_test_tables():
    """清空所有可能污染测试的表（保证每个 setUp 独立）。"""
    conn = db.get_connection()
    for t in (
        "event_atom", "event_time_candidates", "event_impact_curve",
        "event_impact_stat", "raw_news", "ai_decisions",
    ):
        try:
            conn.execute(f"DELETE FROM {t}")
            conn.commit()
        except Exception:
            pass


def _seed_event_atom(
    raw_news_id: int = 1,
    type_code: str = "geopolitical_conflict",
    asset_code: str = "XAU",
    magnitude: float = 1.0,
    magnitude_unit: str = "tweet",
    direction: str = "bullish",
    direction_confidence: float = 0.85,
    event_time_confidence: float = 0.95,
    source: str = "platform_published_at",
) -> int:
    """直接调 insert_event_atom 注入一条 event_atom + 候选时间，返回 atom_id。"""
    import time as _t
    ts = int(_t.time())
    aid = _event_atom.insert_event_atom(
        raw_news_id=raw_news_id,
        event_type_code=type_code,
        event_time=ts,
        asset_code=asset_code,
        event_time_confidence=event_time_confidence,
        event_time_source=source,
        magnitude_value=magnitude,
        magnitude_unit=magnitude_unit,
        actor="测试 actor",
        direction=direction,
        direction_confidence=direction_confidence,
        direction_source=source,
        evidence_count=2,
        reasoning="测试事件原子（v172 API 测试用）",
    )
    _event_atom.insert_event_time_candidate(
        raw_news_id=raw_news_id,
        candidate_ts=ts,
        tz="Asia/Shanghai",
        source=source,
        confidence=event_time_confidence,
        is_primary=True,
        reasoning="seed candidate",
    )
    return aid


def _seed_impact_curve(atom_id: int, asset_code: str = "XAU") -> None:
    """注入 4 窗口影响曲线点。"""
    for wl, delta in [("5m", 0.15), ("30m", 0.42), ("2h", 0.71), ("1d", 0.95)]:
        _eic.record_impact_point(
            event_atom_id=atom_id, asset_code=asset_code, window_label=wl,
            delta_pct=delta, price=2400.0 + delta * 100,
            volume_ratio=1.2 + delta, sample_ts=1700000000, note="test_seed",
        )


class TestEventAtomsList(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        _wipe_test_tables()
        self.client = TestClient(__import__("api_server").app)
        self.atom1 = _seed_event_atom(raw_news_id=10, type_code="geopolitical_conflict", asset_code="XAU")
        self.atom2 = _seed_event_atom(raw_news_id=11, type_code="rate_hike", asset_code="BTC")
        self.atom3 = _seed_event_atom(raw_news_id=12, type_code="cpi_release", asset_code="OIL")
        _seed_impact_curve(self.atom1, "XAU")

    def test_list_no_filter(self):
        r = self.client.get("/api/event_atoms")
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertGreaterEqual(data["count"], 3)
        first = data["items"][0]
        for k in ("id", "raw_news_id", "event_type_code", "event_type_zh",
                  "asset_code", "event_time", "direction", "reasoning", "created_at"):
            self.assertIn(k, first, f"missing key {k}")

    def test_list_filter_by_type_code(self):
        r = self.client.get("/api/event_atoms", params={"type_code": "rate_hike"})
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["items"][0]["event_type_code"], "rate_hike")
        self.assertEqual(data["items"][0]["asset_code"], "BTC")

    def test_list_filter_by_asset_code(self):
        r = self.client.get("/api/event_atoms", params={"asset_code": "OIL"})
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["items"][0]["asset_code"], "OIL")

    def test_list_filter_by_news_id(self):
        r = self.client.get("/api/event_atoms", params={"news_id": 11})
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["items"][0]["raw_news_id"], 11)

    def test_list_limit(self):
        r = self.client.get("/api/event_atoms", params={"limit": 1})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["count"], 1)


class TestEventAtomDetail(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        _wipe_test_tables()
        self.client = TestClient(__import__("api_server").app)
        self.atom = _seed_event_atom(raw_news_id=20, type_code="geopolitical_conflict", asset_code="XAU")
        _seed_impact_curve(self.atom, "XAU")

    def test_get_by_id(self):
        r = self.client.get(f"/api/event_atoms/{self.atom}")
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertEqual(data["atom"]["id"], self.atom)
        self.assertEqual(data["atom"]["asset_code"], "XAU")
        for wl in ("5m", "30m", "2h", "1d"):
            self.assertIn(wl, data["impact_curve"], f"missing window {wl}")
        self.assertGreater(len(data["candidates"]), 0, "should have event_time_candidates rows")

    def test_get_by_id_404(self):
        r = self.client.get("/api/event_atoms/99999999")
        self.assertEqual(r.status_code, 404)


class TestNewsStructured(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        _wipe_test_tables()
        self.client = TestClient(__import__("api_server").app)

    def test_news_structured_empty_returns_404(self):
        r = self.client.get("/api/news/9999/structured")
        self.assertEqual(r.status_code, 404)

    def test_news_atoms_returns_list(self):
        atom = _seed_event_atom(raw_news_id=30)
        r = self.client.get("/api/news/30/atoms")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["news_id"], 30)
        self.assertGreaterEqual(data["count"], 1)
        self.assertEqual(data["items"][0]["id"], atom)

    def test_news_event_timeline(self):
        _seed_event_atom(raw_news_id=40)
        r = self.client.get("/api/news/40/event_timeline")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["news_id"], 40)
        self.assertGreaterEqual(data["count"], 1)
        self.assertIn("primary", data)


class TestEventImpact(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        _wipe_test_tables()
        self.client = TestClient(__import__("api_server").app)

    def test_list_curves(self):
        r = self.client.get("/api/event_impact_curves")
        self.assertEqual(r.status_code, 200)
        for k in ("items", "count", "filters"):
            self.assertIn(k, r.json())

    def test_impact_stat(self):
        r = self.client.get("/api/event_impact_stat")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["days"], 7)
        self.assertIn("items", data)

    def test_impact_stat_max_days(self):
        r = self.client.get("/api/event_impact_stat", params={"days": 200})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["days"], 90, "days should be clamped to 90")

    def test_curve_by_atom(self):
        atom = _seed_event_atom(raw_news_id=50)
        _seed_impact_curve(atom, "XAU")
        r = self.client.get(f"/api/event_impact_curves/by_atom/{atom}")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["atom_id"], atom)
        self.assertIn("rows", data)
        self.assertIn("aggregated", data)


class TestDimTables(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        _wipe_test_tables()
        self.client = TestClient(__import__("api_server").app)

    def test_assets(self):
        r = self.client.get("/api/event_dim/assets")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertGreater(data["count"], 0)
        first = data["items"][0]
        for k in ("id", "code", "name", "kind"):
            self.assertIn(k, first, f"missing dim asset key {k}")

    def test_event_types(self):
        r = self.client.get("/api/event_dim/event_types")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertGreater(data["count"], 0)
        first = data["items"][0]
        for k in ("id", "code", "name_zh", "direction_bias"):
            self.assertIn(k, first, f"missing dim event_type key {k}")


class TestSummary(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        _wipe_test_tables()
        self.client = TestClient(__import__("api_server").app)

    def test_summary_aggregations(self):
        _seed_event_atom(raw_news_id=60, type_code="rate_hike", asset_code="BTC")
        _seed_event_atom(raw_news_id=61, type_code="rate_hike", asset_code="ETH")
        r = self.client.get("/api/event_atom/summary")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["window_days"], 7)
        for k in ("by_type_code", "by_asset_code", "by_category"):
            self.assertIn(k, data)


class TestFetchEventRowsHasStructuredFields(unittest.TestCase):
    """验证 _fetch_event_rows 返回 dict 包含 5 个结构化字段。"""

    def test_api_event_has_structured_fields(self):
        from fastapi.testclient import TestClient
        client = TestClient(__import__("api_server").app)
        r = client.get("/api/events")
        self.assertEqual(r.status_code, 200)
        events = r.json()
        if events:
            ev = events[0]
            for k in ("event_struct", "structured_input", "structured_check",
                      "event_type_code", "snapshot_proximity"):
                self.assertIn(k, ev, f"missing structured key {k}")


if __name__ == "__main__":
    unittest.main()