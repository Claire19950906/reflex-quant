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
        "reasoning_path", "asset_nature", "factor_ranking", "impact_magnitude",
        "asset_reasoning", "sentiment_score", "suggested_action", "reasoning",
        "market_category", "target_asset", "prediction_type", "event_phase",
        "market_confirmation", "expected_horizon", "invalidation_condition",
        "event_strength", "direct_catalyst", "heat_score", "impact_score",
        "asset_impact", "attention_score", "elasticity_window", "data_anchor_ts",
        "hard_data_evidence",
        # v174 强化: 三个字段之前 LLM 0% 填, 升级为强制
        "event_type_code",  # 从 dim_event_type 25 个 code 选 1
        "verification",      # 跨字段一致性自检结果, 防止 [逻辑自检] 与结论矛盾
        "snapshot_proximity", # reasoning 引用的快照时间距事件时间差 (分钟), N/A 写 999
        # v181 结构化产出（准优先）：3 个判断项由模型亲填（enum / 短字符串）
        "asset_classification",  # 资产最主要属性，5 选 1
        "fund_driver",           # 驱动力一句话总结 <=80 字
        "consensus_vs_us",       # 结论与市场共识关系：一致 | 反共识
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
        "sentiment_score": {"type": "number", "minimum": -1.0, "maximum": 1.0},
        "suggested_action": {"type": "string", "enum": ["BUY", "SELL", "HOLD"]},
        "reasoning": {"type": "string", "minLength": 4, "maxLength": 200},
        "market_category": {
            "type": "string",
            # v1 重构：移除 OTHER。其它值（白名单 6 大类 + 任何非白名单值）
            # 都会被 ai_worker 的 market_classifier.validate_pair() 拒绝并写入
            # unclassified_events 表（人工复核 / 规则补全队列）。
            "enum": ["CRYPTO", "GOLD", "OIL", "STOCK", "MACRO", "COMMODITY"],
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


def validate_ai_decision(
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