# -*- coding: utf-8 -*-
"""v322: 熔断状态「凭据指纹」+ 门卫鉴权熔断不再冻结数据 —— 回归单测。

事故背景（2026-11-05 生产实测，用户反馈「怎么还显示待分析」）
------------------------------------------------------------------
熔断状态 v322 起跨进程落盘在 backend/runtime/breakers/*.json（一个进程发现
通道坏了、其它进程一起冷却）。这引入了三个此前不存在的问题：

1) **换 key 后旧熔断状态仍然生效**
   auth（401/403）熔断冷却 24 小时。昨天 key 失效写下的 state="open" 还钉在
   磁盘上 → 今天换上能用的新 key，通道体检仍返回
   "全部通道熔断: minimax1,minimax2" → 主循环**跳过认领**
   → 前端「待分析」只涨不跌。
   修法：落盘状态带「凭据指纹」，指纹不符即视为旧凭据残留 → 作废。

2) **reset() 只清内存不清磁盘 → 复位形同虚设**
   运维手工 reset 后，磁盘上的 open 状态被下一个进程读回来，问题依旧。

3) **门卫熔断跳过时丢了失败类型 → 新闻被永久冻结**
   门卫通道 llm_auth:minimax-gate 处于 open → _call_one 直接跳过、不发请求
   → err_code=None → _LAST_POOL_FAIL_CODES 里没有 401
   → _attach_gate 误判为「瞬时故障」→ 标记 gate_unavailable=1 冻结待回放
   → 主分析的认领 SQL 排除这些行 → 队列整体卡死。
   修法：熔断跳过时也要把语义码（401/429）传出去；且判据改成
   「**存在**鉴权类失败」而非「全部失败码都是鉴权类」。

本文件逐条锁死这三条不变量。
"""

from __future__ import annotations

import os
import sys

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import circuit_breaker
import config
import realtime_filter as rt
import pytest


# ======================================================================
# 不变量 3：作用域遮蔽审计（UnboundLocalError 陷阱）—— 全项目零容忍
# ======================================================================

def test_scope_shadow_audit_reports_zero():
    """全项目不得存在「局部变量遮蔽模块级函数/类」的陷阱。

    事故（2026-11-05）：`engine/ai_worker.py` 里
        from .utils import ... _ts        # _ts 是函数
    某个函数内部写了 `_ts = None`（只想存个时间戳）→ Python 判定
    `_ts` 在该函数内是局部变量 → 同函数里任何 `_ts()` 调用都抛
        UnboundLocalError: cannot access local variable '_ts'
    → 整批 16 条落库回滚 → 前端「待分析」永不下降。

    这个 bug 代码看起来完全正常（变量名合法、逻辑正确、语法无误），
    只在特定数据分支触发 —— 人读代码几乎发现不了 → 必须有自动防线。

    审计脚本本身也必须自证准确（能抓真 bug、不误报），否则大家会忽略它。
    """
    import subprocess
    import sys
    from pathlib import Path

    backend = Path(__file__).resolve().parent.parent
    script = backend / "scripts" / "audit_scope_shadow.py"
    if not script.exists():
        pytest.skip("审计脚本不存在")

    # ① 脚本自检必须通过（能抓真 bug + 不误报）
    st = subprocess.run(
        [sys.executable, str(script), "--selftest"],
        capture_output=True, text=True, cwd=str(backend), timeout=120,
    )
    assert st.returncode == 0, (
        "审计脚本自检失败（它会变成噪音）：\n%s" % st.stdout[-2000:]
    )

    # ② 全项目扫描必须零命中
    proc = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True, text=True, cwd=str(backend), timeout=600,
    )
    assert proc.returncode == 0, (
        "发现局部变量遮蔽模块级名字的陷阱（会抛 UnboundLocalError "
        "→ 整批落库回滚 → 「待分析」永不下降）：\n%s" % proc.stdout[-4000:]
    )


# ======================================================================
# 不变量 4：按用途分账 —— 评审不能吃光主分析的预算
# ======================================================================

