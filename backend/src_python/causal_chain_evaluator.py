"""
v302+ P0: 因果图谱反馈评估器 (causal_chain_evaluator)
==================================================
需求 8 核心: "排除干扰 → 判断哪些因果链对, 哪些错, 再纠正"

backfill: 扫过去 N 天 news_events, 用 chain.trigger_keywords 匹配,
           写入 causal_chain_outcomes (PENDING).

evaluate: 扫 PENDING 且 eval_deadline 已到, 查 ohlcv T+5 真实涨跌,
          判定 HIT/MISS/NEUTRAL, 同步 causal_chains.hit/miss.

判定规则:
  pred_dir=UP   → price[T+5]>price[T+0] 为方向对
  pred_dir=DOWN → price[T+5]<price[T+0] 为方向对
  pred_dir=BOTH → |pct_5d|>=mag_lo 为方向对
  HIT = dir 对 AND mag 落在 [lo,hi]
  MISS = dir 反
  NEUTRAL = dir 对但 mag 不在 [lo,hi]
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Dict, Iterable, Optional

import chain_keywords # v400 P0-1/P0-2: 触发词净化 + 品种绑定唯一口径
import mechanism_family  # v417 P0-3: 同事件扇出按机制族收敛（口径与 causal_graph 共用）

DB_PATH = os.environ.get("TRIDENT_DB_PATH") or os.path.join(os.path.dirname(__file__), "..", "trident_event_bus.db")
EVAL_HORIZON_DAYS = 5
TIME_HORIZON_LOOKBACK = 90
# v427 触发冷却 48h → 24h。
# 依据（近 30 天全链实测）: 48h 下 178 条链合计 2076 次触发、n>=5 的链 163 条；
# 24h 下 3784 次、164 条。新增样本里与 24–48h 旧样本正文重合>0.6 的只占 0.9%
# —— 说明降冷却拿到的是**不同的事件**，不是同一篇稿子重复计数。样本是当前
# 唯一的硬约束（一条链一周才攒 3 条），所以这里只放宽到 24h，并配内容去重：
# 同一题材的第二篇报道仍会被 _same_story 挡掉，避免 Wilson 下界被注水。
TRIGGER_COOLDOWN_HOURS = 24
_STORY_DUP_HOURS = 48      # 内容去重回看窗口（比冷却长，覆盖跨天的同题材报道）
# v417 P0-3：同一事件同一品种最多留几条链。实测"袭击"一条新闻能扇出 160 条链
# （93 条启用链共用该触发词），样本被同源事件灌满，也就谈不上独立证据。
MAX_CHAINS_PER_EVENT_ASSET = int(os.environ.get("CHAIN_MAX_PER_EVENT", "3"))
_CHAIN_NAME_DUP_RATIO = 0.72
_STORY_DUP_RATIO = 0.60    # 字符二元组重合率超过该值视为同一件事

# --- schema 适配常量 (causal_chains 已重设计, 见 migrate_causal_chains.py) ---
# 新表无 expected_direction/magnitude_lo/hi, 评估所需的预测必须在 backfill 时从
# 链的文本(name/cause/effect)推断并快照进 causal_chain_outcomes.predicted_*。
PRICED_IN_PCT = 1.0   # v430：触发前 5 日同向幅度达到 1% 即视为"可能已计价"
DEFAULT_MAG_LO = 1.0
DEFAULT_MAG_HI = 8.0

# trigger_words 里的结构性 token, 不是真正的触发关键词
_CHAIN_STOPWORDS = {"asset", "prompt", "verdict", "event", "eventtype"}

# 方向推断词典见下方 _ACTION_UP/_ACTION_DOWN/_UP_WORDS/_DOWN_WORDS/_NEUTRAL_WORDS

# v302+ P2: 资产别名映射 — 让 chain.asset 能在 headline/body 文本里命中同义/中文写法,
# 避免 XAU/WTI/SOL/ADA/SKHYNIX 等资产因为 news.assets 字段没标而漏匹配.
ASSET_ALIASES = {
    "XAU": ["xau", "gold", "黄金", "gc1", "spot_gold", "金价"],
    "WTI": ["wti", "oil", "原油", "crude", "petroleum", "brent"],
    "SOL": ["sol", "solana"],
    "ADA": ["ada", "cardano", "艾达"],
    "SKHYNIX": ["skhynix", "hynix", "000660", "海力士", "sk_hynix", "hbm", "存储", "内存"],
    "BTC": ["btc", "bitcoin", "比特币"],
    "ETH": ["eth", "ethereum", "以太", "以太坊"],
    "NVDA": ["nvda", "nvidia", "英伟达"],
    "TSLA": ["tsla", "tesla", "特斯拉"],
    "MU": ["mu", "micron", "美光"],
    "BNB": ["bnb", "binance", "币安"],
    "XRP": ["xrp", "ripple"],
    "DOGE": ["doge", "dogecoin", "狗狗币"],
}


@dataclass
class ChainRow:
    chain_id: str
    asset: str
    trigger_keywords: list[str]
    expected_direction: str
    magnitude_lo: float
    magnitude_hi: float
    confidence: float
    created_ts: int = 0        # v405: 链的诞生时间（epoch）—— 不许拿它去解释它出生前的事件
    cause: str = ""            # v428: 因端原文（用于方向语气比对）
    cause_tone: str = ""       # v428: 因端语气 "escalate"/"ease"/""（判不出就空）
    name: str = ""             # v417: 链名（同一事件扇出时用来判断"是不是同一件事"）


def _split_keywords(s):
    if not s:
        return []
    return [k.strip() for k in s.split(",") if k.strip()]


def _load_priced_assets(conn):
    """返回有行情数据的资产集合 —— 只有这些资产才能做 T+5 评估."""
    try:
        return {r[0] for r in conn.execute("SELECT DISTINCT asset FROM ohlcv_daily")}
    except sqlite3.Error:
        return set()


# 明确的交易操作词 —— 优先级最高, 直接说明作者想要什么方向.
# 注意: 这些必须先于"利好/利空"判断, 因为 "美俄石油交易利空→WTI传导" 里
# "利空" 描述的是*触发原因*, 而链条结论是 WTI 上行.
_ACTION_UP = ("做多", "买入", "buy", "long", "加仓", "看涨")
_ACTION_DOWN = ("做空", "卖出", "sell", "short", "减仓", "看跌")
# 结论性方向词 (次优先级)
_UP_WORDS = ("上涨", "走强", "上行", "拉涨", "提振", "偏多", "利多", "看多",
             "多头", "牛市", "涨", "bull", "正向冲击")
_DOWN_WORDS = ("下跌", "走弱", "下行", "回落", "杀跌", "承压", "偏空",
               "看空", "空头", "熊市", "跌", "bear", "负向冲击")
# 双向/无方向 —— 出现即判 BOTH, 不做倾向
_NEUTRAL_WORDS = ("双向", "分化", "震荡", "有限", "模糊", "不确定", "不涨反跌")

# ── v322: 机制级方向语义词 ────────────────────────────────────────────
# 为什么单独一套: name 的词表含单字 "涨"/"跌", 在长文本里会误命中
# ("涨跌幅" 同时含 涨 和 跌)。机制文本是长句, 必须只用多字词。
#
# 机制级 = "这个事件通过什么作用推着价格往哪走" —— 是**作用方向**,
# 不是结论方向。UP 侧: 推升/买盘/流入; DOWN 侧: 压低/卖盘/流出。
_MECH_UP = (
    "推升", "抬升", "推高", "拉升", "上涨", "走高", "上行", "提振", "利多",
    "买盘", "买入盘", "增持", "流入", "涌入", "避险买盘", "做多", "看多",
    "偏多", "升值", "走强", "提价", "涨价", "买进", "需求增加", "利多情绪",
)
_MECH_DOWN = (
    "压低", "打压", "推低", "回落", "下跌", "走低", "下行", "杀跌", "利空",
    "卖盘", "卖出盘", "减仓", "流出", "涌出", "抛售", "做空", "看空",
    "偏空", "跌价", "走弱", "承压", "贬值", "卖压", "需求萎缩", "利空情绪",
)


def _direction_from_mechanism(mechanism: str, precondition: str = "") -> str:
    """从**机制描述**（第一性原理三问的产物）推断方向。

    ⚠️ 绝不读 effect —— effect 是复盘结论（"方向正确"/"LOSS"/"兑现"），
    用它推导"预测"就是数据泄漏，会让 hit rate 虚高。
    mechanism 描述的是"事件通过什么作用推着价格往哪走"，不是结果；
    实测其事后结论污染率只有 13.2%(47/356)，远低于 effect 的 21%。

    两者都出现 → BOTH（条件性："若…则涨；若…则跌" —— 诚实地标成无方向）。
    """
    text = "%s %s" % (mechanism or "", precondition or "")
    if not text.strip():
        return ""
    up = sum(1 for w in _MECH_UP if w in text)
    down = sum(1 for w in _MECH_DOWN if w in text)
    if up and not down:
        return "UP"
    if down and not up:
        return "DOWN"
    return ""


def _infer_direction(name, mechanism: str = "", precondition: str = ""):
    """从链的文本推断预期方向。

    v302+ 只用 name —— 因为 effect 字段 60% 含事后复盘结论
    (WIN/LOSS/方向正确/兑现), 用它推导"预测"会造成数据泄漏, 让 hit rate 虚高.

    ⚠️ v322 实测结论：**mechanism/precondition 不能用来推方向**（已回滚）。
      曾经尝试用机制语义词表（推升/买盘/流入 → UP；压低/卖盘/流出 → DOWN），
      628 条链里给 58 条补上了方向。**生产口径回测结果：**
        · 只信 name 的链（1397 条已对账）：方向命中率 **50.0%**（随机水平）
        · 加上 mechanism 推断后（2928 条）：方向命中率 **41.9%** —— **低于随机**
      原因：机制文本里的涨跌词是**相对别的资产**说的 ——
        "地缘冲突（利空）→ 避险资金流入黄金（利多）" 里"利空"指的是原油不是黄金，
        纯文本词表会把**方向搞反**。
      → 方向必须"对**这个资产**"判断，规则法做不到；要用 LLM 语义（见 10.27 的结论）。
      → 本函数保留 mechanism 参数只为向后兼容，**故意不使用**。
      → 绝不读 effect —— 那是复盘结论，用它就是泄漏。

    优先级: name 中性词 > name 操作词 > name 结论词 > BOTH
    """
    if not name:
        return "BOTH"
    low = name.lower()
    if any(w in low for w in _NEUTRAL_WORDS):
        return "BOTH"
    # 取最后一个操作词: "中东地缘冲突→原油做空 SELL" 里后者是最终结论
    up_pos = max([low.rfind(w) for w in _ACTION_UP] + [-1])
    dn_pos = max([low.rfind(w) for w in _ACTION_DOWN] + [-1])
    if up_pos >= 0 or dn_pos >= 0:
        return "UP" if up_pos > dn_pos else "DOWN"
    up = sum(1 for w in _UP_WORDS if w in low)
    down = sum(1 for w in _DOWN_WORDS if w in low)
    if up > down:
        return "UP"
    if down > up:
        return "DOWN"
    return "BOTH"


LLM_LABELS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "runtime", "chain_direction", "llm_labels.json",
)
_LLM_LABEL_CACHE: dict = {"mtime": None, "map": {}}

# ── v406 P5: 覆盖层的 DB 镜像 ────────────────────────────────────────────
# 标注只存在 runtime/chain_direction/llm_labels.json 里有两个隐患：
#   ① 换机/清 runtime 目录，369 条标注凭空消失，而链表里看不出任何痕迹；
#   ② 想按"方向 = DOWN 且启用"查链，只能把 JSON 读进内存再过滤，SQL 侧无法审计。
# 这里加一张**独立新表**（不是给 causal_chains 加列 —— 那个 ALTER 拿不到排他锁），
# 写入走单事务 upsert，读取优先 DB、缺失再退回 JSON，两边不一致时 DB 赢。
DIRECTION_OVERLAY_TABLE = "chain_direction_overlay"
_OVERLAY_DB_CACHE: dict = {"ts": 0.0, "map": {}}
_OVERLAY_DB_TTL = 300.0


def ensure_overlay_table(conn) -> bool:
    """建覆盖层镜像表（幂等）。失败返回 False，调用方退回 JSON。"""
    try:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS %s (
                 chain_id   INTEGER PRIMARY KEY,
                 direction  TEXT NOT NULL,
                 source     TEXT,
                 confidence REAL,
                 updated_at TEXT,
                 note       TEXT
               )""" % DIRECTION_OVERLAY_TABLE
        )
        conn.commit()
        return True
    except sqlite3.Error:
        return False


