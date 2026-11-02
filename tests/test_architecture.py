"""Архитектурные ограничения: импорты R1.0 и отдельный baseline маркеров R1.5."""

import ast
from collections import Counter
import json
from importlib.util import resolve_name
from pathlib import Path
import sys
import unittest


PACKAGE = "xray_vps_manager"
PACKAGE_ROOT = Path(__file__).resolve().parents[1] / PACKAGE
AREA_ORDER = (
    "metadata", "core", "db", "xray", "caddy", "clients", "traffic",
    "activity", "telegram", "routing", "system", "interfaces",
)
# Исходный список R1.0; следующие задачи только удаляют устранённые пары.
# Идентичность исключения — модуль-источник и модуль-цель, без номеров строк.
ALLOWED_IMPORTS = frozenset({
    # Нормализация и константы ещё находятся в областях clients/activity.
    ("db.repositories.activity", "activity.constants"),
    ("db.repositories.clients", "clients.credentials"),
    ("db.repositories.clients", "clients.payments"),
    # Чтение клиентских данных и разбор email ещё не перенесены из xray.
    ("xray.client_routes", "clients.credentials"),
    ("xray.client_routes", "clients.models"),
    ("xray.client_routes", "clients.repository"),
    ("xray.config", "clients.models"),
    # Клиентские операции пока читают трафик до его отдельного шага миграции.
    ("clients.crud", "traffic.repository"),
    ("clients.limits", "traffic.formatting"),
    ("clients.limits", "traffic.repository"),
    ("clients.runtime", "traffic.repository"),
    ("clients.status", "traffic.repository"),
})


def module_name(path: Path) -> str:
    """Путь относительно корня пакета; __init__.py представляет сам пакет."""
    parts = path.with_suffix("").parts
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join((PACKAGE, *parts))


def module_area(module: str):
    if module == PACKAGE:
        return "metadata"  # Корневой __init__.py содержит только версию.
    if not module.startswith(PACKAGE + "."):
        return None
    relative = module[len(PACKAGE) + 1:]
    # Существующие корневые точки входа сохраняются по плану миграции.
    if relative in {"cli", "runner"} or relative == "telegram.bot" or relative.startswith("telegram.bot."):
        return "interfaces"
    area = relative.split(".")[0]
    if area == "commands":
        return "interfaces"
    if area not in AREA_ORDER or area in {"metadata", "interfaces"}:
        raise ValueError("Неизвестная область пакета: " + module)
    return area


