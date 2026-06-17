"""SQLite persistence for tweets, orders, stops, and daily limits."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterator


class StateDB:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS processed_tweets (
                    tweet_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    text TEXT NOT NULL,
                    signal_json TEXT,
                    processed_at TEXT NOT NULL,
                    action_taken TEXT
                );
                CREATE TABLE IF NOT EXISTS orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tweet_id TEXT,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    amount_krw REAL,
                    qty REAL,
                    price REAL,
                    status TEXT NOT NULL,
                    confirm_token TEXT,
                    order_id TEXT,
                    dry_run INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    raw_json TEXT
                );
                CREATE TABLE IF NOT EXISTS active_stops (
                    broker TEXT NOT NULL DEFAULT 'alpaca',
                    symbol TEXT NOT NULL,
                    stop_price REAL NOT NULL,
                    qty REAL,
                    tweet_id TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (broker, symbol)
                );
                CREATE TABLE IF NOT EXISTS daily_buy_totals (
                    day TEXT PRIMARY KEY,
                    total_krw REAL NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS x_cursor (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS llm_parse_cache (
                    tweet_id TEXT PRIMARY KEY,
                    text TEXT NOT NULL,
                    signal_json TEXT NOT NULL,
                    parsed_at TEXT NOT NULL
                );
                """
            )
            self._migrate_stops_broker(conn)

    def _migrate_stops_broker(self, conn: sqlite3.Connection) -> None:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(active_stops)")}
        if "broker" in cols:
            return
        conn.executescript(
            """
            CREATE TABLE active_stops_new (
                broker TEXT NOT NULL,
                symbol TEXT NOT NULL,
                stop_price REAL NOT NULL,
                qty REAL,
                tweet_id TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (broker, symbol)
            );
            INSERT INTO active_stops_new (broker, symbol, stop_price, qty, tweet_id, updated_at)
            SELECT 'alpaca', symbol, stop_price, qty, tweet_id, updated_at FROM active_stops;
            DROP TABLE active_stops;
            ALTER TABLE active_stops_new RENAME TO active_stops;
            """
        )

    def get_llm_parse_cache(self, tweet_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT text, signal_json FROM llm_parse_cache WHERE tweet_id = ?",
                (tweet_id,),
            ).fetchone()
            if not row:
                return None
            return {
                "text": row["text"],
                "signal": json.loads(row["signal_json"]),
            }

    def set_llm_parse_cache(self, tweet_id: str, text: str, signal: dict[str, Any]) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO llm_parse_cache
                (tweet_id, text, signal_json, parsed_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    tweet_id,
                    text,
                    json.dumps(signal, ensure_ascii=False),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

    def tweet_seen(self, tweet_id: str) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM processed_tweets WHERE tweet_id = ?", (tweet_id,)
            ).fetchone()
            return row is not None

    def mark_tweet(
        self,
        tweet_id: str,
        created_at: str,
        text: str,
        signal: dict[str, Any] | None,
        action_taken: str,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO processed_tweets
                (tweet_id, created_at, text, signal_json, processed_at, action_taken)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    tweet_id,
                    created_at,
                    text,
                    json.dumps(signal, ensure_ascii=False) if signal else None,
                    datetime.now(timezone.utc).isoformat(),
                    action_taken,
                ),
            )

    def record_order(self, **kwargs: Any) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO orders
                (tweet_id, symbol, side, amount_krw, qty, price, status,
                 confirm_token, order_id, dry_run, created_at, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    kwargs.get("tweet_id"),
                    kwargs["symbol"],
                    kwargs["side"],
                    kwargs.get("amount_krw"),
                    kwargs.get("qty"),
                    kwargs.get("price"),
                    kwargs["status"],
                    kwargs.get("confirm_token"),
                    kwargs.get("order_id"),
                    1 if kwargs.get("dry_run") else 0,
                    datetime.now(timezone.utc).isoformat(),
                    json.dumps(kwargs.get("raw_json")) if kwargs.get("raw_json") else None,
                ),
            )
            return int(cur.lastrowid)

    def get_cursor(self, key: str) -> str | None:
        with self._conn() as conn:
            row = conn.execute("SELECT value FROM x_cursor WHERE key = ?", (key,)).fetchone()
            return row["value"] if row else None

    def set_cursor(self, key: str, value: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO x_cursor (key, value) VALUES (?, ?)", (key, value)
            )

    def get_daily_buy_total(self, day: date | None = None) -> float:
        d = (day or date.today()).isoformat()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT total_krw FROM daily_buy_totals WHERE day = ?", (d,)
            ).fetchone()
            return float(row["total_krw"]) if row else 0.0

    def add_daily_buy(self, amount_krw: float, day: date | None = None) -> float:
        d = (day or date.today()).isoformat()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT total_krw FROM daily_buy_totals WHERE day = ?", (d,)
            ).fetchone()
            new_total = (float(row["total_krw"]) if row else 0.0) + amount_krw
            conn.execute(
                """
                INSERT INTO daily_buy_totals (day, total_krw) VALUES (?, ?)
                ON CONFLICT(day) DO UPDATE SET total_krw = excluded.total_krw
                """,
                (d, new_total),
            )
            return new_total

    def upsert_stop(
        self,
        symbol: str,
        stop_price: float,
        qty: float | None,
        tweet_id: str | None,
        *,
        broker: str = "alpaca",
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO active_stops
                (broker, symbol, stop_price, qty, tweet_id, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    broker.lower(),
                    symbol,
                    stop_price,
                    qty,
                    tweet_id,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

    def remove_stop(self, symbol: str, *, broker: str = "alpaca") -> None:
        with self._conn() as conn:
            conn.execute(
                "DELETE FROM active_stops WHERE broker = ? AND symbol = ?",
                (broker.lower(), symbol),
            )

    def list_active_stops(self, *, broker: str | None = None) -> list[dict[str, Any]]:
        with self._conn() as conn:
            if broker:
                rows = conn.execute(
                    "SELECT * FROM active_stops WHERE broker = ?", (broker.lower(),)
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM active_stops").fetchall()
            return [dict(r) for r in rows]