def test_purpose_budget_parsing(monkeypatch):
    """用途预算解析：verify=3000000,translate=1500000"""
    import llm_budget

    monkeypatch.setenv("LLM_PURPOSE_TOKEN_BUDGET",
                       "verify=3000000,translate=1500000")
    assert llm_budget.purpose_token_budget("verify") == 3000000
    assert llm_budget.purpose_token_budget("translate") == 1500000
    # 未列出的用途 → 不限制（走全局预算）
    assert llm_budget.purpose_token_budget("main") == 0
    assert llm_budget.purpose_token_budget("gate") == 0


def test_purpose_budget_disabled_by_default(monkeypatch):
    """没配就全部不限制（不改变现有行为）。"""
    import llm_budget

    monkeypatch.delenv("LLM_PURPOSE_TOKEN_BUDGET", raising=False)
    assert llm_budget.purpose_token_budget("verify") == 0
    ok, _ = llm_budget.check_purpose_budget("verify", 100000)
    assert ok is True, "未配置独立预算时不得拦截"


def test_purpose_budget_blocks_when_exhausted(monkeypatch):
    """该用途超自己的额度 → 拦截（不再挤占主分析预算）。"""
    import llm_budget

    monkeypatch.setenv("LLM_PURPOSE_TOKEN_BUDGET", "verify=1000")
    monkeypatch.setattr(
        llm_budget, "usage_by_purpose",
        lambda purpose, refresh=False: {"requests": 10, "tokens": 5000},
    )
    ok, why = llm_budget.check_purpose_budget("verify", 0)
    assert ok is False, "超出用途预算必须拦截"
    assert "verify" in why


def test_purpose_budget_allows_within_quota(monkeypatch):
    """额度够 → 放行。"""
    import llm_budget

    monkeypatch.setenv("LLM_PURPOSE_TOKEN_BUDGET", "verify=1000000")
    monkeypatch.setattr(
        llm_budget, "usage_by_purpose",
        lambda purpose, refresh=False: {"requests": 3, "tokens": 30000},
    )
    ok, _ = llm_budget.check_purpose_budget("verify", 50000)
    assert ok is True
    # 剩余不足本次调用 → 拦
    ok2, _ = llm_budget.check_purpose_budget("verify", 999999)
    assert ok2 is False, "剩余额度不足本次调用必须拦（别做半截活）"


def test_peer_review_has_own_budget_guard():
    """评审路径必须接上用途预算护栏（源码级锁死，防止被后人删掉）。"""
    from pathlib import Path
    src = Path(__file__).resolve().parent.parent / "src_python" / "engine" / "ai_worker.py"
    text = src.read_text(encoding="utf-8")
    assert 'check_purpose_budget("verify"' in text, (
        "评审路径必须走用途独立预算 —— 否则评审会把主分析预算吃光，"
        "闸门到线后整条链路停摆"
    )


def test_translate_path_returns_original_not_none():
    """翻译被闸门拦截时必须返回原文，不能返回 None。

    返回 None → 调用方拿 None 做切片 → TypeError → 整条新闻被丢弃。
    """
    from pathlib import Path
    src = Path(__file__).resolve().parent.parent / "src_python" / "engine" / "webhook.py"
    text = src.read_text(encoding="utf-8")
    idx = text.index("额度闸门拦截")
    seg = text[idx:idx + 1200]
    assert "return original_title" in seg, (
        "翻译被额度闸门拦截时必须返回原文（return original_title），"
        "返回 None 会让调用方 TypeError → 新闻被丢弃"
    )


# ======================================================================
# 不变量 5：额度预警必须**多通道**且失败可见
# ======================================================================

