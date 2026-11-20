#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v317: 回填历史 raw_news 的全网溯源字段（cross_net_first_seen_ts / cross_net_source）。

背景
----
引擎侧有个 v317 bug：_extract_cross_net_from_text 抽到时间却没匹配到机构名时，
会留下 `cross_net_first_seen_ts` 有值、`cross_net_source` 为空的组合；而
_sanitize_llm_result 里的"来源兜底"被嵌在"时间兜底"分支内（只有 cn_ts 为空时才走），
于是来源永远补不上 → _validate_cross_net_ts 第⑤层（source 需 ≥4 字符）拒收
→ **时间与来源双双被清空**。实测 2268 行 net_origin_info 里明明有可用的时间，
却全部落库为空，前台 NetLagBadge 只能显示灰色的"未溯源"。

引擎侧已修（engine/ai_worker.py）。本脚本把同一套推导口径
（_derive_cross_net_from_noi + _validate_cross_net_ts）套用到**历史数据**上，
保证"脚本回填的"与"引擎新写的"完全同源。

安全策略（重要）
----------------
1. **默认只读演练**（dry-run），必须显式加 --apply 才写库。
2. **只填空**：仅处理 cross_net_first_seen_ts 为空/None 的行，
   绝不覆盖已有的合法溯源值。
3. **不发明来源**：来源必须能从 net_origin_info 里读出来（"来源:XXX" 段，
   或整段 noi 兜底）；读不出来就整行跳过，不猜。
4. **不倒填未来时间**：ts 晚于入库时间（+60s 容差）的行跳过 ——
   引擎侧第④层护栏（v317 修复后才真正生效）同样会拒收它们。
5. 写库前自动备份 DB（--backup-dir，默认与 DB 同目录）。

用法
----
    python3 scripts/backfill_cross_net_provenance.py                 # 演练（只读）
    python3 scripts/backfill_cross_net_provenance.py --apply         # 真正写库（自动备份）
    python3 scripts/backfill_cross_net_provenance.py --limit 50      # 只看前 50 行
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src_python"))

from engine.ai_worker import _derive_cross_net_from_noi, _validate_cross_net_ts

# DB 路径解析 —— 与项目主约定保持一致：
#   config.DB_PATH = os.getenv("TRIDENT_DB_PATH") or <backend>/trident_event_bus.db
# 所以这里优先读 TRIDENT_DB_PATH（项目全局约定），TRIDENT_DB 仅作向后兼容的次选，
# 最后才落到本脚本的默认路径。避免"用 --db 传了 A、环境变量却是 B"的错位。
DB_PATH = (
    os.environ.get("TRIDENT_DB_PATH")
    or os.environ.get("TRIDENT_DB")
    or os.path.join(HERE, "..", "trident_event_bus.db")
)


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _backup(db_path: str, backup_dir: str) -> str:
    """用 SQLite backup API 做**一致性**快照（写库前必做）。

    不用 shutil.copy2 —— 线上 DB 是 WAL 模式，直接拷文件可能拷到一个
    "主库文件 + 未合并的 WAL"的不一致副本。sqlite3 的 backup API 会走
    在线备份协议，产出的副本保证自洽。
    """
    os.makedirs(backup_dir, exist_ok=True)
    base = os.path.basename(db_path)
    stamp = __import__("datetime").datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = os.path.join(backup_dir, f"{base}.{stamp}.bak")
    src = sqlite3.connect(db_path, timeout=30)
    try:
        out = sqlite3.connect(dst)
        try:
            src.backup(out)
        finally:
            out.close()
    finally:
        src.close()
    return dst


