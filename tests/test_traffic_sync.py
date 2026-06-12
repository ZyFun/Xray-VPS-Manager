from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from xray_vps_manager.commands import traffic_sync


class TrafficSyncTests(unittest.TestCase):
    def test_sync_locked_loads_traffic_through_read_switch(self) -> None:
        email = "alice|created=2026-06-12T08:00:00Z"
        db = {
            "clients": {
                "alice": {
                    "email": email,
                    "incoming": 100,
                    "outgoing": 200,
                    "last": {"uplink": 10, "downlink": 20},
                    "history": {},
                }
            }
        }
        runtime = {
            f"user>>>{email}>>>traffic>>>uplink": 15,
            f"user>>>{email}>>>traffic>>>downlink": 30,
        }

        with tempfile.TemporaryDirectory() as tmp_dir:
            with mock.patch.object(traffic_sync, "known_clients", return_value={"alice": email}), \
                mock.patch.object(traffic_sync, "query_runtime_stats", return_value=runtime), \
                mock.patch.object(
                    traffic_sync,
                    "local_bucket_time",
                    return_value=datetime(2026, 6, 12, 8, 0, tzinfo=timezone.utc),
                ), \
                mock.patch.object(traffic_sync, "now", return_value="2026-06-12T05:00:00Z"), \
                mock.patch.object(traffic_sync, "ACCESS_LOG_PATH", Path(tmp_dir) / "missing-access.log"), \
                mock.patch.object(traffic_sync, "log"), \
                mock.patch.object(
                    traffic_sync.traffic_repository,
                    "load_traffic_db_for_read",
                    return_value=db,
                ) as load_for_read, \
                mock.patch.object(
                    traffic_sync.traffic_repository,
                    "load_traffic_db",
                    side_effect=AssertionError("traffic sync must use read-aware load"),
                ), \
                mock.patch.object(traffic_sync, "save_traffic") as save_traffic:
                result = traffic_sync.sync_locked()

        self.assertEqual(result, 0)
        load_for_read.assert_called_once_with(traffic_sync.TRAFFIC_PATH)
        saved = save_traffic.call_args.args[0]
        entry = saved["clients"]["alice"]
        self.assertEqual(entry["incoming"], 105)
        self.assertEqual(entry["outgoing"], 210)
        self.assertEqual(entry["history"]["2026-06-12"]["08"], {"incoming": 5, "outgoing": 10})


if __name__ == "__main__":
    unittest.main()
