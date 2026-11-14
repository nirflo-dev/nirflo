import numpy as np
import pandas as pd
import pytest

from regime_signal import (
    RegimeConfig,
    RegimeDataError,
    apply_band,
    build_positions,
    evaluate,
    hysteresis,
    prepare_probabilities,
    run_analysis,
    walk_forward,
    walk_forward_probabilities,
)

STRATEGIES = ["macd_only", "mrs_only", "macd_mrs"]


def synthetic(n=1100, seed=3, stay=0.97):
    rng = np.random.default_rng(seed)
    state = np.zeros(n, int)
    for i in range(1, n):
        state[i] = state[i - 1] if rng.random() < stay else 1 - state[i - 1]
    r = np.where(state == 0, rng.normal(0.0006, 0.008, n), rng.normal(-0.0004, 0.028, n))
    idx = pd.bdate_range("2019-01-01", periods=n)
    close = pd.Series(1000 * np.cumprod(1 + r), index=idx)
    return close, pd.Series(state, index=idx)


CFG = RegimeConfig(min_window=400, refit_every=42)


@pytest.fixture(scope="module")
def prepared():
    close, state = synthetic()
    probs, fits = prepare_probabilities(close, CFG)
    return close, state, probs, fits


def test_hysteresis_synthetic():
    p = pd.Series([0.1, 0.5, 0.71, 0.6, 0.45, 0.39, 0.5, 0.69, 0.75, 0.5, 0.2])
    assert hysteresis(p, 0.7, 0.4).tolist() == [0, 0, 1, 1, 1, 0, 0, 0, 1, 1, 0]


def test_apply_band():
    s = pd.Series([0.0, 0.30, 0.32, 0.36, 0.50, 0.49, 0.0, 0.05, 0.0])
    out = apply_band(s, 0.10).tolist()
    assert out == [0.0, 0.30, 0.30, 0.30, 0.50, 0.50, 0.0, 0.05, 0.0]


def test_no_lookahead():
    close, _ = synthetic()
    ret = close.pct_change().dropna()
    full = walk_forward_probabilities(ret, CFG)
    cut = 800
    part = walk_forward_probabilities(ret.iloc[:cut], CFG)
    pd.testing.assert_frame_equal(full.iloc[:cut].dropna(), part.dropna(), atol=1e-8)
    held_full = build_positions(close, full.dropna(), CFG)
    held_part = build_positions(close.iloc[: cut + 1], part.dropna(), CFG)
    common = held_part.index.intersection(held_full.index)
    pd.testing.assert_frame_equal(
        held_full.loc[common, STRATEGIES], held_part.loc[common, STRATEGIES], atol=1e-8
    )


def test_label_consistency(prepared):
    _, state, probs, _ = prepared
    truth_calm = (state.reindex(probs.index) == 0).astype(float)
    assert np.corrcoef(probs["ahead"], truth_calm)[0, 1] > 0.4
    assert probs["ahead"].between(0, 1).all()


def test_fit_diagnostics(prepared):
    _, _, _, fits = prepared
    assert len(fits) > 5
    assert (fits["calm_vol"] < fits["volatile_vol"]).all()
    assert fits[["calm_stay", "volatile_stay", "calm_share"]].apply(lambda c: c.between(0, 1)).all().all()
    assert (fits["volatile_duration"] > 1).all()


def test_insufficient_data():
    close, _ = synthetic(n=300)
    with pytest.raises(RegimeDataError):
        run_analysis(close, CFG)


def test_execution_lag_shifts_positions(prepared):
    close, _, probs, fits = prepared
    one = evaluate(close, probs, fits, RegimeConfig(min_window=400, refit_every=42, execution_lag=1))
    two = evaluate(close, probs, fits, RegimeConfig(min_window=400, refit_every=42, execution_lag=2))
    pd.testing.assert_series_equal(
        two["held"]["macd_mrs"].iloc[1:], one["held"]["macd_mrs"].shift(1).fillna(0.0).iloc[1:]
    )


def test_evaluate_outputs(prepared):
    close, _, probs, fits = prepared
    res = evaluate(close, probs, fits, CFG)
    assert set(res["metrics"].index) == {"buy_hold", "macd_only", "mrs_only", "macd_mrs", "bh_matched"}
    assert {"Sharpe kotor", "Return kotor total", "Jumlah entry"} <= set(res["metrics"].columns)
    assert res["held"].iloc[0][STRATEGIES].abs().sum() == 0
    assert (res["held"]["macd_mrs"] >= 0).all()
    assert list(res["summary"].index) == ["Tenang", "Gejolak"]
    assert res["gate_switches_per_year"] >= 0


def test_events_match_fills(prepared):
    close, _, probs, fits = prepared
    res = evaluate(close, probs, fits, CFG)
    held = res["held"]["macd_mrs"]
    active = held.abs() > 1e-6
    first_active = held.index[active & ~active.shift(1, fill_value=False)]
    index = res["components"].index
    for entry, first in zip(res["events"]["entries"], first_active):
        assert index.get_loc(entry) == max(index.get_loc(first) - 1, 0)
    assert len(res["events"]["entries"]) == len(first_active)


def test_knobs_reduce_turnover(prepared):
    close, _, probs, fits = prepared
    base = evaluate(close, probs, fits, CFG)
    tuned = evaluate(
        close, probs, fits,
        RegimeConfig(min_window=400, refit_every=42, size_by_prob=False, rebalance_band=0.1, prob_smooth_span=5),
    )
    assert tuned["metrics"].loc["mrs_only", "Turnover tahunan"] < base["metrics"].loc["mrs_only", "Turnover tahunan"]