#!/usr/bin/env python3
"""v202 R5 cron: review_insights -> causal_chains.trigger_words.

调用方式:
  python3 cron_causal_expand.py [--days=30] [--reset]

参数:
  --days N        看最近 N 天的 review_insights（默认 30）
  --reset         清空现有 trigger_words 后只写白名单关键词（原内容会先备份到 CSV）
  --dry-run       只输出结果不写库

设计原则:
  - 错峰 #2 kg_review（04:00），本脚本 04:10 跑
  - 失败不 panic：写日志后退出 0（让 launchd 不重复拉起）
  - 备份 CSV 默认路径 /Users/claire/.trident/log/causal_chains_trigger_backup.csv
"""
import argparse
import json
import os
import sys
import time
import traceback
from datetime import datetime


LOG_DIR = "/Users/claire/.trident/log"
LOG_FILE = os.path.join(LOG_DIR, "cron_causal_expand.log")
BACKUP_CSV = os.path.join(LOG_DIR, "causal_chains_trigger_backup.csv")

# 确保 src_python 在 path 里（脚本可能从任意目录调用）
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)


def _log(msg):
    ts = datetime.now().isoformat(sep=" ", timespec="seconds")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def main():
    parser = argparse.ArgumentParser(description="v202 R5 cron: review_insights -> causal_chains.trigger_words")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--min-samples", type=int, default=2)
    parser.add_argument("--max-new", type=int, default=5)
    parser.add_argument("--reset", action="store_true",
                        help="清空现有 trigger_words 后只写白名单关键词（原内容会先备份到 CSV）")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--backup-csv", type=str, default=BACKUP_CSV)
    args = parser.parse_args()

    _log(f"=== R5 cron start days={args.days} reset={args.reset} dry_run={args.dry_run} ===")
    started = time.time()

    try:
        from causal_chain_expander import expand_from_insights

        result = expand_from_insights(
            days=args.days,
            min_samples_for_new_chain=args.min_samples,
            max_new_chains=args.max_new,
            dry_run=args.dry_run,
            reset=args.reset,
        )
        result["elapsed_sec"] = round(time.time() - started, 2)
        _log(f"RESULT: {json.dumps(result, ensure_ascii=False)}")
        _log(f"=== R5 cron done ({result['elapsed_sec']}s) ===")
        return 0
    except Exception as exc:
        _log(f"ERROR: {type(exc).__name__}: {exc}")
        _log(traceback.format_exc())
        # 失败不 panic — launchd 不要再拉起
        return 0


if __name__ == "__main__":
    sys.exit(main())
