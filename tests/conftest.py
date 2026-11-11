import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import pytest

from member_services.config import Settings
from member_services.mockserver import run_mock


@pytest.fixture(scope="session")
def pack_dir():
    d = Settings().pack_dir
    if not (d / "mock_services" / "server.py").is_file():
        pytest.skip(f"supplied pack not found at {d}; set PACK_DIR to run integration tests")
    return d


@pytest.fixture
def mock_url(pack_dir):
    """A fresh mock process per test, so in-memory state never leaks between tests."""
    with run_mock(pack_dir) as url:
        yield url
