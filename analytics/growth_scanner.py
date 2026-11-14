from __future__ import annotations

import sys
from pathlib import Path

# Mundur satu folder: dari /analytics ke /pead
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import history as hist
from core.config import DB_CONFIG as config

import argparse
import logging
import math
from statistics import mean, median, stdev
from typing import Any

import numpy as np
import pandas as pd
import psycopg2
from psycopg2.extras import execute_values

from core import history as hist
from core.config import DB_CONFIG as config



logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Quantitative & Econometric Constants
# ---------------------------------------------------------------------------

MAD_SCALE_FACTOR: float = 1.4826
Z_SCORE_THRESHOLD: float = 2.0
Z_SCORE_STRONG_THRESHOLD: float = 3.0
MIN_PEERS_PER_SECTOR: int = 5

# Time-series SUR thresholds mirror cross-sectional Z-score thresholds.
SUR_THRESHOLD: float = Z_SCORE_THRESHOLD
SUR_STRONG_THRESHOLD: float = Z_SCORE_STRONG_THRESHOLD

# Support both IDX XBRL taxonomy ("G. Financials") and legacy labels.
FINANCIAL_SECTOR: str = "G. Financials"
FINANCIAL_SECTOR_ALIASES: frozenset[str] = frozenset({
    "G. Financials",
    "Financial Services",
    "Financials",
})
UNKNOWN_SECTOR: str = "Unknown"

# Comparative baseline & restatement tracking.
USE_REPORTED_COMPARATIVE: bool = True
RESTATEMENT_TOLERANCE: float = 0.01
RESTATEMENT_MIN_ABS_IDR: float = 100_000_000.0  # Rp 100M floor against rounding noise

# Time-series SUR configuration.
# Allows graceful degradation down to MIN_SUR_OBSERVATIONS (2 prior YoY growth
# rates = 3 prior years of filings) when a full 4-year window is unavailable,
# applying small-sample studentization penalty sqrt((n + 1) / n).
SUR_LOOKBACK_YEARS: int = 4
MIN_SUR_OBSERVATIONS: int = 2
SUR_MIN_SIGMA: float = 0.10
SUR_MIN_SURPRISE: float = 0.10

# Growth clipping and IDR-normalized minimum revenue base floor.
# Raising the IDR floor from Rp 1B to Rp 10B (~$600k USD) eliminates dormant
# shell turnarounds while FX normalization preserves valid USD mid/large caps.
GROWTH_CLIP_LOW: float = -1.0
GROWTH_CLIP_HIGH: float = 3.0
MIN_BASE_REVENUE: float = 10_000_000_000.0  # Rp 10 Billion IDR equivalent
DEFAULT_USDIDR_RATE: float = 16_500.0

# Minimum scaled profit movement (25 bps of asset base) to avoid noisy
# CONFIRMED / DIVERGENT flips on negligible accounting changes.
PROFIT_DELTA_DEADBAND: float = 0.0025

# Cross-sectional Z-score peer coverage & historical fallback settings.
PEER_MIN_COVERAGE: float = 0.30
BENCHMARK_LOOKBACK_YEARS: int = 3
BENCHMARK_MIN_TICKERS_PER_YEAR: int = 5
BENCHMARK_MIN_YEARS: int = 2
MIN_DISPERSION_FLOOR: float = 0.02  # Minimum MAD-scaled dispersion floor (2 pp)

Z_SOURCE_PEER: str = "PEER"
Z_SOURCE_HIST: str = "HIST"

LINE_WIDTH: int = 176

INVALID_REVENUE_TAGS: frozenset[str] = frozenset({
    "not_found",
    "nil:SalesAndRevenue",
    "nil",
    "",
})

SCAN_RESULTS_DDL: str = """
    CREATE TABLE IF NOT EXISTS scan_results (
        ticker               text NOT NULL,
        year                 integer NOT NULL,
        period               text NOT NULL,
        sector               text,
        revenue_curr         numeric,
        revenue_prev         numeric,
        growth_yoy           numeric,
        sector_median_growth numeric,
        sector_peer_count    integer,
        z_score              numeric,
        z_source             text,
        sur                  numeric,
        sur_expected_growth  numeric,
        sur_sigma            numeric,
        status               text NOT NULL,
        trigger_source       text,
        direction            text,
        confirmation         text,
        basis                text,
        profit_delta_assets  numeric,
        gross_delta_assets   numeric,
        net_income_curr      numeric,
        net_income_prev      numeric,
        restated             boolean,
        note                 text,
        computed_at          timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (ticker, year, period)
    );
"""

SCAN_RESULTS_COLUMNS: list[str] = [
    "ticker", "year", "period", "sector", "revenue_curr", "revenue_prev", "growth_yoy",
    "sector_median_growth", "sector_peer_count", "z_score", "z_source", "sur", "sur_expected_growth",
    "sur_sigma", "status", "trigger_source", "direction", "confirmation", "basis",
    "profit_delta_assets", "gross_delta_assets", "net_income_curr", "net_income_prev",
    "restated", "note",
]

CANDIDATE_COLUMNS: list[str] = [
    "revenue",
    "revenue_prior",
    "net_income",
    "operating_profit",
    "gross_profit",
    "total_assets",
    "revenue_tag_source",
    "currency",
    "fx_rate_at_report",
    "rounding_level",
]


# ---------------------------------------------------------------------------
# Value & Normalization Helpers
# ---------------------------------------------------------------------------

def to_float(value: Any) -> float | None:
    """Safely converts a scalar to a finite Python float, returning None on NaN/Inf."""
    if value is None or pd.isna(value):
        return None
    try:
        f_val = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f_val):
        return None
    return f_val


def none_if_na(value: Any) -> Any:
    """Returns None if value is None, NaN, or +/-Inf; converts numpy scalars to native Python types."""
    if value is None or pd.isna(value):
        return None
    if isinstance(value, (float, np.floating)):
        f_val = float(value)
        return f_val if math.isfinite(f_val) else None
    if isinstance(value, (int, np.integer)):
        return int(value)
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    return value


def is_financial_sector(sector: str | None) -> bool:
    """Checks whether a sector string corresponds to the financial sector."""
    if not sector or not isinstance(sector, str):
        return False
    s_clean = sector.strip()
    return (
        s_clean in FINANCIAL_SECTOR_ALIASES
        or s_clean.startswith("G. Financial")
        or "financial" in s_clean.lower()
    )


def clip_growth(growth: float) -> float:
    """Clips raw YoY growth rate to [GROWTH_CLIP_LOW, GROWTH_CLIP_HIGH]."""
    return max(GROWTH_CLIP_LOW, min(GROWTH_CLIP_HIGH, float(growth)))


def resolve_fx_multiplier(currency: Any, fx_rate: Any) -> float:
    """
    Determines the IDR conversion multiplier for a filing row.
    IDR rows always use 1.0. Foreign currency (USD) rows use the reported FX
    rate if valid (> 100 IDR/USD), falling back to DEFAULT_USDIDR_RATE.
    """
    curr_str = str(currency).strip().upper() if currency is not None and not pd.isna(currency) else "IDR"
    if curr_str == "IDR" or curr_str == "":
        return 1.0
    rate = to_float(fx_rate)
    if rate is not None and rate > 100.0:
        return rate
    return DEFAULT_USDIDR_RATE


