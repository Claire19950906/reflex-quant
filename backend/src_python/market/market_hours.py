"""全球市场开闭市时间表 — 覆盖 Trident 主交易品种及常见权益市场 (WTI/XAU/BTC/美股/韩股等)。

设计原则:
  - 零外部依赖 (zoneinfo 库 + 静态时区表);
  - current_status() 输出每个市场实时状态,供前端弹幕条渲染;
  - 不存数据库,不写文件,启动零成本。

v213 (海力士坑修复):
  · 新增 kr_equity 韩国 KOSPI 市场（SKHYNIX 主体交易所，含 11:30-12:30 午休）
  · status_for("SKHYNIX") → kr_equity 状态
  · review_agent / abnormal_detector 可复用 status_for(asset) 判定非活跃时段
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

TZ_ET = ZoneInfo("America/New_York")
TZ_HK = ZoneInfo("Asia/Hong_Kong")
TZ_KST = ZoneInfo("Asia/Seoul")
TZ_UTC = ZoneInfo("UTC")

MARKETS: List[Dict[str, Any]] = [
    {
        "id": "wti", "name": "WTI原油", "exchange": "NYMEX", "tag": "USOIL",
        "tz": TZ_ET, "weekdays": (0, 1, 2, 3, 4), "sunday_open": "18:00",
    },
    {
        "id": "xau", "name": "COMEX黄金", "exchange": "COMEX", "tag": "GOLD",
        "tz": TZ_ET, "weekdays": (0, 1, 2, 3, 4), "sunday_open": "18:00",
    },
    {
        "id": "btc", "name": "BTC 现货/永续", "exchange": "Crypto 24x7",
        "tag": "BTC", "tz": TZ_UTC, "weekdays": (0, 1, 2, 3, 4, 5, 6),
        "always_open": True,
        "active_sessions": [
            {"name": "Asia",   "tz": TZ_HK, "open": "08:00", "close": "20:00"},
            {"name": "Europe", "tz": TZ_ET, "open": "07:00", "close": "16:00"},
            {"name": "US",     "tz": TZ_ET, "open": "13:00", "close": "22:00"},
        ],
    },
    {
        "id": "us_equity", "name": "美股 (NYSE/NASDAQ)",
        "exchange": "NYSE/NASDAQ", "tag": "US",
        "tz": TZ_ET, "weekdays": (0, 1, 2, 3, 4),
        "premarket":  ("04:00", "09:30"),
        "regular":    ("09:30", "16:00"),
        "afterhours": ("16:00", "20:00"),
    },
    # v213: 韩国 KOSPI（SKHYNIX 主体交易所）。
    # 交易时间 09:00-15:30 KST，含 11:30-12:30 午休。周末关闭。
    {
        "id": "kr_equity", "name": "韩国 KOSPI (SKHYNIX)",
        "exchange": "KRX/KOSPI", "tag": "KR",
        "tz": TZ_KST, "weekdays": (0, 1, 2, 3, 4),
        "premarket":  ("08:30", "09:00"),
        "regular":    ("09:00", "15:30"),
        "afterhours": ("15:30", "18:00"),
        # 午休：11:30-12:30 KST。status 仍记 open，但 sub_status=lunch_break，
        # 供下游 abnormal_detector / review_agent 在午休收紧异动阈值。
        "lunch_break": ("11:30", "12:30"),
    },
]


def _parse_hhmm(hhmm: str):
    h, m = hhmm.split(":")
    return int(h), int(m)


def _minutes(dt: datetime) -> int:
    return dt.hour * 60 + dt.minute


def _status_us_equity(m, now_local):
    wd = now_local.weekday()
    if wd not in m["weekdays"]:
        return {"status": "closed", "sub_status": "weekend",
                "minutes_to_next": None, "next_event": "周一盘前 04:00 ET"}
    cur = _minutes(now_local)
    pre_o = _parse_hhmm(m["premarket"][0])[0]*60 + _parse_hhmm(m["premarket"][0])[1]
    pre_c = _parse_hhmm(m["premarket"][1])[0]*60 + _parse_hhmm(m["premarket"][1])[1]
    reg_o = _parse_hhmm(m["regular"][0])[0]*60 + _parse_hhmm(m["regular"][0])[1]
    reg_c = _parse_hhmm(m["regular"][1])[0]*60 + _parse_hhmm(m["regular"][1])[1]
    aft_o = _parse_hhmm(m["afterhours"][0])[0]*60 + _parse_hhmm(m["afterhours"][0])[1]
    aft_c = _parse_hhmm(m["afterhours"][1])[0]*60 + _parse_hhmm(m["afterhours"][1])[1]
    if pre_o <= cur < pre_c:
        return {"status": "open", "sub_status": "premarket",
                "minutes_to_next": reg_o - cur, "next_event": "美股开盘"}
    if reg_o <= cur < reg_c:
        return {"status": "open", "sub_status": "regular",
                "minutes_to_next": reg_c - cur, "next_event": "美股收盘"}
    if aft_o <= cur < aft_c:
        return {"status": "open", "sub_status": "afterhours",
                "minutes_to_next": aft_c - cur, "next_event": "盘后结束"}
    if cur < pre_o:
        return {"status": "closed", "sub_status": "before_pre",
                "minutes_to_next": pre_o - cur, "next_event": "盘前开始"}
    return {"status": "closed", "sub_status": "after_close",
            "minutes_to_next": (24*60 - cur) + pre_o, "next_event": "次日盘前"}


def _status_kr_equity(m, now_local):
    """v213: 韩国 KOSPI 状态判定（含 11:30-12:30 午休标记 lunch_break）。

    与 _status_us_equity 类似，但多一项 lunch_break 用于 K.sub_status，
    供 abnormal_detector / review_agent 收紧午休时段异动阈值。
    """
    wd = now_local.weekday()
    if wd not in m["weekdays"]:
        return {"status": "closed", "sub_status": "weekend",
                "minutes_to_next": None, "next_event": "周一盘前 08:30 KST"}
    cur = _minutes(now_local)
    pre_o = _parse_hhmm(m["premarket"][0])[0]*60 + _parse_hhmm(m["premarket"][0])[1]
    pre_c = _parse_hhmm(m["premarket"][1])[0]*60 + _parse_hhmm(m["premarket"][1])[1]
    reg_o = _parse_hhmm(m["regular"][0])[0]*60 + _parse_hhmm(m["regular"][0])[1]
    reg_c = _parse_hhmm(m["regular"][1])[0]*60 + _parse_hhmm(m["regular"][1])[1]
    aft_o = _parse_hhmm(m["afterhours"][0])[0]*60 + _parse_hhmm(m["afterhours"][0])[1]
    aft_c = _parse_hhmm(m["afterhours"][1])[0]*60 + _parse_hhmm(m["afterhours"][1])[1]
    lunch = m.get("lunch_break")
    lunch_o = _parse_hhmm(lunch[0])[0]*60 + _parse_hhmm(lunch[0])[1] if lunch else None
    lunch_c = _parse_hhmm(lunch[1])[0]*60 + _parse_hhmm(lunch[1])[1] if lunch else None
    if pre_o <= cur < pre_c:
        return {"status": "open", "sub_status": "premarket",
                "minutes_to_next": reg_o - cur, "next_event": "韩股开盘"}
    if reg_o <= cur < reg_c:
        # 午休命中子午休 — status 仍 open, 提示子午休让下游收紧阈值。
        if lunch_o is not None and lunch_o <= cur < lunch_c:
            return {"status": "open", "sub_status": "lunch_break",
                    "minutes_to_next": lunch_c - cur, "next_event": "午休结束"}
        return {"status": "open", "sub_status": "regular",
                "minutes_to_next": reg_c - cur, "next_event": "韩股收盘"}
    if aft_o <= cur < aft_c:
        return {"status": "open", "sub_status": "afterhours",
                "minutes_to_next": aft_c - cur, "next_event": "盘后结束"}
    if cur < pre_o:
        return {"status": "closed", "sub_status": "before_pre",
                "minutes_to_next": pre_o - cur, "next_event": "盘前开始"}
    return {"status": "closed", "sub_status": "after_close",
            "minutes_to_next": (24*60 - cur) + pre_o, "next_event": "次日盘前"}


def _status_24x7(m, now_local):
    active = []
    for sess in m.get("active_sessions", []):
        cur = _minutes(datetime.now(sess["tz"]))
        o, c = _parse_hhmm(sess["open"]), _parse_hhmm(sess["close"])
        if o[0]*60+o[1] <= cur < c[0]*60+c[1]:
            active.append(sess["name"])
    return {"status": "open", "sub_status": "always",
            "active_sessions": active,
            "minutes_to_next": None, "next_event": "永续"}


def _status_futures(m, now_local):
    wd = now_local.weekday()
    cur = _minutes(now_local)
    if wd == 6 and cur >= 18*60:
        return {"status": "open", "sub_status": "futures_evening",
                "minutes_to_next": 24*60 - cur, "next_event": "周一凌晨继续"}
    if wd == 6:
        return {"status": "closed", "sub_status": "sunday_before_open",
                "minutes_to_next": 18*60 - cur, "next_event": "周日 18:00 ET 开盘"}
    if wd == 5:
        return {"status": "closed", "sub_status": "saturday",
                "minutes_to_next": ((24-18)*60 + (24*60 - cur)) if cur < 18*60 else None,
                "next_event": "周日 18:00 ET 开盘"}
    if cur >= 1*60:
        return {"status": "open", "sub_status": "futures_day",
                "minutes_to_next": 24*60 - cur, "next_event": "次日凌晨继续"}
    return {"status": "open", "sub_status": "futures_overnight",
            "minutes_to_next": 1*60 - cur, "next_event": "凌晨 01:00 ET 重启"}


def _enrich(m, now_local):
    base = {"id": m["id"], "name": m["name"], "exchange": m["exchange"],
            "tag": m["tag"], "locale_now": now_local.strftime("%Y-%m-%d %H:%M %Z")}
    if m.get("always_open"):
        base.update(_status_24x7(m, now_local))
    elif m["id"] == "kr_equity":
        # v213: KR 单独走 `_status_kr_equity`（带 lunch_break）
        base.update(_status_kr_equity(m, now_local))
    elif "premarket" in m:
        base.update(_status_us_equity(m, now_local))
    else:
        base.update(_status_futures(m, now_local))
    return base


def current_status() -> List[Dict[str, Any]]:
    """返回每个市场的实时状态。"""
    return [_enrich(m, datetime.now(m["tz"])) for m in MARKETS]


def status_for(asset: str) -> Optional[Dict[str, Any]]:
    """按 asset 名称 (BTC/XAU/WTI/US) 取单条状态。

    v213: 新增 SKHYNIX/海力士 → kr_equity 映射。
    """
    asset_up = (asset or "").upper()
    key_map = {"BTC": "btc", "ETH": "btc", "XAU": "xau", "GOLD": "xau",
               "WTI": "wti", "OIL": "wti", "US": "us_equity", "STOCK": "us_equity",
               "NVDA": "us_equity", "TSLA": "us_equity",
               # v213: 海力士单独映射到韩国 KOSPI，避免和 us_equity 混算时段
               "SKHYNIX": "kr_equity", "000660": "kr_equity"}
    kid = key_map.get(asset_up, asset_up.lower())
    for m in MARKETS:
        if m["id"] == kid:
            return _enrich(m, datetime.now(m["tz"]))
    return None


def is_kr(asset: str) -> bool:
    """v213 工具方法：判定 asset 是否归属韩国市场（SKHYNIX 等）。

    review_agent / abnormal_detector 在做"市场时段收紧"前先快速判断。
    """
    return str(asset or "").upper() in {"SKHYNIX", "000660", "KOSPI", "KR"}


if __name__ == "__main__":
    import json
    print(json.dumps(current_status(), ensure_ascii=False, indent=2))