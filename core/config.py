import os
from pathlib import Path

from dotenv import load_dotenv

ENV_VAR = "MINTIGO_ENV_PATH"
DB_KEYS = {
    "dbname": "DB_NAME",
    "user": "DB_USER",
    "password": "DB_PASSWORD",
    "host": "DB_HOST",
    "port": "DB_PORT",
}
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _search_upward(start: Path) -> Path | None:
    for directory in (start, *start.parents):
        candidate = directory / ".env"
        if candidate.is_file():
            return candidate
    return None


def find_env_file() -> Path:
    override = os.getenv(ENV_VAR)
    if override:
        path = Path(override).expanduser()
        if not path.is_file():
            raise RuntimeError(f"{ENV_VAR} menunjuk ke file yang tidak ada: {path}")
        return path
    origins = (Path(__file__).resolve().parent, Path.cwd().resolve())
    for start in origins:
        found = _search_upward(start)
        if found is not None:
            return found
    searched = ", ".join(str(p) for p in origins)
    raise RuntimeError(f".env tidak ditemukan. Dicari ke atas dari: {searched}. Atau set {ENV_VAR}.")


ENV_PATH = find_env_file()
load_dotenv(dotenv_path=ENV_PATH)

DB_CONFIG = {key: os.getenv(name) for key, name in DB_KEYS.items()}

if not DB_CONFIG["password"]:
    raise RuntimeError(f"DB_PASSWORD kosong atau tidak ada di {ENV_PATH}")