"""Разбор вывода `xray x25519` и источник `pbk` в Reality-ссылках (XR-REV-051)."""

import re
import subprocess
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from xray_vps_manager.clients import connections as client_connections
from xray_vps_manager.clients import links as client_links
from xray_vps_manager.xray import crypto


ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = ROOT / "install.sh"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "xray_x25519"

PRIVATE_KEY = "yJ3Qm3n1N2cAvLw0f1l3rX8m7Qf3bK9ZtQ3lQ5m1jXk"
PUBLIC_KEY = "Z0dXv7l8m2a3P4bQ5rS6tU7vW8xY9zA0bC1dE2fG3hI"
HASH32 = "H9yK3nQ0pL2mR4sT6uV8wX1zA3bC5dE7fG9hJ1kL3mN"
STORED_PUBLIC_KEY = "S7oR3dPuBl1cK3y0fTh3C0nn3ct10nR3c0rdAbCdEfG"
CLIENT_ID = "00000000-0000-0000-0000-000000000001"

# Форматы Xray-core `main/commands/all/curve25519.go` (репро P4_051):
# A — до 2025-08-28, B — 7951a5c4 (2025-08-28 … 2026-03-19), C — 35800e95 (latest v26.3.27).
FORMATS = {
    "A private/public key": "a_private_public_key.txt",
    "B password/hash32": "b_password_hash32.txt",
    "C password (publickey)/hash32": "c_password_publickey_hash32.txt",
}

INSTALL_FUNCTION_RE = re.compile(r"^x25519_key_from_output\(\) \{\n.*?^\}\n", re.MULTILINE | re.DOTALL)


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class X25519OutputParseTests(unittest.TestCase):
    def test_parser_reads_all_known_formats(self) -> None:
        for label, name in FORMATS.items():
            with self.subTest(label):
                self.assertEqual(crypto.parse_x25519_output(fixture(name)), (PRIVATE_KEY, PUBLIC_KEY))

    def test_parser_ignores_hash32_regardless_of_line_order(self) -> None:
        output = f"Hash32: {HASH32}\nPassword: {PUBLIC_KEY}\nPrivateKey: {PRIVATE_KEY}\n"

        self.assertEqual(crypto.parse_x25519_output(output), (PRIVATE_KEY, PUBLIC_KEY))

    def test_parser_does_not_take_hash32_as_public_key(self) -> None:
        output = f"PrivateKey: {PRIVATE_KEY}\nPassword: {PUBLIC_KEY[:-1]}\nHash32: {HASH32}\n"

        self.assertEqual(crypto.parse_x25519_output(output), (PRIVATE_KEY, ""))

    def test_parser_rejects_values_that_are_not_43_char_base64url(self) -> None:
        cases = {
            "short": PUBLIC_KEY[:-1],
            "long": PUBLIC_KEY + "A",
            "standard base64 alphabet": PUBLIC_KEY[:-1] + "+",
            "empty": "",
        }
        for label, value in cases.items():
            with self.subTest(label):
                output = f"PrivateKey: {PRIVATE_KEY}\nPassword (PublicKey): {value}\n"
                self.assertEqual(crypto.parse_x25519_output(output), (PRIVATE_KEY, ""))

    def test_is_x25519_key(self) -> None:
        self.assertTrue(crypto.is_x25519_key(PUBLIC_KEY))
        self.assertFalse(crypto.is_x25519_key(""))
        self.assertFalse(crypto.is_x25519_key("public-key"))
        self.assertFalse(crypto.is_x25519_key(None))


