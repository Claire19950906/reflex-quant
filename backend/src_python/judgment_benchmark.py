#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""judgment_benchmark.py — 「判断能力 vs 市场」基准层（纯统计 + 只读适配）

要解决的问题
------------
只看方向命中率（hit_rate）会系统性地误判能力：

  · **单边行情里"，无脑做空"也能有 60%+**。2026-11-04→11-19 这段 BTC/ETH/SOL
    跌 18~22%、WTI 跌 14%、XAU 跌 7%，同期影子层 4h 命中 56.7%，
    而"同一批样本、闭眼喊 SELL"的基线是 62.9% —— 换句话说，
    系统的命中率**低于**一个没有判断力的策略。
  · **多空混着喊**会把命中率稀释/虚抬。61 条样本里 SELL 29/36=80.6%、
    BUY 5/24=20.8%，两个数都不是能力，是"方向配比 × 行情方向"。
  · **判对方向 ≠ 赚钱**。扣掉手续费与滑点后，期望值可能直接翻负。

所以本模块只做一件事：把"命中率"换成三个**必须一起看**的数：

  1. 裸命中率（现状，参考用）
  2. 同批样本的市场基线：always-SELL / always-BUY / 同配比盲选
  3. **平衡准确率（去类偏置）**：唯一能回答"有没有方向判别力"的量

外加一个扣费后的**净期望（bps/笔）**，回答"这么判断能不能赚钱"。

口径定义（写死，改了口径 definition_hash 会变，历史对比自动作废）
------------------------------------------------------------------
样本 = 一条"系统当时喊了方向 + 事后有了真实涨跌"的记录。

    up   := move_pct > 0      down := move_pct < 0      flat := == 0（剔除）
    always_sell_hit := down / (up + down)      —— 无脑做空的基线命中率
    always_buy_hit  := up   / (up + down)
    mix_implied     := p_sell*down率 + p_buy*up率 —— 保持系统自己的多空配比、
                       但方向与行情无关（即"同样的仓位结构，闭眼择时"）
    裸命中           := Σhit / n_settled
    平衡准确率(BA)   := (P(喊BUY|真涨) + P(喊SELL|真跌)) / 2
                        —— 标准定义，按**真实标签**两类各自算召回再平均，
                           多空喊多喊少都不影响这个数
    BA'(对照口径)    := (P(喊对|喊BUY) + P(喊对|喊SELL)) / 2
                        —— 与历史 R4 算法一致，便于和旧数字对齐
    净期望(bps)      := mean(direction_sign × move_pct)×100 − fee_bps

⚠️ BA 与 BA' 必须一起看，原因是一个反直觉的结论：
    **只喊一个方向的系统，BA 恒等于 50。** 全喊 SELL 时 P(喊BUY|真涨)=0、
    P(喊SELL|真跌)=1 → BA=(0+1)/2=50，无论它对错。这不是缺陷，是正确定义
    （它确实没有"区分涨跌"的能力）。但"没信息"和"反着来"是两件事：
    全喊 SELL 而行情 65% 在跌 → BA=50, BA'=65（白送的 beta）；
    全喊 SELL 而行情只有 35% 在跌 → BA=50, BA'=35（系统性反指，可能翻符号就赚）。
    所以判定层用 BA 判"有没有判别力"，用 BA' 判"是不是反着来"。

判定规则（不产 FAIL，只产"能不能下结论"）
------------------------------------------
    n < gate                       → 样本不足（不下结论）
    BA 点估计 ≥ 55 且 95%区间下界 > 50 → 有方向判别力
    BA 点估计 ≥ 55 但区间压不住 50   → 待确认（方向对了，证据还不够）
    50 ≤ BA < 55                    → 无优势（贴着抛硬币）
    BA < 50                         → 反向（比抛硬币还差）

为什么要"平衡准确率"而不是"命中率 + 基线"两个数
------------------------------------------------
基线能说明"这段行情是不是白送的"，但不能说明"去掉白送之后还剩多少"。
BA 直接把类偏置扣掉：多空喊得再偏，只要两类真行情上的召回都过 50，
就确实有判别力；反之永远被拉回 50 附近。2026-11-17 的 R4 归因
（S3 49.2% 拆成 UP 37.1%/DOWN 62.6% → 平均 49.9%）就是同一个结论。

留的"口"（数据进来后只改调用，不改逻辑）
----------------------------------------
    · load_shadow_ledger(conn)       影子账本（闸门前方向 + 4h/24h 真值）— 现在就有
    · load_shadow_reflections(conn)  反射层（direction_ok/direction_wrong）— 现在就有
    · load_json(path)                外部样本（回测/模拟盘导出的 jsonl）— 还没接线
    三个 loader 都吐同一种 Outcome，统计层与数据源完全解耦：
    新数据源进来 = 加一个 loader，判定/口径/区间一行都不用改。

用法
----
    python3 judgment_benchmark.py                      # 影子账本，默认口径
    python3 judgment_benchmark.py --source reflections  # 换反射层口径
    python3 judgment_benchmark.py --fee-bps 20          # 换成本假设
    python3 judgment_benchmark.py --json                # 机读
    python3 judgment_benchmark.py --fixture skill        # 合成样本自检（不碰生产库）
    python3 judgment_benchmark.py --selftest            # 四种世界的断言自检

退出码: 0 = 正常（样本不足也是 0）；2 = 环境错误。**统计结论从不影响退出码。**
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import sqlite3
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

HERE = Path(__file__).resolve().parent
BACKEND_DIR = HERE.parent
DEFAULT_DB = Path(os.environ.get("TRIDENT_DB_PATH") or (BACKEND_DIR / "trident_event_bus.db"))

