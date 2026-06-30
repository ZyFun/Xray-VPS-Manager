import unittest
from unittest import mock

from xray_vps_manager.commands import caddy as caddy_command
from xray_vps_manager.xray import caddy


class CaddyCommandTests(unittest.TestCase):
    def test_random_tls_run_passes_domain_argument(self) -> None:
        result = caddy.RandomTlsApplyResult(
            domain="api.example.com",
            previous_tls_min_version="tls1.2",
            previous_tls_max_version="tls1.2",
            tls_min_version="tls1.3",
            tls_max_version="tls1.3",
            path=caddy.CADDY_CONF_DIR / "api.example.com.caddy",
            backup=None,
        )

        with mock.patch.object(caddy_command.os, "geteuid", return_value=0), mock.patch.object(
            caddy_command.caddy, "apply_random_tls_switch", return_value=result
        ) as apply_switch:
            caddy_command.cmd_random_tls_run(domain="api.example.com", quiet=True)

        apply_switch.assert_called_once_with(domain="api.example.com")


if __name__ == "__main__":
    unittest.main()
