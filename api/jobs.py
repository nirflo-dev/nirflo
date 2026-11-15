"""Job walk-forward rezim di proses terpisah, supaya API tidak ikut macet."""
import multiprocessing as mp
import time
from concurrent.futures import ProcessPoolExecutor
from threading import Lock


def _prepare(ticker: str, refit_every: int) -> dict:
    import pandas as pd
    from analytics import regime_signal as rs
    from core import history as hist

    frame = hist.load_prices([ticker], columns=["close_price", "is_no_trade"]).drop(columns="ticker")
    frame = frame[frame["close_price"].notna()].copy()
    frame["close_price"] = pd.to_numeric(frame["close_price"], errors="coerce")
    frame["price_date"] = pd.to_datetime(frame["price_date"])
    traded = frame[~frame["is_no_trade"].fillna(False).astype(bool)]
    close = traded.set_index("price_date")["close_price"]
    try:
        probs, fits = rs.prepare_probabilities(close, rs.RegimeConfig(refit_every=refit_every))
    except rs.RegimeDataError as exc:
        return {"invalid": str(exc)}
    return {"result": (close, probs, fits)}


class RegimeJobs:
    def __init__(self, workers: int = 2, ttl: int = 3600, retry_after: int = 30):
        self._pool = ProcessPoolExecutor(max_workers=workers, mp_context=mp.get_context("spawn"))
        self._jobs: dict[tuple, dict] = {}
        self._lock = Lock()
        self.ttl, self.retry_after = ttl, retry_after

    def get(self, ticker: str, refit_every: int) -> dict:
        """Mulai job bila belum ada atau sudah kedaluwarsa; selalu mengembalikan snapshot status."""
        key, now = (ticker, refit_every), time.time()
        with self._lock:
            job = self._jobs.get(key)
            if job and job["state"] != "running":
                limit = self.ttl if job["state"] == "ready" else self.retry_after
                if now - job["done_at"] > limit:
                    job = None
            if job is None:
                job = {"state": "running", "started": now}
                self._jobs[key] = job
                self._pool.submit(_prepare, ticker, refit_every).add_done_callback(
                    lambda f, j=job: self._finish(j, f)
                )
            return dict(job)

    def _finish(self, job: dict, future) -> None:
        try:
            out = future.result()
            update = (
                {"state": "ready", "result": out["result"]}
                if "result" in out
                else {"state": "invalid", "message": out["invalid"]}
            )
        except Exception as exc:  # noqa: BLE001
            update = {"state": "error", "message": f"Perhitungan rezim gagal: {exc}"}
        with self._lock:
            job.update(update, done_at=time.time())

    def clear(self) -> None:
        with self._lock:
            self._jobs.clear()