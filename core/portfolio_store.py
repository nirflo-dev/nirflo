import math
import os
from datetime import date

import pandas as pd
import psycopg2
from psycopg2 import errors
from psycopg2.extras import Json

from core import user_store
from core.config import DB_CONFIG

CONNECT_TIMEOUT = 3
DEFAULT_PORTFOLIO = "default"
# Akun yang dipakai CLI. Isi PORTFOLIO_OWNER dengan user id Clerk (kolom users.clerk_id)
# agar data dari CLI tampil di UI akun yang sama.
DEFAULT_CLERK_ID = os.getenv("PORTFOLIO_OWNER", "local")
MAX_CASH = 1e15
HOLDING_COLUMNS = ["id", "portfolio", "ticker", "lots", "buy_date", "user_id"]
EVALUATION_COLUMNS = [
    "as_of",
    "avg_correlation",
    "sharpe_since_buy",
    "sharpe_current_weights",
    "sharpe_equal_weight",
    "warnings",
]

DDL = """
    CREATE TABLE IF NOT EXISTS portfolios (
        id         bigserial PRIMARY KEY,
        user_id    bigint NOT NULL REFERENCES users (id) ON DELETE CASCADE,
        name       text NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(),
        UNIQUE (user_id, name)
    );

    ALTER TABLE portfolios
        ADD COLUMN IF NOT EXISTS cash numeric(18,2) NOT NULL DEFAULT 0 CHECK (cash >= 0);

    CREATE TABLE IF NOT EXISTS portfolio_holdings (
        id           bigserial PRIMARY KEY,
        portfolio_id bigint NOT NULL REFERENCES portfolios (id) ON DELETE CASCADE,
        ticker       varchar(10) NOT NULL REFERENCES emitens (ticker),
        lots         numeric(14,2) NOT NULL CHECK (lots > 0),
        buy_date     date NOT NULL,
        created_at   timestamptz NOT NULL DEFAULT now()
    );
    CREATE INDEX IF NOT EXISTS portfolio_holdings_portfolio_idx
        ON portfolio_holdings (portfolio_id);

    CREATE TABLE IF NOT EXISTS portfolio_evaluations (
        portfolio_id           bigint NOT NULL REFERENCES portfolios (id) ON DELETE CASCADE,
        as_of                  date NOT NULL,
        avg_correlation        numeric,
        sharpe_since_buy       numeric,
        sharpe_current_weights numeric,
        sharpe_equal_weight    numeric,
        warnings               integer NOT NULL DEFAULT 0,
        result                 jsonb NOT NULL,
        computed_at            timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (portfolio_id, as_of)
    );
"""


def _connect():
    return psycopg2.connect(**DB_CONFIG, connect_timeout=CONNECT_TIMEOUT)


def ensure_table():
    user_store.ensure_table()
    conn = _connect()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(DDL)
    finally:
        conn.close()


def default_user_id():
    return user_store.get_or_create_user(DEFAULT_CLERK_ID)


def clean_ticker(value):
    ticker = str(value).strip().upper().removesuffix(".JK")
    if not ticker:
        raise ValueError("ticker kosong")
    return ticker


def _portfolio_id(cur, user_id, name):
    """Id portofolio milik user ini; dibuat bila belum ada."""
    cur.execute(
        "INSERT INTO portfolios (user_id, name) VALUES (%s, %s) "
        "ON CONFLICT (user_id, name) DO UPDATE SET name = EXCLUDED.name RETURNING id",
        (user_id, name),
    )
    return cur.fetchone()[0]


def add_holdings(portfolio, rows, user_id):
    clean = []
    for ticker, lots, buy_date in rows:
        lots = float(lots)
        if lots <= 0:
            raise ValueError("lots harus lebih dari 0")
        buy_date = date.fromisoformat(str(buy_date).strip())
        if buy_date > date.today():
            raise ValueError("buy_date tidak boleh di masa depan")
        clean.append((clean_ticker(ticker), lots, buy_date))
    conn = _connect()
    try:
        with conn, conn.cursor() as cur:
            pid = _portfolio_id(cur, user_id, portfolio)
            cur.executemany(
                "INSERT INTO portfolio_holdings (portfolio_id, ticker, lots, buy_date) "
                "VALUES (%s, %s, %s, %s)",
                [(pid, *row) for row in clean],
            )
    except errors.ForeignKeyViolation as exc:
        if exc.diag.table_name != "portfolio_holdings":
            raise
        raise ValueError(
            "ticker tidak terdaftar di emitens: " + (exc.diag.message_detail or "")
        ) from exc
    finally:
        conn.close()
    return len(clean)


