from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pyclaw.analytics import Analytics


class TestAnalyticsCache(unittest.TestCase):
    def test_load_data_disables_analytics_on_corrupt_json(self):
        # Verifies a corrupt analytics cache is handled by disabling analytics rather than loading partial state.
        # This catches silent state corruption where malformed JSON might leave stale opt-in flags or UUIDs active.
        # The expected disabled state is correct because load_data explicitly treats JSONDecodeError as an invalid cache.
        with tempfile.TemporaryDirectory() as tmpdir:
            temp_home = Path(tmpdir)
            data_file = temp_home / ".pyclaw" / "analytics.json"
            data_file.parent.mkdir(parents=True, exist_ok=True)
            data_file.write_text("{not valid json", encoding="utf-8")

            with patch.object(Path, "home", return_value=temp_home):
                analytics = Analytics(permanently_disable=False)

            self.assertIsNone(analytics.ph)
            self.assertIsNone(analytics.mp)
            self.assertIsNot(analytics.permanently_disable, True)

    def test_load_data_disables_analytics_on_read_error(self):
        # Verifies read-time OS errors disable analytics instead of leaving a half-loaded state.
        # This catches cases where an unreadable cache file would otherwise crash initialization or preserve stale fields.
        # The expected behavior is correct because load_data handles OSError by disabling analytics non-permanently.
        with tempfile.TemporaryDirectory() as tmpdir:
            temp_home = Path(tmpdir)
            analytics_path = temp_home / ".pyclaw" / "analytics.json"
            analytics_path.parent.mkdir(parents=True, exist_ok=True)
            analytics_path.write_text('{"uuid":"abc"}', encoding="utf-8")

            original_read_text = Path.read_text

            def raising_read_text(path_self, *args, **kwargs):
                if path_self == analytics_path:
                    raise OSError("cannot read analytics cache")
                return original_read_text(path_self, *args, **kwargs)

            with (
                patch.object(Path, "home", return_value=temp_home),
                patch.object(Path, "read_text", new=raising_read_text),
            ):
                analytics = Analytics(permanently_disable=False)

            self.assertIsNone(analytics.ph)
            self.assertIsNone(analytics.mp)


if __name__ == "__main__":
    unittest.main()
