import argparse
import json
import sys


import pandas as pd
import psycopg2
import numpy as np

from core import universe_store
from psycopg2 import errors

from analytics.portfolio import evaluate
from core import portfolio_store as store
from core.portfolio_data import load_macro_series, load_price_inputs

VERDICTS = {
    "positif": "di atas suku bunga",
    "negatif": "di bawah suku bunga",
    "tidak_pasti": "belum dapat dibedakan dari nol",
    "tidak_dapat_dihitung": "tidak dapat dihitung",
}


def pct(value, digits=1):
    return "n/a" if value is None else f"{value * 100:.{digits}f}%"


def num(value, digits=2):
    return "n/a" if value is None else f"{value:.{digits}f}"


def print_sharpe(label, block):
    if block["sharpe"] is None:
        print(f"{label:<16} tidak dapat dihitung (n={block['n_obs']})")
        return
    print(
        f"{label:<16} Sharpe {block['sharpe']:>6.2f}  "
        f"selang 95% [{block['ci_low']:.2f}, {block['ci_high']:.2f}]  "
        f"excess {pct(block['annual_excess_return'])}/thn  vol {pct(block['annual_vol'])}/thn  "
        f"n={block['n_obs']} {block['basis']} ({block['start']} s.d. {block['end']})  "
        f"-> {VERDICTS[block['verdict']]}"
    )


def print_report(name, result):
    window = result["window"]
    print(
        f"\nPortofolio: {name} | data per {result['as_of']} | "
        f"jendela {window['weeks']} minggu ({window['start']} s.d. {window['end']})"
    )
    table = pd.DataFrame(
        {"bobot": result["weights"], "kontribusi_risiko": result["risk_contribution"]}
    ).astype(float)
    print("\nBobot dan kontribusi risiko (%)")
    print((table * 100).round(1).to_string())

    tickers = result["correlation"]["tickers"]
    corr = pd.DataFrame(result["correlation"]["matrix"], index=tickers, columns=tickers)
    print("\nKorelasi antar saham (return mingguan)")
    print(corr.round(2).to_string())
    print(f"Rata-rata korelasi berpasangan: {num(result['correlation']['average'])}")

    print("\nSharpe ratio (return total: harga disesuaikan dividen dan split)")
    print_sharpe("Sejak beli", result["sharpe"]["since_buy"])
    print_sharpe("Bobot sekarang", result["sharpe"]["current_weights"])
    print_sharpe("Sama rata", result["sharpe"]["equal_weight"])

    print()
    for message in result["warnings"]:
        print("PERINGATAN:", message)


def load_inputs(portfolio):
    holdings = store.list_holdings(portfolio)
    if holdings.empty:
        sys.exit(f"portofolio '{portfolio}' kosong, isi dulu dengan perintah add atau import")
    tickers = sorted(holdings["ticker"].unique())
    try:
        prices, no_trade = load_price_inputs(tickers, holdings["buy_date"].min())
    except (ValueError, RuntimeError) as exc:
        sys.exit(f"gagal membaca harga: {exc}")
    return holdings, prices, no_trade


def cmd_init(args):
    store.ensure_table()
    print("tabel portfolio_holdings dan portfolio_evaluations siap")


def cmd_add(args):
    store.add_holdings(args.portfolio, [(args.ticker, args.lots, args.date)])
    print("ditambahkan")


def cmd_import(args):
    frame = pd.read_csv(args.file, dtype=str)
    missing = {"ticker", "lots", "buy_date"} - set(frame.columns)
    if missing:
        sys.exit("kolom CSV belum ada: " + ", ".join(sorted(missing)))
    rows = frame[["ticker", "lots", "buy_date"]].itertuples(index=False, name=None)
    print(f"{store.add_holdings(args.portfolio, rows)} baris ditambahkan")


