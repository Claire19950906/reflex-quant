#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""快讯源诊断版 — 打印原始响应，用于对准真实接口结构（沙箱外网不通，请在本地终端跑）。

用法:  python -X utf8 scripts/probe_flash_sources.py
每个源尝试多个已知变体 URL，打印 HTTP 码 + 响应原文前 700 字符。
"""

import requests

UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        " (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
}


def hit(name: str, url: str, referer: str = "https://finance.sina.com.cn/7x24/") -> None:
    print("=" * 70)
    print(f"[{name}]")
    print(f"    URL: {url}")
    try:
        r = requests.get(url, headers={**UA, "Referer": referer}, timeout=10)
        print(f"    HTTP {r.status_code}  Content-Type={r.headers.get('Content-Type','')}  字节={len(r.content)}")
        # 尝试 UTF-8 解码原文
        try:
            text = r.content.decode("utf-8", errors="replace")
        except Exception:
            text = r.text
        text = text.strip()
        print("    原文(前700字符):")
        print("    " + text[:700].replace("\n", "\n    "))
    except Exception as exc:
        print(f"    失败: {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    # 新浪 7x24
    hit("新浪7x24 A(zhibo_id=152)",
        "https://zhibo.sina.com.cn/api/zhibo/feed?page=1&page_size=20&zhibo_id=152&tag_id=0&dire=f&dpc=1&type=0")
    hit("新浪7x24 B(带pagesize)",
        "https://zhibo.sina.com.cn/api/zhibo/feed?page=1&page_size=20&pagesize=20&zhibo_id=152&tag_id=0&dire=f&dpc=1&type=0")
    hit("新浪滚动新闻(roll API)",
        "https://feed.mix.sina.com.cn/api/roll/get?pageid=153&lid=2516&num=20",
        referer="https://finance.sina.com.cn/roll/")

    # 东财 7x24 快讯
    hit("东财 A(biz=web_724)",
        "https://np-listapi.eastmoney.com/comm/web/getFastNewsList?client=web&biz=web_724&fastColumn=102&sortEnd=&pageSize=20",
        referer="https://kuaixun.eastmoney.com/")
    hit("东财 B(biz=web_news_724)",
        "https://np-listapi.eastmoney.com/comm/web/getFastNewsList?client=web&biz=web_news_724&fastColumn=102&sortEnd=&pageSize=20",
        referer="https://kuaixun.eastmoney.com/")
    hit("东财 C(带ut参数)",
        "https://np-listapi.eastmoney.com/comm/web/getFastNewsList?client=web&biz=web_724&fastColumn=102&sortEnd=&pageSize=20&ut=b2884a393a59ad64002292a3e90d46a5",
        referer="https://kuaixun.eastmoney.com/")
    hit("东财 D(老kuaixun HTML接口)",
        "https://newsapi.eastmoney.com/kuaixun/v1/getlist_102_ajaxResult_20_1_.html",
        referer="https://kuaixun.eastmoney.com/")
    print("=" * 70)
    print("完成")