def find_import_violations(sources: dict[Path, str]) -> set[tuple[str, str]]:
    """Проверяет Import/ImportFrom через AST, не исполняя код проверяемых модулей."""
    modules = {module_name(path): (path, code) for path, code in sources.items()}
    known_modules = set()
    for module in modules:
        parts = module.split(".")
        known_modules.update(".".join(parts[:length]) for length in range(1, len(parts) + 1))

    violations = set()
    for module, (path, code) in modules.items():
        source_area = module_area(module)
        package = module if path.name == "__init__.py" else module.rpartition(".")[0]
        for node in ast.walk(ast.parse(code, filename=str(path))):
            if isinstance(node, ast.Import):
                targets = {alias.name for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    base = resolve_name("." * node.level + base, package)
                # Импорт функции/константы относится к её модулю, импорт модуля — к нему самому.
                targets = {
                    base + "." + alias.name if base + "." + alias.name in known_modules else base
                    for alias in node.names
                }
            else:
                continue
            for target in targets:
                target_area = module_area(target)
                if target_area is None:
                    continue
                to_the_right = AREA_ORDER.index(target_area) > AREA_ORDER.index(source_area)
                # Таблица архитектуры строже общего порядка для этих двух областей.
                forbidden_db = source_area in {"xray", "caddy"} and target_area == "db"
                # Корень с версией тоже находится вне разрешённых зависимостей инфраструктуры.
                forbidden_root = source_area in {"core", "db", "xray", "caddy"} and target_area == "metadata"
                if to_the_right or forbidden_db or forbidden_root:
                    violations.add((module.removeprefix(PACKAGE + "."), target.removeprefix(PACKAGE + ".")))
    return violations



# Согласованный R1.5: исключения относятся к роли, а не ко всему пакету области.
INTERFACE_MODULES = frozenset({'cli', 'runner', 'telegram.admin', 'telegram.poller'})
INFRASTRUCTURE_MODULES = frozenset({'xray.apply', 'xray.crypto', 'xray.caddy',
    'telegram.api', 'system.units', 'traffic.stats', 'activity.raw_logs'})
SAFE_SUBPROCESS_NAMES = frozenset({'subprocess.CalledProcessError',
    'subprocess.TimeoutExpired', 'subprocess.CompletedProcess'})
MARKER_BASELINE = Path(__file__).parent / 'fixtures' / 'architecture-markers.json'


def is_interface(module):
    return (module in INTERFACE_MODULES or module in {'commands', 'telegram.bot'}
            or module.startswith(('commands.', 'telegram.bot.')))


def is_infrastructure(module):
    return (module in INFRASTRUCTURE_MODULES or module in {'core', 'db'}
            or module.startswith(('core.', 'db.'))
            or module.startswith('caddy.') and module != 'caddy.model')


def find_marker_violations(sources):
    """Точные AST-вхождения; номера строк не входят в идентичность исключения.

    Импорты с alias учитываются. Строки кода не исполняются; динамическая
    диспетчеризация не является способом обойти архитектурные правила.
    """
    # С 3.13 ast.dump по умолчанию опускает пустые списки; baseline хранит их.
    dump_options = {'show_empty': True} if sys.version_info >= (3, 13) else {}
    violations = Counter()
    for path, code in sources.items():
        module = module_name(path).removeprefix(PACKAGE + '.')
        tree = ast.parse(code, filename=str(path))
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        aliases = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                aliases.update((a.asname or a.name.split('.')[0], a.name) for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                aliases.update((a.asname or a.name, node.module + '.' + a.name) for a in node.names)

        def resolved(node):
            if isinstance(node, ast.Name):
                return aliases.get(node.id, node.id)
            if isinstance(node, ast.Attribute):
                return resolved(node.value) + '.' + node.attr
            return ''

        def add(node, marker):
            scope, parent = [], node
            while parent in parents:
                parent = parents[parent]
                if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    scope.append(parent.name)
            key = (module, '.'.join(reversed(scope)) or '<module>', marker,
                   ast.dump(node, include_attributes=False, **dump_options))
            violations[key] += 1

        references = [node for node in ast.walk(tree)
                      if isinstance(node, (ast.Name, ast.Attribute)) and isinstance(node.ctx, ast.Load)
                      and not (isinstance(parents.get(node), ast.Attribute) and parents[node].value is node)]
        reference_set = set(references)
        subprocess_refs = [resolved(node) for node in references if resolved(node).split('.')[0] == 'subprocess']
        safe_subprocess_import = bool(subprocess_refs) and all(name in SAFE_SUBPROCESS_NAMES for name in subprocess_refs)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                called = resolved(node.func)
                if not is_interface(module):
                    for marker in ('print', 'input', 'sys.exit', 'SystemExit'):
                        if module == 'core.terminal' and marker == 'print':
                            continue
                        if called in (marker, 'builtins.' + marker):
                            add(node, marker)
                if not is_infrastructure(module) and (called in {'open', 'builtins.open', 'io.open'}
                        or isinstance(node.func, ast.Attribute) and node.func.attr == 'open'):
                    add(node, 'open')
            elif isinstance(node, ast.Raise) and isinstance(node.exc, ast.Name):
                if resolved(node.exc) in {'SystemExit', 'builtins.SystemExit'} and not is_interface(module):
                    add(node, 'SystemExit')
            if not is_infrastructure(module):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or '']
                    for marker in {name.split('.')[0] for name in names} & {'subprocess', 'sqlite3'}:
                        if (marker == 'subprocess' and safe_subprocess_import
                                and (isinstance(node, ast.Import) or all(
                                    'subprocess.' + a.name in SAFE_SUBPROCESS_NAMES for a in node.names))):
                            continue
                        add(node, marker)
                elif node in reference_set:
                    name = resolved(node)
                    marker = name.split('.')[0]
                    if marker in {'subprocess', 'sqlite3'} and name not in SAFE_SUBPROCESS_NAMES:
                        add(node, marker)
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and '/usr/local/' in node.value and module != 'core.paths':
                parent = parents.get(node)
                owner = parents.get(parent)
                # Только настоящий docstring; прочие строковые выражения остаются в проверке.
                if (isinstance(parent, ast.Expr) and isinstance(owner, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                        and owner.body and owner.body[0] is parent):
                    continue
                add(node, '/usr/local/')
    return violations


class MarkerCheckerTests(unittest.TestCase):
    def check(self, module, code):
        return find_marker_violations({Path(module.replace('.', '/') + '.py'): code})

    def markers(self, module, code):
        return Counter({marker: sum(n for key, n in self.check(module, code).items() if key[2] == marker)
                        for marker in {key[2] for key in self.check(module, code)}})

    def test_interface_and_terminal_exceptions_do_not_hide_file_io(self):
        code = 'print("x"); input(); sys.exit(1); raise SystemExit(2)'
        for module in ('commands.example', 'runner', 'cli', 'telegram.admin', 'telegram.poller', 'telegram.bot.handler'):
            self.assertFalse(self.check(module, code), module)
        self.assertEqual(self.markers('core.terminal', code), Counter({'input': 1, 'sys.exit': 1, 'SystemExit': 1}))
        self.assertEqual(self.markers('clients.service', code), Counter({'print': 1, 'input': 1, 'sys.exit': 1, 'SystemExit': 1}))
        self.assertEqual(self.markers('commands.example', 'open("x"); path.open("r")'), Counter({'open': 2}))

    def test_infrastructure_roles_are_explicit(self):
        code = 'import subprocess, sqlite3\nsubprocess.run([]); sqlite3.connect("x"); open("x")'
        for module in ('core.process', 'db.database', 'xray.apply', 'xray.crypto', 'xray.caddy', 'caddy.service', 'telegram.api', 'system.units', 'traffic.stats', 'activity.raw_logs'):
            self.assertFalse(self.check(module, code), module)
        for module in ('clients.service', 'xray.config', 'caddy.model', 'system.unit_sync', 'commands.client'):
            self.assertEqual(self.markers(module, code), Counter({'subprocess': 2, 'sqlite3': 2, 'open': 1}), module)

    def test_exception_and_result_references_are_allowed_but_operations_are_not(self):
        code = 'import subprocess as sp\ndef run(result: sp.CompletedProcess):\n try: return result\n except (sp.CalledProcessError, sp.TimeoutExpired): pass'
        self.assertFalse(self.check('system.unit_sync', code))
        self.assertFalse(self.check('commands.example', 'from subprocess import CompletedProcess as Result\nresult = Result([], 0)'))
        found = self.check('commands.example', code + '\nsp.run([])')
        self.assertEqual(sum(found.values()), 2)
        self.assertEqual(sum(self.check('commands.example', 'from subprocess import run as execute\nexecute([])').values()), 2)
        self.assertEqual(sum(self.check('commands.example', 'from subprocess import run, CompletedProcess\nresult: CompletedProcess').values()), 1)

    def test_paths_ignore_only_canonical_module_and_docstrings(self):
        code = '"""Описание /usr/local/bin/tool"""\np = "/usr/local/bin/tool"'
        self.assertFalse(self.check('core.paths', code))
        self.assertEqual(self.markers('core.fs', code), Counter({'/usr/local/': 1}))
        self.assertEqual(self.markers('system.units', 'units = {"service": "ExecStart=/usr/local/bin/tool"}'), Counter({'/usr/local/': 1}))

    def test_identity_is_stable_but_new_occurrences_are_detected(self):
        first = self.check('clients.service', 'def action():\n open("x")')
        moved = self.check('clients.service', '\n# Комментарий\ndef action():\n open("x")')
        self.assertEqual(first, moved)
        doubled = self.check('clients.service', 'def action():\n open("x")\n open("x")')
        self.assertEqual(sum((doubled - first).values()), 1)
        self.assertNotEqual(first, self.check('clients.service', 'def other():\n open("x")'))

    def test_identity_keeps_empty_fields_across_python_versions(self):
        empty_call = "Call(func=Name(id='print', ctx=Load()), args=[], keywords=[])"
        file_call = "Call(func=Name(id='open', ctx=Load()), args=[Constant(value='x')], keywords=[])"
        nested_call = "Call(func=Name(id='print', ctx=Load()), args=[" + file_call + "], keywords=[])"
        self.assertEqual(self.check('clients.service', 'def action():\n print()\n print(open("x"))'), Counter({
            ('clients.service', 'action', 'print', empty_call): 1,
            ('clients.service', 'action', 'print', nested_call): 1,
            ('clients.service', 'action', 'open', file_call): 1,
        }))

    def test_nested_calls_aliases_and_bare_system_exit_are_checked(self):
        self.assertEqual(self.markers('clients.service', 'from builtins import print as emit\nfrom sys import exit as stop\ndef action():\n emit("x"); stop(1)\n raise SystemExit'), Counter({'print': 1, 'sys.exit': 1, 'SystemExit': 1}))


class ImportCheckerTests(unittest.TestCase):
    def check(self, source, code, *, package=False):
        # Пустые модули различают `from package import module` и импорт атрибута.
        modules = (
            "core.paths", "core.fs", "db.database", "xray.config", "caddy.service",
            "clients.models", "clients.credentials", "traffic.repository", "activity.time",
            "telegram.api", "telegram.admin", "telegram.poller", "telegram.bot.dialogs",
            "routing.cascade", "system.backups", "commands.client", "cli", "runner",
        )
        sources = {Path(name.replace(".", "/") + ".py"): "" for name in modules}
        path = Path(source.replace(".", "/"))
        path = path / "__init__.py" if package else path.with_suffix(".py")
        sources[path] = code
        return find_import_violations(sources)

    def test_internal_core_imports_pass_and_external_imports_are_outside_scope(self):
        # R1.0 проверяет только связи внутри xray_vps_manager.
        self.assertEqual(self.check("core.check", """
import json, pathlib
import external_package
import xray_vps_manager_extra.clients
from . import paths
from xray_vps_manager.core.fs import atomic_write
"""), set())

    def test_infrastructure_cannot_import_the_package_root(self):
        for area in ("core", "db", "xray", "caddy"):
            source = area + ".check"
            for code in (
                "import xray_vps_manager as manager",
                "from xray_vps_manager import __version__",
                "from .. import __version__",
            ):
                with self.subTest(source=source, code=code):
                    self.assertEqual(self.check(source, code), {(source, "xray_vps_manager")})

    def test_interfaces_can_import_the_package_root(self):
        for source, code in (
            ("commands.check", "import xray_vps_manager as manager"),
            ("commands.check", "from .. import __version__"),
            ("telegram.bot.check", "from ... import __version__"),
            ("cli", "from . import __version__"),
            ("runner", "from xray_vps_manager import __version__"),
        ):
            with self.subTest(source=source, code=code):
                self.assertEqual(self.check(source, code), set())

    def test_import_and_from_forms_preserve_all_targets_and_aliases(self):
        code = """
import xray_vps_manager.clients.models as models, xray_vps_manager.traffic.repository
from xray_vps_manager import commands as cli_commands
from xray_vps_manager.clients import credentials as creds, models
from xray_vps_manager.clients.models import Client
"""
        self.assertEqual(self.check("core.check", code), {
            ("core.check", "clients.models"),
            ("core.check", "traffic.repository"),
            ("core.check", "commands"),
            ("core.check", "clients.credentials"),
        })

    def test_each_package_boundary_rejects_an_import_to_the_right(self):
        cases = (
            ("core.check", "db.database"),
            ("db.check", "xray.config"),
            ("xray.check", "caddy.service"),
            ("caddy.check", "clients.models"),
            ("clients.check", "traffic.repository"),
            ("traffic.check", "activity.time"),
            ("activity.check", "telegram.api"),
            ("telegram.check", "routing.cascade"),
            ("routing.check", "system.backups"),
            ("system.check", "commands.client"),
            ("system.check", "telegram.bot.dialogs"),
        )
        for source, target in cases:
            with self.subTest(source=source, target=target):
                self.assertEqual(self.check(source, "import xray_vps_manager." + target), {(source, target)})

    def test_imports_to_the_left_and_within_an_area_are_allowed(self):
        cases = (
            ("db.check", "core.paths"), ("xray.check", "core.fs"),
            ("caddy.check", "xray.config"), ("clients.check", "caddy.service"),
            ("traffic.check", "clients.models"), ("activity.check", "traffic.repository"),
            ("telegram.check", "activity.time"), ("routing.check", "telegram.api"),
            ("system.check", "routing.cascade"), ("commands.check", "system.backups"),
            ("clients.check", "clients.credentials"),
        )
        for source, target in cases:
            with self.subTest(source=source, target=target):
                self.assertEqual(self.check(source, "import xray_vps_manager." + target), set())

    def test_xray_and_caddy_cannot_import_db_despite_linear_order(self):
        for source in ("xray.check", "caddy.check"):
            with self.subTest(source=source):
                self.assertEqual(self.check(source, "from xray_vps_manager.db import database"), {
                    (source, "db.database"),
                })

    def test_nested_and_conditional_imports_are_checked(self):
        code = """
def execute():
    import xray_vps_manager.clients.models
    def nested():
        from ..activity.time import parse_time
if TYPE_CHECKING:
    from xray_vps_manager.commands import client
"""
        self.assertEqual(self.check("core.check", code), {
            ("core.check", "clients.models"),
            ("core.check", "activity.time"),
            ("core.check", "commands.client"),
        })

    def test_relative_imports_resolve_from_module_package(self):
        code = """
from ...clients import credentials as creds
from ... import traffic
from ...traffic.repository import load_db
"""
        self.assertEqual(self.check("db.repositories.check", code), {
            ("db.repositories.check", "clients.credentials"),
            ("db.repositories.check", "traffic"),
            ("db.repositories.check", "traffic.repository"),
        })

    def test_init_relative_imports_resolve_from_package_itself(self):
        self.assertEqual(self.check("db.repositories", "from ...clients import credentials", package=True), {
            ("db.repositories", "clients.credentials"),
        })
        self.assertEqual(self.check("core", "from . import paths", package=True), set())

    def test_star_imports_still_check_the_target_area(self):
        self.assertEqual(self.check("core.check", "from xray_vps_manager.clients.models import *"), {
            ("core.check", "clients.models"),
        })

    def test_bot_and_commands_are_interfaces_in_both_directions(self):
        self.assertEqual(self.check("commands.check", "from xray_vps_manager.telegram.bot import dialogs"), set())
        self.assertEqual(self.check("telegram.bot.check", "from xray_vps_manager.commands import client"), set())
        self.assertEqual(self.check("telegram.check", "from .bot import dialogs"), {
            ("telegram.check", "telegram.bot.dialogs"),
        })

    def test_admin_and_poller_have_no_implicit_interface_exception(self):
        for source in ("telegram.admin", "telegram.poller"):
            with self.subTest(source=source):
                self.assertEqual(self.check(source, "from xray_vps_manager.system import backups"), {
                    (source, "system.backups"),
                })

    def test_cli_and_runner_are_explicit_interface_entrypoints(self):
        self.assertEqual(self.check("cli", "from .runner import run_command"), set())
        self.assertEqual(self.check("runner", "from .commands import client"), set())
        self.assertEqual(self.check("core.check", "from xray_vps_manager import runner"), {
            ("core.check", "runner"),
        })

    def test_unknown_areas_fail_instead_of_silently_skipping_imports(self):
        with self.assertRaisesRegex(ValueError, "future"):
            self.check("core.check", "import xray_vps_manager.future.module")
        with self.assertRaisesRegex(ValueError, "future"):
            self.check("future.check", "import xray_vps_manager.core.paths")

    def test_strings_and_dynamic_import_calls_are_outside_static_check(self):
        self.assertEqual(self.check("core.check", """
# import xray_vps_manager.commands.client
text = 'from xray_vps_manager.clients import models'
import_module('xray_vps_manager.commands.client')
"""), set())

    def test_violation_identity_survives_line_and_import_form_changes(self):
        for code in (
            "from xray_vps_manager.clients import credentials",
            "\n\n# Другое положение строки.\ndef execute():\n    from ...clients import credentials as creds\n",
        ):
            with self.subTest(code=code):
                violations = self.check("db.repositories.clients", code)
                self.assertEqual(violations, {("db.repositories.clients", "clients.credentials")})

    def test_new_target_from_the_same_source_is_a_separate_violation(self):
        violations = self.check("db.repositories.clients", """
from ...clients import credentials, models
""")
        self.assertEqual(violations, {
            ("db.repositories.clients", "clients.credentials"),
            ("db.repositories.clients", "clients.models"),
        })


class PackageArchitectureTests(unittest.TestCase):
    def test_package_matches_separate_marker_baseline(self):
        sources = {path.relative_to(PACKAGE_ROOT): path.read_text(encoding='utf-8')
                   for path in sorted(PACKAGE_ROOT.rglob('*.py'))}
        expected = Counter({(row['module'], row['scope'], row['category'], row['ast']): row['count']
                            for row in json.loads(MARKER_BASELINE.read_text())})
        actual = find_marker_violations(sources)
        new, obsolete = actual - expected, expected - actual
        self.assertFalse(new or obsolete, 'Новые маркеры: ' + repr(new) + '\nУдалите устранённые: ' + repr(obsolete))

    def test_current_package_matches_the_explicit_allow_list(self):
        sources = {path.relative_to(PACKAGE_ROOT): path.read_text(encoding="utf-8")
                   for path in sorted(PACKAGE_ROOT.rglob("*.py"))}
        self.assertIn(Path("__init__.py"), sources)
        violations = find_import_violations(sources)
        new = sorted(violations - ALLOWED_IMPORTS)
        obsolete = sorted(ALLOWED_IMPORTS - violations)
        self.assertFalse(new or obsolete, "\n".join([
            "Новые запрещённые импорты: " + repr(new),
            "Удалите устранённые пары из allow-list: " + repr(obsolete),
        ]))


if __name__ == "__main__":
    unittest.main()
