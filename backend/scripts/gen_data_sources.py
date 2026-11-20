"""生成 docs/DATA-SOURCES.md（数据源清单与采集节奏）。

2026-11-20: 补上评分表里缺的那一项「数据源清单与延迟」。原则和 gen_api_map 一样：
**从代码里抽，不手写**——手写的清单三个月就会过期，而代码是唯一真值源。

数据来源（全部只读，不 import 业务模块，避免拉网络依赖）：
  - `feeds/quote_sources.py` 的 `ASSET_ROUTE`（品种 → 数据源 → 合约代码）
  - `feeds/news_sources.py` 的 `SOURCE_KEYS`（新闻源注册表）
  - `config.py` 与 `api_server.py` 里的轮询间隔常量

用法：
    python scripts/gen_data_sources.py            # 重写 docs/DATA-SOURCES.md
    python scripts/gen_data_sources.py --check    # 只校验是否过期（CI 用，过期退出码 1）
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
OUT = BACKEND.parent / "docs" / "DATA-SOURCES.md"

QUOTE_SOURCES = BACKEND / "src_python" / "feeds" / "quote_sources.py"
NEWS_SOURCES = BACKEND / "src_python" / "feeds" / "news_sources.py"
CONFIG = BACKEND / "src_python" / "config.py"
API_SERVER = BACKEND / "src_python" / "api_server.py"

# 这些间隔是"多久去问一次源"，不是"数据有多新"。写清楚，别让人误读。
PACES = [
    ("市场情绪（FNG + Truth Social）", "MARKET_SENTIMENT_POLL_SECONDS", 3600),
    ("金十日历", "JIN10_CALENDAR_POLL_SECONDS", 30),
    ("美股行情（yfinance 兜底）", "YFINANCE_INTERVAL_S", 300),
    ("漏检补偿", "_MISSED_SIG_INTERVAL_S", 300),
    ("异常签名扫描", "_ABNORMAL_SIG_INTERVAL_S", 15),
    ("K 线回补", "_KLINE_BACKFILL_INTERVAL_S", 1800),
    ("链上归档", "_CHAIN_ARCHIVE_INTERVAL_S", 600),
    ("FOMC 升级", "FOMC_UPGRADE_INTERVAL_S", 3600),
    ("联邦资金推送", "FED_PUSH_INTERVAL_S", 300),
    ("回放快照刷新", "_REPLAY_REFRESH_INTERVAL_S", 30),
]


def _asset_route() -> dict[str, dict[str, str]]:
    """用 ast 取下 ASSET_ROUTE 字面量，不执行模块（避免 import 期网络/依赖）。"""
    tree = ast.parse(QUOTE_SOURCES.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == "ASSET_ROUTE":
            return ast.literal_eval(node.value)
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "ASSET_ROUTE":
            return ast.literal_eval(node.value)
    raise SystemExit("没在 quote_sources.py 里找到 ASSET_ROUTE")


def _news_keys() -> list[str]:
    m = re.search(r"SOURCE_KEYS\s*=\s*\(([^)]*)\)", NEWS_SOURCES.read_text(encoding="utf-8"))
    if not m:
        raise SystemExit("没在 news_sources.py 里找到 SOURCE_KEYS")
    return re.findall(r'"([^"]+)"', m.group(1))


def _pace(const: str, fallback: int) -> tuple[int, str | None]:
    """查这个节奏常量有没有环境变量开关；有就返回 (默认值, 变量名)，没有返回 (fallback, None)。"""
    env = const.lstrip("_")
    for path in (CONFIG, API_SERVER):
        txt = path.read_text(encoding="utf-8", errors="ignore")
        m = re.search(rf"""os\.getenv\(\s*["']{re.escape(env)}["']\s*,\s*["']?(\d+)["']?\s*\)""", txt)
        if m:
            return int(m.group(1)), env
    return fallback, None


def _human(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds} 秒"
    if seconds < 3600:
        return f"{seconds // 60} 分钟"
    return f"{seconds // 3600} 小时"


def build() -> str:
    route = _asset_route()
    keys = _news_keys()

    by_market: dict[str, list[str]] = {}
    for asset, info in route.items():
        by_market.setdefault(info.get("market", "?"), []).append(asset)

    lines: list[str] = []
    lines.append("# 数据源清单与采集节奏")
    lines.append("")
    lines.append("> 本页由 `backend/scripts/gen_data_sources.py` 从代码生成，**不要手改**。")
    lines.append("> CI 里 `--check` 会校验它是否过期（源码改了而本页没重生成，就会红）。")
    lines.append("")
    lines.append("## 一、行情：每个品种走哪个源")
    lines.append("")
    lines.append("路由表的唯一事实源是 `backend/src_python/feeds/quote_sources.py` 的 `ASSET_ROUTE`。")
    lines.append("之所以要一张表而不是一个默认源：**不是所有源的合约都存在**——币安没有美股合约，")
    lines.append("Yahoo 没有黄金永续，国内现货源没有 BTC。硬走一条路，结果就是“拿不到价还以为是策略错”。")
    lines.append("")
    lines.append("| 品种 | 数据源 | 合约 / 代码 |")
    lines.append("|---|---|---|")
    for asset, info in sorted(route.items(), key=lambda kv: (kv[1].get("market", ""), kv[0])):
        sym = info.get("symbol") or "—"
        lines.append(f"| `{asset}` | `{info.get('market', '?')}` | `{sym}` |")
    lines.append("")
    lines.append("按源归类（“哪几个源各自覆盖什么”一眼就能看出来）：")
    lines.append("")
    for market, assets in sorted(by_market.items()):
        lines.append(f"- **`{market}`** — {len(assets)} 个品种：{', '.join('`'+a+'`' for a in sorted(assets))}")
    lines.append("")
    lines.append("四级兜底在 `feeds/quote_fallback.py`：主源失败 → 备源 → 缓存价 → 显式报“无价”，")
    lines.append("**不编价**。取不到价时下游闸门会拒绝出信号，而不是用上一个旧价顶着。")
    lines.append("")
    lines.append("## 二、新闻：16 个源，靠指纹去重")
    lines.append("")
    lines.append("注册表在 `backend/src_python/feeds/news_sources.py` 的 `SOURCE_KEYS`：")
    lines.append("")
    lines.append("```")
    lines.append(", ".join(keys))
    lines.append("```")
    lines.append("")
    lines.append(f"共 **{len(keys)}** 个源。同一条消息常被五六家同时转发，所以入库时算 `content_fingerprint`，")
    lines.append("同一指纹只留最早的一条，其余并入 `cross_net_first_seen_ts`（“全网溯源”）——")
    lines.append("前端看到的不是“五条新闻”，而是“一条新闻、五个来源、最早出现在 X 时刻”。")
    lines.append("")
    lines.append("## 三、宏观与慢变量：多久问一次")
    lines.append("")
    lines.append("| 任务 | 常量 | 默认节奏 | 能改环境变量吗 |")
    lines.append("|---|---|---|---|")
    for label, const, fallback in PACES:
        default, env = _pace(const, fallback)
        how = f"是：`{env}`" if env else "否（改常量后重启）"
        lines.append(f"| {label} | `{const}` | {_human(default)} | {how} |")
    lines.append("")
    lines.append("**注意这是“多久问一次”，不是“数据有多新”。** 高频源（新闻）问得密，")
    lines.append("慢变量（情绪、日历）问得稀——问太密只会被封 IP，不会让数据变准。")
    lines.append("")
    lines.append("## 四、滞后：怎么自己量，而不是信一个平均数")
    lines.append("")
    lines.append("本页**不印“平均滞后 N 分钟”这种数**。原因很实在：各源的时间戳口径不一致")
    lines.append("（`timestamp` 是采集时刻的 epoch 秒，`published_at` 是源站给的本地时间文本，")
    lines.append("个别源甚至是“抓取时才发现发布日期”）。混在一起算平均，会得到一个看起来精确、")
    lines.append("实际差 8 小时的假数字。")
    lines.append("")
    lines.append("要量就按下面这条 SQL 自己算，**先按源分组、再挑干净区间**（想更稳就把 AVG 换成中位数算法）：")
    lines.append("")
    lines.append("```sql")
    lines.append("-- 单源滞后的平均值（分钟）；m 落在 -5..1440 之外的行先剔掉，那是口径不一致的脏数据")
    lines.append("WITH lag AS (")
    lines.append("  SELECT source,")
    lines.append("         (julianday(datetime(timestamp,'unixepoch','+8 hours')) - julianday(published_at)) * 1440 AS m")
    lines.append("  FROM raw_news WHERE published_at <> ''")
    lines.append("), clean AS (SELECT source, m FROM lag WHERE m BETWEEN -5 AND 1440)")
    lines.append("SELECT source, COUNT(*) n,")
    lines.append("       ROUND(AVG(m),1) avg_min")
    lines.append("FROM clean GROUP BY source ORDER BY n DESC;")
    lines.append("```")
    lines.append("")
    lines.append("口径对不上的源（平均值为空或离谱），说明它给的不是事件时间而是页面时间——")
    lines.append("这种源只适合做“佐证”，不该进时效性判断。")
    lines.append("")
    lines.append("## 五、明确的缺口")
    lines.append("")
    lines.append("- **没有付费终端**（Bloomberg / Refinitiv / Wind）：只有公开源与 RSS，")
    lines.append("  所以“机构级快讯”这一档天然慢几十秒到几分钟，不假装能赢。")
    lines.append("- **没有逐笔成交（tick）历史**：只有 1 分钟及以上 K 线，")
    lines.append("  做不了微观结构这类基于订单流的分析。")
    lines.append("- **情绪类源反爬严重**：Truth Social 靠低频轮询，断就断，不重试到封号。")
    lines.append("")
    lines.append("相关：[架构](./ARCHITECTURE.md) · [快速上手](./QUICKSTART.md) · [FAQ](./FAQ.md)")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只校验是否过期")
    args = ap.parse_args()
    text = build()
    if args.check:
        old = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if old.strip() != text.strip():
            print(f"{OUT.name} 已过期，请运行 python scripts/gen_data_sources.py 重新生成")
            return 1
        print(f"{OUT.name} 与代码一致")
        return 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text, encoding="utf-8")
    print(f"已写入 {OUT}（{len(text.splitlines())} 行）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
