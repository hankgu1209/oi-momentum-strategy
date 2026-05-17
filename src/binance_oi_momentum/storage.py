from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

from .models import Direction, PaperPosition, PositionStatus, SignalContext


def sqlite_path_from_url(database_url: str) -> Path:
    if database_url.startswith("sqlite:///"):
        return Path(database_url.removeprefix("sqlite:///"))
    return Path(database_url)


class SQLiteStorage:
    """Small SQLite persistence layer for multi-day research runs."""

    def __init__(self, database_url: str) -> None:
        self.path = sqlite_path_from_url(database_url)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def connect(self) -> Iterable[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def initialize(self) -> None:
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at_ms INTEGER NOT NULL,
                    symbol TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    trigger_price REAL NOT NULL,
                    price_change_pct REAL NOT NULL,
                    window_seconds INTEGER NOT NULL,
                    quote_volume_usdt REAL NOT NULL,
                    average_quote_volume_usdt REAL NOT NULL DEFAULT 0,
                    volume_ratio REAL NOT NULL,
                    taker_buy_ratio REAL NOT NULL,
                    taker_sell_ratio REAL NOT NULL DEFAULT 0,
                    open_interest REAL NOT NULL DEFAULT 0,
                    open_interest_value_usdt REAL NOT NULL DEFAULT 0,
                    oi_delta_pct REAL NOT NULL,
                    oi_delta_value_usdt REAL NOT NULL,
                    oi_value_to_volume_ratio REAL NOT NULL,
                    score REAL NOT NULL,
                    risk_allowed INTEGER NOT NULL,
                    risk_reason TEXT NOT NULL,
                    raw_json TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_signals_created_at
                    ON signals(created_at_ms);
                CREATE INDEX IF NOT EXISTS idx_signals_symbol
                    ON signals(symbol);

                CREATE TABLE IF NOT EXISTS paper_positions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    signal_id INTEGER NOT NULL,
                    symbol TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    status TEXT NOT NULL,
                    entry_time_ms INTEGER NOT NULL,
                    entry_price REAL NOT NULL,
                    quantity REAL NOT NULL,
                    notional_usdt REAL NOT NULL,
                    stop_loss_price REAL NOT NULL,
                    take_profit_price REAL NOT NULL,
                    initial_quantity REAL,
                    remaining_quantity REAL,
                    remaining_notional_usdt REAL,
                    scale_out_enabled INTEGER NOT NULL DEFAULT 0,
                    trailing_active INTEGER NOT NULL DEFAULT 0,
                    take_profit_1_price REAL,
                    take_profit_2_price REAL,
                    take_profit_1_time_ms INTEGER,
                    take_profit_1_exit_price REAL,
                    take_profit_1_quantity REAL,
                    take_profit_1_pnl_usdt REAL,
                    take_profit_1_pnl_pct REAL,
                    trailing_stop_price REAL,
                    trailing_pivot_window INTEGER,
                    max_hold_seconds INTEGER NOT NULL,
                    exit_time_ms INTEGER,
                    exit_price REAL,
                    exit_reason TEXT,
                    pnl_usdt REAL,
                    pnl_pct REAL,
                    FOREIGN KEY(signal_id) REFERENCES signals(id)
                );

                CREATE INDEX IF NOT EXISTS idx_positions_status
                    ON paper_positions(status);
                CREATE INDEX IF NOT EXISTS idx_positions_symbol
                    ON paper_positions(symbol);

                CREATE TABLE IF NOT EXISTS scanner_heartbeats (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    updated_at_ms INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    message TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS latest_prices (
                    symbol TEXT PRIMARY KEY,
                    updated_at_ms INTEGER NOT NULL,
                    price REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS signal_checks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    checked_at_ms INTEGER NOT NULL,
                    candidate_detected_at_ms INTEGER NOT NULL,
                    symbol TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    window_seconds INTEGER NOT NULL,
                    candidate_trigger_price REAL NOT NULL,
                    candle_close_time_ms INTEGER NOT NULL,
                    trigger_price REAL,
                    price_change_pct REAL NOT NULL,
                    quote_volume_usdt REAL,
                    average_quote_volume_usdt REAL,
                    volume_ratio REAL,
                    taker_buy_ratio REAL,
                    taker_sell_ratio REAL,
                    open_interest REAL,
                    previous_open_interest REAL,
                    open_interest_value_usdt REAL,
                    oi_delta_pct REAL,
                    oi_delta_value_usdt REAL,
                    oi_value_to_volume_ratio REAL,
                    close_position REAL,
                    score REAL,
                    passed INTEGER NOT NULL,
                    reject_reason TEXT NOT NULL,
                    raw_json TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_signal_checks_checked_at
                    ON signal_checks(checked_at_ms);
                CREATE INDEX IF NOT EXISTS idx_signal_checks_symbol
                    ON signal_checks(symbol);
                """
            )
            self._ensure_column(conn, "signals", "average_quote_volume_usdt", "REAL NOT NULL DEFAULT 0")
            self._ensure_column(conn, "signals", "taker_sell_ratio", "REAL NOT NULL DEFAULT 0")
            self._ensure_column(conn, "signals", "open_interest", "REAL NOT NULL DEFAULT 0")
            self._ensure_column(conn, "signals", "open_interest_value_usdt", "REAL NOT NULL DEFAULT 0")
            self._ensure_column(conn, "paper_positions", "initial_quantity", "REAL")
            self._ensure_column(conn, "paper_positions", "remaining_quantity", "REAL")
            self._ensure_column(conn, "paper_positions", "remaining_notional_usdt", "REAL")
            self._ensure_column(conn, "paper_positions", "scale_out_enabled", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "paper_positions", "trailing_active", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "paper_positions", "take_profit_1_price", "REAL")
            self._ensure_column(conn, "paper_positions", "take_profit_2_price", "REAL")
            self._ensure_column(conn, "paper_positions", "take_profit_1_time_ms", "INTEGER")
            self._ensure_column(conn, "paper_positions", "take_profit_1_exit_price", "REAL")
            self._ensure_column(conn, "paper_positions", "take_profit_1_quantity", "REAL")
            self._ensure_column(conn, "paper_positions", "take_profit_1_pnl_usdt", "REAL")
            self._ensure_column(conn, "paper_positions", "take_profit_1_pnl_pct", "REAL")
            self._ensure_column(conn, "paper_positions", "trailing_stop_price", "REAL")
            self._ensure_column(conn, "paper_positions", "trailing_pivot_window", "INTEGER")
            conn.execute(
                """
                UPDATE paper_positions
                SET
                    initial_quantity = COALESCE(initial_quantity, quantity),
                    remaining_quantity = COALESCE(remaining_quantity, quantity),
                    remaining_notional_usdt = COALESCE(remaining_notional_usdt, notional_usdt),
                    take_profit_1_price = COALESCE(take_profit_1_price, take_profit_price)
                """
            )
            self._ensure_position_journal_view(conn)

    def record_signal_check(self, log: dict[str, Any]) -> int:
        payload = {
            "checked_at_ms": log.get("checked_at_ms"),
            "candidate_detected_at_ms": log.get("candidate_detected_at_ms"),
            "symbol": log.get("symbol"),
            "direction": log.get("direction"),
            "window_seconds": log.get("window_seconds"),
            "candidate_trigger_price": log.get("candidate_trigger_price"),
            "candle_close_time_ms": log.get("candle_close_time_ms"),
            "trigger_price": log.get("trigger_price"),
            "price_change_pct": log.get("price_change_pct"),
            "quote_volume_usdt": log.get("quote_volume_usdt"),
            "average_quote_volume_usdt": log.get("average_quote_volume_usdt"),
            "volume_ratio": log.get("volume_ratio"),
            "taker_buy_ratio": log.get("taker_buy_ratio"),
            "taker_sell_ratio": log.get("taker_sell_ratio"),
            "open_interest": log.get("open_interest"),
            "previous_open_interest": log.get("previous_open_interest"),
            "open_interest_value_usdt": log.get("open_interest_value_usdt"),
            "oi_delta_pct": log.get("oi_delta_pct"),
            "oi_delta_value_usdt": log.get("oi_delta_value_usdt"),
            "oi_value_to_volume_ratio": log.get("oi_value_to_volume_ratio"),
            "close_position": log.get("close_position"),
            "score": log.get("score"),
            "passed": bool(log.get("passed", False)),
            "reject_reason": log.get("reject_reason", ""),
            "raw": log.get("raw", {}),
        }
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO signal_checks(
                    checked_at_ms, candidate_detected_at_ms, symbol, direction,
                    window_seconds, candidate_trigger_price, candle_close_time_ms,
                    trigger_price, price_change_pct, quote_volume_usdt,
                    average_quote_volume_usdt, volume_ratio, taker_buy_ratio,
                    taker_sell_ratio, open_interest, previous_open_interest,
                    open_interest_value_usdt, oi_delta_pct, oi_delta_value_usdt,
                    oi_value_to_volume_ratio, close_position, score, passed,
                    reject_reason, raw_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["checked_at_ms"],
                    payload["candidate_detected_at_ms"],
                    payload["symbol"],
                    payload["direction"],
                    payload["window_seconds"],
                    payload["candidate_trigger_price"],
                    payload["candle_close_time_ms"],
                    payload["trigger_price"],
                    payload["price_change_pct"],
                    payload["quote_volume_usdt"],
                    payload["average_quote_volume_usdt"],
                    payload["volume_ratio"],
                    payload["taker_buy_ratio"],
                    payload["taker_sell_ratio"],
                    payload["open_interest"],
                    payload["previous_open_interest"],
                    payload["open_interest_value_usdt"],
                    payload["oi_delta_pct"],
                    payload["oi_delta_value_usdt"],
                    payload["oi_value_to_volume_ratio"],
                    payload["close_position"],
                    payload["score"],
                    int(payload["passed"]),
                    payload["reject_reason"],
                    json.dumps(payload, ensure_ascii=False),
                ),
            )
            return int(cursor.lastrowid)

    def record_heartbeat(self, timestamp_ms: int, status: str, message: str = "") -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO scanner_heartbeats(id, updated_at_ms, status, message)
                VALUES (1, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    updated_at_ms = excluded.updated_at_ms,
                    status = excluded.status,
                    message = excluded.message
                """,
                (timestamp_ms, status, message),
            )

    def record_latest_price(self, symbol: str, timestamp_ms: int, price: float) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO latest_prices(symbol, updated_at_ms, price)
                VALUES (?, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                    updated_at_ms = excluded.updated_at_ms,
                    price = excluded.price
                """,
                (symbol, timestamp_ms, price),
            )

    def record_signal(
        self,
        context: SignalContext,
        *,
        risk_allowed: bool,
        risk_reason: str,
        raw: dict[str, Any],
    ) -> int:
        payload = {
            "symbol": context.symbol,
            "direction": context.direction.value,
            "timestamp_ms": context.timestamp_ms,
            **raw,
        }
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO signals(
                    created_at_ms, symbol, direction, trigger_price, price_change_pct,
                    window_seconds, quote_volume_usdt, average_quote_volume_usdt,
                    volume_ratio, taker_buy_ratio, taker_sell_ratio, open_interest,
                    open_interest_value_usdt, oi_delta_pct, oi_delta_value_usdt,
                    oi_value_to_volume_ratio, score, risk_allowed, risk_reason, raw_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    context.timestamp_ms,
                    context.symbol,
                    context.direction.value,
                    context.trigger_price,
                    context.price_change_pct,
                    context.window_seconds,
                    context.quote_volume_usdt,
                    context.average_quote_volume_usdt,
                    context.volume_ratio,
                    context.taker_buy_ratio,
                    context.taker_sell_ratio,
                    context.open_interest,
                    context.open_interest_value_usdt,
                    context.oi_delta_pct,
                    context.oi_delta_value_usdt,
                    context.oi_value_to_volume_ratio,
                    context.score,
                    int(risk_allowed),
                    risk_reason,
                    json.dumps(payload, ensure_ascii=False),
                ),
            )
            return int(cursor.lastrowid)

    def open_position(self, position: PaperPosition) -> int:
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO paper_positions(
                    signal_id, symbol, direction, status, entry_time_ms, entry_price,
                    quantity, notional_usdt, stop_loss_price, take_profit_price,
                    initial_quantity, remaining_quantity, remaining_notional_usdt,
                    scale_out_enabled, trailing_active, take_profit_1_price,
                    take_profit_2_price, trailing_pivot_window, max_hold_seconds
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    position.signal_id,
                    position.symbol,
                    position.direction.value,
                    position.status.value,
                    position.entry_time_ms,
                    position.entry_price,
                    position.quantity,
                    position.notional_usdt,
                    position.stop_loss_price,
                    position.take_profit_price,
                    position.initial_quantity,
                    position.remaining_quantity,
                    position.remaining_notional_usdt,
                    int(position.scale_out_enabled),
                    int(position.trailing_active),
                    position.take_profit_1_price,
                    position.take_profit_2_price,
                    position.trailing_pivot_window,
                    position.max_hold_seconds,
                ),
            )
            return int(cursor.lastrowid)

    def mark_first_take_profit(
        self,
        position_id: int,
        *,
        timestamp_ms: int,
        exit_price: float,
        exit_quantity: float,
        remaining_quantity: float,
        remaining_notional_usdt: float,
        pnl_usdt: float,
        pnl_pct: float,
        trailing_stop_price: float | None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE paper_positions
                SET
                    trailing_active = 1,
                    take_profit_1_time_ms = ?,
                    take_profit_1_exit_price = ?,
                    take_profit_1_quantity = ?,
                    take_profit_1_pnl_usdt = ?,
                    take_profit_1_pnl_pct = ?,
                    remaining_quantity = ?,
                    remaining_notional_usdt = ?,
                    trailing_stop_price = ?
                WHERE id = ? AND status = ?
                """,
                (
                    timestamp_ms,
                    exit_price,
                    exit_quantity,
                    pnl_usdt,
                    pnl_pct,
                    remaining_quantity,
                    remaining_notional_usdt,
                    trailing_stop_price,
                    position_id,
                    PositionStatus.OPEN.value,
                ),
            )

    def update_trailing_stop(self, position_id: int, trailing_stop_price: float) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE paper_positions
                SET trailing_stop_price = ?
                WHERE id = ? AND status = ?
                """,
                (trailing_stop_price, position_id, PositionStatus.OPEN.value),
            )

    def close_position(
        self,
        position_id: int,
        *,
        exit_time_ms: int,
        exit_price: float,
        exit_reason: str,
        pnl_usdt: float,
        pnl_pct: float,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE paper_positions
                SET status = ?, exit_time_ms = ?, exit_price = ?, exit_reason = ?,
                    pnl_usdt = ?, pnl_pct = ?,
                    remaining_quantity = 0,
                    remaining_notional_usdt = 0
                WHERE id = ? AND status = ?
                """,
                (
                    PositionStatus.CLOSED.value,
                    exit_time_ms,
                    exit_price,
                    exit_reason,
                    pnl_usdt,
                    pnl_pct,
                    position_id,
                    PositionStatus.OPEN.value,
                ),
            )

    def get_open_positions(self) -> list[PaperPosition]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM paper_positions
                WHERE status = ?
                ORDER BY entry_time_ms ASC
                """,
                (PositionStatus.OPEN.value,),
            ).fetchall()
        return [self._row_to_position(row) for row in rows]

    def count_open_positions(self, direction: Direction | None = None) -> int:
        query = "SELECT COUNT(*) AS count FROM paper_positions WHERE status = ?"
        params: list[Any] = [PositionStatus.OPEN.value]
        if direction is not None:
            query += " AND direction = ?"
            params.append(direction.value)
        with self.connect() as conn:
            row = conn.execute(query, params).fetchone()
        return int(row["count"])

    def last_signal_time_ms(self, symbol: str) -> int | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT MAX(created_at_ms) AS ts FROM signals WHERE symbol = ?",
                (symbol,),
            ).fetchone()
        return None if row["ts"] is None else int(row["ts"])

    def daily_realized_pnl(self, since_ms: int) -> float:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT COALESCE(SUM(pnl_usdt), 0) AS pnl
                FROM paper_positions
                WHERE status = ? AND exit_time_ms >= ?
                """,
                (PositionStatus.CLOSED.value, since_ms),
            ).fetchone()
        return float(row["pnl"])

    @staticmethod
    def _ensure_column(
        conn: sqlite3.Connection,
        table_name: str,
        column_name: str,
        column_definition: str,
    ) -> None:
        columns = {
            row["name"]
            for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        }
        if column_name not in columns:
            conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_definition}")

    @staticmethod
    def _ensure_position_journal_view(conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            DROP VIEW IF EXISTS position_journal;
            CREATE VIEW position_journal AS
            SELECT
                p.id AS position_id,
                p.signal_id,
                p.symbol,
                p.direction,
                p.status,
                p.entry_time_ms,
                p.entry_price,
                p.stop_loss_price,
                p.take_profit_price,
                p.take_profit_1_price,
                p.take_profit_2_price,
                p.take_profit_1_time_ms,
                p.take_profit_1_exit_price,
                p.take_profit_1_quantity,
                p.take_profit_1_pnl_usdt,
                p.take_profit_1_pnl_pct,
                p.trailing_stop_price,
                p.scale_out_enabled,
                p.trailing_active,
                p.trailing_pivot_window,
                p.notional_usdt,
                p.quantity,
                p.initial_quantity,
                p.remaining_quantity,
                p.remaining_notional_usdt,
                p.exit_time_ms,
                p.exit_price,
                p.exit_reason,
                p.pnl_usdt,
                p.pnl_pct,
                s.price_change_pct AS entry_signal_price_change_pct,
                s.quote_volume_usdt AS entry_signal_quote_volume_usdt,
                s.average_quote_volume_usdt AS entry_signal_average_quote_volume_usdt,
                s.volume_ratio AS entry_signal_volume_ratio,
                s.taker_buy_ratio AS entry_signal_taker_buy_ratio,
                s.taker_sell_ratio AS entry_signal_taker_sell_ratio,
                s.open_interest AS entry_signal_open_interest,
                s.open_interest_value_usdt AS entry_signal_open_interest_value_usdt,
                s.oi_delta_pct AS entry_signal_oi_delta_pct,
                s.oi_delta_value_usdt AS entry_signal_oi_delta_value_usdt,
                s.oi_value_to_volume_ratio AS entry_signal_oi_value_to_volume_ratio,
                s.score AS entry_signal_score,
                s.risk_allowed AS entry_signal_risk_allowed,
                s.risk_reason AS entry_signal_risk_reason
            FROM paper_positions p
            LEFT JOIN signals s ON s.id = p.signal_id;
            """
        )

    @staticmethod
    def _row_to_position(row: sqlite3.Row) -> PaperPosition:
        return PaperPosition(
            id=int(row["id"]),
            signal_id=int(row["signal_id"]),
            symbol=row["symbol"],
            direction=Direction(row["direction"]),
            status=PositionStatus(row["status"]),
            entry_time_ms=int(row["entry_time_ms"]),
            entry_price=float(row["entry_price"]),
            quantity=float(row["quantity"]),
            notional_usdt=float(row["notional_usdt"]),
            stop_loss_price=float(row["stop_loss_price"]),
            take_profit_price=float(row["take_profit_price"]),
            max_hold_seconds=int(row["max_hold_seconds"]),
            initial_quantity=float(row["initial_quantity"]) if row["initial_quantity"] is not None else float(row["quantity"]),
            remaining_quantity=float(row["remaining_quantity"]) if row["remaining_quantity"] is not None else float(row["quantity"]),
            remaining_notional_usdt=float(row["remaining_notional_usdt"])
            if row["remaining_notional_usdt"] is not None
            else float(row["notional_usdt"]),
            scale_out_enabled=bool(row["scale_out_enabled"]),
            trailing_active=bool(row["trailing_active"]),
            take_profit_1_price=row["take_profit_1_price"],
            take_profit_2_price=row["take_profit_2_price"],
            take_profit_1_time_ms=row["take_profit_1_time_ms"],
            take_profit_1_exit_price=row["take_profit_1_exit_price"],
            take_profit_1_quantity=row["take_profit_1_quantity"],
            take_profit_1_pnl_usdt=row["take_profit_1_pnl_usdt"],
            take_profit_1_pnl_pct=row["take_profit_1_pnl_pct"],
            trailing_stop_price=row["trailing_stop_price"],
            trailing_pivot_window=row["trailing_pivot_window"],
            exit_time_ms=row["exit_time_ms"],
            exit_price=row["exit_price"],
            exit_reason=row["exit_reason"],
            pnl_usdt=row["pnl_usdt"],
            pnl_pct=row["pnl_pct"],
        )
