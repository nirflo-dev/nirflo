import logging
import time
import warnings
from dataclasses import dataclass, replace

import numpy as np
import pandas as pd
from statsmodels.tools.sm_exceptions import ValueWarning
from statsmodels.tsa.regime_switching.markov_regression import MarkovRegression

logger = logging.getLogger(__name__)

warnings.filterwarnings("ignore", category=ValueWarning, module="statsmodels.tsa.base.tsa_model")

POSITION_EPS = 1e-6


@dataclass(frozen=True)
class RegimeConfig:
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    vol_span: int = 20
    z_clip: float = 3.0
    vol_target: float = 0.15
    periods_per_year: int = 252
    enter_prob: float = 0.7
    exit_prob: float = 0.4
    min_window: int = 500
    refit_every: int = 21
    return_scale: float = 100.0
    long_only: bool = True
    cost_buy: float = 0.0015
    cost_sell: float = 0.0025
    slippage: float = 0.001
    risk_free: float = 0.0
    regime_cutoff: float = 0.5
    max_iter: int = 200
    prob_smooth_span: int = 1
    size_by_prob: bool = True
    rebalance_band: float = 0.0
    execution_lag: int = 2
    warm_start: bool = False


class RegimeDataError(ValueError):
    pass


def macd_hist(close: pd.Series, fast: int, slow: int, signal: int) -> pd.Series:
    line = close.ewm(span=fast, adjust=False).mean() - close.ewm(span=slow, adjust=False).mean()
    return line - line.ewm(span=signal, adjust=False).mean()


def realized_vol(ret: pd.Series, span: int) -> pd.Series:
    return ret.ewm(span=span).std()


def direction_signal(close: pd.Series, ret: pd.Series, cfg: RegimeConfig) -> pd.Series:
    vol = realized_vol(ret, cfg.vol_span)
    hist = macd_hist(close, cfg.macd_fast, cfg.macd_slow, cfg.macd_signal)
    z = (hist / (close * vol)).clip(-cfg.z_clip, cfg.z_clip)
    signal = np.tanh(z)
    if cfg.long_only:
        signal = signal.clip(lower=0.0)
    return signal


def hysteresis(prob: pd.Series, enter: float, exit_: float) -> pd.Series:
    out = np.zeros(len(prob))
    on = False
    for i, value in enumerate(prob.to_numpy(dtype=float)):
        if np.isnan(value):
            on = False
        elif not on and value > enter:
            on = True
        elif on and value < exit_:
            on = False
        out[i] = 1.0 if on else 0.0
    return pd.Series(out, index=prob.index)


def apply_band(series: pd.Series, band: float) -> pd.Series:
    out = np.zeros(len(series))
    current = 0.0
    for i, value in enumerate(series.to_numpy(dtype=float)):
        switches_state = (abs(value) < POSITION_EPS) != (abs(current) < POSITION_EPS)
        if switches_state or abs(value - current) >= band:
            current = value
        out[i] = current
    return pd.Series(out, index=series.index)


def _new_model(y: pd.Series) -> MarkovRegression:
    return MarkovRegression(y, k_regimes=2, trend="c", switching_variance=True)


def _calm_index(params: pd.Series) -> int:
    return int(np.argmin([params["sigma2[0]"], params["sigma2[1]"]]))


def _calm_probabilities(model_result, calm: int) -> tuple[np.ndarray, np.ndarray]:
    filtered = model_result.filtered_marginal_probabilities
    filtered = filtered.to_numpy() if hasattr(filtered, "to_numpy") else np.asarray(filtered)
    transition = np.asarray(model_result.regime_transition)[:, :, 0]
    ahead = filtered @ transition[calm, :]
    return filtered[:, calm], ahead


def _fit_record(date, fit, cfg: RegimeConfig) -> dict:
    params = fit.params
    transition = np.asarray(fit.regime_transition)[:, :, 0]
    calm = _calm_index(params)
    other = 1 - calm
    stay_calm = float(transition[calm, calm])
    stay_volatile = float(transition[other, other])
    leave_calm = max(1.0 - stay_calm, 1e-9)
    leave_volatile = max(1.0 - stay_volatile, 1e-9)
    annual = np.sqrt(cfg.periods_per_year)
    return {
        "date": date,
        "calm_stay": stay_calm,
        "volatile_stay": stay_volatile,
        "calm_duration": 1.0 / leave_calm,
        "volatile_duration": 1.0 / leave_volatile,
        "calm_vol": float(np.sqrt(params[f"sigma2[{calm}]"]) / cfg.return_scale * annual),
        "volatile_vol": float(np.sqrt(params[f"sigma2[{other}]"]) / cfg.return_scale * annual),
        "calm_mean": float(params[f"const[{calm}]"] / cfg.return_scale),
        "volatile_mean": float(params[f"const[{other}]"] / cfg.return_scale),
        "calm_share": float(leave_volatile / (leave_calm + leave_volatile)),
    }


