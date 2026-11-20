"""门卫(gate) → 主分析 交接时的 raw_news 状态判定。

背景（FIX-8）
-------------
修复前 `engine/ai_worker.py` 的门卫失效防御分支，把两件完全不同的事合成一类：

  ① 门卫**看不清**（模型不可用 / 鉴权失效 / http_error / 超时）→ gate_verdict 被写成"无信号"；
  ② 门卫**正常判定没信号**（真的不值得分析）。

两种情况在库里长得一模一样（gate_strength=0 + gate_verdict='无信号'），
而分支的行为是：丢弃 ai_decisions + `raw_news.status='FAILED'` + **retry_count 不增**。

后果（实测 2026-11-09）：近 6 小时 68 条 FAILED 里 **64 条（94.2%）是情况①**，
被当成终态埋掉，永远回不来 → 决策链停摆 14 小时，样本停止增长。

本模块把判定提成**纯函数**，由调用方负责落库，便于单测锁死语义：
  · 看不清  → PENDING（retry_count+1，带冷却，等门卫回放）；达阈值才 FAILED
  · 真无信号 → FILTERED（系统主动决定不分析，**不是失败**，不重试）
  · 噪声    → FILTERED

注意（重要）：`raw_news.status` 的 DB CHECK 约束只允许
('PENDING','PROCESSING','DONE','FAILED','FILTERED')，
**没有** SKIPPED / GATE_PENDING（`event_status.py` 里声明了但库里不认）。
所以这里一律用 FILTERED，不要用 SKIPPED，否则 UPDATE 会 IntegrityError 被吞掉、行卡在 PROCESSING。
"""

from typing import Optional, Tuple

STATUS_PENDING = "PENDING"
STATUS_FAILED = "FAILED"
STATUS_FILTERED = "FILTERED"

#: `gate_unavailable` 取值语义（FIX-8b）
#:   0 = 门卫正常给出了判定
#:   1 = 门卫不可用（限流/网络）→ 冻结待回放，冻结超 30 分钟自动放行
#:   2 = 门卫鉴权失效/额度耗尽 → **降级放行**：主分析照常跑，只是这条决策没有门卫标签
GATE_UNAVAILABLE_OK = 0
GATE_UNAVAILABLE_FROZEN = 1
GATE_UNAVAILABLE_PASSTHROUGH = 2

#: 判定原因（写入日志/诊断用，不落库）
REASON_NOISE = "noise"
REASON_GATE_UNAVAILABLE_RETRY = "gate_unavailable_retry"
REASON_GATE_UNAVAILABLE_EXHAUSTED = "gate_unavailable_exhausted"
REASON_NO_SIGNAL = "no_signal"
REASON_AUTH_PASSTHROUGH = "auth_failed_passthrough"


def resolve_drop_status(
    *,
    gate_strength: Optional[int] = None,
    gate_verdict: Optional[str] = None,
    is_noise: Optional[int] = None,
    gate_unavailable: Optional[int] = None,
    retry_count: Optional[int] = None,
    max_retries: int = 3,
) -> Tuple[Optional[str], int, str]:
    """返回 (新状态, 新 retry_count, 原因)。

    第一项为 None 表示"不处于丢弃分支"，调用方应保持原流程（不写 ai_decisions 之外的任何状态）。

    判定顺序（先因先判，不可调换）：
      1. 噪声            → FILTERED（终态，不重试）
      2. 门卫鉴权失效/额度耗尽（gate_unavailable=2）→ **放行**（返回 None），不做丢弃
      3. 门卫不可用      → PENDING + 重试；重试耗尽才 FAILED
      4. 门卫正常判无信号 → FILTERED（**不是失败**）
    """
    rc = int(retry_count or 0)

    if int(is_noise or 0) == 1:
        return STATUS_FILTERED, rc, REASON_NOISE

    # FIX-8b：门卫鉴权失效/额度耗尽时，realtime_filter 的语义是"降级放行"。
    # 修复前放行到这里又被当成"无信号"打死（设计说放行，实现是打死）。
    # 现在明确放行：不返回任何状态，调用方照常写 ai_decisions。
    if int(gate_unavailable or 0) == 2:
        return None, rc, REASON_AUTH_PASSTHROUGH

    if int(gate_unavailable or 0) == 1:
        nxt = rc + 1
        if nxt >= max_retries:
            return STATUS_FAILED, nxt, REASON_GATE_UNAVAILABLE_EXHAUSTED
        return STATUS_PENDING, nxt, REASON_GATE_UNAVAILABLE_RETRY

    # v324：'未判定' 是门卫没给结论，不能当"判了无信号"丢掉（历史行可能缺
    # gate_unavailable 标记，这里兜一层）。
    if str(gate_verdict or "") == "未判定":
        return None, rc, "门卫未判定·放行"

    if int(gate_strength or 0) == 0 and str(gate_verdict or "") == "无信号":
        return STATUS_FILTERED, rc, REASON_NO_SIGNAL

    return None, rc, ""
