"""
Build a fake data warehouse for the data-lineage POC.

Scenario:
  Upstream file dumps (S3 / log shipper / SFTP / API) → buggy ETL loaders →
  raw DB tables → aggregated `daily_metrics` table.

Layout:
  data/
    warehouse.db               -- the SQLite warehouse
    sources/                   -- physical "upstream" files
      s3_clickstream/2026-04-26.json
      app_logs/2026-04-26.log
      customer_a/2026-04-26.csv
      customer_b/2026-04-26.csv

History strategy:
  - Days 1..29 are random-simulated and inserted directly into the raw tables
    (no files on disk; this is just bulk noise for averaging).
  - "Today" (2026-04-26) is the interesting day. We write four real source
    files, then run intentionally-buggy loaders that import them into the raw
    tables. The bugs produce a file-vs-DB discrepancy that the agent has to
    find by reading both.

Planted bugs for today:
  1) Customer B currency bug — file has 80 orders (75 EUR + 5 USD); the loader
     silently filters out non-USD rows. DB ends up with only 5 rows.
     ⇒ explains a ~60% drop in `daily_metrics.total_revenue`.

  2) Customer A precision bug — file has float amounts like `99.99`; the loader
     casts with `int(amount)` instead of `float(amount)`. DB amounts are
     truncated to whole dollars.
     ⇒ a smaller, subtler discrepancy: file says 99.99, DB says 99.

The other two sources (s3_clickstream, app_logs) load cleanly: their file row
count matches the DB row count. Useful as a "control" — the agent should NOT
flag these as buggy.
"""
from __future__ import annotations

import csv
import json
import os
import random
import sqlite3
from datetime import date, datetime, timedelta

# ---------- paths ----------
HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
SOURCES_DIR = os.path.join(DATA_DIR, "sources")
DB_PATH = os.path.join(DATA_DIR, "warehouse.db")

TODAY = date(2026, 4, 26)
HISTORY_DAYS = 30  # 30 prior days of synthesized history before today

# ---------- lineage metadata (target_field -> upstream sources) ----------
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

# (source_table, source_uri, file_basename, loader_name, schema_note)
SOURCE_REGISTRY = [
    (
        "s3_clickstream_raw",
        "s3://events/clickstream",
        os.path.join(SOURCES_DIR, "s3_clickstream"),
        "load_s3_clickstream",
        "NDJSON, one event per line: {event_id, user_id, page, ts}.",
    ),
    (
        "app_logs_raw",
        "fluentd://app-logs",
        os.path.join(SOURCES_DIR, "app_logs"),
        "load_app_logs",
        "Plain text log: '<ts> <level> <user_id|->  <action>' one per line.",
    ),
    (
        "customer_a_orders_raw",
        "sftp://customer-a/orders",
        os.path.join(SOURCES_DIR, "customer_a"),
        "load_customer_a_orders",
        "CSV with header: order_id,user_id,amount,currency,ts. All amounts USD with 2 decimals.",
    ),
    (
        "customer_b_orders_raw",
        "api://customer-b/orders",
        os.path.join(SOURCES_DIR, "customer_b"),
        "load_customer_b_orders",
        "CSV with header: order_id,user_id,amount,currency,ts. Mixed USD/EUR rows.",
    ),
]


