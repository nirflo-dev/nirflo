import argparse
import io
import logging
import os
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path

import cloudscraper
import pandas as pd
import psycopg2
import yfinance as yf
from psycopg2.extras import execute_values

from pipeline import arelle_loader as loader
from pipeline.idx_browser import ChallengeError, IdxBrowser

from analytics.growth_scanner import (
    build_diagnostic_report,
    get_unscored_tickers,
    print_diagnostic_report,
    save_scan_results,
)
from analytics.signal_report import report_signal_performance

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

config = loader.config

DATA_DIR = Path(os.getenv("PEAD_DATA_DIR", Path(__file__).resolve().parent / "data"))
XBRL_DIR = DATA_DIR / "XBRL"

REQUEST_TIMEOUT = 30
PRICE_UPDATE_LOOKBACK_DAYS = 10
PRICE_BATCH_SIZE = 50
PRICE_BATCH_PAUSE_SECONDS = 1.0

MAX_WORKERS = 6

RECHECK_DAYS_ACTIVE = 1
RECHECK_DAYS_STALE = 7
ACTIVE_WINDOW_DAYS = {"FY": 120, "Q1": 60, "Q2": 60, "Q3": 60}
PERIOD_END_MONTH_DAY = {"FY": (12, 31), "Q1": (3, 31), "Q2": (6, 30), "Q3": (9, 30)}
PERIOD_FOLDERS = {"Q1": "TW1", "Q2": "TW2", "Q3": "TW3", "FY": "Audit"}

IDX_URL_TEMPLATE = (
    "https://www.idx.co.id/Portals/0/StaticData/ListedCompanies/Corporate_Actions/"
    "New_Info_JSX/Jenis_Informasi/01_Laporan_Keuangan/02_Soft_Copy_Laporan_Keuangan/"
    "/Laporan%20Keuangan%20Tahun%20{year}/{period_folder}/{ticker}/instance.zip"
)

BROWSER_BATCH_SIZE = 12
BROWSER_BATCH_PAUSE_SECONDS = 1.0
SCRAPER_BLOCK_ABORT = 20

FETCH_MODE = "auto"

PRICE_UPSERT_SQL = """
    INSERT INTO daily_prices
        (ticker, price_date, open_price, high_price, low_price,
         close_price, adj_close_price, volume, is_no_trade)
    VALUES %s
    ON CONFLICT (ticker, price_date)
    DO UPDATE SET
        open_price = EXCLUDED.open_price,
        high_price = EXCLUDED.high_price,
        low_price = EXCLUDED.low_price,
        close_price = EXCLUDED.close_price,
        adj_close_price = EXCLUDED.adj_close_price,
        volume = EXCLUDED.volume,
        is_no_trade = EXCLUDED.is_no_trade;
"""

_thread_local = threading.local()


BlockedError = ChallengeError


def get_check_targets(latest: int | None = None) -> list[tuple[int, str, str]]:
    current_year = datetime.now().year

    if latest is None:
        return [
            (current_year - 1, "Audit", "FY"),
            (current_year, "TW1", "Q1"),
            (current_year, "TW2", "Q2"),
            (current_year, "TW3", "Q3"),
        ]

    today = date.today()
    ended = []
    for year in (current_year - 1, current_year):
        for tag, folder in PERIOD_FOLDERS.items():
            month, day = PERIOD_END_MONTH_DAY[tag]
            period_end = date(year, month, day)
            if period_end <= today:
                ended.append((period_end, year, folder, tag))
    ended.sort()
    return [(year, folder, tag) for _, year, folder, tag in ended[-latest:]]


