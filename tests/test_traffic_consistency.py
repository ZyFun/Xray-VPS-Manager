from datetime import date, datetime, timezone
import unittest

from xray_vps_manager.traffic import consistency


class TrafficConsistencyTests(unittest.TestCase):
    def test_retained_history_gap_detects_missing_history_for_recent_client(self) -> None:
        traffic_entry = {
            "incoming": 100,
            "outgoing": 200,
            "history": {"2026-06-12": {"08": {"incoming": 30, "outgoing": 40}}},
        }
        client_entry = {"created": "2026-06-01T00:00:00Z"}

        gap = consistency.retained_history_gap("alice", traffic_entry, client_entry, date(2026, 1, 1))

        self.assertIsNotNone(gap)
        self.assertEqual(gap.missing_incoming, 70)
        self.assertEqual(gap.missing_outgoing, 160)

    def test_retained_history_gap_ignores_clients_before_retention_cutoff(self) -> None:
        traffic_entry = {"incoming": 100, "outgoing": 200, "history": {}}
        client_entry = {"created": "2025-01-01T00:00:00Z"}

        gap = consistency.retained_history_gap("alice", traffic_entry, client_entry, date(2026, 1, 1))

        self.assertIsNone(gap)

    def test_repair_retained_history_gaps_adds_missing_delta_once(self) -> None:
        traffic_db = {
            "clients": {
                "alice": {
                    "incoming": 100,
                    "outgoing": 200,
                    "updated": "2026-06-12T05:30:00Z",
                    "history": {"2026-06-12": {"08": {"incoming": 30, "outgoing": 40}}},
                }
            }
        }
        clients = {"alice": {"created": "2026-06-01T00:00:00Z"}}
        bucket_time = datetime(2026, 6, 12, 9, 0, tzinfo=timezone.utc)

        repaired = consistency.repair_retained_history_gaps(traffic_db, clients, date(2026, 1, 1), bucket_time)
        repaired_again = consistency.repair_retained_history_gaps(traffic_db, clients, date(2026, 1, 1), bucket_time)

        self.assertEqual([gap.name for gap in repaired], ["alice"])
        self.assertEqual(repaired_again, [])
        entry = traffic_db["clients"]["alice"]
        self.assertEqual(consistency.history_totals(entry), (100, 200))
        self.assertEqual(entry["history"]["2026-06-12"]["05"], {"incoming": 70, "outgoing": 160})


if __name__ == "__main__":
    unittest.main()
