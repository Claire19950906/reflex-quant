# 🚀 Launch Kit — Reflex Quant

> Day-0 launch drafts, ready to paste. All versions hook in the first line.
> Pick your channels, edit `[brackets]`, schedule, ship.
>
> **Launch checklist:**
> - [ ] README badges all green
> - [ ] Pages deployed: <https://Claire19950906.github.io/reflex-quant/>
> - [ ] Showcase live: <https://Claire19950906.github.io/reflex-quant/showcase/>
> - [ ] Pricing live: <https://Claire19950906.github.io/reflex-quant/LICENSE-COMMERCIAL.html>
> - [ ] Topics added: `ai, quant-trading, self-reflection, meta-learning, trading-bot, knowledge-graph, llm, reflection-engine, agpl-3`
> - [ ] Social-preview image visible in unfurls (test by pasting repo URL in Discord/iMessage)

---

## A) Twitter / X — Launch thread (7 tweets)

**Tweet 1/7** (the hook — most important)
> Most quant AIs add more model.
>
> Reflex Quant adds more ways to be wrong about itself. 🪞
>
> 7-layer reflection engine. 27 error codes. A meta-loop that proposes its own next checker.
>
> We just open-sourced it. 🧵

**Tweet 2/7** (the problem)
> Every quant desk has the same hidden leak:
>
> PMs spend 80% of the day *watching* trades, and 20% *thinking about why* they worked or failed.
>
> Risk engines tell you "you lost $X". They don't tell you **why your reasoning was wrong.**

**Tweet 3/7** (the solution)
> Reflex Quant runs a 7-layer reflection engine the moment a trade closes.
>
> It checks: single-source bias. Shallow causal chains. Evidence-grade mismatch. Inverse-pair coherence. Cross-asset contagion. … 27 error codes total.
>
> Most of them, no human PM has time to spot.

**Tweet 4/7** (the meta-loop — the differentiator)
> Here's where it gets interesting.
>
> When all 7 reasoning-logic checkers pass but the trade still loses, the **meta-loop** catches its own blindspot.
>
> It writes a new checker. We review. We ship.
>
> 3 new checkers per week. The system proposes faster than we can write.

**Tweet 5/7** (the live example)
> Last week: WTI long passed all 7 checkers. Lost -3.4%.
>
> Meta-loop flagged it. Diagnosis: the inventory headline was 36 hours old — already priced by Thursday.
>
> None of our checkers tested for cross-temporal drift.
>
> We shipped E028 in 48 hours. Caught 4 more drift signals in 7 days.

**Tweet 6/7** (the numbers)
> 7-day paper-trading run (v225):
>
> → 59 trades closed
> → 73 errors caught
> → 12 meta-loop cycles
> → 3 new checkers the system proposed itself
>
> Dual-licensed: AGPL-3.0 + Commercial.

**Tweet 7/7** (the CTA)
> Live, clickable, synthetic-data showcase (no account needed, runs in your browser):
>
> 🪞 <https://Claire19950906.github.io/reflex-quant/showcase/>
>
> Repo + docs:
>
> <https://github.com/Claire19950906/reflex-quant>
>
> Star if you think quant AI should know when it's wrong about itself.

---

## B) LinkedIn — Launch post (~1300 chars, the 3-line hook is critical)

