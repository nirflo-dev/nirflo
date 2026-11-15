import argparse
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / "logs"
STEP_TIMEOUT_SECONDS = 3 * 3600

STEPS = {
    "prices": ["run.py", "prices"],
    "fetch": ["run.py", "fetch"],
    "signals": ["-m", "analytics.signal_report", "--csv", "reports/signals.csv"],
    "portfolio": ["portfolio_cli.py", "evaluate-all"],
    "xbrl": ["-m", "pipeline.backup_xbrl"],
    "export": ["data/r2/export_db.py", "--upload"],
}


def log_has_errors(path, offset):
    with open(path, "rb") as f:
        f.seek(offset)
        text = f.read().decode("utf-8", errors="replace")
    return "[ERROR]" in text or "Traceback" in text


def run_step(name, args, log, log_path):
    log.write(f"\n=== {name} | {datetime.now():%Y-%m-%d %H:%M:%S} ===\n")
    log.flush()
    offset = log_path.stat().st_size
    started = time.monotonic()
    try:
        code = subprocess.run(
            [sys.executable, *args],
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            timeout=STEP_TIMEOUT_SECONDS,
        ).returncode
    except subprocess.TimeoutExpired:
        code = -1
    log.flush()
    return code, time.monotonic() - started, log_has_errors(log_path, offset)


def main():
    parser = argparse.ArgumentParser(description="Jalankan alur harian PEAD.")
    parser.add_argument("--skip", nargs="*", default=[], choices=list(STEPS))
    parser.add_argument("--only", nargs="*", default=None, choices=list(STEPS))
    args = parser.parse_args()

    names = [n for n in STEPS if (args.only is None or n in args.only) and n not in args.skip]
    LOG_DIR.mkdir(exist_ok=True)
    (ROOT / "reports").mkdir(exist_ok=True)
    log_path = LOG_DIR / f"daily_{datetime.now():%Y%m%d}.log"

    failed = []
    with open(log_path, "a", encoding="utf-8") as log:
        for name in names:
            code, seconds, has_errors = run_step(name, STEPS[name], log, log_path)
            if code != 0:
                status = f"GAGAL (kode {code})"
                failed.append(name)
            elif has_errors:
                status = "selesai, ada ERROR di log"
            else:
                status = "ok"
            print(f"{name:<10} {status} ({seconds:.0f} dtk)")
    print(f"log: {log_path}")
    if failed:
        print("langkah gagal:", ", ".join(failed))
        sys.exit(1)


if __name__ == "__main__":
    main()