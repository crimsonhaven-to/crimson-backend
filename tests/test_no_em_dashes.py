"""House rule: no em or en dashes in anything this repo ships or documents.

Lumi's persona forbids them in her replies, and that instruction stays credible
only while the code telling her so does not use them itself. Where a dash is data
(a regex, a prompt quoting the banned character) the source spells it as an
escape.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {
    ".git", ".venv", ".venv-test", "venv", "__pycache__", "node_modules",
    ".mypy_cache", ".ruff_cache", ".pytest_cache", "graphify-out",
}
DASHES = ("\u2014", "\u2013")


def _text_files():
    for path in ROOT.rglob("*"):
        if path.is_dir() or SKIP_DIRS.intersection(path.relative_to(ROOT).parts):
            continue
        try:
            yield path, path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue


def test_no_file_contains_an_em_or_en_dash():
    offenders = [
        f"{path.relative_to(ROOT)}:{number}"
        for path, text in _text_files()
        for number, line in enumerate(text.splitlines(), 1)
        if any(dash in line for dash in DASHES)
    ]
    assert not offenders, offenders
