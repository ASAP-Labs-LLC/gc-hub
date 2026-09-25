"""The running release tag. CI writes VERSION into the release zip; a
checkout has none and reports "dev" (correct, and never blank)."""
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent


def read_version(directory: Path = APP_DIR) -> str:
    try:
        line = (Path(directory) / "VERSION").read_text(encoding="utf-8").splitlines()[0].strip()
        return line or "dev"
    except (OSError, IndexError):
        return "dev"


APP_VERSION = read_version()
