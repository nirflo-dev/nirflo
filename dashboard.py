import math
from datetime import date, timedelta

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import psycopg2
import streamlit as st
from plotly.subplots import make_subplots

import signal_report as sr

st.set_page_config(page_title="Mintigo", layout="wide", initial_sidebar_state="expanded")

PAGES = ("Emiten", "Sinyal", "Makro", "Status data")
SIGNAL_STATUSES = ("STRONG", "MODERATE")
HORIZONS = (1, 5, 10, 20)
PERIOD_ORDER = {"Q1": 1, "Q2": 2, "Q3": 3, "FY": 4}
CONFIRMATION_ORDER = ("CONFIRMED", "RECOVERING", "DIVERGENT", "UNCONFIRMED", "--")
MIN_MATURED = 30

FLOW_COLUMNS = ("revenue", "gross_profit", "operating_profit", "net_income", "operating_cash_flow", "capital_expenditure")
BALANCE_COLUMNS = ("total_assets", "total_liabilities", "total_equity", "cash_and_equivalents")

PRICE_RANGES = {"1 bulan": 31, "3 bulan": 92, "6 bulan": 183, "1 tahun": 365, "3 tahun": 1095, "Semua": None}
MACRO_RANGES = {"1 tahun": 1, "3 tahun": 3, "5 tahun": 5, "10 tahun": 10, "Semua": None}

MACRO_ROLES = {
    "key": ("indicator_ticker", "ticker", "indicator_code", "indicator", "series_id", "code", "symbol"),
    "date": ("record_date", "date", "obs_date", "observation_date", "period_date", "as_of_date"),
    "value": ("value", "val", "close", "data_value", "indicator_value"),
}
MACRO_NAME_CANDIDATES = ("indicator_name", "name", "full_name", "description", "label", "title")
MACRO_COLORS = ("#2f81f7", "#2ea043", "#d29922", "#da3633", "#a371f7", "#39c5cf", "#db61a2", "#8b949e")

ACCENT = "#2f81f7"
ACCENT_LIGHT = "#79b8ff"
MUTED = "#8b949e"
UP = "#2ea043"
DOWN = "#da3633"
CHART_HEIGHT = 380

st.markdown(
    """
    <style>
    #MainMenu {visibility: hidden;}
    footer {visibility: hidden;}
    .block-container {padding-top: 2rem; padding-bottom: 3rem;}
    </style>
    """,
    unsafe_allow_html=True,
)


# ---------------------------------------------------------------------------
# Data access
# ---------------------------------------------------------------------------

def query(sql: str, params: tuple | None = None) -> pd.DataFrame:
    conn = psycopg2.connect(**sr.config)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            columns = [d[0] for d in cur.description]
            return pd.DataFrame(cur.fetchall(), columns=columns)
    finally:
        conn.close()


def to_numeric(frame: pd.DataFrame, columns) -> pd.DataFrame:
    for column in columns:
        if column in frame.columns:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame


@st.cache_data(ttl=600)
def table_columns(table: str) -> list[str]:
    frame = query(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = %s ORDER BY ordinal_position;",
        (table,),
    )
    return frame["column_name"].tolist()


@st.cache_data(ttl=600)
def load_tickers() -> pd.DataFrame:
    return query(
        "SELECT ticker, COALESCE(NULLIF(sector, ''), 'Unknown') AS sector "
        "FROM emitens WHERE is_active = TRUE ORDER BY ticker;"
    )


@st.cache_data(ttl=300)
def load_prices(ticker: str) -> pd.DataFrame:
    frame = query(
        "SELECT price_date, close_price, volume, is_no_trade FROM daily_prices "
        "WHERE ticker = %s AND close_price IS NOT NULL ORDER BY price_date;",
        (ticker,),
    )
    frame = to_numeric(frame, ["close_price", "volume"])
    frame["price_date"] = pd.to_datetime(frame["price_date"])
    return frame


@st.cache_data(ttl=300)
def load_reports(ticker: str) -> pd.DataFrame:
    columns = ", ".join(("year", "period", "currency") + BALANCE_COLUMNS + FLOW_COLUMNS)
    frame = query(
        f"SELECT {columns} FROM financial_reports WHERE ticker = %s ORDER BY year, period;",
        (ticker,),
    )
    frame = to_numeric(frame, BALANCE_COLUMNS + FLOW_COLUMNS)
    return frame.drop_duplicates(["year", "period"], keep="last")


@st.cache_data(ttl=300)
def load_scan_history(ticker: str) -> pd.DataFrame:
    try:
        frame = query("SELECT * FROM scan_results WHERE ticker = %s;", (ticker,))
    except Exception:
        return pd.DataFrame()
    frame = to_numeric(frame, ["growth_yoy", "z_score", "sur"])
    if frame.empty:
        return frame
    frame["order"] = frame["period"].map(PERIOD_ORDER)
    return frame.sort_values(["year", "order"], ascending=False)


@st.cache_data(ttl=300)
def load_all_signals() -> list[dict]:
    return sr.fetch_signals(None, SIGNAL_STATUSES)


