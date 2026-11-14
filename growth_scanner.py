import logging
import os
from pathlib import Path
from statistics import mean, median, stdev

import psycopg2
from dotenv import load_dotenv
from psycopg2.extras import execute_values

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

MAD_SCALE_FACTOR = 1.4826
Z_SCORE_THRESHOLD = 2.0
Z_SCORE_STRONG_THRESHOLD = 3.0
MIN_PEERS_PER_SECTOR = 5

# The time-series SUR uses the same thresholds as the cross-sectional z-score.
SUR_THRESHOLD = Z_SCORE_THRESHOLD
SUR_STRONG_THRESHOLD = Z_SCORE_STRONG_THRESHOLD

FINANCIAL_SECTOR = "Financial Services"
UNKNOWN_SECTOR = "Unknown"

# When True, the growth baseline is the comparative revenue reported inside the
# current filing (revenue_prior), which reflects restatements. When False, or
# when revenue_prior is missing, the revenue stored in last year's row is used.
USE_REPORTED_COMPARATIVE = True
RESTATEMENT_TOLERANCE = 0.01

# Time-series SUR: the expectation is the mean of the previous N year-over-year
# growth rates of the same period, standardized by their standard deviation.
# N must be at least 2 for the standard deviation to be defined. N growth rates
# need N + 1 years of same-period revenue before the current year.
#
# With only a few observations the standard deviation is unreliable, so it is
# floored at SUR_MIN_SIGMA. A SUR is also treated as a real surprise only when
# actual growth differs from expected growth by at least SUR_MIN_SURPRISE
# (absolute, 0.10 = 10 percentage points); smaller gaps are capped below the
# moderate threshold when the status is decided, but the raw SUR is still shown.
SUR_LOOKBACK_YEARS = 4
SUR_MIN_SIGMA = 0.10
SUR_MIN_SURPRISE = 0.10

# Growth is clipped to this range before it enters any score, and revenue
# bases below MIN_BASE_REVENUE (reporting currency) are not scored at all,
# because growth from a near-zero base is noise rather than information.
GROWTH_CLIP_LOW = -1.0
GROWTH_CLIP_HIGH = 3.0
MIN_BASE_REVENUE = 1_000_000_000

# Cross-sectional z-score source. The current-period peer group is used only
# when it is large and representative enough; otherwise the z-score falls back
# to a historical benchmark built from the same sector and period in prior years.
# Coverage = peers that already filed / tickers of the sector that filed this
# period last year. Early filers are usually large and atypical, so a thin
# peer group is not trusted.
PEER_MIN_COVERAGE = 0.30
BENCHMARK_LOOKBACK_YEARS = 3
BENCHMARK_MIN_TICKERS_PER_YEAR = 5
BENCHMARK_MIN_YEARS = 2

Z_SOURCE_PEER = "PEER"
Z_SOURCE_HIST = "HIST"

LINE_WIDTH = 176

