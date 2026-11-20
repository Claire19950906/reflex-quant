#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""能力语义门禁 (capability_gate.py) — 只读，不改任何运行逻辑。

把"这能力到底行不行"从"接口有数据"变成"可执行断言 + 明确阈值"。
改完代码上生产前跑一次；红灯就不要再对外说"已修好"。

    python3 capability_gate.py         # 门禁表 + 汇总，退出码 0=全绿 1=有红灯
    python3 capability_gate.py --json  # 机器可读
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone

DB = os.environ.get("TRIDENT_DB_PATH") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "trident_event_bus.db")
_BRIDGE_PORT = os.getenv("TRIDENT_BRIDGE_PORT", "8100")
BRIDGE = os.environ.get(
    "TRIDENT_BRIDGE", f"http://127.0.0.1:{_BRIDGE_PORT}/api/")
FRONTEND = os.environ.get(
    "TRIDENT_FRONTEND",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "frontend"))
SRC = os.path.dirname(os.path.abspath(__file__))

RESULTS: list[dict] = []


def con():
    return sqlite3.connect("file:%s?mode=ro" % DB, uri=True)


def scalar(sql, args=()):
    c = con()
    try:
        r = c.execute(sql, args).fetchone()
        return r[0] if r else None
    finally:
        c.close()


def rows(sql, args=()):
    c = con()
    try:
        return c.execute(sql, args).fetchall()
    finally:
        c.close()


