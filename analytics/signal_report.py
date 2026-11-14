import argparse
import bisect
import csv
import logging
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import mean, median

import psycopg2
from dotenv import load_dotenv
from core.config import DB_CONFIG as config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


DEFAULT_HORIZONS = (1, 5, 10, 20)
DEFAULT_ENTRY_LAG = 1
DEFAULT_MAX_ENTRY_GAP_DAYS = 14
DEFAULT_DETAIL_ROWS = 30
MIN_SAMPLE_WARNING = 30
SUSPECT_RETURN = 0.5
HISTOGRAM_EDGES = (-0.20, -0.10, -0.05, 0.0, 0.05, 0.10, 0.20)
HISTOGRAM_BAR_WIDTH = 40
LINE_WIDTH = 118


def to_date(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    return value


def infer_direction(z: float | None, sur: float | None) -> str:
    if z is not None and sur is not None:
        if (z > 0 and sur < 0) or (z < 0 and sur > 0):
            return "MIXED"
    if z is not None:
        return "POSITIVE" if z > 0 else ("NEGATIVE" if z < 0 else "MIXED")
    if sur is not None:
        return "POSITIVE" if sur > 0 else ("NEGATIVE" if sur < 0 else "MIXED")
    return "MIXED"


def fetch_signals(
    since: date | None = None,
    statuses: tuple[str, ...] | None = None,
) -> list[dict]:
    conn = psycopg2.connect(**config)
    cur = conn.cursor()
    try:
        # Cek kolom opsional yang tersedia di signal_log / scan_results
        cur.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'signal_log';
            """
        )
        cols = {row[0] for row in cur.fetchall()}

        sur_expr = "sur_score" if "sur_score" in cols else ("sur" if "sur" in cols else "NULL::numeric")
        dir_expr = "direction" if "direction" in cols else "NULL::text"
        trig_expr = "trigger_source" if "trigger_source" in cols else "NULL::text"
        late_expr = "is_late" if "is_late" in cols else "FALSE"

        conditions = []
        params = []
        if since is not None:
            conditions.append("detected_at >= %s")
            params.append(since)
        if statuses:
            conditions.append("status = ANY(%s)")
            params.append(list(statuses))

        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        query = f"""
            SELECT id, ticker, year, period, z_score, {sur_expr}, status,
                   confirmation, detected_at, {dir_expr}, {trig_expr}, {late_expr}
            FROM signal_log
            {where_clause}
            ORDER BY detected_at, id;
        """
        cur.execute(query, tuple(params))
        rows = cur.fetchall()
    finally:
        cur.close()
        conn.close()

    results = []
    for row in rows:
        z_val = float(row[4]) if row[4] is not None else None
        sur_val = float(row[5]) if row[5] is not None else None
        direction = row[9] or infer_direction(z_val, sur_val)
        results.append(
            {
                "id": row[0],
                "ticker": row[1],
                "year": row[2],
                "period": row[3],
                "z": z_val,
                "sur": sur_val,
                "status": row[6],
                "confirmation": row[7] or "--",
                "detected": to_date(row[8]),
                "direction": direction,
                "trigger_source": row[10] or "SUE",
                "is_late": bool(row[11]),
            }
        )
    return results


def fetch_sectors(tickers: list[str]) -> dict[str, str]:
    if not tickers:
        return {}
    conn = psycopg2.connect(**config)
    cur = conn.cursor()
    sectors = {}
    try:
        for table in ("tickers", "companies", "emitens", "stock_metadata", "scan_results"):
            cur.execute(
                """
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = %s AND column_name = 'sector';
                """,
                (table,),
            )
            if cur.fetchone():
                cur.execute(
                    f"SELECT DISTINCT ticker, sector FROM {table} WHERE ticker = ANY(%s) AND sector IS NOT NULL;",
                    (tickers,),
                )
                for t, s in cur.fetchall():
                    if t not in sectors and s:
                        sectors[t] = s
    finally:
        cur.close()
        conn.close()
    return sectors


def fetch_market_last() -> date | None:
    conn = psycopg2.connect(**config)
    cur = conn.cursor()
    try:
        cur.execute("SELECT MAX(price_date) FROM daily_prices WHERE close_price IS NOT NULL;")
        row = cur.fetchone()
        return to_date(row[0]) if row and row[0] else None
    finally:
        cur.close()
        conn.close()


def fetch_series(tickers: list[str], start: date, traded_only: bool) -> dict:
    conn = psycopg2.connect(**config)
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT ticker, price_date, close_price, is_no_trade
            FROM daily_prices
            WHERE ticker = ANY(%s) AND price_date >= %s AND close_price IS NOT NULL
            ORDER BY ticker, price_date;
            """,
            (tickers, start),
        )
        rows = cur.fetchall()
    finally:
        cur.close()
        conn.close()

    series = {}
    for ticker, price_date, close_price, is_no_trade in rows:
        if traded_only and is_no_trade:
            continue
        dates, closes = series.setdefault(ticker, ([], []))
        dates.append(price_date)
        closes.append(float(close_price))
    return series


