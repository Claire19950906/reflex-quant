"""v174: LLM 输出 JSON Schema 硬约束 + 价格幻觉校验

提供两个公共函数:
1) validate_ai_decision(result) -> (is_valid, errors)
2) check_price_fabrication(reasoning, snapshot) -> (rate, examples)
3) validate_and_label(result, snapshot) -> 一键校验 + verdict

v171 强化:
- data_anchor_ts 增加 ISO-8601 严格 pattern（拒绝 '上周五' / '今晚' 等模糊串）
- 新增 impact_curve（事件影响曲线 5m / 30m / 2h / 1d 估计）
- 新增 event_window_minutes（事件影响窗口分钟数，权威字段）
"""
import json
import re
from typing import Any, Dict, List, Optional, Tuple

# 严格 ISO-8601 pattern（带 / 不带时区）
_ISO8601_STRICT_PATTERN = (
    r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})"
    r"(Z|[+\-]\d{2}:?\d{2})?$"
)

# ── JSON Schema 定义 (依据 ai_worker.py L1160-1180 + ai_decisions 表结构) ──
AI_DECISION_SCHEMA: Dict[str, Any] = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "AI Decision Output",
    "type": "object",
    "required": [
        # v396 P3：asset_nature / factor_ranking / reasoning 三个"叙述字段"从 required 移除。
        # 实测 schema 失败明细：asset_nature 104 次 / factor_ranking 103 次 / reasoning 100 次，
        # 全是模型交了"空骨架"——但方向、标的、八段 reasoning_path 都在。
        # 这三项下游本来就有**非编造**的补全路径：
        #   asset_nature / factor_ranking → L2 业务知识表（真实词条，v200 P0+）
        #   reasoning                     → 从 reasoning_path 的 [总结] 段回填（v323 同法）
        # 让一个非核心字段把整条分析作废，等于用"格式"否掉"内容"。
        # 核心链路（reasoning_path 100 字 / 标的 / 动作 / 分数 / 影响）仍然强制。
        "reasoning_path", "impact_magnitude",
        # asset_reasoning 实测模型不产出（它是 reasoning 的备用来源），改可选：
        # 缺失时 presanitize 会从 [总结] 段回填 reasoning，不必因此作废整条分析。
        "sentiment_score", "suggested_action",  # v396 P3: reasoning 见上（可选，下游从 [总结] 回填）
        "market_category", "target_asset", "prediction_type", "event_phase",
        "market_confirmation", "expected_horizon", "invalidation_condition",
        "event_strength", "direct_catalyst", "heat_score", "impact_score",
        "asset_impact", "attention_score", "elasticity_window", "data_anchor_ts",
        # hard_data_evidence：快照缺数据时本来就写不出，模型也常留空。
        # 空字符串是"没有硬证据"的如实表达，不该让整条分析作废（改为可选）。
        # "hard_data_evidence" 已移出 required（字段定义保留）。
        # ── v323 修正 ──────────────────────────────────────────────────
        # 下面这批字段原本（v174/v181）被升级成"强制 required"，但实测模型侧
        # 从来不返回它们（MiniMax-M3 每次都是
        #   'asset_reasoning' / 'asset_classification' / 'fund_driver' is a required property
        #   → 一次校验 9 个错），结果是**每一篇新闻的分析都被判 schema 失败**：
        # 走兜底链 → 目标资产退化成 NONE → 整条进 unclassified_events（一天上万条）、
        # 一条决策都产不出来。这不是模型能力问题，是"尺子和产物对不上"：
        # required 里的字段模型根本不产出，下游又早已按空值处理（写 N/A）。
        # 处理原则：**不编造**。字段缺失就如实留空/N-A，由下游标 N/A，
        # 而不是让整条分析因为一个非核心字段缺失而作废。
        # 核心链路字段（reasoning/asset/target/action/score 等）仍然强制。
    ],
    "properties": {
        "reasoning_path": {"type": "string", "minLength": 100},
        "asset_nature": {"type": "string", "minLength": 4, "maxLength": 200},
        "factor_ranking": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["factor", "rank", "direction"],
                "properties": {
                    "factor": {"type": "string", "minLength": 2},
                    "rank": {"type": "integer", "minimum": 1},
                    "direction": {"type": "string", "enum": ["利多", "利空", "中性"]},
                },
            },
            "minItems": 1,
        },
        "impact_magnitude": {"type": "number", "minimum": 0, "maximum": 100},
        "asset_reasoning": {"type": "string", "minLength": 4},
        # v323：prompt 约定 0-100（50 中性），schema 却卡 -1~1 → 模型给 67 被判非法。
        # 放宽到 -100~100，下游 _normalize_sentiment_score 三档自适应，不受影响。
        "sentiment_score": {"type": "number", "minimum": -100.0, "maximum": 100.0},
        "suggested_action": {"type": "string", "enum": ["BUY", "SELL", "HOLD"]},
        "reasoning": {"type": "string", "minLength": 4, "maxLength": 200},
        "market_category": {
            "type": "string",
            # v1 重构：移除 OTHER。其它值（白名单 6 大类 + 任何非白名单值）
            # 都会被 ai_worker 的 market_classifier.validate_pair() 拒绝并写入
            # unclassified_events 表（人工复核 / 规则补全队列）。
            "enum": ["CRYPTO", "GOLD", "OIL", "STOCK", "MACRO", "COMMODITY", ""],
            # v396："" 合法 —— target_asset=NONE（非核心监控品种）时分类客观上不存在。
            # 实测这一格逼出上百次"schema 校验失败 → 全量 retry"，每次多烧一次大模型调用；
            # 而空串本身在 _sanitize 里已被刻意写成"待复核"标记并进 unclassified_events，
            # 是如实的"没有分类"，不是缺数据。允许空串 ≠ 放松内容校验。
        },
        "target_asset": {
            "type": "string",
            # 与 market_classifier.TARGET_TO_MARKET 同步；
            # 新增资产必须**同时**更新：1) 此 schema 2) TARGET_TO_MARKET 3) _ASSET_ALIASES 4) prompt 的"## 3. 资产归类"
            "enum": [
                "BTC", "ETH", "SOL",
                "XAU", "XAG", "COPPER",
                "WTI", "BRENT",
                "CN_STOCK", "US_STOCK", "HK_STOCK", "INDIVIDUAL_STOCK",
                "SKHYNIX", "NVDA", "TSLA", "AAPL", "MSFT", "AMZN", "GOOGL",
                "META", "COIN", "PLTR", "MSTR", "SMCI", "MARA", "RIOT",
                # v211 P1-C：扩半导体产业链 ticker（与海力士同行业新闻常被同时提及）
                "MU", "INTC", "TSMC", "SAMSUNG",
                "CNY", "USD", "EUR", "JPY", "US_BOND", "CN_BOND",
                "NONE",
            ],
        },
        "prediction_type": {"type": "string", "enum": ["reversal", "continuation", "breakout"]},
        "event_phase": {"type": "string", "enum": ["early", "mid", "late"]},
        "market_confirmation": {"type": "string", "enum": ["positive", "negative", "unknown"]},
        "expected_horizon": {"type": "string", "enum": ["intraday", "1-3d", "1w+"]},
        "invalidation_condition": {"type": "string", "minLength": 4, "maxLength": 100},
        "event_strength": {"type": "string", "enum": ["low", "medium", "high"]},
        "direct_catalyst": {"type": "boolean"},
        "heat_score": {"type": "number", "minimum": 0, "maximum": 10},
        "impact_score": {"type": "number", "minimum": 0, "maximum": 10},
        "asset_impact": {"type": "number", "minimum": 0, "maximum": 10},
        "attention_score": {"type": "number", "minimum": 0, "maximum": 100},
        # v343: evidence_confidence —— 选股 SQL 拿它当硬闸门
        #   (COALESCE(evidence_confidence,0) >= EVIDENCE_CONFIDENCE_GATE, 实测阈值 20)，
        #   但 schema 里原本**根本没有这个字段**：模型没有任何理由输出它，
        #   于是 327 条决策里 296 条是默认值 0，闸门只放行 2 条（0.6%）。
        #   这里补成**可选**属性（不进 required，避免重演 v323 那种
        #   "必填字段模型不产出 → 整条分析判失败"的事故），并明确 0-100 口径。
        #   约定：没有可核验硬证据时**不要填**（宁可缺失，也不要凭空给分）。
        "evidence_confidence": {
            "type": "number", "minimum": 0, "maximum": 100,
            "description": (
                "证据置信度 0-100（≥阈值才允许下单）。仅当新闻含可核验的硬证据"
                "（官方数据、交易所公告、财报数字等）时给出；没有硬证据请省略该字段，不要猜。"
            ),
        },
        "elasticity_window": {
            "type": "object",
            "required": ["minutes", "decay", "peak_after_minutes"],
            "properties": {
                "minutes": {"type": "number", "minimum": 0},
                "decay": {"type": "string", "enum": ["fast", "medium", "slow"]},
                "peak_after_minutes": {"type": "number", "minimum": 0},
            },
        },
        "data_anchor_ts": {
            "type": "string",
            "minLength": 3,
            # v171 强化: ISO-8601 严格模式 或 N/A；拒绝 '上周五' / '今晚' / '刚刚'
            # 三态: 'YYYY-MM-DD HH:MM:SS' / 'YYYY-MM-DDTHH:MM:SSZ' / 'N/A'
            "pattern": r"^(N/A|\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(Z|[+\-]\d{2}:?\d{2})?)$",
        },
        "hard_data_evidence": {"type": "string", "minLength": 4},
        # v171 新增: 事件影响曲线（5m / 30m / 2h / 1d 各窗口预期 delta_pct）
        "impact_curve": {
            "type": "object",
            "description": "事件影响曲线: 各窗口预期价格变化百分比（LLM 先验）",
            "properties": {
                "5m":  {"type": ["number", "string", "null"], "description": "5 分钟窗口 delta_pct，N/A 写 null"},
                "30m": {"type": ["number", "string", "null"]},
                "2h":  {"type": ["number", "string", "null"]},
                "1d":  {"type": ["number", "string", "null"]},
            },
            # 至少填一个窗口，否则 LLM 没认真看
            "minProperties": 1,
        },
        # v171 新增: 事件影响窗口分钟数（与 elasticity_window.minutes 互为冗余/校验）
        "event_window_minutes": {
            "type": ["integer", "string"],
            "minimum": 0,
            "maximum": 4320,  # 最多 3 天
            "description": "事件影响窗口分钟数；N/A 写 'N/A'",
        },
        # v174 新增三个字段
        "event_type_code": {
            "type": "string",
            "minLength": 3,
            # 从 dim_event_type 维表 25 个 code 之一 (允许不在表内但警告)
        },
        "verification": {
            "type": "string",
            "minLength": 4,
            "maxLength": 500,
            # 例: "pass | 一致" / "warn | factor_ranking 写利空但 suggested_action=BUY 矛盾"
        },
        "snapshot_proximity": {
            # LLM 自报: 引用的快照时间距离事件发生时间多少分钟
            # 数字 (>=0) 或 N/A; v174 让 LLM 意识到 "别拿陈旧快照当依据"
            "type": ["number", "string"],
        },
        # v181 结构化产出（准优先）：模型亲填的判断项
        "asset_classification": {
            "type": "string",
            "enum": ["避险/抗通胀", "供给驱动", "高Beta", "宏观锚", "政策敏感"],
        },
        "fund_driver": {
            "type": "string",
            "minLength": 2,
            "maxLength": 80,
        },
        "consensus_vs_us": {
            "type": "string",
            "enum": ["一致", "反共识"],
        },
    },
    "additionalProperties": True,
}


