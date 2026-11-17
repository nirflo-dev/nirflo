import csv
import io

import numpy as np
import pandas as pd
import psycopg2
from psycopg2.extras import execute_values

from core.config import DB_CONFIG

CONNECT_TIMEOUT = 3
STATS_COLUMNS = [
    "as_of",
    "ticker",
    "n_weeks",
    "mean_excess_weekly",
    "std_weekly",
    "annual_excess_return",
    "annual_vol",
    "sharpe",
    "sharpe_ci_low",
    "sharpe_ci_high",
    "no_trade_ratio",
    "last_price",
    "last_date",
    "max_daily_move",
    "eligible",
    "note",
]

DDL = """
    CREATE TABLE IF NOT EXISTS universe_stats (
        as_of                date NOT NULL,
        ticker               varchar(10) NOT NULL,
        n_weeks              integer NOT NULL,
        mean_excess_weekly   double precision,
        std_weekly           double precision,
        annual_excess_return numeric,
        annual_vol           numeric,
        sharpe               numeric,
        sharpe_ci_low        numeric,
        sharpe_ci_high       numeric,
        no_trade_ratio       numeric,
        last_price           numeric,
        last_date            date,
        max_daily_move       numeric,
        eligible             boolean NOT NULL,
        note                 text NOT NULL DEFAULT '',
        computed_at          timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (as_of, ticker)
    );

    CREATE TABLE IF NOT EXISTS universe_correlation_latest (
        ticker_a    varchar(10) NOT NULL,
        ticker_b    varchar(10) NOT NULL,
        correlation real NOT NULL,
        n_obs       smallint NOT NULL,
        as_of       date NOT NULL,
        PRIMARY KEY (ticker_a, ticker_b)
    );
"""


def _connect():
    return psycopg2.connect(**DB_CONFIG, connect_timeout=CONNECT_TIMEOUT)


def ensure_tables():
    conn = _connect()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(DDL)
    finally:
        conn.close()


def _stats_rows(as_of, stats):
    frame = stats.reset_index()
    frame.insert(0, "as_of", as_of)
    frame = frame[STATS_COLUMNS]
    frame["last_date"] = pd.to_datetime(frame["last_date"]).dt.date
    frame = frame.astype(object)
    frame = frame.where(frame.notna(), None)
    return [tuple(row) for row in frame.itertuples(index=False, name=None)]


def save_universe(as_of, stats, corr, pair_n):
    rows = _stats_rows(as_of, stats)

    tickers = list(corr.index)
    values = corr.to_numpy(dtype=float)
    counts = pair_n.to_numpy()
    first, second = np.triu_indices(len(tickers), k=1)
    keep = np.isfinite(values[first, second])
    first, second = first[keep], second[keep]

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    for i, j, value, count in zip(first, second, values[first, second], counts[first, second]):
        writer.writerow((tickers[i], tickers[j], f"{value:.5f}", int(count), as_of))
    buffer.seek(0)

    columns = ", ".join(STATS_COLUMNS)
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in STATS_COLUMNS[2:])
    conn = _connect()
    try:
        with conn, conn.cursor() as cur:
            execute_values(
                cur,
                f"INSERT INTO universe_stats ({columns}) VALUES %s "
                f"ON CONFLICT (as_of, ticker) DO UPDATE SET {updates}, computed_at = now()",
                rows,
                page_size=500,
            )
            cur.execute("TRUNCATE universe_correlation_latest")
            cur.copy_expert(
                "COPY universe_correlation_latest (ticker_a, ticker_b, correlation, n_obs, as_of) "
                "FROM STDIN WITH (FORMAT csv)",
                buffer,
            )
    finally:
        conn.close()
    return len(rows), int(keep.sum())


def read_stats(tickers):
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT max(as_of) FROM universe_stats")
            as_of = cur.fetchone()[0]
            if as_of is None:
                return None, pd.DataFrame()
            cur.execute(
                "SELECT ticker, n_weeks, mean_excess_weekly, std_weekly, last_price, "
                "eligible, note FROM universe_stats WHERE as_of = %s AND ticker = ANY(%s)",
                (as_of, list(tickers)),
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    frame = pd.DataFrame(
        rows,
        columns=["ticker", "n_weeks", "mean_excess_weekly", "std_weekly", "last_price", "eligible", "note"],
    ).set_index("ticker")
    for column in ("n_weeks", "mean_excess_weekly", "std_weekly", "last_price"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return as_of, frame


def read_correlation(tickers):
    tickers = sorted(tickers)
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT ticker_a, ticker_b, correlation FROM universe_correlation_latest "
                "WHERE ticker_a = ANY(%s) AND ticker_b = ANY(%s)",
                (tickers, tickers),
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    position = {ticker: i for i, ticker in enumerate(tickers)}
    matrix = np.full((len(tickers), len(tickers)), np.nan)
    np.fill_diagonal(matrix, 1.0)
    for a, b, value in rows:
        i, j = position[a], position[b]
        matrix[i, j] = matrix[j, i] = float(value)
    return pd.DataFrame(matrix, index=tickers, columns=tickers)