# Strategy Specification

## 1. Thesis

Low-liquidity Binance USDT-M altcoin futures can move sharply when new aggressive capital enters the market. If price moves quickly, volume expands, and open interest increases, the move may be driven by new leveraged positioning rather than only passive closing flow.

The strategy attempts to follow that short-term continuation.

The aggressive version uses a probe position:

```text
first signal -> small probe entry
confirmation -> add position
failure -> fast exit
```

The probe is treated as paying for information. The strategy should only reach full planned size when price, volume, open interest, and taker flow continue to agree.

## 2. Market Universe

Target market:

- Binance USDT-M perpetual futures.
- Low to medium liquidity altcoins.
- Exclude BTC, ETH, and very deep major pairs for the main strategy.
- Exclude symbols with unacceptable spread, depth, abnormal funding, trading halt risk, or announcement risk.

Suggested filters:

```text
quote asset: USDT
contract type: perpetual
24h quote volume: above minimum liquidity threshold
spread: below maximum threshold
top-of-book/depth: enough to fill planned order size with acceptable slippage
funding rate: not extremely one-sided
```

## 3. Signal Inputs

### Price Momentum

Use WebSocket mini ticker for broad market scanning:

```text
wss://fstream.binance.com/market/stream?streams=!miniTicker@arr
```

Track rolling windows:

```text
10s price change
30s price change
60s price change
```

Example trigger:

```text
long candidate:
  10s return > 0.8%
  or 30s return > 1.5%
  or 60s return > 2.5%

short candidate:
  10s return < -0.8%
  or 30s return < -1.5%
  or 60s return < -2.5%
```

Exact thresholds must be learned from data.

### Volume Expansion

After a symbol becomes a candidate, fetch recent klines:

```text
GET /fapi/v1/klines?symbol=SYMBOL&interval=1m&limit=30
```

Kline fields include:

- total base volume
- taker buy base volume
- taker buy quote volume

Derived values:

```text
volume_ratio = current_1m_volume / average_previous_20_1m_volume
taker_buy_ratio = taker_buy_base_volume / total_base_volume
taker_sell_ratio = 1 - taker_buy_ratio
```

Example confirmation:

```text
volume_ratio >= 2.0
long: taker_buy_ratio >= 0.58
short: taker_sell_ratio >= 0.58
```

### Open Interest

Use two OI layers.

Current OI for real-time confirmation:

```text
GET /fapi/v1/openInterest?symbol=SYMBOL
```

Historical OI for 5m baseline:

```text
GET /futures/data/openInterestHist?symbol=SYMBOL&period=5m&limit=30
```

Important distinction:

- `openInterest` is useful for current sampling.
- `openInterestHist` has coarse periods, minimum 5m, and should be used as a baseline rather than the first real-time trigger.

Suggested derived values:

```text
oi_delta = current_oi - previous_sampled_oi
oi_delta_pct = oi_delta / previous_sampled_oi
oi_volume_ratio = abs(oi_delta) / current_volume
oi_baseline_ratio = latest_5m_oi_delta / average_previous_5m_oi_delta
```

## 4. Entry Logic

### Long Probe Entry

Enter a small probe long when:

```text
price_return > threshold
volume_ratio > threshold
taker_buy_ratio > threshold
current_oi > previous_sampled_oi
spread <= max_spread
estimated_slippage <= max_slippage
symbol is not in cooldown
```

Suggested initial size:

```text
probe size = 10%-30% of planned position
```

### Short Probe Entry

Enter a small probe short when:

```text
price_return < -threshold
volume_ratio > threshold
taker_sell_ratio > threshold
current_oi > previous_sampled_oi
spread <= max_spread
estimated_slippage <= max_slippage
symbol is not in cooldown
```

## 5. Add-On Logic

Only add after the market confirms the probe.

Long add conditions:

```text
price remains above trigger price
price breaks a fresh short-term high
OI continues to increase
volume remains elevated
taker_buy_ratio remains above 0.55
position has not reached max planned size
```

Short add conditions are symmetrical.

Example scaling:

```text
probe: 20%
first confirmation: add to 60%
second confirmation: add to 100%
```

## 6. Exit Logic

### Fast Failure Exit

Long failure:

```text
price falls below trigger price by 0.3%-0.8%
or no continuation after 30-60 seconds
or taker_buy_ratio drops below 50%
or OI increases while price stalls
```

Short failure:

```text
price rises above trigger price by 0.3%-0.8%
or no continuation after 30-60 seconds
or taker_sell_ratio drops below 50%
or OI increases while price stalls
```

### Hard Stop

Every position must have a hard maximum loss:

```text
single trade risk <= 0.2%-0.5% of account equity
```

Use expected slippage when sizing the position. Do not size using only theoretical stop distance.

### Take Profit

Suggested partial exits:

```text
profit reaches 0.6R-1R: close 30%-50%
profit reaches 1.5R-2R: close another 30%
remaining size: trail by recent 15-30s swing high/low
```

Exit aggressively when:

```text
price continues but OI stops increasing
volume expands but price progress slows
spread widens suddenly
depth disappears
funding or liquidation environment becomes extreme
```

## 7. Risk Controls

Account-level limits:

```text
max risk per trade: 0.2%-0.5%
max daily realized loss: 2%-3%
max consecutive losses: 3
cooldown after max consecutive losses: 30-60 minutes
max simultaneous positions: 2-3
max same-direction exposure: configurable
```

Symbol-level limits:

```text
max spread: 0.15%-0.30%
min depth multiplier: visible depth should be 5x-10x planned order size
cooldown after stop loss: 5-15 minutes
cooldown after abnormal slippage: 30-60 minutes
disable symbol if slippage > 2x estimate
```

System-level limits:

```text
disable trading if websocket stale
disable trading if REST latency too high
disable trading if exchange errors spike
disable trading if local clock is too far from exchange time
disable trading if account state cannot be confirmed
```

## 8. Signal Score

Use a score to decide whether to enter, add, or ignore.

Example:

```text
score =
  price_momentum_score
  + volume_expansion_score
  + oi_growth_score
  + oi_volume_ratio_score
  + taker_imbalance_score
  - spread_penalty
  - slippage_penalty
  - overextension_penalty
  - cooldown_penalty
```

Suggested actions:

```text
score >= 70: allow probe
score >= 85: allow add-on
score < 50 while in position: consider exit
```

The actual weights should be fit from event data, not guessed permanently.

## 9. Backtest And Paper Trading Metrics

For every detected event, record:

```text
symbol
timestamp
direction
trigger price
price return windows
volume ratio
taker buy/sell ratio
current OI
OI delta
spread
estimated depth
score
future return at 30s, 1m, 3m, 5m
max favorable excursion
max adverse excursion
simulated fill price
simulated exit price
fees
slippage
net PnL
```

Minimum analysis:

```text
win rate
average win
average loss
profit factor
expectancy
max drawdown
signal frequency
performance by symbol
performance by time of day
performance by volume bucket
performance by OI bucket
```

The strategy should not move to real trading until paper trading shows positive expectancy after realistic fees and slippage.

