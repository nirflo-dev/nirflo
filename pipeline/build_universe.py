import sys
import time
from datetime import date, timedelta

import pandas as pd

from analytics.universe import LOOKBACK_WEEKS, compute_universe
from core import universe_store as store
from core.history import load_prices
from core.portfolio_data import load_macro_series

WINDOW_DAYS = 7 * (LOOKBACK_WEEKS + 8)
ILLIQUID_THRESHOLD = 0.20


def main():
    started = time.monotonic()
    start = date.today() - timedelta(days=WINDOW_DAYS)
    try:
        frame = load_prices(start=start, columns=["adj_close_price", "is_no_trade"])
        rf = load_macro_series("ID_RATE")
    except (ValueError, RuntimeError) as exc:
        sys.exit(f"gagal membaca data: {exc}")
    if frame.empty:
        sys.exit("tidak ada data harga")

    prices = frame.pivot(index="price_date", columns="ticker", values="adj_close_price").sort_index()
    latest = frame["price_date"].max()
    recent = frame[frame["price_date"] >= latest - pd.Timedelta(days=LOOKBACK_WEEKS * 7)]
    no_trade = recent["is_no_trade"].astype(float).groupby(recent["ticker"]).mean().to_dict()

    try:
        stats, corr, pair_n = compute_universe(prices, rf, no_trade)
    except ValueError as exc:
        sys.exit(str(exc))

    store.ensure_tables()
    as_of = prices.index.max().strftime("%Y-%m-%d")
    n_stats, n_pairs = store.save_universe(as_of, stats, corr, pair_n)

    illiquid = int((stats["no_trade_ratio"] > ILLIQUID_THRESHOLD).sum())
    print(
        f"universe per {as_of}: {n_stats} saham ({int(stats['eligible'].sum())} memenuhi syarat, "
        f"{illiquid} sering tidak diperdagangkan), {n_pairs} pasangan korelasi, "
        f"{time.monotonic() - started:.0f} dtk"
    )
    for reason, count in stats.loc[~stats["eligible"], "note"].value_counts().items():
        print(f"  tidak disertakan: {count} saham, {reason}")


if __name__ == "__main__":
    main()