def summarize_timings(timings: list[dict], total_seconds: float) -> None:
    if not timings:
        return
    frame = pd.DataFrame(timings)
    converged = frame["converged"].fillna(False).astype(bool)
    ok = frame[converged]
    failed = frame[~converged]
    logger.info(
        "walk_forward: %d refit, %d tidak konvergen, total %.1f dtk",
        len(frame), len(failed), total_seconds,
    )
    logger.info(
        "  fit rata-rata: konvergen %.2f dtk (iterasi median %s) | tidak konvergen %.2f dtk | smooth %.2f dtk",
        ok["fit_s"].mean() if len(ok) else float("nan"),
        ok["iterations"].median() if len(ok) else "-",
        failed["fit_s"].mean() if len(failed) else float("nan"),
        frame["smooth_s"].mean(),
    )
    logger.info(
        "  waktu di fit tidak konvergen: %.1f dtk dari total fit %.1f dtk",
        failed["fit_s"].sum(), frame["fit_s"].sum(),
    )


def walk_forward(ret: pd.Series, cfg: RegimeConfig) -> tuple[pd.DataFrame, pd.DataFrame]:
    ret = ret.dropna()
    n = len(ret)
    if n < cfg.min_window + cfg.refit_every:
        raise RegimeDataError(
            f"Data {n} hari tidak cukup, butuh minimal {cfg.min_window + cfg.refit_every} hari bursa"
        )

    y = ret * cfg.return_scale
    filtered = np.full(n, np.nan)
    ahead = np.full(n, np.nan)
    last_params = None
    records = []
    timings = []
    total_refits = len(range(cfg.min_window, n, cfg.refit_every))
    started = time.perf_counter()

    for start in range(cfg.min_window, n, cfg.refit_every):
        stop = min(start + cfg.refit_every, n)
        iterations = None
        converged = None
        smooth_seconds = 0.0

        t0 = time.perf_counter()
        try:
            model = _new_model(y.iloc[:start])
            if cfg.warm_start and last_params is not None:
                fit = model.fit(start_params=last_params.to_numpy(), disp=False, maxiter=cfg.max_iter)
            else:
                fit = model.fit(disp=False, maxiter=cfg.max_iter)
            retvals = getattr(fit, "mle_retvals", None) or {}
            converged = bool(retvals.get("converged", True))
            iterations = retvals.get("iterations")
            if not converged:
                raise RuntimeError("optimisasi tidak konvergen")
            if not np.isfinite(fit.params.to_numpy()).all():
                raise RuntimeError("parameter tidak finite")
            last_params = fit.params
            records.append(_fit_record(ret.index[start - 1], fit, cfg))
        except Exception as exc:
            logger.warning("Refit gagal pada indeks %d (%s), memakai parameter sebelumnya", start, exc)
        fit_seconds = time.perf_counter() - t0

        if last_params is not None:
            t1 = time.perf_counter()
            try:
                result = _new_model(y.iloc[:stop]).smooth(last_params.to_numpy())
                calm = _calm_index(last_params)
                f_calm, a_calm = _calm_probabilities(result, calm)
                filtered[start:stop] = f_calm[start:stop]
                ahead[start:stop] = a_calm[start:stop]
            except Exception as exc:
                logger.warning("Filtering gagal pada indeks %d (%s)", start, exc)
            smooth_seconds = time.perf_counter() - t1

        timings.append({
            "window": start,
            "fit_s": fit_seconds,
            "smooth_s": smooth_seconds,
            "iterations": iterations,
            "converged": converged,
        })
        logger.debug(
            "refit %d/%d window=%d fit=%.2fs smooth=%.2fs iter=%s converged=%s",
            len(timings), total_refits, start, fit_seconds, smooth_seconds, iterations, converged,
        )

    summarize_timings(timings, time.perf_counter() - started)

    probs = pd.DataFrame({"filtered": filtered, "ahead": ahead}, index=ret.index)
    fits = pd.DataFrame(records).set_index("date") if records else pd.DataFrame()
    return probs, fits


def walk_forward_probabilities(ret: pd.Series, cfg: RegimeConfig) -> pd.DataFrame:
    return walk_forward(ret, cfg)[0]


def _clean_close(close: pd.Series) -> pd.Series:
    close = close.dropna()
    return close[close > 0]


