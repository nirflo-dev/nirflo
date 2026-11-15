import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime

import pyarrow as pa
import pyarrow.parquet as pq
from sqlalchemy import create_engine, inspect, text
from sqlalchemy import types as sqltypes
from sqlalchemy.engine import URL

import r2_common as common

logger = common.logger

CHUNK_ROWS = 100_000
SKIP_SCHEMAS = {"information_schema", "pg_catalog", "pg_toast"}
MANIFEST_FIELDS = ["schema", "table", "rows_exported", "rows_pg", "size_mb", "status", "file"]


def db_settings():
    required = ["DB_NAME", "DB_USER", "DB_PASSWORD", "DB_HOST", "DB_PORT"]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        sys.exit("variabel belum diisi di .env: " + ", ".join(missing))
    cfg = {k: os.environ[k] for k in required}
    cfg["PG_DUMP"] = os.environ.get("PG_DUMP", "pg_dump")
    cfg["PG_DOCKER_CONTAINER"] = os.environ.get("PG_DOCKER_CONTAINER", "").strip()
    names = os.environ.get("DUMP_DATABASES", "")
    cfg["DUMP_DATABASES"] = [n.strip() for n in names.split(",") if n.strip()] or [cfg["DB_NAME"]]
    return cfg


def make_engine(cfg):
    url = URL.create(
        "postgresql+psycopg2",
        username=cfg["DB_USER"],
        password=cfg["DB_PASSWORD"],
        host=cfg["DB_HOST"],
        port=int(cfg["DB_PORT"]),
        database=cfg["DB_NAME"],
    )
    return create_engine(url)


# ---------- pg_dump ----------

def verify_dump_in_container(container, path, env):
    """Memastikan file dump bisa dibaca pg_restore (daftar isi dump terbaca utuh)."""
    with open(path, "rb") as f:
        subprocess.run(
            ["docker", "exec", "-i", container, "pg_restore", "--list"],
            stdin=f,
            stdout=subprocess.DEVNULL,
            check=True,
            env=env,
        )


def dump_one(cfg, database, stamp, out_dir):
    path = os.path.join(out_dir, f"{database}_{stamp}.dump")
    tmp_path = path + ".part"
    env = {**os.environ, "PGPASSWORD": cfg["DB_PASSWORD"]}
    container = cfg["PG_DOCKER_CONTAINER"]
    try:
        if container:
            # PGPASSWORD diteruskan dari environment proses ini, tidak muncul di daftar proses.
            cmd = [
                "docker", "exec", "-i", "-e", "PGPASSWORD", container,
                "pg_dump",
                "--format=custom",
                "--compress=9",
                "-U", cfg["DB_USER"],
                "-d", database,
            ]
            with open(tmp_path, "wb") as out:
                subprocess.run(cmd, check=True, env=env, stdout=out)
            verify_dump_in_container(container, tmp_path, env)
        else:
            cmd = [
                cfg["PG_DUMP"],
                "--format=custom",
                "--compress=9",
                "--file", tmp_path,
                "-h", cfg["DB_HOST"],
                "-p", cfg["DB_PORT"],
                "-U", cfg["DB_USER"],
                "-d", database,
            ]
            subprocess.run(cmd, check=True, env=env)
        if os.path.getsize(tmp_path) == 0:
            raise RuntimeError("file dump kosong")
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
    logger.info("dump selesai: %s (%.1f MB)", path, os.path.getsize(path) / 1024**2)
    return path


def run_dump(cfg, stamp):
    container = cfg["PG_DOCKER_CONTAINER"]
    if container:
        if shutil.which("docker") is None:
            sys.exit("docker tidak ditemukan di PATH, aktifkan WSL integration di Docker Desktop")
    else:
        pg_dump = cfg["PG_DUMP"]
        if shutil.which(pg_dump) is None and not os.path.exists(pg_dump):
            sys.exit("pg_dump tidak ditemukan, isi PG_DOCKER_CONTAINER atau PG_DUMP di .env")

    out_dir = os.path.join(common.OUT_ROOT, "dump")
    os.makedirs(out_dir, exist_ok=True)

    paths = []
    failed = 0
    for database in cfg["DUMP_DATABASES"]:
        try:
            paths.append(dump_one(cfg, database, stamp, out_dir))
        except Exception as e:
            logger.error("dump gagal untuk database %s: %s", database, e)
            failed += 1
    return paths, failed


# ---------- Parquet ----------

def quote(name):
    return '"' + name.replace('"', '""') + '"'


def to_float(v):
    return None if v is None else float(v)


def to_text(v):
    if v is None:
        return None
    if isinstance(v, str):
        return v
    if isinstance(v, (dict, list)):
        return json.dumps(v, default=str)
    if isinstance(v, (bytes, memoryview)):
        return bytes(v).hex()
    return str(v)


