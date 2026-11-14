import argparse
import logging
import os
from datetime import datetime
from pathlib import Path

import psycopg2
from bs4 import BeautifulSoup
from dotenv import load_dotenv

from forex_engine import FxRateError, get_fx_quote

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def find_env_file(start: Path) -> Path:
    """Searches upward from `start` for the first .env file."""
    for directory in [start, *start.parents]:
        candidate = directory / ".env"
        if candidate.exists():
            return candidate
    return start.parent.parent / ".env"


env_path = find_env_file(Path(__file__).resolve().parent)
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

BASE_DIR_PATH = Path(__file__).resolve().parent.parent.parent
XBRL_DIR = Path(r"D:\MintigoInvesment\data\XBRL")


START_YEAR = 2023
PERIODS = ["Q1", "Q2", "Q3", "FY"]

FX_DATE_BASIS = "release"
MAX_RELEASE_LAG_DAYS = 180

REVENUE_TAG_PATTERNS = [
    "SalesAndRevenue",
    "TotalInterestAndShariaIncome",
    "TotalOperatingIncome",
    "OperatingRevenue",
    "NetPremiumIncome",
    "PremiumIncome",
    "Revenue",
    "NetSales",
    "IncomeFromInsurancePremiums",
]

NIL_REVENUE_VALUE = 0.0
NIL_REVENUE_SOURCE = "nil:SalesAndRevenue"
COST_OF_REVENUE_TAGS = ["CostOfSalesAndRevenue"]
GROSS_PROFIT_AS_REVENUE_SOURCE = "derived:gross_profit_no_cogs"
FINANCING_REVENUE_TAGS = ["InterestAndFinanceIncome"]
FINANCING_REVENUE_SOURCE = "computed:financing_interest_income"

EXTRA_METRICS = {
    "cash_and_equivalents": ["CashAndCashEquivalents"],
    "interest_expense": ["InterestAndFinanceCosts", "FinanceCosts"],
    "tax_expense": ["TaxBenefitExpenses"],
    "capital_expenditure": [
        "PaymentsForAcquisitionOfPropertyPlantAndEquipment",
        "PaymentsForAcquisitionOfPropertyAndEquipment",
    ],
    "gross_profit": ["GrossProfit"],
}

NET_PPE_CASH_FLOW_TAG = "ProceedsFromDisposalAcquisitionOfPropertyAndEquipment"

OPERATING_PROFIT_TAGS = ["ProfitFromOperation"]
PRETAX_PROFIT_TAGS = ["ProfitLossBeforeIncomeTax"]
FINANCE_COST_TAGS = ["InterestAndFinanceCosts", "FinanceCosts"]
FINANCE_INCOME_TAGS = ["FinanceIncome", "InterestAndFinanceIncome"]
EQUITY_METHOD_TAGS = [
    "ShareOfProfitLossOfAssociatesAccountedForUsingEquityMethod",
    "ShareOfProfitLossOfJointVenturesAccountedForUsingEquityMethod",
]

DEPRECIATION_AMORTIZATION_TOTAL_TAG = "DepreciationAndAmortisationExpenses"
DEPRECIATION_TAG = "Depreciation"
AMORTIZATION_TAG = "Amortization"

NET_INCOME_PARENT_TAGS = ["ProfitLossAttributableToParentEntity"]
SELLING_EXPENSE_TAGS = ["SellingExpenses"]
GENERAL_ADMIN_EXPENSE_TAGS = ["GeneralAndAdministrativeExpenses"]
EPS_TAGS = ["BasicEarningsLossPerShareFromContinuingOperations"]
SHARES_MIN = 1e7
SHARES_MAX = 5e11
EPS_SCALE_FACTORS = (1, 1e3, 1e6)

BANK_REVENUE_BASIS = "net"
BANK_INCLUDE_WRITEOFF_RECOVERIES = True

BANK_INTEREST_INCOME_TAGS = [
    "TotalInterestAndShariaIncome",
    "InterestIncome",
    "RevenueFromFundManagementAsMudharib",
]
BANK_INTEREST_EXPENSE_TAGS = [
    "TotalInterestAndShariaExpense",
    "InterestExpenses",
    "ThirdPartiesShareOnReturnOfTemporarySyirkahFunds",
]
BANK_NON_INTEREST_INCOME_TAGS = [
    "RevenueFromInsurancePremiums",
    "RealisedGainsLossesOnTradingOfMarketableSecurities",
    "GainsLossesOnChangesInFairValueOfMarketableSecurities",
    "InvestmentsIncome",
    "ProvisionsAndCommissionsIncomeFromTransactionsOtherThanLoan",
    "GainsLossesOnChangesInForeignExchangeRates",
    "OtherOperatingIncome",
    "RevenueFromTradingTransactions",
    "DividendsIncome",
    "RealisedGainsLossesFromDerivativeInstruments",
]
BANK_WRITEOFF_RECOVERY_TAG = "RevenueFromRecoveryOfWrittenOffAssets"
BANK_NON_INTEREST_DEDUCTION_TAGS = ["ClaimExpenses"]

if BANK_REVENUE_BASIS not in ("net", "gross"):
    raise RuntimeError("BANK_REVENUE_BASIS must be 'net' or 'gross'")

BANK_REVENUE_SOURCE = f"computed:bank_{BANK_REVENUE_BASIS}_operating_revenue"

SECURITIES_REVENUE_TAGS = [
    "IncomeFromBrokerageActivity",
    "IncomeFromUnderwritingActivitiesAndSellingFees",
    "IncomeFromFinancingTransactions",
    "IncomeFromInvestmentManagementServices",
    "IncomeFromFinancialAdvisoryServices",
    "RealisedGainsLossesOnTradingOfMarketableSecurities",
    "GainsLossesOnChangesInFairValueOfMarketableSecurities",
    "DividendsIncome",
]
SECURITIES_REVENUE_SOURCE = "computed:securities_operating_revenue"

