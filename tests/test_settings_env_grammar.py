"""B1.2 / XR-REV-069: согласованная грамматика и сохранность server.env."""
import itertools
import io
import tarfile
import unittest

from tests.support.sandbox import Sandbox
from xray_vps_manager.core import settings, server_env
from xray_vps_manager.core.errors import ValidationError
from xray_vps_manager.commands import backup
from xray_vps_manager.activity import controls as activity
from xray_vps_manager.traffic import controls as traffic


class EnvGrammarTests(unittest.TestCase):
    def test_read_normalizes_all_keys_and_last_normalized_line_wins(self):
        with Sandbox() as box:
            box.paths.server_env.write_bytes(
                b'  export port \t= 443 # note\r\n'
                b' export\tcustom_Key = first\r\n'
                b'CUSTOM_KEY=second\r\n'
                b' lower = \r\n'
                b'exportable=value\r\n'
            )
            self.assertEqual(settings.read_env(), {
                'PORT': '443', 'CUSTOM_KEY': 'second', 'LOWER': '', 'EXPORTABLE': 'value',
            })

    def test_comments_quotes_escapes_and_shell_characters_are_data(self):
        cases = {
            'a#b': 'a#b',
            '#literal': '#literal',
            ' # comment': '',
            'a\t# comment': 'a',
            'a # comment': 'a',
            '"a # b" # comment': 'a # b',
            "'a # b'\t# comment": 'a # b',
            '"\'both\'"': "'both'",
            "'\"both\"'": '"both"',
            "it's # comment": "it's",
            '"open # literal': '"open # literal',
            "'open # literal": "'open # literal",
            'close"': 'close"',
            'a=b=c': 'a=b=c',
            '  value  ': 'value',
            '"  value  "': '  value  ',
            r'"a\" # b" # comment': 'a" # b',
            r'"C:\\data\file"': r'C:\data\file',
            r"'C:\\data\file'": r'C:\\data\file',
            r'C:\\data\file': r'C:\\data\file',
            '$(touch SHOULD_NOT_EXIST)': '$(touch SHOULD_NOT_EXIST)',
            '`touch SHOULD_NOT_EXIST`': '`touch SHOULD_NOT_EXIST`',
            '$VAR ${VAR}': '$VAR ${VAR}',
        }
        with Sandbox() as box:
            for raw, expected in cases.items():
                with self.subTest(raw=raw):
                    box.paths.server_env.write_text('KEY=' + raw + '\n')
                    self.assertEqual(server_env.read_server_env(strict=True), {'KEY': expected})

    def test_safe_write_and_repeated_read_preserve_values(self):
        values = {
            'EMPTY': '', 'PLAIN': 'a=b#c', 'HASH': 'a # b', 'TAB_HASH': 'a\t# b',
            'SPACES': '  keep  ', 'QUOTES': "'both\" # quote'", 'APOSTROPHE': "it's",
            'UNPAIRED': '"open', 'BACKSLASH': r'"C:\data\\" # end',
            'SHELL': '$(touch SHOULD_NOT_EXIST) `$VAR`',
        }
        with Sandbox() as box:
            settings.write_env(values)
            for _ in range(3):
                self.assertEqual(settings.read_env(), values)
                server_env.write_server_env(server_env.read_server_env())
            self.assertEqual(box.paths.server_env.stat().st_mode & 0o777, 0o640)

    def test_short_combinations_round_trip_without_losing_punctuation(self):
        # Перебор сочетаний ловит взаимодействия кавычек, пробелов, # и слешей.
        alphabet = 'a #\t\'"\\'
        values = {
            f'CUSTOM_{index}': ''.join(chars)
            for index, chars in enumerate(itertools.chain.from_iterable(
                itertools.product(alphabet, repeat=size) for size in range(4)
            ))
        }
        with Sandbox():
            settings.write_env(values)
            self.assertEqual(settings.read_env(), values)
            settings.write_env(settings.read_env())
            self.assertEqual(settings.read_env(), values)

    def test_write_normalizes_mapping_order_and_legacy_removal(self):
        values = {' export custom ': 'keep', ' port ': '443', 'server_name': 'Demo',
                  ' activity_geoip_warning_code ': 'RU'}
        with Sandbox() as box:
            settings.write_env(values, ordered_keys=[' port ', 'PORT', ' server_name '])
            self.assertEqual(box.paths.server_env.read_text(), 'PORT=443\nSERVER_NAME=Demo\nCUSTOM=keep\n')
            self.assertIn(' export custom ', values)

    def test_write_rejects_conflicting_normalized_keys_before_touching_file(self):
        with Sandbox() as box:
            box.paths.server_env.write_text('KEEP=before\n')
            for values in ({'port': '443', 'PORT': '8443'}, {'KEY': 'a', 'export KEY ': 'b'}):
                with self.subTest(values=values), self.assertRaises(ValidationError):
                    settings.write_env(values)
                self.assertEqual(box.paths.server_env.read_text(), 'KEEP=before\n')
                self.assertFalse(box.paths.server_env.with_suffix('.env.tmp').exists())
            settings.write_env({'port': '443', ' PORT ': '443'})
            self.assertEqual(box.paths.server_env.read_text(), 'PORT=443\n')

    def test_update_normalizes_each_input_and_keeps_merge_precedence(self):
        with Sandbox() as box:
            box.paths.server_env.write_text(' export empty =\ncustom="a # b"\nkeep=old\ndrop=x\n')
            result = settings.update_env(
                {' keep ': 'new', 'export drop': 'returned'},
                defaults={'empty': 'fallback', ' fresh ': 'yes'},
                remove=[' drop ', ' FRESH '], ordered_keys=['keep'],
            )
            self.assertEqual(result, {'EMPTY': '', 'CUSTOM': 'a # b', 'KEEP': 'new', 'DROP': 'returned'})
            self.assertEqual(settings.read_env(), result)
            self.assertTrue(box.paths.server_env.read_text().startswith('KEEP=new\n'))

    def test_conflicting_update_input_does_not_modify_file(self):
        with Sandbox() as box:
            box.paths.server_env.write_text('KEEP=before\n')
            for kwargs in ({'changes': {'x': 'a', ' X ': 'b'}},
                           {'changes': {}, 'defaults': {'x': 'a', 'X': 'b'}}):
                with self.subTest(kwargs=kwargs), self.assertRaises(ValidationError):
                    settings.update_env(**kwargs)
                self.assertEqual(box.paths.server_env.read_text(), 'KEEP=before\n')

    def test_line_separators_are_rejected_before_writing(self):
        """108-R02: сериализатор отклоняет все разделители, которые режет read_env."""
        separators = ('\n', '\r', '\r\n', '\v', '\f', '\x1c', '\x1d', '\x1e', '\x85', '\u2028', '\u2029')
        with Sandbox() as box:
            for separator in separators:
                for value in ('a' + separator + 'b', 'a' + separator, separator):
                    for writer in (settings.write_env, settings.update_env, settings.env_text):
                        with self.subTest(separator=repr(separator), value=repr(value), writer=writer.__name__):
                            box.paths.server_env.write_text('KEEP=before\n')
                            tmp = box.paths.server_env.with_suffix('.env.tmp')
                            tmp.unlink(missing_ok=True)
                            with self.assertRaisesRegex(ValidationError, 'single line'):
                                writer({'CUSTOM': value})
                            self.assertEqual(box.paths.server_env.read_text(), 'KEEP=before\n')
                            self.assertFalse(tmp.exists())

    def test_embedded_line_separator_in_key_keeps_existing_temporary_file(self):
        with Sandbox() as box:
            box.paths.server_env.write_text('KEEP=before\n')
            tmp = box.paths.server_env.with_suffix('.env.tmp')
            tmp.write_text('existing temporary content\n')
            for separator in ('\n', '\r', '\v', '\f', '\x1c', '\x1d', '\x1e', '\x85', '\u2028', '\u2029'):
                for writer in (settings.write_env, settings.update_env, settings.env_text):
                    with self.subTest(separator=repr(separator), writer=writer.__name__):
                        with self.assertRaisesRegex(ValidationError, 'single line'):
                            writer({'export custom' + separator + 'key': 'value'})
                        self.assertEqual(box.paths.server_env.read_text(), 'KEEP=before\n')
                        self.assertEqual(tmp.read_text(), 'existing temporary content\n')

    def test_strict_still_only_rejects_lines_without_equals(self):
        with Sandbox() as box:
            # B1.2 не вводит новой валидации malformed ключей и непарных кавычек.
            box.paths.server_env.write_text('=empty key\nBAD KEY=value\nKEY="open\n')
            self.assertEqual(settings.read_env(strict=True), {'': 'empty key', 'BAD KEY': 'value', 'KEY': '"open'})
            box.paths.server_env.write_text('BROKEN\n')
            self.assertEqual(settings.read_env(), {})
            with self.assertRaisesRegex(ValidationError, "invalid line without '='"):
                settings.read_env(strict=True)
            box.paths.server_env.unlink()
            with self.assertRaisesRegex(RuntimeError, 'not found:'):
                settings.read_env(require_exists=True)

    def test_domain_writers_keep_unknown_quoted_value_and_empty_value(self):
        with Sandbox() as box:
            box.paths.server_env.write_text('export custom="  a # b  "\nEMPTY=\n')
            traffic.set_total_multiplier('1.5')
            activity.set_enabled(True)
            values = settings.read_env()
            self.assertEqual(values['CUSTOM'], '  a # b  ')
            self.assertEqual(values['EMPTY'], '')

    def test_portable_backup_and_restore_keep_quoted_values(self):
        with Sandbox() as box:
            values = {'CUSTOM': '  a # b "quoted" \\ ', 'EMPTY': '', 'SERVER_ADDR': 'source.example'}
            settings.write_env(values)
            archive = io.BytesIO()
            with tarfile.open(fileobj=archive, mode='w') as tar:
                backup.add_portable_server_env(tar, 'server.env', box.paths.server_env)
            archive.seek(0)
            with tarfile.open(fileobj=archive, mode='r') as tar:
                info = tar.getmember('server.env')
                self.assertEqual(info.mode, 0o640)
                payload = tar.extractfile(info).read()
            restored = box.root / 'restore'
            restored.mkdir()
            (restored / 'server.env').write_bytes(payload)
            settings.write_env({'SERVER_ADDR': 'target.example', 'MANAGER_PYTHON': '/usr/bin/python3.12'})
            self.assertTrue(backup.copy_server_env_if_exists(restored, 'server.env', box.paths.server_env))
            self.assertEqual(settings.read_env(), {
                'CUSTOM': values['CUSTOM'], 'EMPTY': '', 'SERVER_ADDR': 'target.example',
                'MANAGER_PYTHON': '/usr/bin/python3.12',
            })