def sync_overlay_to_db(conn, labels: dict, source: str = "llm",
                       confidence=None, note: str = "") -> int:
    """把 {chain_id: UP|DOWN|BOTH} upsert 进镜像表，返回写入行数。

    单事务 + `BEGIN IMMEDIATE`：线上有 30+ cron 常驻抢写锁，逐行 commit 会被
    `database is locked` 打死（P1 的教训），所以要么一个事务写完，要么不写。
    """
    ok = {int(k): str(v).upper() for k, v in (labels or {}).items()
          if str(v).upper() in ("UP", "DOWN", "BOTH") and str(k).isdigit()}
    if not ok:
        return 0
    if not ensure_overlay_table(conn):
        return 0
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.Error:
        pass
    n = 0
    try:
        for cid, direction in ok.items():
            conn.execute(
                "INSERT INTO %s (chain_id, direction, source, confidence, updated_at, note)"
                " VALUES (?,?,?,?,?,?)"
                " ON CONFLICT(chain_id) DO UPDATE SET direction=excluded.direction,"
                " source=excluded.source, confidence=excluded.confidence,"
                " updated_at=excluded.updated_at, note=excluded.note"
                % DIRECTION_OVERLAY_TABLE,
                (cid, direction, source, confidence, now, note),
            )
            n += 1
        conn.commit()
    except sqlite3.Error:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        return 0
    _OVERLAY_DB_CACHE["ts"] = 0.0    # 让下次读拿到新值
    return n


def _overlay_from_db() -> dict:
    """从镜像表读覆盖层（进程内缓存 300s）。表不存在/读失败 → 空 dict。"""
    now = time.time()
    if _OVERLAY_DB_CACHE["map"] and now - _OVERLAY_DB_CACHE["ts"] < _OVERLAY_DB_TTL:
        return _OVERLAY_DB_CACHE["map"]
    try:
        import db as _db
        conn = sqlite3.connect(str(_db.get_db_path()), timeout=30)
        conn.row_factory = sqlite3.Row
    except Exception:
        return _OVERLAY_DB_CACHE["map"]
    try:
        rows = conn.execute(
            "SELECT chain_id, direction FROM %s" % DIRECTION_OVERLAY_TABLE
        ).fetchall()
        labels = {str(r["chain_id"]): str(r["direction"]).upper() for r in rows
                  if str(r["direction"]).upper() in ("UP", "DOWN", "BOTH")}
    except sqlite3.Error:
        labels = {}
    finally:
        conn.close()
    if labels:
        _OVERLAY_DB_CACHE.update({"ts": now, "map": labels})
    return labels


def llm_direction_overlay() -> dict:
    """v401 P0-3 的 LLM 方向标注（按链 id 的覆盖层）。

    为什么不塞进表里：`causal_chains` 在线上被 api_server/bridge_server 常驻读取，
    `ALTER TABLE` 拿不到排他锁（实测 database is locked）。而这批标注必须能活过
    `derive_strategy_columns` —— 它每次重派生都会重算 `_infer_direction(name)`，
    `causal_weight_feedback` 每小时对 applied 链跑一次，标注不留痕就会被静默覆盖。

    文件缺失/损坏 → 返回空 dict，退回规则法（宁可退回，也不要 import 失败）。
    """
    try:
        mtime = os.path.getmtime(LLM_LABELS_PATH)
    except OSError:
        return {}
    if _LLM_LABEL_CACHE["mtime"] == mtime:
        return _LLM_LABEL_CACHE["map"]
    try:
        with open(LLM_LABELS_PATH, encoding="utf-8") as fh:
            raw = json.load(fh)
        labels = {str(k): str(v).upper() for k, v in (raw.get("labels") or {}).items()
                  if str(v).upper() in ("UP", "DOWN", "BOTH")}
    except (OSError, ValueError, TypeError, AttributeError):
        labels = {}
    _LLM_LABEL_CACHE.update({"mtime": mtime, "map": labels})
    # DB 镜像优先（JSON 是写入侧现用的落点，DB 是耐久副本；冲突时信 DB）
    return dict(labels, **_overlay_from_db()) if labels else _overlay_from_db()


def has_column(conn, table: str, column: str) -> bool:
    """表里有没有这一列（用于可选的 v401 列 direction_source）。"""
    try:
        return any(r[1] == column for r in conn.execute(f"PRAGMA table_info({table})"))
    except sqlite3.Error:
        return False


def _parse_ts(text) -> int:
    """把 'YYYY-MM-DD HH:MM:SS'（本地时间）转 epoch；解析不了返回 0（=不做时间约束）。"""
    if not text:
        return 0
    try:
        return int(time.mktime(time.strptime(str(text)[:19], "%Y-%m-%d %H:%M:%S")))
    except (ValueError, OverflowError):
        return 0


def _direction_for_write(name, mechanism, precondition, dir_old, dir_src, chain_id=None):
    """方向取值: LLM 标过(P0-3)就用它, 否则退回规则法。

    为什么要记 `direction_source`：v401 之前 `derive_strategy_columns` 每次调用
    都重算 `_infer_direction(name)` —— 一条链只要被编辑/扩链触发一次重派生，
    LLM 标注就会被**规则法静默覆盖**，而且没有任何痕迹。而这套规则法在 v322
    实测只能到 41.9%（低于随机），覆盖掉语义标注等于把最好的信号换回最差的。
    """
    old = str(dir_old or "").upper()
    if str(dir_src or "").lower() == "llm" and old in ("UP", "DOWN", "BOTH"):
        return old
    if chain_id is not None:
        lbl = llm_direction_overlay().get(str(chain_id))
        if lbl:
            return lbl
    return _infer_direction(name, mechanism or "", precondition or "")


def load_active_chains(conn, priced_assets=None):
    """加载可用于评估的因果链.

    v302 兼容层: causal_chains 表已被 migrate_causal_chains.py 重设计, 旧列
    (chain_id/asset/expected_direction/magnitude_lo/hi/confidence/trigger_keywords/active)
    全部不存在. 新结构把资产编码在 trigger_words 的 "Asset,<TICKER>" 前缀里,
    方向则需从 name/cause/effect 文本推断.
    """
    if priced_assets is None:
        priced_assets = _load_priced_assets(conn)
    _src_col = ("direction_source" if has_column(conn, "causal_chains", "direction_source")
                else "'rule' AS direction_source")
    _sel = ("SELECT id, name, cause, effect, trigger_words, weight, mechanism,"
            f" precondition, expected_direction, {_src_col}, created_at")
    rows = conn.execute(_sel + " FROM causal_chains WHERE enabled=1").fetchall()
    out = []
    for r in rows:
        # v400 P0-2: 品种不再取"词表里第一个 ticker"（会让讲 XAU 的链因为词表
        # 里 WTI 排在前面而被绑到 WTI），改按语义打分：effect +3 / name +2 /
        # cause +1 / trigger_words +1。判不准就留 UNKNOWN，不硬猜。
        decision = chain_keywords.asset_for_row(r, priced_assets=priced_assets)
        if decision.asset == "UNKNOWN":
            continue  # 无行情/判不准 → 无法评估, 跳过
        # 关键词：剔除系统 token、品种代码与结构性 stopword（跨资产串味）。
        kws = list(chain_keywords.clean_keywords(
            r["trigger_words"], max_keywords=chain_keywords.MAX_KEYWORDS).kept)
        kws = [p for p in kws if p.lower() not in _CHAIN_STOPWORDS]
        if not kws:
            continue
        out.append(ChainRow(
            chain_id=r[0],
            asset=decision.asset,
            trigger_keywords=kws,
            expected_direction=_direction_for_write(r[1], r[6], r[7], r[8], r[9], r[0]),
            magnitude_lo=DEFAULT_MAG_LO,
            magnitude_hi=DEFAULT_MAG_HI,
            confidence=float(r[5] or 0.5),
            created_ts=_parse_ts(r[10]),
            cause=(r[2] or ""),
            cause_tone=_tone_of(r[2] or ""),
            name=(r[1] or ""),
        ))
    return out


