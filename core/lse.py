import argparse
import io
import json
import os
import sys
import time
from collections import deque
from datetime import date, timedelta
from functools import lru_cache

import pandas as pd
import requests

from core import config

BASE_URL = os.environ.get("LSE_BASE_URL", "https://download.londonstrategicedge.com")
CALLS_PER_MINUTE = int(os.environ.get("LSE_CALLS_PER_MINUTE", "55"))
REQUEST_TIMEOUT = 60
MAX_ATTEMPTS = 8
DEFAULT_COOLDOWN_SECONDS = 20
BUSY_COOLDOWN_SECONDS = 5
PAGE_SIZE = 1000
OVERLAP_DAYS = 120
KEY_DISABLING_CODES = {"key-missing", "key-unknown", "plan-ended", "key-withheld", "account-required"}
DATE_FIELDS = ("day", "date", "released_at")
VALUE_FIELDS = ("value", "close", "actual")

LSE_MAP = {
    'ID_POLICY_RATE': {
        'kind': 'observation', 'country': 'ID',
        'codes': ('ID.interest_rate',), 'categories': ('interest_rate',),
        'category': 'Interest_Rate',
    },
    'ID_INFLATION_YOY': {
        'kind': 'observation', 'country': 'ID',
        'codes': ('ID.inflation_rate',), 'categories': ('inflation_rate',),
        'category': 'Inflation',
    },
    'ID_CORE_INFLATION': {
        'kind': 'observation', 'country': 'ID',
        'codes': ('ID.core_inflation_rate',), 'categories': ('core_inflation_rate',),
        'category': 'Inflation',
    },
    'ID_GDP_YOY': {
        'kind': 'observation', 'country': 'ID',
        'codes': ('ID.gdp_growth_rate',), 'categories': ('gdp_growth_rate',),
        'category': 'Growth',
    },
    'ID_UNEMPLOYMENT': {
        'kind': 'observation', 'country': 'ID',
        'codes': ('ID.unemployment_rate',), 'categories': ('unemployment_rate',),
        'category': 'Labor',
    },
    'ID_PMI_MANUFACTURING': {
        'kind': 'observation', 'country': 'ID',
        'codes': ('ID.manufacturing_pmi',), 'categories': ('manufacturing_pmi',),
        'category': 'Business_Activity',
    },
    'ID_10Y': {
        'kind': 'yield', 'country': 'ID',
        'codes': ('ID10Y',), 'categories': (),
        'category': 'Interest_Rate',
    },
    'US_POLICY_RATE': {
        'kind': 'observation', 'country': 'US',
        'codes': ('US.interest_rate',), 'categories': ('interest_rate',),
        'category': 'Interest_Rate',
    },
}


