import unittest
from unittest.mock import MagicMock, patch

from pyclaw.commands import Commands
from pyclaw.io import InputOutput
from pyclaw.scrape import Scraper


class TestScrape(unittest.TestCase):
    def test_scrape_self_signed_ssl(self):
        scraper_verify = Scraper(
            print_error=MagicMock(), playwright_available=True, verify_ssl=True
        )
        scraper_verify.scrape_with_playwright = MagicMock(return_value=(None, None))
        scraper_verify.scrape_with_httpx = MagicMock(return_value=(None, None))
        result_verify = scraper_verify.scrape("https://self-signed.badssl.com")
        self.assertIsNone(result_verify)
        scraper_verify.print_error.assert_called()
        self.assertFalse(scraper_verify.playwright_available)

        scraper_no_verify = Scraper(
            print_error=MagicMock(), playwright_available=True, verify_ssl=False
        )
        scraper_no_verify.scrape_with_playwright = MagicMock(return_value=(None, None))
        scraper_no_verify.scrape_with_httpx = MagicMock(
            return_value=("self-signed content", "text/plain")
        )
        result_no_verify = scraper_no_verify.scrape("https://self-signed.badssl.com")
        self.assertIsNotNone(result_no_verify)
        self.assertIn("self-signed", result_no_verify)
        scraper_no_verify.print_error.assert_not_called()

    def setUp(self):
        self.io = InputOutput(yes=True)
        self.commands = Commands(self.io, None)

    def test_cmd_web_imports_playwright(self):
        mock_print_error = MagicMock()
        self.commands.io.tool_error = mock_print_error

        with (
            patch("pyclaw.commands.install_playwright", return_value=False),
            patch("pyclaw.commands.Scraper") as mock_scraper_cls,
        ):
            mock_scraper = mock_scraper_cls.return_value
            mock_scraper.scrape.return_value = "Example Domain"
            result = self.commands.cmd_web("https://example.com", return_content=True)

        self.assertIsNotNone(result)
        self.assertNotEqual(result, "")
        self.assertIn("Example Domain", result)
        mock_print_error.assert_not_called()

    def test_scrape_actual_url_with_playwright(self):
        mock_print_error = MagicMock()
        scraper = Scraper(print_error=mock_print_error, playwright_available=True)
        scraper.scrape_with_playwright = MagicMock(
            return_value=("<html><body><h1>Example Domain</h1></body></html>", "text/html")
        )
        scraper.html_to_markdown = MagicMock(return_value="Example Domain")

        result = scraper.scrape("https://example.com")

        self.assertIsNotNone(result)
        self.assertIn("Example Domain", result)
        mock_print_error.assert_not_called()

    def test_scraper_print_error_not_called(self):
        mock_print_error = MagicMock()
        scraper = Scraper(print_error=mock_print_error, verify_ssl=False)

        scraper.scrape_with_httpx = MagicMock(return_value=("plain text", "text/plain"))
        scraper.scrape_with_httpx("https://example.com")
        scraper.try_pandoc()
        scraper.html_to_markdown("<html><body><h1>Test</h1></body></html>")

        mock_print_error.assert_not_called()

    def test_scrape_with_playwright_error_handling(self):
        mock_print_error = MagicMock()
        scraper = Scraper(print_error=mock_print_error, playwright_available=True)
        scraper.scrape_with_playwright = MagicMock(return_value=(None, None))
        scraper.scrape_with_httpx = MagicMock(return_value=(None, None))
        result = scraper.scrape("https://example.com")

        self.assertIsNone(result)
        mock_print_error.assert_called_once_with(
            "Failed to retrieve content from https://example.com"
        )
        mock_print_error.reset_mock()

        scraper.playwright_available = True
        scraper.scrape_with_playwright.return_value = ("Some content", "text/html")
        scraper.scrape_with_httpx.reset_mock()
        result = scraper.scrape("https://example.com")

        self.assertIsNotNone(result)
        scraper.scrape_with_httpx.assert_not_called()
        mock_print_error.assert_not_called()

    def test_scrape_text_plain(self):
        # Create a Scraper instance
        scraper = Scraper(print_error=MagicMock(), playwright_available=True)

        # Mock the scrape_with_playwright method
        plain_text = "This is plain text content."
        scraper.scrape_with_playwright = MagicMock(return_value=(plain_text, "text/plain"))

        # Call the scrape method
        result = scraper.scrape("https://example.com")

        # Assert that the result is the same as the input plain text
        self.assertEqual(result, plain_text)

    def test_scrape_text_html(self):
        # Create a Scraper instance
        scraper = Scraper(print_error=MagicMock(), playwright_available=True)

        # Mock the scrape_with_playwright method
        html_content = "<html><body><h1>Test</h1><p>This is HTML content.</p></body></html>"
        scraper.scrape_with_playwright = MagicMock(return_value=(html_content, "text/html"))

        # Mock the html_to_markdown method
        expected_markdown = "# Test\n\nThis is HTML content."
        scraper.html_to_markdown = MagicMock(return_value=expected_markdown)

        # Call the scrape method
        result = scraper.scrape("https://example.com")

        # Assert that the result is the expected markdown
        self.assertEqual(result, expected_markdown)

        # Assert that html_to_markdown was called with the HTML content
        scraper.html_to_markdown.assert_called_once_with(html_content)


if __name__ == "__main__":
    unittest.main()