def derive_strategy_columns(conn, chain_ids=None) -> dict:
    """把"策略链路"列从知识库列派生出来 —— 与 load_active_chains 完全同源.

    背景: causal_chains 一表两用。
      知识列 (name/cause/effect/trigger_words/enabled/origin/weight) —— 真实数据源
      策略列 (chain_id/asset/trigger_keywords/expected_direction/magnitude_*) ——
        bridge_server 与反馈闭环按这些列查询, 缺失时整条链路静默失效。

    派生规则 (与 load_active_chains 逐条一致, 避免两套语义漂移):
      chain_id            = CAST(id AS TEXT)   —— 必须与 outcomes.chain_id 同键
      asset               = 语义打分判定（v400 P0-2: effect+3 / name+2 / cause+1 /
                            trigger_words+1），判不准 → 'UNKNOWN'
      trigger_keywords    = trigger_words 净化后剩余词（v400 P0-1: 删系统 token /
                            品种代码 / 超长短语 / 重复，上限 12 个）
      expected_direction  = _infer_direction(name)
      magnitude_lo / hi   = DEFAULT_MAG_LO / DEFAULT_MAG_HI
      confidence          = weight (夹到 [0, 1])

    asset 必须是真实 ticker: bridge 的 _match_chains_for_outcome 用
    `WHERE active=1 AND asset=?` 匹配, asset 为 'UNKNOWN' 时命中恒为 0,
    hit/miss 反馈闭环会完全空转。

    chain_ids=None → 全表; 否则只处理指定 id (新建链入库后立即调用)。
    返回统计 dict: {"updated", "with_asset", "priced_assets"}。
    """
    priced_assets = _load_priced_assets(conn)
    # v401: direction_source 是可选列（老库/测试库可能没有）——没有就按 'rule' 处理
    _src_col = ("direction_source" if has_column(conn, "causal_chains", "direction_source")
                else "'rule' AS direction_source")
    _sel = ("SELECT id, name, cause, effect, trigger_words, weight, mechanism,"
            f" precondition, expected_direction, {_src_col}")

    if chain_ids is None:
        rows = conn.execute(_sel + " FROM causal_chains").fetchall()
    else:
        ids = [int(i) for i in chain_ids]
        if not ids:
            return {"updated": 0, "with_asset": 0, "priced_assets": len(priced_assets)}
        qmarks = ",".join("?" * len(ids))
        rows = conn.execute(
            _sel + f" FROM causal_chains WHERE id IN ({qmarks})", ids
        ).fetchall()

    updated = with_asset = 0
    band_cache: dict = {}
    for (cid, name, cause, effect, trigger_words, weight, mechanism, precondition,
         dir_old, dir_src) in rows:
        # v400 P0-2: 品种按语义打分判定（effect+3 / name+2 / cause+1 / trigger+1）。
        # 旧规则"词表里第一个 ticker"会把讲 XAU 的链绑到 WTI；319/752 落到 UNKNOWN。
        decision = chain_keywords.decide_asset(
            name or "", cause or "", effect or "", trigger_words or "",
            priced_assets=priced_assets,
        )
        asset = None if decision.asset == "UNKNOWN" else decision.asset

        # v400 P0-1: 关键词走统一净化（删系统 token / 品种代码 / 超长 / 重复）。
        kws = [p for p in chain_keywords.clean_keywords(
            trigger_words, max_keywords=chain_keywords.MAX_KEYWORDS).kept
            if p.lower() not in _CHAIN_STOPWORDS]

        try:
            conf = float(weight)
        except (TypeError, ValueError):
            conf = 0.5
        conf = max(0.0, min(1.0, conf))

        if asset is None:
            # 无行情资产 → bridge 无法按 asset 命中, 保持 UNKNOWN 且不给关键词
            new_asset, new_kws = "UNKNOWN", ""
        else:
            new_asset, new_kws = asset, ",".join(kws)
            with_asset += 1

        # v314 P1: 幅度带按资产 (当前时点的已实现波动) 推导, 不再是全局常量。
        # 历史 outcome 的带用触发时点的口径重算, 见 resnapshot_predictions。
        if asset is None:
            mag_lo, mag_hi = DEFAULT_MAG_LO, DEFAULT_MAG_HI
        else:
            if asset not in band_cache:
                band_cache[asset] = derive_magnitude_band(conn, asset, int(time.time()))
            mag_lo, mag_hi = band_cache[asset]

        conn.execute(
            "UPDATE causal_chains SET chain_id=CAST(id AS TEXT), asset=?, trigger_keywords=?,"
            " expected_direction=?, magnitude_lo=?, magnitude_hi=?, confidence=?,"
            " updated_at=datetime('now','localtime') WHERE id=?",
            (new_asset, new_kws, _direction_for_write(name, mechanism, precondition,
                                                      dir_old, dir_src, cid),
             mag_lo, mag_hi, conf, cid),
        )
        updated += 1

    return {"updated": updated, "with_asset": with_asset,
            "priced_assets": len(priced_assets)}


_COMMON_TICKERS = {"btc", "eth", "sol", "solana", "xrp", "bnb", "ada", "doge", "mu",
                   "tsla", "nvda", "wti", "xau", "gold", "oil", "skhynix", "micron",
                   "cardano", "fed", "fomc", "sec", "opec"}
# 常见 ticker/通用词 —— 单独出现不足以判定链路相关, 需额外 keyword 确认


def meaningful_keyword_hit(text: str, kws: Iterable[str], asset: str = "") -> list[str]:
    """返回命中的关键词, 但要求至少命中一个"有意义"的关键词。

    规则 (与 _kw_match 一致, 供 bridge 等其它消费者复用):
      - 纯 ASCII 关键词: 需 len>=4 且不在常见 ticker/通用词集合里
      - 含非 ASCII 的关键词: 需 len>=2

    只有 ticker 或过短词命中时视为未命中 —— 否则 'buy'/'sell' 这类
    高频词会让所有链同时命中, 命中量被严重放大。
    asset: 该链自身的资产代码, 同样不算有意义命中 (避免资产名自证)。
    """
    text = text or ""
    text_lower = text.lower()
    tickers = set(_COMMON_TICKERS)
    if asset:
        tickers.add(asset.lower())
    hit: list[str] = []
    meaningful: list[str] = []
    for k in kws or []:
        if not k:
            continue
        if re.match(r"^[A-Za-z0-9_]+$", k):
            if k.lower() in text_lower:
                hit.append(k)
                if k.lower() not in tickers and len(k) >= 4:
                    meaningful.append(k)
        else:
            if k in text:
                hit.append(k)
                if len(k) >= 2:
                    meaningful.append(k)
    return hit if meaningful else []


def _kw_match(headline, body, kws, asset):
    """返回匹配上的 keyword 列表.
    规则: 至少 1 个非资产代码/常见词关键词命中 (避免单纯 ticker 误匹配)."""
    return meaningful_keyword_hit((headline or "") + " " + (body or ""), kws, asset)


# ── v428 方向语气（trigger 层的方向语义）────────────────────────────────────
# 问题：触发词只标题材不标方向 —— "地缘冲突/霍尔木兹" 这类词同时喂给
# 「冲突升级→原油偏多」和「停火降温→原油承压」两条链，双方各自把同一批
# 新闻记成自己的样本，必然一条被判死。实测 22 组链共用同一批触发事件。
# 治法：链的**因端语气**与**事件语气**必须一致，不一致就不触发。
_TONE_ESCALATE = (
    "升级", "袭击", "空袭", "无人机", "炮击", "导弹", "军事打击", "开火", "封锁",
    "断供", "中断", "威胁", "强硬", "制裁", "开战", "冲突升级", "爆炸", "遇袭",
    "击落", "登陆", "入侵", "征兵", "撤离使馆", "打击", "紧张",
    # 英文（raw_news 正文大量是英文/外媒转载，只有中文词表时闸门形同虚设：
    # 实测只拦到 1% 的事件）
    "attack", "strike", "missile", "drone", "escalat", "shelling", "blockade",
    "invasion", "war", "conflict", "sanction", "offensive", "airstrike",
    "retaliat", "mobiliz", "hostilit",
)
_TONE_EASE = (
    "停火", "和谈", "谈判", "会谈", "调解", "斡旋", "缓和", "降温", "恢复",
    "撤退", "让步", "妥协", "达成", "缓解", "停战", "休战", "解除", "和平",
    "ceasefire", "truce", "talks", "negotiat", "de-escalat", "deescalat",
    "easing", "dialogue", "agreement", "withdraw", "retreat", "concession",
    "peace",
)


def _tone_of(text: str) -> str:
    """事件/链因端的语气：escalate（升级）/ ease（缓和）/ ""（判不出或两边都有）。

    用出现次数取胜，平票视为判不出 —— 宁可放过，也不要用一个含糊的标签
    把事件误判成另一个方向（误判会直接污染样本）。
    """
    t = text or ""
    esc = sum(t.count(w) for w in _TONE_ESCALATE)
    ease = sum(t.count(w) for w in _TONE_EASE)
    if esc > ease:
        return "escalate"
    if ease > esc:
        return "ease"
    return ""


def _char_bigrams(text):
    """字符二元组集合（去空白与标点），用于判断两条新闻是否同一件事。"""
    t = re.sub(r"[\s\W_]+", "", (text or "").lower())
    if len(t) < 4:
        return set()
    return {t[i:i + 2] for i in range(len(t) - 1)}


def _same_story(text, prev_texts):
    """text 与已采纳样本是否报道同一件事。

    只比较回看窗口内的样本；用**包含率**（交集/较短者）而不是 Jaccard ——
    同一事件的长稿与短讯 Jaccard 会很低，包含率能抓住。
    """
    cur = _char_bigrams(text)
    if not cur:
        return False
    for old in prev_texts:
        old_b = _char_bigrams(old)
        if not old_b:
            continue
        if len(cur & old_b) / max(1, min(len(cur), len(old_b))) >= _STORY_DUP_RATIO:
            return True
    return False


def _chain_names_similar(a, b):
    """两条链是不是在讲同一件事（链名二元组包含率，取较短者做分母）。"""
    ba, bb = _char_bigrams(a or ""), _char_bigrams(b or "")
    if not ba or not bb:
        return False
    return len(ba & bb) / max(1, min(len(ba), len(bb))) >= _CHAIN_NAME_DUP_RATIO


