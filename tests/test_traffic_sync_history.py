"""xray-traffic-sync на реальной SQLite: golden-прогоны и запись истории дельтами (VIREI-247)."""

from contextlib import redirect_stdout
from datetime import datetime, timezone
from io import StringIO
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest import mock

from xray_vps_manager.commands import traffic_sync
from xray_vps_manager.db import database
from xray_vps_manager.db.repositories import clients as sqlite_clients
from xray_vps_manager.db.repositories import connections as sqlite_connections
from xray_vps_manager.db.repositories import settings as sqlite_settings
from xray_vps_manager.db.repositories import traffic as sqlite_traffic

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "traffic_sync"
MANAGER_TIMEZONE = "Europe/Moscow"

ALICE_VLESS = "alice|created=2025-12-01T10:00:00Z"
ALICE_TROJAN = "alice|created=2026-03-01T10:00:00Z|connection=trojan-tls"
BOB = "bob|created=2026-09-01T10:00:00Z"
CAROL = "carol|created=2025-01-01T10:00:00Z"
TROJAN_PASSWORD = "alice-trojan-password-0123456789abcdef"

# Прогоны заданы в UTC. В Europe/Moscow (UTC+3) это уже 2026-10-21, поэтому граница хранения
# 6 месяцев — 2026-04-21 по календарю менеджера, а не 2026-04-20 по календарю UTC.
RUN1 = datetime(2026, 10, 20, 22, 30, tzinfo=timezone.utc)
RUN2 = datetime(2026, 10, 20, 22, 45, tzinfo=timezone.utc)
RUN3 = datetime(2026, 10, 20, 23, 5, tzinfo=timezone.utc)
STAMP1 = "2026-10-20T22:30:00Z"
STAMP2 = "2026-10-20T22:45:00Z"
STAMP3 = "2026-10-20T23:05:00Z"

ALICE_SEEN = "2026-10-20T21:00:00Z"
TROJAN_SEEN = "2026-10-01T08:00:00Z"
BOB_SEEN = "2026-10-15T09:10:00Z"
CAROL_SEEN = "2025-03-01T10:00:00Z"

TOTALS_COLUMNS = (
    "client_name",
    "email",
    "incoming_bytes",
    "outgoing_bytes",
    "last_runtime_uplink",
    "last_runtime_downlink",
    "last_online_at",
    "last_online_source",
    "last_accepted_at",
    "updated_at",
)
CREDENTIAL_TOTALS_COLUMNS = ("client_name", "connection_tag") + TOTALS_COLUMNS[1:]
HISTORY_COLUMNS = ("client_name", "bucket_date", "bucket_hour", "incoming_bytes", "outgoing_bytes")
CREDENTIAL_HISTORY_COLUMNS = ("client_name", "connection_tag") + HISTORY_COLUMNS[1:]
TABLES = {
    "traffic_totals": (TOTALS_COLUMNS, "client_name"),
    "traffic_history": (HISTORY_COLUMNS, "client_name, bucket_date, bucket_hour"),
    "credential_traffic_totals": (CREDENTIAL_TOTALS_COLUMNS, "client_name, connection_tag"),
    "credential_traffic_history": (
        CREDENTIAL_HISTORY_COLUMNS,
        "client_name, connection_tag, bucket_date, bucket_hour",
    ),
}


def frozen_datetime(instant: datetime):
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return instant.astimezone().replace(tzinfo=None)
            return instant.astimezone(tz)

    return FrozenDatetime


def statsquery_output(stats: dict) -> str:
    items = []
    for email, (uplink, downlink) in stats.items():
        items.append({"name": f"user>>>{email}>>>traffic>>>uplink", "value": str(uplink)})
        items.append({"name": f"user>>>{email}>>>traffic>>>downlink", "value": str(downlink)})
    return json.dumps({"stat": items}, indent=4)


def bucket(incoming: int, outgoing: int) -> dict:
    return {"incoming": incoming, "outgoing": outgoing}


