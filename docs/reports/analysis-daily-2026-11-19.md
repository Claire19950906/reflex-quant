# 分析链日报 2026-11-19

> 只读统计。四个口径同源（`backend/src_python/analysis/` 的纯函数），
> 与运行时落库的 `no_trade_reason` / `analysis_state` 保持一致。

## 终态分布（六终态复算）

```
== 分析终态审计 ==  库=/Users/claire/Desktop/金融1.0/backend/trident_event_bus.db  范围=2026-11-19  行数=80

终态分布（derive_state 复算）
  complete            25   31.2%   分析完整      HOLD=22 SELL=3
  compressed           1    1.2%   压缩输出（有方向）  HOLD=1
  out_of_scope        39   48.8%   不在服务范围    HOLD=39
  data_unavailable     0    0.0%   证据未就绪     
  llm_failed          15   18.8%   模型未返回     HOLD=15
  parse_failed         0    0.0%   输出结构不达标   

其中「不在服务范围」 39 条（ 48.8%）—— 不该计入分析失败
真实分析失败        15 条（ 18.8%），其中 数据 0 / 模型 15 / 结构 0

HOLD 原因分布（derive_no_trade_reason 复算）
  out_of_scope          39   48.8%
  gate_blocked          16   20.0%
  llm_failed            15   18.8%
  direction_neutral      6    7.5%
  (已下单)                  3    3.8%
  compressed             1    1.2%

target_none 的去向（证明越界/失败被混记）
  reason=target_none  → out_of_scope        38

失败归因拆分（模型失败到底失败在哪一环）
  调用成功但输出 <600 tokens          105   占成功调用 105/339（31%），八段写不完 → 多是 compressed/parse_failed 的来源
  调用失败合计                         0   对应不到决策行的部分不会落 llm_failed
  决策侧 llm_failed                15   含「调用成功但返回空」的兜底
  决策侧 parse_failed               0   结构不达标，不是模型没答

口径说明：本次审计不把它写回库；接入运行时后，六终态将落 analysis_state，HOLD 原因将落 no_trade_reason。
```

## 前置路由回放（W1 上线前评估）

```
== 前置路由回放（只读）==  news=217  范围=2026-11-19

路由结论
  adjacent          94   43.3%   进主分析
  core              61   28.1%   进主分析
  unknown           61   28.1%   进主分析
  out_of_scope       1    0.5%   不进主分析

判据命中
  no_evidence             61
  gate_core               46
  text_macro              30
  category_macro          27
  text_mega_cap           20
  text_adjacent           17
  text_core                8
  category_oil             4
  category_crypto          2
  gate_assets_core         1
  stock_hints              1

误杀检查（被挡但实际跑出 BUY/SELL 的新闻）：0
矛盾检查（实际决策是核心品种、路由却判不进分析）：0

结论口径：误杀 0 且矛盾 0 才允许把 ANALYSIS_ROUTE_V2 打开；任一非 0 都先补别名/特征词再回放。
```

## HOLD 反事实结算（准确度的分母）

```
== HOLD 反事实结算 ==  范围=2026-11-19  HOLD 行=23  取不到价格=0

之后 30 分钟位移（有价的 HOLD = 23）
  <0.10%         11   47.8%
  0.10-0.30%     10   43.5%
  0.30-0.60%      2    8.7%
  >0.60%          0    0.0%
  观望在「几乎没波动」的意义上是对的：11/23 = 47.8%

之后 60 分钟位移（有价的 HOLD = 23）
  <0.10%         11   47.8%
  0.10-0.30%     10   43.5%
  0.30-0.60%      2    8.7%
  >0.60%          0    0.0%
  观望在「几乎没波动」的意义上是对的：11/23 = 47.8%

分品种（30 分钟）
  BTC     7 条  <0.10%=3 0.10-0.30%=2 0.30-0.60%=2 >0.60%=0
  ETH     2 条  <0.10%=1 0.10-0.30%=1 0.30-0.60%=0 >0.60%=0
  SOL     1 条  <0.10%=0 0.10-0.30%=1 0.30-0.60%=0 >0.60%=0
  XAU    13 条  <0.10%=7 0.10-0.30%=6 0.30-0.60%=0 >0.60%=0

位移 ≥0.60% 的 HOLD（潜在漏单）Top 0

口径声明：这是「机会成本」不是「准确率」——HOLD 没有方向，只能测「不动 vs 动」的位移。真准确率要等方向性样本 ≥60 笔、且 HOLD 也按反事实结算。
```

## 选择器对比（旧闸门 vs 新档位）

```
== 选择器离线对比 ==  有方向且有 1m 价的样本 = 9  持有期 = 60 分钟

旧选择器（实际通过闸门的）
  已下单                选中  2/9  胜率   0.0%  平均 -0.146%
  被拦下                选中  7/9  胜率  57.1%  平均 +0.063%

新选择器（analysis.confidence.rank）
  A 可建仓              选中 0
  A+B 影子             选中  8/9  胜率  50.0%  平均 +0.018%
  C 只观察              选中  1/9  胜率   0.0%  平均 +0.000%
  A+B+C 有方向          选中  9/9  胜率  44.4%  平均 +0.016%

逐条（按新档位、收益排序）
  B_shadow     id=12715  BTC  BUY  conf= 49.1 收益= +0.49% 完整
  B_shadow     id=12750  XAU  BUY  conf= 70.0 收益= +0.04% 完整
  B_shadow     id=12760  BTC  SELL conf= 54.7 收益= +0.01% 完整
  B_shadow     id=12763  BTC  SELL conf= 54.7 收益= +0.01% 完整
  B_shadow     id=12759  XAU  BUY  conf= 70.0 收益= +0.00% 完整
  B_shadow     id=12764  ETH  SELL conf= 49.1 收益= -0.07% 完整  [旧闸门放行]
  B_shadow     id=12762  SOL  SELL conf= 57.4 收益= -0.11% 完整
  B_shadow     id=12732  ETH  SELL conf= 49.1 收益= -0.22% 完整  [旧闸门放行]
  C_watch      id=12768  XAU  SELL conf= 50.0 收益= -0.00% 压缩

口径：样本只有「模型给过方向」的行；60 分钟单周期、无止损、无成本。结论只用于判断选择器是否『负向选择』，不作为收益承诺。
```

## 读法

- **终态**：先看「真实失败」占比，它才是需要修的部分；`out_of_scope` 不是失败。
- **路由**：只看「误杀」是否为 0；`unknown` 占比高说明判据不足，不是要强行判死。
- **HOLD 反事实**：给 HOLD 补分母，回答「观望到底对不对」。
- **选择器**：比较旧闸门与新档位在同一批样本上的胜率；样本 <60 时只记录、不调阈值。