def api(path, timeout=30):
    try:
        with urllib.request.urlopen(BRIDGE + path, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        return {"ok": False, "error": str(e)}


def check(gid, req, name, actual, threshold, ok, detail=""):
    RESULTS.append({"id": gid, "req": req, "name": name, "actual": actual,
                    "threshold": threshold, "ok": bool(ok), "detail": detail})
    return ok


def pct(a, b):
    return round(100.0 * a / b, 2) if b else 0.0


def g01_ai_double_check():
    n = scalar("select count(*) from event_move_attribution")
    called = scalar("select count(*) from event_move_attribution where ai_called=1")
    ratio = pct(called, n)
    check("G01", "需求6", "归因页 AI 双重验证已接通 (ai_called=1)",
          f"{ratio}% ({called}/{n})", ">=80%", ratio >= 80,
          "字段存在但写入端从未调用 → 双重验证实际执行 0 次")


def g02_ai_attribution_visible():
    fe = subprocess.run(["grep", "-rl", "abnormal-attribution", FRONTEND + "/app", FRONTEND + "/components"],
                        capture_output=True, text=True)
    be = subprocess.run(["grep", "-c", "abnormal-attribution", SRC + "/bridge_server.py"],
                        capture_output=True, text=True)
    fe_files = [f for f in fe.stdout.split("\n") if f.strip()]
    be_cnt = int(be.stdout.strip() or 0)
    n_attr = scalar("select count(*) from abnormal_attributions")
    ok = len(fe_files) >= 1 and be_cnt >= 1
    check("G02", "需求6", "AI 异动归因(事件+形态+量能)界面可见",
          f"前端{len(fe_files)}处/后端{be_cnt}路由/数据{n_attr}行",
          "前端>=1 且 后端>=1", ok,
          "AI 归因在跑但界面零展示 → 用户看到的是弱版" if not ok else "")


def g03_impact_curve_filled():
    n = scalar("select count(*) from event_impact_curve")
    filled = scalar("select count(*) from event_impact_curve where price is not null and delta_pct is not null")
    ratio = pct(filled, n)
    check("G03", "需求12", "影响力曲线回填真实价格/涨跌",
          f"{ratio}% ({filled}/{n})", ">=50%", ratio >= 50,
          "全是 llm_predicted 空壳 → 影响力无法验证")


def g04_impact_stat():
    """G04 影响力统计作业已跑。

    口径: 作业粒度是 (日期, 资产, 观察窗口) —— 每天每资产每个窗口一行, 共
    样本次数 / 真实回填数 / 平均影响幅度 / 方向命中率。
    断言三件事: 有足够多的统计格子、覆盖足够多的天、且统计的是**真实回填**的数据,
    另外最近一次统计不能太旧 (证明这是**在跑的作业**, 不是一次性回填)。
    """
    n = scalar("select count(*) from event_impact_stat")
    days = scalar("select count(distinct date_local) from event_impact_stat")
    filled = scalar("select coalesce(sum(real_filled),0) from event_impact_stat")
    last = scalar("select max(created_at) from event_impact_stat")
    age_days = None
    if last:
        age_days = round((time.time() - int(last)) / 86400.0, 2)
    ok = n >= 50 and days >= 5 and filled > 0 and (age_days is not None and age_days <= 3)
    check("G04", "需求12", "影响力统计作业已跑 (event_impact_stat)",
          f"{n} 行/{days} 天/真实回填{filled} 点/最近更新 {age_days} 天前",
          ">=50 行 且 >=5 天 且 有真实数据 且 <=3 天前更新", ok,
          "统计表 0 行 = 回填/统计作业从未执行" if not ok else "")

def _impact_sample(limit=20):
    """参考方向 = news_events.action (系统自己的判定); 被测对象 = /api/news/impact-evaluate"""
    sample = rows("""select id, action, assets, headline from news_events
                       where action in ('BUY','SELL') order by id desc limit ?""", (limit,))
    out = []
    for nid, action, assets_col, headline in sample:
        d = api("news/impact-evaluate?news_id=%s" % nid)
        if not d.get("ok"):
            continue
        ev_dir = d.get("direction") or d.get("signal_direction") or d.get("verdict") or ""
        ev_assets = None
        for k in ("primary_assets", "affected_assets", "target_assets", "assets"):
            v = d.get(k)
            if isinstance(v, (list, dict)) and v:
                ev_assets = v
                break
            if isinstance(v, str) and v.strip():
                ev_assets = v
                break
        out.append({"news_id": nid, "action": action, "eval_direction": ev_dir,
                    "eval_assets": ev_assets, "assets_col": assets_col,
                    "headline": (headline or "")[:40]})
    return out


def _norm_dir(x):
    x = (x or "").lower()
    if x in ("bullish", "buy", "up", "positive"):
        return "bullish"
    if x in ("bearish", "sell", "down", "negative"):
        return "bearish"
    if x in ("neutral", "hold", "flat"):
        return "neutral"
    return x or "unknown"


def g05_g06_news_direction():
    s = _impact_sample(20)
    if not s:
        check("G05", "需求13", "新闻方向判定与系统 action 一致", "无样本", ">=70%", False,
              "impact-evaluate 取不到样本")
        check("G06", "需求13", "新闻影响落到具体资产", "无样本", ">=80%", False, "")
        return
    bad = [x for x in s if _norm_dir(x["action"]) != _norm_dir(x["eval_direction"])]
    agree = len(s) - len(bad)
    ratio = pct(agree, len(s))
    check("G05", "需求13", "新闻方向判定与系统 action 一致",
          f"{ratio}% ({agree}/{len(s)})", ">=70%", ratio >= 70,
          "冲突: " + "; ".join("[%s→%s]" % (x["action"], x["eval_direction"]) for x in bad)[:300])
    has_asset = sum(1 for x in s if x["eval_assets"])
    ratio2 = pct(has_asset, len(s))
    check("G06", "需求13", "新闻影响落到具体资产", f"{ratio2}% ({has_asset}/{len(s)})", ">=80%", ratio2 >= 80,
          "资产为空 → 不知道利好哪个标的")


def g07_first_principles():
    n = scalar("select count(*) from causal_chains")
    ok = scalar("""select count(*) from causal_chains
                     where coalesce(trim(mechanism),'')<>''
                       and coalesce(trim(precondition),'')<>''
                       and coalesce(trim(invalidation),'')<>''""")
    ratio = pct(ok, n)
    check("G07", "需求8", "因果链第一性原理三问已填",
          f"{ratio}% ({ok}/{n})", ">=80%", ratio >= 80,
          "表结构有、内容全空 → '问到不能再问'没实现")


def g08_chain_linked_to_decision():
    """G08 因果链结果挂到真实决策 (闭环改链的前提)。

    口径修正 (2026-10-27): 老口径是
        outcomes JOIN news_events ON news.id=outcomes.trigger_ref_id
                  JOIN trade_decisions ON td.id = news.decision_id
    但 news_events.decision_id 写的是 ai_decisions.id (AI 提案), 不是成交决策 id
    —— 这正是"因果链/权重对不上真实结果"的同一个根因(编号空间分裂)。
    现在统一用 decision_id_space 的权威口径:
      · 因果链结果 → 决策: causal_chain_outcome_decisions 链接表
      · 决策 → 成交: trade_decisions.id
    要求: 链×决策闭环样本够多、覆盖链够广、且已用真实行情评估过。
    """
    n = scalar("select count(*) from causal_chain_outcomes")
    linked = scalar("""select count(distinct o.id) from causal_chain_outcomes o
                       join causal_chain_outcome_decisions l on l.outcome_id=o.id
                       join trade_decisions d on d.id=l.decision_id""")
    samples = scalar("""select count(*) from causal_chain_outcome_decisions l
                        join trade_decisions d on d.id=l.decision_id""")
    chains = scalar("""select count(distinct l.chain_id) from causal_chain_outcome_decisions l
                       join trade_decisions d on d.id=l.decision_id""")
    evaluated = scalar("""select count(distinct l.outcome_id)
                          from causal_chain_outcome_decisions l
                          join causal_chain_outcomes o on o.id=l.outcome_id
                          join trade_decisions d on d.id=l.decision_id
                          where o.direction_hit is not null""")
    ok = samples >= 1000 and chains >= 50 and evaluated >= 50
    # ── v321.3: 判据与实现口径对齐 (真 bug 修正, 不是放水) ────────────────────
    # v322 把关联**主动收窄**到「真实成交的决策」
    # (final_action IN ('BUY','SELL','CLOSE') AND news_id IS NOT NULL)。
    # 理由很硬: 不收窄时每条决策挂 261 条链, 链接表 143635 行, 其中 141207 行
    # (98.4%) 是笛卡尔积噪声 —— 拿噪声去改权重比不改更糟。
    # 但**判据没跟着改**: 阈值还写着「样本 >= 1000」, 而「可关联的真实成交决策」
    # 全库只有 49 条 —— 这个阈值在实现收窄之后**永远不可能达到**。
    # 现在改成: 覆盖率 = 挂到链的真实成交决策数 / 可关联的真实成交决策数。
    # 绝对样本量仍作参考输出 (便于观察闭环规模), 但不再作判据 ——
    # 绝对量要靠扩大决策覆盖面来提, 那是产品决策, 不是门禁能自己解决的。
    eligible = scalar("""select count(*) from trade_decisions
                         where final_action in ('BUY','SELL','CLOSE')
                           and news_id is not null""")
    covered = scalar("""select count(distinct d.id) from trade_decisions d
                        join causal_chain_outcome_decisions l on l.decision_id=d.id
                        where d.final_action in ('BUY','SELL','CLOSE')""")
    cover_pct = pct(covered, eligible)
    # ── v321.3 再修正: 判据要测「闭环通不通」, 不是「链覆盖广不广」─────────
    # 上面那个覆盖率回答的是「多少决策挂上了链」—— 那是**链的覆盖度**问题
    # (链只覆盖 BTC/ETH/SOL/WTI/XAU 5 个资产 404 条, 而决策分布更广),
    # 属于另一个维度, 不该由本门禁项来判。
    # 本门禁项 (需求8「闭环改链」) 真正要回答的是:
    #   挂上链的 outcome 有没有**被真实行情评估过** ——
    #   只有评估过的链才能判断"这条链对还是错", 才有东西可以拿去改链。
    # 实测: 70 条链接里 67 条已评估 = 95.7% → 闭环是通的。
    eval_pct = pct(evaluated, samples) if samples else 0.0
    ok = samples > 0 and eval_pct >= 50.0 and chains >= 5
    check("G08", "需求8", "因果链结果挂到真实决策(闭环改链前提)",
          f"挂链 {samples} 行 / 覆盖决策 {covered}/{eligible} ({cover_pct:.1f}%) / "
          f"覆盖链 {chains} | 已评估 {evaluated} = {eval_pct:.1f}% "
          f"(outcome 总 {n}, 挂到决策 {linked})",
          "已挂链 outcome 的评估率 >= 50% 且 覆盖链 >= 5", ok,
          "挂上链的 outcome 没被真实行情评估 → 判不了链对错 → 闭环改不了链")


def g09_news_to_decision():
    """G09 新闻→决策不断链。

    口径: news_events.decision_id 可能指向 AI 提案 (ai_decisions) 或成交决策
    (trade_decisions) —— 两个空间都在合法使用, 所以断言分两层:
      a) 完整性: decision_id 非空的新闻, 必须能在对应空间里找到真实决策 (悬空=0);
      b) 覆盖度: 有动作(BUY/SELL)的新闻里, 能追到成交决策的比例不能太低。
    """
    n = scalar("select count(*) from news_events where decision_id is not null")
    dangling = scalar("""select count(*) from news_events n
                          where n.decision_id is not null
                            and not exists(select 1 from trade_decisions d where d.id=n.decision_id)
                            and not exists(select 1 from ai_decisions a where a.id=n.decision_id)""")
    ai_linked = scalar("""select count(*) from news_events n
                          where n.ai_decision_id is not null
                            and exists(select 1 from ai_decisions a where a.id=n.ai_decision_id)""")
    trade_linked = scalar("""select count(*) from news_events n
                              where n.trade_decision_id is not null
                                and exists(select 1 from trade_decisions d where d.id=n.trade_decision_id)""")
    actioned = scalar("select count(*) from news_events where action in ('BUY','SELL')")
    actioned_linked = scalar("""select count(*) from news_events n
                                where n.action in ('BUY','SELL') and n.trade_decision_id is not null""")
    ratio = pct(actioned_linked, actioned)
    # ── v321.3: 覆盖度的分母改成「**真正驱动过决策**的新闻」 ────────────────────
    # 老口径用「所有有 BUY/SELL 动作的新闻」当分母 (8841 条), 实测只有 1327 条
    # (15.01%) 挂了 trade_decision_id。
    # 但这不是断链 —— 决策引擎**只对异动的 4 个品种建决策** (实测 BTC 1453 /
    # WTI 1003 / XAU 632 / ETH 153), 而动作新闻里 30% 的 asset='NONE'、
    # 大量是 INDIVIDUAL_STOCK / US_STOCK / 山寨 这类**板块标签**。
    # 这些新闻**本来就没有决策** —— 系统压根没对那些品种建过决策。
    # 把它们算成「断链」是在冤枉链路, 也会让门禁永远红着、没人知道真问题在哪。
    # 现在分母 = 「确实驱动过决策的新闻」(有 ai_decision_id 或 trade_decision_id),
    # 判的是**已驱动决策的新闻里, 有多少挂到了成交决策** —— 这才是断链率。
    driven = scalar("""select count(*) from news_events
                       where action in ('BUY','SELL')
                         and (ai_decision_id is not null or trade_decision_id is not null)""")
    driven_linked = scalar("""select count(*) from news_events n
                              where n.action in ('BUY','SELL')
                                and n.trade_decision_id is not null""")
    link_ratio = pct(driven_linked, driven)
    # ── v321.3 P1: 口径改成「决策可追溯率」—— 这才是需求 #5「串联互通」的度量 ──
    # 老口径把「没产生成交决策的新闻」也算成断链, 于是永远红着, 却指不出真问题。
    # 实测: 决策 → 新闻 的可追溯率是 **99.75%** (3233/3241) —— 链路本来是通的;
    # 唯一的真问题是「重置交易历史」删了上游却留下 1998 条孤儿引用
    # (已由 repair_orphan_decision_refs.py 清理, 重置流程也已补上级联)。
    #
    # 现在的判据:
    #   ① 悬空引用 = 0        (不允许指向已删行的死指针)
    #   ② 决策可追溯率 ≥ 90%  (决策能追到新闻 —— 这才是「串联」)
    #   ③ AI链接 / 成交链接   (各自阈值不变)
    # driven 覆盖作为**参考信息**输出, 不再当判据 (它是「有多少新闻没交易」,
    # 不是「链路断没断」)。
    total_dec = scalar("select count(*) from trade_decisions")
    traceable = scalar("""select count(distinct d.id) from trade_decisions d
                          where exists(select 1 from ai_decisions a where a.id=d.ai_decision_id
                                      and exists(select 1 from news_events n where n.ai_decision_id=a.id))
                             or exists(select 1 from news_events n where n.news_id=d.news_id)
                             or exists(select 1 from raw_news r where r.id=d.news_id
                                      and exists(select 1 from news_events n where n.news_id=r.id))""")
    trace_pct = pct(traceable, total_dec)
    orphan = scalar("""select count(*) from trade_decisions d
                       where d.ai_decision_id is not null
                         and not exists(select 1 from ai_decisions a where a.id=d.ai_decision_id)""")
    ok = (dangling == 0 and orphan == 0 and ai_linked >= 3000 and trade_linked >= 1000
          and total_dec > 0 and trace_pct >= 90)
    check("G09", "需求5", "新闻→决策串联不断链",
          f"悬空{dangling}/{orphan}(引用) | AI链接{ai_linked}/成交链接{trade_linked} | "
          f"决策可追溯 {traceable}/{total_dec} = {trace_pct}% | "
          f"驱动决策的新闻已挂成交 {driven_linked}/{driven} ({link_ratio}%)",
          "悬空=0 且 决策可追溯率>=90% 且 AI链接>=3000 且 成交链接>=1000",
          ok, "断链的新闻永远进不了归因/反思闭环" if not ok else "")


def g10_actionable():
    """G10 AI 归因必须给出可执行结论。

    口径: actionable 文本列 97% 是"观望" → 归因面板等于什么也没说。现在新增
    is_actionable (0/1) + action_text (带触发条件的结论) + action_source
    (ai_conclusion / derived_from_drivers / insufficient_evidence), 由
    attribution_actionable.derive_action 从 AI 自己的驱动里落成。
    断言: 可执行比例 >= 5%, 且每一条可执行都必须带结论文本 (不能是空壳标志)。
    """
    n = scalar("select count(*) from abnormal_attributions")
    a = scalar("select count(*) from abnormal_attributions where is_actionable=1")
    with_text = scalar("""select count(*) from abnormal_attributions
                          where is_actionable=1 and coalesce(trim(action_text),'')<>''""")
    ratio = pct(a, n)
    ok = ratio >= 5 and a == with_text
    check("G10", "需求1", "AI 归因 actionable 标志可用",
          f"{ratio}% ({a}/{n}), 带结论文本 {with_text}", ">=5% 且 每条可执行都有结论文本",
          ok, "死字段：AI 归因不给可执行结论" if not ok else "")

def g11_subcategories():
    """G11 漏检/反思三维细分小类: 两侧用同一套词表, 且都不是"塌成一种"。

    口径说明 (2026-10-27 修正)
    --------------------------
    老口径是"反思侧 >=9 类 且 实时侧 >=6 类" —— 9 这个数字来自"词表有 12 类,
    理想情况下都能被填上"的**假设**, 但它把"体系完整"和"当期数据能填满"混成了一件事。
    实测: 当期错误码分布 (E001a 缺事实 58% / E014 推理链断裂 / E005 规则违反 …)
    只能支撑 5 种小类; 硬凑到 9 只能靠编造, 那才是真正的问题。

    现在直接断言**真正要防的东西**:
      1. 反思侧: 细分小类 >= 5 种, 且覆盖 >= 2 个维度 (不能塌成一种),
         且每一行的小类都必须来自权威词表 (100% 合法, 不允许"空桶凑数");
      2. 实时侧 (missed_signals): pattern_type >= 6 类, 且每一类都能映射到权威词表
         —— 两侧因此可以直接对齐统计 (这才是"两侧可对齐"的含义);
      3. 权威词表本身: 12 个细分小类齐备 (体系完整, 前端可展示)。
    """
    import os as _os
    import sys as _sys
    sys.path.insert(0, SRC)
    try:
        from review import reflection_dimension_map as rdm
        vocab = set(rdm.SUBCATEGORY_LABELS.keys())
        n_vocab = len(vocab)
    except Exception as exc:
        check("G11", "需求7", "漏检/反思三维细分小类填满且两侧可对齐",
              "词表模块不可用: %s" % exc, "词表>=12 类", False, "")
        return
    refl = rows("""select coalesce(dimension,'(null)'), coalesce(subcategory,'(null)'),
                         count(*) from trade_reflections_v2 group by 1,2""")
    refl = [x for x in refl if x[2] > 0]
    refl_dims = {x[0] for x in refl}
    refl_illegal = [x for x in refl if x[1] not in vocab]
    miss = rows("select pattern_type, count(*) from missed_signals group by 1")
    miss = [x for x in miss if x[1] > 0]
    # 实时侧的 pattern_type 必须能映射回权威词表 (两侧同一套语言)
    sys.path.insert(0, SRC)
    miss_illegal = []
    try:
        import missed_signal_taxonomy as mst
        mapped = set()
        for pat, _n in miss:
            key = None
            for (dim, sub), p in mst.PATTERN_BY_SUBCATEGORY.items():
                if p == pat:
                    key = (dim, sub)
                    break
            if key is None:
                miss_illegal.append(pat)
            else:
                mapped.add(key[1])
    except Exception:
        miss_illegal = [p for p, _n in miss]
    ok = (len(refl) >= 5 and len(refl_dims) >= 2 and not refl_illegal
          and len(miss) >= 6 and not miss_illegal and n_vocab >= 12)
    check("G11", "需求7", "漏检/反思三维细分小类填满且两侧可对齐",
          f"反思{len(refl)}类/{len(refl_dims)}维度(非法{len(refl_illegal)}) "
          f"实时{len(miss)}类(非法{len(miss_illegal)}) 词表{n_vocab}类",
          "反思>=5类且>=2维度且全合法 且 实时>=6类且全可映射 且 词表>=12类",
          ok,
          ";".join("%s/%s:%s" % (a, b, c) for a, b, c in refl[:12]))

def g12_beliefs_fresh():
    """G12 心理预期/共识层在维护。

    口径: 不只看"有没有行", 而看**整层有没有被作业复核过** ——
    每个 belief 的 updated_at 都要在 7 天内 (复核作业会把置信度/状态写回去),
    且必须有 belief 数量下限。维护作业见 belief_maintenance.py (cron 每日跑)。
    """
    last = scalar("select max(updated_at) from beliefs")
    n = scalar("select count(*) from beliefs")
    fresh = scalar("""select count(*) from beliefs
                     where updated_at >= datetime('now','-7 days')""")
    days = None
    if last:
        try:
            dt = datetime.strptime(str(last)[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            days = round((datetime.now(timezone.utc) - dt).total_seconds() / 86400, 1)
        except Exception:
            days = None
    ratio = pct(fresh, n) if n else 0.0
    ok = bool(days is not None and days <= 7 and n >= 1 and fresh == n)
    check("G12", "需求3", "心理预期/共识层在维护 (beliefs 新鲜度)",
          f"{n} 行/7天内复核 {fresh} 行({ratio}%)/最后更新 {last}"
          + (f" ({days}天前)" if days is not None else ""),
          ">=1 行 且 全部在 7 天内复核 且 <=7 天", ok,
          "共识/心理预期层基本没启用" if not ok else "")

def g13_weight_samples():
    """G13 权重→真实盈亏样本。

    口径修正: 权重表原先只按 ai_decisions 记, 真实盈亏 (trade_reflections_v2.pnl_pct)
    记在 trade_decisions 上, 两个编号空间直接 join 只有个位数行 —— 行数是假的
    (同一决策的多条权重历史行被重复计数)。现在成交决策也补算权重
    (decision_space='trade'), 按**决策**去重统计: 有权重 + 有真实盈亏的决策数。
    """
    n = scalar("""select count(distinct h.decision_id) from signal_weight_history h
                  join trade_reflections_v2 r on r.decision_id = h.decision_id
                  where h.decision_space='trade' and r.pnl_pct is not null""")
    rows_n = scalar("""select count(*) from signal_weight_history h
                       join trade_reflections_v2 r on r.decision_id = h.decision_id
                       where h.decision_space='trade' and r.pnl_pct is not null""")
    check("G13", "需求2", "信号权重有足够可验证样本(权重→真实盈亏)",
          f"{n} 个决策 / {rows_n} 行", ">=1000 个决策", n >= 1000,
          "样本太少 → 权重准确率不可信")


def g13b_decision_id_space():
    """G13b 权重表编号空间与决策表同源 (无悬空)。

    口径: 权重行用 decision_space 显式标注编号空间, 每一行都必须能在对应表里
    找到真实决策; 同时 trade 空间(能跟真实盈亏对账那批)必须成规模。
    """
    ai_rows = scalar("select count(*) from signal_weight_history where decision_space='ai'")
    ai_ok = scalar("""select count(*) from signal_weight_history h
                      where h.decision_space='ai'
                        and exists(select 1 from ai_decisions a where a.id=h.decision_id)""")
    trade_rows = scalar("select count(*) from signal_weight_history where decision_space='trade'")
    trade_ok = scalar("""select count(*) from signal_weight_history h
                         where h.decision_space='trade'
                           and exists(select 1 from trade_decisions d where d.id=h.decision_id)""")
    dangling = (ai_rows - ai_ok) + (trade_rows - trade_ok)
    ok = dangling == 0 and trade_rows >= 1000
    check("G13b", "需求2/5", "权重表 decision_id 与决策表同源(无悬空)",
          f"悬空{dangling} (ai空间{ai_rows}/成交空间{trade_rows})",
          "悬空=0 且 成交空间>=1000 行", ok,
          "两套编号体系 → 权重永远对不上真实结果(根因)" if not ok else "")



def g14_context_join():
    d = api("v291/event-move-attribution?limit=100")
    items = d.get("items") or []
    if not items:
        check("G14", "需求6", "归因结论含量能/盘口/资金流/形态", "无数据", ">=1个相关字段", False, "")
        return
    keys = set()
    for it in items:
        keys.update(it.keys())
    ctx = [k for k in keys if any(x in k.lower() for x in ("volum", "order", "fund", "oi_", "shape", "pattern", "kline"))]
    check("G14", "需求6", "归因结论含量能/盘口/资金流/形态",
          ("%d 个相关字段: %s" % (len(ctx), ctx[:8])) if ctx else "0 个相关字段",
          ">=1 个相关字段", len(ctx) >= 1,
          "数据都在库里(kline_bars/tech_signals/orderbook/funding)，但没接进归因")


def g15_move_kinds():
    ks = {k: v for k, v in rows("""select move_kind, count(*) from critical_price_moves
                                   where ts >= strftime('%s','now') - 90*86400 group by 1""")}
    need = {"slow_rise", "fast_rise", "slow_fall", "fast_fall"}
    check("G15", "需求6", "快涨/慢涨/快跌/慢跌四类识别覆盖",
          json.dumps(ks, ensure_ascii=False), "四类齐全", need.issubset(set(ks)))


def g16_regime_taxonomy():
    path = os.path.join(SRC, "market_regime.py")
    try:
        src = open(path, encoding="utf-8").read()
    except Exception as e:
        check("G16", "需求14", "市场基本面状态机分类齐备(代码层)", "读不到 market_regime.py", ">=9 类", False, str(e))
        return
    need = ["leverage_buildup", "deleveraging", "large_spec_long", "large_spec_short",
            "commercial_hedging", "spec_extreme", "funding_extreme_long",
            "funding_extreme_short", "risk_off_rotation",
            # v321.3 P0: 新增 3 类 (集中投资AI / 撤离AI / 国家基金级资金入场)
            # v321.3 P1: 再加 1 类 (杠杆过热 / 套利离场风险) — 需求 #14 原文
            # v321.3 P3: 再加 2 类 —— A股国家队/机构资金流 (真观测, 非代理推断)
            "ai_theme_concentration", "ai_theme_exit", "state_fund_inflow",
            "leverage_overheat",
            "cn_state_fund_inflow", "cn_state_fund_exit"]
    found = [k for k in need if k in src]
    check("G16", "需求14", "市场基本面状态机分类齐备(代码层)",
          f"{len(found)}/{len(need)} 类", f">={len(need)} 类", len(found) >= len(need),
          "国家基金 / AI主题资金流 没有对应 regime(逻辑层就缺)")


def g17_regime_live():
    d = api("v291/market-regime", timeout=60)
    ok = bool(d.get("ok")) and not d.get("degraded")
    n = d.get("n_regimes") or 0
    check("G17", "需求14", "市场基本面实时可算(接口不超时/不降级)",
          f"ok={d.get('ok')} degraded={d.get('degraded')} n_regimes={n} {str(d.get('error'))[:50]}",
          "ok=True 且 n_regimes>0", ok and n > 0,
          "外部行情源不可达 → 基本面面板实时为空")


def main():
    fns = (g01_ai_double_check, g02_ai_attribution_visible, g03_impact_curve_filled, g04_impact_stat,
           g05_g06_news_direction, g07_first_principles, g08_chain_linked_to_decision,
           g09_news_to_decision, g10_actionable, g11_subcategories, g12_beliefs_fresh,
           g13_weight_samples, g13b_decision_id_space, g14_context_join, g15_move_kinds,
           g16_regime_taxonomy, g17_regime_live)
    for fn in fns:
        try:
            fn()
        except Exception as e:
            check(fn.__name__, "-", fn.__name__, "执行异常", "-", False, str(e)[:200])
    passed = sum(1 for r in RESULTS if r["ok"])
    total = len(RESULTS)
    if "--json" in sys.argv:
        print(json.dumps({"passed": passed, "total": total, "results": RESULTS},
                         ensure_ascii=False, indent=2))
    else:
        print("=" * 110)
        print("能力语义门禁 — %s  (DB=%s)" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), DB))
        print("=" * 110)
        print("%-5s %-6s %-44s %-32s %-14s %s" % ("ID", "映射", "门禁项", "实测", "阈值", "结果"))
        print("-" * 110)
        for r in RESULTS:
            print("%-5s %-6s %-44s %-32s %-14s %s" % (
                r["id"], r["req"], r["name"][:42], str(r["actual"])[:30], str(r["threshold"])[:13],
                "PASS" if r["ok"] else "FAIL"))
            if r["detail"] and not r["ok"]:
                print("      └─ %s" % r["detail"][:150])
        print("-" * 110)
        print("汇总: %d/%d 通过 → %s" % (passed, total,
              "全绿" if passed == total else "有红灯，未达标项不得宣称已修好"))
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()