def traffic_entry(email: str, totals: tuple, last: tuple, seen: str, history=None) -> dict:
    return {
        "email": email,
        "incoming": totals[0],
        "outgoing": totals[1],
        "last": {"uplink": last[0], "downlink": last[1]},
        "lastOnline": seen,
        "lastOnlineSource": "traffic",
        "updated": seen,
        "history": history or {},
    }


def credential(connection_tag: str, uuid: str, email: str, created: str) -> dict:
    if connection_tag == "trojan-tls":
        return {
            "connection": connection_tag,
            "id": uuid,
            "protocol": "trojan",
            "created": created,
            "client": {"password": TROJAN_PASSWORD, "email": email},
        }
    return {
        "connection": connection_tag,
        "id": uuid,
        "protocol": "vless",
        "created": created,
        "client": {"id": uuid, "email": email},
    }


def client_record(uuid: str, created: str, credentials: dict) -> dict:
    return {
        "id": uuid,
        "created": created,
        "enabled": True,
        "connection": "vless-reality",
        "client": {"id": uuid, "email": credentials["vless-reality"]["client"]["email"]},
        "credentials": credentials,
    }


class TrafficSyncSandbox:
    """Временные manager.db, config.json и server.env для прогонов `traffic_sync.sync()`."""

    def __init__(self, test: unittest.TestCase) -> None:
        self.test = test
        tmp = tempfile.TemporaryDirectory()
        test.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.db_path = self.root / "manager.db"
        (self.root / "server.env").write_text(f'MANAGER_TIMEZONE="{MANAGER_TIMEZONE}"\n')
        self._patch(database, "MANAGER_DB_PATH", self.db_path)
        self._patch(traffic_sync, "CONFIG_PATH", self.root / "config.json")
        self._patch(traffic_sync, "SERVER_ENV_PATH", self.root / "server.env")
        self._patch(traffic_sync, "LOCK_PATH", self.root / "traffic.lock")
        self._patch(traffic_sync, "ACCESS_LOG_PATH", self.root / "access.log")

    def _patch(self, target, attribute: str, value) -> None:
        patcher = mock.patch.object(target, attribute, value)
        patcher.start()
        self.test.addCleanup(patcher.stop)

    def open(self):
        return database.open_database(self.db_path)

    def seed(self, clients: dict, config_inbounds: list, traffic: dict, credential_traffic: dict) -> None:
        (self.root / "config.json").write_text(json.dumps({"inbounds": config_inbounds}))
        connection = self.open()
        try:
            for tag in ("vless-reality", "trojan-tls"):
                sqlite_connections.upsert_connection(
                    connection,
                    tag,
                    {
                        "tag": tag,
                        "name": tag,
                        "created": "2025-01-01T00:00:00Z",
                        "port": 443 if tag == "vless-reality" else 8443,
                        "sni": "example.com",
                        "dest": "example.com:443",
                        "fingerprint": "chrome",
                    },
                )
            for name, entry in clients.items():
                sqlite_clients.upsert_client(connection, name, entry)
            for name, entry in traffic.items():
                sqlite_traffic.upsert_traffic_entry(connection, name, entry)
            for (name, connection_tag), entry in credential_traffic.items():
                sqlite_traffic.upsert_credential_traffic_entry(connection, name, connection_tag, entry)
            sqlite_settings.set_metadata(connection, "jsonImport.completed", "true")
        finally:
            connection.close()

    def run(self, instant: datetime, stats) -> str:
        """Прогон `traffic_sync.sync()`; `stats` — имя фикстуры `statsquery` или {email: (uplink, downlink)}."""
        if isinstance(stats, str):
            stdout = (FIXTURES / stats).read_text()
        else:
            stdout = statsquery_output(stats)
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")
        output = StringIO()
        with mock.patch.object(traffic_sync, "datetime", frozen_datetime(instant)), \
            mock.patch.object(traffic_sync, "run_capture", return_value=completed) as run_capture, \
            redirect_stdout(output):
            result = traffic_sync.sync()
        self.test.assertEqual(result, 0, output.getvalue())
        self.test.assertIn("statsquery", run_capture.call_args.args[0])
        return output.getvalue()

    def rows(self, table: str, *, with_rowid: bool = False) -> list:
        columns, order = TABLES[table]
        selected = ("rowid",) + columns if with_rowid else columns
        connection = database.open_database(self.db_path, initialize=False)
        try:
            return [
                tuple(row)
                for row in connection.execute(f"SELECT {', '.join(selected)} FROM {table} ORDER BY {order}")
            ]
        finally:
            connection.close()

    def state(self, *, with_rowid: bool = False) -> dict:
        return {table: self.rows(table, with_rowid=with_rowid) for table in TABLES}


