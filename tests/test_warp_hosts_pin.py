"""Регрессия XR-REV-019 (b): `xray-warp` правит `/etc/hosts` только с согласия и умеет снимать пин.

При сбое TLS к `api.cloudflareclient.com` менеджер раньше молча пинил найденный IPv4 в `/etc/hosts`
и удалял все строки с этим именем, включая чужие. Теперь: вопрос в интерактиве, `--pin-hosts` /
`--no-pin-hosts` для неинтерактивного вызова, без TTY и флага — только `WARN`; правится только строка с
управляемым комментарием, чужая строка блокирует пин; `xray-warp remove` снимает управляемую строку.
"""

from contextlib import contextmanager, redirect_stdout
from io import StringIO
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from xray_vps_manager.commands import warp
from xray_vps_manager.routing import warp as warp_routing

WORKING_IP = "8.47.69.1"
MANAGED_LINE = f"{WORKING_IP} {warp.API_HOST} {warp.HOSTS_PIN_COMMENT}"
BASE_HOSTS = "127.0.0.1 localhost\n::1 localhost ip6-localhost\n"


class FakeStdin:
    def __init__(self, tty: bool) -> None:
        self.tty = tty

    def isatty(self) -> bool:
        return self.tty


class HostsFileTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.hosts = self.dir / "hosts"
        for patcher in (
            mock.patch.object(warp, "HOSTS_PATH", self.hosts),
            mock.patch.object(warp.shutil, "chown"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def write_hosts(self, text: str) -> None:
        self.hosts.write_text(text)

    def backups(self) -> list:
        return sorted(self.dir.glob("hosts.bak.xray-warp.*"))

    def run_tls_check(self, pin_hosts=None, tty=False, answer="n"):
        """`ensure_cloudflare_api_tls` при сбое TLS через DNS и рабочем WORKING_IP."""
        output = StringIO()
        with mock.patch.object(warp, "tls_handshake_ok", side_effect=lambda host, **_: host == WORKING_IP), \
                mock.patch.object(warp, "api_candidate_ips", return_value=["8.6.112.1", WORKING_IP]), \
                mock.patch.object(warp.sys, "stdin", FakeStdin(tty)), \
                mock.patch("builtins.input", return_value=answer) as prompt, \
                redirect_stdout(output):
            warp.ensure_cloudflare_api_tls(pin_hosts=pin_hosts)
        return output.getvalue(), prompt


class PinWithoutConsentTests(HostsFileTestCase):
    def test_interactive_no_answer_keeps_hosts_and_shows_line(self) -> None:
        self.write_hosts(BASE_HOSTS)

        output, prompt = self.run_tls_check(tty=True, answer="n")

        self.assertEqual(self.hosts.read_text(), BASE_HOSTS)
        self.assertEqual(self.backups(), [])
        prompt.assert_called_once()
        self.assertIn(MANAGED_LINE, output + prompt.call_args[0][0])

    def test_interactive_eof_keeps_hosts(self) -> None:
        self.write_hosts(BASE_HOSTS)
        with mock.patch.object(warp, "tls_handshake_ok", side_effect=lambda host, **_: host == WORKING_IP), \
                mock.patch.object(warp, "api_candidate_ips", return_value=[WORKING_IP]), \
                mock.patch.object(warp.sys, "stdin", FakeStdin(True)), \
                mock.patch("builtins.input", side_effect=EOFError), \
                redirect_stdout(StringIO()):
            warp.ensure_cloudflare_api_tls(pin_hosts=None)

        self.assertEqual(self.hosts.read_text(), BASE_HOSTS)
        self.assertEqual(self.backups(), [])

    def test_no_tty_without_flag_warns_and_keeps_hosts(self) -> None:
        self.write_hosts(BASE_HOSTS)

        output, prompt = self.run_tls_check(tty=False)

        self.assertEqual(self.hosts.read_text(), BASE_HOSTS)
        self.assertEqual(self.backups(), [])
        prompt.assert_not_called()
        self.assertIn("WARN", output)
        self.assertIn("--pin-hosts", output)

    def test_no_pin_hosts_flag_keeps_hosts_even_in_tty(self) -> None:
        self.write_hosts(BASE_HOSTS)

        output, prompt = self.run_tls_check(pin_hosts=False, tty=True)

        self.assertEqual(self.hosts.read_text(), BASE_HOSTS)
        self.assertEqual(self.backups(), [])
        prompt.assert_not_called()
        self.assertIn("WARN", output)


class PinWithConsentTests(HostsFileTestCase):
    def test_interactive_yes_adds_one_managed_line_with_backup(self) -> None:
        self.write_hosts(BASE_HOSTS)

        self.run_tls_check(tty=True, answer="y")

        self.assertEqual(self.hosts.read_text(), BASE_HOSTS + MANAGED_LINE + "\n")
        backups = self.backups()
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), BASE_HOSTS)

    def test_pin_hosts_flag_without_tty_adds_line(self) -> None:
        self.write_hosts(BASE_HOSTS)

        _, prompt = self.run_tls_check(pin_hosts=True, tty=False)

        prompt.assert_not_called()
        self.assertEqual(self.hosts.read_text(), BASE_HOSTS + MANAGED_LINE + "\n")

    def test_previous_managed_line_is_replaced(self) -> None:
        self.write_hosts(BASE_HOSTS + f"8.6.112.9 {warp.API_HOST} {warp.HOSTS_PIN_COMMENT}\n")

        self.run_tls_check(pin_hosts=True)

        self.assertEqual(self.hosts.read_text(), BASE_HOSTS + MANAGED_LINE + "\n")

    def test_foreign_line_for_same_host_is_kept_and_blocks_pin(self) -> None:
        foreign = f"1.2.3.4 {warp.API_HOST} # manual\n"
        self.write_hosts(BASE_HOSTS + foreign)

        output, _ = self.run_tls_check(pin_hosts=True, tty=True, answer="y")

        self.assertEqual(self.hosts.read_text(), BASE_HOSTS + foreign)
        self.assertEqual(self.backups(), [])
        self.assertIn("WARN", output)
        self.assertIn("1.2.3.4", output)

    def test_changes_made_during_question_are_kept(self) -> None:
        self.write_hosts(BASE_HOSTS)
        edited = BASE_HOSTS + "10.0.0.5 added.example\n"

        def answer_after_edit(_prompt):
            self.write_hosts(edited)
            return "y"

        with mock.patch.object(warp, "tls_handshake_ok", side_effect=lambda host, **_: host == WORKING_IP), \
                mock.patch.object(warp, "api_candidate_ips", return_value=[WORKING_IP]), \
                mock.patch.object(warp.sys, "stdin", FakeStdin(True)), \
                mock.patch("builtins.input", side_effect=answer_after_edit), \
                redirect_stdout(StringIO()):
            warp.ensure_cloudflare_api_tls(pin_hosts=None)

        self.assertEqual(self.hosts.read_text(), edited + MANAGED_LINE + "\n")

    def test_foreign_line_added_during_question_blocks_pin(self) -> None:
        self.write_hosts(BASE_HOSTS)
        edited = BASE_HOSTS + f"1.2.3.4 {warp.API_HOST}\n"

        def answer_after_edit(_prompt):
            self.write_hosts(edited)
            return "y"

        output = StringIO()
        with mock.patch.object(warp, "tls_handshake_ok", side_effect=lambda host, **_: host == WORKING_IP), \
                mock.patch.object(warp, "api_candidate_ips", return_value=[WORKING_IP]), \
                mock.patch.object(warp.sys, "stdin", FakeStdin(True)), \
                mock.patch("builtins.input", side_effect=answer_after_edit), \
                redirect_stdout(output):
            warp.ensure_cloudflare_api_tls(pin_hosts=None)

        self.assertEqual(self.hosts.read_text(), edited)
        self.assertEqual(self.backups(), [])
        self.assertIn("1.2.3.4", output.getvalue())

    def test_foreign_line_in_other_case_blocks_pin(self) -> None:
        foreign = "1.2.3.4 API.CloudflareClient.COM\n"
        self.write_hosts(BASE_HOSTS + foreign)

        output, _ = self.run_tls_check(pin_hosts=True)

        self.assertEqual(self.hosts.read_text(), BASE_HOSTS + foreign)
        self.assertEqual(self.backups(), [])
        self.assertIn("WARN", output)

    def test_working_dns_does_not_touch_hosts(self) -> None:
        self.write_hosts(BASE_HOSTS)
        with mock.patch.object(warp, "tls_handshake_ok", return_value=True), \
                mock.patch("builtins.input") as prompt, redirect_stdout(StringIO()):
            warp.ensure_cloudflare_api_tls(pin_hosts=True)

        prompt.assert_not_called()
        self.assertEqual(self.hosts.read_text(), BASE_HOSTS)


