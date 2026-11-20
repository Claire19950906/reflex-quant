# Pro Optimization Modules — v230p1

3 个新模块,扩展风险/regime/回测能力。所有模块都保持**纯 numpy + 同 repo 依赖**,零新增外部包。

## 模块清单

| 模块 | 用途 | 行数 | 测试 |
|---|---|---|---|
| `risk_metrics_pro.py` | 8 个 SOTA 风险指标 | 700 | 70 |
| `market_regime_hmm.py` | Sticky HMM + BOCPD regime 检测 | 631 | 20 |
| `backtest_pro.py` | Walk-Forward + Bootstrap + Compare + CLI | 749 | 50 |
| **合计** | | **2080** | **140** |

---

## 1. risk_metrics_pro — 8 个 SOTA 指标

### 指标

| 指标 | 公式/思路 | 何时重要 |
|---|---|---|
| `sortino` | 收益 / 下行波动 | 不惩罚上行波动 |
| `calmar` | 年化收益 / MaxDD | 抗回撤优先 |
| `omega` | 收益分布的累积比 | 阈值可调 |
| `tail_ratio` | 右尾 95% / 左尾 95% | 收益分布对称性 |
| `psr` | Probabilistic Sharpe Ratio | 样本量无关性 |
| `dsr` | Deflated Sharpe Ratio | 多策略过拟合校正 |
| `minbt` | PBO (min backtest overfitting) | OOS 过拟合概率 |
| `deflated_expectancy` | 期望收益的多策略校正 | 期望而非 Sharpe |

### 用法

```python
import risk_metrics_pro as rmp

# 完整评估(向后兼容老 metrics + 8 个 pro 嵌套结构)
raw = rmp.compute_portfolio_metrics_pro(returns, periods_per_year=252, n_trials=1)
# raw = {"sharpe": 1.2, "max_drawdown": 0.15, ...,
#        "sortino": {"value": 1.5, "ci_95": (1.2, 1.8), "interp": "..."},
#        "dsr": {...}, ...}

# 单个指标
sr = rmp.sortino_ratio(returns)
dsr = rmp.deflated_sharpe_ratio(sharpe=1.2, n_trials=20, n_obs=252, returns=returns)
```

### 已知 bug 绕开

- `compute_drawdown` 不接受 numpy 数组,需 `.tolist()`(本 PR-3 已自动处理)

---

## 2. market_regime_hmm — Sticky HMM + BOCPD

### 核心组件

#### `StickyHMM`(标量 K=3 优化)

- Baum-Welch EM 训练,sticky transition prior(α=20)
- 标量特化:forward/backward 用纯 Python 循环,K=3 时比 numpy 路径快 3-5x
- 训练后按 μ 升序重排状态 → 固定 **bear < range < bull**(LLM 友好)

#### `BOCPD`(Bayesian Online Change-Point Detection)

- Normal-Inverse-Gamma 联合先验
- `max_run_length=500` 截断防 OOM
- 数值稳定:`_logsumexp` 小 1D (K≤8) 走纯 Python,大数组 `np.clip(-50, 50)` 防 exp 溢出,NaN 兜底

#### `classify_regime_hmm(returns)` — 主入口

- 数据自适应 NIG 先验(首 30 样本估 μ₀, β₀ = α₀ · var_init · 4)
- 输出 `RegimeState` dataclass:regime, confidence, state_probs, days_in_regime, change_score, viterbi_path, factor_weights
- `factor_weights` 用于 `factor_engine` 的 regime-conditional 加权

### 用法

```python
import market_regime_hmm as mrh

# 主入口
state = mrh.classify_regime_hmm(returns, n_iter=10)
print(state.regime, state.confidence, state.change_score)
print(state.factor_weights)
# {'trend': 0.6, 'momentum': 0.7, 'mean_reversion': 1.3, 'volatility': 1.2, 'size': 0.85}

# 直接用 HMM
hmm = mrh.StickyHMM(n_states=3, seed=0, sticky_alpha=20.0)
params = hmm.fit(returns, n_iter=20)
viterbi = hmm.viterbi(returns)

# 直接用 BOCPD
state = mrh.BOCPDState(run_length_probs=np.array([1.0]), t=0)
for v in returns:
    state = mrh.bocpd_update(float(v), state, hazard=1/30.0,
                              mu0=0.0, kappa0=1.0, alpha0=1.0, beta0=0.0001)
    if state.change_score > 0.5:
        print(f"change detected at t={state.t}, score={state.change_score}")
```

