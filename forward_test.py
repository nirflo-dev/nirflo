import logging
import os
from datetime import date, timedelta
from pathlib import Path

import psycopg2
from dotenv import load_dotenv

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

base_dir = Path(__file__).resolve().parent.parent.parent
env_path = base_dir / ".env"
load_dotenv(dotenv_path=env_path)

config = {
    "dbname": os.getenv("DB_NAME"),
    "user": os.getenv("DB_USER"),
    "password": os.getenv("DB_PASSWORD"),
    "host": os.getenv("DB_HOST"),
    "port": os.getenv("DB_PORT"),
}

if not config["password"]:
    raise RuntimeError(f".env not found or DB_PASSWORD is empty. Expected at: {env_path}")

STARTING_CAPITAL = 100_000_000
POSITION_FRACTION = 0.10
MAX_CONCURRENT_POSITIONS = 10
HOLDING_PERIOD_DAYS = 20


def get_ledger_state() -> dict:
    conn = psycopg2.connect(**config)
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT
                COALESCE(SUM(entry_price * shares) FILTER (WHERE status IN ('OPEN', 'CLOSED')), 0) AS total_bought,
                COALESCE(SUM(exit_price * shares) FILTER (WHERE status = 'CLOSED'), 0) AS total_sold,
                COALESCE(SUM((exit_price - entry_price) * shares) FILTER (WHERE status = 'CLOSED'), 0) AS realized_pnl
            FROM paper_positions;
            """
        )
        total_bought, total_sold, realized_pnl = cur.fetchone()
    finally:
        cur.close()
        conn.close()

    cash = STARTING_CAPITAL - float(total_bought) + float(total_sold)
    equity = STARTING_CAPITAL + float(realized_pnl)

    return {"cash": cash, "equity": equity, "realized_pnl": float(realized_pnl)}


def get_open_position_count() -> int:
    conn = psycopg2.connect(**config)
    cur = conn.cursor()
    try:
        cur.execute("SELECT COUNT(*) FROM paper_positions WHERE status = 'OPEN';")
        return cur.fetchone()[0]
    finally:
        cur.close()
        conn.close()


def open_pending_positions() -> None:
    conn = psycopg2.connect(**config)
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT sl.id, sl.ticker, sl.detected_at
            FROM signal_log sl
            LEFT JOIN paper_positions pp ON sl.id = pp.signal_id
            WHERE pp.id IS NULL
            ORDER BY sl.detected_at;
            """
        )
        pending_signals = cur.fetchall()
    finally:
        cur.close()
        conn.close()

    open_count = get_open_position_count()
    available_slots = MAX_CONCURRENT_POSITIONS - open_count

    for signal_id, ticker, detected_at in pending_signals:
        ledger = get_ledger_state()
        position_value = STARTING_CAPITAL * POSITION_FRACTION

        conn = psycopg2.connect(**config)
        cur = conn.cursor()
        try:
            if available_slots <= 0:
                cur.execute(
                    "INSERT INTO paper_positions (signal_id, ticker, status) VALUES (%s, %s, 'SKIPPED_NO_SLOT');",
                    (signal_id, ticker),
                )
                conn.commit()
                logger.info("%s: no slot available, marked SKIPPED_NO_SLOT", ticker)
                continue

            if position_value > ledger["cash"]:
                cur.execute(
                    "INSERT INTO paper_positions (signal_id, ticker, status) VALUES (%s, %s, 'SKIPPED_NO_CASH');",
                    (signal_id, ticker),
                )
                conn.commit()
                logger.info(
                    "%s: insufficient cash (need %.0f, have %.0f), marked SKIPPED_NO_CASH",
                    ticker, position_value, ledger["cash"],
                )
                continue

            cur.execute(
                """
                SELECT price_date, close_price FROM daily_prices
                WHERE ticker = %s AND price_date >= %s
                ORDER BY price_date DESC
                LIMIT 1;
                """,
                (ticker, detected_at),
            )
            price_row = cur.fetchone()

            if price_row is None:
                logger.warning(
                    "%s: no price data available on/after %s yet, entry deferred",
                    ticker, detected_at,
                )
                continue

            entry_date, entry_price = price_row
            shares = position_value / float(entry_price)

            cur.execute(
                """
                INSERT INTO paper_positions (signal_id, ticker, entry_date, entry_price, shares, status)
                VALUES (%s, %s, %s, %s, %s, 'OPEN');
                """,
                (signal_id, ticker, entry_date, entry_price, shares),
            )
            conn.commit()
            available_slots -= 1
            logger.info(
                "Opened %s: entry_date=%s entry_price=%s shares=%.2f (position_value=Rp%.0f)",
                ticker, entry_date, entry_price, shares, position_value,
            )

        except Exception:
            logger.exception("Failed to open position for %s", ticker)
            conn.rollback()
        finally:
            cur.close()
            conn.close()