def benchmark_close(series, target: date) -> float | None:
    if series is None:
        return None
    dates, closes = series
    index = bisect.bisect_right(dates, target) - 1
    return closes[index] if index >= 0 else None


def evaluate_signal(
    signal: dict,
    series,
    benchmark,
    sector_or_lag="Unknown",
    market_last: date | None = None,
    entry_lag: int = DEFAULT_ENTRY_LAG,
    horizons: tuple = DEFAULT_HORIZONS,
    max_gap: int = DEFAULT_MAX_ENTRY_GAP_DAYS,
) -> dict:
    # Mendukung pemanggilan 5 argumen (lama) maupun 8 argumen (dashboard.py)
    if isinstance(sector_or_lag, int) and isinstance(market_last, tuple):
        horizons = market_last
        entry_lag = sector_or_lag
        sector = signal.get("sector", "Unknown")
        market_last = None
    else:
        sector = sector_or_lag or "Unknown"

    flags = []
    if not sector or sector == "Unknown":
        flags.append("UNKNOWN_SECTOR")
    if signal.get("is_late"):
        flags.append("LATE")

    result = {
        **signal,
        "sector": sector,
        "z": signal.get("z"),
        "sur": signal.get("sur"),
        "direction": signal.get("direction") or infer_direction(signal.get("z"), signal.get("sur")),
        "trigger_source": signal.get("trigger_source", "SUE"),
        "entry_date": None,
        "entry_price": None,
        "returns": {},
        "excess": {},
        "flags": flags,
    }

    if series is None:
        flags.append("NO_PRICE")
        return result

    dates, closes = series
    entry_index = bisect.bisect_left(dates, signal["detected"]) + entry_lag
    if entry_index >= len(dates):
        flags.append("NO_PRICE")
        return result

    entry_price = closes[entry_index]
    if entry_price <= 0:
        flags.append("NO_PRICE")
        return result

    entry_date = dates[entry_index]
    result["entry_date"] = entry_date
    result["entry_price"] = entry_price

    if (entry_date - signal["detected"]).days > max_gap:
        flags.append("ENTRY_GAP")

    benchmark_entry = benchmark_close(benchmark, entry_date)
    jumped = False

    for horizon in horizons:
        exit_index = entry_index + horizon
        if exit_index >= len(dates):
            continue
        ret = closes[exit_index] / entry_price - 1
        result["returns"][horizon] = ret
        if abs(ret) >= SUSPECT_RETURN:
            jumped = True
        if benchmark_entry:
            benchmark_exit = benchmark_close(benchmark, dates[exit_index])
            if benchmark_exit:
                result["excess"][horizon] = ret - (benchmark_exit / benchmark_entry - 1)

    if jumped:
        flags.append("PRICE_JUMP")

    return result


def export_csv(evaluated: list[dict], horizons: tuple, filepath: str) -> None:
    base_cols = [
        "id", "ticker", "year", "period", "sector", "status", "direction",
        "trigger_source", "z", "sur", "confirmation", "detected", "entry_date",
        "entry_price", "flags",
    ]
    ret_cols = [f"ret_{h}d" for h in horizons]
    exc_cols = [f"excess_{h}d" for h in horizons]
    fieldnames = base_cols + ret_cols + exc_cols

    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for e in evaluated:
            row = {k: e.get(k) for k in base_cols}
            row["flags"] = "|".join(e.get("flags", []))
            for h in horizons:
                row[f"ret_{h}d"] = e["returns"].get(h)
                row[f"excess_{h}d"] = e["excess"].get(h)
            writer.writerow(row)
    logger.info("Exported %d evaluated signals to %s", len(evaluated), filepath)


def percentile(ordered: list[float], q: float) -> float:
    if len(ordered) == 1:
        return ordered[0]
    position = q * (len(ordered) - 1)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def summarize(values: list[float], positive_is_win: bool) -> dict | None:
    if not values:
        return None
    ordered = sorted(values)
    wins = sum(1 for v in ordered if (v > 0 if positive_is_win else v < 0))
    return {
        "n": len(ordered),
        "mean": mean(ordered),
        "median": median(ordered),
        "win": wins / len(ordered),
        "p10": percentile(ordered, 0.10),
        "p25": percentile(ordered, 0.25),
        "p75": percentile(ordered, 0.75),
        "p90": percentile(ordered, 0.90),
        "min": ordered[0],
        "max": ordered[-1],
    }