@st.cache_data(ttl=300, show_spinner="Membaca database")
def load_signal_performance(entry_lag: int, max_gap: int, benchmark: str | None, since: date) -> pd.DataFrame:
    signals = sr.fetch_signals(since, SIGNAL_STATUSES)
    if not signals:
        return pd.DataFrame()

    tickers = sorted({s["ticker"] for s in signals})
    start = min(s["detected"] for s in signals) - timedelta(days=10)
    series = sr.fetch_series(tickers, start, True)
    sectors = sr.fetch_sectors(tickers)
    market_last = sr.fetch_market_last()
    bench = sr.fetch_series([benchmark], start, False).get(benchmark) if benchmark else None

    records = []
    for s in signals:
        e = sr.evaluate_signal(
            s, series.get(s["ticker"]), bench, sectors.get(s["ticker"], "Unknown"),
            market_last, entry_lag, HORIZONS, max_gap,
        )
        row = {k: e[k] for k in (
            "id", "ticker", "year", "period", "sector", "status", "direction", "trigger_source",
            "z", "sur", "confirmation", "detected", "entry_date", "entry_price", "flags",
        )}
        for h in HORIZONS:
            row[f"ret_{h}d"] = e["returns"].get(h)
            row[f"excess_{h}d"] = e["excess"].get(h)
        records.append(row)
    return pd.DataFrame(records)


@st.cache_data(ttl=300)
def load_scan_periods() -> list[tuple]:
    frame = query("SELECT DISTINCT year, period FROM scan_results;")
    frame["order"] = frame["period"].map(PERIOD_ORDER)
    frame = frame.sort_values(["year", "order"], ascending=False)
    return list(zip(frame["year"], frame["period"]))


@st.cache_data(ttl=300)
def load_scan_results(year: int, period: str) -> pd.DataFrame:
    frame = query("SELECT * FROM scan_results WHERE year = %s AND period = %s;", (year, period))
    return to_numeric(frame, [
        "growth_yoy", "sector_median_growth", "z_score", "sur",
        "revenue_curr", "revenue_prev", "net_income_curr", "net_income_prev",
    ])


# ---------------------------------------------------------------------------
# Formatting and chart helpers
# ---------------------------------------------------------------------------

def fmt_pct(value, decimals: int = 1, signed: bool = False) -> str:
    if value is None or pd.isna(value):
        return "-"
    sign = "+" if signed else ""
    return f"{value * 100:{sign}.{decimals}f}%"


def fmt_number(value, decimals: int = 1) -> str:
    if value is None or pd.isna(value):
        return "-"
    return f"{value:,.{decimals}f}"


def base_layout(fig: go.Figure, **kwargs) -> go.Figure:
    options = {"height": CHART_HEIGHT, "margin": dict(l=10, r=10, t=20, b=10)}
    options.update(kwargs)
    fig.update_layout(**options)
    return fig


def show(fig: go.Figure) -> None:
    st.plotly_chart(fig, use_container_width=True)


def pick(columns: list[str], candidates: tuple) -> str | None:
    return next((c for c in candidates if c in columns), None)


def summarize(values: pd.Series, negative: bool) -> dict:
    values = values.dropna()
    n = len(values)
    if n == 0:
        return {"n": 0, "mean": None, "median": None, "win": None, "ci": 0.0}
    ci = 1.96 * float(values.std(ddof=1)) / math.sqrt(n) if n >= 2 else 0.0
    win = float((values < 0).mean() if negative else (values > 0).mean())
    return {"n": n, "mean": float(values.mean()), "median": float(values.median()), "win": win, "ci": ci}


# ---------------------------------------------------------------------------
# Page: Emiten
# ---------------------------------------------------------------------------

def build_annual(reports: pd.DataFrame) -> pd.DataFrame:
    """Full-year (FY) rows with growth and ratio columns."""
    annual = reports[reports["period"] == "FY"].copy()
    if annual.empty:
        return annual

    annual = annual.sort_values("year").reset_index(drop=True)
    annual["label"] = annual["year"].astype(int).astype(str)

    revenue_base = annual["revenue"].where(annual["revenue"] > 0)
    equity_base = annual["total_equity"].where(annual["total_equity"] > 0)
    annual["gross_margin"] = annual["gross_profit"] / revenue_base
    annual["net_margin"] = annual["net_income"] / revenue_base
    annual["roe"] = annual["net_income"] / equity_base
    annual["der"] = annual["total_liabilities"] / equity_base

    prior = annual[["year", "revenue", "net_income"]].copy()
    prior["year"] += 1
    annual = annual.merge(prior, on="year", how="left", suffixes=("", "_py"))
    annual["revenue_yoy"] = annual["revenue"] / annual["revenue_py"].where(annual["revenue_py"] > 0) - 1
    annual["net_income_yoy"] = annual["net_income"] / annual["net_income_py"].where(annual["net_income_py"] > 0) - 1
    return annual


