#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一次性/幂等回填：raw_news.content_fingerprint + first_seen_ts，ai_decisions.event_struct。

安全：可重复执行；只补空值；先 db.migrate 确保新列存在。
用法：cd backend && python3 backfill_event_trace.py
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src_python"))

import db
from event_struct import extract_events


def main() -> None:
    conn = db.get_connection()
    try:
        db.migrate(conn)

        # 1) raw_news.content_fingerprint 补齐（只补空值）
        fp_rows = conn.execute(
            "SELECT id, content FROM raw_news WHERE COALESCE(content_fingerprint, '') = ''"
        ).fetchall()
        for nid, content in fp_rows:
            conn.execute(
                "UPDATE raw_news SET content_fingerprint=? WHERE id=?",
                (db.content_fingerprint(content or ""), nid),
            )

        # 2) first_seen_ts：按指纹组取最早 timestamp，回填同组所有行（全网最早溯源）
        conn.execute(
            """
            UPDATE raw_news SET first_seen_ts = (
                SELECT MIN(t2.timestamp) FROM raw_news t2
                WHERE t2.content_fingerprint = raw_news.content_fingerprint
                  AND COALESCE(raw_news.content_fingerprint, '') <> ''
            )
            WHERE COALESCE(raw_news.content_fingerprint, '') <> ''
            """
        )

        # 3) ai_decisions.event_struct：按 news_id JOIN raw_news.content 重新抽取（只补空值）
        es_rows = conn.execute(
            """
            SELECT ad.id, rn.content FROM ai_decisions ad
            JOIN raw_news rn ON rn.id = ad.news_id
            WHERE COALESCE(ad.event_struct, '') = ''
            """
        ).fetchall()
        for did, content in es_rows:
            try:
                esj = json.dumps(extract_events(content or ""), ensure_ascii=False)[:1000]
            except Exception:
                esj = ""
            conn.execute("UPDATE ai_decisions SET event_struct=? WHERE id=?", (esj, did))

        conn.commit()
        print(f"backfill done: fingerprint_rows={len(fp_rows)}, "
              f"event_struct_rows={len(es_rows)}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()