def detect_and_fix_scale_anomaly(stored_prev: float, reported_prior: float | None) -> tuple[float | None, bool]:
    """
    Detects 10^3, 10^6, or 10^9 XBRL rounding-unit mismatches between last year's
    stored revenue (e.g., IMJS or CDIA) and the current filing's comparative
    revenue_prior. Returns (reconciled_prior, had_unit_mismatch).
    """
    if reported_prior is None or reported_prior <= 0 or stored_prev <= 0:
        return reported_prior, False

    ratio = reported_prior / stored_prev
    log10_ratio = math.log10(ratio)
    nearest_pow3 = round(log10_ratio / 3.0) * 3
    if abs(nearest_pow3) in (3, 6, 9) and abs(log10_ratio - nearest_pow3) < 0.08:
        # Stored row or comparative had an unapplied Ribuan/Jutaan/Miliaran factor.
        # Prefer the value that sits in a realistic corporate revenue range (>= MIN_BASE_REVENUE).
        if reported_prior >= MIN_BASE_REVENUE > stored_prev:
            return reported_prior, True
        if stored_prev >= MIN_BASE_REVENUE > reported_prior:
            return stored_prev, True
        return max(stored_prev, reported_prior), True

    return reported_prior, False


def baseline_from_row(revenue_prev_row: float, revenue_prior_reported: Any) -> float:
    """Selects the comparative revenue baseline, guarding against scale unit corruption."""
    prev_f = float(revenue_prev_row)
    reported = to_float(revenue_prior_reported)
    reconciled_prior, _ = detect_and_fix_scale_anomaly(prev_f, reported)
    if USE_REPORTED_COMPARATIVE and reconciled_prior is not None and reconciled_prior > 0:
        return reconciled_prior
    return prev_f


def growth_from_row(
    revenue_curr: Any,
    revenue_prev_row: Any,
    revenue_prior_reported: Any,
    min_base: float = MIN_BASE_REVENUE,
) -> float | None:
    """Clipped growth for benchmark history; None when inputs are invalid or base < min_base."""
    curr_f = to_float(revenue_curr)
    prev_f = to_float(revenue_prev_row)
    if curr_f is None or prev_f is None or curr_f <= 0 or prev_f <= 0:
        return None
    baseline = baseline_from_row(prev_f, revenue_prior_reported)
    if baseline < min_base or baseline <= 0:
        return None
    return clip_growth((curr_f - baseline) / baseline)


def safe_load_reports(
    year_from: int,
    year_to: int,
    columns: list[str],
    period: str,
    tickers: list[str] | None = None,
) -> pd.DataFrame:
    """
    Loads financial reports via core.history while gracefully handling optional
    FX/currency columns if an older local parquet snapshot lacks them.
    """
    kwargs: dict[str, Any] = {
        "year_from": year_from,
        "year_to": year_to,
        "columns": columns,
        "period": period,
    }
    if tickers is not None:
        kwargs["tickers"] = tickers

    try:
        frame = hist.load_financial_reports(**kwargs)
    except Exception:
        fallback_cols = [
            c for c in columns
            if c not in ("currency", "fx_rate_at_report", "rounding_level")
        ]
        kwargs["columns"] = fallback_cols
        frame = hist.load_financial_reports(**kwargs)

    for optional_col, default_val in (
        ("currency", "IDR"),
        ("fx_rate_at_report", 1.0),
        ("rounding_level", None),
    ):
        if optional_col not in frame.columns:
            frame[optional_col] = default_val

    return frame


def normalize_monetary_columns_to_idr(frame: pd.DataFrame, monetary_cols: list[str]) -> pd.DataFrame:
    """
    Vectorized conversion of monetary columns into IDR so cross-period joins
    (e.g., ESTI/ANJT switching USD -> IDR) and MIN_BASE_REVENUE checks operate
    on a uniform currency basis. Also preserves raw reporting-currency values.
    """
    if frame.empty:
        return frame

    df = frame.copy()
    curr_series = df["currency"].fillna("IDR").astype(str).str.strip().str.upper()
    fx_series = pd.to_numeric(df["fx_rate_at_report"], errors="coerce")
    fx_mult = np.where(
        (curr_series == "IDR") | (curr_series == ""),
        1.0,
        np.where(fx_series > 100.0, fx_series, DEFAULT_USDIDR_RATE),
    )
    df["fx_multiplier"] = fx_mult

    for col in monetary_cols:
        if col in df.columns:
            raw_num = pd.to_numeric(df[col], errors="coerce")
            df[f"{col}_raw"] = raw_num
            df[col] = raw_num * fx_mult

    return df


# ---------------------------------------------------------------------------
# Data Access
# ---------------------------------------------------------------------------

def fetch_emitens() -> pd.DataFrame:
    """Fetches ticker metadata from emitens table."""
    return hist.run_query("SELECT ticker, sector, is_active FROM emitens;")


def fetch_active_sectors() -> dict[str, str]:
    """{ticker: sector} for active equity tickers (excluding composite index IHSG)."""
    frame = fetch_emitens()
    if frame.empty:
        return {}
    active_mask = frame["is_active"].fillna(False).astype(bool) & (frame["ticker"] != "IHSG")
    frame = frame.loc[active_mask]
    sectors_series = frame["sector"].fillna("").astype(str).str.strip()
    sectors_series = sectors_series.mask(sectors_series == "", UNKNOWN_SECTOR)
    return dict(zip(frame["ticker"].astype(str), sectors_series))


def valid_revenue_rows(frame: pd.DataFrame) -> pd.DataFrame:
    """Filters to rows with strictly positive revenue and a resolved, non-nil XBRL tag."""
    if frame.empty:
        return frame
    rev = pd.to_numeric(frame["revenue"], errors="coerce")
    tag = frame["revenue_tag_source"].fillna("not_found").astype(str).str.strip()
    mask = (
        (rev > 0)
        & (~tag.isin(INVALID_REVENUE_TAGS))
        & (~tag.str.startswith("nil:"))
    )
    return frame.loc[mask].copy()


