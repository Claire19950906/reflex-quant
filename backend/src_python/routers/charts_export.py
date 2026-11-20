# -*- coding: utf-8 -*-
"""通用 K 线 / 导出路由（结构切片，2026-11-17）。

从 api_server.py 搬来 3 条端点：
  /api/klines/{symbol}（按资产路由真实数据源，失败回退库内历史，绝不造假 K 线）
  /api/export/signals（八列模拟交易 xlsx，给 Streamlit 直读）
  /api/export/excel（当日买卖信号 xlsx）
K 线抓取/落库/回读与 get_replay_signals / _paper_metrics / _do_export_excel
仍留在 api_server，router 用 _api() 回引，避免循环 import。
"""
from __future__ import annotations

import asyncio
import io
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

router = APIRouter()


def _api():
    """延迟回引 api_server（K 线助手与导出核心逻辑仍在原文件）。"""
    import api_server
    return api_server


@router.get("/api/klines/{symbol}")
async def get_klines(symbol: str, limit: int = 72, interval: str = "1h"):
    # 通用图表接口：按资产路由真实数据源（加密→币安、XAU→OKX），失败回退库内历史。
    # 绝不 mock 造 K 线（此前 XAU/WTI 拿不到币安合约就造假数据）。
    # 兼容入参：BTCUSDT/ETHUSDT/XAUUSD/WTIUSD/GOLD/OIL/CLUSDT → 规范化到资产代码。
    interval = interval if interval in ("1m", "15m", "1h", "4h", "1d") else "1h"
    pair_map = {
        "BTCUSDT": "BTC", "ETHUSDT": "ETH", "SOLUSDT": "SOL",
        "XAUUSD": "XAU", "GOLD": "XAU", "XAU-USDT-SWAP": "XAU",
        "WTIUSD": "WTI", "OIL": "WTI", "CLUSDT": "WTI",
    }
    asset = pair_map.get(symbol.upper(), symbol.upper())
    bn = _api()._fetch_binance_klines(asset, interval)
    if bn is not None:
        bars = bn[0] or []
        if bars:
            _api()._store_klines(asset, interval, bars)  # 顺手落库，实时失败时兜底
            return bars[:limit]
    # 实时失败 → 回退库内历史（WTI 1d 金十不支持 → 回退库内 FRED 日线；其余周期用 interval 本身）
    bars = _api()._load_klines_from_db(asset, interval, limit)
    if bars:
        return bars
    raise HTTPException(status_code=503, detail=f"{asset} 行情缺失：真实 K 线不可用（无源/网络失败），已停机不凑数")


@router.get("/api/export/signals")
async def export_paper_signals():
    """导出 Streamlit 可直接读取的八列模拟交易记录。"""
    signals = await _api().get_replay_signals(settled=-1, limit=500)
    import xlsxwriter
    output = io.BytesIO()
    workbook = xlsxwriter.Workbook(output)
    worksheet = workbook.add_worksheet("模拟交易")
    headers = ["时间", "新闻内容", "品种", "方向", "入场价", "最大浮盈%", "最大浮亏%", "大模型归因"]
    for column, header in enumerate(headers):
        worksheet.write(0, column, header)
    for index, signal in enumerate(signals, start=1):
        metrics = _api()._paper_metrics(signal, None)
        worksheet.write(index, 0, signal.get("entry_time") or signal.get("news_time") or "")
        worksheet.write(index, 1, signal.get("news_text") or "")
        asset = "XAU" if signal.get("asset") == "GOLD" else signal.get("asset")
        worksheet.write(index, 2, asset or "")
        worksheet.write(index, 3, "多" if signal.get("action") == "BUY" else "空")
        worksheet.write_number(index, 4, float(signal.get("entry_price") or 0.0))
        worksheet.write_number(index, 5, float(signal.get("mfe_pct") if signal.get("mfe_pct") is not None else metrics["live_mfe_pct"]))
        worksheet.write_number(index, 6, float(signal.get("mae_pct") if signal.get("mae_pct") is not None else metrics["live_mae_pct"]))
        worksheet.write(index, 7, signal.get("reasoning_path") or signal.get("reasoning") or "")
    worksheet.set_column(0, 0, 20)
    worksheet.set_column(1, 1, 64)
    worksheet.set_column(2, 7, 16)
    workbook.close()
    output.seek(0)
    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=paper_signals.xlsx"},
    )


@router.get("/api/export/excel")
async def export_excel():
    """Export today's buy/sell signals as Excel."""
    try:
        return await _api()._do_export_excel()
    except Exception as exc:
        import traceback
        traceback.print_exc()
        return StreamingResponse(
            io.BytesIO(f"Export error: {exc}".encode("utf-8")),
            media_type="text/plain; charset=utf-8",
            status_code=500,
        )