class RemoveUnpinTests(HostsFileTestCase):
    @contextmanager
    def remove_env(self, changed: bool):
        with mock.patch.object(warp, "manager_lock", mock.MagicMock()), \
                mock.patch.object(warp, "load_config", return_value={}), \
                mock.patch.object(warp, "load_client_db_optional", return_value=None), \
                mock.patch.object(warp, "save_client_db_optional"), \
                mock.patch.object(warp, "remove_warp", return_value=changed), \
                mock.patch.object(warp, "apply_config", return_value=Path("/tmp/backup.json")), \
                redirect_stdout(StringIO()):
            yield

    def test_remove_drops_only_managed_line(self) -> None:
        foreign = f"1.2.3.4 {warp.API_HOST}\n"
        original = BASE_HOSTS + foreign + MANAGED_LINE + "\n" + "10.0.0.1 other.example\n"
        self.write_hosts(original)

        with self.remove_env(changed=True):
            warp.cmd_remove()

        self.assertEqual(self.hosts.read_text(), BASE_HOSTS + foreign + "10.0.0.1 other.example\n")
        backups = self.backups()
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), original)

    def test_remove_unpins_when_warp_config_already_removed(self) -> None:
        self.write_hosts(BASE_HOSTS + MANAGED_LINE + "\n")

        with self.remove_env(changed=False):
            warp.cmd_remove()

        self.assertEqual(self.hosts.read_text(), BASE_HOSTS)
        self.assertEqual(len(self.backups()), 1)

    def test_remove_without_managed_line_changes_nothing(self) -> None:
        original = BASE_HOSTS + f"1.2.3.4 {warp.API_HOST}\n"
        self.write_hosts(original)

        with self.remove_env(changed=True):
            warp.cmd_remove()

        self.assertEqual(self.hosts.read_text(), original)
        self.assertEqual(self.backups(), [])