def _load_news_with_content(conn, days):
    """一次性载入窗口内新闻+正文.

    backfill 会对每条链调用 find_news_triggers, 若每次都全表扫描 5966 行 × 270 条链
    会退化. 这里做进程内缓存 (同一 conn 下结果不变).
    """
    key = "_news_cache_%s" % days
    cached = getattr(conn, key, None)
    if cached is not None:
        return cached
    start_ts = int(time.time()) - days * 86400
    rows = conn.execute(
        """SELECT ne.id, COALESCE(rn.content, ne.headline, '') || ' ' || COALESCE(ne.body, '') AS content,
                  ne.asset, ne.ts, ne.action, ne.news_id
           FROM news_events ne
           LEFT JOIN raw_news rn ON rn.id = ne.news_id
           WHERE ne.ts>=? ORDER BY ne.ts DESC""",
        (start_ts,),
    ).fetchall()
    try:
        setattr(conn, key, rows)
    except AttributeError:
        pass  # sqlite3.Connection 不允许任意属性时忽略缓存
    return rows


def find_news_triggers(conn, chain, days):
    """扫描新闻, 找触发 chain 的事件.

    v302 兼容层: news_events 表没有 headline/body/assets 列, 实际列为
    (id, news_id, decision_id, ts, source, asset, action, score, is_noise, status).
    标题正文在 raw_news.content, 通过 news_id = raw_news.id 关联 (约 27% 可关联).
    """
    rows = _load_news_with_content(conn, days)
    seen = []   # [(ts, content)]，同时用于冷却与"同一件事"去重
    # v302+ P2: 资产别名集合 — 用 chain.asset 的别名在正文里命中,
    # 避免 news.asset 字段不完整导致漏匹配 (XAU→黄金/WTI→原油 等).
    aliases = ASSET_ALIASES.get(chain.asset.upper(), [chain.asset.lower()])
    alias_lower = [a.lower() for a in aliases]
    for r in rows:
        content = r[1] or ""
        news_asset = (r[2] or "")
        text_lower = content.lower()
        # 双通道匹配: (a) news_events.asset 等于 chain.asset
        #           (b) 正文含 chain.asset 别名
        asset_match = (news_asset or "").upper() == chain.asset.upper()
        alias_match = bool(content) and any(a in text_lower for a in alias_lower)
        if not (asset_match or alias_match):
            continue
        hit = _kw_match(content, "", chain.trigger_keywords, chain.asset)
        if not hit:
            continue
        # v428 语气闸门：因端说"升级"就别用"停火"新闻喂它，反之亦然。
        chain_tone = getattr(chain, "cause_tone", "") or ""
        if chain_tone:
            ev_tone = _tone_of(content)
            if ev_tone and ev_tone != chain_tone:
                continue
        # 冷却：同类事件最短间隔
        if any(abs(r[3] - t) < TRIGGER_COOLDOWN_HOURS * 3600 for t, _ in seen):
            continue
        # 同一件事去重（跨天的同题材报道，冷却挡不住，靠内容重合率挡）
        near = [txt for t, txt in seen
                if abs(r[3] - t) < _STORY_DUP_HOURS * 3600]
        if near and _same_story(content, near):
            continue
        seen.append((r[3], content))
        yield {
            "id": r[0], "news_id": r[5], "headline": content,
            "asset": news_asset, "ts": r[3], "action": r[4],
            "matched_kws": hit,
        }


def _percentile(sorted_vals, pct):
    """线性插值分位数 (sorted_vals 必须已升序)。"""
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    k = (len(sorted_vals) - 1) * (pct / 100.0)
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    if f == c:
        return float(sorted_vals[f])
    return float(sorted_vals[f]) + (float(sorted_vals[c]) - float(sorted_vals[f])) * (k - f)


# 幅度带的分位口径: lo = p10, hi = p90 —— 该资产常态波动区间的**中心 80% 区间**
# (统计学里"正常范围"的标准定义: 中位数 ± 1.28σ, 对应正态分布的 80% 中心区间)。
#   lo 的含义: 低于它 = 市场基本没反应 (触发时点之前该资产 5 日净波动最小的 10%),
#              此时的"方向对"只能算弱命中;
#   hi 的含义: 高于它 = 异动 (幅度超出常态区间), 链的幅度主张偏离。
# 口径与验收阈值无关 —— p10/p90 是先验定的"正常范围"定义, 不是为了跨过某个阈值。
MAG_BAND_LO_PCT = 10
MAG_BAND_HI_PCT = 90
MAG_BAND_LOOKBACK_DAYS = 180
MAG_BAND_MIN_SAMPLES = 12
MAG_BAND_FLOOR = 0.05      # 下限 % (防止带被压到 0 以下)
MAG_BAND_CEIL = 20.0       # 上限 %
_BAND_CACHE: dict = {}


def derive_magnitude_band(conn, asset, asof_ts, lookback_days=MAG_BAND_LOOKBACK_DAYS,
                          min_samples=MAG_BAND_MIN_SAMPLES):
    """按资产 + **asof_ts 之前**的已实现 5 日 |涨跌| 分布推导幅度带。

    背景 (v314 P1): 原来所有链的 magnitude_lo/hi 都被写成全局常量
    (DEFAULT_MAG_LO=1.0 / DEFAULT_MAG_HI=8.0) —— 对低波动资产结构性不可能命中。
    实测: XAU 触发窗口的 5 日 |涨跌| 只有 0.05%~0.23%, 而带下限是 1.0%
    → XAU 的 178 条已评估样本里 HIT 恒为 0 (方向对也只能记 HIT_WEAK),
    真命中率被系统性压低, 与链本身的噪声无关。

    口径 (PIT, 无未来信息):
      - 只用 asof_ts (触发时点) 之前的日线, 之后的价格一律不看;
      - 窗口与评估一致: 5 个**交易日**的 |涨跌| (T0 → T+4);
      - 取该分布的中心 80% 区间 [p10, p90] 作为"该资产常态波动区间";
      - 样本不足 (历史太短) 时退回旧的全局常量, 不臆造。

    返回 (lo, hi), 单位 %。
    """
    if not asset:
        return DEFAULT_MAG_LO, DEFAULT_MAG_HI
    cache_key = (id(conn), asset, int(asof_ts))
    cached = _BAND_CACHE.get(cache_key)
    if cached is not None:
        return cached
    ts_from = int(asof_ts) - int(lookback_days) * 86400
    # 与评估窗口同一份去重序列 (同源同规则); 只保留 ts<=asof 的日线 → 不含未来信息
    series = [x for x in _cached_series(conn, asset)
              if ts_from <= x[0] <= int(asof_ts)]
    rets = []
    for i in range(4, len(series)):
        c0, c1 = series[i - 4][1], series[i][1]
        if c0 > 0:
            rets.append(abs(c1 / c0 - 1.0) * 100.0)
    if len(rets) < min_samples:
        return DEFAULT_MAG_LO, DEFAULT_MAG_HI
    rets.sort()
    lo = _percentile(rets, MAG_BAND_LO_PCT)
    hi = _percentile(rets, MAG_BAND_HI_PCT)
    lo = max(MAG_BAND_FLOOR, lo)
    hi = min(MAG_BAND_CEIL, max(hi, lo * 2.0))
    lo = min(lo, hi / 2.0)
    band = (round(lo, 2), round(hi, 2))
    _BAND_CACHE[cache_key] = band
    return band


def get_close_at_or_before(conn, asset, ts):
    row = conn.execute(
        "SELECT close FROM ohlcv_daily WHERE asset=? AND ts<=? ORDER BY ts DESC LIMIT 1",
        (asset, ts),
    ).fetchone()
    return float(row[0]) if row else None


# ohlcv_daily 的同一资产同一日历日可能有**多行** (不同采集源: yfinance /
# kline_bars_1d / eastmoney_refresh ...)。实测 MU 276 天、XAU 132 天、
# WTI 19 天存在同日双行 (且 close 不同, 例如 XAU 2026-10-21: yfinance 4146.00
# vs kline_bars_1d 4134.60)。
#
# 后果 (v314 P0 修复): 旧实现用 `LIMIT 1 OFFSET n` 按**行**数 T+N, 同一天的两行
# 被当成两个交易日 → "T+5" 实际落在第 2~3 个日历日, 涨跌幅度被严重低估,
# 方向判定也随之出错。实测 548 个 (资产, 日历日) 组合有重复行。
#
# 现在统一按**日历日去重**: 同一天只保留一行 (按 source 优先级 + ts 靠后),
# 再在去重后的序列上按交易日计数。
_SOURCE_PRIORITY = (
    "yfinance", "yfinance_refresh", "eastmoney_refresh",
    "coingecko_refresh", "kline_bars_1d", "backfill_ohlcv_local",
)


def _ohlcv_has_source(conn) -> bool:
    """检测 ohlcv_daily 是否带 source 列。

    v310 P1 修复: 旧实现无条件 `SELECT ts, close, source FROM ohlcv_daily`,
    一旦遇到没有 source 列的库 (精简 schema / 测试内存库 / 迁移前的快照),
    sqlite3.Error 被 except 吞掉 → 整条序列变成空列表 → T0 取不到价 →
    evaluate_one 把每一条 outcome 都判成 EXPIRED(no_ohlcv_at_trigger),
    而真实原因其实是"这张表没有 source 列"。这里改成按实际 schema 选列。
    """
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(ohlcv_daily)")}
    except sqlite3.Error:
        return False
    return "source" in cols


