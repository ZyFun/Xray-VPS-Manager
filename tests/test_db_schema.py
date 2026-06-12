import sqlite3
import unittest

from xray_vps_manager.db import schema


class SQLiteSchemaTests(unittest.TestCase):
    def open_db(self) -> sqlite3.Connection:
        connection = sqlite3.connect(":memory:")
        schema.ensure_schema(connection)
        return connection

    def test_schema_initializes_expected_tables_indexes_and_version(self) -> None:
        with self.open_db() as connection:
            self.assertEqual(schema.schema_version(connection), schema.CURRENT_SCHEMA_VERSION)
            self.assertEqual(
                {
                    "schema_migrations",
                    "manager_metadata",
                    "reality_connections",
                    "clients",
                    "client_traffic_limits",
                    "client_traffic_limit_state",
                    "traffic_totals",
                    "traffic_history",
                    "file_offsets",
                    "activity_events",
                    "activity_event_risks",
                    "activity_exceptions",
                    "telegram_settings",
                    "telegram_subscriptions",
                    "telegram_state",
                    "payment_settings",
                },
                schema.table_names(connection),
            )
            self.assertTrue(
                {
                    "idx_clients_connection",
                    "idx_traffic_history_client_date",
                    "idx_activity_events_client_time",
                    "idx_activity_event_risks_risk",
                    "idx_telegram_subscriptions_uuid",
                }.issubset(schema.index_names(connection))
            )

    def test_schema_initialization_is_idempotent(self) -> None:
        with self.open_db() as connection:
            schema.ensure_schema(connection)

            rows = connection.execute("SELECT version, name FROM schema_migrations").fetchall()
            self.assertEqual(rows, [(1, "initial_manager_schema")])

    def test_schema_rejects_newer_database_versions(self) -> None:
        connection = sqlite3.connect(":memory:")
        connection.execute(f"PRAGMA user_version = {schema.CURRENT_SCHEMA_VERSION + 1}")

        with self.assertRaisesRegex(RuntimeError, "newer than supported"):
            schema.ensure_schema(connection)

    def test_client_related_rows_are_removed_with_client(self) -> None:
        with self.open_db() as connection:
            connection.execute(
                """
                INSERT INTO reality_connections(tag, name, port, sni, dest, fingerprint)
                VALUES ('vless-reality', 'default', 443, 'example.com', 'example.com:443', 'chrome')
                """
            )
            connection.execute(
                """
                INSERT INTO clients(name, uuid, connection_tag)
                VALUES ('alice', '00000000-0000-0000-0000-000000000001', 'vless-reality')
                """
            )
            connection.execute(
                """
                INSERT INTO client_traffic_limits(client_name, period, limit_bytes)
                VALUES ('alice', 'daily', 1073741824)
                """
            )
            connection.execute(
                """
                INSERT INTO traffic_totals(client_name, incoming_bytes, outgoing_bytes)
                VALUES ('alice', 100, 200)
                """
            )
            connection.execute(
                """
                INSERT INTO traffic_history(client_name, bucket_date, bucket_hour, incoming_bytes, outgoing_bytes)
                VALUES ('alice', '2026-06-12', 8, 100, 200)
                """
            )
            connection.execute(
                """
                INSERT INTO activity_events(id, event_time, client_name, host, port)
                VALUES (1, '2026-06-12T08:00:00Z', 'alice', 'example.com', 443)
                """
            )
            connection.execute(
                "INSERT INTO activity_event_risks(event_id, risk) VALUES (1, 'xray-geoip:RU')"
            )

            connection.execute("DELETE FROM clients WHERE name = 'alice'")

            for table in (
                "client_traffic_limits",
                "traffic_totals",
                "traffic_history",
                "activity_events",
                "activity_event_risks",
            ):
                count = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                self.assertEqual(count, 0, table)

    def test_constraints_guard_common_state_values(self) -> None:
        with self.open_db() as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT INTO reality_connections(tag, name, port, sni, dest, fingerprint)
                    VALUES ('bad-port', 'bad', 70000, 'example.com', 'example.com:443', 'chrome')
                    """
                )

            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT INTO clients(name, uuid, payment_type)
                    VALUES ('bob', '00000000-0000-0000-0000-000000000002', 'unknown')
                    """
                )


if __name__ == "__main__":
    unittest.main()
