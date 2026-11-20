import argparse
import json
import sys
from datetime import date, timedelta

import pandas as pd

from core.lse import LseClient, LseError, rows_to_frame


def cmd_keys(client, args):
    for label, data in client.usage():
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


def cmd_macro(client, args):
    series = client.macro_catalogue(args.kind, country=args.country, q=args.q)
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


def cmd_pairs(client, args):
    rows = client.pairs(args.market, q=args.q, limit=args.limit)
    if not rows:
        print("tidak ada instrumen")
        return
    for row in rows:
        print(
            f"{row.get('symbol'):<14} {str(row.get('name'))[:40]:<40} "
            f"{row.get('tick_first')} s.d. {row.get('tick_last')}"
        )


def cmd_probe(client, args):
    start = (date.today() - timedelta(days=args.days)).isoformat()
    payload = client.get_json("/v3/macro/series", kind=args.kind, code=args.code, **{"from": start})
    print("jawaban mentah (800 karakter pertama):")
    print(json.dumps(payload, ensure_ascii=False)[:800])
    print("\nhasil parsing (5 terakhir):")
    print(rows_to_frame(payload).tail(5).to_string(index=False))


def cmd_resolve(client, args):
    from pipeline.update_macro import LSE_MACRO_MAP, LSE_MARKET_MAP

    for ticker, spec in LSE_MACRO_MAP.items():
        found = client.resolve_macro(
            spec["kind"], spec["country"], spec.get("categories", ()), spec.get("codes", ())
        )
        if found is None:
            print(f"{ticker:<22} TIDAK DITEMUKAN (kind={spec['kind']}, country={spec['country']})")
        else:
            print(
                f"{ticker:<22} {found['code']:<30} {found.get('frequency', '')} "
                f"terakhir={found.get('last')} nilai={found.get('latest_value')}"
            )
    for ticker, spec in LSE_MARKET_MAP.items():
        rows = client.pairs(spec["market"], symbol=spec["symbol"])
        print(f"{ticker:<22} {spec['market']}/{spec['symbol']:<22} {'ada' if rows else 'TIDAK ADA'}")


def cmd_fxcheck(client, args):
    from pipeline import forex_engine as fx

    start = date.today() - timedelta(days=args.days)
    lse = fx._download_lse(start)
    yahoo = fx._download_yfinance(start)
    table = pd.concat([lse.rename("lse"), yahoo.rename("yahoo")], axis=1).dropna()
    table["selisih_pct"] = (table["lse"] / table["yahoo"] - 1) * 100
    print(table.round(3).tail(args.days).to_string())
    print(
        f"\nrata-rata selisih mutlak: {table['selisih_pct'].abs().mean():.3f}% "
        f"| maksimum: {table['selisih_pct'].abs().max():.3f}%"
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

    sub.add_parser("resolve", help="cocokkan peta di update_macro dengan katalog").set_defaults(func=cmd_resolve)

    p = sub.add_parser("fxcheck", help="bandingkan USD/IDR LSE dengan Yahoo")
    p.add_argument("--days", type=int, default=20)
    p.set_defaults(func=cmd_fxcheck)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return
    try:
        args.func(LseClient(), args)
    except LseError as exc:
        sys.exit(str(exc))


if __name__ == "__main__":
    main()