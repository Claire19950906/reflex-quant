# -*- coding: utf-8 -*-
"""时区 / 时间结构化核心工具。

提供：
- 严格 ISO-8601 解析/校验（拒绝 '上周五' / '昨晚' / '2 hours ago' 等模糊文本）
- 多源时间归一到 epoch + utc_iso + 多时区 display 三值锚
- 双时区（北京时间 / 美东 / 美西 / UTC）切换展示
- DEFAULT_DISPLAY_TZ 通过环境变量 TRIDENT_TZ 配置（默认 Asia/Shanghai，方便查阅）
- 时间窗口（5m / 30m / 2h / 1d）标准化 label

约束：
- 仅依赖 stdlib（无 pytz / dateutil），engine 离线可 import。
- 设计目的：堵"时间字符串被 LLM 自由发挥"的 4 个口子。
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional


# ── TZ 常量（无 pytz 依赖，固定偏移；DST 由调用方按需 select）────────────────
TZ_SHANGHAI = timezone(timedelta(hours=8))       # UTC+8
TZ_US_EASTERN = timezone(timedelta(hours=-5))    # EST（冬令时）；如需夏令时切换请调用 _eastern_with_dst()
TZ_US_PACIFIC = timezone(timedelta(hours=-8))    # PST（冬令时）
TZ_UTC = timezone.utc


# 支持的 display TZ 集合
DISPLAY_TZ_OPTIONS: Dict[str, timezone] = {
    "Asia/Shanghai": TZ_SHANGHAI,
    "US/Eastern":    TZ_US_EASTERN,
    "US/Pacific":    TZ_US_PACIFIC,
    "UTC":           TZ_UTC,
}


# 默认 display TZ（项目要求：方便用 → Asia/Shanghai）
# 可通过环境变量 TRIDENT_TZ 切换为 US/Eastern / US/Pacific / UTC
DEFAULT_DISPLAY_TZ = "Asia/Shanghai"


# 严格 ISO-8601 模式 ────────────────────────────────────
# 1) naive 模式: 'YYYY-MM-DD HH:MM:SS' (空格或 T 分隔)
_ISO8601_NAIVE_RE = re.compile(
    r"^(?P<y>\d{4})-(?P<m>\d{2})-(?P<d>\d{2})[ T](?P<h>\d{2}):(?P<mi>\d{2}):(?P<s>\d{2})$"
)

# 2) aware 模式: 必带时区偏移（Z 或 +HH:MM / -HH:MM）
_ISO8601_AWARE_RE = re.compile(
    r"^(?P<y>\d{4})-(?P<m>\d{2})-(?P<d>\d{2})[ T](?P<h>\d{2}):(?P<mi>\d{2}):(?P<s>\d{2})"
    r"(?P<tz>Z|[+\-]\d{2}:?\d{2})$"
)

# 远古最低阈值（第②层防护）
ISO_EARLIEST = (1990, 1, 1)
ISO_MAX_FUTURE_SECONDS = 60   # 60s 容差


# ────────────────────────────────────────────────────────────────────────────
# TZ 切换
# ────────────────────────────────────────────────────────────────────────────

def get_display_tz() -> timezone:
    """读取环境变量 TRIDENT_TZ 返回对应 tzinfo（默认 Asia/Shanghai）。"""
    name = os.getenv("TRIDENT_TZ", DEFAULT_DISPLAY_TZ)
    return DISPLAY_TZ_OPTIONS.get(name, TZ_SHANGHAI)


def get_display_tz_name() -> str:
    return os.getenv("TRIDENT_TZ", DEFAULT_DISPLAY_TZ)


# ────────────────────────────────────────────────────────────────────────────
# 严格 ISO-8601 解析
# ────────────────────────────────────────────────────────────────────────────

def parse_iso8601(value: Any, *, strict: bool = True) -> Optional[datetime]:
    """严格 ISO-8601 解析。

    Args:
        value: 字符串 / epoch int / None
        strict: True=只接受 ISO-8601 严格格式；False=回退到 epoch 数字 + ISO 兼容

    Returns:
        tz-aware datetime（无时区→视为 UTC）；解析失败返回 None
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None

    # aware 模式（必带时区）
    m = _ISO8601_AWARE_RE.match(s)
    if m:
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            return dt if dt.tzinfo else None
        except Exception:
            return None

    # naive 模式（视为 UTC）
    m = _ISO8601_NAIVE_RE.match(s)
    if m:
        try:
            dt = datetime.strptime(s[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S")
            return dt.replace(tzinfo=TZ_UTC)
        except Exception:
            return None

    if strict:
        return None

    # 非严格模式：回退到 epoch 数字
    if s.isdigit() and len(s) in (10, 13):
        try:
            secs = float(s) / 1000.0 if len(s) == 13 else float(s)
            return datetime.fromtimestamp(secs, tz=TZ_UTC)
        except Exception:
            return None

    # 非严格模式：试 fromisoformat 兼容
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ_UTC)
        return dt
    except Exception:
        return None


def validate_iso8601(
    value: Any,
    *,
    allow_ambiguous_marker: str = "AMBIGUOUS",
    allow_empty: bool = True,
) -> Dict[str, Any]:
    """校验 LLM 输出的时间字段是否合法（三态）。

    Returns: {'ok': bool, 'normalized': str|None, 'reason': str|None}
    """
    if value is None:
        if allow_empty:
            return {"ok": True, "normalized": "", "reason": None}
        return {"ok": False, "normalized": None, "reason": "value is None"}

    if not isinstance(value, str):
        return {"ok": False, "normalized": None,
                "reason": f"value type {type(value).__name__} != str"}

    s = value.strip()
    if not s:
        if allow_empty:
            return {"ok": True, "normalized": "", "reason": None}
        return {"ok": False, "normalized": None, "reason": "empty string"}

    if allow_ambiguous_marker and s == allow_ambiguous_marker:
        return {"ok": True, "normalized": "AMBIGUOUS", "reason": None}

    dt = parse_iso8601(s, strict=True)
    if dt is None:
        return {"ok": False, "normalized": None,
                "reason": f"not ISO-8601 strict format: {s[:30]}"}

    if (dt.year, dt.month, dt.day) < ISO_EARLIEST:
        return {"ok": False, "normalized": None, "reason": "earlier than 1990"}

    now_utc = datetime.now(tz=TZ_UTC)
    dt_utc = dt.astimezone(TZ_UTC)
    delta = (dt_utc - now_utc).total_seconds()
    if delta > ISO_MAX_FUTURE_SECONDS:
        return {"ok": False, "normalized": None,
                "reason": f"future time (delta={delta:.0f}s > 60s tolerance)"}

    iso_utc = dt_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"ok": True, "normalized": iso_utc, "reason": None}