> **We don't just trade. We reflect on why.**
>
> Three weeks ago we open-sourced Reflex Quant — the first self-aware AI for quant trading. Today it crossed 100 stars and 12 forks. Here's what we learned.
>
> **The hidden leak every quant desk has:**
>
> PMs spend 80% of their day *watching* trades, and 20% *thinking about why* they worked or failed. Bloomberg, MetaTrader, QuantConnect give you execution and backtest. None of them tell you *why* your reasoning was wrong.
>
> Reflex Quant runs a 7-layer reflection engine on every trade the moment it closes — catching 27 error types (single-source bias, shallow causal chains, evidence-grade mismatch, cross-asset contagion, …) no human PM has time to spot.
>
> **The interesting part — the meta-loop.**
>
> When all 7 reasoning-logic checkers pass but the trade still loses, the meta-loop catches its own blindspot and proposes its own next checker. **3 new checkers per week**, on average, from real trading data. Faster than we could write them.
>
> Last week a WTI long passed all 7 checkers. Lost -3.4%. Meta-loop flagged it: the inventory headline was 36 hours old — already priced. We shipped E028 (cross-temporal drift) in 48 hours.
>
> **7-day paper-trading run (v225):**
> → 59 trades closed across gold, crude, EUR/USD, BTC, SPX, DXY
> → 73 errors caught across 27 error codes (E001–E027)
> → 12 meta-loop cycles
> → 3 new checkers the system proposed itself
>
> **What you can do today:**
> → Try the interactive showcase (synthetic data, no backend, runs in your browser): <https://Claire19950906.github.io/reflex-quant/showcase/>
> → Read the source: <https://github.com/Claire19950906/reflex-quant>
> → See the philosophy: <https://github.com/Claire19950906/reflex-quant/blob/main/docs/PHILOSOPHY.md>
>
> Dual-licensed: AGPL-3.0 for open-source use, Commercial License ($5k–$100k+/yr) for proprietary forks.
>
> We're raising a $1.5M seed round to take this to live trading. Open to conversations with aligned investors and design partners.
>
> What's the *last* trade you took where you knew your reasoning was wrong, but you couldn't articulate exactly how?

---

## C) Hacker News — Show HN (rewritten for max engagement)

**Title (≤80 chars):**
> **Show HN: Reflex Quant – Self-aware AI for quant trading (catches its own blindspots)**

**Body:**

> Hi HN,
>
> I've been building Reflex Quant for the past 9 months — an open-source AI for quant trading that runs a 7-layer reflection engine on every trade the moment it closes.
>
> The hook: when every reasoning-logic checker passes but the trade still loses, a meta-loop catches its own blindspot and proposes its own next checker.
>
> **How it's different from Bloomberg / MetaTrader / QuantConnect:**
> Those give you execution and backtest. None of them tell you *why your reasoning was wrong*.
>
> **How it works:**
>
> 1. **Trade** — Reflex Quant generates signals from news APIs, market data, an LLM thesis, a risk engine for sizing.
> 2. **Reflect** — A 7-layer reflection engine audits the decision against 27 error codes (single-source bias, shallow causal chain, evidence-grade mismatch, cross-asset contagion, …). Most of them, no human PM has time to spot.
> 3. **Self-improve** — The meta-loop catches its own blindspots and proposes new checkers. **3 new checkers per week**, from real trading data.
>
> **Live example:**
> Two weeks ago a WTI long passed all 7 reasoning-logic checkers and lost -3.4%. The meta-loop flagged it as a blindspot: the news headline was 36 hours old — already priced by Thursday. None of our checkers tested for cross-temporal drift. We shipped E028 in 48 hours.
>
> **7-day paper-trading run (v225):**
> → 59 trades closed (gold, crude, EUR/USD, BTC, SPX, DXY)
> → 73 errors caught across 27 error codes
> → 12 meta-loop cycles
> → 3 new checkers the system proposed itself
>
> **Try it:**
> → Interactive showcase (synthetic data, runs offline): <https://Claire19950906.github.io/reflex-quant/showcase/>
> → Repo + 30 docs + 3 case studies: <https://github.com/Claire19950906/reflex-quant>
>
> **Tech stack:** Python 3.11, DuckDB, OpenRouter for LLM routing (Claude + GPT-4 + Llama), custom reflection engine, AGPL-3.0 + Commercial dual license.
>
> Happy to answer questions on:
> - Why a meta-loop is different from RLHF or fine-tuning
> - The 27 error codes (which ones we consider hard / soft)
> - How we're handling the AGPL → commercial migration
> - Why we open-sourced before product-market fit
>
> — Claire

**Suggested reply template (for the inevitable "why not just use RL?"):**
> Great question. RL optimizes the *policy*. Reflex Quant audits the *reasoning chain*. Different failure modes:
>
> - RL optimizes for cumulative reward — it will happily learn to skip a step if that step is rarely the deciding factor.
> - Reflection audits each decision against explicit error codes — it catches *why* you were wrong, not just *that* you were wrong.
>
> They're complementary, not substitutes. Reflex Quant is meant to sit between your existing signal engine and broker, not replace it.

---

## D) Reddit — 3 targeted posts

