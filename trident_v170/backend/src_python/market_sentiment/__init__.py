"""market_sentiment — 市场情绪数据源包 (v170+ 数据源加固)。

由 api_server.market_sentiment_watcher 消费, 两条子源:
    fng         — Fear & Greed Index (加密恐慌贪婪, 每日一更)
    trump_posts — Truth Social 特朗普发帖 (政策预期, 高频)

子模块 import 时即注册, 供 watcher 直接调用:
    from market_sentiment import fng, trump_posts

统一约定:
    - fetch_* 网络失败返回 [] , 绝不向 watcher 抛异常
    - to_ingest_items 产出 raw_news 条目 dict:
        id / title / summary / source / published_at / url
    - 入库由 api_server._ingest_external_news_sync(bypass_gate=True) 统一负责,
      本包不直接触碰数据库
"""
from __future__ import annotations

from . import fng, trump_posts

__all__ = ["fng", "trump_posts"]
__version__ = "1.0.0"