def price_chart(prices: pd.DataFrame, window: int | None, show_average: bool, signals: list[dict]) -> go.Figure:
    frame = prices.copy()
    if show_average:
        frame["ma50"] = frame["close_price"].rolling(50).mean()
        frame["ma200"] = frame["close_price"].rolling(200).mean()
    if window is not None:
        frame = frame[frame["price_date"] >= frame["price_date"].max() - pd.Timedelta(days=window)]

    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, row_heights=[0.78, 0.22], vertical_spacing=0.03)
    fig.add_trace(go.Scatter(
        x=frame["price_date"], y=frame["close_price"], name="Penutupan", mode="lines",
        line=dict(color=ACCENT, width=1.8), hovertemplate="%{x|%d %b %Y}<br>%{y:,.0f}<extra></extra>",
    ), row=1, col=1)

    if show_average:
        for name, column, color in (("MA 50", "ma50", ACCENT_LIGHT), ("MA 200", "ma200", MUTED)):
            fig.add_trace(go.Scatter(
                x=frame["price_date"], y=frame[column], name=name, mode="lines",
                line=dict(color=color, width=1, dash="dot"),
            ), row=1, col=1)

    if signals:
        marks = pd.DataFrame(signals)
        marks["detected"] = pd.to_datetime(marks["detected"])
        marks = marks[marks["detected"] >= frame["price_date"].min()]
        if not marks.empty:
            matched = pd.merge_asof(
                marks.sort_values("detected"), frame[["price_date", "close_price"]],
                left_on="detected", right_on="price_date", direction="forward",
            ).dropna(subset=["close_price"])
            fig.add_trace(go.Scatter(
                x=matched["price_date"], y=matched["close_price"], mode="markers", name="Sinyal",
                marker=dict(symbol="triangle-up", size=11, color=UP, line=dict(width=1, color="white")),
                text=matched["period"] + " " + matched["year"].astype(str) + " " + matched["status"],
                hovertemplate="%{text}<extra></extra>",
            ), row=1, col=1)

    traded = frame[~frame["is_no_trade"].fillna(False).astype(bool)]
    fig.add_trace(go.Bar(
        x=traded["price_date"], y=traded["volume"], name="Volume", marker_color=MUTED, showlegend=False,
    ), row=2, col=1)
    fig.update_yaxes(title_text="Harga", row=1, col=1)
    fig.update_yaxes(title_text="Volume", row=2, col=1)
    return base_layout(fig, height=480, legend=dict(orientation="h", y=1.06))


def price_metrics(prices: pd.DataFrame) -> None:
    closes = prices["close_price"].reset_index(drop=True)
    last = closes.iloc[-1]

    def change(rows: int):
        return last / closes.iloc[-1 - rows] - 1 if len(closes) > rows else None

    year = closes.iloc[-252:]
    a, b, c, d, e = st.columns(5)
    a.metric("Penutupan terakhir", fmt_number(last, 0), help=f"Per {prices['price_date'].iloc[-1]:%d %b %Y}")
    b.metric("1 hari", fmt_pct(change(1), signed=True))
    c.metric("1 bulan", fmt_pct(change(21), signed=True))
    d.metric("1 tahun", fmt_pct(change(252), signed=True))
    e.metric("Kisaran 52 minggu", f"{fmt_number(year.min(), 0)} - {fmt_number(year.max(), 0)}")


def statement_charts(annual: pd.DataFrame, unit_label: str) -> None:
    divisor = 1e9

    def bars(columns: dict, margin_line: bool = False) -> go.Figure:
        fig = make_subplots(specs=[[{"secondary_y": margin_line}]])
        for (column, name), color in zip(columns.items(), (ACCENT, ACCENT_LIGHT, MUTED)):
            fig.add_trace(go.Bar(x=annual["label"], y=annual[column] / divisor, name=name, marker_color=color), secondary_y=False)
        if margin_line:
            fig.add_trace(go.Scatter(
                x=annual["label"], y=annual["net_margin"] * 100, name="Margin laba bersih (%)",
                mode="lines+markers", line=dict(color=UP, width=1.5),
            ), secondary_y=True)
            fig.update_yaxes(title_text="Margin (%)", secondary_y=True, showgrid=False)
        fig.update_yaxes(title_text=unit_label, secondary_y=False)
        fig.update_xaxes(type="category")
        return base_layout(fig, barmode="group", legend=dict(orientation="h", y=1.1))

    tab_income, tab_balance, tab_cash = st.tabs(["Laba rugi", "Neraca", "Arus kas"])
    with tab_income:
        show(bars({"revenue": "Pendapatan", "net_income": "Laba bersih"}, margin_line=True))
    with tab_balance:
        show(bars({"total_assets": "Total aset", "total_liabilities": "Total liabilitas", "total_equity": "Ekuitas"}))
    with tab_cash:
        show(bars({"operating_cash_flow": "Arus kas operasi", "capital_expenditure": "Belanja modal"}))


