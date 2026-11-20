import io
import os
import time
from collections import deque
from functools import lru_cache

import pandas as pd
import requests

from core import config

BASE_URL = os.environ.get("LSE_BASE_URL", "https://download.londonstrategicedge.com")
PER_KEY_CALLS_PER_MINUTE = int(os.environ.get("LSE_CALLS_PER_MINUTE", "55"))
REQUEST_TIMEOUT = 60
MAX_ATTEMPTS = 8
JOB_TIMEOUT_SECONDS = 180
DEFAULT_COOLDOWN_SECONDS = 20
BUSY_COOLDOWN_SECONDS = 5
MACRO_PAGE = 1000
KEY_DISABLING_CODES = {
    "key-missing",
    "key-unknown",
    "plan-ended",
    "key-withheld",
    "account-required",
}
DATE_FIELDS = ("day", "date", "released_at")
VALUE_FIELDS = ("value", "close", "actual")


class LseError(RuntimeError):
    def __init__(self, status, code, message, retry_after=None, body=None):
        super().__init__(f"LSE {status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message
        self.retry_after = retry_after
        self.body = body or {}


def load_keys():
    raw = os.environ.get("LSE_API_KEYS") or os.environ.get("LSE_API_KEY") or ""
    keys = []
    for part in raw.replace("\n", ",").replace(";", ",").split(","):
        value = part.strip().strip('"').strip("'")
        if value and value not in keys:
            keys.append(value)
    return keys


def keys_configured():
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
        if len(self.value) > 14:
            return f"{self.value[:8]}...{self.value[-4:]}"
        return "***"


class LseClient:
    def __init__(self, keys=None, base_url=None, per_key_calls=None):
        values = keys if keys is not None else load_keys()
        if not values:
            raise LseError(0, "no-keys", f"LSE_API_KEYS belum diisi di {config.ENV_PATH}")
        self._keys = [_Key(v) for v in values]
        self._base = (base_url or BASE_URL).rstrip("/")
        self._limit = per_key_calls or PER_KEY_CALLS_PER_MINUTE
        self._cursor = 0
        self._catalogues = {}
        self._session = requests.Session()

    def _wait_for(self, key, now):
        while key.calls and now - key.calls[0] >= 60:
            key.calls.popleft()
        wait = max(0.0, key.cooldown_until - now)
        if len(key.calls) >= self._limit:
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
        return LseError(response.status_code, str(code), str(message), retry_after=retry, body=body)

    def _request(self, path, params=None, forced=None):
        last_error = None
        for attempt in range(MAX_ATTEMPTS):
            key = self._acquire(forced)
            try:
                response = self._session.get(
                    self._base + path,
                    params=params,
                    headers={"X-API-Key": key.value},
                    timeout=REQUEST_TIMEOUT,
                )
            except requests.RequestException as exc:
                last_error = LseError(0, "network", f"{type(exc).__name__}: {exc}")
                time.sleep(min(2**attempt, 30))
                continue
            if response.status_code in (200, 202):
                response.lse_key = key
                return response
            error = self._error(response)
            last_error = error
            if error.status == 429:
                if error.code == "allowance-exhausted":
                    key.disabled = "kuota mingguan habis"
                else:
                    busy = error.code in ("connections-exhausted", "exports-busy")
                    default = BUSY_COOLDOWN_SECONDS if busy else DEFAULT_COOLDOWN_SECONDS
                    key.cooldown_until = time.monotonic() + (error.retry_after or default)
                if forced is not None and key.disabled:
                    raise error
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
            return self._job_csv(response.json(), response.lse_key)
        return pd.read_csv(io.BytesIO(response.content))

    def _job_csv(self, job, key):
        status_path = job.get("status_url") or f"/v3/jobs/{job['job_id']}"
        file_path = job.get("download_url") or status_path + "/file"
        deadline = time.monotonic() + JOB_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            state = self.get_json(status_path, forced=key)
            if state.get("status") == "ready":
                return pd.read_csv(io.BytesIO(self._request(file_path, forced=key).content))
            if state.get("status") in ("failed", "expired"):
                raise LseError(409, "export-failed", str(state.get("error") or state.get("status")))
            time.sleep(2)
        raise LseError(504, "job-timeout", "ekspor belum selesai dalam batas waktu")

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
            params = {"kind": kind, "limit": MACRO_PAGE, "offset": offset}
            if country:
                params["country"] = country
            if q:
                params["q"] = q
            page = self.get_json("/v3/macro", **params)
            batch = page.get("series") or []
            series.extend(batch)
            offset += len(batch)
            total = page.get("total")
            if not batch or len(batch) < MACRO_PAGE or (total is not None and offset >= total):
                break
        self._catalogues[cache_key] = series
        return series

    def resolve_macro(self, kind, country, categories=(), codes=()):
        series = self.macro_catalogue(kind, country=country)
        by_code = {item["code"]: item for item in series if "code" in item}
        for code in codes:
            if code in by_code:
                return by_code[code]
        for category in categories:
            found = [item for item in series if item.get("category") == category]
            if found:
                found.sort(
                    key=lambda item: (item.get("last") or "", item.get("stored_records") or 0),
                    reverse=True,
                )
                return found[0]
        return None

    def macro_series(self, kind, code, start=None, end=None):
        params = {"kind": kind, "code": code}
        if start is not None:
            params["from"] = pd.Timestamp(start).strftime("%Y-%m-%d")
        if end is not None:
            params["to"] = pd.Timestamp(end).strftime("%Y-%m-%d")
        return rows_to_frame(self.get_json("/v3/macro/series", **params))

    def pairs(self, market, q=None, symbol=None, limit=100):
        params = {"market": market, "limit": limit}
        if q:
            params["q"] = q
        if symbol:
            params["symbol"] = symbol
        return self.get_json("/v3/pairs", **params).get("pairs", [])


@lru_cache(maxsize=1)
def get_client():
    return LseClient()