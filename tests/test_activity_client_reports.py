from contextlib import redirect_stdout
from datetime import date
from io import StringIO
import unittest
from unittest import mock

from xray_vps_manager.activity import client_reports
from xray_vps_manager.commands import activity as activity_command


class ActivityClientReportsTests(unittest.TestCase):
    def test_client_report_groups_period_events_by_connection(self) -> None:
        events = [
            {
                "time": "2026-06-12T08:00:00Z",
                "connection": "vless-reality",
                "host": "example.com",
                "port": "443",
                "outbound": "cascade-upstream",
            },
            {
                "time": "2026-06-12T09:00:00Z",
                "connection": "trojan-tls",
                "host": "youtube.com",
                "port": "443",
                "outbound": "direct",
                "risks": ["xray-geoip:RU"],
            },
            {
                "time": "2026-06-13T10:00:00Z",
                "connection": "trojan-tls",
                "host": "youtube.com",
                "port": "443",
                "outbound": "direct",
            },
        ]

        with mock.patch.object(client_reports.activity_time, "date_range_from_days", return_value=(date(2026, 6, 12), date(2026, 6, 13))), \
            mock.patch.object(client_reports.activity_exceptions, "exception_items_for_read", return_value=[]), \
            mock.patch.object(client_reports, "known_credential_connections", return_value=["trojan-tls", "vless-reality"]), \
            mock.patch.object(client_reports, "iter_events", return_value=events):
            report = client_reports.client_report("alice", "2")

        self.assertEqual([row[1] for row in report["rows"]], [2, 1])
        self.assertEqual(
            report["credentialRows"],
            [
                ["trojan-tls", 2, 1, "443(2)", "direct(2)", "xray-geoip:RU(1)", "-", "-", "youtube.com(2)"],
                ["vless-reality", 1, 1, "443(1)", "cascade-upstream(1)", "-", "-", "-", "example.com(1)"],
                ["TOTAL", 3, "-", "-", "-", "-", "-", "-", "-"],
            ],
        )

    def test_client_report_includes_zero_rows_for_known_credentials_without_events(self) -> None:
        events = [
            {
                "time": "2026-06-12T09:00:00Z",
                "connection": "trojan-tls",
                "host": "youtube.com",
                "port": "443",
                "outbound": "direct",
            },
        ]

        with mock.patch.object(client_reports.activity_time, "date_range_from_days", return_value=(date(2026, 6, 12), date(2026, 6, 12))), \
            mock.patch.object(client_reports.activity_exceptions, "exception_items_for_read", return_value=[]), \
            mock.patch.object(client_reports, "known_credential_connections", return_value=["trojan-tls", "vless-reality"]), \
            mock.patch.object(client_reports, "iter_events", return_value=events):
            report = client_reports.client_report("alice", "1")

        self.assertEqual(
            report["credentialRows"],
            [
                ["trojan-tls", 1, 1, "443(1)", "direct(1)", "-", "-", "-", "youtube.com(1)"],
                ["vless-reality", 0, 0, "-", "-", "-", "-", "-", "-"],
                ["TOTAL", 1, "-", "-", "-", "-", "-", "-", "-"],
            ],
        )

    def test_suspicious_report_uses_alert_log_events_when_detailed_log_is_empty(self) -> None:
        alerts = [
            {
                "time": "2026-06-12T08:00:00Z",
                "client": "alice",
                "host": "example.ru",
                "port": "443",
                "risk": "xray-geoip:RU",
                "risks": ["xray-geoip:RU"],
                "event_count": 3,
            },
            {
                "time": "2026-06-12T08:05:00Z",
                "client": "alice",
                "risk": "burst",
                "risks": ["burst"],
                "event_count": 10,
            },
        ]

        with mock.patch.object(client_reports.activity_time, "date_range_from_days", return_value=(date(2026, 6, 12), date(2026, 6, 12))), \
            mock.patch.object(client_reports.activity_exceptions, "exception_items_for_read", return_value=[]), \
            mock.patch.object(client_reports.activity_repository, "alert_events_for_read", return_value=alerts):
            report = client_reports.suspicious_report("1")

        self.assertEqual(report["rows"][0][0], "alice")
        self.assertEqual(report["rows"][0][2], 13)
        self.assertIn("burst(10)", report["rows"][0][1])
        self.assertIn("xray-geoip:RU(3)", report["rows"][0][1])
        self.assertIn("detailed activity", report["rows"][0][6])

    def test_geoip_risk_details_uses_alert_log_when_detailed_log_is_empty(self) -> None:
        alerts = [
            {
                "time": "2026-06-12T08:00:00Z",
                "last_seen_at": "2026-06-12T08:05:00Z",
                "client": "alice",
                "host": "example.ru",
                "port": "443",
                "outbound": "geoip-warning-RU",
                "risk": "xray-geoip:RU",
                "risks": ["xray-geoip:RU"],
                "event_count": 3,
            }
        ]

        with (
            mock.patch.object(
                client_reports.activity_time,
                "date_range_from_days",
                return_value=(date(2026, 6, 12), date(2026, 6, 12)),
            ),
            mock.patch.object(client_reports.activity_exceptions, "exception_items_for_read", return_value=[]),
            mock.patch.object(client_reports.activity_repository, "alert_events_for_read", return_value=alerts),
            mock.patch.object(client_reports, "known_clients_for_reports") as known_clients_for_reports,
            mock.patch.object(client_reports, "iter_events") as iter_events,
        ):
            report = client_reports.geoip_risk_details("1")

        self.assertEqual(report["clients"][0]["name"], "alice")
        self.assertEqual(report["clients"][0]["rows"][0][2], "example.ru")
        self.assertEqual(report["clients"][0]["rows"][0][4], "RU")
        known_clients_for_reports.assert_not_called()
        iter_events.assert_not_called()

    def test_counter_growth_rows_compare_latest_bucket_with_previous_average(self) -> None:
        rows = [
            {
                "client": "alice",
                "bucketStart": "2026-06-10",
                "totalEvents": 10,
                "uniqueHosts": 4,
                "uniquePorts": 2,
            },
            {
                "client": "alice",
                "bucketStart": "2026-06-11",
                "totalEvents": 30,
                "uniqueHosts": 9,
                "uniquePorts": 5,
            },
            {
                "client": "bob",
                "bucketStart": "2026-06-10",
                "totalEvents": 100,
                "uniqueHosts": 20,
                "uniquePorts": 8,
            },
            {
                "client": "bob",
                "bucketStart": "2026-06-11",
                "totalEvents": 90,
                "uniqueHosts": 18,
                "uniquePorts": 7,
            },
        ]

        growth = client_reports.counter_growth_rows(rows, limit=10)

        self.assertEqual([row["client"] for row in growth], ["alice"])
        self.assertEqual(growth[0]["bucketStart"], "2026-06-11")
        self.assertEqual(growth[0]["baselineBuckets"], 1)
        self.assertEqual(growth[0]["totalEventsDelta"], 20)
        self.assertEqual(growth[0]["uniqueHostsDelta"], 5)
        self.assertEqual(growth[0]["uniquePortsDelta"], 3)

    def test_report_client_prints_credential_table_for_multi_credential_report(self) -> None:
        output = StringIO()

        with mock.patch.object(
            activity_command.activity_client_reports,
            "client_report",
            return_value={
                "name": "alice",
                "start": date(2026, 6, 12),
                "end": date(2026, 6, 13),
                "rows": [["2026-06-12", 2, 2, "443(2)", "direct(1)", "-", "-", "-", "example.com(1)"]],
                "credentialRows": [
                    ["trojan-tls", 1, 1, "443(1)", "direct(1)", "-", "-", "-", "youtube.com(1)"],
                    ["vless-reality", 1, 1, "443(1)", "cascade-upstream(1)", "-", "-", "-", "example.com(1)"],
                    ["TOTAL", 2, "-", "-", "-", "-", "-", "-", "-"],
                ],
                "totalEvents": 2,
            },
        ), redirect_stdout(output):
            activity_command.report_client("alice", "2")

        text = output.getvalue()
        self.assertIn("Credentials", text)
        self.assertIn("trojan-tls", text)
        self.assertIn("vless-reality", text)


if __name__ == "__main__":
    unittest.main()