# ---------- DB schema ----------
def _reset(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    for t in (
        "s3_clickstream_raw",
        "app_logs_raw",
        "customer_a_orders_raw",
        "customer_b_orders_raw",
        "daily_metrics",
        "_field_lineage",
        "_source_registry",
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
        CREATE TABLE _source_registry (
            source_table   TEXT PRIMARY KEY,
            source_uri     TEXT NOT NULL,
            file_dir       TEXT NOT NULL,
            loader         TEXT NOT NULL,
            schema_note    TEXT NOT NULL
        );
        """
    )
    conn.commit()


def _seed_metadata(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    rows = []
    for tgt_t, tgt_f, src_ts, src_fs, note in LINEAGE:
        etl_job = note.split("`")[1] if "`" in note else ""
        for src_t, src_f in zip(src_ts, src_fs):
            rows.append((tgt_t, tgt_f, src_t, src_f, note, etl_job))
    cur.executemany(
        "INSERT INTO _field_lineage(target_table, target_field, source_table, source_field, transform, etl_job)"
        " VALUES (?,?,?,?,?,?)",
        rows,
    )
    cur.executemany(
        "INSERT INTO _source_registry(source_table, source_uri, file_dir, loader, schema_note) VALUES (?,?,?,?,?)",
        SOURCE_REGISTRY,
    )
    conn.commit()


# ---------- random history (days 1..29) ----------
def _seed_history_days(conn: sqlite3.Connection) -> None:
    rng = random.Random(42)
    cur = conn.cursor()
    s3_id = log_id = a_id = b_id = 1

    for offset in range(HISTORY_DAYS, 0, -1):  # NOTE: 0 excluded — today is loaded from files
        day = TODAY - timedelta(days=offset)

        n_events = rng.randint(950, 1050)
        for _ in range(n_events):
            ts = datetime.combine(day, datetime.min.time()) + timedelta(seconds=rng.randint(0, 86_399))
            cur.execute(
                "INSERT INTO s3_clickstream_raw(event_id, user_id, page, ts) VALUES (?,?,?,?)",
                (s3_id, rng.randint(1, 500), rng.choice(["/", "/pricing", "/docs", "/blog"]), ts.isoformat()),
            )
            s3_id += 1

        n_logs = rng.randint(450, 550)
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

        n_a = rng.randint(45, 55)
        for _ in range(n_a):
            ts = datetime.combine(day, datetime.min.time()) + timedelta(seconds=rng.randint(0, 86_399))
            cur.execute(
                "INSERT INTO customer_a_orders_raw(order_id, user_id, amount, ts) VALUES (?,?,?,?)",
                (a_id, rng.randint(1, 500), round(rng.uniform(40, 200), 2), ts.isoformat()),
            )
            a_id += 1

        n_b = rng.randint(75, 85)
        for _ in range(n_b):
            ts = datetime.combine(day, datetime.min.time()) + timedelta(seconds=rng.randint(0, 86_399))
            cur.execute(
                "INSERT INTO customer_b_orders_raw(order_id, user_id, amount, ts) VALUES (?,?,?,?)",
                (b_id, rng.randint(1, 500), round(rng.uniform(60, 250), 2), ts.isoformat()),
            )
            b_id += 1

    conn.commit()


# ---------- today: write physical files, then run buggy loaders ----------
def _today_iso(seconds: int) -> str:
    return (datetime.combine(TODAY, datetime.min.time()) + timedelta(seconds=seconds)).isoformat()


def _write_today_files() -> dict:
    """Generate the four upstream source files for TODAY. Returns paths."""
    rng = random.Random(2026_04_26)

    s3_dir = os.path.join(SOURCES_DIR, "s3_clickstream")
    log_dir = os.path.join(SOURCES_DIR, "app_logs")
    a_dir = os.path.join(SOURCES_DIR, "customer_a")
    b_dir = os.path.join(SOURCES_DIR, "customer_b")
    for d in (s3_dir, log_dir, a_dir, b_dir):
        os.makedirs(d, exist_ok=True)

    s3_path = os.path.join(s3_dir, f"{TODAY.isoformat()}.json")
    log_path = os.path.join(log_dir, f"{TODAY.isoformat()}.log")
    a_path = os.path.join(a_dir, f"{TODAY.isoformat()}.csv")
    b_path = os.path.join(b_dir, f"{TODAY.isoformat()}.csv")

    # ---- s3 clickstream: 1000 events, NDJSON ----
    with open(s3_path, "w") as f:
        for i in range(1000):
            f.write(json.dumps({
                "event_id": 9_000_000 + i,
                "user_id": rng.randint(1, 500),
                "page": rng.choice(["/", "/pricing", "/docs", "/blog"]),
                "ts": _today_iso(rng.randint(0, 86_399)),
            }) + "\n")

    # ---- app logs: 500 lines (clean) ----
    with open(log_path, "w") as f:
        for i in range(500):
            uid = rng.randint(1, 500)
            line = (
                f"{_today_iso(rng.randint(0, 86_399))} "
                f"{rng.choice(['INFO', 'INFO', 'INFO', 'WARN', 'ERROR'])} "
                f"{uid if rng.random() > 0.1 else '-'} "
                f"{rng.choice(['login', 'view', 'click', 'logout'])}"
            )
            f.write(line + "\n")

    # ---- customer A: 51 orders, USD only, amounts to 2 decimal places ----
    with open(a_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["order_id", "user_id", "amount", "currency", "ts"])
        for i in range(51):
            w.writerow([
                7_000_000 + i,
                rng.randint(1, 500),
                f"{round(rng.uniform(40, 200), 2):.2f}",
                "USD",
                _today_iso(rng.randint(0, 86_399)),
            ])

    # ---- customer B: 80 orders, mostly EUR, only 5 USD ----
    with open(b_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["order_id", "user_id", "amount", "currency", "ts"])
        for i in range(80):
            currency = "USD" if i < 5 else "EUR"
            w.writerow([
                8_000_000 + i,
                rng.randint(1, 500),
                f"{round(rng.uniform(60, 250), 2):.2f}",
                currency,
                _today_iso(rng.randint(0, 86_399)),
            ])

    return {"s3": s3_path, "log": log_path, "a": a_path, "b": b_path}


def _load_today_into_db(conn: sqlite3.Connection, paths: dict) -> None:
    """Run the (buggy!) loaders for today's files."""
    cur = conn.cursor()

    # ---- load_s3_clickstream: clean ----
    with open(paths["s3"]) as f:
        for line in f:
            row = json.loads(line)
            cur.execute(
                "INSERT INTO s3_clickstream_raw(event_id, user_id, page, ts) VALUES (?,?,?,?)",
                (row["event_id"], row["user_id"], row["page"], row["ts"]),
            )

    # ---- load_app_logs: clean ----
    with open(paths["log"]) as f:
        for i, line in enumerate(f):
            ts, level, uid, action = line.strip().split(" ", 3)
            cur.execute(
                "INSERT INTO app_logs_raw(log_id, level, user_id, action, ts) VALUES (?,?,?,?,?)",
                (5_000_000 + i, level, None if uid == "-" else int(uid), action, ts),
            )

    # ---- load_customer_a_orders: BUGGY (precision: int() instead of float()) ----
    with open(paths["a"]) as f:
        reader = csv.DictReader(f)
        for row in reader:
            cur.execute(
                "INSERT INTO customer_a_orders_raw(order_id, user_id, amount, ts) VALUES (?,?,?,?)",
                # BUG: should be float(row["amount"]); int() truncates the cents.
                (int(row["order_id"]), int(row["user_id"]), int(float(row["amount"])), row["ts"]),
            )

    # ---- load_customer_b_orders: BUGGY (currency filter drops EUR rows) ----
    with open(paths["b"]) as f:
        reader = csv.DictReader(f)
        for row in reader:
            # BUG: silently skips non-USD rows instead of converting EUR -> USD.
            if row["currency"] != "USD":
                continue
            cur.execute(
                "INSERT INTO customer_b_orders_raw(order_id, user_id, amount, ts) VALUES (?,?,?,?)",
                (int(row["order_id"]), int(row["user_id"]), float(row["amount"]), row["ts"]),
            )

    conn.commit()


# ---------- aggregation ----------
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
        evt_agg AS (SELECT d, COUNT(*) AS n_events FROM events GROUP BY d),
        usr_agg AS (SELECT d, COUNT(DISTINCT user_id) AS n_users FROM all_users GROUP BY d),
        ord_agg AS (SELECT d, COUNT(*) AS n_orders, COALESCE(SUM(amount),0) AS revenue FROM orders GROUP BY d)
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
        _seed_metadata(conn)
        _seed_history_days(conn)
        paths = _write_today_files()
        _load_today_into_db(conn, paths)
        _aggregate(conn)

        cur = conn.cursor()
        cur.execute(
            "SELECT report_date, total_events, active_users, total_orders, total_revenue "
            "FROM daily_metrics ORDER BY report_date DESC LIMIT 5"
        )
        last5 = cur.fetchall()
        cur.execute("SELECT COUNT(*) FROM customer_b_orders_raw WHERE DATE(ts)='2026-04-26'")
        b_in_db = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM customer_a_orders_raw WHERE DATE(ts)='2026-04-26'")
        a_in_db = cur.fetchone()[0]
    finally:
        conn.close()

    print(f"[ok] warehouse: {DB_PATH}")
    print(f"[ok] sources:   {SOURCES_DIR}")
    print(f"[ok] today files: {paths}")
    print("[ok] last 5 days of daily_metrics:")
    for r in last5:
        print(f"     {r}")
    print(f"[ok] customer_b: file=80 rows  ->  DB={b_in_db} rows  (bug: dropped non-USD)")
    print(f"[ok] customer_a: file=51 rows  ->  DB={a_in_db} rows  (bug: amount int-truncated)")


if __name__ == "__main__":
    main()