def golden_clients() -> dict:
    return {
        "alice": client_record(
            "00000000-0000-0000-0000-000000000001",
            "2025-12-01T10:00:00Z",
            {
                "vless-reality": credential(
                    "vless-reality", "00000000-0000-0000-0000-000000000001", ALICE_VLESS, "2025-12-01T10:00:00Z"
                ),
                "trojan-tls": credential(
                    "trojan-tls", "00000000-0000-0000-0000-000000000011", ALICE_TROJAN, "2026-03-01T10:00:00Z"
                ),
            },
        ),
        "bob": client_record(
            "00000000-0000-0000-0000-000000000002",
            "2026-09-01T10:00:00Z",
            {
                "vless-reality": credential(
                    "vless-reality", "00000000-0000-0000-0000-000000000002", BOB, "2026-09-01T10:00:00Z"
                ),
            },
        ),
        "carol": client_record(
            "00000000-0000-0000-0000-000000000003",
            "2025-01-01T10:00:00Z",
            {
                "vless-reality": credential(
                    "vless-reality", "00000000-0000-0000-0000-000000000003", CAROL, "2025-01-01T10:00:00Z"
                ),
            },
        ),
    }


def golden_inbounds() -> list:
    return [
        {
            "tag": "vless-reality",
            "protocol": "vless",
            "settings": {
                "clients": [
                    {"id": "00000000-0000-0000-0000-000000000001", "email": ALICE_VLESS},
                    {"id": "00000000-0000-0000-0000-000000000002", "email": BOB},
                    {"id": "00000000-0000-0000-0000-000000000003", "email": CAROL},
                ],
                "decryption": "none",
            },
            "streamSettings": {"network": "tcp", "security": "reality"},
        },
        {
            "tag": "trojan-tls",
            "protocol": "trojan",
            "settings": {"clients": [{"password": TROJAN_PASSWORD, "email": ALICE_TROJAN}]},
            "streamSettings": {"network": "ws", "security": "none"},
        },
    ]


def golden_traffic() -> dict:
    return {
        "alice": traffic_entry(
            ALICE_TROJAN,
            (10_000, 20_000),
            (110, 220),
            ALICE_SEEN,
            {
                "2026-04-20": {"23": bucket(1000, 2000)},
                "2026-04-21": {"00": bucket(3000, 4000)},
                "2026-10-21": {"00": bucket(500, 600)},
            },
        ),
        # Создан в окне хранения, а история отстаёт от итогов на 900/1800: пропуск восстанавливается.
        "bob": traffic_entry(BOB, (1000, 2000), (40, 50), BOB_SEEN, {"2026-10-10": {"08": bucket(100, 200)}}),
        # Итоги без истории: клиент создан до окна хранения, восстанавливать нечего.
        "carol": traffic_entry(CAROL, (5000, 6000), (7, 8), CAROL_SEEN),
    }


