# 分析链日报 2026-11-18（快照回补版）

> ⚠️ 口径说明：**线上库当日决策行已被清空**，本文件不是从线上库生成，而是用清库前的
> 快照 `backend/backups/trident_event_bus.db.bak_20261119_080307_before_f1_null`
> 的克隆副本（`/tmp/trident_118_snapshot.db`，只读统计、未写库）重算。
> 时间段与判据同线上代码，四个口径同源 `backend/src_python/analysis/`。
>
> 线上库现状：`ai_decisions` 仅 85 行（id 12700–12784，最早 2026-11-19T07:44），
> 快照库：1274 行（id 11425–12699），其中 2026-11-18 为 **342 行**。

## 终态分布（六终态复算）

```
== 分析终态审计 ==  库=/tmp/trident_118_snapshot.db  范围=2026-11-18  行数=342

终态分布（derive_state 复算）
  complete           158   46.2%   分析完整      BUY=12 HOLD=131 SELL=15
  compressed           3    0.9%   压缩输出（有方向）  HOLD=3
  out_of_scope       125   36.5%   不在服务范围    HOLD=125
  data_unavailable     0    0.0%   证据未就绪     
  llm_failed          56   16.4%   模型未返回     HOLD=56
  parse_failed         0    0.0%   输出结构不达标   

其中「不在服务范围」 125 条（ 36.5%）—— 不该计入分析失败
真实分析失败        56 条（ 16.4%），其中 数据 0 / 模型 56 / 结构 0

HOLD 原因分布（derive_no_trade_reason 复算）
  out_of_scope         125   36.5%
  gate_blocked         113   33.0%
  llm_failed            56   16.4%
  (已下单)                 27    7.9%
  direction_neutral     18    5.3%
  compressed             3    0.9%

target_none 的去向（证明越界/失败被混记）
  reason=target_none  → out_of_scope       122

失败归因拆分（模型失败到底失败在哪一环）
  主分析调用失败：network_error          4   网络/超时/配额
  调用成功但输出 <600 tokens          143   占成功调用 143/444（32%），八段写不完 → 多是 compressed/parse_failed 的来源
  调用失败合计                         4   对应不到决策行的部分不会落 llm_failed
  决策侧 llm_failed                56   含「调用成功但返回空」的兜底
  决策侧 parse_failed               0   结构不达标，不是模型没答

口径说明：本次审计不把它写回库；接入运行时后，六终态将落 analysis_state，HOLD 原因将落 no_trade_reason。
```

## 前置路由回放（W1 上线前评估）

```
== 前置路由回放（只读）==  news=214  范围=2026-11-18

路由结论
  core             127   59.3%   进主分析
  adjacent          74   34.6%   进主分析
  out_of_scope      13    6.1%   不进主分析

判据命中
  gate_core               75
  category_macro          28
  text_mega_cap           24
  text_macro              21
  gate_class_word         17
  gate_assets_core        16
  bucket                  13
  category_gold            7
  text_core                5
  category_oil             5
  category_crypto          2
  text_adjacent            1

误杀检查（被挡但实际跑出 BUY/SELL 的新闻）：0
矛盾检查（实际决策是核心品种、路由却判不进分析）：3
  news=35197 asset=BTC reason=bucket
  news=36322 asset=BTC reason=bucket
  news=36819 asset=BTC reason=bucket

结论口径：误杀 0 且矛盾 0 才允许把 ANALYSIS_ROUTE_V2 打开；任一非 0 都先补别名/特征词再回放。
```

## HOLD 反事实结算（准确度的分母）

