# 分析链日报 2026-11-17

> 只读统计。四个口径同源（`backend/src_python/analysis/` 的纯函数），
> 与运行时落库的 `no_trade_reason` / `analysis_state` 保持一致。

## 终态分布（六终态复算）

```
== 分析终态审计 ==  库=/Users/claire/Desktop/金融1.0/backend/trident_event_bus.db  范围=2026-11-17  行数=227

终态分布（derive_state 复算）
  complete           100   44.1%   分析完整      BUY=7 HOLD=81 SELL=12
  compressed          10    4.4%   压缩输出（有方向）  HOLD=8 SELL=2
  out_of_scope       109   48.0%   不在服务范围    HOLD=109
  data_unavailable     0    0.0%   证据未就绪     
  llm_failed           8    3.5%   模型未返回     HOLD=7 SELL=1
  parse_failed         0    0.0%   输出结构不达标   

其中「不在服务范围」 109 条（ 48.0%）—— 不该计入分析失败
真实分析失败        8 条（  3.5%），其中 数据 0 / 模型 8 / 结构 0

HOLD 原因分布（derive_no_trade_reason 复算）
  out_of_scope         109   48.0%
  gate_blocked          54   23.8%
  direction_neutral     27   11.9%
  (已下单)                 22    9.7%
  compressed             8    3.5%
  llm_failed             7    3.1%

target_none 的去向（证明越界/失败被混记）
  reason=target_none  → out_of_scope       108

失败归因拆分（模型失败到底失败在哪一环）
  主分析调用失败：http_error             4   网络/超时/配额
  调用成功但输出 <600 tokens          262   占成功调用 262/630（42%），八段写不完 → 多是 compressed/parse_failed 的来源
  调用失败合计                         4   对应不到决策行的部分不会落 llm_failed
  决策侧 llm_failed                 8   含「调用成功但返回空」的兜底
  决策侧 parse_failed               0   结构不达标，不是模型没答

口径说明：本次审计不把它写回库；接入运行时后，六终态将落 analysis_state，HOLD 原因将落 no_trade_reason。
```

## 前置路由回放（W1 上线前评估）

```
== 前置路由回放（只读）==  news=199  范围=2026-11-17

路由结论
  core              87   43.7%   进主分析
  out_of_scope      72   36.2%   不进主分析
  adjacent          38   19.1%   进主分析
  unknown            2    1.0%   进主分析

判据命中
  bucket                  63
  gate_core               55
  category_macro          36
  gate_class_word         11
  stock_hints              8
  category_crypto          7
  gate_assets_core         7
  text_core                3
  category_gold            3
  text_adjacent            2
  no_evidence              2
  category_oil             1
  category_stock           1

误杀检查（被挡但实际跑出 BUY/SELL 的新闻）：0
矛盾检查（实际决策是核心品种、路由却判不进分析）：2
  news=32334 asset=ETH reason=bucket
  news=34544 asset=BTC reason=bucket

结论口径：误杀 0 且矛盾 0 才允许把 ANALYSIS_ROUTE_V2 打开；任一非 0 都先补别名/特征词再回放。
```

## HOLD 反事实结算（准确度的分母）

```
== HOLD 反事实结算 ==  范围=2026-11-17  HOLD 行=47  取不到价格=0

之后 30 分钟位移（有价的 HOLD = 47）
  <0.10%         19   40.4%
  0.10-0.30%     20   42.6%
  0.30-0.60%      4    8.5%
  >0.60%          4    8.5%
  观望在「几乎没波动」的意义上是对的：19/47 = 40.4%

之后 60 分钟位移（有价的 HOLD = 47）
  <0.10%         17   36.2%
  0.10-0.30%     15   31.9%
  0.30-0.60%     12   25.5%
  >0.60%          3    6.4%
  观望在「几乎没波动」的意义上是对的：17/47 = 36.2%

分品种（30 分钟）
  BTC    21 条  <0.10%=8 0.10-0.30%=8 0.30-0.60%=2 >0.60%=3
  ETH     6 条  <0.10%=2 0.10-0.30%=2 0.30-0.60%=1 >0.60%=1
  XAU    20 条  <0.10%=9 0.10-0.30%=10 0.30-0.60%=1 >0.60%=0

位移 ≥0.60% 的 HOLD（潜在漏单）Top 4
  id=12001 ETH 19:18 位移=0.76% 降级=0 score=0.0
  id=11997 BTC 19:18 位移=0.69% 降级=0 score=0.0
  id=12000 BTC 19:18 位移=0.69% 降级=0 score=0.0
  id=11965 BTC 11:13 位移=0.66% 降级=1 score=0.0

其中「分析降级」占 1/4 = 25.0%（漏单与分析失败强相关；这些不是阈值问题，是没产出方向）

口径声明：这是「机会成本」不是「准确率」——HOLD 没有方向，只能测「不动 vs 动」的位移。真准确率要等方向性样本 ≥60 笔、且 HOLD 也按反事实结算。
```

