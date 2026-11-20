# Yahoo Finance v8 兜底运行手册

> **适用范围**：XAU / XAG / WTI / HG / BTC / ETH / SOL / DXY / VIX 等 okx_swap 类资产
> 在 OKX 直连 SNI 阻断或无代理场景下，自动降级到 Yahoo Finance v8 chart API。
> 上线时间：2026-10-05

---

## 1. 体系概览

```
                ┌─────────────────────────────────────────────┐
                │   fetch_asset_candles_with_yahoo_fallback() │
                │   （统一路由函数，三处入口都调它）           │
                └────────────────────┬────────────────────────┘
                                     │
                ┌────────────────────┴────────────────────┐
                │                                         │
        ┌───────▼────────┐                       ┌────────▼────────┐
        │  OKX 永续合约  │  ─── 成功 ───►        │  返回 rows       │
        │  fetch_okx     │                       │  （实时优先）    │
        │  _candles()    │  ─── 失败 ───►        │                  │
        └────────────────┘                       └────────┬────────┘
                                                         │ fallback
                                                ┌────────▼────────┐
                                                │ Yahoo v8 chart  │
                                                │ GC=F / SI=F 等  │
                                                │ 限速+熔断+重试  │
                                                └────────┬────────┘
                                                         │
                                                ┌────────▼────────┐
                                                │ 都失败 → None   │
                                                │ (不合成假 bar)  │
                                                └─────────────────┘
```

**三处入口**（每次都自动走 OKX→Yahoo 兜底）：

| 入口 | 文件:行 | 触发场景 |
|---|---|---|
| `market_snapshot._fetch_asset_klines` | `src_python/market_snapshot.py:312` | 行情快照生成（每次 cron tick） |
| `ai_worker._build_evidence_for_asset` | `src_python/engine/ai_worker.py:600` | AI 决策前证据构建 |
| `ai_worker._compute_market_style` | `src_python/engine/ai_worker.py:655` | 市场风格感知 |
| `knowledge_base._fetch_yahoo_wti_klines` | `src_python/knowledge_base.py:49` | WTI 知识库快照（已重构为复用 quote_sources） |

---

## 2. Yahoo 限制与铁律

| 限制 | 数据 | 影响 |
|---|---|---|
| **15-20 分钟延迟** | Yahoo 期货合约（GC=F/SI=F/CL=F）相比现货 ~15min 延迟 | 实时信号会错过开盘瞬间；paper_trading 用 OKX 优先即可 |
| **合约 vs 现货基差 ~$1-3** | GC=F（黄金期货）vs XAU/USD 现货 | XAU 主战场合约价约 4138-4142 vs 现货 4139.28，存量为合约价 |
| **CME 交易时段** | 周末/节假日 Yahoo 返回空 / 全 None bar | 必须过滤 None close（不能污染 verifier） |
| **429 限速极严** | 实测连发 3 次必触发 | 内置 3s 跨请求最小间隔 + 熔断 |
| **查询 1.finance 间歇** | 直连 ~50% 间歇成功率 | 内置 4 次重试（2/4/8s 退避） |

**铁律**（已落地）：
1. **不合成假 K 线**：OKX 和 Yahoo 都失败 → 返回 None，让调用方决定如何处理
2. **filter None bar**：CME 闭市返回的 None close 必须丢弃（不然 verifier 看到假数据）
3. **限速是硬需求**：低于 3s 跨间隔必触发 429 → 更严限速 → 雪崩
4. **熔断必须**：连续 3 次失败 → 90s 短路，避免每请求等满 10s 超时

---

## 3. 数据格式

**Yahoo v8 chart 响应**：
```json
{
  "chart": {
    "result": [{
      "meta": {"symbol": "GC=F", "regularMarketPrice": 4139.28},
      "timestamp": [1762233600, 1762237200, ...],
      "indicators": {
        "quote": [{
          "open":  [4138.5, null, 4139.0, ...],
          "high":  [4139.8, null, 4140.1, ...],
          "low":   [4138.0, null, 4138.7, ...],
          "close": [4139.2, null, 4139.5, ...],
          "volume":[1234,   0,    5678,   ...]
        }]
      }
    }],
    "error": null
  }
}
```