def statement_table(annual: pd.DataFrame, unit_label: str) -> pd.DataFrame:
    divisor = 1e9
    amount_rows = (
        ("Pendapatan", "revenue"), ("Laba kotor", "gross_profit"), ("Laba sebelum pajak", "operating_profit"),
        ("Laba bersih", "net_income"), ("Arus kas operasi", "operating_cash_flow"), ("Belanja modal", "capital_expenditure"),
        ("Total aset", "total_assets"), ("Total liabilitas", "total_liabilities"), ("Total ekuitas", "total_equity"),
        ("Kas dan setara kas", "cash_and_equivalents"),
    )
    ratio_rows = (
        ("Pertumbuhan pendapatan YoY", "revenue_yoy", "pct"), ("Pertumbuhan laba bersih YoY", "net_income_yoy", "pct"),
        ("Margin laba kotor", "gross_margin", "pct"), ("Margin laba bersih", "net_margin", "pct"),
        ("ROE (laba bersih / ekuitas akhir)", "roe", "pct"), ("Liabilitas / ekuitas (x)", "der", "ratio"),
    )

    index = [f"{name} ({unit_label})" for name, _ in amount_rows] + [name for name, _, _ in ratio_rows]
    raw = pd.DataFrame(index=index)
    text = pd.DataFrame(index=index)
    for _, row in annual.iterrows():
        raw_values, text_values = [], []
        for _, column in amount_rows:
            value = row[column] / divisor
            raw_values.append(value)
            text_values.append(fmt_number(value, 1))
        for _, column, kind in ratio_rows:
            raw_values.append(row[column])
            text_values.append(fmt_pct(row[column], 1) if kind == "pct" else fmt_number(row[column], 2))
        raw[row["label"]] = raw_values
        text[row["label"]] = text_values

    st.dataframe(text, use_container_width=True)
    return raw


def ytd_table(reports: pd.DataFrame, annual: pd.DataFrame, unit_label: str) -> pd.DataFrame | None:
    """Current-year cumulative figures against the same cumulative period last year."""
    interim = reports[reports["period"] != "FY"].copy()
    if interim.empty:
        return None

    interim["order"] = interim["period"].map(PERIOD_ORDER)
    latest = interim.sort_values(["year", "order"]).iloc[-1]
    last_full_year = int(annual["year"].max()) if not annual.empty else 0
    if int(latest["year"]) <= last_full_year:
        return None

    current = latest
    match = interim[(interim["year"] == latest["year"] - 1) & (interim["period"] == latest["period"])]
    previous = match.iloc[-1] if not match.empty else None

    rows = []
    for name, column in (("Pendapatan", "revenue"), ("Laba sebelum pajak", "operating_profit"), ("Laba bersih", "net_income")):
        now = current[column] / 1e9
        before = None if previous is None else previous[column] / 1e9
        growth = None if before is None or pd.isna(before) or before <= 0 else now / before - 1
        rows.append({
            "": f"{name} ({unit_label})",
            f"{latest['period']} {int(latest['year']) - 1}": fmt_number(before, 1),
            f"{latest['period']} {int(latest['year'])}": fmt_number(now, 1),
            "Pertumbuhan": fmt_pct(growth, 1, signed=True),
        })
    return pd.DataFrame(rows).set_index("")


def page_ticker() -> None:
    tickers = load_tickers()
    if tickers.empty:
        st.info("Belum ada emiten aktif di tabel emitens.")
        return

    top_left, top_right = st.columns([1, 3])
    ticker = top_left.selectbox("Emiten", tickers["ticker"].tolist(), label_visibility="collapsed")
    sector = tickers.loc[tickers["ticker"] == ticker, "sector"].iloc[0]
    top_right.markdown(f"### {ticker}  \n{sector}")

    tab_price, tab_financial, tab_signal = st.tabs(["Harga", "Laporan keuangan", "Sinyal"])

    with tab_price:
        prices = load_prices(ticker)
        if prices.empty:
            st.info("Belum ada data harga untuk emiten ini.")
        else:
            price_metrics(prices)
            controls = st.columns([3, 1])
            window_label = controls[0].radio("Rentang", list(PRICE_RANGES), index=3, horizontal=True, label_visibility="collapsed")
            show_average = controls[1].checkbox("Rata-rata bergerak")
            own_signals = [s for s in load_all_signals() if s["ticker"] == ticker]
            show(price_chart(prices, PRICE_RANGES[window_label], show_average, own_signals))

    with tab_financial:
        reports = load_reports(ticker)
        annual = build_annual(reports)
        if annual.empty:
            st.info("Belum ada laporan tahunan untuk emiten ini.")
        else:
            currency = annual["currency"].dropna().iloc[-1] if annual["currency"].notna().any() else "IDR"
            unit_label = f"{'Rp' if currency == 'IDR' else currency} miliar"

            statement_charts(annual, unit_label)
            raw = statement_table(annual, unit_label)

            ytd = ytd_table(reports, annual, unit_label)
            if ytd is not None:
                st.markdown("**Tahun berjalan (kumulatif)**")
                st.dataframe(ytd, use_container_width=True)

            st.download_button("Unduh tabel tahunan (CSV)", raw.to_csv(), f"{ticker}_laporan_tahunan.csv", "text/csv")

    with tab_signal:
        history = load_scan_history(ticker)
        if history.empty:
            st.info("Belum ada hasil pemindaian untuk emiten ini.")
        else:
            table = pd.DataFrame({
                "Periode": history["period"] + " " + history["year"].astype(str),
                "Status": history["status"],
                "Pemicu": history["trigger_source"],
                "Arah": history["direction"],
                "Konfirmasi laba": history["confirmation"],
                "Pertumbuhan (%)": history["growth_yoy"] * 100,
                "Z-score": history["z_score"],
                "SUR": history["sur"],
            })
            st.dataframe(
                table, use_container_width=True, hide_index=True,
                column_config={
                    "Pertumbuhan (%)": st.column_config.NumberColumn(format="%+.1f%%"),
                    "Z-score": st.column_config.NumberColumn(format="%.2f"),
                    "SUR": st.column_config.NumberColumn(format="%.2f"),
                },
            )