INSURANCE_TOTAL_INCOME_TAGS = [
    "OtherUnderwritingIncome",
    "NetInvestmentIncome",
    "CommissionIncome",
    "GainsLossesOnSaleOfSharesOfStock",
]
INSURANCE_REVENUE_SOURCE = "computed:insurance_total_income"

EPS_MIN_SIGNIFICANT_DIGITS = 3

SAVE_COLUMNS = [
    "currency", "fx_rate_at_report", "total_assets", "current_assets",
    "total_liabilities", "current_liabilities", "total_equity", "revenue",
    "revenue_prior", "gross_profit", "operating_profit", "net_income",
    "operating_cash_flow", "outstanding_shares", "capital_expenditure",
    "depreciation_amortization", "cash_and_equivalents", "interest_expense",
    "tax_expense", "is_audited", "revenue_tag_source", "report_date", "period_end",
    "report_type", "rounding_level", "sector", "subsector",
]

NEW_COLUMNS = [
    "net_income_prior", "net_income_parent", "net_income_parent_prior",
    "operating_profit_prior", "gross_profit_prior",
    "core_operating_profit", "core_operating_profit_prior",
]

OPTIONAL_COLUMNS = [
    "fx_rate_date", "fx_rate_basis", "operating_profit_source", "outstanding_shares_source",
    "fx_rate_reported", "fx_rate_engine_period_end",
] + NEW_COLUMNS

REQUIRED_COLUMNS = set(SAVE_COLUMNS) | {"ticker", "year", "period"}

SUPPORTED_CURRENCIES = ("IDR", "USD")

FX_RATE_TAG = "ConversionRateAtReportingDateIfPresentationCurrencyIsOtherThanRupiah"
FX_RATE_MIN = 5000
FX_RATE_MAX = 30000
FX_RATE_SCALE_CANDIDATES = (1.0, 1e3, 1e6)

# The company-declared closing rate (DEI) is used as the stored rate only when
# it agrees with the forex_engine rate at period end within this tolerance.
# Otherwise the forex_engine rate is used.
FX_DEI_MAX_DEVIATION = 0.01
FX_BASIS_DEI = "dei_reported"

REPORT_DATE_MAX_LAG_DAYS = 365

FALLBACK_BASIS = "period_end_fallback"

_taxonomy_cache = {}
_active_save_columns = list(SAVE_COLUMNS)


def verify_schema() -> None:
    conn = psycopg2.connect(**config)
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'financial_reports';"
        )
        existing_columns = {row[0] for row in cur.fetchall()}
    finally:
        cur.close()
        conn.close()

    missing = REQUIRED_COLUMNS - existing_columns
    if missing:
        raise RuntimeError(
            f"financial_reports is missing columns: {sorted(missing)}. "
            f"Run the required ALTER TABLE statements before starting the loader."
        )

    optional_present = [c for c in OPTIONAL_COLUMNS if c in existing_columns]
    optional_missing = [c for c in OPTIONAL_COLUMNS if c not in existing_columns]
    _active_save_columns[:] = SAVE_COLUMNS + optional_present

    if optional_missing:
        logger.warning("Optional columns not present and will not be stored: %s", optional_missing)
    logger.info("Schema check passed, all required columns present")


def get_taxonomy_map(taxonomy_type: str) -> dict:
    if taxonomy_type in _taxonomy_cache:
        return _taxonomy_cache[taxonomy_type]

    conn = psycopg2.connect(**config)
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT common_term, xbrl_tag FROM taxonomy_mappings WHERE taxonomy_type = %s;",
            (taxonomy_type,),
        )
        mappings = {}
        for term, tag in cur.fetchall():
            mappings.setdefault(term, []).append(tag)

        if not mappings:
            logger.warning("No taxonomy mappings found for taxonomy_type=%s", taxonomy_type)
            return {}

        _taxonomy_cache[taxonomy_type] = mappings
        return mappings
    except Exception:
        logger.exception("Failed to fetch taxonomy mappings for %s", taxonomy_type)
        return {}
    finally:
        cur.close()
        conn.close()


def pick_current_value(entries: list):
    current = [e for e in entries if e["c"].startswith("Current")]
    if not current:
        return None
    return max(current, key=lambda e: e["d"])["v"]


def significant_digits(text: str) -> int:
    digits = "".join(ch for ch in text if ch.isdigit()).lstrip("0")
    return len(digits)


def pick_current_entry(entries: list):
    current = [e for e in entries if e["c"].startswith("Current")]
    if not current:
        return None
    return max(current, key=lambda e: e["d"])


def first_entry(candidate_tags: list, all_raw_tags: dict):
    for tag in candidate_tags:
        entry = pick_current_entry(all_raw_tags.get(tag, []))
        if entry is not None:
            return entry
    return None


def pick_prior_value(entries: list, target_days: int):
    prior = [e for e in entries if e["c"] == "Prior Duration"]
    if not prior:
        return None
    return min(prior, key=lambda e: abs(e["d"] - target_days))["v"]


def pick_value(entries: list, scope: str, target_days: int):
    if scope == "current":
        return pick_current_value(entries)
    return pick_prior_value(entries, target_days)


def first_value(candidate_tags: list, all_raw_tags: dict, scope: str, target_days: int):
    for tag in candidate_tags:
        value = pick_value(all_raw_tags.get(tag, []), scope, target_days)
        if value is not None:
            return value
    return None


def resolve_metric(candidate_tags: list, all_raw_tags: dict):
    for tag_name in candidate_tags:
        clean_tag = tag_name.split(":")[-1]
        value = pick_current_value(all_raw_tags.get(clean_tag, []))
        if value is not None:
            return value, clean_tag
    return None, None


def resolve_revenue_fallback(all_raw_tags: dict):
    for pattern in REVENUE_TAG_PATTERNS:
        value = pick_current_value(all_raw_tags.get(pattern, []))
        if value is not None:
            return value, f"heuristic:{pattern}"
    return None, "not_found"


