import calendar
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

FX_SYMBOL = "IDR=X"

HISTORY_PADDING_DAYS = 30
MAX_STALENESS_DAYS = 5
MAX_OUTLIER_DEVIATION = 0.02
OUTLIER_WINDOW = 11
RATE_MIN = 5000.0
RATE_MAX = 30000.0

_LEGACY_PERIOD_MONTH = {"Q1": 3, "Q2": 6, "Q3": 9, "Q4": 12, "FY": 12}


class FxRateError(RuntimeError):
    """Raised when a reliable USD/IDR rate cannot be resolved for the requested date."""


@dataclass(frozen=True)
class FxQuote:
    rate: float
    rate_date: date
    as_of: date
    source: str


_history = None
_history_start = None
_failed_start = None
_failed_message = None


def _to_date(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raise FxRateError(f"Invalid as-of date: {value!r}")


def _download_history(start: date) -> pd.Series:
    end = date.today() + timedelta(days=1)  # yfinance end date is exclusive
    data = yf.Ticker(FX_SYMBOL).history(
        start=start.isoformat(), end=end.isoformat(), auto_adjust=False
    )
    if data.empty:
        raise FxRateError(f"No USD/IDR data returned since {start}")

    closes = data["Close"].dropna()
    closes = closes[(closes >= RATE_MIN) & (closes <= RATE_MAX)]

    index = pd.DatetimeIndex(closes.index)
    if index.tz is not None:
        index = index.tz_localize(None)
    closes = pd.Series(closes.to_numpy(dtype=float), index=index.normalize())
    closes = closes[~closes.index.duplicated(keep="last")].sort_index()

    # Data cleaning: drop single-day prints that deviate from the local median.
    local_median = closes.rolling(OUTLIER_WINDOW, center=True, min_periods=3).median()
    deviation = (closes / local_median - 1).abs()
    rejected = closes[deviation > MAX_OUTLIER_DEVIATION]
    for ts, value in rejected.items():
        logger.warning("Rejected outlier USD/IDR close %.2f on %s", value, ts.date())
    closes = closes[deviation <= MAX_OUTLIER_DEVIATION]

    if closes.empty:
        raise FxRateError("No valid USD/IDR closes after cleaning")
    return closes.round(2)


def _ensure_history(as_of: date) -> None:
    global _history, _history_start, _failed_start, _failed_message

    needed_start = as_of - timedelta(days=HISTORY_PADDING_DAYS)

    if _history is not None and _history_start <= needed_start:
        return
    if _failed_start is not None and needed_start >= _failed_start:
        raise FxRateError(_failed_message)

    try:
        _history = _download_history(needed_start)
        _history_start = needed_start
        _failed_start = None
        _failed_message = None
        logger.info(
            "Loaded USD/IDR history: %d closes from %s to %s",
            len(_history), _history.index[0].date(), _history.index[-1].date(),
        )
    except FxRateError as exc:
        _failed_start, _failed_message = needed_start, str(exc)
        raise
    except Exception as exc:
        _failed_start = needed_start
        _failed_message = f"USD/IDR fetch failed: {exc}"
        raise FxRateError(_failed_message) from exc


def get_fx_quote(as_of) -> FxQuote:
    """Last valid USD/IDR close on or before as_of. Raises FxRateError if none is reliable."""
    target = _to_date(as_of)
    if target > date.today():
        raise FxRateError(f"As-of date {target} is in the future")

    _ensure_history(target)

    closes = _history[_history.index <= pd.Timestamp(target)]
    if closes.empty:
        raise FxRateError(f"No USD/IDR close on or before {target}")

    rate_date = closes.index[-1].date()
    gap_days = (target - rate_date).days
    if gap_days > MAX_STALENESS_DAYS:
        raise FxRateError(
            f"Latest valid close is {gap_days} days before {target} (limit {MAX_STALENESS_DAYS})"
        )

    return FxQuote(rate=float(closes.iloc[-1]), rate_date=rate_date, as_of=target, source="yfinance")


def _legacy_period_end(year: int, period: str) -> date:
    month = _LEGACY_PERIOD_MONTH.get(period)
    if month is None:
        raise FxRateError(f"Unknown period '{period}'")
    return date(year, month, calendar.monthrange(year, month)[1])


def get_closing_rate(year: int = None, period: str = "FY", period_end=None, as_of=None) -> float:
    """as_of takes precedence. Year and period are kept for older callers and map to the
    calendar period end only when no explicit date is supplied."""
    if as_of is None:
        as_of = period_end if period_end is not None else _legacy_period_end(year, period)
    return get_fx_quote(as_of).rate


def convert_to_idr(value: float, currency: str, year: int = None, period: str = "FY",
                   period_end=None, as_of=None) -> float:
    """Converts a value to IDR if its currency is USD; otherwise returns it unchanged."""
    if value is None or value == 0:
        return 0
    if currency and currency.upper() == "USD":
        return value * get_closing_rate(year, period, period_end, as_of)
    return value