class X25519CommandTests(unittest.TestCase):
    def test_key_pair_generation_reads_all_known_formats(self) -> None:
        for label, name in FORMATS.items():
            with self.subTest(label), mock.patch.object(crypto.subprocess, "check_output", return_value=fixture(name)) as check_output:
                self.assertEqual(crypto.xray_x25519_keys(), (PRIVATE_KEY, PUBLIC_KEY))
            check_output.assert_called_once_with(["/usr/local/bin/xray", "x25519"], text=True)

    def test_public_key_derivation_reads_all_known_formats(self) -> None:
        for label, name in FORMATS.items():
            with self.subTest(label), mock.patch.object(crypto.subprocess, "check_output", return_value=fixture(name)) as check_output:
                self.assertEqual(crypto.reality_public_key(PRIVATE_KEY), PUBLIC_KEY)
            check_output.assert_called_once_with(["/usr/local/bin/xray", "x25519", "-i", PRIVATE_KEY], text=True)

    def test_key_pair_error_contains_raw_output_without_private_key(self) -> None:
        output = f"PrivateKey: {PRIVATE_KEY}\nPassword: short\nHash32: {HASH32}\n"

        with mock.patch.object(crypto.subprocess, "check_output", return_value=output):
            with self.assertRaises(RuntimeError) as raised:
                crypto.xray_x25519_keys()

        message = str(raised.exception)
        self.assertIn("Failed to generate Reality key pair.", message)
        self.assertIn("PrivateKey: <hidden>", message)
        self.assertIn("Password: short", message)
        self.assertIn(f"Hash32: {HASH32}", message)
        self.assertNotIn(PRIVATE_KEY, message)

    def test_key_pair_error_on_empty_values_shows_labels(self) -> None:
        output = "PrivateKey: \nPassword (PublicKey): \nHash32: \n"

        with mock.patch.object(crypto.subprocess, "check_output", return_value=output):
            with self.assertRaises(RuntimeError) as raised:
                crypto.xray_x25519_keys()

        message = str(raised.exception)
        self.assertIn("Failed to generate Reality key pair.", message)
        self.assertIn("Password (PublicKey):", message)
        self.assertIn("Hash32:", message)

    def test_key_pair_error_hides_key_under_unknown_label(self) -> None:
        output = f"Seed: {PRIVATE_KEY}\nPublic key: {PUBLIC_KEY}\n"

        with mock.patch.object(crypto.subprocess, "check_output", return_value=output):
            with self.assertRaises(RuntimeError) as raised:
                crypto.xray_x25519_keys()

        message = str(raised.exception)
        self.assertIn("Seed: <hidden>", message)
        self.assertIn(f"Public key: {PUBLIC_KEY}", message)
        self.assertNotIn(PRIVATE_KEY, message)

    def test_key_pair_error_on_empty_output(self) -> None:
        with mock.patch.object(crypto.subprocess, "check_output", return_value=""):
            with self.assertRaises(RuntimeError) as raised:
                crypto.xray_x25519_keys()

        self.assertIn("(empty)", str(raised.exception))

    def test_public_key_error_contains_raw_output_without_known_private_key(self) -> None:
        output = f"PrivateKey: {PRIVATE_KEY}\nPublic key: {PRIVATE_KEY}x\nHash32: {HASH32}\n"

        with mock.patch.object(crypto.subprocess, "check_output", return_value=output):
            with self.assertRaises(RuntimeError) as raised:
                crypto.reality_public_key(PRIVATE_KEY)

        message = str(raised.exception)
        self.assertIn("Failed to derive Reality public key.", message)
        self.assertIn("PrivateKey: <hidden>", message)
        self.assertIn("Public key: <hidden>x", message)
        self.assertIn(f"Hash32: {HASH32}", message)
        self.assertNotIn(PRIVATE_KEY, message)


