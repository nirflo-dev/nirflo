import hashlib

import psycopg2

from core import user_store
from core.config import DB_CONFIG
from core.ai_crypto import decrypt, encrypt

CONNECT_TIMEOUT = 3

DDL = """
    CREATE TABLE IF NOT EXISTS user_ai_keys (
        user_id       bigint NOT NULL REFERENCES users (id) ON DELETE CASCADE,
        provider      text NOT NULL,
        encrypted_key text NOT NULL,
        key_hash      text NOT NULL,
        created_at    timestamptz NOT NULL DEFAULT now(),
        updated_at    timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (user_id, provider)
    );
"""


def _connect():
    return psycopg2.connect(**DB_CONFIG, connect_timeout=CONNECT_TIMEOUT)


def ensure_table():
    user_store.ensure_table()
    conn = _connect()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(DDL)
    finally:
        conn.close()


def save_key(user_id, provider, key):
    """Simpan (atau ganti) key pengguna. Yang masuk database hanya hasil enkripsi dan hash SHA-256."""
    encrypted = encrypt(key)
    key_hash = hashlib.sha256(key.encode()).hexdigest()
    conn = _connect()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO user_ai_keys (user_id, provider, encrypted_key, key_hash) "
                "VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (user_id, provider) DO UPDATE SET "
                "encrypted_key = EXCLUDED.encrypted_key, key_hash = EXCLUDED.key_hash, updated_at = now()",
                (user_id, provider, encrypted, key_hash),
            )
    finally:
        conn.close()
    return key_hash


def get_status(user_id, provider):
    """Info tanpa membuka enkripsi: None bila belum tersambung."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT key_hash, updated_at FROM user_ai_keys WHERE user_id = %s AND provider = %s",
                (user_id, provider),
            )
            row = cur.fetchone()
    finally:
        conn.close()
    return {"key_hash": row[0], "updated_at": row[1]} if row else None


def get_key(user_id, provider):
    """Key asli (didekripsi) atau None. Hanya dipanggil dari server saat meneruskan permintaan."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT encrypted_key FROM user_ai_keys WHERE user_id = %s AND provider = %s",
                (user_id, provider),
            )
            row = cur.fetchone()
    finally:
        conn.close()
    return decrypt(row[0]) if row else None


def delete_key(user_id, provider):
    conn = _connect()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM user_ai_keys WHERE user_id = %s AND provider = %s",
                (user_id, provider),
            )
            return cur.rowcount > 0
    finally:
        conn.close()