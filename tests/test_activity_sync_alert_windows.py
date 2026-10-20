"""Окна подозрительной активности (`activity.alertWindows`) в `xray-activity sync`.

Характеризация на фикстуре access.log реального формата и регрессионные тесты VIREI-246:
состояние окон читается и записывается один раз за прогон, `risk_limits()` и часовой пояс —
тоже один раз; после сбоя посреди прогона повторно обработанные события учитываются в окне один раз.
"""

from __future__ import annotations

from contextlib import ExitStack
from datetime import timezone
import hashlib
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

from xray_vps_manager.activity import blocklist as activity_blocklist
from xray_vps_manager.activity import raw_logs
from xray_vps_manager.activity import repository as activity_repository
from xray_vps_manager.activity import settings as activity_settings
from xray_vps_manager.activity import sync as activity_sync
from xray_vps_manager.core import time as core_time
from xray_vps_manager.db import database
from xray_vps_manager.db.repositories import activity as sqlite_activity
from xray_vps_manager.db.repositories import clients as sqlite_clients
from xray_vps_manager.db.repositories import connections as sqlite_connections
from xray_vps_manager.db.repositories import settings as sqlite_settings

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "activity_sync" / "access-alert-windows.log"
WINDOW_KEY = sqlite_activity.ACTIVITY_ALERT_WINDOW_STATE_KEY
# Три события за 5 минут, три хоста и три порта за день: каждое правило срабатывает ровно один раз.
RISK_LIMITS = {"burstEvents": 3, "burstWindowMinutes": 5, "uniqueHosts": 3, "uniquePorts": 3}
KNOWN_CLIENTS = {
    "alice|created=2026-06-12T07:01:00Z": {
        "client": "alice",
        "email": "alice|created=2026-06-12T07:01:00Z",
        "connection": "vless-reality",
    },
    "bob|created=2026-06-12T07:02:00Z": {
        "client": "bob",
        "email": "bob|created=2026-06-12T07:02:00Z",
        "connection": "vless-reality",
    },
}


def unique_hash(kind: str, value: str) -> str:
    return hashlib.sha256(f"{kind}:{value.lower()}".encode("utf-8")).hexdigest()


# Состояние окон после полного успешного прогона фикстуры (часовой пояс менеджера — UTC).
EXPECTED_WINDOW_STATE = {
    "clients": {
        "alice": {
            "lastAlerts": {
                "burst": "2026-06-12T08:00:00+00:00",
                "unique-hosts": "2026-06-12",
                "unique-ports": "2026-06-12",
            },
            "eventTimes": [
                "2026-06-12T08:00:00Z",
                "2026-06-12T08:01:00Z",
                "2026-06-12T08:02:00Z",
                "2026-06-12T08:03:00Z",
                "2026-06-12T08:04:00Z",
            ],
            "uniqueDay": "2026-06-12",
            "hosts": sorted(unique_hash("host", host) for host in ("one.example", "two.example", "three.example")),
            "ports": sorted(unique_hash("port", port) for port in ("443", "8443", "8080")),
        },
        "bob": {
            "lastAlerts": {},
            "eventTimes": ["2026-06-12T08:05:00Z"],
            "uniqueDay": "2026-06-12",
            "hosts": [unique_hash("host", "bob.example")],
            "ports": [unique_hash("port", "443")],
        },
    }
}


class AlertWindowSyncCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.db_path = self.root / "manager.db"
        self.access_log = self.root / "access.log"
        shutil.copyfile(FIXTURE, self.access_log)
        self.make_sqlite_db()

    def make_sqlite_db(self) -> None:
        connection = database.open_database(self.db_path)
        try:
            sqlite_connections.upsert_connection(
                connection,
                "vless-reality",
                {
                    "tag": "vless-reality",
                    "name": "default",
                    "created": "2026-06-12T07:00:00Z",
                    "port": 443,
                    "sni": "example.com",
                    "dest": "example.com:443",
                    "fingerprint": "chrome",
                },
            )
            for index, name in enumerate(("alice", "bob"), start=1):
                sqlite_clients.upsert_client(
                    connection,
                    name,
                    {
                        "id": f"00000000-0000-0000-0000-00000000000{index}",
                        "created": f"2026-06-12T07:0{index}:00Z",
                        "enabled": True,
                        "connection": "vless-reality",
                        "client": {
                            "id": f"00000000-0000-0000-0000-00000000000{index}",
                            "email": f"{name}|created=2026-06-12T07:0{index}:00Z",
                        },
                    },
                )
            sqlite_settings.set_metadata(connection, "jsonImport.completed", "true")
        finally:
            connection.close()

    def sync_patches(self, stack: ExitStack) -> dict:
        """Подменяет только окружение сервера: пути, server.env и обслуживание вне окон."""
        mocks = {
            "risk_limits": mock.Mock(return_value=dict(RISK_LIMITS)),
            "sync_timezone": mock.Mock(return_value=(timezone.utc, "UTC")),
            "repository_timezone": mock.Mock(return_value=(timezone.utc, "UTC")),
        }
        patches = (
            mock.patch.object(database, "MANAGER_DB_PATH", self.db_path),
            mock.patch.object(activity_sync, "ACCESS_LOG_PATH", self.access_log),
            mock.patch.object(activity_sync, "known_clients", return_value=dict(KNOWN_CLIENTS)),
            mock.patch.object(activity_repository, "load_json", return_value={}),
            mock.patch.object(activity_repository, "ensure_dirs"),
            mock.patch.object(activity_settings, "activity_enabled", return_value=False),
            mock.patch.object(activity_settings, "alerts_enabled", return_value=True),
            mock.patch.object(activity_settings, "retention_days", return_value=365),
            mock.patch.object(activity_settings, "alert_retention_days", return_value=90),
            mock.patch.object(activity_settings, "xray_error_event_retention_days", return_value=180),
            mock.patch.object(activity_settings, "risk_limits", mocks["risk_limits"]),
            mock.patch.object(core_time, "manager_timezone", mocks["sync_timezone"]),
            mock.patch.object(core_time, "server_local_timezone", return_value=timezone.utc),
            mock.patch.object(activity_repository, "manager_timezone", mocks["repository_timezone"]),
            mock.patch.object(activity_repository, "prune_alerts_for_write", return_value=0),
            mock.patch.object(activity_repository, "prune_xray_errors_for_write", return_value=0),
            mock.patch.object(raw_logs, "sync_error_log", return_value=0),
            mock.patch.object(activity_blocklist, "reconcile_xray_config", return_value=None),
        )
        for patch in patches:
            stack.enter_context(patch)
        return mocks

    def run_sync(self, *, fail_on_event: int | None = None) -> tuple[int, list[str], dict]:
        messages: list[str] = []
        with ExitStack() as stack:
            mocks = self.sync_patches(stack)
            if fail_on_event is not None:
                original = sqlite_activity.upsert_client_counters
                calls = {"count": 0}

                def failing_upsert(connection, event, display_tz):
                    calls["count"] += 1
                    if calls["count"] == fail_on_event:
                        raise RuntimeError("disk I/O error")
                    return original(connection, event, display_tz)

                stack.enter_context(mock.patch.object(sqlite_activity, "upsert_client_counters", failing_upsert))
            result = activity_sync.sync_activity(messages.append)
        return result, messages, mocks

    def with_connection(self, reader):
        connection = database.open_database(self.db_path)
        try:
            return reader(connection)
        finally:
            connection.close()

    def read_window_state(self) -> dict:
        return self.with_connection(lambda connection: sqlite_activity.get_metadata_json(connection, WINDOW_KEY))

    def read_alerts(self) -> dict:
        rows = self.with_connection(lambda connection: sqlite_activity.list_alert_events(connection, limit=50))
        self.assertEqual(len({row["risk"] for row in rows}), len(rows), "дубли alert-строк по риску")
        return {row["risk"]: row for row in rows}

    def read_hour_counters(self, client: str) -> list[dict]:
        return self.with_connection(
            lambda connection: sqlite_activity.list_client_counters(
                connection, bucket_type="hour", client_name=client
            )
        )

    def read_offset(self) -> dict:
        return self.with_connection(sqlite_activity.get_access_log_state)


