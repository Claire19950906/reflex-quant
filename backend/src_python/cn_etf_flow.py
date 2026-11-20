#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cn_etf_flow.py — v321.3 P3: A股宽基 ETF 资金流 (国家基金/机构资金的**真实观测**)
================================================================================

为什么需要它
------------
需求 #14 问的是「清楚现在市场的基本面: 是集体撤资、集中投资ai、撤离ai、
**机构大资金投资、国家基金投资**、什么角色投资」。原先的 `state_fund_inflow`
只做到了**代理推断** (proxy=true) —— 加密侧没有"国家队"这个观测对象。

但 A 股有: **国家队/机构买 ETF 就是宽基 ETF 的主力净流入**。
东方财富的 push2his 资金流接口直接给这条线, 实测 0.18~0.91s 可达, **不需要代理**。

字段含义 (fields2 顺序):
    f51 日期 | f52 主力净流入 | f53 小单 | f54 中单 | f55 大单 | **f56 超大单**
    f57 主力净占比 | ... | f61 超大单净占比
「超大单」= 机构/国家队量级的单子, 是判断"什么角色在买"最直接的证据。

实测 (2026-11-06):
    沪深300ETF  主力 -7.32 亿  超大单 -10.41 亿
    创业板ETF   主力 -18.46 亿 超大单 -15.75 亿
    科创50ETF   主力 -4.69 亿  超大单  -8.64 亿
→ 宽基 ETF 主力资金净流出 = 机构/国家队在减仓

用法
----
    from cn_etf_flow import fetch_etf_flow, snapshot_flow
    rows = fetch_etf_flow()          # 拉全部标的的日线资金流
    snapshot_flow(conn, rows)        # 落 macro_snapshots
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import urllib.request
from typing import Any, Dict, List, Optional

__all__ = ["ETF_TARGETS", "fetch_etf_flow", "snapshot_flow", "backfill_flow",
           "flow_state"]

_UA = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
       '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')
_BASE = 'https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get'
_F1 = 'f1,f2,f3,f7'
_F2 = 'f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61'
_TIMEOUT = 12

# 宽基 ETF = 国家队/机构配置 A 股的主要通道
ETF_TARGETS = [
    ('1.510300', 'hs300',   '沪深300ETF'),
    ('1.510050', 'sz50',     '上证50ETF'),
    ('1.510500', 'zz500',    '中证500ETF'),
    ('1.588000', 'kc50',     '科创50ETF'),
    ('0.159915', 'cyb',      '创业板ETF'),
    ('1.513100', 'nasdaq',   '纳指ETF'),
    ('1.518880', 'gold_etf', '黄金ETF'),
    ('1.513050', 'hs300hk',  '港股通沪深300ETF'),
]


