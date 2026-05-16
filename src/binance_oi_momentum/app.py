from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import altair as alt
import pandas as pd
import requests
import streamlit as st

from binance_oi_momentum.config import load_config
from binance_oi_momentum.storage import SQLiteStorage, sqlite_path_from_url


DEFAULT_CONFIG = "configs/strategy.example.yaml"


@st.cache_data(ttl=3)
def read_table(database_path: str, query: str) -> pd.DataFrame:
    if not Path(database_path).exists():
        return pd.DataFrame()
    with sqlite3.connect(database_path) as conn:
        return pd.read_sql_query(query, conn)


def format_time_column(frame: pd.DataFrame, column: str) -> pd.DataFrame:
    if column in frame.columns and not frame.empty:
        frame[column.replace("_ms", "")] = pd.to_datetime(frame[column], unit="ms")
    return frame


@st.cache_data(ttl=30)
def fetch_continuous_klines(
    rest_base_url: str,
    symbol: str,
    *,
    interval: str = "1m",
    limit: int = 120,
) -> pd.DataFrame:
    response = requests.get(
        f"{rest_base_url.rstrip('/')}/fapi/v1/continuousKlines",
        params={
            "pair": symbol,
            "contractType": "PERPETUAL",
            "interval": interval,
            "limit": limit,
        },
        timeout=15,
    )
    response.raise_for_status()
    rows = response.json()
    frame = pd.DataFrame(
        rows,
        columns=[
            "open_time_ms",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "close_time_ms",
            "quote_volume_usdt",
            "number_of_trades",
            "taker_buy_volume",
            "taker_buy_quote_volume_usdt",
            "ignore",
        ],
    )
    numeric_columns = [
        "open",
        "high",
        "low",
        "close",
        "volume",
        "quote_volume_usdt",
        "taker_buy_volume",
        "taker_buy_quote_volume_usdt",
    ]
    for column in numeric_columns:
        frame[column] = frame[column].astype(float)
    frame["open_time"] = pd.to_datetime(frame["open_time_ms"], unit="ms")
    frame["taker_sell_quote_volume_usdt"] = (
        frame["quote_volume_usdt"] - frame["taker_buy_quote_volume_usdt"]
    ).clip(lower=0)
    return frame


def add_unrealized_pnl(positions: pd.DataFrame) -> pd.DataFrame:
    if positions.empty or "current_price" not in positions.columns:
        return positions

    positions = positions.copy()
    positions["current_price"] = pd.to_numeric(positions["current_price"], errors="coerce")
    long_mask = positions["direction"] == "long"
    short_mask = positions["direction"] == "short"
    positions["unrealized_pnl_pct"] = 0.0
    positions.loc[long_mask, "unrealized_pnl_pct"] = (
        positions.loc[long_mask, "current_price"] - positions.loc[long_mask, "entry_price"]
    ) / positions.loc[long_mask, "entry_price"]
    positions.loc[short_mask, "unrealized_pnl_pct"] = (
        positions.loc[short_mask, "entry_price"] - positions.loc[short_mask, "current_price"]
    ) / positions.loc[short_mask, "entry_price"]
    positions["unrealized_pnl_usdt"] = (
        positions["notional_usdt"] * positions["unrealized_pnl_pct"]
    ).fillna(0.0)
    positions.loc[positions["status"] != "open", ["unrealized_pnl_pct", "unrealized_pnl_usdt"]] = 0.0
    return positions