def test_budget_warning_uses_all_channels():
    """预警不能只发微信一个通道。

    事故（2026-11-05）：额度打满时用户毫不知情 —— 因为预警只发 Server酱，
    而它一直返回 HTTP 400（SendKey 失效/正文超限）→ **预警静默失效**，
    失败信息还被 except 吞掉 → 直到全局闸门拦停才发现「服务不更新了」。
    """
    from pathlib import Path
    src = Path(__file__).resolve().parent.parent / "src_python" / "llm_budget.py"
    text = src.read_text(encoding="utf-8")
    idx = text.index("def _push(")
    seg = text[idx:idx + 2000]
    assert 'only=["wechat_sct"]' not in seg, (
        "预警不能只发一个通道 —— 单通道故障 = 预警静默 = 用户被偷袭"
    )
    assert "notification_channels.dispatch(title, body)" in seg, (
        "预警必须走多通道并联派发"
    )
    # 失败必须打印出来（不能被 except 吞掉）
    assert "预警通道失败" in seg, "预警通道失败必须可见，否则永远是静默失效"


# ======================================================================
# 不变量 6：评审必须走精简 prompt（不能带完整主分析上下文）
# ======================================================================

def test_peer_review_uses_lean_prompt():
    """评审只求「方向+分数」，不得带完整主分析 prompt。

    事故（2026-11-05 实测）：verify 757 次调用烧了 13.75M token，
    占全天消耗 60%，比主分析（8.2M）还多 56% —— 评审带的是
    八段推理链 + 十几个上下文注入块（10K+ token 输入）。
    """
    from pathlib import Path
    src = Path(__file__).resolve().parent.parent / "src_python" / "engine" / "ai_worker.py"
    text = src.read_text(encoding="utf-8")

    # ① _call_llm_sync 必须有 lean 参数
    assert "lean: bool = False" in text, "_call_llm_sync 必须支持 lean 参数"

    # ② 精简模式必须存在，且不走完整 prompt 组装
    assert "if lean:" in text, "必须实现精简模式分支"

    # ③ 评审调用必须传 lean=True
    assert '_lean_call = (purpose == "verify")' in text, (
        "评审（purpose=verify）必须自动走精简模式"
    )

    # ④ 精简模式必须用极小的 max_tokens（300 而非主分析的 4000）
    idx = text.index("if lean:")
    seg = text[idx:idx + 3000]
    assert '"max_tokens": 300' in seg, (
        "精简模式输出上限必须是 300 token（评审只要方向+分数）"
    )
    # 且必须截断新闻原文（v500：截断值已收敛到 _MAIN_NEWS_CHARS 常量，默认 1200 字符，
    # 可由 MAIN_NEWS_CHARS 覆盖；旧断言写死 [:2000] 与实现漂移）
    assert "[:_MAIN_NEWS_CHARS]" in seg, "精简模式必须截断新闻原文（否则输入仍然很大）"
    assert '_MAIN_NEWS_CHARS = max(600, int(os.getenv("MAIN_NEWS_CHARS", "1200")' in text


def test_peer_review_prompt_is_short():
    """评审 system prompt 必须是**极简**的（不注入八段推理链要求）。"""
    from pathlib import Path
    src = Path(__file__).resolve().parent.parent / "src_python" / "engine" / "ai_worker.py"
    text = src.read_text(encoding="utf-8")
    idx = text.index("_PEER_REVIEW_SYSTEM_PROMPT = (")
    seg = text[idx:idx + 2000]
    end = seg.index("\n    )")
    prompt_block = seg[:end]
    # 不该出现八段推理链的关键词（那些是主分析的要求）
    for kw in ("总框架", "因素定位", "驱动力", "水位博弈",
               "跨资产资金", "反共识结论", "逻辑自检"):
        assert kw not in prompt_block, (
            "评审 prompt 不该包含八段推理链要求（%r）—— 那是主分析的要求" % kw
        )
    assert len(prompt_block) < 1500, (
        "评审 prompt 应控制在 1500 字符内，实际 %d" % len(prompt_block))


# ======================================================================
# 不变量 7：门卫不得把「模型没回答」当成「不是噪音」
# ======================================================================

