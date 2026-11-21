"""Enkripsi key AI pengguna (Fernet). Kunci enkripsinya dibaca dari environment, tidak pernah disimpan di database.

Paket cryptography diimpor saat dipakai, jadi API tetap bisa menyala walau paket ini belum terpasang.
"""
import os

KEY_VAR = "AI_KEY_ENCRYPTION_KEY"
PREVIOUS_VAR = "AI_KEY_ENCRYPTION_KEY_PREVIOUS"


class CryptoUnavailable(RuntimeError):
    """Kunci enkripsi belum diatur, tidak valid, atau tidak cocok dengan data yang tersimpan."""


def _cipher():
    try:
        from cryptography.fernet import Fernet, MultiFernet
    except ImportError as exc:
        raise CryptoUnavailable("Paket cryptography belum terpasang. Jalankan: pip install cryptography") from exc
    # Kunci pertama dipakai untuk mengenkripsi. Semua kunci dicoba saat membuka (untuk rotasi).
    keys = [os.getenv(name, "").strip() for name in (KEY_VAR, PREVIOUS_VAR)]
    keys = [k for k in keys if k]
    if not keys:
        raise CryptoUnavailable(f"Fitur AI belum diaktifkan: {KEY_VAR} belum diisi di .env.")
    try:
        return MultiFernet([Fernet(k) for k in keys])
    except ValueError as exc:
        raise CryptoUnavailable(f"{KEY_VAR} tidak valid. Buat ulang dengan Fernet.generate_key().") from exc


def is_configured() -> bool:
    try:
        _cipher()
    except CryptoUnavailable:
        return False
    return True


def encrypt(value: str) -> str:
    return _cipher().encrypt(value.encode()).decode()


def decrypt(token: str) -> str:
    cipher = _cipher()
    from cryptography.fernet import InvalidToken

    try:
        return cipher.decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise CryptoUnavailable("Key tersimpan tidak bisa dibuka (kunci enkripsi berubah). Sambungkan ulang.") from exc