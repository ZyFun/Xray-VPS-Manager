"""Известные литералы внутри функций: контроль до переноса на paths()."""
import ast
from collections import Counter
import json
from pathlib import Path
import unittest


TESTS = Path(__file__).resolve().parent
PACKAGE = TESTS.parent/'xray_vps_manager'


def absolute_function_literals():
    found = Counter()

    class Visitor(ast.NodeVisitor):
        def __init__(self, module):
            self.module = module
            self.scope = []
            self.in_function = 0

        def visit_ClassDef(self, node):
            self.scope.append(node.name)
            self.generic_visit(node)
            self.scope.pop()

        def visit_FunctionDef(self, node):
            self.scope.append(node.name)
            self.in_function += 1
            self.generic_visit(node)
            self.in_function -= 1
            self.scope.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Constant(self, node):
            # Консервативно учитываем и разделители/URL/Telegram-команды:
            # новый абсолютный путь нельзя скрыть эвристикой по его имени.
            if self.in_function and isinstance(node.value, str) and node.value.startswith('/'):
                found[(self.module, '.'.join(self.scope), node.value)] += 1

    for path in sorted(PACKAGE.rglob('*.py')):
        relative = path.relative_to(PACKAGE).as_posix()
        if relative == 'core/paths.py':
            continue  # Канонические пути определяются именно здесь.
        Visitor(relative).visit(ast.parse(path.read_text(), filename=str(path)))
    return [list(key)+[count] for key, count in sorted(found.items())]


class SandboxPathInventoryTests(unittest.TestCase):
    def test_function_literals_match_explicit_inventory(self):
        expected = json.loads((TESTS/'fixtures/paths/legacy-function-literals.json').read_text())
        self.assertEqual(absolute_function_literals(), expected,
                         'Проверьте новые/удалённые абсолютные литералы и инвентарь ограничений Sandbox')