def golden_credential_traffic() -> dict:
    return {
        ("alice", "vless-reality"): traffic_entry(
            ALICE_VLESS,
            (9000, 18_000),
            (100, 200),
            ALICE_SEEN,
            {
                "2026-04-20": {"23": bucket(1000, 2000)},
                "2026-04-21": {"00": bucket(2500, 3500)},
                "2026-10-21": {"00": bucket(500, 600)},
            },
        ),
        ("alice", "trojan-tls"): traffic_entry(
            ALICE_TROJAN,
            (1000, 2000),
            (10, 20),
            TROJAN_SEEN,
            {"2026-04-21": {"00": bucket(500, 500)}},
        ),
        ("bob", "vless-reality"): traffic_entry(
            BOB, (1000, 2000), (40, 50), BOB_SEEN, {"2026-10-10": {"08": bucket(100, 200)}}
        ),
        ("carol", "vless-reality"): traffic_entry(CAROL, (5000, 6000), (7, 8), CAROL_SEEN),
    }


def seed_golden(sandbox: TrafficSyncSandbox) -> None:
    sandbox.seed(golden_clients(), golden_inbounds(), golden_traffic(), golden_credential_traffic())


EXPECTED_AFTER_RUN1 = {
    "traffic_totals": [
        ("alice", ALICE_TROJAN, 10_050, 20_060, 160, 280, STAMP1, "traffic", None, STAMP1),
        ("bob", BOB, 1000, 2000, 40, 50, BOB_SEEN, "traffic", None, BOB_SEEN),
        ("carol", CAROL, 5000, 6000, 7, 8, CAROL_SEEN, "traffic", None, CAROL_SEEN),
    ],
    "traffic_history": [
        ("alice", "2026-04-21", 0, 3000, 4000),
        ("alice", "2026-10-21", 0, 500, 600),
        ("alice", "2026-10-21", 1, 50, 60),
        ("bob", "2026-10-10", 8, 100, 200),
        ("bob", "2026-10-15", 12, 900, 1800),
    ],
    "credential_traffic_totals": [
        ("alice", "trojan-tls", ALICE_TROJAN, 1000, 2000, 10, 20, TROJAN_SEEN, "traffic", None, TROJAN_SEEN),
        ("alice", "vless-reality", ALICE_VLESS, 9050, 18_060, 150, 260, STAMP1, "traffic", None, STAMP1),
        ("bob", "vless-reality", BOB, 1000, 2000, 40, 50, BOB_SEEN, "traffic", None, BOB_SEEN),
        ("carol", "vless-reality", CAROL, 5000, 6000, 7, 8, CAROL_SEEN, "traffic", None, CAROL_SEEN),
    ],
    "credential_traffic_history": [
        ("alice", "trojan-tls", "2026-04-21", 0, 500, 500),
        ("alice", "vless-reality", "2026-04-21", 0, 2500, 3500),
        ("alice", "vless-reality", "2026-10-21", 0, 500, 600),
        ("alice", "vless-reality", "2026-10-21", 1, 50, 60),
        ("bob", "vless-reality", "2026-10-10", 8, 100, 200),
    ],
}

EXPECTED_AFTER_RUN2 = {
    "traffic_totals": [
        ("alice", ALICE_TROJAN, 10_080, 20_100, 30, 40, STAMP2, "traffic", None, STAMP2),
        ("bob", BOB, 1005, 2030, 5, 30, STAMP2, "traffic", None, STAMP2),
        ("carol", CAROL, 5000, 6000, 0, 0, CAROL_SEEN, "traffic", None, CAROL_SEEN),
    ],
    "traffic_history": [
        ("alice", "2026-04-21", 0, 3000, 4000),
        ("alice", "2026-10-21", 0, 500, 600),
        ("alice", "2026-10-21", 1, 80, 100),
        ("bob", "2026-10-10", 8, 100, 200),
        ("bob", "2026-10-15", 12, 900, 1800),
        ("bob", "2026-10-21", 1, 5, 30),
    ],
    "credential_traffic_totals": [
        ("alice", "trojan-tls", ALICE_TROJAN, 1000, 2000, 0, 0, TROJAN_SEEN, "traffic", None, TROJAN_SEEN),
        ("alice", "vless-reality", ALICE_VLESS, 9080, 18_100, 30, 40, STAMP2, "traffic", None, STAMP2),
        ("bob", "vless-reality", BOB, 1005, 2030, 5, 30, STAMP2, "traffic", None, STAMP2),
        ("carol", "vless-reality", CAROL, 5000, 6000, 0, 0, CAROL_SEEN, "traffic", None, CAROL_SEEN),
    ],
    "credential_traffic_history": [
        ("alice", "trojan-tls", "2026-04-21", 0, 500, 500),
        ("alice", "vless-reality", "2026-04-21", 0, 2500, 3500),
        ("alice", "vless-reality", "2026-10-21", 0, 500, 600),
        ("alice", "vless-reality", "2026-10-21", 1, 80, 100),
        ("bob", "vless-reality", "2026-10-10", 8, 100, 200),
        ("bob", "vless-reality", "2026-10-21", 1, 5, 30),
    ],
}

