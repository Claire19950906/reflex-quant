"""新闻 → 品种 路由器 —— 分清"这条消息到底影响谁", 以及"有多强"。

为什么需要它:
  线上 33% 的决策 target_asset 是 NONE。抽查后发现是两种完全不同的情况被混在一起:
    ① 真无关: "中国人民银行副行长出席行长例会" / "万事达目标价上调"(不在品种池)
       → 判 NONE 是对的, 这正是门卫该挡的垃圾;
    ② 隐含有关系: "Anthropic 计划筹资 30 亿美元" / "欧洲央行管委: 德国经济加速"
       → 新闻里没出现任何品种名, 但沿产业链/宏观路径确实会影响 NVDA / XAU。
       这类被判 NONE 就是漏，样本和信号一起丢了。

  用户的原话点出了关键: "一个新闻有可能没写跟哪个公司相关, 但它需要判断跟哪个公司
  最有相关性……不能把这个产品错误地推到一个对它影响不大的标题上。"
  → 所以这里输出**主品种 + 次品种**, 而不是把弱相关品种当主品种硬推。

三条判定档:
  direct    标题/正文直接点名公司或品种           → 可作主品种
  transmit  沿产业链/宏观路径传导 (OPEC→原油, 美联储→黄金) → 可作主品种
  weak      只有松散联想 (AI 融资→算力需求)       → 只进"观察", 绝不当主品种
  none      找不到任何路径                        → NONE (正确丢弃)

用法:
  python3 news_asset_router.py --audit --days 7     # 审计近 7 天的 NONE 行
  python3 news_asset_router.py --route "标题文本"    # 单条试跑
"""
from __future__ import annotations
from ._paths import SRC_DIR, BACKEND_DIR  # P2: 路径锚点

import argparse
import re
import sqlite3
import sys
from pathlib import Path

DB_PATH = BACKEND_DIR / "trident_event_bus.db"

# 平台可交易/可跟踪的品种池 (与 ai_prompts.ASSET_DISPLAY 对齐)
UNIVERSE = {
    "BTC", "ETH", "SOL", "WTI", "XAU", "NVDA", "TSLA", "AAPL", "MSTR",
    "MSFT", "AMZN", "GOOGL", "COIN", "PLTR", "META", "SMCI", "MARA",
    "RIOT", "SKHYNIX", "SAMSUNG",
}

