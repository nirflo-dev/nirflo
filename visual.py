import logging
import os
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import psycopg2
from dotenv import load_dotenv
from scipy.stats import spearmanr, ttest_ind

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ==========================================
# Configuration
# ==========================================
SCRIPT_DIR = Path(__file__).resolve().parent


def locate_env_file() -> Path:
    """Searches the script folder and each parent folder for a .env file."""
    for folder in [SCRIPT_DIR, *SCRIPT_DIR.parents]:
        candidate = folder / ".env"
        if candidate.exists():
            return candidate
    raise RuntimeError(f".env not found in {SCRIPT_DIR} or any parent folder")


env_path = locate_env_file()
load_dotenv(dotenv_path=env_path)
logger.info("Loaded environment from %s", env_path)

config = {
    "dbname": os.getenv("DB_NAME"),
    "user": os.getenv("DB_USER"),
    "password": os.getenv("DB_PASSWORD"),
    "host": os.getenv("DB_HOST"),
    "port": os.getenv("DB_PORT"),
}

if not config["password"]:
    raise RuntimeError(f"DB_PASSWORD is empty in {env_path}")

PRE_DAYS = 10            # trading days before the event shown on the chart
POST_DAYS = 40           # trading days after the event shown on the chart
HOLD_DAYS = 20           # holding period for the forward-return charts
MIN_PEERS = 5            # minimum tickers per sector-year for the z-score
MAX_NO_TRADE = 0.30      # drop events where >30% of window days had no trade
FILING_LAG_DAYS = 90     # fallback: period_end + 90 days when report_date is missing
RETURN_CLIP = 0.50       # clip absolute daily returns to limit data-error outliers
FIG_DIR = SCRIPT_DIR / "figures"


# ==========================================
# Data access
# ==========================================
def fetch_df(sql: str, params=None) -> pd.DataFrame:
    conn = psycopg2.connect(**config)
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        columns = [d[0] for d in cur.description]
        return pd.DataFrame(cur.fetchall(), columns=columns)
    finally:
        conn.close()


def robust_z(series: pd.Series) -> pd.Series:
    median = series.median()
    mad = (series - median).abs().median() * 1.4826
    if not mad or np.isnan(mad):
        return pd.Series(np.nan, index=series.index)
    return (series - median) / mad


# ==========================================
# Event construction
# ==========================================
def load_events() -> pd.DataFrame:
    df = fetch_df(
        """
        SELECT f.ticker, f.year, f.period, f.revenue, f.revenue_prior,
               f.report_date, f.period_end, e.sector
        FROM financial_reports_v1 f
        LEFT JOIN emitens e ON e.ticker = f.ticker;
        """
    )
    logger.info("Rows in financial_reports_v1: %d", len(df))
    if df.empty:
        raise RuntimeError("financial_reports_v1 returned no rows")
    logger.info("Period values: %s", df["period"].value_counts().to_dict())

    for col in ("revenue", "revenue_prior"):
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df[df["period"].astype(str).str.upper() == "FY"].copy()
    logger.info("After period = FY: %d", len(df))

    # Fill missing revenue_prior from the previous year's FY revenue
    prior_lookup = df.set_index(["ticker", "year"])["revenue"]
    prior_keys = list(zip(df["ticker"], df["year"] - 1))
    prior_from_table = pd.Series(prior_lookup.reindex(prior_keys).to_numpy(), index=df.index)
    logger.info("revenue_prior NULL before fill: %d", int(df["revenue_prior"].isna().sum()))
    df["revenue_prior"] = df["revenue_prior"].fillna(prior_from_table)
    logger.info("revenue_prior NULL after fill: %d", int(df["revenue_prior"].isna().sum()))

    df = df[(df["revenue"] > 0) & (df["revenue_prior"] > 0)].copy()
    logger.info("After revenue and revenue_prior > 0: %d", len(df))

    logger.info("Sector NULL: %d", int(df["sector"].isna().sum()))
    df = df[df["sector"].notna()].copy()
    logger.info("After sector not null: %d", len(df))

    df["growth"] = df["revenue"] / df["revenue_prior"] - 1

    peer_count = df.groupby(["year", "sector"])["ticker"].transform("size")
    df = df[peer_count >= MIN_PEERS].copy()
    logger.info("After MIN_PEERS >= %d per sector-year: %d", MIN_PEERS, len(df))

    df["shock_z"] = df.groupby(["year", "sector"])["growth"].transform(robust_z)
    df = df.dropna(subset=["shock_z"])
    logger.info("After z-score: %d", len(df))

    if df.empty:
        raise RuntimeError("No events left after filtering, see stage counts above")

    report_date = pd.to_datetime(df["report_date"], errors="coerce")
    fallback = pd.to_datetime(df["period_end"], errors="coerce") + pd.Timedelta(days=FILING_LAG_DAYS)
    default = pd.to_datetime((df["year"] + 1).astype(int).astype(str) + "-03-31")
    df["event_date"] = report_date.fillna(fallback).fillna(default)

    logger.info(
        "Final events: %d (report_date missing in %d, fallback used)",
        len(df), int(report_date.isna().sum()),
    )
    return df.reset_index(drop=True)