SCAN_RESULTS_DDL = """
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

SCAN_RESULTS_COLUMNS = [
    "ticker", "year", "period", "sector", "revenue_curr", "revenue_prev", "growth_yoy",
    "sector_median_growth", "sector_peer_count", "z_score", "z_source", "sur", "sur_expected_growth",
    "sur_sigma", "status", "trigger_source", "direction", "confirmation", "basis",
    "profit_delta_assets", "gross_delta_assets", "net_income_curr", "net_income_prev",
    "restated", "note",
]


def to_float(value) -> float | None:
    return None if value is None else float(value)


def clip_growth(growth: float) -> float:
    return max(GROWTH_CLIP_LOW, min(GROWTH_CLIP_HIGH, growth))


def baseline_from_row(revenue_prev_row, revenue_prior_reported) -> float:
    reported = to_float(revenue_prior_reported)
    if USE_REPORTED_COMPARATIVE and reported is not None and reported > 0:
        return reported
    return float(revenue_prev_row)


def growth_from_row(revenue_curr, revenue_prev_row, revenue_prior_reported) -> float | None:
    """Clipped growth for benchmark history; None when the base is below the floor."""
    baseline = baseline_from_row(revenue_prev_row, revenue_prior_reported)
    if baseline < MIN_BASE_REVENUE:
        return None
    return clip_growth((float(revenue_curr) - baseline) / baseline)


def fetch_growth_candidates(year: int, period: str) -> list[dict]:
    """
    Pulls year-over-year revenue growth per active ticker for the given period.

    Every ticker with valid current and prior-year revenue is returned,
    including tickers whose sector is unclassified; sector peer requirements
    are applied later and only affect the cross-sectional z-score.

    The growth baseline is the comparative revenue reported in the current
    filing when available, otherwise last year's stored revenue. growth_yoy is
    the raw growth shown in the report; scoring_growth is the clipped value
    used by every score. Tickers whose baseline is below MIN_BASE_REVENUE are
    flagged low_base and are not scored.

    Profit, gross profit and total assets of the prior-year row are pulled so
    a confirmation check can be layered on top of the revenue-based score.

    Note: the operating_profit column is populated from the pre-tax profit
    tag by the taxonomy mapping, so it is treated as pre-tax profit here.
    """
    conn = psycopg2.connect(**config)
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT
                curr.ticker,
                COALESCE(NULLIF(e.sector, ''), 'Unknown') AS sector,
                curr.revenue AS revenue_curr,
                prev.revenue AS revenue_prev_row,
                curr.revenue_prior AS revenue_prior_reported,
                curr.net_income AS net_income_curr,
                prev.net_income AS net_income_prev,
                curr.operating_profit AS pretax_curr,
                prev.operating_profit AS pretax_prev,
                curr.gross_profit AS gross_curr,
                prev.gross_profit AS gross_prev,
                prev.total_assets AS assets_prev
            FROM financial_reports curr
            JOIN financial_reports prev
                ON curr.ticker = prev.ticker
                AND prev.year = curr.year - 1
                AND prev.period = curr.period
            JOIN emitens e ON curr.ticker = e.ticker
            WHERE curr.year = %s AND curr.period = %s
                AND curr.revenue > 0 AND prev.revenue > 0
                AND curr.revenue_tag_source != 'not_found'
                AND prev.revenue_tag_source != 'not_found'
                AND e.is_active = TRUE;
            """,
            (year, period),
        )
        rows = cur.fetchall()
    finally:
        cur.close()
        conn.close()

    candidates = []
    for (
        ticker, sector, revenue_curr, revenue_prev_row, revenue_prior_reported,
        net_income_curr, net_income_prev, pretax_curr, pretax_prev,
        gross_curr, gross_prev, assets_prev,
    ) in rows:
        revenue_curr = float(revenue_curr)
        revenue_prev_row = float(revenue_prev_row)
        reported = to_float(revenue_prior_reported)

        has_comparative = reported is not None and reported > 0
        restated = (
            has_comparative
            and abs(reported - revenue_prev_row) > RESTATEMENT_TOLERANCE * revenue_prev_row
        )
        baseline = reported if (USE_REPORTED_COMPARATIVE and has_comparative) else revenue_prev_row
        raw_growth = (revenue_curr - baseline) / baseline

        candidates.append({
            "ticker": ticker,
            "sector": sector,
            "revenue_curr": revenue_curr,
            "revenue_prev": baseline,
            "restated": restated,
            "growth_yoy": raw_growth,
            "scoring_growth": clip_growth(raw_growth),
            "low_base": baseline < MIN_BASE_REVENUE,
            "net_income_curr": to_float(net_income_curr),
            "net_income_prev": to_float(net_income_prev),
            "pretax_curr": to_float(pretax_curr),
            "pretax_prev": to_float(pretax_prev),
            "gross_curr": to_float(gross_curr),
            "gross_prev": to_float(gross_prev),
            "assets_prev": to_float(assets_prev),
        })

    logger.info("Fetched %d growth candidates for %s %s", len(candidates), period, year)
    return candidates