# ── 关系表: (正则, [(品种, 权重, 档次, 传导路径)], 说明) ───────────────────
# 权重 = 这条词指向该品种的把握 (0-1)。0.9+ 直配; 0.6-0.9 强传导; <0.6 只算联想。
RELATIONS: list[tuple[str, list[tuple[str, float, str, str]], str]] = [
    # 加密
    (r"比特币|BTC|比特币ETF|现货ETF.*比特|币安|Binance|Coinbase|USDT|加密交易所|"
     r"crypto exchange|bitcoin ETF|digital asset",
     [("BTC", 0.95, "direct", "数字货币主线"), ("ETH", 0.55, "weak", "同板块联动")], "加密核心"),
    (r"以太坊|ETH|ether(?!eum? hack)|Vitalik",
     [("ETH", 0.95, "direct", "以太坊本体"), ("BTC", 0.5, "weak", "同板块联动")], "以太坊"),
    (r"Solana|SOL链|SOL代币",
     [("SOL", 0.9, "direct", "Solana 本体")], "Solana"),
    (r"微策略|MicroStrategy|MSTR",
     [("MSTR", 0.95, "direct", "公司本体"), ("BTC", 0.7, "transmit", "微策略=比特币持仓代理")], "微策略"),
    (r"MARA|Marathon|RIOT|Riot|挖矿|矿企",
     [("MARA", 0.85, "direct", "矿企"), ("RIOT", 0.85, "direct", "矿企"),
      ("BTC", 0.6, "transmit", "挖矿景气=币价代理")], "矿企"),
    (r"稳定币|USDC|USDT发行|加密监管|SEC.*加密|数字资产法案",
     [("BTC", 0.75, "transmit", "监管/流动性影响全板块"),
      ("COIN", 0.6, "weak", "交易平台受益")], "加密监管"),

    # 黄金 / 宏观
    (r"黄金|金价|XAU|gold price|spot gold|bullion|COMEX.*黄金|央行购金",
     [("XAU", 0.95, "direct", "黄金本体")], "黄金"),
    (r"美联储|FOMC|鲍威尔|Powell|降息|rate cut|rate hike|联邦基金利率",
     [("XAU", 0.8, "transmit", "利率→实际利率→金价"),
      ("BTC", 0.55, "weak", "风险偏好联动")], "美联储"),
    (r"CPI|通胀|inflation|PCE|非农|payrolls|失业率|unemployment|美元指数|DXY|零售销售|retail sales",
     [("XAU", 0.75, "transmit", "美国通胀/美元→金价"),
      ("BTC", 0.45, "weak", "风险偏好")], "美国宏观数据",
     r"美|美国|美联储|美元|USD|FOMC|Powell|鲍威尔|联邦|U\.S\.|Fed"),
    (r"地缘|战争|空袭|冲突升级|军事打击|导弹|war|strike|attack|military|missile",
     [("XAU", 0.75, "transmit", "避险资金→黄金"),
      ("WTI", 0.55, "weak", "供给风险→原油")], "地缘避险",
     r"中东|伊朗|Israel|Iran|俄|乌克兰|Ukraine|俄罗斯|红海|Red Sea|霍尔木兹|Hormuz|台海|空袭|战争|军演"),
    (r"制裁|sanction",
     [("XAU", 0.52, "weak", "制裁=避险联想, 影响弱"), ("WTI", 0.5, "weak", "供应扰动联想")], "制裁"),
    (r"欧洲央行|ECB|日本央行|BOJ|英国央行|Bank of Japan",
     [("XAU", 0.45, "weak", "间接流动性联想"),
      ("BTC", 0.35, "weak", "风险偏好")], "其他央行"),

    # 原油
    (r"原油|WTI|布伦特|Brent|OPEC|欧佩克|沙特|Saudi|伊朗石油|俄油|EIA库存|crude|oil price|drilling",
     [("WTI", 0.95, "direct", "原油本体")], "原油"),
    (r"红海|Red Sea|霍尔木兹|Hormuz|oil tanker|油轮|输油管道|pipeline",
     [("WTI", 0.8, "transmit", "运输受阻→供给收缩")], "原油运输"),

    # AI / 半导体
    (r"英伟达|NVIDIA|Nvidia|黄仁勋|Jensen Huang|GPU|H100|H200|Blackwell|AI芯片|算力|AI chip|data center",
     [("NVDA", 0.95, "direct", "AI 算力龙头"),
      ("SMCI", 0.7, "transmit", "AI 服务器配套"),
      ("SKHYNIX", 0.65, "transmit", "HBM 供应"), ("SAMSUNG", 0.6, "transmit", "HBM 供应")], "AI 算力"),
    (r"HBM|存储芯片|DRAM|海力士|SK海力士|Hynix|三星电子|Samsung|美光|Micron",
     [("SKHYNIX", 0.85, "direct", "HBM/存储供给"),
      ("SAMSUNG", 0.75, "direct", "存储供给"), ("NVDA", 0.6, "weak", "上游供应")], "存储"),
    (r"台积电|TSMC|先进封装|CoWoS",
     [("NVDA", 0.7, "transmit", "产能→算力供给"),
      ("SKHYNIX", 0.5, "weak", "同产业链")], "先进封装"),
    (r"Anthropic|OpenAI|大模型|AI应用|AI 融资|生成式|AI startup",
     [("NVDA", 0.55, "weak", "算力需求联想, 非直接受益"),
      ("SMCI", 0.5, "weak", "服务器需求联想")], "AI 应用层"),

    # 个股
    (r"特斯拉|Tesla|TSLA|马斯克|Musk|Cybertruck|FSD",
     [("TSLA", 0.95, "direct", "公司本体")], "特斯拉"),
    (r"苹果|Apple|AAPL|iPhone|库克|Cook",
     [("AAPL", 0.95, "direct", "公司本体")], "苹果"),
    (r"微软|Microsoft|MSFT|Azure|Copilot",
     [("MSFT", 0.95, "direct", "公司本体")], "微软"),
    (r"亚马逊|Amazon|AMZN|AWS",
     [("AMZN", 0.95, "direct", "公司本体")], "亚马逊"),
    (r"谷歌|Alphabet|GOOGL|Google|Gemini",
     [("GOOGL", 0.95, "direct", "公司本体")], "谷歌"),
    (r"Meta|Facebook|扎克伯格|Instagram",
     [("META", 0.95, "direct", "公司本体")], "Meta"),
    (r"Palantir|PLTR",
     [("PLTR", 0.95, "direct", "公司本体")], "Palantir"),
    (r"超微|Super Micro|SMCI",
     [("SMCI", 0.95, "direct", "公司本体")], "超微"),
]