def format_pct(value, width: int = 8, decimals: int = 1) -> str:
    if value is None:
        return f"{'--':>{width}}"
    return f"{value * 100:>{width - 1}.{decimals}f}%"


def build_groups(evaluated: list[dict]) -> list[tuple[str, list[dict]]]:
    groups = [("All", evaluated)]
    for status in ("STRONG", "MODERATE", "WEAK"):
        subset = [e for e in evaluated if e["status"] == status]
        if subset:
            groups.append((status, subset))
    for label in sorted({e["confirmation"] for e in evaluated}):
        groups.append((label, [e for e in evaluated if e["confirmation"] == label]))
    return groups


def print_group_rows(label: str, subset: list[dict], horizons: tuple, metric: str, positive_is_win: bool) -> None:
    title = f"{label} ({len(subset)})"
    for index, horizon in enumerate(horizons):
        values = [e[metric][horizon] for e in subset if horizon in e[metric]]
        summary = summarize(values, positive_is_win)
        name = title if index == 0 else ""
        prefix = f"{name:<22} {'+' + str(horizon) + 'd':>5} {len(values):>5} "
        if summary is None:
            print(prefix + "  (no matured signals yet)")
            continue
        print(
            prefix
            + format_pct(summary["mean"])
            + format_pct(summary["median"])
            + format_pct(summary["win"], 8, 0)
            + format_pct(summary["p10"])
            + format_pct(summary["p25"])
            + format_pct(summary["p75"])
            + format_pct(summary["p90"])
            + format_pct(summary["min"])
            + format_pct(summary["max"])
        )


def histogram_labels() -> list[str]:
    edges = HISTOGRAM_EDGES
    labels = [f"below {edges[0] * 100:.0f}%"]
    for low, high in zip(edges, edges[1:]):
        labels.append(f"{low * 100:.0f}% to {high * 100:.0f}%")
    labels.append(f"{edges[-1] * 100:.0f}% and above")
    return labels


def print_histogram(title: str, values: list[float]) -> None:
    if not values:
        return
    labels = histogram_labels()
    counts = [0] * len(labels)
    for value in values:
        counts[bisect.bisect_right(HISTOGRAM_EDGES, value)] += 1
    peak = max(counts)
    print(f"\n{title}")
    for label, count in zip(labels, counts):
        bar = "#" * round(count / peak * HISTOGRAM_BAR_WIDTH)
        print(f"  {label:<18} {count:>4}  {bar}")


def print_section(title: str, subset: list[dict], horizons: tuple, metric: str, positive_is_win: bool) -> None:
    print(f"\n{title}")
    print("-" * LINE_WIDTH)
    if not subset:
        print("No signals in this group.")
        return
    header = (
        f"{'Group':<22} {'Hold':>5} {'n':>5} "
        f"{'Mean':>8}{'Median':>8}{'Win':>8}{'P10':>8}{'P25':>8}{'P75':>8}{'P90':>8}{'Min':>8}{'Max':>8}"
    )
    print(header)
    for label, group in build_groups(subset):
        print_group_rows(label, group, horizons, metric, positive_is_win)

    longest = horizons[-1]
    values = [e[metric][longest] for e in subset if longest in e[metric]]
    print_histogram(f"Distribution at +{longest}d", values)


def print_detail(evaluated: list[dict], horizons: tuple, metric: str, limit: int) -> None:
    if limit <= 0:
        return
    rows = sorted(evaluated, key=lambda e: (e["detected"], e["id"]), reverse=True)[:limit]
    print(f"\nMost recent {len(rows)} signal(s)")
    print("-" * LINE_WIDTH)
    header = (
        f"{'Ticker':<8} {'Period':<8} {'Status':<9} {'Z':>6} {'Detected':<11} "
        f"{'Entry date':<11} {'Entry':>9}"
        + "".join(f"{'+' + str(h) + 'd':>9}" for h in horizons)
    )
    print(header)
    for e in rows:
        entry_date = e["entry_date"].isoformat() if e["entry_date"] else "--"
        entry_price = f"{e['entry_price']:,.0f}" if e["entry_price"] else "--"
        z_str = f"{e['z']:>6.2f}" if e.get("z") is not None else f"{'--':>6}"
        cells = "".join(format_pct(e[metric].get(h), 9) for h in horizons)
        print(
            f"{e['ticker']:<8} {e['period'] + ' ' + str(e['year']):<8} {e['status']:<9} "
            f"{z_str} {e['detected'].isoformat():<11} {entry_date:<11} {entry_price:>9}{cells}"
        )


