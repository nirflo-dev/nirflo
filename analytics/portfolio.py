from datetime import date

import numpy as np
import pandas as pd

TRADING_DAYS = 252
WEEKS_PER_YEAR = 52
LOT_SIZE = 100
LOOKBACK_WEEKS = 156
MIN_DAILY_OBS = 60
MIN_WEEKLY_OBS = 52
ILLIQUID_THRESHOLD = 0.20
STALE_RF_DAYS = 120
STALE_PRICE_DAYS = 5
JUMP_THRESHOLD = 0.40
Z_95 = 1.96


def _num(x, digits=None):
    if x is None:
        return None
    x = float(x)
    if not np.isfinite(x):
        return None
    return x if digits is None else round(x, digits)


def _date(ts):
    return pd.Timestamp(ts).strftime("%Y-%m-%d")


def _rf_per_period(rf_annual_pct, index, periods):
    per_period = (1 + rf_annual_pct.sort_index() / 100) ** (1 / periods) - 1
    return per_period.reindex(per_period.index.union(index)).ffill().reindex(index)


def _weekly_returns(prices):
    return prices.resample("W-FRI").last().pct_change(fill_method=None).iloc[1:]


def _sharpe(returns, rf_per_period, periods, basis):
    df = pd.concat([returns.rename("r"), rf_per_period.rename("rf")], axis=1).dropna()
    n = len(df)
    result = {
        "sharpe": None,
        "ci_low": None,
        "ci_high": None,
        "verdict": "tidak_dapat_dihitung",
        "annual_excess_return": None,
        "annual_vol": None,
        "start": _date(df.index[0]) if n else None,
        "end": _date(df.index[-1]) if n else None,
        "n_obs": n,
        "basis": basis,
    }
    if n < 3:
        return result
    excess = df["r"] - df["rf"]
    std = excess.std(ddof=1)
    if not np.isfinite(std) or std == 0:
        return result
    mean = excess.mean()
    per_period = mean / std
    se = np.sqrt((1 + 0.5 * per_period**2) / n)
    k = np.sqrt(periods)
    low = (per_period - Z_95 * se) * k
    high = (per_period + Z_95 * se) * k
    result.update(
        sharpe=_num(per_period * k),
        ci_low=_num(low),
        ci_high=_num(high),
        annual_excess_return=_num(mean * periods),
        annual_vol=_num(std * k),
        verdict="positif" if low > 0 else "negatif" if high < 0 else "tidak_pasti",
    )
    return result


def _since_buy_returns(prices, holdings):
    shares = pd.DataFrame(0.0, index=prices.index, columns=prices.columns)
    for row in holdings.itertuples():
        shares.loc[shares.index >= row.buy_date, row.ticker] += row.lots * LOT_SIZE
    prev_value = shares.shift(1) * prices.shift(1)
    total = prev_value.sum(axis=1)
    contribution = (prev_value * prices.pct_change(fill_method=None)).sum(axis=1)
    return (contribution / total).where(total > 0).dropna()


def _price_jumps(prices, since):
    daily = prices.pct_change(fill_method=None).iloc[1:]
    daily = daily[daily.index >= since]
    found = []
    for ticker in daily.columns:
        for day in daily.index[daily[ticker].abs() > JUMP_THRESHOLD]:
            found.append(
                {"ticker": ticker, "date": _date(day), "return": _num(daily.at[day, ticker], 4)}
            )
    return found


def cash_summary(stock_value, cash, stock_vol, stock_excess):
    """Dampak cash pada portofolio.

    Aturan: Sharpe hanya menilai bagian saham. Cash diasumsikan berimbal sebesar suku bunga
    bebas risiko, sehingga excess return dan volatilitasnya nol dan Sharpe tidak berubah berapa
    pun porsi cash-nya. Volatilitas dan excess return total adalah nilai bagian saham dikali
    porsi saham terhadap total.
    """
    cash = max(0.0, float(cash or 0.0))
    stock_value = float(stock_value)
    total = stock_value + cash
    cash_weight = cash / total if total > 0 else 0.0
    share = 1.0 - cash_weight

    def scaled(x):
        return None if x is None else _num(x * share, 6)

    return {
        "cash": {
            "amount": _num(cash, 2),
            "weight": _num(cash_weight, 6),
            "stock_value": _num(stock_value, 2),
            "total_value": _num(total, 2),
        },
        "portfolio_risk": {
            "stock_vol": _num(stock_vol, 6),
            "total_vol": scaled(stock_vol),
            "stock_excess_return": _num(stock_excess, 6),
            "total_excess_return": scaled(stock_excess),
        },
    }