def test_gate_missing_fields_does_not_exclude_real_news():
    """门卫输出**缺字段**时不得判"排除"（否则真实新闻被永久丢弃）。

    事故（2026-11-05 实测）：2340 条正常新闻（波兰央行/俄央行/IIF 数据/
    美联储官员讲话）被门卫判成"排除" → is_noise=1 → 永远不会被分析，
    却在「待分析」计数里挂着（截图里满屏「待 AI 分析」）。

    根因：模型漏输出 step1/step2/core_impact 时，
    `not is_material or not assets or not core` 三个判断全为真 → 硬裁决"排除"。
    「模型没回答」≠「模型回答了不是」—— 前者是不可判定，应保守放行。
    """
    import realtime_filter as rt

    # 模型漏了 step1 / step2 / core_impact（只剩 verdict）
    out = rt._harden_verdict(
        verdict="可交易",
        is_material=False,      # 因为 step1 缺失
        assets=[],             # 因为 step2 缺失
        core="",               # 因为 core_impact 缺失
        kind="总结类",
        timing="即时",
        strength=6,
        impact_1to100=60,
        missing_fields=True,
    )
    assert out != "排除", (
        "门卫字段缺失时不得判排除 —— 「不可判定」必须保守放行，"
        "否则真实新闻被永久丢弃（实测 2340 条）"
    )


def test_gate_explicit_judgement_still_excludes():
    """模型**明确**回答「不重要/没影响标的/写不出核心影响」→ 仍要排除。"""
    import realtime_filter as rt

    out = rt._harden_verdict(
        verdict="排除",
        is_material=False,
        assets=[],
        core="",
        kind="总结类",
        timing="即时",
        strength=6,
        impact_1to100=60,
        missing_fields=False,
    )
    assert out == "排除", "模型明确否定时必须排除（不能因为保守放行而放水）"


def test_gate_opinion_and_low_impact_still_excluded_even_when_fields_missing():
    """观点稿 + 低影响力 → 即使字段缺失也照样排除（这两条不依赖缺失字段）。"""
    import realtime_filter as rt

    # 分析类（观点稿）
    assert rt._harden_verdict(
        verdict="可交易", is_material=False, assets=[], core="",
        kind="分析类", timing="即时", strength=6, impact_1to100=60,
        missing_fields=True,
    ) == "排除", "分析类观点稿必须排除"

    # 低影响力
    assert rt._harden_verdict(
        verdict="可交易", is_material=False, assets=[], core="",
        kind="总结类", timing="即时", strength=1, impact_1to100=5,
        missing_fields=True,
    ) == "排除", "低影响力新闻必须排除"


def test_gate_harden_verdict_default_is_unchanged():
    """默认参数（不传 missing_fields）必须保持旧行为 —— 既有单测不受影响。"""
    import realtime_filter as rt

    assert rt._harden_verdict(
        verdict="可交易", is_material=False, assets=[], core="",
        kind="总结类", timing="即时", strength=6, impact_1to100=60,
    ) == "排除", "不传 missing_fields 时必须维持旧语义（缺字段 → 排除）"


# ======================================================================
# 不变量 8：行情快照必须有**总预算**（不能 N×8 秒串行）
# ======================================================================

def test_snapshot_mtf_klines_use_total_budget():
    """多周期 K 线必须共享一个总预算（原来每周期各 8 秒 → 最坏 32 秒）。

    事故（2026-11-05 实测）：单次 snapshot **97.6 秒**
    （日志：SNAPSHOT PARTIAL (3/4 OK) in 97.609s）→
    单批 wall=145 秒 → 「待分析」降得极慢。
    根因：4 个周期**串行** `fut.result(timeout=8.0)`，每个的计时从调用
    那一刻起算 → 4×8=32 秒；再叠加 ticker/funding 的 10 秒 → 42 秒；
    4 个资产再串行 await → 97.6 秒。
    """
    from pathlib import Path
    src = Path(__file__).resolve().parent.parent / "src_python" / "market_snapshot.py"
    text = src.read_text(encoding="utf-8")
    idx = text.index("def _fetch_mtf_ohlcv(")
    seg = text[idx:idx + 2500]
    assert "total_timeout" in seg, "_fetch_mtf_ohlcv 必须支持总预算参数"
    assert "deadline" in seg, "必须用统一 deadline（而不是每个周期各等 8 秒）"
    # 判据：必须用统一 deadline 的剩余时间（_left），而不是每个周期固定 8 秒
    assert "fut.result(timeout=_left)" in seg, (
        "K 线周期必须用统一 deadline 的剩余时间（_left），"
        "而不是每个周期固定 8 秒（最坏累加成 4×8 秒）"
    )