def report_signal_performance(
    horizons: tuple = DEFAULT_HORIZONS,
    entry_lag: int = DEFAULT_ENTRY_LAG,
    max_gap: int = DEFAULT_MAX_ENTRY_GAP_DAYS,
    since: date | None = None,
    benchmark_ticker: str | None = None,
    detail: int = DEFAULT_DETAIL_ROWS,
    csv_path: str | None = None,
) -> None:
    signals = [s for s in fetch_signals(since) if s["z"] is not None or s["sur"] is not None]
    if not signals:
        print("\nNo signals in signal_log yet, nothing to evaluate.")
        return

    tickers = sorted({s["ticker"] for s in signals})
    start = min(s["detected"] for s in signals) - timedelta(days=10)
    series = fetch_series(tickers, start, traded_only=True)
    sectors = fetch_sectors(tickers)
    market_last = fetch_market_last()

    benchmark = None
    if benchmark_ticker:
        benchmark = fetch_series([benchmark_ticker], start, traded_only=False).get(benchmark_ticker)
        if benchmark is None:
            logger.warning("No price data for benchmark %s, falling back to raw returns", benchmark_ticker)

    evaluated = [
        evaluate_signal(
            s, series.get(s["ticker"]), benchmark, sectors.get(s["ticker"], "Unknown"),
            market_last, entry_lag, horizons, max_gap,
        )
        for s in signals
    ]

    if csv_path:
        export_csv(evaluated, horizons, csv_path)

    metric = "excess" if benchmark else "returns"

    positives = [e for e in evaluated if e["direction"] == "POSITIVE"]
    negatives = [e for e in evaluated if e["direction"] == "NEGATIVE"]
    with_entry = sum(1 for e in evaluated if e["entry_date"] is not None)

    print(f"\n{'=' * LINE_WIDTH}")
    print("Signal performance report")
    print(f"{'=' * LINE_WIDTH}")
    print(
        f"Signals: {len(evaluated)}  |  positive: {len(positives)}  |  negative: {len(negatives)}  "
        f"|  with entry price: {with_entry}"
    )
    print(
        f"Entry: close {entry_lag} traded day(s) after the first close on or after the detection date  "
        f"|  Hold periods in traded days  |  Gross of fees and taxes"
    )
    print(
        f"Metric: {'excess return vs ' + benchmark_ticker if benchmark else 'raw price return (unadjusted close)'}"
    )

    print_section(
        "POSITIVE surprise (z > 0): long thesis, a win is a return above zero",
        positives, horizons, metric, True,
    )
    print_section(
        "NEGATIVE surprise (z < 0): avoid or short thesis, a win is a return below zero",
        negatives, horizons, metric, False,
    )

    print_detail(evaluated, horizons, metric, detail)

    longest = horizons[-1]
    matured = sum(1 for e in evaluated if longest in e["returns"])
    if matured < MIN_SAMPLE_WARNING:
        print(
            f"\nNote: only {matured} signal(s) have reached +{longest}d. "
            f"Fewer than {MIN_SAMPLE_WARNING} is too small to draw conclusions."
        )

    suspect = sorted({e["ticker"] for e in evaluated if "PRICE_JUMP" in e["flags"]})
    if suspect:
        print(f"Check for stock splits or data errors (|return| >= {SUSPECT_RETURN * 100:.0f}%): {', '.join(suspect)}")
    print(f"{'=' * LINE_WIDTH}\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate logged signals against subsequent price moves.")
    parser.add_argument("--horizons", type=int, nargs="+", default=list(DEFAULT_HORIZONS))
    parser.add_argument("--entry-lag", type=int, default=DEFAULT_ENTRY_LAG)
    parser.add_argument("--max-gap", type=int, default=DEFAULT_MAX_ENTRY_GAP_DAYS)
    parser.add_argument("--since", type=date.fromisoformat, default=None)
    parser.add_argument("--benchmark", default=None)
    parser.add_argument("--detail", type=int, default=DEFAULT_DETAIL_ROWS)
    parser.add_argument("--csv", default=None, help="Export evaluated signals to a CSV file")
    args = parser.parse_args()
    if any(h < 1 for h in args.horizons):
        parser.error("--horizons values must be 1 or greater")
    if args.entry_lag < 0:
        parser.error("--entry-lag must be 0 or greater")
    return args


if __name__ == "__main__":
    args = parse_args()
    report_signal_performance(
        horizons=tuple(sorted(set(args.horizons))),
        entry_lag=args.entry_lag,
        max_gap=args.max_gap,
        since=args.since,
        benchmark_ticker=args.benchmark,
        detail=args.detail,
        csv_path=args.csv,
    )