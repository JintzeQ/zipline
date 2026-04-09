# Cybotrade 5m 策略研究（目標：年化 >30%、最大回撤 <20%）

這版流程會「持續研究」：
1. 先用基礎參數網格跑一輪
2. 若未達標，對前幾名參數做隨機微調再跑下一輪
3. 直到達標或達到 `--research-rounds` 上限

## 前置條件

- Python 3.12+
- 有效 Cybotrade API key（若要在線抓資料）

## 安裝

```bash
python3.12 -m pip install -r research/requirements_cybotrade.txt
```

## 執行方式 A：直接抓 Cybotrade 5m

```bash
API_KEY=你的key python research/cybotrade_5m_scalping_research.py \
  --topic 'bybit-linear|candle?interval=5m&symbol=BTCUSDT' \
  --start 2024-01-01 \
  --end 2026-01-01 \
  --min-cagr 0.30 \
  --max-drawdown-limit -0.20 \
  --research-rounds 20 \
  --samples-per-round 80
```

## 執行方式 B：使用既有 CSV（不打 API）

```bash
python research/cybotrade_5m_scalping_research.py \
  --candles-csv research/output/candles_5m.csv \
  --min-cagr 0.30 \
  --max-drawdown-limit -0.20 \
  --research-rounds 20
```

## 策略族群

- `vwap_reversion`
- `vol_breakout`
- `ema_pullback`

## 主要輸出

- `research/output/candles_5m.csv`
- `research/output/iterative_test_results.csv`（每輪 OOS 測試結果）
- `research/output/qualified_strategies.csv`（符合門檻）
- `research/output/best_strategy.json`（若有達標策略）

## API 常見錯誤（依 Cybotrade 文件）

- `API key invalid or missing permissions`：檢查 key 與 datasource 權限
- `AUTH server unavailable` / `Connection timeout`：重試、改 DNS、必要時 VPN

來源：
- https://docs.cybotrade.rs/python/getting-started/overview
- https://docs.cybotrade.rs/python/faq

> 重要：無法保證任何策略一定達標，腳本會在搜尋上限內盡量研究；若仍未達標，代表該資料區間/成本假設下尚無穩健方案。

## 一鍵報告輸出

研究完後，直接執行：

```bash
python research/generate_strategy_report.py \
  --input research/output/iterative_test_results.csv \
  --output-dir research/output \
  --top-n 10
```

會輸出：
- `research/output/strategy_report.md`
- `research/output/strategy_report_summary.json`