def arrow_spec(col_type):
    """Tipe Arrow ditentukan dari tipe kolom Postgres, bukan dari isi chunk pertama."""
    if isinstance(col_type, sqltypes.Boolean):
        return pa.bool_(), None
    if isinstance(col_type, sqltypes.Integer):
        return pa.int64(), None
    if isinstance(col_type, sqltypes.Numeric):  # termasuk Float dan DOUBLE PRECISION
        return pa.float64(), to_float
    if isinstance(col_type, sqltypes.DateTime):
        tz = "UTC" if getattr(col_type, "timezone", False) else None
        return pa.timestamp("us", tz=tz), None
    if isinstance(col_type, sqltypes.Date):
        return pa.date32(), None
    if isinstance(col_type, sqltypes.Time):
        return pa.time64("us"), None
    return pa.string(), to_text


def list_tables(insp):
    tables = []
    for schema in insp.get_schema_names():
        if schema in SKIP_SCHEMAS or schema.startswith("pg_"):
            continue
        for table in insp.get_table_names(schema=schema):
            tables.append((schema, table))
    return tables


def export_table(conn, insp, schema, table, path):
    columns = insp.get_columns(table, schema=schema)
    names = [c["name"] for c in columns]
    specs = [arrow_spec(c["type"]) for c in columns]
    pa_schema = pa.schema([(n, s[0]) for n, s in zip(names, specs)])

    select_list = ", ".join(quote(n) for n in names)
    target = f"{quote(schema)}.{quote(table)}"
    query = text(f"SELECT {select_list} FROM {target}").execution_options(stream_results=True)

    os.makedirs(os.path.dirname(path), exist_ok=True)
    total = 0
    with pq.ParquetWriter(path, pa_schema, compression="zstd") as writer:
        result = conn.execute(query)
        while True:
            rows = result.fetchmany(CHUNK_ROWS)
            if not rows:
                break
            arrays = []
            for i, (arrow_type, convert) in enumerate(specs):
                values = [row[i] for row in rows]
                if convert:
                    values = [convert(v) for v in values]
                arrays.append(pa.array(values, type=arrow_type))
            writer.write_table(pa.Table.from_arrays(arrays, schema=pa_schema))
            total += len(rows)
        result.close()

    expected = conn.execute(text(f"SELECT COUNT(*) FROM {target}")).scalar()
    return total, expected


def run_parquet(engine, stamp):
    out_dir = os.path.join(common.OUT_ROOT, "parquet", stamp)
    os.makedirs(out_dir, exist_ok=True)
    records = []

    # Satu transaksi REPEATABLE READ: semua tabel dibaca dari snapshot data yang sama.
    with engine.connect().execution_options(isolation_level="REPEATABLE READ") as conn:
        conn.execute(text("SET TRANSACTION READ ONLY"))
        insp = inspect(conn)
        for schema, table in list_tables(insp):
            rel = f"{schema}/{table}.parquet"
            path = os.path.join(out_dir, schema, f"{table}.parquet")
            try:
                with conn.begin_nested():  # savepoint: error satu tabel tidak membatalkan yang lain
                    n, expected = export_table(conn, insp, schema, table, path)
                status = "OK" if n == expected else "MISMATCH"
                size_mb = os.path.getsize(path) / 1024**2
                logger.info("%s %s.%s rows=%d/%d size=%.1f MB", status, schema, table, n, expected, size_mb)
                records.append([schema, table, n, expected, round(size_mb, 2), status, rel])
            except Exception as e:
                logger.error("FAILED %s.%s: %s", schema, table, e)
                if os.path.exists(path):
                    os.remove(path)
                records.append([schema, table, 0, -1, 0, "FAILED", ""])
        conn.rollback()

    manifest_path = os.path.join(out_dir, "manifest.csv")
    with open(manifest_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(MANIFEST_FIELDS)
        writer.writerows(records)

    bad = sum(1 for r in records if r[5] != "OK")
    logger.info(
        "parquet selesai: %d tabel, %d tidak OK, total %.1f MB",
        len(records), bad, sum(r[4] for r in records),
    )
    return out_dir, bad


# ---------- Main ----------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--upload", action="store_true")
    parser.add_argument("--only", choices=["dump", "parquet"])
    args = parser.parse_args()

    def job():
        cfg = db_settings()
        stamp = datetime.now().strftime(common.STAMP_FMT)
        failed = 0
        dump_paths = []
        snapshot_dir = None

        if args.only in (None, "dump"):
            dump_paths, bad = run_dump(cfg, stamp)
            failed += bad
        if args.only in (None, "parquet"):
            snapshot_dir, bad = run_parquet(make_engine(cfg), stamp)
            failed += bad

        if args.upload:
            for path in dump_paths:
                failed += common.upload_dump(path)
            if snapshot_dir:
                failed += common.upload_snapshot(snapshot_dir, force=False)

        common.rotate_local()
        logger.info("folder lokal: %s", common.OUT_ROOT)
        return failed

    common.run_job("export_db", job)


if __name__ == "__main__":
    main()