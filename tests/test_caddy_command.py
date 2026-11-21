import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO

from tests.support.sandbox import Sandbox
from tests.test_client_command_read import script_caddy
from tests.test_connection_menu_characterization import script_menu_processes
from xray_vps_manager.caddy import random_tls, service, sites


class CaddyCommandTests(unittest.TestCase):
    def test_random_tls_run_passes_domain_argument(self) -> None:
        with Sandbox() as box:
            script_caddy(box)
            for domain in ("api.example.com", "other.example.com"):
                sites.write_site_config(domain, 10300)
                random_tls.write_random_tls_config(domain, 10300)

            result = box.run_cli("caddy", "random-tls-run", "--domain", "api.example.com", "--quiet")

            self.assertEqual((result.exit_code, result.stdout, result.stderr), (0, "", ""))
            self.assertEqual(sites.site_config_for_domain("api.example.com").tls_min_version, "tls1.3")
            self.assertEqual(sites.site_config_for_domain("other.example.com").tls_min_version, "tls1.2")

    def test_validate_config_streams_only_from_menu(self) -> None:
        with Sandbox() as box:
            script_menu_processes(box)
            command = ("caddy", "validate", "--config", str(box.paths.caddyfile))
            box.processes.script({command: box.processes.result(stdout="valid\n", stderr="notice\n")})

            out, err = StringIO(), StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                service.validate_config()

            self.assertEqual((out.getvalue(), err.getvalue()), ("", ""))
            self.assertEqual(box.processes.calls, [command])
            result = box.run_menu(["2", "4", "1", "3", "0", "0", "0", "0"])
            self.assertEqual(result.exit_code, 0)
            self.assertIn("valid\n", result.stdout)
            self.assertEqual(result.stderr, "notice\n")
            self.assertEqual(box.processes.calls.count(command), 2)

    def test_randomizer_requires_configuration_and_explicit_timer_enable(self) -> None:
        with Sandbox() as box:
            script_menu_processes(box)
            sites.write_site_config("api.example.com", 10300)

            result = box.run_cli("caddy", "random-tls-run", "--domain", "api.example.com")

            self.assertEqual(result.exit_code, 1)
            self.assertIn("api.example.com.env", result.stderr)
            self.assertEqual(sites.site_config_for_domain("api.example.com").tls_min_version, "tls1.2")
            self.assertFalse(box.paths.caddy_random_tls_dir.exists())
            self.assertEqual(box.processes.calls, [])

            result = box.run_menu(["2", "4", "5", "2", "n", "0", "0", "0", "0"])

            self.assertIn("Действие отменено.", result.stdout)
            self.assertFalse(box.paths.caddy_random_tls_dir.exists())
            self.assertFalse(box.paths.systemd_dir.exists())
            self.assertEqual({call[1:] for call in box.processes.calls}, {("version",)})


if __name__ == "__main__":
    unittest.main()
