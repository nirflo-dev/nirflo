"""Portfolio endpoints: holdings, cash (IDR dan USD), evaluation (full and quick), mix and stored history. Data per akun."""
from __future__ import annotations

import math
from contextlib import contextmanager
from datetime import date
from typing import Literal

import numpy as np
import pandas as pd
import psycopg2
from fastapi import APIRouter, Depends, HTTPException, Query
from psycopg2 import errors as pg_errors
from pydantic import BaseModel, Field

from analytics.portfolio import LOT_SIZE, cash_summary, evaluate, fx_state
from api.auth import clerk_display_name, current_clerk_id
from api.main import Json, ttl_cache  # file ini di-include di akhir api/main.py, setelah Json terdefinisi
from core import history as hist
from core import portfolio_store as store
from core import universe_store, user_store
from core.portfolio_data import load_macro_series, load_price_inputs

router = APIRouter(prefix="/api/portfolio")
_ready = False
_user_ids: dict[str, int] = {}

MIX_PREFIX = "mix:"
MIX_MAX = 6

SCHEMA_HINT = (
    "Skema tabel pengguna atau portofolio belum sesuai dengan kode. "
    "Hapus tabel lama (portfolio_evaluations, portfolio_holdings, portfolios, users) "
    "lalu jalankan: python portfolio_cli.py init"
)


@contextmanager
def handled():
    """Pastikan database tersedia dan tabel ada, lalu petakan error domain ke status HTTP."""
    global _ready
    try:
        if not hist.database_available():
            raise HTTPException(503, "Fitur portofolio memerlukan koneksi database dan tidak tersedia pada mode snapshot.")
        if not _ready:
            store.ensure_table()
            _ready = True
        yield
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except (pg_errors.UndefinedTable, pg_errors.UndefinedColumn, pg_errors.InvalidColumnReference) as exc:
        raise HTTPException(503, SCHEMA_HINT) from exc
    except psycopg2.OperationalError as exc:
        raise HTTPException(503, f"Database tidak terjangkau: {str(exc).strip()}") from exc


def current_user(clerk_id: str = Depends(current_clerk_id)) -> int:
    user_id = _user_ids.get(clerk_id)
    if user_id is None:
        with handled():
            user_id, name = user_store.ensure_user(clerk_id)
            if name is None:
                fetched = clerk_display_name(clerk_id)
                if fetched:
                    user_store.set_display_name(user_id, fetched)
        _user_ids[clerk_id] = user_id
    return user_id


def _is_mix(name: str) -> bool:
    return name.startswith(MIX_PREFIX)


def _mix_parts(name: str) -> list[str]:
    parts = [p.strip() for p in name[len(MIX_PREFIX):].split(",")]
    return sorted({p for p in parts if p})


def _clean_name(name: str, *, write: bool = False) -> str:
    """Portofolio gabungan (mix:A,B) hanya boleh dibaca. Nama biasa tidak boleh memuat koma saat ditulis."""
    name = name.strip()
    if _is_mix(name):
        if write:
            raise HTTPException(422, "Portofolio gabungan bersifat baca saja. Ubah lewat portofolio aslinya.")
        parts = _mix_parts(name)
        if not 2 <= len(parts) <= MIX_MAX:
            raise HTTPException(422, f"Gabungan memerlukan 2 sampai {MIX_MAX} portofolio.")
        return MIX_PREFIX + ",".join(parts)
    if not name or len(name) > 60:
        raise HTTPException(422, "Nama portofolio harus 1 sampai 60 karakter.")
    if write and "," in name:
        raise HTTPException(422, "Nama portofolio tidak boleh memuat koma.")
    return name


def _load(user_id: int, name: str):
    """Posisi dan saldo cash (rupiah, dolar). Untuk mix, seluruh posisi digabung dan cash
    dijumlahkan per mata uang; setiap posisi tetap membawa tanggal belinya sendiri."""
    if not _is_mix(name):
        return store.list_holdings(name, user_id=user_id), store.get_cash(name, user_id)
    parts = _mix_parts(name)
    known = set(store.list_portfolios(user_id))
    absent = [p for p in parts if p not in known]
    if absent:
        raise ValueError("Portofolio tidak ditemukan: " + ", ".join(absent))
    holdings = pd.concat([store.list_holdings(p, user_id=user_id) for p in parts], ignore_index=True)
    balances = [store.get_cash(p, user_id) for p in parts]
    return holdings, (sum(b[0] for b in balances), sum(b[1] for b in balances))