def _http_json(url: str, timeout: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """GET 一个 JSON。timeout=None → 用模块级默认 _TIMEOUT。

    v321.3 P2: 之前 `fetch_etf_flow(timeout=...)` 的形参**根本没接进来**
    (内部写死模块级 _TIMEOUT) → 签名承诺可调、实际不可调 = 接口撒谎。
    现在 timeout 一路透传到这里。
    """
    req = urllib.request.Request(url, headers={
        'User-Agent': _UA, 'Accept': '*/*', 'Referer': 'https://quote.eastmoney.com/'})
    with urllib.request.urlopen(req, timeout=(timeout or _TIMEOUT)) as r:
        return json.loads(r.read().decode('utf-8', 'ignore'))


def _parse_klines(klines: List[str]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for line in klines or []:
        parts = str(line).split(',')
        if len(parts) < 11:
            continue
        try:
            out.append({
                'date': parts[0],
                'main_net': float(parts[1]),      # 主力净流入(元)
                'small_net': float(parts[2]),
                'mid_net': float(parts[3]),
                'big_net': float(parts[4]),
                'xl_net': float(parts[5]),        # 超大单净流入(元) ← 机构量级
                'main_pct': float(parts[6]),
                'xl_pct': float(parts[10]),
            })
        except (ValueError, IndexError):
            continue
    return out


def fetch_etf_flow(limit: int = 30, timeout: Optional[float] = None,
                   delay: float = 0.0) -> List[Dict[str, Any]]:
    """拉全部标的的日线资金流。返回 [{key, name, rows:[...]}]。

    limit  = Eastmoney URL 的 lmt → **历史天数**（klt=101 日K）。
             日常采集用 30 足够; 想要历史分位样本就用大值(如 250)做一次回填。
    timeout= 单次 HTTP 超时秒数; None → 用模块级 _TIMEOUT。
    delay  = 标的之间的间隔秒数 (默认 0 = 不等待)。
             ⚠️ v321.3 P2: **必须留间隔** —— 实测 8 个标的连续无间隔抓取
             + 每小时一轮 (192 次/天) 会触发东方财富 IP 风控:
             TLS 握手成功但 HTTP 直接空响应 (RemoteDisconnected), 全部节点一致。
             加 0.8s 间隔把 8 个请求摊开到 ~7 秒, 行为更像正常客户端。

    单个标的失败**不影响其它标的** (如实记录, 不静默吞)。
    """
    out: List[Dict[str, Any]] = []
    for idx, (secid, key, name) in enumerate(ETF_TARGETS):
        if idx and delay > 0:
            time.sleep(delay)
        url = (f'{_BASE}?lmt={int(limit)}&klt=101&secid={secid}'
               f'&fields1={_F1}&fields2={_F2}')
        try:
            d = _http_json(url, timeout=timeout)
        except Exception as exc:
            print(f"[CN-ETF-FLOW] ⚠️ {name} 拉取失败（原先静默吞掉）: "
                  f"{type(exc).__name__}: {str(exc)[:90]}", flush=True)
            continue
        data = (d or {}).get('data') or {}
        rows = _parse_klines(data.get('klines') or [])
        if not rows:
            print(f"[CN-ETF-FLOW] ⚠️ {name} 无资金流数据 (data={bool(data)})", flush=True)
            continue
        out.append({'key': key, 'name': name, 'secid': secid, 'rows': rows})
    return out


def _state_path() -> str:
    d = os.environ.get("TRIDENT_STATE_DIR") or os.path.join(
        os.path.expanduser("~"), ".trident", "state")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        pass
    return os.path.join(d, "cn_etf_flow.last")


def _read_state() -> Dict[str, int]:
    """读取上次尝试/成功的时刻。"""
    try:
        with open(_state_path(), "r") as f:
            raw = (f.read() or "").strip()
        if raw.startswith("{"):
            d = json.loads(raw)
            return {"last_ok": int(d.get("last_ok") or 0),
                    "last_attempt": int(d.get("last_attempt") or 0)}
        return {"last_ok": int(raw or 0), "last_attempt": int(raw or 0)}
    except (OSError, ValueError, TypeError):
        return {"last_ok": 0, "last_attempt": 0}


def _write_state(last_ok: int, last_attempt: int) -> None:
    """记录「上次尝试 / 上次成功」的时刻 —— 用于失败退避。

    为什么需要 (v321.3 P2): 实测触发东方财富 IP 风控后, cron 仍在继续请求
    → 持续撞墙只会让风控**更严**, 且毫无产出。正确行为是失败就**安静退避**。

    ⚠️ 退避必须基于 **last_attempt** 而不是 last_ok:
    若只看 last_ok, 那么"一次都没成功过"时 last_ok=0 → 退避永不触发
    → 外网持续不通时会**每轮都去撞**。last_attempt 无论成败都更新,
    保证"失败也要等一会儿再试"。
    """
    try:
        with open(_state_path(), "w") as f:
            f.write(json.dumps({"last_ok": int(last_ok),
                                "last_attempt": int(last_attempt)}))
    except OSError:
        pass


def _trade_day_ts(date_str: str) -> int:
    """交易日 'YYYY-MM-DD' → 当日 15:00 (收盘后数据确定) 的 unix 时间戳。

    为什么用「交易日 + 15:00」而不是采集时刻:
      · 回填写入的必须是**可复现的固定 ts** —— 同一份历史重复回填不会产生重复行;
      · ts 必须**小于**当天采集的 ts, 这样 flow_state 按 ts DESC 取值时
        永远优先拿到"当日采集"的最新值, 回填不会顶掉实时数据;
      · 分位按 payload.$.date 去重, 所以 ts 只用于排序与新鲜度判断。
    """
    try:
        t = time.mktime(time.strptime(str(date_str).strip(), "%Y-%m-%d"))
    except (ValueError, TypeError):
        return 0
    return int(t) + 15 * 3600


def backfill_flow(conn: sqlite3.Connection, bundles: Optional[List[Dict[str, Any]]] = None,
                  ) -> Dict[str, Any]:
    """**历史回填**: 把 bundles 里每一天的历史都落进 macro_snapshots。

    为什么需要它 (v321.3 P2):
      regime 的历史分位要求 ≥20 个**交易日**样本, 而日常采集每小时写一条 →
      一年才 20 个点, 且同一交易日的多条采集值相同 → 分位永远算不出来。
      Eastmoney 的 lmt 参数一次能拉几百个交易日 → 一次性回填 → 分位立刻可用。

    口径与日常采集**完全一致**:
      · 每行 ts = 该交易日 15:00 (固定 → 幂等, 重复回填不产生重复行)
      · 明细行 key = cn_etf.<target>.main_net / .xl_net
      · 合计行 key = cn_etf_total.main_net / .xl_net, **只有该交易日 8 个标的
        全部齐全才写** (少一个就是口径不一致, 写进去会污染分位)
      · payload 带 date / targets / n_ok / n_expected, 供读侧按交易日去重
    """
    bundles = bundles if bundles is not None else fetch_etf_flow(limit=250)
    n_expected = len(ETF_TARGETS)
    # 按交易日分组: date -> {target_key: row}
    by_date: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for b in bundles:
        for row in (b.get('rows') or []):
            d = str(row.get('date') or '').strip()
            if not d:
                continue
            by_date.setdefault(d, {})[b['key']] = row

    written = 0
    days_total = 0
    days_full = 0
    for date in sorted(by_date):
        ts = _trade_day_ts(date)
        if not ts:
            continue
        days_total += 1
        per_day = by_date[date]
        name_by_key = {b['key']: b.get('name') for b in bundles}
        # ── 明细行 (每个标的自己那天的真实值) ──
        for key, row in per_day.items():
            for field in ('main_net', 'xl_net'):
                mk = f"cn_etf.{key}.{field}"
                try:
                    conn.execute("DELETE FROM macro_snapshots WHERE metric_key=? AND ts=?",
                                 (mk, ts))
                    conn.execute(
                        "INSERT INTO macro_snapshots "
                        "(ts, category, source, metric_key, value, unit, status, payload) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        (ts, 'cn_etf_flow', 'eastmoney', mk,
                         float(row.get(field) or 0.0), 'cny', 'ok',
                         json.dumps({'name': name_by_key.get(key), 'date': date,
                                     'field': field, 'backfill': True},
                                    ensure_ascii=False)),
                    )
                    written += 1
                except sqlite3.Error as exc:
                    print(f"[CN-ETF-FLOW] ⚠️ 回填落库失败: {type(exc).__name__}: "
                          f"{str(exc)[:100]} ({mk})", flush=True)
        # ── 合计行 (口径不完整就不写!) ──
        n_ok = len(per_day)
        if n_ok < n_expected:
            print(f"[CN-ETF-FLOW] ⚠️ {date} 只有 {n_ok}/{n_expected} 个标的 → "
                  f"不写合计 (口径不一致, 写了会污染分位)", flush=True)
            continue
        days_full += 1
        main_sum = sum(float(per_day[k].get('main_net') or 0.0) for k in per_day)
        xl_sum = sum(float(per_day[k].get('xl_net') or 0.0) for k in per_day)
        for mk, val in (('cn_etf_total.main_net', main_sum),
                        ('cn_etf_total.xl_net', xl_sum)):
            try:
                conn.execute("DELETE FROM macro_snapshots WHERE metric_key=? AND ts=?",
                             (mk, ts))
                conn.execute(
                    "INSERT INTO macro_snapshots "
                    "(ts, category, source, metric_key, value, unit, status, payload) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (ts, 'cn_etf_flow', 'eastmoney', mk, float(val), 'cny', 'ok',
                     json.dumps({'targets': sorted(per_day.keys()), 'n_ok': n_ok,
                                 'n_expected': n_expected, 'date': date,
                                 'backfill': True}, ensure_ascii=False)),
                )
                written += 1
            except sqlite3.Error as exc:
                print(f"[CN-ETF-FLOW] ⚠️ 回填合计落库失败: {type(exc).__name__}: "
                      f"{str(exc)[:100]} ({mk})", flush=True)
    try:
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        return {"written": 0, "days_total": days_total, "days_full": days_full,
                "ok": False}
    return {"written": written, "days_total": days_total, "days_full": days_full,
            "n_expected": n_expected, "ok": days_full > 0}