# 新闻类型闸: 这类内容对行情几乎无影响, 直接判 NONE (不占样本)
NOISE_PATTERNS = [
    r"出席.*(会议|例会|论坛|峰会)", r"人事任免|任命|辞职", r"挂牌|落成|签约仪式",
    r"目标价从.*上调至", r"目标价从.*下调至",
]

# 弱相关阈值: 低于此权重一律不进主品种, 只进观察
PRIMARY_MIN_WEIGHT = 0.6

_HASH_PREFIX = re.compile(r"^\s*\[hash:[0-9a-f]+\]\s*", re.I)
_META_TAIL = re.compile(r"(快讯\s*\|?\s*原文[:：]?|原文[:：]?\s*https?://|https?://\S+)", re.I)


def clean_text(raw: str) -> str:
    """剥掉标题里的来源元数据, 只留真正的新闻内容。

    实测: 一条 "Under Armour 下调毛利率" 的标题后面跟了一行
    "Yahoo DXY (DX-Y.NYB) 快讯 | 原文: https://finance.yahoo…" ——
    里面的 "DXY" 被当成"美元指数"命中, 于是这条鞋企新闻被判进了黄金房间。
    来源栏是元数据, 不剥掉就会持续污染分类。
    """
    t = _HASH_PREFIX.sub("", raw or "")
    first = next((ln.strip() for ln in t.splitlines() if ln.strip()), "")
    return _META_TAIL.split(first)[0].strip()


def _compile():
    """把关系表编成正则。支持可选第 4 项 require —— 必须**同时**命中的上下文条件。

    为什么要有 require: 单看 "CPI" 三个字母, 德国 CPI 也会命中; 但德国通胀对
    黄金的实际影响很小, 判成主品种就是"把品种推到影响不大的标题上"(用户明确否掉)。
    真正能推黄金的是美国通胀/美联储 —— 所以这类宏观词必须绑定美国语境。
    """
    out = []
    for rule in RELATIONS:
        pat, assets, note = rule[0], rule[1], rule[2]
        require = rule[3] if len(rule) > 3 else None
        out.append((re.compile(pat, re.I), assets, note,
                    re.compile(require, re.I) if require else None))
    return out


_RULES = _compile()
_NOISE = [re.compile(p, re.I) for p in NOISE_PATTERNS]


