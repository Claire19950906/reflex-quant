"""事件分析状态协议 — api_server(8000) 与 bridge_server(8100) 的唯一真相源。

背景（v176.12 修复）
--------------------
修复前两个 API 各自推断 analysis_status，语义不一致：
  * api_server  `_normalize_analysis_status(raw_status, has_decision)` → 依据 raw_news.status
  * bridge     `_news_row_to_api_event()`                          → 依据 news_events.status，
                                                                       再按 ad_id 是否存在兜底
两表状态不同步（raw_news 噪声停在 PENDING、news_events 噪声是 DONE），
导致同一批新闻在两个端口呈现不同的"待 AI 分析"徽章。

本模块把"状态"定义成一份可复用的协议：
  · 5 个规范状态：PENDING / PROCESSING / DONE / FAILED / SKIPPED
  · SKIPPED —— 噪声新闻的终态（系统主动判定为噪声并跳过 AI 分析），
    与 PENDING(待处理)/PROCESSING(处理中)/DONE(已完成)/FAILED(失败) 并列。
    选 SKIPPED 而非 IGNORED：语义上强调"系统流程主动跳过"，
    且与 DB 里 raw_news.status 的既有 CHECK 约束风格一致。
  · 唯一判定入口 `resolve_analysis_status()`，两个 API 都调用它，
    保证同一行数据在 8000/8100 上得到同一个 analysis_status + 同一个 reason。

状态优先级（高 → 低）
---------------------
  1. SKIPPED   —— is_noise=1：噪声，终态，不会再被 AI 分析
  2. DONE      —— 已有决策记录（has_decision），或 raw_news.status='DONE'
  3. FAILED    —— raw_news.status='FAILED'（重试次数耗尽）
  4. PROCESSING—— raw_news.status='PROCESSING'（worker 已认领）
  5. PENDING   —— 其余（真正待 AI 分析）

噪声优先于其它状态：噪声永远不会显示"待 AI 分析"，因为引擎根本不会处理它。
"""

from typing import Any, Optional

# ── 规范状态常量 ────────────────────────────────────────────────────────────
STATUS_PENDING = "PENDING"
STATUS_PROCESSING = "PROCESSING"
STATUS_DONE = "DONE"
STATUS_FAILED = "FAILED"
STATUS_SKIPPED = "SKIPPED"
# v321: 门卫(LLM)不可用时入库的新闻 —— 等 replay_deferred_gate.py 回放补判。
# 不是终态（还在等回放），但也**不是**"待 AI 分析"（主分析不会认领它，
# 等门卫回放成功、主分析完成之前，它一直卡在这）。
STATUS_GATE_PENDING = "GATE_PENDING"
# v322: 超龄 / 僵尸行终态 —— 系统**主动决定不再分析**（超过时效门槛、已不可交易）。
# 与 FAILED 是两回事：
#   FAILED   = 试过了，没成（分析失败）
#   FILTERED = 根本没试（超龄了，信号已无交易价值）
# 必须分开：把「已过期」显示成「AI 分析失败」是误导。
STATUS_FILTERED = "FILTERED"

#: 全部合法状态（DB CHECK 约束 + API 输出共用同一份）
VALID_STATUSES = (
    STATUS_PENDING,
    STATUS_PROCESSING,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_SKIPPED,
    STATUS_GATE_PENDING,
    STATUS_FILTERED,
)

#: 终态：不会再被 AI worker 处理
#: 注意 GATE_PENDING **不在**终态里 —— 它还在等门卫回放，回放后会重新进入流程。
TERMINAL_STATUSES = (STATUS_DONE, STATUS_FAILED, STATUS_SKIPPED, STATUS_FILTERED)

#: 前端徽章文案（前端 event-bus.tsx 保持同义，避免"待 AI 分析"误显示）
STATUS_LABELS = {
    STATUS_PENDING: "待 AI 分析",
    STATUS_PROCESSING: "AI 分析中",
    STATUS_FAILED: "AI 分析失败",
    STATUS_SKIPPED: "噪声已跳过",
    STATUS_GATE_PENDING: "门卫待回放",
    STATUS_FILTERED: "已归档",
}

#: 无决策时各状态对应的 reason 文案（前端直接展示）
STATUS_REASONS = {
    STATUS_PENDING: "待 AI 分析",
    STATUS_PROCESSING: "AI 分析中",
    STATUS_FAILED: "AI 分析失败",
    STATUS_SKIPPED: "噪声新闻，已跳过 AI 分析",
    STATUS_GATE_PENDING: "门卫(LLM)通道故障时入库，等待回放补判后进入 AI 分析",
    STATUS_FILTERED: "已超龄（超过分析时效门槛，已不可交易），系统主动归档、不再分析",
}


def _norm(status: Any) -> str:
    """把任意来源的状态值归一为大写字符串（空/None → 空串）。"""
    if status is None:
        return ""
    return str(status).strip().upper()


def resolve_analysis_status(
    raw_status: Any = None,
    *,
    has_decision: bool = False,
    is_noise: bool = False,
    gate_pending: bool = False,
) -> str:
    """把「处理状态 + 是否噪声 + 是否有决策」收敛成唯一规范状态。

    参数：
      raw_status   —— 处理状态真值（raw_news.status 或 news_events.status）
      has_decision —— 是否已产出 AI 决策（ai_decisions 有行）
      is_noise     —— 是否为噪声新闻（is_noise=1）
      gate_pending —— 门卫(LLM)未判定（gate_unavailable=1），等回放补判

    返回：PENDING / PROCESSING / DONE / FAILED / SKIPPED / GATE_PENDING 之一

    说明：
      * is_noise 优先级最高 —— 噪声被门卫筛掉，引擎永不认领，
        无论 raw_status 残留成什么（PENDING/PROCESSING/FAILED）都归一到 SKIPPED，
        杜绝"噪声显示待 AI 分析"。
      * has_decision 优先于 raw_status —— 决策已落库即为 DONE，
        即使 raw_news.status 因重置模拟盘等历史操作回退到别的值。
      * gate_pending 优先于 has_decision —— 门卫未判定的行不会被主分析认领，
        不可能有决策；但若历史脏数据同时有两个标记，gate_pending 更能反映真相
        （它还在等回放，而不是已经分析完）。
    """
    if is_noise:
        return STATUS_SKIPPED
    if gate_pending:
        return STATUS_GATE_PENDING
    if has_decision:
        return STATUS_DONE
    st = _norm(raw_status)
    if st in VALID_STATUSES:
        return st
    # 未知/空状态 → PENDING（唯一"还需要被处理"的状态）
    return STATUS_PENDING


def status_reason(status: Any, *, has_reason: bool = False) -> str:
    """无决策记录时给出一条 reason 文案（前端直接展示）。"""
    st = _norm(status)
    if st == STATUS_DONE:
        # 有决策但走了这里 → 说明决策被重置模拟盘清掉了
        return "历史记录已重置删除（重置模拟盘时清空交易）"
    if has_reason:
        return ""
    return STATUS_REASONS.get(st, STATUS_REASONS[STATUS_PENDING])