def snapshot_flow(conn: sqlite3.Connection, bundles: Optional[List[Dict[str, Any]]] = None
                  ) -> Dict[str, Any]:
    """把资金流落到 macro_snapshots (key = cn_etf.<key>.main_net / .xl_net)。"""
    bundles = bundles if bundles is not None else fetch_etf_flow()
    now = int(time.time())
    written = 0
    main_sum = 0.0
    xl_sum = 0.0
    ok_keys: List[str] = []          # ← 实际拿到真实数据的 target
    ok_dates: List[str] = []         # ← 交易日 (用于历史分位按日去重)
    for b in bundles:
        rows = b.get('rows') or []
        if not rows:
            continue
        last = rows[-1]
        date = last.get('date') or ''
        ok_keys.append(b['key'])
        if date and date not in ok_dates:
            ok_dates.append(date)
        for field in ('main_net', 'xl_net'):
            mk = f"cn_etf.{b['key']}.{field}"
            val = float(last.get(field) or 0.0)
            if field == 'main_net':
                main_sum += val
            else:
                xl_sum += val
            try:
                # macro_snapshots 没有 (metric_key, ts) 唯一约束 → 不能用
                # ON CONFLICT (实测报 "does not match any PRIMARY KEY or UNIQUE
                # constraint")。改成「先删同键再插」。
                conn.execute("DELETE FROM macro_snapshots WHERE metric_key=? AND ts=?",
                             (mk, now))
                conn.execute(
                    "INSERT INTO macro_snapshots "
                    "(ts, category, source, metric_key, value, unit, status, payload) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (now, 'cn_etf_flow', 'eastmoney', mk, val,
                     'cny', 'ok',
                     json.dumps({'name': b.get('name'), 'secid': b.get('secid'),
                                 'date': date, 'field': field,
                                 'rows_tail': rows[-3:]}, ensure_ascii=False)),
                )
                written += 1
            except sqlite3.Error as exc:
                print(f"[CN-ETF-FLOW] ⚠️ 落库失败（原先静默吞掉）: "
                      f"{type(exc).__name__}: {str(exc)[:100]} ({mk})", flush=True)
    # ── 合计指标 (给 regime 算历史分位用) ──
    # ⚠️ v321.3 P2: **全部 target 都拉取失败时不能写合计** ——
    # 之前会把 main_total=0 / xl_total=0 当成"真实观测"写进库,
    # regime 侧读到全 0 会**误判成资金流出** (假数据污染真实信号)。
    # 现在: 一个都没成功 → 不写任何合计, 直接返回失败。
    if not ok_keys:
        print("[CN-ETF-FLOW] ⚠️ 无有效数据 (全部 target 拉取失败) → "
              "不写合计, 避免全 0 假数据污染 regime 判定", flush=True)
        try:
            conn.rollback()      # 丢弃本次空跑的任何写
        except sqlite3.Error:
            pass
        return {"written": 0, "targets": 0, "n_ok": 0,
                "main_total": 0.0, "xl_total": 0.0, "ok": False}
    # 部分成功 → status 标 partial, 并在 payload 里记清楚成功/失败清单
    n_expected = len(bundles)
    status = 'ok' if len(ok_keys) >= n_expected else 'partial'
    for mk, val in (('cn_etf_total.main_net', main_sum),
                    ('cn_etf_total.xl_net', xl_sum)):
        try:
            conn.execute("DELETE FROM macro_snapshots WHERE metric_key=? AND ts=?",
                         (mk, now))
            conn.execute(
                "INSERT INTO macro_snapshots "
                "(ts, category, source, metric_key, value, unit, status, payload) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (now, 'cn_etf_flow', 'eastmoney', mk, float(val), 'cny', status,
                 json.dumps({'targets': ok_keys, 'n_ok': len(ok_keys),
                             'n_expected': n_expected,
                             'date': (ok_dates[0] if ok_dates else '')},
                            ensure_ascii=False)),
            )
            written += 1
        except sqlite3.Error as exc:
            print(f"[CN-ETF-FLOW] ⚠️ 合计落库失败（原先静默吞掉）: "
                  f"{type(exc).__name__}: {str(exc)[:100]} ({mk})", flush=True)
    try:
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
    return {"written": written, "targets": len(ok_keys),
            "n_ok": len(ok_keys), "n_expected": n_expected,
            "main_total": main_sum, "xl_total": xl_sum, "ok": True}


