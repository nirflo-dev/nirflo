import psycopg2

from core.config import DB_CONFIG

CONNECT_TIMEOUT = 3

DDL = """
    CREATE TABLE IF NOT EXISTS users (
        id           bigserial PRIMARY KEY,
        clerk_id     text NOT NULL UNIQUE,
        display_name text,
        profile      jsonb NOT NULL DEFAULT '{}'::jsonb,
        created_at   timestamptz NOT NULL DEFAULT now(),
        last_seen_at timestamptz NOT NULL DEFAULT now()
    );
"""


def _connect():
    return psycopg2.connect(**DB_CONFIG, connect_timeout=CONNECT_TIMEOUT)


def ensure_table():
    conn = _connect()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(DDL)
    finally:
        conn.close()


def ensure_user(clerk_id):
    conn = _connect()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users (clerk_id) VALUES (%s) "
                "ON CONFLICT (clerk_id) DO UPDATE SET last_seen_at = now() "
                "RETURNING id, display_name",
                (clerk_id,),
            )
            return cur.fetchone()
    finally:
        conn.close()


def get_or_create_user(clerk_id):
    return ensure_user(clerk_id)[0]


def set_display_name(user_id, name):
    conn = _connect()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET display_name = %s WHERE id = %s AND display_name IS NULL",
                (name, user_id),
            )
    finally:
        conn.close()