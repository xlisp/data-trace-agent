"""
Build a fake data warehouse for the data-lineage POC.

Simulates: 4 upstream "sources" → 1 aggregated `daily_metrics` table.

Sources:
  - s3_clickstream_raw   (S3 dump of page-view events)
  - app_logs_raw         (server log records)
  - customer_a_orders_raw
  - customer_b_orders_raw

Aggregate:
  - daily_metrics(date, total_events, active_users, total_orders, total_revenue)

Today's row is deliberately anomalous so the agent has something to trace:
  * customer_b feed failed (only a handful of orders ingested) → revenue drop
  * app log spam filter mis-applied → event count spike
"""
from __future__ import annotations

import os
import random
import sqlite3
from datetime import date, datetime, timedelta

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
DB_PATH = os.path.join(DATA_DIR, "warehouse.db")

TODAY = date(2026, 4, 26)
HISTORY_DAYS = 30

# Lineage that downstream agent code will register into the MCP server.
# (target_table, target_field, [source_tables], [source_fields], join_condition/notes)
LINEAGE = [
    (
        "daily_metrics", "total_events",
        ["s3_clickstream_raw", "app_logs_raw"],
        ["event_id", "log_id"],
        "ETL job `agg_events_daily`: COUNT(*) over both raw event streams grouped by event_date.",
    ),
    (
        "daily_metrics", "active_users",
        ["s3_clickstream_raw", "app_logs_raw", "customer_a_orders_raw", "customer_b_orders_raw"],
        ["user_id", "user_id", "user_id", "user_id"],
        "ETL job `agg_users_daily`: COUNT(DISTINCT user_id) UNION across all four raw sources by event_date.",
    ),
    (
        "daily_metrics", "total_orders",
        ["customer_a_orders_raw", "customer_b_orders_raw"],
        ["order_id", "order_id"],
        "ETL job `agg_orders_daily`: COUNT(*) over the two customer order feeds grouped by order_date.",
    ),
    (
        "daily_metrics", "total_revenue",
        ["customer_a_orders_raw", "customer_b_orders_raw"],
        ["amount", "amount"],
        "ETL job `agg_orders_daily`: SUM(amount) over the two customer order feeds grouped by order_date.",
    ),
    (
        "daily_metrics", "report_date",
        ["s3_clickstream_raw", "app_logs_raw", "customer_a_orders_raw", "customer_b_orders_raw"],
        ["ts", "ts", "ts", "ts"],
        "Derived as DATE(ts) from any of the raw timestamps.",
    ),
]


