import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

import duckdb
import pandas as pd
import psycopg2

from core.config import DB_CONFIG

logger = logging.getLogger(__name__)

HOT_YEARS = 2
EARLIEST_DATE = date(2013, 1, 1)
PG_CONNECT_TIMEOUT = 3
DB_CHECK_TTL_SECONDS = 60
STAMP_TTL_SECONDS = 600

LATEST_KEY = "snapshots/latest.json"
SNAPSHOT_KEY = "snapshots/{stamp}/public/{table}.parquet"
SNAPSHOT_TABLES = (
    "emitens",
    "scan_results",
    "signal_log",
    "macro_indicators",
    "macro_dictionary",
    "daily_prices",
    "financial_reports",
    "trading_calendar",
)

PEAD_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = Path(os.getenv("MINTIGO_CACHE_DIR", PEAD_ROOT / "cache"))
IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*$")
SNAPSHOT_FILE = re.compile(r"^(?P<table>[a-z_]+)_\d{8}_\d{6}\.parquet$")


@dataclass(frozen=True)
class TableSpec:
    name: str
    archive_key: str
    partition: str
    is_date: bool
    sort: tuple


TABLES = {
    "daily_prices": TableSpec(
        name="daily_prices",
        archive_key="archive/daily_prices_full_20261024.parquet",
        partition="price_date",
        is_date=True,
        sort=("ticker", "price_date"),
    ),
    "financial_reports": TableSpec(
        name="financial_reports",
        archive_key="archive/financial_reports_full_20261024.parquet",
        partition="year",
        is_date=False,
        sort=("ticker", "year", "period"),
    ),
}

_state_lock = threading.Lock()
_download_lock = threading.RLock()
_db_state = {"checked": None, "ok": False}
_stamp_state = {"checked": None, "value": None}
_sql_state = {"stamp": None, "con": None}


# ---------------------------------------------------------------------------
# Database availability
# ---------------------------------------------------------------------------

def _probe_database() -> bool:
    try:
        conn = psycopg2.connect(**DB_CONFIG, connect_timeout=PG_CONNECT_TIMEOUT)
    except psycopg2.OperationalError as exc:
        logger.warning("Database tidak terjangkau: %s", str(exc).strip())
        return False
    conn.close()
    return True


def database_available() -> bool:
    now = time.monotonic()
    with _state_lock:
        checked = _db_state["checked"]
        if checked is not None and now - checked < DB_CHECK_TTL_SECONDS:
            return _db_state["ok"]
    ok = _probe_database()
    with _state_lock:
        _db_state["checked"] = time.monotonic()
        _db_state["ok"] = ok
    return ok


def _mark_database_down() -> None:
    with _state_lock:
        _db_state["checked"] = time.monotonic()
        _db_state["ok"] = False


def reset_caches() -> None:
    """Paksa pengecekan ulang database dan snapshot terbaru. File cache di disk tidak dihapus."""
    with _state_lock:
        _db_state["checked"] = None
        _stamp_state["checked"] = None


# ---------------------------------------------------------------------------
# R2 and local cache
# ---------------------------------------------------------------------------

def _r2_settings() -> dict:
    match = re.search(r"[0-9a-f]{32}", os.getenv("R2_ACCOUNT_ID", ""))
    settings = {
        "account_id": match.group(0) if match else None,
        "key_id": os.getenv("R2_READ_ACCESS_KEY_ID") or os.getenv("R2_ACCESS_KEY_ID"),
        "secret": os.getenv("R2_READ_SECRET_ACCESS_KEY") or os.getenv("R2_SECRET_ACCESS_KEY"),
        "bucket": os.getenv("R2_BUCKET"),
    }
    missing = [name for name, value in settings.items() if not value]
    if missing:
        raise RuntimeError("Konfigurasi R2 belum lengkap: " + ", ".join(missing))
    return settings


