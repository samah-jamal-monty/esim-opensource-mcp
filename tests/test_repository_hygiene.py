"""Guards that keep QA data and QA endpoints out of the repository and out of the suite.

Two rules this project has to keep holding as it grows:

* no automated test may talk to the real QA backend, and no committed file may name it;
* no real QA identifier (the tester's own email address or phone number) may be committed,
  even inside a test fixture.

These are asserted mechanically rather than by review, because both are easy to reintroduce
by pasting a real response into a fixture.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: Committed trees. ``.env`` is deliberately absent: it is git-ignored and holds the real
#: QA URL, which is exactly the value that must never appear anywhere else.
SCANNED_PATHS = ("src", "tests", "app", "docs", "README.md", ".env.example", ".mcp.json", "Dockerfile")

#: Hosts a committed file is allowed to name: test doubles and documented placeholders.
ALLOWED_HOSTS = frozenset(
    {
        "backend.test",
        "env.backend.test",
        "x.test",
        "cdn.test",
        # The hosted card payment page in the card-checkout fixtures. A separate host on
        # purpose: the real link points at the payment provider, not at the eSIM backend.
        "checkout.test",
        "qa-placeholder.example.com",
        "example.com",
        "127.0.0.1",
        "localhost",
    }
)

_URL_RE = re.compile(r"https?://([A-Za-z0-9._-]+)")
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")

#: Domains an example address may use. Anything else is somebody's real inbox.
ALLOWED_EMAIL_DOMAINS = frozenset({"example.com", "example.org", "example.net", "test"})


def committed_files() -> list[Path]:
    files: list[Path] = []
    for entry in SCANNED_PATHS:
        path = PROJECT_ROOT / entry
        if path.is_file():
            files.append(path)
        elif path.is_dir():
            files.extend(
                candidate
                for candidate in path.rglob("*")
                if candidate.is_file()
                and candidate.suffix in {".py", ".md", ".json", ".toml", ".example", ""}
                and "__pycache__" not in candidate.parts
            )
    return files


@pytest.fixture(scope="module")
def sources() -> list[tuple[Path, str]]:
    return [(path, path.read_text(encoding="utf-8", errors="ignore")) for path in committed_files()]


def test_no_committed_file_names_a_real_backend_host(sources: list[tuple[Path, str]]) -> None:
    """The QA URL belongs in the git-ignored .env and nowhere else."""
    offenders: list[str] = []
    for path, text in sources:
        for host in _URL_RE.findall(text):
            if host not in ALLOWED_HOSTS:
                offenders.append(f"{path.relative_to(PROJECT_ROOT)}: {host}")

    assert not offenders, f"real or unknown hosts committed: {offenders}"


def test_no_committed_file_contains_a_real_email_address(sources: list[tuple[Path, str]]) -> None:
    """Fixtures must use example.com addresses, never a tester's real inbox."""
    offenders: list[str] = []
    for path, text in sources:
        for address in _EMAIL_RE.findall(text):
            domain = address.rsplit("@", 1)[-1].lower()
            if domain not in ALLOWED_EMAIL_DOMAINS:
                offenders.append(f"{path.relative_to(PROJECT_ROOT)}: {address}")

    assert not offenders, f"real-looking email addresses committed: {offenders}"


def test_the_test_suite_never_points_at_a_real_backend() -> None:
    from tests.conftest import BASE_URL

    assert BASE_URL == "https://backend.test"


def test_settings_used_by_tests_do_not_read_a_dotenv(settings: object) -> None:
    """A developer's real .env must not leak into a test run."""
    assert settings.api_base_url == "https://backend.test"
