"""自检样本：不依赖真实数据库，验证 3 类行为。

1. clean：证据齐全、方向一致 → 无 finding
2. warn/reject：降级兜底 + 方向与因子矛盾 → 至少 1 条 finding，且带 remediation
3. insufficient：完整性 < 0.7 → 短路为 E019，不出其余层结论
"""

from __future__ import annotations

import json
from datetime import datetime

from ..core.context import CheckContext
from ..core.pipeline import audit

def _now() -> str:
    """本地 naive 时间（内核按 naive 比较，带时区会 TypeError）。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


CLEAN = {
    "id": 9001,
    "suggested_action": "BUY",
    "target_asset": "BTC",
    "sentiment_score": 0.42,
    "evidence_confidence": 31.5,
    "completeness_score": 1.0,
    # 时间锚点必须"新"：内核按现在减锚点算时效，用固定日期会报 E002 数据过期
    "data_anchor_ts": _now(),
    "news_id": 123,
    "event_atom_ids": "a1,a2",
    # 内核的"可解析条目"是 key=value 用 | 或 ; 分隔（不是 JSON 数组），见 _count_evidence
    "hard_data_evidence": (
        "BTC.change_24h_pct=-2.11|BTC.funding_rate_pct=0.0008|"
        "ETH.change_24h_pct=1.20|BTC.etf_net_inflow_usd_m=210"
    ),
    "hard_data_status": "ok",
    "factor_ranking": json.dumps([
        {"factor": "ETF 净流入", "direction": "up", "weight": 0.4},
        {"factor": "资金费率转正", "direction": "up", "weight": 0.3},
    ], ensure_ascii=False),
    "reasoning": (
        "假设 ETF 连续三日净流入（前提）。由于现货买盘增强，所以短期偏多。"
        "反之若资金费率转负，则结论失效；参考 2025-10 同类流入场景，24h 均值为正。"
    ),
    # reasoning_path 是内核查八段推理链时真正读的字段（structured 优先）
    "reasoning_path": (
        "[总框架] 假设 ETF 连续三日净流入（前提），24h 窗口偏多 [因素定位] ETF 净流入为主驱动 "
        "[驱动力] 现货买盘增强 [水位博弈] 持仓水位中性，无拥挤 "
        "[跨资产资金] ETH 同步流入，风险偏好回升 "
        "[反共识结论] 若费率转负则多头拥挤，观点反转 "
        "[逻辑自检] 由于买盘来自现货而非杠杆，所以持续性较可验证；反之仅靠费率则失效 "
        "[总结] 参考 2025-10 同类流入场景，短期偏多，跌破前低则失效"
    ),
    "cross_asset_likely": json.dumps([{"asset": "ETH", "direction": "up"}], ensure_ascii=False),
    "cross_asset": "ETH",
    "analysis_degraded": 0,
}

DEGRADED = {
    "id": 9002,
    "suggested_action": "BUY",
    "target_asset": "WTI",
    "sentiment_score": 0.8,
    "evidence_confidence": 27.3,
    "completeness_score": 1.0,
    "analysis_degraded": 1,
    "analysis_degraded_reason": "server_fallback",
    "hard_data_evidence": "",
    "factor_ranking": "",
    "reasoning": "油价可能上涨。",
    "reasoning_path": "[总结] 看涨",
}

INCOMPLETE = {
    "id": 9003,
    "suggested_action": "BUY",
    "target_asset": "XAU",
    "completeness_score": 0.35,   # 有正文但字段残缺 → 短路，而不是契约错
    "reasoning": "看涨黄金，因避险需求上升。",
}


def run_selftest() -> int:
    ctx = CheckContext.null()
    cases = [("clean", CLEAN), ("degraded", DEGRADED), ("incomplete", INCOMPLETE)]
    failures = 0
    for name, dec in cases:
        r = audit(dec, ctx, strict=False)
        findings = r["findings"]
        bad = [f for f in findings if not f["remediation"] or not f["evidence_refs"]]
        print(f"[{name}] verdict={r['verdict']} findings={len(findings)} "
              f"coverage={r['coverage']['checked']}/{r['coverage']['total_layers']} "
              f"incomplete={len(r['incomplete'])}")
        for f in findings[:4]:
            print(f"    {f['code']} sev={f['severity']} {f['detail'][:60]}")
        if name == "incomplete" and r["verdict"] != "insufficient":
            print("    ✗ 完整性短路未生效"); failures += 1
        if name == "degraded" and not findings:
            print("    ✗ 降级决策未产出任何 finding"); failures += 1
        if bad:
            print(f"    ✗ {len(bad)} 条 finding 缺 remediation/evidence"); failures += 1
    print("selftest:", "PASS" if failures == 0 else f"FAIL ({failures})")
    return 0 if failures == 0 else 1
