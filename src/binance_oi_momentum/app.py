from __future__ import annotations

import sqlite3
import time
import logging
from pathlib import Path

import altair as alt
import pandas as pd
import requests
import streamlit as st

from binance_oi_momentum.config import load_config, save_config
from binance_oi_momentum.logging_utils import configure_logging
from binance_oi_momentum.storage import SQLiteStorage, sqlite_path_from_url


DEFAULT_CONFIG = "configs/strategy.example.yaml"
configure_logging("dashboard")
logger = logging.getLogger(__name__)


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


def render_config_editor(config_path: str, config) -> None:
    st.header("Config")
    st.caption("保存后会写入 YAML。后端 scanner 每 5 秒检测一次配置文件变化，大部分策略阈值无需重新发版即可生效。")

    config_dict = config.model_dump()
    runtime = config_dict.setdefault("runtime", {"scanner_enabled": True})
    exchange = config_dict["exchange"]
    universe = config_dict["universe"]
    signal = config_dict["signal"]
    execution = config_dict["execution"]
    risk = config_dict["risk"]
    exit_config = config_dict["exit"]

    with st.form("strategy_config_form"):
        st.subheader("Program Control")
        runtime["scanner_enabled"] = st.toggle(
            "Scanner enabled",
            value=bool(runtime.get("scanner_enabled", True)),
            help="关闭后后端进程仍保持运行并继续热加载配置，但不会处理行情、触发信号或开纸面仓位。调参前建议先关闭，保存后再开启。",
        )
        if runtime["scanner_enabled"]:
            st.success("Scanner is enabled. 保存后后端会继续扫描行情。")
        else:
            st.warning("Scanner is paused. 保存后后端会暂停扫描，适合安全调整参数。")

        st.subheader("Universe")
        c1, c2 = st.columns(2)
        universe["min_24h_quote_volume"] = c1.number_input(
            "Min 24h quote volume",
            min_value=0,
            value=int(universe["min_24h_quote_volume"]),
            step=1_000_000,
            help="只扫描 24h USDT 成交额不低于该值的合约，避免极端低流动性标的。",
        )
        universe["max_24h_quote_volume"] = c2.number_input(
            "Max 24h quote volume",
            min_value=0,
            value=int(universe["max_24h_quote_volume"]),
            step=10_000_000,
            help="只扫描 24h USDT 成交额不高于该值的合约，用来聚焦小币和中低流动性标的。",
        )
        universe["exclude_symbols"] = [
            item.strip().upper()
            for item in st.text_area(
                "Exclude symbols",
                value="\n".join(universe.get("exclude_symbols") or []),
                help="每行一个 symbol。这里的合约不会被扫描，比如 BTCUSDT、ETHUSDT。",
            ).splitlines()
            if item.strip()
        ]

        st.subheader("Price Trigger")
        c1, c2, c3 = st.columns(3)
        primary_window = c1.number_input(
            "Primary window seconds",
            min_value=1,
            value=int(signal["primary_window_seconds"]),
            step=1,
            help="计算价格涨跌幅的主窗口。当前策略建议 60 秒。",
        )
        long_thresholds = signal.get("long_return_thresholds", {})
        short_thresholds = signal.get("short_return_thresholds", {})
        default_long_return = long_thresholds.get(
            int(primary_window),
            long_thresholds.get(str(int(primary_window)), 0.02),
        )
        default_short_return = short_thresholds.get(
            int(primary_window),
            short_thresholds.get(str(int(primary_window)), -0.02),
        )
        long_return_pct = c2.number_input(
            "Long return threshold %",
            value=float(default_long_return * 100),
            step=0.1,
            help="窗口内涨幅达到该百分比，形成做多候选。",
        )
        short_return_pct = c3.number_input(
            "Short return threshold %",
            value=float(default_short_return * 100),
            step=0.1,
            help="窗口内跌幅达到该百分比或更低，形成做空候选。通常为负数。",
        )
        signal["windows_seconds"] = [int(primary_window)]
        signal["primary_window_seconds"] = int(primary_window)
        signal["long_return_thresholds"] = {int(primary_window): float(long_return_pct) / 100}
        signal["short_return_thresholds"] = {int(primary_window): float(short_return_pct) / 100}

        st.subheader("Candle Confirmation")
        c1, c2, c3 = st.columns(3)
        signal["kline_lookback"] = c1.number_input(
            "Kline lookback",
            min_value=1,
            value=int(signal["kline_lookback"]),
            step=1,
            help="计算平均成交量时使用过去多少根 1m K线。",
        )
        signal["kline_close_delay_ms"] = c2.number_input(
            "Kline close delay ms",
            min_value=0,
            value=int(signal["kline_close_delay_ms"]),
            step=100,
            help="候选触发后等待当前 1m K 收线，再额外等待该毫秒数，避免交易所 K线数据尚未稳定。",
        )
        signal["volume_ratio_min"] = c3.number_input(
            "Volume ratio min",
            min_value=0.0,
            value=float(signal["volume_ratio_min"]),
            step=0.1,
            help="最新 1m quote volume / 过去均量。大于该值才认为放量。",
        )
        c1, c2 = st.columns(2)
        signal["long_close_position_min"] = c1.number_input(
            "Long close position min",
            min_value=0.0,
            max_value=1.0,
            value=float(signal["long_close_position_min"]),
            step=0.01,
            help="做多时，1m 收盘价在本根 K high-low 区间中的位置，0.95 表示收在顶部 5%。",
        )
        signal["short_close_position_max"] = c2.number_input(
            "Short close position max",
            min_value=0.0,
            max_value=1.0,
            value=float(signal["short_close_position_max"]),
            step=0.01,
            help="做空时，1m 收盘价在本根 K high-low 区间中的位置，0.05 表示收在底部 5%。",
        )

        st.subheader("Flow And OI")
        c1, c2, c3 = st.columns(3)
        signal["taker_buy_ratio_min_for_long"] = c1.number_input(
            "Long taker buy ratio min",
            min_value=0.0,
            max_value=1.0,
            value=float(signal["taker_buy_ratio_min_for_long"]),
            step=0.01,
            help="做多时主动买入 quote volume 占比必须大于该值。",
        )
        signal["taker_sell_ratio_min_for_short"] = c2.number_input(
            "Short taker sell ratio min",
            min_value=0.0,
            max_value=1.0,
            value=float(signal["taker_sell_ratio_min_for_short"]),
            step=0.01,
            help="做空时主动卖出 quote volume 占比必须大于该值。",
        )
        signal["oi_delta_pct_min"] = c3.number_input(
            "OI delta pct min",
            min_value=0.0,
            value=float(signal["oi_delta_pct_min"]),
            step=0.01,
            help="新增 OI / 上一个 OI。0.05 表示 OI 至少增加 5%。",
        )
        c1, c2, c3 = st.columns(3)
        signal["oi_value_to_volume_ratio_min"] = c1.number_input(
            "OI value / volume min",
            min_value=0.0,
            value=float(signal["oi_value_to_volume_ratio_min"]),
            step=0.01,
            help="OI value 增量 / 最新 1m quote volume。越高说明新增仓位占成交额比例越大。",
        )
        signal["score_probe_min"] = c2.number_input(
            "Score min",
            min_value=0.0,
            value=float(signal["score_probe_min"]),
            step=1.0,
            help="综合打分阈值。低于该分数不会记录为有效入场信号。",
        )
        signal["signal_cooldown_seconds"] = c3.number_input(
            "Signal cooldown seconds",
            min_value=0,
            value=int(signal["signal_cooldown_seconds"]),
            step=30,
            help="同一 symbol 两次信号之间的最小冷却时间。",
        )

        st.subheader("Paper Trading And Risk")
        c1, c2, c3 = st.columns(3)
        execution["probe_position_fraction"] = c1.number_input(
            "Probe position fraction",
            min_value=0.0,
            max_value=1.0,
            value=float(execution["probe_position_fraction"]),
            step=0.01,
            help="纸面开仓使用初始权益的比例。0.20 表示 20%。",
        )
        risk["initial_equity_usdt"] = c2.number_input(
            "Initial equity USDT",
            min_value=0.0,
            value=float(risk["initial_equity_usdt"]),
            step=100.0,
            help="纸面交易初始权益，用于计算仓位名义金额和日亏损限制。",
        )
        risk["max_daily_loss"] = c3.number_input(
            "Max daily loss",
            min_value=0.0,
            max_value=1.0,
            value=float(risk["max_daily_loss"]),
            step=0.005,
            help="每日最大已实现亏损比例。触发后新信号不再开仓。",
        )
        c1, c2 = st.columns(2)
        risk["max_simultaneous_positions"] = c1.number_input(
            "Max simultaneous positions",
            min_value=0,
            value=int(risk["max_simultaneous_positions"]),
            step=1,
            help="最多同时持有多少个纸面仓位。",
        )
        risk["max_same_direction_positions"] = c2.number_input(
            "Max same direction positions",
            min_value=0,
            value=int(risk["max_same_direction_positions"]),
            step=1,
            help="最多同时持有多少个同方向仓位。",
        )

        st.subheader("Exit")
        c1, c2, c3 = st.columns(3)
        exit_config["stop_loss_pct"] = c1.number_input(
            "Stop loss pct",
            min_value=0.0,
            value=float(exit_config["stop_loss_pct"]),
            step=0.001,
            format="%.4f",
            help="固定止损百分比。0.012 表示 1.2%。",
        )
        exit_config["take_profit_pct"] = c2.number_input(
            "Take profit pct",
            min_value=0.0,
            value=float(exit_config["take_profit_pct"]),
            step=0.001,
            format="%.4f",
            help="固定止盈百分比。0.018 表示 1.8%，即止损 1.2% 时盈亏比 1:1.5。",
        )
        exit_config["max_hold_seconds"] = c3.number_input(
            "Max hold seconds",
            min_value=1,
            value=int(exit_config["max_hold_seconds"]),
            step=60,
            help="超过该持仓秒数仍未止盈/止损，则按超时退出。",
        )

        st.subheader("Advanced")
        c1, c2 = st.columns(2)
        exchange["request_timeout_seconds"] = c1.number_input(
            "REST timeout seconds",
            min_value=1,
            value=int(exchange["request_timeout_seconds"]),
            step=1,
            help="Binance REST 请求超时时间。网络较慢时可以调大。",
        )
        exchange["rest_retries"] = c2.number_input(
            "REST retries",
            min_value=1,
            value=int(exchange["rest_retries"]),
            step=1,
            help="Binance REST 请求失败后的最大重试次数。",
        )

        submitted = st.form_submit_button("Save config", type="primary")

    if submitted:
        save_config(config_path, config_dict)
        logger.info(
            "config saved path=%s scanner_enabled=%s primary_window=%s long_threshold=%s "
            "short_threshold=%s volume_ratio_min=%s oi_delta_pct_min=%s",
            config_path,
            runtime.get("scanner_enabled"),
            signal.get("primary_window_seconds"),
            signal.get("long_return_thresholds"),
            signal.get("short_return_thresholds"),
            signal.get("volume_ratio_min"),
            signal.get("oi_delta_pct_min"),
        )
        st.success(f"Saved {config_path}. Backend scanner will hot-reload shortly.")
        st.cache_data.clear()
        st.rerun()