def fetch_revenue_history(year: int, period: str, tickers: list[str]) -> dict:
    """
    Returns {ticker: {year: revenue}} for the same period across the years
    needed to build the prior growth rates used by the time-series SUR.
    """
    if not tickers:
        return {}

    first_year = year - SUR_LOOKBACK_YEARS - 1
    conn = psycopg2.connect(**config)
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT ticker, year, revenue
            FROM financial_reports
            WHERE period = %s
                AND year BETWEEN %s AND %s
                AND ticker = ANY(%s)
                AND revenue > 0
                AND revenue_tag_source != 'not_found';
            """,
            (period, first_year, year - 1, tickers),
        )
        rows = cur.fetchall()
    finally:
        cur.close()
        conn.close()

    history = {}
    for ticker, row_year, revenue in rows:
        history.setdefault(ticker, {})[row_year] = float(revenue)
    return history


def fetch_ticker_diagnostic_info(year: int, period: str, tickers: list[str]) -> dict:
    conn = psycopg2.connect(**config)
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT
                w.ticker,
                e.sector,
                curr.revenue AS revenue_curr,
                curr.revenue_tag_source AS tag_source_curr,
                prev.revenue AS revenue_prev,
                prev.revenue_tag_source AS tag_source_prev,
                curr.net_income AS net_income_curr,
                prev.net_income AS net_income_prev
            FROM (SELECT unnest(%s::text[]) AS ticker) w
            LEFT JOIN emitens e ON w.ticker = e.ticker
            LEFT JOIN financial_reports curr
                ON w.ticker = curr.ticker AND curr.year = %s AND curr.period = %s
            LEFT JOIN financial_reports prev
                ON w.ticker = prev.ticker AND prev.year = %s - 1 AND prev.period = %s;
            """,
            (tickers, year, period, year, period),
        )
        rows = cur.fetchall()
    finally:
        cur.close()
        conn.close()

    info = {}
    for ticker, sector, rev_curr, tag_curr, rev_prev, tag_prev, ni_curr, ni_prev in rows:
        info[ticker] = {
            "sector": sector,
            "revenue_curr": to_float(rev_curr),
            "tag_source_curr": tag_curr,
            "revenue_prev": to_float(rev_prev),
            "tag_source_prev": tag_prev,
            "net_income_curr": to_float(ni_curr),
            "net_income_prev": to_float(ni_prev),
        }
    return info