# ── 判定参数（写死；改就是改口径）──────────────────────────────────────────
GATE = 200              # 最小样本量：低于它只报区间，不下结论
MARGIN_PT = 5.0         # 平衡准确率要高于抛硬币多少个百分点才算"有判别力"
COIN_PT = 50.0
DEFAULT_FEE_BPS = 12.0  # 往返成本假设（手续费+滑点），bps
BOOT_ROUNDS = 2000
BOOT_SEED = 20261119    # 固定种子 → 同一批样本每次跑出的区间完全一致（可回归）

ST_THIN = "样本不足"
ST_EDGE = "有判别力"
ST_MARGINAL = "待确认"
ST_FLAT = "无优势"
ST_INVERT = "反向"


# ─────────────────────────────────────────────────────────────────────────────
# 数据结构
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class Outcome:
    """一条"喊了方向 + 事后有真值"的样本。三个 loader 都吐这个。"""
    asset: str
    direction: str          # BUY / SELL
    move_pct: float         # 持有期内的真实涨跌幅（%）
    entry_ts: int = 0
    horizon_min: int = 0
    confidence: float = 0.0
    source: str = ""

    @property
    def hit(self) -> int:
        """方向是否与真实涨跌一致。flat(=0) 不算命中也不算错。"""
        if self.direction == "BUY":
            return 1 if self.move_pct > 0 else 0
        if self.direction == "SELL":
            return 1 if self.move_pct < 0 else 0
        return 0

    @property
    def signed_move_pct(self) -> float:
        """按仓位方向折算后的收益（%）：BUY 取 +move，SELL 取 -move。"""
        return self.move_pct if self.direction == "BUY" else -self.move_pct


# ─────────────────────────────────────────────────────────────────────────────
# 基础统计（无第三方依赖，可与 scorecard 的 wilson_interval 互相印证）
# ─────────────────────────────────────────────────────────────────────────────
def wilson_interval(k: float, n: int, z: float = 1.96) -> Tuple[float, float]:
    """Wilson score 95% 区间（比例）。n=0 → (0,1)，表示"什么都不知道"。"""
    if n <= 0:
        return (0.0, 1.0)
    p = max(0.0, min(1.0, float(k) / float(n)))
    denom = 1.0 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = (z * math.sqrt(max(p * (1 - p), 0.0) / n + z * z / (4 * n * n))) / denom
    return (max(0.0, center - half), min(1.0, center + half))


def bootstrap_ci(samples: Sequence[Outcome], stat: Callable[[Sequence[Outcome]], Optional[float]],
                 rounds: int = 400, seed: int = BOOT_SEED) -> Tuple[Optional[float], Optional[float]]:
    """对"样本重采样后重算统计量"的 95% 区间（仅作诊断用，不在热路径）。

    说明：默认热路径不用它 —— 800 条 × 2000 轮要 9 秒，而 gate=200 以上时
    解析区间（见 ba_interval / expectancy_interval）与它的差别小于 0.5pt。
    保留这个函数是为了在样本很少、分布很偏时做交叉验证。
    """
    if not samples:
        return (None, None)
    rng = random.Random(seed)
    n = len(samples)
    vals: List[float] = []
    for _ in range(rounds):
        resample = [samples[rng.randrange(n)] for _ in range(n)]
        v = stat(resample)
        if v is not None:
            vals.append(float(v))
    if len(vals) < 30:
        return (None, None)
    vals.sort()
    return (vals[int(0.025 * len(vals))], vals[min(len(vals) - 1, int(0.975 * len(vals)))])


def ba_interval(samples: Sequence[Outcome]) -> Tuple[Optional[float], Optional[float]]:
    """平衡准确率的 95% 区间（解析解，秒出）。

    BA = (TPR+TNR)/2 是两个独立二项比例的加权和，标准差可直接合成：
        SE(BA) = 0.5 × sqrt( p_up(1-p_up)/n_up + p_dn(1-p_dn)/n_dn )
    n ≥ 200（本模块的 gate）时正态近似的覆盖度与 bootstrap 差别 <0.5pt。
    """
    ups = [s for s in samples if s.move_pct > 0]
    downs = [s for s in samples if s.move_pct < 0]
    if not ups or not downs:
        return (None, None)
    p_up = sum(1 for s in ups if s.direction == "BUY") / len(ups)
    p_dn = sum(1 for s in downs if s.direction == "SELL") / len(downs)
    se = 0.5 * math.sqrt(p_up * (1 - p_up) / len(ups) + p_dn * (1 - p_dn) / len(downs))
    ba = (p_up + p_dn) / 2 * 100.0
    return (ba - 1.96 * se * 100.0, ba + 1.96 * se * 100.0)


def expectancy_interval(samples: Sequence[Outcome],
                        fee_bps: float = DEFAULT_FEE_BPS) -> Tuple[Optional[float], Optional[float]]:
    """净期望（bps）的 95% 区间：mean(signed bps) − fee ± 1.96×SE。"""
    vals = [s.signed_move_pct * 100.0 for s in samples]
    if len(vals) < 2:
        return (None, None)
    mean = sum(vals) / len(vals)
    var = sum((v - mean) ** 2 for v in vals) / (len(vals) - 1)
    se = math.sqrt(var / len(vals))
    net = mean - fee_bps
    return (net - 1.96 * se, net + 1.96 * se)