def cmd_list(args):
    df = store.list_holdings(args.portfolio)
    if df.empty:
        print("belum ada holdings")
        return
    df["buy_date"] = df["buy_date"].dt.strftime("%Y-%m-%d")
    print(df.to_string(index=False))


def cmd_remove(args):
    print("dihapus" if store.remove_holding(args.id) else "id tidak ditemukan")


def cmd_check(args):
    holdings, prices, no_trade = load_inputs(args.portfolio)
    daily = prices.sort_index().pct_change(fill_method=None)
    rows = []
    for ticker in sorted(holdings["ticker"].unique()):
        if ticker not in prices.columns or prices[ticker].dropna().empty:
            rows.append({"ticker": ticker, "catatan": "tidak ada data harga"})
            continue
        series = prices[ticker].dropna()
        moves = daily[ticker].dropna()
        worst = moves.abs().idxmax() if not moves.empty else None
        rows.append(
            {
                "ticker": ticker,
                "data_pertama": series.index[0].date(),
                "data_terakhir": series.index[-1].date(),
                "hari": len(series),
                "tanpa_transaksi": pct(no_trade.get(ticker), 0),
                "gerak_terbesar": pct(moves[worst]) if worst is not None else "n/a",
                "tanggalnya": worst.date() if worst is not None else None,
            }
        )
    print(pd.DataFrame(rows).fillna("").to_string(index=False))


def cmd_evaluate(args):
    holdings, prices, no_trade = load_inputs(args.portfolio)
    result = evaluate(prices, holdings, load_macro_series("ID_RATE"), no_trade)
    print_report(args.portfolio, result)
    if args.save:
        store.save_evaluation(args.portfolio, result)
        print("hasil disimpan ke portfolio_evaluations")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"hasil disimpan ke {args.json}")


def cmd_evaluate_all(args):
    holdings_all = store.list_holdings()
    if holdings_all.empty:
        print("belum ada portofolio")
        return
    tickers = sorted(holdings_all["ticker"].unique())
    try:
        prices, no_trade = load_price_inputs(tickers, holdings_all["buy_date"].min())
        rf = load_macro_series("ID_RATE")
    except (ValueError, RuntimeError) as exc:
        sys.exit(f"gagal membaca data: {exc}")
    failed = 0
    for name, holdings in holdings_all.groupby("portfolio"):
        try:
            result = evaluate(prices, holdings, rf, no_trade)
            store.save_evaluation(name, result)
            print(
                f"{name}: per {result['as_of']} | "
                f"Sharpe {num(result['sharpe']['current_weights']['sharpe'])} | "
                f"korelasi {num(result['correlation']['average'])} | "
                f"peringatan {len(result['warnings'])}"
            )
        except ValueError as exc:
            failed += 1
            print(f"{name}: GAGAL - {exc}")
    if failed:
        sys.exit(1)


def cmd_history(args):
    df = store.list_evaluations(args.portfolio, args.limit)
    if df.empty:
        print("belum ada riwayat evaluasi")
        return
    print(df.round(2).to_string(index=False))

