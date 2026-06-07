import requests
import pytest

from pyclaw import urls


def test_urls():
    url_attributes = [
        attr
        for attr in dir(urls)
        if not callable(getattr(urls, attr)) and not attr.startswith("__")
    ]
    for attr in url_attributes:
        url = getattr(urls, attr)
        try:
            response = requests.get(url, timeout=5)
        except requests.RequestException as exc:
            pytest.skip(f"network unavailable while checking {url}: {exc}")
        assert response.status_code == 200, f"URL {url} returned status code {response.status_code}"