### 与老 `market_regime.py` 关系

- 老 `market_regime.py`:**单时点**资金/价格背离检测(157 行,无时序)
- 新 `market_regime_hmm.py`:**时序**regime 检测(HMM + BOCPD,631 行)
- 两者不冲突,可组合使用

---

## 3. backtest_pro — Walk-Forward + Bootstrap

### 主入口

```python
import backtest_pro as bp

# 单次回测
res = bp.compute_backtest(
    returns, signal, name='my_strategy',
    cost_bps=5.0,           # 单边手续费
    use_regime_hmm=True,    # 自动接 HMM 拆分绩效
    bootstrap_n=1000, seed=0,
)
print(res.sharpe_ci)         # (lo, mid, hi)
print(res.p_value_vs_zero)   # 置换检验 p
print(res.regime_breakdown)  # {'bull': {...}, 'range': {...}, 'bear': {...}}
print(res.to_dict())         # JSON-safe
```

### Walk-Forward

```python
def sig_func(full_r, train_r):
    mu = train_r.mean()
    return np.where(full_r > mu, 1.0, -1.0)

wf = bp.walk_forward_backtest(
    returns, sig_func,
    n_folds=5,
    train_frac=0.7,
    anchored=False,    # True=锚定(训练集从 0 开始),False=滚动
    cost_bps=5.0,
)
print(wf.in_sample_sharpe_mean)
print(wf.out_of_sample_sharpe_mean)
print(wf.oos_degradation)   # < 0 表示过拟合
print(wf.to_dict())
```

### A/B 对比

```python
cmp = bp.compare_strategies(
    returns, signal_a, signal_b,
    name_a='mom', name_b='rev',
    cost_bps=5.0, bootstrap_n=1000, seed=0,
)
print(cmp['sharpe_diff'])
print(cmp['diff_ci'])
print(cmp['p_value_diff'])  # 差 > 0 的置换检验
```

### Sharpe CI / 置换检验(单独可用)

```python
lo, mid, hi = bp.bootstrap_sharpe_ci(returns, n=1000, seed=0, periods=252)
p_value = bp.permutation_test_sharpe(returns, n=500, seed=0)
```

### BacktestResult 字段

```
name, n_obs, metrics (dict), regime_breakdown, sharpe_ci, p_value_vs_zero,
cumulative_return, annualized_return, annualized_vol, max_drawdown,
win_rate, n_trades_per_year, turnover, regime_state, notes
```

### 注意事项

- signal 是同长度 ±1/0 序列(超出部分自动截断)
- `compute_backtest` 自动 `.tolist()` returns(因 `compute_drawdown` 不接受 numpy)
- 短样本 (< 10 观测) 安全降级,返回 `notes=["样本不足"]`
- 含 NaN 自动 drop,`n_obs` 反映有效样本数
- HMM regime 拆分需要 `returns.size >= 30`(HMM 最低要求)

---

## 性能 / 数值稳定性

| 模块 | 关键优化 | 已知陷阱 |
|---|---|---|
| `risk_metrics_pro` | PSI bootstrap / 数值稳定的 Norm CDF | `compute_drawdown` 需 list |
| `market_regime_hmm` | K=3 标量特化 / clip(-50,50) / NaN 兜底 | BOCPD 第一步是初始化高点 |
| `backtest_pro` | block bootstrap 块大小 √T / 纯 numpy | IID permutation 保留原分布,p 不一定小 |

---

## 测试

```bash
cd backend
PYTHONPATH=src_python python3 -m pytest \
    tests/test_risk_metrics_pro.py \
    tests/test_market_regime.py \
    tests/test_market_regime_hmm.py \
    tests/test_backtest_pro.py
# 129 passed in ~40s
```

---

## 整合示例(regime → strategy → backtest)