def _r2_connection() -> duckdb.DuckDBPyConnection:
    settings = _r2_settings()
    con = duckdb.connect()
    con.execute("INSTALL httpfs")
    con.execute("LOAD httpfs")
    con.execute(
        f"CREATE OR REPLACE SECRET r2_secret (TYPE r2, KEY_ID '{settings['key_id']}', "
        f"SECRET '{settings['secret']}', ACCOUNT_ID '{settings['account_id']}')"
    )
    return con


def _cached_copy(key: str, filename: str, order=None) -> Path:
    """Unduh objek R2 satu kali ke disk lokal. Jika order diberikan, file diurutkan agar DuckDB bisa melewati row group."""
    path = CACHE_DIR / filename
    if path.exists():
        return path

    with _download_lock:
        if path.exists():
            return path
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".part")
        order_sql = f" ORDER BY {', '.join(order)}" if order else ""
        bucket = _r2_settings()["bucket"]
        logger.info("Mengunduh dari R2: %s", key)
        con = _r2_connection()
        try:
            con.execute(
                f"COPY (SELECT * FROM read_parquet('r2://{bucket}/{key}'){order_sql}) "
                f"TO '{tmp.as_posix()}' (FORMAT parquet, COMPRESSION zstd, ROW_GROUP_SIZE 100000)"
            )
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
        finally:
            con.close()
        tmp.replace(path)
    return path


def latest_snapshot_stamp() -> str:
    now = time.monotonic()
    with _state_lock:
        checked = _stamp_state["checked"]
        if _stamp_state["value"] and checked is not None and now - checked < STAMP_TTL_SECONDS:
            return _stamp_state["value"]

    bucket = _r2_settings()["bucket"]
    con = _r2_connection()
    try:
        row = con.execute(f"SELECT stamp FROM read_json_auto('r2://{bucket}/{LATEST_KEY}')").fetchone()
    finally:
        con.close()
    if not row:
        raise RuntimeError("latest.json kosong atau tidak ditemukan")

    with _state_lock:
        _stamp_state["checked"] = time.monotonic()
        _stamp_state["value"] = row[0]
    return row[0]


def _snapshot_path(table: str) -> Path:
    stamp = latest_snapshot_stamp()
    spec = TABLES.get(table)
    path = _cached_copy(
        SNAPSHOT_KEY.format(stamp=stamp, table=table),
        f"{table}_{stamp}.parquet",
        spec.sort if spec else None,
    )
    for old in CACHE_DIR.glob(f"{table}_*.parquet"):
        match = SNAPSHOT_FILE.match(old.name)
        if match and match.group("table") == table and old != path:
            try:
                old.unlink()
            except OSError:
                pass
    return path


def _cold_path(spec: TableSpec) -> Path:
    try:
        return _cached_copy(spec.archive_key, Path(spec.archive_key).name, spec.sort)
    except Exception as exc:
        logger.warning("Arsip %s tidak tersedia (%s), memakai snapshot terbaru", spec.archive_key, exc)
        return _snapshot_path(spec.name)


@lru_cache(maxsize=1)
def _local_con() -> duckdb.DuckDBPyConnection:
    return duckdb.connect()


# ---------------------------------------------------------------------------
# Hot and cold boundaries
# ---------------------------------------------------------------------------

def _hot_boundary(spec: TableSpec):
    year = date.today().year - HOT_YEARS + 1
    return date(year, 1, 1) if spec.is_date else year


def _step(spec: TableSpec, value, n: int):
    return value + timedelta(days=n) if spec.is_date else value + n


def _default_bounds(spec: TableSpec):
    if spec.is_date:
        return EARLIEST_DATE, date.today()
    return 0, date.today().year


def _coerce(spec: TableSpec, value):
    if spec.is_date:
        return value.date() if isinstance(value, datetime) else value
    return int(value)


def _check_identifiers(names) -> None:
    for name in names:
        if not IDENTIFIER.match(name):
            raise ValueError(f"nama kolom tidak valid: {name}")


def _select_list(spec: TableSpec, columns) -> str:
    if not columns:
        return "*"
    selected = list(dict.fromkeys([*spec.sort, *columns]))
    _check_identifiers(selected)
    return ", ".join(selected)


