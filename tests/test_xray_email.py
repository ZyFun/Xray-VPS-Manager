"""R3.2: один владелец формата email, без новой валидации."""

import unittest

from xray_vps_manager.clients import models
from xray_vps_manager.xray import email


class XrayEmailTests(unittest.TestCase):
    def test_models_reexport_canonical_functions(self):
        for name in ("email_metadata", "split_email", "email_for_client", "client_name"):
            self.assertIs(getattr(models, name), getattr(email, name))

    def test_metadata_preserves_permissive_parsing(self):
        cases = (
            (None, ("", {})),
            (123, ("123", {})),
            (" alice |created=first|ignored|=empty|created=last|x=a=b|connection=",
             (" alice ", {"created": "last", "x": "a=b", "connection": ""})),
        )
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(email.email_metadata(value), expected)

    def test_split_and_client_name_share_the_same_format(self):
        value = "alice|created=2026-11-08T12:00:00Z|connection=trojan-tls"
        self.assertEqual(email.split_email(value), ("alice", "2026-11-08T12:00:00Z"))
        self.assertEqual(email.client_name({"email": value}), "alice")
        self.assertEqual(email.client_name({}), "")

    def test_generation_preserves_field_order_and_omits_empty_values(self):
        self.assertEqual(email.email_for_client("alice"), "alice")
        self.assertEqual(email.email_for_client("alice", connection_tag="vless-reality"),
                         "alice|connection=vless-reality")
        self.assertEqual(email.email_for_client("alice", "stamp", connection_tag="trojan-tls"),
                         "alice|created=stamp|connection=trojan-tls")
        self.assertEqual(email.email_for_client(123, "a|b"), "123|created=a|b")