```python
import numpy as np
import market_regime_hmm as mrh
import backtest_pro as bp

# 1. 识别 regime
state = mrh.classify_regime_hmm(returns, n_iter=10)
viterbi = state.viterbi_path

# 2. 构造 regime-conditional signal
signal = np.array([
    1.0 if r == 'bull' else
   -1.0 if r == 'bear' else
    0.0
    for r in viterbi
])

# 3. 回测 + walk-forward + 对比
res = bp.compute_backtest(returns, signal, cost_bps=5.0, use_regime_hmm=False)
wf = bp.walk_forward_backtest(returns, lambda fr, tr: signal, cost_bps=5.0)
bh = np.ones_like(returns)
cmp = bp.compare_strategies(returns, signal, bh, cost_bps=5.0)

print(f"Sharpe = {res.metrics['sharpe']:.2f} (95%CI {res.sharpe_ci[0]:.2f}, {res.sharpe_ci[2]:.2f})")
print(f"Walk-forward OOS degradation = {wf.oos_degradation*100:+.1f}%")
print(f"vs B&H: diff = {cmp['sharpe_diff']:.2f} (p = {cmp['p_value_diff']:.3f})")
```

---

## 后续可做(本 PR 范围外)

- 把 `walk_forward_backtest` 接入 `quick_sim_trades` 做历史 5 折 OOS 验证
- 把 `compare_strategies` 接入 `backtest_stats.py` 的 report 流水线
- 在 `factor_engine.py` 用 `state.factor_weights` 做 regime-conditional 加权
- 修 `risk_metrics.compute_drawdown` 接受 numpy(向后兼容风险)

---

## 4. backtest_pro CLI(本轮新增)

`backtest_pro.py` 现在支持 CLI 模式,与 `backtest_stats.py` 保持一致 pattern。

### 启动

```bash
cd backend
PYTHONPATH=src_python python3 src_python/backtest_pro.py --help
```

### 用法示例

```bash
# 1. 默认合成数据(无 --data)-- momentum vs buy_hold
python3 src_python/backtest_pro.py --synthetic bull_bear --signal momentum

# 2. JSON 数据文件 + walk-forward + JSON 输出
python3 src_python/backtest_pro.py \
    --data returns.json \
    --signal mean_revert --lookback 20 \
    --compare buy_hold \
    --walk-forward --n-folds 5 \
    --cost-bps 5 --bootstrap-n 1000 \
    --format json --out report.json

# 3. 列出所有 flag
python3 src_python/backtest_pro.py --help
```

### 内置 signal 工厂

| 名称 | 含义 | 用途 |
|---|---|---|
| `buy_hold` / `bh` | 全 1 | 基准 |
| `zero` / `flat` | 全 0 | 空仓基准(测成本) |
| `momentum` / `mom` | lookback 天均值 > 0 → 1 | 趋势跟随 |
| `mean_revert` / `rev` | lookback 天均值 > 0 → -1 | 均值回归 |

### 内置合成数据(无 --data 时)

| 模式 | 含义 |
|---|---|
| `bull_bear` (默认) | 前半段 +0.2%/天,后半段 -0.1%/天 |
| `iid` | N(0.0005, 0.01) |
| `trending` | 0.0003·t + N(0, 0.01) |

### 报告结构(JSON 格式)

```json
{
  "data_source": "...",
  "n_obs": 500,
  "signal_method": "momentum",
  "compare_to": "signal:buy_hold",
  "a": { "name": "signal:momentum", "metrics": {...}, "sharpe_ci": [...], ... },
  "comparison": { "sharpe_diff": 0.14, "diff_ci": [0.00, 0.05, 0.38], "p_value_diff": 0.0 },
  "walk_forward": { "n_folds": 4, "oos_degradation": 0.192, ... }
}
```

`--format md` 输出可读 Markdown 报告(3 段:主回测 / Walk-Forward / 策略比较)。

---

## 5. risk_metrics_pro bug fix:`tail_ratio` 在全负收益时崩溃

### 触发条件

```python
rmp.tail_ratio([-0.01] * 100)  # ValueError: expected a positive input, got 0.0
```

### 根因

`tail_ratio` 公式:`tr = p95 / |p5|`,然后 `p = 2·(1 − Φ(|log(tr)|·√n/2))`。当 `p95 ≤ 0`(全负收益),`tr ≤ 0`,`log(0)` → ValueError。原代码只 guard 了 `p5`,没 guard `p95`。

### 修复