EXPECTED_AFTER_RUN3 = {
    "traffic_totals": [
        ("alice", ALICE_TROJAN, 10_126, 20_157, 76, 97, STAMP3, "traffic", None, STAMP3),
        ("bob", BOB, 1005, 2030, 5, 30, STAMP2, "traffic", None, STAMP2),
        ("carol", CAROL, 5000, 6000, 0, 0, CAROL_SEEN, "traffic", None, CAROL_SEEN),
    ],
    "traffic_history": [
        ("alice", "2026-04-21", 0, 3000, 4000),
        ("alice", "2026-10-21", 0, 500, 600),
        ("alice", "2026-10-21", 1, 80, 100),
        ("alice", "2026-10-21", 2, 46, 57),
        ("bob", "2026-10-10", 8, 100, 200),
        ("bob", "2026-10-15", 12, 900, 1800),
        ("bob", "2026-10-21", 1, 5, 30),
    ],
    "credential_traffic_totals": [
        ("alice", "trojan-tls", ALICE_TROJAN, 1006, 2007, 6, 7, STAMP3, "traffic", None, STAMP3),
        ("alice", "vless-reality", ALICE_VLESS, 9120, 18_150, 70, 90, STAMP3, "traffic", None, STAMP3),
        ("bob", "vless-reality", BOB, 1005, 2030, 5, 30, STAMP2, "traffic", None, STAMP2),
        ("carol", "vless-reality", CAROL, 5000, 6000, 0, 0, CAROL_SEEN, "traffic", None, CAROL_SEEN),
    ],
    "credential_traffic_history": [
        ("alice", "trojan-tls", "2026-04-21", 0, 500, 500),
        ("alice", "trojan-tls", "2026-10-21", 2, 6, 7),
        ("alice", "vless-reality", "2026-04-21", 0, 2500, 3500),
        ("alice", "vless-reality", "2026-10-21", 0, 500, 600),
        ("alice", "vless-reality", "2026-10-21", 1, 80, 100),
        ("alice", "vless-reality", "2026-10-21", 2, 40, 50),
        ("bob", "vless-reality", "2026-10-10", 8, 100, 200),
        ("bob", "vless-reality", "2026-10-21", 1, 5, 30),
    ],
}