def render_strategy_logic(config) -> None:
    signal = config.signal
    universe = config.universe
    risk = config.risk
    exit_config = config.exit
    execution = config.execution

    st.header("Strategy Logic")
    st.markdown(
        """
        这个系统当前是研究和纸面交易模式，用来连续记录信号、模拟进出场，并观察信号触发后
        是否真的存在短线延续性。它不会发送真实 Binance 下单请求。
        """
    )

    st.subheader("Core Hypothesis")
    st.markdown(
        """
        低流动性 USDT 永续小币在短时间内出现价格趋势波动时，如果同时伴随成交额放大、
        open interest 增加，且 OI 增量相对成交额足够大，可能意味着新资金正在主动建立方向性仓位。
        策略尝试跟随这类短线资金流：

        - 价格上涨 + OI 增加 + 放量：顺势做多
        - 价格下跌 + OI 增加 + 放量：顺势做空
        """
    )

    st.subheader("Universe Filter")
    st.markdown(
        f"""
        扫描标的来自 Binance USDT-M perpetual 合约，并按 24h quote volume 过滤低流动性区间：

        - Quote asset: `{universe["quote_asset"]}`
        - 24h quote volume min: `{universe["min_24h_quote_volume"]:,.0f}` USDT
        - 24h quote volume max: `{universe["max_24h_quote_volume"]:,.0f}` USDT
        - Excluded symbols: `{", ".join(universe.get("exclude_symbols") or []) or "none"}`
        """
    )

    st.subheader("Signal Pipeline")
    st.markdown(
        f"""
        1. WebSocket 接收 `!miniTicker@arr` 的全市场价格，用它快速维护短线价格窗口。
        2. 后端在本地维护 `{signal["windows_seconds"]}` 秒价格窗口，计算短线涨跌幅。
        3. 60 秒涨幅 >= `{signal["long_return_thresholds"][60] * 100:.2f}%` 判定做多候选；60 秒跌幅 <= `{signal["short_return_thresholds"][60] * 100:.2f}%` 判定做空候选。
        4. 候选出现后不立刻交易，等待当前 1m K 线收线，并额外等待 `{signal["kline_close_delay_ms"]}` ms 让交易所数据稳定。
        5. 收线后拉 REST `/fapi/v1/continuousKlines` 的 `{signal["kline_interval"]}` K 线。
        6. 做多要求这根 K 的 close 位于本根 K high-low 区间的 `{signal["long_close_position_min"] * 100:.0f}%` 以上。
        7. 做空要求这根 K 的 close 位于本根 K high-low 区间的 `{signal["short_close_position_max"] * 100:.0f}%` 以下。
        8. 最新 K 线 quote volume / 过去 `{signal["kline_lookback"]}` 根平均 quote volume 必须 >= `{signal["volume_ratio_min"]}`。
        9. 触发候选后再拉 REST `/futures/data/openInterestHist`，比较最近两个 5m OI 点。
        10. 新增 OI / 上一个 OI 必须 >= `{signal["oi_delta_pct_min"]:.4f}`。
        11. `OI value delta / recent quote volume` 必须 >= `{signal["oi_value_to_volume_ratio_min"]}`。
        12. 做多时 taker buy quote volume ratio 必须 >= `{signal["taker_buy_ratio_min_for_long"]}`。
        13. 做空时 taker sell quote volume ratio 必须 >= `{signal["taker_sell_ratio_min_for_short"]}`。
        14. 综合 volume、OI、主动买卖比例，score 必须 >= `{signal["score_probe_min"]}`。
        15. 通过风控后，记录信号并开启纸面仓位，入场价使用已收线 1m K 的 close。
        """
    )

    st.subheader("Price Thresholds")
    threshold_rows = []
    for window in signal["windows_seconds"]:
        threshold_rows.append(
            {
                "window_seconds": window,
                "long_return_min_pct": signal["long_return_thresholds"][window] * 100,
                "short_return_max_pct": signal["short_return_thresholds"][window] * 100,
            }
        )
    st.dataframe(pd.DataFrame(threshold_rows), width="stretch", hide_index=True)

    st.subheader("Paper Trading Exits")
    st.markdown(
        f"""
        当前只做固定规则的纸面交易，用于验证信号质量：

        - Probe position fraction: `{execution["probe_position_fraction"]:.2f}` of initial equity
        - Initial equity: `{risk["initial_equity_usdt"]:,.0f}` USDT
        - Stop loss: `{exit_config["stop_loss_pct"] * 100:.2f}%`
        - Take profit: `{exit_config["take_profit_pct"] * 100:.2f}%`
        - Max hold: `{exit_config["max_hold_seconds"]}` seconds
        - Max simultaneous positions: `{risk["max_simultaneous_positions"]}`
        - Max same direction positions: `{risk["max_same_direction_positions"]}`
        - Max daily loss: `{risk["max_daily_loss"] * 100:.2f}%`
        """
    )

    st.subheader("Recorded Data")
    st.markdown(
        """
        每次信号会写入 SQLite，核心字段包括：

        - `price_change_pct`: 触发窗口内价格涨跌幅
        - `quote_volume_usdt`: 最新 1m K 线 quote volume
        - `average_quote_volume_usdt`: 过去 30 根 1m K 线的平均 quote volume
        - `volume_ratio`: 最新 1m K 线成交额相对过去 30 分钟平均成交额的倍数
        - `taker_buy_ratio` / `taker_sell_ratio`: 最新 1m K 线主动买入/主动卖出 quote volume 占比
        - `open_interest`: 触发时最新 OI
        - `open_interest_value_usdt`: 触发时最新 OI value
        - `oi_delta_pct`: 新增 OI / 上一个 OI
        - `oi_delta_value_usdt`: OI value 的 USDT 增量
        - `oi_value_to_volume_ratio`: OI value 增量 / 最近窗口成交额
        - `score`: 综合打分
        - `risk_allowed` / `risk_reason`: 是否通过风控以及原因
        """
    )

    st.warning("当前仍是研究/纸面交易系统，不会真实下单。低流动性合约滑点、插针和假突破风险很高。")