```
== HOLD 反事实结算 ==  范围=2026-11-18  HOLD 行=123  取不到价格=0

之后 30 分钟位移（有价的 HOLD = 123）
  <0.10%         33   26.8%
  0.10-0.30%     59   48.0%
  0.30-0.60%     13   10.6%
  >0.60%         18   14.6%
  观望在「几乎没波动」的意义上是对的：33/123 = 26.8%

之后 60 分钟位移（有价的 HOLD = 123）
  <0.10%         16   13.0%
  0.10-0.30%     55   44.7%
  0.30-0.60%     26   21.1%
  >0.60%         26   21.1%
  观望在「几乎没波动」的意义上是对的：16/123 = 13.0%

分品种（30 分钟）
  BTC    46 条  <0.10%=12 0.10-0.30%=17 0.30-0.60%=4 >0.60%=13
  ETH    14 条  <0.10%=2 0.10-0.30%=5 0.30-0.60%=4 >0.60%=3
  SOL     5 条  <0.10%=0 0.10-0.30%=2 0.30-0.60%=1 >0.60%=2
  XAU    58 条  <0.10%=19 0.10-0.30%=35 0.30-0.60%=4 >0.60%=0

位移 ≥0.60% 的 HOLD（潜在漏单）Top 12
  id=12456 ETH 22:10 位移=1.89% 降级=0 score=-0.38
  id=12448 BTC 22:10 位移=1.29% 降级=0 score=0.0
  id=12449 BTC 22:10 位移=1.29% 降级=0 score=0.65
  id=12450 BTC 22:10 位移=1.29% 降级=0 score=0.0
  id=12451 BTC 22:10 位移=1.29% 降级=0 score=0.45
  id=12453 BTC 22:10 位移=1.29% 降级=0 score=0.0
  id=12457 SOL 22:10 位移=1.12% 降级=0 score=0.0
  id=12474 ETH 22:23 位移=1.11% 降级=1 score=-0.315
  id=12486 ETH 22:23 位移=1.11% 降级=0 score=0.0
  id=12464 BTC 22:14 位移=0.95% 降级=0 score=-0.3
  id=12465 BTC 22:14 位移=0.95% 降级=0 score=0.0
  id=12163 SOL 00:25 位移=0.79% 降级=0 score=0.0

其中「分析降级」占 2/18 = 11.1%（漏单与分析失败强相关；这些不是阈值问题，是没产出方向）

口径声明：这是「机会成本」不是「准确率」——HOLD 没有方向，只能测「不动 vs 动」的位移。真准确率要等方向性样本 ≥60 笔、且 HOLD 也按反事实结算。
```

## 选择器对比（旧闸门 vs 新档位）