def fetch_sector_history(year: int, period: str) -> tuple[dict, dict]:
    """
    Returns (history, expected_counts).

    history: {sector: {year: [growth_yoy, ...]}} for the same period in the
    BENCHMARK_LOOKBACK_YEARS years before the current one, grouped by the
    sector stored in emitens (one consistent classification for all years).
    Growth is clipped and bases below MIN_BASE_REVENUE are left out.

    expected_counts: {sector: number of active tickers that have this period
    filed last year}, the estimate of how many will file in a full season.
    """
    first_year = year - BENCHMARK_LOOKBACK_YEARS

    conn = psycopg2.connect(**config)
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT
                COALESCE(NULLIF(e.sector, ''), 'Unknown') AS sector,
                curr.year,
                curr.revenue,
                prev.revenue,
                curr.revenue_prior
            FROM financial_reports curr
            JOIN financial_reports prev
                ON curr.ticker = prev.ticker
                AND prev.year = curr.year - 1
                AND prev.period = curr.period
            JOIN emitens e ON curr.ticker = e.ticker
            WHERE curr.period = %s
                AND curr.year BETWEEN %s AND %s
                AND curr.revenue > 0 AND prev.revenue > 0
                AND curr.revenue_tag_source != 'not_found'
                AND prev.revenue_tag_source != 'not_found'
                AND e.is_active = TRUE;
            """,
            (period, first_year, year - 1),
        )
        history_rows = cur.fetchall()

        cur.execute(
            """
            SELECT COALESCE(NULLIF(e.sector, ''), 'Unknown') AS sector, COUNT(*)
            FROM financial_reports fr
            JOIN emitens e ON fr.ticker = e.ticker
            WHERE fr.year = %s AND fr.period = %s
                AND fr.revenue > 0
                AND fr.revenue_tag_source != 'not_found'
                AND e.is_active = TRUE
            GROUP BY 1;
            """,
            (year - 1, period),
        )
        expected_counts = {sector: int(count) for sector, count in cur.fetchall()}
    finally:
        cur.close()
        conn.close()

    history = {}
    for sector, row_year, rev_curr, rev_prev, rev_prior in history_rows:
        growth = growth_from_row(rev_curr, rev_prev, rev_prior)
        if growth is None:
            continue
        history.setdefault(sector, {}).setdefault(row_year, []).append(growth)

    return history, expected_counts


def build_sector_benchmarks(history: dict) -> dict:
    """
    Builds {sector: {"center", "scale", "years"}} from prior years.

    center: median of the yearly sector medians (typical sector growth for
            this period across years).
    scale:  scaled MAD of growth around each year's own sector median, pooled
            across years, so year-to-year shifts in the median do not inflate
            the dispersion.

    A year counts only when the sector had at least
    BENCHMARK_MIN_TICKERS_PER_YEAR tickers; at least BENCHMARK_MIN_YEARS such
    years are required.
    """
    benchmarks = {}
    for sector, by_year in history.items():
        if sector == UNKNOWN_SECTOR:
            continue

        usable = {
            y: values for y, values in by_year.items()
            if len(values) >= BENCHMARK_MIN_TICKERS_PER_YEAR
        }
        if len(usable) < BENCHMARK_MIN_YEARS:
            continue

        yearly_medians = [median(values) for values in usable.values()]
        deviations = []
        for values in usable.values():
            year_median = median(values)
            deviations.extend(abs(g - year_median) for g in values)

        mad = median(deviations)
        if mad == 0:
            continue

        benchmarks[sector] = {
            "center": median(yearly_medians),
            "scale": MAD_SCALE_FACTOR * mad,
            "years": len(usable),
        }
    return benchmarks


def compute_robust_z_scores(
    candidates: list[dict],
    benchmarks: dict | None = None,
    expected_counts: dict | None = None,
) -> tuple[list[dict], dict]:
    """
    Scores each ticker against its sector, using the clipped growth.

    Source selection per sector:
      PEER  current-period peers, used when there are at least
            MIN_PEERS_PER_SECTOR of them AND they cover at least
            PEER_MIN_COVERAGE of the sector's expected full-season count
            (coverage is not enforced when expected_counts is None).
      HIST  historical benchmark from prior years of the same period, used
            when the peer group is too thin and a benchmark exists.

    Low-base tickers are excluded from the peer group and receive no score.
    Sectors that are unclassified, or have neither source, are skipped with a
    reason; their tickers get no z-score.
    """
    benchmarks = benchmarks or {}

    by_sector = {}
    for c in candidates:
        if c.get("low_base"):
            continue
        by_sector.setdefault(c["sector"], []).append(c)

    results = []
    sector_skip_reasons = {}

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

        center = scale = None
        source = None
        peer_reason = None

        if peer_ok:
            growth_values = [c["scoring_growth"] for c in group]
            sector_median = median(growth_values)
            mad = median([abs(g - sector_median) for g in growth_values])
            if mad > 0:
                center, scale, source = sector_median, MAD_SCALE_FACTOR * mad, Z_SOURCE_PEER
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
            if benchmark is not None:
                center, scale, source = benchmark["center"], benchmark["scale"], Z_SOURCE_HIST
                sector_skip_reasons[sector] = f"z vs {benchmark['years']}y sector history; {peer_reason}"
            else:
                sector_skip_reasons[sector] = f"{peer_reason}; no usable sector history"
                continue

        for c in group:
            results.append({
                **c,
                "sector_median_growth": center,
                "sector_peer_count": peers,
                "z_score": (c["scoring_growth"] - center) / scale,
                "z_source": source,
            })

    return results, sector_skip_reasons


def classify_score(value: float) -> str:
    abs_value = abs(value)
    if abs_value >= Z_SCORE_STRONG_THRESHOLD:
        return "STRONG"
    if abs_value >= Z_SCORE_THRESHOLD:
        return "MODERATE"
    return "WEAK"


def classify_signal(z_score: float) -> str:
    return classify_score(z_score)


def combine_scores(z_score: float | None, sur: float | None) -> tuple[str, str, str]:
    """
    Merges the cross-sectional z-score and the time-series SUR into one status.
    Returns (status, trigger_source, direction).

    Status follows the larger absolute score. trigger_source lists which score
    crossed the moderate threshold (Z, SUR or Z+SUR). Direction comes from the
    triggering scores, or from the larger score when neither triggers; it is
    MIXED when Z and SUR trigger in opposite directions.
    """
    available = {name: value for name, value in (("Z", z_score), ("SUR", sur)) if value is not None}
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


def compute_sur(current_growth: float, ticker_history: dict, year: int) -> dict | None:
    """
    Time-series standardized unexpected revenue for one ticker.

    expected = mean of the previous SUR_LOOKBACK_YEARS year-over-year growth
    rates of the same period (each clipped like the current growth); sigma =
    their sample standard deviation, floored at SUR_MIN_SIGMA. Returns None
    when any required year is missing.

    "valid" is False when the gap between actual and expected growth is below
    SUR_MIN_SURPRISE, meaning the score is large only because sigma is small.
    """
    prior_growths = []
    for k in range(1, SUR_LOOKBACK_YEARS + 1):
        revenue_end = ticker_history.get(year - k)
        revenue_start = ticker_history.get(year - k - 1)
        if revenue_end is None or revenue_start is None:
            return None
        prior_growths.append(clip_growth(revenue_end / revenue_start - 1))

    expected = mean(prior_growths)
    sigma = max(stdev(prior_growths), SUR_MIN_SIGMA)
    surprise = current_growth - expected
    return {
        "sur": surprise / sigma,
        "expected_growth": expected,
        "sigma": sigma,
        "surprise": surprise,
        "valid": abs(surprise) >= SUR_MIN_SURPRISE,
    }


def gate_sur(sur_result: dict | None) -> float | None:
    """
    SUR value used for the status decision. When the surprise is too small to
    matter, the score is capped just below the moderate threshold so it cannot
    trigger a signal on its own.
    """
    if sur_result is None:
        return None
    sur = sur_result["sur"]
    if sur_result["valid"]:
        return sur
    cap = SUR_THRESHOLD * 0.99
    return max(-cap, min(cap, sur))


def compute_profit_confirmation(c: dict) -> tuple[str, str, str, float | None]:
    """
    Checks whether profit moved in the same direction as revenue.
    Returns (label, basis, detail, profit_change_scaled_by_prior_assets).

    The change in profit is a plain difference scaled by prior-year total
    assets, so negative or near-zero prior profit cannot distort the measure.
    The sign pattern of the two periods decides the label:

      CONFIRMED   profit in both periods and higher, or loss turned to profit
      RECOVERING  loss in both periods, but the loss narrowed
      DIVERGENT   profit fell, profit turned to loss, or the loss widened
      UNCONFIRMED required data is unavailable

    Basis is PRETAX for non-financial tickers with pre-tax profit available,
    otherwise NET_INC (always the case for financial sector tickers).
    """
    assets_prev = c["assets_prev"]
    if assets_prev is None or assets_prev <= 0:
        return "UNCONFIRMED", "--", "prior-year total assets unavailable", None

    if (
        c["sector"] != FINANCIAL_SECTOR
        and c["pretax_curr"] is not None
        and c["pretax_prev"] is not None
    ):
        basis, curr, prev = "PRETAX", c["pretax_curr"], c["pretax_prev"]
    elif c["net_income_curr"] is not None and c["net_income_prev"] is not None:
        basis, curr, prev = "NET_INC", c["net_income_curr"], c["net_income_prev"]
    else:
        return "UNCONFIRMED", "--", "profit data unavailable for one or both periods", None

    delta_scaled = (curr - prev) / assets_prev
    change_text = f"{delta_scaled * 100:+.2f}% of prior-year assets"

    if curr > 0 and prev > 0:
        if curr > prev:
            return "CONFIRMED", basis, f"profit grew ({change_text})", delta_scaled
        return "DIVERGENT", basis, f"profit fell ({change_text})", delta_scaled

    if curr > 0 and prev <= 0:
        return "CONFIRMED", basis, f"turnaround from loss to profit ({change_text})", delta_scaled

    if curr <= 0 and prev <= 0:
        if curr > prev:
            return "RECOVERING", basis, f"loss narrowed but still loss-making ({change_text})", delta_scaled
        return "DIVERGENT", basis, f"loss widened or unchanged ({change_text})", delta_scaled

    return "DIVERGENT", basis, f"profit turned to loss ({change_text})", delta_scaled


def compute_gross_profit_change(c: dict) -> float | None:
    """
    Change in gross profit scaled by prior-year total assets. Informational:
    gross profit is not affected by tax, interest, joint-venture income or
    foreign exchange items, so it shows whether the operating business improved.
    """
    assets_prev = c["assets_prev"]
    if assets_prev is None or assets_prev <= 0:
        return None
    if c["gross_curr"] is None or c["gross_prev"] is None:
        return None
    return (c["gross_curr"] - c["gross_prev"]) / assets_prev


def empty_row(ticker: str, sector: str | None, note: str, info: dict | None = None) -> dict:
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


def build_diagnostic_report(year: int, period: str, tickers: list[str]) -> list[dict]:
    """
    Produces one row per requested ticker, always.

    Every ticker with valid current and prior-year revenue gets its growth,
    SUR (from its own history) and profit confirmation. The cross-sectional
    z-score comes from this period's sector peers when there are enough of
    them, otherwise from the sector's history. The status follows whichever
    score is larger; a SUR whose surprise is below SUR_MIN_SURPRISE cannot
    trigger a signal. Tickers with a revenue base below MIN_BASE_REVENUE are
    reported but not scored.
    """
    candidates = fetch_growth_candidates(year, period)
    history, expected_counts = fetch_sector_history(year, period)
    benchmarks = build_sector_benchmarks(history)
    scored, sector_skip_reasons = compute_robust_z_scores(candidates, benchmarks, expected_counts)

    candidate_by_ticker = {c["ticker"]: c for c in candidates}
    z_by_ticker = {r["ticker"]: r for r in scored}

    sector_counts = {}
    for c in candidates:
        if not c["low_base"]:
            sector_counts[c["sector"]] = sector_counts.get(c["sector"], 0) + 1

    with_candidate = [t for t in tickers if t in candidate_by_ticker]
    revenue_history = fetch_revenue_history(year, period, with_candidate)
    diagnostic_info = fetch_ticker_diagnostic_info(year, period, tickers)

    report_rows = []

    for ticker in tickers:
        c = candidate_by_ticker.get(ticker)

        if c is None:
            info = diagnostic_info.get(ticker, {})
            sector = info.get("sector")

            if info.get("revenue_curr") is None:
                note = f"no {period} {year} data ingested yet"
            elif info.get("tag_source_curr") == "not_found":
                note = "revenue tag not resolved for current period"
            elif info.get("revenue_prev") is None:
                note = f"no baseline data for {period} {year - 1} (new listing or gap)"
            elif info.get("tag_source_prev") == "not_found":
                note = "revenue tag not resolved for prior-year baseline"
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
                f"prior revenue below {MIN_BASE_REVENUE / 1e9:.0f} bn, growth not scored"
            )
        elif z_score is None:
            reason = sector_skip_reasons.get(c["sector"], "unspecified")
            notes.append(f"no z-score: {reason}")
        elif z_row["z_source"] == Z_SOURCE_HIST:
            notes.append(sector_skip_reasons.get(c["sector"], "z vs sector history"))
        if not c["low_base"]:
            if sur is None:
                notes.append(
                    f"no SUR: needs {SUR_LOOKBACK_YEARS + 1} prior years of {period} revenue"
                )
            elif not sur_result["valid"]:
                notes.append(
                    f"SUR gap below {SUR_MIN_SURPRISE * 100:.0f} pp, not counted"
                )
        if c["restated"]:
            notes.append("comparative revenue differs from prior filing")

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


def ensure_scan_results_table() -> None:
    conn = psycopg2.connect(**config)
    cur = conn.cursor()
    try:
        cur.execute(SCAN_RESULTS_DDL)
        cur.execute("ALTER TABLE scan_results ADD COLUMN IF NOT EXISTS z_source text;")
        conn.commit()
    finally:
        cur.close()
        conn.close()


def save_scan_results(rows: list[dict], year: int, period: str) -> None:
    """
    Upserts one row per ticker for the period. Values are recomputed on every
    save because the z-score moves as more peers report the same period.
    """
    if not rows:
        return

    ensure_scan_results_table()

    values = []
    for r in rows:
        record = {**r, "year": year, "period": period}
        values.append(tuple(record.get(column) for column in SCAN_RESULTS_COLUMNS))

    columns_sql = ", ".join(SCAN_RESULTS_COLUMNS)
    update_sql = ", ".join(
        f"{c} = EXCLUDED.{c}" for c in SCAN_RESULTS_COLUMNS if c not in ("ticker", "year", "period")
    )
    template = "(" + ", ".join(["%s"] * len(SCAN_RESULTS_COLUMNS)) + ")"

    conn = psycopg2.connect(**config)
    cur = conn.cursor()
    try:
        execute_values(
            cur,
            f"""
            INSERT INTO scan_results ({columns_sql}) VALUES %s
            ON CONFLICT (ticker, year, period)
            DO UPDATE SET {update_sql}, computed_at = now();
            """,
            values,
            template=template,
            page_size=500,
        )
        conn.commit()
        logger.info("Saved %d row(s) to scan_results for %s %s", len(values), period, year)
    except Exception:
        logger.exception("Failed to save scan_results")
        conn.rollback()
    finally:
        cur.close()
        conn.close()


def get_unscored_tickers(year: int, period: str) -> list[str]:
    """
    Active tickers that have a filing for the period but no scan_results row yet,
    meaning the scanner has never evaluated them.
    """
    ensure_scan_results_table()

    conn = psycopg2.connect(**config)
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT fr.ticker
            FROM financial_reports fr
            JOIN emitens e ON fr.ticker = e.ticker
            LEFT JOIN scan_results s
                ON s.ticker = fr.ticker AND s.year = fr.year AND s.period = fr.period
            WHERE fr.year = %s AND fr.period = %s
                AND e.is_active = TRUE
                AND s.ticker IS NULL
            ORDER BY fr.ticker;
            """,
            (year, period),
        )
        return [row[0] for row in cur.fetchall()]
    finally:
        cur.close()
        conn.close()