def flow_state(conn: sqlite3.Connection) -> Dict[str, Any]:
    """汇总当前 A股 ETF 主力资金状态 (判定「国家基金/机构 在买还是在撤」)。"""
    out: Dict[str, Any] = {"targets": {}, "main_total": 0.0, "xl_total": 0.0,
                           "n": 0, "sample_size": 0, "latest_ts": 0, "age_s": None}
    try:
        rows = conn.execute(
            "SELECT metric_key, value, ts FROM macro_snapshots "
            "WHERE metric_key LIKE 'cn_etf.%' AND status='ok' AND value IS NOT NULL "
            "ORDER BY ts DESC"
        ).fetchall()
    except sqlite3.Error as exc:
        print(f"[CN-ETF-FLOW] ⚠️ 读取失败（原先静默吞掉）: "
              f"{type(exc).__name__}: {str(exc)[:100]}", flush=True)
        return out
    latest: Dict[str, Dict[str, float]] = {}
    latest_ts = 0
    for r in rows:
        # 用**索引**访问 (不依赖 row_factory —— sqlite3.connect 默认不设它)
        mk = str(r[0])
        val = r[1]
        ts = int(r[2] or 0)
        if ts > latest_ts:
            latest_ts = ts
        parts = mk.split(".")
        if len(parts) != 3:
            continue
        _, key, field = parts
        cur = latest.setdefault(key, {})
        # ⚠️ rows 是 ts DESC —— **第一次**遇到才是最新值。
        # 之前无条件覆盖 → 最后留下的是**最旧**的一条 (取到历史首帧)。
        if field in cur:
            continue
        cur[field] = float(val or 0.0)
    for key, d in sorted(latest.items()):
        main = d.get("main_net")
        xl = d.get("xl_net")
        out["targets"][key] = {"main_net": main, "xl_net": xl}
        if main is not None:
            out["main_total"] += main
        if xl is not None:
            out["xl_total"] += xl
        out["n"] += 1
    try:
        out["sample_size"] = conn.execute(
            "SELECT COUNT(*) FROM macro_snapshots WHERE metric_key LIKE 'cn_etf.%' "
            "AND status='ok' AND value IS NOT NULL").fetchone()[0]
    except sqlite3.Error:
        out["sample_size"] = 0
    # 新鲜度: 最新一条观测距今多久 (外网挂掉/采集失败时 → 数据是陈的,
    # 不能当成"今天的资金动向"用, 否则会把几天前的旧值讲成当日行情)
    out["latest_ts"] = latest_ts
    if latest_ts:
        out["age_s"] = max(0, int(time.time()) - latest_ts)
    return out


