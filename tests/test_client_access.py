from datetime import datetime, timezone
import unittest
from zoneinfo import ZoneInfo

from xray_vps_manager.clients import access
from xray_vps_manager.clients.status import expire_due_clients, reconcile_client_access_status


def client_item(name: str) -> dict:
    return {
        "id": "22222222-2222-2222-2222-222222222222",
        "email": f"{name}|created=2026-01-01T00:00:00Z",
        "flow": "xtls-rprx-vision",
        "level": 0,
    }


def reality_config(items: list[dict]) -> dict:
    return {
        "inbounds": [
            {
                "tag": "vless-reality",
                "protocol": "vless",
                "settings": {"clients": items},
                "streamSettings": {"security": "reality"},
            }
        ]
    }


class ClientAccessTests(unittest.TestCase):
    def test_parse_access_days_accepts_unlimited_aliases_and_positive_days(self) -> None:
        self.assertIsNone(access.parse_access_days(""))
        self.assertIsNone(access.parse_access_days("0"))
        self.assertIsNone(access.parse_access_days("бессрочно"))
        self.assertEqual(access.parse_access_days("30"), 30)

    def test_parse_access_days_rejects_invalid_values(self) -> None:
        with self.assertRaisesRegex(ValueError, "positive number"):
            access.parse_access_days("1.5")
        with self.assertRaisesRegex(ValueError, "too large"):
            access.parse_access_days("36501")

    def test_parse_extend_days_requires_positive_number(self) -> None:
        self.assertEqual(access.parse_extend_days("+5"), 5)
        with self.assertRaisesRegex(ValueError, "positive number"):
            access.parse_extend_days("0")

    def test_access_expired_compares_in_current_timezone(self) -> None:
        entry = {"expiresAt": "2026-06-12T00:00:00+00:00"}

        self.assertFalse(access.access_expired(entry, datetime(2026, 6, 11, 23, 59, tzinfo=timezone.utc)))
        self.assertTrue(access.access_expired(entry, datetime(2026, 6, 12, 0, 0, tzinfo=timezone.utc)))

    def test_expire_due_clients_disables_and_removes_active_client(self) -> None:
        item = client_item("alice")
        config = reality_config([dict(item)])
        db = {
            "clients": {
                "alice": {
                    "id": item["id"],
                    "created": "2026-01-01T00:00:00Z",
                    "enabled": True,
                    "connection": "vless-reality",
                    "client": dict(item),
                    "expiresAt": "2026-06-12T00:00:00+00:00",
                }
            }
        }

        result = expire_due_clients(
            config,
            db,
            now=datetime(2026, 6, 12, 1, tzinfo=timezone.utc),
            stamp="2026-06-12T01:00:00Z",
        )

        self.assertEqual(result.due_names, ["alice"])
        self.assertEqual(config["inbounds"][0]["settings"]["clients"], [])
        self.assertFalse(db["clients"]["alice"]["enabled"])
        self.assertEqual(db["clients"]["alice"]["disabledReason"], "expired")
        self.assertEqual(db["clients"]["alice"]["expiredAt"], "2026-06-12T01:00:00Z")

    def test_traffic_limit_takes_priority_over_expired_access(self) -> None:
        item = client_item("alice")
        config = reality_config([dict(item)])
        db = {
            "clients": {
                "alice": {
                    "id": item["id"],
                    "created": "2026-01-01T00:00:00Z",
                    "enabled": True,
                    "connection": "vless-reality",
                    "client": dict(item),
                    "expiresAt": "2026-06-12T00:00:00+00:00",
                    "trafficLimit": {"period": "daily", "bytes": 100},
                }
            }
        }
        traffic_db = {
            "clients": {
                "alice": {
                    "history": {"2026-06-12": {"10": {"incoming": 100, "outgoing": 0}}}
                }
            }
        }

        entry, changed, status, traffic_status = reconcile_client_access_status(
            config,
            db,
            traffic_db,
            "alice",
            db["clients"]["alice"],
            now=datetime(2026, 6, 12, 12, tzinfo=timezone.utc),
        )

        self.assertTrue(changed)
        self.assertEqual(status, "disabled-traffic-limit")
        self.assertEqual(entry["disabledReason"], "traffic-limit")
        self.assertNotIn("expiredAt", entry)
        self.assertTrue(traffic_status["exceeded"])