def resolve_prior_revenue(tag_source, all_raw_tags: dict, target_days: int):
    if not tag_source or tag_source == "not_found":
        return None
    tag = tag_source.split(":", 1)[-1]
    return pick_prior_value(all_raw_tags.get(tag, []), target_days)


def is_bank_filing(dei: dict) -> bool:
    return "bank" in (dei.get("Subsector") or "").lower()


def is_financial_entity(dei: dict) -> bool:
    return "financ" in (dei.get("Sector") or "").lower()


def compute_bank_revenue(all_raw_tags: dict, scope: str, target_days: int):
    interest_income = first_value(BANK_INTEREST_INCOME_TAGS, all_raw_tags, scope, target_days)
    if interest_income is None:
        return None

    total = interest_income

    if BANK_REVENUE_BASIS == "net":
        interest_expense = first_value(BANK_INTEREST_EXPENSE_TAGS, all_raw_tags, scope, target_days)
        if interest_expense is None:
            return None
        total -= abs(interest_expense)

    income_tags = list(BANK_NON_INTEREST_INCOME_TAGS)
    if BANK_INCLUDE_WRITEOFF_RECOVERIES:
        income_tags.append(BANK_WRITEOFF_RECOVERY_TAG)

    for tag in income_tags:
        value = pick_value(all_raw_tags.get(tag, []), scope, target_days)
        if value is not None:
            total += value

    for tag in BANK_NON_INTEREST_DEDUCTION_TAGS:
        value = pick_value(all_raw_tags.get(tag, []), scope, target_days)
        if value is not None:
            total -= value

    return total


def is_securities_filing(dei: dict) -> bool:
    return "investment service" in (dei.get("Subsector") or "").lower()


def compute_securities_revenue(all_raw_tags: dict, scope: str, target_days: int):
    values = [
        pick_value(all_raw_tags.get(tag, []), scope, target_days)
        for tag in SECURITIES_REVENUE_TAGS
    ]
    present = [v for v in values if v is not None]
    return sum(present) if present else None


def is_financing_filing(dei: dict) -> bool:
    return "financing service" in (dei.get("Subsector") or "").lower()


def has_nil_current_fact(soup, context_map: dict, tag: str) -> bool:
    for el in soup.find_all(True):
        if el.name.split(":")[-1] != tag or not el.has_attr("contextRef"):
            continue
        ctx = context_map.get(el.get("contextRef"))
        if ctx is None or not ctx["label"].startswith("Current"):
            continue
        if (el.get("xsi:nil") or "").lower() == "true":
            return True
    return False


def resolve_last_resort_revenue(soup, context_map: dict, all_raw_tags: dict, dei: dict, target_days: int):
    if is_financing_filing(dei):
        value = first_value(FINANCING_REVENUE_TAGS, all_raw_tags, "current", target_days)
        if value is not None:
            return value, FINANCING_REVENUE_SOURCE

    if has_nil_current_fact(soup, context_map, "SalesAndRevenue"):
        gross_profit = first_value(["GrossProfit"], all_raw_tags, "current", target_days)
        cost_of_revenue = first_value(COST_OF_REVENUE_TAGS, all_raw_tags, "current", target_days)
        if gross_profit is not None and gross_profit > 0 and cost_of_revenue is None:
            return gross_profit, GROSS_PROFIT_AS_REVENUE_SOURCE
        return NIL_REVENUE_VALUE, NIL_REVENUE_SOURCE

    return None, None


def is_insurance_filing(dei: dict) -> bool:
    return "insurance" in (dei.get("Subsector") or "").lower()


def compute_insurance_total_income(all_raw_tags: dict, scope: str, target_days: int):
    values = [
        pick_value(all_raw_tags.get(tag, []), scope, target_days)
        for tag in INSURANCE_TOTAL_INCOME_TAGS
    ]
    present = [v for v in values if v is not None]
    return sum(present) if present else None


def resolve_operating_profit(all_raw_tags: dict, dei: dict, target_days: int, scope: str = "current"):
    reported = first_value(OPERATING_PROFIT_TAGS, all_raw_tags, scope, target_days)
    if reported is not None:
        return reported, "tag:ProfitFromOperation"

    if is_financial_entity(dei):
        return None, None

    pretax = first_value(PRETAX_PROFIT_TAGS, all_raw_tags, scope, target_days)
    if pretax is None:
        return None, None

    finance_costs = first_value(FINANCE_COST_TAGS, all_raw_tags, scope, target_days) or 0.0
    finance_income = first_value(FINANCE_INCOME_TAGS, all_raw_tags, scope, target_days) or 0.0
    equity_method = sum(
        pick_value(all_raw_tags.get(tag, []), scope, target_days) or 0.0 for tag in EQUITY_METHOD_TAGS
    )
    return pretax + finance_costs - finance_income - equity_method, "derived:pretax_plus_finance_net"


def resolve_depreciation_amortization(all_raw_tags: dict, target_days: int):
    total = first_value([DEPRECIATION_AMORTIZATION_TOTAL_TAG], all_raw_tags, "current", target_days)
    if total is not None:
        return total
    parts = [
        first_value([tag], all_raw_tags, "current", target_days)
        for tag in (DEPRECIATION_TAG, AMORTIZATION_TAG)
    ]
    present = [p for p in parts if p is not None]
    return sum(present) if present else None


def derive_shares(net_income, eps, eps_digits=None):
    if not net_income or not eps:
        return None, None
    ratio = net_income / eps
    if ratio <= 0:
        return None, None
    for scale in EPS_SCALE_FACTORS:
        shares = ratio / scale
        if SHARES_MIN <= shares <= SHARES_MAX:
            source = "derived:net_income/basic_eps"
            if scale != 1:
                source += f"_x{int(scale)}"
            if eps_digits is not None and eps_digits < EPS_MIN_SIGNIFICANT_DIGITS:
                source += ":low_precision"
            return round(shares), source
    return None, None