```python
# risk_metrics_pro.py tail_ratio
tr = p95 / abs(p5)
if tr <= 1e-12 or not math.isfinite(tr):
    return MetricResult(
        value=0.0, ci_95=None, p_value=1.0, n_observations=n,
        deflated_by_trials=False,
        interp="P95 ≤ 0:策略收益几乎全负,无尾比意义(全 0)",
    )
```

### 测试

```python
# test_risk_metrics_pro.py TestTail
def test_all_negative_returns_no_crash(self): ...
def test_near_zero_p95_no_log_zero(self): ...
```

这次 bug 是在 `test_cli_main_synthetic_momentum` 跑通时实际触发的——`momentum` 在 `bull_bear` 合成数据上后期熊市仓位 0,导致 strategy returns 全为 0 或负。

---

## 6. backtest_stats.py numpy 加速(PR-4)

(已在上一轮完成,此处简述)

| 函数 | 优化前 | 优化后 | 倍数 |
|---|---|---|---|
| `_spearman` (n=5000) | 424.6 ms | 40.9 ms | **10.4x** |
| `_equity_curve` (n=10000) | 453.0 ms | 38.6 ms | **11.7x** |
| `_percentile` (n=10000) | 30.8 ms | 25.8 ms | 1.2x(保留纯 Python) |

关键发现:
- `np.bincount` 在 index 0 返回 dummy,需 `[1:]` 切掉
- `np.percentile` 在小数据上比 Timsort 慢,纯 Python `sorted()` + lookup 反而更快
- `np.std(ddof=1)` 浮点误差 `≈ 1e-18`,需 `> 1e-12` epsilon 兜底




## §7 backtest_pro 接入 trident DB (PR-6)

### 7.1 集成入口
- `fetch_decisions_as_arrays(conn, lookback_days, asset, require_settled)` — DB → `(returns, signal, meta)` 三元组
- `backtest_from_db(db_path, ...)` — 端到端:拉数据 + 跑 backtest + walk_forward + vs buy_hold
- `_action_to_signal(action)` — `BUY/SELL/HOLD` → `+1/-1/0`,未识别动作默认 0
- `_resolve_db_path(arg_path)` — 优先级:CLI > `config.DB_PATH` > `backend/trident.db`

### 7.2 CLI 新增 flag
```
--db PATH            SQLite 路径(从 ai_decisions 拉已 settle 的 forward_pnl + suggested_action)
--db-days N          回看天数(默认 30)
--db-asset SYMBOL    单资产过滤,例如 BTCUSDT
```

### 7.3 行为约定
- 只读 `settled=1` 的决策(`require_settled=True` 是 `backtest_from_db` 默认)
- `forward_pnl` 字段在 DB 里是**百分数**,集成时 `/100` 转换成小数
- `n_with_pnl < 20` → 返回带 `warning` 字段的 report,`backtest/wf/comparison` 都为 `None`,exit 0
- `walk_forward` 阈值 `n >= 60`(否则 `min_train_size=30` × 2 折都凑不齐)
- 实际数据状态:`trident_event_bus.db` 有 292 个 decisions 但**全 unsettled**,目前对真实 DB 跑会返回 `n=0` warning
- 等系统 settle 流程跑通后,直接 `python3 src_python/backtest_pro.py --db trident_event_bus.db` 即可生成完整报告

### 7.4 端到端 smoke
合成 150 settled decisions(55% 胜率)→ `backtest_from_db` 输出:
- main backtest: sharpe=-0.69(随机信号确实亏)
- walk-forward: n_folds=2(150 折 5 不够,自动减),oos_sharpe=1.38,degradation=-0.34
- vs buy_hold: sharpe_diff=+6.77, p=1.0(buy_hold 完胜随机 AI)

### 7.5 关键设计
- **不复制 `backtest_stats.fetch_decisions`**:`backtest_pro` 自己写 SQL,因为只需要 5 个字段(forward_pnl, suggested_action, settled, target_asset, created_at),不用全表
- **`signal` 复用全样本**:DB 模式下 `walk_forward` 没真正 retrain,只是用全 signal 切片到每折。生产环境应该 retrain,简化起见先这样
- **CLI 走最短路径**:`--db` 不调 `_make_signal`/`compute_backtest`,直接 `json.dumps(report)`,避免不必要的 numpy 计算
- **`n_with_pnl < 20` 是软阈值**:小于 20 时给 warning 报告而非 raise,让用户看到 meta 列表自己判断
