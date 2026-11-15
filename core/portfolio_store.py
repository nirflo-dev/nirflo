from datetime import date

import pandas as pd
import psycopg2
from psycopg2 import errors
from psycopg2.extras import Json

from core.config import DB_CONFIG

CONNECT_TIMEOUT = 3
DEFAULT_PORTFOLIO = "default"
EVALUATION_COLUMNS = [
    "as_of",
    "avg_correlation",
    "sharpe_since_buy",
    "sharpe_current_weights",
    "sharpe_equal_weight",
    "warnings",
]

DDL = """
    CREATE TABLE IF NOT EXISTS portfolio_holdings (
        id         bigserial PRIMARY KEY,
        portfolio  text NOT NULL DEFAULT 'default',
        ticker     varchar(10) NOT NULL REFERENCES emitens(ticker),
        lots       numeric(14,2) NOT NULL CHECK (lots > 0),
        buy_date   date NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now()
    );
    CREATE INDEX IF NOT EXISTS portfolio_holdings_portfolio_idx
        ON portfolio_holdings (portfolio);

    CREATE TABLE IF NOT EXISTS portfolio_evaluations (
        portfolio              text NOT NULL,
        as_of                  date NOT NULL,
        avg_correlation        numeric,
        sharpe_since_buy       numeric,
        sharpe_current_weights numeric,
        sharpe_equal_weight    numeric,
        warnings               integer NOT NULL DEFAULT 0,
        result                 jsonb NOT NULL,
        computed_at            timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (portfolio, as_of)
    );
"""


def _connect():
    return psycopg2.connect(**DB_CONFIG, connect_timeout=CONNECT_TIMEOUT)


def ensure_table():
    conn = _connect()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(DDL)
    finally:
        conn.close()


def clean_ticker(value):
    ticker = str(value).strip().upper().removesuffix(".JK")
    if not ticker:
        raise ValueError("ticker kosong")
    return ticker


def add_holdings(portfolio, rows):
    clean = []
    for ticker, lots, buy_date in rows:
        lots = float(lots)
        if lots <= 0:
            raise ValueError("lots harus lebih dari 0")
        buy_date = date.fromisoformat(str(buy_date).strip())
        if buy_date > date.today():
            raise ValueError("buy_date tidak boleh di masa depan")
        clean.append((portfolio, clean_ticker(ticker), lots, buy_date))
    conn = _connect()
    try:
        with conn, conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO portfolio_holdings (portfolio, ticker, lots, buy_date) "
                "VALUES (%s, %s, %s, %s)",
                clean,
            )
    except errors.ForeignKeyViolation as exc:
        raise ValueError(
            "ticker tidak terdaftar di emitens: " + (exc.diag.message_detail or "")
        ) from exc
    finally:
        conn.close()
    return len(clean)


def list_holdings(portfolio=None):
    sql = "SELECT id, portfolio, ticker, lots, buy_date FROM portfolio_holdings"
    params = ()
    if portfolio:
        sql += " WHERE portfolio = %s"
        params = (portfolio,)
    sql += " ORDER BY portfolio, buy_date, id"
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
    finally:
        conn.close()
    df = pd.DataFrame(rows, columns=["id", "portfolio", "ticker", "lots", "buy_date"])
    df["lots"] = df["lots"].astype(float)
    df["buy_date"] = pd.to_datetime(df["buy_date"])
    return df


def list_portfolios():
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT portfolio FROM portfolio_holdings ORDER BY portfolio")
            return [row[0] for row in cur.fetchall()]
    finally:
        conn.close()


def remove_holding(holding_id):
    conn = _connect()
    try:
        with conn, conn.cursor() as cur:
            cur.execute("DELETE FROM portfolio_holdings WHERE id = %s", (holding_id,))
            return cur.rowcount > 0
    finally:
        conn.close()


def save_evaluation(portfolio, result):
    sharpe = result["sharpe"]
    conn = _connect()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO portfolio_evaluations
                    (portfolio, as_of, avg_correlation, sharpe_since_buy,
                     sharpe_current_weights, sharpe_equal_weight, warnings, result)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (portfolio, as_of) DO UPDATE SET
                    avg_correlation = EXCLUDED.avg_correlation,
                    sharpe_since_buy = EXCLUDED.sharpe_since_buy,
                    sharpe_current_weights = EXCLUDED.sharpe_current_weights,
                    sharpe_equal_weight = EXCLUDED.sharpe_equal_weight,
                    warnings = EXCLUDED.warnings,
                    result = EXCLUDED.result,
                    computed_at = now();
                """,
                (
                    portfolio,
                    result["as_of"],
                    result["correlation"]["average"],
                    sharpe["since_buy"]["sharpe"],
                    sharpe["current_weights"]["sharpe"],
                    sharpe["equal_weight"]["sharpe"],
                    len(result["warnings"]),
                    Json(result),
                ),
            )
    finally:
        conn.close()


def list_evaluations(portfolio, limit=30):
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT as_of, avg_correlation, sharpe_since_buy, sharpe_current_weights, "
                "sharpe_equal_weight, warnings FROM portfolio_evaluations "
                "WHERE portfolio = %s ORDER BY as_of DESC LIMIT %s",
                (portfolio, limit),
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    df = pd.DataFrame(rows, columns=EVALUATION_COLUMNS)
    for column in EVALUATION_COLUMNS[1:5]:
        df[column] = df[column].astype(float)
    return df