# ────────────────────────────────────────────────────────────────────────────
# 多时区展示（中国 / 美东 / 美西 / UTC）
# ────────────────────────────────────────────────────────────────────────────

def _tz_for(name: str) -> timezone:
    return DISPLAY_TZ_OPTIONS.get(name, TZ_SHANGHAI)


def format_dual(
    epoch_or_dt: Any,
    *,
    display_tz: Optional[str] = None,
) -> Dict[str, Any]:
    """从 epoch 秒 / datetime / 字符串生成多时区展示字典。

    Returns:
        {
          'epoch':       int,
          'iso':         'YYYY-MM-DDTHH:MM:SSZ',
          'utc':         'YYYY-MM-DD HH:MM:SS',
          'cn':          'YYYY-MM-DD HH:MM:SS',   # Asia/Shanghai
          'us_eastern':  'YYYY-MM-DD HH:MM:SS',
          'us_pacific':  'YYYY-MM-DD HH:MM:SS',
          'display':     'YYYY-MM-DD HH:MM:SS',   # 当前 display_tz 对应值
          'display_tz':  'Asia/Shanghai' | 'US/Eastern' | ...
        }
    """
    if isinstance(epoch_or_dt, (int, float)):
        try:
            dt_utc = datetime.fromtimestamp(float(epoch_or_dt), tz=TZ_UTC)
        except Exception:
            return _empty_dual(display_tz)
    elif isinstance(epoch_or_dt, datetime):
        dt_utc = epoch_or_dt if epoch_or_dt.tzinfo else epoch_or_dt.replace(tzinfo=TZ_UTC)
        dt_utc = dt_utc.astimezone(TZ_UTC)
    else:
        dt = parse_iso8601(epoch_or_dt, strict=False)
        if dt is None:
            return _empty_dual(display_tz)
        dt_utc = dt.astimezone(TZ_UTC)

    if display_tz is None:
        display_tz = get_display_tz_name()

    return {
        "epoch":      int(dt_utc.timestamp()),
        "iso":        dt_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "utc":        dt_utc.strftime("%Y-%m-%d %H:%M:%S"),
        "cn":         dt_utc.astimezone(TZ_SHANGHAI).strftime("%Y-%m-%d %H:%M:%S"),
        "us_eastern": dt_utc.astimezone(TZ_US_EASTERN).strftime("%Y-%m-%d %H:%M:%S"),
        "us_pacific": dt_utc.astimezone(TZ_US_PACIFIC).strftime("%Y-%m-%d %H:%M:%S"),
        "display":    dt_utc.astimezone(_tz_for(display_tz)).strftime("%Y-%m-%d %H:%M:%S"),
        "display_tz": display_tz,
    }