def test_snapshot_awaits_assets_concurrently():
    """多资产必须**并发 await**（原来串行 for-await → 表现为 N×8 秒）。"""
    from pathlib import Path
    src = Path(__file__).resolve().parent.parent / "src_python" / "market_snapshot.py"
    text = src.read_text(encoding="utf-8")
    idx = text.index("async def get_snapshot(")
    seg = text[idx:idx + 3000]
    # v412: 并发等待从 asyncio.gather 换成 asyncio.wait（要带总预算并 cancel 超时任务，
    # gather 做不到"到点就收"）。不变量不变：必须先并发起任务，不能串行 await。
    assert "asyncio.create_task(" in seg, (
        "多资产必须先并发起任务 —— 串行 await 会让总耗时累加成 N×超时"
    )
    assert ("asyncio.wait(" in seg) or ("asyncio.gather(" in seg), (
        "多资产必须并发等待（asyncio.wait / asyncio.gather）"
    )





# ======================================================================
# 不变量 1：凭据指纹 —— 换 key 后旧熔断状态自动作废
# ======================================================================

def test_credential_epoch_changes_with_key(monkeypatch):
    """任一 LLM key 变化 → 指纹变化。"""
    monkeypatch.setenv("MINIMAX_API_KEY", "sk-aaa")
    monkeypatch.setenv("MINIMAX2_API_KEY", "sk-bbb")
    epoch_a = circuit_breaker.credential_epoch()
    assert epoch_a, "配了 key 就必须有指纹"

    monkeypatch.setenv("MINIMAX2_API_KEY", "sk-ccc")
    epoch_b = circuit_breaker.credential_epoch()
    assert epoch_a != epoch_b, "换一个 key 指纹必须变"

    monkeypatch.setenv("MINIMAX2_API_KEY", "sk-bbb")
    assert circuit_breaker.credential_epoch() == epoch_a, "同 key 指纹必须稳定"


def test_credential_epoch_empty_without_keys(monkeypatch):
    """没有 LLM key 时指纹为空（Yahoo/OKX 等非 LLM 源不受影响）。"""
    for k in circuit_breaker._CRED_ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    assert circuit_breaker.credential_epoch() == ""


def test_stale_breaker_state_is_ignored_after_key_rotation(monkeypatch):
    """核心不变量：旧凭据留下的 open 状态，换 key 后必须**立即失效**。

    这正是生产上「换了能用的 key，引擎仍然跳过认领 → 待分析不降」的根因。
    """
    monkeypatch.setenv("MINIMAX_API_KEY", "sk-old-key")
    monkeypatch.setenv("MINIMAX2_API_KEY", "sk-old-key2")

    brk = circuit_breaker.get("llm:test_epoch_probe", fail_threshold=1,
                              open_seconds=3600.0)
    brk.failure()          # 旧凭据下失败一次 → open，落盘
    assert brk.state == "open", "旧凭据下应处于熔断态"

    # 换 key（模拟运维换 key）—— 同一进程内换，因为指纹是现算的
    monkeypatch.setenv("MINIMAX_API_KEY", "sk-new-key")
    monkeypatch.setenv("MINIMAX2_API_KEY", "sk-new-key2")

    # 模拟「新进程」：直接 new 一个同名熔断器。
    # 刻意**不用** circuit_breaker.reset() —— reset() 现在会删落盘文件，
    # 那样就等于把"旧状态"提前抹掉了，测的就不是 epoch 机制本身了。
    # 直接 new 出来的实例会走同一个 _file_for(name) → 读到旧凭据写的 open。
    brk2 = circuit_breaker.CircuitBreaker("llm:test_epoch_probe",
                                         fail_threshold=1, open_seconds=3600.0)
    assert brk2.allow() is True, (
        "换 key 后旧凭据的熔断状态必须作废 —— 否则引擎会一直跳过认领，"
        "「待分析」只涨不跌"
    )
    assert brk2.state != "open"


