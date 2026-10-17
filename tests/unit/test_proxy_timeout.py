"""The optional nginx frontend must outlive synchronous generation requests."""

from pathlib import Path
import re

from reportagent.services.reproduction import GENERATION_TIMEOUT


def test_api_proxy_allows_generation_timeout_response_to_arrive():
    root = Path(__file__).resolve().parents[2]
    config = (root / "deploy/nginx.conf").read_text(encoding="utf-8")
    api_location = re.search(r"location /api/ \{([^}]+)\}", config)
    assert api_location is not None
    timeout = re.search(r"proxy_read_timeout (\d+)s;", api_location[1])
    assert timeout is not None
    assert int(timeout[1]) >= GENERATION_TIMEOUT + 60
