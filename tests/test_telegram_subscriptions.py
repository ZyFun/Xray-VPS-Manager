import unittest

from xray_vps_manager.telegram import subscriptions


class TelegramSubscriptionTests(unittest.TestCase):
    def test_unsubscribe_chat_mentions_bot_subscription(self) -> None:
        db = {"clientSubscriptions": {"222": {"client": "alice"}}}

        text = subscriptions.unsubscribe_chat(db, "222")

        self.assertEqual(text, "Подписка на бота отключена.")
        self.assertNotIn("222", db["clientSubscriptions"])

    def test_unsubscribe_chat_reports_missing_subscription(self) -> None:
        db = {"clientSubscriptions": {}}

        text = subscriptions.unsubscribe_chat(db, "222")

        self.assertEqual(text, "Активной подписки нет.")

    def test_set_activity_notifications_updates_subscription_without_client_name_in_status(self) -> None:
        db = {
            "clientSubscriptionState": {
                "activityNotificationExceptions": {
                    "222": [
                        {
                            "host": "video.example.ru",
                            "port": "443",
                            "regions": "RU",
                            "clientId": "00000000-0000-0000-0000-000000000001",
                        }
                    ]
                }
            },
            "clientSubscriptions": {
                "222": {
                    "client": "internal_alice",
                    "clientId": "00000000-0000-0000-0000-000000000001",
                    "enabled": True,
                }
            }
        }
        client_db = {
            "clients": {
                "internal_alice": {"id": "00000000-0000-0000-0000-000000000001"},
            }
        }

        self.assertTrue(subscriptions.set_activity_notifications(db, "222", True, "2026-06-12T08:00:00Z"))
        text = subscriptions.activity_notification_status_for_chat(db, "222", client_db)

        self.assertTrue(db["clientSubscriptions"]["222"]["activityNotificationsEnabled"])
        self.assertIn("Клиентская рассылка: включена.", text)
        self.assertIn("Личных исключений: 1.", text)
        self.assertIn("Бот не видит и не сохраняет содержимое", text)
        self.assertNotIn("internal_alice", text)

    def test_remove_activity_exception_for_chat_deletes_selected_item(self) -> None:
        db = {
            "clientSubscriptionState": {
                "activityNotificationExceptions": {
                    "222": [
                        {"host": "first.example.ru", "port": "443", "regions": "RU", "clientId": "client-id"},
                        {"host": "second.example.ru", "port": "8443", "regions": "RU", "clientId": "client-id"},
                    ]
                }
            }
        }

        removed = subscriptions.remove_activity_exception_for_chat(db, "222", 0)

        self.assertEqual(removed["host"], "first.example.ru")
        remaining = subscriptions.activity_exceptions_for_chat(db, "222")
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["host"], "second.example.ru")


if __name__ == "__main__":
    unittest.main()