def main() -> int:
    import argparse
    import os
    ap = argparse.ArgumentParser(description="A股宽基 ETF 资金流采集 (国家基金/机构观测)")
    ap.add_argument("--db", default=os.environ.get("TRIDENT_DB_PATH") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "trident_event_bus.db"))
    ap.add_argument("--limit", type=int, default=30,
                    help="Eastmoney lmt = 历史天数 (日常采集 30 足够)")
    ap.add_argument("--timeout", type=float, default=None,
                    help="单次 HTTP 超时秒数 (默认用模块级 12s)")
    ap.add_argument("--backfill", action="store_true",
                    help="**历史回填**: 把 --limit 指定天数的全部历史按交易日落库 "
                         "(一次性灌满分位样本; 幂等, 可重复跑)")
    ap.add_argument("--delay", type=float, default=0.8,
                    help="标的之间的请求间隔秒数 (默认 0.8; 0 = 不等待)")
    ap.add_argument("--min-interval-min", type=float, default=60.0,
                    help="最短重试间隔(分钟): 距上次**成功**采集不足则本轮跳过 "
                         "(失败退避; 防止持续撞墙加重 IP 风控)")
    ap.add_argument("--force", action="store_true",
                    help="忽略冷却期, 强制抓取一次 (手动排查用)")
    args = ap.parse_args()
    conn = sqlite3.connect(args.db, timeout=120)
    conn.execute("PRAGMA busy_timeout=60000")
    try:
        if args.backfill:
            bundles = fetch_etf_flow(limit=args.limit, timeout=args.timeout,
                                     delay=args.delay)
            st = backfill_flow(conn, bundles)
            print(f"[CN-ETF-FLOW][回填] {json.dumps(st, ensure_ascii=False)}")
            n_days = conn.execute(
                "SELECT COUNT(DISTINCT json_extract(payload,'$.date')) FROM macro_snapshots "
                "WHERE metric_key='cn_etf_total.main_net' AND status='ok'"
            ).fetchone()[0]
            print(f"[CN-ETF-FLOW][回填] 库内可用的**交易日**样本数 = {n_days} "
                  f"(regime 分位需要 >=20)")
            print("[CN-ETF-FLOW] 当前状态:", json.dumps(flow_state(conn), ensure_ascii=False))
            return 0
        # ── 失败退避: 距上次**尝试**不足 min_interval → 本轮直接跳过, 不去撞墙 ──
        # 基于 last_attempt (无论上次成败) → 外网持续不通时也不会每轮都撞
        if not args.force and args.min_interval_min > 0:
            stt = _read_state()
            last_attempt = stt.get("last_attempt") or 0
            if last_attempt:
                gap_min = (time.time() - last_attempt) / 60.0
                if gap_min < args.min_interval_min:
                    print(f"[CN-ETF-FLOW] ⏳ 距上次尝试仅 {gap_min:.1f} 分钟 "
                          f"(< {args.min_interval_min:.0f}) → 本轮跳过, 不请求 "
                          f"(避免持续撞墙加重 IP 风控)", flush=True)
                    return 0
        bundles = fetch_etf_flow(limit=args.limit, timeout=args.timeout,
                                 delay=args.delay)
        st = snapshot_flow(conn, bundles)
        print(f"[CN-ETF-FLOW] {json.dumps(st, ensure_ascii=False)}")
        stt = _read_state()
        now_ts = int(time.time())
        _write_state(now_ts if st.get("ok") else (stt.get("last_ok") or 0), now_ts)
        if st.get("ok"):
            pass
        else:
            print("[CN-ETF-FLOW] ⏸ 本轮无有效数据 → 进入冷却期, 稍后再试", flush=True)
        for b in bundles:
            last = (b.get('rows') or [{}])[-1]
            print(f"  {b.get('name'):<14} 主力 {last.get('main_net', 0)/1e8:>8.2f}亿  "
                  f"超大单 {last.get('xl_net', 0)/1e8:>8.2f}亿  ({last.get('date')})")
        print("[CN-ETF-FLOW] 当前状态:", json.dumps(flow_state(conn), ensure_ascii=False))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
