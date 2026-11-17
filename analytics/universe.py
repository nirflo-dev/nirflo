import numpy as np
import pandas as pd

WEEKS_PER_YEAR = 52
LOOKBACK_WEEKS = 156
MIN_WEEKS = 52
JUMP_THRESHOLD = 0.40
ACTIVE_WITHIN_DAYS = 14
Z_95 = 1.96


def _rf_weekly(rf_annual_pct, index):
    per_week = (1 + rf_annual_pct.sort_index() / 100) ** (1 / WEEKS_PER_YEAR) - 1
    return per_week.reindex(per_week.index.union(index)).ffill().reindex(index)


def compute_universe(prices, rf_annual_pct, no_trade_ratio):
    if rf_annual_pct is None or rf_annual_pct.empty:
        raise ValueError("data risk-free rate kosong")

    prices = prices.sort_index().dropna(axis=1, how="all")
    last_dates = prices.apply(lambda column: column.last_valid_index())
    cutoff = prices.index.max() - pd.Timedelta(days=ACTIVE_WITHIN_DAYS)
    active = last_dates.index[last_dates >= cutoff]
    prices = prices[active].ffill()
    last_dates = last_dates[active]

    weekly = (
        prices.resample("W-FRI").last().pct_change(fill_method=None).iloc[1:].tail(LOOKBACK_WEEKS)
    )
    if len(weekly) < MIN_WEEKS:
        raise ValueError("histori harga terlalu pendek untuk menghitung universe")
    excess = weekly.sub(_rf_weekly(rf_annual_pct, weekly.index), axis=0)

    n = excess.count()
    mean = excess.mean()
    std = excess.std(ddof=1)
    valid_std = std.where(std > 0)
    per_week = mean / valid_std
    se = np.sqrt((1 + 0.5 * per_week**2) / n.where(n > 0))
    k = np.sqrt(WEEKS_PER_YEAR)

    daily = prices.pct_change(fill_method=None).iloc[1:]
    daily = daily[daily.index >= weekly.index[0] - pd.Timedelta(days=7)]
    max_move = daily.abs().max()

    note = pd.Series("", index=excess.columns)
    note[n < MIN_WEEKS] = "histori kurang dari 52 minggu"
    note[(note == "") & (max_move > JUMP_THRESHOLD)] = "lonjakan harga harian lebih dari 40%"
    note[(note == "") & valid_std.isna()] = "volatilitas nol"

    stats = pd.DataFrame(
        {
            "n_weeks": n,
            "mean_excess_weekly": mean,
            "std_weekly": std,
            "annual_excess_return": mean * WEEKS_PER_YEAR,
            "annual_vol": std * k,
            "sharpe": per_week * k,
            "sharpe_ci_low": (per_week - Z_95 * se) * k,
            "sharpe_ci_high": (per_week + Z_95 * se) * k,
            "no_trade_ratio": pd.Series(no_trade_ratio, dtype=float).reindex(excess.columns),
            "last_price": prices.iloc[-1],
            "last_date": last_dates,
            "max_daily_move": max_move,
            "eligible": note == "",
            "note": note,
        }
    )
    stats.index.name = "ticker"

    eligible = sorted(stats.index[stats["eligible"]])
    if len(eligible) < 2:
        raise ValueError("terlalu sedikit saham yang memenuhi syarat")
    returns = weekly[eligible]
    corr = returns.corr(min_periods=MIN_WEEKS)
    present = returns.notna().astype(np.int32)
    pair_n = present.T.dot(present)
    return stats, corr, pair_n