class AlertWindowCharacterizationTests(AlertWindowSyncCase):
    """Текущее поведение успешного прогона и прогона со сбоем; ожидания не меняются после VIREI-246."""

    def test_successful_sync_records_window_alerts_counters_and_state(self) -> None:
        result, messages, _mocks = self.run_sync()

        self.assertEqual(result, 0)
        self.assertIn(
            "Activity sync saved: 6 parsed, 0 detailed, 6 counter updates, 3 alerts, 0 skipped, "
            "0 detailed pruned, 0 alerts pruned, 0 errors pruned.",
            messages,
        )

        alerts = self.read_alerts()
        self.assertEqual(set(alerts), {"burst", "unique-hosts", "unique-ports"})
        burst = alerts["burst"]
        self.assertEqual(
            (burst["client"], burst["connection"], burst["time"], burst["severity"], burst["event_count"]),
            ("alice", "vless-reality", "2026-06-12T08:02:00Z", "warning", 3),
        )
        self.assertEqual((burst["windowMinutes"], burst["eventsInWindow"]), (5, 3))
        self.assertEqual((burst["host"], burst["port"], burst["outbound"]), ("", "", ""))
        hosts = alerts["unique-hosts"]
        self.assertEqual((hosts["client"], hosts["time"], hosts["event_count"]), ("alice", "2026-06-12T08:02:00Z", 3))
        self.assertEqual((hosts["uniqueHosts"], hosts["bucketStart"]), (3, "2026-06-12"))
        ports = alerts["unique-ports"]
        self.assertEqual((ports["client"], ports["time"], ports["event_count"]), ("alice", "2026-06-12T08:03:00Z", 3))
        self.assertEqual((ports["uniquePorts"], ports["bucketStart"]), (3, "2026-06-12"))

        self.assertEqual(self.read_window_state(), EXPECTED_WINDOW_STATE)

        alice_counters = self.read_hour_counters("alice")
        self.assertEqual([(row["bucketStart"], row["totalEvents"]) for row in alice_counters], [("2026-06-12T08:00:00+00:00", 5)])
        self.assertEqual((alice_counters[0]["uniqueHosts"], alice_counters[0]["uniquePorts"]), (3, 3))
        self.assertEqual([(row["bucketStart"], row["totalEvents"]) for row in self.read_hour_counters("bob")], [("2026-06-12T08:00:00+00:00", 1)])

        offset = self.read_offset()
        self.assertEqual((offset["inode"], offset["offset"]), (self.access_log.stat().st_ino, self.access_log.stat().st_size))

    def test_failed_run_keeps_committed_events_and_rerun_reprocesses_them(self) -> None:
        """Текущее поведение, XR-REV-006 (VIREI-167): события до сбоя остаются записанными, offset не
        продвигается, повторный прогон читает их снова. Alert-строки не задваиваются благодаря
        dedupe-ключу `add_alert_event`, а счётчики клиента учитывают перечитанные события второй раз."""
        result, messages, _mocks = self.run_sync(fail_on_event=4)

        self.assertEqual(result, 1)
        self.assertTrue(any("offset was not advanced" in message and "disk I/O error" in message for message in messages))
        self.assertEqual(self.read_offset(), {})
        self.assertEqual(set(self.read_alerts()), {"burst", "unique-hosts"})
        self.assertEqual([row["totalEvents"] for row in self.read_hour_counters("alice")], [3])

        result, _messages, _mocks = self.run_sync()

        self.assertEqual(result, 0)
        self.assertEqual(set(self.read_alerts()), {"burst", "unique-hosts", "unique-ports"})
        # Прежнее поведение: три события до сбоя посчитаны дважды (3 + 5).
        self.assertEqual([row["totalEvents"] for row in self.read_hour_counters("alice")], [8])
        self.assertEqual([row["totalEvents"] for row in self.read_hour_counters("bob")], [1])
        self.assertEqual(self.read_offset()["offset"], self.access_log.stat().st_size)


class AlertWindowOncePerRunTests(AlertWindowSyncCase):
    """VIREI-246: окна, лимиты и часовой пояс — один раз за прогон."""

    def test_window_state_is_read_and_written_once_per_run(self) -> None:
        reads: list[str] = []
        writes: list[str] = []
        original_get = sqlite_activity.get_metadata_json
        original_set = sqlite_activity.set_metadata_json

        def counting_get(connection, key):
            reads.append(key)
            return original_get(connection, key)

        def counting_set(connection, key, value):
            writes.append(key)
            return original_set(connection, key, value)

        with mock.patch.object(sqlite_activity, "get_metadata_json", counting_get), \
                mock.patch.object(sqlite_activity, "set_metadata_json", counting_set):
            result, _messages, _mocks = self.run_sync()

        self.assertEqual(result, 0)
        self.assertEqual(reads.count(WINDOW_KEY), 1)
        self.assertEqual(writes.count(WINDOW_KEY), 1)
        self.assertEqual(self.read_window_state(), EXPECTED_WINDOW_STATE)

    def test_risk_limits_and_manager_timezone_are_read_once_per_run(self) -> None:
        result, _messages, mocks = self.run_sync()

        self.assertEqual(result, 0)
        self.assertEqual(mocks["risk_limits"].call_count, 1)
        self.assertEqual(mocks["sync_timezone"].call_count, 1)
        self.assertEqual(mocks["repository_timezone"].call_count, 0)

    def test_window_state_is_not_saved_when_run_fails_and_rerun_counts_events_once(self) -> None:
        """Изменённое ожидание VIREI-246: при сбое посреди прогона состояние окон не сохраняется
        (как и offset), поэтому повторный прогон учитывает перечитанные события в окне один раз,
        а не дважды. Alert-строки по-прежнему не задваиваются: `add_alert_event` суммирует
        `event_count` в строке с тем же dedupe-ключом."""
        result, _messages, _mocks = self.run_sync(fail_on_event=4)

        self.assertEqual(result, 1)
        self.assertEqual(self.read_window_state(), {})

        result, _messages, _mocks = self.run_sync()

        self.assertEqual(result, 0)
        self.assertEqual(self.read_window_state(), EXPECTED_WINDOW_STATE)
        alerts = self.read_alerts()
        self.assertEqual(
            {risk: row["event_count"] for risk, row in alerts.items()},
            {"burst": 6, "unique-hosts": 6, "unique-ports": 3},
        )


if __name__ == "__main__":
    unittest.main()
