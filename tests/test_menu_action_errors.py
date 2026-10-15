import contextlib
import io
import os
import unittest
from unittest import mock

from xray_vps_manager.commands import menu

SEPARATOR_END = "=" * len(menu.action_separator("Сломанное действие"))


def run_captured(func, *args):
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        result = func(*args)
    return result, stdout.getvalue(), stderr.getvalue()


def raising(exc):
    def action():
        raise exc

    return action


class ExecuteActionErrorTests(unittest.TestCase):
    def test_system_exit_with_message_returns_and_prints_message(self) -> None:
        _result, stdout, stderr = run_captured(
            menu.execute_action, "Сломанное действие", raising(SystemExit("PORT 443 уже занят"))
        )

        self.assertIn("ERROR: PORT 443 уже занят", stderr)
        self.assertIn(SEPARATOR_END, stdout)

    def test_unexpected_exception_returns_and_prints_type_and_message(self) -> None:
        with mock.patch.dict(os.environ, {"XRAY_MENU_DEBUG": ""}):
            _result, stdout, stderr = run_captured(
                menu.execute_action, "Сломанное действие", raising(ValueError("плохое значение"))
            )

        self.assertIn("ERROR: ValueError: плохое значение", stderr)
        self.assertNotIn("Traceback", stderr)
        self.assertIn(SEPARATOR_END, stdout)

    def test_debug_flag_prints_full_traceback(self) -> None:
        with mock.patch.dict(os.environ, {"XRAY_MENU_DEBUG": "1"}):
            _result, _stdout, stderr = run_captured(
                menu.execute_action, "Сломанное действие", raising(KeyError("name"))
            )

        self.assertIn("ERROR: KeyError: 'name'", stderr)
        self.assertIn("Traceback (most recent call last)", stderr)

    def test_menu_die_exit_code_one_is_not_duplicated(self) -> None:
        def action():
            menu.die("Run this script as root.")

        _result, stdout, stderr = run_captured(menu.execute_action, "Сломанное действие", action)

        self.assertEqual(stderr.count("Run this script as root."), 1)
        self.assertEqual(stderr.count("ERROR"), 1)
        self.assertIn(SEPARATOR_END, stdout)

    def test_successful_exit_codes_print_no_error(self) -> None:
        for code in (None, 0):
            with self.subTest(code=code):
                _result, stdout, stderr = run_captured(
                    menu.execute_action, "Сломанное действие", raising(SystemExit(code))
                )

                self.assertEqual(stderr, "")
                self.assertIn(SEPARATOR_END, stdout)

    def test_keyboard_interrupt_prints_cancelled_and_propagates(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(KeyboardInterrupt):
                menu.execute_action("Сломанное действие", raising(KeyboardInterrupt()))

        self.assertIn("Отменено", stdout.getvalue())
        self.assertIn(SEPARATOR_END, stdout.getvalue())

    def test_keyboard_interrupt_in_nested_submenu_prints_cancelled_once(self) -> None:
        # Вход во вложенное подменю — тоже действие родителя.
        def nested_submenu():
            menu.execute_action("Сломанное действие", raising(KeyboardInterrupt()))

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(KeyboardInterrupt):
                menu.execute_action("Подменю", nested_submenu)

        self.assertEqual(stdout.getvalue().count("Отменено"), 1)

    def test_eof_propagates_without_error_message(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            with self.assertRaises(EOFError):
                menu.execute_action("Сломанное действие", raising(EOFError()))

        self.assertNotIn("ERROR", stderr.getvalue())
        self.assertIn(SEPARATOR_END, stdout.getvalue())


class MenuLoopErrorTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.object(menu, "print_menu_header")
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_loop(self, handlers, answers):
        with mock.patch("builtins.input", side_effect=answers) as fake_input:
            result = run_captured(menu.menu_loop, "Тест", [("1", "Действие"), ("0", "Назад")], handlers)
        return fake_input, result

    def test_loop_continues_after_failing_actions(self) -> None:
        for exc in (SystemExit("Некорректный PORT"), ValueError("плохое значение")):
            with self.subTest(exc=type(exc).__name__):
                handlers = {"1": ("Сломанное действие", raising(exc))}

                fake_input, (_result, stdout, stderr) = self.run_loop(handlers, ["1", "0"])

                self.assertEqual(fake_input.call_count, 2)
                self.assertIn(str(exc), stderr)
                self.assertIn(SEPARATOR_END, stdout)

    def test_loop_propagates_eof_from_action(self) -> None:
        handlers = {"1": ("Сломанное действие", raising(EOFError()))}

        with self.assertRaises(EOFError):
            self.run_loop(handlers, ["1", "0"])


class MenuEntryInterruptTests(unittest.TestCase):
    def setUp(self) -> None:
        for patcher in (
            mock.patch.object(menu, "print_menu_header"),
            mock.patch("os.geteuid", return_value=0),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_menu(self, answers):
        with mock.patch("builtins.input", side_effect=answers):
            return run_captured(menu.menu)

    def test_eof_on_main_menu_prompt_exits_cleanly(self) -> None:
        result, _stdout, stderr = self.run_menu(EOFError())

        self.assertIsNone(result)
        self.assertNotIn("Traceback", stderr)

    def test_eof_inside_nested_submenu_exits_cleanly(self) -> None:
        # «Подключения и TLS» -> EOF на «Выбор:» вложенного подменю.
        result, _stdout, stderr = self.run_menu(["2", EOFError()])

        self.assertIsNone(result)
        self.assertNotIn("Traceback", stderr)

    def test_keyboard_interrupt_exits_with_code_130(self) -> None:
        for answers in (KeyboardInterrupt(), ["2", KeyboardInterrupt()]):
            with self.subTest(answers=answers):
                with self.assertRaises(SystemExit) as raised:
                    self.run_menu(answers)

                self.assertEqual(raised.exception.code, 130)


if __name__ == "__main__":
    unittest.main()
