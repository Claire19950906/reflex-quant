# 分析链日报 2026-11-16

> 只读统计。四个口径同源（`backend/src_python/analysis/` 的纯函数），
> 与运行时落库的 `no_trade_reason` / `analysis_state` 保持一致。

## 终态分布（六终态复算）

```
== 分析终态审计 ==  库=/Users/claire/Desktop/金融1.0/backend/trident_event_bus.db  范围=2026-11-16  行数=504

终态分布（derive_state 复算）
  complete            61   12.1%   分析完整      BUY=1 HOLD=58 SELL=2
  compressed          63   12.5%   压缩输出（有方向）  BUY=1 HOLD=62
  out_of_scope       186   36.9%   不在服务范围    HOLD=186
  data_unavailable     0    0.0%   证据未就绪     
  llm_failed          96   19.0%   模型未返回     HOLD=95 SELL=1
  parse_failed        98   19.4%   输出结构不达标   HOLD=98

其中「不在服务范围」 186 条（ 36.9%）—— 不该计入分析失败
真实分析失败        194 条（ 38.5%），其中 数据 0 / 模型 96 / 结构 98

HOLD 原因分布（derive_no_trade_reason 复算）
  out_of_scope         186   36.9%
  parse_failed          98   19.4%
  llm_failed            95   18.8%
  compressed            62   12.3%
  gate_blocked          58   11.5%
  (已下单)                  5    1.0%

target_none 的去向（证明越界/失败被混记）
  reason=target_none  → out_of_scope       152

失败归因拆分（模型失败到底失败在哪一环）
  主分析调用失败：http_error             1   网络/超时/配额
  调用成功但输出 <600 tokens          308   占成功调用 308/1240（25%），八段写不完 → 多是 compressed/parse_failed 的来源
  调用失败合计                         1   对应不到决策行的部分不会落 llm_failed
  决策侧 llm_failed                96   含「调用成功但返回空」的兜底
  决策侧 parse_failed              98   结构不达标，不是模型没答

口径说明：本次审计不把它写回库；接入运行时后，六终态将落 analysis_state，HOLD 原因将落 no_trade_reason。
```

## 前置路由回放（W1 上线前评估）

```
== 前置路由回放（只读）==  news=278  范围=2026-11-16

路由结论
  core             118   42.4%   进主分析
  unknown          100   36.0%   进主分析
  out_of_scope      40   14.4%   不进主分析
  adjacent          20    7.2%   进主分析

判据命中
  no_evidence            100
  category_gold           37
  category_oil            28
  stock_hints             22
  category_crypto         16
  gate_core               15
  text_core               15
  bucket                  12
  category_macro          10
  text_adjacent           10
  category_stock           6
  gate_class_word          5
  gate_assets_core         2

误杀检查（被挡但实际跑出 BUY/SELL 的新闻）：0
矛盾检查（实际决策是核心品种、路由却判不进分析）：2
  news=28318 asset=BTC reason=stock_hints
  news=28358 asset=BTC reason=stock_hints

结论口径：误杀 0 且矛盾 0 才允许把 ANALYSIS_ROUTE_V2 打开；任一非 0 都先补别名/特征词再回放。
```

## HOLD 反事实结算（准确度的分母）

```
== HOLD 反事实结算 ==  范围=2026-11-16  HOLD 行=185  取不到价格=0

之后 30 分钟位移（有价的 HOLD = 185）
  <0.10%         81   43.8%
  0.10-0.30%     62   33.5%
  0.30-0.60%     30   16.2%
  >0.60%         12    6.5%
  观望在「几乎没波动」的意义上是对的：81/185 = 43.8%

之后 60 分钟位移（有价的 HOLD = 185）
  <0.10%         53   28.6%
  0.10-0.30%     71   38.4%
  0.30-0.60%     33   17.8%
  >0.60%         28   15.1%
  观望在「几乎没波动」的意义上是对的：53/185 = 28.6%

分品种（30 分钟）
  BTC    53 条  <0.10%=11 0.10-0.30%=20 0.30-0.60%=16 >0.60%=6
  ETH    12 条  <0.10%=2 0.10-0.30%=3 0.30-0.60%=6 >0.60%=1
  SOL     1 条  <0.10%=0 0.10-0.30%=1 0.30-0.60%=0 >0.60%=0
  XAU   119 条  <0.10%=68 0.10-0.30%=38 0.30-0.60%=8 >0.60%=5

位移 ≥0.60% 的 HOLD（潜在漏单）Top 12
  id=11920 BTC 22:47 位移=1.34% 降级=0 score=0.0
  id=11921 BTC 22:47 位移=1.34% 降级=0 score=0.0
  id=11916 BTC 22:30 位移=0.89% 降级=0 score=0.0
  id=11630 XAU 06:43 位移=0.88% 降级=0 score=0.0
  id=11623 XAU 06:39 位移=0.86% 降级=1 score=0.0
  id=11626 XAU 06:39 位移=0.86% 降级=1 score=0.0
  id=11917 ETH 22:30 位移=0.8% 降级=0 score=0.0
  id=11632 XAU 06:44 位移=0.77% 降级=1 score=0.05
  id=11634 XAU 06:47 位移=0.63% 降级=1 score=0.0
  id=11454 BTC 01:25 位移=0.62% 降级=0 score=0.0
  id=11505 BTC 02:52 位移=0.62% 降级=1 score=0.0
  id=11508 BTC 02:52 位移=0.62% 降级=1 score=0.0

其中「分析降级」占 6/12 = 50.0%（漏单与分析失败强相关；这些不是阈值问题，是没产出方向）

口径声明：这是「机会成本」不是「准确率」——HOLD 没有方向，只能测「不动 vs 动」的位移。真准确率要等方向性样本 ≥60 笔、且 HOLD 也按反事实结算。
```

