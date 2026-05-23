from __future__ import annotations

import sqlite3
import time
import logging
import os
from pathlib import Path

import altair as alt
import pandas as pd
import requests
import streamlit as st

from binance_oi_momentum.config import load_config, save_config
from binance_oi_momentum.logging_utils import configure_logging
from binance_oi_momentum.storage import SQLiteStorage, sqlite_path_from_url


DEFAULT_CONFIG = os.getenv("OI_MOMENTUM_CONFIG", "configs/strategy.local.yaml")
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
        positions.get("remaining_notional_usdt", positions["notional_usdt"]).fillna(
            positions["notional_usdt"]
        )
        * positions["unrealized_pnl_pct"]
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
        long_close_distance_default = signal.get(
            "long_close_distance_max",
            max(1.0 - float(signal.get("long_close_position_min", 0.999)), 0.0),
        )
        short_close_distance_default = signal.get(
            "short_close_distance_max",
            float(signal.get("short_close_position_max", 0.001)),
        )
        c1, c2 = st.columns(2)
        signal["long_close_distance_max"] = c1.number_input(
            "Long close distance max",
            min_value=0.0,
            value=float(long_close_distance_default),
            step=0.0005,
            format="%.4f",
            help="做多时，(high - close) / high 必须不大于该值。0.001 表示 close 距离 high 不超过约 0.1%。",
        )
        signal["short_close_distance_max"] = c2.number_input(
            "Short close distance max",
            min_value=0.0,
            value=float(short_close_distance_default),
            step=0.0005,
            format="%.4f",
            help="做空时，(close - low) / low 必须不大于该值。0.001 表示 close 距离 low 不超过约 0.1%。",
        )
        signal.pop("long_close_position_min", None)
        signal.pop("short_close_position_max", None)

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
        execution["initial_entry_fraction"] = c2.number_input(
            "Initial entry fraction",
            min_value=0.01,
            max_value=1.0,
            value=float(execution.get("initial_entry_fraction", 0.3)),
            step=0.05,
            help="信号确认后立即入场的仓位比例。0.30 表示先入 30%，剩余挂回撤单。",
        )
        execution["scale_in_retrace_fraction"] = c3.number_input(
            "Scale-in retrace fraction",
            min_value=0.0,
            max_value=1.0,
            value=float(execution.get("scale_in_retrace_fraction", 0.4)),
            step=0.05,
            help="剩余仓位等待从 entry 到突破 bar 止损位之间回撤多少比例再入场。0.40 表示回撤 40%。",
        )
        c1, c2, c3 = st.columns(3)
        risk["initial_equity_usdt"] = c1.number_input(
            "Initial equity USDT",
            min_value=0.0,
            value=float(risk["initial_equity_usdt"]),
            step=100.0,
            help="纸面交易初始权益，用于计算仓位名义金额和日亏损限制。",
        )
        risk["max_daily_loss"] = c2.number_input(
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
        c1, c2, c3 = st.columns(3)
        exit_config["scale_out_enabled"] = c1.toggle(
            "Scale out take profit",
            value=bool(exit_config.get("scale_out_enabled", False)),
            help="开启后触及 take profit 先平一部分仓位，剩余仓位进入 K线 pivot trailing stop。",
        )
        exit_config["first_take_profit_fraction"] = c2.number_input(
            "TP1 close fraction",
            min_value=0.0,
            max_value=1.0,
            value=float(exit_config.get("first_take_profit_fraction", 0.5)),
            step=0.05,
            help="分批止盈开启时，TP1 平掉的仓位比例。0.5 表示先平一半。",
        )
        exit_config["trailing_pivot_window"] = c3.number_input(
            "Trailing pivot window",
            min_value=1,
            value=int(exit_config.get("trailing_pivot_window", 5)),
            step=1,
            help="计算 trailing pivot 使用最近多少根已收线 K线。多单取 down pivot，空单取 up pivot。",
        )
        exit_config["trailing_kline_interval"] = st.selectbox(
            "Trailing kline interval",
            ["1m", "3m", "5m", "15m"],
            index=["1m", "3m", "5m", "15m"].index(
                str(exit_config.get("trailing_kline_interval", "1m"))
                if str(exit_config.get("trailing_kline_interval", "1m")) in ["1m", "3m", "5m", "15m"]
                else "1m"
            ),
            help="有持仓后订阅 Binance Kline websocket 的 interval。只有已收线 K线会进入 pivot 计算。",
        )

        st.subheader("Advanced")
        c1, c2, c3 = st.columns(3)
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
        signal["precheck_log_cooldown_seconds"] = c3.number_input(
            "Precheck log cooldown seconds",
            min_value=0,
            value=int(signal.get("precheck_log_cooldown_seconds", 60)),
            step=10,
            help="价格阈值已触发但被流动性或冷却过滤时，Log 记录的最小间隔，避免同一 symbol 刷屏。",
        )
        signal["price_carry_forward_interval_seconds"] = c3.number_input(
            "Price carry-forward seconds",
            min_value=0,
            value=int(signal.get("price_carry_forward_interval_seconds", 5)),
            step=1,
            help="miniTicker 只推变化的 symbol。这里定期把最新价格复制进本地窗口，避免冷门币暴涨前没有 60 秒基准价。",
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
    long_close_distance_max = signal.get(
        "long_close_distance_max",
        max(1.0 - float(signal.get("long_close_position_min", 0.999)), 0.0),
    )
    short_close_distance_max = signal.get(
        "short_close_distance_max",
        float(signal.get("short_close_position_max", 0.001)),
    )
    scale_out_enabled = bool(exit_config.get("scale_out_enabled", False))
    first_take_profit_fraction = float(exit_config.get("first_take_profit_fraction", 0.5))
    trailing_kline_interval = str(exit_config.get("trailing_kline_interval", "1m"))
    trailing_pivot_window = int(exit_config.get("trailing_pivot_window", 5))

    st.header("Strategy Logic")
    st.markdown(
        "这个系统当前是研究和纸面交易模式，用来连续记录候选、有效信号和模拟仓位，验证低流动性合约短线顺势策略是否有延续性。系统不会发送真实 Binance 下单请求。"
    )

    st.subheader("Core Hypothesis")
    st.markdown(
        """
        策略关注 Binance USDT-M 永续里的中低流动性小币。假设是：当价格在短时间内快速单边波动，同时成交额放大、open interest 增加，并且主动买卖方向与价格方向一致时，可能有新资金正在主动建立方向性仓位。

        - 价格上涨 + OI 增加 + 放量：顺势做多
        - 价格下跌 + OI 增加 + 放量：顺势做空
        - 触发后不立即入场，而是等待当前 K 线收线，用收线质量过滤假突破
        """
    )

    st.subheader("Data Sources")
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "用途": "全市场价格扫描",
                    "接口": "`!miniTicker@arr` WebSocket",
                    "说明": "维护本地短线价格窗口，更新最新价和 24h quote volume。miniTicker 只推变化 ticker，因此启动时会用 REST 24hr ticker 预热价格窗口，并定期 carry-forward 最新价。",
                },
                {
                    "用途": "候选后成交量确认",
                    "接口": "`/fapi/v1/continuousKlines` REST",
                    "说明": "候选触发后，等待当前 K 线收线，再获取最新收线 K 和历史均量。",
                },
                {
                    "用途": "OI 确认",
                    "接口": "`/fapi/v1/openInterest` + `/futures/data/openInterestHist` REST",
                    "说明": "用实时 OI qty 对比最近 5m OI 快照 qty，计算新增 OI / 快照 OI。",
                },
                {
                    "用途": "持仓 trailing",
                    "接口": "`<symbol>@kline_<interval>` WebSocket",
                    "说明": "仅在有分批止盈持仓时订阅，且只使用 `k.x=true` 的已收线 K。",
                },
            ]
        ),
        width="stretch",
        hide_index=True,
    )

    st.subheader("Universe")
    st.markdown(
        f"""
        扫描标的来自 Binance USDT-M perpetual 合约。若 `include_symbols` 为空，则从交易所合约列表里加载所有 `TRADING` 状态、quote asset 为 `{universe["quote_asset"]}` 的 perpetual 合约，再按 24h quote volume 做流动性过滤。

        - Quote asset: `{universe["quote_asset"]}`
        - 24h quote volume min: `{universe["min_24h_quote_volume"]:,.0f}` USDT
        - 24h quote volume max: `{universe["max_24h_quote_volume"]:,.0f}` USDT
        - Excluded symbols: `{", ".join(universe.get("exclude_symbols") or []) or "none"}`
        """
    )

    st.subheader("Signal Pipeline")
    st.markdown(
        f"""
        1. 启动时用 `/fapi/v1/ticker/24hr` 预热每个 symbol 的最新价，解决冷门币暴涨前没有历史 tick 的问题。
        2. 接收 `!miniTicker@arr` 全市场 tick，并为每个 symbol 维护 `{signal["windows_seconds"]}` 秒价格窗口。
        3. 因 miniTicker 只推变化 ticker，后端每 `{signal.get("price_carry_forward_interval_seconds", 5)}` 秒把最新价 carry-forward 到本地窗口，保留 60s 基准价。
        4. 对每个窗口计算 `price_return = (current_price - window_base_price) / window_base_price`。
        5. `{primary_window}` 秒涨幅 `>= {long_threshold * 100:.2f}%` 形成做多候选。
        6. `{primary_window}` 秒跌幅 `<= {short_threshold * 100:.2f}%` 形成做空候选。
        7. 候选不会立刻交易，而是等待当前 `{signal["kline_interval"]}` K 线收线，并额外等待 `{signal["kline_close_delay_ms"]}` ms。
        8. 收线后拉 `/fapi/v1/continuousKlines`，获取最新已收线 K 和前 `{signal["kline_lookback"]}` 根 K 的平均成交额。
        9. 再拉 `/fapi/v1/openInterest` 获取实时 OI qty，并用 `/futures/data/openInterestHist` 最近 5m 快照作为 baseline。
        10. 通过方向、成交额、OI、主动买卖比例和分数过滤后，记录有效 signal。
        11. 通过风控后开启纸面仓位，入场价使用已收线 K 的 close。
        """
    )

    st.subheader("Price Trigger")
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

    st.subheader("Candle Confirmation")
    st.markdown(
        f"""
        候选触发后，最新收线 K 需要证明趋势没有明显回落。这里不用 high-low 区间百分位，而是计算 close 距离方向极值的比例。

        - 做多 close 强度：`(high - close) / high <= {long_close_distance_max * 100:.3f}%`
        - 做空 close 强度：`(close - low) / low <= {short_close_distance_max * 100:.3f}%`
        - 成交额放大：`latest_quote_volume / average_quote_volume >= {signal["volume_ratio_min"]:.2f}`
        - 均量窗口：过去 `{signal["kline_lookback"]}` 根已收线 `{signal["kline_interval"]}` K
        """
    )

    st.subheader("OI And Flow Filters")
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "过滤项": "OI delta pct",
                    "定义": "`(realtime_oi_qty - latest_5m_snapshot_oi_qty) / latest_5m_snapshot_oi_qty`",
                    "当前阈值": f">= {signal['oi_delta_pct_min'] * 100:.2f}%",
                },
                {
                    "过滤项": "OI value / volume",
                    "定义": "`max(oi_qty_delta * close_price, 0) / latest_quote_volume`",
                    "当前阈值": f">= {signal['oi_value_to_volume_ratio_min']:.2f}",
                },
                {
                    "过滤项": "Long taker buy ratio",
                    "定义": "`taker_buy_quote_volume / quote_volume`",
                    "当前阈值": f">= {signal['taker_buy_ratio_min_for_long']:.2f}",
                },
                {
                    "过滤项": "Short taker sell ratio",
                    "定义": "`1 - taker_buy_ratio`",
                    "当前阈值": f">= {signal['taker_sell_ratio_min_for_short']:.2f}",
                },
            ]
        ),
        width="stretch",
        hide_index=True,
    )

    st.subheader("Score")
    st.markdown(
        f"""
        Score 是一个用于过滤候选质量的加权分数，当前有效信号要求 `score >= {signal["score_probe_min"]:.0f}`。

        - Volume contribution: `min(volume_ratio / 2, 3) * 15`
        - OI delta contribution: `min(max(oi_delta_pct, 0) / 0.05, 3) * 15`
        - OI value / volume contribution: `min(max(oi_value_to_volume_ratio, 0) / 0.25, 3) * 10`
        - Long flow contribution: `max(taker_buy_ratio - 0.5, 0) * 120`
        - Short flow contribution: `max(taker_sell_ratio - 0.5, 0) * 120`
        - 若将来接入 spread/slippage，会按配置扣分
        """
    )

    st.subheader("Risk Gate")
    st.markdown(
        f"""
        风控只决定是否开启纸面仓位；即使风控拒绝，有效 signal 仍会被记录，方便复盘。

        - Initial equity: `{risk["initial_equity_usdt"]:,.0f}` USDT
        - Probe position fraction: `{execution["probe_position_fraction"]:.2f}`
        - Max simultaneous positions: `{risk["max_simultaneous_positions"]}`
        - Max same direction positions: `{risk["max_same_direction_positions"]}`
        - Max daily loss: `{risk["max_daily_loss"] * 100:.2f}%`
        - Max spread pct: `{risk["max_spread_pct"] * 100:.3f}%`
        - Max estimated slippage pct: `{risk["max_estimated_slippage_pct"] * 100:.3f}%`
        """
    )

    st.subheader("Position And Exit")
    st.markdown(
        f"""
        纸面仓位按入场价和初始权益计算名义金额，不发送真实订单。固定止损始终有效。

        - Initial entry: 有效 signal 的收线 K close，先入 `{execution.get("initial_entry_fraction", 0.3) * 100:.0f}%`
        - Scale-in entry: 剩余仓位等待价格从 entry 向突破 bar 止损位回撤 `{execution.get("scale_in_retrace_fraction", 0.4) * 100:.0f}%`
        - Notional: `initial_equity * probe_position_fraction`
        - Stop loss: 多单用突破 bar low，空单用突破 bar high
        - Take profit target: `{exit_config["take_profit_pct"] * 100:.2f}%`
        - Max hold: `{exit_config["max_hold_seconds"]}` seconds
        - Scale out enabled: `{exit_config.get("scale_out_enabled", False)}`
        - TP1 close fraction: `{exit_config.get("first_take_profit_fraction", 0.5):.2f}`

        持仓开启后，后端订阅该 symbol 的 Kline WebSocket。止盈/止损不再用 last/close price 判断，而是模拟真实挂单：

        - 多单：K 线 `high >= take_profit_price` 视为止盈成交，`low <= stop_loss_price` 视为止损成交
        - 空单：K 线 `low <= take_profit_price` 视为止盈成交，`high >= stop_loss_price` 视为止损成交
        - 若同一根 K 同时触发止盈和止损，当前纸面逻辑保守地优先按止损处理
        """
    )

    st.subheader("Scale Out And Trailing Pivot")
    st.markdown(
        f"""
        分批止盈开启时，仓位到达 take profit target 后不会一次性全平。

        1. TP1 触发：先平 `{first_take_profit_fraction * 100:.0f}%` 仓位，并记录 `take_profit_1_*` 字段。
        2. 剩余仓位进入 trailing 状态，`trailing_active = true`。
        3. 后端启动 Binance Kline WebSocket，订阅当前持仓 symbol 的 `<symbol>@kline_{trailing_kline_interval}`。
        4. TP1/固定止损使用实时 Kline 更新里的 high/low 判断；只有 `k.x=true` 的已收线 K 会参与 pivot 计算。
        5. 多单 raw pivot 使用前 `{trailing_pivot_window}` 根已收线 K 的 down pivot，也就是最低 low。
        6. 空单 raw pivot 使用前 `{trailing_pivot_window}` 根已收线 K 的 up pivot，也就是最高 high。
        7. trailing stop 只向有利方向移动：多单只上移，空单只下移，不会因为新的 raw pivot 变差而放宽止损。
        8. 多单如果 close 跌破 trailing stop，按 `trailing_pivot` 平剩余仓位。
        9. 空单如果 close 涨破 trailing stop，按 `trailing_pivot` 平剩余仓位。
        """
    )

    st.subheader("Logs And Reject Reasons")
    st.markdown(
        """
        Log tab 展示 `signal_checks` 表，也就是价格先触发候选后，系统实际拉到的 K线/OI 数据。即使候选被过滤，也会留下原因。

        常见 reject reason：

        - `missing_kline_data`: K线数据不足或请求为空
        - `missing_open_interest_data`: OI 数据为空
        - `long_close_too_far_from_high`: 做多收盘价离 high 太远
        - `short_close_too_far_from_low`: 做空收盘价离 low 太远
        - `liquidity_filter_failed`: 价格阈值已触发，但 24h quote volume 不在 universe 流动性范围内
        - `signal_cooldown_active`: 价格阈值已触发，但该 symbol 仍在信号冷却期
        - `volume_ratio_below_min`: 成交额放大倍数不足
        - `oi_delta_pct_below_min`: OI 增幅不足
        - `oi_value_to_volume_below_min`: OI value 增量相对成交额不足
        - `taker_buy_ratio_below_min`: 做多主动买入占比不足
        - `taker_sell_ratio_below_min`: 做空主动卖出占比不足
        - `score_below_min`: 综合分数不足
        """
    )

    st.subheader("Recorded Fields")
    st.markdown(
        """
        SQLite 会保存候选检查、有效信号和纸面仓位。核心字段包括：

        - `price_change_pct`: 触发窗口内价格涨跌幅
        - `quote_volume_usdt`: 最新 1m K 线 quote volume
        - `average_quote_volume_usdt`: 过去 30 根 1m K 线的平均 quote volume
        - `volume_ratio`: 最新 1m K 线成交额相对过去 30 分钟平均成交额的倍数
        - `taker_buy_ratio` / `taker_sell_ratio`: 最新 1m K 线主动买入/主动卖出 quote volume 占比
        - `open_interest`: 触发时实时 `/fapi/v1/openInterest` OI qty
        - `open_interest_value_usdt`: 实时 OI qty * 收线价估算值
        - `oi_delta_pct`: 实时 OI qty 相对最近 5m 快照 OI qty 的增幅
        - `oi_delta_value_usdt`: OI qty 增量 * 收线价的估算 USDT 增量
        - `oi_value_to_volume_ratio`: OI value 增量 / 最近窗口成交额
        - `score`: 综合打分
        - `risk_allowed` / `risk_reason`: 是否通过风控以及原因
        - `take_profit_1_price` / `take_profit_1_pnl_usdt`: TP1 价格和已实现收益
        - `trailing_active` / `trailing_stop_price`: 是否进入 trailing 和当前 pivot 止损
        - `remaining_quantity` / `remaining_notional_usdt`: 剩余仓位规模
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
    positions = format_time_column(positions, "take_profit_1_time_ms")

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
        "take_profit_1_price",
        "take_profit_2_price",
        "scale_out_enabled",
        "trailing_active",
        "trailing_stop_price",
        "trailing_pivot_window",
        "current_price",
        "notional_usdt",
        "remaining_notional_usdt",
        "remaining_quantity",
        "unrealized_pnl_usdt",
        "unrealized_pnl_pct",
        "take_profit_1_time",
        "take_profit_1_exit_price",
        "take_profit_1_quantity",
        "take_profit_1_pnl_usdt",
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


def render_signal_log(signal_checks: pd.DataFrame, signal_check_stats: pd.DataFrame, log_limit: int) -> None:
    st.header("Log")
    st.caption(
        "这里记录价格先达到触发阈值后，后端等待 1m 收线并调用 K线/OI 接口捕捉到的数据。"
        f"当前表格显示最新 {log_limit} 条；数据库会继续累计，不会因为前端显示上限停止写入。"
    )

    if signal_checks.empty:
        st.info("No signal checks yet. Scanner needs a price move candidate before this table has data.")
        return

    signal_checks = format_time_column(signal_checks, "checked_at_ms")
    signal_checks = format_time_column(signal_checks, "candidate_detected_at_ms")
    signal_checks = format_time_column(signal_checks, "candle_close_time_ms")
    signal_checks["passed"] = signal_checks["passed"].astype(bool)

    passed_count = int(signal_checks["passed"].sum())
    total_checks = len(signal_checks)
    latest_check_id = None
    if not signal_check_stats.empty:
        total_checks = int(signal_check_stats.iloc[0].get("total_checks") or len(signal_checks))
        latest_check_id = signal_check_stats.iloc[0].get("latest_check_id")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total checks", total_checks)
    c2.metric("Shown", len(signal_checks))
    c3.metric("Pass rate", f"{passed_count / len(signal_checks) * 100:.1f}%")
    latest_reason = str(signal_checks.iloc[0]["reject_reason"] or "passed")
    latest_label = latest_reason if latest_check_id is None else f"#{int(latest_check_id)} {latest_reason}"
    c4.metric("Latest result", latest_label)

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
    c3.metric("TP1", f"{position.get('take_profit_1_price', position['take_profit_price']):g}")
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
    level_rows = [
        {"level": "Entry", "price": position["entry_price"]},
        {"level": "Stop Loss", "price": position["stop_loss_price"]},
        {"level": "Take Profit 1", "price": position.get("take_profit_1_price") or position["take_profit_price"]},
    ]
    if pd.notna(position.get("take_profit_2_price")):
        level_rows.append({"level": "Take Profit 2", "price": position["take_profit_2_price"]})
    if pd.notna(position.get("trailing_stop_price")):
        level_rows.append({"level": "Trailing Stop", "price": position["trailing_stop_price"]})
    levels = pd.DataFrame(level_rows)
    level_color = alt.Scale(
        domain=["Entry", "Stop Loss", "Take Profit 1", "Take Profit 2", "Trailing Stop"],
        range=["#2563eb", "#dc2626", "#16a34a", "#059669", "#9333ea"],
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
    if pd.notna(position.get("take_profit_1_time_ms")) and pd.notna(position.get("take_profit_1_exit_price")):
        point_rows.append(
            {
                "event": "Take Profit 1",
                "time": pd.to_datetime(position["take_profit_1_time_ms"], unit="ms"),
                "price": position["take_profit_1_exit_price"],
            }
        )
    points = pd.DataFrame(point_rows)
    event_color = alt.Scale(
        domain=["Entry", "Exit", "Take Profit"],
        range=["#2563eb", "#f97316", "#16a34a"],
    )
    points["event_type"] = points["event"].apply(
        lambda value: "Exit"
        if value.startswith("Exit")
        else ("Take Profit" if value.startswith("Take Profit") else "Entry")
    )
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
        log_limit = st.selectbox("Log rows", [1000, 3000, 5000, 10000], index=0)

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
        f"""
        SELECT *
        FROM signal_checks
        ORDER BY checked_at_ms DESC, id DESC
        LIMIT {int(log_limit)}
        """,
    )
    signal_check_stats = read_table(
        str(database_path),
        """
        SELECT
            COUNT(*) AS total_checks,
            MAX(id) AS latest_check_id,
            MAX(checked_at_ms) AS latest_checked_at_ms
        FROM signal_checks
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
        render_signal_log(signal_checks, signal_check_stats, int(log_limit))

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
