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


if __name__ == "__main__":
    unittest.main()