## 选择器对比（旧闸门 vs 新档位）

```
== 选择器离线对比 ==  有方向且有 1m 价的样本 = 29  持有期 = 60 分钟

旧选择器（实际通过闸门的）
  已下单                选中  1/29  胜率 100.0%  平均 +0.501%
  被拦下                选中 28/29  胜率  64.3%  平均 -0.029%

新选择器（analysis.confidence.rank）
  A 可建仓              选中 0
  A+B 影子             选中  8/29  胜率  50.0%  平均 -0.021%
  C 只观察              选中  6/29  胜率  83.3%  平均 +0.099%
  A+B+C 有方向          选中 14/29  胜率  64.3%  平均 +0.030%

逐条（按新档位、收益排序）
  B_shadow     id=11919  XAU  SELL conf= 49.1 收益= +0.50% 完整  [旧闸门放行]
  B_shadow     id=11771  BTC  SELL conf= 49.1 收益= +0.39% 完整
  B_shadow     id=11808  BTC  SELL conf= 49.1 收益= +0.29% 完整
  B_shadow     id=11772  ETH  SELL conf= 49.1 收益= +0.25% 完整
  B_shadow     id=11644  BTC  BUY  conf= 49.1 收益= -0.02% 完整
  B_shadow     id=11464  BTC  SELL conf= 49.1 收益= -0.32% 完整
  B_shadow     id=11485  BTC  SELL conf= 49.1 收益= -0.42% 完整
  B_shadow     id=11704  BTC  BUY  conf= 49.1 收益= -0.86% 完整
  C_watch      id=11763  BTC  SELL conf= 40.9 收益= +0.39% 压缩
  C_watch      id=11532  XAU  BUY  conf= 32.5 收益= +0.18% 压缩
  C_watch      id=11534  XAU  BUY  conf= 42.9 收益= +0.18% 完整
  C_watch      id=11486  XAU  BUY  conf= 36.9 收益= +0.11% 压缩
  C_watch      id=11463  XAU  BUY  conf= 42.9 收益= +0.05% 完整
  C_watch      id=11724  XAU  BUY  conf= 42.9 收益= -0.32% 完整
  D_hold       id=11511  XAU  BUY  conf= 23.1 收益= +0.20% 压缩
  D_hold       id=11536  XAU  BUY  conf= 23.1 收益= +0.18% 压缩
  D_hold       id=11451  XAU  BUY  conf= 29.4 收益= +0.17% 压缩
  D_hold       id=11580  XAU  BUY  conf= 23.1 收益= +0.13% 压缩
  D_hold       id=11525  XAU  BUY  conf= 23.1 收益= +0.13% 压缩
  D_hold       id=11474  XAU  BUY  conf= 23.1 收益= +0.12% 压缩
  D_hold       id=11455  XAU  BUY  conf= 29.4 收益= +0.08% 压缩
  D_hold       id=11527  XAU  BUY  conf= 29.4 收益= +0.08% 压缩
  D_hold       id=11469  XAU  BUY  conf= 23.1 收益= +0.08% 压缩
  D_hold       id=11461  XAU  BUY  conf= 23.1 收益= +0.05% 压缩
  D_hold       id=11553  XAU  BUY  conf= 23.1 收益= -0.08% 压缩
  D_hold       id=11554  XAU  BUY  conf= 23.1 收益= -0.08% 压缩
  D_hold       id=11903  BTC  BUY  conf= 35.4 收益= -0.12% 完整
  D_hold       id=11615  XAU  SELL conf= 23.1 收益= -0.72% 压缩
  D_hold       id=11632  XAU  SELL conf= 23.1 收益= -0.96% 压缩

口径：样本只有「模型给过方向」的行；60 分钟单周期、无止损、无成本。结论只用于判断选择器是否『负向选择』，不作为收益承诺。
```

## 读法

- **终态**：先看「真实失败」占比，它才是需要修的部分；`out_of_scope` 不是失败。
- **路由**：只看「误杀」是否为 0；`unknown` 占比高说明判据不足，不是要强行判死。
- **HOLD 反事实**：给 HOLD 补分母，回答「观望到底对不对」。
- **选择器**：比较旧闸门与新档位在同一批样本上的胜率；样本 <60 时只记录、不调阈值。