def prepare_probabilities(close: pd.Series, cfg: RegimeConfig) -> tuple[pd.DataFrame, pd.DataFrame]:
    ret = _clean_close(close).pct_change().dropna()
    probs, fits = walk_forward(ret, cfg)
    probs = probs.dropna()
    if probs.empty:
        raise RegimeDataError("Model rezim gagal dihitung untuk seluruh window")
    return probs, fits


def build_components(close: pd.Series, probs: pd.DataFrame, cfg: RegimeConfig) -> pd.DataFrame:
    ret = close.pct_change()
    hist = macd_hist(close, cfg.macd_fast, cfg.macd_slow, cfg.macd_signal)
    direction = direction_signal(close, ret, cfg)
    daily_vol = realized_vol(ret, cfg.vol_span)
    scale = (cfg.vol_target / (daily_vol * np.sqrt(cfg.periods_per_year))).clip(upper=1.0)

    calm = probs["ahead"]
    if cfg.prob_smooth_span > 1:
        calm = calm.ewm(span=cfg.prob_smooth_span, adjust=False).mean()
    gate = hysteresis(calm, cfg.enter_prob, cfg.exit_prob)

    frame = pd.DataFrame({
        "close": close.reindex(probs.index),
        "hist": hist.reindex(probs.index),
        "direction": direction.reindex(probs.index),
        "scale": scale.reindex(probs.index),
        "calm": calm,
        "gate": gate,
    })
    return frame


def _positions(close: pd.Series, probs: pd.DataFrame, cfg: RegimeConfig):
    if cfg.execution_lag < 1:
        raise ValueError("execution_lag minimal 1")
    comp = build_components(close, probs, cfg)
    weight = comp["calm"] if cfg.size_by_prob else 1.0

    target = pd.DataFrame(index=comp.index)
    target["buy_hold"] = 1.0
    target["macd_only"] = comp["direction"] * comp["scale"]
    target["mrs_only"] = comp["gate"] * weight * comp["scale"]
    target["macd_mrs"] = comp["direction"] * comp["gate"] * weight * comp["scale"]
    target = target.fillna(0.0)

    if cfg.rebalance_band > 0:
        for column in ("macd_only", "mrs_only", "macd_mrs"):
            target[column] = apply_band(target[column], cfg.rebalance_band)

    held = target.shift(cfg.execution_lag).fillna(0.0)
    held["bh_matched"] = float(held["macd_mrs"].abs().mean())
    return held, target, comp


def build_positions(close: pd.Series, probs: pd.DataFrame, cfg: RegimeConfig) -> pd.DataFrame:
    return _positions(close, probs, cfg)[0]


def net_returns(held: pd.DataFrame, ret: pd.Series, cfg: RegimeConfig) -> tuple[pd.DataFrame, pd.DataFrame]:
    ret = ret.reindex(held.index).fillna(0.0)
    daily_rf = cfg.risk_free / cfg.periods_per_year
    idle = (1.0 - held.abs().clip(upper=1.0)) * daily_rf
    gross = held.mul(ret, axis=0) + idle
    delta = held.diff().fillna(held.iloc[0])
    buys = delta.clip(lower=0.0)
    sells = (-delta).clip(lower=0.0)
    costs = buys * (cfg.cost_buy + cfg.slippage) + sells * (cfg.cost_sell + cfg.slippage)
    return gross - costs, gross


def _sharpe(r: pd.Series, daily_rf: float, periods: int) -> float:
    std = r.std(ddof=1)
    if len(r) < 2 or not std > 0:
        return np.nan
    return float((r - daily_rf).mean() / std * np.sqrt(periods))


def performance_metrics(net: pd.DataFrame, gross: pd.DataFrame, held: pd.DataFrame, cfg: RegimeConfig) -> pd.DataFrame:
    rows = {}
    years = len(net) / cfg.periods_per_year
    daily_rf = cfg.risk_free / cfg.periods_per_year
    for name in net.columns:
        r = net[name]
        equity = (1.0 + r).cumprod()
        total = float(equity.iloc[-1] - 1.0)
        cagr = float(equity.iloc[-1] ** (1.0 / years) - 1.0) if years > 0 and equity.iloc[-1] > 0 else np.nan
        active = held[name].abs() > POSITION_EPS
        entries = int((active & ~active.shift(1, fill_value=False)).sum())
        rows[name] = {
            "Return bersih total": total,
            "Return kotor total": float((1.0 + gross[name]).prod() - 1.0),
            "CAGR": cagr,
            "Volatilitas": float(r.std(ddof=1) * np.sqrt(cfg.periods_per_year)),
            "Sharpe": _sharpe(r, daily_rf, cfg.periods_per_year),
            "Sharpe kotor": _sharpe(gross[name], daily_rf, cfg.periods_per_year),
            "Max drawdown": float((equity / equity.cummax() - 1.0).min()),
            "Turnover tahunan": float(held[name].diff().abs().fillna(0.0).sum() / years) if years > 0 else np.nan,
            "Eksposur": float(active.mean()),
            "Rata-rata posisi": float(held[name].abs().mean()),
            "Jumlah entry": entries,
        }
    return pd.DataFrame(rows).T