def ensure_check_log_table() -> None:
    conn = psycopg2.connect(**config)
    cur = conn.cursor()
    try:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS filing_check_log (
                ticker       text NOT NULL,
                year         integer NOT NULL,
                period       text NOT NULL,
                last_checked date NOT NULL DEFAULT CURRENT_DATE,
                check_count  integer NOT NULL DEFAULT 1,
                PRIMARY KEY (ticker, year, period)
            );
            """
        )
        conn.commit()
    finally:
        cur.close()
        conn.close()


def get_already_processed(year: int, period: str) -> set:
    conn = psycopg2.connect(**config)
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT ticker FROM financial_reports WHERE year = %s AND period = %s;",
            (year, period),
        )
        return {row[0] for row in cur.fetchall()}
    finally:
        cur.close()
        conn.close()


def get_last_checked(year: int, period: str) -> dict:
    conn = psycopg2.connect(**config)
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT ticker, last_checked FROM filing_check_log WHERE year = %s AND period = %s;",
            (year, period),
        )
        return {row[0]: row[1] for row in cur.fetchall()}
    finally:
        cur.close()
        conn.close()


def record_missing(year: int, period: str, tickers: list[str]) -> None:
    if not tickers:
        return
    conn = psycopg2.connect(**config)
    cur = conn.cursor()
    try:
        cur.executemany(
            """
            INSERT INTO filing_check_log (ticker, year, period)
            VALUES (%s, %s, %s)
            ON CONFLICT (ticker, year, period)
            DO UPDATE SET last_checked = CURRENT_DATE,
                          check_count = filing_check_log.check_count + 1;
            """,
            [(t, year, period) for t in tickers],
        )
        conn.commit()
    except Exception:
        logger.exception("Failed to record missing filings")
        conn.rollback()
    finally:
        cur.close()
        conn.close()


def recheck_interval_days(year: int, period_tag: str) -> int:
    month, day = PERIOD_END_MONTH_DAY[period_tag]
    period_end = date(year, month, day)
    age_days = (date.today() - period_end).days
    if age_days <= ACTIVE_WINDOW_DAYS[period_tag]:
        return RECHECK_DAYS_ACTIVE
    return RECHECK_DAYS_STALE


def ensure_signal_log_columns() -> None:
    conn = psycopg2.connect(**config)
    cur = conn.cursor()
    try:
        cur.execute("ALTER TABLE signal_log ADD COLUMN IF NOT EXISTS sur numeric;")
        cur.execute("ALTER TABLE signal_log ADD COLUMN IF NOT EXISTS trigger_source text;")
        cur.execute("ALTER TABLE signal_log ADD COLUMN IF NOT EXISTS direction text;")
        cur.execute("ALTER TABLE signal_log ALTER COLUMN z_score DROP NOT NULL;")
        conn.commit()
    except Exception:
        logger.exception("Failed to prepare signal_log columns")
        conn.rollback()
    finally:
        cur.close()
        conn.close()


def log_signals(report_rows: list[dict], year: int, period: str) -> None:
    conn = psycopg2.connect(**config)
    cur = conn.cursor()
    try:
        logged_count = 0
        for r in report_rows:
            if r["status"] not in ("STRONG", "MODERATE"):
                continue
            cur.execute(
                """
                INSERT INTO signal_log
                    (ticker, year, period, z_score, status, confirmation,
                     sur, trigger_source, direction)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (ticker, year, period) DO NOTHING;
                """,
                (
                    r["ticker"], year, period, r["z_score"], r["status"], r["confirmation"],
                    r["sur"], r["trigger_source"], r["direction"],
                ),
            )
            logged_count += cur.rowcount
        conn.commit()
        logger.info("Logged %d new signal(s) to signal_log for %s %s", logged_count, period, year)
    except Exception:
        logger.exception("Failed to log signals")
        conn.rollback()
    finally:
        cur.close()
        conn.close()


def build_filing_url(year: int, period_folder: str, ticker: str) -> str:
    return IDX_URL_TEMPLATE.format(year=year, period_folder=period_folder, ticker=ticker)


def extract_xbrl_bytes(content: bytes) -> bytes | None:
    with zipfile.ZipFile(io.BytesIO(content)) as z:
        names = z.namelist()
        xbrl_name = next((n for n in names if n.lower().endswith(".xbrl")), None)
        if xbrl_name is None:
            xbrl_name = next((n for n in names if n.lower().endswith(".xml")), None)
        return z.read(xbrl_name) if xbrl_name else None


def classify_response(status: int, content: bytes | None) -> str:
    if status == 200 and content is not None and content[:2] == b"PK":
        return "ok"
    if status == 404:
        return "missing"
    if status in (200, 403, 429, 503):
        return "blocked"
    return "error"


def get_scraper():
    if not hasattr(_thread_local, "scraper"):
        _thread_local.scraper = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "mobile": False}
        )
    return _thread_local.scraper


def download_task(year: int, period_folder: str, ticker: str) -> tuple[str, bytes | None]:
    url = build_filing_url(year, period_folder, ticker)

    try:
        response = get_scraper().get(url, timeout=REQUEST_TIMEOUT)
        kind = classify_response(response.status_code, response.content)
        if kind != "ok":
            return kind, None

        xbrl_bytes = extract_xbrl_bytes(response.content)
        if xbrl_bytes is None:
            logger.warning("%s: zip archive contained no XBRL file", ticker)
            return "error", None
        return "ok", xbrl_bytes
    except zipfile.BadZipFile:
        logger.warning("%s: invalid zip archive", ticker)
        return "error", None
    except Exception:
        logger.exception("Download failed for %s", ticker)
        return "error", None


def iter_scraper_downloads(year: int, period_folder: str, to_check: list):
    blocked = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {
            pool.submit(download_task, year, period_folder, ticker): (ticker, tax_type)
            for ticker, tax_type in to_check
        }
        for future in as_completed(futures):
            ticker, tax_type = futures[future]
            status, xbrl_bytes = future.result()
            yield ticker, tax_type, status, xbrl_bytes

            if status == "blocked":
                blocked += 1
                if blocked >= SCRAPER_BLOCK_ABORT:
                    for pending in futures:
                        pending.cancel()
                    raise BlockedError(f"{blocked} blocked responses in this period")


def iter_browser_downloads(session: IdxBrowser, year: int, period_folder: str, to_check: list):
    session.ensure_open()

    for start in range(0, len(to_check), BROWSER_BATCH_SIZE):
        batch = to_check[start: start + BROWSER_BATCH_SIZE]
        urls = [build_filing_url(year, period_folder, ticker) for ticker, _ in batch]
        responses = session.fetch_with_verification(urls)

        exhausted = False
        for (ticker, tax_type), (status, content) in zip(batch, responses):
            kind = classify_response(status, content)
            if kind == "ok":
                try:
                    xbrl_bytes = extract_xbrl_bytes(content)
                except zipfile.BadZipFile:
                    xbrl_bytes = None
                if xbrl_bytes:
                    yield ticker, tax_type, "ok", xbrl_bytes
                else:
                    yield ticker, tax_type, "error", None
                continue
            if kind == "blocked":
                exhausted = True
            yield ticker, tax_type, kind, None

        if exhausted:
            raise BlockedError("Repeated blocking after verification attempts")

        logger.info(
            "Browser fetch progress: %d/%d tickers",
            min(start + BROWSER_BATCH_SIZE, len(to_check)), len(to_check),
        )
        time.sleep(BROWSER_BATCH_PAUSE_SECONDS)


def iter_auto_downloads(session: IdxBrowser, year: int, period_folder: str, to_check: list):
    resolved = set()

    if not session.scraper_blocked:
        try:
            for item in iter_scraper_downloads(year, period_folder, to_check):
                if item[2] == "blocked":
                    continue
                resolved.add(item[0])
                yield item
        except BlockedError as exc:
            logger.warning("cloudscraper blocked (%s), switching to browser for this run", exc)
            session.scraper_blocked = True

    leftover = [(t, tax) for t, tax in to_check if t not in resolved]
    if not leftover:
        return

    if not session.scraper_blocked:
        logger.info("%d ticker(s) blocked on cloudscraper, retrying through browser", len(leftover))
        session.scraper_blocked = True

    logger.info("Browser fetch: %d ticker(s) to request", len(leftover))
    yield from iter_browser_downloads(session, year, period_folder, leftover)


def ingest_filing(
    save_conn,
    ticker: str,
    tax_type: str,
    year: int,
    period_tag: str,
    xbrl_bytes: bytes,
    target_dir: Path,
    stats: dict,
) -> bool:
    try:
        file_path = target_dir / f"{ticker}_{year}_{period_tag}.xbrl"
        if not file_path.exists() or file_path.read_bytes() != xbrl_bytes:
            file_path.write_bytes(xbrl_bytes)

        mappings = loader.get_taxonomy_map(tax_type)
        golden_metrics, revenue_tag_source = loader.parse_filing(
            file_path, ticker, year, period_tag, mappings
        )

        if "revenue" not in golden_metrics:
            logger.warning(
                "%s [%s %s]: revenue not found (taxonomy_type=%s)",
                ticker, period_tag, year, tax_type,
            )

        saved_ok = loader.save_to_db(
            save_conn, ticker, year, period_tag, golden_metrics, revenue_tag_source
        )

        if saved_ok:
            logger.info(
                "Processed %s [%s %s]: revenue_tag_source=%s",
                ticker, period_tag, year, revenue_tag_source,
            )
            return True

        logger.error("%s parsed but NOT saved, excluded from this run's results", ticker)
        stats["save_failures"] += 1
        return False

    except loader.FxRateError as exc:
        logger.error("%s [%s %s]: not saved, %s", ticker, period_tag, year, exc)
        stats["fx_failures"] += 1
        return False

    except Exception:
        logger.exception("Failed to process %s", ticker)
        stats["parse_errors"] += 1
        return False


def fetch_and_process_active_period(
    year: int,
    period_folder: str,
    period_tag: str,
    fetch_mode: str,
    session: IdxBrowser | None,
    force_recheck: bool,
) -> list[str]:
    already_processed = get_already_processed(year, period_tag)
    last_checked = get_last_checked(year, period_tag)

    conn = psycopg2.connect(**config)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT w.ticker, e.taxonomy_type
        FROM watchlist w
        JOIN emitens e ON w.ticker = e.ticker
        WHERE e.is_active = TRUE;
        """
    )
    all_targets = cur.fetchall()
    cur.close()
    conn.close()

    pending = [(t, tax) for t, tax in all_targets if t not in already_processed]

    target_dir = XBRL_DIR / str(year) / period_tag

    from_disk = []
    remaining = []
    for ticker, tax_type in pending:
        if (target_dir / f"{ticker}_{year}_{period_tag}.xbrl").exists():
            from_disk.append((ticker, tax_type))
        else:
            remaining.append((ticker, tax_type))

    interval = recheck_interval_days(year, period_tag)
    today = date.today()
    to_check = []
    skipped_recent = 0
    for ticker, tax_type in remaining:
        checked_on = last_checked.get(ticker)
        if not force_recheck and checked_on is not None and (today - checked_on).days < interval:
            skipped_recent += 1
            continue
        to_check.append((ticker, tax_type))

    logger.info(
        "year=%s period=%s: %d total tickers, %d already processed, %d pending "
        "(%d on disk, %d skipped as checked within %d day(s), %d to request) [fetch_mode=%s%s]",
        year, period_tag, len(all_targets), len(already_processed), len(pending),
        len(from_disk), skipped_recent, interval, len(to_check), fetch_mode,
        ", force_recheck" if force_recheck else "",
    )

    newly_saved_tickers = []

    if not from_disk and not to_check:
        return newly_saved_tickers

    still_missing = []
    blocked_count = 0
    download_errors = 0
    stats = {"parse_errors": 0, "save_failures": 0, "fx_failures": 0}

    target_dir.mkdir(parents=True, exist_ok=True)

    save_conn = psycopg2.connect(**config)
    try:
        for ticker, tax_type in from_disk:
            file_path = target_dir / f"{ticker}_{year}_{period_tag}.xbrl"
            if ingest_filing(save_conn, ticker, tax_type, year, period_tag,
                             file_path.read_bytes(), target_dir, stats):
                newly_saved_tickers.append(ticker)

        if to_check:
            if fetch_mode == "browser":
                downloads = iter_browser_downloads(session, year, period_folder, to_check)
            elif fetch_mode == "auto":
                downloads = iter_auto_downloads(session, year, period_folder, to_check)
            else:
                downloads = iter_scraper_downloads(year, period_folder, to_check)

            try:
                for ticker, tax_type, status, xbrl_bytes in downloads:
                    if status == "missing":
                        still_missing.append(ticker)
                    elif status == "blocked":
                        blocked_count += 1
                    elif status == "error":
                        download_errors += 1
                    elif ingest_filing(save_conn, ticker, tax_type, year, period_tag,
                                       xbrl_bytes, target_dir, stats):
                        newly_saved_tickers.append(ticker)
            except BlockedError as exc:
                logger.error(
                    "[%s %s] fetching stopped, %s. Unfetched tickers are not "
                    "recorded as missing and will be retried next run.",
                    period_tag, year, exc,
                )
    finally:
        save_conn.close()

    record_missing(year, period_tag, still_missing)

    logger.info(
        "year=%s period=%s complete: %d newly saved, %d not released (404), %d blocked, "
        "%d download errors, %d parse errors, %d save failures, %d fx failures",
        year, period_tag, len(newly_saved_tickers), len(still_missing), blocked_count,
        download_errors, stats["parse_errors"], stats["save_failures"], stats["fx_failures"],
    )

    return newly_saved_tickers