def format_pct(value: float | None, decimals: int = 1) -> str:
    return "--" if value is None else f"{value * 100:.{decimals}f}%"


def format_number(value: float | None, decimals: int = 2) -> str:
    return "--" if value is None else f"{value:.{decimals}f}"


def format_billions(value: float | None) -> str:
    return "--" if value is None else f"{value / 1e9:,.1f}"


def zs_label(source: str | None) -> str:
    return {Z_SOURCE_PEER: "P", Z_SOURCE_HIST: "H"}.get(source, "--")


def format_direction(direction: str) -> str:
    return {"POSITIVE": "POS", "NEGATIVE": "NEG", "MIXED": "MIX"}.get(direction, "--")


def print_diagnostic_report(
    rows: list[dict], year: int, period: str, flagged_only: bool = False
) -> None:
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
        f"{SUR_MIN_SURPRISE * 100:.0f} pp (sigma floor {SUR_MIN_SIGMA:.2f}, {SUR_LOOKBACK_YEARS}-year lookback)."
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

    def sort_key(r):
        magnitudes = [abs(v) for v in (r["z_score"], r["sur"]) if v is not None]
        if magnitudes:
            return (0, -max(magnitudes), r["ticker"])
        return (1, 0, r["ticker"])

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


def scan_for_anomalies(year: int, period: str) -> list[dict]:
    candidates = fetch_growth_candidates(year, period)
    scored, _ = compute_robust_z_scores(candidates)

    anomalies = [r for r in scored if abs(r["z_score"]) >= Z_SCORE_THRESHOLD]
    anomalies.sort(key=lambda r: r["z_score"], reverse=True)

    logger.info(
        "Scan complete: %d candidates scored, %d flagged as anomalies (|z| >= %.1f)",
        len(scored), len(anomalies), Z_SCORE_THRESHOLD,
    )
    return anomalies


def print_report(anomalies: list[dict]) -> None:
    if not anomalies:
        print("No anomalies found.")
        return

    print(f"\n{'Ticker':<8} {'Sector':<30} {'Growth YoY':>12} {'Sector Median':>15} {'Z-Score':>10} {'Peers':>6}")
    for a in anomalies:
        print(
            f"{a['ticker']:<8} {a['sector'][:30]:<30} "
            f"{a['growth_yoy']*100:>11.1f}% {a['sector_median_growth']*100:>14.1f}% "
            f"{a['z_score']:>10.2f} {a['sector_peer_count']:>6}"
        )


if __name__ == "__main__":
    YEAR = 2025
    PERIOD = "FY"

    anomalies = scan_for_anomalies(YEAR, PERIOD)
    print_report(anomalies)