# ---------------------------------------------------------------------------
# Page: Sinyal
# ---------------------------------------------------------------------------

def horizon_chart(frame: pd.DataFrame, prefix: str, negative: bool) -> go.Figure:
    means, errors = [], []
    for h in HORIZONS:
        stats = summarize(frame[f"{prefix}_{h}d"], negative)
        means.append(None if stats["mean"] is None else stats["mean"] * 100)
        errors.append(stats["ci"] * 100)
    fig = go.Figure(go.Bar(
        x=[f"+{h} hari" for h in HORIZONS], y=means, marker_color=ACCENT,
        error_y=dict(type="data", array=errors, visible=True),
        hovertemplate="%{x}<br>%{y:.2f}%<extra></extra>",
    ))
    fig.add_hline(y=0, line_width=1, line_color=MUTED)
    return base_layout(fig, yaxis_title="Rata-rata return (%)", showlegend=False)


def confirmation_chart(frame: pd.DataFrame, column: str) -> go.Figure | None:
    rows = []
    for label in CONFIRMATION_ORDER:
        values = frame.loc[frame["confirmation"] == label, column].dropna()
        if len(values):
            rows.append((label, float(values.mean()) * 100, len(values)))
    if not rows:
        return None
    fig = go.Figure(go.Bar(
        x=[r[0] for r in rows], y=[r[1] for r in rows],
        marker_color=[UP if r[1] >= 0 else DOWN for r in rows],
        text=[f"{r[1]:+.1f}% (n={r[2]})" for r in rows], textposition="outside", cliponaxis=False,
    ))
    return base_layout(fig, yaxis_title="Rata-rata return (%)", showlegend=False)


def signal_performance_tab() -> None:
    a, b, c, d = st.columns(4)
    since = a.date_input("Sejak", value=date.today() - timedelta(days=365))
    hz = b.selectbox("Lama tahan", HORIZONS, index=len(HORIZONS) - 1, format_func=lambda h: f"+{h} hari bursa")
    direction = c.selectbox("Arah", ["POSITIVE", "NEGATIVE", "MIXED"])
    benchmark = d.text_input("Benchmark (kode di daily_prices)").strip().upper() or None

    with st.expander("Asumsi entry"):
        e, f = st.columns(2)
        entry_lag = e.number_input("Entry lag (hari bursa)", 0, 5, sr.DEFAULT_ENTRY_LAG)
        max_gap = f.number_input("Batas jeda entry (hari)", 1, 60, sr.DEFAULT_MAX_ENTRY_GAP_DAYS)

    df = load_signal_performance(int(entry_lag), int(max_gap), benchmark, since)
    if df.empty:
        st.info("Belum ada sinyal pada rentang ini.")
        return

    has_excess = benchmark is not None and df[[f"excess_{h}d" for h in HORIZONS]].notna().any().any()
    prefix = "excess" if has_excess else "ret"
    column = f"{prefix}_{hz}d"
    negative = direction == "NEGATIVE"

    frame = df[df["direction"] == direction]
    stats = summarize(frame[column], negative)

    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Sinyal matang", stats["n"], help=f"Sinyal yang sudah mencapai +{hz} hari bursa.")
    k2.metric("Rata-rata", fmt_pct(stats["mean"], signed=True))
    k3.metric("Median", fmt_pct(stats["median"], signed=True))
    k4.metric("Turun" if negative else "Naik", fmt_pct(stats["win"], 0), help="Porsi sinyal yang bergerak sesuai arah tesis.")

    if benchmark and not has_excess:
        st.warning(f"Harga benchmark {benchmark} tidak ditemukan, return ditampilkan tanpa penyesuaian.")
    if stats["n"] < MIN_MATURED:
        st.caption(f"Sampel {stats['n']} masih di bawah {MIN_MATURED}; belum cukup untuk menarik kesimpulan.")

    left, right = st.columns(2)
    with left:
        st.markdown("**Return menurut lama tahan**")
        show(horizon_chart(frame, prefix, negative))
    with right:
        st.markdown("**Return menurut konfirmasi laba**")
        fig = confirmation_chart(frame, column)
        if fig is None:
            st.info("Belum ada sinyal yang matang.")
        else:
            show(fig)

    table = pd.DataFrame({
        "Ticker": frame["ticker"],
        "Periode": frame["period"] + " " + frame["year"].astype(str),
        "Status": frame["status"],
        "Pemicu": frame["trigger_source"],
        "Z-score": frame["z"],
        "SUR": frame["sur"],
        "Konfirmasi laba": frame["confirmation"],
        "Terdeteksi": frame["detected"],
        "Harga entry": frame["entry_price"],
        f"Return +{hz} hari (%)": frame[column] * 100,
        "Catatan data": frame["flags"].map(", ".join),
    }).sort_values("Terdeteksi", ascending=False)

    st.dataframe(
        table, use_container_width=True, hide_index=True,
        column_config={
            "Z-score": st.column_config.NumberColumn(format="%.2f"),
            "SUR": st.column_config.NumberColumn(format="%.2f"),
            "Terdeteksi": st.column_config.DateColumn(format="DD MMM YYYY"),
            "Harga entry": st.column_config.NumberColumn(format="%.0f"),
            f"Return +{hz} hari (%)": st.column_config.NumberColumn(format="%+.1f%%"),
        },
    )
    st.download_button("Unduh tabel (CSV)", table.to_csv(index=False), "sinyal.csv", "text/csv")