class HostsLinesTests(unittest.TestCase):
    def test_pin_lines_replaces_managed_and_keeps_others(self) -> None:
        lines = ["127.0.0.1 localhost", f"9.9.9.9 {warp.API_HOST} {warp.HOSTS_PIN_COMMENT}", "# comment"]

        result = warp_routing.pin_hosts_lines(lines, WORKING_IP, warp.API_HOST, warp.HOSTS_PIN_COMMENT)

        self.assertEqual(result, ["127.0.0.1 localhost", "# comment", MANAGED_LINE])

    def test_unpin_lines_keeps_foreign_lines(self) -> None:
        lines = [f"1.2.3.4 {warp.API_HOST}", MANAGED_LINE]

        self.assertEqual(warp_routing.unpin_hosts_lines(lines, warp.HOSTS_PIN_COMMENT), [f"1.2.3.4 {warp.API_HOST}"])

    def test_foreign_lines_ignore_comments_and_managed_line(self) -> None:
        lines = [f"# 1.2.3.4 {warp.API_HOST}", MANAGED_LINE, f"5.6.7.8 alias {warp.API_HOST}"]

        self.assertEqual(
            warp_routing.foreign_hosts_lines(lines, warp.API_HOST, warp.HOSTS_PIN_COMMENT),
            [f"5.6.7.8 alias {warp.API_HOST}"],
        )

    def test_foreign_lines_match_names_case_insensitively(self) -> None:
        lines = ["1.2.3.4 Api.CloudflareClient.Com"]

        self.assertEqual(warp_routing.foreign_hosts_lines(lines, warp.API_HOST, warp.HOSTS_PIN_COMMENT), lines)


class MainArgsTests(unittest.TestCase):
    def test_create_and_enable_options_are_accepted_before_root(self) -> None:
        from tests.support.sandbox import Sandbox
        import os
        for command, options in (
            ('create', ()), ('create', ('--force',)), ('create', ('--pin-hosts',)),
            ('create', ('--no-pin-hosts', '--force')),
            ('enable', ()), ('enable', ('--pin-hosts',)), ('enable', ('--no-pin-hosts',)),
        ):
            with self.subTest(command=command, options=options), Sandbox() as box:
                with mock.patch.object(os, 'geteuid', return_value=1000):
                    result = box.run_cli('warp', command, *options)
                self.assertEqual((result.exit_code, result.stdout, result.stderr),
                                 (1, '', 'ERROR: Run this script as root.\n'))
                self.assertFalse(box.processes.calls or box.http.calls)

    def test_pin_choice_preserves_three_states(self) -> None:
        for args, expected in (((), None), (('--pin-hosts',), True), (('--no-pin-hosts',), False)):
            with self.subTest(args=args):
                self.assertIs(warp.pin_hosts_choice(warp.parse_flags(args, warp.PIN_HOSTS_FLAGS)), expected)

    def test_bad_flags_print_usage_to_stderr_and_exit_2(self) -> None:
        from tests.support.sandbox import Sandbox
        cases = [
            ('create', '--unknown'),
            ('create', '--pin-hosts', '--no-pin-hosts'),
            ('create', '--force', '--force'),
            ('enable', '--force'),
            ('enable', '--unknown'),
            ('remove', '--pin-hosts'),
        ]
        for args in cases:
            with self.subTest(args=args), Sandbox() as box:
                before = box.config()
                result = box.run_cli('warp', *args)
                self.assertEqual((result.exit_code, result.stdout), (2, ''))
                self.assertIn('Usage:', result.stderr)
                self.assertFalse(box.processes.calls or box.http.calls)
                self.assertEqual(box.config(), before)


if __name__ == "__main__":
    unittest.main()
