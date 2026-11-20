import psycopg2
import psycopg2.extras
import yfinance as yf
import pandas as pd
import numpy as np
import os
import time
import requests
import io
import cloudscraper
from io import BytesIO
from datetime import datetime, timedelta

from core.config import DB_CONFIG as config
from core.lse import LseClient, LseError, keys_configured

_scraper = cloudscraper.create_scraper(browser={'browser': 'chrome', 'platform': 'windows', 'mobile': False})


def _ts():
    return datetime.now().strftime("%H:%M:%S")


def log(msg):
    print(f"[{_ts()}] {msg}")


# ==========================================
# 2. MASTER API MAPPING (YFINANCE / FRED)
# ==========================================
# ==========================================
# 2. MASTER API MAPPING (YFINANCE / FRED)
# ==========================================
API_MAP = {
    # --- MARKET DATA (YAHOO FINANCE) ---
    'USDIDR': 'IDR=X',
    'DXY': 'DX-Y.NYB',
    'VIX': '^VIX',
    'GOLD': 'GC=F',
    'WTI': 'CL=F',
    'BRENT_OIL': 'BZ=F',
    'IHSG': '^JKSE',
    'COAL': 'MTF=F',
    'COPPER': 'HG=F',
    'SILVER': 'SI=F',

    # --- GLOBAL MACRO (FRED) ---
    'US_10Y': 'DGS10',
    'US_CPI': 'CPIAUCSL',
    'ID_CPI': 'IDNCPIALLMINMEI',
    'ID_RATE': 'IR3TIB01IDM156N',
    'ID_M3': 'MABMM301IDM189S',
    'ID_GDP': 'IDNGDPRQPSMEI',
    'ID_RESERVES': 'TRESEGIDM052N',

    # --- COMMODITY PROXIES (FRED) ---
    'NICKEL': 'PNICKUSDM',
    'BAUXITE': 'IR14200',
    'TIN': 'PTINUSDM',           # Ticker Timah (USD per Metric Ton)
    'COAL_GLOBAL': 'PCOALAUUSDM'  # Cadangan harga batu bara Australia (Newcastle)
}

SCALE_MAP = {
    'NICKEL': 1.0,
    'BAUXITE': 0.14,
    'TIN': 1.0,                   # Skala normal untuk Timah USD/MT
    'COAL_GLOBAL': 1.0            # Skala normal untuk batu bara global
}

# ==========================================
# 2B. MASTER MAPPING — TABEL SEKI BANK INDONESIA
# ==========================================
BI_SEKI_MAP = {
    'ID_CURRENT_ACCOUNT': {
        'url': 'https://www.bi.go.id/SEKI/tabel/TABEL5_1.xls',
        'sheet': '5.1',
        'row_label': 'I. Transaksi Berjalan',
        'after_label': None,
    },
    'ID_TRADE_BALANCE': {
        'url': 'https://www.bi.go.id/SEKI/tabel/TABEL5_2.xls',
        'sheet': '5.2',
        'row_label': 'A. Barang',
        'after_label': None,
    },
    'ID_FOREIGN_DEBT': {
        'url': 'https://www.bi.go.id/SEKI/tabel/TABEL5_39.xls',
        'sheet': '5.39',
        'row_label': 'Surat Utang',
        'after_label': 'Kewajiban',
    },
}

LSE_OVERLAP_DAYS = 120

