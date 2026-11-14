import psycopg2
import yfinance as yf
import pandas as pd
import os
from pathlib import Path
from dotenv import load_dotenv
from datetime import datetime, date, timedelta

env_path = Path(__file__).resolve().parents[2] / ".env"
load_dotenv(dotenv_path=env_path)

config = {
    "dbname": os.getenv("DB_NAME"),
    "user": os.getenv("DB_USER"),
    "password": os.getenv("DB_PASSWORD"),
    "host": os.getenv("DB_HOST"),
    "port": os.getenv("DB_PORT")
}

FULL_BACKFILL_START = date(2013, 1, 1)  # dipakai hanya untuk backfill manual


def _ts():
    return datetime.now().strftime("%H:%M:%S")


def log(msg):
    print(f"[{_ts()}] {msg}")


def load_trading_days(cur):
    cur.execute("SELECT calendar_date FROM trading_calendar WHERE is_trading_day = TRUE;")
    trading_days = {row[0] for row in cur.fetchall()}
    if not trading_days:
        raise RuntimeError("trading_calendar kosong — jalankan build_trading_calendar.py dulu")
    return trading_days


def update_daily_prices(start_date: date = None) -> None:
    """
    start_date=None -> full backfill dari FULL_BACKFILL_START (mahal,
    untuk run manual sekali-sekali). Dipanggil dengan start_date recent
    (mis. 10 hari terakhir) untuk incremental update harian dari run.py
    — ON CONFLICT upsert membuat ini aman dipanggil berulang tanpa
    duplikasi, dan jendela pendek membuat panggilan yfinance jauh lebih
    cepat daripada narik ulang histori penuh tiap malam.
    """
    effective_start = start_date or FULL_BACKFILL_START
    log(f"Daily_prices sync | start={effective_start} | interval=1d")

    conn = None
    try:
        conn = psycopg2.connect(**config)
        cur = conn.cursor()

        trading_days = load_trading_days(cur)
        log(f"CALENDAR | {len(trading_days)} valid trading days loaded")

        cur.execute("SELECT ticker FROM emitens WHERE is_active = TRUE;")
        tickers = [row[0] for row in cur.fetchall()]

        if not tickers:
            log("ABORT | emitens table empty | run master-data.py first")
            return

        log(f"UNIVERSE | {len(tickers)} active tickers")

        n_ok, n_empty, n_fail = 0, 0, 0
        total_skipped_calendar = 0
        total_flagged_no_trade = 0
        total_rows_failed_parse = 0

        for ticker in tickers:
            try:
                df = yf.download(
                    f"{ticker}.JK",
                    start=effective_start,
                    interval="1d",
                    auto_adjust=False,
                    progress=False,
                )

                if df.empty:
                    log(f"{ticker:<12} | NO_DATA")
                    n_empty += 1
                    continue

                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.get_level_values(0)

                df = df.reset_index()

                rows_written = 0
                rows_skipped_calendar = 0
                rows_flagged_no_trade = 0
                rows_failed_parse = 0

                for _, row in df.iterrows():
                    price_date = row['Date'].date()

                    if price_date not in trading_days:
                        rows_skipped_calendar += 1
                        continue

                    try:
                        open_p = float(row['Open']) if not pd.isna(row['Open']) else None
                        high_p = float(row['High']) if not pd.isna(row['High']) else None
                        low_p = float(row['Low']) if not pd.isna(row['Low']) else None
                        close_p = float(row['Close']) if not pd.isna(row['Close']) else None
                        adj_close_p = float(row['Adj Close']) if not pd.isna(row['Adj Close']) else None
                        volume = int(row['Volume']) if not pd.isna(row['Volume']) else 0

                        if close_p is None or adj_close_p is None:
                            rows_failed_parse += 1
                            continue

                        is_no_trade = (volume == 0)
                        if is_no_trade:
                            rows_flagged_no_trade += 1

                        cur.execute("""
                            INSERT INTO daily_prices
                                (ticker, price_date, open_price, high_price,
                                 low_price, close_price, adj_close_price,
                                 volume, is_no_trade)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT (ticker, price_date)
                            DO UPDATE SET
                                open_price = EXCLUDED.open_price,
                                high_price = EXCLUDED.high_price,
                                low_price = EXCLUDED.low_price,
                                close_price = EXCLUDED.close_price,
                                adj_close_price = EXCLUDED.adj_close_price,
                                volume = EXCLUDED.volume,
                                is_no_trade = EXCLUDED.is_no_trade;
                        """, (ticker, price_date, open_p, high_p, low_p,
                              close_p, adj_close_p, volume, is_no_trade))
                        rows_written += 1

                    except Exception as e:
                        rows_failed_parse += 1
                        log(f"{ticker:<12} | ROW_PARSE_FAIL | {price_date} | {type(e).__name__}: {e}")
                        continue

                conn.commit()
                log(f"{ticker:<12} | OK | written={rows_written} "
                    f"calendar_skipped={rows_skipped_calendar} "
                    f"no_trade_flagged={rows_flagged_no_trade} "
                    f"parse_failed={rows_failed_parse}")

                n_ok += 1
                total_skipped_calendar += rows_skipped_calendar
                total_flagged_no_trade += rows_flagged_no_trade
                total_rows_failed_parse += rows_failed_parse

            except Exception as e:
                log(f"{ticker:<12} | FAIL | {type(e).__name__}: {e}")
                n_fail += 1
                conn.rollback()

        log(f"SUMMARY | ok={n_ok} empty={n_empty} fail={n_fail} total={len(tickers)}")
        log(f"SUMMARY | calendar_skipped={total_skipped_calendar} "
            f"no_trade_flagged={total_flagged_no_trade} "
            f"parse_failed={total_rows_failed_parse}")

    except Exception as e:
        log(f"FATAL | {type(e).__name__}: {e}")
    finally:
        if conn is not None:
            cur.close()
            conn.close()


if __name__ == "__main__":
    update_daily_prices()  # full backfill saat dijalankan manual