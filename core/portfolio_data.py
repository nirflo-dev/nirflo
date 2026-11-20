from datetime import date, timedelta

import pandas as pd

from core.history import load_prices, run_query

HISTORY_YEARS = 5
NO_TRADE_DAYS = 1095


def load_price_inputs(tickers, earliest_buy):
    start = min(
        earliest_buy.date() - timedelta(days=7),
        date.today() - timedelta(days=365 * HISTORY_YEARS),
    )
    frame = load_prices(
        tickers=list(tickers), start=start, columns=["adj_close_price", "is_no_trade"]
    )
    if frame.empty:
        raise ValueError("tidak ada data harga untuk ticker tersebut")
    wide = frame.pivot(index="price_date", columns="ticker", values="adj_close_price").sort_index()
    cutoff = frame["price_date"].max() - pd.Timedelta(days=NO_TRADE_DAYS)
    recent = frame[frame["price_date"] >= cutoff]
    flags = recent.assign(is_no_trade=recent["is_no_trade"].astype(float))
    ratio = flags.groupby("ticker")["is_no_trade"].mean()
    return wide, ratio.to_dict()


RISK_FREE_PREFERENCE = ("ID_BI_RATE", "ID_POLICY_RATE", "ID_RATE")


def _macro_series(indicator):
    df = run_query(
        "SELECT record_date, value FROM macro_indicators "
        "WHERE indicator_ticker = %s ORDER BY record_date",
        [indicator],
    )
    if df.empty:
        return pd.Series(dtype=float)
    df["record_date"] = pd.to_datetime(df["record_date"])
    return df.set_index("record_date")["value"].astype(float)


def load_risk_free():
    candidates = [_macro_series(name) for name in RISK_FREE_PREFERENCE]
    candidates = [s for s in candidates if not s.empty]
    if not candidates:
        return pd.Series(dtype=float)
    return max(candidates, key=lambda s: s.index.max())


def load_macro_series(indicator):
    if indicator == "ID_RATE":
        return load_risk_free()
    return _macro_series(indicator)