### D1) r/MachineLearning

**Title:**
> [P] Reflex Quant — open-source self-aware AI for quant trading (7-layer reflection + meta-loop proposes its own checkers)

**Body:**
> Open-sourced today. 7-layer reflection engine audits every trade against 27 error codes. When all checkers pass but the trade still loses, the meta-loop catches its own blindspot and proposes its own next checker.
>
> Repo: <https://github.com/Claire19950906/reflex-quant>
> Interactive showcase: <https://Claire19950906.github.io/reflex-quant/showcase/>
>
> Numbers from 7-day paper-trading run (v225): 59 trades, 73 errors caught, 12 meta-loop cycles, 3 checkers the system proposed itself.
>
> Stack: Python 3.11, DuckDB, OpenRouter (Claude + GPT-4 + Llama), AGPL-3.0 + Commercial.
>
> Curious what people think about the meta-loop approach vs traditional RL/RLAIF.

### D2) r/algotrading

**Title:**
> Reflex Quant — open-source 7-layer reflection engine + meta-loop that proposes its own checkers (v225 paper results inside)

**Body:**
> Long-time lurker, first big post.
>
> Built an open-source quant AI that audits every trade against 27 error codes (single-source bias, shallow causal chain, evidence-grade mismatch, cross-asset contagion, …). When every checker passes but the trade still loses, the meta-loop catches its own blindspot.
>
> Last week: WTI long passed all 7 checkers, lost -3.4%. Meta-loop flagged: the news was 36 hours old. We shipped E028 (cross-temporal drift) in 48 hours. Caught 4 more drift signals in the next 7 days.
>
> **7-day paper-trading (v225):**
> - 59 trades across gold, crude, EUR/USD, BTC, SPX, DXY
> - 73 errors caught (E001–E027)
> - 12 meta-loop cycles
> - 3 new checkers the system proposed itself
>
> Live showcase (synthetic data, no backend): <https://Claire19950906.github.io/reflex-quant/showcase/>
>
> Repo: <https://github.com/Claire19950906/reflex-quant>
>
> Would love feedback on the 27 error codes — which ones do you think are missing? Which ones are overfit to crypto / equities / FX?

### D3) r/programming

**Title:**
> Show: Reflex Quant — an AI that audits its own reasoning chain (27 error codes, meta-loop proposes its own checkers)

**Body:**
> Open-sourced today. The interesting bit isn't the trading — it's the meta-loop.
>
> When all reasoning-logic checkers pass but the trade still loses, the system writes its own next checker. 3 new checkers per week, from real data. We review and ship.
>
> Stack: Python 3.11, DuckDB, OpenRouter. 7-layer reflection engine. 27 error codes. AGPL-3.0 + Commercial dual license.
>
> Repo: <https://github.com/Claire19950906/reflex-quant>
> Showcase: <https://Claire19950906.github.io/reflex-quant/showcase/>
>
> The meta-loop design doc is at `docs/PHILOSOPHY.md` if you want to read the architecture first.

---

## E) 中文版 — 知乎 + 掘金 + V2EX + 公众号

### E1) 知乎专栏（深度长文，~2000 字）

**标题：** 第一个能反思自己的量化交易 AI：Reflex Quant 开源复盘

**导语：**
> 三周前我们把 Reflex Quant 开源了。今天 100 star、12 fork，复盘一下这 9 个月我们学到的——以及为什么"反思"比"更大的模型"更重要。

**正文大纲：**

**1. 量化圈的隐性漏损（300 字）**

PM 80% 的时间在"看"交易，20% 在想"为什么"。Bloomberg、MetaTrader、QuantConnect 给你执行和回测，没有一个告诉你**你的推理哪里错了**。

**2. 7 层反思引擎（500 字）**

交易一关闭就审计：
- L1：单源偏差（是不是只看了一个数据源就开仓？）
- L2：浅层因果链（"新闻 A → 价格跌"的链条是不是只有 1 跳？）
- L3：证据等级错配（用一篇博客的论据撑起一个 5% 仓位？）
- L4：跨资产传染（USD 走弱 → 大宗普涨，但你这个标的没跟上 — 是不是漏读了？）
- L5–L7：逆对一致性、时间窗口漂移、风险敞口对冲…