def close_expired_positions() -> None:
    conn = psycopg2.connect(**config)
    cur = conn.cursor()
    try:
        cur.execute("SELECT id, ticker, entry_date FROM paper_positions WHERE status = 'OPEN';")
        open_positions = cur.fetchall()
    finally:
        cur.close()
        conn.close()

    for position_id, ticker, entry_date in open_positions:
        target_exit_date = entry_date + timedelta(days=HOLDING_PERIOD_DAYS * 7 // 5)

        if target_exit_date > date.today():
            continue

        conn = psycopg2.connect(**config)
        cur = conn.cursor()
        try:
            cur.execute(
                """
                SELECT price_date, close_price FROM daily_prices
                WHERE ticker = %s AND price_date >= %s
                ORDER BY price_date ASC
                LIMIT 1;
                """,
                (ticker, target_exit_date),
            )
            price_row = cur.fetchone()

            if price_row is None:
                logger.warning("%s: exit due but no price data at/after %s yet, deferred",
                                ticker, target_exit_date)
                continue

            exit_date, exit_price = price_row

            cur.execute(
                """
                UPDATE paper_positions
                SET exit_date = %s, exit_price = %s, status = 'CLOSED'
                WHERE id = %s;
                """,
                (exit_date, exit_price, position_id),
            )
            conn.commit()
            logger.info("Closed %s: exit_date=%s exit_price=%s", ticker, exit_date, exit_price)

        except Exception:
            logger.exception("Failed to close position for %s", ticker)
            conn.rollback()
        finally:
            cur.close()
            conn.close()


def report_performance() -> None:
    conn = psycopg2.connect(**config)
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT ticker, entry_price, exit_price, shares
            FROM paper_positions
            WHERE status = 'CLOSED';
            """
        )
        closed = cur.fetchall()
    finally:
        cur.close()
        conn.close()

    ledger = get_ledger_state()

    if not closed:
        print("\nNo closed positions yet.")
        print(f"Current cash: Rp {ledger['cash']:,.0f}")
        print(f"Current equity: Rp {ledger['equity']:,.0f}")
        return

    returns = []
    print(f"\n{'Ticker':<8} {'Entry':>12} {'Exit':>12} {'Return':>10}")
    for ticker, entry_price, exit_price, shares in closed:
        ret = (float(exit_price) - float(entry_price)) / float(entry_price)
        returns.append(ret)
        print(f"{ticker:<8} {float(entry_price):>12,.0f} {float(exit_price):>12,.0f} {ret*100:>9.1f}%")

    n = len(returns)
    avg_return = sum(returns) / n
    hit_rate = sum(1 for r in returns if r > 0) / n

    if n > 1:
        variance = sum((r - avg_return) ** 2 for r in returns) / (n - 1)
        std_return = variance ** 0.5
        sharpe = (avg_return / std_return) if std_return > 0 else float("nan")
    else:
        sharpe = float("nan")

    print(f"\nTrades closed: {n}")
    print(f"Average return per trade: {avg_return*100:.1f}%")
    print(f"Hit rate: {hit_rate*100:.1f}%")
    print(f"Sharpe (per-trade basis, not annualized): {sharpe:.2f}")
    print(f"\nCurrent cash: Rp {ledger['cash']:,.0f}")
    print(f"Current equity: Rp {ledger['equity']:,.0f}")
    print(f"Realized P&L: Rp {ledger['realized_pnl']:,.0f} ({ledger['realized_pnl']/STARTING_CAPITAL*100:+.1f}% of initial capital)")


if __name__ == "__main__":
    open_pending_positions()
    close_expired_positions()
    report_performance()