# ---------------------------------------------------------------------------
# Readers
# ---------------------------------------------------------------------------

def _read_parquet(spec, path: Path, tickers, lo, hi, columns, filters) -> pd.DataFrame:
    sql = (
        f"SELECT {_select_list(spec, columns)} FROM read_parquet(?) "
        f"WHERE {spec.partition} >= ? AND {spec.partition} <= ?"
    )
    params = [path.as_posix(), lo, hi]
    if tickers is not None:
        sql += " AND list_contains(?::VARCHAR[], ticker)"
        params.append(list(tickers))
    for column, value in (filters or {}).items():
        _check_identifiers([column])
        sql += f" AND {column} = ?"
        params.append(value)
    return _local_con().cursor().execute(sql, params).df()


@lru_cache(maxsize=4)
def _archive_last(table: str):
    spec = TABLES[table]
    path = _cold_path(spec)
    value = _local_con().cursor().execute(
        f"SELECT MAX({spec.partition}) FROM read_parquet(?)", [path.as_posix()]
    ).fetchone()[0]
    if value is not None and spec.is_date and hasattr(value, "date"):
        return value.date()
    return value


def _read_postgres(spec, tickers, lo, hi, columns, filters) -> pd.DataFrame:
    sql = (
        f"SELECT {_select_list(spec, columns)} FROM {spec.name} "
        f"WHERE {spec.partition} >= %s AND {spec.partition} <= %s"
    )
    params = [lo, hi]
    if tickers is not None:
        sql += " AND ticker = ANY(%s)"
        params.append(list(tickers))
    for column, value in (filters or {}).items():
        _check_identifiers([column])
        sql += f" AND {column} = %s"
        params.append(value)

    conn = psycopg2.connect(**DB_CONFIG, connect_timeout=PG_CONNECT_TIMEOUT)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            names = [d[0] for d in cur.description]
            return pd.DataFrame(cur.fetchall(), columns=names)
    finally:
        conn.close()


def _read_snapshot(spec, tickers, lo, hi, columns, filters) -> pd.DataFrame:
    path = _snapshot_path(spec.name)
    logger.info("Membaca %s dari snapshot R2 %s", spec.name, latest_snapshot_stamp())
    return _read_parquet(spec, path, tickers, lo, hi, columns, filters)


def _read_recent(spec, tickers, lo, hi, columns, filters) -> pd.DataFrame:
    """Sumber data terbaru: database jika terjangkau, selain itu snapshot R2."""
    if database_available():
        try:
            return _read_postgres(spec, tickers, lo, hi, columns, filters)
        except psycopg2.OperationalError as exc:
            _mark_database_down()
            logger.warning("Koneksi database gagal saat membaca %s: %s", spec.name, str(exc).strip())
    return _read_snapshot(spec, tickers, lo, hi, columns, filters)


def _decimals_to_float(frame: pd.DataFrame) -> pd.DataFrame:
    for column in frame.columns:
        if frame[column].dtype == object:
            sample = frame[column].dropna()
            if not sample.empty and isinstance(sample.iloc[0], Decimal):
                frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame


def _normalize(spec: TableSpec, frame: pd.DataFrame) -> pd.DataFrame:
    frame = _decimals_to_float(frame)
    if spec.is_date and spec.partition in frame.columns:
        frame[spec.partition] = pd.to_datetime(frame[spec.partition])
    return frame


# ---------------------------------------------------------------------------
# Public interface: hot and cold tables
# ---------------------------------------------------------------------------