def render_dashboard(
    *,
    heartbeat: pd.DataFrame,
    signals: pd.DataFrame,
    positions: pd.DataFrame,
) -> None:
    now_ms = int(time.time() * 1000)
    heartbeat_age = None
    heartbeat_status = "no data"
    heartbeat_message = ""
    if not heartbeat.empty:
        heartbeat_age = (now_ms - int(heartbeat.iloc[0]["updated_at_ms"])) / 1000
        heartbeat_status = str(heartbeat.iloc[0]["status"])
        heartbeat_message = str(heartbeat.iloc[0]["message"])

    closed_positions = positions[positions["status"] == "closed"] if not positions.empty else positions
    open_positions = positions[positions["status"] == "open"] if not positions.empty else positions
    realized_pnl = 0.0 if closed_positions.empty else float(closed_positions["pnl_usdt"].sum())
    unrealized_pnl = 0.0 if open_positions.empty else float(open_positions["unrealized_pnl_usdt"].sum())
    win_rate = 0.0
    if not closed_positions.empty:
        win_rate = float((closed_positions["pnl_usdt"] > 0).mean() * 100)

    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("Scanner", heartbeat_status, heartbeat_message)
    c2.metric("Heartbeat age", "-" if heartbeat_age is None else f"{heartbeat_age:.0f}s")
    c3.metric("Signals", len(signals))
    c4.metric("Open positions", len(open_positions))
    c5.metric("Unrealized PnL", f"{unrealized_pnl:.2f} USDT")
    c6.metric("Realized PnL", f"{realized_pnl:.2f} USDT", f"{win_rate:.1f}% win")

    if not closed_positions.empty:
        curve = closed_positions.sort_values("exit_time_ms").copy()
        curve["equity_pnl"] = curve["pnl_usdt"].cumsum()
        curve["exit_time"] = pd.to_datetime(curve["exit_time_ms"], unit="ms")
        st.line_chart(curve.set_index("exit_time")["equity_pnl"])

    signals = format_time_column(signals, "created_at_ms")
    positions = format_time_column(positions, "entry_time_ms")
    positions = format_time_column(positions, "exit_time_ms")

    signal_columns = [
        "created_at",
        "symbol",
        "direction",
        "trigger_price",
        "price_change_pct",
        "window_seconds",
        "quote_volume_usdt",
        "average_quote_volume_usdt",
        "volume_ratio",
        "taker_buy_ratio",
        "taker_sell_ratio",
        "open_interest",
        "open_interest_value_usdt",
        "oi_delta_pct",
        "oi_delta_value_usdt",
        "oi_value_to_volume_ratio",
        "score",
        "risk_allowed",
        "risk_reason",
    ]
    position_columns = [
        "entry_time",
        "exit_time",
        "symbol",
        "direction",
        "status",
        "entry_price",
        "exit_price",
        "stop_loss_price",
        "take_profit_price",
        "current_price",
        "notional_usdt",
        "unrealized_pnl_usdt",
        "unrealized_pnl_pct",
        "signal_price_change_pct",
        "signal_open_interest",
        "signal_oi_delta_pct",
        "signal_quote_volume_usdt",
        "signal_volume_ratio",
        "signal_taker_buy_ratio",
        "signal_taker_sell_ratio",
        "pnl_usdt",
        "pnl_pct",
        "exit_reason",
    ]

    st.subheader("Signals")
    st.dataframe(
        signals[[column for column in signal_columns if column in signals.columns]],
        width="stretch",
        hide_index=True,
    )

    st.subheader("Paper Positions")
    st.dataframe(
        positions[[column for column in position_columns if column in positions.columns]],
        width="stretch",
        hide_index=True,
    )


