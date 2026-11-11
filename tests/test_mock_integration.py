import httpx
import pytest

pytestmark = pytest.mark.integration


def test_mock_starts_from_pack_dir_and_reports_clock(mock_url):
    body = httpx.get(f"{mock_url}/health").json()
    assert body == {"status": "ok", "clock": "2026-11-15"}