class AccessDeadlineTimezoneChangeTests(unittest.TestCase):
    """XR-REV-057: смена MANAGER_TIMEZONE сохраняет календарную дату срока и приводит её к 00:00 новой зоны."""

    def test_east_to_west_keeps_calendar_date(self) -> None:
        self.assertEqual(
            access.access_deadline_at_midnight("2026-10-05T00:00:00+10:00", ZoneInfo("Europe/Moscow")),
            "2026-10-05T00:00+03:00",
        )
        self.assertEqual(
            access.access_deadline_at_midnight("2026-10-05T00:00:00+03:00", ZoneInfo("Europe/Kaliningrad")),
            "2026-10-05T00:00+02:00",
        )

    def test_west_to_east_keeps_calendar_date(self) -> None:
        self.assertEqual(
            access.access_deadline_at_midnight("2026-10-05T00:00:00+03:00", ZoneInfo("Asia/Vladivostok")),
            "2026-10-05T00:00+10:00",
        )

    def test_offset_of_written_value_wins_over_new_zone_dst(self) -> None:
        # Срок, записанный при фиксированном летнем смещении (пустой MANAGER_TIMEZONE, XR-REV-065), на зимнюю дату.
        self.assertEqual(
            access.access_deadline_at_midnight("2027-01-10T00:00:00+02:00", ZoneInfo("Europe/Berlin")),
            "2027-01-10T00:00+01:00",
        )

    def test_naive_and_utc_values_keep_date_as_written(self) -> None:
        moscow = ZoneInfo("Europe/Moscow")

        self.assertEqual(access.access_deadline_at_midnight("2026-10-05T00:00:00", moscow), "2026-10-05T00:00+03:00")
        self.assertEqual(access.access_deadline_at_midnight("2026-10-04T21:00:00Z", moscow), "2026-10-04T00:00+03:00")

    def test_empty_or_invalid_value_stays_empty(self) -> None:
        self.assertEqual(access.access_deadline_at_midnight("", ZoneInfo("Europe/Moscow")), "")
        self.assertEqual(access.access_deadline_at_midnight("not-a-date", ZoneInfo("Europe/Moscow")), "")

    def test_future_deadline_is_not_expired_after_east_to_west_normalization(self) -> None:
        moscow = ZoneInfo("Europe/Moscow")
        now = datetime(2026, 10, 4, 12, 0, tzinfo=moscow)
        entry = {"expiresAt": "2026-10-05T00:00:00+10:00"}
        self.assertFalse(access.access_expired(entry, now))

        entry["expiresAt"] = access.access_deadline_at_midnight(entry["expiresAt"], moscow)

        self.assertFalse(access.access_expired(entry, now))

    def test_change_warnings_are_empty_for_standard_deadline(self) -> None:
        warnings = access.access_deadline_change_warnings(
            "alice",
            "2026-10-05T00:00:00+10:00",
            "2026-10-05T00:00+03:00",
            ZoneInfo("Asia/Vladivostok"),
            datetime(2026, 10, 4, 12, 0, tzinfo=ZoneInfo("Europe/Moscow")),
        )

        self.assertEqual(warnings, [])

    def test_change_warning_when_date_shown_before_differs(self) -> None:
        moscow = ZoneInfo("Europe/Moscow")
        value = "2026-10-04T21:00:00Z"
        normalized = access.access_deadline_at_midnight(value, ZoneInfo("Europe/Kaliningrad"))

        warnings = access.access_deadline_change_warnings(
            "bob",
            value,
            normalized,
            moscow,
            datetime(2026, 10, 1, 12, 0, tzinfo=moscow),
        )

        self.assertEqual(warnings, ["client bob: access date changed 2026-10-05 -> 2026-10-04"])

    def test_change_warning_when_future_deadline_has_passed_in_new_zone(self) -> None:
        moscow = ZoneInfo("Europe/Moscow")
        vladivostok = ZoneInfo("Asia/Vladivostok")
        value = "2026-10-17T00:00:00+03:00"
        normalized = access.access_deadline_at_midnight(value, vladivostok)
        now = datetime(2026, 10, 16, 22, 0, tzinfo=moscow).astimezone(vladivostok)

        warnings = access.access_deadline_change_warnings("carol", value, normalized, moscow, now)

        self.assertEqual(
            warnings,
            ["client carol: access until 2026-10-17 00:00 has already passed in the new timezone, access is expired"],
        )

    def test_change_warnings_skip_already_expired_deadline(self) -> None:
        moscow = ZoneInfo("Europe/Moscow")
        vladivostok = ZoneInfo("Asia/Vladivostok")
        value = "2026-10-10T00:00:00+03:00"

        warnings = access.access_deadline_change_warnings(
            "dave",
            value,
            access.access_deadline_at_midnight(value, vladivostok),
            moscow,
            datetime(2026, 10, 16, 22, 0, tzinfo=moscow),
        )

        self.assertEqual(warnings, [])

    def test_change_warning_when_expired_client_deadline_becomes_future(self) -> None:
        # 75-R01: срок истёк в старой зоне, а после смены зоны на запад ещё не наступил — клиент остаётся отключённым.
        vladivostok = ZoneInfo("Asia/Vladivostok")
        value = "2026-10-05T00:00:00+10:00"
        normalized = access.access_deadline_at_midnight(value, ZoneInfo("Europe/Moscow"))
        now = datetime(2026, 10, 5, 1, 0, tzinfo=vladivostok)

        warnings = access.access_deadline_change_warnings(
            "alice", value, normalized, vladivostok, now, disabled_reason="expired"
        )

        self.assertEqual(
            warnings,
            [
                "client alice: disabled as expired, but access until 2026-10-05 00:00 has not passed yet "
                "in the new timezone; enable it: xray-client enable alice"
            ],
        )

    def test_no_enable_hint_for_client_disabled_for_other_reason(self) -> None:
        vladivostok = ZoneInfo("Asia/Vladivostok")
        value = "2026-10-05T00:00:00+10:00"
        normalized = access.access_deadline_at_midnight(value, ZoneInfo("Europe/Moscow"))
        now = datetime(2026, 10, 5, 1, 0, tzinfo=vladivostok)

        for reason in ("", "traffic-limit"):
            with self.subTest(reason=reason):
                self.assertEqual(
                    access.access_deadline_change_warnings(
                        "alice", value, normalized, vladivostok, now, disabled_reason=reason
                    ),
                    [],
                )


if __name__ == "__main__":
    unittest.main()