def render_position_chart(config, positions: pd.DataFrame) -> None:
    st.header("Position Chart")
    if positions.empty:
        st.info("No positions yet.")
        return

    labels = []
    for row in positions.itertuples(index=False):
        labels.append(
            f"#{row.id} {row.symbol} {row.direction} {row.status} entry={row.entry_price:g}"
        )

    selected_label = st.selectbox("Position", labels)
    position = positions.iloc[labels.index(selected_label)]

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Entry", f"{position['entry_price']:g}")
    c2.metric("Stop", f"{position['stop_loss_price']:g}")
    c3.metric("Take Profit", f"{position['take_profit_price']:g}")
    c4.metric("Unrealized", f"{position.get('unrealized_pnl_usdt', 0):.2f} USDT")

    try:
        klines = fetch_continuous_klines(
            config.exchange["rest_base_url"],
            position["symbol"],
            interval="1m",
            limit=120,
        )
    except Exception as exc:
        st.error(f"Failed to load kline data: {type(exc).__name__}: {exc}")
        return

    klines = klines.copy()
    klines["candle_color"] = klines.apply(
        lambda row: "up" if row["close"] >= row["open"] else "down",
        axis=1,
    )

    x_encoding = alt.X(
        "open_time:T",
        title="Time",
        axis=alt.Axis(format="%H:%M", labelAngle=0),
    )
    y_encoding = alt.Y("low:Q", title="Price", scale=alt.Scale(zero=False))
    candle_tooltip = [
        "open_time:T",
        "open:Q",
        "high:Q",
        "low:Q",
        "close:Q",
        "quote_volume_usdt:Q",
        "taker_buy_quote_volume_usdt:Q",
        "taker_sell_quote_volume_usdt:Q",
    ]
    wick_chart = (
        alt.Chart(klines)
        .mark_rule()
        .encode(
            x=x_encoding,
            y=y_encoding,
            y2="high:Q",
            color=alt.Color(
                "candle_color:N",
                scale=alt.Scale(domain=["up", "down"], range=["#16a34a", "#dc2626"]),
                legend=None,
            ),
            tooltip=candle_tooltip,
        )
    )
    body_chart = (
        alt.Chart(klines)
        .mark_bar(size=5)
        .encode(
            x=x_encoding,
            y=alt.Y("open:Q", title="Price", scale=alt.Scale(zero=False)),
            y2="close:Q",
            color=alt.Color(
                "candle_color:N",
                scale=alt.Scale(domain=["up", "down"], range=["#16a34a", "#dc2626"]),
                legend=alt.Legend(title="Candle"),
            ),
            tooltip=candle_tooltip,
        )
    )
    levels = pd.DataFrame(
        [
            {"level": "Entry", "price": position["entry_price"]},
            {"level": "Stop Loss", "price": position["stop_loss_price"]},
            {"level": "Take Profit", "price": position["take_profit_price"]},
        ]
    )
    level_color = alt.Scale(
        domain=["Entry", "Stop Loss", "Take Profit"],
        range=["#2563eb", "#dc2626", "#16a34a"],
    )
    level_rules = (
        alt.Chart(levels)
        .mark_rule(strokeDash=[6, 4])
        .encode(
            y="price:Q",
            color=alt.Color("level:N", scale=level_color, title="Level"),
            tooltip=["level:N", "price:Q"],
        )
    )
    level_labels = (
        alt.Chart(levels)
        .mark_text(align="left", dx=8, dy=-4, fontSize=12)
        .encode(
            x=alt.value(12),
            y="price:Q",
            text="level:N",
            color=alt.Color("level:N", scale=level_color, legend=None),
        )
    )

    point_rows = [
        {
            "event": "Entry",
            "time": pd.to_datetime(position["entry_time_ms"], unit="ms"),
            "price": position["entry_price"],
        }
    ]
    if pd.notna(position.get("exit_time_ms")) and pd.notna(position.get("exit_price")):
        point_rows.append(
            {
                "event": f"Exit: {position.get('exit_reason') or 'closed'}",
                "time": pd.to_datetime(position["exit_time_ms"], unit="ms"),
                "price": position["exit_price"],
            }
        )
    points = pd.DataFrame(point_rows)
    event_color = alt.Scale(domain=["Entry", "Exit"], range=["#2563eb", "#f97316"])
    points["event_type"] = points["event"].apply(lambda value: "Exit" if value.startswith("Exit") else "Entry")
    event_points = (
        alt.Chart(points)
        .mark_point(filled=True, size=110)
        .encode(
            x=alt.X("time:T", title="Time"),
            y=alt.Y("price:Q", title="Price", scale=alt.Scale(zero=False)),
            shape=alt.Shape("event_type:N", title="Event"),
            color=alt.Color("event_type:N", scale=event_color, title="Event"),
            tooltip=["event:N", "time:T", "price:Q"],
        )
    )
    event_labels = (
        alt.Chart(points)
        .mark_text(align="left", dx=8, dy=-8, fontSize=12)
        .encode(
            x="time:T",
            y="price:Q",
            text="event:N",
            color=alt.Color("event_type:N", scale=event_color, legend=None),
        )
    )

    price_chart = (
        wick_chart
        + body_chart
        + level_rules
        + level_labels
        + event_points
        + event_labels
    ).properties(height=460)
    st.altair_chart(price_chart, width="stretch")

    volume_frame = klines[
        [
            "open_time",
            "quote_volume_usdt",
            "taker_buy_quote_volume_usdt",
            "taker_sell_quote_volume_usdt",
        ]
    ].melt("open_time", var_name="volume_type", value_name="volume_usdt")
    volume_chart = (
        alt.Chart(volume_frame)
        .mark_bar(opacity=0.7)
        .encode(
            x=alt.X("open_time:T", title="Time"),
            y=alt.Y("volume_usdt:Q", title="Quote Volume"),
            color=alt.Color("volume_type:N", title="Volume"),
        )
    )
    st.altair_chart(volume_chart, width="stretch")


