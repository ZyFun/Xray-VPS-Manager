from zoneinfo import ZoneInfo
import unittest

from xray_vps_manager.core import time as core_time


class CoreTimeTests(unittest.TestCase):
    def test_xray_access_time_uses_source_timezone(self) -> None:
        self.assertEqual(
            core_time.xray_access_time_to_iso(
                "2026/06/21 02:30:00",
                ZoneInfo("Europe/Moscow"),
            ),
            "2026-06-20T23:30:00Z",
        )


if __name__ == "__main__":
    unittest.main()