**统一输出**（与 OKX 格式对齐）：
```python
[(ts_ms, open, high, low, close, volume), ...]
---

## 4. 资产 → Yahoo 符号映射

| 资产 | Yahoo 符号 | 合约 |
|---|---|---|
| XAU (黄金) | `GC=F` | COMEX 黄金期货主力 |
| XAG (白银) | `SI=F` | COMEX 白银期货主力 |
| WTI (原油) | `CL=F` | NYMEX WTI 原油期货主力 |
| HG (铜) | `HG=F` | COMEX 铜期货主力 |
| BTC | `BTC-USD` | 现货（Yahoo 期货不稳定） |
| ETH | `ETH-USD` | 现货 |
| SOL | `SOL-USD` | 现货 |
| DXY | `DX-Y.NYB` | ICE 美元指数期货 |
| VIX | `^VIX` | CBOE 波动率指数（caret 需 URL 转义为 `%5EVIX`） |

代码位置：`quote_sources.YAHOO_SYMBOL_FOR_ASSET`（13 个条目）

---

## 5. 周期映射

| OKX 周期 | Yahoo 周期 | 说明 |
|---|---|---|
| `1m` | `1m` | |
| `3m` | `3m` | |
| `5m` | `5m` | |
| `15m` | `15m` | |
| `30m` | `30m` | |
| `1H` / `1h` | `60m` | 大写归一 |
| `4h` | `60m` | Yahoo 无 4h，降级为 1h（自动聚合） |
| `1D` / `1d` | `1d` | |
| `1W` / `1w` | `1wk` | Yahoo 用 1wk 不是 1w |
| 其他 | 不映射 → Yahoo 拒绝 | |

代码位置：`quote_sources._OKX_TO_YAHOO_INTERVAL`

---

## 6. 运行命令

### 6.1 探活（首推）
```bash
cd /Users/claire/.cline/data/workspaces/chat/trident_v170/backend
python3 scripts/backfill_xau_yahoo.py --dry-run --probe
```

输出：
```
[probe] symbol=GC=F interval=60m range=5d
[probe] Yahoo HTTP 200 OK, first close = 4139.20, last close = 4139.85
```

如果看到 `Yahoo 不可达` 或 `HTTP 429`，等待 1-2 分钟后重试（SNI 阻断恢复）。

### 6.2 回填 XAU 60 天 60m K 线
```bash
python3 scripts/backfill_xau_yahoo.py --symbol XAU --interval 60m --days 60
```

参数：
- `--symbol {XAU,XAG,WTI,HG,BTC,ETH,SOL,DXY,VIX}`（默认 XAU）
- `--interval {1m,5m,15m,30m,1h,4h,1d,1w}`（默认 60m，4h 自动降级为 1h）
- `--days N`（默认 60，最长 ~730 因 Yahoo 限制）
- `--page-days N`（每页天数，默认 60；越小越稳，越大越快）
- `--dry-run`（只看会拉多少行，不写入 DB）
- `--probe`（拉 1 行验证可用性）

### 6.3 多资产批量回填
```bash
for sym in XAU XAG WTI HG BTC ETH SOL; do
  python3 scripts/backfill_xau_yahoo.py --symbol $sym --interval 1d --days 365
done
```

### 6.4 生产环境启用/禁用
```bash
# .env 中配置（默认开）
YAHOO_FALLBACK_ENABLED=true
YAHOO_MIN_GAP_SECONDS=3
YAHOO_BREAKER_AFTER=3
YAHOO_BREAKER_COOLDOWN_S=90