```
== 选择器离线对比 ==  有方向且有 1m 价的样本 = 36  持有期 = 60 分钟

旧选择器（实际通过闸门的）
  已下单                选中 16/36  胜率  56.2%  平均 +0.081%
  被拦下                选中 20/36  胜率  50.0%  平均 +0.159%

新选择器（analysis.confidence.rank）
  A 可建仓              选中  4/36  胜率 100.0%  平均 +0.790%
  A+B 影子             选中 32/36  胜率  56.2%  平均 +0.110%
  C 只观察              选中  2/36  胜率  50.0%  平均 +0.606%
  A+B+C 有方向          选中 34/36  胜率  55.9%  平均 +0.139%

逐条（按新档位、收益排序）
  A_tradable   id=12411  BTC  SELL conf= 65.7 收益= +1.37% 完整
  A_tradable   id=12456  ETH  SELL conf= 63.5 收益= +1.14% 完整
  A_tradable   id=12306  BTC  SELL conf= 76.7 收益= +0.60% 完整  [旧闸门放行]
  A_tradable   id=12330  XAU  BUY  conf= 63.5 收益= +0.05% 完整  [旧闸门放行]
  B_shadow     id=12436  ETH  SELL conf= 49.1 收益= +0.79% 完整  [旧闸门放行]
  B_shadow     id=12307  XAU  SELL conf= 49.1 收益= +0.61% 完整  [旧闸门放行]
  B_shadow     id=12464  BTC  SELL conf= 49.1 收益= +0.46% 完整
  B_shadow     id=12288  XAU  SELL conf= 49.1 收益= +0.44% 完整  [旧闸门放行]
  B_shadow     id=12435  BTC  SELL conf= 49.1 收益= +0.42% 完整
  B_shadow     id=12443  BTC  SELL conf= 49.1 收益= +0.42% 完整
  B_shadow     id=12347  BTC  SELL conf= 49.1 收益= +0.36% 完整
  B_shadow     id=12308  BTC  SELL conf= 66.7 收益= +0.32% 完整  [旧闸门放行]
  B_shadow     id=12164  BTC  SELL conf= 49.1 收益= +0.25% 完整
  B_shadow     id=12434  XAU  SELL conf= 55.7 收益= +0.20% 完整
  B_shadow     id=12437  XAU  SELL conf= 48.1 收益= +0.20% 完整  [旧闸门放行]
  B_shadow     id=12441  XAU  SELL conf= 53.6 收益= +0.20% 完整
  B_shadow     id=12445  XAU  SELL conf= 49.1 收益= +0.20% 完整  [旧闸门放行]
  B_shadow     id=12195  XAU  SELL conf= 47.1 收益= +0.07% 完整  [旧闸门放行]
  B_shadow     id=12462  XAU  BUY  conf= 50.2 收益= -0.06% 完整
  B_shadow     id=12259  BTC  BUY  conf= 68.3 收益= -0.10% 完整  [旧闸门放行]
  B_shadow     id=12397  XAU  SELL conf= 55.8 收益= -0.13% 完整  [旧闸门放行]
  B_shadow     id=12240  XAU  BUY  conf= 49.1 收益= -0.20% 完整  [旧闸门放行]
  B_shadow     id=12366  XAU  BUY  conf= 49.0 收益= -0.25% 完整  [旧闸门放行]
  B_shadow     id=12333  XAU  BUY  conf= 44.7 收益= -0.26% 完整
  B_shadow     id=12216  BTC  BUY  conf= 49.1 收益= -0.27% 完整
  B_shadow     id=12403  SOL  BUY  conf= 72.2 收益= -0.31% 完整
  B_shadow     id=12172  ETH  SELL conf= 68.3 收益= -0.33% 完整  [旧闸门放行]
  B_shadow     id=12446  BTC  BUY  conf= 49.1 收益= -0.42% 完整
  B_shadow     id=12269  BTC  BUY  conf= 49.1 收益= -0.47% 完整  [旧闸门放行]
  B_shadow     id=12270  BTC  BUY  conf= 45.0 收益= -0.50% 完整  [旧闸门放行]
  B_shadow     id=12449  BTC  BUY  conf= 68.3 收益= -0.64% 完整
  B_shadow     id=12451  BTC  BUY  conf= 57.4 收益= -0.64% 完整
  C_watch      id=12474  ETH  SELL conf= 43.9 收益= +1.32% 压缩
  C_watch      id=12326  BTC  BUY  conf= 51.8 收益= -0.10% 完整
  D_hold       id=12405  XAU  BUY  conf= 29.4 收益= -0.09% 压缩
  D_hold       id=12360  XAU  BUY  conf= 45.4 收益= -0.16% 完整

口径：样本只有「模型给过方向」的行；60 分钟单周期、无止损、无成本。结论只用于判断选择器是否『负向选择』，不作为收益承诺。
```

## 读法

- **终态**：先看「真实失败」占比，它才是需要修的部分；`out_of_scope` 不是失败。
- **路由**：只看「误杀」是否为 0；`unknown` 占比高说明判据不足，不是要强行判死。
- **HOLD 反事实**：给 HOLD 补分母，回答「观望到底对不对」。
- **选择器**：比较旧闸门与新档位在同一批样本上的胜率；样本 <60 时只记录、不调阈值。

## 本日与其他日期的差异

11-18 的路由回放里 `unknown` 为 0（core 59.3% + adjacent 34.6%），而 11-19 的
`unknown` 高达 28.1%。同一天的两套判据（线上门控 vs 影子回放）产出差异，需核对
隔离方式是否一致。