def load_abnormal_returns(events: pd.DataFrame):
    start = events["event_date"].min() - pd.Timedelta(days=60)
    prices = fetch_df(
        """
        SELECT ticker, price_date, adj_close_price, is_no_trade
        FROM daily_prices
        WHERE price_date >= %s;
        """,
        (start.date(),),
    )
    prices["price_date"] = pd.to_datetime(prices["price_date"])
    prices["adj_close_price"] = pd.to_numeric(prices["adj_close_price"], errors="coerce")

    close = prices.pivot(index="price_date", columns="ticker", values="adj_close_price").sort_index()
    no_trade = (
        prices.pivot(index="price_date", columns="ticker", values="is_no_trade")
        .reindex(close.index)
        .fillna(True)
        .astype(bool)
    )

    returns = close.ffill().pct_change(fill_method=None).clip(-RETURN_CLIP, RETURN_CLIP)
    market = returns.mean(axis=1)
    abnormal = returns.sub(market, axis=0)

    logger.info("Price matrix: %d trading days x %d tickers", *close.shape)
    return abnormal, no_trade


def build_event_matrix(events: pd.DataFrame, abnormal: pd.DataFrame, no_trade: pd.DataFrame):
    index = abnormal.index
    rows, kept = [], []
    skipped = {"no_prices": 0, "incomplete_window": 0, "illiquid": 0, "nan": 0}

    for i, ev in events.iterrows():
        ticker = ev["ticker"]
        if ticker not in abnormal.columns:
            skipped["no_prices"] += 1
            continue

        pos = index.searchsorted(ev["event_date"])  # first trading day on/after event date
        lo, hi = pos - PRE_DAYS, pos + POST_DAYS
        if lo < 0 or hi >= len(index):
            skipped["incomplete_window"] += 1
            continue

        if no_trade[ticker].iloc[lo:hi + 1].mean() > MAX_NO_TRADE:
            skipped["illiquid"] += 1
            continue

        window = abnormal[ticker].iloc[lo:hi + 1].to_numpy()
        if np.isnan(window).any():
            skipped["nan"] += 1
            continue

        rows.append(window)
        kept.append(i)

    logger.info("Events kept: %d | skipped: %s", len(kept), skipped)

    meta = events.loc[kept].reset_index(drop=True)
    car = np.cumsum(np.vstack(rows), axis=1)
    car = car - car[:, [PRE_DAYS - 1]]  # rebase so CAR = 0 at t = -1
    return meta, car


# ==========================================
# Charts
# ==========================================
def plot_event_study(meta: pd.DataFrame, car: np.ndarray) -> None:
    meta = meta.copy()
    meta["group"] = pd.qcut(meta["shock_z"], 3, labels=["Low shock", "Mid shock", "High shock"])
    days = np.arange(-PRE_DAYS, POST_DAYS + 1)
    colors = {"Low shock": "#c0392b", "Mid shock": "#7f8c8d", "High shock": "#1f6fb2"}

    fig, ax = plt.subplots(figsize=(10, 6))
    for name in ["Low shock", "Mid shock", "High shock"]:
        mask = (meta["group"] == name).to_numpy()
        data = car[mask] * 100
        mean = data.mean(axis=0)
        se = data.std(axis=0, ddof=1) / np.sqrt(mask.sum())
        ax.plot(days, mean, color=colors[name], linewidth=2, label=f"{name} (n={mask.sum()})")
        ax.fill_between(days, mean - 1.96 * se, mean + 1.96 * se, color=colors[name], alpha=0.15)

    ax.axvline(0, color="black", linestyle="--", linewidth=1)
    ax.axhline(0, color="black", linewidth=0.8)
    ax.set_xlabel("Trading days relative to annual report release (t = 0)")
    ax.set_ylabel("Cumulative abnormal return (%)")
    ax.set_title("Cumulative Abnormal Return Around Annual Report Release\nGrouped by Revenue Growth Shock Tercile")
    ax.legend(frameon=False)
    ax.grid(alpha=0.3)
    fig.text(
        0.01, 0.005,
        "Shock = YoY revenue growth, sector-year robust z-score. Abnormal return = stock return minus equal-weighted market. Band = 95% CI of the mean.",
        fontsize=7, color="gray",
    )
    fig.tight_layout(rect=(0, 0.02, 1, 1))
    fig.savefig(FIG_DIR / "event_study_car.png", dpi=200)