def evaluate(prices, holdings, rf_annual_pct, no_trade_ratio=None, cash=0.0):
    tickers = sorted(holdings["ticker"].unique())
    missing = [t for t in tickers if t not in prices.columns]
    if missing:
        raise ValueError("data harga tidak ditemukan untuk: " + ", ".join(missing))
    if rf_annual_pct is None or rf_annual_pct.empty:
        raise ValueError("data risk-free rate kosong")

    prices = prices[tickers].sort_index().ffill()
    weekly = _weekly_returns(prices).dropna().tail(LOOKBACK_WEEKS)
    if len(weekly) < 2:
        raise ValueError("histori harga bersama terlalu pendek untuk dihitung")

    shares = holdings.groupby("ticker")["lots"].sum().reindex(tickers) * LOT_SIZE
    value = shares * prices.iloc[-1]
    if value.isna().any():
        raise ValueError("harga terakhir tidak tersedia untuk: " + ", ".join(value[value.isna()].index))
    weights = value / value.sum()

    cov = weekly.cov().to_numpy()
    w = weights.to_numpy()
    variance = w @ cov @ w
    risk = w * (cov @ w) / variance if variance > 0 else np.full(len(w), np.nan)

    corr = weekly.corr()
    n = len(tickers)
    avg_corr = (corr.to_numpy().sum() - n) / (n * (n - 1)) if n > 1 else None

    rf_daily = _rf_per_period(rf_annual_pct, prices.index, TRADING_DAYS)
    rf_weekly = _rf_per_period(rf_annual_pct, weekly.index, WEEKS_PER_YEAR)
    since_buy = _since_buy_returns(prices, holdings)
    jumps = _price_jumps(prices, min(weekly.index[0], holdings["buy_date"].min()))

    illiquid = sorted(
        t for t in tickers if (no_trade_ratio or {}).get(t, 0.0) > ILLIQUID_THRESHOLD
    )
    as_of = prices.index[-1]
    rf_age = (as_of - rf_annual_pct.index.max()).days
    price_age = (date.today() - as_of.date()).days

    warnings = []
    if n < 2:
        warnings.append("Korelasi butuh minimal 2 saham berbeda.")
    if len(since_buy) < MIN_DAILY_OBS:
        warnings.append(
            f"Masa kepemilikan hanya {len(since_buy)} hari, Sharpe sejak beli belum bermakna."
        )
    if len(weekly) < MIN_WEEKLY_OBS:
        warnings.append(
            f"Histori bersama semua saham hanya {len(weekly)} minggu, korelasi dan risiko belum stabil."
        )
    if jumps:
        listed = ", ".join(f"{j['ticker']} {j['date']} ({j['return'] * 100:+.0f}%)" for j in jumps[:5])
        warnings.append(
            f"Lonjakan harga harian lebih dari {JUMP_THRESHOLD * 100:.0f}%: {listed}. "
            "Kemungkinan split atau penyesuaian harga belum diterapkan, hasil saham ini tidak dapat dipercaya."
        )
    if illiquid:
        warnings.append(
            "Saham jarang diperdagangkan: " + ", ".join(illiquid)
            + ". Volatilitas bisa terlihat terlalu kecil dan Sharpe terlalu tinggi."
        )
    if price_age > STALE_PRICE_DAYS:
        warnings.append(f"Harga terakhir {price_age} hari lalu, jalankan update harga.")
    if rf_age > STALE_RF_DAYS:
        warnings.append(
            f"Data suku bunga terakhir {rf_age} hari lalu, nilai terakhir dipakai untuk periode setelahnya."
        )

    current = _sharpe(weekly.mul(weights, axis=1).sum(axis=1), rf_weekly, WEEKS_PER_YEAR, "mingguan")
    matrix = [[_num(v, 4) for v in row] for row in corr.to_numpy().tolist()]
    return {
        "as_of": _date(as_of),
        "window": {
            "weeks": len(weekly),
            "start": _date(weekly.index[0]),
            "end": _date(weekly.index[-1]),
        },
        # Bobot dan kontribusi risiko dihitung dari bagian saham saja (jumlah = 100%).
        "weights": {t: _num(v, 6) for t, v in weights.items()},
        "risk_contribution": {t: _num(v, 6) for t, v in zip(tickers, risk)},
        "correlation": {"tickers": tickers, "matrix": matrix, "average": _num(avg_corr, 4)},
        "sharpe": {
            "since_buy": _sharpe(since_buy, rf_daily, TRADING_DAYS, "harian"),
            "current_weights": current,
            "equal_weight": _sharpe(weekly.mean(axis=1), rf_weekly, WEEKS_PER_YEAR, "mingguan"),
        },
        **cash_summary(
            value.sum(), cash, current["annual_vol"], current["annual_excess_return"]
        ),
        "price_jumps": jumps,
        "illiquid_tickers": illiquid,
        "warnings": warnings,
    }