def _reset(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    for t in (
        "s3_clickstream_raw",
        "app_logs_raw",
        "customer_a_orders_raw",
        "customer_b_orders_raw",
        "daily_metrics",
        "_field_lineage",
    ):
        cur.execute(f"DROP TABLE IF EXISTS {t}")
    conn.commit()


def _create_schema(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE s3_clickstream_raw (
            event_id   INTEGER PRIMARY KEY,
            user_id    INTEGER NOT NULL,
            page       TEXT    NOT NULL,
            ts         TEXT    NOT NULL,
            source     TEXT    NOT NULL DEFAULT 's3://events/clickstream'
        );
        CREATE TABLE app_logs_raw (
            log_id     INTEGER PRIMARY KEY,
            level      TEXT    NOT NULL,
            user_id    INTEGER,
            action     TEXT    NOT NULL,
            ts         TEXT    NOT NULL,
            source     TEXT    NOT NULL DEFAULT 'fluentd://app-logs'
        );
        CREATE TABLE customer_a_orders_raw (
            order_id   INTEGER PRIMARY KEY,
            user_id    INTEGER NOT NULL,
            amount     REAL    NOT NULL,
            ts         TEXT    NOT NULL,
            source     TEXT    NOT NULL DEFAULT 'sftp://customer-a/orders'
        );
        CREATE TABLE customer_b_orders_raw (
            order_id   INTEGER PRIMARY KEY,
            user_id    INTEGER NOT NULL,
            amount     REAL    NOT NULL,
            ts         TEXT    NOT NULL,
            source     TEXT    NOT NULL DEFAULT 'api://customer-b/orders'
        );
        CREATE TABLE daily_metrics (
            report_date    TEXT PRIMARY KEY,
            total_events   INTEGER NOT NULL,
            active_users   INTEGER NOT NULL,
            total_orders   INTEGER NOT NULL,
            total_revenue  REAL    NOT NULL
        );
        CREATE TABLE _field_lineage (
            target_table   TEXT NOT NULL,
            target_field   TEXT NOT NULL,
            source_table   TEXT NOT NULL,
            source_field   TEXT NOT NULL,
            transform      TEXT NOT NULL,
            etl_job        TEXT NOT NULL,
            PRIMARY KEY (target_table, target_field, source_table, source_field)
        );
        """
    )
    conn.commit()


def _seed_lineage(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    rows = []
    for tgt_t, tgt_f, src_ts, src_fs, note in LINEAGE:
        # The note doubles as transform description; etl_job is parsed from it.
        etl_job = note.split("`")[1] if "`" in note else ""
        for src_t, src_f in zip(src_ts, src_fs):
            rows.append((tgt_t, tgt_f, src_t, src_f, note, etl_job))
    cur.executemany(
        "INSERT INTO _field_lineage(target_table, target_field, source_table, source_field, transform, etl_job)"
        " VALUES (?,?,?,?,?,?)",
        rows,
    )
    conn.commit()


def _seed_raw(conn: sqlite3.Connection) -> None:
    rng = random.Random(42)
    cur = conn.cursor()
    s3_id = log_id = a_id = b_id = 1

    for offset in range(HISTORY_DAYS, -1, -1):
        day = TODAY - timedelta(days=offset)
        is_today = (day == TODAY)

        # ---- s3 clickstream: ~1000/day, normal today ----
        n_events = rng.randint(950, 1050)
        for _ in range(n_events):
            ts = datetime.combine(day, datetime.min.time()) + timedelta(seconds=rng.randint(0, 86_399))
            cur.execute(
                "INSERT INTO s3_clickstream_raw(event_id, user_id, page, ts) VALUES (?,?,?,?)",
                (s3_id, rng.randint(1, 500), rng.choice(["/", "/pricing", "/docs", "/blog"]), ts.isoformat()),
            )
            s3_id += 1

        # ---- app logs: ~500/day normally, spike to ~1800 today (spam filter mis-applied) ----
        n_logs = rng.randint(1700, 1900) if is_today else rng.randint(450, 550)
        for _ in range(n_logs):
            ts = datetime.combine(day, datetime.min.time()) + timedelta(seconds=rng.randint(0, 86_399))
            cur.execute(
                "INSERT INTO app_logs_raw(log_id, level, user_id, action, ts) VALUES (?,?,?,?,?)",
                (
                    log_id,
                    rng.choice(["INFO", "INFO", "INFO", "WARN", "ERROR"]),
                    rng.randint(1, 500) if rng.random() > 0.1 else None,
                    rng.choice(["login", "view", "click", "logout"]),
                    ts.isoformat(),
                ),
            )
            log_id += 1

        # ---- customer A orders: ~50/day, normal today ----
        n_a = rng.randint(45, 55)
        for _ in range(n_a):
            ts = datetime.combine(day, datetime.min.time()) + timedelta(seconds=rng.randint(0, 86_399))
            cur.execute(
                "INSERT INTO customer_a_orders_raw(order_id, user_id, amount, ts) VALUES (?,?,?,?)",
                (a_id, rng.randint(1, 500), round(rng.uniform(40, 200), 2), ts.isoformat()),
            )
            a_id += 1

        # ---- customer B orders: ~80/day normally, only 5 today (feed broken) ----
        n_b = 5 if is_today else rng.randint(75, 85)
        for _ in range(n_b):
            ts = datetime.combine(day, datetime.min.time()) + timedelta(seconds=rng.randint(0, 86_399))
            cur.execute(
                "INSERT INTO customer_b_orders_raw(order_id, user_id, amount, ts) VALUES (?,?,?,?)",
                (b_id, rng.randint(1, 500), round(rng.uniform(60, 250), 2), ts.isoformat()),
            )
            b_id += 1

    conn.commit()


def _aggregate(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO daily_metrics(report_date, total_events, active_users, total_orders, total_revenue)
        WITH events AS (
            SELECT DATE(ts) AS d, user_id FROM s3_clickstream_raw
            UNION ALL
            SELECT DATE(ts) AS d, user_id FROM app_logs_raw
        ),
        orders AS (
            SELECT DATE(ts) AS d, user_id, amount FROM customer_a_orders_raw
            UNION ALL
            SELECT DATE(ts) AS d, user_id, amount FROM customer_b_orders_raw
        ),
        all_users AS (
            SELECT d, user_id FROM events
            UNION
            SELECT d, user_id FROM orders
        ),
        evt_agg    AS (SELECT d, COUNT(*) AS n_events FROM events GROUP BY d),
        usr_agg    AS (SELECT d, COUNT(DISTINCT user_id) AS n_users FROM all_users GROUP BY d),
        ord_agg    AS (SELECT d, COUNT(*) AS n_orders, COALESCE(SUM(amount),0) AS revenue FROM orders GROUP BY d)
        SELECT
            evt_agg.d,
            evt_agg.n_events,
            COALESCE(usr_agg.n_users, 0),
            COALESCE(ord_agg.n_orders, 0),
            COALESCE(ord_agg.revenue, 0)
        FROM evt_agg
        LEFT JOIN usr_agg ON usr_agg.d = evt_agg.d
        LEFT JOIN ord_agg ON ord_agg.d = evt_agg.d
        ORDER BY evt_agg.d
        """
    )
    conn.commit()


def main() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    conn = sqlite3.connect(DB_PATH)
    try:
        _reset(conn)
        _create_schema(conn)
        _seed_lineage(conn)
        _seed_raw(conn)
        _aggregate(conn)
        cur = conn.cursor()
        cur.execute("SELECT report_date, total_events, active_users, total_orders, total_revenue "
                    "FROM daily_metrics ORDER BY report_date DESC LIMIT 5")
        rows = cur.fetchall()
    finally:
        conn.close()

    print(f"[ok] warehouse built at {DB_PATH}")
    print("[ok] last 5 days of daily_metrics:")
    for r in rows:
        print(f"     {r}")
    print("[ok] lineage entries to register at agent startup:")
    for tgt_t, tgt_f, src_t, src_f, note in LINEAGE:
        print(f"     {tgt_t}.{tgt_f}  <-  " + ", ".join(f"{t}.{f}" for t, f in zip(src_t, src_f)))


if __name__ == "__main__":
    main()
