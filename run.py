import io
import logging
import sys
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path

import cloudscraper
import psycopg2

# run.py and arelle_loader_2.py share a folder, so the loader is importable
# directly. The miner directory holds update_prices.
MINER_DIR = Path(__file__).resolve().parent.parent.parent / "miner"
sys.path.append(str(MINER_DIR))

import arelle_loader_2 as loader
from update_prices import update_daily_prices

from growth_scanner import build_diagnostic_report, print_diagnostic_report
from forward_test import open_pending_positions, close_expired_positions, report_performance

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# Database config and XBRL directory come from the loader so both scripts
# always read and write the same locations.
config = loader.config
XBRL_DIR = loader.XBRL_DIR

REQUEST_TIMEOUT = 30
PRICE_UPDATE_LOOKBACK_DAYS = 10  # short window for the daily incremental update


def get_check_targets() -> list[tuple[int, str, str]]:
    current_year = datetime.now().year
    return [
        (current_year - 1, "Audit", "FY"),
        (current_year, "TW1", "Q1"),
        (current_year, "TW2", "Q2"),
        (current_year, "TW3", "Q3"),
    ]


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
                INSERT INTO signal_log (ticker, year, period, z_score, status, confirmation)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (ticker, year, period) DO NOTHING;
                """,
                (r["ticker"], year, period, r["z_score"], r["status"], r["confirmation"]),
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


def download_xbrl(scraper, year: int, period_folder: str, ticker: str):
    """
    Returns the raw XBRL bytes, or None when the filing is not released yet
    or the archive contains no XBRL file.
    """
    direct_url = (
        f"https://www.idx.co.id/Portals/0/StaticData/ListedCompanies/Corporate_Actions/"
        f"New_Info_JSX/Jenis_Informasi/01_Laporan_Keuangan/02_Soft_Copy_Laporan_Keuangan/"
        f"/Laporan%20Keuangan%20Tahun%20{year}/{period_folder}/{ticker}/instance.zip"
    )

    response = scraper.get(direct_url, timeout=REQUEST_TIMEOUT)
    if response.status_code != 200:
        return None

    with zipfile.ZipFile(io.BytesIO(response.content)) as z:
        xbrl_file = next(
            (n for n in z.namelist() if n.endswith(".xbrl") or n.endswith(".xml")),
            None,
        )
        if not xbrl_file:
            logger.warning("%s: zip archive contained no XBRL file", ticker)
            return None
        return z.read(xbrl_file)


def fetch_and_process_active_period(year: int, period_folder: str, period_tag: str) -> list[str]:
    already_processed = get_already_processed(year, period_tag)

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

    logger.info(
        "year=%s period=%s: %d total tickers, %d already processed, %d pending",
        year, period_tag, len(all_targets), len(already_processed), len(pending),
    )

    newly_saved_tickers = []

    if not pending:
        return newly_saved_tickers

    scraper = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "mobile": False}
    )

    still_missing = 0
    parse_errors = 0
    save_failures = 0
    fx_failures = 0

    target_dir = XBRL_DIR / str(year) / period_tag
    target_dir.mkdir(parents=True, exist_ok=True)

    save_conn = psycopg2.connect(**config)
    try:
        for ticker, tax_type in pending:
            try:
                xbrl_bytes = download_xbrl(scraper, year, period_folder, ticker)
                if xbrl_bytes is None:
                    still_missing += 1
                    continue

                file_path = target_dir / f"{ticker}_{year}_{period_tag}.xbrl"
                with open(file_path, "wb") as f:
                    f.write(xbrl_bytes)

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
                    newly_saved_tickers.append(ticker)
                else:
                    logger.error("%s parsed but NOT saved, excluded from this run's results", ticker)
                    save_failures += 1

            except loader.FxRateError as exc:
                logger.error("%s [%s %s]: not saved, %s", ticker, period_tag, year, exc)
                fx_failures += 1

            except Exception:
                logger.exception("Failed to fetch/process %s", ticker)
                parse_errors += 1
    finally:
        save_conn.close()

    logger.info(
        "year=%s period=%s complete: %d newly saved, %d still not released, "
        "%d parse errors, %d save failures, %d fx failures",
        year, period_tag, len(newly_saved_tickers), still_missing,
        parse_errors, save_failures, fx_failures,
    )

    return newly_saved_tickers


def run() -> dict:
    loader.verify_schema()

    targets = get_check_targets()
    results_by_period = {}

    for year, period_folder, period_tag in targets:
        new_tickers = fetch_and_process_active_period(year, period_folder, period_tag)
        if new_tickers:
            results_by_period[(year, period_tag)] = new_tickers

    return results_by_period


if __name__ == "__main__":
    results_by_period = run()

    if not results_by_period:
        logger.info("No new tickers ingested this run, nothing to report")
    else:
        for (year, period_tag), new_tickers in results_by_period.items():
            report_rows = build_diagnostic_report(year, period_tag, new_tickers)
            print_diagnostic_report(report_rows, year, period_tag)
            log_signals(report_rows, year, period_tag)

    logger.info("Updating daily_prices (incremental, last %d days)", PRICE_UPDATE_LOOKBACK_DAYS)
    update_daily_prices(start_date=date.today() - timedelta(days=PRICE_UPDATE_LOOKBACK_DAYS))

    open_pending_positions()
    close_expired_positions()
    report_performance()