def _daily_series(conn, asset, ts_from=None, ts_to=None):
    """返回某资产的**按日历日去重**的日线序列 [(ts, close), ...] (升序)。

    去重规则: 同一日历日只保留一行 —— 先按 source 优先级 (yfinance 最优先),
    同优先级取 ts 靠后的一行。这样 T0 与 T+N 落在同一口径的序列上。

    v310 P1: source 列是可选的 —— 没有该列时按"未知来源"处理 (优先级最低),
    去重退化为"同一日历日取 ts 靠后的一行"。
    """
    has_source = _ohlcv_has_source(conn)
    sel = "ts, close, source" if has_source else "ts, close"
    where = "asset=?"
    params: list = [asset]
    if ts_from is not None:
        where += " AND ts>=?"
        params.append(int(ts_from))
    if ts_to is not None:
        where += " AND ts<=?"
        params.append(int(ts_to))
    try:
        rows = conn.execute(
            f"SELECT {sel} FROM ohlcv_daily WHERE {where} ORDER BY ts ASC",
            tuple(params),
        ).fetchall()
    except sqlite3.Error:
        return []
    best: dict = {}
    for r in rows:
        ts = int(r[0])
        try:
            close = float(r[1])
        except (TypeError, ValueError):
            continue
        if close <= 0:
            continue
        src = str(r[2] or "") if has_source else ""
        day = time.strftime("%Y-%m-%d", time.localtime(ts))
        rank = (_SOURCE_PRIORITY.index(src) if src in _SOURCE_PRIORITY
                else len(_SOURCE_PRIORITY))
        prev = best.get(day)
        if prev is None:
            best[day] = (ts, close, rank)
        else:
            p_ts, p_close, p_rank = prev
            if rank < p_rank or (rank == p_rank and ts >= p_ts):
                best[day] = (ts, close, rank)
    return [(v[0], v[1]) for _, v in sorted(best.items())]


# 序列缓存: key=(id(conn), asset), value=(cached_at, series, conn_ref)
# conn_ref 保留强引用 → id(conn) 在条目存活期间不会被复用 → 不会串库。
_SERIES_CACHE: "OrderedDict[tuple, tuple]" = OrderedDict()
_SERIES_CACHE_TTL = 120.0
_SERIES_CACHE_MAX = 64


def _cached_series(conn, asset):
    """全量按日去重序列的进程内缓存 (行情按日更新, 缓存 120s 足够安全)。

    evaluate_all 要对 800+ 条 outcome 逐条取价, 每次全表扫描会把评估拖成
    分钟级; 价格数据按日更新, 短缓存不会引入陈旧口径。

    v310 P1: 缓存条目额外持有 conn 强引用 —— 防止 Python 回收 conn 后
    id(conn) 被下一个连接复用, 导致**串库**(拿到另一个库的行情序列)。
    """
    key = (id(conn), asset)
    now = time.time()
    hit = _SERIES_CACHE.get(key)
    if hit is not None and now - hit[0] < _SERIES_CACHE_TTL and hit[2] is conn:
        _SERIES_CACHE.move_to_end(key)
        return hit[1]
    series = _daily_series(conn, asset)
    _SERIES_CACHE[key] = (now, series, conn)
    _SERIES_CACHE.move_to_end(key)
    while len(_SERIES_CACHE) > _SERIES_CACHE_MAX:
        _SERIES_CACHE.popitem(last=False)
    return series


def get_t0_bar(conn, asset, ts):
    """T0 = 触发时点之前(含当日)最后一根**已完整落库**的交易日日线 → (close, ts)。

    v314 P2 (同源口径): 旧实现用 ts_to=trigger 截断**原始行**再去重 ——
    当首选源 (yfinance) 当天的行 ts 晚于触发时点时, 锚点会退回到别的源
    (kline_bars_1d), 而前向窗口又按源优先级选回 yfinance → 同一段行情的
    T0 与 T+5 来自**两个不同的数据源**。实测 19.1% 的已评估行跨源, 同源重算后
    14.5% 的行方向翻转 (XAU 高达 34.8%) —— 对 XAU 这种 5 日净波动只有
    0.05~0.23% 的资产, 这种混用足以翻转方向判定。

    现在改成: 先按 source 优先级**全量**去重, 再取最后一根 ts<=trigger 的日线。
      - T0 与 T+5 必然来自同一份去重序列 (同源同口径);
      - 锚点仍是"触发时点之前最后一根已落库的收盘", 不含未来信息;
      - 代价: 若首选源的当日行在触发之后才落库, 锚点退到前一根日线
        (事件研究的标准做法: 用事件前最后一个完整收盘做锚)。
    """
    series = _cached_series(conn, asset)
    anchor = None
    for bar_ts, close in series:
        if bar_ts <= int(ts):
            anchor = (close, bar_ts)
        else:
            break
    return anchor if anchor else (None, None)


def get_close_t_plus_n(conn, asset, start_ts, n):
    """start_ts 当日或之后第 n 个**交易日**的 close (0=T+0, 4=T+5 第 5 个).

    v310 P1 修复 (方向性 bug): 旧实现拿 `start_ts - 14*86400` 当锚点再 OFFSET n,
    取到的是**触发之前**的第 n 根日线 —— 算出来是"消息出来之前行情怎么走的",
    跟链要预测的未来方向天然相反, 而且完全没用到触发后的真实反应。
    (那 14 天回溯窗口的本意只是"保证窗口里有 5 根连续交易日", 用来放宽**取数起点**
     用的, 不该把结果锚点一起推走。)

    v314 P0 修复 (交易日计数): OFFSET 原来数的是**行**, 而 ohlcv_daily 同一资产
    同一日历日可能有多行 (不同采集源) → T+N 提前落地。改为在**日历日去重**后的
    序列上按交易日计数。

    v314 P2 (同源口径): 与 get_t0_bar 共用同一份去重序列 —— T0 与 T+N 必须
    同源同规则, 否则两端价差里混进了两个数据源的定价差异。

    正确口径: 以 T0 (触发时点之前最后一根已落库日线) 的 ts 为锚, 往后数第 n 根
    (n=0 → T0 本身, n=4 → 第 5 根, 即 T+4), 测的是"消息出来之后价格怎么走"。
    """
    series = [x for x in _cached_series(conn, asset) if x[0] >= int(start_ts)]
    if len(series) <= n:
        return None, None
    ts_n, close_n = series[n]
    return close_n, ts_n


def insert_pending_outcome(conn, chain, ev, deadline_ts):
    """插入 PENDING outcome. 返回 True 表示真正新增了行.

    注意: 不能靠 IntegrityError 判断去重 —— 本表的 UNIQUE 约束会让
    INSERT OR IGNORE **静默跳过**重复行而不抛异常, 导致调用方把"已存在"
    误报成"新增", 计数虚高. 这里改用 cursor.rowcount 判定实际插入.

    v314 P1: 幅度带按**该资产在触发时点之前**的已实现波动推导 (PIT),
    不再沿用全局常量 (低波动资产恒不可能命中)。
    """
    mag_lo, mag_hi = derive_magnitude_band(conn, chain.asset, int(ev["ts"]))
    try:
        cur = conn.execute(
            """INSERT OR IGNORE INTO causal_chain_outcomes
                 (chain_id, trigger_ts, trigger_source, trigger_ref_id,
                  trigger_headline, trigger_keywords_matched,
                  predicted_direction, predicted_mag_lo, predicted_mag_hi,
                  eval_deadline_ts, status)
               VALUES (?, ?, 'news_events', ?, ?, ?, ?, ?, ?, ?, 'PENDING')""",
            (chain.chain_id, ev["ts"], ev["id"],
             (ev["headline"] or "")[:200], ",".join(ev["matched_kws"]),
             chain.expected_direction, mag_lo, mag_hi,
             deadline_ts),
        )
        conn.commit()
        return cur.rowcount > 0
    except sqlite3.IntegrityError:
        return False


# ── v432：评估窗口按事件类型分档 ─────────────────────────────────────────────
# 问题：所有链都用"次日之后第 5 个交易日"评方向。地缘袭击/停火/OPEC 定价这类
# 消息的真实效果是当天到两天内的脉冲，用 5 日尺度去量，等于拿放大镜看流星的尾巴
# （实测方向命中率 50.0%，已计价样本剔除后仍不动）。
# 分档：脉冲型（事件/声明/定价/监管批复）→ T+1；趋势型（利率/通胀/流动性/央行）
# 和判不出类型的 → 保持 T+4（现有口径，不动）。
_HORIZON_PULSE_BARS = 1
_HORIZON_TREND_BARS = 4
_PULSE_WORDS = (
    "袭击", "空袭", "停火", "和谈", "谈判", "制裁", "封锁", "断供", "中断", "减产",
    "增产", "OPEC", "OSP", "地缘", "突发", "事件", "声明", "表态", "批准", "ETF",
    "上市", "公告", "财报", "数据公布", "会议纪要", "爆炸", "冲突", "放行", "豁免",
)
_TREND_WORDS = (
    "实际利率", "收益率", "通胀", "流动性", "央行", "储备", "缩表", "降息", "加息",
    "美元信用", "周期", "结构性", "长期", "信用溢价", "资产负债表",
)
_HORIZON_CACHE: Dict[int, int] = {}


def chain_horizon_bars(conn, chain_id) -> int:
    """该链的评估窗口（交易日偏移）：脉冲型 1，趋势型/判不出 4。"""
    try:
        cid = int(chain_id)
    except (TypeError, ValueError):
        return _HORIZON_TREND_BARS
    if cid in _HORIZON_CACHE:
        return _HORIZON_CACHE[cid]
    try:
        row = conn.execute(
            "SELECT name, cause, effect, mechanism FROM causal_chains WHERE id=?", (cid,)
        ).fetchone()
    except sqlite3.Error:
        return _HORIZON_TREND_BARS
    if not row:
        return _HORIZON_TREND_BARS
    text = " ".join(str(x or "") for x in row)
    pulse = sum(text.count(w) for w in _PULSE_WORDS)
    trend = sum(text.count(w) for w in _TREND_WORDS)
    if pulse > trend:
        bars = _HORIZON_PULSE_BARS
    else:
        bars = _HORIZON_TREND_BARS
    _HORIZON_CACHE[cid] = bars
    return bars


def _resolve_chain_asset(conn, chain_id, priced_assets=None):
    """按 chain_id(= causal_chains.id) 解析资产.

    v302 兼容层: 新表没有 asset 列, 资产编码在 trigger_words 里, 需按
    有行情的 ticker 反查. 找不到返回 None.
    """
    row = conn.execute(
        "SELECT trigger_words FROM causal_chains WHERE id=?", (chain_id,)
    ).fetchone()
    if not row:
        return None
    if priced_assets is None:
        priced_assets = _load_priced_assets(conn)
    for p in (row[0] or "").split(","):
        pu = p.strip().upper()
        if pu in priced_assets:
            return pu
    return None