# ─────────────────────────────────────────────────────────────────────────────
# 三个口径（纯函数，输入样本，不碰库）
# ─────────────────────────────────────────────────────────────────────────────
def direction_block(samples: Sequence[Outcome]) -> Dict[str, dict]:
    """按喊的方向拆开：这是**最容易被误读**的一张表，必须与基线并排看。"""
    out: Dict[str, dict] = {}
    for d in ("BUY", "SELL"):
        sub = [s for s in samples if s.direction == d]
        k = sum(s.hit for s in sub)
        n = len(sub)
        rate = (k / n * 100.0) if n else None
        ci = wilson_interval(k, n) if n else (None, None)
        out[d] = {
            "n": n, "hit": k,
            "rate": None if rate is None else round(rate, 1),
            "ci": (None, None) if ci[0] is None else (round(ci[0] * 100, 1), round(ci[1] * 100, 1)),
        }
    return out


def baseline_block(samples: Sequence[Outcome]) -> Dict[str, Optional[float]]:
    """同批样本、无判断力的三种基线。

    注意：基线是**在这批样本自己身上**算的 —— 时间点、品种、持有期全部对齐，
    不存在"基线窗口和系统窗口对不上"的问题（这是之前手算基线时的最大误差源）。
    """
    up = sum(1 for s in samples if s.move_pct > 0)
    down = sum(1 for s in samples if s.move_pct < 0)
    flat = len(samples) - up - down
    tot = up + down
    if tot == 0:
        return {"n": len(samples), "up": up, "down": down, "flat": flat,
                "always_sell": None, "always_buy": None, "mix_implied": None,
                "p_buy": None, "p_sell": None}
    p_buy = sum(1 for s in samples if s.direction == "BUY") / len(samples)
    p_sell = sum(1 for s in samples if s.direction == "SELL") / len(samples)
    down_rate = down / tot
    up_rate = up / tot
    return {
        "n": len(samples), "up": up, "down": down, "flat": flat,
        "always_sell": round(down_rate * 100, 1),
        "always_buy": round(up_rate * 100, 1),
        # 同配比盲选：仓位结构一样，但与行情无关 → 这是"没有择时能力"的期望命中率
        "mix_implied": round((p_sell * down_rate + p_buy * up_rate) * 100, 1),
        "p_buy": round(p_buy * 100, 1),
        "p_sell": round(p_sell * 100, 1),
    }


def balanced_accuracy(samples: Sequence[Outcome]) -> Optional[float]:
    """平衡准确率（按真实标签两类平均）。单边标签 → None（判不了，不是 0）。"""
    ups = [s for s in samples if s.move_pct > 0]
    downs = [s for s in samples if s.move_pct < 0]
    if not ups or not downs:
        return None
    tpr = sum(1 for s in ups if s.direction == "BUY") / len(ups)    # 真涨里喊对的
    tnr = sum(1 for s in downs if s.direction == "SELL") / len(downs)  # 真跌里喊对的
    return (tpr + tnr) / 2.0 * 100.0


def balanced_accuracy_pred(samples: Sequence[Outcome]) -> Optional[float]:
    """按"喊的方向"两类平均（对照口径，和 R4 的老算法一致，便于和历史数字对上）。

    与 BA(label) 的差别：这里先固定预测类别再算命中，多空配比会轻微影响结果。
    单方向系统（全喊 BUY 或全喊 SELL）退化为**裸命中率** —— 此时它恰好是唯一能
    暴露"系统性反指"的量（全喊 SELL 而只有 35% 在跌 → 35%，而非恒等的 50%）。
    """
    buys = [s for s in samples if s.direction == "BUY"]
    sells = [s for s in samples if s.direction == "SELL"]
    if not buys and not sells:
        return None
    if not buys or not sells:               # 单方向：退化为裸命中
        sub = buys or sells
        return sum(s.hit for s in sub) / len(sub) * 100.0
    r_buy = sum(s.hit for s in buys) / len(buys)
    r_sell = sum(s.hit for s in sells) / len(sells)
    return (r_buy + r_sell) / 2.0 * 100.0