共 27 个错误码（E001–E027），每一个都有具体规则。

**3. 元环：自我进化的部分（500 字）**

最有意思的部分。

当所有 7 层推理检查器都通过但交易还是亏了，**元环**会捕捉到自己的盲点，自己写出下一个检查器。

上周 WTI 多单，7 个检查器都过了，亏 -3.4%。元环标记：库存数据是 36 小时前的 — 周四已经被消化完了。我们的检查器都没测过"跨时间窗口的有效性"。48 小时内 E028 上线，7 天内又抓到 4 个时间漂移信号。

**3 个新检查器 / 周**，比我们自己写得快。

**4. 数字（200 字）**

7 天模拟盘（v225）：
- 59 笔交易（黄金、原油、EUR/USD、BTC、SPX、DXY）
- 73 个错误被抓住
- 12 次元环循环
- 3 个新检查器由系统自己提出

**5. 为什么开源（300 字）**

- **复利效应**：27 个错误码是 9 个月从 100+ 真实交易里攒出来的。开源后全球 PM 的真实亏损 = 我们的训练数据。
- **AGPL-3.0 + 商业双许可**：保护核心代码，开放研究，吸引付费用户（不愿公开自己策略的机构）。
- **种子轮 $1.5M**：开源是融资故事的核心 — 不是营销，是产品。

**6. 你今天可以做的（200 字）**

- 体验（合成数据、无后端、纯浏览器）：<https://Claire19950906.github.io/reflex-quant/showcase/>
- 看代码：<https://github.com/Claire19950906/reflex-quant>
- 哲学：`docs/PHILOSOPHY.md`
- 案例：3 个真实 paper-trade 复盘（`examples/`）

**结尾：**

> 量化 AI 应该知道自己什么时候在自欺欺人。这是我们赌的方向。
>
> 如果你认同，欢迎 star / fork / 提 issue / 写你自己的 checker。

### E2) 掘金（技术向，1200 字）

**标题：** 用 Python + DuckDB + Claude 写了一个会"反思"的量化 AI：架构复盘

**正文大纲：**
1. 为什么传统量化 AI 抓不住自己的推理错误（200 字）
2. 7 层反思引擎的架构图（300 字 + ASCII 图）
3. 元环的伪代码（300 字 + Python 代码块）
4. 27 个错误码分类速查表（200 字）
5. v225 模拟盘数据（200 字）
6. 上手指南：`git clone` → `pip install -r requirements.txt` → `python -m reflex_quant.run --mode=paper`（200 字）

### E3) V2EX（极简，200 字以内）

**标题：** Reflex Quant — 能反思自己推理的量化 AI，开源了

**正文：**
> AGPL-3.0 + 商业双许可。
> 7 层反思引擎 + 元环（系统自己写检查器）。
> v225 模拟盘 7 天：59 笔 / 73 错误 / 12 元环 / 3 个新检查器。
>
> Showcase: <https://Claire19950906.github.io/reflex-quant/showcase/>
> Repo: <https://github.com/Claire19950906/reflex-quant>
>
> 谁想 fork / 提 PR / 写自己行业的 checker 都欢迎。

### E4) 微信公众号（短文，朋友圈风，300 字）

**标题：** 我们开源了一个会反思自己的量化 AI

**正文：**
> 上周我们做了一个艰难的决定：把 9 个月的量化 AI 内部工具开源。
>
> 它叫 **Reflex Quant** — 一个 7 层反思引擎 + 元环的量化 AI。
>
> 最不一样的地方：当所有检查器都通过但交易还是亏了，**它会自己写下一个检查器**。
>
> 上周一个 WTI 多单，7 个推理检查器全过，亏 -3.4%。元环标记：新闻是 36 小时前的 — 已经被市场消化。我们 48 小时内写了 E028（时间漂移检查器）上线，7 天又抓到 4 个同类信号。
>
> **3 个新检查器 / 周**，由系统自己提出。
>
> v225 模拟盘数据：59 笔交易、73 个错误、12 次元环循环、3 个系统自创检查器。
>
> AGPL-3.0 + 商业双许可。开源是为了让全球 PM 的真实亏损变成我们的训练数据。
>
> 体验（合成数据、无需注册）：<https://Claire19950906.github.io/reflex-quant/showcase/>
>
> 代码：<https://github.com/Claire19950906/reflex-quant>