def scan_tab() -> None:
    try:
        periods = load_scan_periods()
    except Exception as exc:
        st.error(f"Tabel scan_results belum bisa dibaca: {exc}")
        return
    if not periods:
        st.info("Belum ada hasil pemindaian. Jalankan run.py fetch terlebih dahulu.")
        return

    a, b = st.columns([1, 3])
    year, period = a.selectbox("Periode", periods, format_func=lambda p: f"{p[1]} {p[0]}")
    statuses = b.multiselect("Status", ["STRONG", "MODERATE", "WEAK", "NO SIGNAL"], default=list(SIGNAL_STATUSES))

    scan = load_scan_results(year, period)
    counts = scan["status"].value_counts()
    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Diperiksa", len(scan))
    k2.metric("STRONG", int(counts.get("STRONG", 0)))
    k3.metric("MODERATE", int(counts.get("MODERATE", 0)))
    k4.metric("Tanpa skor", int(counts.get("NO SIGNAL", 0)))

    filtered = scan[scan["status"].isin(statuses)]
    z_source = filtered["z_source"].map({"PEER": "Peer", "HIST": "Riwayat"}) if "z_source" in filtered.columns else None
    table = pd.DataFrame({
        "Ticker": filtered["ticker"],
        "Sektor": filtered["sector"],
        "Status": filtered["status"],
        "Arah": filtered["direction"],
        "Pemicu": filtered["trigger_source"],
        "Konfirmasi laba": filtered["confirmation"],
        "Pertumbuhan (%)": filtered["growth_yoy"] * 100,
        "Median sektor (%)": filtered["sector_median_growth"] * 100,
        "Z-score": filtered["z_score"],
        "Sumber Z": z_source,
        "SUR": filtered["sur"],
        "Pendapatan (miliar)": (filtered["revenue_curr"] / 1e9).round(1),
        "Pembanding (miliar)": (filtered["revenue_prev"] / 1e9).round(1),
        "Laba bersih (miliar)": (filtered["net_income_curr"] / 1e9).round(1),
        "Pembanding laba (miliar)": (filtered["net_income_prev"] / 1e9).round(1),
        "Catatan": filtered["note"],
    })
    table = table.sort_values("Z-score", key=lambda s: s.abs(), ascending=False, na_position="last")

    st.dataframe(
        table, use_container_width=True, hide_index=True,
        column_config={
            "Pertumbuhan (%)": st.column_config.NumberColumn(format="%+.1f%%"),
            "Median sektor (%)": st.column_config.NumberColumn(format="%+.1f%%"),
            "Z-score": st.column_config.NumberColumn(format="%.2f"),
            "SUR": st.column_config.NumberColumn(format="%.2f"),
            "Pendapatan (miliar)": st.column_config.NumberColumn(format="localized"),
            "Pembanding (miliar)": st.column_config.NumberColumn(format="localized"),
            "Laba bersih (miliar)": st.column_config.NumberColumn(format="localized"),
            "Pembanding laba (miliar)": st.column_config.NumberColumn(format="localized"),
        },
    )
    st.caption("Angka triwulan bersifat kumulatif tahun berjalan dan dibandingkan dengan periode yang sama tahun lalu.")
    st.download_button("Unduh tabel (CSV)", table.to_csv(index=False), f"pemindaian_{period}_{year}.csv", "text/csv")


def page_signals() -> None:
    tab_performance, tab_scan = st.tabs(["Kinerja sinyal", "Hasil pemindaian"])
    with tab_performance:
        signal_performance_tab()
    with tab_scan:
        scan_tab()


# ---------------------------------------------------------------------------
# Page: Makro
# ---------------------------------------------------------------------------

def resolve_macro_columns() -> dict | None:
    columns = table_columns("macro_indicators")
    if not columns:
        st.info("Tabel macro_indicators tidak ditemukan.")
        return None

    mapping = {role: pick(columns, candidates) for role, candidates in MACRO_ROLES.items()}
    unresolved = [role for role, column in mapping.items() if column is None]
    if unresolved:
        st.warning(
            f"Kolom macro_indicators belum dikenali ({', '.join(unresolved)}). "
            f"Kolom yang ada: {', '.join(columns)}."
        )
        for role in unresolved:
            mapping[role] = st.selectbox(f"Kolom {role}", columns, key=f"macro_{role}")
    return mapping


def validated(columns: list[str], *names: str) -> None:
    for name in names:
        if name not in columns:
            raise ValueError(f"Unknown column: {name}")


@st.cache_data(ttl=600)
def load_macro_series(key_column: str, date_column: str, value_column: str, keys: tuple) -> pd.DataFrame:
    columns = table_columns("macro_indicators")
    validated(columns, key_column, date_column, value_column)
    estimated = ', "is_estimated"' if "is_estimated" in columns else ""
    frame = query(
        f'SELECT "{key_column}" AS key, "{date_column}" AS obs_date, "{value_column}" AS value{estimated} '
        f'FROM macro_indicators WHERE "{key_column}" = ANY(%s) AND "{value_column}" IS NOT NULL '
        f'ORDER BY "{date_column}";',
        (list(keys),),
    )
    frame = to_numeric(frame, ["value"])
    frame["obs_date"] = pd.to_datetime(frame["obs_date"])
    return frame.dropna(subset=["value"])


