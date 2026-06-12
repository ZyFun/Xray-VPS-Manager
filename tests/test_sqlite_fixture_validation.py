from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import os
import tempfile
import unittest
from unittest import mock

from fixture_json_state import write_json_state_fixture
from xray_vps_manager.commands import sqlite as sqlite_command
from xray_vps_manager.db import json_import


class SQLiteFixtureValidationTests(unittest.TestCase):
    def test_preflight_validates_fixture_state_without_touching_manager_db(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            paths = write_json_state_fixture(root / "state")
            manager_db_path = root / "manager.db"
            xray_test = root / "xray-test"
            xray_test.write_text("#!/bin/sh\n")
            stdout = StringIO()

            with mock.patch.object(sqlite_command, "MANAGER_DB_PATH", manager_db_path), mock.patch.object(
                sqlite_command, "XRAY_TEST", xray_test
            ), mock.patch.object(sqlite_command.os, "geteuid", return_value=0), mock.patch.object(
                json_import, "JsonStatePaths", return_value=paths
            ), redirect_stdout(stdout):
                code = sqlite_command.preflight()

            output = stdout.getvalue()
            self.assertEqual(code, 0)
            self.assertFalse(manager_db_path.exists())
            self.assertIn("clients: 1", output)
            self.assertIn("traffic: 1", output)
            self.assertIn("activity_events: 1", output)
            self.assertIn("telegram_subscriptions: 1", output)
            self.assertIn("OK SQLite preflight passed.", output)

    def test_validate_cutover_accepts_imported_fixture_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            paths = write_json_state_fixture(root / "state")
            db_path = root / "manager.db"
            json_import.import_json_files(paths, db_path=db_path)
            stdout = StringIO()

            with mock.patch.object(sqlite_command, "MANAGER_DB_PATH", db_path), mock.patch.dict(
                os.environ,
                {"XRAY_MANAGER_SQLITE_READS": "1", "XRAY_MANAGER_SQLITE_WRITES": "1"},
                clear=True,
            ), redirect_stdout(stdout):
                code = sqlite_command.validate_cutover()

            output = stdout.getvalue()
            self.assertEqual(code, 0)
            self.assertIn("clients: 1", output)
            self.assertIn("traffic: 1", output)
            self.assertIn("activity_events: 1", output)
            self.assertIn("telegram_subscriptions: 1", output)
            self.assertIn("clients_source: sqlite", output)
            self.assertIn("traffic_source: sqlite", output)
            self.assertIn("telegram_source: sqlite", output)
            self.assertIn("activity_source: sqlite", output)
            self.assertIn("OK SQLite cutover validation passed.", output)


if __name__ == "__main__":
    unittest.main()