# ── v396 P3：可选叙述字段的"空值"归一化 ────────────────────────────
# 模型常把这些键**带着空值**一起吐出来（"asset_nature": "" / "factor_ranking": []）。
# 只把它们从 required 里删掉没用——键存在、值为空，minLength / minItems 照样报错，
# 整条分析照样作废（实测 asset_nature 104 / factor_ranking 103 / reasoning 100 次）。
#
# 处理：校验期间**临时摘掉**这些空值，让 schema 只看"内容是否成立"；
# 校验完**原样还原**（写回空串/空数组），这样下游的 missing_fields / completeness
# 依然如实记着"这几项没展开"——既不作废内容，也不掩盖缺口。
_EMPTY_NARRATIVE_FIELDS = (
    "asset_nature", "factor_ranking", "reasoning",
    "hard_data_evidence", "asset_reasoning", "fund_driver",
    "consensus_vs_us", "asset_classification",
)

# v396 追加：模型用"没有数据"的口语说法填字段（"N/A" / "不适用" / "-"）。
# 这些值的语义就是"空"，但长度过短会撞 minLength（如 hard_data_evidence 'N/A' 只有 3 字，
# 实测一轮 7 次）。同样在处理时当作空值摘除、校验后原样还原 —— 不美化、不替换成编的内容。
_EMPTY_EQUIVALENTS = {
    "hard_data_evidence": ("", "n/a", "na", "none", "null", "-", "不适用", "无", "暂无"),
    "asset_nature": ("", "n/a", "na", "none", "null", "-", "不适用", "无", "暂无"),
    "asset_classification": ("", "n/a", "na", "none", "null", "-", "不适用", "无", "暂无"),
    "consensus_vs_us": ("", "n/a", "na", "none", "null", "-", "不适用", "无", "暂无"),
}


