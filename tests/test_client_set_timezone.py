"""XR-REV-057: смена MANAGER_TIMEZONE сохраняет календарные даты сроков доступа клиентов."""

from contextlib import contextmanager, redirect_stdout
from datetime import datetime
from io import StringIO
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from zoneinfo import ZoneInfo

from tests.support.sandbox import Sandbox, ProcessResult
from xray_vps_manager.core import clock

from xray_vps_manager.clients import access as client_access
from xray_vps_manager.clients import repository as client_repository
from xray_vps_manager.clients import settings as client_settings
from xray_vps_manager.commands import client as client_command
from xray_vps_manager.core import paths as core_paths
from xray_vps_manager.core.server_env import read_server_env
from xray_vps_manager.db import database
from xray_vps_manager.db.repositories import settings as sqlite_settings

MOSCOW = ZoneInfo("Europe/Moscow")
VLADIVOSTOK = ZoneInfo("Asia/Vladivostok")


def client_entry(index: int, name: str, expires_at: str) -> dict:
    uuid = f"{index:08d}-2222-2222-2222-222222222222"
    entry = {
        "id": uuid,
        "created": "2026-01-01T00:00:00Z",
        "enabled": True,
        "client": {"id": uuid, "email": f"{name}|created=2026-01-01T00:00:00Z", "flow": "xtls-rprx-vision", "level": 0},
    }
    if expires_at:
        entry["expiresAt"] = expires_at
    return entry


def expired_client_entry(index: int, name: str, expires_at: str) -> dict:
    entry = client_entry(index, name, expires_at)
    entry.update(
        {
            "enabled": False,
            "disabledAt": "2026-10-04T14:01:00Z",
            "expiredAt": "2026-10-04T14:01:00Z",
            "disabledReason": "expired",
        }
    )
    return entry


class SetTimezoneAccessDeadlinesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = Sandbox(db=None).__enter__()
        self.addCleanup(self.server.__exit__, None, None, None)
        self.root = self.server.root
        self.db_path = self.server.paths.manager_db
        self.env_path = self.server.paths.server_env
        connection = database.open_database(self.db_path)
        try:
            sqlite_settings.set_metadata(connection, "jsonImport.completed", "true")
        finally:
            connection.close()
        self.server.processes.script({
            ("systemctl", "daemon-reload"): ProcessResult(),
            ("systemctl", "enable", "--now", "xray-raw-log-rotate.timer"): ProcessResult(),
            ("systemctl", "restart", "xray-raw-log-rotate.timer"): ProcessResult(),
        })

    def save_clients(self, deadlines: dict) -> None:
        clients = {
            name: client_entry(index, name, expires_at)
            for index, (name, expires_at) in enumerate(deadlines.items(), start=1)
        }
        client_repository.save_db({"clients": clients}, db_path=self.db_path)

    def deadlines(self) -> dict:
        clients = client_repository.load_db_sql(db_path=self.db_path)["clients"]
        return {name: entry.get("expiresAt", "") for name, entry in clients.items()}

    def configured_timezone_name(self) -> str:
        return client_settings.normalize_timezone(read_server_env(self.env_path).get("MANAGER_TIMEZONE", ""))

    @contextmanager
    def sandbox(self, timezone_name: str, now: datetime):
        self.env_path.write_text(f"MANAGER_TIMEZONE={timezone_name}\n")
        # local_now ещё использует legacy datetime до отдельного переноса часов.
        with clock.frozen(now), mock.patch.object(client_access, "local_now", return_value=now):
            yield

    def set_timezone(self, value: str) -> str:
        output = StringIO()
        with redirect_stdout(output):
            client_command.cmd_set_timezone(value)
        return output.getvalue()

    def test_normalize_east_to_west_keeps_calendar_day_of_every_client(self) -> None:
        self.save_clients(
            {
                "alice": "2026-10-05T00:00:00+10:00",
                "bob": "2026-10-20T00:00:00+10:00",
                "carol": "2027-01-01T00:00:00+10:00",
                "dave": "",
            }
        )
        now = datetime(2026, 10, 4, 12, 0, tzinfo=MOSCOW)

        with self.sandbox("Asia/Vladivostok", now):
            changed, warnings = client_command.normalize_access_deadlines(MOSCOW, VLADIVOSTOK, now=now)

        self.assertEqual(changed, 3)
        self.assertEqual(warnings, [])
        self.assertEqual(
            self.deadlines(),
            {
                "alice": "2026-10-05T00:00+03:00",
                "bob": "2026-10-20T00:00+03:00",
                "carol": "2027-01-01T00:00+03:00",
                "dave": "",
            },
        )
        clients = client_repository.load_db_sql(db_path=self.db_path)["clients"]
        self.assertEqual([name for name, entry in clients.items() if client_access.access_expired(entry, now)], [])

    def test_set_timezone_west_keeps_dates_and_warns_about_changed_list_date(self) -> None:
        self.save_clients(
            {
                "alice": "2026-10-05T00:00:00+10:00",
                # Нестандартное значение в UTC: `xray-client list` во Владивостоке показывал 20 октября.
                "bob": "2026-10-19T14:00:00Z",
            }
        )

        with self.sandbox("Asia/Vladivostok", datetime(2026, 10, 4, 12, 0, tzinfo=MOSCOW)):
            output = self.set_timezone("Europe/Moscow")

        self.assertEqual(read_server_env(self.env_path)["MANAGER_TIMEZONE"], "Europe/Moscow")
        self.assertEqual(
            self.deadlines(),
            {"alice": "2026-10-05T00:00+03:00", "bob": "2026-10-19T00:00+03:00"},
        )
        self.assertIn("Normalized access deadlines: 2\n", output)
        self.assertEqual(
            [line for line in output.splitlines() if line.startswith("WARN:")],
            ["WARN: client bob: access date changed 2026-10-20 -> 2026-10-19"],
        )

    def test_set_timezone_west_hints_enable_for_expired_client_with_future_deadline(self) -> None:
        # 75-R01: в 01:00 по Владивостоку срок alice истёк, expire-due её отключил; в Москве срок ещё не наступил.
        client_repository.save_db(
            {
                "clients": {
                    "alice": expired_client_entry(1, "alice", "2026-10-05T00:00:00+10:00"),
                    "bob": client_entry(2, "bob", "2026-10-20T00:00:00+10:00"),
                }
            },
            db_path=self.db_path,
        )

        with self.sandbox("Asia/Vladivostok", datetime(2026, 10, 5, 1, 0, tzinfo=VLADIVOSTOK)):
            output = self.set_timezone("Europe/Moscow")

        self.assertEqual(
            self.deadlines(),
            {"alice": "2026-10-05T00:00+03:00", "bob": "2026-10-20T00:00+03:00"},
        )
        alice = client_repository.load_db_sql(db_path=self.db_path)["clients"]["alice"]
        self.assertFalse(alice["enabled"])
        self.assertEqual(alice["disabledReason"], "expired")
        self.assertEqual(
            [line for line in output.splitlines() if line.startswith("WARN:")],
            [
                "WARN: client alice: disabled as expired, but access until 2026-10-05 00:00 has not passed yet "
                "in the new timezone; enable it: xray-client enable alice"
            ],
        )

    def test_set_timezone_repairs_invalid_previous_timezone(self) -> None:
        self.save_clients({"alice": "2026-10-05T00:00:00+10:00"})

        with self.sandbox("Europe/Moscow # основная зона", datetime(2026, 10, 4, 12, 0, tzinfo=MOSCOW)):
            output = self.set_timezone("Europe/Moscow")

        self.assertEqual(read_server_env(self.env_path)["MANAGER_TIMEZONE"], "Europe/Moscow")
        self.assertEqual(self.deadlines(), {"alice": "2026-10-05T00:00+03:00"})
        self.assertNotIn("WARN:", output)

    def test_set_timezone_east_warns_when_deadline_has_already_passed(self) -> None:
        self.save_clients(
            {
                "carol": "2026-10-17T00:00:00+03:00",
                "erin": "2026-10-25T00:00:00+03:00",
            }
        )
        now = datetime(2026, 10, 16, 22, 0, tzinfo=MOSCOW).astimezone(VLADIVOSTOK)

        with self.sandbox("Europe/Moscow", now):
            output = self.set_timezone("Asia/Vladivostok")

        self.assertEqual(
            self.deadlines(),
            {"carol": "2026-10-17T00:00+10:00", "erin": "2026-10-25T00:00+10:00"},
        )
        self.assertEqual(
            [line for line in output.splitlines() if line.startswith("WARN:")],
            [
                "WARN: client carol: access until 2026-10-17 00:00 has already passed in the new timezone, "
                "access is expired"
            ],
        )


if __name__ == "__main__":
    unittest.main()
