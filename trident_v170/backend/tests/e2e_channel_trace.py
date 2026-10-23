#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TRIDENT 全链路端到端验证：新闻接入 → 来源打标 → 跨进程去重 → 时区统计 → 正文截断
→ 虚拟实盘信号生成 → 成交对账 → 统计报表（真实样本 + 真实 SQLite schema）。

隔离策略（保证离线可复现、绝不触碰生产库）：
  · TRIDENT_DB_PATH 指向独立临时 SQLite（生产库为 trident_event_bus.db）
  · stock_news/reuters_news.translate_title_sync → 原文透传（隔离翻译网络）
  · api_server.evaluate_news → 确定性 CORE（隔离噪音过滤 L1/L2 模型）
用法：
  python3 backend/tests/e2e_channel_trace.py
  （内部会以不同 PYTHONHASHSEED 起子进程复检跨进程去重）
"""

import os
import sys
import json
import hashlib
import sqlite3
import subprocess
import tempfile
import time as _time
from datetime import datetime

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

# ---------------------------------------------------------------------------
# 子进程分支：跨进程去重复检（父进程已传 TRIDENT_DB_PATH + E2E_ITEMS_JSON）
# ---------------------------------------------------------------------------
if os.environ.get("E2E_PHASE") == "dedup":
    import config  # noqa: E402  (TRIDENT_DB_PATH 已指向父进程的临时库)
    import db  # noqa: E402
    import api_server  # noqa: E402

    api_server.evaluate_news = lambda t, c="": {
        "is_noise": 0, "relevance_score": 0.9, "status": "PENDING", "verdict": "CORE",
    }
    conn = db.get_connection()
    db.migrate(conn)
    conn.close()
    items = json.loads(os.environ.get("E2E_ITEMS_JSON") or "[]")
    inserted = api_server._ingest_external_news_sync(items)
    print(f"E2E_DEDUP_RESULT inserted={inserted} seed={os.environ.get('PYTHONHASHSEED','?')}")
    sys.exit(0)

# ---------------------------------------------------------------------------
# 主进程：先于任何 config import 设定独立临时库
# ---------------------------------------------------------------------------
_TMP = tempfile.mkdtemp(prefix="trident_e2e_")
_DB_PATH = os.path.join(_TMP, "e2e.db")
os.environ["TRIDENT_DB_PATH"] = _DB_PATH

import config  # noqa: E402
import db  # noqa: E402
import api_server  # noqa: E402
import stock_news  # noqa: E402
import reuters_news  # noqa: E402
import quick_sim  # noqa: E402
import news_organizer  # noqa: E402
import timeseries  # noqa: E402

# 隔离网络/模型
stock_news.translate_title_sync = lambda t: t
reuters_news.translate_title_sync = lambda t: t
api_server.evaluate_news = lambda t, c="": {
    "is_noise": 0, "relevance_score": 0.85, "status": "PENDING", "verdict": "CORE",
}

REPORT = []  # [(环节, PASS/FAIL, 详情)]


def check(section, cond, detail=""):
    tag = "PASS" if cond else "FAIL"
    REPORT.append((section, tag, detail))
    print(f"  [{tag}] {section}: {detail}")
    return cond


def _banner(s):
    print("\n" + "=" * 72)
    print("  " + s)
    print("=" * 72)


def _epoch_bj(ts):
    return datetime.fromtimestamp(float(ts), tz=config.TZ_SHANGHAI).strftime(
        "%Y-%m-%d %H:%M:%S")


def _marker(source, item_id):
    return hashlib.sha256(f"{source}:{item_id}".encode("utf-8")).hexdigest()[:24]


def _sha12(title):
    return hashlib.sha256(str(title).encode("utf-8")).hexdigest()[:12]


# ---- 建库 + migrate（幂等，生产同款 schema）----
conn = db.get_connection()
db.migrate(conn)
conn.close()
print(f"临时库: {_DB_PATH}")
print(f"PYTHONHASHSEED(主进程) = {os.environ.get('PYTHONHASHSEED', '随机')}")

# ===========================================================================
# 环节 ① 新闻接入 + 来源打标 + 稳定 id
# ===========================================================================
_banner("① 新闻接入 / 来源打标 / 稳定 id（to_ingest_items）")

now_ts = int(_time.time()) - 60  # 1 分钟前，保证新鲜度闸通过
SAMP_FINN = {"title": "Nvidia beats earnings estimates on AI chip demand",
             "ticker": "NVDA", "publish_ts": now_ts,
             "publisher": "Reuters", "provider": "finnhub"}
SAMP_YAH = {"title": "Tesla unveils next-generation battery pack",
            "ticker": "TSLA", "publish_ts": now_ts,
            "publisher": "Yahoo Finance"}  # 无 provider → Yahoo 兜底
SAMP_REU = {"title": "Federal Reserve holds rates steady, signals patience",
            "news_ts": now_ts, "published_at": _epoch_bj(now_ts)}

finn_item = stock_news.to_ingest_items([SAMP_FINN])[0]
yah_item = stock_news.to_ingest_items([SAMP_YAH])[0]
reu_item = reuters_news.to_ingest_items([SAMP_REU])[0]
items = [finn_item, yah_item, reu_item]

check("①  Finnhub 来源标签", "来源:Finnhub" in finn_item["summary"],
      finn_item["summary"])
check("①  Finnhub 不再误标 Yahoo", "Yahoo Finance" not in finn_item["summary"], "")
check("①  Yahoo 兜底来源名称不重复",
      "来源:Yahoo Finance (Yahoo Finance)" not in yah_item["summary"],
      yah_item["summary"])
check("①  美股 source 键保留 yahoo_stock", finn_item["source"] == "yahoo_stock",
      f"source={finn_item['source']}")
check("①  Reuters source 键", reu_item["source"] == "reuters", reu_item["summary"])
check("①  id 含稳定 sha12(title)",
      _sha12(SAMP_FINN["title"]) in finn_item["id"], finn_item["id"])
# 同标题多次组装 → id 一致（跨进程稳定，不依赖内置 hash()）
ids = {stock_news.to_ingest_items([SAMP_FINN])[0]["id"] for _ in range(3)}
check("①  同一标题多次组装 id 一致", len(ids) == 1, f"id={list(ids)[0]}")

# ===========================================================================
# 环节 ② 入库（_ingest_external_news_sync）+ 去重 + 时区 + 截断
# ===========================================================================
_banner("② 入库 / 同进程去重 / 时区口径 / 正文截断")

inserted_first = api_server._ingest_external_news_sync(items)
check("②  首轮入库条数", inserted_first == 3, f"inserted={inserted_first} (期望 3)")

conn = db.get_connection()
conn.row_factory = sqlite3.Row
rows = conn.execute(
    "SELECT id, source, content, timestamp, ts, published_at FROM raw_news ORDER BY id"
).fetchall()
conn.close()

check("②  raw_news 里刚好 3 条", len(rows) == 3, f"count={len(rows)}")
by_source = {r["source"] for r in rows}
check("②  来源落库包含 yahoo_stock/reuters",
      {"yahoo_stock", "reuters"} <= by_source, str(by_source))

tz_ok = True
trunc_ok = True
marker_ok = True
for it in items:
    m = _marker(it["source"], it["id"])
    tc = db.get_connection()
    tc.row_factory = sqlite3.Row
    rr = tc.execute(
        "SELECT content FROM raw_news WHERE source=? AND content LIKE ?",
        (it["source"], f"%{it['title']}%"),
    ).fetchone()
    tc.close()
    if rr is None or not rr["content"].startswith(f"[hash:{m}]"):
        marker_ok = False
for r in rows:
    s, c, ts, pub = r["source"], r["content"], r["ts"], r["published_at"]
    # 时区口径：timestamp 应为含 +08:00 的北京 ISO；ts 相对该 ISO 的 epoch 应为 0
    ts_iso = r["timestamp"]
    if "+08:00" not in ts_iso:
        tz_ok = False
    # SQLite strftime('%s', timestamp) 相对 ts 字段应一致（±1s 写入时差）
    conn = db.get_connection()
    sql_epoch = conn.execute("SELECT strftime('%s', ?)", (ts_iso,)).fetchone()[0]
    conn.close()
    if sql_epoch is None or abs(int(sql_epoch) - int(ts)) > 2:
        tz_ok = False
    # 截断：content ≤ 1000 字符
    if len(c) > 1000:
        trunc_ok = False

check("②  去重 marker 前缀写入", marker_ok, "content 以 [hash:<sha256[:24]>] 开头")
check("②  timestamp 为带 +08:00 的北京时间 ISO", tz_ok,
      f"样本: {rows[0]['timestamp']}")
check("②  strftime('%s', timestamp) 与 ts 字段一致(UTC epoch 口径)", tz_ok, "")
check("②  content 截断 ≤ 1000", trunc_ok, f"最长={max(len(r['content']) for r in rows)}")

# 同进程去重：同一批 items 再入库一次 → 应 0 插入
inserted_second = api_server._ingest_external_news_sync(items)
check("②  同进程第二次入库被去重", inserted_second == 0,
      f"inserted={inserted_second} (期望 0)")

# 正文截断专项：超过 1000 的长摘要
long_item = {
    "title": "Long form market wrap",
    "summary": "detail " * 400,  # ~2800 字符
    "source": "reuters",
    "id": f"reuters:{now_ts}:{_sha12('Long form market wrap')}",
    "published_at": _epoch_bj(now_ts),
}
api_server._ingest_external_news_sync([long_item])
conn = db.get_connection()
conn.row_factory = sqlite3.Row
lc = conn.execute(
    "SELECT content FROM raw_news WHERE content LIKE '%Long form market wrap%'"
).fetchone()[0]
conn.close()
check("②  超长摘要被截到 1000 字符", len(lc) == 1000, f"len={len(lc)}")
check("②  截断仍保留去重 marker 前缀", lc.startswith("[hash:"), lc[:30] + "...")

# ===========================================================================
# 环节 ③ 新鲜度闸：过时新闻拒收
# ===========================================================================
_banner("③ 新鲜度闸（过时新闻拒收，默认 3h）")
stale_item = {
    "title": "STALE - Fed hikes rates by 100bp",
    "summary": "old news",
    "source": "reuters",
    "id": f"reuters:{now_ts}:{_sha12('STALE - Fed hikes rates by 100bp')}",
    "published_at": _epoch_bj(now_ts - 10 * 3600),  # 10 小时前
}
conn = db.get_connection()
before = conn.execute("SELECT COUNT(*) FROM raw_news").fetchone()[0]
conn.close()
api_server._ingest_external_news_sync([stale_item])
conn = db.get_connection()
after = conn.execute("SELECT COUNT(*) FROM raw_news").fetchone()[0]
conn.close()
check("③  过时新闻(10h)被拒收、不入库", before == after, f"{before} -> {after}")

# ===========================================================================
# 环节 ④ 虚拟实盘信号生成（ai_decisions → pending_signals）
# ===========================================================================
_banner("④ 虚拟实盘信号生成（ai_decisions → quick_sim.pending_signals）")


def _news_id_by(content_substr, source):
    c = db.get_connection()
    c.row_factory = sqlite3.Row
    r = c.execute(
        "SELECT id FROM raw_news WHERE source=? AND content LIKE ? ORDER BY id LIMIT 1",
        (source, f"%{content_substr}%"),
    ).fetchone()
    c.close()
    return r["id"] if r else None


def _add_decision(news_id, action, asset, score, gate):
    c = db.get_connection()
    cur = c.execute(
        """INSERT INTO ai_decisions
           (news_id, sentiment_score, suggested_action, reasoning, target_asset,
            evidence_confidence, trade_gate_reason, agent_model_id)
           VALUES (?,?,?,?,?,?,?,?)""",
        (news_id, score, action, "e2e 端到端验证样本信号", asset,
         0.8, gate, "e2e-model"),
    )
    did = int(cur.lastrowid)
    c.commit()
    c.close()
    return did


nvda_id = _news_id_by("Nvidia", "yahoo_stock")
reu_id = _news_id_by("Federal Reserve", "reuters")
yah_id = _news_id_by("Tesla", "yahoo_stock")
check("④  信号样本成功引用已入库新闻", all([nvda_id, reu_id, yah_id]),
      f"nvda={nvda_id} reuters={reu_id} tesla={yah_id}")

gate = quick_sim._PASSED_REASON  # "证据充分，允许输出方向性结论"
_add_decision(nvda_id, "BUY", "NVDA", 0.90, gate)   # 做多 NVDA
_add_decision(reu_id, "SELL", "BTC", -0.70, gate)    # 宏观利空 → 做空 BTC
_add_decision(yah_id, "HOLD", "XAU", 0.10, "")        # 无方向 → 不建仓

conn = db.get_connection()
pending = quick_sim.pending_signals(conn, limit=50)
conn.close()
acts = sorted(s["action"] for s in pending)
check("④  pending 仅拾取 BUY/SELL(HOLD 被排除)", acts == ["BUY", "SELL"], str(acts))
check("④  pending 带 news_source", all(s["news_source"] for s in pending),
      str([(s["asset"], s["news_source"]) for s in pending]))

# ===========================================================================
# 环节 ⑤ 成交对账（open_trades → settle_trades）
# ===========================================================================
_banner("⑤ 成交对账（真实行情建仓 + 到期结算）")

entry_px = {"NVDA": 140.0, "BTC": 100000.0, "XAU": 45000.0}
exit_px = {"NVDA": 148.0, "BTC": 97000.0}  # NVDA 涨、BTC 跌

conn = db.get_connection()
opened = quick_sim.open_trades(conn, lambda a: entry_px.get(a))
conn.close()
check("⑤  建仓 2 条(BUY/SELL, HOLD 跳过)", len(opened) == 2,
      str([(o["asset"], o["action"], o["entry_price"]) for o in opened]))

conn = db.get_connection()
conn.row_factory = sqlite3.Row
# 模拟已持仓 1 小时（> 20 分钟 horizon）使仓位到期可结算
conn.execute("UPDATE quick_sim_trades SET entry_ts = entry_ts - 3600")
conn.commit()
qrows = conn.execute(
    "SELECT asset, action, entry_price, settled, gate_passed FROM quick_sim_trades"
).fetchall()
conn.close()
check("⑤  未结算仓 2 条且闸门标记通过",
      len(qrows) == 2 and all(r["settled"] == 0 and r["gate_passed"] == 1 for r in qrows),
      str([dict(r) for r in qrows]))

conn = db.get_connection()
settled = quick_sim.settle_trades(conn, lambda a: exit_px.get(a))
conn.close()
check("⑤  结算 2 条", len(settled) == 2,
      str([(s["asset"], s["action"], s["pnl_pct"], s["verdict"]) for s in settled]))

conn = db.get_connection()
conn.row_factory = sqlite3.Row
fin = conn.execute(
    "SELECT asset, action, entry_price, exit_price, pnl_pct, pnl_usdt, verdict"
    " FROM quick_sim_trades ORDER BY asset"
).fetchall()
conn.close()
by_asset = {r["asset"]: r for r in fin}
nvda_win = abs(by_asset["NVDA"]["pnl_pct"] - 5.714286) < 0.01 and by_asset["NVDA"]["verdict"] == "WIN"
btc_win = abs(by_asset["BTC"]["pnl_pct"] - 3.0) < 0.01 and by_asset["BTC"]["verdict"] == "WIN"
check("⑤  BUY NVDA 盈利计算正确(+5.71%)", nvda_win,
      f"pnl_pct={by_asset['NVDA']['pnl_pct']} verdict={by_asset['NVDA']['verdict']}")
check("⑤  SELL BTC 盈利计算正确(+3.0%)", btc_win,
      f"pnl_pct={by_asset['BTC']['pnl_pct']} verdict={by_asset['BTC']['verdict']}")
check("⑤  名义本金 100 USDT", abs(by_asset["NVDA"]["pnl_usdt"] - 5.714286) < 0.1,
      f"NVDA pnl_usdt={by_asset['NVDA']['pnl_usdt']}")

# ===========================================================================
# 环节 ⑥ 统计报表（quick_sim.evaluation + news_organizer 渠道时延）
# ===========================================================================
_banner("⑥ 统计报表（成交对账统计 + 渠道时延）")

conn = db.get_connection()
ev = quick_sim.evaluation(conn)
conn.close()
ov = ev["overall"]
check("⑥  overall 2 胜 0 负 100%",
      ov["wins"] == 2 and ov["losses"] == 0 and ov["winrate_pct"] == 100.0,
      f"wins={ov['wins']} losses={ov['losses']} winrate={ov['winrate_pct']}%")
check("⑥  按资产归因含 NVDA/BTC", {a["asset"] for a in ev["by_asset"]} >= {"NVDA", "BTC"},
      str([(a["asset"], a["winrate_pct"]) for a in ev["by_asset"]]))
check("⑥  按模型归因", len(ev["by_model"]) >= 1,
      str([(m["model"], m["wins"]) for m in ev["by_model"]]))

lat = news_organizer.source_latency_stats(hours=24)
keys = {b["source"] for b in lat}
check("⑥  渠道时延报表归组 yahoo_stock/reuters", {"yahoo_stock", "reuters"} <= keys,
      str(keys))

# ===========================================================================
# 环节 ⑦ 跨进程去重复检（子进程不同 PYTHONHASHSEED）
# ===========================================================================
_banner("⑦ 跨进程去重复检（子进程 PYTHONHASHSEED=1/2 重放同一批新闻）")

self_path = os.path.abspath(__file__)
dedup_ok = True
for seed in ("1", "2"):
    p = subprocess.run(
        [sys.executable, self_path],
        env={**os.environ, "TRIDENT_DB_PATH": _DB_PATH, "E2E_PHASE": "dedup",
             "E2E_ITEMS_JSON": json.dumps(items, ensure_ascii=False),
             "PYTHONHASHSEED": seed},
        capture_output=True, text=True,
    )
    line = [x for x in p.stdout.splitlines() if x.startswith("E2E_DEDUP_RESULT")]
    inserted = int(line[0].split("inserted=")[1].split()[0]) if line else -999
    ok = inserted == 0
    dedup_ok = dedup_ok and ok
    check(f"⑦  子进程 seed={seed} 重放入库 0 条(去重命中)", ok, f"inserted={inserted}")
check("⑦  跨进程去重整体通过(sha256 marker 稳定)", dedup_ok, "")

# ===========================================================================
# 汇总
# ===========================================================================
_banner("汇总报告")
fails = [r for r in REPORT if r[1] == "FAIL"]
for _sec, tag, _detail in REPORT:
    print(f"  {tag:4s} {_sec}")
print("-" * 72)
print(f"  共 {len(REPORT)} 项断言，PASS {len(REPORT) - len(fails)} / FAIL {len(fails)}")
if fails:
    for sec, tag, detail in fails:
        print(f"  [FAIL] {sec}: {detail}")
    sys.exit(1)
print("  ✅ 全链路验证通过")
print(f"  临时库(可复查): {_DB_PATH}")