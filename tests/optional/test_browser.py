import os
import unittest
from unittest.mock import patch

from pyclaw.main import main


class TestBrowser(unittest.TestCase):
    @patch("pyclaw.main.check_streamlit_install", return_value=True)
    @patch("pyclaw.main.launch_gui")
    def test_browser_flag_imports_streamlit(self, mock_launch_gui, mock_check_streamlit_install):
        os.environ["PYCLAW_ANALYTICS"] = "false"

        # Run main with --browser and --yes flags
        main(["--browser", "--yes"])

        mock_check_streamlit_install.assert_called_once()

        # Check that launch_gui was called
        mock_launch_gui.assert_called_once()


if __name__ == "__main__":
    unittest.main()
