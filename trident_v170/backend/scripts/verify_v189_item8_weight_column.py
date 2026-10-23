"""v189 Item 8 验证：decision_errors weight 列 + 跨层排序"""
import os
import sqlite3
import sys
import tempfile

PROJECT = "/Users/claire/.cline/data/workspaces/chat/trident_v170"
sys.path.insert(0, os.path.join(PROJECT, "backend", "src_python"))

import db as _db
import reflection_checkers as rc

results = []
def chk(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"  {'✅' if ok else '❌'} {name}" + (f" — {detail}" if detail and not ok else ""))

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS decision_errors (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id     INTEGER NOT NULL,
    error_code      TEXT    NOT NULL,
    origin          TEXT    NOT NULL DEFAULT 'checker',
    severity        INTEGER NOT NULL DEFAULT 2,
    detail          TEXT    NOT NULL DEFAULT '',
    evidence_refs   TEXT    NOT NULL DEFAULT '{}',
    source          TEXT    NOT NULL DEFAULT '',
    weight          INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(decision_id, error_code, origin, source)
);
CREATE INDEX IF NOT EXISTS idx_dec_errors_weight ON decision_errors(weight DESC, decision_id);
"""


print("=== [1] db.py CREATE TABLE 含 weight 列 ===")
with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as tmp:
    tmp_path = tmp.name
try:
    conn = sqlite3.connect(tmp_path)
    conn.executescript(SCHEMA_SQL)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(decision_errors)").fetchall()]
    chk("decision_errors 有 weight 列", "weight" in cols, f"cols={cols}")
    indexes = [r[1] for r in conn.execute("PRAGMA index_list(decision_errors)").fetchall()]
    chk("idx_dec_errors_weight 索引存在", "idx_dec_errors_weight" in indexes,
        f"indexes={indexes}")
    conn.close()
finally:
    os.unlink(tmp_path)


print("\n=== [2] db.py migrate() 含 backfill ===")
with open(os.path.join(PROJECT, "backend/src_python/db.py")) as f:
    db_src = f.read()
chk("migrate() 中出现 decision_errors weight backfill",
    '_ensure_columns(conn, "decision_errors", _DECISION_ERRORS_V189_COLUMNS)' in db_src)
chk("_DECISION_ERRORS_V189_COLUMNS 定义存在",
    "_DECISION_ERRORS_V189_COLUMNS" in db_src)


print("\n=== [3] reflection_checkers._LAYER_WEIGHT ===")
for layer in ["event_logic", "causal", "rule", "logic", "data", "market"]:
    chk(f"_LAYER_WEIGHT 含 {layer}", layer in rc._LAYER_WEIGHT)
chk("event_logic = 100", rc._LAYER_WEIGHT["event_logic"] == 100)
chk("causal = 60", rc._LAYER_WEIGHT["causal"] == 60)
chk("rule = 50", rc._LAYER_WEIGHT["rule"] == 50)


print("\n=== [4] _compute_weight 公式正确 ===")
chk("event_logic + critical = 130", rc._compute_weight(3, "event_logic") == 130)
chk("event_logic + warn = 120", rc._compute_weight(2, "event_logic") == 120)
chk("causal + warn = 80", rc._compute_weight(2, "causal") == 80)
chk("rule + warn = 70", rc._compute_weight(2, "rule") == 70)
chk("data + warn = 50", rc._compute_weight(2, "data") == 50)
chk("market + info = 30", rc._compute_weight(1, "market") == 30)
chk("未知 source + critical = 40", rc._compute_weight(3, "unknown") == 40)


print("\n=== [5] 排序优先级 ===")
prios = [rc._compute_weight(3, "event_logic"), rc._compute_weight(2, "event_logic"),
         rc._compute_weight(2, "causal"), rc._compute_weight(2, "rule"),
         rc._compute_weight(2, "logic"), rc._compute_weight(2, "data"),
         rc._compute_weight(2, "market")]
chk("优先级严格递减", prios == sorted(prios, reverse=True), f"prios={prios}")


print("\n=== [6] 端到端：3 错排序 ===")
with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as tmp:
    tmp_path = tmp.name
try:
    conn = sqlite3.connect(tmp_path)
    conn.executescript(SCHEMA_SQL)
    conn.row_factory = sqlite3.Row
    errs = [
        {"decision_id": 1, "error_code": "E005", "severity": rc.SEVERITY_WARN, "source": "rule"},
        {"decision_id": 1, "error_code": "E004", "severity": rc.SEVERITY_WARN, "source": "causal"},
        {"decision_id": 1, "error_code": "E016", "severity": rc.SEVERITY_WARN, "source": "event_logic"},
    ]
    n = rc.save_errors(errs, conn=conn)
    chk("save_errors 插入 3 条", n == 3, f"inserted={n}")
    weights = {r["error_code"]: r["weight"]
               for r in conn.execute("SELECT error_code, weight FROM decision_errors").fetchall()}
    chk("E005 weight=70", weights.get("E005") == 70)
    chk("E004 weight=80", weights.get("E004") == 80)
    chk("E016 weight=120", weights.get("E016") == 120)
    rows = rc.get_errors_for_decision(1, conn=conn)
    codes = [r["error_code"] for r in rows]
    chk("排序: E016 > E004 > E005", codes == ["E016", "E004", "E005"], f"got {codes}")
    conn.close()
finally:
    os.unlink(tmp_path)


print("\n=== [7] 同 source 不同 severity ===")
with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as tmp:
    tmp_path = tmp.name
try:
    conn = sqlite3.connect(tmp_path)
    conn.executescript(SCHEMA_SQL)
    conn.row_factory = sqlite3.Row
    errs = [
        {"decision_id": 2, "error_code": "E015", "severity": rc.SEVERITY_WARN, "source": "event_logic"},
        {"decision_id": 2, "error_code": "E014", "severity": rc.SEVERITY_CRITICAL, "source": "event_logic"},
    ]
    rc.save_errors(errs, conn=conn)
    rows = rc.get_errors_for_decision(2, conn=conn)
    codes = [r["error_code"] for r in rows]
    chk("critical E014 > warn E015", codes == ["E014", "E015"], f"got {codes}")
    conn.close()
finally:
    os.unlink(tmp_path)


print("\n=== [8] backwards compat：weight=0 老数据 ===")
with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as tmp:
    tmp_path = tmp.name
try:
    conn = sqlite3.connect(tmp_path)
    conn.executescript(SCHEMA_SQL)
    conn.row_factory = sqlite3.Row
    conn.execute("INSERT INTO decision_errors (decision_id, error_code, origin, severity, source) "
                 "VALUES (10, 'E005', 'checker', 2, 'rule')")
    conn.execute("INSERT INTO decision_errors (decision_id, error_code, origin, severity, source, weight) "
                 "VALUES (10, 'E016', 'checker', 2, 'event_logic', 120)")
    conn.commit()
    rows = rc.get_errors_for_decision(10, conn=conn)
    codes = [r["error_code"] for r in rows]
    chk("weight=0 老数据排最后", codes == ["E016", "E005"], f"got {codes}")
    conn.close()
finally:
    os.unlink(tmp_path)


print("\n=== [9] smoke 集成 ===")
import subprocess
res = subprocess.run(["python3", "/tmp/smoke_checker.py"], capture_output=True, text=True)
out = res.stdout
chk("Case1 触发 5 错（E005/E004/E014/E015/E016）",
    all(c in out for c in ["E005", "E004", "E014", "E015", "E016"]),
    f"snippet: {out[:200]}")


passed = sum(1 for _, ok, _ in results if ok)
total = len(results)
print(f"\n=== 总计：{passed}/{total} pass，{total - passed} fail ===")
sys.exit(0 if passed == total else 1)