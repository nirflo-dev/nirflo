import csv
import glob
import json
import logging
import os
import re
import shutil
import sys
import time
import urllib.request
from contextlib import contextmanager
from datetime import datetime
from functools import lru_cache
from logging.handlers import RotatingFileHandler

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config
from botocore.exceptions import ClientError
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_CANDIDATES = [
    os.path.join(BASE_DIR, ".env"),
    os.path.join(os.path.dirname(BASE_DIR), ".env"),
    os.path.join(os.path.dirname(os.path.dirname(BASE_DIR)), ".env"),
]
OUT_ROOT = os.path.join(BASE_DIR, "backup")
DUMP_ROOT = "backups/"
SNAP_PREFIX = "snapshots/"
LATEST_KEY = SNAP_PREFIX + "latest.json"

STAMP_FMT = "%Y%m%d_%H%M%S"
STAMP_RE = re.compile(r"\d{8}_\d{6}")
SNAP_KEY_RE = re.compile(r"^" + re.escape(SNAP_PREFIX) + r"(\d{8}_\d{6})/")
DUMP_NAME_RE = re.compile(r"^(?P<db>.+)_\d{8}_\d{6}\.dump$")

KEEP_DAILY = 7
KEEP_WEEKLY = 4
KEEP_MONTHLY = 12
KEEP_LOCAL = 3
LOCK_STALE_HOURS = 6

logger = logging.getLogger("backup")

transfer_cfg = TransferConfig(
    multipart_threshold=64 * 1024 * 1024,
    multipart_chunksize=64 * 1024 * 1024,
    max_concurrency=8,
    use_threads=True,
)


# ---------- Environment, logging, lock, healthcheck ----------

def load_env():
    for path in ENV_CANDIDATES:
        if os.path.exists(path):
            load_dotenv(path)
            logger.info("env dimuat dari: %s", path)
            return
    sys.exit("file .env tidak ditemukan di: " + " atau ".join(ENV_CANDIDATES))