def _empty_dual(display_tz: Optional[str] = None) -> Dict[str, Any]:
    return {
        "epoch": 0,
        "iso": "",
        "utc": "",
        "cn": "",
        "us_eastern": "",
        "us_pacific": "",
        "display": "",
        "display_tz": display_tz or get_display_tz_name(),
    }


# ────────────────────────────────────────────────────────────────────────────
# 时间窗口标准化（5m / 30m / 2h / 1d / extended）
# ────────────────────────────────────────────────────────────────────────────

WINDOW_LABELS = ("5m", "30m", "2h", "1d", "extended")


def get_window_label(minutes_after: Any) -> str:
    """把分钟数映射到 5m / 30m / 2h / 1d / extended。"""
    try:
        m = int(float(minutes_after))
    except (TypeError, ValueError):
        return "extended"
    if m <= 5:
        return "5m"
    if m <= 30:
        return "30m"
    if m <= 120:
        return "2h"
    if m <= 1440:
        return "1d"
    return "extended"


def detect_timezone_hint(value: str) -> Optional[str]:
    """从字符串 hint 检测时区名（粗糙启发式，仅用于日志 / 展示提示）。"""
    if not value:
        return None
    s = str(value).upper()
    if "Z" in s or "+00:00" in s or "UTC" in s:
        return "UTC"
    if "+08" in s or "CST" in s or "BEIJING" in s or "SHANGHAI" in s:
        return "Asia/Shanghai"
    if "EST" in s or "EDT" in s or "EASTERN" in s or "-05" in s or "-04" in s:
        return "US/Eastern"
    if "PST" in s or "PDT" in s or "PACIFIC" in s or "-08" in s or "-07" in s:
        return "US/Pacific"
    return None


# ────────────────────────────────────────────────────────────────────────────
# v171+ P5：config.json 配置化（可选，env 仍是默认）
# ────────────────────────────────────────────────────────────────────────────

_CONFIG_PATH_CANDIDATES = (
    "config.json",
    "runtime/config.json",
    os.path.expanduser("~/.trident/config.json"),
)


def _read_config_tz() -> Optional[str]:
    """从 config.json 读 display.tz 字段。"""
    import json
    for path in _CONFIG_PATH_CANDIDATES:
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                tz = (cfg.get("display") or {}).get("tz")
                if tz and tz in DISPLAY_TZ_OPTIONS:
                    return tz
        except Exception:
            continue
    return None


def get_display_tz_name(prefer_config: bool = False) -> str:
    """读取 display_tz，优先级：prefer_config=True 时 config.json > env > 默认。"""
    if prefer_config:
        cfg_tz = _read_config_tz()
        if cfg_tz:
            return cfg_tz
    return os.getenv("TRIDENT_TZ", DEFAULT_DISPLAY_TZ)