def list_holdings(portfolio=None, user_id=None):
    """user_id=None menampilkan semua akun (khusus CLI). API wajib menyebut user_id."""
    where, params = [], []
    if user_id is not None:
        where.append("p.user_id = %s")
        params.append(user_id)
    if portfolio:
        where.append("p.name = %s")
        params.append(portfolio)
    sql = (
        "SELECT h.id, p.name, h.ticker, h.lots, h.buy_date, p.user_id "
        "FROM portfolio_holdings h JOIN portfolios p ON p.id = h.portfolio_id"
    )
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY p.user_id, p.name, h.buy_date, h.id"
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
    finally:
        conn.close()
    df = pd.DataFrame(rows, columns=HOLDING_COLUMNS)
    df["lots"] = df["lots"].astype(float)
    df["buy_date"] = pd.to_datetime(df["buy_date"])
    return df


def list_portfolios(user_id=None):
    sql = "SELECT name FROM portfolios"
    params = ()
    if user_id is not None:
        sql += " WHERE user_id = %s"
        params = (user_id,)
    sql += " ORDER BY name"
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return [row[0] for row in cur.fetchall()]
    finally:
        conn.close()


def list_summaries(user_id):
    """Ringkasan tiap portofolio milik user, termasuk yang baru berisi cash."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT p.name, COUNT(DISTINCT h.ticker), COUNT(h.id), MIN(h.buy_date), p.cash "
                "FROM portfolios p LEFT JOIN portfolio_holdings h ON h.portfolio_id = p.id "
                "WHERE p.user_id = %s GROUP BY p.id, p.name, p.cash ORDER BY p.name",
                (user_id,),
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    return [
        {
            "name": name,
            "tickers": int(tickers),
            "positions": int(positions),
            "firstBuy": first.isoformat() if first else "",
            "cash": float(cash),
        }
        for name, tickers, positions, first, cash in rows
    ]


def get_cash(portfolio, user_id):
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT cash FROM portfolios WHERE user_id = %s AND name = %s", (user_id, portfolio)
            )
            row = cur.fetchone()
    finally:
        conn.close()
    return float(row[0]) if row else 0.0


def set_cash(portfolio, amount, user_id):
    """Mengganti saldo cash portofolio (bukan menambah)."""
    amount = float(amount)
    if not math.isfinite(amount) or amount < 0:
        raise ValueError("cash tidak boleh negatif")
    if amount > MAX_CASH:
        raise ValueError("nilai cash terlalu besar")
    conn = _connect()
    try:
        with conn, conn.cursor() as cur:
            pid = _portfolio_id(cur, user_id, portfolio)
            cur.execute("UPDATE portfolios SET cash = %s WHERE id = %s", (round(amount, 2), pid))
    finally:
        conn.close()


def remove_holding(holding_id, user_id=None):
    """Dengan user_id, posisi milik akun lain tidak ikut terhapus walau id-nya ditebak."""
    sql = (
        "DELETE FROM portfolio_holdings h USING portfolios p "
        "WHERE p.id = h.portfolio_id AND h.id = %s"
    )
    params = [holding_id]
    if user_id is not None:
        sql += " AND p.user_id = %s"
        params.append(user_id)
    conn = _connect()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.rowcount > 0
    finally:
        conn.close()


def delete_portfolio(portfolio, user_id):
    """Menghapus portofolio beserta seluruh posisi dan riwayat evaluasinya (ON DELETE CASCADE)."""
    conn = _connect()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM portfolios WHERE user_id = %s AND name = %s", (user_id, portfolio)
            )
            return cur.rowcount > 0
    finally:
        conn.close()


def save_evaluation(portfolio, result, user_id):
    sharpe = result["sharpe"]
    conn = _connect()
    try:
        with conn, conn.cursor() as cur:
            pid = _portfolio_id(cur, user_id, portfolio)
            cur.execute(
                """
                INSERT INTO portfolio_evaluations
                    (portfolio_id, as_of, avg_correlation, sharpe_since_buy,
                     sharpe_current_weights, sharpe_equal_weight, warnings, result)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (portfolio_id, as_of) DO UPDATE SET
                    avg_correlation = EXCLUDED.avg_correlation,
                    sharpe_since_buy = EXCLUDED.sharpe_since_buy,
                    sharpe_current_weights = EXCLUDED.sharpe_current_weights,
                    sharpe_equal_weight = EXCLUDED.sharpe_equal_weight,
                    warnings = EXCLUDED.warnings,
                    result = EXCLUDED.result,
                    computed_at = now();
                """,
                (
                    pid,
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


def list_evaluations(portfolio, limit, user_id):
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT e.as_of, e.avg_correlation, e.sharpe_since_buy, e.sharpe_current_weights, "
                "e.sharpe_equal_weight, e.warnings "
                "FROM portfolio_evaluations e JOIN portfolios p ON p.id = e.portfolio_id "
                "WHERE p.user_id = %s AND p.name = %s ORDER BY e.as_of DESC LIMIT %s",
                (user_id, portfolio, limit),
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    df = pd.DataFrame(rows, columns=EVALUATION_COLUMNS)
    for column in EVALUATION_COLUMNS[1:5]:
        df[column] = df[column].astype(float)
    return df