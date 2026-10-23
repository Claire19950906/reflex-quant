"""Regression tests for this-round 5 bug fixes.
Covers:
  A1 - trading manager default-disabled
  A2 - SSE event reads from extra_models_consensus JSON
  C1 - event-select no N+1
  F1 - .env.example BINANCE_NOTIONAL_USDT matches config default 5000
  C2 - forward.py:196 inline comment removed
"""
import os
import re
import sys

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)


def test_a1_trading_manager_disabled_by_default():
    import api_server
    saved = os.environ.pop("TRADING_MANAGER_ENABLED", None)
    try:
        assert api_server._trading_manager_enabled() is False
        for v in ("0", "false", "False", "no", "", "off"):
            os.environ["TRADING_MANAGER_ENABLED"] = v
            assert api_server._trading_manager_enabled() is False, v
        for v in ("1", "true", "True", "yes", "on"):
            os.environ["TRADING_MANAGER_ENABLED"] = v
            assert api_server._trading_manager_enabled() is True, v
    finally:
        if saved is not None:
            os.environ["TRADING_MANAGER_ENABLED"] = saved
        else:
            os.environ.pop("TRADING_MANAGER_ENABLED", None)


def test_a1_trading_manager_start_returns_disabled_when_disabled():
    import api_server
    saved = os.environ.pop("TRADING_MANAGER_ENABLED", None)
    try:
        os.environ["TRADING_MANAGER_ENABLED"] = "0"
        out = api_server._TRADING_MANAGER.start()
        assert out["started"] is False
        assert out["reason"] == "disabled_by_default"
    finally:
        if saved is not None:
            os.environ["TRADING_MANAGER_ENABLED"] = saved
        else:
            os.environ.pop("TRADING_MANAGER_ENABLED", None)


def test_a1_authorize_trading_manager_blocks_when_disabled():
    import api_server
    from fastapi import HTTPException
    saved = os.environ.pop("TRADING_MANAGER_ENABLED", None)
    try:
        os.environ["TRADING_MANAGER_ENABLED"] = "0"
        try:
            api_server._authorize_trading_manager("anything")
        except HTTPException as exc:
            assert exc.status_code == 503
        else:
            raise AssertionError("expected HTTPException")
    finally:
        if saved is not None:
            os.environ["TRADING_MANAGER_ENABLED"] = saved
        else:
            os.environ.pop("TRADING_MANAGER_ENABLED", None)


def test_a2_sse_uses_consensus_json_not_missing_columns():
    import api_server
    src_path = api_server.__file__
    with open(src_path, "r", encoding="utf-8") as f:
        src = f.read()
    for col in ("deepseek_action", "chatgpt_action", "gemini_action", "grok_action"):
        needle = '_safe(row, "' + col + '")'
        assert needle not in src, needle


def test_a2_sse_event_picks_consensus_values():
    import api_server
    import sqlite3
    import tempfile
    import db as dbmod

    tmp = tempfile.mktemp(suffix=".db")
    os.environ["TRIDENT_DB_PATH"] = tmp
    try:
        dbmod.migrate(dbmod.get_connection())
        conn = dbmod.get_connection()
        conn.execute(
            "INSERT INTO raw_news (source, content, timestamp, status, is_noise) "
            "VALUES ('x', 'content body', '2024-01-01T00:00:00', 'DONE', 0)"
        )
        nid = conn.execute("SELECT MAX(id) FROM raw_news").fetchone()[0]
        consensus_json = (
            '{"DeepSeek": {"action": "BUY", "reasoning": "ds"},'
            '"ChatGPT": {"action": "SELL", "reasoning": "cg"},'
            '"Gemini": {"action": "HOLD", "reasoning": "ne"},'
            '"Grok": {"action": "BUY", "reasoning": "gr"}}'
        )
        conn.execute(
            "INSERT INTO ai_decisions (news_id, suggested_action, target_asset, "
            "sentiment_score, reasoning, extra_models_consensus, doubao_action, doubao_reasoning, created_at) "
            "VALUES (?, 'BUY', 'BTC', 0.5, 'main', ?, 'BUY', 'ok', '2024-01-01T00:00:01')",
            (nid, consensus_json),
        )
        conn.commit()
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT rn.*, ad.* FROM raw_news rn LEFT JOIN ai_decisions ad ON ad.news_id = rn.id WHERE rn.id = ?",
            (nid,),
        ).fetchone()
        payload = api_server._row_to_event(dict(row))
        assert payload["deepseek_action"] == "BUY"
        assert payload["deepseek_reasoning"] == "ds"
        assert payload["chatgpt_action"] == "SELL"
        assert payload["gemini_action"] == "HOLD"
        assert payload["grok_action"] == "BUY"
    finally:
        os.environ.pop("TRIDENT_DB_PATH", None)
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def test_c1_event_select_no_n_plus_one():
    import api_server
    sql = api_server._EVENT_SELECT
    assert "SELECT MAX(latest.id) FROM ai_decisions latest" not in sql
    assert "GROUP BY news_id" in sql
    assert "latest.latest_id" in sql


def test_f1_env_example_notional_matches_config():
    repo_root = os.path.abspath(os.path.join(SRC, "..", ".."))
    env_example = os.path.join(repo_root, ".env.example")
    with open(env_example, "r", encoding="utf-8") as f:
        text = f.read()
    m = re.search(r"^BINANCE_NOTIONAL_USDT\s*=\s*(\d+)", text, re.MULTILINE)
    assert m is not None
    assert int(m.group(1)) == 5000, m.group(1)


def test_c2_forward_entry_unix_comment_clean():
    repo_root = os.path.abspath(os.path.join(SRC, "..", ".."))
    fwd = os.path.join(repo_root, "backend", "src_python", "engine", "forward.py")
    with open(fwd, "r", encoding="utf-8") as f:
        text = f.read()
    bad = re.findall(r"entry_unix\s*=\s*int\([^)]+\)\s*#[^#]*Impact metrics", text)
    assert not bad, bad
