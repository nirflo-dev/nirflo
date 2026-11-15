import os
import re
import sys
from dataclasses import dataclass, field

import duckdb
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_CANDIDATES = [
    os.path.join(BASE_DIR, ".env"),
    os.path.join(os.path.dirname(BASE_DIR), ".env"),
    os.path.join(os.path.dirname(os.path.dirname(BASE_DIR)), ".env"),
]


@dataclass(frozen=True)
class R2Config:
    # Lokasi di bucket
    snapshot_prefix: str = "snapshots/"
    latest_key: str = "snapshots/latest.json"
    archive_prices_key: str = "archive/daily_prices_full_20261024.parquet"

    # Batas pemisah data harga: sebelum batas dibaca dari arsip,
    # mulai batas dibaca dari snapshot terbaru. Harus sama dengan batas DELETE di Postgres.
    prices_cutoff: str = "2025-01-01"

    # Nama tabel yang boleh dibaca (schema public)
    tables: tuple = field(
        default=(
            "daily_prices",
            "financial_reports",
            "macro_indicators",
            "emitens",
            "scan_results",
            "signal_log",
            "trading_calendar",
        )
    )


CONFIG = R2Config()


def _load_env():
    for path in ENV_CANDIDATES:
        if os.path.exists(path):
            load_dotenv(path)
            return
    sys.exit("file .env tidak ditemukan di: " + " atau ".join(ENV_CANDIDATES))


def _credentials():
    required = ["R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET"]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        sys.exit("variabel R2 belum ada di .env: " + ", ".join(missing))
    match = re.search(r"[0-9a-f]{32}", os.environ["R2_ACCOUNT_ID"])
    if not match:
        sys.exit("R2_ACCOUNT_ID tidak berisi Account ID 32 karakter yang valid")
    return {
        "account_id": match.group(0),
        "key_id": os.environ["R2_ACCESS_KEY_ID"],
        "secret": os.environ["R2_SECRET_ACCESS_KEY"],
        "bucket": os.environ["R2_BUCKET"],
    }


def connect():
    """Mengembalikan (koneksi DuckDB yang sudah terhubung ke R2, nama bucket)."""
    _load_env()
    cred = _credentials()
    con = duckdb.connect()
    con.execute("INSTALL httpfs")
    con.execute("LOAD httpfs")
    con.execute(
        f"""
        CREATE OR REPLACE SECRET r2_secret (
            TYPE r2,
            KEY_ID '{cred["key_id"]}',
            SECRET '{cred["secret"]}',
            ACCOUNT_ID '{cred["account_id"]}'
        )
        """
    )
    return con, cred["bucket"]


def latest_stamp(con, bucket, cfg=CONFIG):
    """Membaca snapshots/latest.json untuk mengetahui snapshot lengkap terbaru."""
    row = con.execute(
        f"SELECT stamp FROM read_json_auto('r2://{bucket}/{cfg.latest_key}')"
    ).fetchone()
    if not row:
        raise RuntimeError("latest.json kosong atau tidak ditemukan")
    return row[0]


def table_uri(bucket, stamp, table, cfg=CONFIG):
    if table not in cfg.tables:
        raise ValueError(f"tabel tidak terdaftar di konfigurasi: {table}")
    return f"r2://{bucket}/{cfg.snapshot_prefix}{stamp}/public/{table}.parquet"


def read_table(con, bucket, table, stamp=None, cfg=CONFIG):
    """DataFrame dari satu tabel pada snapshot terbaru (atau snapshot tertentu)."""
    stamp = stamp or latest_stamp(con, bucket, cfg)
    uri = table_uri(bucket, stamp, table, cfg)
    return con.execute(f"SELECT * FROM read_parquet('{uri}')").df()


def load_prices(con, bucket, tickers=None, start=None, end=None, stamp=None, cfg=CONFIG):
    """
    Harga harian gabungan: arsip (price_date < cutoff) + snapshot (price_date >= cutoff).
    Filter ketat di kedua sisi sehingga tidak ada baris ganda walau snapshot masih memuat data lama.
    """
    stamp = stamp or latest_stamp(con, bucket, cfg)
    archive_uri = f"r2://{bucket}/{cfg.archive_prices_key}"
    snapshot_uri = table_uri(bucket, stamp, "daily_prices", cfg)

    conditions = []
    params = []
    if tickers:
        placeholders = ", ".join("?" for _ in tickers)
        conditions.append(f"ticker IN ({placeholders})")
        params.extend(tickers)
    if start:
        conditions.append("price_date >= CAST(? AS DATE)")
        params.append(start)
    if end:
        conditions.append("price_date <= CAST(? AS DATE)")
        params.append(end)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    query = f"""
        SELECT * FROM (
            SELECT * FROM read_parquet('{archive_uri}')
            WHERE price_date < DATE '{cfg.prices_cutoff}'
            UNION ALL BY NAME
            SELECT * FROM read_parquet('{snapshot_uri}')
            WHERE price_date >= DATE '{cfg.prices_cutoff}'
        )
        {where}
        ORDER BY ticker, price_date
    """
    return con.execute(query, params).df()