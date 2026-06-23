from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from zoneinfo import ZoneInfo

from xray_vps_manager.commands import traffic_sync


class TrafficSyncTests(unittest.TestCase):
    def test_access_log_time_uses_source_timezone(self) -> None:
        self.assertEqual(
            traffic_sync.access_time_to_iso(
                "2026/06/21 02:30:00",
                ZoneInfo("Europe/Moscow"),
            ),
            "2026-06-20T23:30:00Z",
        )

    def test_set_last_online_replaces_future_value_with_valid_event(self) -> None:
        entry = {
            "lastOnline": "2026-06-21T02:30:00Z",
            "lastOnlineSource": "access-log",
        }

        traffic_sync.set_last_online(
            entry,
            "2026-06-20T23:40:00Z",
            "access-log",
            "2026-06-20T23:41:00Z",
        )

        self.assertEqual(entry["lastOnline"], "2026-06-20T23:40:00Z")
        self.assertEqual(entry["lastOnlineSource"], "access-log")

    def test_known_clients_loads_client_db_through_read_switch(self) -> None:
        config_email = "config_user|created=2026-06-12T08:00:00Z"
        db_email = "db_user|created=2026-06-12T09:00:00Z"
        config = {
            "inbounds": [
                {
                    "protocol": "vless",
                    "streamSettings": {"security": "reality"},
                    "settings": {"clients": [{"email": config_email}]},
                }
            ]
        }
        db = {
            "clients": {
                "db_user": {"client": {"email": db_email}},
            }
        }

        with mock.patch.object(traffic_sync, "load_json", return_value=config), \
            mock.patch.object(
                traffic_sync.client_repository,
                "load_db_sql",
                return_value=db,
            ) as load_db_sql:
            self.assertEqual(
                traffic_sync.known_clients(),
                {
                    "config_user": config_email,
                    "db_user": db_email,
                },
            )

        load_db_sql.assert_called_once_with()

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
            with mock.patch.object(traffic_sync, "known_credentials", return_value={("alice", "vless-reality"): email}), \
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
        load_for_read.assert_called_once_with()
        saved = save_traffic.call_args.args[0]
        entry = saved["clients"]["alice"]
        self.assertEqual(entry["incoming"], 105)
        self.assertEqual(entry["outgoing"], 210)
        self.assertEqual(entry["history"]["2026-06-12"]["08"], {"incoming": 5, "outgoing": 10})
        credential = saved["credentials"]["alice"]["vless-reality"]
        self.assertEqual(credential["incoming"], 105)
        self.assertEqual(credential["outgoing"], 210)
        self.assertEqual(credential["history"]["2026-06-12"]["08"], {"incoming": 5, "outgoing": 10})

    def test_new_credential_starts_from_runtime_baseline_without_copying_old_traffic(self) -> None:
        vless_email = "alice|created=2026-06-12T08:00:00Z|connection=vless-reality"
        trojan_email = "alice|created=2026-06-23T17:00:00Z|connection=trojan-tls"
        db = {
            "clients": {
                "alice": {
                    "email": vless_email,
                    "incoming": 1000,
                    "outgoing": 2000,
                    "last": {"uplink": 100, "downlink": 200},
                    "history": {},
                }
            },
            "credentials": {
                "alice": {
                    "vless-reality": {
                        "email": vless_email,
                        "incoming": 1000,
                        "outgoing": 2000,
                        "last": {"uplink": 100, "downlink": 200},
                        "history": {},
                    }
                }
            },
        }
        runtime = {
            f"user>>>{vless_email}>>>traffic>>>uplink": 100,
            f"user>>>{vless_email}>>>traffic>>>downlink": 200,
            f"user>>>{trojan_email}>>>traffic>>>uplink": 93_710,
            f"user>>>{trojan_email}>>>traffic>>>downlink": 120_000,
        }

        with tempfile.TemporaryDirectory() as tmp_dir:
            with mock.patch.object(
                traffic_sync,
                "known_credentials",
                return_value={
                    ("alice", "vless-reality"): vless_email,
                    ("alice", "trojan-tls"): trojan_email,
                },
            ), mock.patch.object(traffic_sync, "query_runtime_stats", return_value=runtime), \
                mock.patch.object(
                    traffic_sync,
                    "local_bucket_time",
                    return_value=datetime(2026, 6, 23, 17, 0, tzinfo=timezone.utc),
                ), \
                mock.patch.object(traffic_sync, "now", return_value="2026-06-23T17:05:00Z"), \
                mock.patch.object(traffic_sync, "ACCESS_LOG_PATH", Path(tmp_dir) / "missing-access.log"), \
                mock.patch.object(traffic_sync, "log"), \
                mock.patch.object(
                    traffic_sync.traffic_repository,
                    "load_traffic_db_for_read",
                    return_value=db,
                ), \
                mock.patch.object(traffic_sync, "save_traffic") as save_traffic:
                result = traffic_sync.sync_locked()

        self.assertEqual(result, 0)
        saved = save_traffic.call_args.args[0]
        client = saved["clients"]["alice"]
        self.assertEqual(client["incoming"], 1000)
        self.assertEqual(client["outgoing"], 2000)
        self.assertNotIn("2026-06-23", client["history"])
        trojan = saved["credentials"]["alice"]["trojan-tls"]
        self.assertEqual(trojan["incoming"], 0)
        self.assertEqual(trojan["outgoing"], 0)
        self.assertEqual(trojan["last"], {"uplink": 93_710, "downlink": 120_000})
        self.assertEqual(trojan["history"], {})

    def test_sync_locked_normalizes_future_last_online(self) -> None:
        email = "iphone|created=2026-06-20T20:00:00Z"
        db = {
            "clients": {
                "iphone": {
                    "email": email,
                    "incoming": 100,
                    "outgoing": 200,
                    "last": {"uplink": 100, "downlink": 200},
                    "lastOnline": "2026-06-21T02:30:00Z",
                    "lastOnlineSource": "access-log",
                    "updated": "2026-06-20T23:30:00Z",
                    "history": {},
                }
            }
        }
        runtime = {
            f"user>>>{email}>>>traffic>>>uplink": 100,
            f"user>>>{email}>>>traffic>>>downlink": 200,
        }

        with tempfile.TemporaryDirectory() as tmp_dir:
            with mock.patch.object(traffic_sync, "known_credentials", return_value={("iphone", "vless-reality"): email}), \
                mock.patch.object(traffic_sync, "query_runtime_stats", return_value=runtime), \
                mock.patch.object(
                    traffic_sync,
                    "local_bucket_time",
                    return_value=datetime(2026, 6, 20, 23, 0, tzinfo=timezone.utc),
                ), \
                mock.patch.object(traffic_sync, "now", return_value="2026-06-20T23:31:00Z"), \
                mock.patch.object(traffic_sync, "ACCESS_LOG_PATH", Path(tmp_dir) / "missing-access.log"), \
                mock.patch.object(traffic_sync, "log"), \
                mock.patch.object(
                    traffic_sync.traffic_repository,
                    "load_traffic_db_for_read",
                    return_value=db,
                ), \
                mock.patch.object(traffic_sync, "save_traffic") as save_traffic:
                result = traffic_sync.sync_locked()

        self.assertEqual(result, 0)
        saved = save_traffic.call_args.args[0]
        entry = saved["clients"]["iphone"]
        self.assertEqual(entry["lastOnline"], "2026-06-20T23:30:00Z")
        self.assertEqual(entry["lastOnlineSource"], "traffic")


if __name__ == "__main__":
    unittest.main()