@st.cache_data(ttl=600)
def load_macro_overview(key_column: str, date_column: str, value_column: str) -> pd.DataFrame:
    """Latest value of every indicator with its value one month and one year earlier."""
    columns = table_columns("macro_indicators")
    validated(columns, key_column, date_column, value_column)
    has_source = "data_source" in columns
    inner_source = ', "data_source"' if has_source else ""
    outer_source = ", l.data_source AS source" if has_source else ""
    frame = query(
        f"""
        WITH latest AS (
            SELECT DISTINCT ON ("{key_column}")
                "{key_column}" AS key, "{date_column}" AS last_date, "{value_column}" AS last_value{inner_source}
            FROM macro_indicators
            WHERE "{value_column}" IS NOT NULL
            ORDER BY "{key_column}", "{date_column}" DESC
        )
        SELECT l.key, l.last_date, l.last_value, m.value AS month_ago, y.value AS year_ago{outer_source}
        FROM latest l
        LEFT JOIN LATERAL (
            SELECT "{value_column}" AS value FROM macro_indicators t
            WHERE t."{key_column}" = l.key AND t."{date_column}" <= l.last_date - INTERVAL '1 month'
                AND t."{value_column}" IS NOT NULL
            ORDER BY t."{date_column}" DESC LIMIT 1
        ) m ON TRUE
        LEFT JOIN LATERAL (
            SELECT "{value_column}" AS value FROM macro_indicators t
            WHERE t."{key_column}" = l.key AND t."{date_column}" <= l.last_date - INTERVAL '1 year'
                AND t."{value_column}" IS NOT NULL
            ORDER BY t."{date_column}" DESC LIMIT 1
        ) y ON TRUE
        ORDER BY l.key;
        """
    )
    frame = to_numeric(frame, ["last_value", "month_ago", "year_ago"])
    frame["last_date"] = pd.to_datetime(frame["last_date"])
    return frame


@st.cache_data(ttl=600)
def load_macro_names() -> dict:
    columns = table_columns("macro_dictionary")
    key_column = pick(columns, MACRO_ROLES["key"])
    name_column = pick(columns, MACRO_NAME_CANDIDATES)
    if not key_column or not name_column:
        return {}
    frame = query(f'SELECT "{key_column}" AS key, "{name_column}" AS name FROM macro_dictionary;')
    return dict(zip(frame["key"], frame["name"]))


def monthly_correlation(data: pd.DataFrame, keys: list[str]) -> pd.DataFrame | None:
    """Correlation of month-to-month changes (percentage change, or difference for non-positive series)."""
    columns = {}
    for key in keys:
        series = data[data["key"] == key].set_index("obs_date")["value"].resample("MS").last().dropna()
        if len(series) < 6:
            continue
        columns[key] = series.pct_change() if (series > 0).all() else series.diff()
    if len(columns) < 2:
        return None
    return pd.DataFrame(columns).dropna().corr()