def fetch_growth_candidates(year: int, period: str) -> list[dict[str, Any]]:
    """
    Pulls and vector-computes YoY revenue growth per active ticker for the given period.

    - Normalizes monetary figures to IDR before cross-year comparison to prevent
      false 16,500x growth/restatement spikes when issuers change reporting currency
      (e.g., ESTI, ANJT) and to evaluate MIN_BASE_REVENUE on a consistent IDR scale.
    - Detects and reconciles 10^3 / 10^6 XBRL rounding-unit mismatches (e.g., IMJS, CDIA).
    - Preserves reporting-currency revenue/net_income for display when currency is consistent,
      or uses IDR-normalized figures when reporting currency changed between years.
    """
    sectors = fetch_active_sectors()
    if not sectors:
        return []

    frame = safe_load_reports(
        year_from=year - 1,
        year_to=year,
        columns=CANDIDATE_COLUMNS,
        period=period,
    )
    if frame.empty:
        logger.info("Fetched 0 growth candidates for %s %s", period, year)
        return []

    frame = frame.loc[frame["ticker"].isin(sectors)].copy()
    monetary_cols = [
        "revenue",
        "revenue_prior",
        "net_income",
        "operating_profit",
        "gross_profit",
        "total_assets",
    ]
    frame = normalize_monetary_columns_to_idr(frame, monetary_cols)

    def prepare_side(target_year: int) -> pd.DataFrame:
        part = frame.loc[frame["year"] == target_year].drop(columns=["year", "period"], errors="ignore")
        part = part.sort_values("ticker").drop_duplicates(subset=["ticker"], keep="last")
        return part.set_index("ticker")

    curr_df = prepare_side(year)
    prev_df = prepare_side(year - 1)
    if curr_df.empty or prev_df.empty:
        logger.info("Fetched 0 growth candidates for %s %s", period, year)
        return []

    merged = curr_df.join(prev_df, how="inner", lsuffix="_curr", rsuffix="_prev")

    tag_curr = merged["revenue_tag_source_curr"].fillna("not_found").astype(str).str.strip()
    tag_prev = merged["revenue_tag_source_prev"].fillna("not_found").astype(str).str.strip()

    valid_mask = (
        (merged["revenue_curr"] > 0)
        & (merged["revenue_prev"] > 0)
        & (~tag_curr.isin(INVALID_REVENUE_TAGS))
        & (~tag_prev.isin(INVALID_REVENUE_TAGS))
        & (~tag_curr.str.startswith("nil:"))
        & (~tag_prev.str.startswith("nil:"))
    )
    merged = merged.loc[valid_mask].sort_index()
    if merged.empty:
        logger.info("Fetched 0 growth candidates for %s %s", period, year)
        return []

    # Vectorized baseline & scale-mismatch reconciliation in IDR terms
    rev_curr_idr = merged["revenue_curr"].to_numpy(dtype=float)
    rev_prev_idr = merged["revenue_prev"].to_numpy(dtype=float)
    rev_prior_idr = merged["revenue_prior_curr"].to_numpy(dtype=float)

    has_prior = np.isfinite(rev_prior_idr) & (rev_prior_idr > 0)
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = np.where(has_prior & (rev_prev_idr > 0), rev_prior_idr / rev_prev_idr, 1.0)
        log10_ratio = np.where(ratio > 0, np.log10(ratio), 0.0)
        nearest_pow3 = np.round(log10_ratio / 3.0) * 3.0
        scale_glitch = (
            has_prior
            & np.isin(np.abs(nearest_pow3), [3.0, 6.0, 9.0])
            & (np.abs(log10_ratio - nearest_pow3) < 0.08)
        )

    reconciled_prior_idr = np.where(
        scale_glitch,
        np.maximum(rev_prev_idr, rev_prior_idr),
        rev_prior_idr,
    )

    abs_diff_idr = np.abs(reconciled_prior_idr - rev_prev_idr)
    restated_arr = (
        has_prior
        & (~scale_glitch)
        & (abs_diff_idr > RESTATEMENT_TOLERANCE * rev_prev_idr)
        & (abs_diff_idr >= RESTATEMENT_MIN_ABS_IDR)
    )

    use_comp = USE_REPORTED_COMPARATIVE & has_prior
    baseline_idr = np.where(use_comp, reconciled_prior_idr, rev_prev_idr)
    raw_growth_arr = (rev_curr_idr - baseline_idr) / baseline_idr
    scoring_growth_arr = np.clip(raw_growth_arr, GROWTH_CLIP_LOW, GROWTH_CLIP_HIGH)
    low_base_arr = baseline_idr < MIN_BASE_REVENUE

    # Preserve native reporting-currency numbers when currency is unchanged and free of scale glitches
    same_currency = (
        merged["currency_curr"].fillna("IDR").astype(str).str.upper()
        == merged["currency_prev"].fillna("IDR").astype(str).str.upper()
    ).to_numpy()
    use_native_display = same_currency & (~scale_glitch)
    fx_curr_arr = merged["fx_multiplier_curr"].to_numpy(dtype=float)

    disp_rev_curr = np.where(use_native_display, merged["revenue_raw_curr"].to_numpy(dtype=float), rev_curr_idr)
    disp_rev_prev = np.where(use_native_display, baseline_idr / fx_curr_arr, baseline_idr)
    disp_ni_curr = np.where(use_native_display, merged["net_income_raw_curr"].to_numpy(dtype=float), merged["net_income_curr"].to_numpy(dtype=float))
    disp_ni_prev = np.where(use_native_display, merged["net_income_raw_prev"].to_numpy(dtype=float), merged["net_income_prev"].to_numpy(dtype=float))

    tickers_idx = merged.index.to_list()
    pretax_curr_arr = merged["operating_profit_curr"].to_numpy(dtype=float)
    pretax_prev_arr = merged["operating_profit_prev"].to_numpy(dtype=float)
    ni_curr_idr_arr = merged["net_income_curr"].to_numpy(dtype=float)
    ni_prev_idr_arr = merged["net_income_prev"].to_numpy(dtype=float)
    gross_curr_arr = merged["gross_profit_curr"].to_numpy(dtype=float)
    gross_prev_arr = merged["gross_profit_prev"].to_numpy(dtype=float)
    assets_curr_arr = merged["total_assets_curr"].to_numpy(dtype=float)
    assets_prev_arr = merged["total_assets_prev"].to_numpy(dtype=float)

    candidates: list[dict[str, Any]] = []
    for i, ticker in enumerate(tickers_idx):
        candidates.append({
            "ticker": ticker,
            "sector": sectors.get(ticker, UNKNOWN_SECTOR),
            "revenue_curr": to_float(disp_rev_curr[i]),
            "revenue_prev": to_float(disp_rev_prev[i]),
            "revenue_curr_idr": to_float(rev_curr_idr[i]),
            "revenue_prev_idr": to_float(baseline_idr[i]),
            "restated": bool(restated_arr[i]),
            "scale_reconciled": bool(scale_glitch[i]),
            "currency_switched": not bool(same_currency[i]),
            "growth_yoy": float(raw_growth_arr[i]),
            "scoring_growth": float(scoring_growth_arr[i]),
            "low_base": bool(low_base_arr[i]),
            "net_income_curr": to_float(disp_ni_curr[i]),
            "net_income_prev": to_float(disp_ni_prev[i]),
            "net_income_curr_idr": to_float(ni_curr_idr_arr[i]),
            "net_income_prev_idr": to_float(ni_prev_idr_arr[i]),
            "pretax_curr": to_float(pretax_curr_arr[i]),
            "pretax_prev": to_float(pretax_prev_arr[i]),
            "gross_curr": to_float(gross_curr_arr[i]),
            "gross_prev": to_float(gross_prev_arr[i]),
            "assets_curr": to_float(assets_curr_arr[i]),
            "assets_prev": to_float(assets_prev_arr[i]),
        })

    logger.info("Fetched %d growth candidates for %s %s", len(candidates), period, year)
    return candidates


def fetch_revenue_history(year: int, period: str, tickers: list[str]) -> dict[str, dict[int, dict[str, float | None]]]:
    """
    Returns {ticker: {year: {"revenue": rev_idr, "revenue_prior": prior_idr}}}
    for the same period across the years needed to build prior YoY growth rates
    for the time-series SUR, normalized to IDR.
    """
    if not tickers:
        return {}

    first_year = year - SUR_LOOKBACK_YEARS - 1
    frame = safe_load_reports(
        tickers=tickers,
        year_from=first_year,
        year_to=year - 1,
        columns=["revenue", "revenue_prior", "revenue_tag_source", "currency", "fx_rate_at_report"],
        period=period,
    )
    frame = valid_revenue_rows(frame)
    if frame.empty:
        return {}

    frame = normalize_monetary_columns_to_idr(frame, ["revenue", "revenue_prior"])
    frame = frame.sort_values(["ticker", "year"]).drop_duplicates(subset=["ticker", "year"], keep="last")

    history: dict[str, dict[int, dict[str, float | None]]] = {}
    for row in frame[["ticker", "year", "revenue", "revenue_prior"]].itertuples(index=False):
        rev_val = to_float(row.revenue)
        if rev_val is None or rev_val <= 0:
            continue
        history.setdefault(str(row.ticker), {})[int(row.year)] = {
            "revenue": rev_val,
            "revenue_prior": to_float(row.revenue_prior),
        }
    return history