def resolve_core_operating_profit(all_raw_tags: dict, dei: dict, target_days: int, scope: str = "current"):
    if is_financial_entity(dei):
        return None

    gross_profit = first_value(["GrossProfit"], all_raw_tags, scope, target_days)
    selling = first_value(SELLING_EXPENSE_TAGS, all_raw_tags, scope, target_days)
    admin = first_value(GENERAL_ADMIN_EXPENSE_TAGS, all_raw_tags, scope, target_days)

    if gross_profit is None or (selling is None and admin is None):
        return None
    return gross_profit - abs(selling or 0.0) - abs(admin or 0.0)


def compute_extended_metrics(all_raw_tags: dict, dei: dict, target_days: int) -> dict:
    operating_profit, operating_source = resolve_operating_profit(all_raw_tags, dei, target_days)
    operating_profit_prior, _ = resolve_operating_profit(all_raw_tags, dei, target_days, "prior")
    net_income = first_value(NET_INCOME_PARENT_TAGS, all_raw_tags, "current", target_days)
    eps_entry = first_entry(EPS_TAGS, all_raw_tags)
    eps = eps_entry["v"] if eps_entry else None
    eps_digits = eps_entry["s"] if eps_entry else None
    shares, shares_source = derive_shares(net_income, eps, eps_digits)
    return {
        "operating_profit": operating_profit,
        "operating_profit_source": operating_source,
        "operating_profit_prior": operating_profit_prior,
        "net_income_parent": net_income,
        "net_income_parent_prior": first_value(NET_INCOME_PARENT_TAGS, all_raw_tags, "prior", target_days),
        "core_operating_profit": resolve_core_operating_profit(all_raw_tags, dei, target_days, "current"),
        "core_operating_profit_prior": resolve_core_operating_profit(all_raw_tags, dei, target_days, "prior"),
        "depreciation_amortization": resolve_depreciation_amortization(all_raw_tags, target_days),
        "derived_shares": shares,
        "derived_shares_source": shares_source,
    }


def read_dei(soup) -> dict:
    out = {}
    for el in soup.find_all(True):
        if el.name.startswith("idx-dei:") or (el.prefix or "") == "idx-dei":
            out[el.name.split(":")[-1]] = el.text.strip()
    return out


def parse_date(value):
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def is_audited_flag(dei: dict):
    text = (dei.get("TypeOfReportOnFinancialStatements") or "").lower()
    if not text:
        return None
    return "unaudit" not in text and "tidak diaudit" not in text


def build_unit_map(soup) -> dict:
    unit_map = {}
    for unit in soup.find_all("unit"):
        measure = unit.find("measure")
        if measure is not None:
            unit_map[unit.get("id")] = measure.text.strip()
    return unit_map


def build_context_map(soup, year: int) -> dict:
    context_map = {}
    for ctx in soup.find_all("context"):
        ctx_id = ctx.get("id")
        if ctx.find("scenario") is not None or ctx.find("segment") is not None:
            context_map[ctx_id] = {"label": "Dimensional", "days": 0, "end": None}
            continue

        instant = ctx.find("instant")
        start_date = ctx.find("startDate")
        end_date = ctx.find("endDate")

        if instant is not None:
            kind, date_text, days = "Instant", instant.text.strip(), 0
        elif end_date is not None:
            kind, date_text = "Duration", end_date.text.strip()
            start = parse_date(start_date.text.strip()) if start_date is not None else None
            end = parse_date(date_text)
            days = (end - start).days if start and end else 0
        else:
            continue

        scope = "Current" if date_text.startswith(str(year)) else "Prior"
        context_map[ctx_id] = {
            "label": f"{scope} {kind}",
            "days": days,
            "end": parse_date(date_text),
        }
    return context_map


def derive_period_end(context_map: dict):
    ends = [
        c["end"] for c in context_map.values()
        if c["label"].startswith("Current") and c["end"] is not None
    ]
    return max(ends) if ends else None


def current_ytd_days(context_map: dict) -> int:
    days = [c["days"] for c in context_map.values() if c["label"] == "Current Duration"]
    return max(days) if days else 0


def extract_raw_tags(soup, context_map: dict, unit_map: dict) -> dict:
    all_raw_tags = {}
    for el in soup.find_all(True):
        if not el.has_attr("contextRef"):
            continue
        ctx = context_map.get(el.get("contextRef"))
        if ctx is None or ctx["label"] == "Dimensional":
            continue
        try:
            val = float(el.text.strip())
        except ValueError:
            continue
        clean_tag = el.name.split(":")[-1]
        all_raw_tags.setdefault(clean_tag, []).append(
            {
                "v": val,
                "c": ctx["label"],
                "d": ctx["days"],
                "u": unit_map.get(el.get("unitRef")),
                "s": significant_digits(el.text.strip()),
            }
        )
    return all_raw_tags


def detect_currency(dei: dict, all_raw_tags: dict) -> str:
    desc = (dei.get("DescriptionOfPresentationCurrency") or "").upper()
    has_usd = "USD" in desc
    has_idr = "IDR" in desc or "RUPIAH" in desc
    if has_usd != has_idr:
        return "USD" if has_usd else "IDR"

    counts = {}
    for entries in all_raw_tags.values():
        for e in entries:
            unit = e["u"] or ""
            if unit.startswith("iso4217:"):
                code = unit.split(":")[-1]
                counts[code] = counts.get(code, 0) + 1

    if not counts:
        return "IDR"
    top = max(counts, key=counts.get)
    return top if top in SUPPORTED_CURRENCIES else "IDR"


def filter_by_currency(all_raw_tags: dict, currency: str) -> dict:
    target = f"iso4217:{currency}"
    filtered = {}
    for tag, entries in all_raw_tags.items():
        kept = [
            e for e in entries
            if not (e["u"] or "").startswith("iso4217:") or e["u"] == target
        ]
        if kept:
            filtered[tag] = kept
    return filtered