def _prune_invalid(conn: sqlite3.Connection, db_path: str, args) -> int:
    """复核模式：把**已有**溯源值但过不了校验的行清空。

    为什么需要：v317 之前 `_validate_cross_net_ts` 第④层（"不能晚于入库"）是**死的** ——
    旧写法 `strptime(our_ts_iso[:19], "%Y-%m-%d %H:%M:%S")` 解析不了真实落库格式
    '2026-10-11T16:05:15+08:00'（T 分隔 + 偏移）→ except 分支直接放行 →
    引擎写进去的历史脏数据（溯源时间晚于入库几小时）就一直留在库里。
    v317 把该层换成 time_utils.parse_ts_to_bj 后护栏才真正生效。

    清空时**同时**清掉 cross_net_source：时间戳被否 → 它作为"依据"的来源同样不可信，
    留着就变成"有出处但没时间"的半截状态。引擎对校验失败的行本来就不写任何字段，
    清空 = 把这两列恢复到引擎当初会产生的状态。
    """
    rows = conn.execute(
        """
        SELECT id, timestamp, net_origin_info, cross_net_first_seen_ts, cross_net_source
        FROM raw_news
        WHERE cross_net_first_seen_ts IS NOT NULL AND cross_net_first_seen_ts <> ''
        ORDER BY id
        """
    ).fetchall()
    if args.limit:
        rows = rows[: args.limit]

    invalid = []
    kept = 0
    ambiguous = 0
    for r in rows:
        v = r["cross_net_first_seen_ts"]
        if (v or "").strip() == "AMBIGUOUS":
            ambiguous += 1
            continue
        ok = _validate_cross_net_ts(
            v, r["cross_net_source"] or "", r["timestamp"] or "")
        if ok:
            kept += 1
        else:
            invalid.append(r)

    print(f"[prune] 已填溯源值的行: {len(rows)}")
    print(f"[prune] 复核通过（保留）: {kept}")
    print(f"[prune] AMBIGUOUS 哨兵值（保留）: {ambiguous}")
    print(f"[prune] 复核不通过（将清空）: {len(invalid)}")

    if args.verbose:
        for r in invalid[:20]:
            print(f"    id={r['id']} ts={r['cross_net_first_seen_ts']!r} "
                  f"ingested={r['timestamp']!r} src={str(r['cross_net_source'])[:40]!r}")

    if not args.apply:
        print("[prune] 演练结束，未写库。加 --apply 真正执行。")
        conn.close()
        return 0

    backup_dir = args.backup_dir or os.path.dirname(db_path)
    bak = _backup(db_path, backup_dir)
    print(f"[prune] 已备份 DB → {bak}")

    try:
        conn.execute("BEGIN IMMEDIATE")
        # 注意：两列都是 `TEXT NOT NULL DEFAULT ''`（不是 NULL 可空），
        # 所以"清空"必须写成空串 ''，写成 NULL 会撞 NOT NULL 约束。
        conn.executemany(
            """
            UPDATE raw_news
               SET cross_net_first_seen_ts = '', cross_net_source = ''
             WHERE id = ?
            """,
            [(r["id"],) for r in invalid],
        )
        conn.commit()
    except Exception as exc:
        conn.rollback()
        print(f"[prune] 写库失败（已回滚）: {exc}", file=sys.stderr)
        conn.close()
        return 1

    print(f"[prune] 完成：清空 {len(invalid)} 行")
    conn.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="回填 raw_news 全网溯源字段（默认演练）")
    ap.add_argument("--apply", action="store_true", help="真正写库（默认只演练）")
    ap.add_argument("--db", default=DB_PATH, help="DB 路径")
    ap.add_argument("--limit", type=int, default=0, help="最多处理多少行（0=不限）")
    ap.add_argument("--backup-dir", default=None, help="备份目录（默认与 DB 同目录）")
    ap.add_argument("--verbose", action="store_true", help="打印逐行明细")
    ap.add_argument(
        "--prune-invalid",
        action="store_true",
        help="复核模式：重新校验**已有**溯源值的行，把不合法的清空"
             "（v317 之前第④层护栏失效——strptime 解析不了 'T 分隔+08:00' 格式，"
             "except 分支直接放行——导致历史脏数据混了进来）",
    )
    args = ap.parse_args()

    db_path = os.path.abspath(args.db)
    if not os.path.exists(db_path):
        print(f"[backfill] DB 不存在: {db_path}", file=sys.stderr)
        return 2

    conn = _connect(db_path)

    if args.prune_invalid:
        return _prune_invalid(conn, db_path, args)

    rows = conn.execute(
        """
        SELECT id, timestamp, net_origin_info,
               cross_net_first_seen_ts, cross_net_source
        FROM raw_news
        WHERE (cross_net_first_seen_ts IS NULL OR cross_net_first_seen_ts = '')
          AND net_origin_info IS NOT NULL AND net_origin_info <> ''
        ORDER BY id
        """
    ).fetchall()
    if args.limit:
        rows = rows[: args.limit]

    print(f"[backfill] 候选行（cn_ts 为空且 noi 非空）: {len(rows)}")
    print(f"[backfill] 模式: {'写库' if args.apply else '演练（只读）'}")

    planned = []
    skipped_no_source = 0
    skipped_future = 0
    skipped_invalid = 0
    already_ok = 0

    for r in rows:
        noi = (r["net_origin_info"] or "").strip()
        ts, src = _derive_cross_net_from_noi(noi)
        if not ts:
            skipped_no_source += 1
            continue
        if not src:
            # 不发明来源：读不出出处就整行跳过
            skipped_no_source += 1
            continue
        # 与引擎同一套 5 层校验（含 v317 修好的"不能晚于入库"）
        valid = _validate_cross_net_ts(ts, src, r["timestamp"] or "")
        if not valid:
            skipped_future += 1
            continue
        if r["cross_net_source"] and len(r["cross_net_source"]) >= 4:
            already_ok += 1
        planned.append((valid, src, r["id"]))

    print(f"[backfill] 计划回填: {len(planned)}")
    print(f"[backfill]   跳过(无可用时间/来源): {skipped_no_source}")
    print(f"[backfill]   跳过(未过校验: 晚于入库/格式非法): {skipped_future}")
    print(f"[backfill]   已有合法来源(仅补时间): {already_ok}")

    if args.verbose:
        for ts, src, rid in planned[:20]:
            print(f"    id={rid} ts={ts!r} src={src[:60]!r}")

    if not args.apply:
        print("[backfill] 演练结束，未写库。加 --apply 真正执行。")
        conn.close()
        return 0

    backup_dir = args.backup_dir or os.path.dirname(db_path)
    bak = _backup(db_path, backup_dir)
    print(f"[backfill] 已备份 DB → {bak}")

    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.executemany(
            """
            UPDATE raw_news
               SET cross_net_first_seen_ts = ?, cross_net_source = ?
             WHERE id = ?
               AND (cross_net_first_seen_ts IS NULL OR cross_net_first_seen_ts = '')
            """,
            [(ts, src, rid) for ts, src, rid in planned],
        )
        conn.commit()
    except Exception as exc:
        conn.rollback()
        print(f"[backfill] 写库失败（已回滚）: {exc}", file=sys.stderr)
        conn.close()
        return 1

    changed = conn.execute(
        "SELECT COUNT(*) FROM raw_news "
        "WHERE cross_net_first_seen_ts IS NOT NULL AND cross_net_first_seen_ts <> ''"
    ).fetchone()[0]
    print(f"[backfill] 完成：本次回填 {len(planned)} 行；"
          f"现共有精确时间 {changed} 行")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