## 选择器对比（旧闸门 vs 新档位）

```
== 选择器离线对比 ==  有方向且有 1m 价的样本 = 19  持有期 = 60 分钟

旧选择器（实际通过闸门的）
  已下单                选中 11/19  胜率  63.6%  平均 +0.148%
  被拦下                选中  8/19  胜率  62.5%  平均 +0.078%

新选择器（analysis.confidence.rank）
  A 可建仓              选中 0
  A+B 影子             选中 14/19  胜率  57.1%  平均 +0.114%
  C 只观察              选中  5/19  胜率  80.0%  平均 +0.133%
  A+B+C 有方向          选中 19/19  胜率  63.2%  平均 +0.119%

逐条（按新档位、收益排序）
  B_shadow     id=12122  ETH  SELL conf= 64.2 收益= +1.13% 完整  [旧闸门放行]
  B_shadow     id=12046  XAU  SELL conf= 74.7 收益= +0.32% 完整  [旧闸门放行]
  B_shadow     id=12061  XAU  SELL conf= 49.1 收益= +0.26% 完整  [旧闸门放行]
  B_shadow     id=11939  BTC  BUY  conf= 52.4 收益= +0.19% 完整  [旧闸门放行]
  B_shadow     id=12047  BTC  SELL conf= 49.1 收益= +0.08% 完整  [旧闸门放行]
  B_shadow     id=12029  ETH  BUY  conf= 49.1 收益= +0.07% 完整
  B_shadow     id=11956  XAU  BUY  conf= 57.4 收益= +0.05% 完整
  B_shadow     id=11932  XAU  BUY  conf= 66.3 收益= +0.04% 完整
  B_shadow     id=12028  BTC  BUY  conf= 49.1 收益= -0.00% 完整
  B_shadow     id=12062  BTC  SELL conf= 49.1 收益= -0.02% 完整
  B_shadow     id=11971  ETH  SELL conf= 49.1 收益= -0.03% 完整  [旧闸门放行]
  B_shadow     id=12104  XAU  BUY  conf= 49.1 收益= -0.05% 完整
  B_shadow     id=11985  XAU  SELL conf= 49.0 收益= -0.16% 完整  [旧闸门放行]
  B_shadow     id=12075  SOL  BUY  conf= 63.0 收益= -0.28% 完整  [旧闸门放行]
  C_watch      id=12135  ETH  SELL conf= 58.2 收益= +1.00% 压缩  [旧闸门放行]
  C_watch      id=11958  XAU  BUY  conf= 39.0 收益= +0.27% 压缩
  C_watch      id=12103  BTC  SELL conf= 52.9 收益= +0.27% 完整
  C_watch      id=12045  BTC  SELL conf= 67.0 收益= +0.08% 压缩  [旧闸门放行]
  C_watch      id=11966  SOL  SELL conf= 52.8 收益= -0.96% 压缩  [旧闸门放行]

口径：样本只有「模型给过方向」的行；60 分钟单周期、无止损、无成本。结论只用于判断选择器是否『负向选择』，不作为收益承诺。
```

## 读法

- **终态**：先看「真实失败」占比，它才是需要修的部分；`out_of_scope` 不是失败。
- **路由**：只看「误杀」是否为 0；`unknown` 占比高说明判据不足，不是要强行判死。
- **HOLD 反事实**：给 HOLD 补分母，回答「观望到底对不对」。
- **选择器**：比较旧闸门与新档位在同一批样本上的胜率；样本 <60 时只记录、不调阈值。
