import logging
import os
from pathlib import Path
from statistics import median

import psycopg2
from dotenv import load_dotenv

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


def fetch_growth_candidates(year: int, period: str) -> list[dict]:
    """
    Pulls YoY revenue growth per ticker for the given period, comparing
    against the same period one year prior. Also pulls net_income for
    both periods (not filtered on it — net_income can legitimately be
    negative or zero) so a confirmation check can be layered on top of
    the revenue-based z-score without changing what counts as a
    statistical outlier.
    """
    conn = psycopg2.connect(**config)
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT 
                curr.ticker,
                e.sector,
                curr.revenue AS revenue_curr,
                prev.revenue AS revenue_prev,
                curr.net_income AS net_income_curr,
                prev.net_income AS net_income_prev
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
                AND e.sector IS NOT NULL AND e.sector != 'Unknown'
                AND e.is_active = TRUE;
            """,
            (year, period),
        )
        rows = cur.fetchall()
    finally:
        cur.close()
        conn.close()

    candidates = []
    for ticker, sector, revenue_curr, revenue_prev, net_income_curr, net_income_prev in rows:
        growth = (float(revenue_curr) - float(revenue_prev)) / float(revenue_prev)
        candidates.append({
            "ticker": ticker,
            "sector": sector,
            "revenue_curr": revenue_curr,
            "revenue_prev": revenue_prev,
            "growth_yoy": growth,
            "net_income_curr": net_income_curr,
            "net_income_prev": net_income_prev,
        })

    logger.info("Fetched %d growth candidates for %s %s", len(candidates), period, year)
    return candidates


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
                prev.revenue_tag_source AS tag_source_prev
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
    for ticker, sector, rev_curr, tag_curr, rev_prev, tag_prev in rows:
        info[ticker] = {
            "sector": sector,
            "revenue_curr": rev_curr,
            "tag_source_curr": tag_curr,
            "revenue_prev": rev_prev,
            "tag_source_prev": tag_prev,
        }
    return info


def compute_robust_z_scores(candidates: list[dict]) -> tuple[list[dict], dict]:
    """
    Groups candidates by sector, computes median + MAD of growth_yoy per
    sector, then scores each ticker against its own sector's distribution.
    """
    by_sector = {}
    for c in candidates:
        by_sector.setdefault(c["sector"], []).append(c)

    results = []
    sector_skip_reasons = {}

    for sector, group in by_sector.items():
        if len(group) < MIN_PEERS_PER_SECTOR:
            sector_skip_reasons[sector] = f"only {len(group)} peers (minimum {MIN_PEERS_PER_SECTOR} required)"
            continue

        growth_values = [c["growth_yoy"] for c in group]
        sector_median = median(growth_values)

        abs_deviations = [abs(g - sector_median) for g in growth_values]
        mad = median(abs_deviations)

        if mad == 0:
            sector_skip_reasons[sector] = "no variation among peers (MAD=0)"
            continue

        scaled_mad = MAD_SCALE_FACTOR * mad

        for c in group:
            z = (c["growth_yoy"] - sector_median) / scaled_mad
            results.append({
                **c,
                "sector_median_growth": sector_median,
                "sector_peer_count": len(group),
                "z_score": z,
            })

    return results, sector_skip_reasons


def classify_signal(z_score: float) -> str:
    abs_z = abs(z_score)
    if abs_z >= Z_SCORE_STRONG_THRESHOLD:
        return "STRONG"
    if abs_z >= Z_SCORE_THRESHOLD:
        return "MODERATE"
    return "WEAK"


def compute_net_income_confirmation(net_income_curr, net_income_prev) -> tuple[str, str]:
    """
    Checks whether net income moved in the same direction as revenue.
    Returns (confirmation_label, detail_string). This is a directional
    sanity check, not a second statistical test — a revenue spike with
    shrinking or negative profit growth is flagged as worth scrutiny,
    since the model's underlying premise (an under-priced improvement
    in the business) is weaker when profitability didn't follow revenue.
    """
    if net_income_curr is None or net_income_prev is None:
        return "UNCONFIRMED", "net income data unavailable for one or both periods"

    net_income_curr = float(net_income_curr)
    net_income_prev = float(net_income_prev)

    if net_income_prev == 0:
        return "UNCONFIRMED", "prior-period net income is zero, growth undefined"

    ni_growth = (net_income_curr - net_income_prev) / abs(net_income_prev)

    if ni_growth > 0:
        return "CONFIRMED", f"net income grew {ni_growth * 100:.1f}% YoY alongside revenue"
    else:
        return "DIVERGENT", f"net income fell {ni_growth * 100:.1f}% YoY despite revenue growth"


def build_diagnostic_report(year: int, period: str, tickers: list[str]) -> list[dict]:
    """
    Produces one row per requested ticker, always. Each scored row now
    also carries a net-income confirmation label, since a revenue-only
    signal can be misleading when profitability moved the other way.
    """
    all_candidates = fetch_growth_candidates(year, period)
    scored, sector_skip_reasons = compute_robust_z_scores(all_candidates)

    scored_by_ticker = {r["ticker"]: r for r in scored}
    diagnostic_info = fetch_ticker_diagnostic_info(year, period, tickers)

    report_rows = []

    for ticker in tickers:
        if ticker in scored_by_ticker:
            r = scored_by_ticker[ticker]
            confirmation, confirmation_detail = compute_net_income_confirmation(
                r["net_income_curr"], r["net_income_prev"]
            )
            report_rows.append({
                "ticker": ticker,
                "sector": r["sector"],
                "growth_yoy": r["growth_yoy"],
                "sector_median_growth": r["sector_median_growth"],
                "sector_peer_count": r["sector_peer_count"],
                "z_score": r["z_score"],
                "status": classify_signal(r["z_score"]),
                "confirmation": confirmation,
                "note": confirmation_detail,
            })
            continue

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
        elif not sector or sector == "Unknown":
            note = "sector not classified in emitens"
        elif sector in sector_skip_reasons:
            note = f"sector '{sector}': {sector_skip_reasons[sector]}"
        else:
            note = "growth could not be computed (unspecified data issue)"

        report_rows.append({
            "ticker": ticker,
            "sector": sector,
            "growth_yoy": None,
            "sector_median_growth": None,
            "sector_peer_count": None,
            "z_score": None,
            "status": "NO SIGNAL",
            "confirmation": "--",
            "note": note,
        })

    return report_rows


def print_diagnostic_report(rows: list[dict], year: int, period: str) -> None:
    if not rows:
        print("\nNo tickers to report.")
        return

    scored_count = sum(1 for r in rows if r["status"] in ("STRONG", "MODERATE", "WEAK"))
    strong_count = sum(1 for r in rows if r["status"] == "STRONG")
    moderate_count = sum(1 for r in rows if r["status"] == "MODERATE")
    no_signal_count = sum(1 for r in rows if r["status"] == "NO SIGNAL")
    confirmed_count = sum(1 for r in rows if r["confirmation"] == "CONFIRMED")
    divergent_count = sum(1 for r in rows if r["confirmation"] == "DIVERGENT")

    print(f"\n{'=' * 115}")
    print(f"Revenue Growth Report — {period} {year}")
    print(f"{'=' * 115}")
    print(
        f"Tickers reviewed: {len(rows)}  |  Scored: {scored_count}  "
        f"|  Strong: {strong_count}  |  Moderate: {moderate_count}  |  No signal: {no_signal_count}"
    )
    print(
        f"Net income confirmation — Confirmed: {confirmed_count}  |  Divergent: {divergent_count}  "
        f"|  Unconfirmed: {scored_count - confirmed_count - divergent_count}"
    )
    print(f"{'-' * 115}")

    header = (
        f"{'Ticker':<8} {'Status':<10} {'Confirm':<12} {'Growth YoY':>12} "
        f"{'Sector Med':>12} {'Z-Score':>9} {'Peers':>6}  Note"
    )
    print(header)
    print(f"{'-' * 115}")

    def sort_key(r):
        if r["z_score"] is not None:
            return (0, -abs(r["z_score"]))
        return (1, r["ticker"])

    for r in sorted(rows, key=sort_key):
        if r["z_score"] is not None:
            print(
                f"{r['ticker']:<8} {r['status']:<10} {r['confirmation']:<12} "
                f"{r['growth_yoy']*100:>11.1f}% {r['sector_median_growth']*100:>11.1f}% "
                f"{r['z_score']:>9.2f} {r['sector_peer_count']:>6}  {r['note']}"
            )
        else:
            print(
                f"{r['ticker']:<8} {r['status']:<10} {r['confirmation']:<12} "
                f"{'--':>12} {'--':>12} {'--':>9} {'--':>6}  {r['note']}"
            )

    print(f"{'=' * 115}\n")


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