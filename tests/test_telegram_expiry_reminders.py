from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import unittest

from xray_vps_manager.telegram import messages, notifications

MANAGER_TZ = ZoneInfo("Europe/Moscow")
CHAT_ID = "1001"
CLIENT_NAME = "alice"
CLIENT_UUID = "11111111-2222-3333-4444-555555555555"


def local(year, month, day, hour=0, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=MANAGER_TZ)


def parse_time(value):
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    return datetime.fromisoformat(raw)


class ExpiryReminderHarness:
    """Прогоны notify_expiry с явным now, зоной менеджера и подменённой отправкой — без сети Telegram."""

    def __init__(self, expires_at):
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.sent = []
        self.saved_sections = []
        self.db = {
            "enabled": True,
            "token": "test-token",
            "paymentTotalAmount": "1000",
            "paymentCurrency": "₽",
            "clientSubscriptions": {
                CHAT_ID: {"client": CLIENT_NAME, "clientId": CLIENT_UUID, "enabled": True},
            },
            "clientSubscriptionState": {"expiryReminders": {}},
        }
        self.entry = {"id": CLIENT_UUID, "paymentType": "paid", "enabled": True}
        self.set_expires_at(expires_at)
        self.client_db = {"clients": {CLIENT_NAME: self.entry}}
        self.ctx = notifications.NotificationContext(
            load_db=lambda: self.db,
            save_db_sections=lambda _db, sections: self.saved_sections.append(tuple(sections)),
            load_client_db=lambda: self.client_db,
            load_traffic_db=lambda: {"clients": {}},
            display_timezone=lambda: (MANAGER_TZ, "Europe/Moscow"),
            format_event_time=lambda value: value,
            format_access_until=lambda value: value,
            parse_time=parse_time,
            utc_now=lambda: self.now,
            utc_stamp=lambda: self.now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            run_capture=lambda *_args, **_kwargs: None,
            send_chat_message=self.send_chat_message,
            send_message=lambda *_args, **_kwargs: None,
            bot_name=lambda _db=None: "Vireika",
        )

    def set_expires_at(self, expires_at):
        self.entry["expiresAt"] = expires_at.isoformat(timespec="seconds")

    def send_chat_message(self, _db, chat_id, message, *_args, **_kwargs):
        self.sent.append((chat_id, message))

    def run(self, now):
        self.now = now.astimezone(timezone.utc)
        before = len(self.sent)
        rc = notifications.notify_expiry(self.ctx, quiet=True)
        if rc != 0:
            raise AssertionError(f"notify_expiry returned {rc}")
        return [message for _chat_id, message in self.sent[before:]]