def main() -> None:
    st.set_page_config(page_title="OI Momentum Monitor", layout="wide")
    st.title("OI Momentum Monitor")

    with st.sidebar:
        config_path = st.text_input("Config", DEFAULT_CONFIG)
        auto_refresh = st.toggle("Auto refresh", value=True)
        refresh_seconds = st.slider("Refresh seconds", 3, 60, 10)

    config = load_config(config_path)
    SQLiteStorage(config.storage["database_url"])
    database_path = sqlite_path_from_url(config.storage["database_url"])

    heartbeat = read_table(
        str(database_path),
        "SELECT * FROM scanner_heartbeats WHERE id = 1",
    )
    signals = read_table(
        str(database_path),
        """
        SELECT *
        FROM signals
        ORDER BY created_at_ms DESC
        LIMIT 500
        """,
    )
    positions = read_table(
        str(database_path),
        """
        SELECT
            p.*,
            lp.price AS current_price,
            lp.updated_at_ms AS current_price_updated_at_ms,
            s.price_change_pct AS signal_price_change_pct,
            s.quote_volume_usdt AS signal_quote_volume_usdt,
            s.average_quote_volume_usdt AS signal_average_quote_volume_usdt,
            s.volume_ratio AS signal_volume_ratio,
            s.taker_buy_ratio AS signal_taker_buy_ratio,
            s.taker_sell_ratio AS signal_taker_sell_ratio,
            s.open_interest AS signal_open_interest,
            s.open_interest_value_usdt AS signal_open_interest_value_usdt,
            s.oi_delta_pct AS signal_oi_delta_pct,
            s.oi_delta_value_usdt AS signal_oi_delta_value_usdt
        FROM paper_positions p
        LEFT JOIN latest_prices lp ON lp.symbol = p.symbol
        LEFT JOIN signals s ON s.id = p.signal_id
        ORDER BY p.entry_time_ms DESC
        LIMIT 500
        """,
    )
    positions = add_unrealized_pnl(positions)

    dashboard_tab, chart_tab, logic_tab = st.tabs(["Monitor", "Position Chart", "Strategy Logic"])
    with dashboard_tab:
        render_dashboard(heartbeat=heartbeat, signals=signals, positions=positions)

    with chart_tab:
        render_position_chart(config, positions)

    with logic_tab:
        render_strategy_logic(config)

    if auto_refresh:
        time.sleep(refresh_seconds)
        st.rerun()


if __name__ == "__main__":
    main()