class TrafficSyncGoldenTests(unittest.TestCase):
    """Golden `xray-traffic-sync`: ожидания фиксируют поведение до VIREI-247 и после него не меняются.

    Три прогона по фикстурам `statsquery`: накопление через сброс счётчиков Xray, обрезка истории
    старше 6 месяцев по календарю часового пояса менеджера, восстановление пропусков
    `repair_retained_history_gaps` и клиент с ненулевыми итогами без истории (carol).
    """

    maxDiff = None

    def setUp(self) -> None:
        self.sandbox = TrafficSyncSandbox(self)
        seed_golden(self.sandbox)

    def test_three_runs_accumulate_totals_and_history(self) -> None:
        # Прогон 1 (01:30 по Москве): у alice/vless дельта 50/60, бакет 2026-04-20 обрезан,
        # bob получает 900/1800 в бакет своего последнего обновления (12:10 по Москве).
        output = self.sandbox.run(RUN1, "statsquery-run1.json")
        self.assertIn("Repaired retained traffic history gaps for clients: bob", output)
        self.assertEqual(self.sandbox.state(), EXPECTED_AFTER_RUN1)

        # Прогон 2 (01:45, после перезапуска Xray): счётчики меньше прежних и считаются заново
        # с нуля; дельта ложится в тот же часовой бакет, пропусков больше нет.
        output = self.sandbox.run(RUN2, "statsquery-run2.json")
        self.assertNotIn("Repaired", output)
        self.assertEqual(self.sandbox.state(), EXPECTED_AFTER_RUN2)

        # Прогон 3 (02:05): новый часовой бакет; у alice активны обе credentials.
        output = self.sandbox.run(RUN3, "statsquery-run3.json")
        self.assertNotIn("Repaired", output)
        self.assertEqual(self.sandbox.state(), EXPECTED_AFTER_RUN3)


RUN1_COUNTERS = {
    ALICE_VLESS: (150, 260),
    ALICE_TROJAN: (10, 20),
    BOB: (40, 50),
    CAROL: (7, 8),
}


def history_statements(statements: list, verb: str) -> list:
    tables = ("traffic_history", "credential_traffic_history")
    return [
        statement
        for statement in statements
        if statement.lstrip().upper().startswith(verb)
        and any(f" {table}" in statement or f"\n{table}" in statement for table in tables)
    ]


