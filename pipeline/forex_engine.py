import calendar
import logging
import os
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

FX_SYMBOL = "IDR=X"
CACHE_FILE = Path(__file__).resolve().parent.parent / "cache" / "usdidr_closes.csv"

HISTORY_PADDING_DAYS = 30
HISTORY_EXTRA_DAYS = 365
CACHE_START_TOLERANCE_DAYS = 5
MAX_STALENESS_DAYS = 5
MAX_OUTLIER_DEVIATION = 0.02
OUTLIER_WINDOW = 11
RATE_MIN = 5000.0
RATE_MAX = 30000.0

_LEGACY_PERIOD_MONTH = {"Q1": 3, "Q2": 6, "Q3": 9, "Q4": 12, "FY": 12}


class FxRateError(RuntimeError):
    pass


@dataclass(frozen=True)
class FxQuote:
    rate: float
    rate_date: date
    as_of: date
    source: str


_lock = threading.RLock()
_history = None
_history_start = None
_history_source = None
_failed_start = None
_failed_message = None


def _to_date(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raise FxRateError(f"Invalid as-of date: {value!r}")


def _download_history(start: date) -> pd.Series:
    end = date.today() + timedelta(days=1)
    data = yf.Ticker(FX_SYMBOL).history(start=start.isoformat(), end=end.isoformat(), auto_adjust=False)
    if data.empty:
        raise FxRateError(f"No USD/IDR data returned since {start}")

    closes = data["Close"].dropna()
    closes = closes[(closes >= RATE_MIN) & (closes <= RATE_MAX)]

    index = pd.DatetimeIndex(closes.index)
    if index.tz is not None:
        index = index.tz_localize(None)
    closes = pd.Series(closes.to_numpy(dtype=float), index=index.normalize())
    closes = closes[~closes.index.duplicated(keep="last")].sort_index()

    local_median = closes.rolling(OUTLIER_WINDOW, center=True, min_periods=3).median()
    deviation = (closes / local_median - 1).abs()
    for ts, value in closes[deviation > MAX_OUTLIER_DEVIATION].items():
        logger.warning("Rejected outlier USD/IDR close %.2f on %s", value, ts.date())
    closes = closes[deviation <= MAX_OUTLIER_DEVIATION]

    if closes.empty:
        raise FxRateError("No valid USD/IDR closes after cleaning")
    return closes.round(2)


def _read_cache():
    if not CACHE_FILE.is_file():
        return None
    try:
        frame = pd.read_csv(CACHE_FILE, parse_dates=["date"])
    except Exception:
        return None
    if frame.empty or "close" not in frame.columns:
        return None
    return pd.Series(frame["close"].to_numpy(dtype=float), index=pd.DatetimeIndex(frame["date"])).sort_index()


def _write_cache(series: pd.Series) -> None:
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame({"date": series.index.strftime("%Y-%m-%d"), "close": series.to_numpy()})
    tmp = CACHE_FILE.with_suffix(".tmp")
    frame.to_csv(tmp, index=False)
    os.replace(tmp, CACHE_FILE)


def _cache_fetched_today() -> bool:
    return datetime.fromtimestamp(CACHE_FILE.stat().st_mtime).date() == date.today()


def _ensure_history(as_of: date) -> None:
    global _history, _history_start, _history_source, _failed_start, _failed_message

    needed_start = as_of - timedelta(days=HISTORY_PADDING_DAYS)

    with _lock:
        if _history is not None and _history_start <= needed_start:
            return
        if _failed_start is not None and needed_start >= _failed_start:
            raise FxRateError(_failed_message)

        cached = _read_cache()
        usable = None
        cached_first = None
        if cached is not None:
            cached_first = cached.index[0].date()
            if cached_first <= needed_start + timedelta(days=CACHE_START_TOLERANCE_DAYS):
                usable = cached
                if pd.Timestamp(as_of) <= cached.index[-1] or _cache_fetched_today():
                    _history, _history_source = cached, "cache"
                    _history_start = cached_first - timedelta(days=CACHE_START_TOLERANCE_DAYS)
                    return

        fetch_start = needed_start - timedelta(days=HISTORY_EXTRA_DAYS)
        if cached_first is not None:
            fetch_start = min(fetch_start, cached_first)

        try:
            series = _download_history(fetch_start)
        except Exception as exc:
            if usable is not None:
                _history, _history_source = usable, "cache"
                _history_start = cached_first - timedelta(days=CACHE_START_TOLERANCE_DAYS)
                logger.warning("Unduhan USD/IDR gagal (%s), memakai cache lama", exc)
                return
            message = str(exc) if isinstance(exc, FxRateError) else f"USD/IDR fetch failed: {exc}"
            _failed_start, _failed_message = needed_start, message
            if isinstance(exc, FxRateError):
                raise
            raise FxRateError(message) from exc

        _history, _history_start, _history_source = series, fetch_start, "yfinance"
        _failed_start = None
        _failed_message = None
        _write_cache(series)
        logger.info(
            "Loaded USD/IDR history: %d closes from %s to %s",
            len(series), series.index[0].date(), series.index[-1].date(),
        )


def get_fx_quote(as_of) -> FxQuote:
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

    return FxQuote(
        rate=float(closes.iloc[-1]),
        rate_date=rate_date,
        as_of=target,
        source=_history_source or "yfinance",
    )


def _legacy_period_end(year: int, period: str) -> date:
    month = _LEGACY_PERIOD_MONTH.get(period)
    if month is None:
        raise FxRateError(f"Unknown period '{period}'")
    return date(year, month, calendar.monthrange(year, month)[1])


def get_closing_rate(year: int = None, period: str = "FY", period_end=None, as_of=None) -> float:
    if as_of is None:
        as_of = period_end if period_end is not None else _legacy_period_end(year, period)
    return get_fx_quote(as_of).rate


def convert_to_idr(value: float, currency: str, year: int = None, period: str = "FY",
                   period_end=None, as_of=None) -> float:
    if value is None or value == 0:
        return 0
    if currency and currency.upper() == "USD":
        return value * get_closing_rate(year, period, period_end, as_of)
    return value