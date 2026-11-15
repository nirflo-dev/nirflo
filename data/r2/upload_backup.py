import argparse
import os

import r2_common as common


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", choices=["dump", "parquet"])
    parser.add_argument("--dump", help="path satu file dump; default: dump terbaru tiap database")
    parser.add_argument("--snapshot", help="nama folder snapshot, misalnya 20261024_220000")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    def job():
        failed = 0
        if args.only in (None, "dump"):
            paths = [args.dump] if args.dump else common.latest_local_dumps()
            if not paths:
                common.logger.warning("tidak ada dump lokal, dilewati")
            for path in paths:
                failed += common.upload_dump(path)
        if args.only in (None, "parquet"):
            if args.snapshot:
                folder = os.path.join(common.OUT_ROOT, "parquet", args.snapshot)
            else:
                folder = common.latest_local_snapshot()
            failed += common.upload_snapshot(folder, args.force)
        return failed

    common.run_job("upload_backup", job)


if __name__ == "__main__":
    main()