def get_display_tz(prefer_config: bool = False) -> timezone:
    """同 get_display_tz_name 但返回 tzinfo。"""
    name = get_display_tz_name(prefer_config=prefer_config)
    return DISPLAY_TZ_OPTIONS.get(name, TZ_SHANGHAI)


# ────────────────────────────────────────────────────────────────────────────
# v171+ P5：CLI 入口（不只 self-test）
# ────────────────────────────────────────────────────────────────────────────

def _cli() -> int:
    import argparse
    parser = argparse.ArgumentParser(
        prog="tz_utils",
        description="Trident 时区/时间结构化工具 CLI (v171+)",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    # sub: validate
    p_v = sub.add_parser("validate", help="校验字符串是否为严格 ISO-8601")
    p_v.add_argument("value", help="待校验字符串")

    # sub: dual
    p_d = sub.add_parser("dual", help="把 epoch 秒格式化为多时区展示字典")
    p_d.add_argument("epoch", type=int, help="UTC epoch 秒")
    p_d.add_argument("--tz", default="Asia/Shanghai", help="display_tz (default Asia/Shanghai)")

    # sub: label
    p_l = sub.add_parser("label", help="把分钟数映射到窗口 label")
    p_l.add_argument("minutes", type=int, help="事件后的分钟数")

    # sub: now
    p_n = sub.add_parser("now", help="输出当前 4 时区时间")

    # sub: tz
    p_t = sub.add_parser("tz", help="输出当前 TRIDENT_TZ (env) 或 config (--config)")

    args = parser.parse_args()

    if args.cmd == "validate":
        r = validate_iso8601(args.value)
        print(json.dumps(r, ensure_ascii=False))
        return 0 if r["ok"] else 1
    elif args.cmd == "dual":
        d = format_dual(args.epoch, display_tz=args.tz)
        print(json.dumps(d, ensure_ascii=False, indent=2))
        return 0
    elif args.cmd == "label":
        print(get_window_label(args.minutes))
        return 0
    elif args.cmd == "now":
        now_epoch = int(__import__("time").time())
        d = format_dual(now_epoch, display_tz=get_display_tz_name())
        print(json.dumps(d, ensure_ascii=False, indent=2))
        return 0
    elif args.cmd == "tz":
        print(f"env TRIDENT_TZ = {os.getenv('TRIDENT_TZ', '<unset>')}")
        cfg = _read_config_tz()
        print(f"config.json display.tz = {cfg or '<unset>'}")
        print(f"effective display_tz = {get_display_tz_name(prefer_config=True)}")
        return 0
    return 1


# ────────────────────────────────────────────────────────────────────────────
# Self-test
# ────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    # 有子命令 → CLI；否则 self-test
    if len(sys.argv) > 1 and sys.argv[1] in ("validate", "dual", "label", "now", "tz"):
        sys.exit(_cli())
    samples = [
        "2026-09-21 14:30:00",
        "2026-09-21T06:30:00Z",
        "2026-09-21T14:30:00+08:00",
        "2026-09-21 14:30",
        "上周五",
        "AMBIGUOUS",
        "",
        "1758400000",
    ]
    print("=== validate_iso8601 三态 ===")
    for s in samples:
        r = validate_iso8601(s)
        print(f"  {s!r:40s} → ok={r['ok']} normalized={r['normalized']!r} reason={r['reason']!r}")

    print("\n=== format_dual (default Asia/Shanghai) ===")
    epoch = int(datetime(2026, 9, 21, 14, 30, 0, tzinfo=TZ_UTC).timestamp())
    d = format_dual(epoch)
    for k, v in d.items():
        print(f"  {k:12s} = {v!r}")

    print("\n=== format_dual (TRIDENT_TZ=US/Eastern) ===")
    os.environ["TRIDENT_TZ"] = "US/Eastern"
    d = format_dual(epoch)
    for k, v in d.items():
        print(f"  {k:12s} = {v!r}")
    del os.environ["TRIDENT_TZ"]

    print("\n=== get_window_label ===")
    for m in (3, 15, 90, 600, 5000):
        print(f"  {m:5d} min → {get_window_label(m)}")