class TrafficSyncHistoryWriteTests(unittest.TestCase):
    """VIREI-247: прогон пишет в историю только изменившиеся бакеты, а не всю историю заново."""

    maxDiff = None

    def setUp(self) -> None:
        self.sandbox = TrafficSyncSandbox(self)
        seed_golden(self.sandbox)

    def traced_run(self, instant: datetime, stats) -> list:
        statements = []
        real_open = database.open_database

        def traced_open(*args, **kwargs):
            connection = real_open(*args, **kwargs)
            connection.set_trace_callback(statements.append)
            return connection

        with mock.patch.object(database, "open_database", side_effect=traced_open):
            self.sandbox.run(instant, stats)
        return statements

    def test_one_active_client_changes_only_its_current_buckets(self) -> None:
        # Первый прогон обрезает старые бакеты и восстанавливает пропуск bob: дальше этого нет.
        self.sandbox.run(RUN1, "statsquery-run1.json")
        before = self.sandbox.state(with_rowid=True)

        counters = dict(RUN1_COUNTERS)
        counters[ALICE_VLESS] = (175, 290)
        statements = self.traced_run(RUN2, counters)

        after = self.sandbox.state(with_rowid=True)
        expected = {
            "traffic_history": [
                row[:4] + (75, 90) if row[1:4] == ("alice", "2026-10-21", 1) else row
                for row in before["traffic_history"]
            ],
            "credential_traffic_history": [
                row[:5] + (75, 90) if row[1:5] == ("alice", "vless-reality", "2026-10-21", 1) else row
                for row in before["credential_traffic_history"]
            ],
        }
        self.assertNotEqual(expected["traffic_history"], before["traffic_history"])
        self.assertEqual(after["traffic_history"], expected["traffic_history"])
        self.assertEqual(after["credential_traffic_history"], expected["credential_traffic_history"])
        # Удаления в истории — только обрезка по границе хранения, без удаления истории клиента.
        for statement in history_statements(statements, "DELETE"):
            self.assertIn("bucket_date <", statement)
            self.assertNotIn("client_name", statement)
        self.assertEqual(len(history_statements(statements, "INSERT")), 2, statements)

    def test_failed_write_keeps_previous_state_and_next_run_counts_delta_once(self) -> None:
        before = self.sandbox.state(with_rowid=True)
        connection = self.sandbox.open()
        try:
            connection.execute(
                """
                CREATE TRIGGER fail_bob_credential_totals
                BEFORE UPDATE ON credential_traffic_totals
                WHEN NEW.client_name = 'bob'
                BEGIN
                    SELECT RAISE(ABORT, 'simulated write failure');
                END
                """
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaisesRegex(sqlite3.IntegrityError, "simulated write failure"):
            self.sandbox.run(RUN1, "statsquery-run1.json")

        self.assertEqual(self.sandbox.state(with_rowid=True), before)

        connection = self.sandbox.open()
        try:
            connection.execute("DROP TRIGGER fail_bob_credential_totals")
            connection.commit()
        finally:
            connection.close()
        self.sandbox.run(RUN1, "statsquery-run1.json")
        self.assertEqual(self.sandbox.state(), EXPECTED_AFTER_RUN1)

    def test_concurrent_commit_inside_run_does_not_fail_write(self) -> None:
        """247-R01: транзакция прогона захватывает право записи до первого чтения.

        В WAL отложенная транзакция, начатая чтением, не становится пишущей после чужого commit:
        `database is locked` сразу, без ожидания `busy_timeout`. Telegram poller пишет `manager.db`
        параллельно с `xray-traffic-sync`, в том числе в `ExecStop`.
        """
        real_client_names = sqlite_traffic.traffic_client_names
        concurrent_writes = []

        def client_names_with_concurrent_commit(connection):
            names = real_client_names(connection)
            if not concurrent_writes:
                other = sqlite3.connect(str(self.sandbox.db_path), timeout=0)
                try:
                    other.execute(
                        "INSERT INTO manager_metadata(key, value) VALUES ('test.concurrentWriter', '1') "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
                    )
                    other.commit()
                    concurrent_writes.append("committed")
                except sqlite3.OperationalError as exc:
                    concurrent_writes.append(f"blocked: {exc}")
                finally:
                    other.close()
            return names

        with mock.patch.object(sqlite_traffic, "traffic_client_names", side_effect=client_names_with_concurrent_commit):
            self.sandbox.run(RUN1, "statsquery-run1.json")

        self.assertEqual(self.sandbox.state(), EXPECTED_AFTER_RUN1)
        # С `timeout=0` конкурент получает отказ, пока прогон держит запись; poller ждал бы busy_timeout.
        self.assertEqual(len(concurrent_writes), 1)
        self.assertTrue(concurrent_writes[0].startswith("blocked"), concurrent_writes)

    def test_prune_removes_idle_history_older_than_retention(self) -> None:
        """Решение пользователя 2026-10-20: одна граница хранения для всех клиентов и credentials.

        До VIREI-247 у простаивающего клиента с ненулевыми итогами, вся история которого старше
        6 месяцев, строки оставались из-за защиты `sync_history` (история в памяти после обрезки пуста).
        """
        dave_email = "dave|created=2025-02-01T10:00:00Z"
        connection = self.sandbox.open()
        try:
            sqlite_clients.upsert_client(
                connection,
                "dave",
                client_record(
                    "00000000-0000-0000-0000-000000000004",
                    "2025-02-01T10:00:00Z",
                    {
                        "vless-reality": credential(
                            "vless-reality",
                            "00000000-0000-0000-0000-000000000004",
                            dave_email,
                            "2025-02-01T10:00:00Z",
                        ),
                    },
                ),
            )
            old_history = {"2026-01-10": {"10": bucket(300, 400)}, "2026-04-20": {"23": bucket(1, 2)}}
            sqlite_traffic.upsert_traffic_entry(
                connection, "dave", traffic_entry(dave_email, (301, 402), (3, 4), "2026-04-20T20:00:00Z", old_history)
            )
            sqlite_traffic.upsert_credential_traffic_entry(
                connection,
                "dave",
                "vless-reality",
                traffic_entry(dave_email, (301, 402), (3, 4), "2026-04-20T20:00:00Z", old_history),
            )
        finally:
            connection.close()
        counters = dict(RUN1_COUNTERS)
        counters[dave_email] = (3, 4)

        self.sandbox.run(RUN1, counters)

        self.assertEqual([row for row in self.sandbox.rows("traffic_history") if row[0] == "dave"], [])
        self.assertEqual([row for row in self.sandbox.rows("credential_traffic_history") if row[0] == "dave"], [])
        totals = {row[0]: row[2:4] for row in self.sandbox.rows("traffic_totals")}
        self.assertEqual(totals["dave"], (301, 402))
        # У остальных клиентов обрезка та же, что в golden-прогоне.
        self.assertEqual(
            [row for row in self.sandbox.rows("traffic_history") if row[0] != "dave"],
            EXPECTED_AFTER_RUN1["traffic_history"],
        )

    def test_new_entries_are_written_whole_once(self) -> None:
        # erin — запись до учёта по credentials: итоги и история есть только у клиента, и первая
        # credential копирует их целиком вместе с базой счётчиков (copy_entry_state).
        erin_email = "erin|created=2026-05-01T10:00:00Z"
        frank_email = "frank|created=2026-10-20T20:00:00Z"
        connection = self.sandbox.open()
        try:
            for name, uuid, created, email in (
                ("erin", "00000000-0000-0000-0000-000000000005", "2026-05-01T10:00:00Z", erin_email),
                ("frank", "00000000-0000-0000-0000-000000000006", "2026-10-20T20:00:00Z", frank_email),
            ):
                sqlite_clients.upsert_client(
                    connection,
                    name,
                    client_record(uuid, created, {"vless-reality": credential("vless-reality", uuid, email, created)}),
                )
            sqlite_traffic.upsert_traffic_entry(
                connection,
                "erin",
                traffic_entry(
                    erin_email,
                    (700, 800),
                    (100, 200),
                    "2026-10-20T21:00:00Z",
                    {"2026-10-01": {"09": bucket(300, 300)}, "2026-10-21": {"00": bucket(400, 500)}},
                ),
            )
        finally:
            connection.close()
        counters = dict(RUN1_COUNTERS)
        counters[erin_email] = (150, 260)
        counters[frank_email] = (1000, 2000)

        self.sandbox.run(RUN1, counters)

        erin_history = [
            ("erin", "2026-10-01", 9, 300, 300),
            ("erin", "2026-10-21", 0, 400, 500),
            ("erin", "2026-10-21", 1, 50, 60),
        ]
        self.assertEqual([row for row in self.sandbox.rows("traffic_history") if row[0] == "erin"], erin_history)
        self.assertEqual(
            [row[:1] + row[2:] for row in self.sandbox.rows("credential_traffic_history") if row[0] == "erin"],
            erin_history,
        )
        credential_totals = {row[:2]: row[3:7] for row in self.sandbox.rows("credential_traffic_totals")}
        self.assertEqual(credential_totals[("erin", "vless-reality")], (750, 860, 150, 260))
        # Новый клиент без базы счётчиков: первый прогон только запоминает счётчики Xray.
        self.assertEqual(credential_totals[("frank", "vless-reality")], (0, 0, 1000, 2000))
        self.assertEqual([row for row in self.sandbox.rows("traffic_history") if row[0] == "frank"], [])

        counters[frank_email] = (1010, 2030)
        self.sandbox.run(RUN2, counters)

        self.assertEqual(
            [row for row in self.sandbox.rows("traffic_history") if row[0] == "frank"],
            [("frank", "2026-10-21", 1, 10, 30)],
        )
        self.assertEqual(
            [row for row in self.sandbox.rows("credential_traffic_history") if row[0] == "frank"],
            [("frank", "vless-reality", "2026-10-21", 1, 10, 30)],
        )
        self.assertEqual([row for row in self.sandbox.rows("traffic_history") if row[0] == "erin"], erin_history)


if __name__ == "__main__":
    unittest.main()
