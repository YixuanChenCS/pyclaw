from urllib.parse import urlparse

from pyclaw import urls


def iter_url_values():
    for attr in dir(urls):
        if attr.startswith("__"):
            continue
        value = getattr(urls, attr)
        if callable(value):
            continue
        yield attr, value


def test_url_constants_are_absolute_https_urls():
    for attr, value in iter_url_values():
        parsed = urlparse(value)
        assert parsed.scheme == "https", f"{attr} should use https: {value}"
        assert parsed.netloc, f"{attr} should include a host: {value}"


def test_pyclaw_docs_urls_share_the_expected_base():
    assert urls.website == "https://pyclaw.chat/"

    docs_attrs = [
        "add_all_files",
        "analytics",
        "edit_errors",
        "edit_formats",
        "enable_playwright",
        "git",
        "install_properly",
        "large_repos",
        "llms",
        "model_warnings",
        "models_and_keys",
        "release_notes",
        "token_limits",
    ]

    for attr in docs_attrs:
        assert getattr(urls, attr).startswith("https://pyclaw.chat/"), attr
