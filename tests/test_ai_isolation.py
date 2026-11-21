"""Menjaga fitur AI tetap terpisah dari perhitungan kuantitatif, dua arah.

Jalankan: pytest tests/test_ai_isolation.py   (atau: python tests/test_ai_isolation.py)
"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

AI_FILES = [ROOT / "api" / "ai.py", ROOT / "core" / "ai_key_store.py", ROOT / "core" / "ai_crypto.py"]
AI_MODULES = ("api.ai", "core.ai_key_store", "core.ai_crypto")

# Kode AI tidak boleh bergantung pada apa pun yang menghitung atau membaca data kuantitatif.
FORBIDDEN_FOR_AI = (
    "analytics", "pipeline",
    "api.main", "api.portfolio", "api.jobs",
    "core.history", "core.portfolio_store", "core.portfolio_data", "core.universe_store",
)

# Kode perhitungan/data tidak boleh mengimpor AI. api/main.py hanya boleh mendaftarkan router-nya.
CALC_DIRS = ("analytics", "pipeline", "core", "api")
CALC_ALLOWED_AI_IMPORT = {ROOT / "api" / "main.py"}


def _matches(name: str, prefixes) -> bool:
    return any(name == p or name.startswith(p + ".") for p in prefixes)


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
    return found


def _calc_files():
    for folder in CALC_DIRS:
        for path in (ROOT / folder).rglob("*.py"):
            if path not in AI_FILES and ".venv" not in path.parts and "__pycache__" not in path.parts:
                yield path


def test_ai_code_does_not_import_calculation_code():
    for path in filter(Path.exists, AI_FILES):
        bad = sorted(n for n in _imports(path) if _matches(n, FORBIDDEN_FOR_AI))
        assert not bad, f"{path.relative_to(ROOT)} mengimpor kode perhitungan: {bad}"


def test_calculation_code_does_not_import_ai():
    for path in _calc_files():
        if path in CALC_ALLOWED_AI_IMPORT:
            continue
        bad = sorted(n for n in _imports(path) if _matches(n, AI_MODULES))
        assert not bad, f"{path.relative_to(ROOT)} mengimpor modul AI: {bad}"


def test_calculation_code_never_mentions_ai_providers():
    for folder in ("analytics", "pipeline"):
        for path in (ROOT / folder).rglob("*.py"):
            text = path.read_text(encoding="utf-8", errors="ignore").lower()
            assert "openrouter" not in text, f"{path.relative_to(ROOT)} menyebut openrouter"


if __name__ == "__main__":
    test_ai_code_does_not_import_calculation_code()
    test_calculation_code_does_not_import_ai()
    test_calculation_code_never_mentions_ai_providers()
    print("OK: fitur AI terisolasi dari perhitungan")