class LseError(RuntimeError):
    def __init__(self, status, code, message, retry_after=None):
        super().__init__(f"LSE {status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message
        self.retry_after = retry_after


def load_keys():
    raw = os.environ.get("LSE_API_KEYS") or os.environ.get("LSE_API_KEY") or ""
    keys = []
    for part in raw.replace("\n", ",").replace(";", ",").split(","):
        value = part.strip().strip('"').strip("'")
        if value and value not in keys:
            keys.append(value)
    return keys


def enabled():
    return bool(load_keys())


def rows_to_frame(payload):
    rows = payload
    if isinstance(payload, dict):
        rows = next(
            (
                payload[name]
                for name in ("rows", "series", "data", "observations", "values")
                if isinstance(payload.get(name), list)
            ),
            None,
        )
    if not isinstance(rows, list):
        keys = ", ".join(sorted(payload)) if isinstance(payload, dict) else type(payload).__name__
        raise LseError(200, "shape-unknown", "bentuk jawaban macro tidak dikenali: " + keys)
    frame = pd.DataFrame(rows)
    if frame.empty:
        return pd.DataFrame(columns=["Date", "Value"])
    date_col = next((c for c in DATE_FIELDS if c in frame.columns), None)
    value_col = next((c for c in VALUE_FIELDS if c in frame.columns), None)
    if date_col is None or value_col is None:
        raise LseError(
            200, "shape-unknown", "kolom tanggal atau nilai tidak ditemukan: " + ", ".join(map(str, frame.columns))
        )
    out = pd.DataFrame(
        {
            "Date": pd.to_datetime(frame[date_col], errors="coerce", utc=True).dt.tz_localize(None).dt.normalize(),
            "Value": pd.to_numeric(frame[value_col], errors="coerce"),
        }
    )
    return out.dropna().drop_duplicates("Date", keep="last").sort_values("Date").reset_index(drop=True)


class _Key:
    def __init__(self, value):
        self.value = value
        self.calls = deque()
        self.cooldown_until = 0.0
        self.disabled = None

    @property
    def label(self):
        return f"{self.value[:8]}...{self.value[-4:]}" if len(self.value) > 14 else "***"


class LseClient:
    def __init__(self, keys=None):
        values = keys if keys is not None else load_keys()
        if not values:
            raise LseError(0, "no-keys", f"LSE_API_KEYS belum diisi di {config.ENV_PATH}")
        self._keys = [_Key(v) for v in values]
        self._cursor = 0
        self._catalogues = {}
        self._session = requests.Session()

    def _wait_for(self, key, now):
        while key.calls and now - key.calls[0] >= 60:
            key.calls.popleft()
        wait = max(0.0, key.cooldown_until - now)
        if len(key.calls) >= CALLS_PER_MINUTE:
            wait = max(wait, 60 - (now - key.calls[0]) + 0.05)
        return wait

    def _acquire(self, forced=None):
        while True:
            if forced is not None:
                pool = [forced]
            else:
                pool = [k for k in self._keys if k.disabled is None]
                if not pool:
                    reasons = "; ".join(f"{k.label}: {k.disabled}" for k in self._keys)
                    raise LseError(0, "no-keys", "semua key LSE tidak bisa dipakai (" + reasons + ")")
                shift = self._cursor % len(pool)
                pool = pool[shift:] + pool[:shift]
            now = time.monotonic()
            ranked = sorted(
                ((self._wait_for(k, now), i, k) for i, k in enumerate(pool)),
                key=lambda item: (item[0], item[1]),
            )
            wait, _, key = ranked[0]
            if wait > 0:
                time.sleep(min(wait, 65))
                continue
            key.calls.append(time.monotonic())
            self._cursor += 1
            return key

    @staticmethod
    def _error(response):
        try:
            body = response.json()
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        retry = body.get("retry_after")
        header = response.headers.get("Retry-After")
        if retry is None and header and header.isdigit():
            retry = int(header)
        code = body.get("code") or body.get("detail") or f"http-{response.status_code}"
        message = body.get("message") or body.get("detail") or response.text[:200]
        return LseError(response.status_code, str(code), str(message), retry_after=retry)

    def _request(self, path, params=None, forced=None):
        last_error = None
        for attempt in range(MAX_ATTEMPTS):
            key = self._acquire(forced)
            try:
                response = self._session.get(
                    BASE_URL.rstrip("/") + path,
                    params=params,
                    headers={"X-API-Key": key.value},
                    timeout=REQUEST_TIMEOUT,
                )
            except requests.RequestException as exc:
                last_error = LseError(0, "network", f"{type(exc).__name__}: {exc}")
                time.sleep(min(2**attempt, 30))
                continue
            if response.status_code in (200, 202):
                return response
            error = self._error(response)
            last_error = error
            if error.status == 429:
                if error.code == "allowance-exhausted":
                    key.disabled = "kuota mingguan habis"
                    if forced is not None:
                        raise error
                else:
                    busy = error.code in ("connections-exhausted", "exports-busy")
                    default = BUSY_COOLDOWN_SECONDS if busy else DEFAULT_COOLDOWN_SECONDS
                    key.cooldown_until = time.monotonic() + (error.retry_after or default)
                continue
            if error.status in (401, 403) and error.code in KEY_DISABLING_CODES:
                key.disabled = error.code
                if forced is not None:
                    raise error
                continue
            if error.status in (502, 503, 504):
                time.sleep(min(2**attempt, 30))
                continue
            raise error
        raise last_error

    def get_json(self, path, forced=None, **params):
        return self._request(path, params, forced).json()

    def export_csv(self, **params):
        response = self._request("/v3/export", {**params, "format": "csv"})
        if response.status_code == 202:
            raise LseError(202, "export-job", "ekspor terlalu besar untuk langsung diunduh, perkecil rentang tanggal")
        return pd.read_csv(io.BytesIO(response.content))

    def usage(self):
        results = []
        for key in self._keys:
            try:
                data = self.get_json("/v3/usage", forced=key)
            except LseError as exc:
                data = {"error": f"{exc.code}: {exc.message}"}
            results.append((key.label, data))
        return results

    def macro_catalogue(self, kind, country=None, q=None):
        cache_key = (kind, country, q)
        if cache_key in self._catalogues:
            return self._catalogues[cache_key]
        series, offset = [], 0
        while True:
            params = {"kind": kind, "limit": PAGE_SIZE, "offset": offset}
            if country:
                params["country"] = country
            if q:
                params["q"] = q
            page = self.get_json("/v3/macro", **params)
            batch = page.get("series") or []
            series.extend(batch)
            offset += len(batch)
            total = page.get("total")
            if not batch or len(batch) < PAGE_SIZE or (total is not None and offset >= total):
                break
        self._catalogues[cache_key] = series
        return series

    def pairs(self, market, q=None, symbol=None, limit=100):
        params = {"market": market, "limit": limit}
        if q:
            params["q"] = q
        if symbol:
            params["symbol"] = symbol
        return self.get_json("/v3/pairs", **params).get("pairs", [])


@lru_cache(maxsize=1)
def client():
    return LseClient()


def resolve(ticker):
    spec = LSE_MAP.get(ticker)
    if spec is None or "kind" not in spec:
        return None
    series = client().macro_catalogue(spec["kind"], country=spec["country"])
    by_code = {item["code"]: item for item in series if "code" in item}
    for code in spec.get("codes", ()):
        if code in by_code:
            return by_code[code]
    for category in spec.get("categories", ()):
        found = [item for item in series if item.get("category") == category]
        if found:
            found.sort(key=lambda item: (item.get("last") or "", item.get("stored_records") or 0), reverse=True)
            return found[0]
    return None


def _fit(value, size):
    text = str(value or "").strip()
    return text[:size] or None


def register(cur):
    found_count, missing = 0, []
    for ticker, spec in LSE_MAP.items():
        info = resolve(ticker) if "kind" in spec else {"name": spec.get("name")}
        if info is None:
            missing.append(ticker)
            continue
        cur.execute("SELECT 1 FROM macro_dictionary WHERE ticker = %s", (ticker,))
        if cur.fetchone() is None:
            cur.execute(
                "INSERT INTO macro_dictionary (ticker, name, category, unit, frequency, data_source) "
                "VALUES (%s, %s, %s, %s, %s, 'LSE')",
                (
                    ticker,
                    _fit(info.get("name") or ticker, 80),
                    _fit(spec.get("category"), 40),
                    _fit(str(info.get("unit") or "").upper(), 20),
                    _fit(str(info.get("frequency") or "").upper(), 20),
                ),
            )
        found_count += 1
    return found_count, missing


def fetch(ticker, since, until):
    spec = LSE_MAP.get(ticker)
    if spec is None:
        return None
    start = pd.Timestamp(since).strftime("%Y-%m-%d")
    end = pd.Timestamp(until).strftime("%Y-%m-%d")
    if "market" in spec:
        frame = client().export_csv(
            market=spec["market"],
            symbol=spec["symbol"],
            timeframe=spec.get("timeframe", "1d"),
            **{"from": start, "to": end},
        )
        if frame.empty or "close" not in frame.columns or "time" not in frame.columns:
            return None
        out = pd.DataFrame(
            {
                "Date": pd.to_datetime(frame["time"], utc=True).dt.tz_localize(None).dt.normalize(),
                "Value": pd.to_numeric(frame["close"], errors="coerce"),
            }
        )
        return out.dropna().drop_duplicates("Date", keep="last").sort_values("Date").reset_index(drop=True)
    info = resolve(ticker)
    if info is None:
        return None
    params = {"kind": spec["kind"], "code": info["code"], "from": start, "to": end}
    return rows_to_frame(client().get_json("/v3/macro/series", **params))


def cmd_keys(args):
    for label, data in client().usage():
        if "error" in data:
            print(f"{label}: {data['error']}")
            continue
        weekly = data.get("weekly") or {}
        print(
            f"{label}: nama={data.get('key')} plan={data.get('plan')} "
            f"panggilan/menit={data.get('calls_per_minute')} "
            f"mingguan={weekly.get('used_bytes')}/{weekly.get('allowance_bytes')} byte "
            f"ditahan={data.get('withheld')}"
        )


def cmd_macro(args):
    series = client().macro_catalogue(args.kind, country=args.country, q=args.q)
    if not series:
        print("tidak ada series")
        return
    frame = pd.DataFrame(series)
    columns = [
        c for c in ("code", "name", "frequency", "first", "last", "latest_value", "latest_at", "unit")
        if c in frame.columns
    ]
    print(frame[columns].to_string(index=False, max_colwidth=48))
    print(f"\n{len(frame)} series")


def cmd_pairs(args):
    rows = client().pairs(args.market, q=args.q, limit=args.limit)
    if not rows:
        print("tidak ada instrumen")
        return
    for row in rows:
        print(
            f"{str(row.get('symbol')):<14} {str(row.get('name'))[:40]:<40} "
            f"{row.get('tick_first')} s.d. {row.get('tick_last')}"
        )


def cmd_probe(args):
    start = (date.today() - timedelta(days=args.days)).isoformat()
    payload = client().get_json("/v3/macro/series", kind=args.kind, code=args.code, **{"from": start})
    print("jawaban mentah (800 karakter pertama):")
    print(json.dumps(payload, ensure_ascii=False)[:800])
    print("\nhasil parsing (5 terakhir):")
    print(rows_to_frame(payload).tail(5).to_string(index=False))


def cmd_resolve(args):
    for ticker, spec in LSE_MAP.items():
        if "market" in spec:
            rows = client().pairs(spec["market"], symbol=spec["symbol"])
            print(f"{ticker:<22} {spec['market']}/{spec['symbol']:<22} {'ada' if rows else 'TIDAK ADA'}")
            continue
        found = resolve(ticker)
        if found is None:
            print(f"{ticker:<22} TIDAK DITEMUKAN (kind={spec['kind']}, country={spec['country']})")
        else:
            print(
                f"{ticker:<22} {found['code']:<30} {found.get('frequency', '')} "
                f"terakhir={found.get('last')} nilai={found.get('latest_value')}"
            )


def main():
    parser = argparse.ArgumentParser(description="Alat bantu LSE: cek key, jelajah katalog, uji parsing.")
    sub = parser.add_subparsers(dest="command", metavar="perintah")

    sub.add_parser("keys", help="batas dan pemakaian tiap key").set_defaults(func=cmd_keys)

    p = sub.add_parser("macro", help="daftar series makro")
    p.add_argument("--country", default="ID")
    p.add_argument("--kind", default="observation", choices=("observation", "yield", "release"))
    p.add_argument("--q")
    p.set_defaults(func=cmd_macro)

    p = sub.add_parser("pairs", help="cari instrumen pasar")
    p.add_argument("--market", default="forex")
    p.add_argument("--q")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_pairs)

    p = sub.add_parser("probe", help="jawaban mentah dan hasil parsing satu series")
    p.add_argument("--kind", default="observation", choices=("observation", "yield", "release"))
    p.add_argument("--code", required=True)
    p.add_argument("--days", type=int, default=120)
    p.set_defaults(func=cmd_probe)

    sub.add_parser("resolve", help="cocokkan LSE_MAP dengan katalog").set_defaults(func=cmd_resolve)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return
    try:
        args.func(args)
    except LseError as exc:
        sys.exit(str(exc))


if __name__ == "__main__":
    main()