# 完全关闭（强制只用 OKX）—— v2026.10.05+ 暂未实现 env 开关，
# 当前为硬编码开启；如需关闭请注释 fetch_asset_candles_with_yahoo_fallback 调用
```

---

## 7. 监控与告警

### 7.1 Yahoo 调用日志
正常调用无 INFO 级别日志。如看到以下日志**说明限速/熔断在生效**：
```
[YAHOO] throttle sleep 2.34s (min_gap=3.0s, since_last=0.66s)
[YAHOO-BREAKER] 连续失败 3 次（params={...}），熔断 90.0s | err=RuntimeError: HTTP 429
```

### 7.2 数据源告警（`source_status`）
- Yahoo 不可达 → 走 OKX 路径 → OKX 也失败 → `source_status` 表的 `kline_bars` 写入 None
- 不会触发单独告警（fallback 自身设计），但 `kline_bars.placeholder_bar=1` 的存量会在真实数据回流时被自然覆盖

### 7.3 关键 SQL（检查覆盖率）
```sql
-- 仍是占位 bar 的 XAU/XAG 行数（应单调递减）
SELECT
  asset_id,
  COUNT(*) FILTER (WHERE placeholder_bar = 1) AS placeholder_count,
  COUNT(*) AS total
FROM kline_bars
WHERE asset_id IN ('XAU','XAG','WTI','HG')
GROUP BY asset_id;

-- Yahoo 回填写入的行数（last_source='yahoo'）
SELECT
  asset_id,
  COUNT(*) AS yahoo_rows,
  MAX(ts) AS latest_ts
FROM kline_bars
WHERE last_source = 'yahoo'
GROUP BY asset_id;
```
```
---

## 8. 故障排查

### Q1: 脚本一直 FAIL（Yahoo 不可达）

**症状**：
```
[probe] Yahoo 不可达
[ERROR] RuntimeError: SSL EOF, HTTP 429, ECONNRESET
```

**排查**：
```bash
# 1. 测网络
curl -I "https://query1.finance.yahoo.com/v8/finance/chart/GC=F?range=5d&interval=60m"

# 2. 如果 SSL EOF → SNI 阻断，等待 1-2 分钟自动恢复
# 3. 如果 429 → 限速，等待 90s（熔断窗口）再试
# 4. 如果 ECONNRESET → 网络抖动，重试即可
```

### Q2: 4139.28 占位 bar 没被覆盖

**症状**：XAU 仍是占位 bar（OHLC 全 4139.28）

**原因**：
- Yahoo 返回的 close 范围和占位 bar 的 ts 不重叠（Yahoo 给的是连续 5 天窗口）
- 或 Yahoo 在 CME 闭市时段返回空（周末/节假日）

**修复**：
```bash
# 拉长 days 覆盖更宽范围
python3 scripts/backfill_xau_yahoo.py --symbol XAU --interval 60m --days 180

# 如果需要彻底覆盖，先清理占位 bar（⚠️ 破坏诚实性铁律 3）
# DELETE FROM kline_bars WHERE asset_id='XAU' AND placeholder_bar=1;
```

### Q3: Yahoo 返回 None 太多

**症状**：60% 的 bar close 为 None

**原因**：
- Yahoo 在 CME 闭市（周末/节假日）确实没有数据
- 这是预期行为，filter 已丢弃

**验证 None 过滤是否生效**：
```bash
python3 -c "
import sys; sys.path.insert(0, 'src_python')
import quote_sources as q
rows = q.fetch_yahoo_candles('GC=F', interval='1d', range_or_period='1mo')
print('total bars:', len(rows))
print('with close:', sum(1 for r in rows if r[4] is not None))
"
```

### Q4: 熔断卡住，新请求一直返回 None

**症状**：明明 Yahoo 恢复了，但连续几秒仍返回 None

**原因**：连续 3 次失败后短路 90s

**修复**：等待 90s 或重启进程（熔断状态在内存中）

### Q5: Yahoo 改版导致解析失败

**症状**：`[ERROR] KeyError: 'quote'` 或 `KeyError: 'timestamp'`

**修复**：
1. 检查 `quote_sources._parse_yahoo_chart()` 的健壮性（已加 try/except）
2. 真实响应保存在 `/tmp/yahoo_failed_*.json`（如果开了 dump）
3. 报 issue 附上原始 JSON