def route(text: str, body: str = "") -> dict:
    """给新闻判品种。返回 primary / secondary / verdict。

    · primary   权重 >= 0.6 且是最高分 (可作主品种, 允许并列)
    · secondary 权重 < 0.6 (只进观察房间, 不占交易样本)
    · verdict: direct / transmit / weak / none / noise

    判定顺序 (v335 修): **先只看标题**。正文里往往顺带提到一堆板块名,
    一起扫会把弱相关抬成主品种 (实测出现过 "Under Armour → 黄金" 这种荒谬结果)。
    标题没有直接关系时, 才用正文前 120 字兜底, 且要求命中至少 2 个关键词。
    """
    # 只扫清洗后的标题前 120 字: 库里的 headline 常带 [hash:] 前缀 + 来源栏 + 整段外文,
    # 全文扫会把元数据里的 "DXY" 之类零词当成命中。宁可少判, 不可乱判。
    head = clean_text(text)[:120]
    if not head.strip() and not (body or "").strip():
        return {"primary": [], "secondary": [], "verdict": "none", "reason": "空文本"}

    def _scan(src: str) -> tuple[dict[str, dict], list[str]]:
        hits: dict[str, dict] = {}
        notes: list[str] = []
        for rx, assets, note, require in _RULES:
            m = rx.search(src)
            if not m:
                continue
            # require 是"上下文闸": 命中了关键词但语境不对(比如德国 CPI 不是
            # 美国通胀), 整条规则不生效 —— 宁可判无关, 不推到弱相关品种上。
            if require and not require.search(src):
                continue
            notes.append(f"{note}({m.group(0)})")
            for asset, w, tier, path in assets:
                if asset not in UNIVERSE:
                    continue
                cur = hits.get(asset)
                if cur and cur["weight"] >= w:
                    continue
                hits[asset] = {"asset": asset, "weight": w, "tier": tier,
                               "path": path, "kw": m.group(0)}
        return hits, notes

    hits, notes = _scan(head)
    if not hits and body:
        # 正文兜底的门槛必须更硬: 只认"正文里明确点名了公司/品种"这种直配,
        # 而且必须是高把握。否则一篇文章顺嘴提到"制裁""关税"就能把无关新闻
        # 拽进黄金房间 —— 这正是用户说的"不能推到影响不大的品种上"。
        b_hits, _ = _scan((body or "")[:120])
        strong = [h for h in b_hits.values()
                  if h["tier"] == "direct" and h["weight"] >= 0.85]
        if len(strong) >= 2:
            hits, notes = {h["asset"]: h for h in strong}, ["正文点名(直配)"]

    if not hits:
        noise = next((p.pattern for p in _NOISE if p.search(head)), None)
        return {"primary": [], "secondary": [], "verdict": "noise" if noise else "none",
                "reason": "公告/人事类, 对行情无影响" if noise else "没有任何传导路径"}
    ranked = sorted(hits.values(), key=lambda h: -h["weight"])
    top = ranked[0]["weight"]
    # 主品种: 达到阈值, 且和最高分差距不大 (>= top - 0.15) —— 避免把弱相关并列当主
    primary = [h for h in ranked
               if h["weight"] >= PRIMARY_MIN_WEIGHT and h["weight"] >= top - 0.15]
    secondary = [h for h in ranked if h not in primary]
    if not primary:
        return {"primary": [], "secondary": secondary, "verdict": "weak",
                "reason": "只有弱相关联想, 不推主品种", "matched": notes}
    verdict = "direct" if any(h["tier"] == "direct" for h in primary) else "transmit"
    return {"primary": primary, "secondary": secondary, "verdict": verdict,
            "reason": " / ".join(notes)}


def audit(days: int) -> None:
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    since = int(__import__("datetime").datetime.now().timestamp()) - days * 86400
    rows = conn.execute(
        """SELECT d.id, d.target_asset, n.headline, n.body
             FROM ai_decisions d JOIN news_events n ON n.news_id = d.news_id
            WHERE d.ts >= ?
              AND (d.target_asset IS NULL OR d.target_asset IN ('', 'NONE'))
            GROUP BY d.id""", (since,)).fetchall()
    total = len(rows)
    buckets: dict[str, list] = {}
    examples: dict[str, list] = {}
    for did, _ta, headline, body in rows:
        r = route(headline or "", body or "")
        v = r["verdict"]
        buckets[v] = buckets.get(v, [])
        buckets[v].append(r)
        examples.setdefault(v, [])
        if len(examples[v]) < 3:
            label = "/".join(h["asset"] for h in r["primary"]) or \
                    "/".join(h["asset"] for h in r["secondary"]) or "—"
            examples[v].append(f"{(headline or '')[:46]} → {label}")
    print(f"\n=== 近 {days} 天 NONE 行审计: 共 {total} 条 ===")
    for v in ("direct", "transmit", "weak", "noise", "none"):
        n = len(buckets.get(v, []))
        pct = 100 * n / total if total else 0
        print(f"  {v:<9} {n:>4} 条 ({pct:>5.1f}%)")
        for e in examples.get(v, [])[:2]:
            print(f"       · {e}")
    recoverable = len(buckets.get("direct", [])) + len(buckets.get("transmit", []))
    print(f"\n可救回(能定主品种) {recoverable} 条 = {100*recoverable/total:.1f}%"
          f" · 应继续丢 {len(buckets.get('none', [])) + len(buckets.get('noise', []))} 条")
    conn.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--audit", action="store_true")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--route", type=str, default="")
    a = ap.parse_args()
    if a.route:
        import json
        print(json.dumps(route(a.route), ensure_ascii=False, indent=2))
        return 0
    if a.audit:
        audit(a.days)
        return 0
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