def test_reset_also_clears_persisted_state(monkeypatch):
    """reset() 必须同时删落盘文件 —— 否则「复位」形同虚设。

    落盘状态被下一个进程（或下一个用例）读回来 → 熔断又被"复活"。
    """
    monkeypatch.setenv("MINIMAX_API_KEY", "sk-x")
    brk = circuit_breaker.get("llm:test_reset_probe", fail_threshold=1,
                              open_seconds=3600.0)
    brk.failure()
    assert brk.state == "open"

    path = circuit_breaker._file_for("llm:test_reset_probe")
    assert path.exists(), "熔断状态应已落盘"

    circuit_breaker.reset("llm:test_reset_probe")
    assert not path.exists(), "reset 必须删除落盘状态文件"

    # 全量 reset 也必须清空目录
    brk2 = circuit_breaker.get("llm:test_reset_probe2", fail_threshold=1,
                               open_seconds=3600.0)
    brk2.failure()
    assert circuit_breaker._file_for("llm:test_reset_probe2").exists()
    circuit_breaker.reset()
    assert not circuit_breaker._file_for("llm:test_reset_probe2").exists()


def test_purge_all_clears_everything(monkeypatch):
    """运维入口 purge_all()：内存 + 磁盘全清。"""
    monkeypatch.setenv("MINIMAX_API_KEY", "sk-y")
    brk = circuit_breaker.get("llm:test_purge_probe", fail_threshold=1,
                              open_seconds=3600.0)
    brk.failure()
    circuit_breaker.purge_all()
    assert circuit_breaker.get("llm:test_purge_probe").allow() is True
    assert not circuit_breaker._file_for("llm:test_purge_probe").exists()


# ======================================================================
# 不变量 2：门卫熔断跳过时必须带上失败类型 → 鉴权失效降级放行
# ======================================================================

def _fake_pool_entry(alias="minimax-gate", model="MiniMax-M2.7-highspeed"):
    return [{
        "id": f"{alias}/{model}",
        "label": f"{model} (fake)",
        "api_base": "https://example.invalid/v1",
        "api_key": "fake-key",
        "request_model": model,
        "extra_body": {},
        "key_alias": alias,
        "is_local": False,
        "timeout": 1.0,
        "use_proxy": False,
        "purpose": ["gate"],
    }]


@pytest.fixture
def _allow_budget(monkeypatch):
    """本文件测熔断/冻结语义，不测额度闸门 —— 默认放行。"""
    import llm_budget
    monkeypatch.setattr(llm_budget, "check",
                        lambda est_tokens=0, purpose="gate": (True, ""))
    monkeypatch.setattr(llm_budget, "consume_rate_slot",
                        lambda purpose="gate", window_s=60.0: (True, ""))


