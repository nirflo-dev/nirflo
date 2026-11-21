"""Konteks halaman untuk asisten AI.

Modul ini murni: tidak membaca database dan tidak mengimpor analytics/ atau pipeline/.
Isinya hanya merapikan snapshot yang dikirim browser (angka yang sudah tampil di layar)
dan menyaringnya menurut izin pengguna.
"""
from __future__ import annotations

import json
import re

SCOPES = ("off", "public", "portfolio")
MAX_ITEM = 6000      # karakter per item setelah diserialisasi
MAX_TOTAL = 16000    # karakter untuk seluruh konteks

_CLEAN = re.compile(r"[^\w .,&/+:()-]")


def _label(value) -> str:
    return _CLEAN.sub("", str(value))[:80]


def pack(items: list[dict], scope: str) -> tuple[str | None, list[str]]:
    """Kembalikan (teks konteks, daftar label yang dipakai). Item portofolio hanya lolos bila scope == 'portfolio'."""
    if scope not in ("public", "portfolio") or not items:
        return None, []
    allowed = {"market"} | ({"portfolio"} if scope == "portfolio" else set())
    parts: list[str] = []
    used: list[str] = []
    total = 0
    for item in items:
        kind = item.get("kind")
        if kind not in allowed:
            continue
        try:
            body = json.dumps(item.get("data"), ensure_ascii=False, separators=(",", ":"), default=str)
        except (TypeError, ValueError):
            continue
        body = body.replace("<", "\\u003c")  # data tidak bisa menutup tag pembungkus
        if len(body) > MAX_ITEM or total + len(body) > MAX_TOTAL:
            continue
        label = _label(item.get("label", ""))
        parts.append(f'<item kind="{kind}" label="{label}">\n{body}\n</item>')
        used.append(label)
        total += len(body)
    if not parts:
        return None, []
    return "<nirflo_data>\n" + "\n".join(parts) + "\n</nirflo_data>", used