def _write_status(conn, outcome_id, status, reason):
    """把状态显式写回 outcome 行。

    v310 P1: 旧实现在"无法评估"时直接 return, 不动表 —— 于是上一轮(旧锚点)
    算出来的 HIT/MISS 留在表里, 被当成新结论继续统计: 真命中率既不可信也不
    可复现(同一条链, 口径一改, 结论还是旧的)。这里把状态显式落库:
      - 前向取价还没到 T+5 → PENDING (还没到评估时点, 等行情到齐)
      - 资产没了 / 触发时点没有行情 → EXPIRED (这条链无法再评估)
    """
    try:
        conn.execute(
            """UPDATE causal_chain_outcomes
                  SET status=?, notes=?, evaluated_at=?,
                      direction_hit=NULL, magnitude_hit=NULL
                WHERE id=?""",
            (status, json.dumps({"reason": reason}, ensure_ascii=False),
             int(time.time()), outcome_id),
        )
        conn.commit()
    except sqlite3.Error as _silent_exc_691:
        print("[causal_chain_evaluator] ⚠️ 写库/调用失败（原先静默吞掉）: %s: %s"
                  % (type(_silent_exc_691).__name__, str(_silent_exc_691)[:120]), flush=True)


#: v322: 市场基准收益缓存 {(ts_t0, ts_t5): pct}
_BENCH_CACHE: Dict[tuple, float] = {}


def _market_benchmark_pct(conn, ts_t0, ts_t5, min_assets=3):
    """同期**等权市场组合**收益（%），中位数口径（抗极值）。

    为什么必须有它（v322 实测）
    -------------------------
    方向命中率若不做基准校正，会被**市场漂移**污染：
    实测样本期只有 **36.4%** 的品种是涨的，于是
        · "无脑预测 DOWN" 的命中率天然就有 63.6%
        · "无脑预测 UP" 的命中率天然只有 36.4%
    拿这种数字当"判别力"是自欺欺人 —— 实测 DOWN 链命中率 63.5% ≈ 市场基准 63.6%，
    **相对基准的判别力其实是 0**。

    做法：把方向判定改成**超额收益** —— excess = 该资产收益 - 同期市场组合收益。
    这样"整个市场都在跌"不会让"预测 DOWN"的链白捡命中率。

    资产不足 min_assets 时返回 0（不校正）—— 宁可不做，也不要用 2 个资产乱校正。
    """
    key = (int(ts_t0), int(ts_t5))
    cached = _BENCH_CACHE.get(key)
    if cached is not None:
        return cached
    pcts = []
    for asset in _load_priced_assets(conn):
        series = _cached_series(conn, asset)
        if not series:
            continue
        c0 = _close_at_or_before(series, ts_t0)
        c5 = _close_at_or_before(series, ts_t5)
        if not c0 or not c5 or c0 <= 0:
            continue
        pcts.append((c5 - c0) / c0 * 100.0)
    if len(pcts) < min_assets:
        _BENCH_CACHE[key] = 0.0
        return 0.0
    pcts.sort()
    mid = len(pcts) // 2
    bench = pcts[mid] if len(pcts) % 2 else (pcts[mid - 1] + pcts[mid]) / 2.0
    _BENCH_CACHE[key] = bench
    return bench


def _close_at_or_before(series, ts):
    """序列里 ts <= 给定 ts 的最后一根 close。"""
    lo, hi = 0, len(series) - 1
    found = None
    while lo <= hi:
        mid = (lo + hi) // 2
        if series[mid][0] <= ts:
            found = series[mid][1]
            lo = mid + 1
        else:
            hi = mid - 1
    return found


def evaluate_one(conn, row, force=False):
    """row 是 outcomes 表的一行. 列序见 SELECT.

    v302+ 新增 force: 跳过 deadline 检查 (用于 force-evaluate).
    """
    outcome_id, chain_id, trigger_ts = row[0], row[1], row[2]
    pred_dir = row[10]
    mag_lo = float(row[11] or 0.0)
    mag_hi = float(row[12] or 999.0)
    deadline_ts = row[13] if len(row) > 13 else None
    now = int(time.time())
    if not force and deadline_ts and now < deadline_ts:
        return {"status": "PENDING", "reason": "not_due"}
    asset = _resolve_chain_asset(conn, chain_id)
    if not asset:
        _write_status(conn, outcome_id, "EXPIRED", "chain_removed_or_no_asset")
        return {"status": "EXPIRED", "reason": "chain_removed_or_no_asset"}
    close_t0, ts_t0 = get_t0_bar(conn, asset, trigger_ts)
    if close_t0 is None:
        _write_status(conn, outcome_id, "EXPIRED", "no_ohlcv_at_trigger")
        return {"status": "EXPIRED", "reason": "no_ohlcv_at_trigger"}
    _bars = chain_horizon_bars(conn, chain_id)          # v432 分档窗口
    close_t5, ts_t5 = get_close_t_plus_n(conn, asset, ts_t0, _bars)
    if close_t5 is None:
        # v310 P1: 前向取价还没到 T+5 —— 这条不是"过期", 而是"还没到评估时点"。
        due_ts = int(deadline_ts or (int(ts_t0) + 7 * 86400))
        if now < due_ts:
            _write_status(conn, outcome_id, "PENDING", "awaiting_t5_bar")
            return {"status": "PENDING", "reason": "awaiting_t5_bar"}
        _write_status(conn, outcome_id, "EXPIRED", "no_ohlcv_at_5d")
        return {"status": "EXPIRED", "reason": "no_ohlcv_at_5d"}
    pct_5d = (close_t5 - close_t0) / close_t0 * 100.0
    abs_pct = abs(pct_5d)
    # ── v322: 方向判定改用**超额收益**（剥离市场漂移）────────────────
    # 原来直接用 pct_5d 的符号 → 样本期整体偏跌时，"预测 DOWN"白捡 63.6% 命中率、
    # "预测 UP"只有 36.4% → 指标被市场 beta 污染，看不出真实判别力。
    # 现在 excess = 该资产 - 同期等权市场组合，"大盘同向涨跌"不再算方向判对。
    bench_pct = _market_benchmark_pct(conn, ts_t0, ts_t5)
    excess_pct = pct_5d - bench_pct
    # v430：**已计价标记** —— 触发前 5 个自然日里，价格是否已经朝"这条链预期的方向"
    # 走过至少 PRICED_IN_PCT。这类样本的失败往往不是机制错，而是消息落地时价已走完
    # （实测链级方向准确率只有 52%，一半的"错"可能属于这一类）。这里只打标不改判定，
    # 便于事后把"机制无效"和"已计价"分开统计。
    prior_pct = None
    priced_in = 0
    if pred_dir in ("UP", "DOWN"):
        try:
            series_pre = _cached_series(conn, asset)
            c_prev = _close_at_or_before(series_pre, int(ts_t0) - 5 * 86400)
            if c_prev and c_prev > 0:
                prior_pct = (close_t0 - c_prev) / c_prev * 100.0
                if (pred_dir == "UP" and prior_pct >= PRICED_IN_PCT) or \
                   (pred_dir == "DOWN" and prior_pct <= -PRICED_IN_PCT):
                    priced_in = 1
        except sqlite3.Error:
            prior_pct = None
    if pred_dir == "UP":
        direction_hit = 1 if excess_pct > 0 else 0
    elif pred_dir == "DOWN":
        direction_hit = 1 if excess_pct < 0 else 0
    else:
        # v429: BOTH（方向中性）链**没有方向主张**，不允许记"方向命中"。
        # 旧实现 direction_hit = 1 if abs_pct >= mag_lo —— 只要波动够大就记方向对，
        # 于是 1,153 条 HIT 里有 359 条是"恒真命中"：它们进 real_hit_rate、进
        # 链级准确率，把"验证通过的机制"比例虚增了一倍多。方向命中列只允许
        # 表达"方向判断对不对"，没判断就没有命中，写 NULL 让所有聚合自动跳过。
        direction_hit = None
    magnitude_hit = 1 if (mag_lo <= abs_pct <= mag_hi) else 0
    # v302+ P2 修复: HIT 判定改为"方向对"为 HIT, 幅度匹配记录到 magnitude_hit 字段,
    # 解决 5d 实际波动 < mag_lo 导致 NEUTRAL 误判 (此前 BTC mag_lo=2% 太严).
    # - HIT:    direction_hit=1 AND magnitude_hit=1  (方向对 + 幅度合理 = 完美)
    # - HIT_WEAK: direction_hit=1 AND magnitude_hit=0 (方向对但幅度偏 = 弱命中)
    # - MISS:   direction_hit=0 (方向反 = 完全错)
    #
    # v312 P0 —— **方向中性(BOTH)链的"没反应"是 NEUTRAL, 不是 MISS**:
    #   pred_dir='BOTH' 的链本来就没有方向主张 ("这个事件会让市场动起来, 但不知道往哪动")。
    #   旧实现把 direction_hit 定义成 "abs_pct >= mag_lo", 于是这类链在市场没动
    #   (< mag_lo) 时 direction_hit=0 → 记成 MISS —— 也就是"方向错了"。
    #   但 MISS 的语义是"方向反"(见上面三行注释), 对 BOTH 链根本无法成立:
    #   市场没动既不是方向对也不是方向错, 是"这条链预测的事件根本没发生"。
    #   schema / 验收 #10 都统计 NEUTRAL (n_neutral 单独一档, 不进 n_eval 分母),
    #   但旧代码从来不产生这一档 —— 实测 257 条已评估里 NEUTRAL=0, 而 BOTH 链的
    #   20 条 MISS 全部是"abs_pct < mag_lo"(均值仅 0.21%), 属同一类误判。
    #   修正后: 方向中性链没达到幅度门槛 → NEUTRAL; 有方向主张的链方向错 → 仍 MISS。
    # v431：**已计价样本不进方向命中率**。触发前 5 日价格已朝预期方向走 ≥1%，
    # 说明消息落地时这段行情大概率已经走完 —— 判对判错都不代表机制的预测力
    # （实测这类样本命中率 0.480，低于 0.568 的未计价样本）。和 BOTH 一样，
    # 方向命中列写 NULL，聚合自动跳过；原始判定记在 notes.dir_hit_raw 里可复核。
    dir_hit_raw = direction_hit
    if priced_in and pred_dir in ("UP", "DOWN"):
        direction_hit = None
    if pred_dir in ("BOTH", ""):
        # 方向中性链只能核"事件是否真的推动了价格"：幅度达标算命中，没动算 NEUTRAL
        if magnitude_hit:
            status = "HIT"
        elif abs_pct >= mag_lo:
            status = "HIT_WEAK"
        else:
            status = "NEUTRAL"
    elif direction_hit is None:
        # 已计价：只记录"消息落地后价格是否继续走"，不再判方向对错
        if magnitude_hit:
            status = "HIT"
        elif abs_pct >= mag_lo:
            status = "HIT_WEAK"
        else:
            status = "NEUTRAL"
    elif direction_hit == 1 and magnitude_hit == 1:
        status = "HIT"
    elif direction_hit == 1:
        status = "HIT_WEAK"
    else:
        status = "MISS"
    conn.execute(
        """UPDATE causal_chain_outcomes
           SET actual_close_at_trigger=?, actual_close_at_5d=?, actual_pct_5d=?,
               status=?, direction_hit=?, magnitude_hit=?, notes=?, evaluated_at=?
           WHERE id=?""",
        (close_t0, close_t5, pct_5d, status, direction_hit, magnitude_hit,
         json.dumps({"ts_t5": ts_t5, "mag_window": [mag_lo, mag_hi],
                     "priced_in": priced_in,
                     "dir_hit_raw": dir_hit_raw,
                     "prior_pct_5d": (round(prior_pct, 3) if prior_pct is not None else None),
                     "pred_dir": pred_dir,
                     "pct_5d": round(pct_5d, 4),
                     "bench_pct": round(bench_pct, 4),
                     "excess_pct": round(excess_pct, 4)},
                    ensure_ascii=False), now, outcome_id),
    )
    # v483 P0-2: 把本次评估结果回写到因果链主表的 hit_count/miss_count/last_verified_ts。
    # 此前评估器只写 outcomes、不反写链表，前端因果页（读 causal_chains.hit_count/
    # miss_count）永远看到 0/0，"链对不对、哪条错"在链层面不可见，闭环最后一段断开。
    # 口径与 outcomes 聚合一致：HIT / HIT_WEAK 记命中，MISS 记落空；NEUTRAL /
    # EXPIRED / 已计价(direction_hit IS NULL) 不计入任一计数。
    _sync_chain_hit_miss(conn, chain_id, status)
    conn.commit()
    return {"status": status, "pct_5d": pct_5d,
            "close_t0": close_t0, "close_t5": close_t5,
            "direction_hit": direction_hit, "magnitude_hit": magnitude_hit}


