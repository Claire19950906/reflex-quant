# -*- coding: utf-8 -*-
"""v382 · scorecard_share 离线单测：空库也能出页、关键口径进页面、无外链。"""
from __future__ import annotations

import scorecard_share as sc


def test_collect_empty_db(fresh_db):
    data = sc.collect(fresh_db)
    assert data["settled"] == 0
    assert data["winrate"] is None
    assert len(data["funnel"]) == 6


def test_render_contains_kpis_and_no_external_links(fresh_db):
    data = sc.collect(fresh_db)
    page = sc.render(data)
    assert "信号成绩单" in page
    assert "成交漏斗" in page
    assert "http://" not in page and "https://" not in page, "自包含页面不能有外链"
    assert "<script" not in page


def test_generate_writes_file(tmp_path, fresh_db):
    out = tmp_path / "card.html"
    path = sc.generate(str(out), conn=fresh_db)
    assert path == str(out)
    assert out.read_text(encoding="utf-8").startswith("<!doctype html>")


def test_funnel_counts_sample(fresh_db):
    fresh_db.execute(
        "INSERT INTO raw_news (id, source, content, timestamp) VALUES (1,'t','x',datetime('now'))"
    )
    fresh_db.execute(
        """INSERT INTO ai_decisions (news_id, target_asset, suggested_action, settled,
                                      sentiment_score, reasoning, forward_pnl, created_at)
           VALUES (1,'XAU','BUY',1,0.5,'r',1.2,datetime('now'))"""
    )
    fresh_db.commit()
    data = sc.collect(fresh_db)
    assert data["news_total"] == 1
    assert data["decisions"] == 1
    assert data["settled"] == 1
    assert data["winrate"] == 100.0