def _require_holdings(user_id: int, name: str):
    holdings, cash = _load(user_id, name)
    if holdings.empty:
        if cash[0] > 0 or cash[1] > 0:
            raise ValueError(
                f"Portofolio '{name}' baru berisi cash. Tambahkan saham untuk menghitung risiko dan Sharpe ratio."
            )
        raise ValueError(f"Portofolio '{name}' kosong. Tambahkan posisi terlebih dahulu.")
    return holdings, cash


@ttl_cache(300)
def full_evaluation(user_id: int, name: str) -> dict:
    holdings, (cash_idr, cash_usd) = _require_holdings(user_id, name)
    tickers = sorted(holdings["ticker"].unique())
    prices, no_trade = load_price_inputs(tickers, holdings["buy_date"].min())
    usdidr = load_macro_series("USDIDR") if cash_usd > 0 else None
    return evaluate(
        prices, holdings, load_macro_series("ID_RATE"), no_trade,
        cash_idr=cash_idr, cash_usd=cash_usd, usdidr=usdidr,
    )


def _verdict(low: float, high: float) -> str:
    return "positif" if low > 0 else "negatif" if high < 0 else "tidak_pasti"


@ttl_cache(300)
def quick_evaluation(user_id: int, name: str) -> dict:
    """Estimasi dari statistik universe, tanpa membaca harga. Logika sama dengan cmd_quick di portfolio_cli.py."""
    holdings, (cash_idr, cash_usd) = _require_holdings(user_id, name)
    tickers = sorted(holdings["ticker"].unique())
    as_of, stats = universe_store.read_stats(tickers)
    if as_of is None:
        raise ValueError("Statistik universe belum ada. Jalankan: python -m pipeline.build_universe")
    problems = [
        f"{t}: " + ("tidak ada di universe" if t not in stats.index else stats.loc[t, "note"])
        for t in tickers
        if t not in stats.index or not stats.loc[t, "eligible"]
    ]
    if problems:
        raise ValueError("Estimasi cepat tidak dapat dihitung. " + "; ".join(problems))

    corr = universe_store.read_correlation(tickers)
    shares = holdings.groupby("ticker")["lots"].sum().reindex(tickers) * LOT_SIZE
    value = shares * stats.loc[tickers, "last_price"]
    weights = (value / value.sum()).to_numpy()
    sigma = stats.loc[tickers, "std_weekly"].to_numpy()
    mu = stats.loc[tickers, "mean_excess_weekly"].to_numpy()
    cov = corr.to_numpy() * np.outer(sigma, sigma)
    variance = float(weights @ cov @ weights)
    if not np.isfinite(variance) or variance <= 0:
        raise ValueError("Korelasi antar saham belum lengkap di universe. Gunakan mode Lengkap.")

    k = math.sqrt(52)
    mean_week = float(weights @ mu)
    per_week = mean_week / math.sqrt(variance)
    n_obs = int(stats.loc[tickers, "n_weeks"].min())
    se = math.sqrt((1 + 0.5 * per_week**2) / n_obs)
    low, high = (per_week - 1.96 * se) * k, (per_week + 1.96 * se) * k
    risk = weights * (cov @ weights) / variance
    count = len(tickers)
    avg_corr = (corr.to_numpy().sum() - count) / (count * (count - 1)) if count > 1 else None
    annual_vol = math.sqrt(variance) * k

    fx = None
    if cash_usd > 0:
        rate, fx_date, fx_vol = fx_state(load_macro_series("USDIDR"))
        # Mode cepat tidak membaca harga, jadi korelasi saham dan kurs tidak dihitung (dianggap 0).
        fx = {"rate": rate, "date": fx_date, "vol": fx_vol, "rho": None}

    return {
        "as_of": str(as_of), "window": None,
        "weights": {t: float(w) for t, w in zip(tickers, weights)},
        "risk_contribution": {t: float(r) for t, r in zip(tickers, risk)},
        "correlation": {"tickers": tickers, "matrix": corr.round(4).to_numpy().tolist(), "average": avg_corr},
        "sharpe": {
            "since_buy": None, "equal_weight": None,
            "current_weights": {
                "sharpe": per_week * k, "ci_low": low, "ci_high": high, "verdict": _verdict(low, high),
                "annual_excess_return": mean_week * 52, "annual_vol": annual_vol,
                "start": None, "end": None, "n_obs": n_obs, "basis": "mingguan",
            },
        },
        **cash_summary(float(value.sum()), cash_idr, cash_usd, fx, annual_vol, mean_week * 52),
        "price_jumps": [], "illiquid_tickers": [], "warnings": [],
    }