def render_strategy_logic(config) -> None:
    signal = config.signal
    universe = config.universe
    risk = config.risk
    exit_config = config.exit
    execution = config.execution
    primary_window = int(signal["primary_window_seconds"])
    long_threshold = signal["long_return_thresholds"].get(
        primary_window,
        signal["long_return_thresholds"].get(str(primary_window), 0.02),
    )
    short_threshold = signal["short_return_thresholds"].get(
        primary_window,
        signal["short_return_thresholds"].get(str(primary_window), -0.02),
    )

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
        3. `{primary_window}` 秒涨幅 >= `{long_threshold * 100:.2f}%` 判定做多候选；`{primary_window}` 秒跌幅 <= `{short_threshold * 100:.2f}%` 判定做空候选。
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
        long_value = signal["long_return_thresholds"].get(
            window,
            signal["long_return_thresholds"].get(str(window)),
        )
        short_value = signal["short_return_thresholds"].get(
            window,
            signal["short_return_thresholds"].get(str(window)),
        )
        threshold_rows.append(
            {
                "window_seconds": window,
                "long_return_min_pct": None if long_value is None else long_value * 100,
                "short_return_max_pct": None if short_value is None else short_value * 100,
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


def render_signal_log(signal_checks: pd.DataFrame) -> None:
    st.header("Log")
    st.caption("这里记录价格先达到触发阈值后，后端等待 1m 收线并调用 K线/OI 接口捕捉到的数据。被成交量、OI、主动买卖比例或收盘位置过滤掉的候选也会保留。")

    if signal_checks.empty:
        st.info("No signal checks yet. Scanner needs a price move candidate before this table has data.")
        return

    signal_checks = format_time_column(signal_checks, "checked_at_ms")
    signal_checks = format_time_column(signal_checks, "candidate_detected_at_ms")
    signal_checks = format_time_column(signal_checks, "candle_close_time_ms")
    signal_checks["passed"] = signal_checks["passed"].astype(bool)

    passed_count = int(signal_checks["passed"].sum())
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Checks", len(signal_checks))
    c2.metric("Passed", passed_count)
    c3.metric("Pass rate", f"{passed_count / len(signal_checks) * 100:.1f}%")
    latest_reason = str(signal_checks.iloc[0]["reject_reason"] or "passed")
    c4.metric("Latest result", latest_reason)

    reason_counts = (
        signal_checks.assign(result=signal_checks["reject_reason"].replace("", "passed"))
        .groupby("result", as_index=False)
        .size()
        .sort_values("size", ascending=False)
    )
    st.subheader("Reject Reasons")
    st.dataframe(reason_counts, width="stretch", hide_index=True)

    log_columns = [
        "checked_at",
        "candidate_detected_at",
        "candle_close_time",
        "symbol",
        "direction",
        "passed",
        "reject_reason",
        "price_change_pct",
        "window_seconds",
        "candidate_trigger_price",
        "trigger_price",
        "quote_volume_usdt",
        "average_quote_volume_usdt",
        "volume_ratio",
        "taker_buy_ratio",
        "taker_sell_ratio",
        "open_interest",
        "previous_open_interest",
        "open_interest_value_usdt",
        "oi_delta_pct",
        "oi_delta_value_usdt",
        "oi_value_to_volume_ratio",
        "close_position",
        "score",
    ]
    st.subheader("Signal Checks")
    st.dataframe(
        signal_checks[[column for column in log_columns if column in signal_checks.columns]],
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
    logger.info(
        "dashboard render config=%s database=%s auto_refresh=%s refresh_seconds=%s",
        config_path,
        database_path,
        auto_refresh,
        refresh_seconds,
    )

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
    signal_checks = read_table(
        str(database_path),
        """
        SELECT *
        FROM signal_checks
        ORDER BY checked_at_ms DESC
        LIMIT 1000
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

    dashboard_tab, log_tab, chart_tab, logic_tab, config_tab = st.tabs(
        ["Monitor", "Log", "Position Chart", "Strategy Logic", "Config"]
    )
    with dashboard_tab:
        render_dashboard(heartbeat=heartbeat, signals=signals, positions=positions)

    with log_tab:
        render_signal_log(signal_checks)

    with chart_tab:
        render_position_chart(config, positions)

    with logic_tab:
        render_strategy_logic(config)

    with config_tab:
        render_config_editor(config_path, config)

    if auto_refresh:
        time.sleep(refresh_seconds)
        st.rerun()


if __name__ == "__main__":
    main()