def _is_empty_equivalent(key: str, value: Any) -> bool:
    """该字段的该值是否等价于"空"（语义上的没有，而不是内容不够长）。"""
    if value in ("", [], None):
        return True
    cands = _EMPTY_EQUIVALENTS.get(key)
    if not cands:
        return False
    if isinstance(value, str):
        return value.strip().lower() in cands
    return False


def _infer_class_from_asset(result: Dict[str, Any]) -> str:
    """按已确定的标的/品类推出 asset_classification（只在文本匹配不到时用）。

    口径来自 _SYSTEM_PROMPT 的资产归类段：
      避险/抗通胀 = 贵金属与贵金属 ETF；供给驱动 = 能源/工业金属；
      高Beta = 加密与权益；宏观锚 = 利率/汇率/国债；政策敏感 = 政策类标的。
    拿不准（无标的或无品类）返回 "" —— 宁可走补全，不瞎填。
    """
    try:
        _ta = str(result.get("target_asset") or "").strip().upper()
        _mc = str(result.get("market_category") or "").strip().upper()
    except Exception:
        return ""
    _BY_ASSET = {
        "XAU": "避险/抗通胀", "XAG": "避险/抗通胀",
        "WTI": "供给驱动", "BRENT": "供给驱动", "COPPER": "供给驱动",
        "BTC": "高Beta", "ETH": "高Beta", "SOL": "高Beta",
        "US_STOCK": "高Beta", "INDIVIDUAL_STOCK": "高Beta", "SKHYNIX": "高Beta",
        "US_BOND": "宏观锚", "CN_BOND": "宏观锚",
        "USD": "宏观锚", "CNY": "宏观锚", "EUR": "宏观锚", "JPY": "宏观锚",
    }
    if _ta in _BY_ASSET:
        return _BY_ASSET[_ta]
    _BY_CAT = {
        "GOLD": "避险/抗通胀", "OIL": "供给驱动", "COMMODITY": "供给驱动",
        "CRYPTO": "高Beta", "STOCK": "高Beta", "MACRO": "宏观锚",
    }
    return _BY_CAT.get(_mc, "")


