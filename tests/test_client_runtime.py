from datetime import datetime, timezone
import unittest

from xray_vps_manager.clients import runtime


class ClientRuntimeTests(unittest.TestCase):
    def test_disabled_client_keeps_last_online_time(self) -> None:
        traffic_db = {
            "clients": {
                "alice": {
                    "lastOnline": "2026-06-12T08:03:00Z",
                },
            },
        }

        state, last_online = runtime.online_state(
            {"name": "alice", "status": "disabled"},
            traffic_db,
            timezone.utc,
            now_utc=datetime(2026, 6, 12, 8, 4, tzinfo=timezone.utc),
        )

        self.assertEqual(state, "offline")
        self.assertEqual(last_online, "2026-06-12 08:03 UTC")

    def test_client_without_last_online_still_reports_never(self) -> None:
        state, last_online = runtime.online_state(
            {"name": "alice", "status": "enabled"},
            {"clients": {"alice": {}}},
            timezone.utc,
            now_utc=datetime(2026, 6, 12, 8, 4, tzinfo=timezone.utc),
        )

        self.assertEqual(state, "offline")
        self.assertEqual(last_online, "never")


if __name__ == "__main__":
    unittest.main()
