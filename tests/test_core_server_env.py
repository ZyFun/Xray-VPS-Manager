from pathlib import Path
import tempfile
import unittest

from xray_vps_manager.core.server_env import read_server_env, write_server_env


class ServerEnvTests(unittest.TestCase):
    def test_read_server_env_ignores_comments_blank_lines_and_invalid_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            env_path = Path(tmp_dir) / "server.env"
            env_path.write_text(
                "\n".join(
                    [
                        "# comment",
                        "",
                        "SERVER_ADDR=example.com",
                        "SERVER_NAME='Demo Server'",
                        'MANAGER_TIMEZONE="Europe/Moscow"',
                        "broken line",
                    ]
                )
                + "\n"
            )

            self.assertEqual(
                read_server_env(env_path),
                {
                    "SERVER_ADDR": "example.com",
                    "SERVER_NAME": "Demo Server",
                    "MANAGER_TIMEZONE": "Europe/Moscow",
                },
            )

    def test_read_server_env_can_require_valid_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            env_path = Path(tmp_dir) / "server.env"
            env_path.write_text("SERVER_ADDR=example.com\nbroken line\n")

            with self.assertRaisesRegex(RuntimeError, "invalid line without '='"):
                read_server_env(env_path, strict=True)

    def test_read_server_env_can_require_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            missing = Path(tmp_dir) / "missing.env"

            self.assertEqual(read_server_env(missing), {})
            with self.assertRaisesRegex(RuntimeError, "not found:"):
                read_server_env(missing, require_exists=True)

    def test_write_server_env_orders_known_keys_and_drops_legacy_geoip_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            env_path = Path(tmp_dir) / "server.env"

            write_server_env(
                {
                    "CUSTOM": "value",
                    "SERVER_NAME": "Demo",
                    "SERVER_ADDR": "example.com",
                    "ACTIVITY_GEOIP_WARNING_CODE": "RU",
                    "MANAGER_TIMEZONE": "Europe/Moscow",
                },
                env_path,
            )

            self.assertEqual(
                env_path.read_text().splitlines(),
                [
                    "SERVER_ADDR=example.com",
                    "SERVER_NAME=Demo",
                    "MANAGER_TIMEZONE=Europe/Moscow",
                    "CUSTOM=value",
                ],
            )
            self.assertEqual(read_server_env(env_path)["CUSTOM"], "value")


if __name__ == "__main__":
    unittest.main()