def get_tickers_with_data(year: int, period: str) -> list[str]:
    conn = psycopg2.connect(**config)
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT fr.ticker
            FROM financial_reports fr
            JOIN emitens e ON fr.ticker = e.ticker
            WHERE fr.year = %s AND fr.period = %s AND e.is_active = TRUE
            ORDER BY fr.ticker;
            """,
            (year, period),
        )
        return [row[0] for row in cur.fetchall()]
    finally:
        cur.close()
        conn.close()


def get_active_tickers() -> list[str]:
    conn = psycopg2.connect(**config)
    cur = conn.cursor()
    try:
        cur.execute("SELECT ticker FROM emitens WHERE is_active = TRUE ORDER BY ticker;")
        return [row[0] for row in cur.fetchall()]
    finally:
        cur.close()
        conn.close()


def load_trading_days(cur) -> set:
    cur.execute("SELECT calendar_date FROM trading_calendar WHERE is_trading_day = TRUE;")
    trading_days = {row[0] for row in cur.fetchall()}
    if not trading_days:
        raise RuntimeError("trading_calendar is empty, build the trading calendar first")
    return trading_days


def download_price_batch(tickers: list[str], start_date: date) -> pd.DataFrame:
    return yf.download(
        [f"{t}.JK" for t in tickers],
        start=start_date,
        interval="1d",
        auto_adjust=False,
        group_by="ticker",
        progress=False,
        threads=True,
    )


def extract_ticker_frame(data: pd.DataFrame, symbol: str) -> pd.DataFrame | None:
    if data is None or data.empty:
        return None

    if isinstance(data.columns, pd.MultiIndex):
        if symbol in set(data.columns.get_level_values(0)):
            frame = data[symbol]
        elif symbol in set(data.columns.get_level_values(1)):
            frame = data.xs(symbol, axis=1, level=1)
        else:
            return None
    else:
        frame = data

    frame = frame.dropna(how="all")
    if frame.empty or "Close" not in frame.columns:
        return None
    return frame[~frame.index.duplicated(keep="last")]


def to_float(value) -> float | None:
    return None if pd.isna(value) else float(value)


def build_price_rows(ticker: str, frame: pd.DataFrame, trading_days: set) -> tuple[list, int, int, int]:
    rows = []
    skipped_calendar = 0
    failed_parse = 0
    flagged_no_trade = 0

    for timestamp, record in frame.iterrows():
        price_date = timestamp.date()
        if price_date not in trading_days:
            skipped_calendar += 1
            continue

        try:
            close_price = to_float(record.get("Close"))
            adj_close_price = to_float(record.get("Adj Close"))
            if close_price is None or adj_close_price is None:
                failed_parse += 1
                continue

            raw_volume = record.get("Volume")
            volume = 0 if pd.isna(raw_volume) else int(raw_volume)
            is_no_trade = volume == 0
            if is_no_trade:
                flagged_no_trade += 1

            rows.append((
                ticker, price_date,
                to_float(record.get("Open")), to_float(record.get("High")),
                to_float(record.get("Low")), close_price, adj_close_price,
                volume, is_no_trade,
            ))
        except Exception:
            failed_parse += 1

    return rows, skipped_calendar, failed_parse, flagged_no_trade


def update_prices(tickers: list[str], start_date: date, scope: str) -> None:
    if not tickers:
        logger.info("daily_prices [%s]: no tickers to update", scope)
        return

    total_batches = (len(tickers) + PRICE_BATCH_SIZE - 1) // PRICE_BATCH_SIZE
    logger.info(
        "daily_prices [%s]: %d tickers in %d batch(es), start=%s",
        scope, len(tickers), total_batches, start_date,
    )

    ok_tickers = 0
    empty_tickers = 0
    failed_tickers = 0
    rows_written = 0
    calendar_skipped = 0
    parse_failed = 0
    no_trade_flagged = 0

    conn = psycopg2.connect(**config)
    cur = conn.cursor()
    try:
        trading_days = load_trading_days(cur)

        for batch_number, offset in enumerate(range(0, len(tickers), PRICE_BATCH_SIZE), start=1):
            batch = tickers[offset: offset + PRICE_BATCH_SIZE]

            try:
                data = download_price_batch(batch, start_date)
            except Exception:
                logger.exception("Price batch %d/%d download failed", batch_number, total_batches)
                failed_tickers += len(batch)
                continue

            batch_rows = []
            batch_ok = 0
            batch_empty = []
            batch_skipped = 0
            batch_bad = 0
            batch_flagged = 0

            for ticker in batch:
                frame = extract_ticker_frame(data, f"{ticker}.JK")
                if frame is None:
                    batch_empty.append(ticker)
                    continue

                rows, skipped, bad, flagged = build_price_rows(ticker, frame, trading_days)
                batch_rows.extend(rows)
                batch_skipped += skipped
                batch_bad += bad
                batch_flagged += flagged
                batch_ok += 1

            if batch_rows:
                try:
                    execute_values(cur, PRICE_UPSERT_SQL, batch_rows, page_size=1000)
                    conn.commit()
                except Exception:
                    logger.exception("Price batch %d/%d insert failed", batch_number, total_batches)
                    conn.rollback()
                    failed_tickers += batch_ok
                    empty_tickers += len(batch_empty)
                    continue

            ok_tickers += batch_ok
            empty_tickers += len(batch_empty)
            rows_written += len(batch_rows)
            calendar_skipped += batch_skipped
            parse_failed += batch_bad
            no_trade_flagged += batch_flagged

            logger.info(
                "Price batch %d/%d: %d ok, %d no data, %d rows written",
                batch_number, total_batches, batch_ok, len(batch_empty), len(batch_rows),
            )
            if batch_empty:
                logger.info("No price data: %s", ", ".join(batch_empty))

            if offset + PRICE_BATCH_SIZE < len(tickers):
                time.sleep(PRICE_BATCH_PAUSE_SECONDS)

        logger.info(
            "daily_prices [%s] summary: ok=%d empty=%d failed=%d total=%d | rows=%d "
            "calendar_skipped=%d no_trade_flagged=%d parse_failed=%d",
            scope, ok_tickers, empty_tickers, failed_tickers, len(tickers), rows_written,
            calendar_skipped, no_trade_flagged, parse_failed,
        )
    except Exception:
        logger.exception("daily_prices update aborted")
    finally:
        cur.close()
        conn.close()


COMMANDS = ("fetch", "report", "prices", "performance")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Nirflo PEAD pipeline. With no command every step runs: fetch new "
        "filings, report and log signals, update prices, report signal performance."
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=COMMANDS,
        default=None,
        help="fetch: ingest new filings, report and log signals. report: print the report "
        "from existing data only, no side effects. prices: update daily_prices only. "
        "performance: print the signal performance report only.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="ignore filing_check_log and request every pending ticker again",
    )
    parser.add_argument(
        "--all-periods",
        action="store_true",
        help="check FY of last year plus Q1-Q3 of this year instead of only the latest period",
    )
    parser.add_argument(
        "--all-rows",
        action="store_true",
        help="with the report command, list every row instead of only STRONG and MODERATE",
    )
    return parser.parse_args()


def run_fetch(targets: list[tuple[int, str, str]], force_recheck: bool) -> dict:
    loader.verify_schema()
    ensure_check_log_table()

    logger.info("Checking: %s", ", ".join(f"{tag} {year}" for year, _, tag in targets))

    session = None
    if FETCH_MODE in ("browser", "auto"):
        session = IdxBrowser()

    results_by_period = {}
    try:
        for year, period_folder, period_tag in targets:
            new_tickers = fetch_and_process_active_period(
                year, period_folder, period_tag, FETCH_MODE, session, force_recheck
            )
            if new_tickers:
                results_by_period[(year, period_tag)] = new_tickers
    finally:
        if session is not None:
            session.close()

    return results_by_period


def process_period_report(
    year: int,
    period_tag: str,
    new_tickers: list[str],
    log_enabled: bool,
    all_rows: bool,
) -> None:
    tickers = get_tickers_with_data(year, period_tag)
    if not tickers:
        logger.info("No data in financial_reports for %s %s", period_tag, year)
        return

    if not log_enabled:
        rows = build_diagnostic_report(year, period_tag, tickers)
        print_diagnostic_report(rows, year, period_tag, flagged_only=not all_rows)
        return

    fresh = set(new_tickers)
    if recheck_interval_days(year, period_tag) == RECHECK_DAYS_ACTIVE:
        fresh.update(get_unscored_tickers(year, period_tag))

    rows = build_diagnostic_report(year, period_tag, tickers)
    save_scan_results(rows, year, period_tag)

    fresh_rows = [r for r in rows if r["ticker"] in fresh]
    if not fresh_rows:
        logger.info("%s %s: no newly ingested or unscored tickers to report", period_tag, year)
        return

    print_diagnostic_report(fresh_rows, year, period_tag)
    log_signals(fresh_rows, year, period_tag)


if __name__ == "__main__":
    args = parse_args()

    command = args.command
    run_all = command is None
    do_fetch = run_all or command == "fetch"
    do_report = run_all or command in ("fetch", "report")
    do_prices = run_all or command == "prices"
    do_performance = run_all or command == "performance"

    targets = get_check_targets(None if args.all_periods else 1)
    logger.info(
        "Command: %s | periods: %s | force: %s | fetch mode: %s",
        command or "all",
        ", ".join(f"{tag} {year}" for year, _, tag in targets),
        args.force, FETCH_MODE,
    )

    results_by_period = {}
    if do_fetch:
        results_by_period = run_fetch(targets, args.force)

    if do_report:
        if do_fetch:
            ensure_signal_log_columns()
        for year, _, period_tag in targets:
            process_period_report(
                year, period_tag,
                results_by_period.get((year, period_tag), []),
                log_enabled=do_fetch,
                all_rows=args.all_rows,
            )

    if do_prices:
        price_start = date.today() - timedelta(days=PRICE_UPDATE_LOOKBACK_DAYS)
        update_prices(get_active_tickers(), price_start, "full")

    if do_performance:
        report_signal_performance()