def coerce_enum_fields(result: Dict[str, Any]) -> int:
    """v416：校验前把两个"枚举型"字段的常见误返**归一**，省掉一次补全调用。

    依据（2026-11-17 实测日志）：
      [V174-SCHEMA] JSON Schema 校验失败 (3 错) |
        [asset_classification] {'primary':'贵金属','instrument':'XAU',...} is not of type 'string'
        [consensus_vs_us] '与既定 SELL 判断一致；偏鹰通胀表态强化…' is not one of ['一致','反共识']
    模型没按「5 选 1 / 一致|反共识」给值，把两个字段当自由文本写：
      · asset_classification 常被写成**对象**（把分类拆成 primary/instrument/key_drivers）；
      · consensus_vs_us 常被写成**一整句话**（里面其实含"一致"/"反共识"）。
    而 schema 是严格枚举 → 整条校验失败 → 触发「schema 精简补全」再调一次模型。
    实测今日 schema_fill_lean 105 次 > 主调用 102 次，**每条分析都在多付一次调用**。

    这里只做"把已有信息塞回枚举"，不改语义：
      · 对象 → 拼成文本再匹配枚举（匹配不到就不动，交给补全流程）；
      · 长句 → 按关键词匹配（先判"反共识/不一致/背离"，再判"一致"）。
    返回成功归一的字段数（0 表示没动）。
    """
    if not isinstance(result, dict):
        return 0
    fixed = 0

    _CLASS_ENUM = ("避险/抗通胀", "供给驱动", "高Beta", "宏观锚", "政策敏感")

    # 同义词表：模型常写"贵金属/非生息资产/避险需求"这类描述，而不是枚举原词。
    # 只放**指向明确**的词；拿不准的宁可不归一（交给补全流程，别把分类改错）。
    _CLASS_SYNONYMS = (
        ("避险/抗通胀", ("避险", "抗通胀", "非生息", "无息资产", "硬资产", "实际利率")),
        ("供给驱动", ("供给", "产能", "库存", "减产", "OPEC", "出清")),
        ("高Beta", ("高Beta", "高beta", "高波动", "风险资产", "成长股")),
        ("宏观锚", ("宏观锚", "流动性", "美元指数", "长端利率", "估值锚")),
        ("政策敏感", ("政策敏感", "监管", "补贴", "关税", "央行政策")),
    )

    def _pick_enum(text: str, enum: tuple) -> str:
        low = str(text or "")
        for e in enum:
            if e and e in low:
                return e
        return ""

    # ── asset_classification：对象/长句 → 5 选 1 ──
    _ac = result.get("asset_classification")
    if _ac is not None and _ac not in ("", [], None):
        if not isinstance(_ac, str):
            parts: List[str] = []
            if isinstance(_ac, dict):
                for v in _ac.values():
                    if isinstance(v, (list, tuple)):
                        parts.extend(str(x) for x in v)
                    elif v is not None:
                        parts.append(str(v))
            elif isinstance(_ac, (list, tuple)):
                parts = [str(x) for x in _ac]
            else:
                parts = [str(_ac)]
            _ac_text = " ".join(parts)
        else:
            _ac_text = _ac
        _hit = _pick_enum(_ac_text, _CLASS_ENUM)
        if not _hit:
            # 没有枚举原词时走同义词表（按命中次数取最匹配的那个）
            _score = {}
            for _e, _kws in _CLASS_SYNONYMS:
                _c = sum(1 for _k in _kws if _k and _k in _ac_text)
                if _c:
                    _score[_e] = _c
            if _score:
                _hit = max(_score, key=_score.get)
        if _hit and _hit != _ac:
            result["asset_classification"] = _hit
            fixed += 1
        elif not _hit:
            # v423 兜底：同义词一个都没命中（模型写"能源类大宗商品／原油价格基准资产…"
            # 这种纯描述句）时，按**已确定的标的/品类**直接归类。
            # 为什么不继续交给补全流程：实测今天 225/440 条分析栽在这两个枚举上，
            # 一半的分析在付第二次调用；而"WTI 用供给驱动、BTC 用高Beta"是这台
            # 系统的既有口径（见 _SYSTEM_PROMPT 的资产归类段），这里只是把它落成代码。
            _infer = _infer_class_from_asset(result)
            if _infer:
                result["asset_classification"] = _infer
                fixed += 1

    # ── consensus_vs_us：长句 → 一致|反共识 ──
    # 实测长句里常常**不含**"一致"二字，而是"认同/成立/符合"这类表述，
    # 例如："本次分析认同新闻所给核心影响……" / "本分析认为该判断方向上成立……"。
    # 这些语义上就是"与共识同向"，归成"一致"；只要出现反向词一律先判"反共识"。
    _cv = result.get("consensus_vs_us")
    # v432（2026-11-17）：模型偶尔把这一格写成对象
    # {consensus / our_view / difference}，schema 要求字符串 → 整条被判失败、
    # 反复 persist_fix 重调（实测 6 条卡在 PROCESSING、DONE 40 分钟不涨）。
    # 这里先拼成一句话，再走下面的"一致/反共识"归一化，不额外烧调用。
    if isinstance(_cv, dict):
        _cv = "；".join(str(v) for v in _cv.values() if v)
        result["consensus_vs_us"] = _cv
        fixed += 1
    if isinstance(_cv, str) and _cv.strip() not in ("", "一致", "反共识"):
        _low = _cv
        # 先判"反向"，避免"与共识不一致"里的"一致"被误命中
        if any(k in _low for k in ("反共识", "不一致", "背离", "相反", "违背", "不同")):
            result["consensus_vs_us"] = "反共识"
            fixed += 1
        elif any(k in _low for k in ("一致", "相符", "吻合", "趋同", "认同", "成立", "符合", "印证")):
            result["consensus_vs_us"] = "一致"
            fixed += 1
        else:
            # v423 兜底：整句里连"一致/反共识"的语义词都没有（例："澳洲联储当前表态
            # 相较其最新通胀预测偏鹰……"）。schema 只有两个值，必须给一个：
            # 反向词已在上面单独判过 → 走到这里按"一致"落库，并在 reasoning 里
            # 保留模型原句，不篡改判断内容。
            #
            # 但纯短词/口语表示"判不了"（看不清/未知/不明/无法判断…）时不兜底 ——
            # 这类值不是方向判断，硬编成"一致"是把不确定写成确定；字段本就可选，
            # 交给校验空值摘除与补全流程拿真值。
            if len(_low) <= 6 and any(k in _low for k in ("看不清", "未知", "不明", "无法判断", "不确定", "待定", "暂无")):
                pass
            else:
                result["consensus_vs_us"] = "一致"
                fixed += 1

    # ── fund_driver：超过 80 字 → 截断 ──
    # schema 约束 maxLength=80，模型偶尔写成一整段（实测 4 次）。
    # 这里截断而不是让整条校验失败去重调模型 —— 它只是"一句话总结"，
    # 截掉尾巴远好过再烧一次调用。
    _fd = result.get("fund_driver")
    if isinstance(_fd, str) and len(_fd) > 80:
        result["fund_driver"] = _fd[:80]
        fixed += 1

    return fixed


