#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_signal_pipeline — 测试数据 & 通道体检（回答"我该怎么测数据"）。

这个脚本解决两件事：
  1. **通道体检**（--check）：每个 LLM 通道发一个极小的请求（max_tokens=8），
     报告哪个通道活着、哪个 429/401/超时。流量消耗可忽略（每通道 ~10 token）。
     → 「流量干没了」「一个信号都没有」时，**先跑这个**，3 秒就知道是不是通道问题。

  2. **注入测试数据**（--inject）：往 raw_news 插一条可控的新闻，绕过门卫
     （直接 is_noise=0 / gate_unavailable=0），等待引擎认领 → 分析 → 落库，
     最后把这条新闻产出的信号（ai_decisions）打出来。
     → 验证「引擎能不能真的从新闻走到信号」这条完整链路。

用法：
    python3 scripts/test_signal_pipeline.py --check
    python3 scripts/test_signal_pipeline.py --inject
    python3 scripts/test_signal_pipeline.py --inject --text "美联储降息 25bp，鲍威尔称通胀已受控"
    python3 scripts/test_signal_pipeline.py --cleanup
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src_python"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import config # noqa: E402

TEST_SOURCE_PREFIX = "TEST:"

DEFAULT_TEST_TEXT = (
    "测试数据：美联储意外宣布降息25个基点，鲍威尔在新闻发布会上表示"
    "「通胀已经得到控制，劳动力市场正在逐步降温」，市场预期年内还将降息两次。"
)


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, timeout=60.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000;")
    return conn


# ── 通道体检 ────────────────────────────────────────────────────────────

def _probe_one(model_cfg: dict, timeout: float = 25.0) -> dict:
    """对单个通道发一个极小请求，返回健康状态（不消耗多少 token）。"""
    import requests

    out = {
        "id": model_cfg.get("id", "?"),
        "label": model_cfg.get("label", model_cfg.get("id", "?")),
        "ok": False,
        "status": "",
        "latency_ms": 0,
        "error": "",
    }
    try:
        session = requests.Session()
        session.trust_env = False
        headers = {
            "Authorization": f"Bearer {model_cfg.get('api_key', '')}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": model_cfg.get("request_model") or model_cfg.get("id"),
            "messages": [{"role": "user", "content": "reply with the single word: ok"}],
            "max_tokens": 8,
            "temperature": 0.0,
        }
        t0 = time.perf_counter()
        resp = session.post(
            f"{model_cfg.get('api_base', '').rstrip('/')}/chat/completions",
            json=payload, headers=headers, timeout=timeout,
        )
        out["latency_ms"] = int((time.perf_counter() - t0) * 1000)
        out["status"] = str(resp.status_code)
        if resp.status_code >= 400:
            out["error"] = resp.text[:160].replace("\n", " ")
        else:
            body = resp.json()
            content = (body.get("choices", [{}])[0].get("message", {})
                       .get("content", "") or "").strip()
            out["ok"] = bool(content)
            if not content:
                out["error"] = "空响应 (finish_reason=%s)" % body.get("choices", [{}])[0].get("finish_reason")
    except Exception as exc:
        out["error"] = "%s: %s" % (type(exc).__name__, str(exc)[:140])
    return out


def channel_check() -> int:
    """对 main / gate 两个用途的池子做通道体检。"""
    print("══ 通道体检 ══" + "═" * 52)
    any_ok = False
    for purpose in ("main", "gate"):
        try:
            pool = config.get_pool_for_purpose(purpose)
        except Exception as exc:
            print(f"\n[purpose={purpose}] 取池失败: {type(exc).__name__}: {exc}")
            continue
        print(f"\n[purpose={purpose}] 池内 {len(pool)} 个通道")
        if not pool:
            print("  ⚠️  池为空 —— 该用途没有任何可用通道")
            continue
        for m in pool:
            r = _probe_one(m)
            flag = "✅" if r["ok"] else "❌"
            print(f"  {flag} {r['id']}")
            print(f"      status={r['status'] or '-'} latency={r['latency_ms']}ms")
            if r["error"]:
                print(f"      error: {r['error']}")
            if r["ok"]:
                any_ok = True
    print()
    if any_ok:
        print("结论：至少有一个通道可用 —— 引擎跑起来就应能产出信号。")
        print("      若仍无信号，看 runtime/ai_worker_progress.json 卡在哪一步。")
    else:
        print("结论：❌ 全部通道不可用 —— 这就是「一个信号都没有」的直接原因。")
        print("      429 = 配额打满（已减少重试次数）；401/403 = key 失效（换 .env）；")
        print("      超时/网络 = 外网不通或 DNS 异常。")
    return 0 if any_ok else 1


# ── 注入测试数据并等待信号 ─────────────────────────────────────────────