def regime_conditional_sharpe(gross: pd.DataFrame, probs: pd.DataFrame, cfg: RegimeConfig) -> pd.DataFrame:
    predicted = probs["ahead"].shift(1).reindex(gross.index)
    valid = predicted.notna()
    regime_calm = predicted > cfg.regime_cutoff
    rows = []
    for label, mask in (("Tenang", regime_calm & valid), ("Gejolak", ~regime_calm & valid)):
        r = gross.loc[mask, "macd_only"]
        b = gross.loc[mask, "buy_hold"]
        std = r.std(ddof=1)
        rows.append({
            "Rezim (prediksi t-1)": label,
            "Hari": int(mask.sum()),
            "Sharpe MACD saja": float(r.mean() / std * np.sqrt(cfg.periods_per_year)) if len(r) > 1 and std > 0 else np.nan,
            "Rata-rata harian MACD (%)": float(r.mean() * 100) if len(r) else np.nan,
            "Rata-rata harian buy-hold (%)": float(b.mean() * 100) if len(b) else np.nan,
        })
    return pd.DataFrame(rows).set_index("Rezim (prediksi t-1)")


def trade_events(held: pd.DataFrame, comp: pd.DataFrame) -> dict:
    index = comp.index
    active = (held["macd_mrs"].abs() > POSITION_EPS).to_numpy()
    previous = np.concatenate([[False], active[:-1]])

    def fill_dates(mask: np.ndarray) -> pd.DatetimeIndex:
        positions = np.maximum(np.flatnonzero(mask) - 1, 0)
        return index[positions]

    wants = (comp["direction"] > POSITION_EPS).to_numpy()
    wanted_before = np.concatenate([[False], wants[:-1]])
    blocked = wants & ~wanted_before & (comp["gate"].to_numpy() < 0.5)
    return {
        "entries": fill_dates(active & ~previous),
        "exits": fill_dates(~active & previous),
        "blocked": index[np.flatnonzero(blocked)],
    }


def regime_summary(fits: pd.DataFrame) -> pd.DataFrame:
    if fits.empty:
        return pd.DataFrame()
    med = fits.median(numeric_only=True)
    rows = {}
    for label, key in (("Tenang", "calm"), ("Gejolak", "volatile")):
        rows[label] = {
            "Volatilitas tahunan": float(med[f"{key}_vol"]),
            "Rata-rata harian": float(med[f"{key}_mean"]),
            "Peluang bertahan": float(med[f"{key}_stay"]),
            "Durasi rata-rata (hari)": float(med[f"{key}_duration"]),
            "Porsi waktu": float(med["calm_share"] if key == "calm" else 1.0 - med["calm_share"]),
        }
    return pd.DataFrame(rows).T


def evaluate(close: pd.Series, probs: pd.DataFrame, fits: pd.DataFrame, cfg: RegimeConfig) -> dict:
    close = _clean_close(close)
    held, target, comp = _positions(close, probs, cfg)
    net, gross = net_returns(held, close.pct_change(), cfg)
    years = len(comp) / cfg.periods_per_year
    switches = float(comp["gate"].diff().abs().fillna(0.0).sum() / years) if years > 0 else np.nan
    return {
        "config": cfg,
        "probs": probs,
        "fits": fits,
        "components": comp,
        "held": held,
        "target": target,
        "net": net,
        "gross": gross,
        "equity": (1.0 + net).cumprod(),
        "events": trade_events(held, comp),
        "metrics": performance_metrics(net, gross, held, cfg),
        "regime_table": regime_conditional_sharpe(gross, probs, cfg),
        "summary": regime_summary(fits),
        "gate_switches_per_year": switches,
        "refits": len(fits),
    }


def run_analysis(close: pd.Series, cfg: RegimeConfig | None = None) -> dict:
    cfg = cfg or RegimeConfig()
    probs, fits = prepare_probabilities(close, cfg)
    return evaluate(close, probs, fits, cfg)


def with_overrides(cfg: RegimeConfig, **kwargs) -> RegimeConfig:
    return replace(cfg, **kwargs)