def load_table(table: str, tickers=None, lo=None, hi=None, columns=None, filters=None) -> pd.DataFrame:
    """
    Cold (partisi di bawah batas hot): parquet lokal via DuckDB, diunduh dari R2 sekali jika belum ada.
    Hot  (partisi pada atau setelah batas hot): database, cadangan snapshot R2.
    Kolom pemisah: price_date (date) untuk daily_prices, year (int) untuk financial_reports.
    Kolom ticker dan kolom urutan tabel selalu ikut terpilih.
    """
    spec = TABLES[table]
    default_lo, default_hi = _default_bounds(spec)
    lo = default_lo if lo is None else _coerce(spec, lo)
    hi = default_hi if hi is None else _coerce(spec, hi)
    boundary = _hot_boundary(spec)
    parts = []

    if lo < boundary:
        cold_hi = min(hi, _step(spec, boundary, -1))
        cold = _read_parquet(spec, _cold_path(spec), tickers, lo, cold_hi, columns, filters)
        parts.append(_normalize(spec, cold))

        archive_last = _archive_last(table)
        if archive_last is None or archive_last < cold_hi:
            gap_lo = lo if archive_last is None else max(lo, _step(spec, archive_last, 1))
            if gap_lo <= cold_hi:
                logger.warning(
                    "Arsip %s hanya sampai %s, periode %s sampai %s dibaca dari sumber terbaru. Buat arsip baru.",
                    table, archive_last, gap_lo, cold_hi,
                )
                gap = _read_recent(spec, tickers, gap_lo, cold_hi, columns, filters)
                parts.append(_normalize(spec, gap))

    if hi >= boundary:
        hot = _read_recent(spec, tickers, max(lo, boundary), hi, columns, filters)
        parts.append(_normalize(spec, hot))

    filled = [p for p in parts if not p.empty]
    if not filled:
        return parts[0] if parts else pd.DataFrame()
    frame = pd.concat(filled, ignore_index=True)
    return frame.sort_values(list(spec.sort)).reset_index(drop=True)


def load_prices(tickers=None, start=None, end=None, columns=None) -> pd.DataFrame:
    return load_table("daily_prices", tickers, start, end, columns)


def load_financial_reports(tickers=None, year_from=None, year_to=None, columns=None, period=None) -> pd.DataFrame:
    filters = {"period": period} if period else None
    return load_table("financial_reports", tickers, year_from, year_to, columns, filters)


# ---------------------------------------------------------------------------
# Public interface: SQL for the dashboard (database, or snapshot without a database)
# ---------------------------------------------------------------------------

def _snapshot_con() -> duckdb.DuckDBPyConnection:
    stamp = latest_snapshot_stamp()
    with _download_lock:
        if _sql_state["stamp"] == stamp and _sql_state["con"] is not None:
            return _sql_state["con"]

        con = duckdb.connect()
        for table in SNAPSHOT_TABLES:
            try:
                path = _snapshot_path(table)
            except Exception as exc:
                logger.warning("Tabel %s tidak tersedia di snapshot: %s", table, exc)
                continue
            con.execute(f"CREATE VIEW {table} AS SELECT * FROM read_parquet('{path.as_posix()}')")

        previous = _sql_state["con"]
        _sql_state["stamp"] = stamp
        _sql_state["con"] = con
        if previous is not None:
            previous.close()
        logger.info("Mode tanpa database: snapshot R2 %s", stamp)
        return con


def _snapshot_query(sql: str, params=None) -> pd.DataFrame:
    translated = sql.replace("%s", "?").replace("'public'", "'main'")
    cursor = _snapshot_con().cursor()
    return cursor.execute(translated, list(params) if params else []).df()


def run_query(sql: str, params=None) -> pd.DataFrame:
    """Jalankan SQL gaya Postgres. Tanpa database, SQL yang sama dijalankan di DuckDB atas snapshot R2."""
    if database_available():
        try:
            conn = psycopg2.connect(**DB_CONFIG, connect_timeout=PG_CONNECT_TIMEOUT)
        except psycopg2.OperationalError as exc:
            _mark_database_down()
            logger.warning("Koneksi database gagal: %s", str(exc).strip())
        else:
            try:
                with conn.cursor() as cur:
                    cur.execute(sql, params)
                    names = [d[0] for d in cur.description]
                    return pd.DataFrame(cur.fetchall(), columns=names)
            finally:
                conn.close()
    return _snapshot_query(sql, params)