def _chain_has_columns(conn, chain_id, cols):
    """老库/精简库可能缺 hit_count 等列；先探测再写，缺列静默跳过。"""
    try:
        have = {r[1] for r in conn.execute("PRAGMA table_info(causal_chains)")}
    except sqlite3.Error:
        return False
    return all(c in have for c in cols)


def _sync_chain_hit_miss(conn, chain_id, status: str) -> None:
    """按本次评估结果累加链级命中/落空计数，并刷新最近验证时间。"""
    if status not in ("HIT", "HIT_WEAK", "MISS"):
        return
    if not _chain_has_columns(conn, chain_id, ("hit_count", "miss_count")):
        return
    hit_inc = 1 if status in ("HIT", "HIT_WEAK") else 0
    miss_inc = 1 if status == "MISS" else 0
    now = int(time.time())
    try:
        conn.execute(
            """UPDATE causal_chains
               SET hit_count = COALESCE(hit_count, 0) + ?,
                   miss_count = COALESCE(miss_count, 0) + ?,
                   last_verified_ts = ?
             WHERE id = ?""",
            (hit_inc, miss_inc, now, int(chain_id)),
        )
        # 样本攒够后，用近期命中率把链置信度往实测方向拉一档（>5 笔才动，
        # 避免一两笔噪声翻转）；拉幅限制在 ±0.15，不越出 [0.1, 0.95]。
        _recalibrate_chain_confidence(conn, chain_id)
    except sqlite3.Error as _sync_exc:
        # v490：链级命中/落空回写失败必须留痕（原先静默 pass），计入 silent_ledger
        # 并打告警，便于反思/指标体系发现"链对不对在链层面不可见"的回潮。
        try:
            import silent_ledger
            silent_ledger.record("causal_chain.sync_hit_miss",
                                 f"chain={chain_id} {type(_sync_exc).__name__}")
        except Exception:
            pass
        print("[causal_chain_evaluator] ⚠️ 链级 hit/miss 回写失败（原先静默吞掉）: "
              "%s: %s" % (type(_sync_exc).__name__, str(_sync_exc)[:120]), flush=True)


def _recalibrate_chain_confidence(conn, chain_id) -> None:
    """链级置信度按实测命中率校准：样本 ≥5 才动，向命中率收敛但留安全边距。"""
    if not _chain_has_columns(conn, chain_id, ("confidence",)):
        return
    try:
        row = conn.execute(
            """SELECT COUNT(*) n,
                      SUM(CASE WHEN status IN ('HIT','HIT_WEAK') THEN 1 ELSE 0 END) hit
                 FROM causal_chain_outcomes
                WHERE chain_id = ? AND status IN ('HIT','HIT_WEAK','MISS')""",
            (int(chain_id),),
        ).fetchone()
    except sqlite3.Error:
        return
    n = int(row[0] or 0)
    if n < 5:
        return
    hit = int(row[1] or 0)
    real_rate = hit / n
    cur = conn.execute("SELECT confidence FROM causal_chains WHERE id=?",
                       (int(chain_id),)).fetchone()
    if not cur or cur[0] is None:
        return
    cur_conf = float(cur[0] or 0.5)
    new_conf = cur_conf + (real_rate - cur_conf) * 0.35
    new_conf = round(max(0.1, min(0.95, new_conf)), 4)
    if abs(new_conf - cur_conf) >= 0.01:
        conn.execute(
            "UPDATE causal_chains SET confidence=? WHERE id=?",
            (new_conf, int(chain_id)),
        )


def backfill(conn, days=TIME_HORIZON_LOOKBACK):
    """扫窗口内新闻 → 为每条链写 PENDING outcome.

    v417 P0-3：**同一事件不许扇出成一场链雨**。
    实测一条"袭击"类新闻能同时触发 160 条链（93 条启用链共用"袭击"这个词），
    结果是 (a) 一次事件灌进 160 行样本、把统计稀释成噪声，(b) 分析时 LLM 收到
    一堆同义链当成独立证据。现在改成两趟：
      1) 先收集所有 (事件, 品种) 候选；
      2) 每个 (事件, 品种) 只留 MAX_CHAINS_PER_EVENT_ASSET 条，且互相不能是
         同一件事（链名二元组包含率 ≥0.72 视为同义），被砍的计入 suppressed。
    """
    chains = load_active_chains(conn)
    # ── 第一趟：收集候选（不写库）
    groups: dict[tuple, list] = {}
    for ch in chains:
        for ev in find_news_triggers(conn, ch, days):
            # v405 前视闸门：链是 10-04 才写下的，就不能拿它去"解释"10-03 的行情。
            if ch.created_ts and int(ev["ts"]) < ch.created_ts:
                continue
            groups.setdefault((ev["id"], ch.asset.upper()), []).append((ch, ev))
    # ── 第二趟：按 (事件, 品种) 收敛后写库
    inserted = 0
    suppressed = 0
    per_chain = {}
    for _key, cands in groups.items():
        # 排序：命中触发词越多（条件越具体）越优先，其次链名越长（越不泛）
        cands.sort(key=lambda t: (-len(t[1].get("matched_kws") or ()),
                                  -len(t[0].name or ""), str(t[0].chain_id)))
        picked: list = []
        fams: list = []
        for ch, ev in cands:
            fam = mechanism_family.family_of(ch.name or ch.cause)
            # 同一事件里"同一族"只留一条：讲的是同一件事，多留一条只是把
            # 同一份行情算两遍（XAU 那 4 条地缘链名字重合度只有 0.2，靠字符串
            # 去重根本挡不住）。
            if fam in fams and fam != "other":
                suppressed += 1
                continue
            if len(picked) >= MAX_CHAINS_PER_EVENT_ASSET:
                suppressed += 1
                continue
            picked.append((ch, ev))
            fams.append(fam)
        for ch, ev in picked:
            # v432：脉冲型链窗口短，截止也给短一点（3 天），让样本更快落地
            _hb = chain_horizon_bars(conn, ch.chain_id)
            deadline_ts = ev["ts"] + (3 if _hb <= _HORIZON_PULSE_BARS else 7) * 86400
            if insert_pending_outcome(conn, ch, ev, deadline_ts):
                inserted += 1
                per_chain[ch.chain_id] = per_chain.get(ch.chain_id, 0) + 1
    return {"chains_scanned": len(chains),
            "outcomes_inserted": inserted,
            "suppressed_same_event": suppressed,
            "lookback_days": days,
            "per_chain": per_chain}