def parse_localized_number(raw: str):
    text = raw.strip()
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        head, _, tail = text.rpartition(",")
        text = text.replace(",", "") if len(tail) == 3 else f"{head}.{tail}"
    try:
        return float(text)
    except ValueError:
        return None


def read_dei_fx_rate(dei: dict):
    raw = dei.get(FX_RATE_TAG)
    if not raw:
        return None
    value = parse_localized_number(raw)
    if value is None:
        logger.warning("Unparseable DEI conversion rate: %r", raw)
        return None
    if value <= 0:
        return None

    candidates = [value * scale for scale in FX_RATE_SCALE_CANDIDATES] + [1 / value]
    for candidate in candidates:
        if FX_RATE_MIN <= candidate <= FX_RATE_MAX:
            return candidate

    logger.warning("DEI conversion rate %r has no plausible interpretation, ignoring", raw)
    return None


def valid_report_date(period_end, letter_date):
    if letter_date is None:
        return None
    if period_end is None:
        return letter_date
    lag = (letter_date - period_end).days
    if 0 <= lag <= REPORT_DATE_MAX_LAG_DAYS:
        return letter_date
    return None


def pick_fx_date(period_end, release_date):
    if FX_DATE_BASIS == "release":
        if release_date is not None:
            if period_end is None:
                return release_date, "release"
            lag = (release_date - period_end).days
            if 0 <= lag <= MAX_RELEASE_LAG_DAYS:
                return release_date, "release"
            logger.warning(
                "Release date %s is %d days from period end %s, using period end",
                release_date, lag, period_end,
            )
        return period_end, FALLBACK_BASIS
    return period_end, "period_end"


def resolve_fx(currency: str, dei: dict, label: str, period_end, release_date) -> dict:
    """
    Resolves the exchange rate stored with a filing.

    Order of preference:
      1. The closing rate declared by the company (DEI), when it agrees with
         the forex_engine rate at period end within FX_DEI_MAX_DEVIATION.
      2. The forex_engine rate at the date chosen by pick_fx_date.

    Returns a dict with:
      rate                    rate stored in fx_rate_at_report
      rate_date               date the stored rate applies to
      basis                   fx_rate_basis value
      reported_rate           normalized DEI rate, or None
      engine_period_end_rate  forex_engine rate at period end, or None
    """
    if currency != "USD":
        return {
            "rate": 1,
            "rate_date": None,
            "basis": None,
            "reported_rate": None,
            "engine_period_end_rate": None,
        }

    as_of, basis = pick_fx_date(period_end, release_date)
    if as_of is None:
        raise FxRateError("no usable date to resolve the exchange rate")

    engine_quote = get_fx_quote(as_of)
    dei_rate = read_dei_fx_rate(dei)

    reference_rate = None
    if period_end is not None:
        if as_of == period_end:
            reference_rate = engine_quote.rate
        else:
            try:
                reference_rate = get_fx_quote(period_end).rate
            except FxRateError:
                logger.warning(
                    "%s: engine rate unavailable at period end %s", label, period_end
                )

    if dei_rate is not None and reference_rate is not None:
        deviation = abs(reference_rate / dei_rate - 1)
        if deviation <= FX_DEI_MAX_DEVIATION:
            return {
                "rate": dei_rate,
                "rate_date": period_end,
                "basis": FX_BASIS_DEI,
                "reported_rate": dei_rate,
                "engine_period_end_rate": reference_rate,
            }
        logger.warning(
            "%s: DEI rate %s differs from engine rate %s at period end %s by %.2f%%, "
            "using engine rate",
            label, f"{dei_rate:,.2f}", f"{reference_rate:,.2f}", period_end, deviation * 100,
        )
    elif dei_rate is not None:
        logger.warning(
            "%s: DEI rate %s cannot be validated without a period end engine rate, "
            "using engine rate",
            label, f"{dei_rate:,.2f}",
        )

    return {
        "rate": engine_quote.rate,
        "rate_date": engine_quote.rate_date,
        "basis": basis,
        "reported_rate": dei_rate,
        "engine_period_end_rate": reference_rate,
    }


def load_soup(full_path):
    with open(full_path, "r", encoding="utf-8") as f:
        return BeautifulSoup(f.read(), "lxml-xml")


def prior_for_term(term: str, golden_metrics: dict, metric_tags: dict, all_raw_tags: dict, target_days: int):
    if golden_metrics.get(term) is None:
        return None
    tag = metric_tags.get(term)
    if tag is None:
        return None
    return pick_prior_value(all_raw_tags.get(tag, []), target_days)