class ExpiryReminderCalendarRemainderTests(unittest.TestCase):
    """XR-REV-013: порог по календарному остатку в зоне менеджера, не более одного сообщения за прогон."""

    def test_three_day_access_does_not_mention_five_days(self) -> None:
        harness = ExpiryReminderHarness(local(2026, 10, 6))

        sent = harness.run(local(2026, 10, 3, 12, 0))

        self.assertEqual(len(sent), 1)
        self.assertIn("Через 3 дня заканчивается текущий период.", sent[0])
        self.assertNotIn("5 дней", sent[0])

    def test_one_day_access_sends_single_message_per_run(self) -> None:
        harness = ExpiryReminderHarness(local(2026, 10, 4))

        sent = harness.run(local(2026, 10, 3, 12, 0))

        self.assertEqual(len(sent), 1)
        self.assertIn("Через 1 день заканчивается текущий период.", sent[0])
        self.assertNotIn("5 дней", sent[0])
        self.assertEqual(harness.run(local(2026, 10, 3, 12, 1)), [])

    def test_d_minus_five_sends_exactly_one_and_repeat_run_sends_nothing(self) -> None:
        harness = ExpiryReminderHarness(local(2026, 10, 20))

        self.assertEqual(harness.run(local(2026, 10, 14, 12, 0)), [])
        self.assertEqual(harness.run(local(2026, 10, 15, 7, 59)), [])
        sent = harness.run(local(2026, 10, 15, 8, 0))
        self.assertEqual(len(sent), 1)
        self.assertIn("Через 5 дней заканчивается текущий период.", sent[0])
        self.assertIn("Доступ до: 2026-10-20 00:00 Europe/Moscow", sent[0])
        self.assertEqual(harness.run(local(2026, 10, 15, 8, 1)), [])
        self.assertEqual(harness.run(local(2026, 10, 15, 23, 59)), [])
        self.assertEqual(harness.run(local(2026, 10, 16, 8, 0)), [])
        self.assertEqual(harness.run(local(2026, 10, 18, 12, 0)), [])

    def test_d_minus_one_sends_exactly_one_and_repeat_run_sends_nothing(self) -> None:
        harness = ExpiryReminderHarness(local(2026, 10, 20))
        self.assertEqual(len(harness.run(local(2026, 10, 15, 8, 0))), 1)

        self.assertEqual(harness.run(local(2026, 10, 19, 7, 59)), [])
        sent = harness.run(local(2026, 10, 19, 8, 0))
        self.assertEqual(len(sent), 1)
        self.assertIn("Через 1 день заканчивается текущий период.", sent[0])
        self.assertEqual(harness.run(local(2026, 10, 19, 8, 1)), [])
        self.assertEqual(harness.run(local(2026, 10, 19, 23, 59)), [])
        self.assertEqual(harness.run(local(2026, 10, 20, 0, 0)), [])

    def test_extension_by_two_days_does_not_mention_five_days(self) -> None:
        harness = ExpiryReminderHarness(local(2026, 10, 6))
        first = harness.run(local(2026, 10, 5, 8, 30))
        self.assertEqual(len(first), 1)
        self.assertIn("Через 1 день", first[0])

        harness.set_expires_at(local(2026, 10, 8))
        sent = harness.run(local(2026, 10, 5, 12, 0))

        self.assertEqual(len(sent), 1)
        self.assertIn("Через 3 дня заканчивается текущий период.", sent[0])
        self.assertNotIn("5 дней", sent[0])
        self.assertEqual(harness.run(local(2026, 10, 5, 12, 1)), [])

    def test_catch_up_after_downtime_within_band_uses_real_remainder(self) -> None:
        harness = ExpiryReminderHarness(local(2026, 10, 20))

        sent = harness.run(local(2026, 10, 16, 9, 0))

        self.assertEqual(len(sent), 1)
        self.assertIn("Через 4 дня заканчивается текущий период.", sent[0])
        self.assertNotIn("5 дней", sent[0])

    def test_catch_up_after_downtime_sends_only_current_threshold(self) -> None:
        harness = ExpiryReminderHarness(local(2026, 10, 20))

        sent = harness.run(local(2026, 10, 19, 10, 0))

        self.assertEqual(len(sent), 1)
        self.assertIn("Через 1 день заканчивается текущий период.", sent[0])
        self.assertEqual(harness.run(local(2026, 10, 19, 10, 1)), [])

    def test_missed_reminder_hour_is_sent_on_next_run_same_day(self) -> None:
        harness = ExpiryReminderHarness(local(2026, 10, 20))

        self.assertEqual(harness.run(local(2026, 10, 15, 7, 0)), [])
        sent = harness.run(local(2026, 10, 15, 11, 30))

        self.assertEqual(len(sent), 1)
        self.assertIn("Через 5 дней", sent[0])

    def test_short_access_waits_for_reminder_hour(self) -> None:
        harness = ExpiryReminderHarness(local(2026, 10, 6))

        self.assertEqual(harness.run(local(2026, 10, 3, 3, 0)), [])
        sent = harness.run(local(2026, 10, 3, 8, 0))

        self.assertEqual(len(sent), 1)
        self.assertIn("Через 3 дня", sent[0])

    def test_expired_access_sends_nothing(self) -> None:
        harness = ExpiryReminderHarness(local(2026, 10, 6))

        self.assertEqual(harness.run(local(2026, 10, 6, 9, 0)), [])

    def test_reminder_keys_saved_through_subscription_state_section(self) -> None:
        harness = ExpiryReminderHarness(local(2026, 10, 6))

        harness.run(local(2026, 10, 3, 12, 0))

        self.assertEqual(harness.saved_sections, [("clientSubscriptionState",)])
        keys = list(harness.db["clientSubscriptionState"]["expiryReminders"])
        self.assertEqual(
            keys,
            [f"{CHAT_ID}|{CLIENT_NAME}|{CLIENT_UUID}|2026-10-06T00:00:00+03:00|5d"],
        )


class ExpiryReminderDayWordTests(unittest.TestCase):
    def build(self, days_left):
        return messages.build_expiry_reminder_message(
            {},
            {},
            days_left,
            local(2026, 10, 20),
            "Europe/Moscow",
            lambda _db=None: "Vireika",
            lambda _db=None: "500 ₽",
        )

    def test_reminder_text_declines_days(self) -> None:
        expected = {
            1: "1 день",
            2: "2 дня",
            4: "4 дня",
            5: "5 дней",
            11: "11 дней",
            12: "12 дней",
            14: "14 дней",
            21: "21 день",
            22: "22 дня",
            25: "25 дней",
            111: "111 дней",
        }
        for days_left, label in expected.items():
            with self.subTest(days_left=days_left):
                self.assertIn(f"Через {label} заканчивается текущий период.", self.build(days_left))


if __name__ == "__main__":
    unittest.main()