def _invalidate() -> None:
    full_evaluation.clear()   # type: ignore[attr-defined]
    quick_evaluation.clear()  # type: ignore[attr-defined]


class NewHolding(BaseModel):
    ticker: str = Field(min_length=1, max_length=10)
    lots: float = Field(gt=0)
    buy_date: date


class CashBody(BaseModel):
    amount: float = Field(ge=0, le=1e15)
    currency: Literal["IDR", "USD"] = "IDR"


@router.get("")
def portfolios(user: int = Depends(current_user)):
    with handled():
        rows = store.list_summaries(user)
    return Json(rows)


@router.get("/fx")
def fx_rate(user: int = Depends(current_user)):
    """Kurs USD/IDR terakhir, dipakai UI untuk menampilkan setara rupiah cash dolar."""
    with handled():
        rate, fx_date, _ = fx_state(load_macro_series("USDIDR"))
    return Json({"pair": "USD/IDR", "rate": rate, "date": fx_date})


@router.get("/{name}/holdings")
def holdings(name: str, user: int = Depends(current_user)):
    name = _clean_name(name)
    with handled():
        df, _ = _load(user, name)
    df = df.drop(columns=["user_id"]).assign(buy_date=df["buy_date"].dt.strftime("%Y-%m-%d"))
    return Json(df.to_dict("records"))


@router.post("/{name}/holdings")
def add_holding(name: str, body: NewHolding, user: int = Depends(current_user)):
    with handled():
        added = store.add_holdings(_clean_name(name, write=True), [(body.ticker, body.lots, body.buy_date.isoformat())], user)
    _invalidate()
    return Json({"added": added})


@router.put("/{name}/cash")
def set_cash(name: str, body: CashBody, user: int = Depends(current_user)):
    with handled():
        store.set_cash(_clean_name(name, write=True), body.amount, user, body.currency)
    _invalidate()
    return Json({"cash": body.amount, "currency": body.currency})


@router.delete("/holdings/{holding_id}")
def remove_holding(holding_id: int, user: int = Depends(current_user)):
    with handled():
        if not store.remove_holding(holding_id, user_id=user):
            raise HTTPException(404, "Posisi tidak ditemukan.")  # pesan sama untuk id milik akun lain
    _invalidate()
    return Json({"removed": holding_id})


@router.get("/{name}/evaluate")
def evaluate_portfolio(name: str, mode: str = Query("full", pattern="^(full|quick)$"), user: int = Depends(current_user)):
    name = _clean_name(name)
    with handled():
        data = full_evaluation(user, name) if mode == "full" else quick_evaluation(user, name)
    return Json({"mode": mode, **data})


@router.get("/{name}/history")
def history(name: str, limit: int = Query(60, ge=1, le=500), user: int = Depends(current_user)):
    with handled():
        df = store.list_evaluations(_clean_name(name), limit, user).sort_values("as_of")
    return Json({
        "x": [d.isoformat() for d in df["as_of"]],
        "sharpe_since_buy": df["sharpe_since_buy"].tolist(),
        "sharpe_current_weights": df["sharpe_current_weights"].tolist(),
        "sharpe_equal_weight": df["sharpe_equal_weight"].tolist(),
        "avg_correlation": df["avg_correlation"].tolist(),
        "warnings": df["warnings"].tolist(),
    })


@router.post("/{name}/history")
def save_snapshot(name: str, user: int = Depends(current_user)):
    name = _clean_name(name, write=True)
    with handled():
        result = full_evaluation(user, name)
        blocks = result["sharpe"]
        if blocks.get("since_buy") is None or blocks.get("equal_weight") is None:
            raise ValueError("Evaluasi lengkap belum dapat dihitung untuk portofolio ini.")
        store.save_evaluation(name, result, user)
    return Json({"saved": result["as_of"]})