def parse_filing(full_path, ticker: str, year: int, period: str, mappings: dict):
    soup = load_soup(full_path)

    dei = read_dei(soup)
    context_map = build_context_map(soup, year)
    unit_map = build_unit_map(soup)
    raw_tags = extract_raw_tags(soup, context_map, unit_map)

    currency = detect_currency(dei, raw_tags)
    all_raw_tags = filter_by_currency(raw_tags, currency)
    target_days = current_ytd_days(context_map)

    period_end = parse_date(dei.get("CurrentPeriodEndDate")) or derive_period_end(context_map)
    letter_date = parse_date(dei.get("DateOfBoardOfDirectorsStatementLetter"))
    release_date = valid_report_date(period_end, letter_date)
    report_date_discarded = letter_date is not None and release_date is None

    label = f"{ticker} [{period} {year}]"
    fx = resolve_fx(currency, dei, label, period_end, release_date)

    bank = is_bank_filing(dei)
    bank_revenue = compute_bank_revenue(all_raw_tags, "current", target_days) if bank else None
    securities = is_securities_filing(dei)
    securities_revenue = (
        compute_securities_revenue(all_raw_tags, "current", target_days) if securities else None
    )
    if bank and bank_revenue is None:
        logger.warning(
            "%s: bank filing but required interest tags not found, using standard revenue resolution",
            label,
        )

    golden_metrics = {
        "currency": currency,
        "fx_rate_at_report": fx["rate"],
        "fx_rate_date": fx["rate_date"],
        "fx_rate_basis": fx["basis"],
        "fx_rate_reported": fx["reported_rate"],
        "fx_rate_engine_period_end": fx["engine_period_end_rate"],
        "_report_date_discarded": report_date_discarded,
    }
    revenue_tag_source = None

    metric_tags = {}
    for term, candidate_tags in mappings.items():
        value, tag_source = resolve_metric(candidate_tags, all_raw_tags)
        if value is not None:
            golden_metrics[term] = value
            metric_tags[term] = tag_source
        if term == "revenue":
            revenue_tag_source = tag_source

    if bank_revenue is not None:
        golden_metrics["revenue"] = bank_revenue
        revenue_tag_source = BANK_REVENUE_SOURCE
    elif securities_revenue is not None:
        golden_metrics["revenue"] = securities_revenue
        revenue_tag_source = SECURITIES_REVENUE_SOURCE
    elif "revenue" not in golden_metrics:
        value, tag_source = resolve_revenue_fallback(all_raw_tags)
        if value is not None:
            golden_metrics["revenue"] = value
        revenue_tag_source = tag_source
        if "revenue" not in golden_metrics:
            last_resort = compute_securities_revenue(all_raw_tags, "current", target_days)
            if last_resort is not None:
                golden_metrics["revenue"] = last_resort
                revenue_tag_source = SECURITIES_REVENUE_SOURCE
            elif is_insurance_filing(dei):
                insurance_income = compute_insurance_total_income(
                    all_raw_tags, "current", target_days
                )
                if insurance_income is not None:
                    golden_metrics["revenue"] = insurance_income
                    revenue_tag_source = INSURANCE_REVENUE_SOURCE
        if "revenue" not in golden_metrics:
            value, tag_source = resolve_last_resort_revenue(
                soup, context_map, all_raw_tags, dei, target_days
            )
            if tag_source is not None:
                golden_metrics["revenue"] = value
                revenue_tag_source = tag_source

    for term, candidates in EXTRA_METRICS.items():
        if term not in golden_metrics:
            value, extra_tag = resolve_metric(candidates, all_raw_tags)
            if value is not None:
                golden_metrics[term] = value
                metric_tags[term] = extra_tag

    if revenue_tag_source == GROSS_PROFIT_AS_REVENUE_SOURCE:
        golden_metrics.pop("gross_profit", None)

    if bank:
        bank_interest_expense = first_value(
            BANK_INTEREST_EXPENSE_TAGS, all_raw_tags, "current", target_days
        )
        if bank_interest_expense is not None:
            golden_metrics["interest_expense"] = abs(bank_interest_expense)

    if "capital_expenditure" not in golden_metrics:
        net_ppe = first_value([NET_PPE_CASH_FLOW_TAG], all_raw_tags, "current", target_days)
        if net_ppe is not None and net_ppe < 0:
            golden_metrics["capital_expenditure"] = -net_ppe

    extended = compute_extended_metrics(all_raw_tags, dei, target_days)

    if golden_metrics.get("operating_profit") is None:
        golden_metrics["operating_profit"] = extended["operating_profit"]
        golden_metrics["operating_profit_source"] = extended["operating_profit_source"]
        golden_metrics["operating_profit_prior"] = (
            extended["operating_profit_prior"] if extended["operating_profit"] is not None else None
        )
    else:
        golden_metrics["operating_profit_source"] = "taxonomy_mapping"
        golden_metrics["operating_profit_prior"] = prior_for_term(
            "operating_profit", golden_metrics, metric_tags, all_raw_tags, target_days
        )

    golden_metrics["net_income_prior"] = prior_for_term(
        "net_income", golden_metrics, metric_tags, all_raw_tags, target_days
    )
    golden_metrics["gross_profit_prior"] = prior_for_term(
        "gross_profit", golden_metrics, metric_tags, all_raw_tags, target_days
    )
    golden_metrics["net_income_parent"] = extended["net_income_parent"]
    golden_metrics["net_income_parent_prior"] = extended["net_income_parent_prior"]
    golden_metrics["core_operating_profit"] = extended["core_operating_profit"]
    golden_metrics["core_operating_profit_prior"] = extended["core_operating_profit_prior"]

    if golden_metrics.get("depreciation_amortization") is None:
        golden_metrics["depreciation_amortization"] = extended["depreciation_amortization"]

    if not golden_metrics.get("outstanding_shares"):
        golden_metrics["outstanding_shares"] = extended["derived_shares"]
        golden_metrics["outstanding_shares_source"] = extended["derived_shares_source"]
    else:
        golden_metrics["outstanding_shares_source"] = "taxonomy_mapping"

    if revenue_tag_source == BANK_REVENUE_SOURCE:
        golden_metrics["revenue_prior"] = compute_bank_revenue(all_raw_tags, "prior", target_days)
    elif revenue_tag_source == SECURITIES_REVENUE_SOURCE:
        golden_metrics["revenue_prior"] = compute_securities_revenue(
            all_raw_tags, "prior", target_days
        )
    elif revenue_tag_source == INSURANCE_REVENUE_SOURCE:
        golden_metrics["revenue_prior"] = compute_insurance_total_income(
            all_raw_tags, "prior", target_days
        )
    elif revenue_tag_source == FINANCING_REVENUE_SOURCE:
        golden_metrics["revenue_prior"] = first_value(
            FINANCING_REVENUE_TAGS, all_raw_tags, "prior", target_days
        )
    elif revenue_tag_source == GROSS_PROFIT_AS_REVENUE_SOURCE:
        golden_metrics["revenue_prior"] = first_value(
            ["GrossProfit"], all_raw_tags, "prior", target_days
        )
    else:
        golden_metrics["revenue_prior"] = resolve_prior_revenue(
            revenue_tag_source, all_raw_tags, target_days
        )

    golden_metrics["report_date"] = release_date
    golden_metrics["period_end"] = period_end
    golden_metrics["report_type"] = dei.get("TypeOfReportOnFinancialStatements")
    golden_metrics["rounding_level"] = dei.get("LevelOfRoundingUsedInFinancialStatements")
    golden_metrics["sector"] = dei.get("Sector")
    golden_metrics["subsector"] = dei.get("Subsector")
    golden_metrics["is_audited"] = is_audited_flag(dei)

    return golden_metrics, revenue_tag_source