def setup_logging(name):
    log_dir = os.path.join(OUT_ROOT, "logs")
    os.makedirs(log_dir, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    file_handler = RotatingFileHandler(
        os.path.join(log_dir, f"{name}.log"),
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    stream_handler = logging.StreamHandler(sys.stdout)
    for handler in (file_handler, stream_handler):
        handler.setFormatter(fmt)
        root.addHandler(handler)
    for noisy in ("boto3", "botocore", "s3transfer", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


@contextmanager
def single_instance(name="backup"):
    os.makedirs(OUT_ROOT, exist_ok=True)
    path = os.path.join(OUT_ROOT, f"{name}.lock")
    if os.path.exists(path):
        age = time.time() - os.path.getmtime(path)
        if age > LOCK_STALE_HOURS * 3600:
            logger.warning("lock lama (%.1f jam) dihapus: %s", age / 3600, path)
            os.remove(path)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        logger.error("proses lain masih berjalan (lock: %s)", path)
        sys.exit(2)
    try:
        os.write(fd, str(os.getpid()).encode())
    finally:
        os.close(fd)
    try:
        yield
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def ping(status):
    base = os.environ.get("HEALTHCHECK_URL")
    if not base:
        return
    suffix = {"start": "/start", "ok": "", "fail": "/fail"}[status]
    try:
        urllib.request.urlopen(base.rstrip("/") + suffix, timeout=10).close()
    except Exception as e:
        logger.warning("ping healthcheck gagal: %s", e)


def run_job(name, fn):
    """Jalankan fn() dengan log, lock, dan healthcheck. fn mengembalikan jumlah kegagalan."""
    setup_logging(name)
    try:
        load_env()
        ping("start")
        with single_instance():
            failed = fn()
    except SystemExit as e:
        if e.code not in (0, None):
            if isinstance(e.code, str):
                logger.error(e.code)
            ping("fail")
        raise
    except Exception:
        logger.exception("job gagal dengan exception")
        ping("fail")
        sys.exit(1)
    if failed:
        logger.error("job selesai dengan %d kegagalan", failed)
        ping("fail")
        sys.exit(1)
    logger.info("job selesai tanpa error")
    ping("ok")


# ---------- R2 ----------

@lru_cache(maxsize=1)
def get_r2():
    required = ["R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET"]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        sys.exit("variabel R2 belum ada di .env: " + ", ".join(missing))
    match = re.search(r"[0-9a-f]{32}", os.environ["R2_ACCOUNT_ID"])
    if not match:
        sys.exit("R2_ACCOUNT_ID tidak berisi Account ID 32 karakter yang valid")
    client = boto3.client(
        "s3",
        endpoint_url=f"https://{match.group(0)}.r2.cloudflarestorage.com",
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
        config=Config(signature_version="s3v4", retries={"max_attempts": 8, "mode": "standard"}),
    )
    return client, os.environ["R2_BUCKET"]


def remote_size(s3, bucket, key):
    try:
        return s3.head_object(Bucket=bucket, Key=key)["ContentLength"]
    except ClientError as e:
        if e.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
            return None
        raise


def upload_verified(s3, bucket, path, key):
    size = os.path.getsize(path)
    try:
        if remote_size(s3, bucket, key) == size:
            logger.info("skip (sudah ada): %s", key)
            return True
        s3.upload_file(path, bucket, key, Config=transfer_cfg)
        ok = remote_size(s3, bucket, key) == size
    except Exception:
        logger.exception("upload gagal: %s", key)
        return False
    if ok:
        logger.info("upload ok: %s (%.1f MB)", key, size / 1024**2)
    else:
        logger.error("ukuran tidak cocok setelah upload: %s", key)
    return ok


def list_keys(s3, bucket, prefix):
    keys = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            keys.append(obj["Key"])
    return keys


# ---------- Retensi (harian / mingguan / bulanan) ----------

def parse_stamp(text):
    match = STAMP_RE.search(text)
    return datetime.strptime(match.group(0), STAMP_FMT) if match else None


def select_keep(stamps):
    """
    Pilih stamp yang dipertahankan: stamp terbaru dari tiap hari (KEEP_DAILY hari terakhir
    yang punya backup), tiap minggu (KEEP_WEEKLY), dan tiap bulan (KEEP_MONTHLY).
    """
    keep = set()
    seen_days, seen_weeks, seen_months = set(), set(), set()
    for s in sorted(stamps, reverse=True):
        day = s.date()
        iso = s.isocalendar()
        week = (iso[0], iso[1])
        month = (s.year, s.month)
        if day not in seen_days and len(seen_days) < KEEP_DAILY:
            seen_days.add(day)
            keep.add(s)
        if week not in seen_weeks and len(seen_weeks) < KEEP_WEEKLY:
            seen_weeks.add(week)
            keep.add(s)
        if month not in seen_months and len(seen_months) < KEEP_MONTHLY:
            seen_months.add(month)
            keep.add(s)
    return keep


def rotate_dumps(s3, bucket, prefix):
    items = {}
    for key in list_keys(s3, bucket, prefix):
        ts = parse_stamp(os.path.basename(key))
        if ts:
            items[key] = ts
    keep = select_keep(list(items.values()))
    for key, ts in items.items():
        if ts not in keep:
            s3.delete_object(Bucket=bucket, Key=key)
            logger.info("dump lama dihapus: %s", key)


def rotate_snapshots(s3, bucket, current_stamp):
    by_stamp = {}
    for key in list_keys(s3, bucket, SNAP_PREFIX):
        match = SNAP_KEY_RE.match(key)
        if match:
            by_stamp.setdefault(match.group(1), []).append(key)

    complete = [st for st, keys in by_stamp.items() if f"{SNAP_PREFIX}{st}/manifest.csv" in keys]
    keep = {s.strftime(STAMP_FMT) for s in select_keep([datetime.strptime(st, STAMP_FMT) for st in complete])}

    for stamp, keys in by_stamp.items():
        if stamp in keep or stamp == current_stamp:
            continue
        for key in keys:
            s3.delete_object(Bucket=bucket, Key=key)
        status = "lama" if stamp in complete else "tidak lengkap"
        logger.info("snapshot %s dihapus: %s", status, stamp)


def rotate_local():
    # Dump dikelompokkan per database supaya KEEP_LOCAL berlaku untuk masing-masing database.
    by_db = {}
    for path in sorted(glob.glob(os.path.join(OUT_ROOT, "dump", "*.dump"))):
        database = dump_database(path)
        if database:
            by_db.setdefault(database, []).append(path)
    for paths in by_db.values():
        for path in paths[:-KEEP_LOCAL]:
            os.remove(path)
            logger.info("dump lokal lama dihapus: %s", path)

    root = os.path.join(OUT_ROOT, "parquet")
    if os.path.isdir(root):
        dirs = sorted(d for d in os.listdir(root) if STAMP_RE.fullmatch(d))
        for name in dirs[:-KEEP_LOCAL]:
            shutil.rmtree(os.path.join(root, name))
            logger.info("snapshot lokal lama dihapus: %s", name)


# ---------- Upload ----------

def dump_database(path):
    """Nama database dari nama file dump: <database>_<YYYYMMDD>_<HHMMSS>.dump."""
    match = DUMP_NAME_RE.match(os.path.basename(path))
    return match.group("db") if match else None


def dump_prefix(database):
    return f"{DUMP_ROOT}{database}/"


def latest_local_dumps():
    """Dump lokal terbaru untuk tiap database."""
    by_db = {}
    for path in sorted(glob.glob(os.path.join(OUT_ROOT, "dump", "*.dump"))):
        database = dump_database(path)
        if database:
            by_db[database] = path
    return [by_db[name] for name in sorted(by_db)]


def latest_local_snapshot():
    root = os.path.join(OUT_ROOT, "parquet")
    if not os.path.isdir(root):
        return None
    dirs = sorted(d for d in os.listdir(root) if STAMP_RE.fullmatch(d))
    return os.path.join(root, dirs[-1]) if dirs else None


def upload_dump(path):
    if not path or not os.path.exists(path):
        logger.warning("dump tidak ditemukan, dilewati")
        return 0
    database = dump_database(path)
    if not database:
        logger.error("nama file dump tidak valid: %s", path)
        return 1
    s3, bucket = get_r2()
    prefix = dump_prefix(database)
    key = prefix + os.path.basename(path)
    if not upload_verified(s3, bucket, path, key):
        return 1
    rotate_dumps(s3, bucket, prefix)
    return 0


def read_manifest(folder):
    path = os.path.join(folder, "manifest.csv")
    if not os.path.exists(path):
        return None
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_latest(s3, bucket, stamp, table_count):
    body = json.dumps(
        {
            "stamp": stamp,
            "prefix": f"{SNAP_PREFIX}{stamp}/",
            "tables": table_count,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        },
        indent=2,
    )
    s3.put_object(Bucket=bucket, Key=LATEST_KEY, Body=body.encode("utf-8"), ContentType="application/json")
    logger.info("latest.json diperbarui: %s", stamp)


def upload_snapshot(folder, force=False):
    if not folder or not os.path.isdir(folder):
        logger.warning("snapshot parquet tidak ditemukan, dilewati")
        return 0
    stamp = os.path.basename(folder)
    if not STAMP_RE.fullmatch(stamp):
        logger.error("nama folder snapshot tidak valid: %s", stamp)
        return 1

    manifest = read_manifest(folder)
    if manifest is None:
        logger.error("manifest.csv tidak ada di snapshot %s, upload dibatalkan", stamp)
        return 1

    bad = [r for r in manifest if r["status"] != "OK"]
    if bad:
        for r in bad:
            logger.error(
                "tabel tidak OK: %s.%s status=%s rows=%s/%s",
                r["schema"], r["table"], r["status"], r["rows_exported"], r["rows_pg"],
            )
        if not force:
            logger.error("upload dibatalkan, gunakan --force untuk tetap mengunggah")
            return 1
        logger.warning("--force aktif, snapshot diunggah dengan %d tabel tidak OK", len(bad))

    s3, bucket = get_r2()
    failed = 0
    for root, _, files in os.walk(folder):
        for name in sorted(files):
            if root == folder and name == "manifest.csv":
                continue
            full = os.path.join(root, name)
            rel = os.path.relpath(full, folder).replace("\\", "/")
            if not upload_verified(s3, bucket, full, f"{SNAP_PREFIX}{stamp}/{rel}"):
                failed += 1
    if failed:
        logger.error("%d file gagal, manifest tidak diunggah (snapshot tetap berstatus belum lengkap)", failed)
        return failed

    manifest_path = os.path.join(folder, "manifest.csv")
    if not upload_verified(s3, bucket, manifest_path, f"{SNAP_PREFIX}{stamp}/manifest.csv"):
        return 1
    if not bad:
        write_latest(s3, bucket, stamp, len(manifest))
    rotate_snapshots(s3, bucket, stamp)
    return 0