# Binance Low-Liquidity Futures Momentum Strategy

This project is for researching, backtesting, paper trading, and eventually live trading a short-term Binance USDT-M futures strategy focused on low-liquidity altcoin contracts.

The core idea:

- Detect fast price movement from Binance futures WebSocket market data.
- Confirm that volume expands.
- Confirm that open interest increases.
- Use taker buy/sell imbalance to infer aggressive direction.
- Enter a small probe position quickly.
- Add only if the market confirms continuation.
- Exit fast when the signal fails.

This is a high-risk research project. Low-liquidity futures can have large spreads, poor depth, extreme slippage, fake breakouts, exchange-side delays, and liquidation spikes. The first production goal is not live trading. The first goal is to collect enough event data to prove whether the signal has positive expectancy after fees and slippage.

## Development Phases

1. **Research logger**
   - Subscribe to Binance futures mini ticker stream.
   - Detect abnormal price moves.
   - Fetch kline, current open interest, and historical open interest context.
   - Store every signal event and the next 30s / 1m / 3m / 5m outcomes.

2. **Backtest**
   - Replay historical candles and OI snapshots where possible.
   - Estimate slippage using spread/depth assumptions.
   - Test thresholds, holding time, stop loss, take profit, and add-on rules.

3. **Paper trading**
   - Run the real-time scanner.
   - Simulate orders against live prices.
   - Track fills, slippage assumptions, latency, and signal quality.

4. **Real trading**
   - Start with tiny size.
   - Enforce strict position, symbol, daily loss, and system health limits.
   - Prefer reduce-only exits and conservative execution.

## Main Data Sources

- WebSocket ticker scan:
  - `wss://fstream.binance.com/market/stream?streams=!miniTicker@arr`
- Current open interest:
  - `GET https://fapi.binance.com/fapi/v1/openInterest?symbol=BTCUSDT`
- Historical open interest baseline:
  - `GET https://fapi.binance.com/futures/data/openInterestHist?symbol=BTCUSDT&period=5m`
- Klines:
  - `GET https://fapi.binance.com/fapi/v1/klines?symbol=BTCUSDT&interval=1m`

## Project Layout

```text
configs/
  strategy.example.yaml
docs/
  strategy.md
src/
  binance_oi_momentum/
    __init__.py
    config.py
    models.py
    scanner.py
    strategy.py
    risk.py
    execution.py
    storage.py
tests/
  test_strategy_rules.py
```

## Quick Start

Create a virtual environment and install the project in editable mode:

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -e ".[dev]"
python3 -m pytest
```

Run the backend scanner in one terminal:

```bash
oi-momentum-scan --config configs/strategy.example.yaml
```

Run the Streamlit dashboard in another terminal:

```bash
streamlit run src/binance_oi_momentum/app.py
```

The current implementation is a non-trading research and paper-trading logger. It subscribes to the Binance all-market mini ticker stream, keeps short rolling windows locally, fetches Kline and `openInterestHist` data only after a price candidate appears, records both candidate checks and accepted signals into SQLite, and opens simulated positions with fixed stop loss, take profit, and max holding time. Do not connect live trading keys until the signal has been measured through backtest and paper trading.

The dashboard `Config` tab includes a `Scanner enabled` switch. Turn it off and save before changing strategy parameters; the backend process stays alive and hot-reloads the YAML, but it stops processing market ticks and will not create new signals or paper positions. Turn it back on and save when you are ready to resume scanning.

## Signal Logic

For each low-liquidity USDT perpetual symbol, the scanner:

- Uses `!miniTicker@arr` WebSocket ticks to maintain a rolling 60s price window.
- Creates a long candidate when the 60s price return is at least `+2%`.
- Creates a short candidate when the 60s price return is at most `-2%`.
- Does not trade immediately after the candidate appears.
- Waits for the current 1m candle to close, then fetches `/fapi/v1/continuousKlines`.
- Requires the latest closed 1m quote volume to be at least `2x` the average quote volume of the previous 30 one-minute candles.
- Requires the closed 1m candle close to be near the extreme of its own high-low range:
  - long: close position >= 95%
  - short: close position <= 5%
- Requires taker buy quote volume ratio >= 60% for longs.
- Requires taker sell quote volume ratio >= 60% for shorts.
- Fetches `/futures/data/openInterestHist` and requires new OI / previous OI to meet the configured threshold.
- Records every candidate check in the dashboard `Log` tab, including price change, Kline volume, taker buy/sell ratio, OI change, score, and reject reason.
- Records every accepted signal and opens a paper position using the closed 1m candle close as entry price.

## Docker Deployment

The Docker setup runs two containers from the same image:

- `oi-momentum-scanner`: backend scanner and paper trader
- `oi-momentum-dashboard`: Streamlit frontend on port `8501`

Both containers share `./data`, so `data/events.sqlite3` persists across restarts.
The dashboard can edit `configs/strategy.example.yaml`; the scanner checks that file every few seconds and hot-reloads strategy/risk/exit settings without rebuilding the Docker image.

### Local Docker Run

```bash
mkdir -p data
docker compose up -d --build
docker compose logs -f scanner
```

Open the dashboard:

```text
http://localhost:8501
```

Useful commands:

```bash
docker compose ps
docker compose logs -f dashboard
docker compose restart scanner
docker compose down
```

After changing parameters in the dashboard `Config` tab, watch for a scanner log line like:

```text
config reloaded from configs/strategy.example.yaml
```

No image rebuild is needed for threshold changes. If you change exchange URLs, mounted paths, or code, restart/rebuild the containers.

### Tencent Cloud Deployment

On a fresh Ubuntu server, install Docker and the Compose plugin:

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl git
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker "$USER"
```

Log out and log back in once so the Docker group takes effect.

Clone and start:

```bash
git clone <your-repo-url> oi-momentum
cd oi-momentum
mkdir -p data
docker compose up -d --build
docker compose logs -f scanner
```

In the Tencent Cloud security group, open TCP port `8501` only to your own IP when possible. Then visit:

```text
http://<server-public-ip>:8501
```

A safer option is to keep port `8501` closed publicly and use an SSH tunnel:

```bash
ssh -L 8501:localhost:8501 ubuntu@<server-public-ip>
```

Then open:

```text
http://localhost:8501
```

If Binance access times out from the server, add proxy environment variables in `docker-compose.yml` under both services, then restart:

```bash
docker compose up -d
```

Back up the SQLite data:

```bash
cp data/events.sqlite3 "data/events-$(date +%Y%m%d-%H%M%S).sqlite3"
```