def net_expectancy(samples: Sequence[Outcome], fee_bps: float = DEFAULT_FEE_BPS) -> Dict[str, Optional[float]]:
    """扣费后的每笔净期望（bps）。这是"能不能赚钱"的最小判据。

    只算"看对方向能赚多少"，不含杠杆、仓位、持有成本之外的东西。
    手续费+滑点按往返一次计（开+平），默认 12bps。
    """
    vals = [s.signed_move_pct * 100.0 for s in samples]   # % → bps
    if not vals:
        return {"n": 0, "gross_bps": None, "net_bps": None, "ci": (None, None), "fee_bps": fee_bps}
    gross = sum(vals) / len(vals)
    net = gross - fee_bps
    ci_net = expectancy_interval(samples, fee_bps)
    return {
        "n": len(vals),
        "gross_bps": round(gross, 1),
        "net_bps": round(net, 1),
        "ci": (None, None) if ci_net[0] is None else (round(ci_net[0], 1), round(ci_net[1], 1)),
        "fee_bps": fee_bps,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 判定：把三个口径合成一句能下结论的话
# ─────────────────────────────────────────────────────────────────────────────
def judge(pack: dict, gate: int = GATE, margin: float = MARGIN_PT) -> Dict[str, str]:
    """输入 benchmark() 的结果，输出 {code, headline, detail}。永不 FAIL。"""
    n = pack["n_settled"]
    if n == 0:
        tp = pack.get("throughput") or {}
        if tp.get("settleable") == 0:
            detail = ("已结算样本为 0，且近 24h 池内没有可结算的方向决策 —— "
                      "这不是'再等等'，是判断链没在出方向（HOLD 占比过高或被闸门全挡）。"
                      "先修产出，否则永远测不了。")
        else:
            detail = "影子层/反射层还没攒到已结算样本；等数据进来再读，不要当成能力为 0。"
        return {"code": "empty", "headline": f"没有可结算样本（{ST_THIN}）", "detail": detail}
    ba = pack["ba_label"]["value"]
    if n < gate or ba is None:
        need = max(gate - n, 0)
        why = "样本量不够" if n < gate else "只有单边真值，平衡准确率判不了"
        tp = pack.get("throughput") or {}
        zero = (tp.get("settleable_per_hour") in (0, 0.0)) or tp.get("settleable") == 0
        if zero:
            detail = ("样本不足，而且**产能为零**：近 24h 没有池内可结算的方向决策 —— "
                      "这不是'再等几天'的问题，是判断链根本没在出方向（HOLD 占比过高）。"
                      "先修产出，再谈能力。")
        else:
            eta = tp.get("eta_days")
            eta_txt = f"，按当前速度约 {eta:.0f} 天" if eta else ""
            detail = (f"{why}。再攒 {need} 条已结算样本{eta_txt}；当前只报区间，不给'行/不行'。")
        return {"code": "thin", "headline": f"{ST_THIN}：n={n}/{gate}", "detail": detail}

    lo = pack["ba_label"]["ci"][0]
    ba_pred = pack["ba_pred"]
    hit_rate = pack["hit_rate"]
    # ① 先判"是不是系统性反指"：全喊一个方向时 BA 恒 50，只有 BA' 能暴露符号反了
    if ba_pred is not None and ba_pred < COIN_PT - margin:
        return {"code": "inverted",
                "headline": f"{ST_INVERT}：BA'={ba_pred:.1f}%（按喊的方向算）",
                "detail": ("按喊的方向统计，系统性低于抛硬币 —— 先查方向口径是不是取反了"
                           "（含 SELL/BUY 互换、涨跌符号、结算价方向），翻符号往往就能救回来；"
                           "不要先去调参。")}
    if ba >= COIN_PT + margin and lo is not None and lo > COIN_PT:
        code, head = "edge", f"{ST_EDGE}：BA={ba:.1f}%（区间下界 {lo:.1f}% > 50%）"
        detail = ("去偏置后两类真行情都判得住，且证据压得住抛硬币；"
                  "下一步看净期望是否为正、以及按品种/置信度分桶是否稳定。")
        net = pack["expectancy"]["net_bps"]
        if net is not None and net < 0:
            detail += (f"⚠️ 但扣费后净期望 {net:+.1f}bps（负）—— 方向判对还不够覆盖成本，"
                       "要么提高持有期/幅度门槛，要么降成本，别只看命中率。")
    elif ba >= COIN_PT + margin:
        code, head = "marginal", f"{ST_MARGINAL}：BA={ba:.1f}%（区间下界压不住 50%）"
        detail = ("方向可能对，但样本还压不住噪音；别据此加倍投入，先按同口径继续攒样本。")
    elif hit_rate is not None and abs(hit_rate - COIN_PT) >= margin:
        # 命中率明显偏离 50，但去偏置后是 50 → 典型 beta：行情/配比白送，不是能力
        code, head = "beta", f"{ST_FLAT}（beta）：裸命中 {hit_rate:.1f}% 看着能打，BA={ba:.1f}%"
        detail = (f"这 {hit_rate:.1f}% 是从行情方向和自身多空配比里白送的；"
                  f"同配比盲选基线 {pack['baseline']['mix_implied']}%、无脑做空基线 "
                  f"{pack['baseline']['always_sell']}%。去掉偏置后没有剩余判别力，"
                  "继续加参数之前先确认特征里有没有真正区分涨跌的信息。")
    else:
        code, head = "flat", f"{ST_FLAT}：BA={ba:.1f}%（贴着抛硬币）"
        detail = "既没有判别力也没有明显偏置，样本上的表现基本等于随机。"
    return {"code": code, "headline": head, "detail": detail}


# ─────────────────────────────────────────────────────────────────────────────
# 汇总：一次算完所有口径
# ─────────────────────────────────────────────────────────────────────────────
def benchmark(samples: Sequence[Outcome], fee_bps: float = DEFAULT_FEE_BPS,
              gate: int = GATE) -> dict:
    """把一批样本压成一份可直接上屏 / 落盘的包。空样本也返回完整结构（不抛）。"""
    settled = [s for s in samples if s.move_pct != 0 and s.direction in ("BUY", "SELL")]
    k = sum(s.hit for s in settled)
    n = len(settled)
    hit_rate = (k / n * 100.0) if n else None
    ci = wilson_interval(k, n) if n else (None, None)

    ba = balanced_accuracy(settled)
    ba_ci = ba_interval(settled) if settled else (None, None)
    ba_pred = balanced_accuracy_pred(settled)

    base = baseline_block(settled)
    # 平盘在 settled 里已被剔掉（move=0 既不算对也不算错），但报告要如实交代
    # "剔了几条" —— 否则用户会以为分母是全部样本，命中率被偷偷放大。
    base["flat"] = sum(1 for s in samples if s.move_pct == 0 and s.direction in ("BUY", "SELL"))
    dirs = direction_block(settled)
    exp_ = net_expectancy(settled, fee_bps)

    pack = {
        "n_samples": len(samples),
        "n_settled": n,
        "hit_rate": None if hit_rate is None else round(hit_rate, 1),
        "hit_ci": (None, None) if ci[0] is None else (round(ci[0] * 100, 1), round(ci[1] * 100, 1)),
        "baseline": base,
        "by_direction": dirs,
        "ba_label": {
            "value": None if ba is None else round(ba, 1),
            "ci": (None, None) if ba_ci[0] is None else (round(ba_ci[0], 1), round(ba_ci[1], 1)),
        },
        "ba_pred": None if ba_pred is None else round(ba_pred, 1),
        # 裸命中 − 同配比盲选 = "择时超额"；正数才有讨论价值，负数说明还不如闭眼配比
        "excess_vs_mix": None if (hit_rate is None or base["mix_implied"] is None)
        else round(hit_rate - base["mix_implied"], 1),
        "excess_vs_always_sell": None if (hit_rate is None or base["always_sell"] is None)
        else round(hit_rate - base["always_sell"], 1),
        "expectancy": exp_,
        "fee_bps": fee_bps,
        "gate": gate,
        "by_asset": _by_asset(settled),
    }
    pack["verdict"] = judge(pack, gate=gate)
    return pack


def _by_asset(settled: Sequence[Outcome]) -> List[dict]:
    """按品种拆：单个品种样本少时只报 (n, 命中)，不报百分比当结论。"""
    rows: Dict[str, dict] = {}
    for s in settled:
        r = rows.setdefault(s.asset, {"asset": s.asset, "n": 0, "hit": 0, "sum_move": 0.0})
        r["n"] += 1
        r["hit"] += s.hit
        r["sum_move"] += s.move_pct
    out = []
    for r in sorted(rows.values(), key=lambda x: -x["n"]):
        out.append({
            "asset": r["asset"], "n": r["n"], "hit": r["hit"],
            "rate": round(r["hit"] / r["n"] * 100.0, 1) if r["n"] else None,
            "avg_move": round(r["sum_move"] / r["n"], 3) if r["n"] else None,
        })
    return out


# ─────────────────────────────────────────────────────────────────────────────
# 数据适配（唯一的"口"：新数据源只加 loader，统计层不动）
# ─────────────────────────────────────────────────────────────────────────────
def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    try:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None
    except sqlite3.Error:
        return False


def load_shadow_ledger(conn: sqlite3.Connection, horizon: Optional[int] = None,
                       since_days: Optional[int] = None) -> List[Outcome]:
    """影子账本：闸门前方向 + 已结算真值。现在就有数据，最干净的样本源。

    v400 口径延续：降级路径拼出来的方向不进账本（由 shadow_ledger.collect 过滤），
    这里只读已结算行（settled_at / hit 非空）。
    """
    if not _has_table(conn, "shadow_ledger"):
        return []
    sql = ("SELECT asset, direction, move_pct, entry_ts, horizon_min, confidence "
           "FROM shadow_ledger WHERE settled_at IS NOT NULL AND hit IS NOT NULL "
           "AND move_pct IS NOT NULL")
    args: List[object] = []
    if horizon:
        sql += " AND horizon_min = ?"
        args.append(int(horizon))
    if since_days and since_days > 0:
        import time as _time
        sql += " AND decided_ts >= ?"
        args.append(int(_time.time()) - int(since_days) * 86400)
    rows = conn.execute(sql + " ORDER BY entry_ts", tuple(args)).fetchall()
    return [Outcome(asset=str(r[0] or ""), direction=str(r[1] or ""), move_pct=float(r[2]),
                    entry_ts=int(r[3] or 0), horizon_min=int(r[4] or 0),
                    confidence=float(r[5] or 0.0), source="shadow_ledger") for r in rows]


def load_shadow_reflections(conn: sqlite3.Connection, since_days: Optional[int] = None) -> List[Outcome]:
    """反射层（4h 真值）：direction_ok / direction_wrong 两类才算判得出。

    verdict='flat'/'insufficient' 的样本没有可用真值，直接剔除 —— 它们正是
    "判不了"的那一部分，混进分母会把命中率稀释成假象。
    """
    if not _has_table(conn, "shadow_reflections"):
        return []
    sql = ("SELECT asset, claimed_direction, realized_move_pct, entry_ts, window_min, "
           "COALESCE(claimed_confidence, 0) FROM shadow_reflections "
           "WHERE verdict IN ('direction_ok','direction_wrong') "
           "AND realized_move_pct IS NOT NULL")
    args: List[object] = []
    if since_days and since_days > 0:
        import time as _time
        sql += " AND exit_ts >= ?"
        args.append(int(_time.time()) - int(since_days) * 86400)
    rows = conn.execute(sql + " ORDER BY entry_ts", tuple(args)).fetchall()
    return [Outcome(asset=str(r[0] or ""), direction=str(r[1] or ""), move_pct=float(r[2]),
                    entry_ts=int(r[3] or 0), horizon_min=int(r[4] or 0),
                    confidence=float(r[5] or 0.0), source="shadow_reflections") for r in rows]


def load_json(path: Path) -> List[Outcome]:
    """外部样本（回测/模拟盘导出）。留给"数据还没进库"的场景：

    jsonl 每行一个对象，字段：asset / direction / move_pct（必需），
    entry_ts / horizon_min / confidence（可选）。按这个格式导出即可直接评估。
    """
    out: List[Outcome] = []
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            out.append(Outcome(
                asset=str(d.get("asset", "")), direction=str(d.get("direction", "")),
                move_pct=float(d["move_pct"]), entry_ts=int(d.get("entry_ts", 0) or 0),
                horizon_min=int(d.get("horizon_min", 0) or 0),
                confidence=float(d.get("confidence", 0) or 0), source="json"))
        except (KeyError, TypeError, ValueError):
            continue
    return out


LOADERS: Dict[str, Callable[..., List[Outcome]]] = {
    "ledger": load_shadow_ledger,
    "reflections": load_shadow_reflections,
}


# ─────────────────────────────────────────────────────────────────────────────
# 产能：样本"会不会来"
#
# 这件事和"准不准"必须分开看。2026-11-19 实测：一天 390 条决策里只有 15 条
# 带方向（3.8%），4 小时窗口内甚至 100% HOLD —— 这种状态下影子层永远攒不到
# 样本，任何"对市场"的结论都无从谈起。所以报告里必须有一行"方向决策/小时"
# 和"距离可判定还差几天"，否则读到的永远只是"样本不足"四个字。
# ─────────────────────────────────────────────────────────────────────────────
def _tradable_assets() -> Optional[set]:
    """能结算的标的清单。不硬编码第二份 —— 直接复用 shadow_ledger.TRADABLE，
    免得池子扩了以后两边口径打架（扩池时只该改一处）。"""
    try:
        import shadow_ledger as _sl
        return set(_sl.TRADABLE)
    except Exception:
        return None


def throughput(conn: sqlite3.Connection, hours: int = 24, gate: int = GATE) -> dict:
    import time as _time
    now = int(_time.time())
    since = now - hours * 3600
    tradable = _tradable_assets()
    out = {"window_hours": hours, "decisions": None, "directional": None,
           "directional_per_hour": None, "settleable": None, "settleable_per_hour": None,
           "settled_total": None, "remaining_to_gate": None, "eta_days": None}
    try:
        if not _has_table(conn, "ai_decisions"):
            return out
        cols = {r[1] for r in conn.execute("PRAGMA table_info(ai_decisions)")}
        deg = "COALESCE(analysis_degraded,0)=0" if "analysis_degraded" in cols else "1=1"
        out["decisions"] = int(conn.execute(
            "SELECT COUNT(*) FROM ai_decisions WHERE ts>=?", (since,)).fetchone()[0] or 0)
        out["directional"] = int(conn.execute(
            "SELECT COUNT(*) FROM ai_decisions WHERE ts>=? AND ungated_action IN ('BUY','SELL') "
            f"AND {deg}", (since,)).fetchone()[0] or 0)
        # 只有"池内标的"能进影子账本 —— 喊了方向但标的是 US_STOCK 这种桶的，结算不了
        if tradable:
            marks = ",".join("?" * len(tradable))
            out["settleable"] = int(conn.execute(
                "SELECT COUNT(*) FROM ai_decisions WHERE ts>=? AND ungated_action IN ('BUY','SELL') "
                f"AND {deg} AND target_asset IN ({marks})",
                (since, *tradable)).fetchone()[0] or 0)
        else:
            out["settleable"] = out["directional"]
    except sqlite3.Error:
        return out
    if out["decisions"]:
        out["directional_per_hour"] = round(out["directional"] / hours, 1)
        out["settleable_per_hour"] = round((out["settleable"] or 0) / hours, 1)
    try:
        if _has_table(conn, "shadow_ledger"):
            out["settled_total"] = int(conn.execute(
                "SELECT COUNT(*) FROM shadow_ledger WHERE settled_at IS NOT NULL").fetchone()[0] or 0)
    except sqlite3.Error:
        pass
    settled = out["settled_total"] or 0
    out["remaining_to_gate"] = max(gate - settled, 0)
    per_hour = out["settleable_per_hour"] or 0.0
    if settled < gate:
        # 只有"每条方向决策都能结算"这个上界；真实结算率还要看取价能不能配上
        out["eta_days"] = round((gate - settled) / (per_hour * 24), 1) if per_hour > 0 else None
    else:
        out["eta_days"] = 0.0
    return out


# ─────────────────────────────────────────────────────────────────────────────
# 渲染
# ─────────────────────────────────────────────────────────────────────────────
def _fmt(v: Optional[float], unit: str = "%", nd: int = 1) -> str:
    return "—" if v is None else f"{v:.{nd}f}{unit}"


def _fmt_ci(ci: Sequence[Optional[float]], unit: str = "%") -> str:
    if ci[0] is None or ci[1] is None:
        return "—"
    return f"[{ci[0]:.1f}, {ci[1]:.1f}]{unit}"


def render(pack: dict) -> str:
    """人读报告。结构固定三段：量 → 三个口径 → 结论。"""
    lines: List[str] = []
    base, dirs, exp_ = pack["baseline"], pack["by_direction"], pack["expectancy"]
    n = pack["n_settled"]

    lines.append("=" * 92)
    lines.append(f"判断能力 vs 市场 · 口径 source={pack.get('source', '—')} · 已结算 n={n} / 门槛 {pack['gate']}")
    lines.append("=" * 92)
    lines.append(f"样本 {pack['n_samples']} 条 → 可结算 {n} 条"
                 f"（剔除平盘 {base.get('flat', 0)} 条 / 无真值）")
    tp = pack.get("throughput") or {}
    if tp.get("directional_per_hour") is not None:
        eta = tp.get("eta_days")
        eta_txt = "—（没有可结算的方向决策，样本不会增加）" if eta is None else f"{eta:.1f} 天"
        lines.append(f"产能: 近 {tp['window_hours']}h 决策 {tp['decisions']} 条 / 带方向 {tp['directional']} 条"
                     f" / 池内可结算 {tp.get('settleable', tp['directional'])} 条"
                     f"（{tp.get('settleable_per_hour', 0):.1f} 条/小时）；"
                     f"已结算 {tp.get('settled_total') or 0}/{pack['gate']}，距可判定约 {eta_txt}")
    if n == 0:
        lines.append("")
        lines.append(f"  {pack['verdict']['headline']}")
        lines.append(f"  {pack['verdict']['detail']}")
        return "\n".join(lines)

    lines.append("")
    lines.append("【一、裸命中（现状口径，只作参考）】")
    lines.append(f"  方向命中率     {_fmt(pack['hit_rate'])}  {_fmt_ci(pack['hit_ci'])}")
    lines.append(f"    其中 SELL    {_fmt(dirs['SELL']['rate'])}  {_fmt_ci(dirs['SELL']['ci'])}  (n={dirs['SELL']['n']})")
    lines.append(f"    其中 BUY     {_fmt(dirs['BUY']['rate'])}  {_fmt_ci(dirs['BUY']['ci'])}  (n={dirs['BUY']['n']})")

    lines.append("")
    lines.append("【二、同批样本的市场基线（拿掉'行情白送'）】")
    lines.append(f"  真实涨/跌/平   {base['up']} / {base['down']} / {base['flat']}"
                 f"    → 跌的占比 = always-SELL 基线 {_fmt(base['always_sell'])}")
    lines.append(f"  多空配比        BUY {_fmt(base['p_buy'])} / SELL {_fmt(base['p_sell'])}")
    lines.append(f"  同配比盲选基线  {_fmt(base['mix_implied'])}   （仓位结构一样、方向与行情无关）")
    lines.append(f"  裸命中 − 无脑做空 = {_fmt(pack['excess_vs_always_sell'], 'pt')}"
                 f"   裸命中 − 同配比盲选 = {_fmt(pack['excess_vs_mix'], 'pt')}")

    lines.append("")
    lines.append("【三、去偏置后的判别力（唯一能回答'有没有能力'的数）】")
    lines.append(f"  平衡准确率 BA  {_fmt(pack['ba_label']['value'])}  {_fmt_ci(pack['ba_label']['ci'])}"
                 f"   ← 按真实涨/跌两类各算召回再平均，多空喊多少都不影响")
    lines.append(f"  对照口径 BA'   {_fmt(pack['ba_pred'])}   ← 按喊的方向两类平均（与历史 R4 算法一致）")

    lines.append("")
    lines.append(f"【四、扣费后的净期望（fee={exp_['fee_bps']:.0f}bps/往返）】")
    lines.append(f"  毛期望 {_fmt(exp_['gross_bps'], 'bps')} → 净期望 {_fmt(exp_['net_bps'], 'bps')}"
                 f"   {_fmt_ci(exp_['ci'], 'bps')}   (n={exp_['n']})")

    if pack["by_asset"]:
        lines.append("")
        lines.append("【五、按品种（单品种 n 小，只看方向不看结论）】")
        for a in pack["by_asset"]:
            lines.append(f"  {a['asset']:<10} n={a['n']:>4}  命中 {a['rate']:>5.1f}%"
                         f"   期间均值 {a['avg_move']:+.3f}%")

    lines.append("")
    lines.append("【结论】")
    lines.append(f"  {pack['verdict']['headline']}")
    lines.append(f"  {pack['verdict']['detail']}")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# 自检：四种"已知世界"—— 逻辑通不通，不靠真实数据也能验
# ─────────────────────────────────────────────────────────────────────────────
def make_fixture(world: str, n: int = 800, seed: int = 7) -> List[Outcome]:
    """合成一批样本，四个世界的结果是**已知**的，用来验证统计层与判定层。

    coin    : 方向随机、涨跌随机        → 裸命中≈50, BA≈50, 应当判"无优势"
    bias    : 全喊 SELL、行情 65% 在跌   → 裸命中≈65, BA≈50, 应当判"无优势"（beta 陷阱）
    skill   : 喊 SELL 时 62% 真跌、喊 BUY 时 62% 真涨 → BA≈62，应当判"有判别力"
    anti    : 喊 SELL 时只有 35% 真跌   → BA≈35，应当判"反向"
    """
    rng = random.Random(seed)
    out: List[Outcome] = []
    assets = ["BTC", "XAU", "WTI", "ETH"]

    def _add(asset: str, direction: str, move: float, i: int) -> None:
        out.append(Outcome(asset=asset, direction=direction, move_pct=round(move, 3),
                           entry_ts=1700000000 + i * 900, horizon_min=240, confidence=0.6,
                           source=f"fixture:{world}"))

    for i in range(n):
        asset = assets[i % len(assets)]
        if world == "coin":
            direction = "BUY" if rng.random() < 0.5 else "SELL"
            up = rng.random() < 0.5
        elif world == "bias":
            direction = "SELL"
            up = rng.random() >= 0.65
        elif world == "skill":
            direction = "BUY" if rng.random() < 0.5 else "SELL"
            up = (rng.random() < 0.62) if direction == "BUY" else (rng.random() >= 0.62)
        elif world == "anti":
            direction = "SELL"
            up = rng.random() >= 0.35
        else:
            raise ValueError(f"未知世界: {world}")
        move = round(rng.uniform(0.05, 0.8), 3) * (1 if up else -1)
        _add(asset, direction, move, i)
    return out


def selftest(verbose: bool = True) -> int:
    """五个世界跑一遍，断言判定码与设计要求一致。返回失败条数。

    每个世界的结果都是**已知**的，所以这套自检等价于"统计层的单元测试"：
    不需要真实数据也能验证"逻辑通不通、口径有没有漂"。
    """
    expect = {
        # 判不了：方向随机 + 涨跌随机 → BA≈50，命中率≈50
        "coin": {"verdict": {"flat", "marginal"}, "ba": (46, 54), "hit": (45, 55)},
        # beta：全喊 SELL + 行情 65% 在跌 → 命中率高、BA 恒 50（必须判"无判别力"）
        "bias": {"verdict": {"beta", "flat"}, "ba": (48, 52), "hit_min": 60},
        # 真有判别力：两类各 62% → BA≈62，必须判 edge
        "skill": {"verdict": {"edge"}, "ba": (58, 68)},
        # 反指：全喊 SELL 但只有 35% 在跌 → BA 恒 50、BA'≈35，必须判 inverted
        "anti": {"verdict": {"inverted"}, "ba": (48, 52), "ba_pred": (28, 42)},
        # 样本门槛：n=30 → 必须判"样本不足"，不许下结论
        "thin": {"verdict": {"thin"}, "n": 30},
    }
    fails = 0
    for world, exp in expect.items():
        samples = make_fixture(world if world != "thin" else "skill", n=exp.get("n", 800))
        pack = benchmark(samples)
        v = pack["verdict"]["code"]
        ba = pack["ba_label"]["value"]
        hit = pack["hit_rate"]
        ba_pred = pack["ba_pred"]
        checks = [v in exp["verdict"]]
        if "ba" in exp:
            checks.append(ba is not None and exp["ba"][0] <= ba <= exp["ba"][1])
        if "hit" in exp:
            checks.append(hit is not None and exp["hit"][0] <= hit <= exp["hit"][1])
        if "hit_min" in exp:
            checks.append(hit is not None and hit >= exp["hit_min"])
        if "ba_pred" in exp:
            checks.append(ba_pred is not None and exp["ba_pred"][0] <= ba_pred <= exp["ba_pred"][1])
        ok = all(checks)
        if verbose:
            print(f"  [{'OK' if ok else 'NO'}] {world:<6} 裸命中={hit}%  BA={ba}%  BA'={ba_pred}%  "
                  f"基线(全空)={pack['baseline']['always_sell']}%  判定={v}")
        fails += 0 if ok else 1

    # 空样本必须优雅退化（数据还没进来时不能炸、也不能报 0 能力）
    empty = benchmark([])
    ok_empty = empty["verdict"]["code"] == "empty" and empty["ba_label"]["value"] is None
    if verbose:
        print(f"  [{'OK' if ok_empty else 'NO'}] empty  空样本 → {empty['verdict']['headline']}")
    fails += 0 if ok_empty else 1

    if verbose:
        print(f"\n自检结果: {'全部通过（口径与判定逻辑自洽）' if fails == 0 else f'{fails} 项不符合预期'}")
    return fails


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────
def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="判断能力 vs 市场（只读）")
    ap.add_argument("--db", default="", help="DB 路径（默认生产库，只读打开）")
    ap.add_argument("--source", default="ledger", choices=["ledger", "reflections", "json"],
                    help="样本源: 影子账本 / 反射层 / 外部 jsonl")
    ap.add_argument("--json-path", default="", help="--source json 时的样本文件")
    ap.add_argument("--horizon", type=int, default=0, help="只取这个持有期（分钟），0=全部")
    ap.add_argument("--since-days", type=int, default=0, help="只看最近 N 天，0=全部")
    ap.add_argument("--fee-bps", type=float, default=DEFAULT_FEE_BPS, help="往返成本（bps）")
    ap.add_argument("--gate", type=int, default=GATE, help="最小样本量")
    ap.add_argument("--window-hours", type=int, default=24, help="产能统计窗口（小时）")
    ap.add_argument("--json", action="store_true", help="机读输出")
    ap.add_argument("--fixture", default="", choices=["", "coin", "bias", "skill", "anti"],
                    help="用合成样本跑一遍（不碰任何库）")
    ap.add_argument("--selftest", action="store_true", help="四种世界断言自检")
    args = ap.parse_args(argv)

    if args.selftest:
        return 0 if selftest() == 0 else 1

    if args.fixture:
        pack = benchmark(make_fixture(args.fixture), fee_bps=args.fee_bps, gate=args.gate)
        pack["source"] = f"fixture:{args.fixture}"
        print(json.dumps(pack, ensure_ascii=False, indent=2) if args.json else render(pack))
        return 0

    if args.source == "json":
        if not args.json_path:
            print("[环境错误] --source json 需要 --json-path", file=sys.stderr)
            return 2
        samples = load_json(Path(args.json_path).expanduser())
        pack = benchmark(samples, fee_bps=args.fee_bps, gate=args.gate)
        pack["source"] = f"json:{args.json_path}"
    else:
        db = Path(args.db).expanduser().resolve() if args.db else DEFAULT_DB
        if not db.exists():
            print(f"[环境错误] 找不到数据库: {db}", file=sys.stderr)
            return 2
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)
        tp = throughput(conn, hours=args.window_hours, gate=args.gate)
        try:
            if args.source == "ledger":
                samples = load_shadow_ledger(conn, horizon=args.horizon or None,
                                             since_days=args.since_days or None)
            else:
                samples = load_shadow_reflections(conn, since_days=args.since_days or None)
        finally:
            conn.close()
        pack = benchmark(samples, fee_bps=args.fee_bps, gate=args.gate)
        pack["source"] = args.source
        pack["throughput"] = tp

    print(json.dumps(pack, ensure_ascii=False, indent=2) if args.json else render(pack))
    return 0


if __name__ == "__main__":
    sys.exit(main())