---

## 9. 测试矩阵

`tests/test_yahoo_fallback.py`（23 个测试全绿）：

| 测试 | 覆盖 |
|---|---|
| `test_fetch_yahoo_candles_success_basic` | 正常解析 Yahoo v8 响应 |
| `test_fetch_yahoo_candles_limit_truncates_tail` | limit 截尾 |
| `test_fetch_yahoo_candles_yahoo_unreachable` | 网络不可达 → None |
| `test_fetch_yahoo_candles_empty_result` | Yahoo 返回空 result → None |
| `test_fetch_yahoo_candles_caret_ticker_escaped` | `^VIX` URL 转义 `%5EVIX` |
| `test_fetch_yahoo_candles_period_overrides_range` | period1/period2 覆盖 range |
| `test_fetch_yahoo_candles_all_none_returns_none` | 全 None → None（不返回污染数据） |
| `test_yahoo_interval_mapping` | OKX→Yahoo 周期映射 |
| `test_yahoo_symbol_mapping_covers_main_assets` | 9 个主战场资产映射覆盖 |
| `test_yahoo_symbol_mapping_xau_uses_gold_futures` | XAU→GC=F 验证 |
| `test_with_fallback_okx_success_no_yahoo_call` | OKX 成功时不调 Yahoo |
| `test_with_fallback_okx_fails_uses_yahoo` | OKX 失败自动降级 Yahoo |
| `test_with_fallback_both_fail_returns_none` | 都失败 → None |
| `test_with_fallback_no_yahoo_mapping_returns_none` | 无映射资产 → None |
| `test_yahoo_throttle_respects_min_gap` | 限速 3s |
| `test_yahoo_breaker_via_get_json` | 熔断 open（monkeypatch 真实熔断） |
| `test_yahoo_get_json_circuit_breaker` | 熔断状态下新调用短路 |
| `test_okx_to_yahoo_interval_covers_main_bars` | OKX→Yahoo 12 个周期覆盖 |
| `test_market_snapshot_okx_uses_yahoo_fallback` | market_snapshot 接通 |
| `test_market_snapshot_binance_branch_unchanged` | Binance 分支未受影响 |
| `test_ai_worker_build_evidence_uses_yahoo_fallback` | ai_worker 接通 |
| `test_ai_worker_market_style_uses_yahoo_fallback` | ai_worker market_style 接通 |
| `test_knowledge_base_wti_uses_quote_sources_yahoo` | knowledge_base WTI 重构 |

---

## 10. 已知限制

1. **实时性**：15-20 分钟延迟，对 1m/5m 高频信号不可用
2. **合约价 vs 现货**：XAU 占位 bar 4139.28 是合约价；Yahoo 返回的 GC=F 也是合约价 → 误差小，**直接覆盖是安全的**
3. **周末数据**：CME 闭市，Yahoo 返回 None bar，已 filter 丢弃
4. **环境变量**：当前为硬编码开启，未实现 `YAHOO_FALLBACK_ENABLED=false` 运行时切换（需要重启或加 ENV 读取）
5. **熔断状态**：进程内共享；不同进程独立计数（多 worker 部署会重复熔断）
6. **测试 SNI**：开发环境实测 ~50% 间歇成功率；生产环境（阿里云 HK/EU）通常 ≥95%

---

## 11. 相关文件

- `src_python/quote_sources.py`：Yahoo fetcher 实现（限速 + 熔断 + 重试 + 过滤）
- `src_python/market_snapshot.py`：行情快照入口
- `src_python/engine/ai_worker.py`：AI 决策证据入口
- `src_python/knowledge_base.py`：WTI 知识库（已重构复用 quote_sources）
- `scripts/backfill_xau_yahoo.py`：回填脚本（dry-run/probe/分页）
- `tests/test_yahoo_fallback.py`：23 个测试
- `.env`：配置示例（YAHOO_FALLBACK_ENABLED 等）

---

最后更新：2026-10-05（首次发布）