def fetch_ticker_diagnostic_info(year: int, period: str, tickers: list[str]) -> dict[str, dict[str, Any]]:
    """Fetches raw diagnostic fields for tickers that could not be scored as candidates."""
    if not tickers:
        return {}

    emitens = fetch_emitens()
    raw_sector = {
        str(t): none_if_na(s)
        for t, s in zip(emitens["ticker"], emitens["sector"])
    }

    frame = safe_load_reports(
        tickers=tickers,
        year_from=year - 1,
        year_to=year,
        columns=["revenue", "revenue_tag_source", "net_income"],
        period=period,
    )
    by_key = {(str(r.ticker), int(r.year)): r for r in frame.itertuples(index=False)}

    info: dict[str, dict[str, Any]] = {}
    for ticker in tickers:
        curr = by_key.get((ticker, year))
        prev = by_key.get((ticker, year - 1))
        info[ticker] = {
            "sector": raw_sector.get(ticker),
            "revenue_curr": to_float(curr.revenue) if curr is not None else None,
            "tag_source_curr": none_if_na(curr.revenue_tag_source) if curr is not None else None,
            "revenue_prev": to_float(prev.revenue) if prev is not None else None,
            "tag_source_prev": none_if_na(prev.revenue_tag_source) if prev is not None else None,
            "net_income_curr": to_float(curr.net_income) if curr is not None else None,
            "net_income_prev": to_float(prev.net_income) if prev is not None else None,
        }
    return info


def fetch_sector_history(year: int, period: str) -> tuple[dict[str, dict[int, list[float]]], dict[str, int]]:
    """
    Returns (history, expected_counts) using vectorized IDR-normalized growth rates.
    """
    first_year = year - BENCHMARK_LOOKBACK_YEARS
    sectors = fetch_active_sectors()
    if not sectors:
        return {}, {}

    frame = safe_load_reports(
        year_from=first_year - 1,
        year_to=year - 1,
        columns=["revenue", "revenue_prior", "revenue_tag_source", "currency", "fx_rate_at_report"],
        period=period,
    )
    frame = valid_revenue_rows(frame)
    frame = frame.loc[frame["ticker"].isin(sectors)].copy()
    if frame.empty:
        return {}, {}

    frame = normalize_monetary_columns_to_idr(frame, ["revenue", "revenue_prior"])
    frame = frame.sort_values(["ticker", "year"]).drop_duplicates(subset=["ticker", "year"], keep="last")
    frame["sector"] = frame["ticker"].map(sectors)

    last_year = frame.loc[frame["year"] == year - 1]
    expected_counts: dict[str, int] = (
        last_year.groupby("sector")["ticker"].nunique().to_dict()
        if not last_year.empty
        else {}
    )

    prev = frame[["ticker", "year", "revenue"]].rename(columns={"revenue": "revenue_prev"})
    prev["year"] = prev["year"] + 1
    merged = frame.merge(prev, on=["ticker", "year"], how="inner")
    merged = merged.loc[merged["year"] >= first_year]
    if merged.empty:
        return {}, expected_counts

    history: dict[str, dict[int, list[float]]] = {}
    for row in merged[["sector", "year", "revenue", "revenue_prev", "revenue_prior"]].itertuples(index=False):
        growth = growth_from_row(row.revenue, row.revenue_prev, row.revenue_prior)
        if growth is None:
            continue
        history.setdefault(str(row.sector), {}).setdefault(int(row.year), []).append(growth)

    return history, expected_counts


# ---------------------------------------------------------------------------
# Quantitative Scoring (Cross-Sectional Robust Z-Score & Time-Series SUR)
# ---------------------------------------------------------------------------

def _robust_dispersion(values: list[float], center: float) -> float:
    """
    Computes MAD * 1.4826 dispersion around `center`. If MAD collapses to 0.0
    (e.g., clustered growth rates), falls back to scaled IQR (IQR / 1.349),
    floored at MIN_DISPERSION_FLOOR if non-zero variation exists.
    """
    deviations = [abs(v - center) for v in values]
    mad = median(deviations)
    if mad > 0:
        return max(MAD_SCALE_FACTOR * mad, MIN_DISPERSION_FLOOR)

    arr = np.asarray(values, dtype=float)
    if arr.size >= 2 and float(np.ptp(arr)) > 0:
        q75, q25 = np.percentile(arr, [75, 25])
        iqr_scale = float(q75 - q25) / 1.349
        if iqr_scale > 0:
            return max(iqr_scale, MIN_DISPERSION_FLOOR)
        sample_sd = float(np.std(arr, ddof=1))
        if sample_sd > 0:
            return max(sample_sd, MIN_DISPERSION_FLOOR)

    return 0.0


def build_sector_benchmarks(history: dict[str, dict[int, list[float]]]) -> dict[str, dict[str, Any]]:
    """
    Builds {sector: {"center", "scale", "years"}} from prior years.
    Uses pooled yearly-centered absolute deviations so macroeconomic shifts in
    sector median across years do not artificially inflate cross-sectional scale.
    """
    benchmarks: dict[str, dict[str, Any]] = {}
    for sector, by_year in history.items():
        if sector == UNKNOWN_SECTOR:
            continue

        usable = {
            y: values
            for y, values in by_year.items()
            if len(values) >= BENCHMARK_MIN_TICKERS_PER_YEAR
        }
        if len(usable) < BENCHMARK_MIN_YEARS:
            continue

        yearly_medians = [median(values) for values in usable.values()]
        deviations: list[float] = []
        for values in usable.values():
            year_med = median(values)
            deviations.extend(abs(g - year_med) for g in values)

        mad = median(deviations)
        if mad > 0:
            scale = max(MAD_SCALE_FACTOR * mad, MIN_DISPERSION_FLOOR)
        else:
            dev_arr = np.asarray(deviations, dtype=float)
            if dev_arr.size >= 2 and float(np.max(dev_arr)) > 0:
                scale = max(float(np.percentile(dev_arr, 75)) * MAD_SCALE_FACTOR, MIN_DISPERSION_FLOOR)
            else:
                continue

        benchmarks[sector] = {
            "center": median(yearly_medians),
            "scale": scale,
            "years": len(usable),
        }
    return benchmarks