def inject_test_news(text: str) -> int:
    """插入一条测试新闻（绕过门卫），返回 news_id。"""
    conn = _connect()
    try:
        now = int(time.time())
        cur = conn.execute(
            "INSERT INTO raw_news"
            " (source, content, timestamp, ts, status, is_noise, relevance_score,"
            "  published_at, gate_verdict, gate_unavailable, retry_count)"
            " VALUES (?, ?, ?, ?, 'PENDING', 0, 0.9, ?, '', 0, 0)",
            (TEST_SOURCE_PREFIX + "manual",
             "[hash:test] " + text,
             time.strftime("%Y-%m-%d %H:%M:%S"),
             now,
             time.strftime("%Y-%m-%d %H:%M:%S")),
        )
        conn.commit()
        news_id = int(cur.lastrowid)
        print(f"✅ 已注入测试新闻 news_id={news_id}")
        print(f"   内容: {text[:80]}")
        return news_id
    finally:
        conn.close()


def wait_for_signal(news_id: int, timeout_s: float = 300.0,
                     poll_s: float = 5.0) -> int:
    """轮询等待这条新闻被分析完，然后打印产出的信号。"""
    print(f"\n⏳ 等待引擎认领并分析（最多 {timeout_s:.0f}s）…")
    print("   提示：引擎需在运行 —— launchctl list | grep com.trident.engine")
    deadline = time.time() + timeout_s
    last_status = None
    while time.time() < deadline:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT status, retry_count FROM raw_news WHERE id = ?", (news_id,)
            ).fetchone()
            status = row["status"] if row else "GONE"
            retry = row["retry_count"] if row else 0
        finally:
            conn.close()
        if status != last_status:
            print(f"   [{time.strftime('%H:%M:%S')}] raw_news.status = {status}"
                  f" (retry_count={retry})")
            last_status = status
        if status in ("DONE", "FAILED"):
            break
        time.sleep(poll_s)
    else:
        print("⏰ 超时未完成。查 runtime/ai_worker_progress.json 看引擎卡在哪一步。")
        return 2

    conn = _connect()
    try:
        if status == "FAILED":
            print("❌ 这条新闻最终 FAILED —— 通道仍不可用，先跑 --check 排查。")
            return 1
        dec = conn.execute(
            "SELECT id, target_asset, suggested_action, sentiment_score,"
            "       agent_model_id, created_at, reasoning, invalidation_condition"
            " FROM ai_decisions WHERE news_id = ? ORDER BY id DESC",
            (news_id,),
        ).fetchall()
        if not dec:
            print("⚠️  raw_news 已 DONE，但 ai_decisions 里没有对应信号 —— "
                  "分析在写库前丢了，查引擎日志。")
            return 1
        print(f"\n🎉 产出 {len(dec)} 条信号：")
        for d in dec:
            print("  " + "─" * 60)
            print(f"  decision_id : {d['id']}")
            print(f"  标的        : {d['target_asset']}")
            print(f"  建议        : {d['suggested_action']}  "
                  f"(score={d['sentiment_score']})")
            print(f"  模型        : {d['agent_model_id']}")
            print(f"  时间        : {d['created_at']}")
            reason = (d["reasoning"] or "").replace("\n", " ")
            print(f"  推理        : {reason[:300]}")
            if d["invalidation_condition"]:
                print(f"  失效条件    : {d['invalidation_condition'][:160]}")
        return 0
    finally:
        conn.close()


def cleanup() -> int:
    """删除此前注入的测试新闻（及其信号）。"""
    conn = _connect()
    try:
        ids = [r[0] for r in conn.execute(
            "SELECT id FROM raw_news WHERE source LIKE ?", (TEST_SOURCE_PREFIX + "%",)
        ).fetchall()]
        if not ids:
            print("没有测试数据需要清理。")
            return 0
        qs = ",".join(str(int(i)) for i in ids)
        conn.execute(f"DELETE FROM ai_decisions WHERE news_id IN ({qs})")
        conn.execute(f"DELETE FROM raw_news WHERE id IN ({qs})")
        conn.commit()
        print(f"🧹 已清理 {len(ids)} 条测试新闻及其信号")
        return 0
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="测试数据 & 通道体检")
    ap.add_argument("--check", action="store_true", help="通道体检（发极小请求探活）")
    ap.add_argument("--inject", action="store_true", help="注入测试新闻并等待出信号")
    ap.add_argument("--cleanup", action="store_true", help="清理注入过的测试数据")
    ap.add_argument("--text", default=DEFAULT_TEST_TEXT, help="自定义测试新闻内容")
    ap.add_argument("--timeout", type=float, default=300.0, help="等待信号的超时秒数")
    args = ap.parse_args()

    if args.cleanup:
        return cleanup()
    if args.inject:
        nid = inject_test_news(args.text)
        return wait_for_signal(nid, timeout_s=args.timeout)
    # 默认就是体检
    return channel_check()


if __name__ == "__main__":
    raise SystemExit(main())