def inspect_filing(ticker: str, year: int, period: str, keyword: str = "") -> None:
    full_path = XBRL_DIR / str(year) / period / f"{ticker}_{year}_{period}.xbrl"
    if not full_path.exists():
        print(f"File not found: {full_path}")
        return

    soup = load_soup(full_path)
    dei = read_dei(soup)
    context_map = build_context_map(soup, year)
    unit_map = build_unit_map(soup)
    raw_tags = extract_raw_tags(soup, context_map, unit_map)

    currency = detect_currency(dei, raw_tags)
    filtered = filter_by_currency(raw_tags, currency)
    units_present = sorted({e["u"] for entries in raw_tags.values() for e in entries if e["u"]})
    target_days = current_ytd_days(context_map)

    period_end = parse_date(dei.get("CurrentPeriodEndDate")) or derive_period_end(context_map)
    letter_date = parse_date(dei.get("DateOfBoardOfDirectorsStatementLetter"))
    release_date = valid_report_date(period_end, letter_date)

    print(f"File: {full_path}")
    print(f"Detected currency: {currency}")
    print(f"DescriptionOfPresentationCurrency: {dei.get('DescriptionOfPresentationCurrency')}")
    print(f"CurrentPeriodEndDate (DEI): {dei.get('CurrentPeriodEndDate')}")
    print(f"Period end (derived from contexts): {derive_period_end(context_map)}")
    print(f"Current cumulative duration (days): {target_days}")
    print(f"DateOfBoardOfDirectorsStatementLetter: {dei.get('DateOfBoardOfDirectorsStatementLetter') or '-'}")
    print(f"DEI conversion rate (raw): {dei.get(FX_RATE_TAG) or '-'}")
    parsed_rate = read_dei_fx_rate(dei)
    print(f"DEI conversion rate (normalized): {'-' if parsed_rate is None else f'{parsed_rate:,.2f}'}")

    try:
        fx = resolve_fx(currency, dei, f"{ticker} [{period} {year}]", period_end, release_date)
        engine_pe = fx["engine_period_end_rate"]
        print(
            f"FX decision: basis={fx['basis'] or '-'}, rate={fx['rate']:,.2f}, "
            f"rate_date={fx['rate_date'] or '-'}, "
            f"engine at period end={'-' if engine_pe is None else f'{engine_pe:,.2f}'}"
        )
    except FxRateError as exc:
        print(f"FX decision: failed ({exc})")

    print(f"Units present: {units_present}")
    print(f"Sector / Subsector: {dei.get('Sector')} / {dei.get('Subsector')}")

    if is_bank_filing(dei):
        current = compute_bank_revenue(filtered, "current", target_days)
        prior = compute_bank_revenue(filtered, "prior", target_days)
        print(f"Bank revenue ({BANK_REVENUE_BASIS}) current: {current if current is None else f'{current:,.0f}'}")
        print(f"Bank revenue ({BANK_REVENUE_BASIS}) prior:   {prior if prior is None else f'{prior:,.0f}'}")

    if is_securities_filing(dei):
        sec_current = compute_securities_revenue(filtered, "current", target_days)
        sec_prior = compute_securities_revenue(filtered, "prior", target_days)
        print(f"Securities revenue current: {sec_current if sec_current is None else f'{sec_current:,.0f}'}")
        print(f"Securities revenue prior:   {sec_prior if sec_prior is None else f'{sec_prior:,.0f}'}")

    if is_insurance_filing(dei):
        ins_current = compute_insurance_total_income(filtered, "current", target_days)
        ins_prior = compute_insurance_total_income(filtered, "prior", target_days)
        print(f"Insurance total income current (fallback): {ins_current if ins_current is None else f'{ins_current:,.0f}'}")
        print(f"Insurance total income prior (fallback):   {ins_prior if ins_prior is None else f'{ins_prior:,.0f}'}")

    extended = compute_extended_metrics(filtered, dei, target_days)
    gross_profit = first_value(["GrossProfit"], filtered, "current", target_days)
    for name, value in (
        ("Gross profit", gross_profit),
        ("Operating profit", extended["operating_profit"]),
        ("Depreciation and amortization", extended["depreciation_amortization"]),
        ("Derived shares", extended["derived_shares"]),
    ):
        print(f"{name}: {'-' if value is None else f'{value:,.0f}'}")
    print(f"Operating profit source: {extended['operating_profit_source'] or '-'}")
    print(f"Shares source: {extended['derived_shares_source'] or '-'}")

    print("-" * 100)
    print(f"{'tag':<60}{'context':<20}{'days':>6}  {'unit':<14}{'value':>20}")

    key = keyword.lower()
    for tag in sorted(raw_tags):
        if key and key not in tag.lower():
            continue
        for e in raw_tags[tag]:
            print(f"{tag:<60}{e['c']:<20}{e['d']:>6}  {(e['u'] or '-'):<14}{e['v']:>20,.0f}")


def save_to_db(conn, ticker, year, period, golden_data, revenue_tag_source) -> bool:
    data = dict(golden_data)
    data["revenue_tag_source"] = revenue_tag_source
    data.setdefault("currency", "IDR")
    data.setdefault("fx_rate_at_report", 1)

    save_columns = list(_active_save_columns)
    cols = ["ticker", "year", "period"] + save_columns
    placeholders = ", ".join(["%s"] * len(cols))
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in save_columns)
    sql = (
        f"INSERT INTO financial_reports ({', '.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT (ticker, year, period) DO UPDATE SET {updates};"
    )
    values = [ticker, year, period] + [data.get(c) for c in save_columns]

    cur = conn.cursor()
    try:
        cur.execute(sql, values)
        conn.commit()
        return True
    except Exception:
        logger.exception("Failed to save %s to database", ticker)
        conn.rollback()
        return False
    finally:
        cur.close()


