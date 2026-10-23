"""R1.0 / VIREI-101: статические импорты между областями, без проверок ввода-вывода R1.5."""

import ast
from importlib.util import resolve_name
from pathlib import Path
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
    ("db.repositories.activity", "activity.time"),
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
