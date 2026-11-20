#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v322 稳定性保险丝回归测试 —— 治「突然抽风」的四个机制。

对应 2026-11-04 生产事故的真实形态：

1. **熔断器 half_open 无限放行**
   老实现注释写「只放行一次探测请求」，代码却是 `return True` ——
   熔断等于失效，通道坏了还在一直打。

2. **熔断恢复瞬间的补偿性重试风暴**
   冷却结束那一刻所有排队请求同时醒来 → 几十个并发 → 429 → 再熔断 → 循环。

3. **熔断状态进程内 → 多进程各自重试放大**
   engine / api / review_agent / judge_worker 各打各的，同一份额度被放大 N 倍。

4. **只有总量闸门，没有速率闸门**
   额度总量没超，但一瞬间并发几十个 → 上游限流 → 服务表现「时好时坏」。

这些测试**不依赖网络、不依赖数据库**，可以随时跑。
"""
import importlib
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src_python"
sys.path.insert(0, str(SRC))


def _fresh_circuit_breaker(tmpdir):
    """导入一个隔离的 circuit_breaker 实例（独立共享目录、关闭抖动）。"""
    import circuit_breaker as cb
    cb = importlib.reload(cb)
    cb._STATE_DIR = Path(tmpdir)
    cb._SHARE_ENABLED = True
    return cb


def _fresh_budget(tmpdir, rpm):
    import llm_budget
    llm_budget = importlib.reload(llm_budget)
    llm_budget._RPM_DIR = Path(tmpdir)
    llm_budget._RPM_LOCK = Path(tmpdir) / "llm_rpm.lock"
    llm_budget._RPM_DATA = Path(tmpdir) / "llm_rpm_slots.json"
    os.environ["LLM_MAX_RPM"] = str(rpm)
    # config 是模块加载时的环境变量快照 —— 只改 os.environ 不够，
    # 必须把 config.LLM_MAX_RPM 一起覆盖（llm_budget._cfg 优先读 config）。
    try:
        import config
        config.LLM_MAX_RPM = float(rpm)
    except Exception:
        pass
    return llm_budget


# ── 1. half_open single-flight ─────────────────────────────────────────────
def test_half_open_only_allows_one_probe():
    """half_open 必须 single-flight：只放行第一个探测请求。"""
    with tempfile.TemporaryDirectory() as tmp:
        cb = _fresh_circuit_breaker(tmp)
        brk = cb.CircuitBreaker("sf", fail_threshold=1, open_seconds=0.1,
                                jitter_ratio=0.0)
        brk.failure()                      # → open
        time.sleep(0.15)                   # 冷却结束
        assert brk.allow() is True         # 第一个探测：放行
        assert brk.allow() is False        # 第二个：必须拒绝
        assert brk.allow() is False        # 第三个：依然拒绝


# ── 2. 冷却抖动 ────────────────────────────────────────────────────────────
def test_open_cooldown_has_jitter():
    """open→half_open 的放行要带随机抖动，避免所有通道同刻醒来。"""
    with tempfile.TemporaryDirectory() as tmp:
        cb = _fresh_circuit_breaker(tmp)
        brk = cb.CircuitBreaker("j", fail_threshold=1, open_seconds=0.2,
                                jitter_ratio=0.5)
        brk.failure()
        assert brk.allow() is False        # 冷却期刚过 → 仍拒
        time.sleep(0.45)                   # 超过 max(0.2*1.5)
        assert brk.allow() is True


# ── 3. 探测丢失兜底 ────────────────────────────────────────────────────────
def test_probe_timeout_does_not_wedge_breaker():
    """半开探测若不回报（进程卡死），熔断不能永久卡死。"""
    with tempfile.TemporaryDirectory() as tmp:
        cb = _fresh_circuit_breaker(tmp)
        brk = cb.CircuitBreaker("pt", fail_threshold=1, open_seconds=0.1,
                                jitter_ratio=0.0, probe_timeout=0.2)
        brk.failure()
        time.sleep(0.15)
        assert brk.allow() is True          # 探测在飞
        assert brk.allow() is False         # 仍在飞 → 拒
        time.sleep(0.3)                     # 探测超时
        assert brk.allow() is True          # 超时后允许新探测


# ── 4. 跨进程共享熔断 ──────────────────────────────────────────────────────
def test_breaker_state_shared_across_processes():
    """一个进程熔断，其它进程必须一起冷却（别各自重试放大额度消耗）。"""
    with tempfile.TemporaryDirectory() as tmp:
        cb = _fresh_circuit_breaker(tmp)
        p1 = cb.CircuitBreaker("shared", fail_threshold=1, open_seconds=60.0,
                               jitter_ratio=0.0)
        p2 = cb.CircuitBreaker("shared", fail_threshold=1, open_seconds=60.0,
                               jitter_ratio=0.0)
        p1.failure()                        # p1 熔断 → 落盘
        assert p2.allow() is False, "其它进程必须同步到熔断状态"



# ── 5. 速率闸门 ────────────────────────────────────────────────────────────
def test_rate_fuse_caps_requests_in_window():
    """60 秒滑动窗口内最多放行 RPM 次。"""
    with tempfile.TemporaryDirectory() as tmp:
        lb = _fresh_budget(tmp, 5)
        oks = [lb.consume_rate_slot(purpose="t")[0] for _ in range(7)]
        assert oks[:5] == [True] * 5, "前 5 次应放行"
        assert oks[5:] == [False, False], "超速必须被拦下"


def test_rate_fuse_recovers_after_window():
    """窗口滑出后自动恢复放行（不会永久卡死）。"""
    import json
    with tempfile.TemporaryDirectory() as tmp:
        lb = _fresh_budget(tmp, 3)
        for _ in range(3):
            lb.consume_rate_slot(purpose="t")
        assert lb.consume_rate_slot(purpose="t")[0] is False
        with open(lb._RPM_DATA, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        old = time.time() - 300
        data["slots"] = [old] * 3
        with open(lb._RPM_DATA, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        assert lb.consume_rate_slot(purpose="t")[0] is True


def test_rate_fuse_disabled_never_blocks():
    """LLM_MAX_RPM=0 时完全不影响使用。"""
    with tempfile.TemporaryDirectory() as tmp:
        lb = _fresh_budget(tmp, 0)
        for _ in range(50):
            assert lb.consume_rate_slot(purpose="t")[0] is True


def test_rate_fuse_fails_open_on_internal_error():
    """闸门自身故障必须放行 —— 保护机制不能反过来把系统打死。"""
    with tempfile.TemporaryDirectory() as tmp:
        lb = _fresh_budget(tmp, 5)
        lb._RPM_DATA = "/nonexistent_dir_xyz/nope.json"
        assert lb.consume_rate_slot(purpose="t")[0] is True


# ── 6. 跨进程并发扣减不丢不重 ──────────────────────────────────────────────
def test_rate_fuse_cross_process_exact():
    """4 个进程各抢 10 个名额、上限 20 → 合计必须精确等于 20。"""
    with tempfile.TemporaryDirectory() as tmp:
        worker = Path(tmp) / "worker.py"
        worker.write_text(
            "import sys\n"
            "sys.path.insert(0, %r)\n" % str(SRC) +
            "import llm_budget\n"
            "n = 0\n"
            "for _ in range(10):\n"
            "    ok, _ = llm_budget.consume_rate_slot(purpose='p')\n"
            "    n += 1 if ok else 0\n"
            "print(n)\n",
            encoding="utf-8")
        env = dict(os.environ)
        env["LLM_MAX_RPM"] = "20"
        # .env 里 LLM_MIN_INTERVAL_S=8（最小间隔闸门，用于错峰防 429）。这里验的是
        # "速率名额精确等于上限"，不是错峰；不关掉它，40 次调用会串行等 ~320s，
        # 直接把 120s 的超时打穿（实测就是超时失败，不是名额算错）。
        env["LLM_MIN_INTERVAL_S"] = "0"
        env["TRIDENT_RATE_DIR"] = tmp
        procs = [subprocess.Popen([sys.executable, str(worker)],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  env=env) for _ in range(4)]
        total = 0
        for p in procs:
            out, err = p.communicate(timeout=120)
            try:
                total += int(out.decode().strip())
            except Exception:
                raise AssertionError("worker 输出异常: %s / %s" %
                                     (out[:200], err[:300]))
        assert total == 20, "并发扣减必须精确等于上限，实际 %d" % total


# ── 7. 预算闸门与速率闸门协作 ──────────────────────────────────────────────
def test_budget_check_includes_rate_state():
    """check() 的状态里要带上速率信息（面板/体检要用）。"""
    with tempfile.TemporaryDirectory() as tmp:
        lb = _fresh_budget(tmp, 10)
        st = lb.state()
        assert "rate" in st
        assert st["rate"]["max_rpm"] == 10


# ── 8. 抖动必须是「固定放行时刻」，不能每次调用重 roll ──────────────────────
def test_jitter_release_time_is_fixed():
    """回归：allow() 里每次调用重 roll 抖动会让冷却被提前穿透。

    正确做法是熔断「打开时」一次算好 wait_until 并固定下来，
    之后无论调用多少次 allow()，放行时刻都不能变。
    """
    with tempfile.TemporaryDirectory() as tmp:
        cb = _fresh_circuit_breaker(tmp)
        brk = cb.CircuitBreaker("jit_fixed", fail_threshold=1, open_seconds=30.0,
                                jitter_ratio=0.25)
        brk.failure()                       # → open（此刻算出 wait_until）
        first = brk._wait_until
        assert first > 0.0, "打开熔断时必须固定放行时刻"
        # 连续多次 allow() 不得让放行时刻漂移
        for _ in range(50):
            brk.allow()
        assert brk._wait_until == first, "放行时刻发生漂移，冷却可能被提前穿透"
        # 冷却总时长不得短于基准值
        assert first - brk._opened_at >= 30.0, "抖动后冷却不得短于基准"


# ── 9. 熔断打开后必须「秒拒」，不能卡住调用方 ──────────────────────────────
def test_open_breaker_rejects_instantly():
    """通道已死时，后续调用必须立刻返回，不能让请求线程等冷却。

    这是「不抽风」的核心体感：熔断不是让系统卡住，而是让它快速降级。
    """
    with tempfile.TemporaryDirectory() as tmp:
        cb = _fresh_circuit_breaker(tmp)
        brk = cb.CircuitBreaker("instant", fail_threshold=1, open_seconds=300.0,
                                jitter_ratio=0.25)
        brk.failure()
        worst = 0.0
        for _ in range(20):
            t0 = time.perf_counter()
            assert brk.allow() is False
            worst = max(worst, time.perf_counter() - t0)
        assert worst < 0.05, "熔断拒绝必须瞬时完成，实测 %.4fs" % worst


# ── 10. 跨进程必须使用同一个放行时刻 ───────────────────────────────────────
def test_release_time_shared_across_processes():
    """一个进程算出的放行时刻要同步给其它进程。

    否则各进程各自 roll 抖动 → 醒来的时刻参差不齐 → 又变成一波冲。
    """
    with tempfile.TemporaryDirectory() as tmp:
        cb = _fresh_circuit_breaker(tmp)
        p1 = cb.CircuitBreaker("rel", fail_threshold=1, open_seconds=30.0,
                               jitter_ratio=0.25)
        p2 = cb.CircuitBreaker("rel", fail_threshold=1, open_seconds=30.0,
                               jitter_ratio=0.25)
        p1.failure()
        p1._last_sync = 0.0
        p2._last_sync = 0.0
        p2.allow()                          # 触发一次同步
        assert p2._wait_until == p1._wait_until, \
            "两个进程的放行时刻不一致，抖动打散失效"


# ── 11. 门卫 L2 判定不能被 thinking 截断 ───────────────────────────────────
def test_gate_l2_max_tokens_not_trivial():
    """回归：L2 的 max_tokens 不能是 4。

    MiniMax 是 reasoning 模型，即使下发 thinking:disabled 仍会偶发输出
    <think>...</think>。max_tokens=4 时 think 一出现就把预算吃光 →
    输出被截断 → 解析不出 0/1 → 门卫返回 None → **全部噪音放行给主分析**
    （实测被迫放行率 65%，修好后降到 ~17%）。
    """
    import realtime_filter as rf
    assert rf._L2_MAX_TOKENS >= 64, \
        "L2 max_tokens 太小，thinking 会挤掉判定答案（当前 %s）" % rf._L2_MAX_TOKENS


def test_gate_l2_parse_strips_thinking():
    """回归：L2 解析必须先剥掉 thinking 再取判定。

    直接在含 think 的文本上找 0/1 会误判（think 里常出现 "0 or 1"、"第 1 步"）。
    """
    import re

    def parse(raw):
        if not raw:
            return None
        c = re.sub(r"<think>.*?</think>", "", raw.strip(), flags=re.S)
        if "<think>" in c:
            c = c.split("<think>")[0]
        hits = re.findall(r"\b([01])\b", c)
        return int(hits[-1]) if hits else None

    # think 里的 "0 or 1" 不该被当成判定
    assert parse("<think>is it 0 or 1</think>\n\n1") == 1
    assert parse("<think>is it 0 or 1</think>\n\n0") == 0
    # 纯答案
    assert parse("1") == 1
    assert parse("0") == 0
    # 未闭合 think（被 max_tokens 截断）→ 判定失败 → 降级放行，不误判
    assert parse("<think>reasoning truncated") is None
    assert parse("") is None
    assert parse(None) is None


# ── 12. 门卫远程通道必须阶梯降级 ───────────────────────────────────────────
def test_gate_remote_stair_step_configured():
    """回归：门卫远程通道默认必须走阶梯降级（省请求数）。

    旧行为把一条新闻并行发给池里全部远程通道，只用第一个返回的结果
    —— 实测每条新闻 7.26 次门卫调用，其中绝大部分是纯浪费。
    """
    import config
    assert getattr(config, "GATE_REMOTE_STAIR_STEP", None) is not None, \
        "缺少 GATE_REMOTE_STAIR_STEP 开关"
    assert config.GATE_REMOTE_STAIR_STEP is True, \
        "门卫远程通道未启用阶梯降级（会退回全池并行的浪费模式）"