def compute_robust_z_scores(
    candidates: list[dict[str, Any]],
    benchmarks: dict[str, dict[str, Any]] | None = None,
    expected_counts: dict[str, int] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """
    Scores each non-low-base ticker against its sector peers (PEER) when peer
    count >= MIN_PEERS_PER_SECTOR and filing coverage >= PEER_MIN_COVERAGE,
    falling back to multi-year sector history (HIST) when early-filer coverage
    is too thin.
    """
    benchmarks = benchmarks or {}

    by_sector: dict[str, list[dict[str, Any]]] = {}
    for c in candidates:
        if c.get("low_base"):
            continue
        by_sector.setdefault(c["sector"], []).append(c)

    results: list[dict[str, Any]] = []
    sector_skip_reasons: dict[str, str] = {}

    for sector, group in by_sector.items():
        if sector == UNKNOWN_SECTOR:
            sector_skip_reasons[sector] = "sector not classified in emitens"
            continue

        peers = len(group)
        expected = (expected_counts or {}).get(sector)
        coverage = None if not expected else peers / expected

        peer_ok = peers >= MIN_PEERS_PER_SECTOR
        if expected_counts is not None:
            peer_ok = peer_ok and coverage is not None and coverage >= PEER_MIN_COVERAGE

        center: float | None = None
        scale: float | None = None
        source: str | None = None
        peer_reason: str | None = None

        if peer_ok:
            growth_values = [float(c["scoring_growth"]) for c in group]
            sector_median = median(growth_values)
            disp = _robust_dispersion(growth_values, sector_median)
            if disp > 0:
                center, scale, source = sector_median, disp, Z_SOURCE_PEER
            else:
                peer_reason = "no variation among peers (MAD=0)"
        else:
            if peers < MIN_PEERS_PER_SECTOR:
                peer_reason = f"only {peers} peers (minimum {MIN_PEERS_PER_SECTOR})"
            elif coverage is None:
                peer_reason = f"{peers} peers, expected filing count unknown"
            else:
                peer_reason = (
                    f"{peers}/{expected} peers filed ({coverage * 100:.0f}%, "
                    f"minimum {PEER_MIN_COVERAGE * 100:.0f}%)"
                )

        if source is None:
            benchmark = benchmarks.get(sector)
            if benchmark is not None and benchmark.get("scale", 0) > 0:
                center = float(benchmark["center"])
                scale = float(benchmark["scale"])
                source = Z_SOURCE_HIST
                sector_skip_reasons[sector] = f"z vs {benchmark['years']}y sector history; {peer_reason}"
            else:
                sector_skip_reasons[sector] = f"{peer_reason}; no usable sector history"
                continue

        assert center is not None and scale is not None and scale > 0
        for c in group:
            results.append({
                **c,
                "sector_median_growth": center,
                "sector_peer_count": peers,
                "z_score": (float(c["scoring_growth"]) - center) / scale,
                "z_source": source,
            })

    return results, sector_skip_reasons


def classify_score(value: float) -> str:
    """Classifies absolute Z-score or SUR magnitude into STRONG, MODERATE, or WEAK."""
    abs_value = abs(value)
    if abs_value >= Z_SCORE_STRONG_THRESHOLD:
        return "STRONG"
    if abs_value >= Z_SCORE_THRESHOLD:
        return "MODERATE"
    return "WEAK"


def classify_signal(z_score: float) -> str:
    """Alias for classify_score preserved for backward compatibility."""
    return classify_score(z_score)


def combine_scores(z_score: float | None, sur: float | None) -> tuple[str, str, str]:
    """
    Merges cross-sectional Z-score and time-series SUR into (status, trigger_source, direction).
    """
    available = {
        name: float(value)
        for name, value in (("Z", z_score), ("SUR", sur))
        if value is not None and math.isfinite(float(value))
    }
    if not available:
        return "NO SIGNAL", "--", "--"

    strongest = max(available.values(), key=abs)
    status = classify_score(strongest)

    triggering = {name: v for name, v in available.items() if abs(v) >= Z_SCORE_THRESHOLD}
    trigger_source = "+".join(triggering) if triggering else "--"

    basis_values = list(triggering.values()) if triggering else [strongest]
    signs = {v > 0 for v in basis_values}
    if len(signs) > 1:
        direction = "MIXED"
    else:
        direction = "POSITIVE" if signs.pop() else "NEGATIVE"

    return status, trigger_source, direction


def _extract_year_entry(entry: Any) -> tuple[float | None, float | None]:
    """Supports both dict entries {'revenue': ..., 'revenue_prior': ...} and plain float values."""
    if entry is None:
        return None, None
    if isinstance(entry, dict):
        return to_float(entry.get("revenue")), to_float(entry.get("revenue_prior"))
    return to_float(entry), None


def compute_sur(
    current_growth: float,
    ticker_history: dict[int, Any],
    year: int,
) -> dict[str, Any] | None:
    """
    Time-series Standardized Unexpected Revenue (SUR) for one ticker.

    Improvements over legacy implementation:
    1. Respects restated comparative baselines (`revenue_prior`) and scale-glitch
       guards when computing historical same-period YoY growth rates.
    2. Enforces MIN_BASE_REVENUE on historical baselines so dormant prior years
       do not inject +300% noise into the expectation window.
    3. Supports between MIN_SUR_OBSERVATIONS (2) and SUR_LOOKBACK_YEARS (4)
       valid prior growth rates and applies a small-sample prediction-interval
       adjustment `sigma * sqrt((n + 1) / n)` to prevent small-N over-signaling.
    """
    if not ticker_history:
        return None

    prior_growths: list[float] = []
    for k in range(1, SUR_LOOKBACK_YEARS + 1):
        rev_end, prior_reported = _extract_year_entry(ticker_history.get(year - k))
        rev_start, _ = _extract_year_entry(ticker_history.get(year - k - 1))

        if rev_end is None or rev_end <= 0:
            continue
        # If year - k - 1 is missing as a standalone filing row, allow comparative
        # revenue_prior reported inside the year - k filing to serve as baseline.
        if (rev_start is None or rev_start <= 0) and prior_reported is not None and prior_reported >= MIN_BASE_REVENUE:
            g = clip_growth((rev_end - prior_reported) / prior_reported)
            prior_growths.append(g)
            continue

        g = growth_from_row(rev_end, rev_start, prior_reported)
        if g is not None:
            prior_growths.append(g)

    n_obs = len(prior_growths)
    if n_obs < MIN_SUR_OBSERVATIONS:
        return None

    expected = mean(prior_growths)
    raw_sd = stdev(prior_growths) if n_obs >= 2 else SUR_MIN_SIGMA
    # Studentized forecast-error scaling sqrt((n + 1) / n) for small sample sizes
    adjusted_sd = raw_sd * math.sqrt((n_obs + 1.0) / n_obs)
    sigma = max(adjusted_sd, SUR_MIN_SIGMA)
    surprise = float(current_growth) - expected

    return {
        "sur": surprise / sigma,
        "expected_growth": expected,
        "sigma": sigma,
        "surprise": surprise,
        "n_obs": n_obs,
        "valid": abs(surprise) >= SUR_MIN_SURPRISE,
    }


def gate_sur(sur_result: dict[str, Any] | None) -> float | None:
    """Caps SUR below SUR_THRESHOLD when absolute surprise is below SUR_MIN_SURPRISE."""
    if sur_result is None:
        return None
    sur = float(sur_result["sur"])
    if sur_result.get("valid", False):
        return sur
    cap = SUR_THRESHOLD * 0.99
    return max(-cap, min(cap, sur))


def _resolve_asset_scale(c: dict[str, Any]) -> tuple[float | None, str]:
    """
    Determines the asset denominator for scaling profit deltas:
    - Uses `max(assets_prev, 0.5 * (assets_curr + assets_prev))` when `assets_curr`
      is available so massive current-period M&A / rights-issue asset injections
      (e.g., MKNT 586x asset jump, INET 22.6x jump) do not inflate `dPft/TA`.
    - Falls back to prior-year IDR revenue when `total_assets` is NaN (such as the
      224 insurance rows in `G. Financials`).
    """
    assets_prev = to_float(c.get("assets_prev"))
    assets_curr = to_float(c.get("assets_curr"))

    if assets_prev is not None and assets_prev > 0:
        if assets_curr is not None and assets_curr > assets_prev * 1.5:
            avg_assets = 0.5 * (assets_prev + assets_curr)
            return avg_assets, "average total assets"
        return assets_prev, "prior-year assets"

    if assets_curr is not None and assets_curr > 0:
        return assets_curr, "current-year assets"

    rev_prev_idr = to_float(c.get("revenue_prev_idr")) or to_float(c.get("revenue_prev"))
    if rev_prev_idr is not None and rev_prev_idr > 0:
        return rev_prev_idr, "prior-year revenue (assets unavailable)"

    return None, ""


def compute_profit_confirmation(c: dict[str, Any]) -> tuple[str, str, str, float | None]:
    """
    Evaluates whether bottom-line/operating profit confirms top-line revenue growth.
    Uses NET_INC for all financial sector tickers (`G. Financials`) and PRETAX
    (`operating_profit`) for non-financial tickers when available.
    """
    denom, denom_label = _resolve_asset_scale(c)
    if denom is None or denom <= 0:
        return "UNCONFIRMED", "--", "prior-year total assets unavailable", None

    fin_sector = is_financial_sector(c.get("sector"))
    pretax_curr = to_float(c.get("pretax_curr"))
    pretax_prev = to_float(c.get("pretax_prev"))
    ni_curr = to_float(c.get("net_income_curr_idr"))
    if ni_curr is None:
        ni_curr = to_float(c.get("net_income_curr"))
    ni_prev = to_float(c.get("net_income_prev_idr"))
    if ni_prev is None:
        ni_prev = to_float(c.get("net_income_prev"))

    if not fin_sector and pretax_curr is not None and pretax_prev is not None:
        basis, curr, prev = "PRETAX", pretax_curr, pretax_prev
    elif ni_curr is not None and ni_prev is not None:
        basis, curr, prev = "NET_INC", ni_curr, ni_prev
    else:
        return "UNCONFIRMED", "--", "profit data unavailable for one or both periods", None

    delta_scaled = (curr - prev) / denom
    # Only populate `profit_delta_assets` numeric column when scaled by actual assets
    reported_delta = delta_scaled if "assets" in denom_label and "unavailable" not in denom_label else None
    change_text = f"{delta_scaled * 100:+.2f}% of {denom_label}"

    # Deadband check for negligible profit changes when sign does not flip
    is_flat = abs(delta_scaled) < PROFIT_DELTA_DEADBAND and ((curr > 0) == (prev > 0))

    if curr > 0 and prev > 0:
        if curr >= prev or is_flat:
            return "CONFIRMED", basis, f"profit grew or held steady ({change_text})", reported_delta
        return "DIVERGENT", basis, f"profit fell ({change_text})", reported_delta

    if curr > 0 and prev <= 0:
        return "CONFIRMED", basis, f"turnaround from loss to profit ({change_text})", reported_delta

    if curr <= 0 and prev <= 0:
        if curr > prev and not is_flat:
            return "RECOVERING", basis, f"loss narrowed but still loss-making ({change_text})", reported_delta
        return "DIVERGENT", basis, f"loss widened or unchanged ({change_text})", reported_delta

    return "DIVERGENT", basis, f"profit turned to loss ({change_text})", reported_delta


def compute_gross_profit_change(c: dict[str, Any]) -> float | None:
    """Change in gross profit scaled by prior-year (or average) total assets."""
    denom, denom_label = _resolve_asset_scale(c)
    if denom is None or denom <= 0 or "unavailable" in denom_label:
        return None
    gross_curr = to_float(c.get("gross_curr"))
    gross_prev = to_float(c.get("gross_prev"))
    if gross_curr is None or gross_prev is None:
        return None
    return (gross_curr - gross_prev) / denom


def empty_row(
    ticker: str,
    sector: str | None,
    note: str,
    info: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Constructs a default diagnostic row for unscorable tickers."""
    info = info or {}
    return {
        "ticker": ticker,
        "sector": sector,
        "revenue_curr": info.get("revenue_curr"),
        "revenue_prev": info.get("revenue_prev"),
        "net_income_curr": info.get("net_income_curr"),
        "net_income_prev": info.get("net_income_prev"),
        "growth_yoy": None,
        "sector_median_growth": None,
        "sector_peer_count": None,
        "z_score": None,
        "z_source": None,
        "sur": None,
        "sur_expected_growth": None,
        "sur_sigma": None,
        "status": "NO SIGNAL",
        "trigger_source": "--",
        "direction": "--",
        "confirmation": "--",
        "basis": "--",
        "profit_delta_assets": None,
        "gross_delta_assets": None,
        "restated": False,
        "note": note,
    }


def build_diagnostic_report(year: int, period: str, tickers: list[str]) -> list[dict[str, Any]]:
    """
    Produces one complete diagnostic row per requested ticker.
    """
    candidates = fetch_growth_candidates(year, period)
    history, expected_counts = fetch_sector_history(year, period)
    benchmarks = build_sector_benchmarks(history)
    scored, sector_skip_reasons = compute_robust_z_scores(candidates, benchmarks, expected_counts)

    candidate_by_ticker = {c["ticker"]: c for c in candidates}
    z_by_ticker = {r["ticker"]: r for r in scored}

    sector_counts: dict[str, int] = {}
    for c in candidates:
        if not c["low_base"]:
            sector_counts[c["sector"]] = sector_counts.get(c["sector"], 0) + 1

    with_candidate = [t for t in tickers if t in candidate_by_ticker]
    revenue_history = fetch_revenue_history(year, period, with_candidate)
    missing_tickers = [t for t in tickers if t not in candidate_by_ticker]
    diagnostic_info = fetch_ticker_diagnostic_info(year, period, missing_tickers)

    report_rows: list[dict[str, Any]] = []

    for ticker in tickers:
        c = candidate_by_ticker.get(ticker)

        if c is None:
            info = diagnostic_info.get(ticker, {})
            sector = info.get("sector")
            rev_c = info.get("revenue_curr")
            rev_p = info.get("revenue_prev")
            tag_c = str(info.get("tag_source_curr") or "")
            tag_p = str(info.get("tag_source_prev") or "")

            if rev_c is None:
                note = f"no {period} {year} data ingested yet"
            elif tag_c in INVALID_REVENUE_TAGS or tag_c.startswith("nil:"):
                note = "revenue tag not resolved or nil for current period"
            elif rev_c < 0:
                note = "negative operating revenue in current period"
            elif rev_p is None:
                note = f"no baseline data for {period} {year - 1} (new listing or gap)"
            elif tag_p in INVALID_REVENUE_TAGS or tag_p.startswith("nil:"):
                note = "revenue tag not resolved or nil for prior-year baseline"
            elif rev_p < 0:
                note = "negative operating revenue in prior-year baseline"
            else:
                note = "growth could not be computed (zero revenue or inactive ticker)"

            report_rows.append(empty_row(ticker, sector, note, info))
            continue

        z_row = z_by_ticker.get(ticker)
        z_score = z_row["z_score"] if z_row else None

        if c["low_base"]:
            sur_result = None
        else:
            sur_result = compute_sur(c["scoring_growth"], revenue_history.get(ticker, {}), year)
        sur = sur_result["sur"] if sur_result else None

        status, trigger_source, direction = combine_scores(z_score, gate_sur(sur_result))
        confirmation, basis, detail, profit_delta = compute_profit_confirmation(c)

        notes = [detail]
        if c["low_base"]:
            notes.append(
                f"prior revenue below IDR {MIN_BASE_REVENUE / 1e9:.0f} bn eqv, growth not scored"
            )
        elif z_score is None:
            reason = sector_skip_reasons.get(c["sector"], "unspecified")
            notes.append(f"no z-score: {reason}")
        elif z_row is not None and z_row["z_source"] == Z_SOURCE_HIST:
            notes.append(sector_skip_reasons.get(c["sector"], "z vs sector history"))

        if not c["low_base"]:
            if sur is None:
                notes.append(
                    f"no SUR: needs at least {MIN_SUR_OBSERVATIONS} prior YoY {period} observations"
                )
            elif not sur_result["valid"]:
                notes.append(
                    f"SUR gap below {SUR_MIN_SURPRISE * 100:.0f} pp, not counted"
                )

        if c.get("restated"):
            notes.append("comparative revenue differs from prior filing")
        if c.get("scale_reconciled"):
            notes.append("prior unit scale mismatch auto-reconciled")
        if c.get("currency_switched"):
            notes.append("reporting currency changed; evaluated in IDR")

        report_rows.append({
            "ticker": ticker,
            "sector": c["sector"],
            "revenue_curr": c["revenue_curr"],
            "revenue_prev": c["revenue_prev"],
            "net_income_curr": c["net_income_curr"],
            "net_income_prev": c["net_income_prev"],
            "growth_yoy": c["growth_yoy"],
            "sector_median_growth": z_row["sector_median_growth"] if z_row else None,
            "sector_peer_count": sector_counts.get(c["sector"]),
            "z_score": z_score,
            "z_source": z_row["z_source"] if z_row else None,
            "sur": sur,
            "sur_expected_growth": sur_result["expected_growth"] if sur_result else None,
            "sur_sigma": sur_result["sigma"] if sur_result else None,
            "status": status,
            "trigger_source": trigger_source,
            "direction": direction,
            "confirmation": confirmation,
            "basis": basis,
            "profit_delta_assets": profit_delta,
            "gross_delta_assets": compute_gross_profit_change(c),
            "restated": c["restated"],
            "note": " | ".join(notes),
        })

    return report_rows


# ---------------------------------------------------------------------------
# Database Persistence (PostgreSQL Batch Upsert)
# ---------------------------------------------------------------------------

def ensure_scan_results_table() -> None:
    """Ensures scan_results table and schema migrations exist in PostgreSQL."""
    if not hist.database_available():
        logger.warning("Database not reachable, scan_results table was not checked")
        return

    with psycopg2.connect(**config) as conn:
        with conn.cursor() as cur:
            cur.execute(SCAN_RESULTS_DDL)
            cur.execute("ALTER TABLE scan_results ADD COLUMN IF NOT EXISTS z_source text;")
        conn.commit()


def save_scan_results(rows: list[dict[str, Any]], year: int, period: str) -> None:
    """
    Batch-upserts diagnostic scan rows into scan_results inside a single database
    connection, sanitizing NaN/Inf/NumPy types before calling execute_values.
    """
    if not rows:
        return

    if not hist.database_available():
        logger.warning(
            "Database not reachable, %d scan result(s) for %s %s were not saved",
            len(rows), period, year,
        )
        return

    values: list[tuple[Any, ...]] = []
    for r in rows:
        record = {**r, "year": int(year), "period": str(period)}
        row_tuple = []
        for col in SCAN_RESULTS_COLUMNS:
            val = record.get(col)
            # Pastikan kolom restated dikirim sebagai bool murni Python (True/False)
            if col == "restated":
                row_tuple.append(bool(val) if val is not None and not pd.isna(val) else False)
            else:
                row_tuple.append(none_if_na(val))
        values.append(tuple(row_tuple))

    columns_sql = ", ".join(SCAN_RESULTS_COLUMNS)
    update_sql = ", ".join(
        f"{c} = EXCLUDED.{c}"
        for c in SCAN_RESULTS_COLUMNS
        if c not in ("ticker", "year", "period")
    )
    
    # Template dengan explicit cast ::boolean untuk kolom restated
    template_cols = []
    for col in SCAN_RESULTS_COLUMNS:
        if col == "restated":
            template_cols.append("%s::boolean")
        else:
            template_cols.append("%s")
    template = "(" + ", ".join(template_cols) + ")"

    try:
        with psycopg2.connect(**config) as conn:
            with conn.cursor() as cur:
                cur.execute(SCAN_RESULTS_DDL)
                cur.execute("ALTER TABLE scan_results ADD COLUMN IF NOT EXISTS z_source text;")
                execute_values(
                    cur,
                    f"""
                    INSERT INTO scan_results ({columns_sql}) VALUES %s
                    ON CONFLICT (ticker, year, period)
                    DO UPDATE SET {update_sql}, computed_at = now();
                    """,
                    values,
                    template=template,
                    page_size=1000,
                )
            conn.commit()
        logger.info("Saved %d row(s) to scan_results for %s %s", len(values), period, year)
    except Exception:
        logger.exception("Failed to save scan_results for %s %s", period, year)


def get_unscored_tickers(year: int, period: str) -> list[str]:
    """
    Returns active tickers that have a filing for (year, period) in financial_reports
    but do not yet have a corresponding row in scan_results.
    """
    if hist.database_available():
        ensure_scan_results_table()

    frame = hist.run_query(
        """
        SELECT DISTINCT fr.ticker
        FROM financial_reports fr
        JOIN emitens e ON fr.ticker = e.ticker
        LEFT JOIN scan_results s
            ON s.ticker = fr.ticker AND s.year = fr.year AND s.period = fr.period
        WHERE fr.year = %s AND fr.period = %s
            AND e.is_active = TRUE
            AND fr.ticker <> 'IHSG'
            AND s.ticker IS NULL
        ORDER BY fr.ticker;
        """,
        (year, period),
    )
    if frame.empty:
        return []
    return frame["ticker"].astype(str).tolist()


def get_period_tickers(year: int, period: str) -> list[str]:
    """Returns all active tickers that have filed for the target (year, period)."""
    sectors = fetch_active_sectors()
    frame = safe_load_reports(
        year_from=year,
        year_to=year,
        columns=["revenue"],
        period=period,
    )
    if frame.empty:
        return []
    tickers = sorted(set(frame["ticker"].astype(str)).intersection(sectors.keys()))
    return tickers


# ---------------------------------------------------------------------------
# Formatting & Diagnostic Reporting
# ---------------------------------------------------------------------------

def format_pct(value: float | None, decimals: int = 1) -> str:
    val = to_float(value)
    return "--" if val is None else f"{val * 100:.{decimals}f}%"


def format_number(value: float | None, decimals: int = 2) -> str:
    val = to_float(value)
    return "--" if val is None else f"{val:.{decimals}f}"


def format_billions(value: float | None) -> str:
    val = to_float(value)
    return "--" if val is None else f"{val / 1e9:,.1f}"


def zs_label(source: str | None) -> str:
    return {Z_SOURCE_PEER: "P", Z_SOURCE_HIST: "H"}.get(source or "", "--")


def format_direction(direction: str) -> str:
    return {"POSITIVE": "POS", "NEGATIVE": "NEG", "MIXED": "MIX"}.get(direction, "--")


def print_diagnostic_report(
    rows: list[dict[str, Any]],
    year: int,
    period: str,
    flagged_only: bool = False,
) -> None:
    """Prints the formatted cross-sectional Z-score & time-series SUR diagnostic report."""
    if not rows:
        print("\nNo tickers to report.")
        return

    scored_count = sum(1 for r in rows if r["status"] in ("STRONG", "MODERATE", "WEAK"))
    strong_count = sum(1 for r in rows if r["status"] == "STRONG")
    moderate_count = sum(1 for r in rows if r["status"] == "MODERATE")
    no_signal_count = sum(1 for r in rows if r["status"] == "NO SIGNAL")
    z_count = sum(1 for r in rows if r["z_score"] is not None)
    z_peer_count = sum(1 for r in rows if r["z_source"] == Z_SOURCE_PEER)
    z_hist_count = sum(1 for r in rows if r["z_source"] == Z_SOURCE_HIST)
    sur_count = sum(1 for r in rows if r["sur"] is not None)
    label_counts = {
        label: sum(1 for r in rows if r["confirmation"] == label)
        for label in ("CONFIRMED", "RECOVERING", "DIVERGENT", "UNCONFIRMED")
    }

    print(f"\n{'=' * LINE_WIDTH}")
    print(f"Revenue Growth Report - {period} {year}")
    print(f"{'=' * LINE_WIDTH}")
    print(
        f"Tickers reviewed: {len(rows)}  |  Scored: {scored_count}  "
        f"|  Strong: {strong_count}  |  Moderate: {moderate_count}  |  No signal: {no_signal_count}"
    )
    print(
        f"Z-score available: {z_count} (peers this period: {z_peer_count}, sector history: {z_hist_count})  "
        f"|  SUR available: {sur_count}  (of {len(rows)} tickers)"
    )
    print(
        f"Profit confirmation - Confirmed: {label_counts['CONFIRMED']}  "
        f"|  Recovering: {label_counts['RECOVERING']}  "
        f"|  Divergent: {label_counts['DIVERGENT']}  "
        f"|  Unconfirmed: {label_counts['UNCONFIRMED']}"
    )
    print("Rev and NI are in billions of the reporting currency; Prior is the comparative period.")
    print("Status follows the larger of |Z| and |SUR|. Trig shows which score crossed the threshold.")
    print(
        f"SUR counts toward the status only when growth differs from expected by at least "
        f"{SUR_MIN_SURPRISE * 100:.0f} pp (sigma floor {SUR_MIN_SIGMA:.2f}, up to {SUR_LOOKBACK_YEARS}-year lookback)."
    )
    print("ZS: P = z against this period's sector peers, H = z against the sector's history of the same period.")
    if flagged_only:
        print("Listing STRONG and MODERATE rows only.")
    print(f"{'-' * LINE_WIDTH}")

    header = (
        f"{'Ticker':<8} {'Status':<9} {'Trig':<6} {'Dir':<4} {'Confirm':<12} {'Basis':<8} "
        f"{'Rev (bn)':>11} {'Rev Prior':>11} {'Growth':>8} {'Sec Med':>8} "
        f"{'NI (bn)':>10} {'NI Prior':>10} {'Z':>6} {'ZS':>2} {'SUR':>6} "
        f"{'dPft/TA':>8} {'dGP/TA':>8} {'Peers':>5}  Note"
    )
    print(header)
    print(f"{'-' * LINE_WIDTH}")

    def sort_key(r: dict[str, Any]) -> tuple[int, float, str]:
        magnitudes = [
            abs(float(v))
            for v in (r.get("z_score"), r.get("sur"))
            if v is not None and math.isfinite(float(v))
        ]
        if magnitudes:
            return (0, -max(magnitudes), str(r["ticker"]))
        return (1, 0.0, str(r["ticker"]))

    listed = [r for r in rows if r["status"] in ("STRONG", "MODERATE")] if flagged_only else rows

    for r in sorted(listed, key=sort_key):
        peers = "--" if r["sector_peer_count"] is None else str(r["sector_peer_count"])
        print(
            f"{r['ticker']:<8} {r['status']:<9} {r['trigger_source']:<6} "
            f"{format_direction(r['direction']):<4} {r['confirmation']:<12} {r['basis']:<8} "
            f"{format_billions(r['revenue_curr']):>11} {format_billions(r['revenue_prev']):>11} "
            f"{format_pct(r['growth_yoy']):>8} {format_pct(r['sector_median_growth']):>8} "
            f"{format_billions(r['net_income_curr']):>10} {format_billions(r['net_income_prev']):>10} "
            f"{format_number(r['z_score']):>6} {zs_label(r['z_source']):>2} {format_number(r['sur']):>6} "
            f"{format_pct(r['profit_delta_assets'], 2):>8} {format_pct(r['gross_delta_assets'], 2):>8} "
            f"{peers:>5}  {r['note']}"
        )

    print(f"{'=' * LINE_WIDTH}\n")


def scan_for_anomalies(year: int, period: str) -> list[dict[str, Any]]:
    """
    Backward-compatible wrapper returning STRONG and MODERATE rows from the full
    diagnostic pipeline (including historical sector benchmarks and time-series SUR).
    """
    tickers = get_period_tickers(year, period)
    if not tickers:
        return []
    rows = build_diagnostic_report(year, period, tickers)
    anomalies = [r for r in rows if r["status"] in ("STRONG", "MODERATE")]
    anomalies.sort(
        key=lambda r: max(
            abs(to_float(r.get("z_score")) or 0.0),
            abs(to_float(r.get("sur")) or 0.0),
        ),
        reverse=True,
    )
    logger.info(
        "Scan complete: %d tickers evaluated, %d flagged as anomalies",
        len(rows), len(anomalies),
    )
    return anomalies


def print_report(anomalies: list[dict[str, Any]]) -> None:
    """Backward-compatible wrapper for printing anomaly rows."""
    if not anomalies:
        print("No anomalies found.")
        return
    print_diagnostic_report(anomalies, year=0, period="SCAN", flagged_only=True)


# ---------------------------------------------------------------------------
# End-to-End CLI Pipeline
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the post-earnings YoY revenue growth & SUR diagnostic scanner."
    )
    parser.add_argument("--year", type=int, default=2026, help="Target fiscal year (default: 2026)")
    parser.add_argument("--period", type=str, default="Q3", help="Target period: Q1, Q2, Q3, or FY (default: Q3)")
    parser.add_argument("--tickers", nargs="*", help="Optional explicit list of tickers to evaluate")
    parser.add_argument("--all-rows", action="store_true", help="Print all evaluated tickers, not just STRONG/MODERATE")
    parser.add_argument("--no-save", action="store_true", help="Skip saving results to PostgreSQL scan_results")
    args = parser.parse_args()

    target_period = args.period.strip().upper()
    target_tickers = (
        [t.strip().upper() for t in args.tickers if t.strip()]
        if args.tickers
        else get_period_tickers(args.year, target_period)
    )

    if not target_tickers:
        logger.info("No active tickers with filings found for %s %s", target_period, args.year)
        return

    rows = build_diagnostic_report(args.year, target_period, target_tickers)
    if not args.no_save:
        save_scan_results(rows, args.year, target_period)
    print_diagnostic_report(rows, args.year, target_period, flagged_only=not args.all_rows)


if __name__ == "__main__":
    main()