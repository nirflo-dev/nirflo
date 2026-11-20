import argparse
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / "logs"
STEP_TIMEOUT_SECONDS = 3 * 3600
STOP_GRACE_SECONDS = 30

STEPS = {
    "prices": ["run.py", "prices"],
    "fetch": ["run.py", "fetch"],
    "universe": ["-m", "pipeline.build_universe"],
    "signals": ["-m", "analytics.signal_report", "--csv", "reports/signals.csv"],
    "portfolio": ["portfolio_cli.py", "evaluate-all"],
    "xbrl": ["data/r2/backup_xbrl.py"],
    "export": ["data/r2/export_db.py", "--upload"],
    "macro": ["-m", "pipeline.update_macro"],
}
R2_STEPS = ("xbrl", "export")


def log_has_errors(path, offset):
    with open(path, "rb") as f:
        f.seek(offset)
        text = f.read().decode("utf-8", errors="replace")
    return "[ERROR]" in text or "Traceback" in text


def stop_child(process, send_interrupt):
    if send_interrupt and process.poll() is None:
        process.send_signal(signal.SIGINT)
    try:
        process.wait(timeout=STOP_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def run_step(name, args, log, log_path):
    log.write(f"\n=== {name} | {datetime.now():%Y-%m-%d %H:%M:%S} ===\n")
    log.flush()
    offset = log_path.stat().st_size
    started = time.monotonic()
    process = subprocess.Popen(
        [sys.executable, *args],
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    try:
        code = process.wait(timeout=STEP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        stop_child(process, send_interrupt=True)
        code = -1
    except KeyboardInterrupt:
        stop_child(process, send_interrupt=False)
        raise
    log.flush()
    return code, time.monotonic() - started, log_has_errors(log_path, offset)


def select_steps(only, skip, include_r2):
    names = [n for n in STEPS if (only is None or n in only) and n not in skip]
    if only is None and not include_r2:
        names = [n for n in names if n not in R2_STEPS]
    return names


def main():
    parser = argparse.ArgumentParser(description="Jalankan alur harian PEAD.")
    parser.add_argument("--skip", nargs="*", default=[], choices=list(STEPS))
    parser.add_argument("--only", nargs="*", default=None, choices=list(STEPS))
    parser.add_argument("--r2", action="store_true", help="sertakan langkah R2 (xbrl dan export)")
    args = parser.parse_args()

    names = select_steps(args.only, args.skip, args.r2)
    LOG_DIR.mkdir(exist_ok=True)
    (ROOT / "reports").mkdir(exist_ok=True)
    log_path = LOG_DIR / f"daily_{datetime.now():%Y%m%d}.log"
    print(f"log: {log_path}")
    print(f"pantau: tail -f {log_path}")
    print("langkah: " + ", ".join(names), flush=True)

    failed = []
    current = None
    try:
        with open(log_path, "a", encoding="utf-8") as log:
            for name in names:
                current = name
                print(f"{name:<10} mulai {datetime.now():%H:%M:%S} ...", flush=True)
                code, seconds, has_errors = run_step(name, STEPS[name], log, log_path)
                if code != 0:
                    status = f"GAGAL (kode {code})"
                    failed.append(name)
                elif has_errors:
                    status = "selesai, ada ERROR di log"
                else:
                    status = "ok"
                print(f"{name:<10} {status} ({seconds:.0f} dtk)", flush=True)
    except KeyboardInterrupt:
        print(f"\ndihentikan pengguna pada langkah: {current}", flush=True)
        sys.exit(130)

    if failed:
        print("langkah gagal:", ", ".join(failed))
        sys.exit(1)


if __name__ == "__main__":
    main()