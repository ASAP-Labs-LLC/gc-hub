"""Guard: QBench credentials must never be literals in this repository.

They are resolved at runtime from the local store (see ``qbench_secrets``).
The literal values are deliberately absent from this file -- the assertion is
on the *shape* of the assignment, so this test can never itself reintroduce a
secret. Verified by planting a literal and watching it fail.
"""
import pathlib
import re

SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules", "build", "dist"}

# CLIENT_ID/CLIENT_SECRET assigned a quoted literal, or a UUID used as an
# os.getenv() fallback -- both are how the secrets leaked before.
ASSIGN = re.compile(
    r"""^\s*[A-Z_]*CLIENT_(?:ID|SECRET)[A-Z_]*\s*=\s*['"][^'"]{8,}['"]""", re.M)
GETENV_FALLBACK = re.compile(
    r"""os\.getenv\(\s*['"][^'"]*CLIENT_(?:ID|SECRET)[^'"]*['"]\s*,\s*['"][0-9a-fA-F-]{20,}['"]""")


def _source_files():
    root = pathlib.Path(__file__).resolve().parent.parent
    for path in list(root.rglob("*.py")) + list(root.rglob("*.pyw")):
        if SKIP_DIRS & set(path.parts):
            continue
        if path.name == pathlib.Path(__file__).name:
            continue
        yield root, path


def test_no_hardcoded_client_credentials():
    offenders = []
    for root, path in _source_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        if ASSIGN.search(text) or GETENV_FALLBACK.search(text):
            offenders.append(str(path.relative_to(root)))
    assert offenders == [], (
        "QBench credentials must come from qbench_secrets, not literals. "
        f"Offending files: {offenders}")