class RealityLinkPublicKeyTests(unittest.TestCase):
    def config(self) -> dict:
        inbound = client_connections.make_reality_inbound("vless-reality", 443, "example.com", PRIVATE_KEY, "abcd")
        return {"inbounds": [inbound], "outbounds": []}

    def db(self, public_key: str) -> dict:
        return {
            "connections": {
                "vless-reality": {
                    "tag": "vless-reality",
                    "name": "default",
                    "created": "2026-10-18T00:00:00Z",
                    "publicKey": public_key,
                    "shortId": "abcd",
                }
            },
            "clients": {"alice": {"id": CLIENT_ID, "connection": "vless-reality"}},
        }

    def link(self, config: dict, db: dict) -> str:
        with mock.patch.object(client_links, "server_addr", return_value="vpn.example"), \
            mock.patch.object(client_links, "server_name", return_value="Xray"):
            return client_links.link_for(config, CLIENT_ID, "alice", db=db)

    def test_link_uses_stored_public_key_without_running_xray(self) -> None:
        db = self.db(STORED_PUBLIC_KEY)

        with mock.patch.object(crypto.subprocess, "check_output", side_effect=AssertionError("xray must not run")), \
            mock.patch.object(client_links, "reality_public_key", side_effect=AssertionError("must not derive")):
            link = self.link(self.config(), db)

        self.assertEqual(parse_qs(urlsplit(link).query)["pbk"], [STORED_PUBLIC_KEY])

    def test_link_derives_public_key_when_stored_one_is_empty(self) -> None:
        db = self.db("")

        with mock.patch.object(crypto.subprocess, "check_output", return_value=fixture(FORMATS["B password/hash32"])) as check_output:
            link = self.link(self.config(), db)

        self.assertEqual(parse_qs(urlsplit(link).query)["pbk"], [PUBLIC_KEY])
        check_output.assert_called_once_with(["/usr/local/bin/xray", "x25519", "-i", PRIVATE_KEY], text=True)
        self.assertEqual(db["connections"]["vless-reality"]["publicKey"], "")

    def test_link_derives_public_key_when_stored_one_is_malformed(self) -> None:
        db = self.db("public-key")

        with mock.patch.object(client_links, "reality_public_key", return_value=PUBLIC_KEY) as derive:
            link = self.link(self.config(), db)

        self.assertEqual(parse_qs(urlsplit(link).query)["pbk"], [PUBLIC_KEY])
        derive.assert_called_once_with(PRIVATE_KEY)
        self.assertEqual(db["connections"]["vless-reality"]["publicKey"], "public-key")

    def test_link_derives_public_key_for_connection_without_db_record(self) -> None:
        db = {"connections": {}, "clients": {"alice": {"id": CLIENT_ID, "connection": "vless-reality"}}}

        with mock.patch.object(client_links, "reality_public_key", return_value=PUBLIC_KEY) as derive:
            link = self.link(self.config(), db)

        self.assertEqual(parse_qs(urlsplit(link).query)["pbk"], [PUBLIC_KEY])
        derive.assert_called_once_with(PRIVATE_KEY)


class InstallScriptX25519ParseTests(unittest.TestCase):
    def install_function(self) -> str:
        match = INSTALL_FUNCTION_RE.search(INSTALL_SH.read_text(encoding="utf-8"))
        self.assertIsNotNone(match, "install.sh must define x25519_key_from_output()")
        return match.group(0)

    def run_parser(self, kind: str, output: str) -> str:
        script = self.install_function() + 'printf "%s" "$2" | x25519_key_from_output "$1"\n'
        result = subprocess.run(
            ["bash", "-c", script, "_", kind, output],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def test_install_parser_reads_all_known_formats(self) -> None:
        for label, name in FORMATS.items():
            with self.subTest(label):
                output = fixture(name)
                self.assertEqual(self.run_parser("private", output), PRIVATE_KEY)
                self.assertEqual(self.run_parser("public", output), PUBLIC_KEY)

    def test_install_parser_ignores_hash32_and_rejects_bad_values(self) -> None:
        reordered = f"Hash32: {HASH32}\nPassword: {PUBLIC_KEY}\r\nPrivateKey: {PRIVATE_KEY}\r\n"
        self.assertEqual(self.run_parser("public", reordered), PUBLIC_KEY)
        self.assertEqual(self.run_parser("private", reordered), PRIVATE_KEY)

        bad = f"PrivateKey: short\nPassword: {PUBLIC_KEY}A\nHash32: {HASH32}\n"
        self.assertEqual(self.run_parser("private", bad), "")
        self.assertEqual(self.run_parser("public", bad), "")

        empty = "PrivateKey: \nPassword (PublicKey): \n"
        self.assertEqual(self.run_parser("private", empty), "")
        self.assertEqual(self.run_parser("public", empty), "")

    def test_install_script_uses_tolerant_parser_for_generated_keys(self) -> None:
        content = INSTALL_SH.read_text(encoding="utf-8")

        self.assertIn("private_key=\"$(printf '%s\\n' \"$keys\" | x25519_key_from_output private)\"", content)
        self.assertIn("public_key=\"$(printf '%s\\n' \"$keys\" | x25519_key_from_output public)\"", content)
        self.assertNotIn("/^Password \\(PublicKey\\):/", content)


if __name__ == "__main__":
    unittest.main()
