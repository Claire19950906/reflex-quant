#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v189 Item 4: cross_asset JSONL <-> evidence mutual verification."""
import os, sys, json, sqlite3, tempfile
ROOT = "/Users/claire/.cline/data/workspaces/chat/trident_v170/backend/src_python"
sys.path.insert(0, ROOT)
import reflection_checkers as rc

results = []

def chk(label, ok, detail=""):
    tag = "OK" if ok else "FAIL"
    results.append((label, ok, detail))
    if detail:
        print("  [" + tag + "] " + label + " -- " + detail)
    else:
        print("  [" + tag + "] " + label)

print("=== [1] _extract_cross_asset_likely dual schema ===")
d_in = {"cross_asset_likely": [{"what": "ETH", "direction": "up"},
                               {"asset": "DXY", "dir": "down"}]}
out = rc._extract_cross_asset_likely(d_in)
chk("dict schema -> 2", len(out) == 2)

s_in = {"cross_asset_likely": ["ETH up", "DXY down", "BTC bull"]}
out = rc._extract_cross_asset_likely(s_in)
chk("str schema -> 3", len(out) == 3)
chk("'ETH up' -> ETH", len(out) > 0 and out[0].get("what") == "ETH")

m_in = {"cross_asset_likely": [{"what": "GOLD", "direction": "up"}, "ETH bull"]}
out = rc._extract_cross_asset_likely(m_in)
chk("mixed schema -> 2", len(out) == 2)

for bad in [{}, {"cross_asset_likely": ""}, {"cross_asset_likely": "[]"},
            {"cross_asset_likely": []}, {"cross_asset_likely": ["", "  "]},
            {"cross_asset_likely": "not json"}]:
    out = rc._extract_cross_asset_likely(bad)
    chk("bad input -> []", out == [])

print("=== [2] E017 constants/sort/guidance ===")
chk("E017 constant", hasattr(rc, "E017_CROSS_ASSET_UNVERIFIED"))
src = open(os.path.join(ROOT, "reflection_checkers.py"), encoding="utf-8").read()
chk("E017 in sort table", '"E017": 4' in src)
chk("E017 in event_logic set", '("E014", "E015", "E016", "E017")' in src)
chk("E017 guidance registered", '"E017":' in src and "E017_CROSS_ASSET_UNVERIFIED" in src)

print("=== [3] E017 triggers on unverified cross_asset ===")
dec = {"id": 100, "target_asset": "BTC", "suggested_action": "BUY",
       "reasoning": "BTC demand strong, technical bullish outlook today.",
       "hard_data_evidence": "BTC.volume=12000", "news_text": "",
       "cross_asset_likely": [{"what": "ETH", "direction": "up"},
                              {"what": "BTC", "direction": "up"}]}
errs = rc.check_event_logic(dec)
e017 = [e for e in errs if e["error_code"] == "E017"]
chk("E017 triggers 1", len(e017) == 1)
if e017:
    unv = e017[0]["evidence_refs"].get("unverified", [])
    chk("unverified only ETH", len(unv) == 1 and unv[0]["what"] == "ETH")
chk("source=event_logic", e017[0]["source"] == "event_logic")
chk("severity=warn", e017[0]["severity"] == rc.SEVERITY_WARN)
print("=== [4] E017 no-trigger when evidence verifies ===")
dec = {"id": 101, "target_asset": "BTC", "suggested_action": "BUY",
       "reasoning": "BTC bullish. DXY down helps BTC. ETH follows.",
       "hard_data_evidence": "DXY.price=99.5|ETH.volume=20000",
       "cross_asset_likely": [{"what": "DXY", "direction": "down"},
                              {"what": "ETH", "direction": "up"}]}
errs = rc.check_event_logic(dec)
e017 = [e for e in errs if e["error_code"] == "E017"]
chk("E017 no-trigger", len(e017) == 0)

print("=== [5] news_text verifies ===")
dec = {"id": 102, "target_asset": "GOLD", "suggested_action": "BUY",
       "reasoning": "Gold long-term allocation.", "hard_data_evidence": "",
       "news_text": "DXY weakens boosts gold upside expectations",
       "cross_asset_likely": [{"what": "DXY", "direction": "down"}]}
errs = rc.check_event_logic(dec)
e017 = [e for e in errs if e["error_code"] == "E017"]
chk("news_text verifies", len(e017) == 0)

print("=== [6] str schema triggers ===")
dec = {"id": 103, "target_asset": "BTC", "suggested_action": "BUY",
       "reasoning": "BTC demand strong.", "hard_data_evidence": "",
       "cross_asset_likely": ["ETH up"]}
errs = rc.check_event_logic(dec)
e017 = [e for e in errs if e["error_code"] == "E017"]
chk("str schema -> E017", len(e017) == 1)

print("=== [7] E016 + E017 coexist ===")
dec = {"id": 104, "target_asset": "BTC", "suggested_action": "BUY",
       "reasoning": "BTC strong", "hard_data_evidence": "",
       "cross_asset_likely": [{"what": "ETH", "direction": "down"}]}
errs = rc.check_event_logic(dec)
codes = {e["error_code"] for e in errs}
chk("E016 + E017 both", "E016" in codes and "E017" in codes, "got " + str(codes))

print("=== [8] SELL also triggers ===")
dec = {"id": 105, "target_asset": "BTC", "suggested_action": "SELL",
       "reasoning": "BTC top signal", "hard_data_evidence": "",
       "cross_asset_likely": [{"what": "ETH", "direction": "down"}]}
errs = rc.check_event_logic(dec)
e017 = [e for e in errs if e["error_code"] == "E017"]
chk("SELL triggers E017", len(e017) == 1)

print("=== [9] end-to-end save_errors ===")
SCHEMA = """
CREATE TABLE decision_errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id INTEGER NOT NULL,
    error_code TEXT NOT NULL,
    origin TEXT NOT NULL DEFAULT 'checker',
    severity INTEGER NOT NULL DEFAULT 2,
    detail TEXT, evidence_refs TEXT, source TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    trade_uuid TEXT, weight INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_dec_errors_weight ON decision_errors(weight DESC, decision_id);
"""
with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as tmp:
    tmp_path = tmp.name
try:
    conn = sqlite3.connect(tmp_path)
    conn.executescript(SCHEMA)
    conn.row_factory = sqlite3.Row
    dec = {"id": 200, "target_asset": "BTC", "suggested_action": "BUY",
           "reasoning": "BTC strong", "hard_data_evidence": "",
           "cross_asset_likely": [{"what": "ETH", "direction": "down"}]}
    errs = rc.check_event_logic(dec)
    n = rc.save_errors(errs, conn=conn)
    chk("save_errors n>=2", n >= 2, "n=" + str(n))
    rows = rc.get_errors_for_decision(200, conn=conn)
    codes = [r["error_code"] for r in rows]
    chk("E016/E017 in top 3 (event_logic layer)",
        set(codes[:3]) >= {"E016", "E017"},
        "got " + str(codes))
    chk("E014 critical = highest weight",
        codes[0] == "E014",
        "got top=" + str(codes[0]) if codes else "empty")
    conn.close()
finally:
    os.unlink(tmp_path)

passed = sum(1 for _, ok, _ in results if ok)
total = len(results)
print("=== Total: " + str(passed) + "/" + str(total) + " pass, " + str(total - passed) + " fail ===")
sys.exit(0 if passed == total else 1)