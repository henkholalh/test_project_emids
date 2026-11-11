"""Guards that run in CI: no secrets and no supplied pack data may ever be committed."""
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

SECRET_PATTERNS = {
    "tenant API token": re.compile(r"mbrsvc_[a-z0-9_]{6,}"),
    "directory API key": re.compile(r"cfn_dir_[0-9a-f]{8,}"),
    "Anthropic API key": re.compile(r"sk-ant-[A-Za-z0-9_-]{10,}"),
    "US SSN": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
}
FORBIDDEN_PATHS = ("mock_services/", "kb/", "data/", "tools/", "eval/conversations.json", "BRIEF.md")


def tracked_files() -> list[str]:
    try:
        out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"], cwd=ROOT,
                             capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")
    return [f for f in out.splitlines() if f]


def test_no_supplied_pack_files_in_repo():
    bad = [f for f in tracked_files() if f.startswith(FORBIDDEN_PATHS)]
    assert not bad, f"supplied pack files must not be committed: {bad}"


def test_no_secrets_or_ssns_in_repo():
    hits = []
    for rel in tracked_files():
        if rel == "tests/test_repo_hygiene.py":
            continue
        try:
            text = (ROOT / rel).read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError):
            continue
        hits += [f"{rel}: {name}" for name, rx in SECRET_PATTERNS.items() if rx.search(text)]
    assert not hits, f"possible secrets or PHI committed: {hits}"