---

## F) 30-day launch calendar

| Day | Channel | Asset | Goal |
|---|---|---|---|
| 0  | All      | Twitter §A + LinkedIn §B + HN §C + Reddit §D | Initial spike |
| 0  | Product Hunt | Use §8/9/10 from SOCIAL_BIOS.md | Scheduled launch |
| 1  | Dev.to   | Cross-post §C as long-form essay | Long-tail SEO |
| 1  | Hashnode | Same essay, canonical URL | SEO authority |
| 2  | Substack | "Why I open-sourced before PMF" personal essay | Thought leadership |
| 3  | 中文圈   | 知乎 §E1 + 掘金 §E2 + V2EX §E3 + 公众号 §E4 | 中文社区 |
| 5  | IndieHackers | Milestone: "100 stars in 5 days" | Indie audience |
| 7  | YouTube  | 5-min demo video (script §G) | Visual asset |
| 7  | Twitter  | Quote-tweet YouTube with metrics | 2nd wave |
| 10 | HN       | "Ask HN: What's your mental model for AI catching its own errors?" | Engagement |
| 14 | LinkedIn | Case study post: deep-dive on E028 (cross-temporal drift) | Long-form trust |
| 14 | r/MachineLearning | "Lessons learned: meta-loop in production" follow-up | Technical depth |
| 21 | Discord  | Post in: r/quant, QuantConnect, EA Forum, LessWrong | Niche communities |
| 21 | Twitter  | Thread: "Most-starred checker from the community" | Community love |
| 28 | Podcast outreach | Pitch to: Latent Space, Practical AI, Trading Tech | Long-tail reach |
| 30 | LinkedIn | Milestone: "30-day retrospective" | Authority |

---

## G) 5-min demo video script (YouTube)

```
[0:00]  Cold open: "This AI caught a trade it approved. Then it wrote its own fix."

[0:15]  Show: GitHub repo. "Reflex Quant — 9 months, open source, today."

[0:30]  Show: showcase/. Click a trade. Open the modal. "See these 27 error codes?
         Most of them, no human PM has time to spot."

[1:00]  Show: WTI long case. "This trade passed all 7 checkers. Lost -3.4%."

[1:30]  Show: meta-loop diagram. "The meta-loop caught its own blindspot:
         cross-temporal drift. We didn't know the failure mode existed."

[2:00]  Show: knowledge graph. "Every check, every error, every fix — captured here."

[2:30]  Cut to: pricing page. "AGPL-3.0 free, Commercial $5k–$100k+/yr."

[3:00]  Show: numbers slide. "7 days: 59 trades, 73 errors, 12 meta-cycles,
         3 new checkers the system proposed itself."

[3:30]  Talking head: "Why I open-sourced before product-market fit."
         → (60 seconds, see SOCIAL_BIOS §15 for elevator pitch)

[4:30]  Call-to-action: "Try the showcase. Star the repo. Write your own checker."

[5:00]  End card.
```

**Suggested tags:** `ai, quant-trading, self-reflection, meta-learning, trading-bot, llm, knowledge-graph, open-source`

---

## H) Press / newsletter one-liners

For pitching to: **The Register**, **Hacker Newsletter**, **TLDR AI**, **Ben's Bites**, **Import AI**, **The Batch** (Andrew Ng), **AlphaSignal**:

> **Reflex Quant is the first self-aware AI for quant trading.**
>
> It runs a 7-layer reflection engine on every trade — catching 27 error types no human PM has time to spot. When every checker passes but the trade still loses, the meta-loop catches its own blindspot and proposes its own next checker.
>
> Open-sourced today. AGPL-3.0 + Commercial. Live showcase: <https://Claire19950906.github.io/reflex-quant/showcase/>
>
> **Numbers from 7-day paper-trading run (v225):** 59 trades closed, 73 errors caught, 12 meta-loop cycles, 3 new checkers the system proposed itself.
>
> **Founder available for interview:** [Your name], [your email], [calendar link].

---

## License

This launch kit is dual-licensed under AGPL-3.0 and the Reflex Quant Commercial License — same as the rest of the repo. Quote with attribution.