def validate_ai_decision(
    result: Dict[str, Any],
    *,
    apply_fallback: bool = True,
) -> Tuple[bool, List[str]]:
    """v396 P3 包装层：允许叙述字段留空（空 ≠ 错），空值摘除后校验、校验后还原。"""
    if not isinstance(result, dict):
        return _validate_ai_decision_impl(result, apply_fallback=apply_fallback)

    # v416：先把枚举字段的常见误返归一，避免"类型错"白白触发一次补全调用
    try:
        coerce_enum_fields(result)
    except Exception:
        pass

    _stash: List[Tuple[str, Any]] = []
    if "_empty_narrative_fields" not in result:
        result["_empty_narrative_fields"] = []
    for _k in _EMPTY_NARRATIVE_FIELDS:
        if _k in result and _is_empty_equivalent(_k, result[_k]):
            _stash.append((_k, result[_k]))
            if _k not in result["_empty_narrative_fields"]:
                result["_empty_narrative_fields"].append(_k)
            result.pop(_k, None)
    try:
        return _validate_ai_decision_impl(result, apply_fallback=apply_fallback)
    finally:
        for _k, _v in _stash:
            result[_k] = _v


def _validate_ai_decision_impl(
    result: Dict[str, Any],
    *,
    apply_fallback: bool = True,
) -> Tuple[bool, List[str]]:
    """用 JSON Schema 校验 LLM 输出。失败返回 (False, [error...])。

    v171+ 新增 apply_fallback 参数：
        - True（默认）：失败时尝试自动 fallback（把 data_anchor_ts 模糊文本 → ISO 当前时间，
          impact_curve 缺省 → {}，event_window_minutes 缺省 → 60），标记 _fallback_used=True，
          再次校验；若仍然失败，返回 (False, errors)。
        - False：保持原行为，失败即返回 (False, errors)。
    """
    if not isinstance(result, dict):
        return False, [f"根类型必须是 object, 实际是 {type(result).__name__}"]

    try:
        from jsonschema import Draft7Validator
    except ImportError:
        return True, ["[WARN] jsonschema 未安装, 跳过 schema 校验"]

    errors: List[str] = []
    validator = Draft7Validator(AI_DECISION_SCHEMA)
    for err in validator.iter_errors(result):
        path = ".".join(str(p) for p in err.absolute_path) if err.absolute_path else "(root)"
        msg = err.message
        if len(msg) > 200:
            msg = msg[:200] + "..."
        errors.append(f"[{path}] {msg}")

    if not errors:
        return True, []

    if not apply_fallback:
        return False, errors

    # ── v171+ fallback 路径：尝试救活常见可补字段 ─────────────────────
    fallback_used = False
    # 1) data_anchor_ts 模糊 → 当前 UTC ISO
    ts_val = result.get("data_anchor_ts")
    if isinstance(ts_val, str) and ts_val.strip() not in ("", "N/A", "AMBIGUOUS"):
        # 模糊文本（如 "上周五" / "今晚"）→ fallback 到 ISO 当前时间
        try:
            from tz_utils import validate_iso8601
            chk = validate_iso8601(ts_val)
            if not chk.get("ok"):
                from clock import safe_now
                from datetime import datetime as _dt, timezone as _tz
                now_epoch = safe_now()
                result["data_anchor_ts"] = _dt.fromtimestamp(
                    int(now_epoch), tz=_tz.utc
                ).strftime("%Y-%m-%dT%H:%M:%SZ")
                fallback_used = True
        except Exception:
            pass

    # 2) impact_curve 缺省 / 空 / 不合法 → 全 0 占位（schema minProperties=1 要求至少 1 键）
    ic_val = result.get("impact_curve")
    if not isinstance(ic_val, dict) or not ic_val:
        result["impact_curve"] = {"5m": 0.0, "30m": 0.0, "2h": 0.0, "1d": 0.0}
        fallback_used = True

    # 3) event_window_minutes 缺省/越界 → 60 (1h)
    ewm = result.get("event_window_minutes")
    if not isinstance(ewm, int) or isinstance(ewm, bool) or ewm < 0 or ewm > 4320:
        if ewm != "N/A":
            result["event_window_minutes"] = 60
            fallback_used = True

    if fallback_used:
        result["_fallback_used"] = True
        # 重新校验
        errors2: List[str] = []
        validator2 = Draft7Validator(AI_DECISION_SCHEMA)
        for err in validator2.iter_errors(result):
            path = ".".join(str(p) for p in err.absolute_path) if err.absolute_path else "(root)"
            msg = err.message
            if len(msg) > 200:
                msg = msg[:200] + "..."
            errors2.append(f"[{path}] {msg}")
        if not errors2:
            return True, []
        return False, errors2

    return False, errors