LSE_MACRO_MAP = {
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

LSE_MARKET_MAP = {
    'USDIDR': {'market': 'forex', 'symbol': 'USD_IDR', 'timeframe': '1d'},
}

MONTH_MAP = {
    'JAN': 1, 'FEB': 2, 'MAR': 3, 'APR': 4, 'MAY': 5, 'JUN': 6,
    'JUL': 7, 'AUG': 8, 'SEP': 9, 'OCT': 10, 'NOV': 11, 'DEC': 12,
}
QUARTER_MAP = {'Q1': 3, 'Q2': 6, 'Q3': 9, 'Q4': 12}

# ==========================================
# 2C. KONFIGURASI BPS (CPI GAP-FILL)
# ==========================================
BPS_API_KEY = os.getenv("BPS_API_KEY")
BPS_TABLE_ID = 915
BPS_DOMAIN = "0000"
BPS_LANG = "ind"

BULAN_MAP = {
    'Januari': 1, 'Februari': 2, 'Maret': 3, 'April': 4, 'Mei': 5, 'Juni': 6,
    'Juli': 7, 'Agustus': 8, 'September': 9, 'Oktober': 10, 'November': 11, 'Desember': 12
}

BPS_MAX_MOM_CHANGE_PCT = 5.0
BPS_MAX_YOY_DEVIATION_PP = 3.0


def _last_day_of_month(year, month):
    return pd.Timestamp(year=year, month=month, day=1) + pd.offsets.MonthEnd(0)


def _clean(x):
    if pd.isna(x):
        return ''
    return str(x).replace('*', '').strip().upper()


# ==========================================
# 3A. HELPER: FRED DATA EXTRACTOR
# ==========================================
def fetch_fred_csv(series_id, start_date, end_date):
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
    try:
        response = requests.get(url, timeout=15)
        if response.status_code != 200:
            log(f"  {series_id:<24} | FRED_HTTP_{response.status_code}")
            return None
        df = pd.read_csv(io.BytesIO(response.content), na_values='.')
        df.columns = ['Date', 'Value']
        df['Date'] = pd.to_datetime(df['Date'])
        mask = (df['Date'] >= start_date) & (df['Date'] <= end_date)
        return df.loc[mask]
    except Exception as e:
        log(f"  {series_id:<24} | FRED_FETCH_ERROR | {type(e).__name__}: {e}")
        return None


# ==========================================
# 3B. HELPER: PARSER TABEL SEKI (.xls -> time series)
# ==========================================
def fetch_bi_seki(entry):
    try:
        resp = _scraper.get(entry['url'], timeout=20)
        if resp.status_code != 200:
            log(f"  SEKI_DOWNLOAD_FAIL | HTTP {resp.status_code}")
            return None

        raw = pd.read_excel(io.BytesIO(resp.content), sheet_name=entry['sheet'], header=None)

        period_row_idx, label_row_idx, period_map, period_type = None, None, None, None

        for i in range(len(raw)):
            row_vals = raw.iloc[i].apply(_clean)
            if row_vals.isin(QUARTER_MAP.keys()).any():
                period_row_idx = i
                label_row_idx = i - 1
                period_map = QUARTER_MAP
                period_type = 'QUARTER'
                break

        if period_row_idx is None:
            for i in range(len(raw)):
                row_vals = raw.iloc[i].apply(_clean)
                if row_vals.isin(MONTH_MAP.keys()).any():
                    period_row_idx = i
                    label_row_idx = i - 1
                    period_map = MONTH_MAP
                    period_type = 'MONTH'
                    break

        if period_row_idx is None:
            log(f"  SEKI_PARSE_FAIL | period header not found in sheet '{entry['sheet']}'")
            return None

        log(f"  SEKI_FORMAT | period_type={period_type}")

        label_row = raw.iloc[label_row_idx]
        period_row = raw.iloc[period_row_idx]

        col_year_map = {}
        current_block = []

        def flush_block(block):
            if not block:
                return
            year_val = None
            for c in block:
                if pd.notna(label_row.iloc[c]):
                    year_val = label_row.iloc[c]
                    break
            for c in block:
                col_year_map[c] = year_val

        for col in range(len(period_row)):
            p_str = _clean(period_row.iloc[col])
            if p_str in period_map:
                current_block.append(col)
            else:
                flush_block(current_block)
                current_block = []
        flush_block(current_block)

        search_start = 0
        if entry.get('after_label'):
            for col_check in range(min(5, raw.shape[1])):
                label_col = raw.iloc[:, col_check].apply(lambda x: str(x) if pd.notna(x) else '')
                anchor = label_col[label_col == entry['after_label']].index
                if len(anchor) > 0:
                    search_start = anchor[0]
                    break

        target_row_idx = None
        for col_check in range(min(5, raw.shape[1])):
            label_col = raw.iloc[search_start:, col_check].apply(lambda x: str(x) if pd.notna(x) else '')
            matches = label_col[label_col.str.contains(entry['row_label'], na=False, regex=False)].index
            if len(matches) > 0:
                target_row_idx = matches[0]
                break

        if target_row_idx is None:
            log(f"  SEKI_PARSE_FAIL | row '{entry['row_label']}' not found")
            return None

        values = raw.iloc[target_row_idx]

        records = []
        for col, year_val in col_year_map.items():
            p_str = _clean(period_row.iloc[col])
            v = values.iloc[col]
            if pd.isna(year_val) or pd.isna(v) or p_str not in period_map:
                continue
            month = period_map[p_str]
            try:
                date = _last_day_of_month(int(year_val), month)
                records.append({'Date': date, 'Value': float(v)})
            except (ValueError, TypeError):
                continue

        df = pd.DataFrame(records).drop_duplicates(subset='Date').sort_values('Date').reset_index(drop=True)
        return df

    except Exception as e:
        log(f"  SEKI_PARSE_ERROR | {type(e).__name__}: {e}")
        return None


# ==========================================
# 3C. HELPER: BPS STATICTABLE (CPI GAP-FILL)
# ==========================================
def get_statictable_metadata(table_id, domain=BPS_DOMAIN, lang=BPS_LANG, key=BPS_API_KEY):
    url = (
        f"https://webapi.bps.go.id/v1/api/view/"
        f"domain/{domain}/model/statictable/lang/{lang}/id/{table_id}/key/{key}"
    )
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("status") != "OK":
        raise ValueError(f"BPS API error untuk id={table_id}: {payload}")
    return payload["data"]


def download_statictable_excel(metadata, retries=3):
    excel_url = metadata.get("excel")
    if not excel_url:
        raise ValueError("Metadata tidak punya link excel — cek response mentahnya.")

    headers = {"User-Agent": "Mozilla/5.0"}
    last_err = None
    content = None

    for attempt in range(1, retries + 1):
        try:
            r = requests.get(excel_url, timeout=60, headers=headers)
            r.raise_for_status()
            content = r.content
            break
        except Exception as e:
            last_err = e
            log(f"BPS_DOWNLOAD          | attempt {attempt}/{retries} gagal | {type(e).__name__}: {str(e)[:150]}")
            time.sleep(2 * attempt)

    if content is None:
        raise last_err

    return pd.read_excel(BytesIO(content), header=None)


def parse_bps_yoy_table(raw, start_after):
    header_row = raw.iloc[2]
    year_cols = {col: int(float(header_row[col])) for col in raw.columns[1:] if pd.notna(header_row[col])}

    records = []
    for i in range(3, 15):
        bulan_nama = raw.iloc[i, 0]
        if bulan_nama not in BULAN_MAP:
            continue
        bulan_num = BULAN_MAP[bulan_nama]
        for col, year in year_cols.items():
            val = raw.iloc[i][col]
            if pd.isna(val):
                continue
            records.append({
                'record_date': pd.Timestamp(year=year, month=bulan_num, day=1),
                'value': float(val)
            })

    df = pd.DataFrame(records).sort_values('record_date').reset_index(drop=True)
    cutoff = pd.Timestamp(start_after)
    return df[df['record_date'] > cutoff].reset_index(drop=True)


def ensure_ticker_registered(ticker, cur, name, category=None, unit=None, frequency=None, data_source=None):
    cur.execute("SELECT 1 FROM macro_dictionary WHERE ticker = %s", (ticker,))
    if cur.fetchone() is None:
        cur.execute(
            """
            INSERT INTO macro_dictionary (ticker, name, category, unit, frequency, data_source)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (ticker, name, category, unit, frequency, data_source)
        )
        log(f"REGISTER              | ticker '{ticker}' didaftarkan ke macro_dictionary")


def get_series(ticker, conn):
    query = """
        SELECT record_date, value
        FROM macro_indicators
        WHERE indicator_ticker = %s
        ORDER BY record_date
    """
    df = pd.read_sql(query, conn, params=(ticker,))
    df['record_date'] = pd.to_datetime(df['record_date'])
    return df.set_index('record_date')['value']


def reconstruct_series(hist, yoy_df):
    hist = hist.copy()
    hist.index = hist.index.to_period('M').to_timestamp()

    yoy = yoy_df.set_index('record_date')['value'].sort_index()
    yoy.index = yoy.index.to_period('M').to_timestamp()

    combined_lookup = hist.to_dict()
    reconstructed = {}

    for dt in sorted(yoy.index):
        base_date = dt - pd.DateOffset(months=12)
        base_value = combined_lookup.get(base_date)
        if base_value is None:
            log(f"BPS_CPI_GAPFILL       | skip {dt.date()} | base {base_date.date()} tidak ditemukan")
            continue
        new_value = base_value * (1 + yoy.loc[dt] / 100)
        reconstructed[dt] = new_value
        combined_lookup[dt] = new_value

    recon_df = pd.Series(reconstructed).sort_index().reset_index()
    recon_df.columns = ['record_date', 'value']
    return recon_df


def sanity_check_cpi(hist, recon_df, yoy_df):
    critical, warnings = [], []

    if recon_df.empty:
        critical.append("hasil rekonstruksi kosong")
        return critical, warnings

    combined = pd.concat([hist, recon_df.set_index('record_date')['value']]).sort_index()
    combined = combined[~combined.index.duplicated(keep='last')]

    if (recon_df['value'] <= 0).any():
        critical.append("ada nilai index hasil rekonstruksi <= 0")

    mom_pct = combined.pct_change() * 100
    mom_pct_recon = mom_pct.loc[recon_df['record_date']]
    extreme_mom = mom_pct_recon[mom_pct_recon.abs() > BPS_MAX_MOM_CHANGE_PCT]
    for dt, pct in extreme_mom.items():
        warnings.append(f"lompatan MoM {pct:+.2f}% di {dt.date()}")

    yoy_lookup = yoy_df.set_index('record_date')['value']
    for dt in recon_df['record_date']:
        base_date = dt - pd.DateOffset(months=12)
        if base_date not in combined.index or dt not in combined.index:
            continue
        implied_yoy = (combined[dt] / combined[base_date] - 1) * 100
        original_yoy = yoy_lookup.get(dt)
        if original_yoy is None:
            continue
        diff = abs(implied_yoy - original_yoy)
        if diff > BPS_MAX_YOY_DEVIATION_PP:
            warnings.append(f"{dt.date()}: YoY implied beda {diff:.2f}pp dari YoY BPS asli")

    return critical, warnings


def run_bps_cpi_gapfill(conn, cur):
    """Dipanggil di akhir update_macro_data() — isi gap ID_CPI pakai chain YoY BPS,
    hanya untuk periode setelah data FRED terakhir yang valid."""
    log("BPS_CPI_GAPFILL       | start")

    if not BPS_API_KEY:
        log("BPS_CPI_GAPFILL       | SKIP | BPS_API_KEY tidak diset di .env")
        return

    try:
        hist = get_series('ID_CPI', conn)
        if hist.empty:
            log("BPS_CPI_GAPFILL       | SKIP | ID_CPI belum ada histori sama sekali")
            return
        last_valid_date = hist.index.max().strftime('%Y-%m-%d')

        meta = get_statictable_metadata(BPS_TABLE_ID)
        raw = download_statictable_excel(meta)

        # parse_bps_yoy_table sudah otomatis cuma ambil baris SETELAH last_valid_date —
        # jadi kalau BPS belum rilis data baru, yoy_df bakal kosong dan berhenti di sini
        # (efisiensi: gak ada perhitungan/upsert yang jalan sia-sia)
        yoy_df = parse_bps_yoy_table(raw, start_after=last_valid_date)
        if yoy_df.empty:
            log(f"BPS_CPI_GAPFILL       | UP_TO_DATE | tidak ada gap baru (anchor={last_valid_date})")
            return

        recon_df = reconstruct_series(hist, yoy_df)
        critical, warnings = sanity_check_cpi(hist, recon_df, yoy_df)

        for w in warnings:
            log(f"BPS_CPI_GAPFILL       | WARN | {w}")

        if critical:
            for c in critical:
                log(f"BPS_CPI_GAPFILL       | FAIL | {c}")
            log("BPS_CPI_GAPFILL       | ABORTED | tidak ada perubahan disimpan ke ID_CPI")
            return

        ensure_ticker_registered(
            'ID_CPI_YOY_BPS', cur,
            name='Indonesia CPI Inflation Rate YoY (BPS)',
            category='inflation', unit='percent', frequency='monthly', data_source='BPS'
        )
        conn.commit()

        _upsert_dataframe(cur, 'ID_CPI_YOY_BPS', yoy_df.rename(columns={'record_date': 'Date', 'value': 'Value'}),
                           data_source='BPS', is_estimated=False)
        _upsert_dataframe(cur, 'ID_CPI', recon_df.rename(columns={'record_date': 'Date', 'value': 'Value'}),
                           data_source='BPS_YOY_CHAIN', is_estimated=True)
        conn.commit()

        log(f"BPS_CPI_GAPFILL       | OK | rows={len(recon_df)} range={recon_df['record_date'].min().date()}..{recon_df['record_date'].max().date()}")

    except Exception as e:
        log(f"BPS_CPI_GAPFILL       | FAIL | {type(e).__name__}: {e}")
        conn.rollback()


_LSE_RESOLVED = {}


def lse_resolve(client, ticker_db):
    if ticker_db in _LSE_RESOLVED:
        return _LSE_RESOLVED[ticker_db]
    spec = LSE_MACRO_MAP.get(ticker_db)
    found = None
    if spec:
        found = client.resolve_macro(
            spec['kind'], spec['country'], spec.get('categories', ()), spec.get('codes', ())
        )
    _LSE_RESOLVED[ticker_db] = found
    return found


def register_lse_tickers(conn, cur, client):
    for ticker_db, spec in LSE_MACRO_MAP.items():
        found = lse_resolve(client, ticker_db)
        if found is None:
            log(f"LSE_REGISTER          | {ticker_db:<22} | TIDAK DITEMUKAN | jalankan: python -m pipeline.lse_tools resolve")
            continue
        ensure_ticker_registered(
            ticker_db, cur,
            name=found.get('name') or ticker_db,
            category=spec.get('category'),
            unit=str(found.get('unit') or '').upper() or None,
            frequency=str(found.get('frequency') or '').upper() or None,
            data_source='LSE',
        )
    conn.commit()


def fetch_lse(client, ticker_db, since, until):
    market_spec = LSE_MARKET_MAP.get(ticker_db)
    if market_spec:
        frame = client.export_csv(
            market=market_spec['market'],
            symbol=market_spec['symbol'],
            timeframe=market_spec.get('timeframe', '1d'),
            **{'from': pd.Timestamp(since).strftime('%Y-%m-%d'), 'to': pd.Timestamp(until).strftime('%Y-%m-%d')},
        )
        if frame.empty or 'close' not in frame.columns or 'time' not in frame.columns:
            return None
        out = pd.DataFrame({
            'Date': pd.to_datetime(frame['time'], utc=True).dt.tz_localize(None).dt.normalize(),
            'Value': pd.to_numeric(frame['close'], errors='coerce'),
        })
        return out.dropna().drop_duplicates('Date', keep='last').sort_values('Date').reset_index(drop=True)

    found = lse_resolve(client, ticker_db)
    if found is None:
        log(f"  {ticker_db:<24} | LSE_CODE_NOT_FOUND")
        return None
    spec = LSE_MACRO_MAP[ticker_db]
    return client.macro_series(spec['kind'], found['code'], start=since, end=until)

# ==========================================
# 4. HELPER: CEK DATA TERAKHIR & UPSERT KE DB
# ==========================================
def _get_last_date(cur, ticker_db):
    """Ambil tanggal terakhir yang sudah ada di DB untuk ticker ini.
    None kalau ticker belum punya histori sama sekali."""
    cur.execute("""
        SELECT MAX(record_date) FROM macro_indicators WHERE indicator_ticker = %s;
    """, (ticker_db,))
    result = cur.fetchone()[0]
    return pd.Timestamp(result) if result else None


def _upsert_dataframe(cur, ticker_db, df, data_source=None, is_estimated=False):
    inserted_count = 0
    for _, row in df.iterrows():
        cur.execute("""
            INSERT INTO macro_indicators (indicator_ticker, record_date, value, data_source, is_estimated)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (indicator_ticker, record_date)
            DO UPDATE SET value = EXCLUDED.value,
                          data_source = EXCLUDED.data_source,
                          is_estimated = EXCLUDED.is_estimated;
        """, (ticker_db, row['Date'].date(), float(row['Value']), data_source, is_estimated))
        inserted_count += 1
    return inserted_count


# ==========================================
# 5. CORE ENGINE: UPDATE PROCESS
# ==========================================
def update_macro_data(full_backfill: bool = False, backfill_years: int = 15):
    """
    full_backfill=False (default): perilaku normal, incremental update
    ke depan dari data terakhir yang ada di DB — dipakai untuk run rutin
    harian/mingguan.

    full_backfill=True: SEKALI jalan untuk mengisi histori lama yang
    belum ada di DB (misal ID_CPI/US_CPI/USDIDR yang kepotong mulai
    2016 gara-gara rolling 10-tahun window). Mode ini MELEWATI filter
    "cuma baris lebih baru dari last_date" — supaya data yang lebih tua
    dari yang sudah ada tetap bisa masuk. ON CONFLICT DO UPDATE di
    _upsert_dataframe() memastikan ini aman, tidak menimpa data yang
    sudah benar di tanggal yang sama.
    """
    mode_label = "FULL_BACKFILL" if full_backfill else "INCREMENTAL"
    log(f"Macro sync v6.0 | mode={mode_label} | sources=YFINANCE,FRED,BI_SEKI, LSE")

    try:
        conn = psycopg2.connect(**config)
        cur = conn.cursor()

        cur.execute("SELECT ticker, data_source FROM macro_dictionary;")
        dictionary = cur.fetchall()

        if not dictionary:
            log("ABORT | macro_dictionary empty")
            return

        end_date = datetime.now()
        window_years = backfill_years if full_backfill else 10
        start_date = end_date - timedelta(days=365 * window_years)

        log(f"UNIVERSE | {len(dictionary)} indicators | window={window_years}y")

        n_ok, n_empty, n_skip, n_fail, n_uptodate = 0, 0, 0, 0, 0

        for ticker_db, source in dictionary:
            try:
                df = pd.DataFrame()

                # --- EKSTRAKSI --- (tidak berubah)
                if source == 'YFINANCE':
                    api_ticker = API_MAP.get(ticker_db)
                    if not api_ticker:
                        log(f"{ticker_db:<24} | SKIP | not registered in API_MAP")
                        n_skip += 1
                        continue
                    df = yf.download(api_ticker, start=start_date, end=end_date, interval="1d", progress=False)
                    if not df.empty:
                        if isinstance(df.columns, pd.MultiIndex):
                            df.columns = df.columns.get_level_values(0)
                        df = df.reset_index()
                        df['Value'] = df['Adj Close'] if 'Adj Close' in df.columns else df['Close']

                elif source == 'FRED':
                    api_ticker = API_MAP.get(ticker_db)
                    if not api_ticker:
                        log(f"{ticker_db:<24} | SKIP | not registered in API_MAP")
                        n_skip += 1
                        continue
                    df = fetch_fred_csv(api_ticker, start_date, end_date)

                elif source == 'BI_SEKI':
                    entry = BI_SEKI_MAP.get(ticker_db)
                    if not entry:
                        log(f"{ticker_db:<24} | SKIP | not registered in BI_SEKI_MAP")
                        n_skip += 1
                        continue
                    df = fetch_bi_seki(entry)
                
                elif source == 'LSE':
                    if lse is None:
                        log(f"{ticker_db:<24} | SKIP | LSE_API_KEYS belum diisi")
                        n_skip += 1
                        continue
                    last_known = _get_last_date(cur, ticker_db)
                    since = start_date
                    if not full_backfill and last_known is not None:
                        since = max(pd.Timestamp(start_date), last_known - timedelta(days=LSE_OVERLAP_DAYS))
                    df = fetch_lse(lse, ticker_db, since, end_date)

                else:
                    log(f"{ticker_db:<24} | SKIP | unrecognized source '{source}'")
                    n_skip += 1
                    continue

                # --- VALIDASI ---
                if df is None or df.empty:
                    log(f"{ticker_db:<24} | NO_DATA | source={source}")
                    n_empty += 1
                    continue

                df = df.dropna(subset=['Value'])

                # --- CEK DATA SUDAH TERSEDIA (EFISIENSI) ---
                # HANYA diterapkan di mode incremental. Di mode full_backfill,
                # filter ini DILEWATI supaya data lama (lebih tua dari yang
                # sudah ada di DB) tetap bisa masuk.
                last_date = _get_last_date(cur, ticker_db)
                if source != 'LSE' and not full_backfill and last_date is not None:
                    df = df[pd.to_datetime(df['Date']) > last_date]

                if df.empty:
                    log(f"{ticker_db:<24} | UP_TO_DATE | source={source:<9} last={last_date.date() if last_date is not None else '-'}")
                    n_uptodate += 1
                    continue

                # --- SCALING ---
                if ticker_db in SCALE_MAP:
                    df['Value'] = df['Value'] * SCALE_MAP[ticker_db]
                    log(f"{ticker_db:<24} | SCALE_APPLIED | factor={SCALE_MAP[ticker_db]}")

                # --- DATABASE UPSERT ---
                inserted_count = _upsert_dataframe(cur, ticker_db, df, data_source=source, is_estimated=False)
                conn.commit()
                log(f"{ticker_db:<24} | OK | source={source:<9} rows={inserted_count}")
                n_ok += 1

            except Exception as e:
                log(f"{ticker_db:<24} | FAIL | {type(e).__name__}: {e}")
                n_fail += 1
                conn.rollback()

        log(f"SUMMARY | ok={n_ok} up_to_date={n_uptodate} empty={n_empty} skip={n_skip} fail={n_fail} total={len(dictionary)}")

        if not full_backfill:
            run_bps_cpi_gapfill(conn, cur)

    except Exception as e:
        log(f"FATAL | DB_ERROR | {type(e).__name__}: {e}")
    finally:
        if 'conn' in locals():
            cur.close()
            conn.close()
            log("CONN_CLOSED")


if __name__ == "__main__":
    import sys
    if "--backfill" in sys.argv:
        update_macro_data(full_backfill=True, backfill_years=15)
    else:
        update_macro_data()