def plot_forward_returns(meta: pd.DataFrame, car: np.ndarray) -> None:
    fwd = (car[:, PRE_DAYS + HOLD_DAYS] - car[:, PRE_DAYS]) * 100  # close t=0 to close t=+HOLD
    data = meta.assign(fwd=fwd)

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(13, 5.5))

    # Quintile bars
    data["quintile"] = pd.qcut(data["shock_z"], 5, labels=["Q1\n(lowest)", "Q2", "Q3", "Q4", "Q5\n(highest)"])
    grp = data.groupby("quintile", observed=True)["fwd"]
    means, ses, counts = grp.mean(), grp.std() / np.sqrt(grp.count()), grp.count()
    ax1.bar(range(5), means.values, yerr=1.96 * ses.values, capsize=4, color="#1f6fb2", alpha=0.85)
    for x, (m, n) in enumerate(zip(means.values, counts.values)):
        ax1.text(x, 0, f"n={n}", ha="center", va="bottom" if m < 0 else "top", fontsize=8, color="white" if abs(m) > 1 else "black")
    ax1.set_xticks(range(5))
    ax1.set_xticklabels(means.index)
    ax1.axhline(0, color="black", linewidth=0.8)
    ax1.set_ylabel(f"Mean abnormal return, t=0 to t=+{HOLD_DAYS} (%)")
    ax1.set_title("Forward Abnormal Return by Shock Quintile")
    ax1.grid(axis="y", alpha=0.3)

    # Binned scatter
    data["decile"] = pd.qcut(data["shock_z"], 10, labels=False, duplicates="drop")
    binned = data.groupby("decile").agg(x=("shock_z", "mean"), y=("fwd", "mean"))
    ax2.scatter(data["shock_z"], data["fwd"], s=8, color="gray", alpha=0.25, label="Individual events")
    ax2.scatter(binned["x"], binned["y"], s=60, color="#c0392b", zorder=3, label="Decile mean")
    rho, pval = spearmanr(data["shock_z"], data["fwd"])
    ax2.axhline(0, color="black", linewidth=0.8)
    lo, hi = data["fwd"].quantile([0.02, 0.98])
    ax2.set_ylim(lo, hi)
    ax2.set_xlabel("Revenue shock (sector-year robust z-score)")
    ax2.set_ylabel(f"Abnormal return, t=0 to t=+{HOLD_DAYS} (%)")
    ax2.set_title(f"Shock vs Forward Return (Spearman = {rho:.3f}, p = {pval:.3f})")
    ax2.legend(frameon=False)
    ax2.grid(alpha=0.3)

    fig.tight_layout()
    fig.savefig(FIG_DIR / "forward_return_quintile_scatter.png", dpi=200)

    top = data.loc[data["quintile"] == data["quintile"].cat.categories[-1], "fwd"]
    bottom = data.loc[data["quintile"] == data["quintile"].cat.categories[0], "fwd"]
    t_stat, t_p = ttest_ind(top, bottom, equal_var=False)
    logger.info("Spearman IC: %.4f (p=%.4f)", rho, pval)
    logger.info("Q5 - Q1 spread: %.2f%% (Welch t=%.2f, p=%.4f)", top.mean() - bottom.mean(), t_stat, t_p)


# ==========================================
# Execution
# ==========================================
if __name__ == "__main__":
    FIG_DIR.mkdir(exist_ok=True)

    events = load_events()
    abnormal, no_trade = load_abnormal_returns(events)
    meta, car = build_event_matrix(events, abnormal, no_trade)

    plot_event_study(meta, car)
    plot_forward_returns(meta, car)

    logger.info("Figures saved to %s", FIG_DIR.resolve())
    plt.show()