def validate_time_fields(result: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """v171 后置强校验：data_anchor_ts 必须严格 ISO-8601 或 N/A。

    JSON Schema pattern 已能挡掉大部分模糊时间，但跨 schema 版本演进时
    （jsonschema 缺失 / 老库 schema 缺失）需要兜底。

    Returns:
        (ok, errors)
    """
    errors: List[str] = []
    if not isinstance(result, dict):
        return True, []
    val = result.get("data_anchor_ts")
    if val is None or (isinstance(val, str) and val.strip() in ("", "N/A")):
        return True, []
    if not isinstance(val, str):
        errors.append(f"[data_anchor_ts] 必须是 string, 实际 {type(val).__name__}")
        return False, errors
    if not re.match(_ISO8601_STRICT_PATTERN, val.strip()):
        errors.append(
            f"[data_anchor_ts] 不是 ISO-8601 严格格式: {val[:30]!r} "
            "(应为 'YYYY-MM-DD HH:MM:SS' / 'YYYY-MM-DDTHH:MM:SSZ' 或 'N/A')"
        )
        return False, errors
    return True, []


def validate_window_minutes(result: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """v171 后置校验：event_window_minutes 必须在 [0, 4320] 范围内（或 N/A）。"""
    errors: List[str] = []
    val = result.get("event_window_minutes")
    if val is None:
        return True, []
    if isinstance(val, str):
        if val.strip() in ("", "N/A"):
            return True, []
        errors.append(f"[event_window_minutes] 字符串值仅支持 'N/A': {val!r}")
        return False, errors
    if isinstance(val, bool):  # bool 是 int 子类，先排除
        errors.append("[event_window_minutes] bool 类型不合法")
        return False, errors
    if isinstance(val, (int, float)):
        if val < 0 or val > 4320:
            errors.append(f"[event_window_minutes] 超出范围 [0, 4320]: {val}")
            return False, errors
        return True, []
    errors.append(f"[event_window_minutes] 类型不合法: {type(val).__name__}")
    return False, errors


# ── 价格幻觉校验 ────────────────────────────────────────────────────
_NUM_RE = re.compile(r"(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)")


def _extract_numbers(text: str) -> List[float]:
    """从 reasoning 抽所有数字 (整数 + 小数), 忽略千分位逗号。"""
    if not text:
        return []
    out: List[float] = []
    for m in _NUM_RE.finditer(text):
        raw = m.group(1).replace(",", "")
        try:
            out.append(float(raw))
        except (ValueError, TypeError):
            pass
    return out


def _load_snapshot_prices(snapshot: Any) -> Dict[str, List[float]]:
    """从 structured_input.snapshot 抽所有价格字典 → {asset: [price...]}"""
    if isinstance(snapshot, str):
        try:
            snapshot = json.loads(snapshot)
        except Exception:
            return {}
    if not isinstance(snapshot, dict):
        return {}
    snap_list = snapshot.get("snapshot") if isinstance(snapshot.get("snapshot"), list) else None
    if not snap_list:
        snap_list = [snapshot]
    prices: Dict[str, List[float]] = {}
    for entry in snap_list:
        if not isinstance(entry, dict):
            continue
        asset = str(entry.get("asset") or entry.get("symbol") or entry.get("code") or "").upper()
        if not asset:
            continue
        for k in ("snap_price", "price_at_news_ts", "current_price", "price", "open", "close"):
            v = entry.get(k)
            if isinstance(v, (int, float)) and 100 < v < 10000000:
                prices.setdefault(asset, []).append(float(v))
    return prices


def check_price_fabrication(
    reasoning: str,
    snapshot: Any,
    tolerance_pct: float = 1.0,
) -> Tuple[float, List[str]]:
    """检查 reasoning 中的价格数字是否在 snapshot 合理范围内 (±tolerance_pct)。

    Returns: (fabrication_rate, fabricated_examples)
    """
    if not reasoning:
        return 0.0, []
    snap_prices = _load_snapshot_prices(snapshot)
    if not snap_prices:
        return 0.0, ["[INFO] snapshot 为空, 跳过价格幻觉校验"]
    all_prices: List[float] = []
    for ps in snap_prices.values():
        all_prices.extend(ps)
    text = reasoning
    asset_positions = []
    for asset in snap_prices:
        for m in re.finditer(r"\b" + re.escape(asset) + r"\b", text, re.IGNORECASE):
            asset_positions.append((m.start(), m.end(), asset))

    def _nearest_asset(pos: int) -> Optional[str]:
        if not asset_positions:
            return None
        best = None
        best_d = 9999
        for start, end, asset in asset_positions:
            d = min(abs(pos - start), abs(pos - end))
            if d < best_d:
                best = asset
                best_d = d
        return best if best_d < 60 else None

    def _is_fabricated(num: float, near_asset: Optional[str]) -> bool:
        if num < 100 or num > 1e7:
            return False
        if near_asset and near_asset in snap_prices and snap_prices[near_asset]:
            ref = snap_prices[near_asset][0]
            delta = abs(num - ref) / ref * 100.0 if ref > 0 else 999
            return delta > tolerance_pct
        if all_prices:
            closest = min(all_prices, key=lambda p: abs(p - num))
            delta = abs(num - closest) / closest * 100.0 if closest > 0 else 999
            return delta > tolerance_pct
        return False

    fabricated_count = 0
    total_count = 0
    examples: List[str] = []
    for m in _NUM_RE.finditer(text):
        raw = m.group(1).replace(",", "")
        try:
            num = float(raw)
        except (ValueError, TypeError):
            continue
        if num < 100 or num > 1e7:
            continue
        total_count += 1
        near_asset = _nearest_asset(m.start())
        if _is_fabricated(num, near_asset):
            fabricated_count += 1
            snippet = text[max(0, m.start() - 15): min(len(text), m.end() + 15)].replace("\n", " ")
            examples.append(f"数字={num} asset={near_asset or '(无标识)'} 上下文: ...{snippet}...")
    rate = (fabricated_count / total_count) if total_count > 0 else 0.0
    return rate, examples[:5]


def validate_and_label(
    result: Dict[str, Any],
    snapshot: Any = None,
    fabrication_tolerance: float = 0.3,
) -> Dict[str, Any]:
    """一键校验 + 贴标签。

    Returns: {
      schema_valid, schema_errors, fabrication_rate, fabrication_examples,
      verdict ("pass"|"fabricated"|"schema_invalid"|"warn"|"fabricated_soft"), retry_hint
    }
    """
    ok, schema_errors = validate_ai_decision(result)
    fab_rate, fab_examples = (0.0, [])
    if snapshot:
        fab_rate, fab_examples = check_price_fabrication(
            result.get("reasoning", "") or "", snapshot, tolerance_pct=fabrication_tolerance
        )
    if not ok:
        verdict = "schema_invalid"
        retry_hint = (
            "JSON Schema 校验失败, 请严格按以下修正:\n"
            + "\n".join(f"- {e}" for e in schema_errors[:10])
            + "\n所有 enum 字段(BUY/SELL/HOLD, market_category 等)必须从允许值中选取。"
        )
    elif fab_rate >= 0.5:
        verdict = "fabricated"
        retry_hint = (
            f"价格幻觉检出 {fab_rate*100:.0f}% 数字无法对位 structured_input.snapshot。"
            "请只引用 snapshot 中实际存在的时间戳+价格组合;对于'目前 BTC 在 67000'这类断言,"
            "必须能在 hard_data_evidence 写出对应时间的具体数值, 否则改写 N/A。"
        )
    elif fab_rate >= 0.3:
        # v176：软边界 — 不强制 HOLD，仅写 WARN（避免假阴性）
        verdict = "fabricated_soft"
        retry_hint = (
            f"价格幻觉软边界 {fab_rate*100:.0f}% (30-50%): 不强制 HOLD，但请在 reasoning 里"
            "标明哪些数字是 N/A，避免引用未在 snapshot 出现的价格。"
        )
    elif fab_rate >= 0.15:
        verdict = "warn"
        retry_hint = (
            f"价格对位轻微偏差 {fab_rate*100:.0f}%：下次推理时核对 snapshot 时间戳。"
        )
    else:
        verdict = "pass"
        retry_hint = ""
    return {
        "schema_valid": ok,
        "schema_errors": schema_errors,
        "fabrication_rate": fab_rate,
        "fabrication_examples": fab_examples,
        "verdict": verdict,
        "retry_hint": retry_hint,
    }