def test_auth_circuit_skip_degrades_instead_of_freezing(monkeypatch, _allow_budget):
    """核心不变量：门卫**鉴权熔断**时新闻必须降级放行，不能冻结待回放。

    冻结 = 等一个永远不会成功的结果 = 队列永久卡死 = 「待分析」只涨不跌。
    """
    circuit_breaker.reset()
    monkeypatch.setattr(config, "get_pool_for_purpose", lambda p: _fake_pool_entry())

    # 把门卫通道打到 auth 熔断（open，24h 冷却）
    auth_brk = rt._llm_breaker("minimax-gate", "auth")
    assert auth_brk is not None
    auth_brk.failure()
    assert auth_brk.state == "open"

    # 门卫不可用 → _attach_gate 必须走「降级放行」而不是「冻结待回放」
    out = rt._attach_gate({"is_noise": 0, "relevance_score": 0.5,
                           "status": "PENDING"}, "标题", "正文")

    # v3215(FIX-8b)：放行的标记值从 0 改成 2。
    # 原来写 0 虽然"没冻结"，但主分析端看到 `gate_strength=0 + verdict='无信号'`
    # 会把它当"没信号"丢弃并标 FAILED —— 等于放了又拦（实测 449 条里 344 条这么没的）。
    # 2 才是真正的"降级放行"：认领门槛 IN (0,2) 放它进来，且不带门卫标签。
    assert out["gate_unavailable"] == 2, (
        "鉴权熔断必须真降级放行（gate_unavailable=2）："
        "0 会被主分析判成『门卫说没信号』→ FILTERED/FAILED"
    )
    # v449：规则判决接管（这条输入无任何关键词命中 → 规则判「无信号」，按噪音归档）；
    # 关键不变量只剩「不许冻成 1 / 不许静默留未判定」。
    assert out.get("gate_rule_only") == 1, "v449：判定必须来自规则"
    assert out["gate_verdict"] in ("可交易", "无信号"), (
        f"规则必须给真结论，不许留未判定：{out.get('gate_verdict')!r}")
    circuit_breaker.reset()


def test_network_only_failure_still_freezes_for_replay(monkeypatch, _allow_budget):
    """纯网络/限流故障 → 仍应冻结待回放（回放可能成功，冻结是安全的）。"""
    circuit_breaker.reset()
    # v427 起超时/网络默认降级放行（避免错过建仓窗口）；这里显式退回旧的冻结语义，
    # 锁定「纯瞬时故障可回放」这条路径。
    monkeypatch.setenv("TRIDENT_GATE_FREEZE_ON_TIMEOUT", "1")
    monkeypatch.setattr(config, "get_pool_for_purpose", lambda p: _fake_pool_entry())

    # 让门卫调用失败（网络错误，非鉴权）
    def fake_post(api_base, api_key, model, payload, timeout, err_sink=None):
        if err_sink is not None:
            err_sink.update({"http_code": None, "kind": "network", "model": model})
        return None

    monkeypatch.setattr(rt, "_post_openai_compat", fake_post)

    out = rt._attach_gate({"is_noise": 0, "relevance_score": 0.5,
                           "status": "PENDING"}, "标题", "正文")
    assert out["gate_unavailable"] == 1, (
        "网络故障应冻结待回放（回放可能成功）"
    )
    assert out["is_noise"] == 0, "不可判定 ≠ 是噪音"
    circuit_breaker.reset()


def test_circuit_skip_reports_semantic_code(monkeypatch, _allow_budget):
    """熔断跳过时必须把语义码传出去（否则上层分不清 401 与网络故障）。"""
    circuit_breaker.reset()
    monkeypatch.setattr(config, "get_pool_for_purpose", lambda p: _fake_pool_entry())

    auth_brk = rt._llm_breaker("minimax-gate", "auth")
    auth_brk.failure()
    assert auth_brk.state == "open"

    seen_calls = []

    def _never_called(*a, **kw):
        seen_calls.append("called")
        return None

    monkeypatch.setattr(rt, "_post_openai_compat", _never_called)

    rt._call_pool_sync("sys", "user")
    assert not seen_calls, "熔断打开时不应发出任何真实请求"

    # 失败码里必须含 401（鉴权语义）
    assert 401 in rt._LAST_POOL_FAIL_CODES, (
        "熔断跳过必须携带鉴权语义码 401，"
        "否则上层会把鉴权失效误判成瞬时故障 → 冻结数据 → 队列卡死"
    )
    circuit_breaker.reset()
    rt._LAST_POOL_FAIL_CODES.clear()