def cmd_quick(args):
    holdings = store.list_holdings(args.portfolio)
    if holdings.empty:
        sys.exit(f"portofolio '{args.portfolio}' kosong")
    tickers = sorted(holdings["ticker"].unique())
    as_of, stats = universe_store.read_stats(tickers)
    if as_of is None:
        sys.exit("statistik universe belum ada, jalankan: python -m pipeline.build_universe")
    problems = [
        f"{t}: " + ("tidak ada di universe" if t not in stats.index else stats.loc[t, "note"])
        for t in tickers
        if t not in stats.index or not stats.loc[t, "eligible"]
    ]
    if problems:
        sys.exit("tidak bisa dihitung cepat:\n  " + "\n  ".join(problems))

    corr = universe_store.read_correlation(tickers)
    shares = holdings.groupby("ticker")["lots"].sum().reindex(tickers) * 100
    value = shares * stats.loc[tickers, "last_price"]
    weights = (value / value.sum()).to_numpy()
    sigma = stats.loc[tickers, "std_weekly"].to_numpy()
    mu = stats.loc[tickers, "mean_excess_weekly"].to_numpy()
    cov = corr.to_numpy() * np.outer(sigma, sigma)
    variance = float(weights @ cov @ weights)
    if not np.isfinite(variance) or variance <= 0:
        sys.exit("korelasi antar saham belum lengkap di universe, gunakan perintah evaluate")

    k = np.sqrt(52)
    mean_week = float(weights @ mu)
    per_week = mean_week / np.sqrt(variance)
    n_obs = int(stats.loc[tickers, "n_weeks"].min())
    se = np.sqrt((1 + 0.5 * per_week**2) / n_obs)
    risk = weights * (cov @ weights) / variance
    count = len(tickers)
    avg_corr = (corr.to_numpy().sum() - count) / (count * (count - 1)) if count > 1 else None

    table = pd.DataFrame({"bobot": weights, "kontribusi_risiko": risk}, index=tickers) * 100
    print(f"\nEstimasi cepat dari statistik universe per {as_of} (tanpa membaca harga)")
    print(table.round(1).to_string())
    print(f"Rata-rata korelasi berpasangan: {num(avg_corr)}")
    print(
        f"Sharpe bobot sekarang: {per_week * k:.2f}  "
        f"selang 95% [{(per_week - 1.96 * se) * k:.2f}, {(per_week + 1.96 * se) * k:.2f}]  "
        f"excess {pct(mean_week * 52)}/thn  vol {pct(np.sqrt(variance) * k)}/thn  n={n_obs} mingguan"
    )
    print("Tidak mencakup Sharpe sejak beli; untuk itu gunakan evaluate.")


def main():
    parser = argparse.ArgumentParser(
        description="Evaluasi portofolio saham IDX: korelasi dan Sharpe ratio."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--portfolio", default=store.DEFAULT_PORTFOLIO)

    sub.add_parser("init", help="buat tabel").set_defaults(func=cmd_init)

    p = sub.add_parser("add", parents=[common], help="tambah satu pembelian")
    p.add_argument("ticker")
    p.add_argument("lots", type=float)
    p.add_argument("date", help="YYYY-MM-DD")
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("import", parents=[common], help="impor CSV: ticker,lots,buy_date")
    p.add_argument("file")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("list", help="lihat holdings")
    p.add_argument("--portfolio", default=None)
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("remove", help="hapus holding berdasarkan id")
    p.add_argument("id", type=int)
    p.set_defaults(func=cmd_remove)

    p = sub.add_parser("check", parents=[common], help="cek kualitas data harga portofolio")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("evaluate", parents=[common], help="hitung korelasi dan Sharpe")
    p.add_argument("--save", action="store_true", help="simpan hasil ke database")
    p.add_argument("--json", default=None, help="simpan hasil ke file JSON")
    p.set_defaults(func=cmd_evaluate)

    sub.add_parser(
        "evaluate-all", help="evaluasi semua portofolio dan simpan ke database"
    ).set_defaults(func=cmd_evaluate_all)

    p = sub.add_parser("history", parents=[common], help="riwayat evaluasi tersimpan")
    p.add_argument("--limit", type=int, default=30)
    p.set_defaults(func=cmd_history)

    p = sub.add_parser("quick", parents=[common], help="estimasi cepat dari statistik universe (tanpa harga)")
    p.set_defaults(func=cmd_quick)

    args = parser.parse_args()
    try:
        args.func(args)
    except ValueError as exc:
        sys.exit(str(exc))
    except errors.UndefinedTable:
        sys.exit("tabel belum ada, jalankan dulu: python portfolio_cli.py init")
    except psycopg2.OperationalError as exc:
        sys.exit(f"database tidak terjangkau: {str(exc).strip()}")


if __name__ == "__main__":
    main()