def process_hybrid(year: int, period: str, tickers=None) -> None:
    logger.info("Processing year=%s period=%s", year, period)

    conn = psycopg2.connect(**config)
    cur = conn.cursor()
    if tickers:
        cur.execute(
            "SELECT ticker, taxonomy_type FROM emitens WHERE is_active = TRUE AND ticker = ANY(%s);",
            (list(tickers),),
        )
    else:
        cur.execute("SELECT ticker, taxonomy_type FROM emitens WHERE is_active = TRUE;")
    active_emitens = cur.fetchall()
    cur.close()

    processed = 0
    skipped_no_file = 0
    revenue_not_found = 0
    bank_revenue_computed = 0
    securities_revenue_computed = 0
    insurance_revenue_computed = 0
    gross_profit_found = 0
    operating_profit_found = 0
    da_found = 0
    shares_found = 0
    report_dates_discarded = 0
    fx_failures = 0
    fx_date_fallbacks = 0
    fx_dei_used = 0
    fx_dei_rejected = 0
    errors = 0

    try:
        for ticker, tax_type in active_emitens:
            file_name = f"{ticker}_{year}_{period}.xbrl"
            full_path = XBRL_DIR / str(year) / period / file_name

            if not full_path.exists():
                skipped_no_file += 1
                continue

            try:
                mappings = get_taxonomy_map(tax_type)
                golden_metrics, revenue_tag_source = parse_filing(
                    full_path, ticker, year, period, mappings
                )

                if "revenue" not in golden_metrics:
                    logger.warning(
                        "%s [%s %s]: revenue not found (taxonomy_type=%s)",
                        ticker, period, year, tax_type,
                    )
                    revenue_not_found += 1

                if revenue_tag_source == BANK_REVENUE_SOURCE:
                    bank_revenue_computed += 1
                if revenue_tag_source == SECURITIES_REVENUE_SOURCE:
                    securities_revenue_computed += 1
                if revenue_tag_source == INSURANCE_REVENUE_SOURCE:
                    insurance_revenue_computed += 1

                fx_basis = golden_metrics.get("fx_rate_basis")
                if fx_basis == FX_BASIS_DEI:
                    fx_dei_used += 1
                elif golden_metrics.get("fx_rate_reported") is not None:
                    fx_dei_rejected += 1
                if fx_basis == FALLBACK_BASIS:
                    fx_date_fallbacks += 1

                if golden_metrics.get("gross_profit") is not None:
                    gross_profit_found += 1
                if golden_metrics.get("operating_profit") is not None:
                    operating_profit_found += 1
                if golden_metrics.get("depreciation_amortization") is not None:
                    da_found += 1
                if golden_metrics.get("outstanding_shares"):
                    shares_found += 1
                if golden_metrics.get("_report_date_discarded"):
                    report_dates_discarded += 1

                if save_to_db(conn, ticker, year, period, golden_metrics, revenue_tag_source):
                    processed += 1
                else:
                    errors += 1

            except FxRateError as exc:
                logger.error("%s [%s %s]: not saved, %s", ticker, period, year, exc)
                fx_failures += 1

            except Exception:
                logger.exception("Failed to process %s [%s %s]", ticker, period, year)
                errors += 1
    finally:
        conn.close()

    logger.info(
        "Finished year=%s period=%s: %d processed, %d no file, %d revenue not found, "
        "%d bank revenue computed, %d securities revenue computed, "
        "%d insurance revenue computed, %d fx failures, "
        "%d fx date fallbacks to period end, %d errors",
        year, period, processed, skipped_no_file, revenue_not_found,
        bank_revenue_computed, securities_revenue_computed,
        insurance_revenue_computed, fx_failures, fx_date_fallbacks, errors,
    )
    logger.info(
        "FX year=%s period=%s: %d filings used the DEI rate, "
        "%d had a DEI rate rejected in favour of the engine rate",
        year, period, fx_dei_used, fx_dei_rejected,
    )
    logger.info(
        "Coverage year=%s period=%s: gross profit %d, operating profit %d, "
        "depreciation and amortization %d, shares %d of %d processed, "
        "%d stale letter dates discarded",
        year, period, gross_profit_found, operating_profit_found, da_found,
        shares_found, processed, report_dates_discarded,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Mintigo XBRL bridge data loader")
    parser.add_argument(
        "--inspect",
        nargs=3,
        metavar=("TICKER", "YEAR", "PERIOD"),
        help="Print raw tags of a single filing instead of loading the database",
    )
    parser.add_argument("--keyword", default="", help="Filter tag names when using --inspect")
    parser.add_argument("--start-year", type=int, default=START_YEAR)
    parser.add_argument("--end-year", type=int, default=datetime.now().year)
    parser.add_argument("--period", nargs="+", choices=PERIODS, default=PERIODS)
    parser.add_argument("--ticker", nargs="+", default=None)
    args = parser.parse_args()

    if args.inspect:
        ticker, year, period = args.inspect
        inspect_filing(ticker, int(year), period, args.keyword)
        return

    tickers = [t.upper() for t in args.ticker] if args.ticker else None

    verify_schema()
    for year in range(args.start_year, args.end_year + 1):
        for period in args.period:
            process_hybrid(year, period, tickers)


if __name__ == "__main__":
    main()


#python arelle_loader_2.py
#python arelle_loader_2.py --start-year 2023
#python arelle_loader_2.py --start-year 2024 --end-year 2024
#python arelle_loader_2.py --start-year 2023 --end-year 2025

#python arelle_loader_2.py --start-year 2023 --period FY
#python arelle_loader_2.py --start-year 2023 --period Q1 Q3

#python arelle_loader_2.py --inspect BBCA 2023 FY
#python arelle_loader_2.py --inspect BBCA 2023 FY --keyword Revenue