def evaluate_pending(conn, force=False, max_age_days=None):
    """Evaluate PENDING outcomes whose eval_deadline has passed.

    v302+ 新增:
      force=True        → 忽略 deadline,评估所有 PENDING (用于测试 / 短期窗口信号)
      max_age_days=int  → 仅评估 trigger 在 N 天以内的 PENDING (防止误评远古噪音)
    """
    now = int(time.time())
    if force:
        if max_age_days is not None:
            min_ts = now - int(max_age_days * 86400)
            rows = conn.execute(
                """SELECT id, chain_id, trigger_ts, trigger_source, trigger_ref_id,
                          trigger_headline, trigger_keywords_matched, predicted_direction,
                          NULL, NULL, predicted_direction, predicted_mag_lo, predicted_mag_hi,
                          eval_deadline_ts
                   FROM causal_chain_outcomes
                   WHERE status='PENDING' AND trigger_ts>=?
                   ORDER BY trigger_ts ASC""",
                (min_ts,),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT id, chain_id, trigger_ts, trigger_source, trigger_ref_id,
                          trigger_headline, trigger_keywords_matched, predicted_direction,
                          NULL, NULL, predicted_direction, predicted_mag_lo, predicted_mag_hi,
                          eval_deadline_ts
                   FROM causal_chain_outcomes
                   WHERE status='PENDING'
                   ORDER BY trigger_ts ASC""",
            ).fetchall()
    else:
        rows = conn.execute(
            """SELECT id, chain_id, trigger_ts, trigger_source, trigger_ref_id,
                      trigger_headline, trigger_keywords_matched, predicted_direction,
                      NULL, NULL, predicted_direction, predicted_mag_lo, predicted_mag_hi,
                      eval_deadline_ts
               FROM causal_chain_outcomes
               WHERE status='PENDING' AND eval_deadline_ts<=?
               ORDER BY trigger_ts ASC""",
            (now,),
        ).fetchall()
    out = {"evaluated": 0, "hit": 0, "hit_weak": 0, "miss": 0, "neutral": 0, "expired": 0, "samples": [], "force_mode": force, "max_age_days": max_age_days}
    for r in rows:
        result = evaluate_one(conn, r, force=force)
        out["evaluated"] += 1
        st = result["status"].lower()
        if st in out:
            out[st] += 1
        if len(out["samples"]) < 5:
            out["samples"].append({
                "chain_id": r[1], "headline": (r[5] or "")[:60], "result": result,
            })
    return out


def get_feedback_summary(conn, days=TIME_HORIZON_LOOKBACK):
    start_ts = int(time.time()) - days * 86400
    chains = load_active_chains(conn)
    out_chains = []
    for ch in chains:
        outcomes = conn.execute(
            """SELECT trigger_ts, trigger_headline, status, actual_pct_5d,
                      direction_hit, magnitude_hit
               FROM causal_chain_outcomes
               WHERE chain_id=? AND trigger_ts>=?
               ORDER BY trigger_ts DESC""",
            (ch.chain_id, start_ts),
        ).fetchall()
        n = len(outcomes)
        # v302: evaluate_one 会产出 HIT_WEAK (方向对但幅度偏), 计入有效评估,
        # 否则这类样本会被当成"未评估"而虚高 MISS 比例.
        n_hit = sum(1 for o in outcomes if o[2] in ("HIT", "HIT_WEAK"))
        n_miss = sum(1 for o in outcomes if o[2] == "MISS")
        n_neu = sum(1 for o in outcomes if o[2] == "NEUTRAL")
        n_pend = sum(1 for o in outcomes if o[2] == "PENDING")
        n_eval = n_hit + n_miss + n_neu
        real_hit_rate = (n_hit / n_eval) if n_eval > 0 else None
        calib_gap = ((ch.confidence - real_hit_rate) if real_hit_rate is not None else None)
        out_chains.append({
            "chain_id": ch.chain_id, "asset": ch.asset,
            "expected_direction": ch.expected_direction,
            "magnitude_lo": ch.magnitude_lo, "magnitude_hi": ch.magnitude_hi,
            "stated_confidence": ch.confidence,
            "lifetime_hit": n_hit, "lifetime_miss": n_miss,
            "window_days": days,
            "outcomes_n": n, "outcomes_hit": n_hit, "outcomes_miss": n_miss,
            "outcomes_neutral": n_neu, "outcomes_pending": n_pend,
            "real_hit_rate": real_hit_rate,
            "calibration_gap": calib_gap,
            "needs_recalibration": (
                real_hit_rate is not None and abs(calib_gap) > 0.15 and n_eval >= 5
            ),
            "recent_outcomes": [
                {"ts": o[0],
                 "headline": (o[1] or "")[:80],
                 "status": o[2],
                 "pct_5d": o[3],
                 "direction_hit": (bool(o[4]) if o[4] is not None else None),
                 "magnitude_hit": (bool(o[5]) if o[5] is not None else None)}
                for o in outcomes[:8]
            ],
        })
    total_o = sum(c["outcomes_n"] for c in out_chains)
    total_hit = sum(c["outcomes_hit"] for c in out_chains)
    total_miss = sum(c["outcomes_miss"] for c in out_chains)
    total_neu = sum(c["outcomes_neutral"] for c in out_chains)
    total_pend = sum(c["outcomes_pending"] for c in out_chains)
    total_eval = total_hit + total_miss + total_neu
    return {
        "window_days": days,
        "chains": out_chains,
        "summary": {
            "n_chains": len(out_chains),
            "n_chains_with_outcomes": sum(1 for c in out_chains if c["outcomes_n"] > 0),
            "n_chains_needs_recalibration": sum(1 for c in out_chains if c["needs_recalibration"]),
            "total_outcomes": total_o,
            "total_hit": total_hit,
            "total_miss": total_miss,
            "total_neutral": total_neu,
            "total_pending": total_pend,
            "overall_hit_rate": (total_hit / total_eval) if total_eval > 0 else None,
        },
    }


def resnapshot_predictions(conn):
    """用链当前的 expected_direction / magnitude 刷新 outcome 的预测快照。

    v310 P1: outcomes.predicted_* 是"backfill 时从链文本推断的快照"。当初的
    设计意图是**只用 name 推断**（effect 字段含事后结论, 用它会数据泄漏）,
    但快照把**推断逻辑的版本**也一起冻住了: 后来 _infer_direction 的词典补全
    (例如"买盘/推升"等), 链表里的 expected_direction 已经更新, 快照却还是旧口径 ——
    于是评估拿一个"链现在根本不会说的话"去对市场, 命中率既不可信也不可复现。

    快照冻结的是**结论**, 不是推断逻辑: 同一个 name 在同一套词典下必然推出同一个
    方向, 所以重算快照不引入任何事后信息。这里只按链表当前的 expected_direction
    与 magnitude 覆盖快照, 不碰已评估行的结论(结论由 evaluate_one 重算)。

    v314 P1: 幅度带不再从链表的全局常量抄, 而是按**该资产在触发时点之前**的
    已实现波动重算 (PIT): 历史 outcome 必须用触发时点的口径, 用"今天"的
    波动率去评"三个月前"的链 = 把未来信息灌进历史评估。

    返回 {"updated": N, "bands": N}。
    """
    cur = conn.execute(
        """UPDATE causal_chain_outcomes
              SET predicted_direction = (
                      SELECT expected_direction FROM causal_chains
                       WHERE CAST(causal_chains.id AS TEXT) = causal_chain_outcomes.chain_id),
                  predicted_mag_lo = (
                      SELECT magnitude_lo FROM causal_chains
                       WHERE CAST(causal_chains.id AS TEXT) = causal_chain_outcomes.chain_id),
                  predicted_mag_hi = (
                      SELECT magnitude_hi FROM causal_chains
                       WHERE CAST(causal_chains.id AS TEXT) = causal_chain_outcomes.chain_id)
            WHERE EXISTS (SELECT 1 FROM causal_chains
                           WHERE CAST(causal_chains.id AS TEXT) = causal_chain_outcomes.chain_id
                             AND expected_direction IS NOT NULL
                             AND expected_direction <> causal_chain_outcomes.predicted_direction)""")
    updated = cur.rowcount or 0
    conn.commit()

    # 幅度带: 逐行按 (资产, 触发时点) 重算 (PIT)
    rows = conn.execute(
        "SELECT id, chain_id, trigger_ts FROM causal_chain_outcomes").fetchall()
    bands = 0
    for oid, cid, ts in rows:
        asset = _resolve_chain_asset(conn, cid)
        lo, hi = derive_magnitude_band(conn, asset, int(ts))
        cur = conn.execute(
            "UPDATE causal_chain_outcomes SET predicted_mag_lo=?, predicted_mag_hi=? WHERE id=?",
            (lo, hi, oid))
        bands += cur.rowcount or 0
    conn.commit()
    return {"updated": updated, "bands": bands}


def evaluate_all(conn, max_age_days=None):
    """重新评估**全部** outcome 行 (含已评估过的)。

    v310 P1: 取价锚点从"向前回看"改成"向后数"之后, 历史行必须重算一遍,
    否则表里留的还是旧口径的结果 (真命中率被系统性低估)。这是给
    `evaluate --recheck` 用的入口, 走的是和 evaluate_pending 同一个 evaluate_one。

    重算前先 resnapshot_predictions(): 快照冻结的是结论而不是推断逻辑版本,
    词典补全后必须按链当前的口径重算预测, 否则评估对的是"链不会说的话"。
    """
    now = int(time.time())
    resnap = resnapshot_predictions(conn)
    where, params = "", ()
    if max_age_days is not None:
        where = " WHERE trigger_ts>=?"
        params = (now - int(max_age_days * 86400),)
    rows = conn.execute(
        """SELECT id, chain_id, trigger_ts, trigger_source, trigger_ref_id,
                  trigger_headline, trigger_keywords_matched, predicted_direction,
                  NULL, NULL, predicted_direction, predicted_mag_lo, predicted_mag_hi,
                  eval_deadline_ts
           FROM causal_chain_outcomes""" + where + " ORDER BY trigger_ts ASC",
        params,
    ).fetchall()
    out = {"evaluated": 0, "hit": 0, "hit_weak": 0, "miss": 0, "neutral": 0,
           "expired": 0, "pending": 0, "samples": [], "force_mode": True,
           "resnapshotted": resnap.get("updated", 0),
           "max_age_days": max_age_days}
    for r in rows:
        result = evaluate_one(conn, r, force=True)
        out["evaluated"] += 1
        st = result["status"].lower()
        if st in out:
            out[st] += 1
        if len(out["samples"]) < 5:
            out["samples"].append({"chain_id": r[1], "headline": (r[5] or "")[:60],
                                   "result": result})
    return out


def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("cmd", choices=["backfill", "evaluate", "summary", "all"])
    p.add_argument("--days", type=int, default=TIME_HORIZON_LOOKBACK)
    p.add_argument("--recheck", action="store_true",
                   help="重算**全部** outcome (含已评估过的) —— 取价锚点修正后必须重跑一次")
    args = p.parse_args()
    conn = sqlite3.connect(DB_PATH)
    if args.cmd == "backfill":
        print(json.dumps(backfill(conn, args.days), indent=2, ensure_ascii=False))
    elif args.cmd == "evaluate":
        if args.recheck:
            print(json.dumps(evaluate_all(conn), indent=2, ensure_ascii=False))
        else:
            print(json.dumps(evaluate_pending(conn), indent=2, ensure_ascii=False))
    elif args.cmd == "summary":
        print(json.dumps(get_feedback_summary(conn, args.days), indent=2, ensure_ascii=False))
    else:
        print(">>> backfill")
        print(json.dumps(backfill(conn, args.days), indent=2, ensure_ascii=False))
        print(">>> evaluate")
        print(json.dumps(evaluate_pending(conn), indent=2, ensure_ascii=False))
        print(">>> summary (first 1500 chars)")
        s = json.dumps(get_feedback_summary(conn, args.days), indent=2, ensure_ascii=False)
        print(s[:1500] + ("..." if len(s) > 1500 else ""))
    conn.close()


if __name__ == "__main__":
    main()