def page_macro() -> None:
    mapping = resolve_macro_columns()
    if mapping is None:
        return

    overview = load_macro_overview(mapping["key"], mapping["date"], mapping["value"])
    if overview.empty:
        st.info("Belum ada data di macro_indicators.")
        return

    names = load_macro_names()
    labels = {k: (f"{k} - {names[k]}" if k in names and pd.notna(names[k]) else k) for k in overview["key"]}

    table = pd.DataFrame({
        "Indikator": overview["key"].map(labels),
        "Terakhir": overview["last_value"],
        "Tanggal": overview["last_date"].dt.date,
        "1 bulan (%)": (overview["last_value"] / overview["month_ago"].where(overview["month_ago"] > 0) - 1) * 100,
        "12 bulan (%)": (overview["last_value"] / overview["year_ago"].where(overview["year_ago"] > 0) - 1) * 100,
    })
    if "source" in overview.columns:
        table["Sumber"] = overview["source"]

    st.markdown("**Daftar indikator**")
    st.dataframe(
        table, use_container_width=True, hide_index=True,
        column_config={
            "Terakhir": st.column_config.NumberColumn(format="%.2f"),
            "Tanggal": st.column_config.DateColumn(format="DD MMM YYYY"),
            "1 bulan (%)": st.column_config.NumberColumn(format="%+.1f%%"),
            "12 bulan (%)": st.column_config.NumberColumn(format="%+.1f%%"),
        },
    )
    st.download_button("Unduh daftar (CSV)", table.to_csv(index=False), "daftar_makro.csv", "text/csv")

    st.markdown("**Grafik**")
    keys = list(overview["key"])
    default = [k for k in ("IHSG", "USDIDR") if k in keys] or keys[:2]
    a, b, c = st.columns([4, 2, 2])
    selected = a.multiselect(
        "Indikator", keys, default=default, format_func=lambda k: labels[k],
        label_visibility="collapsed", placeholder="Pilih indikator",
    )
    range_label = b.radio("Rentang", list(MACRO_RANGES), index=2, horizontal=True, label_visibility="collapsed")
    mode = c.radio("Tampilan", ["Per indikator", "Perbandingan"], horizontal=True, label_visibility="collapsed")
    if not selected:
        st.info("Pilih minimal satu indikator.")
        return

    data = load_macro_series(mapping["key"], mapping["date"], mapping["value"], tuple(selected))
    years = MACRO_RANGES[range_label]
    if years is not None and not data.empty:
        data = data[data["obs_date"] >= data["obs_date"].max() - pd.DateOffset(years=years)]

    colors = {key: MACRO_COLORS[i % len(MACRO_COLORS)] for i, key in enumerate(selected)}

    if mode == "Per indikator":
        columns = st.columns(2)
        for i, key in enumerate(selected):
            series = data[data["key"] == key]
            with columns[i % 2]:
                st.markdown(f"**{labels[key]}**")
                if series.empty:
                    st.info("Tidak ada data pada rentang ini.")
                    continue
                fig = go.Figure(go.Scatter(
                    x=series["obs_date"], y=series["value"], mode="lines", line=dict(color=colors[key], width=1.8),
                    hovertemplate="%{x|%d %b %Y}<br>%{y:,.2f}<extra></extra>",
                ))
                show(base_layout(fig, height=260, showlegend=False))
        return

    fig = go.Figure()
    for key in selected:
        series = data[data["key"] == key]
        if series.empty or not series["value"].iloc[0]:
            continue
        rebased = series["value"] / series["value"].iloc[0] * 100
        fig.add_trace(go.Scatter(
            x=series["obs_date"], y=rebased, name=labels[key], mode="lines", line=dict(color=colors[key], width=1.8),
            hovertemplate="%{x|%d %b %Y}<br>%{y:.1f}<extra>" + key + "</extra>",
        ))
    fig.add_hline(y=100, line_width=1, line_color=MUTED)
    show(base_layout(fig, height=440, yaxis_title="Indeks (awal rentang = 100)", legend=dict(orientation="h", y=1.1)))

    correlation = monthly_correlation(data, selected)
    if correlation is not None:
        correlation.index = [labels[k] for k in correlation.index]
        correlation.columns = [labels[k] for k in correlation.columns]
        st.markdown("**Korelasi perubahan bulanan**")
        st.dataframe(correlation.round(2), use_container_width=True)


# ---------------------------------------------------------------------------
# Page: Status data
# ---------------------------------------------------------------------------

def safe_query(sql: str) -> pd.DataFrame | None:
    try:
        return query(sql)
    except Exception as exc:
        st.error(f"Query gagal: {exc}")
        return None


def page_status() -> None:
    left, right = st.columns(2)

    with left:
        st.markdown("**Harga**")
        prices = safe_query(
            "SELECT MAX(price_date) AS tanggal_terakhir, COUNT(DISTINCT ticker) AS jumlah_emiten FROM daily_prices;"
        )
        if prices is not None:
            st.dataframe(prices, hide_index=True, use_container_width=True)

        st.markdown("**Sinyal**")
        signals = safe_query(
            "SELECT COUNT(*) AS jumlah, MIN(detected_at)::date AS pertama, MAX(detected_at)::date AS terakhir FROM signal_log;"
        )
        if signals is not None:
            st.dataframe(signals, hide_index=True, use_container_width=True)

    with right:
        st.markdown("**Laporan keuangan terbaru**")
        reports = safe_query(
            "SELECT year AS tahun, period AS periode, COUNT(*) AS jumlah_emiten "
            "FROM financial_reports GROUP BY year, period ORDER BY year DESC, period DESC LIMIT 8;"
        )
        if reports is not None:
            st.dataframe(reports, hide_index=True, use_container_width=True)

        st.markdown("**Hasil pemindaian**")
        scans = safe_query(
            "SELECT year AS tahun, period AS periode, COUNT(*) AS jumlah, MAX(computed_at)::timestamp(0) AS dihitung "
            "FROM scan_results GROUP BY year, period ORDER BY year DESC, period DESC LIMIT 8;"
        )
        if scans is not None:
            st.dataframe(scans, hide_index=True, use_container_width=True)

    mapping_columns = table_columns("macro_indicators")
    key_column = pick(mapping_columns, MACRO_ROLES["key"])
    date_column = pick(mapping_columns, MACRO_ROLES["date"])
    if key_column and date_column:
        st.markdown("**Makro**")
        macro = safe_query(
            f'SELECT "{key_column}" AS indikator, MAX("{date_column}") AS tanggal_terakhir '
            f'FROM macro_indicators GROUP BY 1 ORDER BY 2 ASC;'
        )
        if macro is not None:
            st.dataframe(macro, hide_index=True, use_container_width=True)


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------

st.sidebar.title("Mintigo")
page = st.sidebar.radio("Halaman", PAGES, label_visibility="collapsed")
if st.sidebar.button("Muat ulang data", use_container_width=True):
    st.cache_data.clear()
    st.rerun()

st.title(page)

if page == "Emiten":
    page_ticker()
elif page == "Sinyal":
    page_signals()
elif page == "Makro":
    page_macro()
else:
    page_status()